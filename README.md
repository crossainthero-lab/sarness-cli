# Sarness CLI

A terminal coding agent for **Sarness Engine**. Inference runs on your engine (for example a DGX Spark);
**file editing, searching, Git and every command run on the computer where you start `sarness`**, inside the project
you launch it from. The CLI talks to the engine's stateless `POST /v1/agent/completions` over authenticated HTTP/SSE.

```
 your Ubuntu terminal                                   Sarness Engine (GPU server)
 ┌───────────────────────────────┐   SSH tunnel / TLS   ┌───────────────────────────┐
 │ sarness: agent loop + tools   │ ── messages+tools ──▶│ /v1/agent/completions     │
 │ files · commands · git (HERE) │ ◀── text/tool_calls ─│ model inference only      │
 └───────────────────────────────┘                      └───────────────────────────┘
```

## Install (Ubuntu, no sudo)

```bash
git clone https://github.com/crossainthero-lab/sarness-cli.git
cd sarness-cli
./install.sh                    # isolated pipx environment, `sarness` lands in ~/.local/bin
sarness setup                   # connect to your own Sarness Engine (guided)
sarness tui install             # build + install the Sarness terminal (pinned source, ~5 min, ~3.5 GB temporary)
cd ~/my-project && sarness      # start coding
```

* `./install.sh --check` shows what it would do. It is idempotent and never needs root.
* **Upgrade / reinstall:** `sarness upgrade` (re-runs `install.sh` from the recorded source folder; `--pull` runs `git pull --ff-only` first),
  or just `./install.sh` again.
* If another executable called `sarness` already sits in `~/.local/bin`, the installer inspects it. The Sarness *desktop* launcher is
  kept as `~/.local/bin/sarness-desktop` (a backup is stored in `~/.local/share/sarness-cli/backups/`) and the user-level
  `Sarness.desktop` entry is repointed, so the desktop icon keeps working. Anything unrecognised is left alone and the install stops.
* Requires Python ≥ 3.11 and `pipx`. Uses `prompt_toolkit` for line editing; `secretstorage` (optional extra) imports tokens from
  Sarness Desktop's keyring.

You can also download this repository using GitHub's **Code → Download ZIP**, extract it, and run `./install.sh` inside it.
The engine is a separate service: see [Sarness](https://github.com/crossainthero-lab/Sarness). An authenticated Engine API 1.x
with `/v1/agent/completions` and a model capable of structured tool calls is required. The CLI does not install a GPU server.
When working in the parent Sarness source tree, the installer is `cli/install.sh`.

## Connect: `sarness setup`

```bash
sarness setup                                        # guided; enter your engine address and API token
sarness setup --name my-engine --ssh user@engine.example # SSH keys/agent + engine token; no desktop app required
sarness setup --yes --name spark --from-desktop "My Spark" \
              --ssh user@spark.lan --token-from-desktop --model qwen3-coder-next
```

Transports (a token is **never** sent in clear text over the network):

| Transport | How | Notes |
|---|---|---|
| **SSH tunnel** (recommended) | `ssh -f -N -L 127.0.0.1:<port>:<engine-host>:8742` | Uses your SSH keys/agent; host keys are always verified by OpenSSH; the tunnel is opened on demand, re-opened after a drop, and closed on exit. It detaches from the terminal, so Ctrl-C to cancel a model turn cannot kill it. |
| **HTTPS** | `--url https://…` | Certificates are verified; or **pinned** by SHA-256 fingerprint (`--tls-pin`, shown and confirmed during setup). A pin mismatch refuses the connection. |
| Plain HTTP | only with an explicit `--allow-insecure-http` / typed "I understand" | Remote plain HTTP is refused otherwise. Loopback (the tunnel's own end) is allowed. |

The token is read from the Sarness Desktop keyring (`--token-from-desktop`), a `--token-file` (mode 0600), `$SARNESS_TOKEN`
(`--token-env`) or a hidden prompt. It is stored in `~/.config/sarness-cli/secrets.json` (0600, directory 0700), never in the config
file, shell history, logs, or Git. Several servers can coexist (`sarness server list|use|remove`, `--server NAME`).

## Everyday commands

```bash
sarness                     # the Sarness terminal in the current directory (same as `sarness code`)
sarness code "fix the failing test"    # one task through the Sarness terminal, non-interactive
sarness code --session <id> # resume a Sarness terminal session (--continue: the latest)
sarness --dangerously-skip-permissions # bypass mode (unrestricted execution; OS permissions still apply)
sarness native              # the Python runtime (pre-0.4 interface): REPL, `sarness native "task"`
sarness --resume            # browse and resume previous Python-runtime sessions (also -c, -p "task", run "…")
sarness tui status|install  # the Sarness terminal build
sarness tui migrate         # copy this project's 0.3.x (OpenCode) sessions into the Sarness terminal
sarness models              # installed models: ids, context limits, tool/reasoning support (--json, --probe)
sarness model [id]          # show / set the default model for the current server (--project: this folder only)
sarness config              # effective settings and where each value comes from; `config set KEY VALUE [--project]`
sarness doctor              # checks PATH, files, SSH/tunnel, engine, token, models
sarness cabinet             # inspect server memory (RAM/VRAM) and load/unload models
sarness sessions | server | upgrade | --version
```

The Python runtime's slash commands: `/help /cabinet /model /effort /thinking /permissions /status /plan /diff /compact /clear /resume /rename /exit`.
**Enter** sends, **Alt+Enter** (or Esc,Enter, or a trailing ``) inserts a newline, **Ctrl-C** cancels the running task (state is saved),
**Ctrl-D** quits. Large pasted prompts are automatically collapsed into compact `▸ Pasted text` indicators (expand/edit with **Ctrl-E**).
The live bottom status bar shows: `Model · Effort · Server ● · RAM Used/Total · Permission Mode`.

## Models

`sarness models` lists what the engine really has. Nothing is invented: the engine's agent route reports `tools: null` for local
models, shown as **`unverified`**; "reasoning" comes from the engine's model registry. `sarness models --probe` sends one tiny
structured tool-call request, **only to the model that is already loaded** (it never loads a model to test it) and caches the result.

Switching: `--model <ref|name|unique fragment>`, `sarness model <id>`, `/model [id]`. The choice is remembered per server, or per
folder with `--project`. A model change mid-session is safe: the transcript is engine/model neutral and a note is appended for the new model.

### Shared-GPU etiquette (important on a shared engine)

The engine reloads a model whenever the requested `ctx`/`gpu_layers` differ from what is loaded, and loading a model unloads the
previous one. Sarness therefore:

* sends the **loaded** model's exact `ctx`/`gpu_layers` with every request, so using an already-loaded model never reloads it;
* if a *different* model is loaded, **asks before unloading it** (`--allow-model-swap` for scripts) and refuses outright while it is generating for someone else;
* checks free memory before any load (the model's estimated size + max(8 GB, 12 % of memory) must fit in what the engine host reports free, counting other GPU processes); if not, it asks (`--allow-low-memory` for scripts) rather than starving other workloads;
* re-checks the engine before **every turn**: if another client switched the model meanwhile, it pauses instead of evicting theirs.

## Effort and thinking — what is real

Sarness Engine 0.4.0 forwards exactly one reasoning control to the model server: the boolean **`thinking`** (chat-template
`enable_thinking`), and only for local models. There is **no native low/medium/high reasoning-effort parameter** (not even for
gpt-oss). So `--effort` is an **agent-side strategy**, reported as such by `/effort` and `/status`:

| effort | per-turn output budget | thinking when `--thinking auto` (reasoning-capable local models only) | agent behaviour | step limit |
|---|---|---|---|---|
| low | 4 096 | off | direct, minimal exploration, one quick check | ×0.5 |
| medium | 8 192 | model default (parameter not sent) | normal inspect → edit → verify | ×1 |
| high | 16 384 (+8 192 if thinking) | on | `update_plan` first, read files fully, review own diff | ×1.5 |
| max | 32 768 (+16 384 if thinking) | on | high + a mandatory final `git diff` review pass | ×2 |

* `--thinking on|off|auto`, `/thinking …` are native toggles; on models the engine does not report as reasoning-capable they are
  sent only if you ask, and the CLI says they may have no effect. For remote-endpoint models they are unsupported and ignored.
* **Generation budget and context are budgeted separately.** The output budget never exceeds 45 % of the context window, so
  a high effort on a small window cannot starve the prompt (the report says when it was reduced). If reasoning eats a whole turn's
  budget, the next turn automatically runs with thinking off.
* `--max-tokens N` (or `config set max_output_tokens N`) overrides the answer budget.

## Permissions

Enforced in the **tool execution layer** (`policy.py`), before any side effect. The model cannot change them, approve anything, or
influence a verdict by what it writes; approvals only come from your terminal.

| | ask | auto (default) | full-access | bypass |
|---|---|---|---|---|
| read inside the workspace | ✔ | ✔ | ✔ | ✔ |
| edit inside the workspace | asks | ✔ | ✔ | ✔ |
| ordinary commands (tests, builds, git local) | asks | ✔ | ✔ | ✔ |
| dependency installs, network access | asks | asks | ✔ | ✔ |
| local destructive (`git reset --hard`, `rm -rf .`, recursive folder delete) | asks | asks | asks | ✔ |
| secret-looking files (`.env`, `*.pem`, …), `.git` internals | asks | asks | asks | ✔ |
| outside the workspace · `sudo` · `git push` / `ssh` / publishing · `curl … \| sh` | asks | asks | asks | ✔ |
| credential stores (`~/.ssh`, `~/.aws`, Sarness config) · `rm -rf /` `~` · `mkfs` · `dd of=/dev/…` · shutdown · fork bombs | **refused** | **refused** | **refused** | ✔ |

* `--mode ask|auto|full-access`, `/permissions [mode]`. `/permissions X --save` stores ask/auto/full-access as the default.
* **Bypass** is `--dangerously-skip-permissions` or `/permissions bypass` (typed confirmation only when enabling it).
  It authorizes all tool calls before command classification or filesystem policy. There is no Sarness denylist,
  privileged-command exception, approval callback, or outside-workspace restriction in bypass.
  `sudo`, `pkexec`, SSH, network/publishing, destructive commands, Git internals, credential-shaped fixture paths,
  absolute paths and escaping symlinks are allowed subject to OS permissions.
* Bypass cannot be a global default or selected by a project file. It is recorded in private session metadata:
  explicitly resuming a bypass session restores it without repeating the flag. `--mode ask|auto|full-access`
  explicitly selects normal mode on resume; an explicit bypass flag or inherited bypass takes precedence.
  New prompts, `/new`, `/resume`, delegates and nested local CLI tasks retain the active policy.
  The status bar displays **BYPASS** and the launch banner states unrestricted execution.
* Every executor uses one effective session policy. Delegates share it, including outside the parent workspace in bypass.
  Shell children inherit `SARNESS_PERMISSION_CONTEXT`, including after changing directories. SSH does not automatically
  forward environment variables: include `--dangerously-skip-permissions` when launching Sarness on a remote host.
  There is no native subagent scheduler. Missing/malformed context fails explicitly instead of silently downgrading.
* Normal-mode approvals show the diff, apply to the exact action, and remember "always" only for that call this session.
  Normal file tools remain confined to the workspace; project settings can only request `ask`/`auto`.
* Bypass shell commands inherit the existing environment, including authorized authentication mechanisms.
  Normal commands still remove secret-looking environment variables. Both retain closed stdin, process-group
  cancellation/timeouts, bounded output and known-token output redaction. These are execution mechanics, not permission gates.
  Bypass does not grant Linux privileges: `sudo -n` succeeds only under existing authorization and otherwise reports sudo's
  actual authentication failure. No sudo configuration or OS authentication is changed.
* The normal-mode shell classifier is a conservative heuristic, not an OS sandbox.
  See [the permission execution contract and acceptance procedure](docs/PERMISSIONS.md).

## Long output and agent-loop hardening

Each rule below has a regression test (`tests/`).

* **Only a cleanly finished turn is acted on.** The SSE stream must end with `done`; a dropped connection mid-turn executes nothing.
* **`finish_reason=length` discards the whole turn**, and the cause is classified: a tool call cut off (told which tool and how big),
  reasoning that consumed the budget (thinking is switched off for the next turn), or plain text. Three in a row stop with advice.
* **Malformed JSON anywhere in a turn discards the turn** — a partial turn never half-executes. Schema errors (unknown tool, wrong
  argument types) come back as tool errors; argument types are validated strictly (numeric strings are coerced losslessly).
* **Chunked writing without duplication:** `write_file` + `append_file(expected_length)`; a retried append that already landed reports
  "already applied" instead of duplicating; `write_file` of identical content is a no-op; elision placeholders (`# ... rest unchanged`)
  are refused; Python/JSON syntax is checked after writes.
* **Tool calls written as ordinary text are never executed.** They are detected with a real JSON decoder (not a regex), the model is told once, and then the
  session stops and says the model cannot operate the agent — switch with `/model`.
* **Bounded memory and transcript:** command output is streamed to a log file (full log kept on disk, 50 MB cap, head+tail returned);
  tool results scale with the context window; old rounds are compacted *in place* with a digest and archived to `archive.jsonl`;
  `repaired_history` degrades in stages and never separates a tool message from its call.
* **Retries and timeouts:** bounded reconnect with backoff for network failures (the tunnel is re-opened), no retry for auth/busy/model errors,
  context-overflow errors shrink the budget, command and request timeouts are configurable.
* **Loops:** identical calls, identical call+result, and repeated identical errors are detected (warn, then stop); repeated denials pause the task.
* **Crash/interrupt safety:** the session is written atomically after every message; every recorded call has a recorded result; after a
  crash an unfinished call is marked "status unknown" and **never replayed**. Ctrl-C kills the process group and keeps the session.
* **Completion is checked against reality:** after edits the agent must run a command; the final run summary (changed files with
  +/−, last command and its real exit code, "NOT RUN"/"FAILED" when applicable) is computed from tool results, not from the model's claim.

## Configuration

`sarness config` prints every effective setting and its source. Layers (low → high): defaults, `~/.config/sarness-cli/config.json`,
the server profile (default model), a private per-folder entry (`config set … --project`), a repository `.sarness/settings.json`
(whitelisted, never loosens permissions), environment (`SARNESS_SERVER/MODEL/EFFORT`), flags.

Settings: `server model effort thinking permissions max_output_tokens context_tokens max_steps command_timeout_s request_timeout_s retries
show_reasoning log_level session_dir workspace`. Files (all 0600, private dirs 0700): config, `secrets.json`, sessions
(`~/.local/state/sarness-cli/sessions/<id>.json` plus `<id>/events.jsonl` and `cmd-logs/`). Nothing machine-specific is written inside your projects.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,keyring]' && .venv/bin/python -m pytest tests -q
```

The suite includes a real local HTTP/SSE engine, a real TCP relay behind a stand-in `ssh`, TLS pinning against a self-signed certificate,
and end-to-end `sarness run` flows. Permission propagation and installed/live verification are documented in [docs/PERMISSIONS.md](docs/PERMISSIONS.md).

## Not implemented / known limits

* No native reasoning-effort control (engine limitation, see above); no SNP/1 transport (SSH tunnel and TLS are used instead).
* Tool calling for local models is *unverified* unless probed; small models (≤ 8B) write weak tests and can rewrite files wholesale — review the diffs.
* Commands are not OS-sandboxed. Linux/Ubuntu only (macOS likely works; Windows untested).
* One agent task at a time; no background process tool (`start_process`) and no MCP.

## The Sarness terminal (`sarness`, `sarness code`)

`sarness` opens the Sarness terminal: a maintained fork of [OpenCode](https://github.com/anomalyco/opencode) 2.0.32
(MIT) with Sarness branding, a bordered composer and a persistent line under it:

```
────────────────────────────────────────────────────────────
❯ Fix Schemra checkpoint recovery and verify tests
────────────────────────────────────────────────────────────
  Qwen3.6 35B-A3B · High · Spark · BYPASS · build
```

It keeps OpenCode's agent runtime and tools: read/write/edit/search, shell, parallel tool calls, slash commands,
the command palette (`ctrl+p`), sessions, resume and forking, undo/redo, compaction, agents and subagents, `@` file
mentions, attachments, MCP, skills, custom commands, diffs, context usage and cancellation (`esc`). Inference goes
only to Sarness Engine, through a loopback-only bridge with a fresh per-launch key. The Sarness token never reaches the
terminal.

* **Install:** `sarness tui install` builds it from the pinned upstream commit plus the patch series in
  `sarness_cli/tui_build/` (Bun is pinned and checksum-verified). The previous build is kept for rollback. If it is
  not installed, plain `sarness` falls back to the Python runtime and says so.
* **Separate from OpenCode:** its data lives in `~/.local/share/sarness-code` (config `~/.config/sarness-code`). A
  normal `opencode` installation is never written to or used for inference. Sessions from 0.3.x `sarness code` were
  stored by the stock OpenCode: run `sarness tui migrate` in a project to copy that project's sessions across (OpenCode
  is only asked to list and export them; the originals stay where they are).
* **Startup:** if your default model is loaded it starts at once. If nothing is loaded, it loads the default (only if
  it fits in the engine host's free memory with headroom) and waits for it. If a *different* model is loaded, it asks
  whether to use that one or switch; scripted runs fail with the exact flag instead (`--model <loaded>` or
  `--allow-model-swap`). With no default yet, it opens the model selector and saves your choice.
* **The info line** is live: the model and effort variant currently selected (`ctrl+t` cycles effort), the server,
  and the permission mode the generated rules enforce (auto-accept toggled in the terminal shows as `AUTO-ACCEPT`).
  Engine 0.4 only has thinking on/off, so `Low` = thinking off and `Medium`/`High` = thinking on; they do not differ
  otherwise.
* **Permissions:** `ask` asks for edits and commands. `auto` allows edits and ordinary commands but asks for installs,
  network, `rm -r`, `git push`, `sudo` and similar. `full-access` asks only for destructive commands. Bypass allows
  everything.
* **Sarness commands:** `/cabinet` opens the model cabinet (the terminal pauses while it runs). `/upgrade` points to
  `sarness upgrade`. `/connect` explains that models come only from Sarness Engine.
* Only the loaded model is offered, with its live ctx/gpu_layers pinned: the bridge never reloads or swaps a model
  behind other users. Ctrl+C belongs to the terminal (the launcher does not die on it).

See [docs/TERMINAL.md](docs/TERMINAL.md) for the fork design and delta, and [docs/OPENCODE.md](docs/OPENCODE.md) for
the bridge protocol and earlier live results.
