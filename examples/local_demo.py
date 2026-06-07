"""P2Pearl local end-to-end demo — no pearld, no GPU, no pearl_mining needed.

Boots TWO real P2Pearl pool nodes (each = sharechain + daemon orchestrator + stratum
server + P2P gossip, all running as live asyncio services on loopback sockets),
connects them as peers, then drives simulated miners that authorize and submit shares
to node A over the real stratum protocol. You can watch a share flow through:

    miner --stratum--> nodeA: verify -> sidechain -> PPLNS -> P2P gossip --> nodeB

Proof verification and header/block assembly are faked (that part needs the native
pearl_mining + a real node), so this exercises the full ORCHESTRATION + networking,
not the cryptography. Run it:  python examples/local_demo.py

The demo itself lives in ``p2pearl.demo``; this is a thin runnable shim. You can also
run it via the installed CLI (``p2pearl demo``) or ``python -m p2pearl demo``.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from p2pearl import demo  # noqa: E402

if __name__ == "__main__":
    asyncio.run(demo.main())
