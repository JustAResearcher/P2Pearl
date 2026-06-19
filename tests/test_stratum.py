"""Tests for the P2Pearl stratum server.

Each async scenario runs its own event loop via ``asyncio.run`` (no pytest-asyncio
dependency). A real loopback TCP socket exercises the full framing + dispatch; the
submit handler is a fake recorder, so no proofs/sharechain/node are needed.
"""

import asyncio
import base64
import json
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2pearl.stratum.server import (  # noqa: E402
    JobRegistry,
    StratumJob,
    StratumServer,
    SubmitResult,
    parse_worker,
)

# A plausible bech32m P2TR address (hrp prl1 + bech32 data chars).
ADDR = "prl1p" + "q" * 58
# A real 76-byte incomplete header: version | prev(32) | merkle(32) | timestamp | nbits.
_HDR = struct.pack("<I", 1) + b"\x11" * 32 + b"\x22" * 32 + struct.pack("<I", 0x499602D2) + struct.pack("<I", 0x1E01FFFF)
HEADER_HEX = _HDR.hex()
PROOF_B64 = base64.b64encode(b"plain-proof-bytes").decode()
SHARE_TARGET = 1 << 248


class _Recorder:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def __call__(self, submission):
        self.calls.append(submission)
        return self.result


async def _send(writer, obj):
    writer.write((json.dumps(obj) + "\n").encode())
    await writer.drain()


async def _recv(reader):
    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    return json.loads(line)


async def _serve(handler):
    server = StratumServer(handler, host="127.0.0.1", port=0)
    await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    return server, reader, writer


async def _teardown(server, writer):
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    await server.stop()


# --------------------------------------------------------------------------- #
# Unit tests (no socket)
# --------------------------------------------------------------------------- #

def test_parse_worker():
    addr, label = parse_worker(ADDR + ".rig.gpu0")
    assert addr == ADDR and label == "rig.gpu0"
    addr2, label2 = parse_worker(ADDR)
    assert addr2 == ADDR and label2 == "default"
    addr3, _ = parse_worker("notanaddress.worker")
    assert addr3 is None


def test_job_registry_eviction():
    reg = JobRegistry(max_size=2)
    j1 = reg.mint(HEADER_HEX, SHARE_TARGET, 1)
    reg.mint(HEADER_HEX, SHARE_TARGET, 2)
    j3 = reg.mint(HEADER_HEX, SHARE_TARGET, 3)
    assert reg.get(j1.job_id) is None          # oldest evicted
    assert reg.latest().job_id == j3.job_id


def test_job_derived_fields():
    job = StratumJob("00000001-0001", HEADER_HEX, SHARE_TARGET, 1000)
    assert job.ntime_hex == "499602d2"
    assert job.nbits_hex == "1e01ffff"
    assert job.prev_hash_hex == "11" * 32
    assert int(job.target_hex, 16) == SHARE_TARGET
    obj = job.notify_params("object", True)
    assert obj["header"] == HEADER_HEX and int(obj["target"], 16) == SHARE_TARGET
    pos = job.notify_params("positional", True)
    assert pos[0] == "00000001-0001" and pos[2] == HEADER_HEX and pos[6] is True


def test_broadcast_does_not_block_on_slow_connection():
    asyncio.run(_broadcast_not_blocked_by_slow_connection())


class _SlowPushConn:
    conn_id = 1
    ready = True

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def push_job(self, clean):
        self.started.set()
        await self.release.wait()

    def close(self):
        self.closed = True


class _FastPushConn:
    conn_id = 2
    ready = True

    def __init__(self):
        self.called = asyncio.Event()
        self.closed = False

    async def push_job(self, clean):
        self.called.set()

    def close(self):
        self.closed = True


async def _broadcast_not_blocked_by_slow_connection():
    server = StratumServer(_Recorder(SubmitResult(accepted=True)), host="127.0.0.1", port=0)
    slow = _SlowPushConn()
    fast = _FastPushConn()
    server._conns = {slow, fast}

    task = asyncio.create_task(server.refresh())
    await asyncio.wait_for(slow.started.wait(), timeout=0.2)
    await asyncio.wait_for(fast.called.wait(), timeout=0.2)
    slow.release.set()
    await asyncio.wait_for(task, timeout=0.2)
    assert not fast.closed


# --------------------------------------------------------------------------- #
# Object dialect (what SRBMiner speaks)
# --------------------------------------------------------------------------- #

def test_object_dialect_end_to_end():
    asyncio.run(_object_flow())


async def _object_flow():
    rec = _Recorder(SubmitResult(accepted=True))
    server, reader, writer = await _serve(rec)
    try:
        await _send(writer, {"id": 1, "method": "mining.authorize",
                             "params": {"wallet": ADDR, "worker": "rig1", "agent": "SRBMiner"}})
        resp = await _recv(reader)
        assert resp["id"] == 1 and resp["result"] is True

        job = await server.update_job(HEADER_HEX, SHARE_TARGET, 1000)
        notif = await _recv(reader)
        assert notif["method"] == "mining.notify"
        p = notif["params"]
        assert p["job_id"] == job.job_id and p["header"] == HEADER_HEX
        assert int(p["target"], 16) == SHARE_TARGET and p["height"] == 1000

        await _send(writer, {"id": 2, "method": "mining.submit",
                             "params": {"job_id": job.job_id, "plain_proof": PROOF_B64, "hs": 149.0}})
        ack = await _recv(reader)
        assert ack["id"] == 2 and ack["result"] is True

        assert len(rec.calls) == 1
        sub = rec.calls[0]
        assert sub.job.job_id == job.job_id
        assert sub.plain_proof_b64 == PROOF_B64
        assert sub.worker_address == ADDR and sub.worker_label == "rig1"
    finally:
        await _teardown(server, writer)


# --------------------------------------------------------------------------- #
# Positional dialect (alphapool / alpha-miner) tolerance
# --------------------------------------------------------------------------- #

def test_positional_dialect_end_to_end():
    asyncio.run(_positional_flow())


async def _positional_flow():
    rec = _Recorder(SubmitResult(accepted=True))
    server, reader, writer = await _serve(rec)
    try:
        await _send(writer, {"id": 1, "method": "mining.configure", "params": [["pearl/v1"], {}]})
        cfg = await _recv(reader)
        assert cfg["result"]["pearl/v1"] is True

        await _send(writer, {"id": 2, "method": "mining.subscribe", "params": ["alpha-miner/0.1"]})
        sub_resp = await _recv(reader)
        assert isinstance(sub_resp["result"], list) and sub_resp["result"][1] == ""

        await _send(writer, {"id": 3, "method": "mining.authorize", "params": [ADDR + ".rig", "x"]})
        auth = await _recv(reader)
        assert auth["result"] is True

        job = await server.update_job(HEADER_HEX, SHARE_TARGET, 2000)
        notif = await _recv(reader)
        assert notif["method"] == "mining.notify"
        params = notif["params"]
        assert isinstance(params, list) and params[0] == job.job_id and params[2] == HEADER_HEX

        await _send(writer, {"id": 4, "method": "mining.submit",
                             "params": ["rig", job.job_id, PROOF_B64]})
        ack = await _recv(reader)
        assert ack["id"] == 4 and ack["result"] is True
        assert rec.calls[0].worker_address == ADDR
    finally:
        await _teardown(server, writer)


# --------------------------------------------------------------------------- #
# Rejection paths
# --------------------------------------------------------------------------- #

def test_stale_job_rejected():
    asyncio.run(_stale_flow())


async def _stale_flow():
    rec = _Recorder(SubmitResult(accepted=True))
    server, reader, writer = await _serve(rec)
    try:
        await _send(writer, {"id": 1, "method": "mining.authorize", "params": {"wallet": ADDR, "worker": "r"}})
        await _recv(reader)
        await _send(writer, {"id": 2, "method": "mining.submit",
                             "params": {"job_id": "deadbeef-0001", "plain_proof": PROOF_B64, "hs": 1}})
        err = await _recv(reader)
        assert err["error"][0] == 21          # STALE_SHARE_CODE
        assert rec.calls == []                 # handler never called for a stale job
    finally:
        await _teardown(server, writer)


def test_malformed_proof_rejected():
    asyncio.run(_malformed_flow())


async def _malformed_flow():
    rec = _Recorder(SubmitResult(accepted=True))
    server, reader, writer = await _serve(rec)
    try:
        await _send(writer, {"id": 1, "method": "mining.authorize", "params": {"wallet": ADDR, "worker": "r"}})
        await _recv(reader)
        job = await server.update_job(HEADER_HEX, SHARE_TARGET, 1)
        await _recv(reader)  # notify
        await _send(writer, {"id": 2, "method": "mining.submit",
                             "params": {"job_id": job.job_id, "plain_proof": "@@@not-base64@@@", "hs": 1}})
        err = await _recv(reader)
        assert err["error"][0] == -32602       # INVALID_PARAMS_CODE
        assert rec.calls == []
    finally:
        await _teardown(server, writer)


def test_handler_rejection_becomes_error():
    asyncio.run(_reject_flow())


async def _reject_flow():
    rec = _Recorder(SubmitResult(accepted=False, error_code=23, error_message="low difficulty"))
    server, reader, writer = await _serve(rec)
    try:
        await _send(writer, {"id": 1, "method": "mining.authorize", "params": {"wallet": ADDR, "worker": "r"}})
        await _recv(reader)
        job = await server.update_job(HEADER_HEX, SHARE_TARGET, 1)
        await _recv(reader)
        await _send(writer, {"id": 2, "method": "mining.submit",
                             "params": {"job_id": job.job_id, "plain_proof": PROOF_B64, "hs": 1}})
        err = await _recv(reader)
        assert err["error"][0] == 23 and err["error"][1] == "low difficulty"
        assert len(rec.calls) == 1             # handler WAS called (it decided to reject)
    finally:
        await _teardown(server, writer)


def test_large_plain_proof_frame():
    # ~547 KB base64 line: exercises the 2**20 read limit (default 64 KiB would fail).
    asyncio.run(_large_flow())


def test_submit_timing_reports_frame_read(monkeypatch, capsys):
    monkeypatch.setenv("P2PEARL_TRACE_SUBMIT", "1")
    asyncio.run(_timing_flow())
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("P2PEARL_STRATUM_TIMING "))
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["job_id"]
    assert payload["frame_bytes"] > len(PROOF_B64)
    assert payload["wire_ms"] >= payload["total_ms"]
    assert "read_ms" in payload


async def _large_flow():
    big = base64.b64encode(b"x" * (400 * 1024)).decode()
    rec = _Recorder(SubmitResult(accepted=True))
    server, reader, writer = await _serve(rec)
    try:
        await _send(writer, {"id": 1, "method": "mining.authorize", "params": {"wallet": ADDR, "worker": "r"}})
        await _recv(reader)
        job = await server.update_job(HEADER_HEX, SHARE_TARGET, 1)
        await _recv(reader)
        await _send(writer, {"id": 2, "method": "mining.submit",
                             "params": {"job_id": job.job_id, "plain_proof": big, "hs": 1}})
        ack = await _recv(reader)
        assert ack["result"] is True
        assert rec.calls[0].plain_proof_b64 == big
    finally:
        await _teardown(server, writer)


async def _timing_flow():
    rec = _Recorder(SubmitResult(accepted=True))
    server, reader, writer = await _serve(rec)
    try:
        await _send(writer, {"id": 1, "method": "mining.authorize", "params": {"wallet": ADDR, "worker": "r"}})
        await _recv(reader)
        job = await server.update_job(HEADER_HEX, SHARE_TARGET, 1)
        await _recv(reader)
        await _send(writer, {"id": 2, "method": "mining.submit",
                             "params": {"job_id": job.job_id, "plain_proof": PROOF_B64, "hs": 1}})
        ack = await _recv(reader)
        assert ack["result"] is True
    finally:
        await _teardown(server, writer)


def test_unknown_method_tolerated():
    asyncio.run(_unknown_flow())


async def _unknown_flow():
    rec = _Recorder(SubmitResult(accepted=True))
    server, reader, writer = await _serve(rec)
    try:
        await _send(writer, {"id": 9, "method": "mining.ping", "params": []})
        resp = await _recv(reader)
        assert resp["id"] == 9 and resp["result"] is True   # tolerant ack
    finally:
        await _teardown(server, writer)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
