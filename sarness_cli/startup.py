"""Get a usable model loaded before `sarness code` starts OpenCode, without ever disturbing other engine users.

Rules: a loaded model that fits is used as is; nothing loaded -> load the chosen model if it is installed,
supported and fits next to the host's other workloads; a *different* model loaded -> never unloaded silently
(interactive: ask; scripted: only with --allow-model-swap). Non-interactive runs never wait on a prompt."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .runtime import ModelError, Prepared, check_chat_model, memory_warning, prepare_model, resolve_model

LOAD_TIMEOUT_S = 900       # a cold 35B load from NVMe takes ~1-2 min; leave room for a slow disk
SETTLE_TIMEOUT_S = 900     # how long to wait for somebody else's load to finish before deciding


class StartupCancelled(ModelError):
    pass


@dataclass
class StartupUI:
    """I/O seam (tests replace these). choose() returns an option key or None (= cancel)."""
    interactive: bool
    info: Callable[[str], None]
    choose: Callable[[str, str, list[tuple[str, str, str]]], str | None]
    pick_model: Callable[[list[dict[str, Any]], str | None], dict[str, Any] | None]


@dataclass
class Ready:
    model: dict[str, Any]
    status: dict[str, Any]
    loaded_now: bool = False
    save_default: bool = False   # the user picked this model in the selector because none was configured


def _ref_id(model: dict[str, Any]) -> str:
    return model["ref"].split(":", 1)[1]


def _name(model: dict[str, Any] | None, fallback: str = "?") -> str:
    return (model or {}).get("name") or (model or {}).get("ref") or fallback


def _usable(model: dict[str, Any]) -> str | None:
    """Why this model cannot be used by the OpenCode runtime, or None."""
    try:
        check_chat_model(model)
    except ModelError as exc:
        return str(exc)
    if model.get("supported") is False:
        return f"{_name(model)} is not supported by this engine: {model.get('unsupported_reason') or 'unsupported'}"
    if not model.get("local"):
        return (f"{_name(model)} is a remote endpoint model; the Sarness terminal only drives a model loaded in "
                "Sarness Engine itself")
    return None


def wait_settled(engine: Any, ui: StartupUI, *, timeout: float = SETTLE_TIMEOUT_S, poll: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """If the engine is in the middle of a load (anyone's), wait for it instead of firing a second one."""
    st = engine.llm_status()
    if st.get("state") != "loading":
        return st
    start = clock()
    ui.info(f"The engine is already loading {st.get('model_name') or st.get('model_id') or 'a model'}; waiting for it...")
    while st.get("state") == "loading":
        if clock() - start > timeout:
            raise ModelError(f"The engine has been loading {st.get('model_id') or 'a model'} for over {int(timeout)} s. "
                             "Check it with 'sarness cabinet' and try again.")
        sleep(poll)
        st = engine.llm_status()
    return st


def load_and_wait(engine: Any, model: dict[str, Any], prepared: Prepared, ui: StartupUI, *,
                  timeout: float = LOAD_TIMEOUT_S, poll: float = 1.0, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """POST /v1/llm/load and wait until /v1/llm/status reports this exact model ready.

    The engine's load call blocks for the whole load, longer than the client's 30 s request timeout, so it runs
    in a thread and readiness is judged from llm_status (a client-side timeout does not stop the engine)."""
    want = _ref_id(model)
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["status"] = engine.llm_load(want, prepared.ctx, prepared.gpu_layers)
        except Exception as exc:  # noqa: BLE001 - reported below
            box["error"] = exc

    t = threading.Thread(target=run, name="sarness-llm-load", daemon=True)
    start = clock()
    t.start()
    ui.info(f"Loading {_name(model)} into Sarness Engine (ctx {prepared.ctx:,}). This can take a minute or two...")
    shown = start
    try:
        while True:
            t.join(poll)
            err = box.get("error")
            if err is not None and getattr(err, "kind", "") not in ("timeout", "network"):
                raise ModelError(f"Sarness Engine could not load {_name(model)}: {err}")
            try:
                st = engine.llm_status()
            except Exception as exc:  # noqa: BLE001
                if t.is_alive() and clock() - start < timeout:
                    continue
                raise ModelError(f"Lost contact with Sarness Engine while loading {_name(model)}: {exc}") from exc
            state, mid = st.get("state"), st.get("model_id")
            if state == "ready" and mid == want:
                return st
            if state == "error" and (mid in (want, None) or not t.is_alive()):
                raise ModelError(f"Sarness Engine failed to load {_name(model)}: {st.get('error') or 'unknown error'}")
            if state == "ready" and mid and mid != want and not t.is_alive():
                raise ModelError(f"While loading {_name(model)}, the engine ended up with {st.get('model_name') or mid} "
                                 "loaded instead (another client changed it). Nothing else was unloaded by Sarness.")
            if state == "stopped" and not t.is_alive() and err is None:
                raise ModelError(f"The load of {_name(model)} finished but the engine reports no model loaded "
                                 "(the load was cancelled or the engine restarted). Try again.")
            if clock() - start > timeout:
                raise ModelError(f"{_name(model)} was still not ready after {int(timeout)} s (engine state: {state}). "
                                 "The engine may still be loading it; check 'sarness cabinet'.")
            if clock() - shown >= 15:
                shown = clock()
                ui.info(f"  ... still loading ({int(clock() - start)} s, engine state: {state})")
    except KeyboardInterrupt:
        raise StartupCancelled(f"Stopped waiting. The engine may still finish loading {_name(model)} on its own; "
                               "Sarness did not unload anything.") from None


def _resolve(models: list[dict[str, Any]], selector: str, loaded_ref: str | None) -> dict[str, Any]:
    """resolve_model, except an ambiguous fragment that covers the loaded model means the loaded model."""
    try:
        return resolve_model(models, selector)
    except ModelError:
        if loaded_ref:
            sel = selector.strip().lower()
            hit = next((m for m in models if m.get("ref") == loaded_ref), None)
            if hit and (sel in hit["ref"].lower() or sel in str(hit.get("name", "")).lower()):
                return hit
        raise


def ensure_ready(engine: Any, models: list[dict[str, Any]], configured: str | None, ui: StartupUI, *,
                 allow_swap: bool = False, allow_low_memory: bool = False, wanted_ctx: int | None = None,
                 load_timeout: float = LOAD_TIMEOUT_S, settle_timeout: float = SETTLE_TIMEOUT_S) -> Ready:
    status = wait_settled(engine, ui, timeout=settle_timeout)
    loaded_id = status.get("model_id") if status.get("state") == "ready" else None
    loaded_ref = f"local:{loaded_id}" if loaded_id else None
    loaded = next((m for m in models if m.get("ref") == loaded_ref), None) if loaded_ref else None
    if loaded_ref and loaded is None:   # loaded but not listed (e.g. not in /v1/agent/models): still usable as is
        loaded = {"ref": loaded_ref, "name": status.get("model_name") or loaded_id, "local": True}
    save_default = False

    target: dict[str, Any] | None = None
    if configured:
        try:
            target = _resolve(models, configured, loaded_ref)
        except ModelError as exc:
            if ui.interactive:
                ui.info(f"Your default model '{configured}' is not available on this engine ({exc}).")
                target = ui.pick_model(models, loaded_ref)
                if target is None:
                    raise StartupCancelled("No model selected.") from None
                save_default = True
            elif loaded is not None and not _usable(loaded):
                ui.info(f"Default model '{configured}' is not installed on this engine; using the loaded "
                        f"{_name(loaded)} instead.")
                target = loaded
            else:
                raise ModelError(f"Default model '{configured}' is not available on this engine: {exc}") from exc
    elif loaded is not None and not _usable(loaded) and not ui.interactive:
        target = loaded
    elif ui.interactive:
        ui.info("No default model is set for this server. Pick one (the loaded model is preselected):")
        target = ui.pick_model(models, loaded_ref)
        if target is None:
            raise StartupCancelled("No model selected.")
        save_default = True
    else:
        usable = [m for m in models if not _usable(m)]
        if len(usable) == 1:
            target = usable[0]
        else:
            raise ModelError("No model is loaded and no default model is set, and there is more than one to choose "
                             "from. Run 'sarness model <id>' once (or pass --model).")

    why = _usable(target)
    if why:
        raise ModelError(why)

    # 1. already loaded -> use it as is (its ctx/gpu_layers get pinned by the bridge)
    if loaded_ref and target["ref"] == loaded_ref:
        return Ready(target, status, save_default=save_default)

    # 2. a different model is loaded -> never unload it silently
    if loaded is not None:
        target = _decide_over_loaded(engine, target, loaded, status, ui, allow_swap=allow_swap)
        if target["ref"] == loaded_ref:
            return Ready(target, status, save_default=save_default)

    # 3. load it (nothing loaded, or a swap was explicitly approved): memory guard first
    while True:
        try:
            prepared = prepare_model(engine, target, wanted_ctx=wanted_ctx, confirm_swap=None,
                                     allow_swap=True, allow_low_memory=allow_low_memory)
            break
        except ModelError as exc:
            if "generating a response" in str(exc) or not ui.interactive:
                raise
            choice = ui.choose(f"Cannot load {_name(target)} safely", str(exc), [
                ("other", "Choose another model", "a smaller model may fit next to the other workloads"),
                ("cancel", "Cancel", "load nothing; free memory on the engine host and try again")])
            if choice != "other":
                raise StartupCancelled(str(exc)) from None
            picked = ui.pick_model(models, None)
            if picked is None:
                raise StartupCancelled("No model selected.") from None
            if _usable(picked):
                raise ModelError(_usable(picked)) from None
            target, save_default = picked, save_default or not configured
            if loaded_ref and target["ref"] == loaded_ref:
                return Ready(target, engine.llm_status(), save_default=save_default)
    st = load_and_wait(engine, target, prepared, ui, timeout=load_timeout)
    return Ready(target, st, loaded_now=True, save_default=save_default)


def _decide_over_loaded(engine: Any, target: dict[str, Any], loaded: dict[str, Any], status: dict[str, Any],
                        ui: StartupUI, *, allow_swap: bool) -> dict[str, Any]:
    busy = int(status.get("active_streams") or 0) > 0
    lname, tname = _name(loaded), _name(target)
    if allow_swap and not busy:
        _check_swap_memory(engine, target, status, lname)
        ui.info(f"--allow-model-swap: unloading {lname} to load {tname}.")
        return target
    if not ui.interactive:
        raise ModelError(
            f"{tname} is your default model, but the engine has {lname} loaded" +
            (" and it is generating a response for another client" if busy else "") +
            f". Sarness never unloads a model without asking. Use the loaded one with "
            f"'--model {loaded['ref']}', or approve the switch with '--allow-model-swap'"
            + (" once it is idle." if busy else "."))
    idle = int((time.time() - float(status.get("last_used") or 0)) // 60) if status.get("last_used") else None
    options = [("use", f"Use the loaded {lname}", "starts immediately; nothing on the engine changes (recommended)")]
    if busy:
        sub = f"{lname} is loaded and generating a response for another client, so switching is not possible now."
    else:
        sub = (f"{lname} is loaded" + (f" (last used {idle} min ago)" if idle is not None else "") +
               f". Your default is {tname}.")
        options.append(("switch", f"Switch to {tname}", f"UNLOADS {lname} for every client of this engine"))
    options.append(("cancel", "Cancel", "start nothing"))
    choice = ui.choose("A different model is loaded", sub, options)
    if choice == "use":
        return loaded
    if choice == "switch":
        fresh = engine.llm_status()   # re-check: someone may have started a request while the menu was open
        if int(fresh.get("active_streams") or 0) > 0 or fresh.get("state") == "loading":
            raise ModelError(f"{lname} became busy while you were choosing; not switching. Try again shortly.")
        _check_swap_memory(engine, target, fresh, lname)
        return target
    raise StartupCancelled("Cancelled; the engine was left as it was.")


def _check_swap_memory(engine: Any, target: dict[str, Any], status: dict[str, Any], lname: str) -> None:
    warn = memory_warning(engine, target, int(status.get("vram_estimate_mb") or 0))
    if warn:
        raise ModelError(warn + f" Not switching; {lname} stays loaded.")
