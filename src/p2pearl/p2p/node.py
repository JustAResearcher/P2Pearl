"""P2P gossip layer: broadcast shares and found blocks; manage peers.

Net-new (see ROADMAP, "P2P network"). Modeled on P2Pool's broadcast protocol,
adapted to Pearl's large share payloads:

  * Gossip a compact SHARE ANNOUNCE (the ShareBlock, ~hundreds of bytes) first;
    peers fetch the bulky proof (~60 KB ZK / ~137-370 KB plain_proof) on demand,
    so a peer that already has the share never re-downloads the proof.
  * Only shares meeting pool difficulty are gossiped (the O(N^2) guard that makes
    the sharechain scale — see docs/blueprint.md §1.3).
  * DoS/Sybil: per-peer rate limits, ban on invalid proof/commitment, bounded
    reorg depth.

Message types (planned): HELLO/peer-exchange, SHARE_ANNOUNCE, GET_PROOF, PROOF,
BLOCK_FOUND, GET_SHARES (sync the PPLNS window on join).
"""

from __future__ import annotations

from collections.abc import Callable

from ..consensus.share import ShareBlock


class P2PNode:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._on_share: Callable[[ShareBlock, bytes], None] | None = None

    def on_share(self, callback: "Callable[[ShareBlock, bytes], None]") -> None:
        """Register a handler called with (share, proof_bytes) for each new share."""
        self._on_share = callback

    def start(self) -> None:
        raise NotImplementedError("P2P layer — see ROADMAP 'P2P network'")

    def broadcast_share(self, share: ShareBlock, proof_bytes: bytes) -> None:
        raise NotImplementedError("P2P layer — see ROADMAP 'P2P network'")

    def broadcast_block(self, block_hex: str) -> None:
        raise NotImplementedError("P2P layer — see ROADMAP 'P2P network'")
