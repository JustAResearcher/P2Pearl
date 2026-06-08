#!/usr/bin/env python3
"""Apply the M2 nbits-override binding to a Pearl checkout so ``pearl_mining`` exposes
``verify_plain_proof_with_nbits`` — the share-target grader P2Pearl needs.

Stock ``pearl_mining`` (from pearl-research-labs/pearl) can verify a plain proof only
against the header's *block* nbits. A P2Pool-style pool must also accept *shares* that
clear an easier *share* target. The Rust verifier already has the nbits-override check
(``check_jackpot_difficulty_with_nbits`` in ``zk-pow/src/api/sanity_checks.rs``, used by
the Go ``VerifyZKCertificateWithNbits``); it's just not surfaced to Python. This script
adds a thin, additive wrapper that exposes it.

The change is **two functions, ~40 lines, purely additive** — no upstream code is
modified or removed. It is idempotent (safe to re-run) and anchors on source text (not
line numbers), so it tolerates unrelated changes around the insertion points.

Usage:
    git clone https://github.com/pearl-research-labs/pearl
    python apply_m2_binding.py path/to/pearl
    # then build the Python module, e.g.:
    cd path/to/pearl/py-pearl-mining && maturin develop --release

See ``integration/py-pearl-mining-nbits-override.md`` and ``docs/running-a-node.md``.
"""
import pathlib
import sys

NEW_FN = '''/// Like `verify_plain_proof` but grades the recomputed jackpot at `nbits_override`
/// (the easy SHARE target) instead of the header's block nbits. Share-grading only;
/// never use for block acceptance. (P2Pearl M2 — additive wrapper.)
pub fn verify_plain_proof_with_nbits(
    block_header: &IncompleteBlockHeader,
    plain_proof: &PlainProof,
    nbits_override: Option<u32>,
) -> Result<()> {
    let (private_params, mut public_params) = parse_plain_proof(*block_header, plain_proof)?;
    public_params_sanity_check(&public_params)?;
    for strip in private_params.s_a.iter().chain(private_params.s_b.iter()) {
        for &val in strip {
            ensure!((-64..=64).contains(&val), "Matrix value {} out of range [-64, 64]", val);
        }
    }
    let compiled = CompiledPublicParams::from(&public_params);
    let noise = compute_noise(&compiled);
    let jackpot = compute_jackpot(&compiled, &private_params.s_a, &private_params.s_b, &noise);
    public_params.hash_jackpot = compute_jackpot_hash(&jackpot, compiled.a_noise_seed());
    check_jackpot_difficulty_with_nbits(&public_params, nbits_override)?;
    Ok(())
}

'''

NEW_PYF = '''#[pyfunction]
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

'''


def _fail(path: pathlib.Path, what: str) -> None:
    print(f"!! {path.name}: {what}")
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if "verify_plain_proof" in line or "wrap_pyfunction" in line:
            print(f"   {ln}: {line.strip()}")
    sys.exit(2)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = pathlib.Path(sys.argv[1]).expanduser().resolve()
    verify_rs = root / "zk-pow/src/api/verify.rs"
    lib_rs = root / "py-pearl-mining/src/lib.rs"
    for p in (verify_rs, lib_rs):
        if not p.exists():
            print(f"!! not a Pearl checkout (missing {p.relative_to(root)}): {root}")
            return 2

    # 1) zk-pow verify.rs — add the pub fn just before the existing verify_plain_proof.
    src = verify_rs.read_text(encoding="utf-8")
    if "fn verify_plain_proof_with_nbits" in src:
        print("verify.rs: already patched")
    else:
        anchor = "pub fn verify_plain_proof("
        if anchor not in src:
            _fail(verify_rs, "anchor 'pub fn verify_plain_proof(' not found")
        i = src.index(anchor)
        verify_rs.write_text(src[:i] + NEW_FN + src[i:], encoding="utf-8")
        print("verify.rs: added verify_plain_proof_with_nbits")

    # 2) py-pearl-mining lib.rs — add the #[pyfunction] and register it on the module.
    src = lib_rs.read_text(encoding="utf-8")
    if "fn verify_plain_proof_with_nbits" in src:
        print("lib.rs: already patched")
    else:
        anchor = "#[pyfunction]\nfn verify_plain_proof("
        if anchor not in src:
            _fail(lib_rs, "anchor '#[pyfunction] fn verify_plain_proof(' not found")
        i = src.index(anchor)
        src = src[:i] + NEW_PYF + src[i:]
        reg = "m.add_function(wrap_pyfunction!(verify_plain_proof, m)?)?;"
        if reg not in src:
            _fail(lib_rs, "module-registration anchor for verify_plain_proof not found")
        src = src.replace(
            reg,
            reg + "\n    m.add_function(wrap_pyfunction!(verify_plain_proof_with_nbits, m)?)?;",
            1,
        )
        lib_rs.write_text(src, encoding="utf-8")
        print("lib.rs: added + registered verify_plain_proof_with_nbits")

    print("\nM2 binding applied. Build it, e.g.:")
    print(f"  cd {root / 'py-pearl-mining'} && maturin develop --release")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
