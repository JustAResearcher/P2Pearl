# P2Pearl roadmap

Status legend: ✅ implemented & unit-tested · 🟡 partial/untested · ⬜ not started

## Done (v0.0.1 scaffold)
- ✅ Consensus params / chainparams (`config.py`)
- ✅ Share block format + canonical serialization + id (`consensus/share.py`)
- ✅ Feeless, operator-less PPLNS split (`consensus/pplns.py`)
- ✅ Sidechain difficulty <-> target + retarget (`consensus/difficulty.py`)
- 🟡 pearld JSON-RPC client (`chain/node_rpc.py`) — code complete, needs a live node
- 🟡 Multi-output coinbase builder (`chain/coinbase.py`) — code complete, needs `bitcoinutils` + a template
- 🟡 Proof verification wrappers (`pow/verify.py`) — needs `pearl_mining` (+ an nbits-override binding)

## Milestones

### M1 — Sidechain engine (`consensus/sharechain.py`)  ✅
The core net-new work; mirrors Monero P2Pool `src/side_chain.cpp`.
- Share linkage validation (parent known, parent anchor not stale, timestamps sane)
- GHOST uncles (depth ≤ `UNCLE_BLOCK_DEPTH`, penalty `UNCLE_PENALTY_PERCENT`)
- PPLNS weight walk over the window (`pplns_weights`)
- Cumulative-difficulty chain selection + bounded reorg
- Persistence (store only the PPLNS window of shares + proofs; prune older)

**Status: ✅ implemented & unit-tested** (18 tests in `tests/test_sharechain.py`: genesis,
linear extension, cumulative difficulty, reorg-by-weight, full GHOST uncle rules, PPLNS
window cap, pruning, and every rejection path). Deferred to integration: pin a hardcoded
genesis id; explicit max-reorg-depth check (currently bounded by pruning); on-disk
persistence (in-memory today).

### M2 — `pearl_mining` bindings  ✅
- Expose an **nbits-override** plain-proof verifier (`check_jackpot_difficulty_with_nbits` /
  Go `VerifyZKCertificateWithNbits`) through `py-pearl-mining` so shares can be graded at the
  share target, not the header's block nbits. (`pow/verify.verify_share` depends on this.)
- Confirm exact signatures of `PlainProof.from_base64`, `verify_plain_proof`, `generate_proof`.

**Status: ✅ implemented & verified** (20-agent review + 49 P2Pearl tests; `zk-pow` `cargo build`
clean, exit 0). Added `pub fn verify_plain_proof_with_nbits` (`zk-pow/src/api/verify.rs`) + the
`#[pyfunction]` wrapper (`py-pearl-mining/src/lib.rs`) — both additive, applied in the Pearl working
tree (see [`integration/py-pearl-mining-nbits-override.md`](integration/py-pearl-mining-nbits-override.md)).
Fixed two real `pow/verify.py` bugs (raw-bytes header → typed `IncompleteBlockHeader.from_bytes`;
`bool(tuple)` always-truthy → unpack `(ok, _msg)`). Added `difficulty.target_to_bits` (the
256-bit-target → compact-`u32` converter the caller needs). Deferred: a live native test needs
`maturin develop` on a Linux rig; the block-acceptance path (M3) must use the unmodified
`verify_plain_proof`, never the override.

### M3 — Block assembly & submission + daemon wiring  ✅
- When a share clears the parent block target: run `generate_proof` (the recursive plonky2
  prover), assemble `ZK_CERTIFICATE | HEADER | TX_COUNT | TXNS`, `submitblock`, gossip.
- Measure prover wall-clock; confirm it is safely ≪ 194 s (else self-orphan risk).

**Status: ✅ implemented & unit-tested** (`daemon.py` `PoolNode` orchestrator + `ParentTemplate`;
`pow.verify.verify_block_solution`; 8 tests in `tests/test_daemon.py` incl. a full
PoolNode <-> StratumServer <-> two-miner integration test). The daemon wires it together: per-miner
job building (PPLNS coinbase + `OP_RETURN<candidate share id>`), submit -> `verify_share` ->
`sharechain.add_share` -> gossip, and the block path -> `verify_block_solution` (the UNMODIFIED
verifier) -> assemble -> `submitblock`. Also fixed a design bug integration surfaced: a share's
committed id now EXCLUDES `pow_hash` (the coinbase must commit before the solution exists).
⚠️ The production header/block-assembly adapters in `build_production_node` need bitcoinutils +
pearl_mining + the Pearl gateway (Linux build) and byte-orientation validation on testnet; the
orchestration itself is fully tested with fakes. Share-target calibration + sidechain difficulty
retarget wiring remain.

### M4 — Stratum front-end (`stratum/server.py`)  ✅
- Serve a **dialect-tolerant** Pearlhash stratum server so the fleet connects unchanged. SRBMiner
  speaks the LuckyPool/Herominers **object** dialect (the default); the server also tolerates the
  alphapool **positional** handshake and mirrors whichever the client used.
- Push `mining.notify` (header + 256-bit share `target`) on each new parent/sidechain tip; handle
  base64 `plain_proof` submits; route to an injected handler (verify -> sharechain -> maybe block).

**Status: ✅ implemented & unit-tested** (`stratum/server.py` + `stratum/protocol.py`; 10 tests in
`tests/test_stratum.py`: object + positional end-to-end over real loopback sockets, stale / malformed
/ handler-reject paths, the 547 KB submit-frame read-limit, unknown-method tolerance, job-registry
eviction). Transport + protocol only; the daemon (M3) injects the submit handler and calls
`update_job` on tip changes. ⚠️ The object-dialect choice is INFERRED (the fleet mines Herominers with
0 rejects), not from a live SRBMiner capture — confirm with a `bridge/logproxy` capture before
production; the tolerant design mitigates. See [`integration/stratum-dialect.md`](integration/stratum-dialect.md).

### M5 — P2P network (`p2p/node.py`)  ⬜
- SHARE_ANNOUNCE + on-demand GET_PROOF/PROOF (keep large proofs off the broadcast path)
- Peer discovery/bootstrap, HELLO/peer-exchange, window sync on join
- DoS/Sybil: pool-difficulty-only gossip, per-peer rate limits, ban invalid, bounded reorg

### M6 — Integration & testnet  ⬜
- End-to-end on Pearl regtest/testnet with a small fleet; verify a found block pays the
  PPLNS set on-chain with 0 operator outputs.

## Open design questions (from the blueprint)
1. **Bitcoin P2Pool commitment serialization** — confirm forrestv's exact coinbase commitment
   layout before finalizing our `OP_RETURN <share_id>` format.
2. **Dust / carry-forward** — current rule drops sub-min miners this block (shares persist
   in-window). Confirm against BTC P2Pool; decide explicit cross-window carry if desired.
3. **Deterministic template reconstruction** — pin tx-selection + output ordering so every
   peer's coinbase hashes identically (payouts are already deterministically ordered).
4. **Calibrate** `SHARE_TARGET_TIME_SECONDS` and `PPLNS_WINDOW_SHARES` to live Pearl network
   difficulty; decide whether to launch a single tier or main+mini.
5. **Parent reorgs** — ensure the sidechain reacts cleanly when pearld reorgs the parent tip.

See `docs/blueprint.md` for the full source-grounded architecture.
