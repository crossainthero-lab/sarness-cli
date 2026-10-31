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
