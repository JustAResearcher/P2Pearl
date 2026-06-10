"""P2Pearl command-line entry point.

Subcommands:
  gui     open the graphical control panel (settings form + start/stop + live log)
  demo    run the local end-to-end demo (no node/GPU/native build needed)
  daemon  wire and run a live pool node (needs pearld + pearl_mining + bitcoinutils)

A double-clicked executable opens the GUI (or runs the demo if no display).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__


def _double_clicked() -> bool:
    """True if this exe was launched by double-click (its console holds no shell),
    so we should pause before exit instead of letting the window vanish.
    A PyInstaller onefile exe runs as TWO console processes (bootloader parent +
    this child), so a double-clicked exe sees 2; a shell launch adds the shell on
    top (3+). A child spawned with CREATE_NO_WINDOW (e.g. by the GUI) also sees 2
    but has NO console window — nobody could press Enter there, so it must NOT
    count as a double-click (it would hang forever on the pause). Windows-only;
    any failure falls back to False (normal CLI)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        if not k32.GetConsoleWindow():
            return False        # hidden/windowless console: not interactive
        buf = (ctypes.c_uint * 16)()
        count = k32.GetConsoleProcessList(buf, 16)
        return count <= (2 if getattr(sys, "frozen", False) else 1)
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="p2pearl", description=__doc__)
    parser.add_argument("--version", action="version", version=f"p2pearl {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="open the graphical control panel (settings + start/stop + log)")
    sub.add_parser("demo", help="run the local end-to-end demo")
    dp = sub.add_parser("daemon", help="run a live pool node (serves a stratum port; needs pearld + pearl_mining)")
    dp.add_argument("--rpc-url", help="pearld JSON-RPC URL (default http://127.0.0.1:44107)")
    dp.add_argument("--rpc-user", help="pearld RPC user (or env P2PEARL_RPC_USER; default 'user')")
    dp.add_argument("--rpc-pass", help="pearld RPC password (or env P2PEARL_RPC_PASS; default 'pass')")
    dp.add_argument("--stratum-host", default="0.0.0.0", help="stratum bind host (default 0.0.0.0)")
    dp.add_argument("--stratum-port", type=int, default=3360, help="stratum port (default 3360)")
    dp.add_argument("--p2p-host", default="0.0.0.0", help="P2P bind host (default 0.0.0.0)")
    dp.add_argument("--p2p-port", type=int, default=37900, help="P2P port (default 37900)")
    dp.add_argument("--peer", action="append", metavar="HOST:PORT",
                    help="connect to a peer node for share gossip (repeatable)")
    dp.add_argument("--share-target", help="override the sidechain GENESIS bootstrap share target as int/hex "
                                           "(CONSENSUS: every node on the sidechain must use the same value; "
                                           "after genesis the target retargets automatically)")
    dp.add_argument("--pause-cmd", metavar="CMD",
                    help="shell command run just before the ZK prove of a found block "
                         "(e.g. pause a co-located CPU miner so the prove runs contention-free)")
    dp.add_argument("--resume-cmd", metavar="CMD",
                    help="shell command run right after the prove (undoes --pause-cmd)")

    args = parser.parse_args(argv)
    standalone = _double_clicked()
    ran_gui = False
    try:
        # A double-clicked exe arrives with no subcommand; open the control panel.
        if args.command == "gui" or (args.command is None and standalone):
            from . import gui
            code = gui.main()
            if code != gui.GUI_UNAVAILABLE:
                ran_gui = True          # the window was the interaction; don't pause after
                return code
            if args.command == "gui":
                return code             # explicitly asked for the GUI and there is none
            print("(no GUI available - running the demo instead)\n")
            from . import demo
            asyncio.run(demo.main())
            return 0
        if args.command == "demo":
            from . import demo
            asyncio.run(demo.main())
            return 0
        if args.command == "daemon":
            import os

            from . import config as cfgmod
            from . import daemon
            node_cfg = cfgmod.NodeRPCConfig(
                url=args.rpc_url or cfgmod.PARENT_RPC_DEFAULT_URL,
                user=args.rpc_user or os.environ.get("P2PEARL_RPC_USER", "user"),
                password=args.rpc_pass or os.environ.get("P2PEARL_RPC_PASS", "pass"),
            )
            peers = []
            for spec in (args.peer or []):
                host, sep, port = spec.rpartition(":")
                if sep and port:
                    peers.append((host, int(port)))
            dcfg = cfgmod.DaemonConfig(
                node=node_cfg, stratum_host=args.stratum_host, stratum_port=args.stratum_port,
                p2p_host=args.p2p_host, p2p_port=args.p2p_port, peers=tuple(peers))
            st = int(args.share_target, 0) if args.share_target else None
            return daemon.main(cfg=dcfg, share_target=st,
                               pause_cmd=args.pause_cmd, resume_cmd=args.resume_cmd)
        parser.print_help()
        return 0
    finally:
        # Keep a double-click console window readable — except after the GUI, whose
        # (hidden) console nobody can press Enter in.
        if standalone and not ran_gui:
            try:
                input("\nPress Enter to exit . . . ")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
