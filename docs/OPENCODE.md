# OpenCode runtime: evaluation, design and live results

Status: supported runtime since 0.3.0 (startup auto-load since 0.3.1) (`sarness code`, or `runtime = opencode`). The native runtime stays the
default; see "Why native is still the default" below.

## Execution flow

```
sarness code ──> OpenCode 2.x (TUI, agent loop, tools; runs on this computer, in the workspace)
                   │  OpenAI Chat Completions, http://127.0.0.1:<random>/v1, per-launch bearer key
                   ▼
                 Sarness bridge (sarness_cli/openai_bridge.py, in the sarness process)
                   │  /v1/agent/completions over the existing SSH tunnel or pinned TLS (Sarness token)
                   ▼
                 Sarness Engine on the Spark ──> llama-server ──> loaded model
```

No other inference backend is involved. The bridge listens on 127.0.0.1 only and exits with OpenCode.

## Why OpenCode, and what was checked

Candidates: OpenCode, Aider, OpenHands CLI. OpenCode was preferred and is the one validated live:

| need | OpenCode 2.0.32 |
| --- | --- |
| structured tool calls, parallel calls | yes, via `@opencode/ai/providers/openai-compatible` |
| sessions, resume, compaction, undo | yes (SQLite store, `--continue`, `--session`, snapshots) |
| local inference | any OpenAI-compatible endpoint; config `providers` (v2 schema) |
| permissions | rule-based (`ask`/`allow`/`deny`, bash patterns), `--auto` |
| terminal UI | mature TUI; status slots exist but the 2.x TUI plugin API is unpublished |

Aider has no tool-calling agent loop of this kind (it edits via diff formats in a chat loop). OpenHands is heavier
(Docker runtime by default). Neither was needed once OpenCode passed.

Verified against the installed binary, not assumed:

* `opencode --version` = `2.0.32`. The TUI command has **no `--model` flag** (only `run` does); the model is set
  in config. `--standalone` and `--auto` exist on both the TUI and `run`.
* The v2 config schema (`https://opencode.ai/config.json`) uses `providers.<id>.{package,settings,models}` with
  `variants: [{id, body}]`; the old `provider`/`npm`/`options` keys are deprecated.
* OpenCode takes its project directory from `$PWD`, **not** the process cwd. An early test without `PWD` ran the
  agent in the parent shell's directory. The launcher now sets `PWD` and passes the directory (regression test:
  `test_env_sets_pwd_so_opencode_acts_in_the_workspace`).
* `--standalone` is required: the shared background service would not inherit the per-launch endpoint and key.

## Protocol guarantees (tests in `tests/test_openai_bridge.py`)

* Tool calls come only from the engine's final `done` event, so they are complete. A turn that hit the output
  limit (`finish_reason: length`) never carries tool calls.
* Engine 0.4 emits `done` with `finish_reason: null` when llama-server's stream breaks off. The engine client now
  raises a retryable stream error for that (both runtimes). The bridge retries while nothing has reached OpenCode;
  after that OpenCode gets an explicit error event, never a `stop`.
* Errors before the first token keep a real HTTP status (`context_length_exceeded` → 400, so OpenCode compacts;
  busy → 409; engine down → 502).
* Reasoning streams as `reasoning_content`; usage is sent as the final chunk.
* The advertised context limit is the **loaded** ctx (e.g. 131072), not the model maximum (262144).
* A client disconnect closes the upstream request, which cancels generation on the engine.
* Only the loaded local model is offered and every request pins its ctx/gpu_layers; if someone swaps the model,
  requests fail with 409 instead of reloading.
* Startup (0.3.1, `sarness_cli/startup.py`, tests in `tests/test_startup.py`) loads the configured model when
  nothing is loaded and it fits, and never unloads a different loaded model without an explicit choice
  (interactive menu or `--allow-model-swap`). The bridge starts with the post-load live status.

## Live results (2026-10-31, Engine 0.4.0 on the Spark, Qwen3.6 35B-A3B MXFP4_MOE, ctx 131072)

* Raw engine probe: one request produced 3 parallel structured calls (`read_file` ×2, `run_command`),
  `finish_reason: tool_calls`, usage reported, reasoning streamed (thinking on) or absent (thinking off).
* `opencode run` bug-fix task (failing assertion in `calc.py`): shell → read ×2 → edit → shell, verified `PASS`,
  45 s, 6 engine requests, all `finish_reason: tool_calls`/`stop`, in the correct workspace.
* Effort variants: `#low` → `thinking: false`, `#high` → `thinking: true` on the wire.
* Cancellation: a 6000-token streaming request was disconnected after 4 s; engine `active_streams` returned to 0
  within 0.6 s.

## Why native is still the default

The 2.0.32 TUI always shows OpenCode's own home screen; the slot that could carry a Sarness logo or a
model/effort line under the composer belongs to an unpublished 2.x plugin API (the published
`@opencode-ai/plugin` 1.x typings name different slots). Shipping against it would break on point releases.
The native runtime already shows SARNESS, the model/effort/server/mode line under the prompt and `/cabinet`.
Switch with `sarness config set runtime opencode`; switch back with `... runtime native`.

## Known limits

* Validated only with OpenCode 2.x (the launcher refuses other majors).
* Engine 0.4 has no reasoning-effort levels beyond thinking on/off.
* OpenCode's `question` tool does not work in non-interactive `sarness code "task"` runs.
* Theme: OpenCode's `/theme` picker applies; no Sarness theme is shipped.
