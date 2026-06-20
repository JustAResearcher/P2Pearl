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
PAYOUT_STATS_PREFIX = "P2PEARL_PAYOUT_STATS "

# --- P2Pearl sidechain consensus (the "sharechain") ---
SIDECHAIN_VERSION = 5          # v5: vardiff share_target + retarget-derived target_limit
SHARE_TARGET_TIME_SECONDS = 10           # one share every ~10s on average (per-pool difficulty)
PPLNS_WINDOW_SHARES = 1000               # reward look-back; tune to ~a few parent blocks of work
UNCLE_BLOCK_DEPTH = 3                    # an uncle may be referenced up to N sidechain heights back
UNCLE_PENALTY_PERCENT = 20               # uncle weight = difficulty * (100 - penalty) // 100
MIN_PAYOUT_GRAINS = 100_000             # below this a miner is skipped this block; shares persist in-window

# The largest possible 256-bit target (difficulty 1 ceiling).
MAX_TARGET = (1 << 256) - 1

# Sidechain difficulty retarget. The maximum target a share may carry is derived
# deterministically from the chain it extends (see Sharechain.expected_target):
# estimated pool work-rate over the last RETARGET_WINDOW_SHARES, aimed at one base
# share per SHARE_TARGET_TIME_SECONDS, clamped to move at most RETARGET_CLAMP x per share.
# Integer arithmetic only — these are consensus.
RETARGET_WINDOW_SHARES = 60              # look-back (in shares) for the work-rate estimate
RETARGET_CLAMP = 4                       # max per-share target movement (both directions)
BOOTSTRAP_SHARE_TARGET = MAX_TARGET // (1 << 50)  # genesis difficulty ~1.1e15; retarget takes over
MAX_TIMESTAMP_DRIFT_SECONDS = 300        # reject shares stamped further than this into the future

# Runtime vardiff policy. This is not consensus: a node may ask its directly
# connected miners for shares harder than the current sidechain target limit.
# v5 shares carry both values, so peers credit the extra work correctly.
STRATUM_TARGET_FACTOR = 256              # initial factor; adaptive vardiff tunes per worker
STRATUM_VARDIFF_TARGET_SECONDS = 60
STRATUM_VARDIFF_MAX_FACTOR = 1 << 24


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
    stratum_target_factor: int = STRATUM_TARGET_FACTOR
    peers: tuple = ()                    # ((host, port), ...) outbound P2P peers to dial on start
    data_dir: str = "./p2pearl-data"
