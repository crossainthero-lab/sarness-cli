"""The multi-stage task in the real Sarness terminal: the actual binary in a pseudo-terminal, a scripted model behind the real
bridge, a small context window so the terminal compacts the session by itself, and real files, tests and deployment at the end.

Skipped without an installed terminal (CI has none); the install checks on a real machine run it."""
import json
import os
import signal
import time

import pytest

from ctrl_c_support import Dispatch, command, configure, is_title_request, marked_processes, unique_marker
from fake_engine_server import FakeEngineServer, TOKEN, done
from ptyharness import PtySession
from scenario_rig import CORE_FIXED, TASK, Persona, Sandbox
from sarness_cli.guard import Ledger
from test_ctrl_c_tui import READY, TUI, submit

pytestmark = [
    pytest.mark.skipif(TUI is None, reason="no Sarness terminal (sarness-tui) installed; run `sarness tui install`"),
    pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs Linux /proc"),
]
SUMMARY = ("## Objective\n- Fix a bug in a project and deploy it to a test server.\n## Work State\n### Completed\n- some inspection\n"
           "## Next Move\n1. continue\n")


def scenario_server(persona: Persona, ctx: int):
    server = FakeEngineServer([])
    summaries = {"n": 0}
    sizes: list[int] = []

    def script(body):
        if is_title_request(body):
            yield done("Scenario")
            return
        text = json.dumps(body.get("messages", []))
        if "You MUST use this format" in text or "Update the existing checkpoint" in text:
            summaries["n"] += 1
            yield {"type": "delta", "content": SUMMARY, "reasoning": ""}
            yield done(SUMMARY)
            return
        turn = persona.respond(body["messages"])
        text_out = next((t[1] for t in turn if t[0] == "text"), "")
        calls = [(t[1], t[2]) for t in turn if t[0] == "call"]
        if text_out:
            yield {"type": "delta", "content": text_out, "reasoning": ""}
        final = done(text_out, calls=calls)
        # like the real engine: the prompt tokens are the size of what was sent (messages and tool definitions)
        final["usage"]["prompt_tokens"] = int((len(json.dumps(body["messages"])) + len(json.dumps(body.get("tools") or []))) / 3.6)
        sizes.append(final["usage"]["prompt_tokens"])
        yield final

    server.turns = Dispatch(script)
    server.llm.update(state="ready", model_id="coder@Q4", model_name="Coder Q4", ctx=ctx, gpu_layers=999, last_used=time.time())
    server.summaries = summaries
    server.sizes = sizes
    return server


def run_in_terminal(tmp_path, monkeypatch, *, ctx=24576, permissions="full-access", guard="on", stubborn=False):
    sb = Sandbox(tmp_path / "sb")
    persona = Persona("tui", stubborn=stubborn, proj=str(sb.proj))
    server = scenario_server(persona, ctx)
    configure(server, runtime="tui", permissions=permissions)
    for name in ("data", "cache"):
        (tmp_path / name).mkdir()
    marker = unique_marker("SCENARIO")
    env = dict(os.environ, TERM="xterm-256color", SARNESS_TUI_BIN=TUI, SARNESS_GUARD=guard, SARNESS_CTRLC_MARKER=marker,
               XDG_DATA_HOME=str(tmp_path / "data"), XDG_CACHE_HOME=str(tmp_path / "cache"), **sb.env)
    session = PtySession([*command(), "-C", str(sb.proj), "code"], env=env, cwd=str(sb.proj), rows=50, cols=140)
    return sb, persona, server, session, marker


def finish(session, server, marker):
    try:
        session.ctrl_c()
        session.wait_exit(8)
    finally:
        session.cleanup()
        server.stop()
        for pid in marked_processes(marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def test_the_real_terminal_finishes_the_task_through_its_own_compactions(tmp_path, monkeypatch):
    sb, persona, server, session, marker = run_in_terminal(tmp_path, monkeypatch)
    try:
        session.wait_for(READY, 40)
        session.pump(0.5)
        submit(session, TASK)
        session.wait_screen("Done. mean() now divides", 180)
        session.pump(1.0)
        ledgers = list((tmp_path / "state" / "sarness-cli" / "ledgers").glob("ses_*.json"))
        assert ledgers, "the terminal's session has a ledger"
        led = Ledger.open(ledgers[0].stem)
        assert sb.local_core() == CORE_FIXED and sb.tests_pass_now() and sb.remote_core() == CORE_FIXED and sb.restarted()
        assert led.unmet() == [], led.unmet()
        kinds = [i["kind"] for i in led.d["interventions"]]
        assert "confirmation" in kinds and "ssh-repair" in kinds and "premature-complete" in kinds and "give-up" not in kinds
        assert server.summaries["n"] >= 2 and len(led.d["compactions"]) >= 2, "the terminal compacted the session by itself, more than once"
        assert led.d["deploys"][-1]["verified"] and led.d["tests"][-1]["rev"] == led.rev
    finally:
        finish(session, server, marker)


def test_the_original_failure_reproduces_in_the_real_terminal_without_the_guard(tmp_path, monkeypatch):
    sb, persona, server, session, marker = run_in_terminal(tmp_path, monkeypatch, guard="off")
    try:
        session.wait_for(READY, 40)
        session.pump(0.5)
        submit(session, TASK)
        session.wait_screen("Should I proceed with the SSH connection now?", 60)
        session.pump(3.0)
        assert sb.local_core() != CORE_FIXED and not sb.restarted(), "the run stopped at the question; nothing was done"
    finally:
        finish(session, server, marker)


def test_ask_mode_keeps_the_real_approval_prompt_and_a_rejection_is_final(tmp_path, monkeypatch):
    """The guard stops the chat question ("Should I proceed?"), never the real approval: outside bypass the terminal still asks,
    shows the command that will really run (repaired), and nothing reaches the server when the user rejects it."""
    sb, persona, server, session, marker = run_in_terminal(tmp_path, monkeypatch, permissions="ask")
    try:
        session.wait_for(READY, 40)
        session.pump(0.5)
        submit(session, TASK)
        session.wait_screen("Permission required", 40)
        shown = session.screen()
        assert "ssh fylume@testhost 'cd ~/app && git status'" in shown and "sshfylume" not in shown
        assert sb.ssh_calls() == [], "nothing ran before the answer"
        for _ in range(2):                                       # Allow once -> Always allow -> Reject
            session.send("\x1b[C")
            session.pump(0.3)
        session.send("\r")
        session.wait_screen("declined", 20)
        session.pump(2.0)
        assert sb.ssh_calls() == [] and not sb.restarted() and sb.local_core() != CORE_FIXED, "a rejection is final"
        assert len([line for line in persona.log if line.startswith("shell")]) == 1, "the model was not pushed on after a rejection"
    finally:
        finish(session, server, marker)
