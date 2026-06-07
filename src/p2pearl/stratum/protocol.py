"""Stratum JSON-RPC framing + Pearlhash dialect parsing for the P2Pearl pool server.

The server tolerates BOTH Pearlhash stratum dialects so the existing fleet connects
unchanged:

  * "object" dialect (LuckyPool / Herominers — what SRBMiner speaks): authorize-first,
    params are JSON objects, e.g. ``mining.authorize {"wallet","worker","agent"}``,
    ``mining.notify {"job_id","header","target","height"}``,
    ``mining.submit {"job_id","plain_proof","hs"}``.
  * "positional" dialect (alphapool — what alpha-miner speaks): configure/subscribe
    first, params are JSON arrays.

Responses are bare ``{"id", "result"}`` (no ``jsonrpc`` field), which both dialects'
clients accept; server-initiated notifications omit ``id``. (Mirrors the framing in
the Pearl repo's pearl-stratum-srv/protocol.py.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Stratum reject codes (subset of pearl-stratum-srv/protocol.py).
STALE_SHARE_CODE = 21          # submit against an evicted / unknown job_id
LOW_DIFF_CODE = 23             # share does not clear the share target
UNKNOWN_METHOD_CODE = 25       # method not handled
INVALID_PARAMS_CODE = -32602   # malformed request / params / base64 proof


def encode_response(req_id: Any, result: Any) -> bytes:
    return (json.dumps({"id": req_id, "result": result}, separators=(",", ":")) + "\n").encode()


def encode_error(req_id: Any, code: int, message: str) -> bytes:
    payload = {"id": req_id, "result": None, "error": [code, message, None]}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def encode_notification(method: str, params: Any) -> bytes:
    payload = {"id": None, "method": method, "params": params}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


@dataclass
class Request:
    id: Any
    method: str
    params: Any


def parse_request(line: bytes) -> Request:
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("request is not a JSON object")
    method = obj.get("method")
    if not isinstance(method, str):
        raise ValueError("request missing string 'method'")
    return Request(obj.get("id"), method, obj.get("params"))


def parse_authorize(params: Any) -> tuple[str, str, str]:
    """Return ``(login, worker, dialect)``.

    object:     ``{"wallet": "prl1...", "worker": "rig", "agent": "..."}``
    positional: ``["prl1....rig", "password"]``
    """
    if isinstance(params, dict):
        return str(params.get("wallet", "")), str(params.get("worker", "")), "object"
    if isinstance(params, list) and params:
        return str(params[0]), "", "positional"
    raise ValueError("bad mining.authorize params")


def parse_submit(params: Any) -> tuple[str, str]:
    """Return ``(job_id, plain_proof_b64)`` from either dialect.

    object:     ``{"job_id": "...", "plain_proof": "<b64>", "hs": <float>}``
    positional: ``["worker", "job_id", "<b64>"]``
    """
    if isinstance(params, dict):
        return str(params["job_id"]), str(params["plain_proof"])
    if isinstance(params, list) and len(params) >= 3:
        return str(params[1]), str(params[2])
    raise ValueError("bad mining.submit params")
