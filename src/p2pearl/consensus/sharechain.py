"""The sharechain: store, validate, select, and reorg P2Pearl shares.

This is the net-new consensus engine — there is no equivalent in the Pearl repo.
It corresponds to Monero P2Pool's ``src/side_chain.cpp``. Interfaces are defined
here; the logic is the main build item (see ROADMAP, "Sidechain engine").

Design (from docs/blueprint.md §4.2, §4.6):
  * Shares form a chain via ``prev_share_id``; GHOST ``uncle_ids`` fold in
    competing same-height work (penalized, referenceable up to UNCLE_BLOCK_DEPTH
    heights back).
  * Chain selection = highest cumulative sidechain difficulty.
  * ``pplns_weights`` walks the last ``window`` shares and sums each address's
    (uncle-penalized) share difficulty — the input to ``compute_pplns_payouts``.
"""

from __future__ import annotations

from .. import config
from .pplns import uncle_weight
from .share import ShareBlock


class Sharechain:
    def __init__(self, window: int = config.PPLNS_WINDOW_SHARES) -> None:
        self.window = window
        self._by_id: dict[bytes, ShareBlock] = {}
        self._tip_id: bytes | None = None

    # --- mutation -----------------------------------------------------------
    def add_share(self, share: ShareBlock, *, verified: bool) -> bool:
        """Validate (structure + linkage + PoW-already-verified) and insert.

        Returns True if it became (or extended toward) the best tip. Must reject
        shares with unknown parents, bad uncle depth, or stale parent anchors.
        """
        raise NotImplementedError("sidechain engine — see ROADMAP 'Sidechain engine'")

    # --- queries ------------------------------------------------------------
    def tip(self) -> ShareBlock | None:
        return self._by_id.get(self._tip_id) if self._tip_id else None

    def pplns_weights(self) -> list[tuple[str, int]]:
        """Sum (uncle-penalized) share difficulty per address over the PPLNS window.

        Feeds ``compute_pplns_payouts``. ``uncle_weight`` applies the penalty.
        """
        raise NotImplementedError("sidechain engine — see ROADMAP 'Sidechain engine'")

    def is_valid_successor(self, share: ShareBlock) -> bool:
        raise NotImplementedError("sidechain engine — see ROADMAP 'Sidechain engine'")

    def cumulative_difficulty(self, share_id: bytes) -> int:
        raise NotImplementedError("sidechain engine — see ROADMAP 'Sidechain engine'")
