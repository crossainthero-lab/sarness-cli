"""Interactive session: prompt_toolkit input (multiline, history, completion) and slash commands."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import has_completions
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.shortcuts import PromptSession
from prompt_toolkit.styles import Style as PTStyle

from . import __version__
from .agent import Interrupted, RunConfig, Session, SessionBlocked
from .config import (ConfigError, DEFAULTS, EFFORTS, PERMISSION_MODES, SAVEABLE_MODES, THINKING,
                     coerce_setting, config_path, effective_settings, load_config, save_config,
                     secrets_path, state_dir, update_settings)
from .connection import Connection, LiveEngine
from .engine import EngineError, explain_engine_error
from .menu import (select_effort, select_model, select_permissions, select_server,
                   select_session, select_thinking)
from .modelinfo import cache_models, capabilities, effort_report, load_cached_models
from .paste import PasteLexer, PasteManager
from .runtime import ModelError, Prepared, describe_model, prepare_model, resolve_model
from .ui import Console, banner_bypass, warning_bypass

HELP = """\
Sarness CLI — Slash Commands

Model & Inference
  /cabinet                   Inspect RAM/VRAM, view loaded models, load/unload
  /model [name|id]           Interactive model selector (or set by name/id)
  /effort [low|medium|high|max]  Interactive reasoning-effort selector
  /thinking [on|off|auto]    Configure model thinking/reasoning mode

Server & Diagnostics
  /server                    Inspect server status, view diagnostics, switch server
  /reconnect                 Reconnect to the Sarness Engine and refresh models
  /disconnect                Disconnect from server and enter offline mode

Workspace & Tools
  /status                    Show connection, model, context, effort, permissions, git
  /permissions [mode]        Set mode: ask | auto | full-access | bypass
  /tools                     List available workspace tools and their permission levels
  /diff                      Show git diff of workspace changes
  /plan                      Show current task plan / checklist
  /config [get|set|unset]    Inspect or change configuration settings

Session Management
  /new                       Start a fresh conversation session
  /clear                     Clear current conversation messages
  /sessions                  Browse and switch to saved sessions
  /history                   Display recent conversation turns and tool calls
  /resume [id|name]          Resume a specific saved session
  /rename <name>             Name the current session
  /compact                   Archive older messages while preserving context digest

General
  /help                      Show this help reference
  /exit                      Quit Sarness CLI (Ctrl-D also exits)

Navigation: Enter = send; Esc+Enter or \\+Enter = newline; Ctrl-C = cancel task / clear line.
"""

SLASH_COMMANDS = [
    ("/help", "Show help and available commands"),
    ("/cabinet", "Inspect server RAM/VRAM, view loaded models, load/unload"),
    ("/model", "Select or inspect model (terminal picker)"),
    ("/effort", "Set reasoning effort strategy (low, medium, high, max)"),
    ("/thinking", "Configure model reasoning / thinking mode (on, off, auto)"),
    ("/permissions", "Set permission mode (ask, auto, full-access, bypass)"),
    ("/server", "Inspect server connection, diagnose errors, switch server"),
    ("/status", "Show model, context, effort, permissions, and git status"),
    ("/config", "Inspect or update configuration settings"),
    ("/clear", "Clear current conversation messages"),
    ("/new", "Start a fresh session"),
    ("/sessions", "Browse and resume saved sessions"),
    ("/history", "Show recent conversation turns and tool calls"),
    ("/tools", "List available local tools and permission policies"),
    ("/reconnect", "Reconnect to Sarness Engine and refresh models"),
    ("/disconnect", "Disconnect from server and enter offline mode"),
    ("/diff", "Show git diff of workspace changes"),
    ("/plan", "Show task checklist and plan"),
    ("/compact", "Archive old messages, keep digest in context"),
    ("/rename", "Rename the current session"),
    ("/exit", "Quit Sarness CLI"),
]


class SlashCommandCompleter(Completer):
    def __init__(self, repl: Repl):
        self.repl = repl

    def get_completions(self, document, complete_event):  # noqa: ANN001
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        parts = text.split(None, 1)
        # Completing arguments for known commands
        if len(parts) > 1 or (len(parts) == 1 and text.endswith(" ")):
            cmd = parts[0].lower()
            arg_prefix = parts[1] if len(parts) > 1 else ""
            p = arg_prefix.lower()
            if cmd == "/effort":
                for eff in ("low", "medium", "high", "max"):
                    if eff.startswith(p):
                        yield Completion(eff, start_position=-len(arg_prefix), display=eff)
            elif cmd == "/cabinet":
                for act in ("status", "unload", "release", "load", "refresh"):
                    if act.startswith(p):
                        yield Completion(act, start_position=-len(arg_prefix), display=act)
            elif cmd == "/thinking":
                for th in ("auto", "on", "off"):
                    if th.startswith(p):
                        yield Completion(th, start_position=-len(arg_prefix), display=th)
            elif cmd == "/permissions":
                for pm in ("ask", "auto", "full-access", "bypass"):
                    if pm.startswith(p):
                        yield Completion(pm, start_position=-len(arg_prefix), display=pm)
            elif cmd == "/model":
                for m in self.repl.models:
                    ref = m.get("ref", "")
                    name = m.get("name", "")
                    if ref.lower().startswith(p) or name.lower().startswith(p):
                        yield Completion(ref, start_position=-len(arg_prefix), display=name or ref, display_meta=ref)
            elif cmd == "/config":
                for act in ("show", "get", "set", "unset", "path"):
                    if act.startswith(p):
                        yield Completion(act, start_position=-len(arg_prefix), display=act)
            return

        # Completing slash commands themselves
        prefix = text.lower()
        commands = self.repl.get_slash_commands()
        for cmd, desc in commands:
            if cmd.lower().startswith(prefix):
                yield Completion(cmd, start_position=-len(text), display=cmd, display_meta=desc)


class Repl:
    def __init__(self, session: Session, engine: Any, console: Console, models: list[dict[str, Any]],
                 settings: dict[str, Any], server: str, workspace: Path, scope_pref: str,
                 connected: bool = True, connection_error: str | None = None, conn: Connection | None = None):
        self.s = session
        self.engine = engine
        self.con = console
        self.models = list(models)
        self.settings = settings
        self.server = server
        self.workspace = workspace
        self.scope = scope_pref
        self.cfg: RunConfig = session.cfg
        self.max_steps = settings.get("max_steps", 60)
        self.connected = connected
        self.connection_error = connection_error
        self.conn = conn or (getattr(engine, "conn", None) if engine else None)
        self.paste_mgr = PasteManager()
        self._telemetry: dict[str, Any] = {
            "ram_used_gb": None,
            "ram_total_gb": None,
            "vram_used_gb": None,
            "vram_total_gb": None,
            "unified": False,
            "ts": 0.0,
        }
        self._fetch_telemetry_async(force=True)

    def _fetch_telemetry_async(self, force: bool = False) -> None:
        if not self.connected or not self.conn:
            return
        eng_obj = getattr(self.conn, "engine", None)
        if not eng_obj:
            return
        now = time.monotonic()
        if not force and (now - float(self._telemetry.get("ts", 0.0))) < 10.0:
            return
        self._telemetry["ts"] = now

        def _worker() -> None:
            try:
                data = eng_obj.system()
                hw = data.get("hardware", {})
                mem = hw.get("memory", {})
                gpus = hw.get("gpus", [])
                total_ram = mem.get("total_mb")
                avail_ram = mem.get("available_mb")
                used_ram = mem.get("used_mb")
                if used_ram is None and total_ram is not None and avail_ram is not None:
                    used_ram = total_ram - avail_ram

                is_unified = False
                v_total = None
                v_used = None
                if gpus:
                    g0 = gpus[0]
                    is_unified = bool(g0.get("unified_memory")) or (total_ram is not None and g0.get("vram_total_mb") == total_ram)
                    v_total = g0.get("vram_total_mb")
                    v_used = g0.get("vram_used_mb")

                self._telemetry["ram_used_gb"] = (used_ram / 1024.0) if used_ram is not None else None
                self._telemetry["ram_total_gb"] = (total_ram / 1024.0) if total_ram is not None else None
                self._telemetry["vram_used_gb"] = (v_used / 1024.0) if v_used is not None else None
                self._telemetry["vram_total_gb"] = (v_total / 1024.0) if v_total is not None else None
                self._telemetry["unified"] = is_unified
            except Exception:
                pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def get_slash_commands(self) -> list[tuple[str, str]]:
        out = []
        for cmd, desc in SLASH_COMMANDS:
            if not self.connected:
                if cmd == "/cabinet":
                    desc = "Server memory & models (offline: connection required)"
                elif cmd == "/model":
                    desc = "Select or inspect model (offline: view cached)"
                elif cmd == "/reconnect":
                    desc = "Connect to Sarness Engine"
                elif cmd in {"/diff", "/status", "/plan", "/history", "/tools", "/config", "/new", "/clear", "/sessions"}:
                    pass
            out.append((cmd, desc))
        return out

    # ------------------------------------------------------------------ input
    def _make_prompt(self):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        hist = state_dir() / "history"
        hist.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        kb = KeyBindings()

        @kb.add("down", filter=has_completions)
        def _down(event):  # noqa: ANN001
            event.current_buffer.complete_next()

        @kb.add("up", filter=has_completions)
        def _up(event):  # noqa: ANN001
            event.current_buffer.complete_previous()

        @kb.add("tab")
        def _tab(event):  # noqa: ANN001
            b = event.current_buffer
            if b.complete_state:
                b.complete_next()
            else:
                b.start_completion(select_first=True)

        @kb.add("escape", filter=has_completions)
        def _esc_comp(event):  # noqa: ANN001
            event.current_buffer.cancel_completion()

        @kb.add("enter", filter=has_completions)
        def _enter_comp(event):  # noqa: ANN001
            b = event.current_buffer
            if b.complete_state and b.complete_state.current_completion:
                b.apply_completion(b.complete_state.current_completion)
            else:
                b.cancel_completion()
                b.validate_and_handle()

        @kb.add("enter")
        def _(event):  # noqa: ANN001
            buf = event.current_buffer
            if buf.text.endswith("\\"):
                buf.delete_before_cursor(1)
                buf.insert_text("\n")
            else:
                buf.validate_and_handle()

        @kb.add("escape", "enter")
        @kb.add("c-j")
        def _nl(event):  # noqa: ANN001
            event.current_buffer.insert_text("\n")

        @kb.add(Keys.BracketedPaste)
        def _paste(event):  # noqa: ANN001
            data = event.data
            data = data.replace("\r\n", "\n").replace("\r", "\n")
            if self.paste_mgr.should_collapse(data):
                token = self.paste_mgr.register(data)
                event.current_buffer.insert_text(token)
            else:
                event.current_buffer.insert_text(data)

        @kb.add("c-e")
        @kb.add("escape", "e")
        def _expand(event):  # noqa: ANN001
            buf = event.current_buffer
            self.paste_mgr.expand_buffer(buf)

        pt_style = PTStyle.from_dict({
            "prompt-name": "#ffffff bold",
            "prompt-arrow": "#38bdf8 bold",
            "pasted-block": "#38bdf8 bold bg:#182234",
            "completion-menu": "bg:#242424 #e0e0e0",
            "completion-menu.completion": "bg:#2b2b2b #ffffff",
            "completion-menu.completion.current": "bg:#0284c7 #ffffff bold",
            "completion-menu.meta": "bg:#1e1e1e #888888",
            "completion-menu.meta.current": "bg:#0284c7 #d0d0d0",
            "bottom-toolbar": "bg:#18181b #d4d4d8",
            "toolbar-model": "#ffffff bold",
            "toolbar-effort": "#a1a1aa",
            "toolbar-server": "#e4e4e7",
            "toolbar-dot-connected": "#22c55e bold",
            "toolbar-dot-offline": "#f59e0b",
            "toolbar-memory": "#38bdf8",
            "toolbar-bypass": "#fbbf24 bold",
            "toolbar-mode": "#a1a1aa",
            "toolbar-context": "#71717a",
            "toolbar-sep": "#52525b",
        })

        return PromptSession(
            history=FileHistory(str(hist)),
            multiline=True,
            key_bindings=kb,
            bottom_toolbar=self.render_toolbar,
            completer=SlashCommandCompleter(self),
            complete_while_typing=True,
            lexer=PasteLexer(),
            style=pt_style,
            prompt_continuation=lambda w, n, s: [("class:prompt-arrow", "… ")],
        )

    def render_toolbar(self) -> FormattedText:
        now = time.monotonic()
        if self.connected and (now - float(self._telemetry.get("ts", 0.0))) > 15.0:
            self._fetch_telemetry_async()

        c = self.cfg
        # Model
        model_short = (c.model.split(":", 1)[-1] if c.model else "None")
        if not self.connected and c.model:
            model_short += " [offline]"

        # Effort
        effort_str = f"{c.effort.title()} effort"

        # Server & connection indicator
        server_name = self.server or "server"
        dot_style = "class:toolbar-dot-connected" if self.connected else "class:toolbar-dot-offline"
        dot_char = "●" if self.connected else "○"

        # Telemetry memory
        telem = self._telemetry
        ram_used = telem.get("ram_used_gb")
        ram_total = telem.get("ram_total_gb")
        vram_used = telem.get("vram_used_gb")
        vram_total = telem.get("vram_total_gb")
        unified = telem.get("unified", False)

        mem_parts = []
        if ram_used is not None and ram_total is not None:
            mem_parts.append(f"RAM {ram_used:.1f}/{ram_total:.1f} GB")
        if not unified and vram_used is not None and vram_total is not None:
            mem_parts.append(f"VRAM {vram_used:.1f}/{vram_total:.1f} GB")
        mem_str = " · ".join(mem_parts) if mem_parts else ""

        # Permission mode
        mode = self.s.ws.mode
        mode_style = "class:toolbar-bypass" if mode == "bypass" else "class:toolbar-mode"
        mode_display = "BYPASS" if mode == "bypass" else mode.title()

        # Session / context usage
        ctx_parts = []
        if self.s.stats.turns > 0:
            ctx_parts.append(f"{self.s.stats.turns} turns")
        if c.ctx:
            ctx_parts.append(f"ctx {c.ctx}")
        ctx_str = " · ".join(ctx_parts)

        # Assemble clean visual hierarchy
        res: list[tuple[str, str]] = [
            (mode_style, f" {mode_display}"),
            ("class:toolbar-sep", "  ·  "),
            ("class:toolbar-model", model_short),
            ("class:toolbar-sep", "  ·  "),
            ("class:toolbar-effort", effort_str),
            ("class:toolbar-sep", "  ·  "),
            ("class:toolbar-server", f"{server_name} "),
            (dot_style, dot_char),
        ]
        if mem_str:
            res.extend([
                ("class:toolbar-sep", "  ·  "),
                ("class:toolbar-memory", mem_str),
            ])
        if ctx_str:
            res.extend([
                ("class:toolbar-sep", "  ·  "),
                ("class:toolbar-context", f"{ctx_str} "),
            ])
        return FormattedText(res)

    def read(self, ps: Any) -> str | None:
        try:
            if ps:
                prompt_label = [("class:prompt-name", "sarness "), ("class:prompt-arrow", "❯ ")]
                raw = ps.prompt(prompt_label)
                expanded = self.paste_mgr.expand(raw)
                return expanded.strip()
            raw = input("\nsarness ❯ ").strip()
            return self.paste_mgr.expand(raw).strip()
        except EOFError:
            return None
        except KeyboardInterrupt:
            self.con.err(self.con.st.dim("(Ctrl-C clears the line; Ctrl-D or /exit quits)"))
            return ""

    # ------------------------------------------------------------------ main loop
    def run(self) -> int:
        c = self.con
        ps = self._make_prompt()

        if self.connected:
            c.err(c.st.bold(f"Sarness CLI {__version__}") + c.st.dim(f"  ·  {self.server}  ·  {self.workspace}"))
            c.err(c.st.dim(f"model {self.cfg.model} · effort {self.cfg.effort} · permissions {self.s.ws.mode} · session {self.s.id[:8]}  ·  /help"))
        else:
            c.err(c.st.bold(f"Sarness CLI {__version__}\n"))
            model_info = self.cfg.model if self.cfg.model and self.cfg.model != "none" else "Not connected"
            if self.models and any(m.get("cached") for m in self.models):
                model_info += " [cached]"
            c.err(f"Model:  {model_info}")
            c.err(f"Server: {c.st.yellow('Offline')} ({self.server})")
            c.err(c.st.dim("\nUnable to connect to the configured server."))
            c.err(c.st.dim("Check /server for details or /reconnect to retry.\n"))

        if len(self.s.messages) > 1:
            c.err(c.st.dim(f"Resumed session {self.s.id[:8]} ({len(self.s.messages)} messages) · {self.cfg.model} · {self.cfg.effort} effort"))

        if self.s.ws.mode == "bypass":
            c.err(banner_bypass(c.st))

        while True:
            line = self.read(ps)
            if line is None:
                break
            if not line:
                continue
            if line.startswith("/"):
                if self.command(line):
                    break
                continue
            self.task(line)

        bypass_flag = " --dangerously-skip-permissions" if self.s.ws.mode == "bypass" else ""
        c.err(c.st.dim(f"session {self.s.id} saved. Resume: sarness{bypass_flag} resume {self.s.name or self.s.id}"))
        return 0

    def task(self, prompt: str) -> None:
        if not self.connected or not self.engine:
            self.con.err(self.con.st.yellow(f"\n■ Cannot execute task: server '{self.server}' is offline."))
            if self.connection_error:
                self.con.err(self.con.st.dim(f"  Reason: {self.connection_error}"))
            self.con.err(self.con.st.dim("  Check /server for details or run /reconnect when the server is reachable."))
            return

        try:
            self.s.run(self.engine, prompt, None)
        except Interrupted:
            self.con.spin.stop()
            self.con.err(self.con.st.yellow("\n■ cancelled. State is saved; any command in flight was killed. Type a new instruction, or /resume later."))
        except SessionBlocked as exc:
            self.con.spin.stop()
            self.con.err(self.con.st.red(f"\n■ stopped: {exc}"))
            self.con.summary(self.s.verification())
        except EngineError as exc:
            self.con.spin.stop()
            if exc.kind in ("network", "timeout", "connection_refused", "server_unavailable"):
                self.connected = False
                self.connection_error = str(exc)
                self.con.err(self.con.st.red(f"\n■ Connection to server lost: {exc}"))
                self.con.err(self.con.st.dim("  Session state is preserved. Run /reconnect once the server recovers."))
            else:
                self.con.err(self.con.st.red(f"\n■ Engine error: {exc}"))
        except KeyboardInterrupt:
            self.con.spin.stop()
            self.con.err(self.con.st.yellow("\n■ cancelled."))

    # ------------------------------------------------------------------ commands
    def command(self, line: str) -> bool:
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        c = self.con
        try:
            if cmd in {"/exit", "/quit"}:
                return True
            if cmd == "/help":
                c.err(HELP)
            elif cmd == "/cabinet":
                self.cmd_cabinet(args)
            elif cmd == "/model":
                self.cmd_model(args)
            elif cmd == "/effort":
                self.cmd_effort(args)
            elif cmd == "/thinking":
                self.cmd_thinking(args)
            elif cmd == "/permissions":
                self.cmd_permissions(args)
            elif cmd == "/server":
                self.cmd_server(args)
            elif cmd == "/reconnect":
                self.cmd_reconnect()
            elif cmd == "/disconnect":
                self.cmd_disconnect()
            elif cmd == "/status":
                self.cmd_status()
            elif cmd == "/plan":
                c.plan(self.s.ws.plan) if self.s.ws.plan else c.err("No plan yet.")
            elif cmd == "/diff":
                r = subprocess.run(["git", "diff", "--no-color"], cwd=self.workspace, capture_output=True, text=True)
                c.print_diff(r.stdout, 400) if r.stdout else c.err("No uncommitted changes (or not a git repository).")
            elif cmd == "/compact":
                n = self.s.compact_now()
                c.err(f"archived {n} old messages; a digest of files/commands/plan stays in context." if n else "Nothing to compact yet.")
            elif cmd == "/clear":
                self.s.clear()
                c.err("Conversation cleared (previous session remains resumable). New session " + self.s.id[:8])
            elif cmd == "/new":
                self.cmd_new()
            elif cmd == "/sessions":
                self.cmd_sessions()
            elif cmd == "/history":
                self.cmd_history()
            elif cmd == "/tools":
                self.cmd_tools()
            elif cmd == "/resume":
                self.cmd_resume(args)
            elif cmd == "/rename":
                self.s.name = "-".join(args)[:48] or self.s.name
                self.s.save()
                c.err(f"session named '{self.s.name}'")
            elif cmd == "/config":
                self.cmd_config(args)
            else:
                c.err(f"Unknown command {cmd}. /help lists available commands.")
        except (ConfigError, ModelError, ValueError) as exc:
            c.err(c.st.red(f"! {exc}"))
        return False

    def _remember(self, changes: dict[str, Any]) -> None:
        try:
            scope = "project" if self.scope == "project" else ("server" if set(changes) == {"model"} else "global")
            where = update_settings(changes, workspace=self.workspace, scope=scope, server=self.server)
            self.con.err(self.con.st.dim(f"(remembered in {where})"))
        except ConfigError as exc:
            self.con.err(self.con.st.yellow(f"! not saved: {exc}"))

    # ------------------------------------------------------------------ command implementations
    def cmd_cabinet(self, args: list[str]) -> None:
        c = self.con
        if not self.connected or not self.conn or not getattr(self.conn, "engine", None):
            c.err(c.st.yellow(f"Not connected to server '{self.server}'. /cabinet requires an active server connection."))
            c.err(c.st.dim("Run /reconnect or /server to establish connection."))
            return

        from .cabinet import run_cabinet
        try:
            run_cabinet(self.conn.engine, self.con, self.server, repl=self, args=args)
        except Exception as exc:  # noqa: BLE001
            c.err(c.st.red(f"! Cabinet error: {exc}"))

    def cmd_model(self, args: list[str]) -> None:
        c = self.con
        # Refresh models from engine if connected
        if self.connected and self.engine and getattr(self.engine, "conn", None):
            try:
                fresh = self.engine.conn.engine.models()
                if fresh:
                    self.models = fresh
                    cache_models(self.server, fresh)
            except Exception:  # noqa: BLE001
                pass

        if not args:
            if not self.models and self.server:
                self.models = load_cached_models(self.server)

            if not self.models:
                c.err(c.st.yellow(f"No models found or cached for server '{self.server}'. Connect to the server to discover models."))
                return

            chosen = select_model(self.models, self.cfg.model, self.server, is_offline=not self.connected)
            if not chosen:
                c.err(c.st.dim("(model selection cancelled)"))
                return
            model = chosen
        else:
            try:
                model = resolve_model(self.models, " ".join(args))
            except ModelError as exc:
                if self.connected and self.engine:
                    try:
                        eng_obj = getattr(self.engine, "conn", None) and self.engine.conn.engine
                        if eng_obj:
                            all_m = eng_obj.installed_models()
                            target_str = " ".join(args).strip().lower()
                            match = next((m for m in all_m if m.get("id", "").lower() == target_str or m.get("name", "").lower() == target_str or f"local:{m.get('id', '')}".lower() == target_str), None)
                            if match and match.get("category") and match["category"] != "llm":
                                raise ModelError(f"Cannot chat with '{match.get('name', match.get('id'))}': it is a {match['category']} model, not an LLM. Sarness CLI only supports LLM text/chat models.")
                    except ModelError:
                        raise
                    except Exception:
                        pass
                raise exc

        from .runtime import check_chat_model
        check_chat_model(model)

        if model.get("supported") is False:
            raise ModelError(f"{model.get('name', model.get('ref'))} is not supported by this engine: {model.get('unsupported_reason') or 'unknown reason'}")

        if model["ref"] == self.cfg.model:
            c.err(f"Already using {model.get('name', model['ref'])} ({model['ref']})")
            return

        if not self.connected or not self.engine:
            self.cfg.model = model["ref"]
            self.cfg.caps = capabilities(model, self.server)
            self._remember({"model": model["ref"]})
            c.err(c.st.green(f"✓ preferred model → {model.get('name', model['ref'])}") +
                  c.st.dim(f"  ({model['ref']}) [saved; will load on server connect]"))
            return

        prepared = prepare_model(self.engine.conn.engine, model, wanted_ctx=self.settings.get("context_tokens"),
                                 allow_low_memory=False,
                                 confirm_swap=lambda msg: c.confirm(msg + "\nSwitch anyway?") is not False)
        old = self.cfg.model
        self.engine.replaces = prepared.replaces
        self.cfg.model, self.cfg.caps = model["ref"], capabilities(model, self.server)
        self.cfg.ctx, self.cfg.gpu_layers, self.cfg.pin_ctx = prepared.ctx, prepared.gpu_layers, prepared.pin
        self.s.ctx_shrink = 1.0
        self.s.messages.append({"role": "user", "content": f"[sarness] The model was switched from {old} to {model['ref']}. "
                                 "Earlier assistant turns came from the previous model; continue the same task."})
        self.s.save()
        c.err(c.st.green(f"✓ model → {model.get('name', model['ref'])}") +
              c.st.dim(f"  ({model['ref']})  ctx {prepared.ctx}  tools: {self.cfg.caps.tool_calling}"))
        for n in prepared.notes:
            c.err(c.st.dim("  · " + n))
        if self.cfg.caps.tools_reported is not True and self.cfg.caps.tools_probed is not True:
            c.err(c.st.yellow("  ! tool calling for this model is unverified; if it writes calls as text the session will stop and say so."))
        self._remember({"model": model["ref"]})

    def cmd_effort(self, args: list[str]) -> None:
        if not args:
            chosen = select_effort(self.cfg.effort, self.cfg.caps)
            if not chosen:
                self.con.err(self.con.st.dim("(effort unchanged)"))
                return
            args = [chosen]

        eff = args[0].lower()
        if eff not in EFFORTS:
            raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
        self.cfg.effort = eff
        self._remember({"effort": eff})
        self.con.err(self.con.st.green(f"✓ reasoning effort → {eff.title()}"))
        for line in effort_report(self.cfg.caps, self.cfg.effort, self.cfg.thinking, self.cfg.ctx, self.cfg.user_max_tokens):
            self.con.err(line)

    def cmd_thinking(self, args: list[str]) -> None:
        if not args:
            chosen = select_thinking(self.cfg.thinking, self.cfg.caps)
            if not chosen:
                self.con.err(self.con.st.dim("(thinking mode unchanged)"))
                return
            args = [chosen]

        th = args[0].lower()
        if th not in THINKING:
            raise ValueError("usage: /thinking on|off|auto")
        self.cfg.thinking = th
        self._remember({"thinking": th})
        self.con.err(self.con.st.green(f"✓ thinking → {th}") + f"  (model toggle: {self.cfg.caps.thinking_toggle})")

    def cmd_permissions(self, args: list[str]) -> None:
        c = self.con
        save = "--save" in args
        args = [a for a in args if a != "--save"]

        if not args:
            chosen = select_permissions(self.s.ws.mode)
            if not chosen:
                c.err(c.st.dim("(permissions unchanged)"))
                return
            args = [chosen]

        mode = args[0]
        if mode not in PERMISSION_MODES:
            raise ValueError(f"mode must be one of {', '.join(PERMISSION_MODES)}")

        if mode == "bypass" and self.s.ws.mode != "bypass":
            c.err(warning_bypass(c.st))
            try:
                if c.input_fn("Type 'bypass' to enable for THIS session only: ").strip() != "bypass":
                    c.err("Not enabled.")
                    return
            except (EOFError, KeyboardInterrupt):
                return

        self.s.ws.mode = mode
        self.s.ws.remembered.clear()
        c.err(c.st.green(f"✓ permissions → {mode}") + c.st.dim("  (this session only)" if mode == "bypass" or not save else ""))
        if save and mode in SAVEABLE_MODES:
            self._remember({"permissions": mode})
        elif save:
            c.err(c.st.yellow("! bypass is never saved as a default"))

    def cmd_server(self, args: list[str]) -> None:
        c = self.con
        cfg = load_config()
        profiles = cfg.get("profiles", {})

        if args and args[0] in profiles:
            self._switch_server(args[0])
            return

        choice = select_server(profiles, self.server, self.connected)
        if not choice:
            return

        if choice == "action:reconnect":
            self.cmd_reconnect()
        elif choice == "action:disconnect":
            self.cmd_disconnect()
        elif choice == "action:diagnostics":
            self.cmd_server_diagnostics()
        elif choice.startswith("server:"):
            target_srv = choice.split(":", 1)[1]
            self._switch_server(target_srv)

    def _switch_server(self, name: str) -> None:
        c = self.con
        cfg = load_config()
        if name not in cfg.get("profiles", {}):
            c.err(c.st.red(f"Unknown server '{name}'"))
            return

        c.err(c.st.dim(f"Switching server to {name}..."))
        if self.conn:
            self.conn.close()

        self.server = name
        prof = cfg["profiles"][name]
        try:
            self.conn = Connection(name, prof, request_timeout=self.settings["request_timeout_s"],
                                   allow_insecure_http=bool(prof.get("insecure_http")), interactive=sys.stdin.isatty())
        except Exception as exc:
            self.connected = False
            self.connection_error = str(exc)
            c.err(c.st.yellow(f"! switched to {name}, but configuration has error: {exc}"))
            return

        self.cmd_reconnect()

    def cmd_server_diagnostics(self) -> None:
        c = self.con
        cfg = load_config()
        prof = cfg.get("profiles", {}).get(self.server, {})
        tgt = (prof.get("ssh", {}).get("target") and f"ssh {prof['ssh']['target']} -> {prof['ssh'].get('engine_host')}:{prof['ssh'].get('engine_port')}") or prof.get("url") or "none"

        c.err(c.st.bold("Server Diagnostics"))
        c.err(f"  Server Profile:   {self.server}")
        c.err(f"  Target Endpoint:  {tgt}")
        c.err(f"  Transport Type:   {prof.get('transport', 'direct')}")
        c.err(f"  Connection State: {c.st.green('Connected') if self.connected else c.st.yellow('Offline')}")

        diag = explain_engine_error(
            EngineError(self.connection_error) if self.connection_error else None,
            url=tgt,
            server=self.server,
        )
        c.err(f"  Diagnosis:        {diag['category']} — {diag['summary']}")
        c.err(f"  Details:          {diag['detail']}")
        c.err(f"  Suggestion:       {c.st.cyan(diag['suggestion'])}")

    def cmd_reconnect(self) -> None:
        c = self.con
        c.err(c.st.dim(f"Connecting to {self.server}..."))
        if not self.conn:
            cfg = load_config()
            prof = cfg.get("profiles", {}).get(self.server)
            if not prof:
                c.err(c.st.red(f"Server profile '{self.server}' not found."))
                return
            try:
                self.conn = Connection(self.server, prof, request_timeout=self.settings["request_timeout_s"],
                                       allow_insecure_http=bool(prof.get("insecure_http")), interactive=sys.stdin.isatty())
            except Exception as exc:
                self.connected = False
                self.connection_error = str(exc)
                c.err(c.st.red(f"Connection setup failed: {exc}"))
                return

        try:
            eng = self.conn.connect()
            t0 = time.monotonic()
            eng.health()
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            models = eng.models()
            cache_models(self.server, models)
            self.models = models
            loaded = eng.llm_status().get("model_id")

            # Setup live engine
            self.engine = LiveEngine(self.conn)
            self.connected = True
            self.connection_error = None

            # Resolve model
            target_model = None
            if self.cfg.model and self.cfg.model != "none":
                try:
                    target_model = resolve_model(models, self.cfg.model)
                except Exception:
                    pass
            if not target_model and models:
                target_model = models[0]

            if target_model:
                try:
                    prep = prepare_model(eng, target_model, wanted_ctx=self.settings.get("context_tokens"),
                                         allow_swap=False, allow_low_memory=False, confirm_swap=None)
                    self.cfg.model = target_model["ref"]
                    self.cfg.caps = capabilities(target_model, self.server)
                    self.cfg.ctx = prep.ctx
                except Exception:
                    self.cfg.model = target_model["ref"]
                    self.cfg.caps = capabilities(target_model, self.server)

            c.err(c.st.green(f"✓ Connected to {self.server} ({self.conn.description}) [latency {elapsed_ms}ms]"))
            c.err(c.st.dim(f"  Model: {self.cfg.model} · ctx {self.cfg.ctx} · {len(self.models)} models available"))
        except Exception as exc:
            self.connected = False
            self.connection_error = str(exc)
            diag = explain_engine_error(exc, url=self.conn.description, server=self.server)
            c.err(c.st.red(f"✗ Reconnect failed: {diag['category']} — {diag['summary']}"))
            c.err(c.st.dim(f"  {diag['detail']}"))
            c.err(c.st.cyan(f"  Tip: {diag['suggestion']}"))

    def cmd_disconnect(self) -> None:
        if self.conn:
            self.conn.close()
        self.connected = False
        self.connection_error = "Manually disconnected"
        self.con.err(self.con.st.yellow(f"✓ Disconnected from {self.server}. Running in offline mode."))

    def cmd_status(self) -> None:
        c, cfg = self.con, self.cfg
        used = self.s.stats
        git = subprocess.run(["git", "status", "--short", "--branch"], cwd=self.workspace, capture_output=True, text=True).stdout.strip().splitlines()
        conn_str = c.st.green("Connected") if self.connected else c.st.yellow(f"Offline ({self.connection_error or 'unreachable'})")

        model_display = cfg.model
        if not self.connected and self.models and any(m.get("cached") for m in self.models):
            model_display += " [cached - offline]"

        rows = [
            ("connection", conn_str),
            ("server", f"{self.server} ({self.conn.description if self.conn else 'no connection'})"),
            ("workspace", str(self.workspace)),
            ("model", model_display),
            ("context window", f"{cfg.ctx} tokens" + (" (assumed)" if not cfg.pin_ctx and not cfg.caps.local else "")),
            ("tool calling", cfg.caps.tool_calling),
            ("effort", f"{cfg.effort} (agent strategy)"),
            ("thinking", f"{cfg.thinking}  [{cfg.caps.thinking_toggle}]"),
            ("permissions", self.s.ws.mode),
            ("session", f"{self.s.id} ({self.s.name or 'unnamed'}) status={self.s.status}"),
            ("usage", f"{used.turns} turns, {used.prompt_tokens} prompt / {used.completion_tokens} output tokens, {used.tools_run} tool runs"),
            ("git", git[0] if git else "not a git repository"),
        ]
        for k, v in rows:
            c.err(f"  {c.st.dim(k.ljust(24))}{v}")
        for line in git[1:8]:
            c.err(c.st.dim("                          " + line))

    def cmd_new(self) -> None:
        c = self.con
        self.s.save()
        old_id = self.s.id
        self.s.ws.plan = []
        self.s.ws.remembered.clear()
        self.s = Session(self.s.ws, self.cfg, ui=self.con)
        c.err(c.st.green(f"✓ Started new session {self.s.id[:8]}") + c.st.dim(f"  (previous session {old_id[:8]} saved)"))

    def cmd_sessions(self) -> None:
        sessions = Session.list_sessions(50)
        if not sessions:
            self.con.err("No saved sessions found.")
            return

        chosen_id = select_session(sessions, self.s.id)
        if not chosen_id or chosen_id == self.s.id:
            return

        self.cmd_resume([chosen_id])

    def cmd_resume(self, args: list[str]) -> None:
        c = self.con
        if not args:
            self.cmd_sessions()
            return
        sid = Session.find(args[0])
        new = Session.load(sid, self.s.ws, self.cfg, self.con)
        new.swap_confirm = self.s.swap_confirm
        self.s = new
        self.cfg.model = new.cfg.model
        self.cfg.effort = new.cfg.effort
        self.cfg.thinking = new.cfg.thinking
        if self.models:
            found = next((m for m in self.models if m.get("ref") == self.cfg.model or m.get("id") == self.cfg.model), None)
            if found:
                self.cfg.caps = capabilities(found, self.server)
        self._fetch_telemetry_async(force=True)
        c.err(c.st.green(f"✓ Resumed {new.name or new.id[:8]}") +
              c.st.dim(f"  ({len(new.messages)} messages, model: {self.cfg.model}, effort: {self.cfg.effort}, status: {new.status}). Send an instruction to continue."))

    def cmd_history(self) -> None:
        c = self.con
        msgs = self.s.messages
        if not msgs:
            c.err("No messages in this session yet.")
            return

        c.err(c.st.bold(f"Session History ({len(msgs)} messages)\n"))
        for i, m in enumerate(msgs, 1):
            role = m.get("role", "unknown")
            content = m.get("content") or ""
            calls = m.get("tool_calls") or []

            if role == "system":
                c.err(c.st.dim(f"[{i}] system: ({len(content)} chars)"))
            elif role == "user":
                preview = content.replace("\n", " ")[:120]
                c.err(c.st.cyan(f"[{i}] user: ") + preview)
            elif role == "assistant":
                preview = content.replace("\n", " ")[:120] if content else ""
                call_info = f" (called {len(calls)} tools: {', '.join(c.get('function', {}).get('name', '') for c in calls)})" if calls else ""
                c.err(c.st.green(f"[{i}] assistant: ") + preview + c.st.dim(call_info))
            elif role == "tool":
                call_name = m.get("name") or m.get("tool_call_id") or "tool"
                res_preview = content.replace("\n", " ")[:100]
                c.err(c.st.dim(f"[{i}] tool ({call_name}): {res_preview}"))

    def cmd_tools(self) -> None:
        c = self.con
        c.err(c.st.bold(f"Workspace Tools (Permission Mode: {self.s.ws.mode})\n"))
        tool_descriptions = [
            ("read_file", "Read file contents", "Always allowed (read-only)"),
            ("list_dir", "List directory contents", "Always allowed (read-only)"),
            ("search_files", "Search files for text patterns", "Always allowed (read-only)"),
            ("write_file", "Create or overwrite a file", "Prompts in 'ask'; auto-approved in 'auto' and 'full-access'"),
            ("edit_file", "Perform exact string replacements", "Prompts in 'ask'; auto-approved in 'auto' and 'full-access'"),
            ("append_file", "Append text to a file", "Prompts in 'ask'; auto-approved in 'auto' and 'full-access'"),
            ("run_command", "Execute bash shell commands", "Prompts in 'ask'; low-risk approved in 'auto'; root/destructive always asks"),
            ("git_diff", "View git repository diff", "Always allowed (read-only)"),
            ("git_status", "Check git working tree status", "Always allowed (read-only)"),
            ("update_plan", "Update the task plan checklist", "Always allowed (agent planning)"),
        ]
        for name, desc, policy in tool_descriptions:
            c.err(f"  {c.st.cyan(name.ljust(16))} {desc}")
            c.err(f"    {c.st.dim('Policy: ' + policy)}")

    def cmd_config(self, args: list[str]) -> None:
        c = self.con
        if not args or args[0] == "show":
            settings, origin = effective_settings(self.workspace)
            c.err(c.st.bold(f"Configuration Settings ({self.workspace})\n"))
            for k in sorted(DEFAULTS):
                c.err(f"  {k:<20} {json.dumps(settings[k]):<26} [{origin[k]}]")
            c.err(c.st.dim(f"\nFiles: {config_path()} · {secrets_path()}"))
            return

        action = args[0]
        if action == "path":
            c.err(f"config  {config_path()}\nsecrets {secrets_path()}")
            return

        if action == "get" and len(args) > 1:
            settings, _ = effective_settings(self.workspace)
            c.err(json.dumps(settings.get(args[1])))
            return

        if action in ("set", "unset"):
            if len(args) < 2 or (action == "set" and len(args) < 3):
                raise ConfigError("usage: /config set KEY VALUE   |   /config unset KEY")
            key = args[1]
            if key not in DEFAULTS:
                raise ConfigError(f"Unknown setting '{key}'. Known: {', '.join(sorted(DEFAULTS))}")
            if action == "unset":
                cfg = load_config()
                cfg["settings"].pop(key, None)
                save_config(cfg)
                c.err(c.st.green(f"✓ unset {key}"))
                return
            val = coerce_setting(key, args[2])
            where = update_settings({key: val}, workspace=self.workspace, scope="global")
            c.err(c.st.green(f"✓ {key} = {val!r} ({where})"))
            return

        raise ConfigError("usage: /config [show|get|set|unset|path]")
