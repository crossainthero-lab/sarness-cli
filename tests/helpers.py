import json
from pathlib import Path

from sarness_cli import agent, tools
from sarness_cli.modelinfo import Capabilities


def call(name, args, call_id="id1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args) if not isinstance(args, str) else args}}


def result(calls=None, content="", finish=None, reasoning="", usage=None):
    calls = calls or []
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = calls
    return {"type": "done", "message": msg, "reasoning": reasoning, "finish_reason": finish or ("tool_calls" if calls else "stop"), "usage": usage}


class FakeEngine:
    """Scripted engine: each completion pops the next item (a result dict or an Exception)."""
    def __init__(self, *results):
        self.results = list(results)
        self.history = []
        self.params = []
        self.reconnects = 0

    def completion(self, model, messages, tools_, params, on_event=None):
        self.history.append(json.loads(json.dumps(messages)))
        self.params.append(dict(params))
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        if on_event:
            if item["message"].get("content"):
                on_event({"type": "delta", "content": item["message"]["content"], "reasoning": ""})
        return item

    def reconnect(self):
        self.reconnects += 1


def make_caps(reasoning=True, local=True):
    return Capabilities(ref="local:fake", local=local, reasoning=reasoning, tools_reported=None, tools_probed=None, ctx_max=32768, ctx_default=32768)


def make_cfg(**kw):
    base = dict(model="local:fake", caps=make_caps(), effort="medium", ctx=32768, server="test", retries=3)
    base.update(kw)
    return agent.RunConfig(**base)


def make_session(tmp_path: Path, mode="auto", approve=lambda _: True, **cfgkw):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    ws = tools.Workspace(root, mode, approve)
    return agent.Session(ws, make_cfg(**cfgkw)), root
