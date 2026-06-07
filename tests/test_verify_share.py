"""Tests for ``p2pearl.pow.verify.verify_share`` against a FAKE ``pearl_mining``.

No native build is required: a stub ``pearl_mining`` module is installed into
``sys.modules`` (via monkeypatch) before each FRESH import of ``p2pearl.pow.verify``,
so these exercise the Python wiring (lazy import, header reconstruction from raw
bytes via from_bytes, share_nbits passthrough, (bool, str) tuple unpack, and the
NotImplementedError fallback) WITHOUT compiling the PyO3 extension.
"""

import importlib
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

HEADER_76 = bytes(range(76))      # arbitrary 76-byte incomplete header
PROOF_B64 = "UExBSU5QUk9PRg=="    # arbitrary base64; the fake parser ignores content


class _FakeHeader:
    """Stand-in for pearl_mining.IncompleteBlockHeader."""

    def __init__(self, version, prev_block, merkle_root, timestamp, nbits):
        self.version = version
        self.prev_block = prev_block
        self.merkle_root = merkle_root
        self.timestamp = timestamp
        self.nbits = nbits

    @staticmethod
    def from_bytes(data):
        # Mirrors the real IncompleteBlockHeader.from_bytes len==76 enforcement.
        if len(data) != 76:
            raise ValueError("data must be exactly 76 bytes")
        return _FakeHeader(0, data[4:36], data[36:68], 0, 0)


class _FakePlainProof:
    """Stand-in for pearl_mining.PlainProof."""

    def __init__(self, tag):
        self.tag = tag

    @staticmethod
    def from_base64(data):
        return _FakePlainProof(data)


def _make_fake_module(verify_impl):
    """Build a stub ``pearl_mining`` module.

    ``verify_impl`` is bound as ``verify_plain_proof_with_nbits``; pass ``None`` to
    omit the attribute entirely (exercises the NotImplementedError fallback).
    """
    mod = types.ModuleType("pearl_mining")
    mod.IncompleteBlockHeader = _FakeHeader
    mod.PlainProof = _FakePlainProof
    if verify_impl is not None:
        mod.verify_plain_proof_with_nbits = verify_impl
    return mod


def _load_verify(monkeypatch, fake_module):
    """Install the fake module and import a FRESH copy of verify."""
    monkeypatch.setitem(sys.modules, "pearl_mining", fake_module)
    monkeypatch.delitem(sys.modules, "p2pearl.pow.verify", raising=False)
    return importlib.import_module("p2pearl.pow.verify")


def test_verify_share_accepted(monkeypatch):
    calls = {}

    def fake_verify(header, proof, nbits):
        calls["header"] = header
        calls["proof"] = proof
        calls["nbits"] = nbits
        return (True, "Mining solution verified successfully")

    verify = _load_verify(monkeypatch, _make_fake_module(fake_verify))
    out = verify.verify_share(HEADER_76, PROOF_B64, 0x1E01FFFF)

    assert out is True
    # Header was rebuilt from raw bytes via from_bytes (NOT passed as bytes).
    assert isinstance(calls["header"], _FakeHeader)
    # PlainProof came from from_base64 with the exact b64 we passed.
    assert isinstance(calls["proof"], _FakePlainProof)
    assert calls["proof"].tag == PROOF_B64
    # share_nbits (compact u32) is forwarded unchanged.
    assert calls["nbits"] == 0x1E01FFFF


def test_verify_share_rejected_returns_false(monkeypatch):
    def fake_verify(header, proof, nbits):
        return (False, "Jackpot condition not satisfied: hash does not meet difficulty target")

    verify = _load_verify(monkeypatch, _make_fake_module(fake_verify))
    assert verify.verify_share(HEADER_76, PROOF_B64, 0x1E01FFFF) is False


def test_verify_share_unpacks_tuple_not_truthy_tuple(monkeypatch):
    # Regression guard: bool((False, "msg")) is True. verify_share MUST unpack
    # element [0], so a rejected proof must come back False, not the truthy tuple.
    def fake_verify(header, proof, nbits):
        return (False, "rejected")

    verify = _load_verify(monkeypatch, _make_fake_module(fake_verify))
    result = verify.verify_share(HEADER_76, PROOF_B64, 0x207FFFFF)
    assert result is False
    assert result is not True


def test_verify_share_not_implemented_without_binding(monkeypatch):
    verify = _load_verify(monkeypatch, _make_fake_module(None))
    with pytest.raises(NotImplementedError):
        verify.verify_share(HEADER_76, PROOF_B64, 0x1E01FFFF)


def test_verify_share_bad_header_length(monkeypatch):
    def fake_verify(header, proof, nbits):  # pragma: no cover - never reached
        return (True, "ok")

    verify = _load_verify(monkeypatch, _make_fake_module(fake_verify))
    with pytest.raises(ValueError):
        verify.verify_share(b"\x00" * 75, PROOF_B64, 0x1E01FFFF)


def test_verify_share_proof_parsed_from_b64(monkeypatch):
    seen = {}

    def fake_verify(header, proof, nbits):
        seen["tag"] = proof.tag
        return (True, "ok")

    verify = _load_verify(monkeypatch, _make_fake_module(fake_verify))
    verify.verify_share(HEADER_76, "QUJD", 0x1E01FFFF)
    assert seen["tag"] == "QUJD"


def test_meets_target_le_comparison(monkeypatch):
    verify = _load_verify(monkeypatch, _make_fake_module(None))
    # 0x01 little-endian = 1; clears target 1 (<=), fails target 0.
    h = (1).to_bytes(32, "little")
    assert verify.meets_target(h, 1) is True
    assert verify.meets_target(h, 0) is False


def test_meets_target_bad_length(monkeypatch):
    verify = _load_verify(monkeypatch, _make_fake_module(None))
    with pytest.raises(ValueError):
        verify.meets_target(b"\x00" * 31, 1)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
