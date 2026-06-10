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
import queue
import subprocess
import sys
import threading
from pathlib import Path

from . import __version__
from .config import NodeRPCConfig

SETTINGS_PATH = Path.home() / ".p2pearl" / "gui.json"
GUI_UNAVAILABLE = 2          # main() return code: tkinter/display missing — caller may fall back

DEFAULTS = {
    "rpc_url": "http://127.0.0.1:44107",
    "rpc_user": "user",
    "rpc_pass": "pass",
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


def build_daemon_args(settings: dict) -> list[str]:
    """Map the settings dict onto `p2pearl daemon` CLI arguments."""
    s = {**DEFAULTS, **settings}
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


def self_command() -> list[str]:
    """How to re-invoke this same program (frozen exe or `python -m p2pearl`)."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "p2pearl"]


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
    state = {"proc": None, "label": ""}      # the running child (daemon or demo)

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
    add_entry(node_box, 0, 0, "RPC URL", "rpc_url", width=34,
              hint="44107 mainnet / 44109 testnet convention")
    add_entry(node_box, 1, 0, "RPC user", "rpc_user")
    add_entry(node_box, 2, 0, "RPC password", "rpc_pass", show="•")

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
        return s

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
        log(f"--- {label} started: {' '.join(args)} ---")

        def reader():
            for line in proc.stdout:
                events.put(("log", line))
            events.put(("exit", proc.wait()))

        threading.Thread(target=reader, daemon=True).start()

    def start_node():
        spawn(build_daemon_args(current_settings()), "node")

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

    # ---- log pane ---------------------------------------------------------- #
    log_text = scrolledtext.ScrolledText(outer, height=14, state="disabled",
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
                elif ev[0] == "exit":
                    log(f"--- {state['label']} exited (code {ev[1]}) ---")
                    state["proc"] = None
                    status_var.set("stopped")
                    start_btn["state"] = demo_btn["state"] = "normal"
                    stop_btn["state"] = "disabled"
                elif ev[0] == "rpc":
                    _ok, msg = ev[1], ev[2]
                    status_var.set(msg)
                    log(msg)
                    test_btn["state"] = "normal"
        except queue.Empty:
            pass
        root.after(120, poll)

    def on_close():
        proc = state["proc"]
        if proc is not None:
            if not messagebox.askokcancel("P2Pearl", f"Stop the running {state['label']} and quit?"):
                return
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        save_settings(current_settings())
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    toggle_adv()
    root.after(120, poll)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
