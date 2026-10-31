"""Shared pieces for the Ctrl+C regression tests: a scriptable engine that can stay busy for as long as a test
needs, and process lookups that only ever match this test's own unique marker."""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

from fake_engine_server import FakeEngineServer, TOKEN, done
from sarness_cli import config


REPLY = "ACK-7731"        # a reply no status line or label can contain by accident


class Dispatch(list):
    """`turns` replacement: every completion request is answered by `fn(request_body)`, a generator of SSE events
    (dicts), pauses (numbers, seconds) and nothing else."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def pop(self, index=0):
        return self.fn


def last_user(body):
    for message in reversed(body.get("messages", [])):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else " ".join(
                part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def is_title_request(body):
    system = " ".join(str(m.get("content")) for m in body.get("messages", []) if m.get("role") == "system").lower()
    return "title" in system[:400] and "generat" in system[:400]


def engine_for(tool_name: str, marker: str, *, quick_after_tool="TOOL-DONE-5520"):
    """An engine whose answer depends on the first word of the user's message:
    SLOWGEN streams words every 0.3 s for two minutes; SLEEPCMD asks for a long-running shell command (tagged with
    `marker` so tests can find exactly that process); anything else gets a one-word reply."""
    server = FakeEngineServer([])

    def script(body):
        if is_title_request(body):
            yield done("Test session")
            return
        messages = body.get("messages", [])
        if messages and messages[-1].get("role") == "tool":
            yield {"type": "delta", "content": quick_after_tool, "reasoning": ""}
            yield done(quick_after_tool)
            return
        text = last_user(body)
        if "SLOWGEN" in text:
            for _ in range(400):
                yield {"type": "delta", "content": "word ", "reasoning": ""}
                yield 0.3
            yield done("word " * 400)
        elif "SLEEPCMD" in text:
            command = os.environ.get("CTRL_C_TEST_PREFIX", "") + f"sleep {sleep_tag(marker)}"
            yield done("", calls=[(tool_name, {"command": command})])
        else:
            yield {"type": "delta", "content": REPLY, "reasoning": ""}
            yield done(REPLY)

    server.turns = Dispatch(script)
    server.llm.update(state="ready", model_id="coder@Q4", model_name="Coder Q4", ctx=16384, gpu_layers=999,
                      last_used=time.time())
    return server


def configure(server, *, permissions="auto", runtime="tui"):
    """Write the isolated profile (requires the `isolated_home` fixture, which points XDG_CONFIG_HOME at tmp)."""
    if str(config.config_dir()) != os.environ.get("SARNESS_TEST_CONFIG_ROOT"):
        raise RuntimeError("Ctrl+C tests require the isolated_home fixture before writing configuration")
    cfg = config.load_config()
    cfg["profiles"]["fake"] = {"transport": "direct", "url": server.url, "insecure_http": True, "model": "local:coder@Q4"}
    cfg["settings"].update(server="fake", permissions=permissions, effort="low", runtime=runtime)
    config.save_config(cfg)
    config.set_secret("fake", TOKEN)


def command() -> list[str]:
    installed = os.environ.get("SARNESS_TEST_EXECUTABLE")
    return [installed] if installed else [sys.executable, "-m", "sarness_cli.cli"]


def unique_marker(prefix="SARNESS-CTRLC") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def marked_processes(marker: str) -> list[int]:
    """Live processes whose command line or environment contains this test's unique marker. Nothing else can match."""
    needle = marker.encode()
    me = os.getpid()
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            if stat[stat.rindex(")") + 2:].split()[0] == "Z":
                continue
            if needle in Path(f"/proc/{entry}/cmdline").read_bytes() or needle in Path(f"/proc/{entry}/environ").read_bytes():
                found.append(int(entry))
        except OSError:
            continue
    return sorted(found)


def sleep_tag(marker: str) -> str:
    """A unique sleep duration for this test, e.g. `600.482913377`. A shell that is given `sleep 600 # tag` execs
    sleep directly and drops the comment, but the argument survives, so it identifies exactly this test's command."""
    return "600." + str(int(marker.rsplit("-", 1)[1], 16))[:9]


def running_sleeps(marker: str) -> list[int]:
    """Live `sleep <tag>` processes for this test (the command the fake model asked to run, or the user typed)."""
    want = ["sleep", sleep_tag(marker)]
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            if stat[stat.rindex(")") + 2:].split()[0] == "Z":
                continue
            argv = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if [a.decode("utf-8", "replace") for a in argv if a][:2] == want:
            found.append(int(entry))
    return sorted(found)


def wait_until(predicate, timeout: float, step: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def stream_count(server) -> int:
    return int(server.llm.get("active_streams") or 0)


def dump(obj) -> str:
    return json.dumps(obj, indent=1, default=str)[:1500]
