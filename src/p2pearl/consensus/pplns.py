"""Feeless, operator-less PPLNS reward split.

When the pool finds a Pearl block, the block reward is divided across the miners
who hold shares in the PPLNS window, proportionally to each miner's summed share
weight (normal shares count full difficulty; uncles count
``difficulty * (100 - UNCLE_PENALTY_PERCENT) // 100``). The result is the list of
coinbase outputs.

Unlike a centralized pool there is **no operator address and no fee**: every
grain goes to miners. This is a deliberate divergence from the Pearl repo's
reference ``pearl_stratum_srv.payouts`` (which takes a 1% operator cut and routes
dust to the operator). Sub-minimum miners are simply skipped this block; their
shares remain in the window and may pay out in a later block — the documented
P2Pool minimum-hashrate / payout-variance effect.

The split is deterministic and integer-exact: ``sum(p.grains for p in result)``
equals ``block_reward_grains`` whenever at least one address qualifies.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Payout:
    address: str
    grains: int


def compute_pplns_payouts(
    block_reward_grains: int,
    weights_by_addr: list[tuple[str, int]],
    min_payout_grains: int = 0,
) -> list[Payout]:
    """Split ``block_reward_grains`` across miners by share weight.

    Args:
        block_reward_grains: the coinbase value to distribute (subsidy + fees).
        weights_by_addr: ``[(address, weight)]`` — weight is the sum of this
            address's (uncle-penalized) share difficulties across the PPLNS window.
            Duplicate addresses are summed defensively.
        min_payout_grains: addresses whose proportional amount is below this are
            dropped this block (their shares persist in-window for a later block).

    Returns:
        Deterministic ``list[Payout]`` sorted by ``(-grains, address)``. Empty if
        there is nothing to pay. ``sum(grains) == block_reward_grains`` when
        non-empty.
    """
    if block_reward_grains < 0:
        raise ValueError("block_reward_grains must be non-negative")
    if min_payout_grains < 0:
        raise ValueError("min_payout_grains must be non-negative")

    survivors: dict[str, int] = {}
    for addr, weight in weights_by_addr:
        if weight > 0:
            survivors[addr] = survivors.get(addr, 0) + weight

    if not survivors or block_reward_grains == 0:
        return []

    # Drop sub-minimum recipients one at a time (smallest first). Dropping shrinks
    # the denominator, so survivors' shares only rise — this is monotonic and
    # fairer than dropping the whole below-min set at once.
    while len(survivors) > 1:
        total = sum(survivors.values())
        worst_addr, worst_weight = min(survivors.items(), key=lambda kv: (kv[1], kv[0]))
        if (block_reward_grains * worst_weight) // total >= min_payout_grains:
            break
        del survivors[worst_addr]

    ordered = sorted(survivors.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(w for _, w in ordered)

    # Pot too small for even the largest miner to clear the minimum: pay it all to
    # the single largest-weight address rather than burn the reward.
    if (block_reward_grains * ordered[0][1]) // total < min_payout_grains:
        return [Payout(ordered[0][0], block_reward_grains)]

    payouts: list[Payout] = []
    awarded = 0
    for addr, weight in ordered[:-1]:
        grains = (block_reward_grains * weight) // total
        payouts.append(Payout(addr, grains))
        awarded += grains
    # Last entry absorbs the integer remainder so the sum is exact.
    payouts.append(Payout(ordered[-1][0], block_reward_grains - awarded))

    return sorted(payouts, key=lambda p: (-p.grains, p.address))


def uncle_weight(difficulty: int, penalty_percent: int) -> int:
    """Weight contributed by an uncle share (penalized vs. a normal share)."""
    return difficulty * (100 - penalty_percent) // 100
