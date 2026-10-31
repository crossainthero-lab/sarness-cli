"""`sarness code` startup: get the right model ready without ever evicting someone else's (0.3.1 regression)."""
import threading
import time

import pytest

from sarness_cli import cli, config
from sarness_cli.engine import EngineError
from sarness_cli.runtime import ModelError
from sarness_cli.startup import StartupCancelled, StartupUI, ensure_ready, load_and_wait, wait_settled
from fake_engine_server import MODELS, TOKEN, FakeEngineServer

CODER, SMALL = MODELS[0], MODELS[1]
GPU = {"vram_total_mb": 124608, "vram_free_mb": 100000, "compute_processes": []}


class Engine:
    """In-process engine double: llm_status / llm_load / get_json('/v1/system')."""

    def __init__(self, state="stopped", model_id=None, ctx=None, active=0, free_mb=100000, load=None):
        self.st = {"state": state, "model_id": model_id, "model_name": model_id, "ctx": ctx, "gpu_layers": 999 if model_id else None,
                   "active_streams": active, "last_used": time.time() - 600, "vram_estimate_mb": 20000 if model_id else None, "error": None}
        self.free = free_mb
        self.loads, self.unloads = [], 0
        self.load_impl = load

    def llm_status(self):
        return dict(self.st)

    def get_json(self, path):
        assert path == "/v1/system"
        return {"hardware": {"gpus": [dict(GPU, vram_free_mb=self.free)]}}

    def llm_load(self, model_id, ctx=None, gpu_layers=None):
        self.loads.append((model_id, ctx, gpu_layers))
        if self.load_impl:
            return self.load_impl(self, model_id, ctx)
        self.st.update(state="ready", model_id=model_id, model_name=model_id, ctx=ctx, gpu_layers=999, active_streams=0)
        return dict(self.st)


def ui(interactive=False, choices=(), picks=()):
    log, asked = [], []
    choices, picks = list(choices), list(picks)

    def choose(title, sub, options):
        asked.append((title, sub, [k for k, _, _ in options]))
        return choices.pop(0)

    def pick(models, current):
        asked.append(("pick", current, None))
        p = picks.pop(0)
        return next((m for m in models if m["ref"] == p), None) if p else None

    u = StartupUI(interactive, log.append, choose, pick)
    u.log, u.asked = log, asked
    return u


# 1. correct model loaded -> immediately, nothing loaded or unloaded
def test_configured_model_already_loaded_is_used_as_is():
    eng = Engine("ready", "coder@Q4", ctx=131072)
    r = ensure_ready(eng, MODELS, "local:coder@Q4", ui())
    assert r.model["ref"] == "local:coder@Q4" and r.status["ctx"] == 131072 and not r.loaded_now
    assert eng.loads == []


# 2. nothing loaded -> load the configured model, wait for ready
def test_nothing_loaded_autoloads_the_configured_model_with_its_default_ctx():
    eng = Engine("stopped")
    r = ensure_ready(eng, MODELS, "local:coder@Q4", ui())
    assert eng.loads == [("coder@Q4", 16384, None)]
    assert r.loaded_now and r.status["state"] == "ready" and r.status["model_id"] == "coder@Q4"


def test_autoload_honours_the_context_setting_and_an_engine_error_state():
    eng = Engine("error")  # a previous load failed: nothing usable is loaded
    ensure_ready(eng, MODELS, "coder@Q4", ui(), wanted_ctx=32768)
    assert eng.loads == [("coder@Q4", 32768, None)]


def test_unsupported_or_non_chat_model_is_never_loaded():
    eng = Engine("stopped")
    bad = [dict(CODER, supported=False, unsupported_reason="needs a newer llama.cpp"), SMALL]
    with pytest.raises(ModelError, match="newer llama.cpp"):
        ensure_ready(eng, bad, "local:coder@Q4", ui())
    with pytest.raises(ModelError, match="not an LLM"):
        ensure_ready(eng, [dict(CODER, category="image")], "local:coder@Q4", ui())
    assert eng.loads == []


# 3. a different model is loaded
def test_different_model_loaded_scripted_run_fails_clearly_without_touching_it():
    eng = Engine("ready", "small@Q8", ctx=8192)
    with pytest.raises(ModelError) as ei:
        ensure_ready(eng, MODELS, "local:coder@Q4", ui())
    assert "--model local:small@Q8" in str(ei.value) and "--allow-model-swap" in str(ei.value)
    assert eng.loads == []


def test_different_model_loaded_interactive_use_loaded_is_the_default_choice():
    eng = Engine("ready", "small@Q8", ctx=8192)
    u = ui(True, choices=["use"])
    r = ensure_ready(eng, MODELS, "local:coder@Q4", u)
    assert r.model["ref"] == "local:small@Q8" and eng.loads == []
    title, sub, keys = u.asked[0]
    assert keys == ["use", "switch", "cancel"] and "Small Q8 is loaded" in sub and "Coder Q4" in sub


def test_different_model_loaded_interactive_switch_loads_the_default():
    eng = Engine("ready", "small@Q8", ctx=8192)
    r = ensure_ready(eng, MODELS, "local:coder@Q4", ui(True, choices=["switch"]))
    assert r.model["ref"] == "local:coder@Q4" and r.loaded_now and eng.loads[0][0] == "coder@Q4"


def test_switch_is_not_offered_while_the_loaded_model_is_generating():
    eng = Engine("ready", "small@Q8", ctx=8192, active=1)
    u = ui(True, choices=["cancel"])
    with pytest.raises(StartupCancelled):
        ensure_ready(eng, MODELS, "local:coder@Q4", u)
    assert u.asked[0][2] == ["use", "cancel"] and eng.loads == []
    with pytest.raises(ModelError, match="generating"):     # and --allow-model-swap does not override that
        ensure_ready(eng, MODELS, "local:coder@Q4", ui(), allow_swap=True)
    assert eng.loads == []


def test_switch_rechecks_for_a_request_that_started_while_the_menu_was_open():
    eng = Engine("ready", "small@Q8", ctx=8192)
    u = ui(True)
    u.choose = lambda *a: (eng.st.update(active_streams=1), "switch")[1]
    with pytest.raises(ModelError, match="became busy"):
        ensure_ready(eng, MODELS, "local:coder@Q4", u)
    assert eng.loads == []


def test_allow_model_swap_switches_in_a_scripted_run_but_not_into_too_little_memory():
    eng = Engine("ready", "small@Q8", ctx=8192)
    r = ensure_ready(eng, MODELS, "local:coder@Q4", ui(), allow_swap=True)
    assert r.model["ref"] == "local:coder@Q4" and eng.loads
    tight = Engine("ready", "small@Q8", ctx=8192, free_mb=1000)   # 1 GB free + 20 GB freed < 20 GB + 14.6 GB headroom
    with pytest.raises(ModelError, match="Not switching"):
        ensure_ready(tight, MODELS, "local:coder@Q4", ui(), allow_swap=True)
    assert tight.loads == []


# 4. no configured model
def test_no_default_interactive_selector_prefers_the_loaded_model_and_saves_it():
    eng = Engine("ready", "small@Q8", ctx=8192)
    u = ui(True, picks=["local:small@Q8"])
    r = ensure_ready(eng, MODELS, None, u)
    assert u.asked[0] == ("pick", "local:small@Q8", None)
    assert r.model["ref"] == "local:small@Q8" and r.save_default and eng.loads == []


def test_no_default_scripted_uses_the_loaded_model_without_prompting():
    eng = Engine("ready", "small@Q8", ctx=8192)
    r = ensure_ready(eng, MODELS, None, ui())
    assert r.model["ref"] == "local:small@Q8" and not r.save_default


def test_no_default_nothing_loaded_scripted_is_ambiguous_and_fails():
    with pytest.raises(ModelError, match="sarness model"):
        ensure_ready(Engine("stopped"), MODELS, None, ui())


def test_no_default_nothing_loaded_single_model_is_unambiguous():
    eng = Engine("stopped")
    ensure_ready(eng, [SMALL], None, ui())
    assert eng.loads[0][0] == "small@Q8"


def test_selector_cancel_starts_nothing():
    with pytest.raises(StartupCancelled):
        ensure_ready(Engine("stopped"), MODELS, None, ui(True, picks=[None]))


# 5. configured identifier differs from the engine ref
@pytest.mark.parametrize("configured", ["coder@Q4", "Coder Q4", "coder", "local:coder@Q4"])
def test_configured_identifier_variants_resolve_to_the_engine_ref(configured):
    eng = Engine("ready", "coder@Q4", ctx=65536)
    assert ensure_ready(eng, MODELS, configured, ui()).model["ref"] == "local:coder@Q4"


def test_ambiguous_fragment_that_covers_the_loaded_model_means_the_loaded_model():
    eng = Engine("ready", "small@Q8", ctx=8192)
    models = MODELS + [dict(SMALL, ref="local:small@Q4", name="Small Q4")]
    assert ensure_ready(eng, models, "small", ui()).model["ref"] == "local:small@Q8"


def test_stale_default_scripted_falls_back_to_the_loaded_model():
    eng = Engine("ready", "small@Q8", ctx=8192)
    u = ui()
    r = ensure_ready(eng, MODELS, "local:deleted-model@Q4", u)
    assert r.model["ref"] == "local:small@Q8" and any("not installed" in m for m in u.log)


def test_stale_default_interactive_reselects_and_saves():
    eng = Engine("stopped")
    r = ensure_ready(eng, MODELS, "local:deleted-model@Q4", ui(True, picks=["local:small@Q8"]))
    assert r.save_default and r.model["ref"] == "local:small@Q8" and eng.loads[0][0] == "small@Q8"


def test_stale_default_nothing_loaded_scripted_fails():
    with pytest.raises(ModelError, match="not available"):
        ensure_ready(Engine("stopped"), MODELS, "local:deleted-model@Q4", ui())


# 6. not enough memory / busy
def test_insufficient_memory_scripted_refuses_and_explains():
    eng = Engine("stopped", free_mb=20000)
    with pytest.raises(ModelError, match="safety headroom"):
        ensure_ready(eng, MODELS, "local:coder@Q4", ui())
    assert eng.loads == []


def test_insufficient_memory_interactive_offers_a_smaller_model():
    eng = Engine("stopped", free_mb=20000)
    u = ui(True, choices=["other"], picks=["local:small@Q8"])
    r = ensure_ready(eng, MODELS, "local:coder@Q4", u)
    assert u.asked[0][2] == ["other", "cancel"]      # never a "load anyway" option
    assert r.model["ref"] == "local:small@Q8" and eng.loads == [("small@Q8", 8192, None)]


def test_insufficient_memory_interactive_cancel():
    eng = Engine("stopped", free_mb=20000)
    with pytest.raises(StartupCancelled, match="headroom"):
        ensure_ready(eng, MODELS, "local:coder@Q4", ui(True, choices=["cancel"]))
    assert eng.loads == []


def test_allow_low_memory_overrides_the_memory_guard_only_when_asked():
    eng = Engine("stopped", free_mb=20000)
    ensure_ready(eng, MODELS, "local:coder@Q4", ui(), allow_low_memory=True)
    assert eng.loads


def test_waits_for_a_load_in_progress_instead_of_starting_another():
    eng = Engine("loading", "coder@Q4")
    seq = iter([{"state": "loading"}, {"state": "ready", "ctx": 65536}])
    real = eng.llm_status

    def status():
        nxt = next(seq, None)
        if nxt:
            eng.st.update(nxt)
        return real()
    eng.llm_status = status
    u = ui()
    st = wait_settled(eng, u, poll=0, sleep=lambda s: None)
    assert st["state"] == "ready" and any("already loading" in m for m in u.log) and eng.loads == []


def test_endless_foreign_load_times_out():
    eng = Engine("loading", "coder@Q4")
    t = iter(range(0, 10000, 100))
    with pytest.raises(ModelError, match="loading"):
        wait_settled(eng, ui(), timeout=250, poll=0, sleep=lambda s: None, clock=lambda: next(t))


# 8. load failures
def _prep(ctx=16384):
    from sarness_cli.runtime import Prepared
    return Prepared(ctx, None, True, False, [], True)


def test_load_http_error_is_reported():
    def fail(eng, mid, ctx):
        raise EngineError("HTTP 404: LLM 'coder@Q4' is not installed", "http")
    with pytest.raises(ModelError, match="not installed"):
        load_and_wait(Engine("stopped", load=fail), CODER, _prep(), ui(), poll=0.01)


def test_client_side_timeout_keeps_waiting_for_the_engine_to_become_ready():
    def slow(eng, mid, ctx):
        eng.st.update(state="loading", model_id=mid)
        threading.Timer(0.15, lambda: eng.st.update(state="ready", ctx=ctx)).start()
        raise EngineError("Timed out talking to the engine", "timeout", retryable=True)
    st = load_and_wait(Engine("stopped", load=slow), CODER, _prep(), ui(), poll=0.02)
    assert st["state"] == "ready" and st["model_id"] == "coder@Q4" and st["ctx"] == 16384


def test_engine_error_state_during_load_is_reported():
    def broken(eng, mid, ctx):
        eng.st.update(state="error", model_id=mid, error="llama-server exited (out of memory)")
        return dict(eng.st)
    with pytest.raises(ModelError, match="out of memory"):
        load_and_wait(Engine("stopped", load=broken), CODER, _prep(), ui(), poll=0.01)


def test_cancelled_load_is_reported():
    def cancelled(eng, mid, ctx):
        eng.st.update(state="stopped", model_id=None)
        return dict(eng.st)
    with pytest.raises(ModelError, match="cancelled"):
        load_and_wait(Engine("stopped", load=cancelled), CODER, _prep(), ui(), poll=0.01)


def test_someone_else_swapping_during_our_load_is_reported():
    def raced(eng, mid, ctx):
        eng.st.update(state="ready", model_id="small@Q8")
        return dict(eng.st)
    with pytest.raises(ModelError, match="another client"):
        load_and_wait(Engine("stopped", load=raced), CODER, _prep(), ui(), poll=0.01)


def test_load_that_never_becomes_ready_times_out():
    def hang(eng, mid, ctx):
        eng.st.update(state="loading", model_id=mid)
        time.sleep(1)
        return dict(eng.st)
    t0 = time.monotonic()
    with pytest.raises(ModelError, match="still not ready"):
        load_and_wait(Engine("stopped", load=hang), CODER, _prep(), ui(), timeout=0.1, poll=0.02)
    assert time.monotonic() - t0 < 1


def test_ctrl_c_while_waiting_stops_waiting_without_unloading(monkeypatch):
    def hang(eng, mid, ctx):
        eng.st.update(state="loading", model_id=mid)
        time.sleep(0.5)
        return dict(eng.st)
    eng = Engine("stopped", load=hang)
    calls = {"n": 0}
    real = eng.llm_status

    def status():
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real()
    eng.llm_status = status
    with pytest.raises(StartupCancelled, match="did not unload"):
        load_and_wait(eng, CODER, _prep(), ui(), poll=0.01)
    assert eng.unloads == 0


# End to end through the real CLI entry points, a real HTTP fake engine, and the launcher stubbed out.
@pytest.fixture
def code_env(monkeypatch, tmp_path):
    servers, launches = [], []

    def make(llm=None, model="local:coder@Q4", runtime=None):
        s = FakeEngineServer([], llm_status=llm)
        servers.append(s)
        cfg = config.load_config()
        cfg["profiles"]["fake"] = {"transport": "direct", "url": s.url, **({"model": model} if model else {})}
        cfg["settings"].update({"server": "fake", "effort": "high", "permissions": "auto", **({"runtime": runtime} if runtime else {})})
        config.save_config(cfg)
        config.set_secret("fake", TOKEN)
        return s

    def fake_launch(engine, models, selected, workspace, **kw):
        launches.append({"selected": selected, "workspace": workspace, **kw})
        return 0
    monkeypatch.setattr(cli, "launch_opencode", fake_launch)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    yield make, launches
    for s in servers:
        s.stop()


def run(*argv):
    with pytest.raises(SystemExit) as ei:
        cli.main(list(argv))
    return ei.value.code


def test_cli_code_with_nothing_loaded_autoloads_then_launches_with_the_live_status(code_env):
    make, launches = code_env
    s = make()
    assert run("code") == 0
    assert [b["model_id"] for b in s.loads] == ["coder@Q4"] and s.unloads == 0
    (launch,) = launches
    assert launch["selected"] == "local:coder@Q4" and launch["effort"] == "high" and launch["mode"] == "auto"
    assert launch["live"]["state"] == "ready" and launch["live"]["model_id"] == "coder@Q4" and launch["live"]["ctx"] == 16384
    assert launch["args"] is None and launch["tui_args"] == []


def test_cli_code_task_autoloads_and_runs_noninteractively(code_env):
    make, launches = code_env
    s = make()
    assert run("code", "fix", "the", "bug") == 0
    assert s.loads and launches[0]["args"] == ["run", "-m", "sarness/local:coder@Q4", "fix the bug"]


def test_cli_plain_sarness_with_opencode_runtime_autoloads(code_env):
    make, launches = code_env
    s = make(runtime="opencode")
    assert run() == 0
    assert s.loads and launches[0]["selected"] == "local:coder@Q4"


def test_cli_code_with_the_model_loaded_launches_immediately_and_keeps_session_flags(code_env):
    make, launches = code_env
    s = make(llm={"state": "ready", "model_id": "coder@Q4", "ctx": 131072, "gpu_layers": 999, "active_streams": 0, "last_used": 0})
    assert run("code", "--session", "ses_123", "--continue") == 0
    assert s.loads == [] and launches[0]["live"]["ctx"] == 131072
    assert launches[0]["tui_args"] == ["--session", "ses_123", "--continue"]


def test_cli_code_with_another_model_loaded_fails_clearly_when_scripted(code_env, capsys):
    make, launches = code_env
    s = make(llm={"state": "ready", "model_id": "small@Q8", "ctx": 8192, "gpu_layers": 999, "active_streams": 0, "last_used": 0})
    assert run("code", "do it") == 1
    err = capsys.readouterr().err
    assert "--allow-model-swap" in err and "local:small@Q8" in err
    assert s.loads == [] and s.unloads == 0 and launches == []
    assert run("code", "--model", "small", "do it") == 0 and launches[0]["selected"] == "local:small@Q8" and s.loads == []


def test_cli_code_without_a_default_saves_the_interactively_chosen_model(code_env, monkeypatch):
    make, launches = code_env
    make(model=None, llm={"state": "ready", "model_id": "small@Q8", "ctx": 8192, "gpu_layers": 999, "active_streams": 0, "last_used": 0})
    monkeypatch.setattr(cli, "startup_ui", lambda server: ui(True, picks=["local:small@Q8"]))
    assert run("code") == 0
    assert config.load_config()["profiles"]["fake"]["model"] == "local:small@Q8"
    assert launches[0]["selected"] == "local:small@Q8"


def test_cli_code_bypass_does_not_imply_a_model_swap(code_env):
    make, launches = code_env
    s = make(llm={"state": "ready", "model_id": "small@Q8", "ctx": 8192, "gpu_layers": 999, "active_streams": 0, "last_used": 0})
    assert run("code", "--dangerously-skip-permissions", "task") == 1
    assert s.loads == [] and launches == []
    assert run("code", "--dangerously-skip-permissions", "--allow-model-swap", "task") == 0
    assert [b["model_id"] for b in s.loads] == ["coder@Q4"] and launches[0]["mode"] == "bypass"
