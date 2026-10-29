# Permission policy and bypass verification

`--dangerously-skip-permissions` selects a live, workspace-scoped `EffectivePermissions` policy for the process. It is not saved as a default or recovered from untrusted session content. Pass the flag again on a later launch or resume.

## Findings

The original argument parser and top-level workspace already retained bypass for ordinary commands. A baseline live coding task in that path completed without prompting; the reported interruption could not be reproduced on that direct path. The installed executable was an editable pipx installation pointing to the checkout, rather than a stale wheel.

The reproducible propagation failure was downstream: `Workspace` defaulted silently to `ask` without parent context, and the shell executor stripped every `SARNESS_*` variable. A nested `sarness run` therefore read the configured permission default again instead of inheriting the active session policy. There is no native subagent scheduler; delegated tools and nested CLI tasks are the available delegation paths.

Authorization also lived in individual handlers, omitted some registered helpers, and happened after file contents were read for edit previews. The general policy had an allow fallback for unclassified tools. These gaps made enforcement and future delegation inconsistent.

## Enforcement

1. CLI parsing preserves the bypass flag before or after the `run`/`resume` subcommand.
2. Launch resolves an effective policy after locating the restored workspace. The active launch flag takes precedence.
3. The agent, toolbar, and every workspace executor use that same policy. New prompts, `/new`, and `/resume` retain it.
4. Dispatch validates arguments and authorizes the tool before entering its handler. Bypass is evaluated before constructing an ordinary approval request.
5. `Workspace.delegate()` shares policy and confirmation handling, confines children to the authorized workspace, and uses separate command logs. Nested execution also inherits the active policy through a scoped context variable.
6. Shell commands receive `SARNESS_PERMISSION_CONTEXT` containing only the effective mode and authorized root. A nested CLI validates this context; it cannot implicitly expand to another workspace. Missing executor context and invalid inherited context produce explicit errors.
7. Unknown tools fail closed. Normal Ask-mode approvals and edit previews remain. Sensitive file contents are not read just to render approval previews, and bulk searches filter escaping symlinks.
8. Session JSON never restores permission authority. A fresh launch without the flag uses its current configured mode.

Credential stores and catastrophic commands remain refused. Secret-looking files, Git internals, privilege escalation, system write targets, remote changes, publishing, and workspace boundaries remain explicitly gated. Model swaps and shared-engine memory checks retain their independent confirmations. Shell classification is heuristic, not an operating-system sandbox.

## Verification, 2026-10-30

The installed executable was rebuilt/reinstalled using `./install.sh` and resolved in a fresh login shell to `~/.local/bin/sarness` (pipx).

A real interactive CLI session against a running Sarness Engine and Qwen3.6 35B-A3B completed a resumed coding task with reads, multiple exact edits, three test/build commands, file creation, and deletion. A second prompt in the same session created a virtual environment, installed a local wheel with pip, verified its import, and launched a nested `sarness run` **without** a bypass flag. The nested task wrote a file and ran the tests. All final commands exited zero; zero ordinary approval prompts appeared.

Launch used:

```sh
sarness --dangerously-skip-permissions --resume SESSION_ID \
  --model local:qwen3.6-35b-a3b@MXFP4_MOE --effort low
```

All **270 tests passed** against the installed CLI. Regression coverage includes real shell/file effects, builds, mode changes, scoped delegation, multiple interactive prompts, nested CLI execution, launch/resume precedence, missing/invalid context, normal approvals, and critical boundaries. The process tests use a scripted HTTP/SSE inference server while executing actual installed CLI processes and real tools. To run them against an installed executable:

```sh
SARNESS_TEST_EXECUTABLE="$(command -v sarness)" python -m pytest tests -q
```
