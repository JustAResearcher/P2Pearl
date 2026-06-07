"""Sidechain difficulty <-> target conversion and retargeting.

The sidechain keeps its own difficulty, independent of Pearl's parent-chain WTEMA
retarget. The goal is one share roughly every ``SHARE_TARGET_TIME_SECONDS`` across
the whole pool, so the sidechain target tracks total pool hashrate.

Convention (Bitcoin/Pearl-style): a *larger* target is *easier*; difficulty is
``MAX_TARGET // target``. ``hash_jackpot`` (as a little-endian integer) must be
``<= target`` for a valid share — see ``p2pearl.pow.verify.meets_target``.
"""

from __future__ import annotations

MAX_TARGET = (1 << 256) - 1


def target_to_difficulty(target: int) -> int:
    if target <= 0:
        return 0
    return MAX_TARGET // target


def difficulty_to_target(difficulty: int) -> int:
    if difficulty <= 0:
        return MAX_TARGET
    return MAX_TARGET // difficulty


def next_share_target(
    current_target: int,
    actual_interval_seconds: float,
    target_time_seconds: float,
    max_step: float = 4.0,
    min_step: float = 0.25,
) -> int:
    """Retarget toward ``target_time_seconds`` per share.

    If shares arrived too slowly (``actual > target_time``) the target grows
    (gets easier); too quickly and it shrinks (gets harder). The multiplicative
    step is clamped to ``[min_step, max_step]`` to damp oscillation.
    """
    if target_time_seconds <= 0:
        raise ValueError("target_time_seconds must be positive")
    if actual_interval_seconds <= 0:
        ratio = min_step
    else:
        ratio = actual_interval_seconds / target_time_seconds
    ratio = max(min_step, min(max_step, ratio))
    new_target = int(current_target * ratio)
    return max(1, min(MAX_TARGET, new_target))
