import json
import os
import stat

import pytest

from sarness_cli import config as C


def test_defaults_and_layers(tmp_path):
    ws = tmp_path / "w"
    ws.mkdir()
    s, o = C.effective_settings(ws)
    assert s["effort"] == "medium" and o["effort"] == "default" and s["permissions"] == "auto"
    C.update_settings({"effort": "high"})
    s, o = C.effective_settings(ws)
    assert (s["effort"], o["effort"]) == ("high", "global")
    cfg = C.load_config()
    cfg["profiles"]["a"] = {"transport": "direct", "url": "http://127.0.0.1:1", "model": "local:a"}
    cfg["profiles"]["b"] = {"transport": "direct", "url": "http://127.0.0.1:1", "model": "local:b"}
    cfg["settings"]["server"] = "a"
    C.save_config(cfg)
    assert C.effective_settings(ws)[0]["model"] == "local:a" and C.effective_settings(ws, {"server": "b"})[0]["model"] == "local:b"   # per-server model
    assert C.effective_settings(ws)[1]["model"] == "server 'a'"
    C.update_settings({"model": "local:proj"}, workspace=ws, scope="project")
    s, o = C.effective_settings(ws)
    assert s["model"] == "local:proj" and o["model"] == "project (private)"
    s, o = C.effective_settings(ws, {"model": "local:flag", "effort": None})
    assert s["model"] == "local:flag" and o["model"] == "flag" and s["effort"] == "high"
    other = tmp_path / "other"
    other.mkdir()
    assert C.effective_settings(other)[0]["model"] == "local:a"      # a project override does not leak into other projects


def test_environment_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("SARNESS_EFFORT", "max")
    assert C.effective_settings(tmp_path)[0]["effort"] == "max"


def test_bypass_can_never_be_saved_or_configured():
    for fn in (lambda: C.update_settings({"permissions": "bypass"}), lambda: C.coerce_setting("permissions", "bypass"), lambda: C.validate_setting("permissions", "bypass")):
        with pytest.raises(C.ConfigError, match="never be saved"):
            fn()
    assert "bypass" not in json.dumps(C.load_config())


def test_repository_settings_cannot_escalate_or_smuggle(tmp_path):
    ws = tmp_path / "repo"
    (ws / ".git").mkdir(parents=True)
    (ws / ".sarness").mkdir()
    (ws / ".sarness" / "settings.json").write_text(json.dumps(
        {"permissions": "full-access", "server": "evil", "model": "local:x", "effort": "high", "token": "x", "command_timeout_s": 5}))
    settings, f, notes = C.load_project_settings(ws)
    assert settings == {"model": "local:x", "effort": "high", "command_timeout_s": 5}
    assert any("full-access" in n for n in notes) and any("'server'" in n for n in notes) and any("'token'" in n for n in notes)
    (ws / ".sarness" / "settings.json").write_text(json.dumps({"permissions": "ask"}))
    assert C.effective_settings(ws)[0]["permissions"] == "ask"        # tightening is fine


def test_secrets_are_private_and_unsafe_modes_refused():
    C.set_secret("spark", "tok-123")
    assert stat.S_IMODE(C.secrets_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(C.config_dir().stat().st_mode) == 0o700
    assert C.get_secret("spark") == "tok-123"
    os.chmod(C.secrets_path(), 0o644)
    with pytest.raises(C.ConfigError, match="unsafe permissions"):
        C.get_secret("spark")
    os.chmod(C.secrets_path(), 0o600)
    C.delete_secret("spark")
    assert C.get_secret("spark") is None


def test_config_file_never_contains_tokens():
    cfg = C.load_config()
    cfg["profiles"]["x"] = {"transport": "direct", "url": "http://127.0.0.1:1", "legacy_token": "LEAK"}
    C.save_config(cfg)
    assert "LEAK" not in C.config_path().read_text()


def test_legacy_010_config_is_migrated_in_memory():
    C.atomic_json(C.config_path(), {"url": "http://127.0.0.1:8742", "token": "old-token"})
    cfg = C.load_config()
    assert cfg["profiles"]["default"]["url"] == "http://127.0.0.1:8742" and cfg["settings"]["server"] == "default"


def test_setting_validation():
    assert C.coerce_setting("max_steps", "30") == 30 and C.coerce_setting("show_reasoning", "on") is True
    assert C.coerce_setting("context_tokens", "none") is None
    for k, v in [("effort", "extreme"), ("max_steps", "0"), ("max_steps", "x"), ("nonsense", "1"), ("retries", "99")]:
        with pytest.raises(C.ConfigError):
            C.coerce_setting(k, v)
