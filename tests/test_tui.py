"""The Sarness terminal launcher: binary lookup, the environment it gets, and the packaged build recipe."""
import re
from pathlib import Path

import pytest

from sarness_cli import __version__, tui
from sarness_cli.openai_bridge import opencode_command, opencode_env, opencode_major


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho 'sarness-tui v2.0.32'\n")
    path.chmod(0o755)
    return path


def test_installed_current_build_is_found_under_xdg_data(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("SARNESS_TUI_BIN", raising=False)
    assert tui.find_binary() is None
    with pytest.raises(RuntimeError, match="sarness tui install"):
        tui.require_binary()
    build = _exe(tmp_path / "data" / "sarness-cli" / "tui" / "0.4.0-2.0.32-abc" / "sarness-tui")
    (tmp_path / "data" / "sarness-cli" / "tui" / "current").symlink_to(build.parent.name)
    assert tui.find_binary() == tmp_path / "data" / "sarness-cli" / "tui" / "current" / "sarness-tui"


def test_development_override_and_non_executables(monkeypatch, tmp_path):
    plain = tmp_path / "sarness-tui"
    plain.write_text("x")
    monkeypatch.setenv("SARNESS_TUI_BIN", str(plain))
    assert tui.find_binary() is None            # not executable: never run
    plain.chmod(0o755)
    assert tui.find_binary() == plain


def test_never_falls_back_to_a_separately_installed_opencode(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("SARNESS_TUI_BIN", raising=False)
    _exe(tmp_path / "bin" / "opencode")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("HOME", str(tmp_path))
    _exe(tmp_path / ".opencode" / "bin" / "opencode")
    assert tui.find_binary() is None


def test_env_carries_display_facts_and_keeps_the_pwd_fix(tmp_path):
    env = opencode_env({"x": 1}, tmp_path, server="spark", mode="bypass", effort="high")
    assert env["SARNESS_SERVER"] == "spark" and env["SARNESS_MODE"] == "bypass" and env["SARNESS_EFFORT"] == "high"
    assert env["SARNESS_VERSION"] == __version__ and env["SARNESS_TUI"] == "1" and env["SARNESS_BIN"]
    assert env["PWD"] == str(tmp_path) and env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"


def test_terminal_command_is_standalone_in_the_workspace(tmp_path):
    assert opencode_command("/x/sarness-tui", tmp_path, "auto", None) == ["/x/sarness-tui", "--standalone", str(tmp_path)]
    assert opencode_command("/x/sarness-tui", tmp_path, "bypass", None, ["--session", "ses_1"])[-3:] == \
        ["ses_1", "--auto", str(tmp_path)]


def test_version_gate_accepts_the_sarness_terminal():
    assert opencode_major("sarness-tui v2.0.32") == 2
    with pytest.raises(RuntimeError):
        opencode_major("sarness-tui v3.0.0")


def test_build_recipe_is_pinned_and_packaged():
    upstream = (tui.BUILD_DIR / "UPSTREAM").read_text()
    assert re.search(r"^OPENCODE_COMMIT=[0-9a-f]{40}$", upstream, re.M)
    assert re.search(r"^BUN_SHA256_linux_x64=[0-9a-f]{64}$", upstream, re.M)
    patches = sorted((tui.BUILD_DIR / "patches").glob("*.patch"))
    assert patches, "the Sarness patch series is missing"
    script = (tui.BUILD_DIR / "build.sh").read_text()
    assert "sha256sum -c" in script and "--frozen-lockfile" in script and "OPENCODE_COMMIT" in script
    assert '!= "$(basename "$dest")"' in script        # a same-build reinstall keeps the rollback target
    import tomllib
    data = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    shipped = data["tool"]["setuptools"]["package-data"]["sarness_cli"]
    assert "tui_build/build.sh" in shipped and "tui_build/patches/*.patch" in shipped


def test_migrate_copies_project_sessions_without_writing_to_opencode(tmp_path):
    log = tmp_path / "calls.log"
    upstream = tmp_path / "opencode"
    upstream.write_text(f"""#!/bin/sh
echo "opencode $* pwd=$PWD" >> {log}
case "$1 $2" in
  "session list") printf 'ses_a\\tFirst\\t10/31/2026\\nses_b\\tSecond\\t10/31/2026\\nses_c\\tBroken\\t10/31/2026\\n' ;;
  "session export") [ "$4" = ses_c ] && exit 1; echo '{{"info":{{"id":"'$4'"}}}}' ;;
  *) exit 9 ;;
esac
""")
    terminal = tmp_path / "sarness-tui"
    terminal.write_text(f"""#!/bin/sh
echo "sarness-tui $1 $2 $3 $(cat "$4")" >> {log}
grep -q ses_b "$4" && {{ echo "Session already exists"; exit 0; }}
echo "Imported session"
""")
    for exe in (upstream, terminal):
        exe.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    done = tui.migrate_sessions(project, terminal, str(upstream))
    assert done == {"imported": ["ses_a"], "existing": ["ses_b"], "failed": ["ses_c"]}
    calls = log.read_text().splitlines()
    assert all(c.split()[1] in ("session",) for c in calls)          # only list/export/import were run
    assert {c.split()[2] for c in calls if c.startswith("opencode")} == {"list", "export"}
    assert all(f"pwd={project}" in c for c in calls if c.startswith("opencode"))
    assert all(c.split()[3] == "--standalone" for c in calls)         # never leaves a background service running
