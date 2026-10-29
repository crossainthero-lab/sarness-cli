"""Paths, private JSON files, layered settings and server profiles.

Precedence (highest first): command-line flag, environment, project settings
(`.sarness/settings.json`, non-secret and de-privileged), global config, defaults.
Tokens never live in the config file: they are kept in `secrets.json` (0600).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

APP = "sarness-cli"


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / APP


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def sessions_root() -> Path:
    """Where sessions live: the `session_dir` setting if set, else the XDG state directory."""
    configured = load_config()["settings"].get("session_dir")
    return Path(configured).expanduser() if configured else state_dir() / "sessions"


def config_path() -> Path:
    return config_dir() / "config.json"


def secrets_path() -> Path:
    return config_dir() / "secrets.json"


EFFORTS = ("low", "medium", "high", "max")
PERMISSION_MODES = ("ask", "auto", "full-access", "bypass")
SAVEABLE_MODES = ("ask", "auto", "full-access")
PROJECT_MODES = ("ask", "auto")  # a repository must not be able to grant itself more
THINKING = ("auto", "on", "off")

DEFAULTS: dict[str, Any] = {
    "server": None,
    "model": None,
    "effort": "medium",
    "thinking": "auto",
    "permissions": "auto",
    "max_output_tokens": None,   # None: derived from effort
    "context_tokens": None,      # None: whatever the engine has loaded / the model default
    "max_steps": 60,
    "command_timeout_s": 120,
    "request_timeout_s": 300,
    "retries": 3,
    "workspace": None,
    "show_reasoning": False,
    "log_level": "basic",        # off | basic | verbose
    "session_dir": None,
}
# Setting -> (validator description, check)
_CHECKS = {
    "effort": lambda v: v in EFFORTS,
    "thinking": lambda v: v in THINKING,
    "permissions": lambda v: v in SAVEABLE_MODES,
    "log_level": lambda v: v in ("off", "basic", "verbose"),
    "show_reasoning": lambda v: isinstance(v, bool),
    "max_steps": lambda v: isinstance(v, int) and 1 <= v <= 1000,
    "command_timeout_s": lambda v: isinstance(v, int) and 1 <= v <= 3600,
    "request_timeout_s": lambda v: isinstance(v, int) and 10 <= v <= 3600,
    "retries": lambda v: isinstance(v, int) and 0 <= v <= 10,
    "max_output_tokens": lambda v: v is None or (isinstance(v, int) and 256 <= v <= 131072),
    "context_tokens": lambda v: v is None or (isinstance(v, int) and 2048 <= v <= 1048576),
    "server": lambda v: v is None or isinstance(v, str),
    "model": lambda v: v is None or (isinstance(v, str) and v),
    "workspace": lambda v: v is None or isinstance(v, str),
    "session_dir": lambda v: v is None or isinstance(v, str),
}
PROJECT_KEYS = {"model", "effort", "thinking", "permissions", "max_output_tokens", "context_tokens",
                "max_steps", "command_timeout_s", "show_reasoning"}


class ConfigError(Exception):
    pass


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_json(path: Path, *, private: bool = True) -> Any:
    if not path.exists():
        return {}
    if private and path.stat().st_mode & 0o077:
        raise ConfigError(f"Private file has unsafe permissions: {path} (run: chmod 600 '{path}')")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc


def validate_setting(key: str, value: Any) -> Any:
    if key not in DEFAULTS:
        raise ConfigError(f"Unknown setting '{key}'. Known: {', '.join(sorted(DEFAULTS))}")
    if key == "permissions" and value == "bypass":
        raise ConfigError("'bypass' can never be saved as a default; pass --dangerously-skip-permissions per run")
    if not _CHECKS[key](value):
        raise ConfigError(f"Invalid value for {key}: {value!r}")
    return value


def coerce_setting(key: str, text: str) -> Any:
    """Turn the command-line text of `sarness config set` into the setting's type."""
    if key not in DEFAULTS:
        raise ConfigError(f"Unknown setting '{key}'. Known: {', '.join(sorted(DEFAULTS))}")
    if text.lower() in {"none", "null", "default"} and DEFAULTS[key] is None or text.lower() == "null":
        return validate_setting(key, None)
    if isinstance(DEFAULTS[key], bool):
        if text.lower() not in {"true", "false", "on", "off", "1", "0"}:
            raise ConfigError(f"{key} must be true or false")
        return validate_setting(key, text.lower() in {"true", "on", "1"})
    if key in {"max_steps", "command_timeout_s", "request_timeout_s", "retries", "max_output_tokens", "context_tokens"}:
        try:
            return validate_setting(key, int(text))
        except ValueError:
            raise ConfigError(f"{key} must be an integer") from None
    return validate_setting(key, text)


# --------------------------------------------------------------------------- global config
def load_config() -> dict[str, Any]:
    """Global config, migrating the 0.1.0 {url, token} format on the fly (no write)."""
    raw = load_json(config_path())
    if "url" in raw and "profiles" not in raw:  # 0.1.0
        legacy = {"default": {"transport": "direct", "url": raw["url"], "legacy_token": raw.get("token")}}
        raw = {"version": 2, "profiles": legacy, "settings": {"server": "default"}}
    raw.setdefault("version", 2)
    raw.setdefault("profiles", {})
    raw.setdefault("settings", {})
    raw.setdefault("projects", {})
    return raw


def save_config(cfg: dict[str, Any]) -> None:
    clean = {k: v for k, v in cfg.items()}
    for p in clean.get("profiles", {}).values():
        p.pop("legacy_token", None)
    atomic_json(config_path(), clean)


# --------------------------------------------------------------------------- secrets
def get_secret(profile: str) -> str | None:
    value = load_json(secrets_path()).get(profile)
    return value if isinstance(value, str) and value else None


def set_secret(profile: str, token: str) -> None:
    data = load_json(secrets_path())
    data[profile] = token
    atomic_json(secrets_path(), data)


def delete_secret(profile: str) -> None:
    data = load_json(secrets_path())
    if data.pop(profile, None) is not None:
        atomic_json(secrets_path(), data)


# --------------------------------------------------------------------------- project settings
def find_project_file(start: Path) -> Path | None:
    cur = start.resolve()
    for d in (cur, *cur.parents):
        f = d / ".sarness" / "settings.json"
        if f.is_file():
            return f
        if (d / ".git").exists():
            break
    return None


def load_project_settings(start: Path) -> tuple[dict[str, Any], Path | None, list[str]]:
    """Repository-supplied settings are untrusted: only a whitelist, and never more than 'auto'."""
    f = find_project_file(start)
    if not f:
        return {}, None, []
    notes: list[str] = []
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f, [f"ignored {f}: {exc}"]
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in PROJECT_KEYS:
            notes.append(f"ignored project setting '{k}' (not allowed in a repository file)")
        elif k == "permissions" and v not in PROJECT_MODES:
            notes.append(f"ignored project permissions '{v}': a repository may only request {', '.join(PROJECT_MODES)}")
        else:
            try:
                out[k] = validate_setting(k, v)
            except ConfigError as exc:
                notes.append(f"ignored project setting: {exc}")
    return out, f, notes


def effective_settings(workspace: Path, overrides: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """Merged settings plus where each value came from."""
    cfg = load_config()
    merged, origin = dict(DEFAULTS), {k: "default" for k in DEFAULTS}
    layers: list[tuple[str, dict[str, Any]]] = [("global", cfg["settings"])]
    entry = cfg["projects"].get(str(workspace.resolve()), {})
    layers.append(("project (private)", entry))
    proj, _f, _n = load_project_settings(workspace)
    layers.append(("project file", proj))
    env = {}
    for k, var in (("server", "SARNESS_SERVER"), ("model", "SARNESS_MODEL"), ("effort", "SARNESS_EFFORT")):
        if os.environ.get(var):
            env[k] = os.environ[var]
    layers.append(("environment", env))
    layers.append(("flag", {k: v for k, v in (overrides or {}).items() if v is not None}))
    # The default model belongs to a server (model refs differ between engines): it sits between global and project.
    def apply(name: str, layer: dict[str, Any]) -> None:
        for k, v in layer.items():
            if k in DEFAULTS:
                try:
                    merged[k] = validate_setting(k, v)
                    origin[k] = name
                except ConfigError:
                    continue
    apply(*layers[0])
    server = (overrides or {}).get("server") or env.get("server") or merged["server"]
    prof_model = cfg["profiles"].get(server or "", {}).get("model") if server else None
    if prof_model:
        apply(f"server '{server}'", {"model": prof_model})
    for name, layer in layers[1:]:
        apply(name, layer)
    return merged, origin


def update_settings(changes: dict[str, Any], *, workspace: Path | None = None, scope: str = "global", server: str | None = None) -> str:
    """Persist settings. scope: global | server (the model of one engine profile) | project (private map keyed by absolute path)."""
    for k, v in changes.items():
        validate_setting(k, v)
    cfg = load_config()
    if scope == "server" and server and server in cfg["profiles"] and set(changes) == {"model"}:
        cfg["profiles"][server]["model"] = changes["model"]
        where = f"server '{server}'"
    elif scope == "project" and workspace is not None:
        key = str(workspace.resolve())
        cfg["projects"].setdefault(key, {}).update({k: v for k, v in changes.items() if k in PROJECT_KEYS})
        where = f"project {key}"
    else:
        cfg["settings"].update(changes)
        where = "global config"
    save_config(cfg)
    return where


def project_scope(workspace: Path) -> str:
    """Where should /model-style remembered choices go for this workspace."""
    return "project" if str(workspace.resolve()) in load_config()["projects"] else "global"
