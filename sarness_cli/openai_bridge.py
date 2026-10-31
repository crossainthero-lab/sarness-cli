"""Loopback-only OpenAI Chat Completions adapter: OpenCode -> Sarness Engine.

This is a protocol bridge, NOT an inference engine. Every completion is sent to Sarness Engine's
`/v1/agent/completions` over the existing authenticated connection (SSH tunnel or HTTPS); the Sarness
token never reaches OpenCode, which only gets a random per-launch key for 127.0.0.1.

Invariants (each has a regression test in tests/test_openai_bridge.py):
- Tool calls are emitted only from the engine's final `done` event, i.e. complete; a turn cut by the output
  limit (`finish_reason: length`) never carries tool calls, so a truncated call cannot be executed.
- A stream that ends without a finish reason is an error, never a successful `stop`. Before any byte reaches
  the client it is retried (inference has no side effects); afterwards the client gets an error event.
- When the client disconnects, the upstream engine request is closed, which cancels the generation.
- A local model is used only if it is the one already loaded, with its live ctx/gpu_layers pinned, so a request
  can never silently reload or evict a model.
- OpenCode runs in the requested workspace: it takes its directory from $PWD, not the process cwd.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, tui
from .engine import EngineError
from .guard import Guard, Ledger
from .guard.core import parse_call

try:                                # POSIX terminals only; elsewhere the launcher simply skips terminal restoration
    import termios
except ImportError:                 # pragma: no cover
    termios = None                  # type: ignore[assignment]

MAX_BODY = 32 * 1024 * 1024
STREAM_RETRIES = 2                  # extra attempts while nothing has been sent to the client
EFFORTS = ("low", "medium", "high")
VALIDATED_MAJOR = 2                 # OpenCode major version checked live against Sarness Engine
SAMPLING = ("temperature", "top_p", "top_k", "min_p", "seed", "presence_penalty", "frequency_penalty")
MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")
MAX_HIDDEN_CALLS = 4                # guard follow-up model calls for one client request (the guard's own bounds are tighter)


def output_limit(ctx: int) -> int:
    return max(1024, min(32768, ctx // 4))


def model_context(model: dict[str, Any], live: dict[str, Any] | None) -> int:
    """Use the loaded model's actual context, never a guessed local capacity."""
    if model.get("local"):
        if live and f"local:{live.get('model_id')}" == model.get("ref") and int(live.get("ctx") or 0) > 0:
            return int(live["ctx"])
        raise ValueError(f"The engine did not report the loaded context limit for {model.get('ref')}")
    return int(model.get("default_ctx") or model.get("context_max") or 0)


def display_name(model: dict[str, Any]) -> str:
    """'Qwen3.6 35B-A3B (vision) · MXFP4_MOE' -> 'Qwen3.6 35B-A3B': the info line under the prompt stays short."""
    name = str(model.get("name") or model.get("ref") or "")
    return re.sub(r"\s*\((?:vision)\)", "", name.split(" · ")[0]).strip() or name


def opencode_config(url: str, models: list[dict[str, Any]], default: str, key: str, *,
                    live: dict[str, Any] | None = None, permission: dict[str, Any] | None = None,
                    effort: str | None = None) -> dict[str, Any]:
    """OpenCode 2.x configuration (`providers` schema) for the Sarness bridge provider."""
    catalog: dict[str, Any] = {}
    for m in models:
        ref = str(m.get("ref") or "")
        if not ref or m.get("supported") is False:
            continue
        if m.get("local") and not (live and f"local:{live.get('model_id')}" == ref and live.get("state") == "ready"):
            continue            # only the loaded local model: selecting another would evict it for everyone
        ctx = model_context(m, live)
        catalog[ref] = {
            "modelID": ref,
            "name": display_name(m),
            "limit": {"context": ctx, "output": output_limit(ctx)},
            "capabilities": {
                "tools": True, "temperature": True,
                "input": ["text"], "output": ["text"],
                "reasoning": {"supported": True, "effort": list(EFFORTS)},
            },
            "compatibility": {"reasoningField": "reasoning_content", "streamUsage": True,
                              "maxTokensField": "max_tokens", "requireFinishReason": True},
            "variants": [{"id": e, "body": {"reasoning_effort": e}} for e in EFFORTS],
        }
    if default not in catalog:
        raise ValueError(f"The selected Sarness model {default!r} is unavailable (a local model must be loaded first)")
    cfg: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        # Explicit selection so the terminal starts on the configured effort variant ("max" -> the top variant).
        "model": {"providerID": "sarness", "model": default,
                  "variant": effort if effort in EFFORTS else ("high" if effort == "max" else "medium")},
        "small_model": f"sarness/{default}",
        "autoupdate": False,
        "share": "disabled",
        "enabled_providers": ["sarness"],
        "providers": {
            "sarness": {
                "name": "Sarness Engine",
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": url, "apiKey": key, "headerTimeout": 900000, "chunkTimeout": 900000},
                "models": catalog,
            }
        },
    }
    if permission is not None:
        cfg["permission"] = permission
    return cfg


# Sarness permission modes expressed as OpenCode rules. OpenCode matches the most specific bash pattern.
_ASK_COMMANDS = ["sudo *", "su *", "rm -r*", "rm -f*", "git push*", "git reset --hard*", "git clean*",
                 "pip install*", "pip3 install*", "uv pip install*", "npm install*", "npm i *", "pnpm add*",
                 "yarn add*", "apt*", "curl *", "wget *", "ssh *", "scp *", "rsync *", "docker *", "dd *",
                 "mkfs*", "chmod -R*", "chown*", "kill*", "pkill*", "systemctl*"]


def opencode_permission(mode: str) -> dict[str, Any]:
    if mode == "ask":
        return {"edit": "ask", "bash": "ask", "webfetch": "ask", "external_directory": "ask"}
    if mode == "auto":
        return {"edit": "allow", "webfetch": "ask", "external_directory": "ask",
                "bash": {"*": "allow", **{p: "ask" for p in _ASK_COMMANDS}}}
    if mode == "full-access":
        return {"edit": "allow", "webfetch": "allow", "external_directory": "ask",
                "bash": {"*": "allow", **{p: "ask" for p in ("sudo *", "rm -r*", "rm -f*", "git push*",
                                                            "git reset --hard*", "git clean*", "dd *", "mkfs*")}}}
    if mode == "bypass":
        return {"*": "allow"}
    raise ValueError(f"unknown permission mode {mode!r}")


def opencode_major(version_text: str) -> int:
    """Fail closed when the installed OpenCode version cannot be identified or was never validated."""
    match = re.search(r"(?<![0-9])([0-9]+)\.[0-9]+(?:\.[0-9]+)?", version_text)
    if not match:
        raise RuntimeError(f"Cannot identify the Sarness terminal version from {version_text!r}")
    major = int(match.group(1))
    if major != VALIDATED_MAJOR:
        raise RuntimeError(f"{version_text.strip()} is not supported; the Sarness bridge is validated "
                           f"against OpenCode {VALIDATED_MAJOR}.x")
    return major


def effort_params(effort: Any, default: str) -> dict[str, Any]:
    """Sarness Engine 0.4 exposes reasoning only as on/off: low/none disable thinking, others enable it."""
    effort = effort if effort in (*EFFORTS, "max", "none", "minimal") else default
    return {"thinking": effort not in ("low", "none", "minimal")}


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, engine: Any, models: list[dict[str, Any]], *, effort: str = "medium",
                 retries: int = STREAM_RETRIES, live: dict[str, Any] | None = None, mode: str = "auto",
                 workspace: Path | str | None = None, guard: bool | None = None):
        self.engine = engine
        self.mode = mode
        self.workspace = str(workspace) if workspace else None
        self.guard_enabled = (os.environ.get("SARNESS_GUARD", "on").lower() not in ("off", "0", "no")) if guard is None else guard
        self._guards: dict[str, Guard] = {}
        self._guard_lock = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}
        self.guard_events: list[dict[str, Any]] = []
        self.models = {str(m["ref"]): m for m in models if m.get("ref") and m.get("supported") is not False}
        self.expected_live_context = int(live.get("ctx") or 0) if live else None
        self.key = secrets.token_urlsafe(32)
        self.effort = effort
        self.retries = retries
        self.stats = {"requests": 0, "completed": 0, "errors": 0, "cancelled": 0, "retries": 0, "guard_interventions": 0}
        super().__init__(("127.0.0.1", 0), _Handler)

    def guard_for(self, session: str) -> tuple[Guard, threading.Lock]:
        """One guard (and its ledger) per terminal session; the session id is stable across compaction and resume."""
        with self._guard_lock:
            guard = self._guards.get(session)
            if guard is None:
                guard = Guard(Ledger.open(session, runtime="tui"), mode=self.mode, runtime="tui", workspace=self.workspace)
                self._guards[session] = guard
            return guard, self._session_locks.setdefault(session, threading.Lock())

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    def start(self) -> threading.Thread:
        worker = threading.Thread(target=self.serve_forever, name="sarness-openai-bridge", daemon=True)
        worker.start()
        return worker


class ClientGone(Exception):
    pass


class _Handler(BaseHTTPRequestHandler):
    server: BridgeServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass            # no prompts, payloads or keys in logs

    # ------------------------------------------------------------ plumbing
    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, code: str = "sarness_bridge_error") -> None:
        self._json(status, {"error": {"message": message, "type": code, "code": code}})

    def _authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.server.key)

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except OSError as exc:
            raise ClientGone() from exc

    def _event(self, payload: dict[str, Any]) -> None:
        self._write(b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n")

    def _client_closed(self) -> bool:
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return bool(ready) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    # ------------------------------------------------------------ routes
    def do_GET(self) -> None:
        if not self._authorized():
            return self._error(401, "Unauthorized local Sarness bridge request")
        path = self.path.split("?")[0].rstrip("/")
        if path == "/v1/models":
            self._json(200, {"object": "list", "data": [
                {"id": ref, "object": "model", "owned_by": "sarness-engine"} for ref in self.server.models]})
        elif path == "/health":
            self._json(200, {"status": "ok", **self.server.stats})
        else:
            self._error(404, "Unknown endpoint")

    def do_POST(self) -> None:
        if not self._authorized():
            return self._error(401, "Unauthorized local Sarness bridge request")
        if self.path.split("?")[0].rstrip("/") != "/v1/chat/completions":
            return self._error(404, "Unknown endpoint")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                return self._error(413, "Missing or oversized completion request")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            model = data.get("model")
            if model not in self.server.models:
                return self._error(404, f"Model {model!r} is not served by this Sarness bridge", "model_not_found")
            messages = data.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError("messages must be a non-empty array")
            tools = data.get("tools") or []
            if not isinstance(tools, list):
                raise ValueError("tools must be an array")
        except (ValueError, UnicodeError) as exc:
            return self._error(400, f"Invalid completion request: {exc}", "invalid_request_error")

        self.server.stats["requests"] += 1
        messages = [{k: v for k, v in m.items() if k in MESSAGE_KEYS} for m in messages if isinstance(m, dict)]
        guard, messages = self._guard_prepare(messages)
        params: dict[str, Any] = {k: data[k] for k in SAMPLING if data.get(k) is not None}
        params["max_tokens"] = int(data.get("max_completion_tokens") or data.get("max_tokens") or 4096)
        params.update(effort_params(data.get("reasoning_effort") or data.get("reasoningEffort"), self.server.effort))
        if self.server.models[model].get("local"):
            try:
                live = self.server.engine.llm_status()
            except EngineError as exc:
                return self._error(502, f"Cannot inspect the loaded model: {exc}", "engine_unavailable")
            if live.get("state") != "ready" or f"local:{live.get('model_id')}" != model:
                return self._error(409, "The selected model is no longer loaded on the engine; load it again "
                                        "(sarness cabinet) - the bridge never swaps models implicitly", "model_not_loaded")
            if self.server.expected_live_context is not None and int(live.get("ctx") or 0) != self.server.expected_live_context:
                return self._error(409, "The loaded model context changed or is unavailable; restart Sarness to refresh its context limit", "model_context_changed")
            if live.get("ctx"):
                params["ctx"] = int(live["ctx"])
            if live.get("gpu_layers") is not None:
                params["gpu_layers"] = live["gpu_layers"]
        if data.get("stream") is True:
            include_usage = bool((data.get("stream_options") or {}).get("include_usage", True))
            self._stream(model, messages, tools, params, include_usage, guard)
        else:
            self._complete(model, messages, tools, params, guard)

    # ------------------------------------------------------------ the agent guard
    def _guard_prepare(self, messages: list[dict[str, Any]]) -> tuple[Guard | None, list[dict[str, Any]]]:
        """Feed the session's ledger from this request and, after a compaction, append the verified task state so the
        model keeps its objective, findings and next action. Title and summary requests are left alone."""
        if not self.server.guard_enabled:
            return None, messages
        utility = Guard.utility_kind(messages)
        if utility == "title":
            return None, messages
        session = (self.headers.get("x-opencode-session-id") or self.headers.get("x-opencode-session")
                   or self.headers.get("x-session-id") or "")
        if not session:
            return None, messages
        guard, lock = self.server.guard_for(session)
        parent = self.headers.get("x-session-affinity")
        with lock:
            if parent and parent != session:
                guard.led.d["parent"] = parent
            guard.observe(messages)           # a summary request carries the whole history about to be replaced: last chance to see it
            if utility:
                return None, messages
            note = guard.note(messages, budget=3200)
        if note:
            messages = messages + [{"role": "user", "content": note}]
            guard.led.save()
        return guard, messages

    def _guard_turns(self, guard: Guard, model: str, messages: list, tools: list, params: dict[str, Any], done: dict[str, Any],
                     finish: str, calls: list[dict[str, Any]], on_event: Any, emit: Any) -> tuple[dict[str, Any], str, list]:
        """Review the finished turn. A model that asks permission it already has, announces work it did not do, declares
        victory early or proposes an established repeat is continued with a hidden follow-up call; its first words stay
        in the transcript (they were already streamed). Returns the final (done, finish, calls)."""
        if finish in ("length", "error"):
            return done, finish, calls
        session = guard.led.key
        lock = self.server._session_locks.setdefault(session, threading.Lock())
        text = str((done.get("message") or {}).get("content") or "")
        chain: list[dict[str, Any]] = []
        total_text = text
        for attempt in range(MAX_HIDDEN_CALLS + 1):
            with lock:
                before = len(guard.events)
                verdict = guard.review(text=text, calls=calls, messages=messages + chain)
                for ev in guard.events[before:]:
                    self.server.guard_events.append({"session": session, **ev})
                    self.server.stats["guard_interventions"] += 1
            if verdict.action == "pass":
                break
            if verdict.action == "rewrite":
                calls = [{**c, "index": i} for i, c in enumerate(verdict.calls or [])]
                if not calls:
                    finish = "stop"
                else:
                    finish = "tool_calls"
                break
            if verdict.action == "give_up":
                notice = "\n\n[sarness] " + verdict.note
                emit(notice)
                total_text += notice
                calls, finish = [], "stop"
                break
            if attempt >= MAX_HIDDEN_CALLS:
                break
            chain = chain + ([{"role": "assistant", "content": text}] if text.strip() else []) + \
                [{"role": "user", "content": verdict.note}]
            produced = {"n": 0}

            def hidden_event(ev: dict[str, Any]) -> None:
                if ev.get("type") == "delta" and ev.get("content"):
                    if not produced["n"] and total_text.strip():
                        emit("\n\n")
                    produced["n"] += len(ev["content"])
                if on_event is not None:
                    on_event(ev)
            try:
                nxt = self._upstream(model, messages + chain, tools, params, hidden_event)
                nfinish, ncalls, problem = self._finish(nxt)
                if problem:
                    raise EngineError(problem, "stream", retryable=True)
            except EngineError:
                break                                # a failed follow-up must not fail the user's turn: keep what we have
            done, finish, calls = nxt, nfinish, ncalls
            text = str((nxt.get("message") or {}).get("content") or "")
            if text and not produced["n"]:
                emit(("\n\n" if total_text.strip() else "") + text)
            total_text += ("\n\n" if total_text.strip() and text else "") + text
            if finish in ("length", "error"):
                break
        done = {**done, "message": {**(done.get("message") or {}), "content": total_text}}
        if calls:
            with lock:
                guard.led.note_pending([(c["id"], c["function"]["name"], parse_call(c)[1]) for c in calls])
        return done, finish, calls

    # ------------------------------------------------------------ completion
    def _upstream(self, model: str, messages: list, tools: list, params: dict[str, Any], on_event: Any) -> dict[str, Any]:
        """One engine call, aborted (which cancels the generation) as soon as the client disconnects."""
        holder: dict[str, Any] = {}
        stop = threading.Event()

        def watch() -> None:
            while not stop.wait(0.5):
                if self._client_closed():
                    holder["gone"] = True
                    resp = holder.get("resp")
                    try:
                        sock = resp.fp.raw._sock if resp is not None else None   # type: ignore[union-attr]
                        if sock is not None:
                            sock.shutdown(socket.SHUT_RDWR)
                    except (AttributeError, OSError):
                        pass
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            done = self.server.engine.completion(model, messages, tools, params, on_event,
                                                 on_open=lambda r: holder.__setitem__("resp", r))
        except EngineError:
            if holder.get("gone"):
                raise ClientGone() from None
            raise
        finally:
            stop.set()
        if holder.get("gone"):
            raise ClientGone()
        return done

    @staticmethod
    def _finish(done: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str | None]:
        """(finish_reason, complete tool calls, problem). Never returns calls from a cut-off turn."""
        message = done.get("message") or {}
        finish = done.get("finish_reason")
        calls = []
        for i, c in enumerate(message.get("tool_calls") or []):
            fn = c.get("function") or {}
            args = fn.get("arguments")
            calls.append({"index": i, "id": c.get("id") or f"call_{secrets.token_hex(6)}", "type": "function",
                          "function": {"name": fn.get("name") or "",
                                       "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
        if finish is None:
            return "error", [], "the engine stream ended without a finish reason (generation was cut off)"
        if finish == "length":
            return "length", [], None          # any call in a truncated turn is incomplete by definition
        if calls:
            return "tool_calls", calls, None
        return ("stop" if finish in ("stop", "tool_calls", "eos") else str(finish)), [], None

    @staticmethod
    def _usage(done: dict[str, Any]) -> dict[str, Any]:
        u = dict(done.get("usage") or {})
        t = done.get("timings") or {}
        u.setdefault("prompt_tokens", t.get("prompt_n", 0))
        u.setdefault("completion_tokens", t.get("predicted_n", 0))
        u.setdefault("total_tokens", (u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0))
        return u

    @staticmethod
    def _engine_error(exc: EngineError) -> tuple[int, str, str]:
        if exc.kind == "context":
            return 400, f"This model's maximum context length was exceeded: {exc}", "context_length_exceeded"
        if exc.kind == "busy":
            return 409, str(exc), "engine_busy"
        if exc.kind == "auth":
            return 502, str(exc), "engine_auth"
        return 502, f"Sarness Engine failure: {exc}", "engine_error"

    def _complete(self, model: str, messages: list, tools: list, params: dict[str, Any], guard: Guard | None = None) -> None:
        attempt = 0
        while True:
            try:
                done = self._upstream(model, messages, tools, params, None)
                finish, calls, problem = self._finish(done)
                if problem:
                    raise EngineError(problem, "stream", retryable=True)
                break
            except ClientGone:
                self.server.stats["cancelled"] += 1
                return
            except EngineError as exc:
                if exc.retryable and attempt < self.server.retries:
                    attempt += 1
                    self.server.stats["retries"] += 1
                    time.sleep(min(8.0, 1.5 * 2 ** (attempt - 1)))
                    continue
                self.server.stats["errors"] += 1
                return self._error(*self._engine_error(exc))
        if guard is not None:
            try:
                done, finish, calls = self._guard_turns(guard, model, messages, tools, params, done, finish, calls, None,
                                                        lambda _t: None)
            except ClientGone:
                self.server.stats["cancelled"] += 1
                return
        message: dict[str, Any] = {"role": "assistant", "content": done["message"].get("content") or ""}
        if done.get("reasoning"):
            message["reasoning_content"] = done["reasoning"]
        if calls:
            message["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in calls]
        self.server.stats["completed"] += 1
        self._json(200, {"id": "chatcmpl-" + secrets.token_hex(12), "object": "chat.completion",
                         "created": int(time.time()), "model": model,
                         "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                         "usage": self._usage(done)})

    def _stream(self, model: str, messages: list, tools: list, params: dict[str, Any], include_usage: bool,
                guard: Guard | None = None) -> None:
        ident = "chatcmpl-" + secrets.token_hex(12)
        created = int(time.time())
        state = {"headers": False, "sent": False}

        def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
            self._event({"id": ident, "object": "chat.completion.chunk", "created": created, "model": model,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

        def open_stream() -> None:
            # Headers are sent lazily so failures before the first token keep a real HTTP status.
            if not state["headers"]:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                state["headers"] = True
                chunk({"role": "assistant", "content": ""})

        def on_event(ev: dict[str, Any]) -> None:
            kind = ev.get("type")
            if kind == "delta":
                if ev.get("reasoning"):
                    open_stream()
                    chunk({"reasoning_content": ev["reasoning"]})
                    state["sent"] = True
                if ev.get("content"):
                    open_stream()
                    chunk({"content": ev["content"]})
                    state["sent"] = True
            elif kind == "tool_call" and state["headers"]:
                self._write(b": tool-call in progress\n\n")     # keeps the client alive and detects disconnects

        attempt = 0
        while True:
            try:
                done = self._upstream(model, messages, tools, params, on_event)
                finish, calls, problem = self._finish(done)
                if problem:
                    raise EngineError(problem, "stream", retryable=True)
                break
            except ClientGone:
                self.server.stats["cancelled"] += 1
                self.close_connection = True
                return
            except EngineError as exc:
                if exc.retryable and not state["sent"] and attempt < self.server.retries:
                    attempt += 1
                    self.server.stats["retries"] += 1
                    time.sleep(min(8.0, 1.5 * 2 ** (attempt - 1)))
                    continue
                self.server.stats["errors"] += 1
                status, message, code = self._engine_error(exc)
                if not state["headers"]:
                    return self._error(status, message, code)
                try:   # mid-stream: an explicit error, never a finish_reason that looks like success
                    self._event({"error": {"message": message, "type": code, "code": code}})
                except ClientGone:
                    pass
                self.close_connection = True
                return
        try:
            if guard is not None:
                def emit(text: str) -> None:
                    open_stream()
                    chunk({"content": text})
                    state["sent"] = True
                done, finish, calls = self._guard_turns(guard, model, messages, tools, params, done, finish, calls, on_event, emit)
            open_stream()
            if not state["sent"] and done["message"].get("content"):
                chunk({"content": done["message"]["content"]})
            if calls:
                chunk({"tool_calls": calls})
            chunk({}, finish)
            if include_usage:
                self._event({"id": ident, "object": "chat.completion.chunk", "created": created, "model": model,
                             "choices": [], "usage": self._usage(done)})
            self._write(b"data: [DONE]\n\n")
            self.server.stats["completed"] += 1
        except ClientGone:
            self.server.stats["cancelled"] += 1
        self.close_connection = True


def opencode_binary() -> str:
    """The Sarness terminal (forked OpenCode). A separately installed `opencode` is never used or modified."""
    return str(tui.require_binary())


def opencode_env(cfg: dict[str, Any], workspace: Path, config_dir: Path | None = None, *,
                 server: str | None = None, mode: str | None = None, effort: str | None = None,
                 autocompact: str = "on") -> dict[str, str]:
    env = os.environ.copy()
    # Display facts for the terminal's info line and delegated commands; enforcement stays in cfg and the bridge.
    env.update({"SARNESS_TUI": "1", "SARNESS_VERSION": __version__, "SARNESS_BIN": tui.sarness_executable()})
    if server:
        env["SARNESS_SERVER"] = server
    if mode:
        env["SARNESS_MODE"] = mode
    if effort:
        env["SARNESS_EFFORT"] = effort
    env["SARNESS_AUTOCOMPACT_DEFAULT"] = "off" if autocompact == "off" else "on"   # sessions that never ran /autocompact
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    # OpenCode resolves its project directory from $PWD, not from the process cwd: without this, its tools act
    # on whatever directory the parent shell was in.
    env["PWD"] = str(workspace)
    if config_dir is not None:
        env["OPENCODE_CONFIG_DIR"] = str(config_dir)
    return env


def opencode_command(binary: str, workspace: Path, mode: str, args: list[str] | None,
                     tui_args: list[str] | None = None) -> list[str]:
    """--standalone: a private server inherits this per-launch endpoint and key (the shared background service
    would not). --auto only in Sarness bypass mode. The TUI takes no --model flag; the model comes from config."""
    sub = list(args or [])
    cmd = [binary, *sub[:1], "--standalone", *sub[1:]] if sub else [binary, "--standalone", *(tui_args or [])]
    if mode == "bypass":
        cmd.append("--auto")
    if not sub:
        cmd.append(str(workspace))
    return cmd


# What a full-screen program leaves behind if it dies without cleaning up: alternate screen, mouse reporting,
# bracketed paste, the kitty keyboard protocol and a hidden cursor.
TERMINAL_RESET = (b"\x1b[?1049l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?2004l\x1b[?2031l"
                  b"\x1b[<u\x1b[>4;0m\x1b[0m\x1b[?25h")


def restore_terminal(saved: Any | None) -> None:
    """Put the controlling terminal back the way the launcher found it (used when the terminal program crashed)."""
    try:
        if saved is not None and termios is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
        if sys.stdout.isatty():
            sys.stdout.buffer.write(TERMINAL_RESET)
            sys.stdout.buffer.flush()
    except (OSError, ValueError):
        pass


def run_foreground(cmd: list[str], *, stop_grace: float = 5.0, **kw: Any) -> int:
    """Run the terminal as the foreground program. Ctrl+C belongs to it (cancel a turn, clear input, exit): the
    launcher must not die on SIGINT, because subprocess.call would then kill the terminal mid-session.

    SIGTERM and SIGHUP (closing the window, `kill`) are forwarded to the terminal program, which is the only
    process signalled; it gets `stop_grace` seconds to leave cleanly before it, and only it, is killed. If it dies
    abnormally the terminal modes it set are reset, so the user's shell is not left raw."""
    in_main = threading.current_thread() is threading.main_thread()
    child: dict[str, Any] = {}
    stopping: dict[str, float] = {}

    def forward(signum: int, _frame: Any) -> None:
        proc = child.get("proc")
        if proc is not None and proc.poll() is None:
            stopping.setdefault("since", time.monotonic())
            with contextlib.suppress(OSError):
                proc.send_signal(signum)

    saved = None
    if termios is not None and sys.stdin.isatty():
        with contextlib.suppress(OSError, termios.error):
            saved = termios.tcgetattr(sys.stdin.fileno())
    previous: dict[int, Any] = {}
    if in_main:
        # A handler, not SIG_IGN: an ignored disposition would be inherited by the terminal and every command it runs.
        previous[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: None)
        previous[signal.SIGTERM] = signal.signal(signal.SIGTERM, forward)
        previous[signal.SIGHUP] = signal.signal(signal.SIGHUP, forward)
    try:
        proc = subprocess.Popen(cmd, **kw)
        child["proc"] = proc
        while True:
            try:
                code = proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if "since" in stopping and time.monotonic() - stopping["since"] > stop_grace:
                    proc.kill()
                    stopping["since"] = float("inf")
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if code < 0:                                  # killed by a signal: report it the way a shell does
        restore_terminal(saved)
        return 128 + -code
    if code >= 128 or "since" in stopping:
        restore_terminal(saved)
    return code


def launch_opencode(engine: Any, models: list[dict[str, Any]], selected: str, workspace: Path, *,
                    mode: str = "auto", effort: str = "medium", live: dict[str, Any] | None = None,
                    args: list[str] | None = None, tui_args: list[str] | None = None,
                    config_dir: Path | None = None, server: str | None = None, autocompact: str = "on") -> int:
    """Run OpenCode against a temporary, authenticated, loopback-only bridge. `args` go to OpenCode
    (e.g. ["run", "--format", "json", "task"]); the default is the interactive TUI in `workspace`."""
    binary = opencode_binary()
    version = subprocess.check_output([binary, "--version"], text=True, timeout=20).strip()
    opencode_major(version)
    workspace = workspace.resolve()
    bridge = BridgeServer(engine, models, effort=effort, live=live, mode=mode, workspace=workspace)
    bridge.start()
    try:
        cfg = opencode_config(bridge.url, models, selected, bridge.key, live=live, permission=opencode_permission(mode),
                              effort=effort)
        return run_foreground(opencode_command(binary, workspace, mode, args, tui_args), cwd=workspace,
                              env=opencode_env(cfg, workspace, config_dir, server=server, mode=mode, effort=effort,
                                         autocompact=autocompact))
    finally:
        bridge.shutdown()
        bridge.server_close()
