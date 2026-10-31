"""A real HTTP/SSE server speaking the engine's agent API, scripted per test."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "test-token-1234567890abcdef"
MODELS = [
    {"ref": "local:coder@Q4", "name": "Coder Q4", "provider": "Sarness Engine", "vision": False, "context_max": 65536, "default_ctx": 16384,
     "vram_mb": 20000, "size": 1, "local": True, "tools": None},
    {"ref": "local:small@Q8", "name": "Small Q8", "provider": "Sarness Engine", "vision": False, "context_max": 32768, "default_ctx": 8192,
     "vram_mb": 2000, "size": 1, "local": True, "tools": None},
]
REGISTRY = [{"id": "coder@Q4", "name": "Coder Q4", "category": "llm", "capabilities": {"reasoning": True}, "meta": {"architecture": "qwen3"}, "tags": [], "supported": True, "vram_mb": 20000, "loaded": False},
            {"id": "small@Q8", "name": "Small Q8", "category": "llm", "capabilities": {"reasoning": False}, "meta": {"architecture": "qwen3"}, "tags": [], "supported": True, "vram_mb": 2000, "loaded": False}]


class FakeEngineServer:
    def __init__(self, turns, llm_status=None, system_data=None, registry=None):
        self.turns = list(turns)             # each: list of SSE event dicts
        self.completions = []                # request bodies received
        self.loads = []                      # /v1/llm/load bodies received
        self.unloads = 0
        self.llm = llm_status or {"state": "stopped", "model_id": None, "ctx": 8192, "gpu_layers": 999, "active_streams": 0, "last_used": 0}
        self.models_registry = [dict(m) for m in (registry or REGISTRY)]
        self.busy = False
        self.disconnects = 0                 # streams the client closed before the server finished them
        self.lock = threading.Lock()
        outer = self

        self.system_data = system_data or {
            "hardware": {
                "memory": {"total_mb": 124608, "used_mb": 24576, "available_mb": 100032},
                "gpus": [{
                    "index": 0,
                    "name": "NVIDIA GB10",
                    "vram_total_mb": 124608,
                    "vram_used_mb": 24576,
                    "vram_free_mb": 100032,
                    "utilization_pct": 5,
                    "temperature_c": 42,
                }],
            },
            "runtime": {
                "worker": {
                    "alive": True,
                    "busy": False,
                    "loaded": [],
                    "vram_allocated_mb": None,
                    "vram_reserved_mb": None,
                }
            },
            "llm": outer.llm,
        }

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, body, code=200):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def _authed(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._json({"detail": "unauthorized"}, 401)
                    return False
                return True

            def do_GET(self):
                if self.path == "/v1/health":
                    return self._json({"status": "ok", "service": "sarness-engine", "api_version": "1.1", "engine_version": "0.4.0"})
                if not self._authed():
                    return
                if self.path == "/v1/agent/models":
                    return self._json(MODELS)
                if self.path.startswith("/v1/models"):
                    cat = None
                    if "category=" in self.path:
                        cat = self.path.split("category=", 1)[1].split("&")[0]
                    items = outer.models_registry
                    if cat:
                        items = [m for m in items if m.get("category") == cat]
                    return self._json(items)
                if self.path == "/v1/llm/status":
                    return self._json(outer.llm)
                if self.path == "/v1/system":
                    outer.system_data["llm"] = outer.llm
                    return self._json(outer.system_data)
                self._json({"detail": "nope"}, 404)

            def do_POST(self):
                if not self._authed():
                    return
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}

                if self.path == "/v1/llm/load":
                    if outer.busy:
                        return self._json({"detail": "engine busy"}, 409)
                    mid = body.get("model_id")
                    outer.loads.append(body)
                    outer.llm.update(state="ready", model_id=mid, model_name=mid)
                    if body.get("ctx"):
                        outer.llm["ctx"] = body["ctx"]
                    for m in outer.models_registry:
                        if m.get("id") == mid:
                            m["loaded"] = True
                        elif m.get("category") == "llm":
                            m["loaded"] = False
                    return self._json(outer.llm)

                if self.path == "/v1/llm/unload":
                    if outer.busy:
                        return self._json({"detail": "engine busy"}, 409)
                    outer.unloads += 1
                    outer.llm.update(state="stopped", model_id=None)
                    for m in outer.models_registry:
                        if m.get("category") == "llm":
                            m["loaded"] = False
                    return self._json(outer.llm)

                if self.path == "/v1/worker/release":
                    if outer.busy:
                        return self._json({"detail": "worker busy"}, 409)
                    outer.system_data["runtime"]["worker"]["loaded"] = []
                    for m in outer.models_registry:
                        if m.get("category") in ("image", "video", "upscaler"):
                            m["loaded"] = False
                    return self._json({"ok": True})

                if self.path == "/v1/worker/stop":
                    return self._json({"ok": True})

                outer.completions.append(body)
                events = outer.turns.pop(0)
                if callable(events):              # lazy turn: a generator, so a test can stream slowly and be cancelled
                    events = events(body)
                if events == "http500":
                    return self._json({"detail": "boom"}, 500)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                with outer.lock:
                    outer.llm["active_streams"] = int(outer.llm.get("active_streams") or 0) + 1
                try:
                    for ev in events:
                        if ev == "DROP":              # server vanishes mid-stream: no `done` event, connection closed
                            self.wfile.flush()
                            self.connection.shutdown(2)
                            return
                        if isinstance(ev, (int, float)):   # pause (seconds) between events, to model a slow generation
                            time.sleep(ev)
                            continue
                        if isinstance(ev, str):       # raw (possibly corrupt) SSE payload
                            self.wfile.write(("data: " + ev + "\n\n").encode())
                        else:
                            self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    with outer.lock:                  # the real engine releases the stream when the client goes away
                        outer.disconnects += 1
                finally:
                    with outer.lock:
                        outer.llm["active_streams"] = max(0, int(outer.llm.get("active_streams") or 0) - 1)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def done(content="", calls=None, finish=None, reasoning=""):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return {"type": "done", "message": msg, "reasoning": reasoning, "finish_reason": finish or ("tool_calls" if calls else "stop"),
            "usage": {"prompt_tokens": 500, "completion_tokens": 40}, "timings": None}
