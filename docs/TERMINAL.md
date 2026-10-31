# The Sarness terminal

Status: the default interface from 0.4.0 (`sarness`, `sarness code`). The Python runtime remains as `sarness native`.

## What it is

A maintained source fork of [OpenCode](https://github.com/anomalyco/opencode) (MIT licence, Copyright (c) 2025
opencode), pinned to **v2.0.32** (`843a6c514e606b436a7fe713422ace0a93ad3d61`). This is the version validated with the
Sarness bridge in 0.3.x. The fork is a patch series in `sarness_cli/tui_build/patches/`, applied by
`sarness tui install` (see `sarness_cli/tui_build/README.md`). The upstream licence is installed next to the binary.

```
SARNESS TERMINAL  (sarness-tui: forked OpenCode TUI, agent runtime and tools; runs on this computer)
      │  OpenAI Chat Completions, http://127.0.0.1:<random>/v1, per-launch bearer key
      ▼
Sarness bridge    (sarness_cli/openai_bridge.py, inside the `sarness` process)
      │  /v1/agent/completions over the SSH tunnel or pinned TLS (Sarness token)
      ▼
Sarness Engine ──> llama-server ──> loaded model (e.g. Qwen3.6 35B-A3B)
```

The `sarness` Python CLI stays the entry point. It resolves settings and permissions, makes sure a model is ready
(0.3.1 startup: auto-load, memory guard, no silent swaps), starts the bridge, then runs `sarness-tui --standalone` in
the workspace. That process gets the generated config (the Sarness provider only, the permission rules, the effort
variant) and display facts (`SARNESS_SERVER`, `SARNESS_MODE`, `SARNESS_EFFORT`, `SARNESS_BIN`, `SARNESS_VERSION`).

## Fork delta (7 commits: 5 changes plus 2 upstream test adaptations, all marked `SARNESS:` in the source)

| area | change |
| --- | --- |
| storage | `util/src/global.ts`: app directory `sarness-code` (data/config/state/cache), so a separate OpenCode install is never read or written |
| identity | executable `sarness-tui`; CLI help, terminal title (`Sarness`, `Sarness \| <session>`), notifications, dialogs, crash screen, hints (`sarness code --session …`) |
| installer | `upgrade`/`uninstall` refuse (the upstream updater could act on another install); `/update` is replaced by `/upgrade`, which points to `sarness upgrade` |
| theme | new default `sarness` theme: black background, white text, grey metadata, blue accents; the violet hues are remapped to blue |
| composer | `component/prompt/index.tsx`: full-width `─` rules above and below the input, a `❯` marker, no filled panel; the existing live footer (spinner, `esc interrupt`, location, context usage, shortcuts) stays below |
| info line | `component/prompt/metadata.tsx` `SarnessInfoLine`: model · effort · server · permission mode (agent muted). It is always visible and narrows by dropping the agent, then the server, then truncating the model |
| home | SARNESS wordmark (3 rows) top-left, a muted tagline, and the composer at the bottom. The footer shows the `sarness` version. The exit screen and `run` splash use the Sarness mark |
| commands | `/cabinet` suspends the TUI and runs the real `sarness cabinet` (then Enter returns). `/connect` explains that models come only from Sarness Engine. Cloud "free models" onboarding is removed |
| effort | the launch config's variant (your `--effort`/setting) starts each new session until you pick one with `ctrl+t` |
| ctrl+c | `app.tsx`, `component/prompt/index.tsx`, `config/keybind.ts` (patch 0007): the first press cancels running work, else clears, else exits; works during startup (see below) |

Sessions from 0.3.x (stock OpenCode storage) are copied per project with `sarness tui migrate`: upstream's own
`session list|export --standalone` on the OpenCode side and `session import --standalone` on the Sarness side. The
`--standalone` matters: without it each call starts a persistent background service.

Not changed: the agent loop, tools, sessions, compaction, undo, subagents, MCP, skills, permissions engine, server,
protocol and internal package names (`@opencode/*`). Project-level `.opencode/` directories and `AGENTS.md` are still
read, as upstream does.

## Ctrl+C

One press always does one visible thing, the first that applies:

| situation | first Ctrl+C |
| --- | --- |
| a dialog, menu or permission prompt is open | closes it (rejects the permission) |
| the model is generating, or the agent is running tools | cancels the turn: the Engine stream is released, the agent's shell command is stopped, a "Cancelled" notice appears, you are at the prompt; what you were typing is kept |
| a `!` command you started is running | stops that command (its process group gets SIGTERM, then SIGKILL after 3 s) and nothing else |
| idle, text in the composer | clears the composer |
| idle, composer empty | exits cleanly |
| the app is still starting | exits |

`esc` twice remains the deliberate interrupt (it can resume queued steering). Commands the agent started in the
background (a dev server) are not touched by Ctrl+C; they end when you exit. A cancelled session is saved and resumes
with `sarness code --session …`; the interrupted tool call is recorded as interrupted and is never re-run.

Why this needed a fix: in raw mode the kernel does not turn Ctrl+C into SIGINT, so the program must read the key itself.
Until its keymap mounted the terminal program did not, and the key was dropped. The renderer's own exit-on-Ctrl+C
covers that window now, and the keymap takes over once the app is up. Regression tests drive a real pseudo-terminal
(`tests/test_ctrl_c_tui.py`).

## Honesty notes

* Effort: Engine 0.4 only distinguishes thinking on/off. The line shows the selected variant; `Medium` and `High`
  both send thinking on.
* Permission mode: the line shows the Sarness mode the generated rules were built from. If you toggle OpenCode's
  auto-accept inside the terminal, it shows `AUTO-ACCEPT`, which is what is then enforced.
* Model switching inside the terminal lists only the loaded model. Use `/cabinet` (or `sarness model`) to change
  what the engine has loaded. The bridge refuses (409) if the loaded model changes under a running session.

## Platforms

Linux x64: built, installed and tested. Linux arm64: same recipe (pinned arm64 Bun), built and tested on a DGX Spark from 0.4.1 (see the changelog). macOS and Windows:
not supported by the build recipe.

## Upstream tests

Run with `bun test --timeout 30000` in each package of the patched tree (Linux x64, Bun 1.4.2), compared with the
pristine v2.0.32 tree on the same machine:

| package | pristine v2.0.32 | Sarness fork |
| --- | --- | --- |
| `packages/tui` | 1491 pass | 1490 pass, 2 skip, 0 fail |
| `packages/cli` | 331 pass, 9 skip | 332 pass, 9 skip, 0 fail |
| `packages/util` | 95 pass | 95 pass |

Every upstream failure on the unadapted fork was a deliberate change: the `sarness-code` directories, the Sarness
strings and theme, the info line text, and the gated updater. The tests were updated to expect them (patches 0005
and 0006). The TUI count is one lower because the cloud onboarding test was removed with the onboarding. The CLI
count is one higher because of a new test that the upstream updater refuses to run without
`SARNESS_ALLOW_UPSTREAM_UPDATER`; the upstream upgrade tests still run with that variable set.
