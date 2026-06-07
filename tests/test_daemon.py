"""Tests for the PoolNode daemon orchestration.

Unit tests drive ``build_job_for`` / ``handle_submit`` with fake injected adapters
(no pearld / pearl_mining / bitcoinutils). One integration test wires a real
StratumServer to the PoolNode and connects two fake miners over loopback sockets to
prove per-miner jobs and the full submit -> verify -> sharechain -> gossip path.
"""

import asyncio
import base64
import json
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2pearl.consensus.sharechain import GENESIS_PREV, Sharechain  # noqa: E402
from p2pearl.daemon import ParentTemplate, PoolNode, serialize_payouts  # noqa: E402
from p2pearl.stratum.server import StratumJob, StratumServer, Submission  # noqa: E402

ADDR_A = "prl1p" + "q" * 58
ADDR_B = "prl1p" + "r" * 58
PROOF_B64 = base64.b64encode(b"plain-proof").decode()
SHARE_TARGET = 1 << 248
TEMPLATE = ParentTemplate(
    height=1000, prev_block=b"\x22" * 32, bits=0x1E01FFFF,
    curtime=0x499602D2, coinbase_value=5_000_000_000,
)


def _fake_make_header(template, payouts, share_id):
    # 76-byte header; encode share_id as the "merkle" so each miner's header differs.
    hdr = (struct.pack("<I", template.version & 0xFFFFFFFF) + template.prev_block
           + share_id + struct.pack("<I", template.curtime) + struct.pack("<I", template.bits))
    return hdr.hex(), {"share_id": share_id, "payouts": payouts}


class _Rec:
    def __init__(self):
        self.calls = []

    async def __call__(self, *args):
        self.calls.append(args)


async def _noop(*args):
    return None


def _node(sc, *, verify_share=True, verify_block=False, assemble="BLOCKHEX",
          submit_block=None, broadcast_share=None, broadcast_block=None, stratum=None):
    return PoolNode(
        sharechain=sc,
        share_target=SHARE_TARGET,
        make_header=_fake_make_header,
        verify_share=(lambda *a: verify_share),
        verify_block=(lambda *a: verify_block),
        assemble_block=(lambda ctx, proof: assemble),
        submit_block=submit_block or _noop,
        broadcast_share=broadcast_share,
        broadcast_block=broadcast_block,
        stratum=stratum,
    )


# --------------------------------------------------------------------------- #
# Unit: job building
# --------------------------------------------------------------------------- #

def test_build_job_for_creates_candidate():
    sc = Sharechain(window=10)
    node = _node(sc)
    node.set_template(TEMPLATE)
    spec = node.build_job_for(ADDR_A)
    assert spec is not None
    header_hex, target, height, ctx = spec
    assert target == SHARE_TARGET and height == 0
    assert len(bytes.fromhex(header_hex)) == 76
    assert ctx.candidate.miner_address == ADDR_A
    assert ctx.candidate.prev_share_id == GENESIS_PREV
    assert node.build_job_for(None) is None          # no worker address


def test_build_job_none_without_template():
    node = _node(Sharechain(window=10))
    assert node.build_job_for(ADDR_A) is None


def test_build_production_node_wires_stratum_and_p2p():
    # `p2pearl daemon` must serve miners AND gossip to peers: build_production_node wires
    # a StratumServer (job source = build_job_for) + a P2PNode (broadcast hooks). No
    # pearld / pearl_mining needed to *construct* it; the lazy deps load only when mining.
    from p2pearl.daemon import build_production_node
    pool, node = build_production_node()
    assert pool.stratum is not None and pool.stratum._job_builder == pool.build_job_for
    assert pool.p2p is not None
    assert pool._broadcast_share == pool.p2p.broadcast_share
    assert pool._broadcast_block == pool.p2p.broadcast_block


def test_verify_incoming_rejects_forged_payout_set():
    # A peer recomputes the deterministic PPLNS payouts from its OWN sharechain; a share
    # whose payout_set_hash doesn't match is rejected before any header reconstruction
    # (so no pearl_mining needed). This is what stops a peer forging the reward split.
    from p2pearl.consensus.share import ShareBlock
    from p2pearl.daemon import _make_verify_incoming
    verify = _make_verify_incoming(Sharechain(window=10), min_payout=0)
    forged = ShareBlock(
        version=2, sidechain_height=0, prev_share_id=GENESIS_PREV,
        parent_prev_block=b"\x00" * 32, parent_height=1, timestamp=1000,
        share_target=SHARE_TARGET, block_nbits=0x1E01FFFF,
        coinbase_version=0x20000000, coinbase_value=5_000_000_000,
        miner_address=ADDR_A, payout_set_hash=b"\xde" * 32)   # bogus commitment
    assert verify(forged, PROOF_B64) is False


def test_serialize_payouts_deterministic():
    from p2pearl.consensus.pplns import Payout
    a = serialize_payouts([Payout("addrA", 10), Payout("addrB", 20)])
    b = serialize_payouts([Payout("addrA", 10), Payout("addrB", 20)])
    assert a == b and len(a) > 0


# --------------------------------------------------------------------------- #
# Unit: submit handling
# --------------------------------------------------------------------------- #

def test_handle_submit_accepts_share():
    asyncio.run(_share_flow())


async def _share_flow():
    sc = Sharechain(window=10)
    bshare, bblock, sblock = _Rec(), _Rec(), _Rec()
    node = _node(sc, verify_block=False, submit_block=sblock,
                 broadcast_share=bshare, broadcast_block=bblock)
    node.set_template(TEMPLATE)
    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    job = StratumJob("00001000-0001", header_hex, target, height, ctx)
    res = await node.handle_submit(Submission(ADDR_A, "rigA", job, PROOF_B64))
    assert res.accepted
    assert len(sc) == 1 and sc.tip().miner_address == ADDR_A
    assert len(bshare.calls) == 1                 # share gossiped
    assert not bblock.calls and not sblock.calls  # not a block


def test_handle_submit_block_path():
    asyncio.run(_block_flow())


async def _block_flow():
    sc = Sharechain(window=10)
    bblock, sblock = _Rec(), _Rec()
    node = _node(sc, verify_block=True, assemble="DEADBEEF",
                 submit_block=sblock, broadcast_block=bblock)
    node.set_template(TEMPLATE)
    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    job = StratumJob("00001000-0002", header_hex, target, height, ctx)
    res = await node.handle_submit(Submission(ADDR_A, "rigA", job, PROOF_B64))
    assert res.accepted
    assert sblock.calls and sblock.calls[0][0] == "DEADBEEF"   # submitblock got the assembled hex
    assert len(bblock.calls) == 1


def test_handle_submit_rejects_bad_share():
    asyncio.run(_reject_flow())


async def _reject_flow():
    sc = Sharechain(window=10)
    node = _node(sc, verify_share=False, verify_block=True)
    node.set_template(TEMPLATE)
    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    job = StratumJob("00001000-0003", header_hex, target, height, ctx)
    res = await node.handle_submit(Submission(ADDR_A, "rigA", job, PROOF_B64))
    assert not res.accepted and res.error_code == 23   # LOW_DIFF
    assert len(sc) == 0                                # sidechain untouched


def test_handle_submit_no_context():
    asyncio.run(_no_ctx_flow())


async def _no_ctx_flow():
    node = _node(Sharechain(window=10))
    job = StratumJob("x", "00" * 76, SHARE_TARGET, 0, None)   # context missing
    res = await node.handle_submit(Submission(ADDR_A, "rigA", job, PROOF_B64))
    assert not res.accepted and res.error_code == -32602


# --------------------------------------------------------------------------- #
# Integration: PoolNode + real StratumServer + two fake miners
# --------------------------------------------------------------------------- #

async def _send(writer, obj):
    writer.write((json.dumps(obj) + "\n").encode())
    await writer.drain()


async def _recv(reader):
    return json.loads(await asyncio.wait_for(reader.readline(), timeout=5.0))


async def _recv_result(reader, want_id):
    while True:
        msg = await _recv(reader)
        if msg.get("method") is not None:
            continue                       # skip server-initiated notifications
        if msg.get("id") == want_id:
            return msg


def test_integration_per_miner_jobs_and_submit():
    asyncio.run(_integration())


async def _integration():
    sc = Sharechain(window=10)
    bshare = _Rec()
    node = _node(sc, broadcast_share=bshare)
    server = StratumServer(node.handle_submit, host="127.0.0.1", port=0)
    node.stratum = server
    server.set_job_builder(node.build_job_for)
    await server.start()
    conns = []
    try:
        ra, wa = await asyncio.open_connection("127.0.0.1", server.port)
        conns.append(wa)
        await _send(wa, {"id": 1, "method": "mining.authorize",
                         "params": {"wallet": ADDR_A, "worker": "rigA"}})
        assert (await _recv(ra))["result"] is True

        rb, wb = await asyncio.open_connection("127.0.0.1", server.port)
        conns.append(wb)
        await _send(wb, {"id": 1, "method": "mining.authorize",
                         "params": {"wallet": ADDR_B, "worker": "rigB"}})
        assert (await _recv(rb))["result"] is True

        # Set the template AFTER authorize (so the authorize-time push finds no job),
        # then refresh -> each miner gets its OWN job.
        node.set_template(TEMPLATE)
        await server.refresh()
        na, nb = await _recv(ra), await _recv(rb)
        assert na["method"] == "mining.notify" and nb["method"] == "mining.notify"
        # Per-miner headers differ (each commits a candidate share crediting that miner).
        assert na["params"]["header"] != nb["params"]["header"]

        job_a = na["params"]["job_id"]
        await _send(wa, {"id": 2, "method": "mining.submit",
                         "params": {"job_id": job_a, "plain_proof": PROOF_B64, "hs": 1}})
        ack = await _recv_result(ra, 2)
        assert ack["result"] is True
        assert len(sc) == 1 and sc.tip().miner_address == ADDR_A
        assert len(bshare.calls) == 1
    finally:
        for w in conns:
            w.close()
        await server.stop()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
