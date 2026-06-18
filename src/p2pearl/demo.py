"""P2Pearl local end-to-end demo — no pearld, no GPU, no pearl_mining needed.

Boots TWO real P2Pearl pool nodes (each = sharechain + daemon orchestrator + stratum
server + P2P gossip, all running as live asyncio services on loopback sockets),
connects them as peers, then drives simulated miners that authorize and submit shares
to node A over the real stratum protocol. You can watch a share flow through:

    miner --stratum--> nodeA: verify -> sidechain -> PPLNS -> P2P gossip --> nodeB

Proof verification and header/block assembly are faked (that part needs the native
pearl_mining + a real node), so this exercises the full ORCHESTRATION + networking,
not the cryptography. Run it:  p2pearl demo  (or  python -m p2pearl demo)
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct

from p2pearl import config
from p2pearl.consensus.pplns import compute_pplns_payouts
from p2pearl.consensus.sharechain import Sharechain
from p2pearl.consensus.subsidy import block_subsidy
from p2pearl.daemon import ParentTemplate, PoolNode
from p2pearl.p2p.node import P2PNode
from p2pearl.stratum.server import StratumServer

GRAIN = config.GRAIN_PER_PEARL
# A stand-in parent block template (what pearld's getblocktemplate would return).
TEMPLATE = ParentTemplate(
    height=42_000, prev_block=b"\x11" * 32, bits=0x1E01FFFF,
    curtime=1_777_270_000, coinbase_value=block_subsidy(42_000),   # the real h42000 subsidy
)

# Three demo miners (valid-looking bech32m P2TR addresses).
MINERS = {
    "rig-A": "prl1p" + "q" * 58,
    "rig-B": "prl1p" + "r" * 58,
    "rig-C": "prl1p" + "z" * 58,
}


def _fake_make_header(template, payouts, share_id):
    # Real 76-byte header; encode the candidate share id as the "merkle" so each
    # miner's job header is distinct (as it would be on-chain).
    hdr = (struct.pack("<I", template.version & 0xFFFFFFFF) + template.prev_block
           + share_id + struct.pack("<I", template.curtime) + struct.pack("<I", template.bits))
    return hdr.hex(), {"share_id": share_id}


class Pool:
    """One fully-wired P2Pearl node."""

    def __init__(self, name, *, find_block_on=None):
        self.name = name
        self.sharechain = Sharechain(window=config.PPLNS_WINDOW_SHARES)
        self.blocks_found = 0
        self._submits = 0
        self._find_block_on = find_block_on

        async def submit_block(block_hex):
            self.blocks_found += 1
            print(f"   [{name}]  *** BLOCK FOUND -> submitblock to pearld ({block_hex[:12]}...) ***")

        def verify_block(_header, _proof, _cert_version=1):
            self._submits += 1
            return self._find_block_on is not None and self._submits == self._find_block_on

        # p2p first (so the daemon can take its broadcast hooks); on_new_share fires
        # when a peer's share lands -> advance our miners' jobs.
        self.p2p = P2PNode(
            sharechain=self.sharechain, verify_incoming=lambda s, p: True,
            host="127.0.0.1", port=0, on_new_share=self._on_gossiped_share)
        self.node = PoolNode(
            sharechain=self.sharechain,
            make_header=_fake_make_header,
            verify_share=lambda *a: True, verify_block=verify_block,
            assemble_block=lambda ctx, proof: "b10c4" * 8,
            submit_block=submit_block,
            broadcast_share=self.p2p.broadcast_share,
            broadcast_block=self.p2p.broadcast_block)
        self.stratum = StratumServer(self.node.handle_submit, host="127.0.0.1", port=0)
        self.node.stratum = self.stratum
        self.stratum.set_job_builder(self.node.build_job_for)

    async def _on_gossiped_share(self, share):
        print(f"   [{self.name}]  <-- received share via P2P gossip "
              f"(h={share.sidechain_height}, miner {share.miner_address[:9]}..) "
              f"| sidechain now {len(self.sharechain)} shares")
        await self.stratum.refresh()

    async def start(self):
        await self.stratum.start()
        await self.p2p.start()
        self.node.set_template(TEMPLATE)

    async def stop(self):
        await self.stratum.stop()
        await self.p2p.stop()


async def simulate_miner(stratum_port, address, worker):
    """Connect over the real stratum, authorize, get a job, submit one (fake) share."""
    reader, writer = await asyncio.open_connection("127.0.0.1", stratum_port)

    async def send(obj):
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()

    async def recv():
        return json.loads(await asyncio.wait_for(reader.readline(), timeout=5.0))

    await send({"id": 1, "method": "mining.authorize",
                "params": {"wallet": address, "worker": worker, "agent": "demo-miner/1.0"}})
    assert (await recv()).get("result") is True
    notify = await recv()                       # job pushed right after authorize
    job_id = notify["params"]["job_id"]
    proof = base64.b64encode(f"FAKE-PLAIN-PROOF::{worker}::{job_id}".encode()).decode()
    await send({"id": 2, "method": "mining.submit",
                "params": {"job_id": job_id, "plain_proof": proof, "hs": 149.0}})
    while True:                                  # skip job refreshes, read our ack
        msg = await recv()
        if msg.get("method") is None and msg.get("id") == 2:
            ack = msg
            break
    writer.close()
    return job_id, ack.get("result")


def _fmt_prl(grains):
    return f"{grains / GRAIN:.4f} PRL"


async def main():
    print("=" * 74)
    print("P2Pearl local demo - two gossiping pool nodes + simulated miners")
    print("(no pearld / no GPU / no pearl_mining -- proof verification is faked)")
    print("=" * 74)

    a = Pool("nodeA", find_block_on=4)           # nodeA 'finds a block' on the 4th share
    b = Pool("nodeB")
    await a.start()
    await b.start()
    await b.p2p.connect("127.0.0.1", a.p2p.port)
    await asyncio.sleep(0.1)
    print(f"\nnodeA  stratum :{a.stratum.port}  p2p :{a.p2p.port}")
    print(f"nodeB  stratum :{b.stratum.port}  p2p :{b.p2p.port}   (peered to nodeA: "
          f"{b.p2p.peer_count} peer, {a.p2p.peer_count} peer)")
    print(f"\nparent template: height {TEMPLATE.height}, subsidy {_fmt_prl(TEMPLATE.coinbase_value)}, "
          f"share window {config.PPLNS_WINDOW_SHARES}\n")

    # A sequence of shares from different rigs submitted to nodeA's stratum.
    plan = ["rig-A", "rig-B", "rig-A", "rig-C", "rig-B"]
    try:
        for i, worker in enumerate(plan, 1):
            print(f"[{i}] {worker} submits a share to nodeA ...")
            job_id, ok = await simulate_miner(a.stratum.port, MINERS[worker], worker)
            await asyncio.sleep(0.25)            # let the P2P gossip settle
            print(f"    accepted={ok}  job={job_id}  | nodeA sidechain={len(a.sharechain)} "
                  f"nodeB sidechain={len(b.sharechain)}\n")

        # Show the feeless PPLNS split the next block's coinbase would pay.
        print("-" * 74)
        weights = a.sharechain.pplns_weights()
        payouts = compute_pplns_payouts(TEMPLATE.coinbase_value, weights, config.MIN_PAYOUT_GRAINS)
        print("PPLNS payout the next block coinbase would pay (feeless, no operator):")
        addr_to_rig = {v: k for k, v in MINERS.items()}
        for p in payouts:
            print(f"    {addr_to_rig.get(p.address, p.address[:14]):8} {p.address[:18]}..  {_fmt_prl(p.grains)}")
        print(f"    total = {_fmt_prl(sum(p.grains for p in payouts))}  (== subsidy; 0 to any operator)")
        print("-" * 74)

        tip_a = a.sharechain.tip().share_id().hex()[:16]
        tip_b = b.sharechain.tip().share_id().hex()[:16] if b.sharechain.tip() else "(none)"
        print(f"\nFinal state:")
        print(f"   nodeA: {len(a.sharechain)} shares, tip {tip_a}, blocks found {a.blocks_found}")
        print(f"   nodeB: {len(b.sharechain)} shares, tip {tip_b}  (synced purely via P2P gossip)")
        print(f"   sidechains agree: {tip_a == tip_b}")
        print("\nThe full pipeline ran end-to-end as live services. To run it for real, see"
              "\nthe M6 issue: build pearl_mining + point a node/miner at it.")
    finally:
        await a.stop()
        await b.stop()


if __name__ == "__main__":
    asyncio.run(main())
