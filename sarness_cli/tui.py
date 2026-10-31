"""The Sarness terminal: a maintained fork of OpenCode 2.x (MIT) built from a pinned upstream commit plus the patch
series in sarness_cli/tui_build/. It is installed under ~/.local/share/sarness-cli/tui and never writes to a separate
`opencode` installation (its data lives in ~/.local/share/sarness-code, ~/.config/sarness-code, ...); only
`sarness tui migrate` runs that installation, to list and export its sessions."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__

BINARY = "sarness-tui"
BUILD_DIR = Path(__file__).parent / "tui_build"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def install_root() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "sarness-cli" / "tui"


def build_root() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "sarness-cli" / "tui-build"


def find_binary() -> Path | None:
    """SARNESS_TUI_BIN (development override), else the installed `current` build."""
    override = os.environ.get("SARNESS_TUI_BIN")
    candidates = [Path(override)] if override else [install_root() / "current" / BINARY]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def require_binary() -> Path:
    found = find_binary()
    if not found:
        raise RuntimeError("The Sarness terminal is not installed. Build and install it with 'sarness tui install' "
                           "(or use the Python runtime: 'sarness native').")
    return found


def sarness_executable() -> str:
    """Absolute path of the running `sarness` entry point, for commands the terminal delegates (e.g. /cabinet)."""
    argv0 = Path(sys.argv[0])
    if argv0.name == "sarness" and argv0.exists():
        return str(argv0.resolve())
    return shutil.which("sarness") or str(argv0.resolve())


def installed_version(binary: Path) -> str:
    return subprocess.check_output([str(binary), "--version"], text=True, timeout=20).strip()


def install(*, keep_build: bool = False, jobs_env: dict[str, str] | None = None) -> int:
    """Build the pinned fork with the bundled script and install it (the previous build stays for rollback)."""
    script = BUILD_DIR / "build.sh"
    if not script.is_file():
        raise RuntimeError(f"Build script missing from this installation ({script})")
    env = {**os.environ, **(jobs_env or {}), "SARNESS_VERSION": __version__,
           "SARNESS_TUI_INSTALL_ROOT": str(install_root()), "SARNESS_TUI_BUILD_ROOT": str(build_root()),
           "SARNESS_TUI_KEEP_BUILD": "1" if keep_build else "0"}
    return subprocess.call(["bash", str(script)], env=env)


def upstream_opencode() -> str | None:
    """The user's own OpenCode install, used only to export its sessions for `sarness tui migrate`."""
    home = Path.home() / ".opencode" / "bin" / "opencode"
    return shutil.which("opencode") or (str(home) if home.is_file() and os.access(home, os.X_OK) else None)


def migrate_sessions(directory: Path, binary: Path, upstream: str) -> dict[str, list[str]]:
    """Copy this project's OpenCode sessions (e.g. from Sarness 0.3.x) into the Sarness terminal's own storage.
    OpenCode is only asked to list and export; nothing in its storage is changed. Re-running is harmless.
    Every call is --standalone: without it both executables start a background service that outlives the command."""
    env = {**os.environ, "PWD": str(directory), "OPENCODE_DISABLE_AUTOUPDATE": "1"}
    run = dict(cwd=directory, env=env, capture_output=True, text=True, timeout=120)
    listing = subprocess.run([upstream, "session", "list", "--standalone"], **run)
    if listing.returncode != 0:
        raise RuntimeError(f"'opencode session list' failed: {(listing.stderr or listing.stdout).strip()[:300]}")
    ids = [line.split()[0] for line in listing.stdout.splitlines() if line.startswith("ses_")]
    result: dict[str, list[str]] = {"imported": [], "existing": [], "failed": []}
    with tempfile.TemporaryDirectory(prefix="sarness-migrate-") as tmp:
        for sid in ids:
            path = Path(tmp) / f"{sid}.json"
            exported = subprocess.run([upstream, "session", "export", "--standalone", sid], **run)
            if exported.returncode != 0 or not exported.stdout.strip():
                result["failed"].append(sid)
                continue
            path.write_text(exported.stdout)
            imported = subprocess.run([str(binary), "session", "import", "--standalone", str(path)], **run)
            out = imported.stdout + imported.stderr
            if "already exists" in out:
                result["existing"].append(sid)
            elif imported.returncode == 0:
                result["imported"].append(sid)
            else:
                result["failed"].append(sid)
    return result
