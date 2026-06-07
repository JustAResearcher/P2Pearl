# Pearl P2Pool — Architecture Blueprint

**A decentralized, zero‑fee mining pool for Pearl (Pearlhash PoUW), modeled on Monero's P2Pool.**

Feasibility‑focused architecture blueprint. Pearl mechanics are grounded in the local source at `C:/Source/pearl`; P2Pool mechanics are from primary sources (the `SChernykh/p2pool` README + `docs/MERGE_MINING.MD` + `src/side_chain.cpp`, Monero core, and SChernykh's GitHub issue #389), fact‑checked via a 98‑agent deep‑research pass.

Date: 2026‑06‑06.

---

## 0. Verdict (make‑or‑break)

**It is feasible — and Pearl is arguably a *cleaner* P2Pool target than Monero.** The thing that could have killed it (an exotic PoW that can't carry a reusable sidechain commitment) is a non‑issue: **Pearl is a btcd/Bitcoin fork** (full node `pearld` in `pearl/node/`, ISC‑licensed, implementing the matmul Proof‑of‑Useful‑Work of arXiv:2504.09971). It is a UTXO chain with `getblocktemplate`, a real Bitcoin coinbase, a transaction merkle root, and Bitcoin `nbits`. The PoUW is bolted on as a **succinct, CPU‑verifiable ZK certificate** — exactly the property a decentralized sharechain needs.

| # | Make‑or‑break question | Answer | Source |
|---|---|---|---|
| **(a)** | Header exposes a commitment field? | **Yes — two.** Bitcoin `merkle_root` over `[coinbase]+txs`, plus a fully miner‑controlled coinbase whose `OP_RETURN` output carries the sidechain commitment. | `pearl_header.py`; coinbase builder `blockchain_utils.py:91`; consensus allows `NullDataTy` `validate.go:222` |
| **(b)** | Deterministic template reconstruction? | **Yes.** GBT returns mempool txs + `coinbasevalue`; the *client* builds the coinbase; subsidy is a closed‑form function of height. Every peer rebuilds an identical template. | `pearl_client.py:88`; `CalcBlockSubsidy` `validate.go:160` |
| **(c)** | One solution clears share‑target *and* block‑target? | **Yes, natively.** PoW is `U256_LE(hash_jackpot) ≤ bound(nbits)` — a monotone threshold on a single keyed‑BLAKE3 value. The verifier exposes an `nbits_override` path so the *same* solution is graded against an easy share‑`nbits` and the canonical block‑`nbits`. Targets are nested. | `sanity_checks.rs:71` `check_jackpot_difficulty_with_nbits`; `verify.rs:30`; Go `VerifyZKCertificateWithNbits` |
| **(d)** | Does PoUW break the "embed commitment + reuse PoW" trick? | **No — it reinforces it.** The proof is cryptographically bound to the coinbase (`merkle_root` feeds both `job_key=blake3(header‖cfg)` and the circuit's `public_data_commitment`), so a proof is valid **only** for its exact payout set. Verification is succinct (recursive plonky2 STARK→PLONK, ~60 KB, ~14 FRI queries, **CPU‑only, no GEMM recompute, cost independent of matrix size**). | `proof_utils.rs:970` (`public_data_commitment`); `pearl_circuit.rs:1‑77`, `verify.rs` |
| **(e)** | Coinbase split across many miners? | **Yes.** Coinbase outputs are an uncapped `TxOutput` list; miner addresses `prl1p…` are bech32m **P2TR**, an allowed coinbase script type. N P2TR outputs + 1 `OP_RETURN` is consensus‑legal. The in‑tree `pearl‑stratum‑srv` already computes the exact PPLNS split. | `blockchain_utils.py:123`; allowed script types `validate.go:228`; `pearl‑stratum‑srv/payouts.py` |

**Bottom line:** the design transfers almost 1:1 from Bitcoin P2Pool. The bulk of the work is net‑new sidechain‑consensus code (there is *no* pool/stratum/P2Pool code in the repo to fork). The only genuinely Pearl‑specific engineering risk is **share/proof size on the gossip layer** (shares are ~60 KB–370 KB, vs. a few hundred bytes in BTC/XMR P2Pool) — manageable, but it shapes the network design.

---

## 1. Reference design: how Monero P2Pool works

(Verified primary‑source facts; these are the pieces we map onto Pearl in §3–4.)

### 1.1 The sharechain (a second, faster blockchain)
P2Pool runs a **separate blockchain — the "sharechain" — merge‑mined with the parent chain**. Miners mine parent‑chain block *candidates*; a candidate that clears an easy **share target** becomes a sharechain block ("share"); a candidate that also clears the hard parent target is a real Monero block. Monero P2Pool parameters: **10‑second share time**, **PPLNS window up to 2160 shares (~6 h, auto‑adjustable, capped 2160)** (`p2pool/README.md` "Default P2Pool parameters").

### 1.2 The merge‑mined coinbase commitment (the core trick)
A **single PoW solution satisfies both chains** because the parent‑chain coinbase commits the current sharechain block, and the parent PoW hashes that coinbase (via the tx merkle root). Concretely in Monero: a `TX_EXTRA_MERGE_MINING_TAG` (tag byte `0x03`) in the coinbase `tx_extra` carries a varint "merkle‑tree‑parameters" value + a **32‑byte merkle root** (`docs/MERGE_MINING.MD`; Monero `tx_extra.h`). **Ratification:** the coinbase (`miner_tx`) hash **must be the first leaf** of the parent tx merkle tree (`get_tx_tree_hash` pushes the coinbase hash first), so a short merkle branch proves the aux commitment was inside the PoW‑hashed root (Tari RFC‑0132; Monero `cryptonote_format_utils.cpp`). Bitcoin P2Pool instead embeds its commitment in the **coinbase scriptSig / `OP_RETURN`** (conceptually identical; exact byte layout was *not* pinned to a primary source in this pass — see Open Questions).

### 1.3 Share difficulty is **per‑pool, not per‑miner**
This is the single most important scaling decision. Sidechain difficulty = **pool_hashrate × share_time** — every miner mines to the *same* share difficulty; only shares meeting that one difficulty are gossiped. Per‑miner variable difficulty would be **O(N²) gossip** ("2× miners → 4× traffic… doesn't scale, like at all" — SChernykh, issue #389). To serve different miner sizes, P2Pool instead runs **three difficulty‑tiered sidechains** (main, mini `:37888`, nano `:37890`; nano uses 30 s share time, 10 % uncle penalty).

### 1.4 PPLNS payout — no operator, 0 % fee, non‑custodial
There is **no pool wallet**. Each miner builds a parent block template whose **coinbase pays *every* miner holding shares in the PPLNS window directly**, each amount **proportional to the total difficulty of that miner's shares in the window** (`README.md`, Moneropedia). 0 % pool fee, 0 payout fee, structurally (payouts are consensus‑enforced coinbase outputs, not a trusted disbursement). "Pool admin can't go rogue… there is no pool admin." (v4.6+ ships an *opt‑out* donation that uses spare merge‑mining capacity on a *separate* aux chain — it takes 0 from Monero rewards; a Pearl port simply omits it.)

### 1.5 Uncle shares (GHOST) — no orphaned work
Competing same‑height shares are included as **uncles worth 20 % less** (`unclePenalty=20` → 80 % weight), referenceable up to **3 sharechain blocks back** (`UNCLE_BLOCK_DEPTH=3`, `src/side_chain.cpp`). This is why "all your shares will be accounted for." The original Bitcoin P2Pool used a *linear* sharechain and orphaned (unpaid) competing shares — the main reason small miners disliked it.

### 1.6 Consensus & failure modes
- **Cheap share verification:** a peer validates an incoming share with merkle proofs only — no parent re‑execution (`MERGE_MINING.MD`).
- **Minimum hashrate / variance:** if the pool can't find parent blocks faster than ~1 per PPLNS window, some shares expire **unpaid** (~15 MH/s floor on Monero's main sharechain); it averages out long‑run. This is P2Pool's central UX limitation and the reason for the tiered sidechains.

---

## 2. Pearl mechanics that matter (ground truth from source)

### 2.1 Pearl is a btcd fork — we can run the whole stack
`C:/Source/pearl` is the `pearl-research-labs/pearl` monorepo. Components: **`node/`** = `pearld` full node (consensus, P2P, mempool, UTXO, RPC — a btcd fork in Go); `wallet/` (Oyster HD wallet); `spv/`; `dnsseeder/`; `zk-pow/` (Rust, plonky2 ZK PoW); `pearl-blake3/`; `plonky2/` (vendored); `py-pearl-mining/` (PyO3 `pearl_mining` bindings); **`miner/`** (GPU miner + `pearl-gateway` + `pearl-stratum-srv`). **There is no pool/stratum/P2Pool code in‑tree** (the fleet's stratum lives outside the repo). Chain params (`node/chaincfg/params.go`): mainnet magic `PRLM`, P2P `:44108`, RPC `:44107`, **block time 194 s (3 m 14 s)**, **per‑block WTEMA retarget** (1‑week half‑life), **coinbase maturity 100**, max block 1,000,000 vbytes, address HRP `prl`/`tprl`/`rprl`, `GrainPerPearl = 1e8`. Emission: smooth decay, **no halvings**, `subsidy = totalSupply·C / ((h+C)(h−1+C))`, `totalSupply = 2.1e9 PRL`, `C = 650226` (`validate.go:160`).

### 2.2 Header + PoW predicate — **there is no nonce**
`IncompleteBlockHeader` (76 bytes, `zk-pow/src/api/proof.rs`) = `{version u32, prev_block [32], merkle_root [32], timestamp u32, nbits u32}` — **no nonce field**. The full header adds a 32‑byte `proof_commitment` (= hash of the ZK certificate), appended *after* mining (`pearl_header.py`). The mining search space is the **randomly drawn A/B int8 matrices** ("the A/B *are* the nonce"). The header binds the work: `job_key = blake3(incomplete_header ‖ mining_config)` seeds the noise (`mine.rs:156`).

PoW predicate (NOT `double_sha256(header) < target` — that's only used for the tx merkle root):
```
U256_LE(hash_jackpot) ≤ extract_difficulty_bound(nbits, mining_config)
```
where `hash_jackpot = blake3(jackpot_words, key = a_noise_seed)` and `a_noise_seed` derives from `job_key`; `extract_difficulty_bound` = standard Bitcoin compact‑bits decode of `nbits`, then ×`(h·w·k)` (a constant for a fixed mining config) (`sanity_checks.rs:71‑106`). `hash_jackpot` depends on `(header, config, A, B)` **but not on the target** → a found solution is graded against *any* `nbits` you supply. **Share and block targets are nested thresholds on the same value.** ✅ (a)(c)

One "attempt" = draw random A/B, derive noise from the header, run one noisy int8 GEMM over the tile grid → *many* candidate jackpot hashes per GEMM (one per tile), each a threshold test (`mine.rs:19‑118`).

### 2.3 The ZK certificate — succinct, CPU‑verifiable, coinbase‑bound
The PoUW proof is a **real 3‑layer recursive plonky2 system** (Starky STARK → plonky2 PLONK → ZK plonky2), not a Fiat‑Shamir spot‑check (`pearl_circuit.rs:1‑77`). The on‑chain object `ZKProof = {public_data[164], proof_data ≤ 60000}` (`proof_utils.rs:907`). **Verification:** CPU‑only (no GPU, grep `cuda|gpu` in `zk-pow` → none), **no GEMM recompute**, **cost independent of (m,n,k)** — one PLONK verify (vanishing‑poly eval + ~14 FRI query rounds, `circuit_utils.rs:117`). Order ~1–10 ms/core; parallelizable via the Go/FFI path (the Python binding holds a global mutex). **Anti‑replay / coinbase binding:** `public_data_commitment = blake3("V1" ‖ block_header_bytes ‖ public_data ‖ pow_bits ‖ rate_bits)` is a circuit public input, and `block_header_bytes` contains `merkle_root` → the proof is valid **only** for its exact coinbase (`proof_utils.rs:970`). ✅ (d)

`plain_proof` = the *raw* (non‑succinct) solution — Merkle‑authenticated int8 row strips of A and Bᵀ + indices (`zk-pow/src/ffi/plain_proof.rs`), bincode+base64, **~137 KB–370 KB**. `verify_plain_proof` re‑derives noise + jackpot over the revealed strips and checks difficulty (`verify.rs:94`) — cheaper than mining (touches only sampled rows, not the full GEMM), **no prover needed**. `generate_proof(header, plain_proof) → ZKProof` runs the expensive prover to make the succinct cert (`proof_generator.py`).

### 2.4 Coinbase / template / payout — fully P2Pool‑compatible
- **GBT, client builds coinbase:** `getblocktemplate` with `capabilities:["coinbasevalue","coinbase/append"]` returns mempool txs + `coinbasevalue` (`pearl_client.py:88`); node emits a placeholder coinbase only (`node/mining/mining.go:224`). The coinbase is a real `bitcoinutils.Transaction` built in `create_coinbase_transaction` (`blockchain_utils.py:91`) — **outputs are a `TxOutput` list with no count cap**, scriptSig = BIP34 height + extranonce byte + aux flags.
- **Allowed coinbase output scripts (consensus):** **P2TR, P2MR (BIP360), `OP_RETURN` only** — no legacy scripts (`validate.go:228`). Miner addresses `prl1p…` are bech32m **P2TR** → `OP_1 <32‑byte program>` (`get_script_pubkey_from_p2tr_address` `blockchain_utils.py:146`). ✅ (e)
- **Value rule:** Σ coinbase outputs ≤ `subsidy(h) + fees`; under‑pay legal, over‑pay = `ErrBadCoinbaseValue` (`validate.go:1004`). Maturity 100 blocks.
- **Submit:** `submitblock(block_hex)` where block = `ZK_CERTIFICATE ‖ HEADER ‖ TX_COUNT(varint) ‖ TXNS` (`pearl_block.py:48`, `pearl_client.py:103`). The node is an **external `pearld`** over JSON‑RPC (default `http://…:44107`), but its source is in‑tree, so consensus rules are fully known.

### 2.5 Miner interface — the fleet already speaks a poolable protocol
Two stratum dialects exist; the production fleet runs **SRBMiner‑Multi `--algorithm pearlhash`** (Herominers today). Critically, the stratum job **already separates the share target from the block target**: `mining.notify` carries `header` (with block `nbits`) **and** a separate `target` (the share threshold, sent as a 256‑bit value); the device accepts when `jackpot_hash ≤ target·(h·w·k)`. **No extranonce / no nonce partitioning** exists (the search space is the random matrices) — so pooling is *connection consolidation + share counting*, not work‑splitting. The in‑tree **`pearl-stratum-srv`** is a near‑complete reference: long‑poll job push on new tips, `clean_jobs` broadcast, vardiff, **PPLNS split** (`compute_pplns_payouts → [(addr, sats)]`) — but it pays **off‑chain via wallet `sendmany`**. Converting that PPLNS list into **on‑chain coinbase outputs** is the heart of turning it into a true P2Pool.

---

## 3. Mapping: P2Pool piece → Pearl

| P2Pool concept | Monero/Bitcoin mechanism | Pearl equivalent | Transfer |
|---|---|---|---|
| Parent block candidate | Monero/BTC block template | Pearl GBT template + client‑built coinbase | **Clean** (`pearl-gateway` already does it) |
| Sidechain commitment | `tx_extra` tag (XMR) / coinbase scriptSig‑`OP_RETURN` (BTC) | **`OP_RETURN` output** = hash of the new share | **Clean** (Bitcoin‑style; `OP_RETURN` is consensus‑allowed) |
| Ratification of commitment by PoW | coinbase‑first tx merkle root | Same — Pearl `merkle_root` over `[coinbase]+txs` (`blockchain_utils.py:23`) | **Clean** |
| One PoW → two targets | `hash < min(parent,share)`, submit to whichever met | `hash_jackpot ≤ bound(nbits)`; grade vs share‑`nbits` and block‑`nbits` via `nbits_override`; stratum already sends a separate share `target` | **Clean / already present** |
| Share difficulty = pool_hashrate × share_time | per‑pool, gossip only pool‑diff shares | Identical; sidechain difficulty retargets to a chosen Pearl share time | **Clean** |
| PPLNS payout → coinbase outputs | N outputs, weight ∝ Σ share difficulty | N **P2TR** outputs from `compute_pplns_payouts`; reuse the existing splitter | **Clean** (move off‑chain → coinbase) |
| Uncle shares (GHOST) | `UNCLE_BLOCK_DEPTH=3`, 20 % penalty | Same algorithm on the Pearl sidechain | **Port logic** (net‑new code, same math) |
| Cheap share verification | merkle proof, no re‑exec | `verify_plain_proof` (CPU, sampled rows) or ZK `verify_proof` (~ms) | **Clean — Pearl's killer feature** |
| Sidechain block / consensus engine | P2Pool C++ (`side_chain.cpp`, gossip, reorg) | **Net‑new** for Pearl | **Build from scratch** |
| Share on the wire | ~few hundred bytes | **~60 KB (ZK) / ~137–370 KB (plain)** | **Redesign** — bandwidth/storage is the real delta |
| Parent‑chain retarget | XMR/BTC | Pearl **per‑block WTEMA, 194 s** | **Re‑parameterize** sidechain math |

---

## 4. Pearl P2Pool architecture

### 4.1 Components
```
           ┌─────────────────────────────────────────────────────────┐
           │  pearl-p2pool  (the new daemon — one per miner/node)     │
           │                                                          │
  pearld ◀─┤  • GBT poller + deterministic coinbase/template builder  │
  (:44107) │  • Sidechain engine: share blocks, PPLNS, uncles,        │
           │    difficulty retarget, chain selection, reorg           │
           │  • P2P gossip (shares + found blocks) + peer mgr         │
  submit ─▶│  • Share verifier (verify_plain_proof / verify_proof)    │
  block    │  • Stratum server (alphapool dialect — SRBMiner-ready)   │
           └───────────────▲──────────────────────────────────────────┘
                           │ stratum (mining.notify {header, share_target} / submit plain_proof)
                  SRBMiner / GPU fleet  (unchanged)
```
Reuse wholesale: `pearl-gateway` GBT/coinbase/submit primitives (`pearl_client.py`, `blockchain_utils.py`, `pearl_block.py`), `pearl-stratum-srv` stratum + PPLNS split + long‑poll, `pearl_mining.verify_plain_proof/verify_proof/generate_proof`. Build new: the sidechain consensus engine + the P2P layer + the coinbase‑embedding of PPLNS.

### 4.2 The sidechain (share block)
A Pearl **share** = a candidate Pearl block whose `hash_jackpot ≤ bound(share_nbits)` and whose coinbase pays the PPLNS window. The **sidechain block** stored/gossiped contains:
- `prev_share_hash`, `sidechain_height`, `uncle_refs[]` (GHOST), `sidechain_cumulative_difficulty`;
- the **finder's `prl1p…` P2TR address**;
- the parent anchor: `prev_block` (Pearl tip it built on), `parent_height`, `timestamp`, `share_nbits`, the **block `nbits`** used;
- the parent coinbase pre‑image (enough to rebuild it deterministically) — i.e. the PPLNS payout set it commits to;
- the **PoW evidence**: `plain_proof` (for share validation) or `ZKProof` (if also a block).
The parent coinbase's `OP_RETURN` = `H(sidechain_block_header)`. Because `merkle_root` covers that coinbase and feeds `job_key`+`public_data_commitment`, the PoW and proof are bound to *this exact* share/payout set (no replay).

**Recommended consensus parameters (Pearl):**
- **Share time:** 10 s (mirror P2Pool main). Sidechain difficulty = `pool_hashrate × 10 s`, retargeted continuously (independent of the parent's WTEMA).
- **PPLNS window:** size to ≈ a small integer number of *parent* blocks so payouts land regularly. At 194 s parent blocks, a ~2000‑share window ≈ 5.6 h ≈ ~104 parent blocks — generous; tune toward "≈ a few parent blocks per window" to cut variance (e.g. 600–1200 shares). **Window length is a tunable; calibrate to live network difficulty.**
- **Uncles:** `UNCLE_DEPTH=3`, penalty 20 % (port `side_chain.cpp` math).
- **Chain selection:** highest cumulative sidechain difficulty; bounded reorg depth (≤ window).

### 4.3 Single solution → share or block
The daemon hands each miner: `incomplete_header` (with the **block** `nbits` for the current Pearl tip + the PPLNS‑paying coinbase's `merkle_root`) **and** a **share `target`** (= `bound(share_nbits)`, easier). The miner (SRBMiner, unchanged) draws A/B, and submits any `plain_proof` with `hash_jackpot ≤ share_target`. The daemon:
1. `verify_plain_proof(header, plain_proof)` against `share_nbits` → valid share → add to sidechain, gossip.
2. Also test against the **block** `nbits`. If it clears, `generate_proof(...)` → `ZKProof`, assemble `ZK_CERTIFICATE‖HEADER‖TXNS`, `submitblock` to `pearld`, and gossip the found block. (The prover runs **once per real block, by the finder** — no worse than today's solo `gateway` path, which already proves‑then‑submits.)

### 4.4 PPLNS → coinbase (the feeless payout)
For the current sidechain tip, walk back the PPLNS window, sum each address's share weights (normal 100 %, uncle 80 %), and build the coinbase outputs **deterministically**:
- one **P2TR** `TxOutput(amount, OP_1‖program)` per distinct address, `amount = floor((subsidy(h)+fees) · weight_i / Σweights)`;
- **deterministic ordering** (e.g. by address bytes) and a fixed remainder rule (assign the floor dust to a defined output) so every peer's coinbase is byte‑identical;
- one `OP_RETURN` = sidechain commitment; keep the segwit witness‑commitment output (`aa21a9ed…`) required by `rules:["segwit"]`.
Reuse `pearl-stratum-srv/payouts.py compute_pplns_payouts` (already returns `[(addr, sats)]` summing exactly to the pot) — just emit outputs instead of a wallet `sendmany`. **0 % fee is structural**: no wallet, no operator, payouts are consensus‑enforced coinbase outputs.
**Dust:** P2Pool‑style `min_payout` (the reference uses 100,000 grains = 0.001 PRL); below‑threshold weights **carry forward** to the next window rather than create dust UTXOs (recommended; exact rule is an Open Question to pin against BTC P2Pool).

### 4.5 Share verification & the bandwidth problem (the one real Pearl‑specific design axis)
Because per‑pool difficulty means **only ~1 share per share_time is gossiped network‑wide** (not one per miner), raw rates are modest, but each share is large:
- **Gossip `plain_proof` (~137–370 KB), not the ZK cert.** It needs no prover and validates via `verify_plain_proof` (CPU, sampled rows). At 10 s share time that's ~14–37 KB/s network‑wide — fine.
- **Retain only the PPLNS window** of shares (prune older). At 370 KB × 1000 shares ≈ 370 MB worst‑case proof storage; using a compacted share representation (store the `plain_proof` only for the window, drop strips outside it) keeps it bounded.
- **Validating cost:** `verify_plain_proof` per share ≪ a full GEMM; a fresh peer can re‑validate a 1000‑share window in seconds. The succinct ZK path (~ms, size‑independent) is the fallback if `plain_proof` bandwidth proves too high.
- **This is the inverse of BTC/XMR P2Pool** (tiny shares): here CPU is cheap but **bytes are the constraint** — so favor a low peer fan‑out / relay topology and consider gossiping a share *header* first, fetching the proof on demand.

### 4.6 P2P layer & consensus
Net‑new, modeled on P2Pool: gossip `share` and `block` messages; peer discovery via seed nodes (could piggyback Pearl's `*.pearlresearch.ai` seeders for bootstrap, plus pool‑specific seeds); **DoS/Sybil**: accept only shares meeting pool difficulty (the O(N²) guard), rate‑limit per peer, ban on invalid `plain_proof`/commitment, cap reorg depth at the window. Chain selection = highest cumulative sidechain difficulty; uncles fold in orphaned work.

### 4.7 Miner compatibility
Present work over `pearl-stratum-srv`'s **alphapool‑compatible stratum** (the dialect SRBMiner/alpha‑miner already speak): `mining.notify` with the PPLNS‑coinbase `header` + the pool `share_target`; `mining.submit [worker, job_id, plain_proof_b64]`; long‑poll → `clean_jobs` on every new sidechain tip or parent tip. The fleet connects with **zero miner changes** — only the `--pool` endpoint changes.

---

## 5. Hard blockers & risks (honest)

| Item | Severity | Reality |
|---|---|---|
| **Sidechain consensus engine is net‑new** | **High effort, not a blocker** | No P2Pool/pool code in‑tree. This is the bulk of the build (P2Pool's own `side_chain.cpp` + p2p is ~tens of kLOC). Math/structure ports directly; it's *work*, not a wall. |
| **Share/proof bandwidth & storage** | **Medium** | 60 KB–370 KB per share vs. hundreds of bytes. Bounded by per‑pool difficulty (≈1 share/share_time globally) + window pruning + on‑demand proof fetch. The defining design constraint, but tractable. |
| **Prover latency for found blocks** | **Low** | The finder must run the recursive plonky2 prover before `submitblock`. **This already happens in solo/gateway mining** and the network tolerates it; P2Pool adds no new requirement. Verify prover wall‑clock ≪ 194 s to avoid self‑orphaning. |
| **Deterministic template/coinbase reconstruction** | **Low/Medium** | Every peer must rebuild a byte‑identical coinbase. Needs strict tx‑selection + output‑ordering + dust rules. Standard Bitcoin‑P2Pool engineering. |
| **Per‑block WTEMA, 194 s parent blocks** | **Low** | Sidechain difficulty + window math must use the WTEMA/194 s model, not Bitcoin's 2016‑step. Re‑parameterization only. |
| **Coinbase script‑type restriction (P2TR/P2MR/OP_RETURN)** | **None** | Miner addresses are P2TR; commitment is `OP_RETURN`. Fully compatible. |
| **Minimum‑hashrate / payout variance** | **Inherent** | Same as Monero P2Pool: if the pool can't find a Pearl block per window, some shares expire unpaid. Mitigate by sizing the window and (optionally) offering a lower‑difficulty "mini" sidechain tier. |
| **No nonce / random‑matrix search** | **None (simplifies)** | No extranonce coordination; miners draw independent matrices. Eliminates a whole class of stratum work‑splitting logic. |

**No identified make‑or‑break blocker.** Every "(a)–(e)" requirement is satisfied by Pearl's existing consensus + the in‑tree gateway/stratum primitives.

---

## 6. What to reuse vs. build

**Reuse (in‑tree):**
- `pearl/node` (`pearld`) — run our own node to anchor the sidechain.
- `pearl/miner/pearl-gateway` — `getblocktemplate` → coinbase builder → `submitblock` (`pearl_client.py`, `blockchain_utils.py`, `pearl_block.py`, `zk_certificate.py`).
- `pearl/miner/pearl-stratum-srv` — stratum server (alphapool dialect), long‑poll job push, vardiff, and `payouts.py` PPLNS split.
- `pearl_mining` (`py-pearl-mining`) — `verify_plain_proof`, `verify_proof`, `generate_proof`.

**Build new:**
1. **Sidechain engine** — share‑block format, store/validate, PPLNS accounting, uncles (port `side_chain.cpp` constants/math), continuous difficulty retarget, cumulative‑difficulty chain selection, bounded reorg.
2. **Coinbase‑embedding of PPLNS** — turn `compute_pplns_payouts` into deterministic P2TR coinbase outputs + `OP_RETURN(H(share))`; deterministic ordering/dust rules.
3. **P2P gossip** — share/block messages, peer discovery/bootstrap, anti‑Sybil/DoS, on‑demand proof fetch.
4. **Glue** — wire the sidechain tip into the stratum `mining.notify` (header+share_target) and route winning shares to `generate_proof`+`submitblock`+gossip.

---

## 7. Open questions to resolve before building
1. **Bitcoin P2Pool's exact coinbase‑commitment serialization** (scriptSig vs `OP_RETURN`, how the previous‑share hash / share merkle root is encoded) — confirm against forrestv's source / Bitcoin Wiki before fixing Pearl's `OP_RETURN` format. *(Web pass surfaced the concept but didn't pin the bytes.)*
2. **Dust / minimum‑output rule** — carry‑forward vs. drop for sub‑`min_payout` miners (affects UTXO bloat and small‑miner fairness).
3. **Deterministic template reconstruction** — exact tx‑selection + output‑ordering + tie‑break so independently built coinbases hash identically.
4. **Calibrate share time + PPLNS window** to live Pearl network difficulty (the ~15 MH/s‑equivalent floor for Pearl), and decide whether to launch a single tier or main+mini.
5. **Prover wall‑clock** on the finder for a real block — measure to confirm it's safely ≪ 194 s.
6. **`pearld` getblocktemplate longpoll** semantics + reorg notifications — confirm the sidechain reacts to parent reorgs cleanly.

---

### Appendix — key source references
**Pearl (local):** `pearl/node/blockchain/validate.go` (subsidy/maturity/coinbase rules), `node/chaincfg/params.go` (chain params), `node/mining/mining.go` (GBT coinbase); `zk-pow/src/api/{proof,proof_utils,sanity_checks,verify}.rs` (header, PoW predicate, target, proof binding), `zk-pow/src/circuit/pearl_circuit.rs` (ZK system), `zk-pow/src/ffi/plain_proof.rs`; `miner/pearl-gateway/src/pearl_gateway/{pearl_client,blockchain_utils,...}` (GBT/coinbase/submit), `.../blockchain_utils/{pearl_header,pearl_block,zk_certificate}.py`; `miner/pearl-stratum-srv/{connection,server,payouts,job_registry}.py` (stratum + PPLNS reference).
**P2Pool (web, primary):** `SChernykh/p2pool` `README.md`, `docs/MERGE_MINING.MD`, `src/side_chain.cpp`, `src/pool_block.h`, GitHub issue #389; Monero `tx_extra.h`, `cryptonote_format_utils.cpp`; Tari RFC‑0132; Bitcoin Wiki P2Pool.
