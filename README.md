# P2Pearl

**A decentralized, zero‑fee, P2Pool‑style mining pool for [Pearl](https://github.com/pearl-research-labs/pearl) (the Pearlhash proof‑of‑useful‑work coin).**

P2Pearl is to Pearl what [P2Pool](https://github.com/SChernykh/p2pool) is to Monero: a peer‑to‑peer mining pool with **no operator, no pool wallet, and a 0 % fee**. Miners mine a shared *sharechain*; when the pool finds a real Pearl block, its coinbase pays every recent contributor directly and proportionally, enforced by consensus — there is no one to take a cut, go rogue, or be shut down.

> **Status: early scaffold (v0.0.2).** The consensus math (share format, PPLNS split, difficulty), the sidechain engine, the proof‑verification wrappers, the node RPC client, and the multi‑output coinbase builder are implemented and unit‑tested. The P2P gossip layer, and the stratum front‑end are stubbed with defined interfaces. See [`ROADMAP.md`](ROADMAP.md) and the full design in [`docs/blueprint.md`](docs/blueprint.md).

## Why this is feasible (and why Pearl is a clean target)

Pearl is a **btcd/Bitcoin fork** (UTXO chain, `getblocktemplate`, real Bitcoin coinbase, transaction merkle root, `nbits` compact targets) with the Pearlhash proof‑of‑useful‑work bolted on as a **succinct, CPU‑verifiable ZK certificate**. That combination is almost ideal for a P2Pool port:

- **One solution clears two targets.** Pearl's PoW is a plain threshold, `U256(hash_jackpot) ≤ bound(nbits)`, and the hash is independent of the target — so the *same* solution is graded against an easy **share** target and the hard **block** target (share and block targets are nested). The Pearlhash stratum job already carries a share `target` distinct from the header's block `nbits`.
- **The coinbase carries the commitment.** P2Pearl writes its sidechain commitment into an `OP_RETURN` output and splits the reward across many `OP_1`/P2TR miner outputs — both consensus‑legal in Pearl (`P2TR` / `P2MR` / `OP_RETURN` only).
- **Shares verify cheaply.** A peer validates an incoming share with `verify_plain_proof` (CPU, no GEMM recompute) or the ~60 KB recursive‑plonky2 ZK certificate (~ms, size‑independent), and every proof is cryptographically bound to its exact coinbase/payout set (no replay).

The one genuinely Pearl‑specific constraint is **share/proof size on the wire** (60 KB–370 KB per share vs. a few hundred bytes in BTC/XMR P2Pool); the network design is shaped around it (per‑pool difficulty caps gossip to ~1 share / share‑time globally; prune to the PPLNS window; fetch proofs on demand).

## Why Python

The entire reusable surface from the Pearl repo is Python: the gateway's `getblocktemplate`→coinbase→`submitblock` path, the `pearl-stratum-srv` stratum server + PPLNS split, and the `pearl_mining` (PyO3) verification bindings (`verify_plain_proof` / `verify_proof` / `generate_proof`). The original Bitcoin P2Pool was also Python. Share throughput is low (~1 share / 10 s globally), so Python is fine for the sidechain/P2P layer; the perf‑critical proof verification already lives in compiled Rust behind `pearl_mining`. A native core can be swapped in later if needed.

## Architecture

```
           ┌─────────────────────────────────────────────────────────┐
           │  p2pearl  (one daemon per miner/node)                    │
  pearld ◀─┤  • node RPC: getblocktemplate / submitblock              │
  (:44107) │  • coinbase builder: PPLNS P2TR outputs + OP_RETURN      │
           │  • sidechain engine: shares, PPLNS, uncles, retarget     │
  submit ─▶│  • share verifier: pearl_mining.verify_plain_proof       │
  block    │  • P2P gossip (shares + found blocks)                    │
           │  • stratum server (alphapool dialect — SRBMiner‑ready)   │
           └───────────────▲──────────────────────────────────────────┘
                           │ stratum: notify{header, share_target} / submit plain_proof
                  SRBMiner / GPU fleet (unchanged — just repoint --pool)
```

See [`docs/blueprint.md`](docs/blueprint.md) for the full, source‑grounded design.

## Repository layout

```
src/p2pearl/
  config.py              consensus params + runtime config (the sidechain "chainparams")
  consensus/
    share.py             ShareBlock: sidechain block format + canonical serialization + id   [implemented]
    pplns.py             feeless, operator‑less PPLNS reward split                            [implemented]
    difficulty.py        sidechain difficulty <-> target, retarget                            [implemented]
    sharechain.py        store / validate / chain‑select / reorg                              [stub]
  chain/
    node_rpc.py          minimal pearld JSON‑RPC client (getblocktemplate / submitblock)      [implemented]
    coinbase.py          multi‑output coinbase: PPLNS P2TR outputs + OP_RETURN commitment     [implemented]
  pow/
    verify.py            wrappers over pearl_mining (nested target + plain‑proof verify)       [implemented]
  p2p/node.py            share/block gossip + peer manager                                    [stub]
  stratum/server.py      miner‑facing stratum front‑end                                       [stub]
  daemon.py              wires it together; `p2pearl` entrypoint                              [stub]
tests/                   unit tests for the implemented consensus math
```

## Development

```bash
# from the repo root
python -m venv .venv && . .venv/Scripts/activate      # Windows; use bin/activate on *nix
pip install -e ".[dev]"

# run the implemented unit tests (pure stdlib — no node or GPU needed)
PYTHONPATH=src python -m pytest -q
# or, without pytest installed:
PYTHONPATH=src python tests/test_pplns.py && PYTHONPATH=src python tests/test_share.py
```

Integration (sidechain/P2P/stratum) additionally needs a running `pearld` plus the Pearl repo's `pearl_mining` module and `bitcoinutils`; see [`ROADMAP.md`](ROADMAP.md).

## Relationship to the Pearl repo

P2Pearl depends on, but does not vendor, the Pearl repo (`pearl_mining` for proof verification; the gateway's block/coinbase serialization conventions). It anchors its sidechain to a `pearld` full node you run yourself. Consensus rules referenced throughout are grounded in `pearl/node/blockchain/validate.go` and `pearl/node/chaincfg/params.go`.

## License

MIT — see [`LICENSE`](LICENSE).
