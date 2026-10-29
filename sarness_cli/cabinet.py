"""Interactive /cabinet command: server RAM/VRAM inspection and model load/unload management."""
from __future__ import annotations

import sys
from typing import Any

from .menu import MenuItem, TerminalMenu
from .modelinfo import capabilities
from .runtime import ModelError
from .ui import Console


def format_mb(mb: float | None) -> str:
    """Format megabytes into a human-readable string (GB or MB)."""
    if mb is None:
        return "unknown"
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{int(mb)} MB"


def fetch_cabinet_state(engine: Any) -> dict[str, Any]:
    """Fetch live hardware, memory, LLM status and installed models from the engine."""
    sys_info = engine.system() if hasattr(engine, "system") else engine.get_json("/v1/system")
    llm_info = engine.llm_status() if hasattr(engine, "llm_status") else engine.get_json("/v1/llm/status")
    all_models = engine.installed_models() if hasattr(engine, "installed_models") else engine.get_json("/v1/models")

    hardware = sys_info.get("hardware") or {}
    memory = hardware.get("memory") or {}
    total_ram_mb = memory.get("total_mb")
    used_ram_mb = memory.get("used_mb")
    avail_ram_mb = memory.get("available_mb")
    if used_ram_mb is None and total_ram_mb is not None and avail_ram_mb is not None:
        used_ram_mb = total_ram_mb - avail_ram_mb

    gpus = []
    for g in hardware.get("gpus") or []:
        idx = g.get("index", 0)
        name = g.get("name") or f"GPU {idx}"
        v_total = g.get("vram_total_mb")
        v_used = g.get("vram_used_mb")
        v_free = g.get("vram_free_mb")
        if v_free is None and v_total is not None and v_used is not None:
            v_free = v_total - v_used
        gpus.append({
            "index": idx,
            "name": name,
            "vram_total_mb": v_total,
            "vram_used_mb": v_used,
            "vram_free_mb": v_free,
            "utilization_pct": g.get("utilization_pct"),
            "temperature_c": g.get("temperature_c"),
            "unified_memory": g.get("unified_memory", False),
        })

    worker = sys_info.get("runtime", {}).get("worker") or {}
    worker_loaded = worker.get("loaded") or []

    models_by_id = {m.get("id"): m for m in all_models if isinstance(m, dict) and m.get("id")}

    loaded_models: list[dict[str, Any]] = []
    loaded_ids: set[str] = set()

    # 1. LLM status
    if llm_info.get("state") in ("ready", "loading") and llm_info.get("model_id"):
        mid = llm_info["model_id"]
        found = models_by_id.get(mid, {})
        mname = llm_info.get("model_name") or found.get("name") or mid
        vram = llm_info.get("vram_estimate_mb") or found.get("vram_mb")
        loaded_models.append({
            "id": mid,
            "name": mname,
            "category": "llm",
            "vram_mb": vram,
            "state": llm_info.get("state"),
            "ctx": llm_info.get("ctx"),
            "is_llm": True,
            "is_worker": False,
            "raw": found,
        })
        loaded_ids.add(mid)

    # 2. Worker loaded models (image/video/diffusion/upscaler)
    for w in worker_loaded:
        k = w.get("key", "")
        raw_id = k.split(":", 1)[1] if ":" in k else k
        found = models_by_id.get(raw_id) or models_by_id.get(k) or {}
        kind = w.get("kind") or found.get("category") or "image"
        name = found.get("name") or raw_id
        vram = w.get("vram_mb") or found.get("vram_mb")
        loaded_models.append({
            "id": raw_id,
            "name": name,
            "category": kind,
            "vram_mb": vram,
            "is_llm": False,
            "is_worker": True,
            "raw": found,
        })
        loaded_ids.add(raw_id)
        if k:
            loaded_ids.add(k)

    # 3. Models registry reported as loaded
    for m in all_models:
        if isinstance(m, dict) and m.get("loaded") and m.get("id") not in loaded_ids:
            mid = m["id"]
            cat = m.get("category", "llm")
            loaded_models.append({
                "id": mid,
                "name": m.get("name") or mid,
                "category": cat,
                "vram_mb": m.get("vram_mb"),
                "is_llm": (cat == "llm"),
                "is_worker": (cat != "llm"),
                "raw": m,
            })
            loaded_ids.add(mid)

    return {
        "server_info": sys_info.get("engine", {}),
        "ram": {
            "total_mb": total_ram_mb,
            "used_mb": used_ram_mb,
            "available_mb": avail_ram_mb,
        },
        "gpus": gpus,
        "worker": worker,
        "llm": llm_info,
        "loaded_models": loaded_models,
        "all_models": all_models,
    }


def render_cabinet_overview(console: Console, state: dict[str, Any], server_name: str) -> None:
    """Print the server RAM, GPU VRAM and loaded models overview."""
    c = console
    st = c.st

    c.err(st.bold(f"\nServer Hardware & Memory ({server_name})"))
    c.err(st.dim("─" * 68))

    # RAM
    ram = state.get("ram") or {}
    total_r = ram.get("total_mb")
    used_r = ram.get("used_mb")
    avail_r = ram.get("available_mb")

    if total_r is not None:
        pct = (used_r / total_r * 100) if total_r and used_r is not None else 0
        ram_line = f"{format_mb(used_r)} used / {format_mb(total_r)} total ({format_mb(avail_r)} available, {pct:.1f}% used)"
    else:
        ram_line = "Memory statistics unavailable"
    c.err(f"  {st.dim('System RAM:'.ljust(16))} {ram_line}")

    # GPUs
    gpus = state.get("gpus") or []
    if not gpus:
        c.err(f"  {st.dim('GPU VRAM:'.ljust(16))} {st.yellow('No NVIDIA GPU detected (CPU inference mode)')}")
    else:
        for g in gpus:
            g_name = g.get("name")
            v_tot = g.get("vram_total_mb")
            v_used = g.get("vram_used_mb")
            v_free = g.get("vram_free_mb")
            g_pct = (v_used / v_tot * 100) if v_tot and v_used is not None else 0
            u_tag = " [unified memory]" if g.get("unified_memory") else ""
            gpu_idx = g.get("index", 0)
            gpu_label = f"GPU {gpu_idx}:"
            c.err(f"  {st.dim(gpu_label.ljust(16))} {st.bold(g_name)}{u_tag}")
            c.err(f"  {st.dim('  VRAM:'.ljust(16))} {format_mb(v_used)} used / {format_mb(v_tot)} total ({format_mb(v_free)} free, {g_pct:.1f}% used)")
            if g.get("utilization_pct") is not None or g.get("temperature_c") is not None:
                meta = []
                if g.get("utilization_pct") is not None:
                    meta.append(f"util: {g['utilization_pct']}%")
                if g.get("temperature_c") is not None:
                    meta.append(f"temp: {g['temperature_c']}°C")
                c.err(f"  {st.dim('  Telemetry:'.ljust(16))} {st.dim(' · '.join(meta))}")

    # Worker runtime info
    worker = state.get("worker") or {}
    w_alloc = worker.get("vram_allocated_mb")
    w_res = worker.get("vram_reserved_mb")
    if w_alloc or w_res:
        c.err(f"  {st.dim('Worker VRAM:'.ljust(16))} {format_mb(w_alloc)} allocated · {format_mb(w_res)} reserved by PyTorch")

    # Loaded models
    loaded = state.get("loaded_models") or []
    c.err(st.dim("─" * 68))
    c.err(st.bold(f"Loaded Models ({len(loaded)})"))
    if not loaded:
        c.err(f"  {st.dim('○ None — GPU/CPU memory is idle (no models loaded)')}")
    else:
        for m in loaded:
            cat_badge = f"[{m.get('category', 'unknown').upper()}]"
            vram_str = f"~{format_mb(m.get('vram_mb'))} VRAM" if m.get("vram_mb") else "memory usage unknown"
            extra = []
            if m.get("state"):
                extra.append(f"state: {m['state']}")
            if m.get("ctx"):
                extra.append(f"ctx: {m['ctx']:,}")
            extra_str = f"  [{' · '.join(extra)}]" if extra else ""
            c.err(f"  {st.green('●')} {st.bold(m.get('name'))} {st.cyan(cat_badge)} — {st.dim(vram_str)}{st.dim(extra_str)}")

    c.err("")


def build_cabinet_menu(state: dict[str, Any]) -> list[MenuItem]:
    """Construct selectable menu items for TerminalMenu."""
    items: list[MenuItem] = []

    # Quick action: Refresh
    items.append(MenuItem(
        key="action:refresh",
        label="↻ Refresh Server State",
        description="Update live memory statistics and model list",
        metadata="",
    ))

    loaded_models = state.get("loaded_models") or []
    loaded_ids = {m["id"] for m in loaded_models}

    # Actions for loaded models
    llm_loaded = [m for m in loaded_models if m.get("is_llm")]
    worker_loaded = [m for m in loaded_models if m.get("is_worker")]

    if llm_loaded:
        m = llm_loaded[0]
        vram_str = format_mb(m.get("vram_mb"))
        items.append(MenuItem(
            key=f"action:unload_llm:{m['id']}",
            label=f"⏏ Unload LLM: {m.get('name')}",
            description="Unload from server memory to free VRAM",
            metadata=f"frees ~{vram_str}",
        ))

    if worker_loaded:
        count = len(worker_loaded)
        names = ", ".join(m.get("name") for m in worker_loaded[:3])
        if count > 3:
            names += f" (+{count - 3} more)"
        items.append(MenuItem(
            key="action:release_worker",
            label=f"⏏ Release Worker Models ({count} loaded)",
            description=f"Drop cached diffusion/image/video models ({names})",
            metadata="release VRAM",
        ))

    # All installed models: loaded first, then available
    all_models = state.get("all_models") or []

    # Sort: loaded first, then LLMs, then others
    def sort_key(m: dict[str, Any]) -> tuple[int, int, str]:
        mid = m.get("id", "")
        is_lod = 0 if mid in loaded_ids or m.get("loaded") else 1
        is_llm = 0 if m.get("category") == "llm" else 1
        return (is_lod, is_llm, m.get("name", mid).lower())

    sorted_models = sorted(all_models, key=sort_key)

    for m in sorted_models:
        if not isinstance(m, dict):
            continue
        mid = m.get("id", "")
        name = m.get("name") or mid
        cat = m.get("category", "llm").lower()
        vram = format_mb(m.get("vram_mb"))
        is_lod = mid in loaded_ids or bool(m.get("loaded"))

        if is_lod:
            if cat == "llm":
                items.append(MenuItem(
                    key=f"item:loaded_llm:{mid}",
                    label=f"● [LOADED] {name}",
                    description="LLM · select to unload or inspect",
                    metadata=f"~{vram} VRAM",
                    data=m,
                ))
            else:
                items.append(MenuItem(
                    key=f"item:loaded_worker:{mid}",
                    label=f"● [LOADED] {name}",
                    description=f"{cat.upper()} · select to unload and free VRAM",
                    metadata=f"~{vram} VRAM",
                    data=m,
                ))
        else:
            if cat == "llm":
                items.append(MenuItem(
                    key=f"item:load_llm:{mid}",
                    label=f"○ {name}",
                    description="LLM · ready to load into server memory",
                    metadata=f"est. {vram}",
                    data=m,
                ))
            else:
                # Image and video models that are not loaded cannot be run for chat
                items.append(MenuItem(
                    key=f"item:unsupported:{mid}",
                    label=f"○ {name}",
                    description=f"{cat.upper()} model · generation only (cannot chat in CLI)",
                    metadata=f"est. {vram}",
                    disabled=True,
                    disabled_reason="Image/video model (CLI only supports LLM chat)",
                    data=m,
                ))

    # Exit action
    items.append(MenuItem(
        key="action:exit",
        label="← Back / Exit Cabinet",
        description="Return to Sarness prompt",
        metadata="",
    ))

    return items


def run_cabinet(engine: Any, console: Console, server_name: str,
                repl: Any = None, args: list[str] | None = None,
                input_pipe: Any = None) -> None:
    """Run the cabinet command, supporting non-interactive overview, direct subcommands, or interactive menu."""
    c = console
    st = c.st

    # Subcommand handling: /cabinet status, /cabinet unload, /cabinet release, /cabinet load <id>, /cabinet refresh
    subcmd = args[0].lower() if args else None

    if subcmd in ("--help", "-h", "help"):
        c.err(st.bold("Sarness Cabinet — Server Memory & Model Manager"))
        c.err("  /cabinet               Open interactive memory and model management menu")
        c.err("  /cabinet status        Display server RAM, GPU VRAM, and loaded models overview")
        c.err("  /cabinet refresh       Refresh memory statistics and available model catalog")
        c.err("  /cabinet unload        Unload currently loaded LLM from server memory")
        c.err("  /cabinet release       Release cached image/video worker models from GPU VRAM")
        c.err("  /cabinet load <model>  Load a specific installed LLM into server memory")
        return

    # Direct action: refresh
    if subcmd == "refresh":
        c.spin.start("Refreshing server models and state...")
        try:
            state = fetch_cabinet_state(engine)
            c.spin.stop()
            render_cabinet_overview(c, state, server_name)
            c.err(st.green("✓ Refreshed server state."))
        except Exception as exc:  # noqa: BLE001
            c.spin.stop()
            c.err(st.red(f"✗ Failed to refresh server: {exc}"))
        return

    # Direct action: unload LLM
    if subcmd == "unload":
        c.spin.start("Unloading LLM from server memory...")
        try:
            res = engine.llm_unload() if hasattr(engine, "llm_unload") else engine.post_json("/v1/llm/unload", {})
            c.spin.stop()
            c.err(st.green("✓ Unloaded LLM from server memory."))
            if repl and hasattr(repl, "cfg"):
                c.err(st.dim(f"  Server model state: {res.get('state', 'stopped')}"))
        except Exception as exc:  # noqa: BLE001
            c.spin.stop()
            c.err(st.red(f"✗ Failed to unload LLM: {exc}"))
        return

    # Direct action: release worker models
    if subcmd == "release":
        c.spin.start("Releasing image/video worker models...")
        try:
            if hasattr(engine, "worker_release"):
                engine.worker_release()
            else:
                engine.post_json("/v1/worker/release", {})
            c.spin.stop()
            c.err(st.green("✓ Released worker models from GPU memory."))
        except Exception as exc:  # noqa: BLE001
            c.spin.stop()
            c.err(st.red(f"✗ Failed to release worker models: {exc}"))
        return

    # Direct action: load <model_id>
    if subcmd == "load":
        if len(args or []) < 2:
            c.err(st.yellow("Usage: /cabinet load <model_id>"))
            return
        target = args[1]
        c.spin.start(f"Loading '{target}' into server memory...")
        try:
            # Check model category
            all_m = engine.installed_models() if hasattr(engine, "installed_models") else engine.get_json("/v1/models")
            found = next((m for m in all_m if m.get("id") == target or m.get("name") == target), None)
            if found and found.get("category") and found["category"] != "llm":
                c.spin.stop()
                raise ModelError(f"Cannot load '{target}': it is a {found['category']} model, not an LLM. Sarness CLI only supports LLM text/chat models.")

            res = engine.llm_load(target) if hasattr(engine, "llm_load") else engine.post_json("/v1/llm/load", {"model_id": target})
            c.spin.stop()
            c.err(st.green(f"✓ Successfully loaded '{target}' into server memory."))
            if repl and hasattr(repl, "cfg"):
                repl.cfg.model = f"local:{target}" if not target.startswith(("local:", "endpoint:")) else target
                if found:
                    repl.cfg.caps = capabilities(found, server_name)
                c.err(st.dim(f"  Active session model switched to {repl.cfg.model}"))
        except Exception as exc:  # noqa: BLE001
            c.spin.stop()
            c.err(st.red(f"✗ Failed to load '{target}': {exc}"))
        return

    # Fetch initial state
    try:
        c.spin.start("Connecting to server and querying memory...")
        state = fetch_cabinet_state(engine)
        c.spin.stop()
    except Exception as exc:  # noqa: BLE001
        c.spin.stop()
        c.err(st.red(f"✗ Cannot query server cabinet: {exc}"))
        return

    # Direct action: status / non-interactive
    is_interactive = (sys.stdin.isatty() and sys.stdout.isatty()) or input_pipe is not None
    if subcmd == "status" or not is_interactive:
        render_cabinet_overview(c, state, server_name)
        # In non-interactive mode, also list available models
        c.err(st.bold("Available Models:"))
        for m in state.get("all_models", []):
            cat = m.get("category", "unknown").upper()
            lod = " [LOADED]" if m.get("loaded") else ""
            vram = f"~{format_mb(m.get('vram_mb'))}" if m.get("vram_mb") else ""
            c.err(f"  • {m.get('name', m.get('id'))} [{cat}]{lod} {st.dim(vram)}")
        return

    # Interactive Loop
    while True:
        render_cabinet_overview(c, state, server_name)
        items = build_cabinet_menu(state)
        menu = TerminalMenu(
            title="Sarness Cabinet — Server Memory & Model Management",
            items=items,
            subtitle="Select a loaded model to unload, an LLM to load, or ↻ Refresh",
            page_size=12,
            show_filter=True,
        )

        chosen = menu.run(input_pipe=input_pipe)
        if not chosen or chosen.key == "action:exit":
            c.err(st.dim("(exited cabinet)"))
            break

        key = chosen.key

        # Action: Refresh
        if key == "action:refresh":
            c.spin.start("Refreshing server status...")
            try:
                state = fetch_cabinet_state(engine)
                c.spin.stop()
                c.err(st.green("✓ Refreshed server state."))
            except Exception as exc:  # noqa: BLE001
                c.spin.stop()
                c.err(st.red(f"✗ Refresh failed: {exc}"))
            if input_pipe is not None:
                break
            continue

        # Action: Unload LLM
        if key.startswith(("action:unload_llm:", "item:loaded_llm:")):
            mid = key.split(":", 2)[-1]
            c.spin.start(f"Unloading LLM '{mid}'...")
            try:
                if hasattr(engine, "llm_unload"):
                    engine.llm_unload()
                else:
                    engine.post_json("/v1/llm/unload", {})
                c.spin.stop()
                c.err(st.green(f"✓ Unloaded {mid} from server memory."))
                state = fetch_cabinet_state(engine)
            except Exception as exc:  # noqa: BLE001
                c.spin.stop()
                c.err(st.red(f"✗ Failed to unload LLM: {exc}"))
            if input_pipe is not None:
                break
            continue

        # Action: Release Worker Models (image / video)
        if key == "action:release_worker" or key.startswith("item:loaded_worker:"):
            target_desc = "worker model(s)"
            if key.startswith("item:loaded_worker:"):
                mid = key.split(":", 2)[-1]
                target_desc = f"worker model '{mid}'"
            c.spin.start(f"Releasing {target_desc} from GPU VRAM...")
            try:
                if hasattr(engine, "worker_release"):
                    engine.worker_release()
                else:
                    engine.post_json("/v1/worker/release", {})
                c.spin.stop()
                c.err(st.green(f"✓ Released {target_desc} from GPU memory."))
                state = fetch_cabinet_state(engine)
            except Exception as exc:  # noqa: BLE001
                c.spin.stop()
                c.err(st.red(f"✗ Failed to release worker models: {exc}"))
            if input_pipe is not None:
                break
            continue

        # Action: Load an unloaded LLM
        if key.startswith("item:load_llm:"):
            mid = key.split(":", 2)[-1]
            m_data = chosen.data or {}
            m_name = m_data.get("name") or mid
            vram_est = format_mb(m_data.get("vram_mb"))

            # Confirmation in interactive mode
            if input_pipe is None and hasattr(c, "confirm") and not c.confirm(f"Load '{m_name}' into server memory? (est. {vram_est})"):
                c.err(st.dim("(load cancelled)"))
                continue

            c.spin.start(f"Loading '{m_name}' into server memory...")
            try:
                if hasattr(engine, "llm_load"):
                    engine.llm_load(mid)
                else:
                    engine.post_json("/v1/llm/load", {"model_id": mid})
                c.spin.stop()
                c.err(st.green(f"✓ Successfully loaded '{m_name}' into server memory."))

                # Optionally offer to switch REPL active session model
                if repl and hasattr(repl, "cfg") and input_pipe is None and hasattr(c, "confirm") and c.confirm(f"Set '{m_name}' as your active chat model for this session?"):
                    repl.cfg.model = f"local:{mid}"
                    repl.cfg.caps = capabilities(m_data, server_name)
                    repl._remember({"model": repl.cfg.model})
                    c.err(st.green(f"✓ Active model switched to {m_name} ({repl.cfg.model})"))

                state = fetch_cabinet_state(engine)
            except Exception as exc:  # noqa: BLE001
                c.spin.stop()
                c.err(st.red(f"✗ Failed to load '{m_name}': {exc}"))
            if input_pipe is not None:
                break
            continue

        # Prevent attempting to chat or load unsupported image/video models
        if key.startswith("item:unsupported:"):
            mid = key.split(":", 2)[-1]
            m_data = chosen.data or {}
            cat = m_data.get("category", "image/video")
            c.err(st.yellow(f"Cannot chat with '{m_data.get('name', mid)}': it is a {cat} model. Sarness CLI only supports LLM text/chat models."))
            if input_pipe is not None:
                break
            continue
