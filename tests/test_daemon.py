"""Tests for the PoolNode daemon orchestration.

Unit tests drive ``build_job_for`` / ``handle_submit`` with fake injected adapters
(no pearld / pearl_mining / bitcoinutils). One integration test wires a real
StratumServer to the PoolNode and connects two fake miners over loopback sockets to
prove per-miner jobs and the full submit -> verify -> sharechain -> gossip path.
"""

import asyncio
import base64
from contextlib import suppress
import json
import os
import struct
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import p2pearl.daemon as daemon_mod  # noqa: E402
from p2pearl.consensus.share import ShareBlock  # noqa: E402
from p2pearl.consensus.sharechain import GENESIS_PREV, Sharechain  # noqa: E402
from p2pearl.daemon import (  # noqa: E402
    ParentTemplate,
    PoolNode,
    _ensure_prover_env,
    format_payout_estimate,
    payout_estimate_snapshot,
    serialize_payouts,
)
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


class _SlowStratum:
    def __init__(self):
        self.called = asyncio.Event()
        self.release = asyncio.Event()
        self.count = 0
        self._job_builder = None

    def set_job_builder(self, builder):
        self._job_builder = builder

    async def refresh(self):
        self.count += 1
        self.called.set()
        await self.release.wait()


class _SlowBroadcast:
    def __init__(self):
        self.called = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def __call__(self, *args):
        self.calls.append(args)
        self.called.set()
        await self.release.wait()


class _SlowVerifyShare:
    def __init__(self, result=True):
        self.result = result
        self.called = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args):
        self.called.set()
        self.release.wait()
        return self.result


class _RefreshRecorder:
    def __init__(self):
        self.called = asyncio.Event()
        self.count = 0

    def set_job_builder(self, builder):
        self._job_builder = builder

    async def refresh(self):
        self.count += 1
        self.called.set()


class _StaticGBT:
    def __init__(self):
        self.calls = 0

    def get_block_template(self):
        self.calls += 1
        return {
            "height": 1000,
            "previousblockhash": "22" * 32,
            "bits": "1e01ffff",
            "curtime": 1_700_000_000,
            "coinbasevalue": 5_000_000_000,
        }


async def _noop(*args):
    return None


async def _drain_background(node):
    while node._background_tasks:
        await asyncio.gather(*list(node._background_tasks))
        await asyncio.sleep(0)


def _node(sc, *, verify_share=True, verify_block=False, assemble="BLOCKHEX",
          submit_block=None, broadcast_share=None, broadcast_block=None, stratum=None,
          stratum_target_factor=1):
    verify_share_fn = verify_share if callable(verify_share) else (lambda *a: verify_share)
    verify_block_fn = verify_block if callable(verify_block) else (lambda *a: verify_block)
    return PoolNode(
        sharechain=sc,
        make_header=_fake_make_header,
        verify_share=verify_share_fn,
        verify_block=verify_block_fn,
        assemble_block=(lambda ctx, proof: assemble),
        submit_block=submit_block or _noop,
        broadcast_share=broadcast_share,
        broadcast_block=broadcast_block,
        stratum=stratum,
        stratum_target_factor=stratum_target_factor,
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


def test_build_job_for_applies_stratum_target_factor():
    sc = Sharechain(window=10, bootstrap_target=SHARE_TARGET)
    node = _node(sc, stratum_target_factor=8)
    node.set_template(TEMPLATE)
    spec = node.build_job_for(ADDR_A)
    assert spec is not None
    _header_hex, target, _height, ctx = spec
    assert target == SHARE_TARGET // 8
    assert ctx.candidate.share_target == SHARE_TARGET // 8
    assert ctx.candidate.target_limit == SHARE_TARGET


def test_vardiff_hardens_fast_worker(monkeypatch):
    sc = Sharechain(window=10, bootstrap_target=SHARE_TARGET)
    node = _node(sc, stratum_target_factor=8)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: clock["now"])
    sub = Submission(ADDR_A, "rigA", StratumJob("j", "00" * 76, SHARE_TARGET, 0), PROOF_B64)

    assert not node._record_vardiff_share(sub)
    clock["now"] += 10
    assert node._record_vardiff_share(sub)
    assert node._worker_target_factor(ADDR_A, "rigA") == 16
    assert node._worker_target_factor(ADDR_A, "rigB") == 8

    node.set_template(TEMPLATE)
    _header_hex, target_a, _height, ctx_a = node.build_job_for(ADDR_A, "rigA")
    _header_hex, target_b, _height, ctx_b = node.build_job_for(ADDR_A, "rigB")
    assert target_a == SHARE_TARGET // 16
    assert target_b == SHARE_TARGET // 8
    assert ctx_a.candidate.target_limit == ctx_b.candidate.target_limit == SHARE_TARGET


def test_vardiff_eases_slow_worker(monkeypatch):
    node = _node(Sharechain(window=10, bootstrap_target=SHARE_TARGET), stratum_target_factor=8)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: clock["now"])
    sub = Submission(ADDR_A, "rigA", StratumJob("j", "00" * 76, SHARE_TARGET, 0), PROOF_B64)

    node._record_vardiff_share(sub)
    clock["now"] += 180
    assert node._record_vardiff_share(sub)
    assert node._worker_target_factor(ADDR_A, "rigA") == 4


def test_jobs_use_live_monotonic_timestamps_for_retarget(monkeypatch):
    asyncio.run(_jobs_use_live_monotonic_timestamps_for_retarget(monkeypatch))


async def _jobs_use_live_monotonic_timestamps_for_retarget(monkeypatch):
    template = ParentTemplate(
        height=1000, prev_block=b"\x22" * 32, bits=0x1E01FFFF,
        curtime=1_700_000_000, coinbase_value=5_000_000_000,
    )
    stable_target = daemon_mod.config.BOOTSTRAP_SHARE_TARGET
    sc = Sharechain(window=10, bootstrap_target=stable_target)
    node = _node(sc, verify_block=False)
    node.set_template(template)
    clock = {"now": template.curtime}
    monkeypatch.setattr(daemon_mod.time, "time", lambda: clock["now"])

    async def mine_one(job_id):
        spec = node.build_job_for(ADDR_A)
        assert spec is not None
        header_hex, target, height, ctx = spec
        header_ts = struct.unpack_from("<I", bytes.fromhex(header_hex), 68)[0]
        assert header_ts == ctx.candidate.timestamp
        result = await node.handle_submit(
            Submission(ADDR_A, "rig", StratumJob(job_id, header_hex, target, height, ctx), PROOF_B64))
        assert result.accepted
        await _drain_background(node)
        return target, ctx.candidate.timestamp

    first_target, first_ts = await mine_one("retarget-1")
    clock["now"] += 10
    second_target, second_ts = await mine_one("retarget-2")
    clock["now"] += 10
    third = node.build_job_for(ADDR_A)
    assert third is not None
    third_header, third_target, _, third_ctx = third

    assert first_target == second_target == third_target == stable_target
    assert (first_ts, second_ts, third_ctx.candidate.timestamp) == (
        template.curtime, template.curtime + 10, template.curtime + 20)
    assert struct.unpack_from("<I", bytes.fromhex(third_header), 68)[0] == third_ctx.candidate.timestamp


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


def test_ensure_prover_env_sets_cuda_defaults(monkeypatch):
    for key in ("RAYON_NUM_THREADS", "_RJEM_MALLOC_CONF", "NUM_OF_GPUS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(daemon_mod, "_preload_wsl_nvidia_driver_libs", lambda: ())

    _ensure_prover_env()

    assert os.environ["RAYON_NUM_THREADS"] == str(os.cpu_count() or 1)
    assert os.environ["_RJEM_MALLOC_CONF"] == "background_thread:true"
    assert os.environ["NUM_OF_GPUS"] == "1"


def test_run_survives_transient_template_rpc_failure():
    asyncio.run(_run_survives_transient_template_rpc_failure())


async def _run_survives_transient_template_rpc_failure():
    class FlakyRPC:
        def __init__(self):
            self.calls = 0

        def get_block_template(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary RPC refusal")
            return {
                "height": 1000,
                "previousblockhash": "22" * 32,
                "bits": "1e01ffff",
                "curtime": 123,
                "coinbasevalue": 5_000_000_000,
            }

    stratum = _RefreshRecorder()
    node = _node(Sharechain(window=10), stratum=stratum)
    rpc = FlakyRPC()
    task = asyncio.create_task(node.run(rpc, poll_interval=0.01))
    try:
        await asyncio.wait_for(stratum.called.wait(), timeout=1.0)
        assert rpc.calls >= 2
        assert node._template is not None
        assert stratum.count == 1
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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


def test_payout_estimate_snapshot_after_shares(monkeypatch):
    sc = Sharechain(window=10, bootstrap_target=SHARE_TARGET)
    node = _node(sc)
    node.set_template(TEMPLATE)
    clock = {"now": TEMPLATE.curtime}
    monkeypatch.setattr(daemon_mod.time, "time", lambda: clock["now"])

    for addr in (ADDR_A, ADDR_B):
        header_hex, target, height, ctx = node.build_job_for(addr)
        asyncio.run(node.handle_submit(
            Submission(addr, "rig", StratumJob(addr[-4:], header_hex, target, height, ctx), PROOF_B64)))
        clock["now"] += 10

    snapshot = payout_estimate_snapshot(sc, block_reward_grains=1_000_000_000, min_payout_grains=0)
    assert snapshot["window_shares"] == 2
    assert snapshot["window_max"] == 10
    assert snapshot["total_weight"] > 0
    rows = {row["address"]: row for row in snapshot["addresses"]}
    assert rows[ADDR_A]["percent_bps"] == rows[ADDR_B]["percent_bps"] == 5000
    assert rows[ADDR_A]["estimated_grains"] == rows[ADDR_B]["estimated_grains"] == 500_000_000
    assert "50.00%" in format_payout_estimate(snapshot)


# --------------------------------------------------------------------------- #
# Unit: submit handling
# --------------------------------------------------------------------------- #

def test_handle_submit_accepts_share():
    asyncio.run(_share_flow())


def test_handle_submit_ack_not_blocked_by_stratum_refresh():
    asyncio.run(_submit_ack_not_blocked_by_refresh())


def test_handle_submit_ack_not_blocked_by_share_gossip():
    asyncio.run(_submit_ack_not_blocked_by_share_gossip())


def test_handle_submit_ack_not_blocked_by_share_verification():
    asyncio.run(_submit_ack_not_blocked_by_share_verification())


def test_best_tip_refresh_not_blocked_by_block_check():
    asyncio.run(_best_tip_refresh_not_blocked_by_block_check())


def test_stratum_refresh_requests_are_coalesced():
    asyncio.run(_stratum_refresh_requests_are_coalesced())


def test_run_refreshes_same_parent_for_time_based_target(monkeypatch):
    asyncio.run(_run_refreshes_same_parent_for_time_based_target(monkeypatch))


async def _submit_ack_not_blocked_by_refresh():
    sc = Sharechain(window=10)
    stratum = _SlowStratum()
    node = _node(sc, verify_block=False, stratum=stratum)
    node.set_template(TEMPLATE)
    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    job = StratumJob("00001000-ack", header_hex, target, height, ctx)

    res = await asyncio.wait_for(
        node.handle_submit(Submission(ADDR_A, "rigA", job, PROOF_B64)),
        timeout=0.2,
    )

    assert res.accepted
    await asyncio.wait_for(stratum.called.wait(), timeout=0.2)
    stratum.release.set()
    await _drain_background(node)


async def _submit_ack_not_blocked_by_share_gossip():
    sc = Sharechain(window=10)
    broadcast = _SlowBroadcast()
    node = _node(sc, verify_block=False, broadcast_share=broadcast)
    node.set_template(TEMPLATE)
    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    job = StratumJob("00001000-gossip", header_hex, target, height, ctx)

    res = await asyncio.wait_for(
        node.handle_submit(Submission(ADDR_A, "rigA", job, PROOF_B64)),
        timeout=0.2,
    )

    assert res.accepted
    await asyncio.wait_for(broadcast.called.wait(), timeout=0.2)
    broadcast.release.set()
    await _drain_background(node)


async def _submit_ack_not_blocked_by_share_verification():
    sc = Sharechain(window=10)
    verify = _SlowVerifyShare()
    node = _node(sc, verify_share=verify, verify_block=False)
    node.set_template(TEMPLATE)
    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    job = StratumJob("00001000-verify", header_hex, target, height, ctx)

    res = await asyncio.wait_for(
        node.handle_submit(Submission(ADDR_A, "rigA", job, PROOF_B64)),
        timeout=0.2,
    )

    assert res.accepted
    assert len(sc) == 0
    assert await asyncio.to_thread(verify.called.wait, 0.2)
    verify.release.set()
    await _drain_background(node)
    assert len(sc) == 1


async def _best_tip_refresh_not_blocked_by_block_check():
    sc = Sharechain(window=10)
    stratum = _SlowStratum()
    block_check = _SlowVerifyShare(result=False)
    node = _node(sc, verify_block=block_check, stratum=stratum)
    node.set_template(TEMPLATE)
    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    job = StratumJob("00001000-refresh", header_hex, target, height, ctx)

    res = await node.handle_submit(Submission(ADDR_A, "rigA", job, PROOF_B64))

    assert res.accepted
    await asyncio.wait_for(stratum.called.wait(), timeout=0.2)
    assert await asyncio.to_thread(block_check.called.wait, 0.2)
    stratum.release.set()
    block_check.release.set()
    await _drain_background(node)


async def _stratum_refresh_requests_are_coalesced():
    sc = Sharechain(window=10)
    stratum = _SlowStratum()
    node = _node(sc, verify_block=False, stratum=stratum)
    node.set_template(TEMPLATE)

    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    first = await node.handle_submit(
        Submission(ADDR_A, "rigA", StratumJob("00001000-a", header_hex, target, height, ctx), PROOF_B64))
    assert first.accepted
    await asyncio.wait_for(stratum.called.wait(), timeout=0.2)
    assert stratum.count == 1

    header_hex, target, height, ctx = node.build_job_for(ADDR_B)
    second = await node.handle_submit(
        Submission(ADDR_B, "rigB", StratumJob("00001000-b", header_hex, target, height, ctx), PROOF_B64))
    assert second.accepted
    assert stratum.count == 1

    stratum.release.set()
    await asyncio.wait_for(_drain_background(node), timeout=0.2)
    assert stratum.count == 2


async def _run_refreshes_same_parent_for_time_based_target(monkeypatch):
    stratum = _RefreshRecorder()
    node = _node(Sharechain(window=10), stratum=stratum)
    monkeypatch.setattr(daemon_mod.config, "SHARE_TARGET_TIME_SECONDS", 0.01)
    task = asyncio.create_task(node.run(_StaticGBT(), poll_interval=0.005))
    try:
        deadline = asyncio.get_running_loop().time() + 0.2
        while stratum.count < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        assert stratum.count >= 2
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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
    await _drain_background(node)
    assert len(sc) == 1 and sc.tip().miner_address == ADDR_A
    assert len(bshare.calls) == 1                 # share gossiped
    assert not bblock.calls and not sblock.calls  # not a block


def test_handle_submit_duplicate_same_job_is_idempotent():
    asyncio.run(_duplicate_same_job_flow())


async def _duplicate_same_job_flow():
    sc = Sharechain(window=10)
    bshare = _Rec()
    node = _node(sc, verify_block=False, broadcast_share=bshare)
    node.set_template(TEMPLATE)
    header_hex, target, height, ctx = node.build_job_for(ADDR_A)
    job = StratumJob("00001000-0001", header_hex, target, height, ctx)

    first = await node.handle_submit(Submission(ADDR_A, "rigA", job, PROOF_B64))
    second = await node.handle_submit(Submission(ADDR_A, "rigA", job, PROOF_B64))

    assert first.accepted and second.accepted
    await _drain_background(node)
    assert len(sc) == 1
    assert len(bshare.calls) == 1


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
    await _drain_background(node)
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
    await _drain_background(node)
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

    await _drain_background(node)
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
    await _drain_background(node)
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
    await _drain_background(node)
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
    assert res.accepted                                # fast ACK; validation is post-ACK
    await _drain_background(node)
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
