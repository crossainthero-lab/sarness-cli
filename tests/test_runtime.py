import time

import pytest

from sarness_cli.modelinfo import EFFORT, capabilities, plan_turn
from sarness_cli.runtime import ModelError, prepare_model, resolve_model
from fake_engine_server import MODELS
from helpers import make_caps


class Eng:
    def __init__(self, free_mb=None, **st):
        self.st = {"state": "stopped", "model_id": None, "ctx": 8192, "gpu_layers": 999, "active_streams": 0, "last_used": 0, **st}
        self.free_mb = free_mb

    def get_json(self, path):
        if self.free_mb is None:
            raise RuntimeError("no hardware info")
        return {"hardware": {"gpus": [{"vram_total_mb": 124608, "vram_free_mb": self.free_mb,
                                       "compute_processes": [{"pid": 4242, "used_mb": 46744}]}]}}

    def llm_status(self):
        return self.st


def test_resolve_model_variants():
    assert resolve_model(MODELS, "local:small@Q8")["name"] == "Small Q8"
    assert resolve_model(MODELS, "small@Q8")["ref"] == "local:small@Q8"
    assert resolve_model(MODELS, "Coder Q4")["ref"] == "local:coder@Q4"
    assert resolve_model(MODELS, "coder")["ref"] == "local:coder@Q4"
    with pytest.raises(ModelError, match="matches 2"):
        resolve_model(MODELS, "q")
    with pytest.raises(ModelError, match="No model matches"):
        resolve_model(MODELS, "gpt-9")


def test_same_loaded_model_is_pinned_to_its_exact_settings_so_nothing_reloads():
    p = prepare_model(Eng(state="ready", model_id="coder@Q4", ctx=32768, gpu_layers=999), MODELS[0], wanted_ctx=65536, confirm_swap=None)
    assert (p.ctx, p.gpu_layers, p.pin, p.will_load) == (32768, 999, True, False)
    assert any("ignored" in n for n in p.notes)


def test_nothing_loaded_uses_model_default_context_and_needs_no_confirmation():
    p = prepare_model(Eng(), MODELS[0], wanted_ctx=None, confirm_swap=lambda m: pytest.fail("must not ask"))
    assert p.ctx == 16384 and p.will_load
    assert prepare_model(Eng(), MODELS[0], wanted_ctx=10**9, confirm_swap=None).ctx == 65536   # capped to the model max


def test_other_model_loaded_requires_explicit_consent_and_never_while_generating():
    busy = Eng(state="ready", model_id="other", model_name="Other", active_streams=1)
    with pytest.raises(ModelError, match="generating a response for another client"):
        prepare_model(busy, MODELS[0], wanted_ctx=None, confirm_swap=lambda m: True, allow_swap=True)   # even with consent flags
    idle = Eng(state="ready", model_id="other", model_name="Other", last_used=time.time() - 120)
    with pytest.raises(ModelError, match="UNLOAD"):
        prepare_model(idle, MODELS[0], wanted_ctx=None, confirm_swap=lambda m: False)
    with pytest.raises(ModelError, match="--allow-model-swap"):
        prepare_model(idle, MODELS[0], wanted_ctx=None, confirm_swap=None)
    asked = []
    p = prepare_model(idle, MODELS[0], wanted_ctx=None, confirm_swap=lambda m: asked.append(m) or True)
    assert p.will_load and asked and "Other" in asked[0]
    assert prepare_model(idle, MODELS[0], wanted_ctx=None, confirm_swap=None, allow_swap=True).will_load


def test_remote_endpoint_model_has_no_ctx_pin():
    ep = {"ref": "endpoint:x:m", "name": "m", "local": False, "context_max": None}
    p = prepare_model(Eng(), ep, wanted_ctx=None, confirm_swap=None)
    assert not p.pin and p.assumed


@pytest.mark.parametrize("effort", list(EFFORT))
@pytest.mark.parametrize("ctx", [8192, 16384, 32768, 131072])
@pytest.mark.parametrize("thinking", ["auto", "on", "off"])
def test_output_budget_never_starves_the_prompt(effort, ctx, thinking):
    plan = plan_turn(make_caps(), effort, thinking, ctx, None)
    assert plan.max_tokens <= max(512, int(ctx * 0.45))


def test_thinking_resolution_is_honest():
    c = make_caps(reasoning=True)
    assert plan_turn(c, "low", "auto", 32768, None).thinking is False
    assert plan_turn(c, "medium", "auto", 32768, None).thinking is None           # model default: parameter not sent
    assert plan_turn(c, "max", "auto", 32768, None).thinking is True
    assert plan_turn(c, "max", "off", 32768, None).thinking is False
    non = make_caps(reasoning=False)
    assert plan_turn(non, "max", "auto", 32768, None).thinking is None            # never invented for a non-reasoning model
    assert "may have no effect" in plan_turn(non, "max", "on", 32768, None).thinking_note
    remote = make_caps(local=False)
    assert plan_turn(remote, "max", "on", 32768, None).thinking is None
    assert "unsupported" in plan_turn(remote, "max", "on", 32768, None).thinking_note
    assert plan_turn(c, "high", "auto", 32768, None, force_no_thinking=True).thinking is False


def test_user_output_limit_is_respected_and_thinking_allowance_is_added():
    c = make_caps()
    assert plan_turn(c, "low", "off", 65536, 1000).max_tokens == 1000
    assert plan_turn(c, "high", "on", 65536, 1000).max_tokens == 1000 + EFFORT["high"]["think_extra"]


def test_capability_wording_never_claims_what_it_does_not_know():
    caps = capabilities({**MODELS[0], "reasoning": None}, "s")
    assert caps.tool_calling.startswith("unverified") and caps.thinking_toggle.startswith("unknown")


def test_load_that_would_not_fit_next_to_other_workloads_needs_consent():
    big = {**MODELS[0], "vram_mb": 56000}                      # needs 56 GB + 14.9 GB headroom
    with pytest.raises(ModelError, match="only 53 GB of 121 GB is free.*pid 4242"):
        prepare_model(Eng(free_mb=55000), big, wanted_ctx=None, confirm_swap=lambda m: False)
    with pytest.raises(ModelError, match="--allow-low-memory"):
        prepare_model(Eng(free_mb=55000), big, wanted_ctx=None, confirm_swap=None)
    assert prepare_model(Eng(free_mb=55000), big, wanted_ctx=None, confirm_swap=None, allow_low_memory=True).will_load
    assert prepare_model(Eng(free_mb=55000), big, wanted_ctx=None, confirm_swap=lambda m: True).will_load
    assert prepare_model(Eng(free_mb=90000), big, wanted_ctx=None, confirm_swap=None).will_load    # plenty of room: no prompt
    assert prepare_model(Eng(), big, wanted_ctx=None, confirm_swap=None).will_load                 # no hardware info: never blocks


def test_memory_freed_by_a_consented_swap_is_counted_and_a_loaded_model_needs_no_check():
    big = {**MODELS[0], "vram_mb": 56000}
    st = dict(state="ready", model_id="other", model_name="Other", last_used=time.time() - 600, vram_estimate_mb=30000)
    assert prepare_model(Eng(free_mb=45000, **st), big, wanted_ctx=None, confirm_swap=lambda m: True).will_load   # 45 + 30 freed >= 71
    loaded = Eng(free_mb=1000, state="ready", model_id="coder@Q4", ctx=16384)
    assert not prepare_model(loaded, big, wanted_ctx=None, confirm_swap=None).will_load                           # already in memory
