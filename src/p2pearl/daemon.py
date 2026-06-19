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
  handle_submit(sub): sanity-check the job -> ACK the miner; post-ACK follow-up
    verifies the share target, adds it to the sharechain, gossips it, checks whether
    it also clears the BLOCK target, submits/gossips any block, and refreshes jobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from . import __version__, config
from .consensus.difficulty import target_to_bits
from .consensus.pplns import Payout, compute_pplns_payouts
from .consensus.share import ShareBlock, double_sha256
from .consensus.sharechain import GENESIS_PREV, Sharechain
from .consensus.subsidy import block_subsidy
from .stratum import protocol as P
from .stratum.server import StratumServer, Submission, SubmitResult

_LOGGER = logging.getLogger(__name__)

_PRELOADED_PROVER_LIBS: tuple[str, ...] = ()


def _submit_timing_threshold_ms() -> float:
    try:
        return float(os.environ.get("P2PEARL_SUBMIT_TIMING_MS", "75"))
    except ValueError:
        return 75.0


def _trace_all_submits() -> bool:
    return os.environ.get("P2PEARL_TRACE_SUBMIT", "").lower() in {"1", "true", "yes", "on"}


def _emit_submit_timing(
    submission: Submission,
    accepted: bool,
    reason: str | None,
    started_at: float,
    phases: list[tuple[str, float]],
) -> None:
    total_ms = (time.perf_counter() - started_at) * 1000.0
    threshold_ms = _submit_timing_threshold_ms()
    if not _trace_all_submits() and total_ms < threshold_ms:
        return
    payload = {
        "job_id": submission.job.job_id,
        "height": submission.job.height,
        "worker": submission.worker_label,
        "accepted": accepted,
        "reason": reason,
        "total_ms": round(total_ms, 3),
        "phases_ms": {label: round(ms, 3) for label, ms in phases},
    }
    print("P2PEARL_SUBMIT_TIMING " + json.dumps(payload, separators=(",", ":")), flush=True)


def _emit_submit_validation_timing(
    submission: Submission,
    accepted: bool,
    reason: str | None,
    started_at: float,
    phases: list[tuple[str, float]],
) -> None:
    total_ms = (time.perf_counter() - started_at) * 1000.0
    threshold_ms = _submit_timing_threshold_ms()
    if not _trace_all_submits() and total_ms < threshold_ms:
        return
    payload = {
        "job_id": submission.job.job_id,
        "height": submission.job.height,
        "worker": submission.worker_label,
        "accepted": accepted,
        "reason": reason,
        "total_ms": round(total_ms, 3),
        "phases_ms": {label: round(ms, 3) for label, ms in phases},
    }
    print("P2PEARL_SUBMIT_VALIDATION " + json.dumps(payload, separators=(",", ":")), flush=True)


@dataclass
class ParentTemplate:
    """The fields of a pearld getblocktemplate response that P2Pearl needs."""

    height: int
    prev_block: bytes                  # 32 bytes (as returned by GBT previousblockhash)
    bits: int                          # compact block nbits (u32)
    curtime: int                       # unix seconds
    coinbase_value: int                # GBT's subsidy + fees, in grains (informational —
                                       # jobs pay the EXACT subsidy; shares are coinbase-only)
    version: int = 0x20000000
    required_cert_version: int = 1     # GBT requiredcertversion (1 pre-MoE fork, 2 after)
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
            required_cert_version=int(gbt.get("requiredcertversion", 1)),
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


def payout_estimate_snapshot(
    sharechain: Sharechain,
    block_reward_grains: int,
    min_payout_grains: int = config.MIN_PAYOUT_GRAINS,
) -> dict:
    """Current deterministic PPLNS estimate if the next parent block paid now."""
    weights = sharechain.pplns_weights()
    total_weight = sum(weight for _, weight in weights)
    payouts = compute_pplns_payouts(block_reward_grains, weights, min_payout_grains)
    payout_by_addr = {p.address: p.grains for p in payouts}
    tip = sharechain.tip()
    window_shares = min(tip.sidechain_height + 1, sharechain.window) if tip else 0
    rows = []
    for address, weight in weights:
        potential = (block_reward_grains * weight) // total_weight if total_weight else 0
        rows.append({
            "address": address,
            "weight": weight,
            "percent_bps": (weight * 10_000) // total_weight if total_weight else 0,
            "potential_grains": potential,
            "estimated_grains": payout_by_addr.get(address, 0),
        })
    return {
        "window_shares": window_shares,
        "window_max": sharechain.window,
        "block_reward_grains": block_reward_grains,
        "min_payout_grains": min_payout_grains,
        "total_weight": total_weight,
        "addresses": rows,
    }


def _prl(grains: int) -> str:
    return f"{grains / config.GRAIN_PER_PEARL:.8f}".rstrip("0").rstrip(".") or "0"


def _short_addr(address: str) -> str:
    return address if len(address) <= 18 else f"{address[:10]}...{address[-6:]}"


def format_payout_estimate(snapshot: dict) -> str:
    """Human one-line payout estimate for terminal users and GUI logs."""
    rows = snapshot.get("addresses", [])
    window = f"{snapshot.get('window_shares', 0)}/{snapshot.get('window_max', 0)}"
    if not rows:
        return f"  payout est  : window {window}; no accepted shares yet"
    parts = []
    for row in rows[:3]:
        pct = row["percent_bps"] / 100
        est = row["estimated_grains"]
        if est == 0 and row["potential_grains"] > 0:
            payout = f"<{_prl(snapshot['min_payout_grains'])} PRL"
        else:
            payout = f"{_prl(est)} PRL"
        parts.append(f"{_short_addr(row['address'])}: {pct:.2f}% ~{payout}")
    extra = "" if len(rows) <= 3 else f"; +{len(rows) - 3} more"
    return f"  payout est  : window {window}; " + ", ".join(parts) + extra


# Injected adapter signatures.
MakeHeader = Callable[["ParentTemplate", "list[Payout]", bytes], "tuple[str, Any]"]
AssembleBlock = Callable[[Any, str], str]
VerifyShare = Callable[[bytes, str, int, int], bool]
VerifyBlock = Callable[[bytes, str, int], bool]
SubmitBlock = Callable[[str], Awaitable[Any]]
BroadcastShare = Callable[[ShareBlock, str], Awaitable[None]]
BroadcastBlock = Callable[[str], Awaitable[None]]
AssembleHook = Callable[[], Awaitable[None]]   # run around block assembly (e.g. pause co-located load)
MakeHeaderFromShare = Callable[[ShareBlock], "tuple[bytes, dict] | None"]   # rebuild a gossiped share's block


@dataclass
class _JobContext:
    candidate: ShareBlock
    header_ctx: Any
    payouts: list
    cert_version: int


class PoolNode:
    def __init__(
        self,
        *,
        sharechain: Sharechain,
        make_header: MakeHeader,
        verify_share: VerifyShare,
        verify_block: VerifyBlock,
        assemble_block: AssembleBlock,
        submit_block: SubmitBlock,
        stratum: StratumServer | None = None,
        broadcast_share: BroadcastShare | None = None,
        broadcast_block: BroadcastBlock | None = None,
        pre_assemble: AssembleHook | None = None,
        post_assemble: AssembleHook | None = None,
        make_header_from_share: MakeHeaderFromShare | None = None,
        min_payout_grains: int = config.MIN_PAYOUT_GRAINS,
        stratum_target_factor: int = 1,
    ) -> None:
        self.sharechain = sharechain
        self._make_header = make_header
        self._verify_share = verify_share
        self._verify_block = verify_block
        self._assemble_block = assemble_block
        self._submit_block = submit_block
        self.stratum = stratum
        self._broadcast_share = broadcast_share
        self._broadcast_block = broadcast_block
        self._pre_assemble = pre_assemble
        self._post_assemble = post_assemble
        self._make_header_from_share = make_header_from_share
        self._min_payout = min_payout_grains
        self._stratum_target_factor = max(1, int(stratum_target_factor))
        self._template: ParentTemplate | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._pending_shares: set[bytes] = set()
        self._refresh_task: asyncio.Task | None = None
        self._refresh_again = False
        self._last_job_refresh_at = 0.0
        # Block-assembly is the ZK prover — seconds of CPU on the critical find->submit
        # path. Serialize it (one prove at a time; concurrent proves only fight for the
        # same cores) and DEDUP per parent tip so a flood of block-clearing shares can't
        # stack N x the prove time. Reset when the parent advances (set_template).
        self._assemble_lock = asyncio.Lock()
        self._assembled_parents: set[bytes] = set()
        if stratum is not None:
            stratum.set_job_builder(self.build_job_for)

    def _spawn_background(self, label: str, coro) -> asyncio.Task:
        """Run non-consensus follow-up work without delaying miner submit ACKs."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._background_tasks.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.exception("%s failed", label)

        task.add_done_callback(_done)
        return task

    async def _run_stratum_refresh(self) -> None:
        assert self.stratum is not None
        try:
            while True:
                self._refresh_again = False
                await self.stratum.refresh()
                if not self._refresh_again:
                    return
        finally:
            self._refresh_task = None

    def _request_stratum_refresh(self) -> None:
        if self.stratum is not None:
            self._last_job_refresh_at = time.monotonic()
            if self._refresh_task is not None and not self._refresh_task.done():
                self._refresh_again = True
                return
            self._refresh_task = self._spawn_background("stratum refresh", self._run_stratum_refresh())

    async def _run_submit_followup(
        self,
        submission: Submission,
        ctx: _JobContext,
        header_bytes: bytes,
    ) -> None:
        """Run accepted-share work that should not hold the miner ACK open."""
        self.emit_payout_stats()

        is_block = await asyncio.to_thread(
            self._verify_block, header_bytes, submission.plain_proof_b64, ctx.cert_version)
        if is_block:
            await self._assemble_and_submit(ctx, submission.plain_proof_b64)

    async def _run_submit_validation(
        self,
        submission: Submission,
        ctx: _JobContext,
        header_bytes: bytes,
        share_nbits: int,
    ) -> None:
        started_at = time.perf_counter()
        last_mark = started_at
        phases: list[tuple[str, float]] = []
        share_id = ctx.candidate.share_id()

        def mark(label: str) -> None:
            nonlocal last_mark
            now = time.perf_counter()
            phases.append((label, (now - last_mark) * 1000.0))
            last_mark = now

        def finish(accepted: bool, reason: str | None = None) -> None:
            _emit_submit_validation_timing(submission, accepted, reason, started_at, phases)

        try:
            try:
                verified = await asyncio.to_thread(
                    self._verify_share,
                    header_bytes,
                    submission.plain_proof_b64,
                    share_nbits,
                    ctx.cert_version,
                )
            except Exception as exc:
                mark("verify_share")
                finish(False, f"verify failed: {exc}")
                return
            mark("verify_share")
            if not verified:
                finish(False, "share does not meet target")
                return

            proof_bytes = submission.plain_proof_b64.encode("ascii")
            added = self.sharechain.add_share(ctx.candidate, verified=True, proof=proof_bytes)
            mark("sharechain_add")
            if not added.accepted:
                if added.reason == "duplicate" and share_id in self.sharechain:
                    finish(True, "duplicate")
                    return
                finish(False, added.reason or "share rejected")
                return

            if added.is_best_tip:
                self._request_stratum_refresh()

            if self._broadcast_share is not None:
                self._spawn_background(
                    "share gossip",
                    self._broadcast_share(ctx.candidate, submission.plain_proof_b64),
                )
            self._spawn_background(
                "submit follow-up",
                self._run_submit_followup(submission, ctx, header_bytes),
            )
            mark("schedule_followup")
            finish(True)
        finally:
            self._pending_shares.discard(share_id)

    def set_template(self, template: ParentTemplate) -> None:
        prev = self._template.prev_block if self._template is not None else None
        if template.prev_block != prev:
            self._assembled_parents.clear()   # new parent tip -> a fresh block race
        self._template = template

    def emit_payout_stats(self) -> None:
        """Print current PPLNS payout estimate for the GUI and terminal operators."""
        if self._template is None:
            return
        snapshot = payout_estimate_snapshot(
            self.sharechain, block_subsidy(self._template.height), self._min_payout)
        print(format_payout_estimate(snapshot), flush=True)
        print(config.PAYOUT_STATS_PREFIX + json.dumps(snapshot, separators=(",", ":")), flush=True)

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
        share_timestamp = max(
            int(time.time()),
            template.curtime,
            (tip.timestamp + 1) if tip is not None else template.curtime,
        )
        job_template = replace(template, curtime=share_timestamp)
        # Consensus values — what _validate will demand of the submitted share:
        # the chain-derived target limit, and the EXACT parent subsidy (shares are
        # coinbase-only, so GBT's coinbasevalue — subsidy + mempool fees — would
        # overpay and make the assembled block invalid for the parent chain).
        target_limit = self.sharechain.expected_target(prev_share_id, share_timestamp)
        if target_limit is None:
            target_limit = int(tip.target_limit)   # truncated local history right after a
                                                   # window sync — carry the tip's limit
        share_target = max(1, target_limit // self._stratum_target_factor)
        coinbase_value = block_subsidy(template.height)

        weights = self.sharechain.pplns_weights()
        payouts = compute_pplns_payouts(coinbase_value, weights, self._min_payout)
        payout_set_hash = double_sha256(serialize_payouts(payouts))

        candidate = ShareBlock(
            version=config.SIDECHAIN_VERSION,
            sidechain_height=s_height,
            prev_share_id=prev_share_id,
            parent_prev_block=template.prev_block,
            parent_height=template.height,
            timestamp=share_timestamp,
            share_target=share_target,
            target_limit=target_limit,
            block_nbits=template.bits,
            coinbase_version=template.version,
            coinbase_value=coinbase_value,
            miner_address=worker_address,
            payout_set_hash=payout_set_hash,
        )
        header_hex, header_ctx = self._make_header(job_template, payouts, candidate.share_id())
        ctx = _JobContext(
            candidate=candidate, header_ctx=header_ctx, payouts=payouts,
            cert_version=template.required_cert_version,
        )
        return (header_hex, share_target, s_height, ctx)

    # --- submit handling (the stratum submit_handler) ----------------------- #
    async def handle_submit(self, submission: Submission) -> SubmitResult:
        started_at = time.perf_counter()
        last_mark = started_at
        phases: list[tuple[str, float]] = []

        def mark(label: str) -> None:
            nonlocal last_mark
            now = time.perf_counter()
            phases.append((label, (now - last_mark) * 1000.0))
            last_mark = now

        def finish(accepted: bool, code: int | None = None, message: str | None = None) -> SubmitResult:
            _emit_submit_timing(submission, accepted, message, started_at, phases)
            return SubmitResult(accepted, code, message)

        ctx = submission.job.context
        if not isinstance(ctx, _JobContext):
            mark("prepare")
            return finish(False, P.INVALID_PARAMS_CODE, "job has no candidate share")
        header_bytes = bytes.fromhex(submission.job.incomplete_header_hex)
        share_nbits = target_to_bits(submission.job.share_target)
        mark("prepare")

        share_id = ctx.candidate.share_id()
        if share_id in self.sharechain or share_id in self._pending_shares:
            mark("duplicate_check")
            return finish(True, message="duplicate")

        # 1. Fast ACK: expensive proof verification and sharechain mutation happen
        #    after the miner response, so miner-reported ping tracks network RTT.
        self._pending_shares.add(share_id)
        self._spawn_background(
            "submit validation",
            self._run_submit_validation(submission, ctx, header_bytes, share_nbits),
        )
        mark("schedule_validation")
        return finish(True)

    async def _assemble_and_submit(self, ctx: _JobContext, plain_proof_b64: str) -> None:
        """ZK-prove + submit the found Pearl block — the orphan-critical step.

        The ZK prover IS the whole find-block -> submit latency (seconds of CPU). To
        keep that window as small as possible and the node healthy while it runs:
          * prove in a worker thread so the event loop keeps serving miners and
            gossiping (a frozen node can't even read the next share);
          * hold ``_assemble_lock`` so only one prove runs at a time (two concurrent
            proves just halve each other's cores) and DEDUP by parent tip, so a fast
            GPU on an easy share target that lands many block-clearing shares for the
            same parent proves+submits ONCE instead of stacking N x the prove time;
          * run optional pre/post hooks around the prove (e.g. pause co-located CPU
            mining) for a contention-free, faster proof.
        """
        parent = ctx.candidate.parent_prev_block
        async with self._assemble_lock:
            if parent in self._assembled_parents:
                return                                   # already proved this tip's block
            if self._pre_assemble is not None:
                await self._pre_assemble()
            try:
                block_hex = await asyncio.to_thread(
                    self._assemble_block, ctx.header_ctx, plain_proof_b64)
            finally:
                if self._post_assemble is not None:
                    await self._post_assemble()
            await self._submit_block(block_hex)
            self._assembled_parents.add(parent)          # mark done only on success
            if self._broadcast_block is not None:
                await self._broadcast_block(block_hex)

    async def try_collaborative_submit(self, share: ShareBlock, proof_b64: str) -> None:
        """A peer gossiped a share we verified at the SHARE target; if it ALSO clears the
        BLOCK target for our CURRENT parent tip, race to assemble + submit it.

        The coinbase pays the same deterministic PPLNS set no matter who submits (feeless,
        no operator), so the fastest node in the pool can win a block found by ANY node —
        capping pool-wide orphan risk at the fastest prover's time, not each finder's. Reuses
        ``_assemble_and_submit`` (one prove at a time, deduped per parent — so this and our
        own miner's submit can't double-prove the same tip). Stale shares (a parent that is
        no longer our Pearl tip) are skipped before any expensive prove.
        """
        if self._make_header_from_share is None:
            return
        template = self._template
        if template is None or share.parent_prev_block != template.prev_block:
            return                                       # not our current Pearl tip -> stale
        built = self._make_header_from_share(share)
        if built is None:
            return                                       # forged payout set (already rejected upstream)
        header_bytes, header_ctx = built
        if isinstance(header_ctx, dict):
            header_ctx["cert_version"] = template.required_cert_version
        if not self._verify_block(header_bytes, proof_b64, template.required_cert_version):
            return                                       # clears share target but not the block target
        ctx = _JobContext(
            candidate=share, header_ctx=header_ctx, payouts=[],
            cert_version=template.required_cert_version,
        )
        await self._assemble_and_submit(ctx, proof_b64)

    # --- production poll loop ---------------------------------------------- #
    async def run(self, node: Any, poll_interval: float = 2.0) -> None:
        """Poll ``node`` for new parent tips and refresh jobs. Tests drive
        ``set_template`` / ``handle_submit`` directly instead of running this."""
        last_prev: bytes | None = None
        rpc_failures = 0
        while True:
            try:
                result = node.get_block_template()
                gbt = await result if asyncio.iscoroutine(result) else result
                template = ParentTemplate.from_gbt(gbt)
            except Exception as exc:
                rpc_failures += 1
                if rpc_failures == 1 or rpc_failures % 30 == 0:
                    print(
                        f"  parent RPC unavailable ({exc}); keeping miners connected and retrying",
                        flush=True,
                    )
                await asyncio.sleep(poll_interval)
                continue
            if rpc_failures:
                print("  parent RPC recovered; refreshing miner jobs", flush=True)
                rpc_failures = 0
            if template.prev_block != last_prev:
                last_prev = template.prev_block
                self.set_template(template)
                self._request_stratum_refresh()
            elif time.monotonic() - self._last_job_refresh_at >= config.SHARE_TARGET_TIME_SECONDS:
                self.set_template(template)
                self._request_stratum_refresh()
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
        payouts, share_id, block_subsidy(template.height), template.height)
    header_ctx = {
        "header": header, "coinbase_tx": coinbase_tx, "transactions": [],
        "cert_version": template.required_cert_version,
    }
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
    cert_version = int(header_ctx.get("cert_version", 1))
    generate = getattr(pearl_mining, "generate_proof_for_cert_version", None)
    enabled_v2_quotient_gpu = False
    if cert_version >= 2 and "PEARL_QUOTIENT_GPU" not in os.environ:
        mode = os.environ.get("P2PEARL_QUOTIENT_GPU", "auto").strip().lower()
        if mode not in {"0", "false", "no", "off", "disabled"}:
            os.environ["PEARL_QUOTIENT_GPU"] = "1"
            enabled_v2_quotient_gpu = True
    try:
        zk_proof = (generate(cert_version, incomplete, proof) if generate
                    else pearl_mining.generate_proof(incomplete, proof))
    finally:
        if enabled_v2_quotient_gpu:
            os.environ.pop("PEARL_QUOTIENT_GPU", None)
    pearl_header = PearlHeader(incomplete)
    try:
        from pearl_gateway.blockchain_utils.zk_certificate import CertificateVersion  # type: ignore
        zk_cert = ZKCertificate.from_pearl_header(
            pearl_header, zk_proof, cert_version=CertificateVersion(cert_version))
    except (ImportError, TypeError):
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


def _reconstruct_share(sharechain: Sharechain, min_payout: int, share: ShareBlock):
    """Trustlessly rebuild what a gossiped share's PoW commits to, from OUR OWN sharechain:
    recompute the deterministic PPLNS payouts as of the share's parent, confirm the share
    commits to exactly that set (``payout_set_hash`` — a peer can't forge the reward split),
    and reconstruct the byte-identical header + coinbase. Returns ``(header, coinbase_tx,
    payouts)`` or ``None`` if the payout set doesn't match. REQUIRES bitcoinutils + pearl_mining.
    """
    weights = sharechain.pplns_weights(share.prev_share_id)
    payouts = compute_pplns_payouts(share.coinbase_value, weights, min_payout)
    if double_sha256(serialize_payouts(payouts)) != share.payout_set_hash:
        return None  # coinbase doesn't pay the deterministic PPLNS set
    header, coinbase_tx = _build_share_header(
        share.coinbase_version, share.parent_prev_block, share.timestamp,
        share.block_nbits, payouts, share.share_id(), share.coinbase_value,
        share.parent_height)
    return header, coinbase_tx, payouts


def _make_verify_incoming(sharechain: Sharechain, min_payout: int,
                          cert_version_for_share: Callable[[ShareBlock], int] | None = None):
    """Build the production P2P ``verify_incoming(share, proof_b64) -> bool`` — reconstruct
    the exact header the finder mined and verify the proof at the SHARE target. The
    unit-tested P2P layer injects a fake."""
    from .pow.verify import verify_share

    def verify_incoming(share: ShareBlock, proof_b64: str) -> bool:
        recon = _reconstruct_share(sharechain, min_payout, share)
        if recon is None:
            return False
        header, _cb, _payouts = recon
        cert_version = cert_version_for_share(share) if cert_version_for_share else 1
        return verify_share(
            bytes(header.to_bytes()), proof_b64, target_to_bits(share.share_target),
            cert_version,
        )

    return verify_incoming


def _make_header_from_share(sharechain: Sharechain, min_payout: int):
    """Build ``reconstruct(share) -> (header_bytes, header_ctx) | None`` for collaborative
    block submission: rebuild the byte-identical header + coinbase a gossiped block-clearing
    share commits to, so ANY node can assemble + submit that block. The header_ctx matches
    what ``_production_assemble_block`` consumes (header + coinbase_tx + empty tx list)."""
    def reconstruct(share: ShareBlock):
        recon = _reconstruct_share(sharechain, min_payout, share)
        if recon is None:
            return None
        header, coinbase_tx, _payouts = recon
        header_ctx = {"header": header, "coinbase_tx": coinbase_tx, "transactions": []}
        return bytes(header.to_bytes()), header_ctx

    return reconstruct


def _preload_wsl_nvidia_driver_libs() -> tuple[str, ...]:
    """Preload WSL's matching NVIDIA driver JIT libs before CUDA initializes.

    WSL can mix ``libcuda`` from the Windows driver with older distro
    ``libnvidia-ptxjitcompiler`` packages. On Blackwell/CUDA 13 this can segfault
    during ``cudaGetDeviceCount`` before any Pearl code runs.
    """
    global _PRELOADED_PROVER_LIBS
    if sys.platform != "linux":
        return _PRELOADED_PROVER_LIBS
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as f:
            version = f.read().lower()
    except OSError:
        return _PRELOADED_PROVER_LIBS
    if "microsoft" not in version and "wsl" not in version:
        return _PRELOADED_PROVER_LIBS

    import ctypes
    import glob

    loaded: list[str] = []
    for soname in ("libnvidia-ptxjitcompiler.so.1", "libnvidia-nvvm.so.4"):
        matches = sorted(glob.glob(f"/usr/lib/wsl/drivers/*/{soname}"))
        if not matches:
            continue
        try:
            ctypes.CDLL(matches[0], mode=ctypes.RTLD_GLOBAL)
        except OSError:
            continue
        loaded.append(matches[0])
    _PRELOADED_PROVER_LIBS = tuple(loaded)
    return _PRELOADED_PROVER_LIBS


def _ensure_prover_env() -> None:
    """Tune the ZK prover's native runtime BEFORE ``pearl_mining`` is first imported —
    both rayon and jemalloc read their config when the ``.so`` initializes, so this MUST
    run before the first import/prove (``build_production_node`` is well before it).

    * ``RAYON_NUM_THREADS`` — pin the prover's rayon pool. Leaving it UNSET makes a
      found-block proof ~2x slower under load (≈17.5s vs ≈10s on a 16C/32T box); proving
      plateaus at the physical core count, so the logical-CPU count is a safe ceiling.
    * ``_RJEM_MALLOC_CONF=background_thread:true`` — move jemalloc's page purging onto a
      background thread so its ``madvise()`` churn doesn't block the proving threads
      (~3% faster, measured). tikv-jemalloc reads the ``_rjem_``-prefixed var (NOT plain
      ``MALLOC_CONF``). We deliberately keep default decay — ``dirty_decay_ms:-1``
      (never purge) is faster still but OOMs a memory-constrained node.
    * ``NUM_OF_GPUS=1`` — drive zeknox's CUDA backend as a single-GPU prover unless
      the operator explicitly sets a different value.

    Operators may override these by exporting them themselves (honored via ``setdefault``).
    """
    _preload_wsl_nvidia_driver_libs()
    os.environ.setdefault("RAYON_NUM_THREADS", str(os.cpu_count() or 1))
    os.environ.setdefault("_RJEM_MALLOC_CONF", "background_thread:true")
    os.environ.setdefault("NUM_OF_GPUS", "1")


HOOK_TIMEOUT_SECONDS = 10.0   # a found block must NEVER wait forever on a hook


def _shell_hook(cmd: str | None) -> "AssembleHook | None":
    """Wrap a shell command as an async block-assembly hook, or ``None`` if no command.

    Used to pause/resume co-located CPU load (e.g. an XMR miner on the same box) around
    the ~seconds-long ZK prove: a contention-free machine proves several times faster
    (measured ≈9.7s -> ≈3.3s when a co-located RandomX miner is paused).

    Hardened so a found block can never be lost to a bad hook:
      * bounded by ``HOOK_TIMEOUT_SECONDS`` (then kill the subprocess and continue);
      * any failure is logged, never raised into the block path.
    NB: match the target by *name*, e.g. ``pkill -STOP -x xmrig`` — a ``pgrep -f``
    pattern also matches the hook's own ``sh -c`` process (its cmdline contains the
    pattern), which SIGSTOPs the hook itself and would hang it (caught by the timeout).
    """
    if not cmd:
        return None

    async def _run() -> None:
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(proc.wait(), timeout=HOOK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print(f"  assemble hook timed out after {HOOK_TIMEOUT_SECONDS:.0f}s ({cmd!r}); "
                  f"continuing (a found block must not wait on a hook)", flush=True)
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:  # pragma: no cover
                    pass
        except Exception as exc:  # pragma: no cover - defensive
            print(f"  assemble hook failed ({cmd!r}): {exc}", flush=True)

    return _run


def build_production_node(cfg: config.DaemonConfig | None = None, share_target: int | None = None,
                          pause_cmd: str | None = None, resume_cmd: str | None = None) -> "tuple[PoolNode, Any]":
    """Wire a PoolNode with the real node RPC + verifiers + assembly adapters.

    The share target retargets automatically (one share per
    ``SHARE_TARGET_TIME_SECONDS``, derived from the sharechain — see
    ``Sharechain.expected_target``). ``share_target`` overrides only the GENESIS
    bootstrap target; it is sidechain CONSENSUS, so every node on the same
    sidechain must use the same value."""
    from .chain.node_rpc import NodeRPC
    from .p2p.node import P2PNode
    from .pow.verify import verify_block_solution, verify_share
    from .stratum.server import StratumServer

    _ensure_prover_env()                    # tune rayon + jemalloc BEFORE the first prove
    cfg = cfg or config.DaemonConfig()
    node = NodeRPC(cfg.node)
    sharechain = (Sharechain(bootstrap_target=share_target) if share_target is not None
                  else Sharechain())

    async def submit_block(block_hex: str):
        return await asyncio.to_thread(node.submit_block, block_hex)

    pool = PoolNode(
        sharechain=sharechain,
        make_header=_production_make_header,
        verify_share=verify_share,
        verify_block=verify_block_solution,
        assemble_block=_production_assemble_block,
        submit_block=submit_block,
        pre_assemble=_shell_hook(pause_cmd),     # e.g. pause a co-located XMR miner
        post_assemble=_shell_hook(resume_cmd),   # ...and resume it after the prove
        stratum_target_factor=cfg.stratum_target_factor,
    )
    # Miner-facing stratum: each connecting miner gets its OWN job (its own PPLNS
    # coinbase). build_job_for is the per-connection job source; handle_submit grades
    # the submitted share. ``serve`` runs the server alongside the GBT poll loop.
    stratum = StratumServer(pool.handle_submit, host=cfg.stratum_host, port=cfg.stratum_port)
    pool.stratum = stratum
    stratum.set_job_builder(pool.build_job_for)

    # Collaborative submission: rebuild the block a gossiped block-clearing share commits
    # to, so any node can win a block found by a slower peer (same feeless PPLNS payout).
    pool._make_header_from_share = _make_header_from_share(sharechain, pool._min_payout)

    # P2P gossip: shares/blocks propagate to peers; an incoming share is VERIFIED by
    # reconstructing its header (the same deterministic build the finder used) before
    # it is added or relayed -> a peer can forge neither the PoW nor the PPLNS split.
    async def _on_new_share(_share):
        pool.emit_payout_stats()
        pool._request_stratum_refresh()        # a peer's share advanced the tip -> new jobs

    def _cert_version_for_share(share: ShareBlock) -> int:
        template = pool._template
        if template is not None:
            return template.required_cert_version
        return 1

    p2p = P2PNode(
        sharechain=sharechain,
        verify_incoming=_make_verify_incoming(
            sharechain, pool._min_payout, _cert_version_for_share),
        host=cfg.p2p_host, port=cfg.p2p_port, on_new_share=_on_new_share,
        on_block_candidate=pool.try_collaborative_submit)   # race to submit peers' block-clearing shares
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
    # Un-strand a co-located miner a PRIOR instance may have left paused: the pause/resume
    # around a prove is balanced (finally block), but a crash/restart/SIGKILL mid-prove can
    # skip the resume. Running it once at startup self-heals that. Safe no-op otherwise.
    if pool._post_assemble is not None:
        await pool._post_assemble()
    await pool.stratum.start()
    if getattr(pool, "p2p", None) is not None:
        await pool.p2p.start()
        for host, port in peers:
            try:
                await pool.p2p.connect(host, int(port))
            except Exception as exc:
                print(f"  p2p peer    : could not connect {host}:{port}: {exc}", flush=True)
        print(f"  p2p         : {pool.p2p.host}:{pool.p2p.port}  ({pool.p2p.peer_count} peer(s) connected)", flush=True)
    print(f"  prover      : RAYON_NUM_THREADS={os.environ.get('RAYON_NUM_THREADS')}"
          f"  jemalloc={os.environ.get('_RJEM_MALLOC_CONF')}"
          f"  NUM_OF_GPUS={os.environ.get('NUM_OF_GPUS')}"
          f"  quotient-gpu={os.environ.get('P2PEARL_QUOTIENT_GPU', 'auto-v2')}"
          + ("  + pause-hook during prove" if pool._pre_assemble is not None else ""), flush=True)
    if _PRELOADED_PROVER_LIBS:
        print(f"  gpu libs    : preloaded {len(_PRELOADED_PROVER_LIBS)} WSL NVIDIA driver lib(s)", flush=True)
    s_host, s_port = pool.stratum.host, pool.stratum.port
    print(f"  stratum     : {s_host}:{s_port}  (point your miners here)", flush=True)
    print(f"  e.g.  SRBMiner-MULTI --algorithm pearlhash --pool {s_host}:{s_port} --wallet <prl1...> --disable-cpu", flush=True)
    print("  serving — Ctrl-C to stop", flush=True)
    await asyncio.gather(pool.run(node), pool.stratum.serve_forever())


def main(cfg: "config.DaemonConfig | None" = None, share_target: int | None = None,
         pause_cmd: str | None = None, resume_cmd: str | None = None) -> int:
    cfg = cfg or config.DaemonConfig()
    print(f"P2Pearl v{__version__} daemon")
    try:
        pool, node = build_production_node(cfg, share_target, pause_cmd, resume_cmd)
    except Exception as exc:  # pragma: no cover - needs live deps
        print(f"could not wire production node: {exc}")
        print("This build is missing the native proof stack. The release p2pearl.exe includes")
        print("it; for a source install, build pearl_mining per docs/running-a-node.md.")
        return 1
    print(f"  parent node : {node._cfg.url if hasattr(node, '_cfg') else '?'}")
    try:
        asyncio.run(_serve(pool, node, cfg.peers))
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    except Exception as exc:  # pragma: no cover - needs a live pearld
        print(f"\n  could not reach pearld at {node._cfg.url}: {exc}")
        print("  Is your Pearl node running? Start pearld first (Windows: pearld.exe from the")
        print("  release zip; see docs/running-a-node.md), wait for it to sync, and check the")
        print("  RPC URL/user/password match it. The GUI's 'Test pearld' button checks this.")
        print("  ('p2pearl demo' runs with no node at all.)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
