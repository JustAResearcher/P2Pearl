"""Minimal JSON-RPC client for a local pearld full node.

Implements only the two calls P2Pearl needs:
  * getblocktemplate — learn the parent tip, mempool transactions, target, and
    coinbase value (we request ``coinbasevalue`` so *we* build the coinbase).
  * submitblock — publish a found Pearl block.

Call shape mirrors the Pearl gateway's pearl_client.py. Stdlib only (urllib); no
third-party HTTP dependency.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

from ..config import NodeRPCConfig


class NodeRPCError(RuntimeError):
    """A pearld RPC call failed (transport error or RPC-level error object)."""


class NodeRPC:
    def __init__(self, cfg: NodeRPCConfig) -> None:
        self._cfg = cfg
        self._auth = base64.b64encode(f"{cfg.user}:{cfg.password}".encode()).decode()
        self._id = 0

    def _call(self, method: str, params: list[Any], timeout: float = 30.0) -> Any:
        self._id += 1
        payload = json.dumps(
            {"jsonrpc": "1.0", "id": self._id, "method": method, "params": params}
        ).encode()
        req = urllib.request.Request(
            self._cfg.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {self._auth}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # bitcoind-style daemons return the RPC error object even on HTTP 500.
            try:
                body = json.loads(exc.read())
            except Exception:
                raise NodeRPCError(f"{method}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise NodeRPCError(
                f"{method}: cannot reach node at {self._cfg.url}: {exc}"
            ) from exc
        if body.get("error"):
            raise NodeRPCError(f"{method}: {body['error']}")
        return body.get("result")

    def get_block_template(self) -> dict:
        """Bitcoin-style GBT; we supply the coinbase, so request coinbasevalue."""
        return self._call(
            "getblocktemplate",
            [{"capabilities": ["coinbasevalue", "coinbase/append"], "rules": ["segwit"]}],
        )

    def submit_block(self, block_hex: str) -> Any:
        """Submit a serialized Pearl block. pearld returns null on accept."""
        return self._call("submitblock", [block_hex])
