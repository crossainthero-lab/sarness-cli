# Permission policy and runtime acceptance

`--dangerously-skip-permissions` selects a workspace-scoped `EffectivePermissions` policy. Every tool dispatcher, delegate and nested CLI uses that policy. BYPASS tool decisions are **allow or deny**, never interactive approval. Normal modes retain their approval behavior.

## Actual failure found in 0.2.1

A live PTY running the installed pipx executable against Spark Engine 0.4.0 and Qwen3.6 35B-A3B MXFP4_MOE reproduced:

```text
permissions bypass
▸ run_command find . -type f -not -path './.git/*' -not -path './.venv/*'
┃ [HIGH] run_command: find . -type f -not -path './.git/*' -not -path './.venv/*'
┃   why asked: Sensitive file or Git internals: `./.git/*`
┃ Allow? [y]es / [N]o / [a]lways for this exact action:
```

This was an executor approval, not a model question. The launch flag was intact. The shell classifier treated a search exclusion pattern as a filesystem operand, set `sensitive`, and `verdict_for_command()` returned `ask` before checking bypass. `Workspace._gate()` passed that decision to `Console.confirm()`.

Both earlier changes left boundary approvals ahead of bypass. The central-policy regression tests even required bypass to ask for remote, privileged and sensitive actions. Live checks exercised simple commands that did not hit a false boundary. A banner-only PTY test could not detect this behavior. Propagation fixes were useful, but could not correct these verdicts.

The installed local executable also pointed to an editable checkout in `Documents/Sarness-cli/cli`, separate from the canonical public checkout. Its permission implementation matched the release, so this was not the cause of the reproduced prompt. Installation must still be verified independently of a working tree.

## Corrected execution contract

1. CLI parsing preserves the launch flag before/after subcommands; restored workspaces are resolved before selecting the launch policy.
2. `EffectivePermissions` is authoritative. Routine workspace reads, edits, writes, local shell commands, builds and tests are allowed in BYPASS. Protected boundaries return explicit denial, without invoking confirmation.
3. Search-pattern arguments to find/rg/grep are distinguished from filesystem operands. Actual roots, sensitive operands, and find execution arguments are still checked. Shell classification remains heuristic, not an operating-system sandbox.
4. Every registered tool is authorized before its handler reads or mutates anything. Unknown tools and missing/invalid executor context fail explicitly. A final policy invariant converts any future interactive verdict to denial in BYPASS.
5. `Workspace.delegate()` shares the same policy object and confines children to the authorized root. Context variables cover synchronous nested dispatch; shell children receive a scoped `SARNESS_PERMISSION_CONTEXT`. Nested Sarness commands do not need another bypass flag.
6. Tool failures reset dispatch context and do not alter permissions. Permission events record the actual mode, tool, verdict and reason in each session's `events.jsonl` without file contents or command arguments.
7. Additional prompts, `/new`, and in-process `/resume` retain the active launch policy. Re-selecting an already active BYPASS mode needs no second typed confirmation. The terminal banner and status bar visibly show BYPASS.
8. Saved conversation content cannot grant permission authority. A new process must opt in again with the flag; the displayed resume command now includes it automatically for BYPASS sessions. This preserves the existing rule that bypass is never a saved default.
9. Credential stores and catastrophic commands remain denied in every mode. Secret-looking files, Git internals, privileged actions, remote changes/publishing and outside-workspace shell operands are denied in BYPASS and retain confirmation in ordinary modes. Model loading/switching and shared-engine memory consent remain independent resource controls.

## Verification

The regular suite runs actual file/shell tools and installed executable PTYs with a deterministic HTTP/SSE inference fixture. It covers root flag propagation, multi-tool tasks, filesystem edits, shell/build execution, delegated/nested CLI calls, resume, missing context, failures, explicit boundary denials, and actual normal-mode approvals:

```sh
SARNESS_TEST_EXECUTABLE="$(command -v sarness)" python -m pytest tests -q
python -m ruff check sarness_cli tests
```

The separate live runner uses the existing configuration and REAL model, with no inference mocks or automatic approval answers in bypass:

```sh
python3 tests/live_permission_acceptance.py --output /tmp/sarness-live-local
# Run the same command on Spark after installing the verified commit.
```

It captures complete PTY transcripts and a report in the output directory. It creates only an isolated temporary project and normal CLI session state. It creates multiple Python files, runs a deliberately failing test, fixes it on a later prompt, executes the program and tests/build again, reproduces the source-inventory trigger, creates/moves files, adds a follow-up file, resumes across processes, delegates to a nested installed CLI without a bypass flag, exercises `/new` and in-process resume, and verifies an actual normal-mode approval. Files and test results are checked independently of model claims. Any unexpected approval makes the live run fail.
