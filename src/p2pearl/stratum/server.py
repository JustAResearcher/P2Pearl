"""Miner-facing stratum server for P2Pearl.

Presents Pearlhash work to GPU miners (SRBMiner ``--algorithm pearlhash`` et al.)
and routes submitted shares to an injected handler. The server is transport +
protocol only — it knows nothing about proofs or the sharechain. Two seams connect
it to the rest of P2Pearl (the daemon wires them in M3):

  * ``update_job(header_hex, share_target, height)`` — call on every new parent or
    sidechain tip to mint a job and broadcast ``mining.notify`` to all miners.
  * ``submit_handler`` — an async callback invoked per submitted share; it returns a
    :class:`SubmitResult` the server turns into an ack or a stratum error. The daemon
    wires this to ``pow.verify.verify_share`` -> ``sharechain.add_share`` -> (maybe)
    block submission.

It tolerates BOTH Pearlhash stratum dialects (object / LuckyPool / Herominers, which
SRBMiner speaks, and positional / alphapool) so the production fleet connects with no
miner changes — see ``protocol`` and ``docs/blueprint.md`` §4.7. Architecture mirrors
the Pearl repo's ``pearl-stratum-srv`` (async listener, per-connection task, bounded
job registry, mint->broadcast seam), minus the public-pool concerns (vardiff,
``pearl.challenge``) which the P2P layer handles instead.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from . import protocol as P

_LOGGER = logging.getLogger(__name__)


def _submit_timing_threshold_ms() -> float:
    try:
        return float(os.environ.get("P2PEARL_SUBMIT_TIMING_MS", "75"))
    except ValueError:
        return 75.0


def _trace_all_submits() -> bool:
    return os.environ.get("P2PEARL_TRACE_SUBMIT", "").lower() in {"1", "true", "yes", "on"}


def _emit_submit_timing(
    conn: "_Connection",
    req_id: object,
    job_id: str | None,
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
        "conn_id": conn.conn_id,
        "peer": str(conn.writer.get_extra_info("peername")),
        "request_id": req_id,
        "job_id": job_id,
        "worker": conn.worker_label,
        "accepted": accepted,
        "reason": reason,
        "total_ms": round(total_ms, 3),
        "phases_ms": {label: round(ms, 3) for label, ms in phases},
    }
    print("P2PEARL_STRATUM_TIMING " + json.dumps(payload, separators=(",", ":")), flush=True)

# A ``mining.submit`` frame carries a 137-368 KB base64 plain_proof on ONE line;
# asyncio's default 64 KiB readline limit would raise LimitOverrunError on the first
# real submit, so the listener must be opened with a larger stream limit.
READ_LIMIT = 2 ** 20
SEND_TIMEOUT_SECONDS = 5.0

_VALID_HRPS = ("prl1", "tprl1", "sprl1", "rprl1")
_BECH32_CHARSET = frozenset("qpzry9x8gf2tvdw0s3jn54khce6mua7l")


def parse_worker(raw: str) -> tuple[str | None, str]:
    """Split a worker login ``prl1...address[.label]`` into ``(address, label)``.

    Lenient bech32m sanity only — a wrong address merely mis-credits the miner; the
    sharechain/coinbase enforce real P2TR validity downstream. Returns ``(None, raw)``
    if the address part is not a plausible Pearl address.
    """
    addr, _, label = raw.partition(".")
    label = label or "default"
    hrp = next((h for h in _VALID_HRPS if addr.startswith(h)), None)
    if hrp is None or not (50 <= len(addr) <= 100):
        return None, raw
    data = addr[len(hrp):]
    if data and all(c in _BECH32_CHARSET for c in data):
        return addr, label
    return None, raw


@dataclass
class StratumJob:
    job_id: str
    incomplete_header_hex: str   # 76-byte incomplete block header, hex (152 chars)
    share_target: int            # 256-bit share threshold
    height: int
    context: object | None = None  # opaque payload the job builder attaches (the candidate ShareBlock)

    def _header(self) -> bytes:
        return bytes.fromhex(self.incomplete_header_hex)

    @property
    def prev_hash_hex(self) -> str:
        return self._header()[4:36].hex()

    @property
    def ntime_hex(self) -> str:
        return f"{int.from_bytes(self._header()[68:72], 'little'):08x}"

    @property
    def nbits_hex(self) -> str:
        return f"{int.from_bytes(self._header()[72:76], 'little'):08x}"

    @property
    def target_hex(self) -> str:
        return f"{self.share_target:064x}"

    def notify_params(self, dialect: str, clean: bool):
        if dialect == "positional":
            return [self.job_id, self.prev_hash_hex, self.incomplete_header_hex, 0,
                    self.ntime_hex, self.nbits_hex, clean]
        return {"job_id": self.job_id, "header": self.incomplete_header_hex,
                "target": self.target_hex, "height": self.height}


@dataclass
class Submission:
    worker_address: str | None
    worker_label: str
    job: StratumJob
    plain_proof_b64: str


@dataclass
class SubmitResult:
    accepted: bool
    error_code: int | None = None
    error_message: str | None = None


SubmitHandler = Callable[["Submission"], Awaitable["SubmitResult"]]


class JobRegistry:
    """Bounded ``job_id -> StratumJob`` map. Stale == key-absent (see error 21)."""

    def __init__(self, max_size: int = 16) -> None:
        self._jobs: "OrderedDict[str, StratumJob]" = OrderedDict()
        self._seq = 0
        self._max = max_size

    def mint(self, incomplete_header_hex: str, share_target: int, height: int,
             context: object | None = None) -> StratumJob:
        self._seq = (self._seq + 1) & 0xFFFF
        job_id = f"{height & 0xFFFFFFFF:08x}-{self._seq:04x}"
        job = StratumJob(job_id, incomplete_header_hex, share_target, height, context)
        self._jobs[job_id] = job
        while len(self._jobs) > self._max:
            self._jobs.popitem(last=False)
        return job

    def get(self, job_id: str) -> StratumJob | None:
        return self._jobs.get(job_id)

    def latest(self) -> StratumJob | None:
        if not self._jobs:
            return None
        return next(reversed(self._jobs.values()))


class _Connection:
    def __init__(self, server: "StratumServer", reader, writer, conn_id: int) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.conn_id = conn_id
        self.dialect = "object"           # default; set on subscribe/authorize
        self.subscribed = False
        self.authorized = False
        self.worker_address: str | None = None
        self.worker_label = "default"
        self._send_lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self.authorized or self.subscribed

    async def send(self, frame: bytes) -> None:
        # Serialize writes so a broadcast notify can't interleave with a reply.
        async with self._send_lock:
            if self.writer.is_closing():
                raise ConnectionError("connection closing")
            self.writer.write(frame)
            try:
                await asyncio.wait_for(self.writer.drain(), timeout=SEND_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                self.close()
                raise ConnectionError("send timed out") from exc
            except (ConnectionError, OSError):
                self.close()
                raise

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass

    async def run(self) -> None:
        while True:
            try:
                line = await self.reader.readline()
            except (ConnectionError, OSError):
                break
            if not line:
                break          # EOF
            if not line.strip():
                continue
            try:
                await self._dispatch(line)
            except (ConnectionError, OSError):
                break

    async def _dispatch(self, line: bytes) -> None:
        try:
            req = P.parse_request(line)
        except Exception as exc:
            await self.send(P.encode_error(None, P.INVALID_PARAMS_CODE, f"bad request: {exc}"))
            return
        handler = self._HANDLERS.get(req.method)
        if handler is None:
            # Tolerant: ack unknown methods true (the proven bridge does this so a
            # miner sending extra/unknown methods still connects unchanged).
            await self.send(P.encode_response(req.id, True))
            return
        try:
            await handler(self, req)
        except (ConnectionError, OSError):
            raise
        except Exception as exc:
            _LOGGER.exception("conn %d handler %s failed", self.conn_id, req.method)
            await self.send(P.encode_error(req.id, P.INVALID_PARAMS_CODE, str(exc)))

    async def _handle_configure(self, req: "P.Request") -> None:
        self.dialect = "positional"
        await self.send(P.encode_response(
            req.id, {"pearl/v1": True, "pearl/v1.share_format": "base64"}))

    async def _handle_subscribe(self, req: "P.Request") -> None:
        self.dialect = "positional"
        self.subscribed = True
        tag = f"{self.conn_id:08x}"
        await self.send(P.encode_response(
            req.id, [[["mining.set_difficulty", tag], ["mining.notify", tag]], "", 0]))
        await self.push_job(clean=True)

    async def _handle_challenge_response(self, req: "P.Request") -> None:
        # We never challenge our own fleet; accept immediately.
        await self.send(P.encode_response(req.id, {"result": True}))

    async def _handle_authorize(self, req: "P.Request") -> None:
        login, worker, dialect = P.parse_authorize(req.params)
        self.dialect = dialect
        raw = login if ("." in login or not worker) else f"{login}.{worker}"
        self.worker_address, self.worker_label = parse_worker(raw)
        self.authorized = True
        await self.send(P.encode_response(req.id, True))
        await self.push_job(clean=True)

    async def _handle_submit(self, req: "P.Request") -> None:
        started_at = time.perf_counter()
        last_mark = started_at
        phases: list[tuple[str, float]] = []
        job_id: str | None = None

        def mark(label: str) -> None:
            nonlocal last_mark
            now = time.perf_counter()
            phases.append((label, (now - last_mark) * 1000.0))
            last_mark = now

        async def finish(frame: bytes, accepted: bool, reason: str | None) -> None:
            await self.send(frame)
            mark("send_response")
            _emit_submit_timing(self, req.id, job_id, accepted, reason, started_at, phases)

        try:
            job_id, proof_b64 = P.parse_submit(req.params)
        except Exception as exc:
            mark("parse_submit")
            await finish(P.encode_error(req.id, P.INVALID_PARAMS_CODE, str(exc)), False, str(exc))
            return
        mark("parse_submit")
        job = self.server.registry.get(job_id)
        if job is None:
            mark("lookup_job")
            await finish(P.encode_error(req.id, P.STALE_SHARE_CODE, "Job not found"), False, "Job not found")
            return
        mark("lookup_job")
        try:
            base64.b64decode(proof_b64, validate=True)
        except Exception as exc:
            mark("base64_decode")
            reason = f"bad plain_proof: {exc}"
            await finish(P.encode_error(req.id, P.INVALID_PARAMS_CODE, reason), False, reason)
            return
        mark("base64_decode")
        submission = Submission(self.worker_address, self.worker_label, job, proof_b64)
        result = await self.server._on_submit(submission)
        mark("submit_handler")
        if result.accepted:
            await finish(P.encode_response(req.id, True), True, None)
        else:
            reason = result.error_message or "rejected"
            await finish(P.encode_error(
                req.id, result.error_code or P.LOW_DIFF_CODE,
                reason), False, reason)

    async def push_job(self, clean: bool) -> None:
        if not self.ready:
            return
        job = self.server._mint_job_for(self)
        if job is None:
            return
        await self.send(P.encode_notification("mining.notify", job.notify_params(self.dialect, clean)))

    _HANDLERS = {
        "mining.configure": _handle_configure,
        "mining.subscribe": _handle_subscribe,
        "mining.authorize": _handle_authorize,
        "mining.submit": _handle_submit,
        "pearl.challenge_response": _handle_challenge_response,
    }


class StratumServer:
    def __init__(
        self,
        submit_handler: SubmitHandler,
        host: str = "0.0.0.0",
        port: int = 3360,
        job_history: int = 16,
    ) -> None:
        self._on_submit = submit_handler
        self.host = host
        self.port = port
        self.registry = JobRegistry(job_history)
        self._conns: set[_Connection] = set()
        self._server: asyncio.AbstractServer | None = None
        self._conn_seq = 0
        self._job_builder: Callable[[str | None], tuple | None] | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_client, host=self.host, port=self.port, limit=READ_LIMIT)
        # Reflect the actually-bound port (useful when port=0 in tests).
        self.port = self._server.sockets[0].getsockname()[1]

    @property
    def connection_count(self) -> int:
        return len(self._conns)

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("call start() before serve_forever()")
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def set_job_builder(self, builder: "Callable[[str | None], tuple | None]") -> None:
        """Register a per-connection job source for P2Pool-style per-miner coinbases.

        ``builder(worker_address) -> (incomplete_header_hex, share_target, height, context)``
        or ``None``. With a builder set, each miner gets its OWN job — its coinbase pays the
        PPLNS window and commits a share crediting that miner — so call ``refresh()`` on a tip
        change rather than ``update_job()``.
        """
        self._job_builder = builder

    def _mint_job_for(self, conn: "_Connection") -> "StratumJob | None":
        if self._job_builder is not None:
            spec = self._job_builder(conn.worker_address)
            return None if spec is None else self.registry.mint(*spec)
        return self.registry.latest()

    async def refresh(self) -> None:
        """Re-mint and broadcast a fresh per-connection job to every ready miner (tip change)."""
        await self._broadcast(clean=True)

    async def update_job(self, incomplete_header_hex: str, share_target: int, height: int) -> StratumJob:
        """Mint a single global job and broadcast it (the simple / no-job-builder path)."""
        job = self.registry.mint(incomplete_header_hex, share_target, height)
        await self._broadcast(clean=True)
        return job

    async def _broadcast(self, clean: bool) -> None:
        conns = [conn for conn in sorted(self._conns, key=lambda c: c.conn_id) if conn.ready]
        if not conns:
            return

        async def push(conn: _Connection) -> None:
            try:
                await conn.push_job(clean=clean)
            except (ConnectionError, OSError) as exc:
                _LOGGER.info("dropping conn %d during broadcast: %s", conn.conn_id, exc)
                conn.close()
            except Exception:
                _LOGGER.exception("broadcast to conn %d failed", conn.conn_id)
                conn.close()

        await asyncio.gather(*(push(conn) for conn in conns))

    async def _on_client(self, reader, writer) -> None:
        self._conn_seq += 1
        conn = _Connection(self, reader, writer, self._conn_seq)
        self._conns.add(conn)
        try:
            await conn.run()
        finally:
            self._conns.discard(conn)
            conn.close()
