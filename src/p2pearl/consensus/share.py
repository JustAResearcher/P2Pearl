"""The P2Pearl sidechain block (a "share").

A *share* is a candidate Pearl block whose Pearlhash solution clears the easy
sidechain (share) target. It commits — via the parent coinbase's OP_RETURN — to
this ShareBlock's id, and its coinbase pays the PPLNS window (bound by
``payout_set_hash``). A share that *also* clears the parent block ``nbits`` is a
real Pearl block.

This module defines the canonical wire/serialization format of a ShareBlock and
its id. Serialization is little-endian fixed-width + Bitcoin-style varints, so
every peer derives an identical id. The id is ``double_sha256(serialize())``.

The bulky proof-of-work payload (the ~137-370 KB ``plain_proof`` or the ~60 KB ZK
certificate) is carried alongside a share on the wire and referenced here only by
``pow_hash`` (the little-endian ``hash_jackpot``); it is intentionally NOT part of
the id pre-image, so a share's identity is small and stable.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from hashlib import sha256

MAX_TARGET = (1 << 256) - 1
_HASH32 = 32


def double_sha256(data: bytes) -> bytes:
    """Bitcoin-style double SHA-256 (matches Pearl's blockchain_utils.double_sha256)."""
    return sha256(sha256(data).digest()).digest()


def _write_varint(n: int) -> bytes:
    if n < 0:
        raise ValueError("varint must be non-negative")
    if n < 0xFD:
        return bytes((n,))
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def _read_varint(data: bytes, off: int) -> tuple[int, int]:
    first = data[off]
    off += 1
    if first < 0xFD:
        return first, off
    if first == 0xFD:
        return struct.unpack_from("<H", data, off)[0], off + 2
    if first == 0xFE:
        return struct.unpack_from("<I", data, off)[0], off + 4
    return struct.unpack_from("<Q", data, off)[0], off + 8


@dataclass
class ShareBlock:
    """A sidechain block. All hash fields are 32 raw bytes."""

    version: int                     # u32  sidechain consensus version
    sidechain_height: int            # u64  height on the sharechain
    prev_share_id: bytes             # 32   parent share on the sharechain
    parent_prev_block: bytes         # 32   the Pearl tip this candidate builds on
    parent_height: int               # u32  Pearl block height being mined
    timestamp: int                   # u32  unix seconds
    share_target: int                # 256-bit sidechain (share) threshold
    block_nbits: int                 # u32  parent compact target carried in the header
    miner_address: str               # bech32m P2TR payout address of this share's finder
    payout_set_hash: bytes           # 32   binds the deterministic PPLNS coinbase output set
    uncle_ids: list[bytes] = field(default_factory=list)  # each 32; GHOST uncles referenced by this share
    # NON-committed PoW evidence: set AFTER mining; excluded from serialize()/share_id so the
    # parent coinbase OP_RETURN can commit to share_id BEFORE the solution exists. Carried
    # alongside the share on the wire for relay; recomputable from the proof.
    pow_hash: bytes = b"\x00" * 32   # 32   hash_jackpot (little-endian) of the winning solution

    def __post_init__(self) -> None:
        for name in ("prev_share_id", "parent_prev_block", "payout_set_hash", "pow_hash"):
            v = getattr(self, name)
            if len(v) != _HASH32:
                raise ValueError(f"{name} must be {_HASH32} bytes, got {len(v)}")
        for u in self.uncle_ids:
            if len(u) != _HASH32:
                raise ValueError("each uncle id must be 32 bytes")
        if not (0 < self.share_target <= MAX_TARGET):
            raise ValueError("share_target out of range")

    def serialize(self) -> bytes:
        out = bytearray()
        out += struct.pack("<I", self.version)
        out += struct.pack("<Q", self.sidechain_height)
        out += self.prev_share_id
        out += self.parent_prev_block
        out += struct.pack("<I", self.parent_height)
        out += struct.pack("<I", self.timestamp)
        out += self.share_target.to_bytes(32, "big")
        out += struct.pack("<I", self.block_nbits)
        addr = self.miner_address.encode("ascii")
        out += _write_varint(len(addr)) + addr
        out += self.payout_set_hash
        out += _write_varint(len(self.uncle_ids))
        for u in self.uncle_ids:
            out += u
        return bytes(out)

    @classmethod
    def deserialize(cls, data: bytes) -> "ShareBlock":
        off = 0
        version = struct.unpack_from("<I", data, off)[0]; off += 4
        sidechain_height = struct.unpack_from("<Q", data, off)[0]; off += 8
        prev_share_id = data[off:off + 32]; off += 32
        parent_prev_block = data[off:off + 32]; off += 32
        parent_height = struct.unpack_from("<I", data, off)[0]; off += 4
        timestamp = struct.unpack_from("<I", data, off)[0]; off += 4
        share_target = int.from_bytes(data[off:off + 32], "big"); off += 32
        block_nbits = struct.unpack_from("<I", data, off)[0]; off += 4
        addr_len, off = _read_varint(data, off)
        miner_address = data[off:off + addr_len].decode("ascii"); off += addr_len
        payout_set_hash = data[off:off + 32]; off += 32
        n_uncles, off = _read_varint(data, off)
        uncle_ids = []
        for _ in range(n_uncles):
            uncle_ids.append(data[off:off + 32]); off += 32
        return cls(
            version=version,
            sidechain_height=sidechain_height,
            prev_share_id=prev_share_id,
            parent_prev_block=parent_prev_block,
            parent_height=parent_height,
            timestamp=timestamp,
            share_target=share_target,
            block_nbits=block_nbits,
            miner_address=miner_address,
            payout_set_hash=payout_set_hash,
            uncle_ids=uncle_ids,
        )

    def share_id(self) -> bytes:
        """32-byte canonical id; this is what the parent coinbase OP_RETURN commits to."""
        return double_sha256(self.serialize())

    def difficulty(self) -> int:
        """Sidechain work represented by this share (= MAX_TARGET // share_target)."""
        return MAX_TARGET // self.share_target
