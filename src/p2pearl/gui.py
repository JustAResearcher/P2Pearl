"""P2Pearl graphical control panel — put your settings in and go.

A single window (stdlib tkinter, nothing to install) that wraps `p2pearl daemon`:
fill in how to reach your pearld node, click **Start node**, and watch the live
log. Settings persist to ``~/.p2pearl/gui.json`` so the next launch is
open-and-click. A **Run demo** button shows the whole pipeline with no setup at
all. Double-clicking the packaged executable opens this GUI.

The daemon runs as a child process (this same executable with ``daemon`` args),
so Stop is instant and a daemon crash can never take the window down. tkinter is
imported lazily inside :func:`main` so headless machines can still import this
module (the CLI falls back to the demo when no GUI is possible).
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import __version__, config
from .config import NodeRPCConfig

SETTINGS_PATH = Path.home() / ".p2pearl" / "gui.json"
PEARLD_INSTALL_DIR = Path.home() / ".p2pearl" / "bin"
PEARLD_DATA_DIR = Path.home() / ".p2pearl" / "pearld-data"
MIN_MANAGED_PEARLD_VERSION = (1, 1, 0)  # Pearl MoE hard fork needs v2 certificates.
GUI_UNAVAILABLE = 2          # main() return code: tkinter/display missing — caller may fall back

# Networks the managed pearld can run on ("regtest" is for tests/dev, not the UI).
NETWORKS = {
    "mainnet": {"flag": None, "rpc_port": 44107},
    "testnet": {"flag": "--testnet", "rpc_port": 44109},
    "regtest": {"flag": "--regtest", "rpc_port": 44107},
}

DEFAULTS = {
    "rpc_url": "http://127.0.0.1:44107",
    "rpc_user": "user",
    "rpc_pass": "pass",
    "manage_pearld": "auto",     # "1"/"0"; "auto" = on iff a bundled pearld exists
    "network": "mainnet",        # managed pearld network
    "stratum_host": "0.0.0.0",
    "stratum_port": "3360",
    "p2p_host": "0.0.0.0",
    "p2p_port": "37900",
    "peers": "",                 # one host:port per line
    "wallet": "",                # only used to render the miner command
    "share_target": "",
    "pause_cmd": "",
    "resume_cmd": "",
}


# --------------------------------------------------------------------------- #
# Pure helpers (no tkinter — unit-tested headless)
# --------------------------------------------------------------------------- #

def load_settings(path: Path = SETTINGS_PATH) -> dict:
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        settings.update({k: str(v) for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass                     # first run / unreadable file -> defaults
    return settings


def save_settings(settings: dict, path: Path = SETTINGS_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass                     # persistence is best-effort, never fatal


def normalize_settings(settings: dict) -> dict:
    """Apply derived settings that should not be hand-edited in managed mode."""
    s = {**DEFAULTS, **settings}
    if s["network"] not in NETWORKS:
        s["network"] = DEFAULTS["network"]
    if s["manage_pearld"] == "1":
        s["rpc_url"] = managed_rpc_url(s["network"])
    return s


def build_daemon_args(settings: dict) -> list[str]:
    """Map the settings dict onto `p2pearl daemon` CLI arguments."""
    s = normalize_settings(settings)
    args = ["daemon", "--rpc-url", s["rpc_url"].strip()]
    if s["rpc_user"].strip():
        args += ["--rpc-user", s["rpc_user"].strip()]
    if s["rpc_pass"]:
        args += ["--rpc-pass", s["rpc_pass"]]
    args += ["--stratum-host", s["stratum_host"].strip() or "0.0.0.0",
             "--stratum-port", s["stratum_port"].strip() or "3360",
             "--p2p-host", s["p2p_host"].strip() or "0.0.0.0",
             "--p2p-port", s["p2p_port"].strip() or "37900"]
    for line in s["peers"].splitlines():
        peer = line.strip()
        if peer:
            args += ["--peer", peer]
    if s["share_target"].strip():
        args += ["--share-target", s["share_target"].strip()]
    if s["pause_cmd"].strip():
        args += ["--pause-cmd", s["pause_cmd"].strip()]
    if s["resume_cmd"].strip():
        args += ["--resume-cmd", s["resume_cmd"].strip()]
    return args


def miner_command(settings: dict) -> str:
    """The SRBMiner command a miner runs to mine on this node."""
    s = {**DEFAULTS, **settings}
    port = s["stratum_port"].strip() or "3360"
    wallet = s["wallet"].strip() or "<your-prl1p...-address>"
    return (f"SRBMiner-MULTI --algorithm pearlhash --pool <this-machine-ip>:{port} "
            f"--wallet {wallet} --disable-cpu")


def format_prl_amount(grains: int) -> str:
    return f"{int(grains) / config.GRAIN_PER_PEARL:.8f}".rstrip("0").rstrip(".") or "0"


def payout_stats_summary(snapshot: dict | None) -> str:
    if not snapshot or not snapshot.get("addresses"):
        return "No accepted shares yet."
    reward = format_prl_amount(snapshot.get("block_reward_grains", 0))
    return (f"PPLNS window: {snapshot.get('window_shares', 0)} / "
            f"{snapshot.get('window_max', 0)} shares; next block reward: {reward} PRL")


def payout_stats_rows(snapshot: dict | None, limit: int = 10) -> list[tuple[str, str, str, str]]:
    if not snapshot:
        return []
    rows = []
    min_payout = int(snapshot.get("min_payout_grains", 0))
    for row in snapshot.get("addresses", [])[:limit]:
        pct = int(row.get("percent_bps", 0)) / 100
        estimated = int(row.get("estimated_grains", 0))
        potential = int(row.get("potential_grains", 0))
        if estimated == 0 and potential > 0 and min_payout:
            payout = f"<{format_prl_amount(min_payout)} PRL"
        else:
            payout = f"{format_prl_amount(estimated)} PRL"
        rows.append((
            row.get("address", ""),
            f"{pct:.2f}%",
            payout,
            f"{int(row.get('weight', 0)):,}",
        ))
    return rows


def self_command() -> list[str]:
    """How to re-invoke this same program (frozen exe or `python -m p2pearl`)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "p2pearl"]


def bundled_pearld_dir() -> Path | None:
    """The pearld binaries packaged inside the frozen exe, if this build has them."""
    if getattr(sys, "frozen", False):
        d = Path(getattr(sys, "_MEIPASS", "")) / "pearld_bin"
        if (d / _pearld_name()).exists():
            return d
    return None


def _pearld_name() -> str:
    return "pearld.exe" if os.name == "nt" else "pearld"


def pearld_version(exe: Path) -> tuple[int, int, int] | None:
    """Return ``pearld --version`` as a tuple, or None if it cannot be read."""
    creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [str(exe), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=creationflags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"pearld version (\d+)\.(\d+)\.(\d+)", proc.stdout)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def format_version(version: tuple[int, int, int] | None) -> str:
    return "unknown" if version is None else ".".join(str(part) for part in version)


def managed_pearld_too_old(version: tuple[int, int, int] | None) -> bool:
    return version is not None and version < MIN_MANAGED_PEARLD_VERSION


def ensure_pearld_installed(source_dir: Path | None = None,
                            dest: Path = PEARLD_INSTALL_DIR) -> Path | None:
    """Make a persistent pearld available and return its path (or None).

    Copies the bundled binaries (pearld, prlctl, LICENSE) out of the onefile
    exe's temp dir into ``dest`` — the frozen exe's extraction dir vanishes on
    exit, so a long-running pearld must live somewhere durable. Re-copies when
    the bundled binary differs (a new release upgrades the install). Falls back
    to a pearld already on PATH for source/Linux installs.
    """
    exe = dest / _pearld_name()
    src = source_dir if source_dir is not None else bundled_pearld_dir()
    if src is not None and (src / _pearld_name()).exists():
        bundled = src / _pearld_name()
        if not exe.exists() or exe.stat().st_size != bundled.stat().st_size:
            dest.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                if f.is_file():
                    shutil.copy2(f, dest / f.name)
    if exe.exists():
        return exe
    on_path = shutil.which("pearld")
    return Path(on_path) if on_path else None


def pearld_args(exe: Path, settings: dict, datadir: Path = PEARLD_DATA_DIR) -> list[str]:
    """The command line for the MANAGED pearld, derived from the GUI settings."""
    s = {**DEFAULTS, **settings}
    net = NETWORKS[s["network"]]
    args = [
        str(exe), "--notls",
        f"--rpcuser={s['rpc_user'].strip() or 'user'}",
        f"--rpcpass={s['rpc_pass'] or 'pass'}",
        f"--rpclisten=127.0.0.1:{net['rpc_port']}",
        f"--datadir={datadir}",
        f"--logdir={datadir / 'logs'}",
        # Outbound-only: a MANAGED node must never die on a chain-P2P port clash
        # (e.g. another pearld on the same box); outbound peers fully sync it.
        "--nolisten",
    ]
    if net["flag"]:
        args.append(net["flag"])
    return args


def managed_rpc_url(network: str) -> str:
    return f"http://127.0.0.1:{NETWORKS[network]['rpc_port']}"


def watch_pearld_sync(settings: dict, stop_event: "threading.Event", post) -> None:
    """Poll the managed pearld until it can serve work, reporting progress.

    ``post(event_tuple)`` receives ("pearld_status", text) while starting/syncing
    and a final ("pearld_ready", height) once getblocktemplate succeeds (pearld
    only serves templates when fully synced — exactly what the pool needs).
    """
    from .chain.node_rpc import NodeRPC, NodeRPCError
    cfg = NodeRPCConfig(url=settings["rpc_url"].strip(),
                        user=settings["rpc_user"].strip(),
                        password=settings["rpc_pass"])
    rpc = NodeRPC(cfg)
    while not stop_event.is_set():
        try:
            info = rpc._call("getblockchaininfo", [], timeout=5)
            try:
                rpc.get_block_template()
                post(("pearld_ready", info.get("blocks", 0)))
                return
            except NodeRPCError:
                post(("pearld_status",
                      f"pearld syncing — height {info.get('blocks', 0):,} "
                      f"(headers {info.get('headers', 0):,})"))
        except Exception:
            post(("pearld_status", "pearld starting ..."))
        stop_event.wait(3)


def stop_pearld_gracefully(proc: "subprocess.Popen", settings: dict | None = None) -> None:
    """Clean shutdown so the database flushes (a hard kill can lose recent blocks).

    Uses pearld's own ``stop`` RPC — console control events cannot reach a
    windowless Windows child, but the RPC works everywhere. Escalates to
    terminate/kill only if the RPC route fails.
    """
    if proc.poll() is not None:
        return
    if settings is not None:
        try:
            from .chain.node_rpc import NodeRPC
            cfg = NodeRPCConfig(url=settings["rpc_url"].strip(),
                                user=settings["rpc_user"].strip(),
                                password=settings["rpc_pass"])
            NodeRPC(cfg)._call("stop", [], timeout=5)
        except Exception:
            pass
        try:
            proc.wait(timeout=25)
            return
        except subprocess.TimeoutExpired:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def test_rpc(settings: dict) -> tuple[bool, str]:
    """One quick getblocktemplate against pearld; returns (ok, human message)."""
    from .chain.node_rpc import NodeRPC, NodeRPCError
    cfg = NodeRPCConfig(url=settings["rpc_url"].strip(),
                        user=settings["rpc_user"].strip(),
                        password=settings["rpc_pass"])
    try:
        gbt = NodeRPC(cfg).get_block_template()
        return True, f"pearld OK — tip height {gbt.get('height', '?')}"
    except NodeRPCError as exc:
        msg = str(exc)
        if "cannot reach" in msg:
            return False, "pearld unreachable — is it running? Check the RPC URL."
        return False, f"pearld reachable, but: {msg[:160]}"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"test failed: {exc}"


def _hide_own_console() -> None:
    """Hide the console window behind the GUI when the exe was double-clicked
    (the console holds only the PyInstaller bootloader + us). No-op elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        buf = (ctypes.c_uint * 16)()
        if k32.GetConsoleProcessList(buf, 16) <= 2:
            hwnd = k32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
    except Exception:
        print("tkinter is not available — no GUI on this system. Use `p2pearl daemon` (see --help).")
        return GUI_UNAVAILABLE
    try:
        root = tk.Tk()
    except Exception as exc:                 # no display (headless box / SSH session)
        print(f"could not open a window ({exc}) — use `p2pearl daemon` (see --help).")
        return GUI_UNAVAILABLE
    _hide_own_console()

    root.title(f"P2Pearl v{__version__} — decentralized zero-fee Pearl mining pool")
    root.minsize(680, 560)

    settings = load_settings()
    events: "queue.Queue[tuple]" = queue.Queue()
    state = {
        "proc": None, "label": "",           # the running child (daemon or demo)
        "pearld_proc": None,                 # the managed pearld, if we started one
        "pearld_ready": False,
        "watch_stop": None,                  # threading.Event for the sync watcher
        "pending_node_start": False,         # start the node as soon as pearld is ready
    }

    # ---- settings form ---------------------------------------------------- #
    outer = ttk.Frame(root, padding=10)
    outer.pack(fill="both", expand=True)
    fields: dict[str, tk.Variable] = {}

    def add_entry(parent, row, col, label, key, show=None, width=24, hint=""):
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=2)
        var = tk.StringVar(value=settings[key])
        entry = ttk.Entry(parent, textvariable=var, width=width, show=show)
        entry.grid(row=row, column=col + 1, sticky="we", pady=2)
        if hint:
            ttk.Label(parent, text=hint, foreground="#888").grid(
                row=row, column=col + 2, sticky="w", padx=(8, 0))
        fields[key] = var
        return entry

    node_box = ttk.LabelFrame(outer, text=" Your Pearl node (pearld) ", padding=8)
    node_box.pack(fill="x")
    node_box.columnconfigure(1, weight=1)

    pearld_available = (bundled_pearld_dir() is not None
                        or (PEARLD_INSTALL_DIR / _pearld_name()).exists()
                        or shutil.which("pearld") is not None)
    manage_var = tk.BooleanVar(value=(settings["manage_pearld"] == "1"
                                      or (settings["manage_pearld"] == "auto" and pearld_available)))
    network_var = tk.StringVar(value=settings["network"] if settings["network"] in ("mainnet", "testnet")
                               else "mainnet")
    manage_row = ttk.Frame(node_box)
    manage_row.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
    ttk.Checkbutton(manage_row, text="Run pearld for me", variable=manage_var).pack(side="left")
    ttk.Combobox(manage_row, textvariable=network_var, values=("mainnet", "testnet"),
                 state="readonly", width=9).pack(side="left", padx=(8, 0))
    ttk.Label(manage_row, text="starts + syncs the bundled Pearl node, then starts the pool",
              foreground="#888").pack(side="left", padx=(8, 0))

    rpc_url_entry = add_entry(node_box, 1, 0, "RPC URL", "rpc_url", width=34,
                              hint="44107 mainnet / 44109 testnet convention")
    add_entry(node_box, 2, 0, "RPC user", "rpc_user")
    add_entry(node_box, 3, 0, "RPC password", "rpc_pass", show="•")

    def _sync_managed_url(*_):
        if manage_var.get():
            fields["rpc_url"].set(managed_rpc_url(network_var.get()))
            rpc_url_entry.configure(state="readonly")
        else:
            rpc_url_entry.configure(state="normal")

    manage_var.trace_add("write", _sync_managed_url)
    network_var.trace_add("write", _sync_managed_url)
    _sync_managed_url()

    pool_box = ttk.LabelFrame(outer, text=" Pool settings ", padding=8)
    pool_box.pack(fill="x", pady=(8, 0))
    pool_box.columnconfigure(1, weight=1)
    add_entry(pool_box, 0, 0, "Stratum port", "stratum_port", width=8,
              hint="miners connect here")
    add_entry(pool_box, 1, 0, "P2P port", "p2p_port", width=8,
              hint="other operators connect here")
    add_entry(pool_box, 2, 0, "Your wallet", "wallet", width=34,
              hint="optional — fills in the miner command below")
    ttk.Label(pool_box, text="Peers").grid(row=3, column=0, sticky="nw", pady=2)
    peers_text = tk.Text(pool_box, height=2, width=34)
    peers_text.insert("1.0", settings["peers"])
    peers_text.grid(row=3, column=1, sticky="we", pady=2)
    ttk.Label(pool_box, text="one host:port per line; empty = solo pool",
              foreground="#888").grid(row=3, column=2, sticky="w", padx=(8, 0))

    adv_box = ttk.LabelFrame(outer, text=" Advanced ", padding=8)
    adv_box.columnconfigure(1, weight=1)
    add_entry(adv_box, 0, 0, "Stratum bind", "stratum_host", hint="0.0.0.0 = all interfaces")
    add_entry(adv_box, 1, 0, "P2P bind", "p2p_host")
    add_entry(adv_box, 2, 0, "Genesis share target", "share_target",
              hint="consensus — leave empty unless bootstrapping a new sidechain")
    add_entry(adv_box, 3, 0, "Pause cmd (pre-prove)", "pause_cmd", width=34,
              hint="e.g. pkill -STOP -x xmrig")
    add_entry(adv_box, 4, 0, "Resume cmd", "resume_cmd", width=34,
              hint="e.g. pkill -CONT -x xmrig")

    adv_shown = tk.BooleanVar(value=any(settings[k].strip() not in ("", DEFAULTS[k])
                                        for k in ("stratum_host", "p2p_host", "share_target",
                                                  "pause_cmd", "resume_cmd")))

    def toggle_adv():
        if adv_shown.get():
            adv_box.pack(fill="x", pady=(8, 0), before=btn_row)
        else:
            adv_box.pack_forget()

    # ---- buttons + status ------------------------------------------------- #
    btn_row = ttk.Frame(outer)
    btn_row.pack(fill="x", pady=8)
    ttk.Checkbutton(btn_row, text="Advanced", variable=adv_shown,
                    command=toggle_adv).pack(side="right")
    status_var = tk.StringVar(value="stopped")
    ttk.Label(btn_row, textvariable=status_var).pack(side="right", padx=10)

    def current_settings() -> dict:
        s = {k: v.get() for k, v in fields.items()}
        s["peers"] = peers_text.get("1.0", "end").strip()
        s["manage_pearld"] = "1" if manage_var.get() else "0"
        s["network"] = network_var.get()
        return normalize_settings(s)

    def log(line: str) -> None:
        log_text.configure(state="normal")
        log_text.insert("end", line.rstrip("\n") + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")

    def spawn(args: list[str], label: str) -> None:
        if state["proc"] is not None:
            messagebox.showinfo("P2Pearl", f"The {state['label']} is already running — stop it first.")
            return
        s = current_settings()
        save_settings(s)
        creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                self_command() + args,
                stdin=subprocess.DEVNULL,   # any stray input() in the child gets EOF, never hangs
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=creationflags)
        except OSError as exc:
            messagebox.showerror("P2Pearl", f"could not start: {exc}")
            return
        state["proc"], state["label"] = proc, label
        status_var.set(f"{label} running (pid {proc.pid})")
        start_btn["state"] = demo_btn["state"] = "disabled"
        stop_btn["state"] = "normal"
        update_payout_stats(None)
        stats_summary_var.set("Waiting for accepted shares ...")
        log(f"--- {label} started: {' '.join(args)} ---")

        def reader():
            for line in proc.stdout:
                if line.startswith(config.PAYOUT_STATS_PREFIX):
                    try:
                        payload = json.loads(line[len(config.PAYOUT_STATS_PREFIX):])
                        events.put(("payout_stats", payload))
                    except ValueError:
                        events.put(("log", line))
                else:
                    events.put(("log", line))
            events.put(("exit", proc.wait()))

        threading.Thread(target=reader, daemon=True).start()

    def start_pearld(s: dict) -> bool:
        exe = ensure_pearld_installed()
        if exe is None:
            messagebox.showerror(
                "P2Pearl",
                "No pearld available: this build has no bundled Pearl node and none was "
                "found on PATH. Untick 'Run pearld for me' and point the RPC URL at a "
                "pearld you run yourself (see docs/running-a-node.md).")
            manage_var.set(False)
            return False
        version = pearld_version(exe)
        if managed_pearld_too_old(version):
            msg = (
                f"Managed pearld is too old for the current Pearl network "
                f"(found {format_version(version)}, need "
                f"{format_version(MIN_MANAGED_PEARLD_VERSION)}+). Pearl's June 2026 "
                "MoE hard fork requires v2 certificates. Install an updated P2Pearl "
                "bundle, or untick 'Run pearld for me' and point the RPC URL at "
                "pearld 1.1.0 or newer."
            )
            log(msg)
            messagebox.showerror("P2Pearl", msg)
            return False
        flags = (0x08000000 | 0x00000200) if sys.platform == "win32" else 0  # NO_WINDOW | NEW_PROCESS_GROUP
        PEARLD_DATA_DIR.mkdir(parents=True, exist_ok=True)
        console_log = open(PEARLD_DATA_DIR / "pearld-console.log", "ab")
        try:
            proc = subprocess.Popen(pearld_args(exe, s), stdin=subprocess.DEVNULL,
                                    stdout=console_log, stderr=subprocess.STDOUT,
                                    creationflags=flags)
        except OSError as exc:
            console_log.close()
            messagebox.showerror("P2Pearl", f"could not start pearld: {exc}")
            return False
        finally:
            console_log.close()                 # the child holds its own handle
        state["pearld_proc"] = proc
        state["pearld_ready"] = False
        log(f"--- pearld started (pid {proc.pid}, {s['network']}) ---")
        log(f"    binaries : {exe.parent}")
        log(f"    chain data + logs: {PEARLD_DATA_DIR}")
        _start_watcher(s)
        return True

    def _start_watcher(s: dict) -> None:
        if state["watch_stop"] is not None:
            state["watch_stop"].set()            # retire any previous watcher
        stop_ev = threading.Event()
        state["watch_stop"] = stop_ev
        threading.Thread(target=watch_pearld_sync, args=(s, stop_ev, events.put),
                         daemon=True).start()

    def start_node():
        s = current_settings()
        if manage_var.get() and not state["pearld_ready"]:
            save_settings(s)
            state["pending_node_start"] = True
            start_btn["state"] = "disabled"
            pp = state["pearld_proc"]
            if pp is not None and pp.poll() is None:
                status_var.set("waiting for pearld to sync ...")
                return                           # already managing one; watcher will fire
            # Maybe a pearld is ALREADY serving this RPC URL (started by hand or a
            # previous session) — adopt it instead of colliding with it.
            status_var.set("checking for an existing pearld ...")

            def probe():
                events.put(("pearld_probe", test_rpc(s)[0]))

            threading.Thread(target=probe, daemon=True).start()
            return                               # continues on ("pearld_probe", ok)
        spawn(build_daemon_args(s), "node")

    def run_demo():
        spawn(["demo"], "demo")

    def stop():
        proc = state["proc"]
        if proc is None:
            return
        log(f"--- stopping {state['label']} ---")
        proc.terminate()

    def test_connection():
        test_btn["state"] = "disabled"
        status_var.set("testing pearld ...")
        s = current_settings()
        save_settings(s)

        def worker():
            events.put(("rpc", *test_rpc(s)))

        threading.Thread(target=worker, daemon=True).start()

    test_btn = ttk.Button(btn_row, text="Test pearld", command=test_connection)
    test_btn.pack(side="left")
    start_btn = ttk.Button(btn_row, text="▶ Start node", command=start_node)
    start_btn.pack(side="left", padx=6)
    stop_btn = ttk.Button(btn_row, text="■ Stop", command=stop, state="disabled")
    stop_btn.pack(side="left")
    demo_btn = ttk.Button(btn_row, text="Run demo (no setup)", command=run_demo)
    demo_btn.pack(side="left", padx=6)

    # ---- miner command helper --------------------------------------------- #
    miner_row = ttk.Frame(outer)
    miner_row.pack(fill="x")
    ttk.Label(miner_row, text="Miners run:").pack(side="left")
    miner_var = tk.StringVar(value=miner_command(settings))
    miner_entry = ttk.Entry(miner_row, textvariable=miner_var, state="readonly")
    miner_entry.pack(side="left", fill="x", expand=True, padx=6)

    def copy_miner():
        root.clipboard_clear()
        root.clipboard_append(miner_var.get())

    ttk.Button(miner_row, text="Copy", command=copy_miner).pack(side="left")

    def refresh_miner_cmd(*_):
        miner_var.set(miner_command(current_settings()))

    fields["stratum_port"].trace_add("write", refresh_miner_cmd)
    fields["wallet"].trace_add("write", refresh_miner_cmd)

    # ---- payout estimate -------------------------------------------------- #
    stats_box = ttk.LabelFrame(outer, text=" Payout estimate (next block) ", padding=8)
    stats_box.pack(fill="x", pady=(8, 0))
    stats_summary_var = tk.StringVar(value=payout_stats_summary(None))
    ttk.Label(stats_box, textvariable=stats_summary_var).pack(anchor="w")
    stats_tree = ttk.Treeview(
        stats_box, columns=("wallet", "share", "payout", "weight"), show="headings", height=4)
    for col, label, width, anchor in (
        ("wallet", "Wallet", 360, "w"),
        ("share", "PPLNS share", 95, "e"),
        ("payout", "Est. payout", 120, "e"),
        ("weight", "Weight", 120, "e"),
    ):
        stats_tree.heading(col, text=label)
        stats_tree.column(col, width=width, anchor=anchor, stretch=(col == "wallet"))
    stats_tree.pack(fill="x", pady=(4, 0))

    def update_payout_stats(snapshot: dict) -> None:
        stats_summary_var.set(payout_stats_summary(snapshot))
        for item in stats_tree.get_children():
            stats_tree.delete(item)
        for row in payout_stats_rows(snapshot):
            stats_tree.insert("", "end", values=row)

    # ---- log pane ---------------------------------------------------------- #
    log_text = scrolledtext.ScrolledText(outer, height=12, state="disabled",
                                         font=("Consolas", 9))
    log_text.pack(fill="both", expand=True, pady=(8, 0))
    log(f"P2Pearl v{__version__} control panel. Fill in your settings and click Start node —")
    log("or click 'Run demo (no setup)' to watch the pipeline work without a Pearl node.")
    log(f"Settings persist to {SETTINGS_PATH}")

    # ---- event pump -------------------------------------------------------- #
    def poll():
        try:
            while True:
                ev = events.get_nowait()
                if ev[0] == "log":
                    log(ev[1])
                elif ev[0] == "payout_stats":
                    update_payout_stats(ev[1])
                elif ev[0] == "exit":
                    log(f"--- {state['label']} exited (code {ev[1]}) ---")
                    state["proc"] = None
                    status_var.set("stopped")
                    start_btn["state"] = demo_btn["state"] = "normal"
                    stop_btn["state"] = "disabled"
                elif ev[0] == "pearld_probe":
                    if not state["pending_node_start"]:
                        pass                     # superseded (user changed course)
                    elif ev[1]:
                        log("--- a pearld is already serving this RPC URL — using it ---")
                        status_var.set("waiting for pearld to sync ...")
                        _start_watcher(current_settings())
                    elif start_pearld(current_settings()):
                        status_var.set("waiting for pearld to sync ...")
                    else:
                        state["pending_node_start"] = False
                        start_btn["state"] = "normal"
                        status_var.set("stopped")
                elif ev[0] == "pearld_status":
                    if state["pearld_proc"] is not None or state["pending_node_start"]:
                        status_var.set(ev[1])
                elif ev[0] == "pearld_ready":
                    state["pearld_ready"] = True
                    log(f"--- pearld is synced and serving (height {ev[1]:,}) ---")
                    status_var.set(f"pearld ready — height {ev[1]:,}")
                    if state["pending_node_start"]:
                        state["pending_node_start"] = False
                        start_btn["state"] = "normal"
                        spawn(build_daemon_args(current_settings()), "node")
                elif ev[0] == "rpc":
                    _ok, msg = ev[1], ev[2]
                    status_var.set(msg)
                    log(msg)
                    test_btn["state"] = "normal"
        except queue.Empty:
            pass
        # A managed pearld that died (port clash, bad flags) must not strand the UI.
        pp = state["pearld_proc"]
        if pp is not None and pp.poll() is not None:
            log(f"--- pearld exited (code {pp.returncode}) — see "
                f"{PEARLD_DATA_DIR / 'pearld-console.log'} ---")
            log("    hint: if another pearld is already running on this machine, close it —")
            log("    or untick 'Run pearld for me' and point the RPC URL at it (it must be")
            log("    started with --rpcuser/--rpcpass for P2Pearl to reach it).")
            state["pearld_proc"] = None
            state["pearld_ready"] = False
            if state["watch_stop"] is not None:
                state["watch_stop"].set()
            if state["pending_node_start"]:
                state["pending_node_start"] = False
                start_btn["state"] = "normal"
                status_var.set("pearld exited — fix and Start again")
        root.after(120, poll)

    def on_close():
        running = [n for n, p in (("pool node", state["proc"]), ("pearld", state["pearld_proc"]))
                   if p is not None and p.poll() is None]
        if running:
            if not messagebox.askokcancel("P2Pearl", f"Stop the running {' + '.join(running)} and quit?"):
                return
        if state["watch_stop"] is not None:
            state["watch_stop"].set()
        proc = state["proc"]
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if state["pearld_proc"] is not None:
            stop_pearld_gracefully(state["pearld_proc"], current_settings())  # clean flush, ~seconds
        save_settings(current_settings())
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    toggle_adv()
    root.after(120, poll)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
