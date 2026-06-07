"""Miner-facing stratum server.

P2Pearl presents work over the alphapool-compatible Pearlhash stratum dialect —
the one SRBMiner ``--algorithm pearlhash`` and alpha-miner already speak — so the
existing fleet connects with no miner changes (just repoint ``--pool``). The Pearl
repo's ``pearl_stratum_srv`` is the reference implementation to adapt (connection
handling, long-poll job push, vardiff, base64 ``plain_proof`` submit).

Key difference from a solo pool: the job's coinbase pays the PPLNS window (built by
``p2pearl.chain.coinbase``), and the ``share_target`` is the sidechain pool
difficulty. On a valid submit we verify (``p2pearl.pow.verify``), hand the share to
the sidechain + P2P layers, and if it also clears the block target, route it to
block submission.

Job push semantics (see docs/blueprint.md §4.7): broadcast ``mining.notify`` with
``clean_jobs=true`` on every new parent tip or new sidechain tip.
"""

from __future__ import annotations

from collections.abc import Callable


class StratumServer:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._on_submit: Callable[[str, str], None] | None = None

    def on_submit(self, callback: "Callable[[str, str], None]") -> None:
        """Register handler called with (worker_address, plain_proof_b64) per submit."""
        self._on_submit = callback

    def start(self) -> None:
        raise NotImplementedError("stratum front-end — see ROADMAP 'Stratum front-end'")

    def notify(self, incomplete_header_hex: str, share_target: int, *, clean: bool) -> None:
        """Push a new job (header template + sidechain share target) to all miners."""
        raise NotImplementedError("stratum front-end — see ROADMAP 'Stratum front-end'")
