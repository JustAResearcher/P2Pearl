# Running a P2Pearl node

A P2Pearl node connects to your own `pearld` full node, serves a stratum port your
miners point at, gossips shares with other operators over P2P, and — when it finds a
block — pays the PPLNS window directly in the coinbase (no operator, no fee). This guide
builds everything from source on Linux.

> **Why the patch step?** P2Pearl grades *shares* at an easy share target, which needs
> `pearl_mining.verify_plain_proof_with_nbits`. The Rust verifier already implements the
> nbits override (`check_jackpot_difficulty_with_nbits`, used by the Go node); it just
> isn't surfaced to Python upstream. `tools/apply_m2_binding.py` adds that ~40-line
> additive wrapper to a stock Pearl checkout. See
> [`integration/py-pearl-mining-nbits-override.md`](../integration/py-pearl-mining-nbits-override.md).

## Prerequisites
- **Go 1.26.1+**, a **Rust** toolchain, a **C compiler**, and the [Task](https://taskfile.dev) runner — for `pearld` (see Pearl's `node/README.md`).
- **Python 3.10+** and **[maturin](https://www.maturin.rs)** — for the `pearl_mining` module.

## 1. Clone Pearl and apply the M2 binding
```bash
git clone https://github.com/pearl-research-labs/pearl
git clone https://github.com/JustAResearcher/P2Pearl
python P2Pearl/tools/apply_m2_binding.py pearl     # additive + idempotent
```

## 2. Build `pearl_mining` (the proof verifier/prover)
```bash
python -m venv venv && . venv/bin/activate
pip install maturin
( cd pearl/py-pearl-mining && maturin develop --release )   # ~1 min
python -c "import pearl_mining; print(pearl_mining.verify_plain_proof_with_nbits)"  # must exist
```

## 3. Build `pearld` (the Pearl full node)
```bash
( cd pearl && task build:pearld build:prlctl )   # builds zk-pow FFI + libxmss, then the Go node
```
Then run it and let it sync (testnet shown; drop `--testnet` for mainnet):
```bash
./pearl/bin/pearld --testnet --notls \
  --rpcuser=u --rpcpass=p --rpclisten=127.0.0.1:44109 &
# wait until `prlctl ... getblocktemplate` stops returning "downloading blocks"
```

## 4. Install P2Pearl and its runtime deps
```bash
pip install -e P2Pearl            # the p2pearl package + CLI
pip install bitcoin-utils numpy   # coinbase building + ZK-certificate serialization
# PearlBlock/PearlHeader/ZKCertificate live in the Pearl gateway; only its pure-Python
# blockchain_utils is needed (NO torch), so just put its src on the path:
export PYTHONPATH="$PWD/pearl/miner/pearl-gateway/src:$PYTHONPATH"
```

## 5. Run your node and join the network
```bash
p2pearl daemon \
  --rpc-url http://127.0.0.1:44109 --rpc-user u --rpc-pass p \
  --peer 107.214.187.2:37900            # an existing operator's node (or omit to run solo)
```
Point miners at your stratum (default `:3360`):
```bash
SRBMiner-MULTI --algorithm pearlhash --pool <your-node-ip>:3360 --wallet <prl1...> --disable-cpu
```
Your node now serves miners, gossips shares with peers (each is trustlessly verified by
reconstructing its header), and any block it finds pays every recent contributor across
the network their share — feeless, no operator. To let others reach you, forward TCP
`3360` (stratum) and `37900` (P2P) to your node.

## Prover speed — avoiding orphaned blocks

When your pool finds a block, the node must generate its Pearlhash **ZK certificate**
(`pearl_mining.generate_proof`) before `submitblock`. That proof is the *entire* latency
between finding a block and announcing it — every second another (non-pool) Pearl miner
could find the same height and orphan yours. It is CPU-bound recursive proving; the node
already keeps the event loop responsive (proves in a worker thread), serializes proving,
and deduplicates so a flood of block-clearing shares proves **once**. Two operator knobs
make the proof itself much faster (measured on a 16C/32T Ryzen, warm prover):

| | co-located CPU load running | load paused for the prove |
|---|---|---|
| `RAYON_NUM_THREADS` **unset** | ~17.5 s | ~5.8 s |
| `RAYON_NUM_THREADS` **set** (=cores) | ~9.7 s | **~3.3 s** |

1. **`RAYON_NUM_THREADS` is pinned automatically** to your logical-CPU count (proving
   plateaus at the physical core count, so that is the ceiling). Leaving it unset makes
   the prover ~2× slower under load. Override by exporting it yourself if you want fewer
   threads. *(It must be set before `pearl_mining` is first imported; the daemon does this
   for you.)*
2. **Pause co-located CPU load during the prove.** If the same box also CPU-mines (e.g.
   XMR), that contention roughly triples the prove time. Hand the daemon a command to
   pause/resume it around the (rare) block-assembly:
   ```bash
   p2pearl daemon --rpc-url ... \
     --pause-cmd  'pkill -STOP -x xmrig' \
     --resume-cmd 'pkill -CONT -x xmrig'
   ```
   The prove runs on a contention-free machine (~3.3 s here) and the miner resumes
   immediately after — negligible lost CPU-mining time since blocks are rare. Match the
   miner **by name** (`pkill -x xmrig`), not `pgrep -f xmrig`: a `-f` pattern also matches
   the hook's own shell process and would SIGSTOP it. The hook is best-effort and bounded
   by a timeout — a failure or hang is logged and never blocks the found block.

A faster CPU helps proportionally up to ~16 effective threads; a GPU/accelerated prover
is not available in the upstream plonky2 build (CPU only).

> **Status:** validated on the public Pearl testnet — independent operators' shares
> cross-verify and real blocks pay multiple operators proportionally. Mainnet adds
> `share_target` calibration and per-miner vardiff (on the roadmap).
