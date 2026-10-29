"""Tests for /cabinet command: RAM/VRAM display, model loading/unloading, menu interaction, and error handling."""
import pytest
from fake_engine_server import TOKEN, FakeEngineServer
from prompt_toolkit.input import create_pipe_input
from sarness_cli.cabinet import (
    build_cabinet_menu,
    fetch_cabinet_state,
    format_mb,
    render_cabinet_overview,
    run_cabinet,
)
from sarness_cli.engine import Engine
from sarness_cli.repl import HELP, SLASH_COMMANDS, SlashCommandCompleter
from sarness_cli.runtime import ModelError, check_chat_model, prepare_model
from sarness_cli.ui import Console


class DummyDoc:
    def __init__(self, text: str):
        self.text_before_cursor = text


TEST_REGISTRY = [
    {"id": "qwen-4b@Q4", "name": "Qwen 4B", "category": "llm", "capabilities": {"reasoning": True}, "vram_mb": 4000, "loaded": True},
    {"id": "deepseek-coder@Q8", "name": "DeepSeek Coder", "category": "llm", "capabilities": {"reasoning": True}, "vram_mb": 14000, "loaded": False},
    {"id": "sdxl-turbo", "name": "SDXL Turbo", "category": "image", "capabilities": {}, "vram_mb": 7400, "loaded": True},
    {"id": "wan-video-1.3b", "name": "Wan Video 1.3B", "category": "video", "capabilities": {}, "vram_mb": 8500, "loaded": False},
]

TEST_SYSTEM_DATA = {
    "hardware": {
        "memory": {"total_mb": 65536, "used_mb": 20480, "available_mb": 45056},
        "gpus": [{
            "index": 0,
            "name": "NVIDIA GeForce RTX 4090",
            "vram_total_mb": 24576,
            "vram_used_mb": 11400,
            "vram_free_mb": 13176,
            "utilization_pct": 22,
            "temperature_c": 56,
        }],
    },
    "runtime": {
        "worker": {
            "alive": True,
            "busy": False,
            "loaded": [{"key": "image:sdxl-turbo", "kind": "image", "vram_mb": 7400}],
            "vram_allocated_mb": 7400,
            "vram_reserved_mb": 7800,
        }
    },
    "llm": {
        "state": "ready",
        "model_id": "qwen-4b@Q4",
        "model_name": "Qwen 4B",
        "ctx": 16384,
        "vram_estimate_mb": 4000,
    },
}


# -----------------------------------------------------------------------------
# 1. Formatting and State Extraction Tests
# -----------------------------------------------------------------------------

def test_format_mb():
    assert format_mb(None) == "unknown"
    assert format_mb(512) == "512 MB"
    assert format_mb(1024) == "1.0 GB"
    assert format_mb(24576) == "24.0 GB"
    assert format_mb(65536) == "64.0 GB"


def test_fetch_cabinet_state():
    server = FakeEngineServer([], llm_status=TEST_SYSTEM_DATA["llm"],
                              system_data=TEST_SYSTEM_DATA, registry=TEST_REGISTRY)
    try:
        eng = Engine(server.url, TOKEN)
        state = fetch_cabinet_state(eng)

        # RAM
        ram = state["ram"]
        assert ram["total_mb"] == 65536
        assert ram["used_mb"] == 20480
        assert ram["available_mb"] == 45056

        # GPU VRAM
        gpus = state["gpus"]
        assert len(gpus) == 1
        assert gpus[0]["name"] == "NVIDIA GeForce RTX 4090"
        assert gpus[0]["vram_total_mb"] == 24576
        assert gpus[0]["vram_used_mb"] == 11400
        assert gpus[0]["vram_free_mb"] == 13176

        # Loaded models: LLM + image worker model
        loaded = state["loaded_models"]
        loaded_ids = {m["id"] for m in loaded}
        assert "qwen-4b@Q4" in loaded_ids
        assert "sdxl-turbo" in loaded_ids

        llm_m = next(m for m in loaded if m["id"] == "qwen-4b@Q4")
        assert llm_m["category"] == "llm"
        assert llm_m["is_llm"] is True
        assert llm_m["vram_mb"] == 4000

        img_m = next(m for m in loaded if m["id"] == "sdxl-turbo")
        assert img_m["category"] == "image"
        assert img_m["is_worker"] is True
        assert img_m["vram_mb"] == 7400
    finally:
        server.stop()


def test_render_cabinet_overview():
    err_lines = []
    con = Console()
    con.err = lambda msg: err_lines.append(str(msg))

    state = {
        "ram": {"total_mb": 32768, "used_mb": 8192, "available_mb": 24576},
        "gpus": [{
            "index": 0,
            "name": "NVIDIA RTX 4070 Ti",
            "vram_total_mb": 12288,
            "vram_used_mb": 4096,
            "vram_free_mb": 8192,
            "utilization_pct": 10,
            "temperature_c": 45,
            "unified_memory": False,
        }],
        "worker": {"vram_allocated_mb": 4096, "vram_reserved_mb": 4500},
        "loaded_models": [
            {"id": "qwen", "name": "Qwen 4B", "category": "llm", "vram_mb": 4096, "state": "ready", "ctx": 8192},
            {"id": "sdxl", "name": "SDXL Turbo", "category": "image", "vram_mb": 6000},
        ],
    }

    render_cabinet_overview(con, state, "test-server")
    output = "\n".join(err_lines)
    assert "System RAM:" in output
    assert "8.0 GB used / 32.0 GB total" in output
    assert "24.0 GB available" in output
    assert "NVIDIA RTX 4070 Ti" in output
    assert "4.0 GB used / 12.0 GB total" in output
    assert "8.0 GB free" in output
    assert "Qwen 4B" in output
    assert "[LLM]" in output
    assert "SDXL Turbo" in output
    assert "[IMAGE]" in output


def test_render_cabinet_overview_no_gpu():
    err_lines = []
    con = Console()
    con.err = lambda msg: err_lines.append(str(msg))

    state = {
        "ram": {"total_mb": 16384, "used_mb": 4096, "available_mb": 12288},
        "gpus": [],
        "worker": {},
        "loaded_models": [],
    }

    render_cabinet_overview(con, state, "cpu-server")
    output = "\n".join(err_lines)
    assert "No NVIDIA GPU detected" in output
    assert "None — GPU/CPU memory is idle" in output


# -----------------------------------------------------------------------------
# 2. Menu Construction & Model Categorization Tests
# -----------------------------------------------------------------------------

def test_build_cabinet_menu():
    state = {
        "loaded_models": [
            {"id": "qwen-4b@Q4", "name": "Qwen 4B", "category": "llm", "vram_mb": 4000, "is_llm": True},
            {"id": "sdxl-turbo", "name": "SDXL Turbo", "category": "image", "vram_mb": 7400, "is_worker": True},
        ],
        "all_models": TEST_REGISTRY,
    }
    items = build_cabinet_menu(state)
    item_keys = [it.key for it in items]

    # Action items
    assert "action:refresh" in item_keys
    assert "action:unload_llm:qwen-4b@Q4" in item_keys
    assert "action:release_worker" in item_keys
    assert "action:exit" in item_keys

    # Loaded LLM is selectable
    llm_item = next(it for it in items if it.key == "item:loaded_llm:qwen-4b@Q4")
    assert llm_item.disabled is False

    # Loaded image model is selectable (to support UNLOADING)
    img_item = next(it for it in items if it.key == "item:loaded_worker:sdxl-turbo")
    assert img_item.disabled is False

    # Unloaded LLM is enabled and selectable to load
    unloaded_llm = next(it for it in items if it.key == "item:load_llm:deepseek-coder@Q8")
    assert unloaded_llm.disabled is False

    # Unloaded video model is DISABLED to prevent attempting to chat/load
    video_item = next(it for it in items if it.key == "item:unsupported:wan-video-1.3b")
    assert video_item.disabled is True
    assert "Image/video model" in video_item.disabled_reason


# -----------------------------------------------------------------------------
# 3. Chat Protection against Image/Video Models Tests
# -----------------------------------------------------------------------------

def test_prevent_chat_with_image_video_models():
    # 1. check_chat_model helper
    img_model = {"ref": "local:sdxl-turbo", "name": "SDXL Turbo", "category": "image"}
    with pytest.raises(ModelError, match="Cannot chat with SDXL Turbo: it is a image model, not an LLM"):
        check_chat_model(img_model)

    vid_model = {"ref": "local:wan2.1", "name": "Wan 2.1", "category": "video"}
    with pytest.raises(ModelError, match="Cannot chat with Wan 2.1: it is a video model, not an LLM"):
        check_chat_model(vid_model)

    llm_model = {"ref": "local:qwen", "name": "Qwen", "category": "llm"}
    check_chat_model(llm_model)  # should not raise

    # 2. prepare_model guard
    class DummyEngine:
        def llm_status(self):
            return {"state": "ready", "model_id": "dummy"}

    with pytest.raises(ModelError, match="Cannot chat with SDXL Turbo"):
        prepare_model(DummyEngine(), img_model, wanted_ctx=8192, confirm_swap=None)


# -----------------------------------------------------------------------------
# 4. Interactive Menu Execution (via create_pipe_input)
# -----------------------------------------------------------------------------

def test_cabinet_interactive_unload_llm():
    server = FakeEngineServer([], llm_status={"state": "ready", "model_id": "qwen-4b@Q4", "ctx": 8192},
                              system_data=TEST_SYSTEM_DATA, registry=TEST_REGISTRY)
    try:
        eng = Engine(server.url, TOKEN)
        con = Console()
        err_msgs = []
        con.err = lambda msg: err_msgs.append(str(msg))

        with create_pipe_input() as pipe:
            # Navigate Down to "⏏ Unload LLM: Qwen 4B" then Enter
            pipe.send_text("\x1b[B\r")
            run_cabinet(eng, con, "test-server", input_pipe=pipe)

        # Verify LLM was unloaded on server
        status = eng.llm_status()
        assert status["state"] == "stopped"
        assert status["model_id"] is None
        assert any("Unloaded" in msg for msg in err_msgs)
    finally:
        server.stop()


def test_cabinet_interactive_release_worker():
    sys_copy = dict(TEST_SYSTEM_DATA)
    sys_copy["runtime"]["worker"]["loaded"] = [{"key": "image:sdxl-turbo", "kind": "image", "vram_mb": 7400}]
    server = FakeEngineServer([], llm_status={"state": "ready", "model_id": "qwen-4b@Q4"},
                              system_data=sys_copy, registry=TEST_REGISTRY)
    try:
        eng = Engine(server.url, TOKEN)
        con = Console()
        err_msgs = []
        con.err = lambda msg: err_msgs.append(str(msg))

        with create_pipe_input() as pipe:
            # Down twice to "⏏ Release Worker Models" then Enter
            pipe.send_text("\x1b[B\x1b[B\r")
            run_cabinet(eng, con, "test-server", input_pipe=pipe)

        # Verify worker models cleared
        sys_after = eng.system()
        assert sys_after["runtime"]["worker"]["loaded"] == []
        assert any("Released" in msg for msg in err_msgs)
    finally:
        server.stop()


def test_cabinet_interactive_load_llm():
    server = FakeEngineServer([], llm_status={"state": "stopped", "model_id": None},
                              system_data=TEST_SYSTEM_DATA, registry=TEST_REGISTRY)
    try:
        eng = Engine(server.url, TOKEN)
        con = Console()
        err_msgs = []
        con.err = lambda msg: err_msgs.append(str(msg))

        with create_pipe_input() as pipe:
            # Filter for "deepseek" and press Enter to load it
            pipe.send_text("deepseek\r")
            run_cabinet(eng, con, "test-server", input_pipe=pipe)

        status = eng.llm_status()
        assert status["state"] == "ready"
        assert status["model_id"] == "deepseek-coder@Q8"
        assert any("Successfully loaded" in msg for msg in err_msgs)
    finally:
        server.stop()


def test_cabinet_interactive_refresh():
    server = FakeEngineServer([], llm_status={"state": "ready", "model_id": "qwen-4b@Q4"},
                              system_data=TEST_SYSTEM_DATA, registry=TEST_REGISTRY)
    try:
        eng = Engine(server.url, TOKEN)
        con = Console()
        err_msgs = []
        con.err = lambda msg: err_msgs.append(str(msg))

        with create_pipe_input() as pipe:
            # First item is "↻ Refresh Server State" -> press Enter
            pipe.send_text("\r")
            run_cabinet(eng, con, "test-server", input_pipe=pipe)

        assert any("Refreshed server state" in msg for msg in err_msgs)
    finally:
        server.stop()


# -----------------------------------------------------------------------------
# 5. Direct Subcommands & Error Handling Tests
# -----------------------------------------------------------------------------

def test_cabinet_direct_subcommands():
    server = FakeEngineServer([], llm_status={"state": "ready", "model_id": "qwen-4b@Q4"},
                              system_data=TEST_SYSTEM_DATA, registry=TEST_REGISTRY)
    try:
        eng = Engine(server.url, TOKEN)
        con = Console()
        err_msgs = []
        con.err = lambda msg: err_msgs.append(str(msg))

        # 1. /cabinet status (non-interactive)
        run_cabinet(eng, con, "test-server", args=["status"])
        output = "\n".join(err_msgs)
        assert "System RAM:" in output
        assert "Available Models:" in output

        # 2. /cabinet unload
        err_msgs.clear()
        run_cabinet(eng, con, "test-server", args=["unload"])
        assert any("Unloaded LLM" in msg for msg in err_msgs)
        assert eng.llm_status()["state"] == "stopped"

        # 3. /cabinet load
        err_msgs.clear()
        run_cabinet(eng, con, "test-server", args=["load", "deepseek-coder@Q8"])
        assert any("Successfully loaded" in msg for msg in err_msgs)
        assert eng.llm_status()["model_id"] == "deepseek-coder@Q8"

        # 4. /cabinet load of an image model must fail
        err_msgs.clear()
        run_cabinet(eng, con, "test-server", args=["load", "sdxl-turbo"])
        assert any("Cannot load 'sdxl-turbo': it is a image model" in msg for msg in err_msgs)

        # 5. /cabinet release
        err_msgs.clear()
        run_cabinet(eng, con, "test-server", args=["release"])
        assert any("Released worker models" in msg for msg in err_msgs)

        # 6. /cabinet refresh
        err_msgs.clear()
        run_cabinet(eng, con, "test-server", args=["refresh"])
        assert any("Refreshed server state" in msg for msg in err_msgs)
    finally:
        server.stop()


def test_cabinet_error_handling_busy():
    server = FakeEngineServer([], llm_status={"state": "ready", "model_id": "qwen-4b@Q4"},
                              system_data=TEST_SYSTEM_DATA, registry=TEST_REGISTRY)
    server.busy = True
    try:
        eng = Engine(server.url, TOKEN)
        con = Console()
        err_msgs = []
        con.err = lambda msg: err_msgs.append(str(msg))

        run_cabinet(eng, con, "test-server", args=["unload"])
        assert any("engine busy" in msg.lower() or "busy" in msg.lower() for msg in err_msgs)
    finally:
        server.stop()


# -----------------------------------------------------------------------------
# 6. Slash Command Menu Discovery & Auto-complete Tests
# -----------------------------------------------------------------------------

def test_slash_command_menu_discovery():
    assert any(cmd == "/cabinet" for cmd, _ in SLASH_COMMANDS)
    assert "/cabinet" in HELP


def test_slash_command_autocomplete():
    class DummyRepl:
        def __init__(self):
            self.models = []
            self.connected = True
            self.server = "test"

        def get_slash_commands(self):
            return SLASH_COMMANDS

    completer = SlashCommandCompleter(DummyRepl())

    # Typing "/c" should suggest "/cabinet" and "/clear" and "/compact"
    completions = list(completer.get_completions(DummyDoc("/c"), None))
    comp_texts = [c.text for c in completions]
    assert "/cabinet" in comp_texts

    # Typing "/cabinet " should complete subcommands
    sub_completions = list(completer.get_completions(DummyDoc("/cabinet "), None))
    sub_texts = [c.text for c in sub_completions]
    assert "status" in sub_texts
    assert "unload" in sub_texts
    assert "release" in sub_texts
    assert "load" in sub_texts
    assert "refresh" in sub_texts
