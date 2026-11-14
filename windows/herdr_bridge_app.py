"""herdr-bridge: local, non-admin controller for the reviewed Herdr transport."""
from __future__ import annotations
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

VERSION = "0.2.1"
PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))
from bridge_config import load_bridge_config, runtime_root

ROOT = runtime_root()
TRANSPORT = ROOT / "windows_bridge.py"
EXPECTED_HASH = "fe67a717f0774c595c4d2cb2f4f11de32d172526305690beda9fbfd340a89f5e"
EXPECTED_CONFIG_HASH = "681f2af0e113533d4a9d12f958e20f0f33dab4303de8ce8b04c3cadaba9549c2"
PYTHON = Path(sys.executable)
HERDR = Path(os.environ.get("HERDR_APP", ROOT.parent / "Programs" / "herdr-app" / "herdr-app.exe"))

def expected_core():
    return load_bridge_config(ROOT)["core_id"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RECOVERY = "sudo systemctl start ssh"

def read_status(root=ROOT):
    try:
        with (root / "status.json").open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Status file is unexpectedly large")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Invalid status record")
        return value
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError) as exc:
        return {"phase": "unreadable", "reason": str(exc)}

def verify_transport(path=TRANSPORT):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != EXPECTED_HASH:
        raise RuntimeError("Transport code changed. Review it before starting.")
    config_digest = hashlib.sha256((ROOT / "bridge_config.py").read_bytes()).hexdigest()
    if config_digest != EXPECTED_CONFIG_HASH:
        raise RuntimeError("Bridge config loader changed. Review it before starting.")
    return digest

def helper_locked(root=ROOT):
    import msvcrt
    path = root / "publisher.lock"
    if not path.exists():
        return False
    with path.open("r+b") as handle:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (13, 36) or getattr(exc, "winerror", None) in (5, 33, 36):
                return True
            raise
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return False

def python_process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        return False
    try:
        if kernel.WaitForSingleObject(handle, 0) != 258:
            return False
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return False
        return Path(buf.value).resolve() == Path(sys.executable).resolve()
    finally:
        kernel.CloseHandle(handle)

def view_state(status, locked, alive, stop_requested=False):
    phase = status.get("phase", "unknown")
    if not locked:
        if phase == "failed_closed":
            return "Stopped safely", str(status.get("reason") or "Transport refused an unsafe change."), "#b45309"
        return "Stopped", "The bridge is off. Herdr cores and workers are unaffected.", "#64748b"
    if stop_requested:
        return "Stopping", "Waiting for the existing helper to close its own transport.", "#b45309"
    if not alive:
        return "Checking helper", "A bridge lock is held, but its process identity is not verified. No duplicate will be started.", "#b45309"
    if status.get("core") != expected_core():
        return "Identity mismatch", "Status names a different core. Start is blocked until reviewed.", "#b91c1c"
    if phase == "connected":
        return "Helper reports connected", "The last transport handshake succeeded. This is the helper\u2019s reported state, not an end-to-end health probe.", "#15803d"
    if phase == "listening":
        return "Ready for the VM", "The private VM endpoint is ready. Waiting for Herdr to attach.", "#0369a1"
    if phase in ("connecting", "starting_connection", "disconnected", "starting"):
        return "Reconnecting", "The existing helper is retrying the pinned SSH connection. No password prompts.", "#b45309"
    return "Check connection", str(status.get("reason") or "The helper is running; connectivity is not confirmed."), "#b45309"

def require_account():
    if os.name != "nt":
        raise RuntimeError("This launcher is configured for the Windows host.")
    if ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("Run herdr-bridge normally, not as administrator.")
    import csv, io
    result = subprocess.run([r"C:\Windows\System32\whoami.exe", "/user", "/fo", "csv", "/nh"],
                            check=True, capture_output=True, timeout=5, creationflags=NO_WINDOW)
    rows = list(csv.reader(io.StringIO(result.stdout.decode("utf-8", errors="replace"))))
    if not rows or rows[0][-1].strip() != load_bridge_config(ROOT)["user_sid"]:
        raise RuntimeError("This bridge belongs to a different Windows account.")

class Controller:
    def snapshot(self):
        status = read_status()
        locked = helper_locked()
        alive = python_process_alive(status.get("helper_pid")) if locked else False
        return status, locked, alive, (ROOT / "stop.request").exists()

    def start(self):
        require_account()
        verify_transport()
        if helper_locked():
            return "Already running. No duplicate helper was started."
        (ROOT / "stop.request").unlink(missing_ok=True)
        env = dict(os.environ)
        for key in ("HC_SOCKET", "HC_SESSION", "HC_TERMINAL", "HC_CALLER"):
            env.pop(key, None)
        with (ROOT / "helper.stdout.log").open("ab") as out, (ROOT / "helper.stderr.log").open("ab") as err:
            process = subprocess.Popen([str(PYTHON), "-I", str(TRANSPORT)],
                                       stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       env=env, creationflags=NO_WINDOW, close_fds=True)
        return f"Started helper {process.pid}. Waiting for verified connection."

    def stop(self):
        require_account()
        (ROOT / "stop.request").write_text("Stop requested in herdr-bridge.", encoding="utf-8")
        return "Stop requested. Herdr cores and workers are not stopped."

def show_app(smoke=False):
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = tk.Tk()
    root.title("herdr-bridge")
    root.geometry("720x660")
    root.minsize(720, 640)
    root.configure(bg="#f5f7fb")
    style = ttk.Style(root)
    style.theme_use("vista" if "vista" in style.theme_names() else "clam")
    style.configure("TFrame", background="#f5f7fb")
    style.configure("TLabel", background="#f5f7fb", font=("Segoe UI", 10))
    style.configure("TButton", font=("Segoe UI", 10), padding=(12, 8))
    body = ttk.Frame(root, padding=26)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="herdr-bridge", font=("Segoe UI", 24, "bold")).pack(anchor="w")
    ttk.Label(body, text="Windows Herdr  \u2194  Linux VM", foreground="#475569").pack(anchor="w", pady=(2, 20))
    title = tk.StringVar(value="Checking connection")
    detail = tk.StringVar()
    metadata = tk.StringVar()
    status_label = ttk.Label(body, textvariable=title, font=("Segoe UI", 18, "bold"))
    status_label.pack(anchor="w")
    ttk.Label(body, textvariable=detail, wraplength=650).pack(anchor="w", pady=(5, 8))
    ttk.Label(body, textvariable=metadata, foreground="#64748b", wraplength=650).pack(anchor="w")
    buttons = ttk.Frame(body)
    buttons.pack(anchor="w", pady=20)
    controller = Controller()
    note = tk.StringVar(value="Opening this window does not start, stop, or restart a bridge.")
    start_button = ttk.Button(buttons, text="Start bridge")
    start_button.pack(side="left", padx=(0, 8))
    stop_button = ttk.Button(buttons, text="Stop bridge")
    stop_button.pack(side="left", padx=(0, 8))
    def action(which):
        try:
            if which == "stop" and not messagebox.askyesno(
                "Stop bridge?", "Disconnect Windows from the VM?\n\nHerdr cores and workers keep running.", parent=root):
                return
            note.set(controller.start() if which == "start" else controller.stop())
        except Exception as exc:
            messagebox.showerror("herdr-bridge", str(exc), parent=root)
        refresh(False)
    start_button.configure(command=lambda: action("start"))
    stop_button.configure(command=lambda: action("stop"))
    def open_herdr():
        try:
            os.startfile(str(HERDR))
        except OSError as exc:
            messagebox.showerror("Open Herdr", str(exc), parent=root)
    ttk.Button(buttons, text="Open Herdr", command=open_herdr).pack(side="left")
    ttk.Label(body, textvariable=note, wraplength=650, foreground="#334155").pack(anchor="w", pady=(0, 16))
    ttk.Separator(body).pack(fill="x", pady=(0, 16))
    ttk.Label(body, text="Recovery", font=("Segoe UI", 12, "bold")).pack(anchor="w")
    ttk.Label(body, text="If the VM has rebooted and SSH is stopped, run this in its own terminal:",
              wraplength=650).pack(anchor="w", pady=(4, 6))
    command_row = ttk.Frame(body)
    command_row.pack(fill="x")
    command = ttk.Entry(command_row, font=("Consolas", 11))
    command.insert(0, RECOVERY)
    command.configure(state="readonly")
    command.pack(side="left", fill="x", expand=True, padx=(0, 8))
    def copy_command():
        root.clipboard_clear()
        root.clipboard_append(RECOVERY)
        note.set("Copied. Run it in the VM and enter your password there, never in chat.")
    ttk.Button(command_row, text="Copy", command=copy_command).pack(side="left")
    ttk.Label(body, text="No automatic startup is configured by this app. Closing this window leaves an existing bridge running.\n"
              "The VM can run commands and access files as your Windows account. No inbound Windows port is opened.",
              wraplength=650, foreground="#64748b").pack(anchor="w", pady=(15, 10))
    def diagnostics():
        try:
            status, locked, alive, stopped = controller.snapshot()
            diag = json.dumps({"version": VERSION, "status": status, "lock_held": locked,
                               "helper_runtime_matches": alive, "stop_requested": stopped,
                               "transport_hash_verified": verify_transport() == EXPECTED_HASH}, indent=2)
            win = tk.Toplevel(root)
            win.title("herdr-bridge diagnostics")
            win.geometry("670x400")
            text = tk.Text(win, wrap="word", font=("Consolas", 10))
            text.pack(fill="both", expand=True)
            text.insert("1.0", diag)
            text.configure(state="disabled")
        except Exception as exc:
            messagebox.showerror("Diagnostics", str(exc), parent=root)
    ttk.Button(body, text="Diagnostics", command=diagnostics).pack(anchor="w")
    def refresh(schedule=True):
        try:
            status, locked, alive, stopped = controller.snapshot()
            heading, message, color = view_state(status, locked, alive, stopped)
            title.set(heading); detail.set(message)
            status_label.configure(foreground=color)
            metadata.set(f"Helper: {status.get('helper_pid', '-')}   |   Windows core: {status.get('server_pid', '-')}\n"
                         f"Last connection event: {status.get('time_utc', '-')}   |   Attempt: {status.get('attempt', '-')}")
            start_button.configure(state="disabled" if locked else "normal")
            stop_button.configure(state="normal" if locked and not stopped else "disabled")
        except Exception as exc:
            title.set("Unable to verify"); detail.set(str(exc))
            start_button.configure(state="disabled"); stop_button.configure(state="disabled")
        if schedule:
            root.after(1500, refresh)
    refresh()
    if smoke:
        root.update()
        print(json.dumps({"title": root.title(), "status": title.get(),
                          "geometry": root.geometry(), "requested_height": body.winfo_reqheight(),
                          "actual_height": body.winfo_height()}))
        root.after(1200, root.destroy)
    root.mainloop()

def self_test():
    import tempfile, unittest
    class Tests(unittest.TestCase):
        def test_transport_pin(self):
            self.assertEqual(verify_transport(), EXPECTED_HASH)
        def test_pin_rejects_change(self):
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "bad.py"; p.write_text("changed")
                self.assertRaises(RuntimeError, verify_transport, p)
        def test_missing_status(self):
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(read_status(Path(d)), {})
        def test_invalid_status(self):
            with tempfile.TemporaryDirectory() as d:
                p = Path(d); (p / "status.json").write_text("{")
                self.assertEqual(read_status(p)["phase"], "unreadable")
        def test_stale_connected_not_claimed(self):
            self.assertEqual(view_state({"phase": "connected"}, False, False)[0], "Stopped")
        def test_connected_ignores_old_failure_fields(self):
            s = {"phase":"connected", "core":expected_core(), "ssh_exit":255, "ending":"disconnected"}
            self.assertEqual(view_state(s, True, True)[0], "Helper reports connected")
        def test_lock_without_process_blocks(self):
            self.assertEqual(view_state({}, True, False)[0], "Checking helper")
        def test_stop_pending(self):
            self.assertEqual(view_state({}, True, True, True)[0], "Stopping")
        def test_wrong_core(self):
            self.assertEqual(view_state({"core":"wrong"}, True, True)[0], "Identity mismatch")
        def test_listening_not_connected(self):
            self.assertEqual(view_state({"core":expected_core(),"phase":"listening"}, True, True)[0], "Ready for the VM")
        def test_failed_closed(self):
            self.assertEqual(view_state({"phase":"failed_closed","reason":"changed core"}, False, False)[0], "Stopped safely")
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    return 0 if result.wasSuccessful() else 1

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    require_account()
    if args.status:
        data = Controller().snapshot()
        print(json.dumps({"display": view_state(*data), "status": data[0], "lock_held":data[1], "helper_runtime_matches":data[2]}))
        return 0
    show_app(args.smoke_test)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        if sys.executable.lower().endswith("pythonw.exe"):
            ctypes.windll.user32.MessageBoxW(None, str(exc), "herdr-bridge", 0x10)
        else:
            print(f"herdr-bridge: {exc}", file=sys.stderr)
        sys.exit(1)
