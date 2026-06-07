"""P2Pearl daemon — the orchestrator that wires the components into a mining node.

``PoolNode`` connects: the parent-chain template (pearld GBT) + the sharechain +
PPLNS + the coinbase builder + share/block verification + the stratum server +
(optionally) the P2P layer. Dependencies are INJECTED, so the orchestration is
unit-tested with fakes (no pearld / pearl_mining / bitcoinutils needed);
``build_production_node`` wires the real implementations for a live deployment.

Per-miner jobs: each share's coinbase pays the PPLNS window AND commits a share
crediting the finder, so every miner needs its OWN job (its own coinbase -> its own
merkle root -> its own header). The stratum server's ``set_job_builder`` hook calls
``PoolNode.build_job_for(worker_address)`` per connection.

Flow:
  build_job_for(addr): sidechain tip -> PPLNS payouts -> coinbase(payouts +
    OP_RETURN<candidate share id>) -> incomplete header -> (header, share_target).
  handle_submit(sub): verify_share at the SHARE target -> sharechain.add_share ->
    gossip; if it ALSO clears the BLOCK target (the UNMODIFIED verifier) -> assemble
    + submitblock + gossip the block -> refresh every miner's job.
"""

from __future__ import annotations

import asyncio
import struct
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import __version__, config
from .consensus.difficulty import target_to_bits
from .consensus.pplns import Payout, compute_pplns_payouts
from .consensus.share import ShareBlock, double_sha256
from .consensus.sharechain import GENESIS_PREV, Sharechain
from .stratum import protocol as P
from .stratum.server import StratumServer, Submission, SubmitResult


@dataclass
class ParentTemplate:
    """The fields of a pearld getblocktemplate response that P2Pearl needs."""

    height: int
    prev_block: bytes                  # 32 bytes (as returned by GBT previousblockhash)
    bits: int                          # compact block nbits (u32)
    curtime: int                       # unix seconds
    coinbase_value: int                # subsidy + fees, in grains
    version: int = 0x20000000
    witness_commitment: str | None = None        # GBT default_witness_commitment (hex)
    coinbaseaux_flags: str | None = None          # GBT coinbaseaux.flags (hex)
    transactions: list = field(default_factory=list)  # raw template txs (opaque to the orchestrator)

    @classmethod
    def from_gbt(cls, gbt: dict) -> "ParentTemplate":
        bits = gbt["bits"]
        aux = gbt.get("coinbaseaux") or {}
        return cls(
            height=int(gbt["height"]),
            prev_block=bytes.fromhex(gbt["previousblockhash"]),
            bits=int(bits, 16) if isinstance(bits, str) else int(bits),
            curtime=int(gbt["curtime"]),
            coinbase_value=int(gbt["coinbasevalue"]),
            version=int(gbt.get("version", 0x20000000)),
            witness_commitment=gbt.get("default_witness_commitment"),
            coinbaseaux_flags=aux.get("flags"),
            transactions=list(gbt.get("transactions", [])),
        )


def serialize_payouts(payouts: list[Payout]) -> bytes:
    """Deterministic encoding of the PPLNS output set, hashed into payout_set_hash."""
    out = bytearray(struct.pack("<I", len(payouts)))
    for p in payouts:
        addr = p.address.encode("ascii")
        out += struct.pack("<H", len(addr)) + addr + struct.pack("<Q", p.grains)
    return bytes(out)


# Injected adapter signatures.
MakeHeader = Callable[["ParentTemplate", "list[Payout]", bytes], "tuple[str, Any]"]
AssembleBlock = Callable[[Any, str], str]
VerifyShare = Callable[[bytes, str, int], bool]
VerifyBlock = Callable[[bytes, str], bool]
SubmitBlock = Callable[[str], Awaitable[Any]]
BroadcastShare = Callable[[ShareBlock, str], Awaitable[None]]
BroadcastBlock = Callable[[str], Awaitable[None]]


@dataclass
class _JobContext:
    candidate: ShareBlock
    header_ctx: Any
    payouts: list


class PoolNode:
    def __init__(
        self,
        *,
        sharechain: Sharechain,
        share_target: int,
        make_header: MakeHeader,
        verify_share: VerifyShare,
        verify_block: VerifyBlock,
        assemble_block: AssembleBlock,
        submit_block: SubmitBlock,
        stratum: StratumServer | None = None,
        broadcast_share: BroadcastShare | None = None,
        broadcast_block: BroadcastBlock | None = None,
        min_payout_grains: int = config.MIN_PAYOUT_GRAINS,
    ) -> None:
        self.sharechain = sharechain
        self.share_target = share_target
        self._make_header = make_header
        self._verify_share = verify_share
        self._verify_block = verify_block
        self._assemble_block = assemble_block
        self._submit_block = submit_block
        self.stratum = stratum
        self._broadcast_share = broadcast_share
        self._broadcast_block = broadcast_block
        self._min_payout = min_payout_grains
        self._template: ParentTemplate | None = None
        if stratum is not None:
            stratum.set_job_builder(self.build_job_for)

    def set_template(self, template: ParentTemplate) -> None:
        self._template = template

    # --- job building (called per-connection by the stratum) ---------------- #
    def build_job_for(self, worker_address: str | None):
        """Build this miner's job: a candidate share + the header that commits it.

        Returns ``(incomplete_header_hex, share_target, height, _JobContext)`` for the
        stratum to mint, or ``None`` if there is no template / address yet.
        """
        template = self._template
        if template is None or not worker_address:
            return None

        tip = self.sharechain.tip()
        prev_share_id = tip.share_id() if tip is not None else GENESIS_PREV
        s_height = (tip.sidechain_height + 1) if tip is not None else 0

        weights = self.sharechain.pplns_weights()
        payouts = compute_pplns_payouts(template.coinbase_value, weights, self._min_payout)
        payout_set_hash = double_sha256(serialize_payouts(payouts))

        candidate = ShareBlock(
            version=config.SIDECHAIN_VERSION,
            sidechain_height=s_height,
            prev_share_id=prev_share_id,
            parent_prev_block=template.prev_block,
            parent_height=template.height,
            timestamp=template.curtime,
            share_target=self.share_target,
            block_nbits=template.bits,
            coinbase_version=template.version,
            coinbase_value=template.coinbase_value,
            miner_address=worker_address,
            payout_set_hash=payout_set_hash,
        )
        header_hex, header_ctx = self._make_header(template, payouts, candidate.share_id())
        ctx = _JobContext(candidate=candidate, header_ctx=header_ctx, payouts=payouts)
        return (header_hex, self.share_target, s_height, ctx)

    # --- submit handling (the stratum submit_handler) ----------------------- #
    async def handle_submit(self, submission: Submission) -> SubmitResult:
        ctx = submission.job.context
        if not isinstance(ctx, _JobContext):
            return SubmitResult(False, P.INVALID_PARAMS_CODE, "job has no candidate share")
        header_bytes = bytes.fromhex(submission.job.incomplete_header_hex)
        share_nbits = target_to_bits(submission.job.share_target)

        # 1. Cheap share-target verification (nbits override).
        if not self._verify_share(header_bytes, submission.plain_proof_b64, share_nbits):
            return SubmitResult(False, P.LOW_DIFF_CODE, "share does not meet target")

        # 2. Record the share on the sidechain (PoW already verified above).
        proof_bytes = submission.plain_proof_b64.encode("ascii")
        added = self.sharechain.add_share(ctx.candidate, verified=True, proof=proof_bytes)
        if not added.accepted:
            return SubmitResult(False, P.STALE_SHARE_CODE, added.reason or "share rejected")

        # 3. Gossip the share to peers.
        if self._broadcast_share is not None:
            await self._broadcast_share(ctx.candidate, submission.plain_proof_b64)

        # 4. Block-found path: confirm at the BLOCK target with the UNMODIFIED verifier,
        #    then assemble the full Pearl block, submit it, and gossip it.
        if self._verify_block(header_bytes, submission.plain_proof_b64):
            block_hex = self._assemble_block(ctx.header_ctx, submission.plain_proof_b64)
            await self._submit_block(block_hex)
            if self._broadcast_block is not None:
                await self._broadcast_block(block_hex)

        # 5. New sidechain tip -> rebuild every miner's job (new prev_share + PPLNS).
        if self.stratum is not None:
            await self.stratum.refresh()
        return SubmitResult(True)

    # --- production poll loop ---------------------------------------------- #
    async def run(self, node: Any, poll_interval: float = 2.0) -> None:
        """Poll ``node`` for new parent tips and refresh jobs. Tests drive
        ``set_template`` / ``handle_submit`` directly instead of running this."""
        last_prev: bytes | None = None
        while True:
            result = node.get_block_template()
            gbt = await result if asyncio.iscoroutine(result) else result
            template = ParentTemplate.from_gbt(gbt)
            if template.prev_block != last_prev:
                last_prev = template.prev_block
                self.set_template(template)
                if self.stratum is not None:
                    await self.stratum.refresh()
            await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------- #
# Production wiring. These adapters need bitcoinutils + pearl_mining + the Pearl
# gateway on the path and a Linux-built py-pearl-mining; they are NOT exercised by
# the unit tests. Header/coinbase byte orientation MUST be validated on testnet
# (see integration/stratum-dialect.md) before mainnet use.
# ---------------------------------------------------------------------------- #

def _build_share_header(version, prev_block, timestamp, nbits, payouts, share_id,
                        coinbase_value, parent_height):
    """Deterministically build the coinbase-only header a share's PoW commits to.

    Used by BOTH the finder (``_production_make_header``) and a verifying peer
    (``verify_incoming``) so reconstruction is byte-identical: coinbase-only (no
    mempool txs), no coinbaseaux flags, fixed extranonce, and the empty-block witness
    commitment computed deterministically — ``double_sha256(64 zero bytes)``, the value
    pearld's GBT returns for an empty mempool. Returns ``(IncompleteBlockHeader,
    coinbase_tx)``. REQUIRES bitcoinutils + pearl_mining.
    """
    from .chain.coinbase import assemble_coinbase_tx, build_coinbase_outputs

    import pearl_mining  # type: ignore

    outputs = build_coinbase_outputs(payouts, share_id, coinbase_value)
    coinbase_tx = assemble_coinbase_tx(
        outputs, parent_height, coinbase_aux=None,
        default_witness_commitment=double_sha256(b"\x00" * 64).hex())
    merkle_root = _tx_merkle_root([coinbase_tx.get_txid()])
    header = pearl_mining.IncompleteBlockHeader(version, prev_block, merkle_root, timestamp, nbits)
    return header, coinbase_tx


def _production_make_header(template: ParentTemplate, payouts: list[Payout], share_id: bytes):
    """Build a candidate share's coinbase-only header + coinbase via the shared
    deterministic builder (so peers reconstruct it identically). REQUIRES bitcoinutils
    + pearl_mining."""
    header, coinbase_tx = _build_share_header(
        template.version, template.prev_block, template.curtime, template.bits,
        payouts, share_id, template.coinbase_value, template.height)
    header_ctx = {"header": header, "coinbase_tx": coinbase_tx, "transactions": []}
    return bytes(header.to_bytes()).hex(), header_ctx


def _production_assemble_block(header_ctx: dict, plain_proof_b64: str) -> str:
    """Generate the ZK certificate and serialize the full Pearl block. REQUIRES
    pearl_mining + the Pearl gateway (PearlBlock / ZKCertificate)."""
    import pearl_mining  # type: ignore
    from pearl_gateway.blockchain_utils.pearl_block import PearlBlock  # type: ignore
    from pearl_gateway.blockchain_utils.pearl_header import PearlHeader  # type: ignore
    from pearl_gateway.blockchain_utils.zk_certificate import ZKCertificate  # type: ignore

    proof = pearl_mining.PlainProof.from_base64(plain_proof_b64)
    incomplete = header_ctx["header"]
    zk_proof = pearl_mining.generate_proof(incomplete, proof)
    pearl_header = PearlHeader(incomplete)
    zk_cert = ZKCertificate.from_pearl_header(pearl_header, zk_proof)
    # PearlBlock serializes raw_txns as bytes (zk_cert | header | count | txs). The
    # coinbase is serialized WITH its witness; template txs arrive as raw GBT hex.
    coinbase_tx = header_ctx["coinbase_tx"]
    raw_txns = [coinbase_tx.to_bytes(getattr(coinbase_tx, "has_segwit", False))] + [
        bytes.fromhex(t["data"]) for t in header_ctx["transactions"]
    ]
    return PearlBlock(pearl_header, raw_txns, zk_cert).serialize().hex()


def _tx_merkle_root(txids_hex: list[str]) -> bytes:
    """Bitcoin transaction merkle root from txids (display/big-endian hex)."""
    level = [bytes.fromhex(t)[::-1] for t in txids_hex]  # to little-endian
    if not level:
        return b"\x00" * 32
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [double_sha256(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0][::-1]  # back to big-endian


def _make_verify_incoming(sharechain: Sharechain, min_payout: int):
    """Build the production P2P ``verify_incoming(share, proof_b64) -> bool``.

    Trustless: recompute the deterministic PPLNS payouts as of the share's parent from
    OUR OWN sharechain, confirm the share commits to exactly that set (``payout_set_hash``
    — so a peer cannot forge the reward split), reconstruct the EXACT header the finder
    mined (the shared deterministic builder), and verify the proof at the share target.
    REQUIRES bitcoinutils + pearl_mining (lazy). The unit-tested P2P layer injects a fake.
    """
    from .pow.verify import verify_share

    def verify_incoming(share: ShareBlock, proof_b64: str) -> bool:
        weights = sharechain.pplns_weights(share.prev_share_id)
        payouts = compute_pplns_payouts(share.coinbase_value, weights, min_payout)
        if double_sha256(serialize_payouts(payouts)) != share.payout_set_hash:
            return False  # coinbase doesn't pay the deterministic PPLNS set
        header, _cb = _build_share_header(
            share.coinbase_version, share.parent_prev_block, share.timestamp,
            share.block_nbits, payouts, share.share_id(), share.coinbase_value,
            share.parent_height)
        return verify_share(bytes(header.to_bytes()), proof_b64, target_to_bits(share.share_target))

    return verify_incoming


def build_production_node(cfg: config.DaemonConfig | None = None, share_target: int | None = None) -> "tuple[PoolNode, Any]":
    """Wire a PoolNode with the real node RPC + verifiers + assembly adapters.

    ``share_target`` defaults to a placeholder that MUST be calibrated to live pool
    hashrate (sidechain difficulty = pool_hashrate * share_time)."""
    from .chain.node_rpc import NodeRPC
    from .p2p.node import P2PNode
    from .pow.verify import verify_block_solution, verify_share
    from .stratum.server import StratumServer

    cfg = cfg or config.DaemonConfig()
    node = NodeRPC(cfg.node)
    sharechain = Sharechain()
    if share_target is None:
        share_target = config.MAX_TARGET >> 24  # placeholder; calibrate to pool hashrate

    async def submit_block(block_hex: str):
        return await asyncio.to_thread(node.submit_block, block_hex)

    pool = PoolNode(
        sharechain=sharechain,
        share_target=share_target,
        make_header=_production_make_header,
        verify_share=verify_share,
        verify_block=verify_block_solution,
        assemble_block=_production_assemble_block,
        submit_block=submit_block,
    )
    # Miner-facing stratum: each connecting miner gets its OWN job (its own PPLNS
    # coinbase). build_job_for is the per-connection job source; handle_submit grades
    # the submitted share. ``serve`` runs the server alongside the GBT poll loop.
    stratum = StratumServer(pool.handle_submit, host=cfg.stratum_host, port=cfg.stratum_port)
    pool.stratum = stratum
    stratum.set_job_builder(pool.build_job_for)

    # P2P gossip: shares/blocks propagate to peers; an incoming share is VERIFIED by
    # reconstructing its header (the same deterministic build the finder used) before
    # it is added or relayed -> a peer can forge neither the PoW nor the PPLNS split.
    async def _on_new_share(_share):
        await stratum.refresh()                # a peer's share advanced the tip -> new jobs

    p2p = P2PNode(
        sharechain=sharechain,
        verify_incoming=_make_verify_incoming(sharechain, pool._min_payout),
        host=cfg.p2p_host, port=cfg.p2p_port, on_new_share=_on_new_share)
    pool._broadcast_share = p2p.broadcast_share
    pool._broadcast_block = p2p.broadcast_block
    pool.p2p = p2p
    return pool, node


async def _serve(pool: "PoolNode", node: Any, peers=()) -> None:
    """Run the stratum server, the P2P gossip layer, and the parent-chain poll loop.

    Primes the first template (fails fast + cleanly if pearld is unreachable), binds the
    stratum + P2P listeners, connects any configured peers, then serves until stopped.
    """
    gbt = await asyncio.to_thread(node.get_block_template)
    pool.set_template(ParentTemplate.from_gbt(gbt))
    await pool.stratum.start()
    if getattr(pool, "p2p", None) is not None:
        await pool.p2p.start()
        for host, port in peers:
            try:
                await pool.p2p.connect(host, int(port))
            except Exception as exc:
                print(f"  p2p peer    : could not connect {host}:{port}: {exc}", flush=True)
        print(f"  p2p         : {pool.p2p.host}:{pool.p2p.port}  ({pool.p2p.peer_count} peer(s) connected)", flush=True)
    s_host, s_port = pool.stratum.host, pool.stratum.port
    print(f"  stratum     : {s_host}:{s_port}  (point your miners here)", flush=True)
    print(f"  e.g.  SRBMiner-MULTI --algorithm pearlhash --pool {s_host}:{s_port} --wallet <prl1...> --disable-cpu", flush=True)
    print("  serving — Ctrl-C to stop", flush=True)
    await asyncio.gather(pool.run(node), pool.stratum.serve_forever())


def main(cfg: "config.DaemonConfig | None" = None, share_target: int | None = None) -> int:
    cfg = cfg or config.DaemonConfig()
    print(f"P2Pearl v{__version__} daemon")
    try:
        pool, node = build_production_node(cfg, share_target)
    except Exception as exc:  # pragma: no cover - needs live deps
        print(f"could not wire production node: {exc}")
        print("A live node needs a running pearld + a Linux-built pearl_mining + bitcoinutils.")
        print("See ROADMAP.md and integration/.")
        return 1
    print(f"  parent node : {node._cfg.url if hasattr(node, '_cfg') else '?'}")
    try:
        asyncio.run(_serve(pool, node, cfg.peers))
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    except Exception as exc:  # pragma: no cover - needs a live pearld
        print(f"\n  could not reach pearld at {node._cfg.url}: {exc}")
        print("  P2Pearl needs a running Pearl node + a Linux-built pearl_mining to run live.")
        print("  The test suite and 'p2pearl demo' run with no node. See ROADMAP.md (M6).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
