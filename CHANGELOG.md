# Changelog

## 0.4.0 - 2026-10-31

### Added
- **The Sarness terminal.** `sarness` and `sarness code` now open a Sarness-branded fork of OpenCode 2.0.32 (MIT),
  built from a pinned upstream commit plus a patch series (`sarness_cli/tui_build/`, design in docs/TERMINAL.md). It
  keeps OpenCode's agent runtime, tools, sessions, compaction, undo, subagents, MCP, skills, `@` mentions, command
  palette and diffs. Inference still goes only to Sarness Engine through the authenticated loopback bridge.
  - Sarness identity throughout: SARNESS wordmark, terminal title, help, dialogs, notifications, crash screen, and the
    exit screen with a `sarness code --session …` resume hint. Upstream attribution is kept (LICENSE-OpenCode is
    installed with the binary).
  - Bordered composer (full-width rules, `❯`) with a persistent line underneath: model · effort · server ·
    permission mode. It stays visible while idle, thinking and running tools; live status and context usage stay in
    the row below.
  - Default `sarness` theme: black, white, grey metadata, blue accents.
  - `/cabinet` runs the real model cabinet. `/upgrade` points to `sarness upgrade`. `/connect` explains that models
    come only from Sarness Engine. The cloud "free models" onboarding is removed, and the upstream self-updater and
    uninstaller refuse to run.
  - Separate storage (`~/.local/share/sarness-code`, `~/.config/sarness-code`): a normal `opencode` install is never
    run, read or modified.
- `sarness tui install|status|path`: builds with a pinned, checksum-verified Bun and upstream's frozen lockfile, then
  installs side by side and keeps the previous build (`previous` symlink) for rollback.
- `sarness tui migrate`: copies the current project's OpenCode sessions (e.g. from 0.3.x, which ran stock OpenCode)
  into the terminal's own storage. OpenCode is only asked to list and export (`--standalone`, so no background service
  is left running); re-running skips sessions already present.
- `sarness native [task]`: the Python runtime (the pre-0.4 interface) stays available. Plain `sarness` falls back to
  it, with a notice, when the terminal is not installed; `sarness config set runtime native` makes it the default.

### Fixed
- Ctrl+C in `sarness code` killed the terminal: the Python launcher died on SIGINT and `subprocess.call` killed the
  child (exit 130). The launcher now leaves Ctrl+C to the terminal, without making the terminal or the commands it
  runs ignore SIGINT.
- The configured effort (`--effort`, the `effort` setting) now selects the terminal's starting variant. In 0.3.x it
  only set the bridge's fallback, while OpenCode remembered its own variant.

### Unchanged
- 0.3.1 startup (auto-load, readiness wait, memory guard, no silent swaps), the bridge protocol guarantees (cut
  streams, incomplete tool calls, context pinning, cancellation), permission modes, and native sessions.

Rollback: `pipx install --force 'git+https://github.com/crossainthero-lab/sarness-cli@v0.3.1'` (the terminal build
can stay installed; 0.3.1 does not use it).

## 0.3.1 - 2026-10-31

### Fixed
- `sarness code` (and `sarness code "task"`, and plain `sarness` with `runtime = opencode`) refused to start when
  the configured model was not already loaded. Startup now gets a model ready without disturbing other users
  (`sarness_cli/startup.py`):
  - configured model loaded: starts immediately;
  - nothing loaded: loads the configured model if it is installed, supported and fits in the engine host's free
    memory with headroom, waits until the engine reports it ready (the engine's load call outlives the 30 s
    client timeout, so readiness is read from `/v1/llm/status`), then starts;
  - a different model loaded: never unloaded silently. Interactive: choose *use the loaded model* (default),
    *switch* (re-checked for a request that started meanwhile; not offered while it is generating) or cancel.
    Scripted: fails with the exact `--model` / `--allow-model-swap` to use. `--dangerously-skip-permissions`
    does not imply a swap;
  - no default model: the model selector (loaded model preselected), and the choice is saved for the server;
    scripted runs use the loaded model, or the only installed one;
  - a default given as a name, id or fragment resolves to the engine ref; a default that is no longer installed
    falls back to the loaded model (scripted) or the selector (interactive);
  - not enough memory: refuses with the figures; interactive offers another model or cancel, never "load anyway"
    (`--allow-low-memory` remains the explicit override);
  - waits for a load already in progress instead of starting a second one; reports load errors, engine `error`
    state, cancelled loads, a model swapped by another client mid-load, and a timeout; Ctrl+C stops waiting and
    unloads nothing.
  The bridge is then started with the engine's live status, so it advertises and pins the real ctx/gpu_layers.

Rollback: `pipx install --force 'git+https://github.com/crossainthero-lab/sarness-cli@v0.3.0'`.

## 0.3.0 - 2026-10-31

Integrates PR #1 (stream recovery, 0.2.4), PR #2 (unlimited turns, large files) and PR #3 (OpenCode bridge),
with the defects found in review and live testing fixed.

### Added
- `sarness code [task]` (alias `sarness opencode`): OpenCode 2.x as agent runtime with inference on Sarness Engine,
  through a loopback, per-launch-authenticated bridge. `runtime` setting (`native` | `opencode`). See
  docs/OPENCODE.md.
- Bridge: streamed reasoning, usage, real finish reasons, HTTP status for pre-stream errors, retries before the
  first byte, cancellation on client disconnect, loaded-ctx context limits, effort variants.

### Changed
- `max_steps` defaults to 0 (unlimited); loop/repetition guards and checkpoints still apply. `--max-steps N` and
  `sarness config set max_steps N` restore a ceiling.
- `read_file`/`search` stream files of any size; `read_file` tolerates invalid UTF-8 and clips giant lines;
  `search` skips binary files and says how many.
- The unfulfilled-action nudge only looks at the closing sentences and ignores offers.

### Fixed
- `sarness resume ID --mode auto "text"` failed on Python <= 3.12 (argparse); the prompt is now collected on every
  Python version.
- The run summary marks files whose net diff against git is empty (e.g. a reverted temporary edit) as
  `net unchanged vs git` instead of listing them as changed.
- A `done` event without `finish_reason` (Engine 0.4 sends this when llama-server's stream breaks off) was accepted
  as a finished turn; it is now a retryable stream error and its tool calls are never run.
- PR #3 launcher: passed `--model` to the OpenCode TUI (unsupported), guessed the v2 variant schema, advertised the
  model's maximum context instead of the loaded one, and did not set `$PWD` (OpenCode then acted in the parent
  shell's directory).

### Rollback
`pipx install --force 'git+https://github.com/crossainthero-lab/sarness-cli@03d2d25'` (the 0.2.4 release commit; or install the 0.2.4
wheel). Config and native sessions are unchanged by 0.3.0; the only new setting (`runtime`) is ignored by 0.2.x.
