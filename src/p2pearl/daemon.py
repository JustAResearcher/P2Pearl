"""P2Pearl daemon — wires the components into the mining loop.

Orchestration (target shape; the sidechain/P2P/stratum pieces are still stubs):

    1. Poll pearld getblocktemplate for the current parent tip.
    2. From the sidechain tip, compute PPLNS weights -> compute_pplns_payouts ->
       coinbase outputs (PPLNS P2TR payouts + OP_RETURN <next share id>).
    3. Build the incomplete header (parent nbits + coinbase merkle root) and push
       it to miners via stratum with the sidechain share_target.
    4. On a submitted share: verify (pow.verify), add to the sharechain, gossip.
    5. If the share also clears the parent block target: generate the ZK proof,
       assemble ZK_CERTIFICATE|HEADER|TXNS, submitblock, and gossip the block.

This entrypoint currently validates config and reports status; it does not yet run
the loop (see ROADMAP).
"""

from __future__ import annotations

import sys

from . import __version__, config


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = config.DaemonConfig()
    print(f"P2Pearl v{__version__} — decentralized zero-fee Pearl pool")
    print(f"  parent node     : {cfg.node.url}")
    print(f"  stratum         : {cfg.stratum_host}:{cfg.stratum_port}")
    print(f"  p2p             : {cfg.p2p_host}:{cfg.p2p_port}")
    print(f"  share time      : {config.SHARE_TARGET_TIME_SECONDS}s")
    print(f"  PPLNS window    : {config.PPLNS_WINDOW_SHARES} shares")
    print()
    print("Consensus math, coinbase builder, node RPC and proof wrappers are ready;")
    print("the sidechain engine, P2P layer and stratum front-end are not yet wired.")
    print("See ROADMAP.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
