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

from p2pearl.consensus.share import ShareBlock  # noqa: E402
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
    sc = Sharechain(window=10, bootstrap_target=SHARE_TARGET)
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


def test_parent_template_reads_required_cert_version():
    gbt = {
        "height": 1000,
        "previousblockhash": "22" * 32,
        "bits": "1e01ffff",
        "curtime": 123,
        "coinbasevalue": 5_000_000_000,
        "requiredcertversion": 2,
    }
    assert ParentTemplate.from_gbt(gbt).required_cert_version == 2


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
    from p2pearl.config import SIDECHAIN_VERSION
    forged = ShareBlock(
        version=SIDECHAIN_VERSION, sidechain_height=0, prev_share_id=GENESIS_PREV,
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


def test_handle_submit_passes_cert_version_to_verifiers():
    asyncio.run(_cert_version_flow())


async def _cert_version_flow():
    sc = Sharechain(window=10)
    seen = {}

    def verify_share(*args):
        seen["share_args"] = args
        return True

    def verify_block(*args):
        seen["block_args"] = args
        return False

    node = PoolNode(
        sharechain=sc, make_header=_fake_make_header,
        verify_share=verify_share, verify_block=verify_block,
        assemble_block=(lambda ctx, proof: "BLOCKHEX"), submit_block=_noop,
    )
    node.set_template(ParentTemplate(
        height=TEMPLATE.height, prev_block=TEMPLATE.prev_block, bits=TEMPLATE.bits,
        curtime=TEMPLATE.curtime, coinbase_value=TEMPLATE.coinbase_value,
        required_cert_version=2,
    ))
    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    job = StratumJob("00001000-0004", header_hex, target, height, ctx)
    assert (await node.handle_submit(Submission(ADDR_A, "rigA", job, PROOF_B64))).accepted
    assert seen["share_args"][-1] == 2
    assert seen["block_args"][-1] == 2


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


def test_handle_submit_block_flood_deduped_and_hooks():
    asyncio.run(_flood_flow())


async def _flood_flow():
    # A fast GPU on an easy share target can land many block-clearing shares for the
    # SAME parent tip. The node must ZK-prove + submitblock ONCE (not stack N x the
    # ~seconds-long prove and miss the orphan window), run the pre/post hooks around
    # that one prove, yet still record every valid share. A new parent re-arms it.
    sc = Sharechain(window=10)
    assembled = []
    pre, post, sblock, bblock = _Rec(), _Rec(), _Rec(), _Rec()

    def assemble(ctx, proof):          # sync, like the production adapter
        assembled.append(proof)
        return "DEADBEEF"

    node = PoolNode(
        sharechain=sc, make_header=_fake_make_header,
        verify_share=(lambda *a: True), verify_block=(lambda *a: True),
        assemble_block=assemble, submit_block=sblock, broadcast_block=bblock,
        pre_assemble=pre, post_assemble=post)
    node.set_template(TEMPLATE)

    for addr, jid in ((ADDR_A, "j1"), (ADDR_B, "j2")):       # two block-clearing shares, same parent
        header_hex, target, height, ctx = node.build_job_for(addr)
        res = await node.handle_submit(Submission(addr, "rig", StratumJob(jid, header_hex, target, height, ctx), PROOF_B64))
        assert res.accepted

    assert len(assembled) == 1                               # proved exactly once
    assert len(sblock.calls) == 1 and sblock.calls[0][0] == "DEADBEEF"
    assert len(bblock.calls) == 1                            # block gossiped once
    assert len(pre.calls) == 1 and len(post.calls) == 1      # hooks ran around the one prove
    assert len(sc) == 2                                      # both shares still recorded

    node.set_template(ParentTemplate(                        # a NEW parent tip re-arms assembly
        height=1001, prev_block=b"\x33" * 32, bits=TEMPLATE.bits,
        curtime=TEMPLATE.curtime, coinbase_value=TEMPLATE.coinbase_value))
    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    assert (await node.handle_submit(Submission(ADDR_A, "rig", StratumJob("j3", header_hex, target, height, ctx), PROOF_B64))).accepted
    assert len(assembled) == 2


# --------------------------------------------------------------------------- #
# Unit: collaborative submission (any node submits a peer's block-clearing share)
# --------------------------------------------------------------------------- #

def _gossip_share(parent_prev=TEMPLATE.prev_block, height=0, prev=GENESIS_PREV, addr=ADDR_B):
    from p2pearl.config import SIDECHAIN_VERSION
    return ShareBlock(
        version=SIDECHAIN_VERSION, sidechain_height=height, prev_share_id=prev,
        parent_prev_block=parent_prev, parent_height=TEMPLATE.height, timestamp=TEMPLATE.curtime,
        share_target=SHARE_TARGET, block_nbits=TEMPLATE.bits, coinbase_version=TEMPLATE.version,
        coinbase_value=TEMPLATE.coinbase_value, miner_address=addr, payout_set_hash=b"\x00" * 32)


def _collab_node(sc, assembled, sblock, verify_block=True):
    def assemble(ctx, proof):
        assembled.append(proof)
        return "CAFE"
    # reconstruct(share) -> (header_bytes, header_ctx); real wiring rebuilds via PPLNS
    def mk(share):
        return (b"\x01" * 76, {"sid": share.share_id()})
    return PoolNode(
        sharechain=sc, make_header=_fake_make_header,
        verify_share=(lambda *a: True), verify_block=(lambda *a: verify_block),
        assemble_block=assemble, submit_block=sblock, make_header_from_share=mk)


def test_collaborative_submit_block():
    asyncio.run(_collab_ok())


async def _collab_ok():
    # A peer's gossiped share that also clears the block target for OUR current tip ->
    # we assemble + submit it (same feeless payout, so it's the pool's block either way).
    sc = Sharechain(window=10)
    assembled, sblock = [], _Rec()
    node = _collab_node(sc, assembled, sblock)
    node.set_template(TEMPLATE)
    await node.try_collaborative_submit(_gossip_share(), PROOF_B64)
    assert assembled == [PROOF_B64]
    assert sblock.calls and sblock.calls[0][0] == "CAFE"


def test_collaborative_submit_skips_stale_parent():
    asyncio.run(_collab_stale())


async def _collab_stale():
    # A share whose parent is NOT our current Pearl tip is stale -> never prove it.
    sc = Sharechain(window=10)
    assembled, sblock = [], _Rec()
    node = _collab_node(sc, assembled, sblock)
    node.set_template(TEMPLATE)
    await node.try_collaborative_submit(_gossip_share(parent_prev=b"\x99" * 32), PROOF_B64)
    assert assembled == [] and not sblock.calls


def test_collaborative_submit_not_a_block_skips():
    asyncio.run(_collab_not_block())


async def _collab_not_block():
    # Clears the share target (it's a valid share) but NOT the block target -> no submit.
    sc = Sharechain(window=10)
    assembled, sblock = [], _Rec()
    node = _collab_node(sc, assembled, sblock, verify_block=False)
    node.set_template(TEMPLATE)
    await node.try_collaborative_submit(_gossip_share(), PROOF_B64)
    assert assembled == [] and not sblock.calls


def test_collaborative_submit_deduped_with_own_block():
    asyncio.run(_collab_dedup())


async def _collab_dedup():
    # Our own miner already proved+submitted this tip's block; a peer's block-clearing share
    # for the SAME parent must NOT trigger a second prove (shared per-parent dedup).
    sc = Sharechain(window=10)
    assembled, sblock = [], _Rec()
    node = _collab_node(sc, assembled, sblock)
    node.set_template(TEMPLATE)
    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    await node.handle_submit(Submission(ADDR_A, "rigA", StratumJob("j", header_hex, target, height, ctx), PROOF_B64))
    assert len(assembled) == 1
    await node.try_collaborative_submit(_gossip_share(), PROOF_B64)
    assert len(assembled) == 1                       # deduped: no second prove


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
