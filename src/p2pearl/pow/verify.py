"""Proof verification wrappers over the Pearl ``pearl_mining`` (PyO3) module.

Two responsibilities:
  1. The nested-target check, ``meets_target`` — used for BOTH the share target
     and the parent block target (the same ``hash_jackpot`` is graded against two
     thresholds). This is pure Python and always available.
  2. Cheap verification of an incoming share's ``plain_proof`` (CPU, no GEMM
     recompute), bound to a specific incomplete header, at the SHARE target.

``pearl_mining`` is imported lazily so this package imports (and the unit tests
run) on machines without it.
"""

from __future__ import annotations


def _pearl_mining():
    try:
        import pearl_mining  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ImportError(
            "pearl_mining is required for proof verification; build/install the "
            "Pearl repo's py-pearl-mining (maturin develop)."
        ) from exc
    return pearl_mining


def meets_target(hash_jackpot_le: bytes, target: int) -> bool:
    """True iff this PoW solution clears ``target``.

    Pearl compares the 256-bit ``hash_jackpot`` as a LITTLE-endian integer against
    the (already h*w*k-adjusted) bound. Pass the share bound for a share check and
    the block bound for a block check — the hash is identical, only the threshold
    differs (this is what makes one solution able to satisfy both).
    """
    if len(hash_jackpot_le) != 32:
        raise ValueError("hash_jackpot_le must be 32 bytes")
    return int.from_bytes(hash_jackpot_le, "little") <= target


def verify_share(incomplete_header_bytes: bytes, plain_proof_b64: str, share_nbits: int) -> bool:
    """Verify a submitted share's plain proof against the SHARE target.

    A share clears the easy share target, not the block ``nbits`` embedded in the
    header, so verification must grade the recomputed jackpot at ``share_nbits``.
    The Rust verifier supports an nbits override
    (``check_jackpot_difficulty_with_nbits`` / Go ``VerifyZKCertificateWithNbits``);
    if the PyO3 layer does not yet expose it, add a thin binding (see ROADMAP).
    """
    pm = _pearl_mining()
    proof = pm.PlainProof.from_base64(plain_proof_b64)
    verify_with_nbits = getattr(pm, "verify_plain_proof_with_nbits", None)
    if verify_with_nbits is None:
        raise NotImplementedError(
            "pearl_mining exposes no nbits-override plain-proof verifier. Expose "
            "check_jackpot_difficulty_with_nbits through py-pearl-mining so shares "
            "can be graded at the share target (see ROADMAP, 'pearl_mining bindings')."
        )
    return bool(verify_with_nbits(incomplete_header_bytes, proof, share_nbits))
