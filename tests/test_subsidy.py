"""Tests for the replicated Pearl block-subsidy schedule (consensus/subsidy.py)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2pearl.consensus.subsidy import (  # noqa: E402
    EMISSION_CONSTANT,
    TOTAL_SUPPLY_GRAINS,
    block_subsidy,
)


def test_constants_match_pearld_source():
    # validate.go: totalSupply = 2100000000 * GrainPerPearl; defaultEmissionConstant = 650226
    assert TOTAL_SUPPLY_GRAINS == 2_100_000_000 * 100_000_000
    assert EMISSION_CONSTANT == 650226


def test_genesis_has_no_subsidy():
    assert block_subsidy(0) == 0


def test_first_block_subsidy_exact():
    # subsidy(1) = T*E // ((1+E)*E) = T // (1+E), floor — exactly what Go's big.Int derives
    e = EMISSION_CONSTANT
    assert block_subsidy(1) == TOTAL_SUPPLY_GRAINS * e // ((1 + e) * e)
    assert block_subsidy(1) == TOTAL_SUPPLY_GRAINS // (1 + e)


def test_testnet_observed_magnitude():
    # The public Pearl testnet paid ~2883 PRL (≈288.3e9 grains) per block around h≈38k
    # (observed on-chain at heights 37981/38108). The schedule must reproduce that.
    s = block_subsidy(38_000)
    assert abs(s - 288_300_000_000) < 1_000_000_000  # within ~0.35%


def test_monotonically_decreasing():
    prev = block_subsidy(1)
    for h in (2, 10, 100, 10_000, 650_226, 5_000_000, 50_000_000):
        cur = block_subsidy(h)
        assert cur < prev
        prev = cur


def test_emission_bounded_by_total_supply():
    # The hyperbolic schedule telescopes: sum_h subsidy(h) <= totalSupply. Spot-check
    # a large prefix stays under the cap (coarse stride upper-bounds each stride's
    # blocks by its FIRST block's subsidy, since the schedule decreases).
    total, h, stride = 0, 1, 10_000
    while h < 3_000_000:
        total += block_subsidy(h) * stride
        h += stride
    assert total < TOTAL_SUPPLY_GRAINS


def test_fits_u64():
    # coinbase_value is serialized <Q in the share format
    assert block_subsidy(1) < (1 << 64)
