"""`sarness setup` (guided or flag-driven) and `sarness doctor`."""
from __future__ import annotations

import getpass
import hashlib
import os
import shutil
import socket
import ssl
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import (ConfigError, EFFORTS, SAVEABLE_MODES, config_path, load_config, save_config,
                     secrets_path, set_secret, state_dir, effective_settings)
from .connection import Connection, get_profile
from .desktop import desktop_profiles, desktop_token, ssh_hosts
from .engine import EngineError
from .runtime import describe_model


def _ask(prompt: str, default: str = "", input_fn: Callable[[str], str] = input) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        return input_fn(f"{prompt}{suffix}: ").strip() or default
    except EOFError:
        return default


def fetch_fingerprint(host: str, port: int) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=10) as raw, ctx.wrap_socket(raw, server_hostname=host) as tls:
        return hashlib.sha256(tls.getpeercert(binary_form=True) or b"").hexdigest()


def read_token_file(path: str) -> str:
    p = Path(path).expanduser()
    if p.stat().st_mode & 0o077:
        raise ConfigError(f"{p} is readable by others; chmod 600 it first")
    return p.read_text(encoding="utf-8").strip()


def guess_ssh_target(host: str) -> str:
    for h in ssh_hosts():
        if h.get("hostname") == host or h.get("alias") == host:
            return h["alias"]
    return f"{os.environ.get('USER', 'user')}@{host}"


def run_setup(a: Any, say: Callable[[str], None] = lambda s: print(s, file=sys.stderr)) -> int:
    interactive = sys.stdin.isatty() and not a.yes
    ask = lambda p, d="": _ask(p, d, input) if interactive else d  # noqa: E731
    cfg = load_config()

    # ---- 1. which engine
    name = a.name
    choice: dict[str, Any] = {}
    if a.from_desktop:
        match = [p for p in desktop_profiles() if p.get("name") == a.from_desktop or p.get("id") == a.from_desktop]
        if len(match) != 1:
            raise ConfigError(f"No unique Sarness Desktop profile '{a.from_desktop}'. Known: {', '.join(p['name'] for p in desktop_profiles()) or 'none'}")
        choice = match[0]
    elif not (a.url or a.ssh or a.engine_host):
        if not interactive:
            raise ConfigError("Non-interactive setup needs --from-desktop NAME, or --ssh TARGET --engine-host H, or --url URL")
        cands = desktop_profiles()
        say("Sarness Engines I can see:")
        for i, p in enumerate(cands, 1):
            say(f"  {i}. {p['name']}  ({p.get('scheme', 'http')}://{p['host']}:{p.get('port', 8742)}, from Sarness Desktop)")
        say(f"  {len(cands) + 1}. Enter an address manually")
        pick = ask("Choose", "1")
        if pick.isdigit() and 1 <= int(pick) <= len(cands):
            choice = cands[int(pick) - 1]
    name = name or choice.get("name") or ask("Name for this server", "engine")
    profile: dict[str, Any] = {}
    host = a.engine_host or choice.get("host") or ""
    port = int(a.engine_port or choice.get("port") or 8742)

    # ---- 2. secure transport
    if a.url:
        transport = "https" if a.url.startswith("https://") else "http"
    elif a.ssh:
        transport = "ssh"
    else:
        if not host:
            host = ask("Engine host or IP")
        say(f"\nHow should Sarness reach {host}:{port}?\n  1. SSH tunnel (recommended: encrypted, uses your SSH keys)\n  2. HTTPS URL (certificate verified, or pinned)\n"
            "  3. Plain HTTP on the LAN (NOT secure: your token travels in clear text)")
        transport = {"1": "ssh", "2": "https", "3": "http"}.get(ask("Choose", "1"), "ssh")
    if transport == "ssh":
        target = a.ssh or ask("SSH target (user@host or an ~/.ssh/config alias)", guess_ssh_target(host))
        profile = {"transport": "ssh", "ssh": {"target": target, "engine_host": host or "127.0.0.1", "engine_port": port}}
    elif transport == "https":
        url = a.url or ask("HTTPS URL", f"https://{host}:{port}")
        profile = {"transport": "direct", "url": url.rstrip("/")}
        if a.tls_pin:
            profile["tls_pin"] = a.tls_pin.lower().replace(":", "")
        elif interactive:
            from urllib.parse import urlsplit
            u = urlsplit(url)
            if ask("Pin the server certificate fingerprint instead of using system CAs? (y/N)", "n").lower().startswith("y"):
                fp = fetch_fingerprint(u.hostname, u.port or 443)
                say(f"  server certificate SHA-256: {fp}")
                if ask("Trust exactly this certificate? (y/N)", "n").lower().startswith("y"):
                    profile["tls_pin"] = fp
    else:
        url = a.url or f"http://{host}:{port}"
        if not a.allow_insecure_http:
            if not interactive or ask("Type 'I understand' to send the token over plain HTTP", "") != "I understand":
                raise ConfigError("Refusing plain HTTP. Use an SSH tunnel (--ssh) or HTTPS, or pass --allow-insecure-http explicitly.")
        profile = {"transport": "direct", "url": url.rstrip("/"), "insecure_http": True}

    # ---- 3. token (never echoed, never on a command line)
    token = None
    if a.token_file:
        token = read_token_file(a.token_file)
    elif os.environ.get("SARNESS_TOKEN") and (a.token_env or not interactive):
        token = os.environ["SARNESS_TOKEN"].strip()
    elif a.token_from_desktop or (choice.get("id") and not a.no_desktop_token):
        token = desktop_token(choice.get("id", "")) if choice.get("id") else None
        if token:
            say("  token imported from the Sarness Desktop keyring (not displayed)")
    if not token and interactive:
        token = getpass.getpass("Sarness Engine token (hidden): ").strip()
    if not token:
        raise ConfigError("No token available. Use --token-file PATH, SARNESS_TOKEN with --token-env, import from Desktop, or run interactively.")

    # ---- 4. real connection test
    say("\nTesting the connection ...")
    probe_profile = {**profile}
    conn = Connection(name, probe_profile, token=token, allow_insecure_http=a.allow_insecure_http, interactive=sys.stdin.isatty())
    try:
        engine = conn.connect()
        health = engine.health()
        models = engine.models()
        st = engine.llm_status()
    except EngineError as exc:
        conn.close()
        raise ConfigError(f"Connection test failed: {exc}") from exc
    say(f"  ✓ Sarness Engine {health.get('engine_version')} (API {health.get('api_version')}) via {conn.description}")
    say(f"  ✓ token accepted, {len(models)} chat model(s) installed")
    loaded = st.get("model_id")
    say(f"  · currently loaded: {st.get('model_name') or loaded or 'nothing'}" + (f" (ctx {st.get('ctx')})" if loaded else ""))
    conn.close()

    # ---- 5. save profile + secret
    cfg["profiles"][name] = profile
    set_secret(name, token)
    settings = cfg["settings"]
    if len(cfg["profiles"]) == 1 or a.make_default or not settings.get("server"):
        settings["server"] = name
    # ---- 6. defaults
    chosen = a.model
    if not chosen and interactive:
        say("\nInstalled models:")
        for i, m in enumerate(models, 1):
            say(f"  {i:>2}. {describe_model(m, name, loaded)}")
        pick = ask("Default coding model (number, id, or Enter to decide later)", "")
        chosen = models[int(pick) - 1]["ref"] if pick.isdigit() and 1 <= int(pick) <= len(models) else pick
    if chosen:
        from .runtime import resolve_model
        chosen = resolve_model(models, chosen)["ref"]
        profile["model"] = chosen          # a model ref only makes sense for the engine that has it
    effort = a.effort or ask(f"Default effort ({'/'.join(EFFORTS)})", settings.get("effort", "medium"))
    mode = a.mode or ask(f"Default permissions ({'/'.join(SAVEABLE_MODES)})", settings.get("permissions", "auto"))
    if effort not in EFFORTS or mode not in SAVEABLE_MODES:
        raise ConfigError(f"effort must be one of {EFFORTS}, permissions one of {SAVEABLE_MODES}")
    settings["effort"], settings["permissions"] = effort, mode
    save_config(cfg)
    say(f"\n✓ saved server '{name}' (default model: {profile.get('model') or 'choose at launch'}, effort {effort}, permissions {mode})")
    say(f"  config  {config_path()}  (0600)\n  secrets {secrets_path()}  (0600)\nRun: sarness")
    return 0


# ----------------------------------------------------------------------------- doctor
def doctor(server: str | None, workspace: Path, say: Callable[[str], None] = print) -> int:
    results: list[tuple[str, str, str]] = []

    def check(status: str, name: str, detail: str = "") -> None:
        results.append((status, name, detail))
        icon = {"ok": "✓", "warn": "!", "fail": "✗"}[status]
        say(f"  {icon} {name}" + (f" - {detail}" if detail else ""))

    say(f"sarness doctor (cli {__version__})")
    check("ok" if sys.version_info >= (3, 11) else "fail", "Python", sys.version.split()[0])
    me = shutil.which("sarness")
    others = [str(Path(d) / "sarness") for d in os.environ.get("PATH", "").split(":") if d and (Path(d) / "sarness").exists()]
    if me:
        check("ok", "sarness on PATH", me)
        shadow = [o for o in others if os.path.realpath(o) != os.path.realpath(me)]
        if shadow:
            check("warn", "other 'sarness' executables on PATH", ", ".join(shadow) + " (the first on PATH wins)")
    else:
        check("warn", "sarness is not on PATH in this shell", "add ~/.local/bin to PATH")
    for label, path in (("config", config_path()), ("secrets", secrets_path())):
        if path.exists():
            bad = path.stat().st_mode & 0o077
            check("fail" if bad else "ok", f"{label} file permissions", f"{path} {oct(stat.S_IMODE(path.stat().st_mode))}" + (" - run chmod 600" if bad else ""))
    for tool in ("git", "ssh"):
        check("ok" if shutil.which(tool) else ("warn" if tool == "ssh" else "fail"), tool, shutil.which(tool) or "not found")
    try:
        import prompt_toolkit
        check("ok", "prompt_toolkit", prompt_toolkit.__version__)
    except ImportError:
        check("warn", "prompt_toolkit missing", "line editing falls back to plain input()")
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True, mode=0o700)
    free = shutil.disk_usage(sd).free // 2**20
    check("ok" if free > 200 else "warn", "state directory", f"{sd} ({free} MiB free)")
    settings, origin = effective_settings(workspace)
    try:
        name, prof = get_profile(server or settings["server"])
    except ConfigError as exc:
        check("fail", "server profile", str(exc))
        return 1
    check("ok", "server profile", f"{name} ({prof.get('transport')})")
    t0 = time.time()
    try:
        conn = Connection(name, prof, interactive=False)
        engine = conn.connect()
        check("ok", "transport", conn.description)
        health = engine.health()
        check("ok", "engine reachable", f"v{health.get('engine_version')} API {health.get('api_version')} in {(time.time() - t0) * 1000:.0f} ms")
        models = engine.models()
        check("ok", "token + /v1/agent/models", f"{len(models)} chat models")
        st = engine.llm_status()
        check("ok", "loaded model", f"{st.get('model_name') or st.get('model_id') or 'none'} (state {st.get('state')}, ctx {st.get('ctx')})")
        want = settings.get("model")
        if want:
            found = any(m["ref"] == want for m in models)
            check("ok" if found else "fail", "default model installed", want if found else f"{want} is not on this server; run: sarness model")
        conn.close()
    except (EngineError, ConfigError) as exc:
        check("fail", "engine connection", str(exc))
    fails = sum(1 for r in results if r[0] == "fail")
    warns = sum(1 for r in results if r[0] == "warn")
    say(f"\n{'All checks passed' if not fails else str(fails) + ' problem(s)'}{f', {warns} warning(s)' if warns else ''}.")
    return 1 if fails else 0
