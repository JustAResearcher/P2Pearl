"""Tests for the sidechain engine (consensus/sharechain.py)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2pearl.consensus.pplns import uncle_weight  # noqa: E402
from p2pearl.consensus.share import ShareBlock  # noqa: E402
from p2pearl.consensus.sharechain import GENESIS_PREV, Sharechain  # noqa: E402

Z = b"\x00" * 32
T = 1 << 248  # base share target -> difficulty ~255


def mk(prev, height, *, miner="prlA", target=T, ts=1000, uncles=None, parent_height=1, version=1):
    return ShareBlock(
        version=version,
        sidechain_height=height,
        prev_share_id=prev,
        parent_prev_block=Z,
        parent_height=parent_height,
        timestamp=ts,
        share_target=target,
        block_nbits=0x1E01FFFF,
        miner_address=miner,
        payout_set_hash=Z,
        pow_hash=Z,
        uncle_ids=uncles or [],
    )


def add(sc, share, proof=None):
    return sc.add_share(share, verified=True, proof=proof)


def _genesis(sc):
    g = mk(GENESIS_PREV, 0, miner="prlG")
    assert add(sc, g).accepted
    return g


def test_genesis():
    sc = Sharechain(window=4)
    g = mk(GENESIS_PREV, 0)
    r = add(sc, g)
    assert r.accepted and r.is_best_tip
    assert sc.tip().share_id() == g.share_id()
    assert sc.height() == 0 and len(sc) == 1


def test_genesis_bad_height_rejected():
    sc = Sharechain(window=4)
    r = add(sc, mk(GENESIS_PREV, 7))
    assert not r.accepted and r.reason == "genesis height != 0"


def test_linear_chain_and_cumulative():
    sc = Sharechain(window=8)
    g = _genesis(sc)
    s1 = mk(g.share_id(), 1, miner="prlA")
    s2 = mk(s1.share_id(), 2, miner="prlB")
    assert add(sc, s1).is_best_tip
    assert add(sc, s2).is_best_tip
    assert sc.tip().share_id() == s2.share_id()
    assert sc.height() == 2
    # all three shares share the same difficulty (same target)
    d = g.difficulty()
    assert sc.cumulative_difficulty(s2.share_id()) == 3 * d


def test_duplicate_rejected():
    sc = Sharechain(window=4)
    g = _genesis(sc)
    r = add(sc, mk(GENESIS_PREV, 0, miner="prlG"))  # identical -> same id
    assert not r.accepted and r.reason == "duplicate"


def test_orphan_rejected():
    sc = Sharechain(window=4)
    _genesis(sc)
    r = add(sc, mk(b"\x09" * 32, 1))
    assert not r.accepted and r.reason == "orphan: unknown parent"


def test_bad_height_rejected():
    sc = Sharechain(window=4)
    g = _genesis(sc)
    r = add(sc, mk(g.share_id(), 5))
    assert not r.accepted and r.reason == "bad height"


def test_timestamp_regression_rejected():
    sc = Sharechain(window=4)
    g = mk(GENESIS_PREV, 0, ts=1000)
    add(sc, g)
    r = add(sc, mk(g.share_id(), 1, ts=999))
    assert not r.accepted and r.reason == "timestamp regression"


def test_parent_height_regression_rejected():
    sc = Sharechain(window=4)
    g = mk(GENESIS_PREV, 0, parent_height=10)
    add(sc, g)
    r = add(sc, mk(g.share_id(), 1, parent_height=9))
    assert not r.accepted and r.reason == "parent height regression"


def test_unverified_rejected():
    sc = Sharechain(window=4)
    r = sc.add_share(mk(GENESIS_PREV, 0), verified=False)
    assert not r.accepted and r.reason == "not verified"


def test_bad_version_rejected():
    sc = Sharechain(window=4)
    r = add(sc, mk(GENESIS_PREV, 0, version=99))
    assert not r.accepted and r.reason == "bad version"


def test_reorg_higher_cumulative_wins():
    sc = Sharechain(window=8)
    g = _genesis(sc)
    a1 = mk(g.share_id(), 1, miner="prlA", target=T)            # diff ~255
    assert add(sc, a1).is_best_tip
    b1 = mk(g.share_id(), 1, miner="prlB", target=(1 << 240))   # higher diff -> reorg
    r = add(sc, b1)
    assert r.accepted and r.is_best_tip
    assert sc.tip().share_id() == b1.share_id()


def test_competing_share_same_diff_keeps_first_tip():
    sc = Sharechain(window=8)
    g = _genesis(sc)
    a1 = mk(g.share_id(), 1, miner="prlA")
    b1 = mk(g.share_id(), 1, miner="prlB")  # same diff
    assert add(sc, a1).is_best_tip
    r = add(sc, b1)
    assert r.accepted and not r.is_best_tip       # accepted as a sibling, not tip
    assert sc.tip().share_id() == a1.share_id()


def test_uncle_inclusion_payout_and_weight():
    sc = Sharechain(window=8)
    g = _genesis(sc)                                    # miner prlG
    a1 = mk(g.share_id(), 1, miner="prlA")
    b1 = mk(g.share_id(), 1, miner="prlB")              # sibling of a1 (will be an uncle)
    add(sc, a1)
    add(sc, b1)
    c2 = mk(a1.share_id(), 2, miner="prlC", uncles=[b1.share_id()])
    r = add(sc, c2)
    assert r.accepted and r.is_best_tip
    # cumulative includes the uncle's FULL difficulty
    expected = sc.cumulative_difficulty(a1.share_id()) + c2.difficulty() + b1.difficulty()
    assert sc.cumulative_difficulty(c2.share_id()) == expected
    # PPLNS: uncle pays its miner a PENALIZED weight
    weights = dict(sc.pplns_weights())
    d = g.difficulty()
    assert weights["prlC"] == d and weights["prlA"] == d and weights["prlG"] == d
    assert weights["prlB"] == uncle_weight(d, sc.uncle_penalty_percent)


def test_uncle_unknown_rejected():
    sc = Sharechain(window=8)
    g = _genesis(sc)
    a1 = mk(g.share_id(), 1)
    add(sc, a1)
    r = add(sc, mk(a1.share_id(), 2, uncles=[b"\x07" * 32]))
    assert not r.accepted and r.reason == "unknown uncle"


def test_uncle_on_main_chain_rejected():
    sc = Sharechain(window=8)
    g = _genesis(sc)
    a1 = mk(g.share_id(), 1)
    add(sc, a1)
    r = add(sc, mk(a1.share_id(), 2, uncles=[a1.share_id()]))  # a1 is an ancestor
    assert not r.accepted and r.reason == "uncle on main chain"


def test_uncle_out_of_depth_rejected():
    sc = Sharechain(window=16, uncle_depth=3)
    g = _genesis(sc)
    # main chain g(0) -> s1 -> s2 -> s3 -> s4 -> s5
    prev = g
    chain = []
    for h in range(1, 6):
        s = mk(prev.share_id(), h, miner=f"prl{h}")
        add(sc, s)
        chain.append(s)
        prev = s
    # an old sibling at height 1 (off-chain), referenced from height 6 -> too deep
    old = mk(g.share_id(), 1, miner="prlOLD")
    add(sc, old)
    s6 = mk(chain[-1].share_id(), 6, uncles=[old.share_id()])
    r = add(sc, s6)
    assert not r.accepted and r.reason == "uncle out of depth"


def test_pplns_window_cap():
    sc = Sharechain(window=3)
    g = _genesis(sc)
    prev = g
    for h in range(1, 6):  # heights 1..5, distinct miners
        s = mk(prev.share_id(), h, miner=f"prl{h}")
        add(sc, s)
        prev = s
    weights = dict(sc.pplns_weights())
    # only the last 3 main-chain shares (heights 5,4,3) are in the window
    assert set(weights) == {"prl5", "prl4", "prl3"}


def test_pruning_bounds_storage():
    sc = Sharechain(window=3)  # retention = 2*3 + 3 = 9
    g = _genesis(sc)
    prev = g
    for h in range(1, 15):  # heights up to 14
        s = mk(prev.share_id(), h, miner=f"prl{h}")
        add(sc, s)
        prev = s
    # tip height 14, cutoff = 14 - 9 = 5 -> heights 0..4 dropped, 5..14 kept (10)
    assert sc.height() == 14
    assert len(sc) == 10


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
