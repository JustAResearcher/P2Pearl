# P2Pearl

**A decentralized, zero-fee, P2Pool-style mining pool for [Pearl](https://github.com/pearl-research-labs/pearl) (the Pearlhash proof-of-useful-work coin).**

P2Pearl is to Pearl what [P2Pool](https://github.com/SChernykh/p2pool) is to Monero: a peer-to-peer mining pool with **no operator, no pool wallet, and a 0 % fee**. Miners mine a shared *sharechain*; when the pool finds a real Pearl block, its coinbase pays every recent contributor directly and proportionally, enforced by consensus — there is no one to take a cut, go rogue, or be shut down.

> **Status: early scaffold (v0.0.6).** Implemented and unit-tested (73 tests): the consensus core (share format, feeless PPLNS split, difficulty + `target_to_bits`, and the **sidechain engine** — linkage validation, GHOST uncles, cumulative-difficulty chain selection, PPLNS walk, pruning), the **nbits-override share verifier** wrappers, the pearld node RPC client, the multi-output coinbase builder, and the **miner-facing stratum server** (dialect-tolerant), and the **daemon orchestrator** (`PoolNode`) that wires them into a node — per-miner jobs, submit -> verify -> sharechain -> gossip, and the block-found -> assemble -> submitblock path. and the **P2P gossip layer** (`p2p/node.py`: announce + on-demand proof fetch, relay, window sync). All five milestones M1-M5 are implemented and unit-tested, and the **M6 live loop is validated end-to-end**: against a Pearl `regtest` node, P2Pearl mined real blocks that `pearld` accepted, with a feeless PPLNS coinbase (0 operator outputs) confirmed on-chain. What remains is hardening for a public testnet (P2P `verify_incoming`, share-target calibration). See [`ROADMAP.md`](ROADMAP.md) and the full design in [`docs/blueprint.md`](docs/blueprint.md).

## Run / Download

A prebuilt single-file Windows executable, `p2pearl.exe`, is attached to the [GitHub release](https://github.com/JustAResearcher/P2Pearl/releases). No Python install needed — **double-click it to run the demo** (the window stays open until you press Enter), or run it from a terminal:

```bat
p2pearl.exe --version       :: print the version
p2pearl.exe demo            :: run the local end-to-end demo (no node/GPU/native build)
p2pearl.exe daemon          :: wire + run a live pool node (needs pearld + pearl_mining + bitcoinutils)
```

`p2pearl.exe demo` boots two gossiping pool nodes and simulated miners and prints a share flowing verify -> sidechain -> PPLNS -> P2P gossip across both nodes — the same demo as `examples/local_demo.py`. From a source checkout the same CLI is available as `python -m p2pearl` (or `p2pearl` after `pip install -e .`).

### Run a pool node (operators, Linux)

`p2pearl daemon` connects to your local `pearld`, serves a stratum port your GPU miners point at, and — when it finds a Pearl block — pays the PPLNS window directly in the coinbase (no operator wallet, no fee). It needs a running `pearld` and the native `pearl_mining` build (see the M6 notes in [issue #1](https://github.com/JustAResearcher/P2Pearl/issues/1)):

```bash
pip install -e .
p2pearl daemon --rpc-url http://127.0.0.1:44107 --rpc-user <u> --rpc-pass <p>
# then point miners at it (same as any Pearlhash pool):
SRBMiner-MULTI --algorithm pearlhash --pool <node-ip>:3360 --wallet <prl1...> --disable-cpu
```

`p2pearl daemon --help` lists the flags (`--stratum-port`, `--share-target`, RPC creds; creds also read from `P2PEARL_RPC_USER` / `P2PEARL_RPC_PASS`). Validated on regtest: a real SRBMiner GPU mines into `p2pearl daemon` and the blocks pearld accepts pay the miner's PPLNS share with zero operator outputs.

> **Decentralization status:** today each node runs **standalone** (its blocks pay its own connected miners' PPLNS window). The cross-operator **P2P gossip** that fuses many nodes into one shared, trustless pool is the next milestone — it needs trustless share verification (`verify_incoming`), which in turn needs the share to carry enough to deterministically reconstruct each other's headers. Tracked in [issue #1](https://github.com/JustAResearcher/P2Pearl/issues/1).

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

See [`docs/blueprint.md`](docs/blueprint.md) for the full, source-grounded design.

## Repository layout

```
src/p2pearl/
  config.py              consensus params + runtime config (the sidechain "chainparams")
  consensus/
    share.py             ShareBlock: sidechain block format + serialization + id    [implemented]
    pplns.py             feeless, operator-less PPLNS reward split                   [implemented]
    difficulty.py        sidechain difficulty <-> target, retarget, target_to_bits  [implemented]
    sharechain.py        store / validate / GHOST uncles / chain-select / prune     [implemented]
  chain/
    node_rpc.py          minimal pearld JSON-RPC client (getblocktemplate/submit)   [implemented]
    coinbase.py          multi-output coinbase: PPLNS P2TR outputs + OP_RETURN      [implemented]
  pow/
    verify.py            pearl_mining wrappers (nested target + nbits-override)      [implemented]
  stratum/
    protocol.py          JSON-RPC framing + Pearlhash dialect parsing               [implemented]
    server.py            dialect-tolerant miner-facing stratum server               [implemented]
  p2p/node.py            gossip: announce/on-demand proof fetch, relay, sync         [implemented]
  daemon.py              PoolNode orchestrator: per-miner jobs, verify, block       [implemented]
tests/                   unit tests (73 passing)
integration/             cross-repo notes (py-pearl-mining binding, stratum dialect)
```

## Status & how to test

All five milestones (M1-M5) are implemented and unit-tested (73 tests): the sidechain engine, feeless PPLNS, share verification, the dialect-tolerant stratum server, the daemon orchestrator (per-miner jobs), and the P2P gossip layer are wired together and orchestration-tested end-to-end with fakes. **What remains is live bring-up against a real Pearl node + GPU miners**, tracked in [issue #1 (M6)](https://github.com/JustAResearcher/P2Pearl/issues/1). **Testers and contributors with a Pearl node and/or a GPU rig are very welcome.**

Runs today — no node, no GPU, no native build:

```bash
pip install -e ".[dev]"
PYTHONPATH=src python -m pytest -q       # 73 passing (pure stdlib + a faked pearl_mining)
python examples/local_demo.py            # watch the full pipeline run: 2 nodes, stratum, P2P gossip, PPLNS
```

`examples/local_demo.py` boots two real pool nodes (stratum + P2P gossip as live services), drives simulated miners over the real stratum protocol, and shows shares flowing verify -> sidechain -> gossip across both nodes plus the feeless PPLNS payout — all with no node/GPU/native build.

A live pool additionally needs (details in [issue #1](https://github.com/JustAResearcher/P2Pearl/issues/1)):

1. The `verify_plain_proof_with_nbits` binding built into `pearl_mining` — an additive Pearl-repo change documented in [`integration/py-pearl-mining-nbits-override.md`](integration/py-pearl-mining-nbits-override.md) (`maturin develop` on Linux).
2. Validation of the coinbase/header **byte orientation** against a Pearl regtest/testnet — the production `make_header`/`assemble_block` adapters and the P2P `verify_incoming` are the only pieces tested with fakes.
3. `share_target` calibration to live pool hashrate + sidechain difficulty retarget.

## Development

```bash
# from the repo root
python -m venv .venv && . .venv/Scripts/activate      # Windows; use bin/activate on *nix
pip install -e ".[dev]"

# run the full unit-test suite (pure stdlib + a faked pearl_mining; no node or GPU needed)
PYTHONPATH=src python -m pytest -q
```

A live deployment additionally needs a running `pearld`, the Pearl repo's `pearl_mining` module
(built via `maturin develop` on a Linux rig) and `bitcoinutils`; see [`ROADMAP.md`](ROADMAP.md) and
[`integration/`](integration/).

## Relationship to the Pearl repo

P2Pearl depends on, but does not vendor, the Pearl repo (`pearl_mining` for proof verification; the gateway's block/coinbase serialization conventions). It anchors its sidechain to a `pearld` full node you run yourself. The M2 share-verification binding is an additive change to the Pearl repo's `zk-pow` + `py-pearl-mining`, documented in [`integration/py-pearl-mining-nbits-override.md`](integration/py-pearl-mining-nbits-override.md). Consensus rules referenced throughout are grounded in `pearl/node/blockchain/validate.go` and `pearl/node/chaincfg/params.go`.

## License

MIT — see [`LICENSE`](LICENSE).
