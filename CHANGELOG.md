# Changelog

## 0.4.2 - 2026-11-01

Agent reliability. A run that was told what to do now does it: it does not stop to ask permission it already has, it
does not forget what it did when the context is compacted, it does not repeat finished work, and it does not report
progress it cannot show. Both runtimes (the Sarness terminal and `sarness native`) share one implementation; see
[docs/GUARD.md](docs/GUARD.md) for the design and the evidence behind it.

### Fixed
- **"Should I proceed with the SSH connection now?"** after an explicit instruction. Two causes. After a compaction the
  terminal's checkpoint told the model to treat the whole record as "historical context, not new instructions", so
  the model had nothing to act on and asked (once through the `question` tool). And a reply that only asks ends the
  agent loop. A turn with no tool call is now read against the user's request: asking permission the request already
  covers, announcing work that did not happen, or declaring victory with required steps unproven is continued with the
  next action. The model's words stay in the transcript, no tool call is invented, the pushes are bounded (three in a
  row, then a checkpoint and a plain statement of what is blocked), and the runtime's own approval prompts are
  untouched: outside bypass the terminal still asks, shows the command that will really run, and a rejection is final.
- **Compaction loops** (nine compactions in 31 messages in a real Trinodal session, each after one to three steps).
  The window arithmetic left a 32k model about 5k tokens of working room beside ~11.5k tokens of fixed prompt, so one
  file read overflowed it. The reserve below a 32-64k window is now the model's output limit (was a flat 16k), the part
  kept verbatim is bounded by the budget, and a catalogue of many skills (42 of them were ~6.5k tokens on every
  request) shrinks to one sentence each (patch 0009).
- **The user's request disappeared at compaction.** An agent run is one request followed by tool rounds; the compactor
  treated that as "one exchange, nothing older" and kept nothing, so the request survived only as the model's
  paraphrase. The request is now always kept word for word beside the summary and a long run keeps its newest steps.
  The tail of a long command output (the test summary, "Exited with code N") now survives too; it used to be cut,
  which made a finished test run look unfinished and sent the model round again.
- **Completed work repeated after compaction.** A structured execution ledger (`sarness.ledger/1`, kept outside the
  conversation) records what tool calls actually did: exit codes, files with hashes, tests against the current change,
  deployments and whether anything verified them, failed commands with their corrections, and learned facts
  (`systemctl --user`, "this directory has no .git: it is an rsync deployment copy"). After a compaction the model is
  given the objective, the facts, the evidence and the single next action, not asked to rediscover them.
- **Repeats and loops.** An identical read, diagnostic or test run with nothing changed since is not run; a failed
  command is not repeated unchanged; an interrupted deploy is held back once so its state is checked; repeated planning
  without execution, repeated compactions without progress and A-B-A-B cycles are caught. Legitimate repetition (tests
  after an edit, a file whose text was compacted away) is untouched. There is still no cap on the number of steps.
- `sshuser@host` (no space after `ssh`) is repaired to `ssh user@host` and the repair is recorded. Other mistakes are
  reported, not rewritten.
- **Ctrl+C could still be lost** in the instant after a `!` command or prompt was submitted, before the client had been
  told it was running (the `!` cancel test failed about one run in three). A just-submitted command or prompt now
  counts as active work (patch 0010).

### Changed
- Automatic compaction is on by default (setting `autocompact`, default `on`; `/autocompact on|off|status` per
  session as before). The unreleased `/autocompact` controls defaulted to off, which turns the first context overflow
  into an error; with the ledger in place compaction is safe. `sarness config set autocompact off` restores that.
- The fork gains patches 0008 to 0012 (0008 is the `/autocompact` controls, written separately and included here).

### Tests
- `tests/test_scenario_*.py` run one engineering task (ssh to a test server, inspect, fix, test, deploy, verify)
  with a scripted model that has the habits seen in the real sessions, real tool execution, a fake server (an `ssh`
  stub, a user-scope-only `systemctl`, an rsync deployment copy without `.git`) and three forced compactions: through
  the bridge, through the native loop, and in the real terminal (the terminal compacts by itself). They cover all four
  permission modes, a model that ignores the note, resume from the ledger, an interrupted deploy, a read-only request,
  a rejected approval, and reproduce the original failures with the guard switched off.
- Fork: `sarness-compaction-room.test.ts` (window arithmetic, request preserved, output tail, skills catalogue,
  checkpoint wording, auto-compaction default) and the Ctrl+C wiring checks. Three upstream expectations were adapted.

### Known limits
- Core tests of the patched tree that depend on the machine's own `~/.claude` and `~/.agents` fail on this desktop
  with or without these patches (home and config discovery); the rest of the suite passes.
- A model that never produces a tool call is stopped after three pushes; the guard cannot make it work.
- In the terminal, `ssh` approvals remain the terminal's own prompts; there is no per-task pre-approval.

## 0.4.1 - 2026-10-31

One Ctrl+C now has one visible, immediate effect everywhere: it cancels what is running, otherwise clears the line,
otherwise exits. No more pressing it two or three times.

### Fixed
- **Ctrl+C was dropped during startup.** The Sarness terminal switches the terminal to raw mode (so the kernel stops
  turning Ctrl+C into SIGINT) about half a second after launch, but its keymap only mounted 0.5-1 s later (longer on a
  big project), and nothing read the key in between: a press in that window vanished and had to be repeated. The
  renderer's own exit-on-Ctrl+C now stays on until the app's keymap takes over (patch 0007).
- **Ctrl+C could not stop a running turn.** In the terminal, cancelling was only Escape pressed twice; Ctrl+C either
  cleared the composer (so a press with text in it did nothing to the task) or quit the whole app. It now cancels the
  running turn (Engine stream released), the agent's shell command, or a `!` command you started, and stays at the
  prompt with a "Cancelled" notice. Text you were typing is kept. Escape keeps its deliberate two-step interrupt.
  Pressing Ctrl+C again then clears the line, and on an empty idle prompt it exits.
- **`sarness native` never exited on Ctrl+C** at an empty prompt (only a hint was printed; it needed Ctrl-D or
  `/exit`). Ctrl+C now clears typed text, and on an empty line quits.
- **Commands were SIGKILLed with no chance to clean up** when interrupted in `sarness native`. The command's own
  process group (and only that group) now gets SIGINT, then SIGTERM, then SIGKILL, each after a bounded wait
  (1.5 s / 3 s / 5 s); a second Ctrl+C skips the waiting. The terminal's shell tool already did SIGTERM then SIGKILL
  after 3 s.
- The launcher now forwards SIGTERM/SIGHUP to the terminal it started (and kills only that child after 5 s if it will
  not leave), reports a signal death as `128+n`, and resets the alternate screen, mouse, keyboard and cursor modes
  if the terminal program dies without cleaning up, so the user's shell is not left raw.
- A Ctrl+C that landed after `ssh -f` had detached the tunnel master could leave that master running; opening a
  tunnel now closes it on any interruption.
- `sarness tui install` of the build that is already current pointed `previous` at itself, losing the rollback
  target. `previous` now only moves when the build changes (PR #7).

### Tests
- Real-keyboard regression tests: a pseudo-terminal sends the actual 0x03 byte once and checks the visible effect,
  the Engine's active stream count (the fake engine now tracks it), that the owned command is gone, that unrelated
  processes survive, that no process outlives the session, and that the terminal is restored
  (`tests/test_ctrl_c_native.py`, `test_ctrl_c_launcher.py`, and `test_ctrl_c_tui.py`, which runs where a
  `sarness-tui` binary is installed). On 0.4.0 the terminal tests fail on the startup window and on cancelling.

### Known limits
- Prompt processing cannot be aborted. The Engine only sends response headers once the first token exists, so a
  cancel during a long prefill closes the connection at once but the engine's stream is released when prefill ends
  (the next write fails), not mid-prefill.
- SSH: Sarness only starts the tunnel (a port forward, no remote command). If the agent runs `ssh host cmd`, Ctrl+C
  stops that local `ssh` client and nothing else; a remote command started without a tty may keep running until it
  next writes. Sarness never kills remote processes.

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

### Tested
- Upstream test suites on the patched tree: `packages/tui` 1490 pass / 0 fail, `packages/cli` 332 pass / 0 fail,
  `packages/util` 95 pass (pristine v2.0.32 on the same machine: 1491, 331, 95; differences in docs/TERMINAL.md).
- Live on Linux x64 against Sarness Engine: a multi-step edit-and-test task in ask mode, Esc cancellation (engine
  stream released within a second), exit/resume, Ctrl+C exit (code 0), `/cabinet`, effort cycling, narrow widths,
  and resuming a session migrated from 0.3.x.

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
