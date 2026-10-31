"""The OpenCode adapter, exercised without a GPU or an OpenCode binary. Live validation is in docs/OPENCODE.md."""
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sarness_cli.engine import EngineError
from sarness_cli.openai_bridge import (BridgeServer, opencode_command, opencode_config, opencode_env,
                                       opencode_major, opencode_permission, model_context)

MODEL = {"ref": "local:qwen-test", "name": "Qwen test", "local": True,
         "reasoning": True, "context_max": 262144, "default_ctx": 32768, "supported": True}
LIVE = {"state": "ready", "model_id": "qwen-test", "ctx": 131072, "gpu_layers": 80}
CALL = {"id": "call_1", "type": "function", "function": {"name": "run_command", "arguments": "{\"command\":\"echo hello\"}"}}


def done(content="hello", calls=(CALL,), finish="tool_calls", **extra):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = list(calls)
    return {"type": "done", "message": msg, "finish_reason": finish,
            "usage": {"prompt_tokens": 3, "completion_tokens": 8, "total_tokens": 11}, **extra}


class FakeBridgeEngine:
    """Replays scripted turns: each is (events, final) or an exception to raise."""

    def __init__(self, turns=None):
        self.calls = []
        self.turns = list(turns or [([{"type": "delta", "content": "hello", "reasoning": ""}], done())])
        self.live = dict(LIVE)
        self.closed = threading.Event()

    def llm_status(self):
        return self.live

    def completion(self, model, messages, tools, params, on_event=None, on_open=None):
        self.calls.append((model, messages, tools, params))
        turn = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        if isinstance(turn, Exception):
            raise turn
        events, final = turn
        for ev in events:
            if ev == "SLOW":
                for _ in range(60):          # a long generation; stops when the bridge aborts it
                    if self.closed.is_set():
                        raise EngineError("Connection lost mid-response", "network", retryable=True)
                    time.sleep(0.05)
                continue
            if on_event:
                on_event(ev)
        return final


def serve(engine, **kw):
    server = BridgeServer(engine, [MODEL], **kw)
    server.start()
    return server


@pytest.fixture
def bridge():
    engine = FakeBridgeEngine()
    server = serve(engine)
    try:
        yield server, engine
    finally:
        server.shutdown()
        server.server_close()


def request(server, path, payload=None, *, authorized=True):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if authorized:
        headers["Authorization"] = f"Bearer {server.key}"
    return urllib.request.urlopen(urllib.request.Request(server.url + path, data=body, headers=headers), timeout=10)


def sse(body):
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]


def chat(stream=False, **kw):
    return {"model": MODEL["ref"], "messages": [{"role": "user", "content": "act"}], "stream": stream, **kw}


# ------------------------------------------------------------------ configuration
def test_context_uses_live_local_limit_without_fictitious_fallback():
    assert model_context(MODEL, {**LIVE, "ctx": 32768}) == 32768
    with pytest.raises(ValueError, match="did not report"):
        model_context(MODEL, {**LIVE, "ctx": None})
    assert model_context({"ref": "remote:unknown", "local": False}, None) == 0


def test_bridge_rejects_changed_live_context():
    engine = FakeBridgeEngine()
    server = serve(engine, live=LIVE)
    engine.live["ctx"] = 32768
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            request(server, "/chat/completions", chat())
        assert error.value.code == 409
        assert "restart Sarness" in error.value.read().decode()
        assert not engine.calls
    finally:
        server.shutdown()
        server.server_close()

def test_config_uses_v2_providers_schema_and_the_loaded_context():
    cfg = opencode_config("http://127.0.0.1:9922/v1", [MODEL], MODEL["ref"], "k", live=LIVE)
    provider = cfg["providers"]["sarness"]
    assert cfg["model"] == {"providerID": "sarness", "model": "local:qwen-test", "variant": "medium"}
    assert provider["package"] == "@opencode/ai/providers/openai-compatible"
    assert provider["settings"]["baseURL"] == "http://127.0.0.1:9922/v1"
    entry = provider["models"][MODEL["ref"]]
    # the engine serves the loaded ctx (131072), not the model's 262144 maximum
    assert entry["limit"]["context"] == 131072
    assert entry["limit"]["output"] == 32768
    assert entry["capabilities"]["tools"] is True
    assert [v["id"] for v in entry["variants"]] == ["low", "medium", "high"]
    assert entry["variants"][2]["body"] == {"reasoning_effort": "high"}
    assert entry["compatibility"]["reasoningField"] == "reasoning_content"


def test_config_starts_on_the_configured_effort_variant_and_short_name():
    for effort, variant in (("low", "low"), ("high", "high"), ("max", "high"), (None, "medium")):
        cfg = opencode_config("http://127.0.0.1:1/v1", [MODEL], MODEL["ref"], "k", live=LIVE, effort=effort)
        assert cfg["model"]["variant"] == variant
    named = dict(MODEL, name="Qwen3.6 35B-A3B (vision) · MXFP4_MOE")
    cfg = opencode_config("http://127.0.0.1:1/v1", [named], MODEL["ref"], "k", live=LIVE)
    assert cfg["providers"]["sarness"]["models"][MODEL["ref"]]["name"] == "Qwen3.6 35B-A3B"


def test_config_never_offers_an_unloaded_local_model():
    other = {**MODEL, "ref": "local:other"}
    cfg = opencode_config("http://x/v1", [MODEL, other], MODEL["ref"], "k", live=LIVE)
    assert list(cfg["providers"]["sarness"]["models"]) == [MODEL["ref"]]
    with pytest.raises(ValueError, match="unavailable"):
        opencode_config("http://x/v1", [MODEL, other], "local:other", "k", live=LIVE)


def test_permission_modes_map_to_opencode_rules():
    assert opencode_permission("ask")["bash"] == "ask"
    auto = opencode_permission("auto")
    assert auto["edit"] == "allow" and auto["bash"]["*"] == "allow" and auto["bash"]["git push*"] == "ask"
    assert opencode_permission("full-access")["bash"]["sudo *"] == "ask"
    assert opencode_permission("bypass") == {"*": "allow"}
    with pytest.raises(ValueError):
        opencode_permission("yolo")


def test_command_is_standalone_and_auto_only_in_bypass(tmp_path):
    assert opencode_command("oc", tmp_path, "auto", None) == ["oc", "--standalone", str(tmp_path)]
    assert opencode_command("oc", tmp_path, "bypass", None) == ["oc", "--standalone", "--auto", str(tmp_path)]
    run = opencode_command("oc", tmp_path, "auto", ["run", "--format", "json", "task"])
    assert run[:3] == ["oc", "run", "--standalone"] and "--model" not in run and "--auto" not in run


def test_env_sets_pwd_so_opencode_acts_in_the_workspace(tmp_path, monkeypatch):
    # Regression: OpenCode takes its project directory from $PWD; a stale PWD made it edit the parent's repo.
    monkeypatch.setenv("PWD", "/somewhere/else")
    env = opencode_env({"a": 1}, tmp_path)
    assert env["PWD"] == str(tmp_path)
    assert json.loads(env["OPENCODE_CONFIG_CONTENT"]) == {"a": 1}


def test_version_gate_accepts_only_the_validated_major():
    assert opencode_major("2.0.32") == 2
    with pytest.raises(RuntimeError, match="Cannot identify"):
        opencode_major("unparseable")
    for v in ("1.4.0", "3.0.0"):
        with pytest.raises(RuntimeError, match="not supported"):
            opencode_major(v)


# ------------------------------------------------------------------ HTTP behaviour
def test_requires_the_per_launch_key(bridge):
    server, _ = bridge
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/models", authorized=False)
    assert exc.value.code == 401
    assert json.load(request(server, "/models"))["data"][0]["id"] == "local:qwen-test"


def test_binds_loopback_only(bridge):
    server, _ = bridge
    assert server.server_address[0] == "127.0.0.1"


def test_nonstream_preserves_tool_calls_pins_model_and_maps_effort(bridge):
    server, engine = bridge
    result = json.load(request(server, "/chat/completions", chat(max_tokens=2048, reasoning_effort="high")))
    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "run_command"
    params = engine.calls[0][3]
    assert params["ctx"] == 131072 and params["gpu_layers"] == 80 and params["thinking"] is True
    assert params["max_tokens"] == 2048
    assert result["usage"]["total_tokens"] == 11
    json.load(request(server, "/chat/completions", chat(reasoning_effort="low")))
    assert engine.calls[1][3]["thinking"] is False


def test_stream_has_reasoning_content_tool_calls_finish_and_usage():
    engine = FakeBridgeEngine([([{"type": "delta", "content": "", "reasoning": "think"},
                                 {"type": "delta", "content": "hello", "reasoning": ""},
                                 {"type": "tool_call", "index": 0, "name": "run_command"}], done())])
    server = serve(engine)
    try:
        body = request(server, "/chat/completions", chat(stream=True)).read().decode()
    finally:
        server.shutdown(); server.server_close()
    events = sse(body)
    deltas = [e["choices"][0]["delta"] for e in events if e["choices"]]
    assert {"reasoning_content": "think"} in deltas and {"content": "hello"} in deltas
    assert any("tool_calls" in d for d in deltas)
    assert [e["choices"][0]["finish_reason"] for e in events if e["choices"]][-1] == "tool_calls"
    assert events[-1]["usage"]["completion_tokens"] == 8
    assert body.rstrip().endswith("data: [DONE]")


def test_truncated_turn_never_emits_tool_calls():
    engine = FakeBridgeEngine([([{"type": "delta", "content": "writing", "reasoning": ""}],
                                done(finish="length"))])
    server = serve(engine)
    try:
        body = request(server, "/chat/completions", chat(stream=True)).read().decode()
        nonstream = json.load(request(server, "/chat/completions", chat()))
    finally:
        server.shutdown(); server.server_close()
    assert '"tool_calls"' not in body
    assert [e["choices"][0]["finish_reason"] for e in sse(body) if e["choices"]][-1] == "length"
    assert nonstream["choices"][0]["finish_reason"] == "length"
    assert "tool_calls" not in nonstream["choices"][0]["message"]


def test_missing_finish_reason_is_retried_then_reported_not_success():
    cut = ([], done(calls=(), finish=None, content=""))
    ok = ([{"type": "delta", "content": "fine", "reasoning": ""}], done(calls=(), finish="stop", content="fine"))
    engine = FakeBridgeEngine([cut, ok])
    server = serve(engine)
    try:
        body = request(server, "/chat/completions", chat(stream=True)).read().decode()
    finally:
        server.shutdown(); server.server_close()
    assert len(engine.calls) == 2 and server.stats["retries"] == 1
    assert [e["choices"][0]["finish_reason"] for e in sse(body) if e["choices"]][-1] == "stop"

    always_cut = FakeBridgeEngine([([{"type": "delta", "content": "partial", "reasoning": ""}],
                                    done(calls=(CALL,), finish=None))])
    server = serve(always_cut, retries=2)
    try:
        body = request(server, "/chat/completions", chat(stream=True)).read().decode()
    finally:
        server.shutdown(); server.server_close()
    # content was already sent, so no retry; the client gets an error, not a finish reason or a tool call
    assert len(always_cut.calls) == 1
    assert '"error"' in body and '"tool_calls"' not in body and "finish_reason\": \"stop" not in body


def test_errors_before_streaming_keep_http_status():
    engine = FakeBridgeEngine([EngineError("prompt too long for n_ctx", "context")])
    server = serve(engine)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, "/chat/completions", chat(stream=True))
        assert exc.value.code == 400
        assert json.loads(exc.value.read())["error"]["code"] == "context_length_exceeded"
    finally:
        server.shutdown(); server.server_close()


def test_retryable_network_error_retried_with_backoff(monkeypatch):
    monkeypatch.setattr("sarness_cli.openai_bridge.time.sleep", lambda s: None)
    engine = FakeBridgeEngine([EngineError("reset", "network", retryable=True), ([], done())])
    server = serve(engine)
    try:
        result = json.load(request(server, "/chat/completions", chat()))
    finally:
        server.shutdown(); server.server_close()
    assert result["choices"][0]["finish_reason"] == "tool_calls" and len(engine.calls) == 2


def test_refuses_when_the_model_was_swapped_or_unknown(bridge):
    server, engine = bridge
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/chat/completions", {**chat(), "model": "local:other"})
    assert exc.value.code == 404
    engine.live = {**LIVE, "model_id": "someone-elses-model"}
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/chat/completions", chat())
    assert exc.value.code == 409
    assert not engine.calls


def test_client_disconnect_cancels_the_engine_request():
    engine = FakeBridgeEngine([(["SLOW"], done())])
    server = serve(engine)
    original = engine.completion

    def completion(*a, on_open=None, **kw):
        class Resp:                    # stands in for the HTTP response the bridge shuts down
            class fp:
                class raw:
                    class _sock:
                        @staticmethod
                        def shutdown(how):
                            engine.closed.set()
        on_open(Resp)
        return original(*a, **kw)

    engine.completion = completion
    try:
        payload = json.dumps(chat(stream=True)).encode()
        sock = socket.create_connection(server.server_address)
        sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + server.key.encode()
                     + b"\r\nContent-Type: application/json\r\nContent-Length: " + str(len(payload)).encode()
                     + b"\r\n\r\n" + payload)
        time.sleep(0.3)
        sock.close()
        assert engine.closed.wait(3), "engine request was not aborted after the client went away"
        deadline = time.time() + 3
        while server.stats["cancelled"] == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert server.stats["cancelled"] == 1 and server.stats["completed"] == 0
    finally:
        server.shutdown(); server.server_close()


def test_launch_fails_closed_without_opencode(monkeypatch, tmp_path):
    from sarness_cli import openai_bridge as ob
    monkeypatch.setattr(ob.shutil, "which", lambda name: None)
    monkeypatch.setattr(ob.Path, "home", staticmethod(lambda: Path(tmp_path)))
    with pytest.raises(RuntimeError, match="not installed"):
        ob.launch_opencode(FakeBridgeEngine(), [MODEL], MODEL["ref"], tmp_path, live=LIVE)


def test_launcher_survives_ctrl_c_and_the_terminal_keeps_default_sigint():
    import signal
    import sys
    from sarness_cli.openai_bridge import run_foreground
    code = ("import os, signal, sys; os.kill(os.getppid(), signal.SIGINT); "
            "sys.exit(0 if signal.getsignal(signal.SIGINT) is signal.default_int_handler else 7)")
    before = signal.getsignal(signal.SIGINT)
    assert run_foreground([sys.executable, "-c", code]) == 0     # parent got SIGINT and did not die or kill the child
    assert signal.getsignal(signal.SIGINT) is before
