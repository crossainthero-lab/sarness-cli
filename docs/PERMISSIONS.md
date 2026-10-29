# Permission execution contract and acceptance

Version 0.2.3 makes `--dangerously-skip-permissions` unrestricted at the Sarness execution boundary. Version 0.2.2 still classified bypass commands, refused protected categories, confined file tools/delegates, filtered discovery, and instructed the model to refuse privileged/remote/outside-workspace requests. Replacing `ask` with `deny` did not implement bypass.

## Authoritative decision

`Workspace._gate()` immediately records `allow` and returns in bypass, before calling any downstream permission handler or approval callback. `EffectivePermissions.assess()`, `assess()`, `verdict_for_command()`, `verdict_for_path()` and `boundary()` also immediately allow bypass. Command classification is never invoked for a bypass verdict. Unknown tool names and malformed arguments remain tool API errors, not permission decisions.

There is no bypass exception for catastrophic command strings, credential stores, secret-shaped filenames, Git internals, protected/system/outside paths, privileged commands, remote operations, publishing, network/install commands, obfuscated or nested shell commands, or destructive operations. Destructive live tests operate only on disposable fixtures; catastrophic system command strings are assessed only, never executed in tests.

## Audited execution paths

- `policy.py`: command/path verdicts, classifier, boundary helper, inherited context, delegate scope and effective policy. Normal-mode classifier and restrictions remain intact.
- `tools.py`: dispatch authorization, path resolution, list/search/find discovery, all file read/write/edit/append/move/delete handlers, shell subprocess environment, Git helpers and nested/delegated contexts. Bypass removes workspace and sensitive-file filters and retains the authorized environment. Active nested bypass cannot be accidentally replaced by a helper's default mode.
- `agent.py`: model instructions, permission exception handling, denial streaks, model preflight consent and session persistence/listing. Genuine filesystem `PermissionError` becomes `OS PERMISSION ERROR`, not `DENIED`, and does not count toward policy-denial stops. Shell failures keep the actual exit code and stderr.
- `cli.py`: launch flags, resume/continue resolution, project settings precedence, effective policy construction, model preparation and swap callbacks. Private session metadata records mode; explicit resume restores bypass unless the user selects normal mode. Conversation text does not grant authority.
- `repl.py`, `ui.py`, `menu.py`: truthful bypass display, idempotent mode selection, new/resumed sessions, reconnect/model preparation and tool help. The typed `/permissions bypass` consent applies only when transitioning from normal mode; starting with the flag never requests it.
- `cabinet.py`: bypass skips model-load and active-model consent in the interactive cabinet. Normal-mode consent is unchanged.
- `runtime.py`, `connection.py`, `engine.py`, `tunnel.py`, `setup.py`, `config.py`: reviewed for hidden execution approvals. Engine transport authentication, TLS verification, SSH host-key verification, malformed configuration and service/resource failures remain genuine connection/setup requirements, not tool permission denials. Bypass does not rewrite OS, SSH or sudo configuration.
- `context.py`, `core.py`, `desktop.py`, `modelinfo.py`, `paste.py`, package entrypoints and installer: reviewed; no additional tool permission handler. Delegation uses shared Workspace policies or nested CLI processes; no native agent scheduler exists.

## Inheritance and OS authority

Shell children receive `SARNESS_PERMISSION_CONTEXT`; bypass remains effective after changing directories or entering a sibling workspace. Synchronous nested dispatch uses a context variable and shares the policy object. Failures reset the context without changing the policy. Explicitly resumed sessions restore bypass from private session metadata; global/project configuration cannot enable it. Explicit user mode changes are honored.

SSH does not forward arbitrary local environment variables by default. A remotely launched Sarness process needs the bypass flag or an explicitly forwarded context. The bypass model instructions authorize remote CLI invocations with that flag. SSH commands themselves are unrestricted. Existing SSH authorization and host-key trust are still required.

Linux DAC/ACLs, read-only mounts, immutable files, sudo authentication, polkit authorization and remote credentials still apply. `sudo -n` uses existing unattended authorization or returns its own authentication error. Sarness never defeats authentication or modifies sudoers. Tool schemas, argument validation, file-format/size handling, output bounds, timeout/cancellation and loop/step limits remain execution mechanics.

## Verification

Run the full suite against the installed executable, then lint, compile and build:

```sh
SARNESS_TEST_EXECUTABLE="$(command -v sarness)" python -m pytest tests -q
python -m ruff check sarness_cli tests
python -m compileall -q sarness_cli tests
bash -n install.sh
python -m pip wheel . --no-deps --wheel-dir /tmp/sarness-wheel
```

`test_unrestricted.py` covers unconditional allow at every policy entrypoint, a future unknown policy tool, synthetic credential-shaped/Git paths across file tools and discovery, absolute/traversal/symlink paths, disposable root deletion, outside delegates, nested defaults, child cwd changes, environment preservation and distinct OS error reporting. Existing tests cover normal-mode restrictions and actual approval prompts. Installed PTY tests cover multiple prompts, nested CLI execution and resumed sessions.

The opt-in live runner uses the actual configured engine and installed executable, with no model or tool mocks and no automatic bypass approval answers:

```sh
python3 tests/live_permission_acceptance.py --output /tmp/sarness-live-local --remote-target fylume@192.168.20.193
```

It isolates configuration/state and fixtures, records raw PTY transcripts, verifies filesystem effects independently and inspects permission events. Run it on both machines. The result must show zero Sarness approval prompts, zero bypass policy refusals, no mode downgrades, actual remote/nested execution, truthful sudo/OS failures and a working normal-mode approval control. Spark's self-SSH test can use an existing forwarded SSH agent and a temporary known-hosts file copied from the already trusted local entry, without changing machine configuration.
