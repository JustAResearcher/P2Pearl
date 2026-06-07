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


def target_to_bits(target: int) -> int:
    """Encode a 256-bit ``target`` as a Bitcoin-compact ``nbits`` (u32).

    Inverse of the Pearl gateway's ``bits_to_target`` (mantissa * 256**(exp-3)).
    The Pearl proof verifier (``pearl_mining.verify_plain_proof_with_nbits``) takes
    the share threshold as a compact ``u32`` nbits, decodes it via
    ``nbits_to_difficulty`` and applies the ``h*w*k`` factor itself, so a share
    target produced here must go through this encoder before it is handed to
    ``p2pearl.pow.verify.verify_share``.

    Lossy: the 24-bit mantissa is rounded DOWN, so the encoded target is ``<=`` the
    input (a share graded at the encoded nbits is at worst very slightly *harder*
    than ``target``, never easier).
    """
    if target <= 0:
        return 0
    nbytes = (target.bit_length() + 7) // 8
    if nbytes <= 3:
        mantissa = target << (8 * (3 - nbytes))
    else:
        mantissa = target >> (8 * (nbytes - 3))
    # The compact format's mantissa is signed; if the top bit is set, shift down a
    # byte and bump the exponent so the sign bit stays clear.
    if mantissa & 0x00800000:
        mantissa >>= 8
        nbytes += 1
    return (nbytes << 24) | (mantissa & 0x007FFFFF)
