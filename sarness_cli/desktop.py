"""Optional import of connection details from Sarness Desktop (no secrets are printed or logged)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import _xdg


def desktop_profiles() -> list[dict[str, Any]]:
    path = _xdg("XDG_CONFIG_HOME", ".config") / "sarness" / "profiles.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [p for p in data.get("profiles", []) if isinstance(p, dict) and p.get("host")]


def desktop_token(profile_id: str) -> str | None:
    """Read the token Sarness Desktop stored in the Secret Service (libsecret) for this profile."""
    if not re.fullmatch(r"[0-9a-f]{32}", profile_id):
        return None
    try:
        import secretstorage  # optional extra: sarness-cli[keyring]
        conn = secretstorage.dbus_init()
        col = secretstorage.get_default_collection(conn)
        if col.is_locked():
            return None
        for item in col.search_items({"service": "sarness", "username": profile_id}):
            return item.get_secret().decode("utf-8").strip() or None
    except Exception:  # noqa: BLE001 - no keyring / D-Bus / locked: fall through to secret-tool
        pass
    tool = shutil.which("secret-tool")
    if tool:
        res = subprocess.run([tool, "lookup", "service", "sarness", "username", profile_id], capture_output=True, text=True, timeout=20)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    return None


def ssh_hosts() -> list[dict[str, str]]:
    """Host aliases from ~/.ssh/config (name, hostname, user) - for the setup menu only."""
    out: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    cfg = Path.home() / ".ssh" / "config"
    try:
        lines = cfg.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or line.lstrip().startswith("#"):
            continue
        key, val = parts[0].lower(), parts[1].strip()
        if key == "host" and "*" not in val and "?" not in val:
            cur = {"alias": val.split()[0]}
            out.append(cur)
        elif cur is not None and key in {"hostname", "user"}:
            cur[key] = val
    return out
