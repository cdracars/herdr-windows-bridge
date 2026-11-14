"""Private Herdr reverse bridge. No listener or autostart on Windows."""
import argparse
import _winapi
from ctypes import wintypes
import csv
import ctypes
import io
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

MODULE_DIR = Path(__file__).resolve().parent
CONFIG_MODULE_DIR = MODULE_DIR if (MODULE_DIR / "bridge_config.py").exists() else MODULE_DIR.parent
sys.path.insert(0, str(CONFIG_MODULE_DIR))
from bridge_config import load_bridge_config, runtime_root

ROOT = runtime_root()
CONFIG = load_bridge_config(ROOT)
HC = str(Path(os.environ.get("HERDR_HC", ROOT.parent / "Programs" / "herdr-core" / "hc.exe")))
SSH = str(Path(os.environ.get("HERDR_SSH", r"C:\Windows\System32\OpenSSH\ssh.exe")))
WHOAMI = str(Path(os.environ.get("HERDR_WHOAMI", r"C:\Windows\System32\whoami.exe")))
CORE = CONFIG["core_id"]
SID = CONFIG["user_sid"]
SERVER_PID = None
HOLDER_PID = None
REMOTE = CONFIG["remote_command"]
NO_WINDOW = subprocess.CREATE_NO_WINDOW
PIPE_PATH = CONFIG["pipe_path"]
KEY = CONFIG["ssh_key_path"]
KNOWN_HOSTS = CONFIG["known_hosts_path"]
HAS_OWNED_LOCK = False
SERVER_HANDLE = None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL

class ExistingPipe:
    """Byte-mode, overlapped access to one already-existing Herdr pipe."""
    def __init__(self):
        if SERVER_HANDLE is None or _winapi.WaitForSingleObject(SERVER_HANDLE, 0) != 258:
            raise RuntimeError("original Herdr server process is no longer alive")
        self.lock = threading.Lock()
        self.pending = set()
        self.closing = False
        self.handle = _winapi.CreateFile(PIPE_PATH, 0xC0000000, 0, 0, 3,
                                        0x40000000 | 0x00100000 | 0x00010000, 0)
        try:
            pid = wintypes.DWORD()
            if not kernel32.GetNamedPipeServerProcessId(self.handle, ctypes.byref(pid)):
                raise ctypes.WinError(ctypes.get_last_error())
            if pid.value != SERVER_PID:
                raise RuntimeError("existing pipe belongs to an unexpected server PID")
            _winapi.SetNamedPipeHandleState(self.handle, 0, None, None)
        except BaseException:
            _winapi.CloseHandle(self.handle)
            self.handle = None
            raise

    def operation(self, writing, value):
        with self.lock:
            if self.closing:
                raise BrokenPipeError("pipe is closing")
            if writing:
                ov, err = _winapi.WriteFile(self.handle, value, overlapped=True)
            else:
                ov, err = _winapi.ReadFile(self.handle, value, overlapped=True)
            self.pending.add(ov)
        try:
            count, err = ov.GetOverlappedResult(True)
            if err not in (0, 234):
                if err in (109, 232, 233, 995):
                    return 0 if writing else b""
                raise OSError(err, "overlapped pipe operation failed")
            return count if writing else bytes(ov.getbuffer())[:count]
        finally:
            with self.lock:
                self.pending.discard(ov)

    def read(self, size):
        return self.operation(False, size)

    def write(self, value):
        return self.operation(True, value)

    def close_write(self):
        # Windows Herdr treats named-pipe EOF as full disconnect, not SHUT_WR.
        self.cancel()

    def cancel(self):
        with self.lock:
            self.closing = True
            for ov in tuple(self.pending):
                try: ov.cancel()
                except OSError: pass

    def close(self):
        self.cancel()
        with self.lock:
            if self.pending:
                raise RuntimeError("cannot close handle while overlapped operations remain")
            if self.handle is not None:
                _winapi.CloseHandle(self.handle)
                self.handle = None
STOP = ROOT / "stop.request"
STATUS = ROOT / "status.json"
LOG = ROOT / "bridge.log"
ENV = dict(os.environ)
ENV.pop("HC_SOCKET", None)
ENV.pop("HC_SESSION", None)
ENV.pop("HC_TERMINAL", None)
ENV.pop("HC_CALLER", None)
ENV["HC_SESSION"] = "default"
status_lock = threading.Lock()
state = {"helper_pid": os.getpid(), "core": CORE, "phase": "starting", "attempt": 0}

def report(phase, **fields):
    with status_lock:
        state.update(fields, phase=phase, time_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        temp = STATUS.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(temp, STATUS)
        if LOG.exists() and LOG.stat().st_size > 65536:
            os.replace(LOG, ROOT / "bridge.previous.log")
        with LOG.open("a", encoding="utf-8") as log:
            log.write(f"{state['time_utc']} {phase} {json.dumps(fields, sort_keys=True)}\n")

def checked_core():
    if SERVER_HANDLE is not None and _winapi.WaitForSingleObject(SERVER_HANDLE, 0) != 258:
        raise RuntimeError("original Herdr server process ended; refusing restart")
    if ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("refusing elevated account")
    identity = subprocess.run([WHOAMI, "/user", "/fo", "csv", "/nh"],
        capture_output=True, timeout=5, check=True, creationflags=NO_WINDOW)
    rows = list(csv.reader(io.StringIO(identity.stdout.decode("utf-8", errors="replace"))))
    if not rows or rows[0][-1].strip() != SID:
        raise RuntimeError("unexpected Windows account")
    out = subprocess.run([HC, "server", "status"], capture_output=True, timeout=5,
        env=ENV, check=True, creationflags=NO_WINDOW)
    info = json.loads(out.stdout)
    if info.get("core", {}).get("id") != CORE or (SERVER_PID is not None and info.get("pid") != SERVER_PID):
        raise RuntimeError("expected existing Windows core is absent or changed")
    if HOLDER_PID is not None and info.get("holder", {}).get("pid") != HOLDER_PID:
        raise RuntimeError("expected existing Windows holder changed")
    return info

def pin_existing_core():
    global SERVER_PID, HOLDER_PID, SERVER_HANDLE
    info = checked_core()
    SERVER_PID = info["pid"]
    HOLDER_PID = info["holder"]["pid"]
    SERVER_HANDLE = _winapi.OpenProcess(0x00100000, False, SERVER_PID)
    checked_core()  # Same snapshot and still-live retained process, before any exposure.

def pump(source, destination, ended):
    try:
        while True:
            block = source.read(65536)
            if not block:
                break
            view = memoryview(block)
            while view:
                count = destination.write(view)
                if count <= 0:
                    raise OSError("short pipe write")
                view = view[count:]
    except (OSError, ValueError):
        pass
    finally:
        try:
            destination.close_write() if hasattr(destination, "close_write") else destination.close()
        except (OSError, ValueError):
            pass
        ended.set()

def drain(stream, from_ssh=False):
    # Only fixed readiness markers are interpreted. No terminal/API bytes are logged.
    pending = bytearray()
    try:
        while True:
            block = os.read(stream.fileno(), 1024)
            if not block:
                return
            pending.extend(block)
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending = bytearray(rest)
                if from_ssh and line.strip() in (b"BRIDGE_READY", b"BRIDGE_CONNECTED"):
                    report("listening" if line.strip() == b"BRIDGE_READY" else "connected")
            if len(pending) > 2048:
                pending.clear()
    except (OSError, ValueError):
        return

def stop_children(children):
    for child in children:
        if child.stdin is not None:
            try: child.stdin.close()
            except (OSError, ValueError): pass
    for child in children:
        if child.poll() is None:
            child.terminate()  # Only our transport PID, never a process tree or the core server.
    for child in children:
        try: child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)

def one_connection():
    checked_core()
    children = []
    threads = []
    pipe = None
    try:
        # OPEN_EXISTING and server-PID verification. No core-starting command.
        pipe = ExistingPipe()
        ssh = subprocess.Popen([SSH, "-F", "NUL", "-T", "-i", KEY, "-l", CONFIG["ssh_user"],
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "UserKnownHostsFile=" + KNOWN_HOSTS, "-o", "HostKeyAlias=herdr-linux-vm",
            "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlgorithms=ssh-ed25519",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "ForwardAgent=no", "-o", "PermitLocalCommand=no",
            "-o", "ConnectTimeout=6", "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3", "-o", "ClearAllForwardings=yes",
            CONFIG["ssh_host"], REMOTE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, env=ENV, creationflags=NO_WINDOW)
        children.append(ssh)
        report("connecting", server_pid=SERVER_PID, ssh_pid=ssh.pid)
        ends = [threading.Event(), threading.Event()]
        calls = [
            (pump, (pipe, ssh.stdin, ends[0])),
            (pump, (ssh.stdout, pipe, ends[1])),
            (drain, (ssh.stderr, True)),
        ]
        for fn, args in calls:
            thread = threading.Thread(target=fn, args=args, daemon=True)
            threads.append(thread)
            thread.start()
        while not STOP.exists():
            if any(e.is_set() for e in ends) or ssh.poll() is not None:
                break
            if _winapi.WaitForSingleObject(SERVER_HANDLE, 0) != 258:
                raise RuntimeError("original Herdr server process ended; refusing restart")
            time.sleep(0.1)
        return {"ssh_exit": ssh.poll(), "ending": "stopped" if STOP.exists() else "disconnected"}
    finally:
        # EOF ends this full-duplex Herdr connection. Never replay queued bytes.
        if pipe is not None:
            pipe.cancel()
        stop_children(children)
        for child in children:
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    try: stream.close()
                    except (OSError, ValueError): pass
        for thread in threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("transport worker did not stop; refusing another connection")
        if pipe is not None:
            pipe.close()

def self_test():
    # Exact binary transit through pump, including zeros, 0xff, CR/LF and >64KiB.
    import hashlib
    payload = bytes(range(256)) * 4096
    a_r, a_w = os.pipe()
    b_r, b_w = os.pipe()
    ended = threading.Event()
    source = os.fdopen(a_r, "rb", buffering=0)
    destination = os.fdopen(b_w, "wb", buffering=0)
    worker = threading.Thread(target=pump, args=(source, destination, ended), daemon=True)
    worker.start()
    def feed():
        with os.fdopen(a_w, "wb", buffering=0) as f:
            view = memoryview(payload)
            while view:
                n = f.write(view)
                view = view[n:]
    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    result = bytearray()
    with os.fdopen(b_r, "rb", buffering=0) as f:
        while True:
            block = f.read(65536)
            if not block: break
            result.extend(block)
    feeder.join(timeout=5)
    worker.join(timeout=5)
    source.close()
    if bytes(result) != payload or not ended.is_set():
        raise RuntimeError("binary pump test failed")
    print(json.dumps({"binary_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                      "binary_exact": True, "eof_closed": True}))
    info = checked_core()
    print(json.dumps({"identity": "expected non-admin account", "core": CORE,
                      "server_pid": info["pid"], "holder_pid": info["holder"]["pid"]}))

def main():
    global HAS_OWNED_LOCK, SERVER_HANDLE
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if STOP.exists():
        raise RuntimeError("stop.request exists; explicit start required")
    with (ROOT / "publisher.lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("another Windows bridge helper holds the lock")
        HAS_OWNED_LOCK = True
        failure = None
        try:
            pin_existing_core()
            attempt = 0
            while not STOP.exists():
                attempt += 1
                report("starting_connection", attempt=attempt)
                result = one_connection()
                report("disconnected", **result)
                until = time.monotonic() + min(attempt, 5)
                while not STOP.exists() and time.monotonic() < until:
                    time.sleep(0.1)
        except BaseException as exc:
            failure = str(exc)
            raise
        finally:
            report("failed_closed" if failure else "stopped", reason=failure, ssh_pid=None)
            if SERVER_HANDLE is not None:
                _winapi.CloseHandle(SERVER_HANDLE)
                SERVER_HANDLE = None
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"BRIDGE_ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
