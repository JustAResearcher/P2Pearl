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

### M2 — `pearl_mining` bindings  ⬜
- Expose an **nbits-override** plain-proof verifier (`check_jackpot_difficulty_with_nbits` /
  Go `VerifyZKCertificateWithNbits`) through `py-pearl-mining` so shares can be graded at the
  share target, not the header's block nbits. (`pow/verify.verify_share` depends on this.)
- Confirm exact signatures of `PlainProof.from_base64`, `verify_plain_proof`, `generate_proof`.

### M3 — Block assembly & submission  ⬜
- When a share clears the parent block target: run `generate_proof` (the recursive plonky2
  prover), assemble `ZK_CERTIFICATE | HEADER | TX_COUNT | TXNS`, `submitblock`, gossip.
- Measure prover wall-clock; confirm it is safely ≪ 194 s (else self-orphan risk).

### M4 — Stratum front-end (`stratum/server.py`)  ⬜
- Adapt `pearl_stratum_srv` (alphapool dialect) so SRBMiner connects unchanged.
- Push `mining.notify {header, share_target}` on each new parent/sidechain tip; handle base64
  `plain_proof` submits; route to verify -> sharechain -> (maybe) block submit.

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
