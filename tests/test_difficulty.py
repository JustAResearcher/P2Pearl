"""Tests for sidechain difficulty/target conversion and retargeting."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2pearl.consensus.difficulty import (  # noqa: E402
    MAX_TARGET,
    difficulty_to_target,
    next_share_target,
    target_to_difficulty,
)


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
