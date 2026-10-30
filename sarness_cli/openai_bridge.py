"""Loopback-only OpenAI Chat Completions adapter for OpenCode -> Sarness Engine.

This is a compatibility bridge, NOT a replacement inference engine.
No Sarness token is sent to OpenCode. The existing authenticated Engine client
owns its HTTPS/SSH connection and passes complete structured tool calls through.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .engine import EngineError

MAX_BODY = 8 * 1024 * 1024


def opencode_config(url: str, models: list[dict[str, Any]], default: str, key: str) -> dict[str, Any]:
    """Generate in-memory OpenCode 1.x provider configuration, including effort variants."""
    catalog = {}
    for m in models:
        ref = str(m.get("ref") or "")
        if not ref or m.get("supported") is False:
            continue
        ctx = max(2048, int(m.get("context_max") or m.get("default_ctx") or 32768))
        model: dict[str, Any] = {
            "name": str(m.get("name") or ref),
            "limit": {"context": ctx, "output": min(8192, max(1024, ctx // 4))},
        }
        if m.get("reasoning") is True:
            model["variants"] = {
                "low": {"reasoningEffort": "low"},
                "medium": {"reasoningEffort": "medium"},
                "high": {"reasoningEffort": "high"},
                "max": {"reasoningEffort": "high"},
            }
        catalog[ref] = model
    if default not in catalog:
        raise ValueError(f"The selected Sarness model {default!r} is unavailable")
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"sarness/{default}",
        "provider": {
            "sarness": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Sarness Engine",
                "options": {"baseURL": url, "apiKey": key},
                "models": catalog,
            }
        },
    }


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, engine: Any, models: list[dict[str, Any]]):
        self.engine = engine
        self.models = {str(m["ref"]): m for m in models if m.get("ref") and m.get("supported") is not False}
        self.key = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    def start(self) -> threading.Thread:
        worker = threading.Thread(target=self.serve_forever, name="sarness-openai-bridge", daemon=True)
        worker.start()
        return worker


class _Handler(BaseHTTPRequestHandler):
    server: BridgeServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # No payloads, authorization headers, or prompts in local HTTP logs.
        pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": {"message": message, "type": "sarness_bridge_error"}})

    def _authorized(self) -> bool:
        # A random per-launch key restricts access even from other local processes.
        return self.headers.get("Authorization") == "Bearer " + self.server.key

    def do_GET(self) -> None:
        if not self._authorized():
            self._error(401, "Unauthorized local Sarness bridge request")
            return
        if self.path.rstrip("/") == "/v1/models":
            self._json(200, {"object": "list", "data": [
                {"id": ref, "object": "model", "owned_by": "sarness"}
                for ref in self.server.models
            ]})
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._error(404, "Unknown endpoint")

    def _chunk(self, model: str, ident: str, delta: dict[str, Any], finish: str | None = None) -> None:
        data = {
            "id": ident, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        self.wfile.write(b"data: " + json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n\n")
        self.wfile.flush()

    def _done(self) -> None:
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    @staticmethod
    def _tools(message: dict[str, Any]) -> list[dict[str, Any]]:
        calls = []
        for idx, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function") or {}
            args = fn.get("arguments") or "{}"
            calls.append({
                "index": idx, "id": call.get("id") or f"call_{idx}", "type": "function",
                "function": {
                    "name": fn.get("name") or "",
                    "arguments": args if isinstance(args, str) else json.dumps(args),
                },
            })
        return calls

    def do_POST(self) -> None:
        if not self._authorized():
            self._error(401, "Unauthorized local Sarness bridge request")
            return
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._error(404, "Unknown endpoint")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                self._error(413, "Missing or oversized completion request")
                return
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Completion body must be a JSON object")
            model = data.get("model")
            if model not in self.server.models:
                self._error(400, f"Model {model!r} not enabled in Sarness Engine")
                return
            messages = data.get("messages")
            if not isinstance(messages, list):
                raise ValueError("messages must be an array")
            tools = data.get("tools") or []
            if not isinstance(tools, list):
                raise ValueError("tools must be an array")
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self._error(400, f"Invalid completion request: {exc}")
            return

        params: dict[str, Any] = {
            "temperature": data.get("temperature", 0.2),
            "max_tokens": data.get("max_completion_tokens") or data.get("max_tokens") or 4096,
        }
        if data.get("top_p") is not None:
            params["top_p"] = data["top_p"]
        effort = data.get("reasoning_effort") or data.get("reasoningEffort")
        if effort in {"low", "medium", "high", "max"}:
            params["thinking"] = effort != "low"

        # The engine can reload a model when ctx/gpu_layers are omitted or differ.
        # Pin its live parameters and refuse implicit model swaps behind OpenCode.
        if model.startswith("local:"):
            try:
                live = self.server.engine.llm_status()
            except EngineError as exc:
                self._error(502, f"Cannot inspect the loaded model: {exc}")
                return
            if live.get("state") != "ready" or f"local:{live.get('model_id')}" != model:
                self._error(409, "Selected model is no longer loaded; use sarness cabinet to load it explicitly")
                return
            if live.get("ctx"):
                params["ctx"] = int(live["ctx"])
            if live.get("gpu_layers") is not None:
                params["gpu_layers"] = live["gpu_layers"]

        streaming = data.get("stream") is True
        ident = "chatcmpl-" + secrets.token_hex(12)
        sent_content = False
        started_stream = False
        try:
            if streaming:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                started_stream = True
                self._chunk(model, ident, {"role": "assistant"})

                def on_event(ev: dict[str, Any]) -> None:
                    nonlocal sent_content
                    if ev.get("type") == "delta" and ev.get("content"):
                        self._chunk(model, ident, {"content": ev["content"]})
                        sent_content = True

                done = self.server.engine.completion(model, messages, tools, params, on_event)
                message = done["message"]
                if not sent_content and message.get("content"):
                    self._chunk(model, ident, {"content": message["content"]})
                calls = self._tools(message)
                if calls:
                    self._chunk(model, ident, {"tool_calls": calls})
                finish = "tool_calls" if calls else done.get("finish_reason") or "stop"
                self._chunk(model, ident, {}, finish)
                self._done()
                self.close_connection = True
            else:
                done = self.server.engine.completion(model, messages, tools, params)
                message = dict(done["message"])
                calls = self._tools(message)
                if calls:
                    message["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in calls]
                message["role"] = "assistant"
                finish = "tool_calls" if calls else done.get("finish_reason") or "stop"
                self._json(200, {
                    "id": ident, "object": "chat.completion", "created": int(time.time()),
                    "model": model, "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": done.get("usage") or {},
                })
        except (EngineError, OSError, ValueError, KeyError) as exc:
            if started_stream:
                try:
                    err = {"error": {"message": f"Sarness Engine failure: {exc}", "type": "sarness_bridge_error"}}
                    self.wfile.write(b"data: " + json.dumps(err).encode() + b"\n\n")
                    self._done()
                except OSError:
                    pass
                self.close_connection = True
            else:
                self._error(502, f"Sarness Engine failure: {exc}")


def launch_opencode(engine: Any, models: list[dict[str, Any]], selected: str, workspace: Path,
                    *, auto: bool = False) -> int:
    """Launch OpenCode TUI against a temporary, authenticated, loopback-only bridge."""
    if not shutil.which("opencode"):
        raise RuntimeError("OpenCode is not installed. Install from https://opencode.ai/, then retry.")
    bridge = BridgeServer(engine, models)
    bridge.start()
    try:
        cfg = opencode_config(bridge.url, models, selected, bridge.key)
        env = os.environ.copy()
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
        cmd = ["opencode", "--model", f"sarness/{selected}"]
        if auto:
            cmd.append("--auto")
        return subprocess.call(cmd, cwd=workspace, env=env)
    finally:
        bridge.shutdown()
        bridge.server_close()
