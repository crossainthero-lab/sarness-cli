"""The agent guard as the terminal meets it: through the real HTTP bridge, with a scripted model."""
import json
import urllib.request

import pytest

from guardkit import assistant, call, checkpoint, edit, read, shell, system, tool, user
from sarness_cli.engine import EngineError
from sarness_cli.guard import Ledger
from test_openai_bridge import MODEL, FakeBridgeEngine, done, serve, sse

TASK = ("SSH into fylume@192.168.20.193, inspect Trinodal, find the issue, implement the correction, run tests and deploy. "
        "Proceed autonomously.")


def delta(text):
    return {"type": "delta", "content": text, "reasoning": ""}


def text_turn(text):
    return ([delta(text)], done(text, calls=(), finish="stop"))


def call_turn(name, args, text=""):
    c = {"id": f"call_{abs(hash(json.dumps(args))) % 10**6}", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}
    return ([delta(text)] if text else [], done(text, calls=(c,), finish="tool_calls"))


def post(server, messages, *, session="ses_guard", stream=True, parent=None):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {server.key}", "x-opencode-session-id": session}
    if parent:
        headers["x-session-affinity"] = parent
    body = json.dumps({"model": MODEL["ref"], "messages": messages, "stream": stream, "tools": []}).encode()
    resp = urllib.request.urlopen(urllib.request.Request(server.url + "/chat/completions", data=body, headers=headers), timeout=20)
    raw = resp.read().decode()
    if not stream:
        return json.loads(raw)["choices"][0]
    text, calls, finish = "", [], None
    for ev in sse(raw):
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            text += d.get("content") or ""
            calls += d.get("tool_calls") or []
            finish = ch.get("finish_reason") or finish
    return {"text": text, "calls": calls, "finish": finish}


@pytest.fixture
def bridge():
    made = []

    def start(turns, **kw):
        engine = FakeBridgeEngine(turns)
        server = serve(engine, **kw)
        made.append(server)
        return server, engine
    yield start
    for server in made:
        server.shutdown()
        server.server_close()


def convo(*turns, task=TASK):
    out = [system(), user(task)]
    for t in turns:
        out += t
    return out


def test_a_permission_question_is_continued_and_the_next_turn_makes_a_real_call(bridge):
    server, engine = bridge([text_turn("I see the plan. Should I proceed with the SSH connection now?"),
                             call_turn("shell", {"command": "ssh fylume@192.168.20.193 'ls ~/trinodal-ai'"})])
    out = post(server, convo())
    assert out["text"].startswith("I see the plan. Should I proceed with the SSH connection now?"), "the model's words stay in the transcript"
    assert out["finish"] == "tool_calls" and json.loads(out["calls"][0]["function"]["arguments"])["command"].startswith("ssh fylume@")
    assert len(engine.calls) == 2
    second = engine.calls[1][1]
    assert second[-2] == {"role": "assistant", "content": "I see the plan. Should I proceed with the SSH connection now?"}
    assert second[-1]["role"] == "user" and second[-1]["content"].startswith("[sarness-guard]") and "Do not ask" in second[-1]["content"]
    assert [e["kind"] for e in server.guard_events] == ["confirmation"]


def test_the_same_works_without_streaming(bridge):
    server, _ = bridge([text_turn("Shall I connect to the server?"), call_turn("shell", {"command": "ssh fylume@192.168.20.193 ls"})])
    choice = post(server, convo(), stream=False)
    assert choice["finish_reason"] == "tool_calls" and choice["message"]["tool_calls"][0]["function"]["name"] == "shell"
    assert choice["message"]["content"].startswith("Shall I connect")


def test_a_model_that_never_acts_is_stopped_after_bounded_attempts_with_a_checkpoint(bridge):
    server, engine = bridge([text_turn("Should I proceed?")])
    out = post(server, convo())
    assert out["calls"] == [] and out["finish"] == "stop"
    assert "[sarness] Stopped" in out["text"] and "task state is saved" in out["text"]
    assert len(engine.calls) == 4, "one original call and three nudged retries, then the guard stopped"
    led = Ledger.open("ses_guard")
    assert any(b["key"].startswith("guard:") for b in led.d["blockers"]) and led.interventions("give-up")
    # a later turn of the same conversation is not pushed again until the user says something new
    again = post(server, convo(), session="ses_guard")
    assert len(engine.calls) == 5 and "Stopped" not in again["text"]


def test_no_tool_call_is_ever_invented(bridge):
    server, engine = bridge([text_turn("I'll deploy now."), text_turn("Deploying now, one moment."), text_turn("Okay, deploying."),
                             text_turn("Done deploying.")])
    out = post(server, convo())
    assert out["calls"] == [] and out["finish"] == "stop"


def test_a_malformed_ssh_command_reaches_the_terminal_repaired(bridge):
    server, _ = bridge([call_turn("shell", {"command": 'sshfylume@192.168.20.193 "cd ~/trinodal-ai && git status"'})])
    out = post(server, convo())
    assert json.loads(out["calls"][0]["function"]["arguments"])["command"] == 'ssh fylume@192.168.20.193 "cd ~/trinodal-ai && git status"'
    assert Ledger.open("ses_guard").interventions("ssh-repair")


def test_a_repeat_of_an_established_inspection_is_replaced_by_the_models_next_choice(bridge):
    history = convo(read("/w/app.py"))
    server, engine = bridge([call_turn("read", {"path": "/w/app.py"}), call_turn("edit", {"path": "/w/app.py", "oldString": "a", "newString": "b"})])
    out = post(server, history)
    assert json.loads(out["calls"][0]["function"]["arguments"])["path"] == "/w/app.py" and out["calls"][0]["function"]["name"] == "edit"
    assert "repeats work whose result is already established" in engine.calls[1][1][-1]["content"]


def test_the_wrong_systemd_scope_is_corrected_by_the_recorded_fact(bridge):
    ok = shell("ssh fylume@192.168.20.193 'systemctl --user status trinodal-app'", "Loaded: loaded (user)\nActive: active (running)")
    server, engine = bridge([call_turn("shell", {"command": "ssh fylume@192.168.20.193 'systemctl status trinodal-app'"}),
                             call_turn("shell", {"command": "ssh fylume@192.168.20.193 'systemctl --user restart trinodal-app'"})])
    out = post(server, convo(ok))
    assert "--user" in json.loads(out["calls"][0]["function"]["arguments"])["command"]
    assert "--user" in engine.calls[1][1][-1]["content"]


def test_after_compaction_the_model_is_given_the_objective_the_facts_and_the_next_action(bridge):
    found = [shell("ssh fylume@192.168.20.193 'systemctl --user status trinodal-app'", "Loaded: loaded (user)\nActive: active"),
             shell("ssh fylume@192.168.20.193 'cd ~/trinodal-ai && git status'", "fatal: not a git repository", code=128)]
    server, engine = bridge([text_turn("ok")])
    post(server, convo(*found))
    assert engine.calls[0][1][-1]["role"] != "user" or "[sarness-guard]" not in engine.calls[0][1][-1]["content"], "no note before compaction"
    for n in range(3):                       # three compactions in a row: the state survives each
        post(server, [system(), user(TASK), checkpoint(f"## Objective\n- summary {n}\n(the model's own summary lost the details)")])
        note = engine.calls[-1][1][-1]["content"]
        assert note.startswith("[sarness-guard] TASK STATE") and TASK[:50] in note
        assert "systemctl --user" in note and "deployment copy" in note and "NEXT ACTION" in note
    assert len(Ledger.open("ses_guard").d["compactions"]) == 3


def test_summary_and_title_requests_are_untouched(bridge):
    server, engine = bridge([text_turn("Should I proceed? ## Objective ...")])
    out = post(server, [system(), user(TASK), checkpoint(), user("You MUST summarize the conversation above into a structured summary")])
    assert len(engine.calls) == 1 and out["text"].startswith("Should I proceed?")
    assert engine.calls[0][1][-1]["content"].startswith("You MUST summarize")


def test_a_subagent_session_has_its_own_ledger_that_points_at_its_parent(bridge):
    server, _ = bridge([text_turn("done")])
    post(server, [system(), user("Inspect the logs and report")], session="ses_child", parent="ses_parent")
    assert Ledger.open("ses_child").d["parent"] == "ses_parent" and not Ledger.open("ses_parent").d["ops"]


def test_requests_without_a_session_id_are_passed_through_unchanged(bridge):
    server, engine = bridge([text_turn("Should I proceed?")])
    req = urllib.request.Request(server.url + "/chat/completions", method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {server.key}"},
        data=json.dumps({"model": MODEL["ref"], "messages": convo(), "stream": False}).encode())
    assert json.loads(urllib.request.urlopen(req, timeout=10).read())["choices"][0]["message"]["content"] == "Should I proceed?"
    assert len(engine.calls) == 1


def test_the_guard_can_be_switched_off(bridge):
    server, engine = bridge([text_turn("Should I proceed?")], guard=False)
    assert post(server, convo(), stream=False)["message"]["content"] == "Should I proceed?" and len(engine.calls) == 1


def test_a_failing_follow_up_never_fails_the_users_turn(bridge):
    server, engine = bridge([text_turn("Should I proceed?"), EngineError("engine hiccup", "http", retryable=False)])
    out = post(server, convo(), stream=False)
    assert out["message"]["content"] == "Should I proceed?" and out["finish_reason"] == "stop"


def test_the_ledger_survives_a_restart_and_a_resumed_session_continues_from_it(bridge):
    server, _ = bridge([text_turn("ok")])
    post(server, convo(edit("/w/a.py"), shell("pytest -q", "3 passed")))
    server2, engine2 = bridge([text_turn("ok")])               # a new launcher process: same state directory
    post(server2, [system(), user(TASK), checkpoint("resumed after a restart")])
    note = engine2.calls[-1][1][-1]["content"]
    assert "Files changed" in note and "pytest -q" in note and "Still to do" in note
