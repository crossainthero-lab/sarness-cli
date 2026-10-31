"""A deterministic multi-stage engineering task and the pieces to run it end to end without a GPU or a real server.

    "SSH into the authorised test server, inspect the project, fix the bug, run the tests and deploy the verified result."

* `Sandbox`   a local project (git repo with a bug, tests, an rsync deploy script) and a fake remote host: an `ssh` stub
              that runs commands inside a directory standing in for the server's home, a `systemctl` stub that knows the
              service only in the *user* scope, and a remote copy of the project that has no `.git` (an rsync deployment).
* `Persona`   a scripted model that only knows what is visible in the messages it is sent (tool results and the guard's
              task-state note) and that has the habits seen in the Trinodal sessions: asking before it starts, glueing
              `sshuser@host`, doubting a result it already has, checking the wrong systemd scope, repeating inspections after
              a compaction, declaring victory early.
* `BridgeHarness`  stands in for the terminal: it sends the history to the real HTTP bridge, runs the tool calls it gets
              back for real, and compacts the history at chosen stages the way the terminal does.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path
from typing import Any, Callable

from guardkit import checkpoint, system, user

HOST = "testhost"
TARGET = f"fylume@{HOST}"
TASK = (f"SSH into {TARGET} (my authorised test server), inspect the app in ~/app against this project, find the bug in "
        "calc/core.py, fix it, run the tests and deploy the verified result with scripts/deploy.sh. Proceed autonomously.")

CORE_BUGGY = "def mean(values):\n    return sum(values) / (len(values) + 1)\n"
CORE_FIXED = "def mean(values):\n    return sum(values) / len(values)\n"
TEST_SRC = ("from calc.core import mean\n\n\ndef test_mean():\n    assert mean([2, 4, 6]) == 4\n\n\n"
            "def test_single():\n    assert mean([5]) == 5\n")
DEPLOY_SH = textwrap.dedent('''\
    #!/bin/sh
    # deploy the library to the test server: a plain file copy, the server keeps no git checkout
    set -e
    rsync -a --delete -e ssh calc/ fylume@testhost:~/app/calc/
    ssh fylume@testhost 'systemctl --user restart app'
    echo "deployed"
''')

SSH_STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import os, subprocess, sys
    args = sys.argv[1:]
    i, target = 0, None
    while i < len(args):
        a = args[i]
        if a in ("-o", "-i", "-p", "-l", "-F", "-J", "-L", "-R", "-D", "-E", "-b", "-c", "-m", "-S", "-W", "-w"):
            if a == "-l":
                target = (args[i + 1] + "@") if i + 1 < len(args) else ""
            i += 2
            continue
        if a.startswith("-") and len(a) > 1:
            i += 1
            continue
        target = (target or "") + a
        i += 1
        break
    cmd = args[i:]
    host = (target or "").split("@")[-1]
    if host != "testhost":
        sys.stderr.write("ssh: Could not resolve hostname %s: Name or service not known\\n" % host)
        sys.exit(255)
    if not cmd:
        sys.stderr.write("ssh: interactive sessions are not available here\\n")
        sys.exit(1)
    env = dict(os.environ, HOME=os.environ["REMOTE_HOME"], PATH=os.environ["STUB_BIN"] + ":" + os.environ["PATH"])
    os.chdir(os.environ["REMOTE_HOME"])
    sys.exit(subprocess.call(["/bin/sh", "-c", " ".join(cmd)], env=env))
''')

SYSTEMCTL_STUB = textwrap.dedent('''\
    #!/usr/bin/env python3
    import os, sys
    args = [a for a in sys.argv[1:] if not a.startswith("--no-")]
    user = "--user" in args
    pos = [a for a in args if not a.startswith("-")]
    verb, unit = (pos + ["", ""])[:2]
    unit = unit.removesuffix(".service")
    state = os.path.join(os.environ["REMOTE_HOME"], ".state")
    if unit != "app" or not user:
        if verb == "is-active":
            print("inactive"); sys.exit(3)
        sys.stderr.write("Unit %s.service could not be found.\\n" % unit); sys.exit(4)
    if verb == "status":
        print("\\u25cf app.service - calc test app\\n     Loaded: loaded (~/.config/systemd/user/app.service; enabled)\\n"
              "     Active: active (running) since Sat 2026-10-31")
    elif verb == "is-active":
        print("active")
    elif verb in ("restart", "start", "reload"):
        open(os.path.join(state, "restarted"), "a").write("restart\\n")
    else:
        sys.stderr.write("unsupported verb\\n"); sys.exit(1)
''')


class Sandbox:
    def __init__(self, base: Path):
        self.base = base
        self.proj = base / "proj"
        self.remote = base / "remote"
        self.bin = base / "bin"
        for d in (self.proj / "calc", self.proj / "tests", self.proj / "scripts", self.remote / "app" / "calc",
                  self.remote / ".state", self.remote / ".config" / "systemd" / "user", self.bin):
            d.mkdir(parents=True, exist_ok=True)
        (self.proj / "calc" / "__init__.py").write_text("")
        (self.proj / "calc" / "core.py").write_text(CORE_BUGGY)
        (self.proj / "tests" / "test_core.py").write_text(TEST_SRC)
        (self.proj / "scripts" / "deploy.sh").write_text(DEPLOY_SH)
        (self.proj / "conftest.py").write_text("")
        (self.remote / "app" / "calc" / "core.py").write_text(CORE_BUGGY)
        (self.remote / ".config" / "systemd" / "user" / "app.service").write_text("[Service]\nExecStart=/bin/true\n")
        for name, src in (("ssh", SSH_STUB), ("systemctl", SYSTEMCTL_STUB)):
            f = self.bin / name
            f.write_text(src)
            f.chmod(f.stat().st_mode | stat.S_IXUSR)
        git = lambda *a: subprocess.run(["git", *a], cwd=self.proj, check=True, capture_output=True)  # noqa: E731
        git("init", "-q")
        git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "initial")
        self.env = {"REMOTE_HOME": str(self.remote), "STUB_BIN": str(self.bin), "PATH": f"{self.bin}:{os.environ['PATH']}"}

    def apply_env(self, monkeypatch) -> None:
        for k, v in self.env.items():
            monkeypatch.setenv(k, v)

    # observable outcomes, read from disk and nothing else
    def local_core(self) -> str:
        return (self.proj / "calc" / "core.py").read_text()

    def remote_core(self) -> str:
        return (self.remote / "app" / "calc" / "core.py").read_text()

    def restarted(self) -> bool:
        return (self.remote / ".state" / "restarted").exists()

    def tests_pass_now(self) -> bool:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=self.proj, capture_output=True)
        return r.returncode == 0


# ---------------------------------------------------------------------------------------------------- the model
def _text(m: dict) -> str:
    c = m.get("content")
    return c if isinstance(c, str) else " ".join(str(p.get("text", "")) for p in (c or []) if isinstance(p, dict))


class View:
    """What the model can see in a request: tool calls with their results, and every user-side note."""

    def __init__(self, messages: list[dict]):
        self.messages = messages
        calls: dict[str, tuple[str, dict]] = {}
        self.results: list[tuple[str, dict, str]] = []
        for m in messages:
            if m.get("role") == "assistant":
                for c in m.get("tool_calls") or []:
                    fn = c["function"]
                    calls[c["id"]] = (fn["name"], json.loads(fn["arguments"] or "{}"))
            elif m.get("role") == "tool" and m.get("tool_call_id") in calls:
                name, args = calls[m["tool_call_id"]]
                self.results.append((name, args, _text(m)))
        users = [_text(m) for m in messages if m.get("role") == "user"]
        self.notes = "\n".join(u for u in users if "TASK STATE" in u or "<conversation-checkpoint>" in u or "[sarness]" in u)
        self.low_notes = self.notes.lower()
        self.compacted = any("<conversation-checkpoint>" in u or "Earlier steps were compacted" in u for u in users)
        self.nudged_last = bool(users) and users[-1].startswith("[sarness-guard]")

    def saw(self, needle: str, result: str | None = None) -> bool:
        for name, args, text in self.results:
            cmd = str(args.get("command") or args.get("path") or "")
            if needle in cmd and (result is None or result in text):
                return True
        return False

    def noted(self, needle: str) -> bool:
        return needle.lower() in self.low_notes


class Persona:
    """The scripted model. `stubborn` ignores the task-state note's advice once after each compaction and repeats old
    diagnostics (what the compacting model in the Trinodal sessions did); an attentive persona uses it."""

    def __init__(self, dialect: str = "tui", stubborn: bool = False, proj: str = ".", ask_first: bool = True):
        self.d = dialect
        self.stubborn = stubborn
        self.proj = proj
        self.asked = not ask_first
        self.glued = self.doubted = self.double_read = self.tried_system = False
        self.premature_1 = self.premature_2 = False
        self.stubborn_epochs: set[int] = set()   # one entry per compaction (its checkpoint text is unique)
        self.milestone = ""
        self.log: list[str] = []
        self.requests = 0

    # tool dialects
    @property
    def sh(self):
        return "shell" if self.d == "tui" else "run_command"

    @property
    def rd(self):
        return "read" if self.d == "tui" else "read_file"

    def call(self, name, args):
        self.log.append(f"{name} {json.dumps(args)[:90]}")
        return ("call", name, args)

    def text(self, t):
        self.log.append("text " + t[:60])
        return ("text", t)

    def ssh(self, remote: str, glued: bool = False):
        return self.call(self.sh, {"command": f"ssh{'' if glued else ' '}{TARGET} {shlex.quote(remote)}"})

    def respond(self, messages: list[dict]) -> list[tuple]:
        """The model's next turn: a list of ("call", tool, args) and at most one ("text", ...)."""
        self.requests += 1
        v = View(messages)
        epoch = hash(next((_text(m) for m in reversed(messages) if "<conversation-checkpoint>" in _text(m)
                           or _text(m).startswith("[sarness] Earlier steps were compacted")), ""))
        out = self._decide(v, epoch)
        return out if isinstance(out, list) else [out]

    def _decide(self, v: View, epoch: int):
        if not self.asked:
            self.asked = True
            return self.text("I have read the task. Should I proceed with the SSH connection now?")
        # --- reach the server and look at it
        layout_known = v.saw("git status", "not a git repository") or v.noted("deployment copy")
        if not (v.saw(TARGET) or v.noted(HOST)) or not layout_known:
            if not self.glued:
                self.glued = True
                return self.ssh("cd ~/app && git status", glued=True)
            if v.saw("sshfylume", "command not found") and not v.saw("ssh fylume", None):
                return self.ssh("cd ~/app && git status")
            if not layout_known:
                return self.ssh("cd ~/app && git status")
        if not self.doubted and not v.noted("deployment copy"):
            self.doubted = True                                   # "not a git repository... maybe broken? check again"
            return self.ssh("cd ~/app && git status")
        # --- how the service is run
        scope_known = v.saw("--user", "Loaded: loaded") or v.noted("systemctl --user")
        if self.stubborn and v.compacted and epoch not in self.stubborn_epochs:
            self.stubborn_epochs.add(epoch)
            return self.ssh("systemctl status app --no-pager")    # the habit the note is meant to end
        if not scope_known:
            if not self.tried_system:
                self.tried_system = True
                return self.ssh("systemctl status app --no-pager")
            return self.ssh("systemctl --user status app --no-pager")
        # --- read the code (three reads in one turn), and wrongly read the same file again
        read_core = v.saw("calc/core.py", "def mean")
        deploy_known = v.saw("deploy.sh", "rsync") or v.noted("rsync")
        edited = v.noted("Files changed") or any(n in ("edit", "edit_file") for n, _, _ in v.results)
        if not edited:
            if not (read_core and deploy_known) and not (v.compacted and deploy_known):
                if not read_core or not deploy_known:
                    todo = []
                    if not read_core:
                        todo.append(self.call(self.rd, {"path": f"{self.proj}/calc/core.py"}))
                    if not v.saw("tests/test_core.py"):
                        todo.append(self.call(self.rd, {"path": f"{self.proj}/tests/test_core.py"}))
                    if not deploy_known:
                        todo.append(self.call(self.rd, {"path": f"{self.proj}/scripts/deploy.sh"}))
                    if todo:
                        return todo
            if read_core and not self.double_read:
                self.double_read = True
                self.milestone = "inspected"
                return self.call(self.rd, {"path": f"{self.proj}/calc/core.py"})      # "just to be sure"
            if not read_core:                                       # compacted away: needed to edit
                return self.call(self.rd, {"path": f"{self.proj}/calc/core.py"})
            self.milestone = "inspected"
            return self.call("edit" if self.d == "tui" else "edit_file",
                             {"path": f"{self.proj}/calc/core.py",
                              **({"oldString": "(len(values) + 1)", "newString": "len(values)"} if self.d == "tui"
                                 else {"old_string": "(len(values) + 1)", "new_string": "len(values)"})})
        # --- tests against the changed code
        tested = v.noted("(current)") and "exit 0" in v.low_notes or self._test_after_last_edit(v)
        if not tested:
            self.milestone = "tested"
            return self.call(self.sh, {"command": f"cd {self.proj} && {sys.executable} -m pytest -q -p no:cacheprovider"})
        if not self.premature_1 and not (v.noted("Deployments:")):
            self.premature_1 = True
            return self.text("All done! The bug is fixed (mean() now divides by len(values)) and the tests pass. Summary of the work above.")
        # --- deploy
        deployed = v.noted("Deployments:") or v.saw("deploy.sh", "deployed")
        if not deployed:
            self.milestone = "deployed"
            return self.call(self.sh, {"command": f"cd {self.proj} && sh scripts/deploy.sh"})
        if not self.premature_2 and not v.noted("verified by"):
            self.premature_2 = True
            return self.text("Deployment complete. Everything is in place.")
        # --- independent verification
        verified = v.noted("verified by") or (v.saw("is-active") and v.saw("~/app/calc/core.py"))
        if not verified:
            self.milestone = "verified"
            return [self.ssh("systemctl --user is-active app"), self.ssh("cat ~/app/calc/core.py")]
        return self.text("Done. mean() now divides by len(values); 2 tests passed; deployed with scripts/deploy.sh to the test "
                         "server and verified there (service active, remote copy contains the fix).")

    @staticmethod
    def _test_after_last_edit(v: View) -> bool:
        last_edit = max((i for i, (n, _, _) in enumerate(v.results) if n in ("edit", "edit_file")), default=-1)
        for i, (n, args, text) in enumerate(v.results):
            if i > last_edit and "pytest" in str(args.get("command", "")) and ("passed" in text or "exit code: 0" in text):
                return True
        return False


# ---------------------------------------------------------------------------------------------------- tool execution
def run_tui_tool(sb: Sandbox, name: str, args: dict) -> str:
    """The terminal's tools, as far as the scenario needs them, with the result text the terminal produces."""
    if name == "shell":
        r = subprocess.run(["/bin/sh", "-c", args["command"]], cwd=sb.proj, capture_output=True, text=True, env={**os.environ, **sb.env})
        out = (r.stdout + r.stderr)
        return out + (f"\n Exited with code {r.returncode}" if r.returncode else "")
    if name == "read":
        p = Path(args["path"])
        lines = p.read_text().splitlines()
        return f"Read file {p}, lines 1-{len(lines)}\n" + "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines, 1))
    if name == "edit":
        p = Path(args["path"])
        text = p.read_text()
        if args["oldString"] not in text:
            return "Error: oldString not found in file"
        p.write_text(text.replace(args["oldString"], args["newString"], 1))
        return f"Edited file {p}"
    return f"Error: unsupported tool {name}"


class BridgeHarness:
    """The terminal's side of the conversation, against the real bridge."""

    def __init__(self, server, sb: Sandbox, persona: Persona, *, session="ses_scenario", compact_at=("inspected", "tested", "deployed"),
                 keep_request=True, task=TASK, post: Callable | None = None):
        self.server, self.sb, self.persona, self.session = server, sb, persona, session
        self.compact_at, self.keep_request, self.task = set(compact_at), keep_request, task
        self.history: list[dict] = [system(), user(task)]
        self.compactions = 0
        self.texts: list[str] = []
        self.rounds = 0
        self.executed: list[tuple[str, dict]] = []
        self.post = post or self._post

    def _post(self, messages):
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.server.key}", "x-opencode-session-id": self.session}
        body = json.dumps({"model": "local:qwen-test", "messages": messages, "stream": False, "tools": []}).encode()
        resp = urllib.request.urlopen(urllib.request.Request(self.server.url + "/chat/completions", data=body, headers=headers), timeout=30)
        return json.loads(resp.read())["choices"][0]

    def compact(self) -> None:
        """Replace the history with a checkpoint, as the terminal's compaction does: a lossy summary of the work so far, and
        (with the fork patch) the user's request word for word beside it."""
        self.compactions += 1
        # the terminal first asks the model to summarise the whole history; the bridge sees it before it is replaced
        self.post(self.history + [user("You MUST summarize the conversation above into a structured summary that will be given to another agent")])
        summary = (f"## Objective\n- Fix a bug on the test server project and deploy it.\n## Work State\n### Completed\n"
                   f"- some inspection ({self.compactions} compaction)\n## Next Move\n1. (none)\n")
        cp = checkpoint(summary)
        if self.keep_request:
            cp["content"] = cp["content"].replace("</conversation-checkpoint>",
                                                  f"<recent-context>\n[User]: {self.task}\n</recent-context>\n</conversation-checkpoint>")
        self.history = [system(), cp]

    def run(self, max_rounds: int = 60) -> str:
        final = ""
        for _ in range(max_rounds):
            choice = self.post(self.history)
            msg = choice["message"]
            text, calls = msg.get("content") or "", msg.get("tool_calls") or []
            if text:
                self.texts.append(text)
            if not calls:
                final = text
                break
            self.history.append({"role": "assistant", "content": text, "tool_calls": calls})
            for c in calls:
                args = json.loads(c["function"]["arguments"] or "{}")
                self.executed.append((c["function"]["name"], args))
                self.history.append({"role": "tool", "tool_call_id": c["id"],
                                     "content": run_tui_tool(self.sb, c["function"]["name"], args)})
            self.rounds += 1
            if self.persona.milestone in self.compact_at:
                self.compact_at.discard(self.persona.milestone)
                self.compact()
        return final


class ScenarioEngine:
    """An engine for the bridge or the native loop that answers from a Persona."""

    def __init__(self, persona: Persona, *, summariser: Callable[[list], str] | None = None):
        self.persona = persona
        self.calls: list[list[dict]] = []
        self.live = {"state": "ready", "model_id": "qwen-test", "ctx": 131072, "gpu_layers": 80}
        self._n = 0

    def llm_status(self):
        return self.live

    def completion(self, model, messages, tools, params, on_event=None, on_open=None):
        self.calls.append(json.loads(json.dumps(messages)))
        if _text(messages[-1]).startswith("You MUST summarize the conversation above"):
            if on_event:
                on_event({"type": "delta", "content": "## Objective\n- summary", "reasoning": ""})
            return {"type": "done", "message": {"role": "assistant", "content": "## Objective\n- summary"}, "reasoning": "",
                    "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        turn = self.persona.respond(messages)
        text = next((t[1] for t in turn if t[0] == "text"), "")
        calls = []
        for t in turn:
            if t[0] == "call":
                self._n += 1
                calls.append({"id": f"call_{self._n}", "type": "function", "function": {"name": t[1], "arguments": json.dumps(t[2])}})
        if text and on_event:
            on_event({"type": "delta", "content": text, "reasoning": ""})
        msg: dict[str, Any] = {"role": "assistant", "content": text}
        if calls:
            msg["tool_calls"] = calls
        return {"type": "done", "message": msg, "reasoning": "", "finish_reason": "tool_calls" if calls else "stop",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
