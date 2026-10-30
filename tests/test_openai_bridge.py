"""The OpenCode adapter is exercised without a real GPU or OpenCode binary."""
import json
import urllib.error
import urllib.request

import pytest

from sarness_cli.openai_bridge import BridgeServer, opencode_config, opencode_major


MODEL = {"ref": "local:qwen-test", "name": "Qwen test", "local": True,
         "reasoning": True, "context_max": 32768, "supported": True}


class FakeBridgeEngine:
    def __init__(self):
        self.calls = []

    def llm_status(self):
        return {"state": "ready", "model_id": "qwen-test", "ctx": 32768, "gpu_layers": 80}

    def completion(self, model, messages, tools, params, on_event=None):
        self.calls.append((model, messages, tools, params))
        if on_event:
            on_event({"type": "delta", "content": "hello", "reasoning": ""})
        return {
            "message": {"role": "assistant", "content": "hello",
                        "tool_calls": [{"id": "call_1", "type": "function",
                                        "function": {"name": "run_command", "arguments": "{\"command\":\"echo hello\"}"}}]},
            "finish_reason": "tool_calls",
            "usage": {"prompt_tokens": 3, "completion_tokens": 8},
        }


@pytest.fixture
def bridge():
    engine = FakeBridgeEngine()
    server = BridgeServer(engine, [MODEL])
    server.start()
    try:
        yield server, engine
    finally:
        server.shutdown()
        server.server_close()


def request(server, path, payload=None, *, authorized=True):
    url = server.url + path
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if authorized:
        headers["Authorization"] = f"Bearer {server.key}"
    return urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=5)


def test_opencode_configuration_contains_local_bridge_and_effort():
    cfg = opencode_config("http://127.0.0.1:9922/v1", [MODEL], MODEL["ref"], "temporary-key")
    provider = cfg["provider"]["sarness"]
    assert cfg["model"] == "sarness/local:qwen-test"
    assert provider["options"]["baseURL"] == "http://127.0.0.1:9922/v1"
    assert provider["models"][MODEL["ref"]]["variants"]["high"]["reasoningEffort"] == "high"
    assert provider["options"]["apiKey"] == "temporary-key"
    with pytest.raises(ValueError, match="unavailable"):
        opencode_config("http://127.0.0.1:1/v1", [MODEL], "local:other", "key")


def test_bridge_requires_random_authorization_key(bridge):
    server, _ = bridge
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/models", authorized=False)
    assert exc.value.code == 401
    models = json.load(request(server, "/models"))
    assert models["data"][0]["id"] == "local:qwen-test"


def test_bridge_nonstream_preserves_structured_tool_calls(bridge):
    server, engine = bridge
    result = json.load(request(server, "/chat/completions", {
        "model": MODEL["ref"], "messages": [{"role": "user", "content": "say hi"}],
        "tools": [{"type": "function", "function": {
            "name": "run_command", "parameters": {"type": "object", "properties": {}}}}],
        "stream": False, "max_tokens": 2048, "reasoning_effort": "high",
    }))
    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "run_command"
    assert engine.calls[0][3]["ctx"] == 32768
    assert engine.calls[0][3]["gpu_layers"] == 80
    assert engine.calls[0][3]["thinking"] is True


def test_bridge_streams_content_and_final_tool_calls(bridge):
    server, engine = bridge
    stream = request(server, "/chat/completions", {
        "model": MODEL["ref"], "messages": [{"role": "user", "content": "act"}],
        "stream": True,
    })
    body = stream.read().decode()
    assert '"content": "hello"' in body
    assert '"tool_calls"' in body
    assert '"finish_reason": "tool_calls"' in body
    assert "data: [DONE]" in body
    assert len(engine.calls) == 1


def test_bridge_rejects_unlisted_model(bridge):
    server, engine = bridge
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/chat/completions", {"model": "local:other", "messages": [], "stream": False})
    assert exc.value.code == 400
    assert not engine.calls

def test_opencode_v2_provider_model_and_effort_schema():
    cfg = opencode_config("http://127.0.0.1:9922/v1", [MODEL], MODEL["ref"], "temporary-key", major=2)
    provider = cfg["providers"]["sarness"]
    assert cfg["model"] == "sarness/local:qwen-test"
    assert provider["package"] == "@opencode/ai/providers/openai-compatible"
    assert provider["settings"]["apiKey"] == "temporary-key"
    assert provider["env"] == ["SARNESS_BRIDGE_API_KEY"]
    entry = provider["models"][MODEL["ref"]]
    assert entry["modelID"] == MODEL["ref"]
    assert entry["capabilities"]["tools"] is True
    assert entry["variants"][2]["id"] == "high"
    assert entry["variants"][2]["settings"]["reasoningEffort"] == "high"


def test_opencode_version_identification_is_explicit():
    assert opencode_major("opencode 1.2.3") == 1
    assert opencode_major("2.0.1") == 2
    with pytest.raises(RuntimeError, match="Cannot identify"):
        opencode_major("unparseable")
    with pytest.raises(RuntimeError, match="not validated"):
        opencode_major("3.0.0")


def test_openai_bridge_rejects_unknown_provider_major():
    with pytest.raises(ValueError, match="Unsupported OpenCode"):
        opencode_config("http://127.0.0.1:9922/v1", [MODEL], MODEL["ref"], "temporary-key", major=3)
