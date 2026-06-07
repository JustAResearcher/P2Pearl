"""Tests for ShareBlock serialization and id determinism."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2pearl.consensus.share import ShareBlock  # noqa: E402


def _sample(**over):
    base = dict(
        version=1,
        sidechain_height=12345,
        prev_share_id=b"\x11" * 32,
        parent_prev_block=b"\x22" * 32,
        parent_height=987654,
        timestamp=1_777_000_000,
        share_target=(1 << 240),
        block_nbits=0x1E01FFFF,
        miner_address="prl1pexampleexampleexampleexampleexampleexampleexampleexa",
        payout_set_hash=b"\x33" * 32,
        uncle_ids=[b"\x55" * 32, b"\x66" * 32],
    )
    base.update(over)
    return ShareBlock(**base)


def test_round_trip():
    s = _sample()
    raw = s.serialize()
    back = ShareBlock.deserialize(raw)
    assert back == s
    assert back.serialize() == raw


def test_round_trip_no_uncles():
    s = _sample(uncle_ids=[])
    assert ShareBlock.deserialize(s.serialize()) == s


def test_id_is_deterministic_and_32_bytes():
    s = _sample()
    assert len(s.share_id()) == 32
    assert s.share_id() == _sample().share_id()


def test_id_changes_with_any_field():
    base_id = _sample().share_id()
    assert _sample(timestamp=1_777_000_001).share_id() != base_id
    assert _sample(miner_address="prl1pother").share_id() != base_id
    assert _sample(uncle_ids=[]).share_id() != base_id
    # pow_hash is non-committed evidence: it must NOT change the committed id.
    assert _sample(pow_hash=b"\x99" * 32).share_id() == base_id


def test_difficulty_inverse_of_target():
    s = _sample(share_target=(1 << 240))
    assert s.difficulty() == ((1 << 256) - 1) // (1 << 240)


def test_bad_hash_length_rejected():
    try:
        _sample(pow_hash=b"\x00" * 31)
    except ValueError:
        return
    raise AssertionError("expected ValueError for 31-byte hash")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
