"""P2Pearl configuration and sidechain consensus parameters.

The SIDECHAIN_* / SHARE_* / PPLNS_* / UNCLE_* constants below define the P2Pearl
sidechain. Every node on the same sidechain MUST agree on them byte-for-byte —
they are consensus, not preferences. Changing one forks the sidechain.

Parent-chain (Pearl mainnet) facts are grounded in
pearl/node/chaincfg/params.go and pearl/node/blockchain/validate.go.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Parent chain: Pearl mainnet (from pearl/node/chaincfg/params.go) ---
PARENT_BLOCK_TIME_SECONDS = 194          # 3m14s target spacing (per-block WTEMA retarget)
GRAIN_PER_PEARL = 100_000_000            # 1e8 base units ("grain")
COINBASE_MATURITY = 100                  # blocks before a coinbase output is spendable
ADDRESS_HRP = "prl"                      # bech32m P2TR human-readable part (mainnet; tprl/rprl on test nets)
PARENT_RPC_DEFAULT_URL = "http://127.0.0.1:44107"

# --- P2Pearl sidechain consensus (the "sharechain") ---
SIDECHAIN_VERSION = 1
SHARE_TARGET_TIME_SECONDS = 10           # one share every ~10s on average (per-pool difficulty)
PPLNS_WINDOW_SHARES = 1000               # reward look-back; tune to ~a few parent blocks of work
UNCLE_BLOCK_DEPTH = 3                    # an uncle may be referenced up to N sidechain heights back
UNCLE_PENALTY_PERCENT = 20               # uncle weight = difficulty * (100 - penalty) // 100
MIN_PAYOUT_GRAINS = 100_000             # below this a miner is skipped this block; shares persist in-window

# Sidechain difficulty retarget clamps (multiplicative bound per share)
RETARGET_MAX_STEP = 4.0
RETARGET_MIN_STEP = 0.25

# The largest possible 256-bit target (difficulty 1 ceiling).
MAX_TARGET = (1 << 256) - 1


@dataclass(frozen=True)
class NodeRPCConfig:
    """Connection to the local pearld full node's JSON-RPC."""

    url: str = PARENT_RPC_DEFAULT_URL
    user: str = "user"
    password: str = "pass"


@dataclass(frozen=True)
class DaemonConfig:
    """Runtime (non-consensus) configuration for one P2Pearl node."""

    node: NodeRPCConfig = field(default_factory=NodeRPCConfig)
    payout_address: str = ""             # this node's prl1p... P2TR address (where our shares pay us)
    stratum_host: str = "0.0.0.0"
    stratum_port: int = 3360
    p2p_host: str = "0.0.0.0"
    p2p_port: int = 37900
    data_dir: str = "./p2pearl-data"
