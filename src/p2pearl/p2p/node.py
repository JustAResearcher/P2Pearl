"""P2P gossip layer for the P2Pearl sidechain (ROADMAP M5).

Modeled on P2Pool's broadcast protocol, adapted to Pearl's large share payloads:
gossip a compact SHARE_ANNOUNCE (the serialized ShareBlock, ~hundreds of bytes)
first; a peer that doesn't have the share fetches the bulky proof (~60-370 KB) on
demand (GET_PROOF/PROOF), so a proof is never re-downloaded and the broadcast path
stays small. Only sharechain-valid shares propagate (each clears pool difficulty) —
the O(N^2) guard that lets the sidechain scale.

DoS/Sybil hygiene: dedupe against the sharechain; drop shares whose parent is
unknown (request a window sync instead); VERIFY every proof before adding/relaying;
coarse per-connection flood cap; bound the on-demand proof cache.

Dependencies are injected, so the layer is unit-tested with fakes (no node, no
pearl_mining):
  * sharechain      - the shared Sharechain (dedupe / linkage / add / window walk).
  * verify_incoming - (share, proof_b64) -> bool: reconstruct the header and verify
                      the PoW at the share target. In production this reuses the
                      daemon's header reconstruction (the "deterministic template
                      reconstruction" the blueprint flags); in tests it is a fake.

Wire-up: the daemon passes ``P2PNode.broadcast_share`` / ``broadcast_block`` as its
``broadcast_*`` hooks; ``on_new_share`` is wired to ``stratum.refresh`` so a peer's
share advances every miner's job.

Messages (newline-delimited JSON, ``t`` = type):
  hello {v,port,tip,height}      handshake + tip exchange
  peers {peers:[[host,port],..]} peer exchange
  share {share:<hex>}            SHARE_ANNOUNCE (no proof)
  getproof {id:<hex>}            request a proof
  proof {id:<hex>,proof:<b64>}   the bulky proof, on demand
  getshares {from:<height>}      request the window on join
  shares {shares:[[<hex>,<b64>]]} window sync (shares WITH proofs, oldest first)
  block {block:<hex>}            a found Pearl block (relayed)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from ..consensus.share import ShareBlock
from ..consensus.sharechain import GENESIS_PREV, Sharechain

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
READ_LIMIT = 2 ** 22          # proof / shares messages carry 60-370 KB base64 payloads
MAX_MSGS_PER_CONN = 200_000   # coarse flood cap before dropping a peer
MAX_STORED_PROOFS = 4096      # bound the on-demand proof cache
SYNC_LIMIT = 2048             # max shares per window sync
SYNC_BATCH = 8                # shares per 'shares' message — each proof is ~137-370 KB,
                              # so bundling the whole window in one line would exceed READ_LIMIT
MAX_BLOCK_HEX = 2_000_000     # cap a relayed 'block' payload (a real Pearl block is ~120 KB hex)
MAX_BLOCKS_SEEN = 512         # bounded LRU of recently-relayed block hashes (storm guard)
# Per-peer rate caps on the EXPENSIVE request handlers (those that send data back —
# the amplification vectors). Fixed window: (max served per window, window seconds).
# A legit peer needs a handful; an attacker is throttled + dropped past the hard cap.
RATE_LIMITS = {
    "getshares": (30, 10.0),  # each is a full window walk + up to SYNC_LIMIT proof sends
    "getproof": (2000, 10.0),  # each sends one 137-370 KB proof (also drain-backpressured)
    "block": (200, 10.0),     # relayed broadcasts
    "hello": (10, 10.0),
}
HARD_RATE_STRIKES = 5         # consecutive over-limit hits on one kind -> drop the peer

VerifyIncoming = Callable[[ShareBlock, str], bool]
OnNewShare = Callable[[ShareBlock], Awaitable[None]]
OnBlock = Callable[[str], Awaitable[None]]
OnBlockCandidate = Callable[[ShareBlock, str], Awaitable[None]]   # a verified live share -> maybe submit its block


def _encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


class _Peer:
    def __init__(self, reader, writer, peer_id: int) -> None:
        self.reader = reader
        self.writer = writer
        self.peer_id = peer_id
        self.listen_addr: tuple[str, int] | None = None
        self._send_lock = asyncio.Lock()
        self._buckets: dict[str, list] = {}   # kind -> [window_start, count]
        self.strikes = 0                      # over-limit hits (any kind), in a row

    async def send(self, msg: dict) -> None:
        async with self._send_lock:
            self.writer.write(_encode(msg))
            await self.writer.drain()

    def allow(self, kind: str, now: float) -> bool:
        """Fixed-window rate gate for an expensive request kind; True iff under cap."""
        limit, window = RATE_LIMITS[kind]
        b = self._buckets.get(kind)
        if b is None or now - b[0] >= window:
            self._buckets[kind] = [now, 1]
            self.strikes = 0
            return True
        if b[1] >= limit:
            self.strikes += 1
            return False
        b[1] += 1
        self.strikes = 0
        return True


class P2PNode:
    def __init__(
        self,
        *,
        sharechain: Sharechain,
        verify_incoming: VerifyIncoming,
        host: str = "0.0.0.0",
        port: int = 37900,
        on_new_share: OnNewShare | None = None,
        on_block: OnBlock | None = None,
        on_block_candidate: OnBlockCandidate | None = None,
    ) -> None:
        self.sharechain = sharechain
        self._verify = verify_incoming
        self.host = host
        self.port = port
        self._on_new_share = on_new_share
        self._on_block = on_block
        self._on_block_candidate = on_block_candidate
        self._peers: set[_Peer] = set()
        self._known_addrs: set[tuple[str, int]] = set()
        self._proofs: "OrderedDict[str, str]" = OrderedDict()   # share_id hex -> proof b64
        self._pending: dict[str, ShareBlock] = {}               # announced, awaiting proof
        self._blocks_seen: "OrderedDict[bytes, bool]" = OrderedDict()  # relayed-block LRU (storm guard)
        self._server: asyncio.AbstractServer | None = None
        self._peer_seq = 0
        self._tasks: set[asyncio.Task] = set()

    # --- lifecycle ---------------------------------------------------------- #
    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_inbound, host=self.host, port=self.port, limit=READ_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Close connections + cancel tasks FIRST, then the listener. Bound
        # wait_closed so a still-draining transport can't hang shutdown.
        for task in list(self._tasks):
            task.cancel()
        for peer in list(self._peers):
            try:
                peer.writer.close()
            except Exception:
                pass
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
            except Exception:
                pass

    @property
    def peer_count(self) -> int:
        return len(self._peers)

    async def connect(self, host: str, port: int) -> None:
        reader, writer = await asyncio.open_connection(host, port, limit=READ_LIMIT)
        peer = self._register(reader, writer)
        await self._send_hello(peer)
        task = asyncio.ensure_future(self._run_peer(peer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- broadcast (the PoolNode hooks) ------------------------------------- #
    async def broadcast_share(self, share: ShareBlock, proof_b64: str) -> None:
        self._store_proof(share.share_id().hex(), proof_b64)
        await self._announce(share, exclude=None)

    async def broadcast_block(self, block_hex: str) -> None:
        self._seen_block(block_hex)                  # our own block: don't relay it back to us
        await self._broadcast({"t": "block", "block": block_hex}, exclude=None)

    # --- connection handling ------------------------------------------------ #
    def _register(self, reader, writer) -> _Peer:
        self._peer_seq += 1
        peer = _Peer(reader, writer, self._peer_seq)
        self._peers.add(peer)
        return peer

    async def _on_inbound(self, reader, writer) -> None:
        peer = self._register(reader, writer)
        await self._send_hello(peer)
        await self._run_peer(peer)

    async def _run_peer(self, peer: _Peer) -> None:
        count = 0
        try:
            while True:
                line = await peer.reader.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                count += 1
                if count > MAX_MSGS_PER_CONN:
                    _LOGGER.warning("peer %d flooding; dropping", peer.peer_id)
                    break
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if not await self._dispatch(peer, msg):
                    break
        except (ConnectionError, OSError):
            pass
        except ValueError:        # readline LimitOverrunError: oversized line -> drop peer
            _LOGGER.warning("peer %d sent an oversized message; dropping", peer.peer_id)
        finally:
            self._peers.discard(peer)
            try:
                peer.writer.close()
            except Exception:
                pass

    async def _send_hello(self, peer: _Peer) -> None:
        tip = self.sharechain.best_tip_id()
        await peer.send({
            "t": "hello", "v": PROTOCOL_VERSION, "port": self.port,
            "tip": tip.hex() if tip else "", "height": self.sharechain.height(),
        })

    async def _dispatch(self, peer: _Peer, msg: dict) -> bool:
        """Handle one message. Returns False if the peer should be dropped."""
        kind = msg.get("t")
        handler = _HANDLERS.get(kind)
        if handler is None:
            return True
        if kind in RATE_LIMITS and not peer.allow(kind, time.monotonic()):
            if peer.strikes >= HARD_RATE_STRIKES:
                _LOGGER.warning("peer %d kept flooding %r; dropping", peer.peer_id, kind)
                return False
            return True                              # over the window cap: ignore this one
        try:
            await handler(self, peer, msg)
        except Exception:
            _LOGGER.exception("p2p handler %s failed", kind)
        return True

    # --- message handlers --------------------------------------------------- #
    async def _h_hello(self, peer: _Peer, msg: dict) -> None:
        port = msg.get("port")
        if isinstance(port, int):
            host = peer.writer.get_extra_info("peername", ("", 0))[0]
            peer.listen_addr = (host, port)
            self._known_addrs.add((host, port))
        await peer.send({"t": "peers", "peers": [list(a) for a in list(self._known_addrs)[:32]]})
        if int(msg.get("height", -1)) > self.sharechain.height():
            await peer.send({"t": "getshares", "from": self.sharechain.height() + 1})

    async def _h_peers(self, peer: _Peer, msg: dict) -> None:
        for a in msg.get("peers", []):
            if isinstance(a, list) and len(a) == 2:
                self._known_addrs.add((a[0], int(a[1])))

    async def _h_share(self, peer: _Peer, msg: dict) -> None:
        try:
            share = ShareBlock.deserialize(bytes.fromhex(msg["share"]))
        except Exception:
            return
        sid_bytes = share.share_id()
        sid = sid_bytes.hex()
        if sid_bytes in self.sharechain or sid in self._pending:
            return                                  # dedupe
        if share.prev_share_id != GENESIS_PREV and share.prev_share_id not in self.sharechain:
            await peer.send({"t": "getshares", "from": max(0, share.sidechain_height - 1)})
            return                                  # orphan: sync instead
        if not self.sharechain.is_valid_successor(share):
            return                                  # consensus-invalid (target/subsidy/linkage):
                                                    # don't fetch + verify its 137-370 KB proof
        self._pending[sid] = share
        await peer.send({"t": "getproof", "id": sid})

    async def _h_getproof(self, peer: _Peer, msg: dict) -> None:
        proof = self._proofs.get(msg.get("id", ""))
        if proof is not None:
            await peer.send({"t": "proof", "id": msg["id"], "proof": proof})

    async def _h_proof(self, peer: _Peer, msg: dict) -> None:
        sid = msg.get("id", "")
        share = self._pending.pop(sid, None)
        if share is None:
            return
        proof_b64 = msg.get("proof", "")
        if not self._verify(share, proof_b64):
            _LOGGER.warning("peer %d sent an invalid proof for %s", peer.peer_id, sid[:12])
            return
        if not self.sharechain.add_share(share, verified=True, proof=proof_b64.encode("ascii")).accepted:
            return
        self._store_proof(sid, proof_b64)
        if self._on_new_share is not None:
            await self._on_new_share(share)
        await self._announce(share, exclude=peer)   # relay onward
        if self._on_block_candidate is not None:
            # If this LIVE share also clears the block target, race to submit its block.
            # Fire-and-forget: the prove takes seconds and must not stall this peer's read
            # loop or delay relaying the share to other peers. (Window-sync shares are stale,
            # so only the live path triggers this.)
            t = asyncio.ensure_future(self._on_block_candidate(share, proof_b64))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _h_getshares(self, peer: _Peer, msg: dict) -> None:
        # Stream the window oldest-first in BATCHES. Bundling every share+proof into one
        # JSON line can exceed READ_LIMIT (proofs are ~137-370 KB), which would break a
        # peer joining a populated pool; several smaller messages stay under the limit and
        # keep parents ahead of children (``_h_shares`` adds each only if its parent exists).
        batch: list = []
        for share in self._window_shares(int(msg.get("from", 0))):
            proof = self._proofs.get(share.share_id().hex())
            if proof is None:                        # only serve shares we can prove
                continue
            batch.append([share.serialize().hex(), proof])
            if len(batch) >= SYNC_BATCH:
                await peer.send({"t": "shares", "shares": batch})
                batch = []
        if batch:
            await peer.send({"t": "shares", "shares": batch})

    async def _h_shares(self, peer: _Peer, msg: dict) -> None:
        # Window sync carries proofs inline and is ordered oldest-first, so each
        # parent is added before its children (the live announce path can't).
        for pair in msg.get("shares", [])[:SYNC_LIMIT]:
            if not (isinstance(pair, list) and len(pair) == 2):
                continue
            try:
                share = ShareBlock.deserialize(bytes.fromhex(pair[0]))
            except Exception:
                continue
            sid_bytes = share.share_id()
            if sid_bytes in self.sharechain:
                continue
            if share.prev_share_id != GENESIS_PREV and share.prev_share_id not in self.sharechain:
                continue                             # gap; the live path will fill it
            if not self.sharechain.is_valid_successor(share):
                continue                             # consensus-invalid: skip the proof verify
            if not self._verify(share, pair[1]):
                continue
            if self.sharechain.add_share(share, verified=True, proof=pair[1].encode("ascii")).accepted:
                self._store_proof(sid_bytes.hex(), pair[1])

    async def _h_block(self, peer: _Peer, msg: dict) -> None:
        block_hex = msg.get("block")
        # Reject junk early; cap the size so a peer can't fan out huge payloads.
        if not isinstance(block_hex, str) or not (0 < len(block_hex) <= MAX_BLOCK_HEX):
            return
        if self._seen_block(block_hex):
            return                                   # already relayed: stops a broadcast storm
        if self._on_block is not None:
            await self._on_block(block_hex)
        await self._broadcast({"t": "block", "block": block_hex}, exclude=peer)

    # --- helpers ------------------------------------------------------------ #
    def _store_proof(self, sid: str, proof_b64: str) -> None:
        self._proofs[sid] = proof_b64
        self._proofs.move_to_end(sid)
        while len(self._proofs) > MAX_STORED_PROOFS:
            self._proofs.popitem(last=False)

    def _seen_block(self, block_hex: str) -> bool:
        """Record a block by hash; return True if it was ALREADY seen (a repeat)."""
        h = hashlib.sha256(block_hex.encode("ascii", "ignore")).digest()
        if h in self._blocks_seen:
            self._blocks_seen.move_to_end(h)
            return True
        self._blocks_seen[h] = True
        while len(self._blocks_seen) > MAX_BLOCKS_SEEN:
            self._blocks_seen.popitem(last=False)
        return False

    def _window_shares(self, from_height: int) -> list[ShareBlock]:
        out: list[ShareBlock] = []
        cur = self.sharechain.best_tip_id()
        while cur and cur != GENESIS_PREV and len(out) < SYNC_LIMIT:
            share = self.sharechain.get(cur)
            if share is None or share.sidechain_height < from_height:
                break
            out.append(share)
            cur = share.prev_share_id
        out.reverse()                                # oldest first
        return out

    async def _announce(self, share: ShareBlock, exclude: _Peer | None) -> None:
        await self._broadcast({"t": "share", "share": share.serialize().hex()}, exclude=exclude)

    async def _broadcast(self, msg: dict, exclude: _Peer | None) -> None:
        for peer in list(self._peers):
            if peer is exclude:
                continue
            try:
                await peer.send(msg)
            except Exception:
                _LOGGER.exception("broadcast to peer %d failed", peer.peer_id)


_HANDLERS = {
    "hello": P2PNode._h_hello,
    "peers": P2PNode._h_peers,
    "share": P2PNode._h_share,
    "getproof": P2PNode._h_getproof,
    "proof": P2PNode._h_proof,
    "getshares": P2PNode._h_getshares,
    "shares": P2PNode._h_shares,
    "block": P2PNode._h_block,
}
