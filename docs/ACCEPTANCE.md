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
