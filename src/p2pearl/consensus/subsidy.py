"""Pearl's block-subsidy schedule, replicated exactly from the parent chain.

Mirrors ``CalcBlockSubsidy`` in pearl/node/blockchain/validate.go: a smooth
hyperbolic emission (NOT Bitcoin-style halvings),

    subsidy(h) = totalSupply * E // ((h + E) * (h - 1 + E))      [grains]

where ``E = 4 years / TargetTimePerBlock`` (650226 at Pearl's 194s blocks; the
same on mainnet, testnet, and regtest — every network uses 194s). Go computes
this with big.Int and floor division on positive operands, which Python's ``//``
matches exactly.

This is sidechain consensus: shares are coinbase-only (no transaction-fee
collection in share format v3), so a share's ``coinbase_value`` MUST equal
``block_subsidy(parent_height)`` — a peer validates that without trusting the
finder or its own GBT (whose ``coinbasevalue`` includes mempool fees).
"""

from __future__ import annotations

from .. import config

TOTAL_SUPPLY_GRAINS = 2_100_000_000 * config.GRAIN_PER_PEARL
EMISSION_CONSTANT = (4 * 365 * 24 * 60 * 60) // config.PARENT_BLOCK_TIME_SECONDS  # = 650226


def block_subsidy(height: int) -> int:
    """The exact coinbase subsidy (grains) of the Pearl block at ``height``."""
    if height <= 0:
        return 0  # the genesis block has no subsidy (heights are unsigned upstream)
    e = EMISSION_CONSTANT
    return TOTAL_SUPPLY_GRAINS * e // ((height + e) * (height - 1 + e))
