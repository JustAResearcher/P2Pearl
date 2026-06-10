# P2Pearl

**A decentralized, zero-fee, P2Pool-style mining pool for [Pearl](https://github.com/pearl-research-labs/pearl) (the Pearlhash proof-of-useful-work coin).**

P2Pearl is to Pearl what [P2Pool](https://github.com/SChernykh/p2pool) is to Monero: a peer-to-peer mining pool with **no operator, no pool wallet, and a 0 % fee**. Miners mine a shared *sharechain*; when the pool finds a real Pearl block, its coinbase pays every recent contributor directly and proportionally, enforced by consensus — there is no one to take a cut, go rogue, or be shut down.

> **Status (v0.0.12+, 102 tests):** validated live on the **public Pearl testnet** — real canonical blocks mined (heights 37981, 37988, 38108, 38111), including blocks paying **two independent operators'** miners proportionally in a single feeless coinbase. The sidechain now has a **consensus share-difficulty retarget** (every share's target is derived from the chain and enforced by every peer) and **subsidy-exact coinbase validation** (replicates `pearld`'s emission schedule grain-for-grain; verified against a live node). Remaining before a mainnet pool we'd stake real money on: per-miner vardiff, transaction-fee collection, and a longer multi-operator soak. See [`ROADMAP.md`](ROADMAP.md) and the design in [`docs/blueprint.md`](docs/blueprint.md).

---

## Choose your path

| You are… | You need | Start here |
|---|---|---|
| **Curious** — just want to see it work | nothing (Windows/Linux binary) | [Try it in 60 seconds](#try-it-in-60-seconds) |
| **A miner** — have a GPU, want to mine feelessly | SRBMiner + a pool node's address | [Mine on a P2Pearl node](#mine-on-a-p2pearl-node-gpu-owners) |
| **An operator** — want to run a pool node | a Linux box, `pearld`, one build step | [Run a pool node](#run-a-pool-node-operators) + [Configuration reference](#configuration-reference) |

---

## Try it in 60 seconds

Download `p2pearl.exe` (Windows) or `p2pearl-linux-x86_64` (Linux) from the [latest release](https://github.com/JustAResearcher/P2Pearl/releases). No Python needed.

- **Windows:** double-click `p2pearl.exe`. It runs a live local demo — two pool nodes gossiping over real sockets, simulated miners, a feeless PPLNS payout — and the window stays open until you press Enter.
- **Linux:** `chmod +x p2pearl-linux-x86_64 && ./p2pearl-linux-x86_64 demo`
- From a terminal: `p2pearl.exe --version`, `p2pearl.exe demo`, `p2pearl.exe daemon --help`.

The demo fakes only the GPU cryptography; everything else (stratum, sharechain, PPLNS, P2P verification) is the real code path. From a source checkout the same CLI is `python -m p2pearl`.

## Mine on a P2Pearl node (GPU owners)

You don't run P2Pearl at all — you point your existing Pearlhash miner at someone's P2Pearl node, exactly like any pool:

```bash
SRBMiner-MULTI --algorithm pearlhash --pool <node-ip>:3360 --wallet <your-prl1p...-address> --disable-cpu
```

- **Wallet:** a Pearl **P2TR** address — `prl1p…` on mainnet, `tprl1p…` on testnet. The coinbase pays *this address directly on-chain*; there is no pool balance, no withdrawal, no minimum-payout account. (Sub-0.001 PRL dust shares are deferred to a later block, never lost.)
- **Worker name:** append `.rigname` to the wallet (`prl1p….rig01`) to tell your rigs apart in logs.
- **Fee: 0 %.** The coinbase splits the entire block reward across the PPLNS window. Nothing else.
- Share difficulty is set by the node (the sidechain retargets to ~1 share / 10 s pool-wide); your miner just follows the job target like any stratum pool.

## Run a pool node (operators)

A node = your own `pearld` + `p2pearl daemon`. The daemon serves the stratum port miners connect to, gossips shares with peer nodes (each share is trustlessly re-verified), and when the pool finds a block, submits it with the feeless PPLNS coinbase. **[`docs/running-a-node.md`](docs/running-a-node.md) is the complete from-source walkthrough** (clone Pearl → one additive patch → build `pearl_mining` + `pearld` → run). The short version:

```bash
# one-time build (Linux): see docs/running-a-node.md for the full recipe
git clone https://github.com/pearl-research-labs/pearl
git clone https://github.com/JustAResearcher/P2Pearl
python P2Pearl/tools/apply_m2_binding.py pearl
python -m venv venv && . venv/bin/activate && pip install maturin
( cd pearl/py-pearl-mining && maturin develop --release )
( cd pearl && task build:pearld build:prlctl )
pip install -e P2Pearl bitcoin-utils numpy
export PYTHONPATH="$PWD/pearl/miner/pearl-gateway/src:$PYTHONPATH"

# run (testnet shown)
./pearl/bin/pearld --testnet --notls --rpcuser=u --rpcpass=p --rpclisten=127.0.0.1:44109 &
p2pearl daemon --rpc-url http://127.0.0.1:44109 --rpc-user u --rpc-pass p \
               --peer <another-operator>:37900
```

Then point miners at `<your-ip>:3360`, and forward TCP **3360** + **37900** if you want miners/peers outside your LAN.

---

## Configuration reference

Everything an operator can configure, in one place. There is **no config file** — the daemon is configured entirely by command-line flags and a few environment variables; the *sidechain consensus* lives in code (`src/p2pearl/config.py`) and must be identical on every node.

### `p2pearl daemon` flags

| Flag | Default | What it does / when to change it |
|---|---|---|
| `--rpc-url` | `http://127.0.0.1:44107` | Your `pearld` JSON-RPC endpoint. `44107` is the mainnet convention; this guide uses `44109` for testnet. Must match `pearld`'s `--rpclisten`. |
| `--rpc-user` | `user` | `pearld` RPC username (must match `--rpcuser`). Prefer the env var below for secrets. |
| `--rpc-pass` | `pass` | `pearld` RPC password (must match `--rpcpass`). Prefer the env var below. |
| `--stratum-host` | `0.0.0.0` | Bind address for the miner-facing stratum listener. Use `127.0.0.1` to accept only local miners. |
| `--stratum-port` | `3360` | The port miners point `--pool` at. |
| `--p2p-host` | `0.0.0.0` | Bind address for the share-gossip listener. |
| `--p2p-port` | `37900` | The port other operators `--peer` to. |
| `--peer HOST:PORT` | *(none)* | Another operator's P2P endpoint. **Repeatable.** With no peers you run a solo pool (still feeless, still PPLNS across your own miners). Peering merges everyone into ONE pool: shares gossip both ways, every node trustlessly re-verifies them, and any node's block pays the whole network's window. New nodes window-sync the recent sharechain automatically on connect. |
| `--share-target INT\|HEX` | built-in | **Consensus — leave it alone** unless you are bootstrapping a brand-new private sidechain. Overrides only the GENESIS bootstrap target (`0x…` hex or decimal); after genesis the target retargets automatically. Every node on the same sidechain must use the same value or they will reject each other's shares. |
| `--pause-cmd CMD` | *(none)* | Shell command run just before the (rare, CPU-heavy) block-prove — e.g. `'pkill -STOP -x xmrig'` to pause a co-located CPU miner. Cuts prove time ~3× on a busy box. Bounded by a 10 s timeout; failures never block a found block. |
| `--resume-cmd CMD` | *(none)* | Undoes `--pause-cmd` right after the prove — e.g. `'pkill -CONT -x xmrig'`. Also run once at startup to self-heal a crash that died mid-prove. |

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `P2PEARL_RPC_USER` / `P2PEARL_RPC_PASS` | — | RPC credentials, instead of putting them on the command line (visible in `ps`). Flags win if both are set. |
| `RAYON_NUM_THREADS` | auto (= logical CPUs) | Thread count of the Rust ZK prover. The daemon pins this automatically — leaving it unset upstream makes a found-block prove ~2× slower under load. Export it yourself only to *reduce* threads. |
| `_RJEM_MALLOC_CONF` | `background_thread:true` | jemalloc tuning for the prover (set automatically). Do **not** set `dirty_decay_ms:-1` on a memory-constrained box — it OOMs. |
| `PYTHONPATH` | — | Must include `pearl/miner/pearl-gateway/src` (for block/ZK-certificate serialization; pure Python, no torch). Source installs only — the test suite and demo don't need it. |

### Ports at a glance

| Port | Protocol | Direction | Forward through your router? |
|---|---|---|---|
| `3360` | stratum (TCP) | miners → your node | Only if miners outside your LAN should reach you |
| `37900` | P2P gossip (TCP) | other operators ↔ you | Yes, if you want inbound peers (outbound `--peer` works without it) |
| `44107` / `44109` | pearld JSON-RPC | daemon → pearld, localhost | **Never** expose this publicly |
| `44060` / `44110` | pearld chain P2P | pearld ↔ Pearl network | Recommended for a well-connected full node |

### `pearld` settings that matter to P2Pearl

A known-good `~/.pearld/pearld.conf` for a pool node (testnet):

```ini
testnet=1          ; drop for mainnet
notls=1            ; or configure TLS and use https:// in --rpc-url
rpcuser=u
rpcpass=p
rpclisten=127.0.0.1:44109
```

The daemon polls `getblocktemplate` every ~2 s and calls `submitblock`; no wallet, indexes, or special flags are required in `pearld`. The daemon fails fast at startup if it can't reach the RPC (check URL/credentials first — see Troubleshooting).

### Sidechain consensus parameters (`src/p2pearl/config.py`)

These define the sidechain itself. **Every node on a sidechain must agree on all of them — changing any one forks you off the pool.** They are deliberately not flags.

| Constant | Value | Meaning |
|---|---|---|
| `SIDECHAIN_VERSION` | `3` | Share format/rules version. v3 = consensus retarget + subsidy-exact coinbase. v2 shares are rejected (and vice versa), so all peered nodes must run the same major version. |
| `SHARE_TARGET_TIME_SECONDS` | `10` | The retarget aims for one share per 10 s **pool-wide**. |
| `RETARGET_WINDOW_SHARES` | `60` | Work-rate look-back for the retarget (~10 min of shares). |
| `RETARGET_CLAMP` | `4` | A share's target may move at most 4× per share, either direction (damps oscillation and timestamp games). |
| `BOOTSTRAP_SHARE_TARGET` | difficulty 64 | The genesis share target; the retarget takes over from share #2. Override per-deployment with `--share-target` (consensus!). |
| `MAX_TIMESTAMP_DRIFT_SECONDS` | `300` | Shares stamped >5 min into the future are rejected (protects the retarget). |
| `PPLNS_WINDOW_SHARES` | `1000` | The coinbase pays the miners of the last N shares, proportional to share difficulty (~2.8 h of shares at target rate). |
| `UNCLE_BLOCK_DEPTH` / `UNCLE_PENALTY_PERCENT` | `3` / `20` | Orphaned-but-recent shares still count: full weight for chain selection, 80 % weight for payout. |
| `MIN_PAYOUT_GRAINS` | `100000` (0.001 PRL) | Below this, a miner is skipped *this* block; their shares stay in-window for the next one. |

Two consensus rules worth knowing as an operator:

- **Share targets are not negotiable.** Every share must carry exactly the target the sharechain derives for its position (`Sharechain.expected_target`). Your node computes it, stamps it into jobs, and rejects any gossiped share that disagrees — so no peer can manufacture cheap weight or flood the chain.
- **Coinbase values are subsidy-exact.** Shares are coinbase-only (no mempool transactions yet), and every share's `coinbase_value` must equal Pearl's emission schedule for its height — replicated from `pearld`'s `CalcBlockSubsidy` and validated grain-for-grain against a live node. A finder cannot inflate the pot, and your blocks can never overpay (which `pearld` would reject).

### Running as a service (systemd)

```ini
# /etc/systemd/system/pearld.service
[Unit]
Description=Pearl full node
After=network-online.target
[Service]
ExecStart=/opt/pearl/bin/pearld --configfile=/root/.pearld/pearld.conf
Restart=always
[Install]
WantedBy=multi-user.target

# /etc/systemd/system/p2pearl.service
[Unit]
Description=P2Pearl pool node
After=pearld.service
Requires=pearld.service
[Service]
Environment=PYTHONPATH=/opt/pearl/miner/pearl-gateway/src
Environment=P2PEARL_RPC_USER=u
Environment=P2PEARL_RPC_PASS=p
ExecStart=/opt/venv/bin/p2pearl daemon --rpc-url http://127.0.0.1:44109 \
    --peer <other-operator>:37900
Restart=always
[Install]
WantedBy=multi-user.target
```

`systemctl enable --now pearld p2pearl` and both survive reboots. (The public testnet node runs exactly this shape.)

### Prover speed (don't lose found blocks)

The only latency between *finding* a block and *announcing* it is generating its ZK certificate (~3–17 s of pure CPU). The daemon already pins prover threads, proves in a worker thread, serializes and dedups proves; your two knobs are `--pause-cmd`/`--resume-cmd` (pause co-located CPU load → ~3× faster prove) and **collaborative submission**, which is automatic: every peered node races to prove-and-submit any block-clearing share the moment it arrives, so the pool's orphan exposure is the *fastest* node's prove time, not each finder's. Full measurements in [`docs/running-a-node.md`](docs/running-a-node.md#prover-speed--avoiding-orphaned-blocks).

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `could not reach pearld at …` on startup | Wrong `--rpc-url`/credentials, or `pearld` still syncing. Test: `curl -u u:p -d '{"method":"getblocktemplate","params":[]}' http://127.0.0.1:44109/`. |
| Miner connects but never gets a job | The daemon primes its first job from `getblocktemplate` — if `pearld` is mid-sync, GBT errors until it reaches the tip. Wait for sync. |
| Shares rejected: `share does not meet target` | Normal occasionally (the miner raced a retarget/job refresh). Constant rejections → miner is on the wrong algorithm or a stale connection; restart the miner. |
| Gossiped shares rejected: `bad share target` / `bad coinbase value` | The peer is on different consensus (old version, or a different `--share-target` bootstrap). All nodes must run the same `SIDECHAIN_VERSION` and genesis target. |
| Peer connect fails | Their `37900` isn't reachable (port-forward/firewall), or version mismatch. Outbound `--peer` needs no forwarding on *your* side. |
| Block found but not on-chain | Likely orphaned — another miner found the height first while proving. Keep the prover fast (`--pause-cmd`, peers for collaborative submit). |
| Windows EXE closes instantly | Fixed in v0.0.12 — re-download. |

---

## Why this is feasible (and why Pearl is a clean target)

Pearl is a **btcd/Bitcoin fork** (UTXO chain, `getblocktemplate`, real Bitcoin coinbase, transaction merkle root, `nbits` compact targets) with the Pearlhash proof-of-useful-work bolted on as a **succinct, CPU-verifiable ZK certificate**. That combination is almost ideal for a P2Pool port:

- **One solution clears two targets.** Pearl's PoW is a plain threshold, `U256(hash_jackpot) <= bound(nbits)`, and the hash is independent of the target — so the *same* solution is graded against an easy **share** target and the hard **block** target (share and block targets are nested). The Pearlhash stratum job already carries a share `target` distinct from the header's block `nbits`.
- **The coinbase carries the commitment.** P2Pearl writes its sidechain commitment into an `OP_RETURN` output and splits the reward across many `OP_1`/P2TR miner outputs — both consensus-legal in Pearl (`P2TR` / `P2MR` / `OP_RETURN` only).
- **Shares verify cheaply.** A peer validates an incoming share with `verify_plain_proof` (CPU, no GEMM recompute) or the ~60 KB recursive-plonky2 ZK certificate (~ms, size-independent), and every proof is cryptographically bound to its exact coinbase/payout set (no replay).

The one genuinely Pearl-specific constraint is **share/proof size on the wire** (60 KB–370 KB per share vs. a few hundred bytes in BTC/XMR P2Pool); the network design is shaped around it (per-pool difficulty caps gossip to ~1 share / share-time globally; prune to the PPLNS window; fetch proofs on demand).

## Why Python

The entire reusable surface from the Pearl repo is Python: the gateway's `getblocktemplate` -> coinbase -> `submitblock` path, the `pearl-stratum-srv` stratum server + PPLNS split, and the `pearl_mining` (PyO3) verification bindings (`verify_plain_proof` / `verify_proof` / `generate_proof`). The original Bitcoin P2Pool was also Python. Share throughput is low (~1 share / 10 s globally), so Python is fine for the sidechain/P2P layer; the perf-critical proof verification already lives in compiled Rust behind `pearl_mining`. A native core can be swapped in later if needed.

## Architecture

```
           +---------------------------------------------------------+
           |  p2pearl  (one daemon per miner/node)                   |
  pearld <-|  - node RPC: getblocktemplate / submitblock             |
  (:44107) |  - coinbase builder: PPLNS P2TR outputs + OP_RETURN     |
           |  - sidechain engine: shares, PPLNS, uncles, retarget    |
  submit ->|  - share verifier: pearl_mining.verify_plain_proof      |
  block    |  - P2P gossip (shares + found blocks)                   |
           |  - stratum server (dialect-tolerant; SRBMiner-ready)    |
           +----------------^----------------------------------------+
                            | stratum: notify{header, share_target} / submit plain_proof
                  SRBMiner / GPU fleet (unchanged - just repoint --pool)
```

> **Decentralization:** nodes form one shared pool by **gossiping shares over P2P** (`--peer`). Each incoming share is **trustlessly verified** — a peer recomputes the deterministic PPLNS payouts from its *own* sharechain, confirms the share commits to exactly that set, reconstructs the byte-identical header, verifies the proof at the consensus share target, and checks the coinbase value against Pearl's emission schedule. A finder can forge neither the PoW, the reward split, the difficulty, nor the pot size. Validated end-to-end on the public testnet with independent operators.

See [`docs/blueprint.md`](docs/blueprint.md) for the full, source-grounded design.

## Repository layout

```
src/p2pearl/
  config.py              consensus params + runtime config (the sidechain "chainparams")
  consensus/
    share.py             ShareBlock: sidechain block format + serialization + id
    pplns.py             feeless, operator-less PPLNS reward split
    difficulty.py        difficulty <-> target, consensus retarget, target_to_bits
    subsidy.py           Pearl's emission schedule, replicated exactly from pearld
    sharechain.py        store / validate / GHOST uncles / chain-select / retarget / prune
  chain/
    node_rpc.py          minimal pearld JSON-RPC client (getblocktemplate/submit)
    coinbase.py          multi-output coinbase: PPLNS P2TR outputs + OP_RETURN
  pow/
    verify.py            pearl_mining wrappers (nested target + nbits-override)
  stratum/
    protocol.py          JSON-RPC framing + Pearlhash dialect parsing
    server.py            dialect-tolerant miner-facing stratum server
  p2p/node.py            gossip: announce/on-demand proof fetch, relay, window sync
  daemon.py              PoolNode orchestrator: per-miner jobs, verify, block path
tests/                   unit tests (102 passing)
docs/                    blueprint + running-a-node guide
tools/apply_m2_binding.py  one-step additive patch for a stock Pearl checkout
integration/             cross-repo notes (py-pearl-mining binding, stratum dialect)
```

## Status & how to test

Runs today — no node, no GPU, no native build:

```bash
pip install -e ".[dev]"
PYTHONPATH=src python -m pytest -q       # 102 passing (pure stdlib + a faked pearl_mining)
python -m p2pearl demo                   # watch the full pipeline: 2 nodes, stratum, P2P gossip, PPLNS
```

Live validation so far: real blocks mined and accepted on regtest **and the public Pearl testnet**, GPU miners (SRBMiner) connecting with zero protocol changes, two independent operators cross-verifying shares and sharing feeless coinbases on-chain. **Testers and contributors with a Pearl node and/or a GPU rig are very welcome** — see [issue #1](https://github.com/JustAResearcher/P2Pearl/issues/1).

Toward mainnet: per-miner vardiff, transaction-fee collection (shares are coinbase-only today), and a longer multi-operator testnet soak.

## Development

```bash
# from the repo root
python -m venv .venv && . .venv/Scripts/activate      # Windows; use bin/activate on *nix
pip install -e ".[dev]"

# run the full unit-test suite (pure stdlib + a faked pearl_mining; no node or GPU needed)
PYTHONPATH=src python -m pytest -q
```

A live deployment additionally needs a running `pearld`, the Pearl repo's `pearl_mining` module
(built via `maturin develop` on a Linux rig) and `bitcoinutils`; see [`docs/running-a-node.md`](docs/running-a-node.md).

## Relationship to the Pearl repo

P2Pearl depends on, but does not vendor, the Pearl repo (`pearl_mining` for proof verification; the gateway's block/coinbase serialization conventions). It anchors its sidechain to a `pearld` full node you run yourself. The M2 share-verification binding is an additive change to the Pearl repo's `zk-pow` + `py-pearl-mining`, applied by [`tools/apply_m2_binding.py`](tools/apply_m2_binding.py) and documented in [`integration/py-pearl-mining-nbits-override.md`](integration/py-pearl-mining-nbits-override.md). Consensus rules referenced throughout are grounded in `pearl/node/blockchain/validate.go` and `pearl/node/chaincfg/params.go`.

## License

MIT — see [`LICENSE`](LICENSE).
