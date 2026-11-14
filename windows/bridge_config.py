"""Load host-specific settings for the Windows bridge from LocalAppData."""
from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
import re

MAX_CONFIG_BYTES = 32768
REQUIRED_KEYS = {
    "core_id", "user_sid", "pipe_path", "ssh_host", "ssh_user",
    "ssh_key_path", "known_hosts_path", "remote_command",
}


def runtime_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is missing; run this helper in the signed-in Windows user session")
    return Path(local_app_data) / "HerdrBridge"


def core_process_ids(status: object) -> tuple[int, int]:
    """Return verified server/holder PIDs, or explain why no session is bridgeable."""
    if not isinstance(status, dict):
        raise RuntimeError("Herdr returned an invalid server status object")
    server_pid = status.get("pid")
    if type(server_pid) is not int or server_pid <= 0:
        raise RuntimeError("Herdr server status has no valid core PID")
    holder = status.get("holder")
    if not isinstance(holder, dict):
        raise RuntimeError("No active Herdr holder is attached. Open an existing Herdr session, then start the bridge again.")
    holder_pid = holder.get("pid")
    if type(holder_pid) is not int or holder_pid <= 0:
        raise RuntimeError("Herdr holder status has no valid PID. Keep the existing Herdr session open and retry.")
    return server_pid, holder_pid


def load_bridge_config(root: Path | None = None) -> dict[str, str]:
    root = Path(root) if root is not None else runtime_root()
    config_path = Path(os.environ.get("HERDR_BRIDGE_CONFIG", root / "bridge.json"))
    raw = config_path.read_bytes()
    if len(raw) > MAX_CONFIG_BYTES:
        raise RuntimeError("Bridge config is unexpectedly large")
    try:
        config = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Bridge config is not valid UTF-8 JSON") from exc
    if not isinstance(config, dict) or set(config) != REQUIRED_KEYS:
        raise RuntimeError("Bridge config has missing or unexpected fields")
    if any(not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value
           for value in config.values()):
        raise RuntimeError("Bridge config values must be non-empty strings")
    if not re.fullmatch(r"[0-9a-f]{32}", config["core_id"]):
        raise RuntimeError("Bridge config core_id must be a 32-character lowercase hex ID")
    if not re.fullmatch(r"S-1-[0-9]+(?:-[0-9]+){2,}", config["user_sid"]):
        raise RuntimeError("Bridge config user_sid is malformed")
    pipe_pattern = re.escape(r"\\.\pipe\herdr-core-") + r"[A-Za-z0-9_.-]+" + re.escape("-default.sock")
    if not re.fullmatch(pipe_pattern, config["pipe_path"]):
        raise RuntimeError("Bridge config pipe_path is not a default Herdr named pipe")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", config["ssh_host"]):
        raise RuntimeError("Bridge config ssh_host contains unsupported characters")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", config["ssh_user"]):
        raise RuntimeError("Bridge config ssh_user contains unsupported characters")
    if not re.fullmatch(r"python3 -I /[A-Za-z0-9_./-]+/vm_bridge\.py publish", config["remote_command"]):
        raise RuntimeError("Bridge config remote_command is not the approved publisher command")
    if "/../" in config["remote_command"]:
        raise RuntimeError("Bridge config remote_command contains a parent path segment")
    for key in ("ssh_key_path", "known_hosts_path"):
        expanded = os.path.expandvars(config[key])
        if not (Path(expanded).is_absolute() or ntpath.isabs(expanded)):
            raise RuntimeError(f"Bridge config {key} must be an absolute local path")
        config[key] = expanded
    return config
