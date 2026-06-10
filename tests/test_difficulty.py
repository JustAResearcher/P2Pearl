"""Tests for sidechain difficulty/target conversion and retargeting."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2pearl.consensus.difficulty import (  # noqa: E402
    MAX_TARGET,
    difficulty_to_target,
    next_share_target,
    retarget_target,
    target_to_bits,
    target_to_difficulty,
)


def _bits_to_target(bits: int) -> int:
    """Reference compact-bits decoder (full range, matching Rust nbits_to_difficulty).

    The Pearl gateway's bits_to_target uses the same left-shift for the
    exponent >= 3 targets it ever sees; this also handles the small-exponent
    case so the round-trip holds for tiny targets.
    """
    exponent = (bits >> 24) & 0xFF
    mantissa = bits & 0xFFFFFF
    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def test_target_difficulty_inverse():
    assert target_to_difficulty(MAX_TARGET) == 1
    t = 1 << 200
    assert difficulty_to_target(target_to_difficulty(t)) <= MAX_TARGET
    assert target_to_difficulty(0) == 0


def test_retarget_slower_makes_easier():
    # Shares arriving slower than target => bigger (easier) target.
    cur = 1 << 200
    nt = next_share_target(cur, actual_interval_seconds=20, target_time_seconds=10)
    assert nt > cur


def test_retarget_faster_makes_harder():
    cur = 1 << 200
    nt = next_share_target(cur, actual_interval_seconds=5, target_time_seconds=10)
    assert nt < cur


def test_retarget_clamped():
    cur = 1 << 200
    # 100x too slow, but step is clamped to max_step=4.0
    nt = next_share_target(cur, actual_interval_seconds=1000, target_time_seconds=10, max_step=4.0)
    assert nt == min(MAX_TARGET, cur * 4)


def test_retarget_never_zero():
    nt = next_share_target(1, actual_interval_seconds=0, target_time_seconds=10)
    assert nt >= 1


def test_retarget_target_steady_state_is_fixed_point():
    # Work-rate exactly at one share per share_time -> the target is unchanged.
    prev = MAX_TARGET // 64                     # difficulty 64
    work, span = 59 * 64, 59 * 10               # 59 intervals at 10s, diff 64 each
    assert retarget_target(prev, work, span, 10) == prev


def test_retarget_target_consensus_clamps():
    prev = MAX_TARGET // 64
    # 10x too fast -> clamped to a /4 hardening step
    assert retarget_target(prev, 64, 1, 10) == prev // 4
    # 100x too slow -> clamped to a x4 easing step
    assert retarget_target(prev, 64, 1000, 10) == prev * 4


def test_retarget_target_integer_only_and_bounds():
    # Pure-int math on 256-bit values; never exceeds MAX_TARGET, never below 1.
    assert retarget_target(MAX_TARGET, 1, 1_000_000, 10) == MAX_TARGET
    assert retarget_target(4, 1 << 256, 1, 10) == 1   # floor at 1, not 0
    assert retarget_target(123, 0, 10, 10) == 123     # no work observed -> carry


def test_target_to_bits_roundtrip_canonical():
    # Canonical nbits round-trip exactly (0x1d00ffff exercises the sign-bit shift).
    for nb in (0x1D00FFFF, 0x1B0404CB, 0x1E01FFFF, 0x1C0FFFFF):
        assert target_to_bits(_bits_to_target(nb)) == nb


def test_target_to_bits_lossy_never_easier():
    # Encoding never yields an easier threshold: decode(encode(t)) <= t.
    for t in (1, 1 << 8, (1 << 240) + 12345, MAX_TARGET):
        assert _bits_to_target(target_to_bits(t)) <= t


def test_target_to_bits_zero_and_negative():
    assert target_to_bits(0) == 0
    assert target_to_bits(-5) == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
