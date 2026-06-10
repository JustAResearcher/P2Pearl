"""The sharechain: store, validate, select, and reorg P2Pearl shares.

Net-new consensus engine (no equivalent in the Pearl repo); corresponds to Monero
P2Pool's ``src/side_chain.cpp``, adapted to P2Pearl's ``ShareBlock`` and Pearl's
facts.

Responsibilities (ROADMAP M1):
  * Linkage + structural validation of incoming shares.
  * GHOST uncles: a share may reference recent off-chain siblings (within
    ``uncle_depth``) so their work is not wasted; uncle work counts toward chain
    weight in full, and toward PPLNS payout at a penalty.
  * Cumulative-difficulty chain selection (best tip = most total work, including
    uncles); reorg is implicit — queries follow ``prev_share_id`` from the best
    tip, so switching the tip switches the active chain.
  * PPLNS weight walk over the last ``window`` main-chain shares.
  * Pruning of shares (and their proofs) older than the retention horizon.

PoW is NOT checked here — callers verify it (``p2pearl.pow.verify``) and pass
``verified=True``; the engine trusts that flag and enforces everything else.

Note: any height-0 share with the genesis predecessor is accepted as a genesis.
A production sidechain pins a single hardcoded genesis id; that check is a small
hardening left for integration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .. import config
from .difficulty import retarget_target
from .pplns import uncle_weight
from .share import ShareBlock
from .subsidy import block_subsidy

# The predecessor id of a genesis share (no parent).
GENESIS_PREV = b"\x00" * 32


@dataclass(frozen=True)
class AddResult:
    """Outcome of ``add_share``."""

    accepted: bool
    is_best_tip: bool
    reason: str = ""


@dataclass
class _Entry:
    share: ShareBlock
    cumulative_difficulty: int
    proof: bytes | None = None


class Sharechain:
    def __init__(
        self,
        window: int = config.PPLNS_WINDOW_SHARES,
        uncle_depth: int = config.UNCLE_BLOCK_DEPTH,
        uncle_penalty_percent: int = config.UNCLE_PENALTY_PERCENT,
        bootstrap_target: int = config.BOOTSTRAP_SHARE_TARGET,
        share_time: int = config.SHARE_TARGET_TIME_SECONDS,
        retarget_window: int = config.RETARGET_WINDOW_SHARES,
    ) -> None:
        self.window = window
        self.uncle_depth = uncle_depth
        self.uncle_penalty_percent = uncle_penalty_percent
        self.bootstrap_target = bootstrap_target
        self.share_time = share_time
        self.retarget_window = retarget_window
        # Keep enough history for a full-window reorg plus a full PPLNS walk on the
        # new tip, plus uncle reach. Shares (and proofs) below this are pruned.
        self._retention = 2 * window + uncle_depth
        self._entries: dict[bytes, _Entry] = {}
        self._best_tip: bytes | None = None

    # ------------------------------------------------------------------ queries
    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, share_id: bytes) -> bool:
        return share_id in self._entries

    def get(self, share_id: bytes) -> ShareBlock | None:
        entry = self._entries.get(share_id)
        return entry.share if entry else None

    def get_proof(self, share_id: bytes) -> bytes | None:
        entry = self._entries.get(share_id)
        return entry.proof if entry else None

    def best_tip_id(self) -> bytes | None:
        return self._best_tip

    def tip(self) -> ShareBlock | None:
        entry = self._entries.get(self._best_tip) if self._best_tip else None
        return entry.share if entry else None

    def height(self) -> int:
        tip = self.tip()
        return tip.sidechain_height if tip is not None else -1

    def cumulative_difficulty(self, share_id: bytes) -> int:
        entry = self._entries.get(share_id)
        return entry.cumulative_difficulty if entry else 0

    def is_valid_successor(self, share: ShareBlock, now: float | None = None) -> bool:
        """True if ``share`` would be accepted right now (no insertion)."""
        return self._validate(share, now)[0]

    def expected_target(self, prev_share_id: bytes) -> int | None:
        """The share_target consensus REQUIRES of a share extending ``prev_share_id``.

        Deterministic from the chain alone, so the finder (``build_job_for``) and
        every verifying peer (``_validate``) derive the identical value: the
        bootstrap target for a genesis share, then :func:`retarget_target` over
        the work-rate of the last ``retarget_window`` shares ending at the parent.

        Returns ``None`` when the target CANNOT be derived deterministically:
        unknown parent, or the look-back hits a pruned/unsynced ancestor before
        reaching a full window or genesis. The latter happens only near a
        window-sync base — a node must not derive a target from a truncated
        window (full-history peers would derive a different one); it skips the
        check there and still verifies the PoW at the stamped target.
        """
        if prev_share_id == GENESIS_PREV:
            return self.bootstrap_target
        entry = self._entries.get(prev_share_id)
        if entry is None:
            return None
        shares = [entry.share]
        cur = entry.share.prev_share_id
        while cur != GENESIS_PREV and len(shares) < self.retarget_window:
            e = self._entries.get(cur)
            if e is None:
                return None  # truncated history: cannot derive the consensus target
            shares.append(e.share)
            cur = e.share.prev_share_id
        if len(shares) < 2:
            return entry.share.share_target  # no interval to measure yet — carry
        work = sum(s.difficulty() for s in shares[:-1])  # work mined during the span
        span = shares[0].timestamp - shares[-1].timestamp
        return retarget_target(
            entry.share.share_target, work, span, self.share_time, config.RETARGET_CLAMP)

    # ------------------------------------------------------------------ mutation
    def add_share(
        self, share: ShareBlock, *, verified: bool, proof: bytes | None = None,
        now: float | None = None,
    ) -> AddResult:
        """Validate and insert ``share``.

        ``verified`` MUST be the result of a successful PoW/proof check at the
        share target. Returns an :class:`AddResult`; ``is_best_tip`` is True when
        this share became the new best tip (i.e. extended or reorged the chain).
        """
        if not verified:
            return AddResult(False, False, "not verified")

        ok, reason = self._validate(share, now)
        if not ok:
            return AddResult(False, False, reason)

        sid = share.share_id()
        if share.prev_share_id == GENESIS_PREV:
            cumulative = share.difficulty()
        else:
            cumulative = self._entries[share.prev_share_id].cumulative_difficulty
            cumulative += share.difficulty()
            for uid in share.uncle_ids:
                cumulative += self._entries[uid].share.difficulty()

        self._entries[sid] = _Entry(share, cumulative, proof)

        is_best = (
            self._best_tip is None
            or cumulative > self._entries[self._best_tip].cumulative_difficulty
        )
        if is_best:
            self._best_tip = sid
            self._prune()
        return AddResult(True, is_best, "")

    # ------------------------------------------------------------------ PPLNS
    def pplns_weights(self, tip_id: bytes | None = None) -> list[tuple[str, int]]:
        """Sum (uncle-penalized) share difficulty per address over the window.

        Walks back ``window`` main-chain shares from ``tip_id`` (default: the best
        tip). Each main-chain share contributes its full difficulty to its miner;
        each uncle it references contributes ``uncle_weight`` (penalized), counted
        once. Pass a specific share id to compute the window *as of* that share —
        a peer verifying an incoming share recomputes the same payouts the finder
        committed by walking from the share's ``prev_share_id``. The result feeds
        :func:`p2pearl.consensus.pplns.compute_pplns_payouts`.
        """
        weights: dict[str, int] = {}
        seen_uncles: set[bytes] = set()
        cur = tip_id if tip_id is not None else self._best_tip
        count = 0
        while cur is not None and count < self.window:
            entry = self._entries.get(cur)
            if entry is None:
                break  # pruned ancestor; window is shorter than retention so this is rare
            share = entry.share
            weights[share.miner_address] = weights.get(share.miner_address, 0) + share.difficulty()
            for uid in share.uncle_ids:
                if uid in seen_uncles:
                    continue
                uentry = self._entries.get(uid)
                if uentry is None:
                    continue
                seen_uncles.add(uid)
                w = uncle_weight(uentry.share.difficulty(), self.uncle_penalty_percent)
                weights[uentry.share.miner_address] = (
                    weights.get(uentry.share.miner_address, 0) + w
                )
            count += 1
            cur = share.prev_share_id if share.prev_share_id != GENESIS_PREV else None
        return sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))

    # ------------------------------------------------------------------ internal
    def _validate(self, share: ShareBlock, now: float | None = None) -> tuple[bool, str]:
        if now is None:
            now = time.time()
        sid = share.share_id()
        if sid in self._entries:
            return False, "duplicate"
        if share.version != config.SIDECHAIN_VERSION:
            return False, "bad version"
        if share.timestamp > now + config.MAX_TIMESTAMP_DRIFT_SECONDS:
            return False, "timestamp too far in future"
        if share.coinbase_value != block_subsidy(share.parent_height):
            return False, "bad coinbase value"
        if len(set(share.uncle_ids)) != len(share.uncle_ids):
            return False, "duplicate uncle"
        if sid in share.uncle_ids:
            return False, "self uncle"

        if share.prev_share_id == GENESIS_PREV:
            if share.sidechain_height != 0:
                return False, "genesis height != 0"
            if share.uncle_ids:
                return False, "genesis with uncles"
            if share.share_target != self.bootstrap_target:
                return False, "bad share target"
            return True, ""

        parent_entry = self._entries.get(share.prev_share_id)
        if parent_entry is None:
            return False, "orphan: unknown parent"
        parent = parent_entry.share
        if share.sidechain_height != parent.sidechain_height + 1:
            return False, "bad height"
        if share.timestamp < parent.timestamp:
            return False, "timestamp regression"
        if share.parent_height < parent.parent_height:
            return False, "parent height regression"
        expected = self.expected_target(share.prev_share_id)
        if expected is not None and share.share_target != expected:
            return False, "bad share target"

        ancestors, ancestor_uncles = self._recent(share.prev_share_id, self.uncle_depth + 1)
        lo = share.sidechain_height - self.uncle_depth
        hi = share.sidechain_height - 1
        for uid in share.uncle_ids:
            uentry = self._entries.get(uid)
            if uentry is None:
                return False, "unknown uncle"
            if uid in ancestors:
                return False, "uncle on main chain"
            if uid in ancestor_uncles:
                return False, "uncle already referenced"
            if not (lo <= uentry.share.sidechain_height <= hi):
                return False, "uncle out of depth"
            if uentry.share.prev_share_id not in ancestors:
                return False, "uncle not a sibling of the main chain"
        return True, ""

    def _recent(self, start_id: bytes, limit: int) -> tuple[set[bytes], set[bytes]]:
        """Collect up to ``limit`` ancestor ids (from ``start_id`` upward) and the
        set of uncle ids those ancestors already reference."""
        ids: set[bytes] = set()
        uncles: set[bytes] = set()
        cur = start_id
        n = 0
        while cur and cur != GENESIS_PREV and n < limit:
            entry = self._entries.get(cur)
            if entry is None:
                break
            ids.add(cur)
            for uid in entry.share.uncle_ids:
                uncles.add(uid)
            cur = entry.share.prev_share_id
            n += 1
        return ids, uncles

    def _prune(self) -> None:
        tip = self.tip()
        if tip is None:
            return
        cutoff = tip.sidechain_height - self._retention
        if cutoff <= 0:
            return
        stale = [sid for sid, e in self._entries.items() if e.share.sidechain_height < cutoff]
        for sid in stale:
            del self._entries[sid]
