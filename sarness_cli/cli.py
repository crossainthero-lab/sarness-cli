"""Command-line entry point."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .agent import Interrupted, Session, SessionBlocked
from .config import (ConfigError, DEFAULTS, EFFORTS, THINKING, coerce_setting, config_dir, config_path,
                     effective_settings, load_config, load_project_settings, project_scope, save_config, secrets_path, update_settings,
                     get_secret, delete_secret)
from .connection import Connection, LiveEngine, find_profile, get_profile
from .engine import EngineError, explain_engine_error
from .menu import select_model
from .modelinfo import cache_models, load_cached_models, record_probe
from .repl import Repl
from .runtime import ModelError, Prepared, build_run_config, describe_model, prepare_model, resolve_model
from .setup import doctor, run_setup
from .tools import Workspace
from .ui import Console, banner_bypass
from .policy import EffectivePermissions

COMMANDS = {"setup", "models", "model", "config", "doctor", "sessions", "resume", "run", "server", "upgrade", "cabinet"}


def common_flags(p: argparse.ArgumentParser, suppress: bool) -> None:
    d = argparse.SUPPRESS if suppress else None
    p.add_argument("--server", default=d, help="Named Sarness Engine profile (see 'sarness server list')")
    p.add_argument("--model", default=d, help="Model ref, name or unique fragment (see 'sarness models')")
    p.add_argument("--effort", choices=EFFORTS, default=d, help="Reasoning/agent effort strategy")
    p.add_argument("--thinking", choices=THINKING, default=d, help="Model thinking mode (models that support it)")
    p.add_argument("--mode", default=d, metavar="{ask,auto,full-access}", help="Permission mode for this run")
    p.add_argument("--dangerously-skip-permissions", action="store_true", default=d if suppress else False,
                   help="Unrestricted execution: skip all Sarness permission approvals and denials (OS permissions still apply)")
    p.add_argument("-C", "--workspace", default=d, help="Project folder on THIS computer (default: current directory)")
    p.add_argument("--max-tokens", type=int, default=d, help="Maximum output tokens per model turn")
    p.add_argument("--max-steps", type=int, default=d, help="Maximum model turns per task (0 = unlimited; default: unlimited)")
    p.add_argument("--allow-insecure-http", action="store_true", default=d if suppress else False, help="Permit cleartext remote HTTP (token exposure risk)")
    p.add_argument("--allow-model-swap", action="store_true", default=d if suppress else False,
                   help="Allow loading a model over a different loaded one (it unloads for everyone)")
    p.add_argument("--allow-low-memory", action="store_true", default=d if suppress else False,
                   help="Load a model even if the engine host lacks free memory for it (can starve other workloads)")
    p.add_argument("--show-reasoning", action="store_true", default=d if suppress else False, help="Stream the model's reasoning text dimly")
    p.add_argument("--verbose", action="store_true", default=d if suppress else False, help="Show token usage and tool output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sarness", description="Terminal coding agent powered by your Sarness Engine (inference remote, tools local).",
                                     epilog="Run 'sarness setup' once, then just 'sarness' inside a project.")
    parser.add_argument("--version", action="version", version=f"sarness-cli {__version__}")
    common_flags(parser, suppress=False)
    parser.add_argument("-p", "--print", dest="prompt_opt", metavar="TASK", help="Run one task non-interactively and exit")
    parser.add_argument("-c", "--continue", dest="continue_last", action="store_true", help="Continue the most recent session for this workspace")
    parser.add_argument("-r", "--resume", nargs="?", const="", default=None, metavar="SESSION",
                        help="Resume a previous session (interactively or by session id)")
    sp = parser.add_subparsers(dest="command")
    parents = argparse.ArgumentParser(add_help=False)
    common_flags(parents, suppress=True)

    s = sp.add_parser("setup", parents=[parents], help="Connect to a Sarness Engine (guided)")
    s.add_argument("--name"); s.add_argument("--from-desktop", metavar="NAME", help="Reuse a Sarness Desktop profile (address + keyring token)")
    s.add_argument("--ssh", metavar="TARGET", help="SSH target for a tunnel, e.g. user@host"); s.add_argument("--engine-host"); s.add_argument("--engine-port", type=int)
    s.add_argument("--url", help="HTTPS engine URL"); s.add_argument("--tls-pin", help="SHA-256 fingerprint to pin")
    s.add_argument("--token-file"); s.add_argument("--token-env", action="store_true", help="Read the token from $SARNESS_TOKEN")
    s.add_argument("--token-from-desktop", action="store_true"); s.add_argument("--no-desktop-token", action="store_true")
    s.add_argument("--make-default", action="store_true"); s.add_argument("--yes", action="store_true", help="Never prompt")
    sp.add_parser("models", parents=[parents], help="List installed models").add_argument("--probe", action="store_true", help="Test tool calling on the ALREADY LOADED model")
    sp.choices["models"].add_argument("--json", action="store_true")
    m = sp.add_parser("model", parents=[parents], help="Show or set the default model"); m.add_argument("id", nargs="?"); m.add_argument("--project", action="store_true")
    c = sp.add_parser("config", parents=[parents], help="Inspect or change settings")
    c.add_argument("action", nargs="?", choices=["show", "get", "set", "unset", "path"], default="show"); c.add_argument("key", nargs="?"); c.add_argument("value", nargs="?")
    c.add_argument("--project", action="store_true", help="Apply to this workspace only")
    sp.add_parser("doctor", parents=[parents], help="Diagnose installation and connection")
    sp.add_parser("sessions", parents=[parents], help="List saved sessions")
    r = sp.add_parser("resume", parents=[parents], help="Resume a saved session"); r.add_argument("session", nargs="?"); r.add_argument("prompt", nargs="*")
    ru = sp.add_parser("run", parents=[parents], help="Run one task non-interactively"); ru.add_argument("prompt", nargs="*")
    sv = sp.add_parser("server", parents=[parents], help="Manage engine profiles"); sv.add_argument("action", choices=["list", "use", "remove"]); sv.add_argument("name", nargs="?")
    up = sp.add_parser("upgrade", parents=[parents], help="Reinstall from the recorded source directory"); up.add_argument("--pull", action="store_true")
    cab = sp.add_parser("cabinet", parents=[parents], help="Inspect server memory, view and load/unload models")
    cab.add_argument("subcommand", nargs="*", help="status, unload, release, load <id>, refresh")
    return parser


def resolve_workspace(a: argparse.Namespace) -> Path:
    """--workspace, else the `workspace` setting, else the current directory."""
    chosen = getattr(a, "workspace", None) or load_config()["settings"].get("workspace") or os.getcwd()
    return Path(chosen).expanduser().resolve()


def say(msg: str = "") -> None:
    print(msg)


def overrides(a: argparse.Namespace) -> dict[str, Any]:
    o: dict[str, Any] = {k: getattr(a, k, None) for k in ("server", "effort", "thinking", "max_steps")}
    o["max_output_tokens"] = getattr(a, "max_tokens", None)
    if getattr(a, "show_reasoning", False):
        o["show_reasoning"] = True
    if getattr(a, "model", None):
        o["model"] = a.model
    mode = getattr(a, "mode", None)
    if mode:
        if mode == "bypass":
            raise ConfigError("Bypass is never selected with --mode. Use --dangerously-skip-permissions (this run only) or /permissions bypass.")
        if mode not in ("ask", "auto", "full-access"):
            raise ConfigError("--mode must be ask, auto or full-access")
        o["permissions"] = mode
    return o


def open_connection(a: argparse.Namespace, settings: dict[str, Any]) -> tuple[str, Connection]:
    name, prof = get_profile(getattr(a, "server", None) or settings["server"])
    return name, Connection(name, prof, request_timeout=settings["request_timeout_s"], allow_insecure_http=getattr(a, "allow_insecure_http", False),
                            interactive=sys.stdin.isatty())


def pick_model(models: list[dict[str, Any]], settings: dict[str, Any], server: str, console: Console, loaded: str | None) -> dict[str, Any]:
    if settings.get("model"):
        m = resolve_model(models, settings["model"])
        if m.get("category") and m.get("category") != "llm":
            raise ConfigError(f"Cannot chat with '{m.get('name', m.get('ref'))}': it is a {m['category']} model, not an LLM. Sarness CLI only supports LLM text/chat models.")
        return m
    if len(models) == 1:
        m = models[0]
        if m.get("category") and m.get("category") != "llm":
            raise ConfigError(f"Cannot chat with '{m.get('name', m.get('ref'))}': it is a {m['category']} model, not an LLM. Sarness CLI only supports LLM text/chat models.")
        return m
    if not sys.stdin.isatty():
        raise ConfigError("No default model chosen. Pass --model or run 'sarness model <id>'.")
    try:
        chosen = select_model(models, None, server)
        if chosen:
            return chosen
    except Exception:
        pass
    for i, m in enumerate(models, 1):
        console.err(f"{i:>2}. {describe_model(m, server, loaded)}")
    pick = console.input_fn("Choose a model (number): ").strip()
    if not pick.isdigit() or not 1 <= int(pick) <= len(models):
        raise ConfigError("No model selected")
    return models[int(pick) - 1]


def start_session(a: argparse.Namespace, prompt: str | None, resume_id: str | None = None) -> int:
    workspace = resolve_workspace(a)
    settings, origin = effective_settings(workspace, overrides(a))
    saved_bypass = False
    if resume_id:
        sessions = Session.list_sessions(500)
        saved = next((s for s in sessions if s["id"] == resume_id), None)
        saved_bypass = bool(saved and saved.get("permissions") == "bypass" and not getattr(a, "mode", None))
        if saved and saved.get("workspace"):
            workspace = Path(saved["workspace"]).resolve()
            a.workspace = str(workspace)
            settings, origin = effective_settings(workspace, overrides(a))
            if saved.get("model") and not getattr(a, "model", None):
                settings["model"] = saved["model"]
            if saved.get("effort") and not getattr(a, "effort", None):
                settings["effort"] = saved["effort"]
            if saved.get("thinking") and not getattr(a, "thinking", None):
                settings["thinking"] = saved["thinking"]
    _, _, notes = load_project_settings(workspace)
    console = Console(show_reasoning=settings["show_reasoning"], verbose=getattr(a, "verbose", False) or settings["log_level"] == "verbose")
    for n in notes:
        console.notice(n, "warn")
    permissions = EffectivePermissions.for_launch(
        workspace, settings["permissions"], bypass=getattr(a, "dangerously_skip_permissions", False) or saved_bypass)
    mode = permissions.mode

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if prompt is None and not interactive and not resume_id:
        prompt = sys.stdin.read().strip() or None
        if not prompt:
            raise ConfigError("Nothing to do: pass a task (sarness run \"...\" / -p \"...\") or use an interactive terminal")

    is_scripted = (prompt is not None) or (resume_id and not interactive)

    # 1. Resolve server profile
    srv_arg = getattr(a, "server", None) or settings.get("server")
    name, prof, prof_err = find_profile(srv_arg)

    conn: Connection | None = None
    if name and prof:
        try:
            conn = Connection(name, prof, request_timeout=settings["request_timeout_s"],
                              allow_insecure_http=getattr(a, "allow_insecure_http", False),
                              interactive=interactive)
        except ConfigError as exc:
            prof_err = str(exc)

    engine_obj: Any = None
    models: list[dict[str, Any]] = []
    loaded: str | None = None
    model: dict[str, Any] | None = None
    prepared: Prepared | None = None
    connected = False
    connection_error: str | None = prof_err

    if conn and not prof_err:
        try:
            eng = conn.connect()
            probe_timeout = settings["request_timeout_s"] if is_scripted else 3.0
            old_timeout = eng.timeout
            eng.timeout = probe_timeout
            try:
                eng.health()
            finally:
                eng.timeout = old_timeout

            models = eng.models()
            cache_models(name, models)
            loaded = eng.llm_status().get("model_id")
            connected = True
            engine_obj = eng
        except (EngineError, ConfigError, Exception) as exc:
            connection_error = str(exc)
            if is_scripted:
                if conn:
                    conn.close()
                console.err(console.st.red(f"Error: {exc}"))
                return 1
            connected = False

    # Offline fallback: load cached models
    if not connected and name:
        cached = load_cached_models(name)
        if cached:
            models = cached

    # Resolve model
    if connected and engine_obj:
        try:
            model = pick_model(models, settings, name, console, loaded)
            if model.get("supported") is False:
                raise ConfigError(f"{model['name']} is not supported by this engine: {model.get('unsupported_reason')}")
            prepared = prepare_model(engine_obj, model, wanted_ctx=settings.get("context_tokens"),
                                     allow_swap=mode == "bypass" or getattr(a, "allow_model_swap", False),
                                     allow_low_memory=mode == "bypass" or getattr(a, "allow_low_memory", False),
                                     confirm_swap=lambda msg: console.confirm(msg + "\nLoad it anyway?") is not False)
        except (EngineError, ModelError, ConfigError) as exc:
            if is_scripted:
                if conn:
                    conn.close()
                console.err(console.st.red(f"Error: {exc}"))
                return 1
            connected = False
            connection_error = str(exc)

    if not connected or not model or not prepared:
        # Construct fallback model & run config for offline REPL
        chosen_ref = settings.get("model")
        if chosen_ref and models:
            try:
                model = resolve_model(models, chosen_ref)
            except Exception:
                model = models[0] if models else None
        elif models:
            model = models[0]
        else:
            model = {"ref": chosen_ref or "none", "name": chosen_ref or "Not connected", "local": True}

        ctx_tokens = int(settings.get("context_tokens") or model.get("default_ctx") or 8192)
        prepared = Prepared(ctx=ctx_tokens, gpu_layers=None, pin=False, assumed=True, notes=[], will_load=False)

    server_name = name or "none"
    cfg = build_run_config(model, prepared, settings, server_name)
    cfg.log_level = "verbose" if console.verbose else cfg.log_level
    result_cap = max(3000, min(16000, int(prepared.ctx * 3 * 0.08)))
    token_redact = [conn.token] if conn and conn.token else []
    ws = Workspace(workspace, confirmer=console.confirm, permissions=permissions, command_timeout=settings["command_timeout_s"], result_cap=result_cap, redact=token_redact)

    session: Session
    try:
        if resume_id:
            session = Session.load(resume_id, ws, cfg, console)
        else:
            session = Session(ws, cfg, ui=console)
        session.swap_confirm = lambda msg: session.ws.mode == "bypass" or console.confirm(msg + "\nContinue anyway?") is not False
        live = LiveEngine(conn, replaces=prepared.replaces) if conn and connected else None

        if is_scripted:
            if not connected or not live:
                console.err(console.st.red(f"Error: Cannot reach server {server_name}: {connection_error}"))
                return 1
            if mode == "bypass":
                console.err(banner_bypass(console.st))
            for n in prepared.notes:
                console.notice(n)
            console.err(console.st.dim(f"{server_name} · {model['ref']} · effort {cfg.effort} · permissions {mode} · {workspace} · session {session.id[:8]}"))
            try:
                session.run(live, prompt, None)
                return 0
            except Interrupted:
                console.err(console.st.yellow("\n■ cancelled. State saved."))
                console.summary(session.verification())
                return 130
            except SessionBlocked as exc:
                console.err(console.st.red(f"\n■ stopped: {exc}"))
                console.summary(session.verification())
                return 1

        repl = Repl(session, live, console, models, settings, server_name, workspace,
                    project_scope(workspace), connected=connected, connection_error=connection_error, conn=conn)
        return repl.run()
    except (ConfigError, ValueError, FileNotFoundError, EngineError) as exc:
        console.err(console.st.red(f"Error: {exc}"))
        return 1
    finally:
        if conn and is_scripted:
            conn.close()


# ----------------------------------------------------------------------------- subcommands
def cmd_models(a: argparse.Namespace) -> int:
    workspace = resolve_workspace(a)
    settings, _ = effective_settings(workspace, overrides(a))
    name, conn = open_connection(a, settings)
    try:
        engine = conn.connect()
        models, st = engine.models(), engine.llm_status()
        cache_models(name, models)
        loaded = st.get("model_id")
        if getattr(a, "json", False):
            say(json.dumps({"server": name, "loaded": loaded, "models": models}, indent=2))
            return 0
        say(f"Server {conn.description}\nLoaded now: {st.get('model_name') or 'nothing'} (state {st.get('state')}, ctx {st.get('ctx')})\n")
        for m in models:
            mark = "*" if m["ref"] == settings.get("model") else " "
            say(f"{mark} {describe_model(m, name, loaded)}")
        say("\n* = your default. 'tools: unverified' means the engine does not report tool-calling support; Sarness never assumes it. "
            "Run 'sarness models --probe' to test the loaded model.")
        if getattr(a, "probe", False):
            return probe(engine, name, models, st)
        return 0
    except EngineError as exc:
        cached = load_cached_models(name)
        if cached and not getattr(a, "json", False):
            say(f"Server {name} is offline ({exc})\nShowing cached models from previous connection:\n")
            for m in cached:
                mark = "*" if m["ref"] == settings.get("model") else " "
                say(f"{mark} {describe_model(m, name, None)}")
            return 0
        say(f"Error: {exc}")
        return 1
    finally:
        conn.close()


def probe(engine: Any, server: str, models: list[dict[str, Any]], st: dict[str, Any]) -> int:
    """Tiny structured-tool-call test, only ever against the model that is ALREADY loaded."""
    if st.get("state") != "ready" or not st.get("model_id"):
        say("\nprobe skipped: no model is loaded and Sarness will not load one just to test it.")
        return 0
    ref = f"local:{st['model_id']}"
    tool = [{"type": "function", "function": {"name": "get_time", "description": "Returns the time.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]
    done = engine.completion(ref, [{"role": "user", "content": "Call the get_time function now. Do not answer in text."}], tool,
                             {"temperature": 0, "max_tokens": 256, "ctx": st.get("ctx"), "gpu_layers": st.get("gpu_layers")})
    ok = any((c.get("function") or {}).get("name") == "get_time" for c in done["message"].get("tool_calls") or [])
    record_probe(server, ref, ok, done["message"].get("content", "")[:100])
    say(f"\nprobe {ref}: structured tool call {'RECEIVED - tool calling works' if ok else 'NOT received - this model may not drive the agent'}")
    return 0 if ok else 1


def cmd_model(a: argparse.Namespace) -> int:
    workspace = resolve_workspace(a)
    settings, origin = effective_settings(workspace, overrides(a))
    if not a.id:
        if sys.stdin.isatty():
            name, prof, _ = find_profile(getattr(a, "server", None) or settings.get("server"))
            if name and prof:
                try:
                    conn = Connection(name, prof, request_timeout=settings["request_timeout_s"],
                                      allow_insecure_http=getattr(a, "allow_insecure_http", False), interactive=True)
                    try:
                        eng = conn.connect()
                        models = eng.models()
                        cache_models(name, models)
                    except Exception:
                        models = load_cached_models(name)
                    finally:
                        conn.close()
                    if models:
                        chosen = select_model(models, settings.get("model"), name)
                        if chosen:
                            where = update_settings({"model": chosen["ref"]}, workspace=workspace,
                                                    scope="project" if a.project else "server", server=name)
                            say(f"✓ default model for {where}: {chosen['ref']}  ({chosen.get('name')})")
                            return 0
                except Exception:
                    pass
        say(f"default model: {settings.get('model') or '(none chosen)'}  [{origin['model']}]")
        say("Change it:  sarness model <id>   |  list:  sarness models")
        return 0
    name, conn = open_connection(a, settings)
    try:
        eng = conn.connect()
        try:
            model = resolve_model(eng.models(), a.id)
        except ModelError as exc:
            all_m = eng.installed_models()
            target_str = a.id.strip().lower()
            match = next((m for m in all_m if m.get("id", "").lower() == target_str or m.get("name", "").lower() == target_str or f"local:{m.get('id', '')}".lower() == target_str), None)
            if match and match.get("category") and match["category"] != "llm":
                say(f"Error: Cannot chat with '{match.get('name', match.get('id'))}': it is a {match['category']} model, not an LLM. Sarness CLI only supports LLM text/chat models.")
                return 1
            raise exc
        if model.get("category") and model.get("category") != "llm":
            say(f"Error: Cannot set '{model.get('name', model.get('ref'))}' as default model: it is a {model['category']} model, not an LLM. Sarness CLI only supports LLM text/chat models.")
            return 1
    except (EngineError, ModelError) as exc:
        say(f"Error: {exc}")
        return 1
    finally:
        conn.close()
    where = update_settings({"model": model["ref"]}, workspace=workspace, scope="project" if a.project else "server", server=name)
    say(f"✓ default model for {where}: {model['ref']}  ({model['name']})")
    return 0


def cmd_config(a: argparse.Namespace) -> int:
    workspace = resolve_workspace(a)
    if a.action == "path":
        say(f"config  {config_path()}\nsecrets {secrets_path()}")
        return 0
    if a.action in {"set", "unset"}:
        if not a.key or (a.action == "set" and a.value is None):
            raise ConfigError("usage: sarness config set KEY VALUE   |   sarness config unset KEY")
        if a.key not in DEFAULTS:
            raise ConfigError(f"Unknown setting '{a.key}'. Known: {', '.join(sorted(DEFAULTS))}")
        if a.action == "unset":
            cfg = load_config()
            target = cfg["projects"].setdefault(str(workspace), {}) if a.project else cfg["settings"]
            target.pop(a.key, None)
            save_config(cfg)
            say(f"✓ unset {a.key}")
            return 0
        value = coerce_setting(a.key, a.value)
        where = update_settings({a.key: value}, workspace=workspace, scope="project" if a.project else "global")
        say(f"✓ {a.key} = {value!r}  ({where})")
        return 0
    settings, origin = effective_settings(workspace)
    if a.action == "get":
        say(json.dumps(settings.get(a.key)) if a.key in settings else "unknown setting")
        return 0
    cfg = load_config()
    say(f"Effective settings for {workspace}\n")
    for k in DEFAULTS:
        say(f"  {k:<20} {json.dumps(settings[k]):<28} [{origin[k]}]")
    say("\nServers:")
    for n, p in cfg["profiles"].items():
        tgt = p.get("ssh", {}).get("target") and f"ssh {p['ssh']['target']} -> {p['ssh'].get('engine_host')}:{p['ssh'].get('engine_port')}" or p.get("url")
        say(f"  {'*' if n == settings['server'] else ' '} {n:<14} {tgt}  token: {'stored' if get_secret(n) or p.get('legacy_token') else 'MISSING'}")
    if not cfg["profiles"]:
        say("  (none - run 'sarness setup')")
    say(f"\nFiles: {config_path()}  {secrets_path()}  (both 0600; tokens are never shown)")
    say("Change: sarness config set KEY VALUE [--project]   Bypass cannot be a global default; it is retained for explicitly resumed sessions.")
    return 0


def cmd_server(a: argparse.Namespace) -> int:
    cfg = load_config()
    if a.action == "list":
        for n, p in cfg["profiles"].items():
            say(f"{'*' if n == cfg['settings'].get('server') else ' '} {n}  ({p.get('transport')})")
        return 0
    if a.name not in cfg["profiles"]:
        raise ConfigError(f"Unknown server '{a.name}'. Known: {', '.join(cfg['profiles']) or 'none'}")
    if a.action == "use":
        cfg["settings"]["server"] = a.name
        save_config(cfg)
        say(f"✓ default server is now {a.name}")
    else:
        del cfg["profiles"][a.name]
        if cfg["settings"].get("server") == a.name:
            cfg["settings"]["server"] = None
        save_config(cfg)
        delete_secret(a.name)
        say(f"✓ removed {a.name} and its stored token")
    return 0


def cmd_sessions(a: argparse.Namespace) -> int:
    for s in Session.list_sessions():
        say(f"{s['id'][:8]}  {str(s['status']):<24} {s['name'] or '-':<32} {s['model']}  {s['workspace']}")
    return 0


def cmd_upgrade(a: argparse.Namespace) -> int:
    info = config_dir() / "install.json"
    try:
        src = json.loads(info.read_text())["source"]
    except (OSError, KeyError, json.JSONDecodeError):
        say("No recorded source directory. From the sarness-cli source folder run: ./install.sh")
        return 1
    if getattr(a, "pull", False):
        subprocess.run(["git", "-C", src, "pull", "--ff-only"], check=False)
    script = Path(src) / "install.sh"
    if not script.exists():
        say(f"{script} not found")
        return 1
    return subprocess.call([str(script)])


def cmd_cabinet(a: argparse.Namespace) -> int:
    workspace = resolve_workspace(a)
    settings, _ = effective_settings(workspace, overrides(a))
    permissions = EffectivePermissions.for_launch(
        workspace, settings["permissions"], bypass=getattr(a, "dangerously_skip_permissions", False))
    console = Console(show_reasoning=settings["show_reasoning"], verbose=getattr(a, "verbose", False) or settings["log_level"] == "verbose")
    cfg = load_config()
    server_name = getattr(a, "server", None) or settings.get("server") or cfg["settings"].get("server")
    prof = cfg.get("profiles", {}).get(server_name)
    if not prof:
        console.err(console.st.red(f"Server profile '{server_name}' not found. Run 'sarness setup'."))
        return 1

    conn = Connection(server_name, prof, request_timeout=settings["request_timeout_s"],
                      allow_insecure_http=getattr(a, "allow_insecure_http", False) or bool(prof.get("insecure_http")),
                      interactive=sys.stdin.isatty())
    try:
        eng = conn.connect()
        from .cabinet import run_cabinet
        args = getattr(a, "subcommand", None) or []
        run_cabinet(eng, console, server_name, args=args, bypass=permissions.mode == "bypass")
        return 0
    except Exception as exc:
        console.err(console.st.red(f"Error: {exc}"))
        return 1
    finally:
        conn.close()


def handle_resume(target_id: str | None, a: argparse.Namespace, prompt: str | None = None) -> int:
    ws = resolve_workspace(a)
    sessions = Session.list_sessions(500)
    if not sessions:
        raise FileNotFoundError("No saved sessions found.")

    sid: str | None = None
    if target_id and target_id.strip():
        sid = Session.find(target_id.strip())
    elif sys.stdin.isatty():
        from .menu import select_session
        sid = select_session(sessions)
        if not sid:
            say("Resume cancelled.")
            return 0
    else:
        mine = [s for s in sessions if s["workspace"] == str(ws.resolve())]
        if not mine:
            raise FileNotFoundError("No saved session for this workspace to resume")
        sid = mine[0]["id"]

    saved = next((s for s in sessions if s["id"] == sid), None)
    if saved and saved.get("workspace"):
        a.workspace = saved["workspace"]
    return start_session(a, prompt, resume_id=sid)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    a = parser.parse_args(argv)
    try:
        cmd = a.command
        if cmd == "setup":
            code = run_setup(a)
        elif cmd == "models":
            code = cmd_models(a)
        elif cmd == "model":
            code = cmd_model(a)
        elif cmd == "config":
            code = cmd_config(a)
        elif cmd == "doctor":
            workspace = resolve_workspace(a)
            code = doctor(getattr(a, "server", None), workspace)
        elif cmd == "sessions":
            code = cmd_sessions(a)
        elif cmd == "server":
            code = cmd_server(a)
        elif cmd == "upgrade":
            code = cmd_upgrade(a)
        elif cmd == "cabinet":
            code = cmd_cabinet(a)
        elif cmd == "run":
            code = start_session(a, " ".join(a.prompt).strip() or None)
        elif cmd == "resume":
            prompt = " ".join(a.prompt).strip() or None
            code = handle_resume(a.session, a, prompt=prompt)
        elif getattr(a, "resume", None) is not None:
            code = handle_resume(a.resume, a, prompt=a.prompt_opt)
        else:
            resume_id = None
            if a.continue_last:
                ws = str(resolve_workspace(a))
                mine = [s for s in Session.list_sessions(500) if s["workspace"] == ws]
                if not mine:
                    raise FileNotFoundError("No saved session for this workspace to continue")
                resume_id = mine[0]["id"]
            code = start_session(a, a.prompt_opt, resume_id)
    except KeyboardInterrupt:
        print("\nInterrupted. Session state is saved.", file=sys.stderr)
        raise SystemExit(130) from None
    except (ConfigError, ValueError, FileNotFoundError, OSError, EngineError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(code)


if __name__ == "__main__":
    main()
