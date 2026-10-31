# Live acceptance: Schemra (2026-10-31, 0.3.0)

Installed `sarness` 0.3.0 run directly on the DGX Spark (Python 3.12, aarch64), native runtime, mode `auto`, model
Qwen3.6 35B-A3B MXFP4_MOE (ctx 131072) on Sarness Engine 0.4.0. Workspace: a fresh clone of the deployed Schemra
repo under `~/.local/state/sarness-cli-acceptance/schemra-ws`, with `SCHEMRA_HOME` pointed at a scratch dir and
`CUDA_VISIBLE_DEVICES=""` (no training; the live `~/schemra-data` and services were not touched). Driver:
`schemra_acceptance.sh` (kept with the evidence on the Spark). Runs were started only after the Spark had been
idle of Schemra jobs for 10 minutes.

| step | result |
| --- | --- |
| baseline (by hand) | Schemra suite 5/5 pass |
| 1. find/inspect project | agent summarised worker → engine → checkpoint → retention → merge flow with file refs |
| 2. run suite, blockers | ran it; no missing dependencies |
| 3. bounded improvement | added `tests/test_retention.py` (9 tests); suite 14 pass, verified independently |
| review finding | first version re-implemented `retention()` in a fake class (tests did not cover the real method) |
| 4. correct the error | second session rewrote the tests to call `engine_hf.Run.retention`; mutation check: breaking the real guard → 4 failures (reproduced by hand), restore → `git diff schemra/` empty |
| 5. interrupt / resume | Ctrl+C (SIGINT) during a test command → exit 130, session `interrupted`; `sarness resume` re-ran the cancelled command and finished; 3 commands logged, none replayed |
| 6. honest summary | run summary built from tool results; Schemra's `test_worker_pause_resume_crash_recovery` was observed to fail once and pass otherwise (timing-sensitive, real flake in Schemra) |

CLI defects this found and fixed in 0.3.0: `resume ID --opts "text"` rejected on Python <= 3.12; summary listed a
reverted temporary edit as a change. A harness mistake was also found: a background job in a non-interactive
shell ignores SIGINT, so the first "interrupt" test never interrupted anything (re-done with job control).

Model-quality observation: the agent's own reasoning about flakiness was once self-contradictory, and its first
test was tautological. The CLI's verification line reports what ran and exited; it cannot judge test quality,
so review agent-written tests.



# Live acceptance: Ctrl+C (2026-10-31, 0.4.1)

Installed `sarness` 0.4.1 and its installed terminal, on the Linux x86_64 desktop and on the DGX Spark (aarch64, built
there with `sarness tui install`). Every test sends the actual `0x03` byte once through a pseudo-terminal (the Spark
runs also go through a real `ssh -tt`), then checks the visible effect, the Engine's `active_streams`, that the owned
command is gone, that no process carrying the run's marker survives, and that the terminal is restored. Real-Engine
runs used the model that was already loaded (no load or swap), a scratch config/data copy, disposable directories, and
waited until the Engine had no stream from anyone else and enough free memory (the Spark was running other work).

| check | desktop | Spark |
| --- | --- | --- |
| hermetic terminal suite (`tests/test_ctrl_c_tui.py`: startup 0.2-2.5 s, idle, text, generation, shell, `!`, stubborn command) | 13/13 (pristine build from the patch series) | 13/13 (aarch64 binary) |
| Python suite (native, launcher, everything else) | 453 passed | 453 passed |
| over `ssh -tt` with a fake engine (idle, generation, shell command, startup) | | 14/14, nothing left on the Spark |
| real Engine: startup Ctrl+C at 0.4-5 s through the launcher and SSH tunnel | 5/5, no tunnel master left | |
| real Engine: `sarness` shows model, effort, server, permissions; no OpenCode welcome | yes | yes |
| `/cabinet` opens and one Ctrl+C returns to the terminal | checked by hand against a fake engine | yes (real Engine; nothing was selected) |
| real agent task: wrote `calc.py` and `test_calc.py`, ran the test | | yes (test printed PASS) |
| real generation, one Ctrl+C: stream released, app stays at the prompt | released in 4.2 s | released in 0.3-1.8 s |
| session continues after the cancel (a reversed-word question the prompt cannot contain) | yes | yes |
| `!` command and the agent's shell command, one Ctrl+C each | `!` yes | both yes (the process was gone) |
| idle Ctrl+C exits; the shell is usable; no Sarness process left | yes | yes |
| resume (`--session`) restores the conversation and answers a new request | yes | yes |

Not covered: cancelling during prefill (the Engine sends response headers only with the first token, so a stream is
released when prefill ends), and a remote command started by an `ssh` the agent itself runs (Sarness only stops the
local client).


# Live acceptance: agent reliability (2026-11-01, 0.4.2)

Real model, real server. The agent ran on the desktop (terminal built from the 0.4.2 patch series, CLI from the branch) against the
DGX Spark's Sarness Engine (Qwen3.6 35B-A3B MXFP4_MOE, ctx 32768, the model that was already loaded: nothing was loaded, swapped or
unloaded) through the user's SSH tunnel profile, and SSHed to the Spark for real. The "server" was a throwaway directory and one
disposable user service (`sarness-accept-g042-r1`, a tiny HTTP service with a seeded bug) that were removed afterwards. Mode `auto`
with a strict approval driver: each permission prompt was read and approved only if the command was clearly about that directory
and that one unit (heredoc and `bash -c` forms are unwrapped and checked inside); everything else was rejected and logged. Before
every run the Spark was idle (load 0.0-0.3, GPU 0%, 57-61 GB available); other Spark processes (Engine and its llama-server, Schemra,
the semechka services) had the same PIDs and start times before and after every run. No credential value is in the saved evidence.
The task: read a long operations file, find why `/mean?values=2,4,6` returns 3.0 on the server, fix `calc/core.py`, run the tests,
deploy with `scripts/deploy.sh` (rsync plus `systemctl --user restart`), verify the live service. The checks below were made by the
driver afterwards from files, the live service and the ledger, never from the model's account.

| run | what | outcome (independent checks) |
| --- | --- | --- |
| D | **pure 0.4.1**, a ~5k-character multi-phase directive (the shape of the Trinodal one) | **failed.** The directive was compacted within the first minute; the summary dropped the user name, so the model SSHed as `dembele@` instead of `fylume@` and typed `systemctl--user`. Nothing was changed (the approval driver rejected the wrong-target command) |
| E | **0.4.2**, the same directive | **done**: source fixed, tests pass (run after the change), deploy ran and the service answers 4.0, remote copy equals local, only the expected files on the server. 1 real compaction, 23 recorded operations, 9 assistant turns, 0 questions, 2 malformed `ssh` commands repaired by the guard (the approval prompts showed the corrected command) |
| A | 0.4.2, short prompt, guard on | **done** (same checks): 1 compaction, 16 operations, 8 assistant turns, 0 interventions needed |
| B | 0.4.2, three prompts in one session (inspect, fix and test, deploy and verify) with `/compact` typed between them: 2 deliberate compactions at different stages | **done**: the objective changed with each prompt, the facts, the file change and the test run (against the current change) carried across both compactions, the deploy was accepted only because tests had run against the current change. 17 operations, 15 assistant turns, 1 `announced` push (a stated intention with no tool call), 0 questions. The model tried to edit the deployed copy over ssh (`sed -i`), which the approval driver rejected; it then used the project and `deploy.sh` |
| C | 0.4.2 terminal, short prompt, **guard off** | done as well: on a simple task this model needed no help, so these runs do not show the guard to be necessary on their own. The deterministic scenarios (`tests/test_scenario_*.py`) are what pin each guard behaviour |

Metrics (runs A, B, E, guard on): duplicate diagnostics that ran with no change in between: 0 (every repeat the ledger shows had a source
change, a deploy or a compaction between its two runs, which the guard correctly let through); repeated failing commands: 0; unnecessary
confirmation questions reaching the user: 0 (no `question` tool call and no text-only turn asking permission in any of the runs);
tool calls before the first source change: 6 (B), 11-12 (A), 16 (E); files changed: 1 (`calc/core.py`); tests run: 1 each, all after
the last change; completion correct in all of them.

What this does and does not show. It shows that 0.4.2 completes a realistic multi-step ssh, fix, test, deploy task on the real model
through real compactions, and that the same long directive broke pure 0.4.1 in the way the real sessions did. The window arithmetic,
the request kept beside the summary and the checkpoint wording (patch 0009) are active in runs A, B, C and E; run C therefore does
not isolate the guard from them. Qwen3.6 is a capable model; the permission-asking and loop behaviours in the real sessions did not
recur in these short runs, so they are covered by the scripted scenarios, not by a live reproduction. No second local model was
loaded for comparison: that would have evicted the loaded model on a shared Spark.

Bugs this acceptance found in the guard itself (all fixed and covered by tests): a compaction request flattens the history into one
user-role message of transcript text, which was being read as the user's words (and could have widened the scope); a call the user
declined at an approval prompt was recorded as having run.
