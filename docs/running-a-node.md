# Running a P2Pearl node

A P2Pearl node connects to your own `pearld` full node, serves a stratum port your
miners point at, gossips shares with other operators over P2P, and — when it finds a
block — pays the PPLNS window directly in the coinbase (no operator, no fee). This guide
builds everything from source on Linux; **Windows users usually need none of it** — see
[Windows](#windows-native) below.

> **Why the patch step?** P2Pearl grades *shares* at an easy share target, which needs
> an nbits override on `verify_plain_proof`. Upstream pearl merged exactly that
> (PR #161: `verify_plain_proof(header, proof, nbits_override=None)`), so on a current
> checkout `tools/apply_m2_binding.py` detects it and **no-ops** — just build. On an
> older checkout it adds the equivalent ~40-line additive wrapper. See
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

## Windows (native)

The whole node stack runs natively on Windows — validated end-to-end (a real block
built, CPU-mined, and ZK-proved on a Windows machine was accepted by `pearld`).

**The no-build way:** the release [`p2pearl.exe`](https://github.com/JustAResearcher/P2Pearl/releases/latest)
already bundles `pearl_mining` + the whole coinbase stack. Download it, run it (the
control panel opens), fill in the RPC fields for your `pearld`, click **Test pearld**,
then **Start node**. Done.

Live-node testing after Pearl's June 2026 MoE hard fork requires `pearld` 1.1.0 or
newer. If the GUI-managed node is older, **Start node** now stops immediately with an
upgrade message instead of waiting for a node that cannot sync v2 certificate blocks.

**From source** (only if you want to build it yourself) — prereqs: Python 3.12+,
[Rust](https://rustup.rs) with the MSVC toolchain, git:

```powershell
git clone https://github.com/pearl-research-labs/pearl
git clone https://github.com/JustAResearcher/P2Pearl
python P2Pearl\tools\apply_m2_binding.py pearl     # no-ops on current pearl (PR #161)
python -m venv venv; .\venv\Scripts\Activate.ps1
pip install maturin
cd pearl\py-pearl-mining; maturin develop --release; cd ..\..
pip install -e P2Pearl bitcoin-utils numpy
$env:PYTHONPATH = "$PWD\pearl\miner\pearl-gateway\src"
p2pearl gui        # or: p2pearl daemon --rpc-url http://<pearld-host>:44109 ...
```

**Where does `pearld` run? Natively on Windows too — no WSL anywhere.**

- **Let the exe handle it (easiest)**: `p2pearl.exe` BUNDLES `pearld`. Tick
  **"Run pearld for me"** in the control panel and Start — it extracts the node to
  `~/.p2pearl/bin`, runs it with your RPC credentials (chain data in
  `~/.p2pearl/pearld-data`), shows sync progress, starts the pool when ready, and
  shuts it down cleanly (via the `stop` RPC) when you close the window. The bundled
  node must be `pearld` 1.1.0 or newer for current mainnet/testnet blocks.
- **Download it separately**: `pearld-windows-x86_64.zip` is attached to the
  [release](https://github.com/JustAResearcher/P2Pearl/releases/latest) (built from
  stock upstream source; ISC license included). Unzip and run:

  ```powershell
  .\pearld.exe --notls --rpcuser=u --rpcpass=p                                    # mainnet
  .\pearld.exe --testnet --notls --rpcuser=u --rpcpass=p --rpclisten=127.0.0.1:44109  # testnet
  ```

  Let it sync, then point P2Pearl at `http://127.0.0.1:44107` (mainnet) or `:44109`
  (testnet). Validated end-to-end: a native-Windows `pearld` accepted blocks built,
  mined, and ZK-proved by the native-Windows P2Pearl stack — one machine, zero Linux.
- **Build it yourself**: [`tools/build_pearld_windows.ps1`](../tools/build_pearld_windows.ps1)
  automates the whole thing (Rust `windows-gnu` FFI + mingw `libxmss` + Go/CGO).
  Prereqs: Go 1.26+, rustup, and mingw-w64 (e.g. [winlibs.com](https://winlibs.com)).
- Or run `pearld` on any other machine you can reach over RPC (keep RPC off the
  public internet), or in WSL per the Linux steps above.

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
> cross-verify and real blocks pay multiple operators proportionally. The share target
> now retargets by consensus (~1 share / 10 s pool-wide) and coinbase values are
> validated against Pearl's exact emission schedule. Mainnet adds per-miner vardiff and
> transaction-fee collection (on the roadmap); the full knob list is in the README's
> [Configuration reference](../README.md#configuration-reference).
