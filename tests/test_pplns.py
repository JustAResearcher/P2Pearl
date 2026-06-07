"""Tests for the feeless PPLNS reward split."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2pearl.consensus.pplns import Payout, compute_pplns_payouts, uncle_weight  # noqa: E402


def test_exact_sum_and_proportional():
    pot = 1_000_000_000
    weights = [("addrA", 3), ("addrB", 1)]
    payouts = compute_pplns_payouts(pot, weights)
    assert sum(p.grains for p in payouts) == pot  # integer-exact, no leakage
    by = {p.address: p.grains for p in payouts}
    assert by["addrA"] == 750_000_000
    assert by["addrB"] == 250_000_000


def test_no_operator_no_fee():
    # Every grain goes to miners; there is no operator entry.
    pot = 777
    payouts = compute_pplns_payouts(pot, [("a", 1), ("b", 1)])
    assert sum(p.grains for p in payouts) == pot
    assert {p.address for p in payouts} == {"a", "b"}


def test_deterministic_order():
    pot = 100
    a = compute_pplns_payouts(pot, [("z", 1), ("a", 1), ("m", 2)])
    b = compute_pplns_payouts(pot, [("m", 2), ("a", 1), ("z", 1)])
    assert a == b  # order of inputs must not matter
    assert a == sorted(a, key=lambda p: (-p.grains, p.address))


def test_min_payout_drops_dust_but_keeps_sum_exact():
    pot = 1_000_000
    # 'whale' dominates; 'dust' would get ~1 grain, below the minimum.
    weights = [("whale", 1_000_000), ("dust", 1)]
    payouts = compute_pplns_payouts(pot, weights, min_payout_grains=100_000)
    assert {p.address for p in payouts} == {"whale"}
    assert sum(p.grains for p in payouts) == pot


def test_single_survivor_gets_whole_pot():
    payouts = compute_pplns_payouts(500, [("solo", 42)])
    assert payouts == [Payout("solo", 500)]


def test_pot_too_small_pays_largest():
    # No one clears the minimum; the largest-weight address takes it all.
    payouts = compute_pplns_payouts(10, [("a", 5), ("b", 4)], min_payout_grains=100)
    assert payouts == [Payout("a", 10)]


def test_duplicate_addresses_summed():
    payouts = compute_pplns_payouts(100, [("a", 1), ("a", 1), ("b", 2)])
    by = {p.address: p.grains for p in payouts}
    assert by["a"] == 50 and by["b"] == 50


def test_uncle_weight_penalty():
    assert uncle_weight(100, 20) == 80
    assert uncle_weight(100, 0) == 100


def test_empty_inputs():
    assert compute_pplns_payouts(100, []) == []
    assert compute_pplns_payouts(0, [("a", 1)]) == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
