"""Tests for the P2P gossip layer.

Each scenario runs its own event loop (asyncio.run) and connects real P2PNode
instances over loopback sockets. ``verify_incoming`` is faked, so no node /
pearl_mining is needed; the sharechain and gossip protocol are exercised for real.
"""

import asyncio
import base64
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2pearl.config import BOOTSTRAP_SHARE_TARGET, SIDECHAIN_VERSION  # noqa: E402
from p2pearl.consensus.share import ShareBlock  # noqa: E402
from p2pearl.consensus.sharechain import GENESIS_PREV, Sharechain  # noqa: E402
from p2pearl.consensus.subsidy import block_subsidy  # noqa: E402
from p2pearl.p2p.node import P2PNode  # noqa: E402

PROOF_B64 = base64.b64encode(b"plain-proof-bytes").decode()


def _share(prev, height, miner="prl1pqqqqminer", target=BOOTSTRAP_SHARE_TARGET):
    # 10s spacing == SHARE_TARGET_TIME, so the consensus retarget holds the
    # bootstrap target at every height and the helper can stamp it directly.
    return ShareBlock(
        version=SIDECHAIN_VERSION,
        sidechain_height=height,
        prev_share_id=prev,
        parent_prev_block=b"\x22" * 32,
        parent_height=1,
        timestamp=1000 + 10 * height,
        share_target=target,
        block_nbits=0x1E01FFFF,
        coinbase_version=0x20000000,
        coinbase_value=block_subsidy(1),
        miner_address=miner,
        payout_set_hash=b"\x33" * 32,
    )


def _node(*, verify=True, on_block=None):
    return P2PNode(
        sharechain=Sharechain(window=100),
        verify_incoming=(lambda s, p: verify),
        host="127.0.0.1",
        port=0,
        on_block=on_block,
    )


async def _wait_until(pred, timeout=5.0):
    for _ in range(max(1, int(timeout / 0.02))):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


# --------------------------------------------------------------------------- #

def test_share_propagates_with_on_demand_proof():
    asyncio.run(_propagate())


async def _propagate():
    a, b = _node(), _node()
    await a.start()
    await b.start()
    try:
        await b.connect("127.0.0.1", a.port)
        assert await _wait_until(lambda: a.peer_count >= 1 and b.peer_count >= 1)
        g = _share(GENESIS_PREV, 0)
        await a.broadcast_share(g, PROOF_B64)
        assert await _wait_until(lambda: len(b.sharechain) >= 1)
        assert b.sharechain.tip().share_id() == g.share_id()
        # b fetched and stored the proof, so it can serve it onward
        assert b._proofs.get(g.share_id().hex()) == PROOF_B64
    finally:
        await a.stop()
        await b.stop()


def test_invalid_proof_is_dropped():
    asyncio.run(_invalid())


async def _invalid():
    a, b = _node(), _node(verify=False)   # b rejects every proof
    await a.start()
    await b.start()
    try:
        await b.connect("127.0.0.1", a.port)
        assert await _wait_until(lambda: a.peer_count >= 1)
        await a.broadcast_share(_share(GENESIS_PREV, 0), PROOF_B64)
        await asyncio.sleep(0.3)              # allow the round-trips to complete
        assert len(b.sharechain) == 0         # invalid proof -> not added
    finally:
        await a.stop()
        await b.stop()


def test_relay_three_nodes():
    asyncio.run(_relay())


async def _relay():
    a, b, c = _node(), _node(), _node()
    for n in (a, b, c):
        await n.start()
    try:
        await b.connect("127.0.0.1", a.port)
        await c.connect("127.0.0.1", b.port)
        assert await _wait_until(lambda: b.peer_count >= 2 and a.peer_count >= 1 and c.peer_count >= 1)
        g = _share(GENESIS_PREV, 0)
        await a.broadcast_share(g, PROOF_B64)
        # a -> b (verify, add, relay) -> c (fetch from b, verify, add)
        assert await _wait_until(lambda: len(c.sharechain) >= 1, timeout=8.0)
        assert c.sharechain.tip().share_id() == g.share_id()
    finally:
        for n in (a, b, c):
            await n.stop()


def test_window_sync_on_join():
    asyncio.run(_sync())


async def _sync():
    a = _node()
    g = _share(GENESIS_PREV, 0)
    child = _share(g.share_id(), 1)
    a.sharechain.add_share(g, verified=True)
    a._store_proof(g.share_id().hex(), PROOF_B64)
    a.sharechain.add_share(child, verified=True)
    a._store_proof(child.share_id().hex(), PROOF_B64)

    c = _node()
    await a.start()
    await c.start()
    try:
        await c.connect("127.0.0.1", a.port)   # c is empty -> requests the window on hello
        assert await _wait_until(lambda: len(c.sharechain) >= 2, timeout=8.0)
        assert c.sharechain.height() == 1
        assert c.sharechain.tip().share_id() == child.share_id()
    finally:
        await a.stop()
        await c.stop()


def test_window_sync_chunked_over_batch():
    asyncio.run(_sync_chunked())


async def _sync_chunked():
    # A window larger than SYNC_BATCH must sync via MULTIPLE 'shares' messages (a single
    # JSON line bundling every proof would exceed READ_LIMIT). All shares still arrive,
    # oldest-first, so a joining node rebuilds the full chain.
    a = _node()
    prev, shares = GENESIS_PREV, []
    for h in range(20):                          # > SYNC_BATCH (8) -> 3 messages
        s = _share(prev, h)
        a.sharechain.add_share(s, verified=True)
        a._store_proof(s.share_id().hex(), PROOF_B64)
        shares.append(s)
        prev = s.share_id()

    c = _node()
    await a.start()
    await c.start()
    try:
        await c.connect("127.0.0.1", a.port)
        assert await _wait_until(lambda: len(c.sharechain) >= 20, timeout=8.0)
        assert c.sharechain.height() == 19
        assert c.sharechain.tip().share_id() == shares[-1].share_id()
    finally:
        await a.stop()
        await c.stop()


def test_block_relay():
    asyncio.run(_block())


async def _block():
    got = []

    async def on_block(block_hex):
        got.append(block_hex)

    a = _node()
    b = _node(on_block=on_block)
    await a.start()
    await b.start()
    try:
        await b.connect("127.0.0.1", a.port)
        assert await _wait_until(lambda: a.peer_count >= 1)
        await a.broadcast_block("00aabbccdd")
        assert await _wait_until(lambda: got == ["00aabbccdd"])
    finally:
        await a.stop()
        await b.stop()


def test_on_block_candidate_fires_for_gossiped_share():
    asyncio.run(_block_candidate())


async def _block_candidate():
    # A verified LIVE gossiped share fires on_block_candidate on the receiver, with the
    # share + its proof — the hook the daemon uses to race-submit a peer's found block.
    seen = []

    async def on_cand(share, proof_b64):
        seen.append((share.share_id(), proof_b64))

    a = _node()
    b = P2PNode(sharechain=Sharechain(window=100), verify_incoming=(lambda s, p: True),
                host="127.0.0.1", port=0, on_block_candidate=on_cand)
    await a.start()
    await b.start()
    try:
        await b.connect("127.0.0.1", a.port)
        assert await _wait_until(lambda: a.peer_count >= 1 and b.peer_count >= 1)
        g = _share(GENESIS_PREV, 0)
        await a.broadcast_share(g, PROOF_B64)
        assert await _wait_until(lambda: len(seen) >= 1)
        assert seen[0] == (g.share_id(), PROOF_B64)
    finally:
        await a.stop()
        await b.stop()


def test_duplicate_announce_added_once():
    asyncio.run(_dedupe())


async def _dedupe():
    a, b = _node(), _node()
    await a.start()
    await b.start()
    try:
        await b.connect("127.0.0.1", a.port)
        assert await _wait_until(lambda: a.peer_count >= 1)
        g = _share(GENESIS_PREV, 0)
        await a.broadcast_share(g, PROOF_B64)
        await a.broadcast_share(g, PROOF_B64)   # announced twice
        assert await _wait_until(lambda: len(b.sharechain) >= 1)
        await asyncio.sleep(0.2)
        assert len(b.sharechain) == 1           # deduped
    finally:
        await a.stop()
        await b.stop()


# --------------------------------------------------------------------------- #
# DoS hardening (mirrors the P2Pool June-2026 P2P-server fixes)
# --------------------------------------------------------------------------- #

def test_block_relay_deduped_no_storm():
    asyncio.run(_block_dedup())


async def _block_dedup():
    # A 3-node cycle a->b->c->a must NOT loop a block forever: each node relays a
    # given block at most once (LRU of seen block hashes).
    got = []

    async def on_block(h):
        got.append(h)

    a, b, c = _node(), _node(on_block=on_block), _node()
    for n in (a, b, c):
        await n.start()
    try:
        await b.connect("127.0.0.1", a.port)
        await c.connect("127.0.0.1", b.port)
        await a.connect("127.0.0.1", c.port)              # close the cycle
        assert await _wait_until(lambda: a.peer_count and b.peer_count and c.peer_count)
        await a.broadcast_block("00aabbccdd")
        await asyncio.sleep(0.4)
        assert got.count("00aabbccdd") == 1               # b saw it exactly once, no storm
    finally:
        for n in (a, b, c):
            await n.stop()


def test_block_oversize_and_junk_rejected():
    asyncio.run(_block_junk())


async def _block_junk():
    from p2pearl.p2p.node import MAX_BLOCK_HEX
    got = []

    async def on_block(h):
        got.append(h)

    a, b = _node(), _node(on_block=on_block)
    await a.start()
    await b.start()
    try:
        await b.connect("127.0.0.1", a.port)
        assert await _wait_until(lambda: a.peer_count >= 1)
        await a._broadcast({"t": "block", "block": "ab" * (MAX_BLOCK_HEX)}, exclude=None)  # too big
        await a._broadcast({"t": "block", "block": 12345}, exclude=None)                   # not a str
        await asyncio.sleep(0.3)
        assert got == []                                   # neither was accepted/relayed
    finally:
        await a.stop()
        await b.stop()


def test_rate_limit_drops_flooding_peer():
    asyncio.run(_rate_limit())


async def _rate_limit():
    # Hammering an expensive request kind past its window cap + strikes drops the peer.
    from p2pearl.p2p.node import HARD_RATE_STRIKES, RATE_LIMITS, P2PNode
    a = _node()
    await a.start()
    rb, wb = None, None
    try:
        rb, wb = await asyncio.open_connection("127.0.0.1", a.port)
        await asyncio.wait_for(rb.readline(), timeout=5)   # server hello
        cap, _window = RATE_LIMITS["getproof"]
        for _ in range(cap + HARD_RATE_STRIKES + 5):
            wb.write((json.dumps({"t": "getproof", "id": "00"}) + "\n").encode())
        await wb.drain()
        # the server drops the connection once strikes exceed the threshold
        assert await _wait_until(lambda: a.peer_count == 0, timeout=5.0)
    finally:
        if wb is not None:
            wb.close()
        await a.stop()


def test_rate_limit_allows_normal_use():
    from p2pearl.p2p.node import _Peer
    p = _Peer(None, None, 1)
    # A handful of getshares in the window are fine (well under the cap).
    assert all(p.allow("getshares", now=100.0 + i * 0.01) for i in range(5))
    # A fresh window resets the count.
    assert p.allow("getshares", now=200.0)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
