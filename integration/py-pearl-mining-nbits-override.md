# Integration: `verify_plain_proof_with_nbits` in `py-pearl-mining`

P2Pearl grades a submitted **share** at the easy *share* target, not the header's
block `nbits`. The Pearl proof verifier supports this (`zk-pow` has
`check_jackpot_difficulty_with_nbits` and the Go `VerifyZKCertificateWithNbits`),
but the **plain-proof** path was never wired to it and the PyO3 module never
exposed it. M2 adds a thin, additive binding.

> These two edits go in the **Pearl repo**, not in P2Pearl. Apply them to a fresh
> clone with **[`tools/apply_m2_binding.py`](../tools/apply_m2_binding.py)** (additive +
> idempotent), then build `pearl_mining` — see
> [`docs/running-a-node.md`](../docs/running-a-node.md). P2Pearl's code degrades
> gracefully (clear `NotImplementedError`) if the binding is absent.

## Edit 1 — `zk-pow/src/api/verify.rs` (new public fn, additive)

Add `verify_plain_proof_with_nbits` immediately after `verify_plain_proof`. It is a
byte-for-byte clone whose **only** difference is the final difficulty call:

```rust
pub fn verify_plain_proof_with_nbits(
    block_header: &IncompleteBlockHeader,
    plain_proof: &PlainProof,
    nbits_override: Option<u32>,
) -> Result<()> {
    // ... identical steps 1-6 (parse_plain_proof header/coinbase binding,
    // public_params_sanity_check, [-64,64] strip range, compute_noise,
    // compute_jackpot, hash_jackpot recompute) ...
    public_params.hash_jackpot = compute_jackpot_hash(&jackpot, compiled.a_noise_seed());
    check_jackpot_difficulty_with_nbits(&public_params, nbits_override)?;  // <-- only change
    Ok(())
}
```

No new imports (`check_jackpot_difficulty_with_nbits` is already imported at
`verify.rs:9`). The original `verify_plain_proof` is untouched, so block-level
verification keeps grading at the header's block `nbits`.

## Edit 2 — `py-pearl-mining/src/lib.rs` (new `#[pyfunction]` + registration)

```rust
#[pyfunction]
fn verify_plain_proof_with_nbits(
    block_header: IncompleteBlockHeader,
    plain_proof: PlainProof,
    nbits: u32,
) -> PyResult<(bool, String)> {
    match verify::verify_plain_proof_with_nbits(&block_header, &plain_proof, Some(nbits)) {
        Ok(()) => Ok((true, "Mining solution verified successfully".into())),
        Err(e) => Ok((false, e.to_string())),
    }
}
```

Registered next to the existing `verify_plain_proof` inside `#[pymodule] fn pearl_mining`.
Mirrors `verify_plain_proof` exactly: by-value pyclass args, no circuit cache (the
plain-proof path is pure recompute), and returns `(ok, message)` — a *rejected*
proof is `(False, msg)`, never a raised exception.

## P2Pearl side (committed)

- `src/p2pearl/pow/verify.py` — `verify_share` now (a) reconstructs a typed
  `IncompleteBlockHeader` via `pm.IncompleteBlockHeader.from_bytes(...)` instead of
  passing raw bytes, and (b) unpacks `(ok, _msg)` instead of `bool(tuple)` (which is
  always truthy — the old code would have accepted *every* share, including rejected
  ones). Tested in `tests/test_verify_share.py` against a fake `pearl_mining`.
- `src/p2pearl/consensus/difficulty.py` — new `target_to_bits(target) -> u32`
  (inverse of the gateway's `bits_to_target`). The binding takes a **compact `u32`
  nbits**, not a 256-bit target: the Rust side decodes it via `nbits_to_difficulty`
  and applies the `h*w*k` (~2^19) factor itself. A caller converts a 256-bit share
  target with `target_to_bits` before calling `verify_share`.

## Build & verify

```bash
# Rust compile-check (no Python needed):
cargo build --manifest-path <pearl>/zk-pow/Cargo.toml --lib

# Build the PyO3 module (Linux rig / target platform; needs maturin + py>=3.12):
cd <pearl>/py-pearl-mining && maturin develop --release
python -c "import pearl_mining; assert hasattr(pearl_mining, 'verify_plain_proof_with_nbits')"

# P2Pearl Python tests (no native build; fakes pearl_mining):
cd <P2Pearl> && python -m pytest -q
```

The only prebuilt artifact in the Pearl tree (`py-pearl-mining/target/release/libpearl_mining.so`)
is a **Linux** ELF and lacks the new symbol until rebuilt; a live `verify_share`
needs the `maturin develop` rebuild on the target platform.

## Still open (not M2)

- **Block acceptance must NOT use the override.** When P2Pearl adds the block-found
  path (M3), it must call the unmodified `verify_plain_proof` (header `nbits`), never
  `verify_plain_proof_with_nbits` — the override is share-grading only.
- **Caller wiring (M3/M4).** `verify_share` has no production caller yet (the stratum
  front-end and daemon loop are stubs). When wired, the daemon must hand `verify_share`
  the 76-byte incomplete header in the same on-wire (reversed-hash) orientation the
  stratum path uses, and `target_to_bits(share_target)` for the nbits.
- **`share_nbits` range validation.** Over-easy nbits saturates the bound to `U256::MAX`
  (no-op gate) and malformed nbits decodes to difficulty 0 (nothing passes); validate
  in the caller / `target_to_bits` path.
