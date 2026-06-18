"""Proof verification wrappers over the Pearl ``pearl_mining`` (PyO3) module.

Responsibilities:
  1. The nested-target check, ``meets_target`` - used for BOTH the share target
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
    the block bound for a block check - the hash is identical, only the threshold
    differs (this is what makes one solution able to satisfy both).
    """
    if len(hash_jackpot_le) != 32:
        raise ValueError("hash_jackpot_le must be 32 bytes")
    return int.from_bytes(hash_jackpot_le, "little") <= target


def verify_share(incomplete_header_bytes: bytes, plain_proof_b64: str, share_nbits: int,
                 cert_version: int | None = None) -> bool:
    """Verify a submitted share's plain proof against the SHARE target.

    A share clears the easy share target, not the block ``nbits`` embedded in the
    header, so verification grades the recomputed jackpot at ``share_nbits``.

    ``share_nbits`` is a Bitcoin-compact ``u32`` target (NOT a raw 256-bit value):
    the Rust verifier decodes it via ``nbits_to_difficulty`` and applies the
    ``h*w*k`` difficulty-adjustment factor itself, so the bound matches how the
    pool grades. Passing a raw 256-bit target would skip the ``h*w*k`` multiply
    (~2**19) and grade far too hard - zero shares. Convert a 256-bit share target
    to compact nbits with ``p2pearl.consensus.difficulty.target_to_bits``.

    Upstream pearl merged the nbits override into ``verify_plain_proof`` itself
    (PR #161: ``verify_plain_proof(header, proof, nbits_override=None)``), so a
    STOCK pearl checkout needs no patch. Older checkouts expose the same
    capability as the patched ``verify_plain_proof_with_nbits`` (added by
    ``tools/apply_m2_binding.py``); both forms return ``(ok: bool, message:
    str)`` — a rejected proof is ``(False, msg)``, not an exception.
    """
    pm = _pearl_mining()
    proof = pm.PlainProof.from_base64(plain_proof_b64)
    # The binding takes a typed IncompleteBlockHeader, not raw bytes. from_bytes
    # enforces the 76-byte layout and reverses prev_block/merkle_root back to the
    # internal orientation that job_key = blake3(header || config) expects.
    header = pm.IncompleteBlockHeader.from_bytes(incomplete_header_bytes)
    versioned = getattr(pm, "verify_plain_proof_for_cert_version", None)
    if versioned is not None and cert_version is not None:
        ok, _msg = versioned(cert_version, header, proof, nbits_override=share_nbits)
        return bool(ok)
    native = getattr(pm, "verify_plain_proof", None)
    if native is not None:
        try:
            # Unpack element [0] so a falsy result is not masked by tuple-truthiness.
            ok, _msg = native(header, proof, nbits_override=share_nbits)
            return bool(ok)
        except TypeError:
            pass  # pre-#161 pearl_mining without the kwarg — try the patched binding
    verify_with_nbits = getattr(pm, "verify_plain_proof_with_nbits", None)
    if verify_with_nbits is None:
        raise NotImplementedError(
            "this pearl_mining build cannot grade shares at an nbits override: "
            "update the pearl checkout (verify_plain_proof gained nbits_override "
            "in PR #161) or apply tools/apply_m2_binding.py and rebuild."
        )
    ok, _msg = verify_with_nbits(header, proof, share_nbits)
    return bool(ok)


def verify_block_solution(incomplete_header_bytes: bytes, plain_proof_b64: str,
                          cert_version: int | None = None) -> bool:
    """Verify a plain proof at the BLOCK target (the header's own ``nbits``).

    Used only on the block-found path: when a share also clears the parent block
    target, the daemon confirms it with the UNMODIFIED ``verify_plain_proof`` (which
    grades at the header's block nbits) before assembling and submitting the block.
    NEVER use ``verify_share`` (the nbits override) for block acceptance.
    """
    pm = _pearl_mining()
    proof = pm.PlainProof.from_base64(plain_proof_b64)
    header = pm.IncompleteBlockHeader.from_bytes(incomplete_header_bytes)
    versioned = getattr(pm, "verify_plain_proof_for_cert_version", None)
    if versioned is not None and cert_version is not None:
        ok, _msg = versioned(cert_version, header, proof)
        return bool(ok)
    ok, _msg = pm.verify_plain_proof(header, proof)
    return bool(ok)
