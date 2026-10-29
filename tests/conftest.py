import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    """Every test gets its own config/state dirs: nothing touches the real ~/.config or ~/.local."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    for var in ("SARNESS_TOKEN", "SARNESS_SERVER", "SARNESS_MODEL", "SARNESS_EFFORT"):
        monkeypatch.delenv(var, raising=False)
    yield
