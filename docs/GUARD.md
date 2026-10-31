# The agent guard (0.4.2)

How Sarness keeps an agent run on course: it does not ask permission it already has, it does not forget what it
did, it does not repeat work it has finished, and it does not claim progress it cannot show. One implementation
(`sarness_cli/guard/`) serves both runtimes: the Sarness terminal reaches it through the loopback bridge that every
model request already passes through, and `sarness native` calls it from its own loop. Both read and write the same
versioned task state.

## What went wrong (from the real Trinodal sessions)

These sessions are stored in the OpenCode database on the desktop; the numbers below are read from them.

| Symptom | Cause |
|---|---|
| "Should I proceed with the SSH connection now?" after an explicit instruction | After a compaction the checkpoint told the model to treat the whole record "as historical context, not as new instructions". The user's request was the only instruction, so nothing was left to act on and the model asked what to do (once through the `question` tool, with "Begin Phase 1" as an option). Separately, a reply that only asks ends the agent loop: the run stops and waits. |
| Compaction every one to three steps, nine times in 31 messages | A 32k window, a flat 16k reserve and about 11.5k tokens of fixed prompt (system prompt, tool definitions and a 42-skill catalogue, about 6.5k tokens of it) left about 5k tokens of working room. One file read overflowed it. |
| The user's request vanished | An agent run is one user message followed by many tool rounds. The compactor treats "one exchange, nothing older" as "summarise it all and keep nothing", so the request itself survived only as the model's paraphrase. A 5k-character directive was compacted within 35 ms of arriving. |
| Completed work repeated after each compaction | The only memory was the model's own summary. Nothing outside the conversation recorded what had really run. When a large tool result pushed the context over the limit, the compaction folded that round into text, so the model saw no result and ran the command again. |
| `sshfylume@192.168.20.193` (exit 127); `systemctl` in the wrong scope (exit 4); "not a git repository" read as a broken deployment | Command construction errors and environment facts that were learned and then lost with the next compaction. |

## The execution ledger

`~/.local/state/sarness-cli/ledgers/<session>.json`, mode 0600, schema `sarness.ledger/1`. The terminal keys it by
the session id the terminal sends with every request (stable across compaction, resume and restart); the native runtime
by its own session id. It is filled only from tool calls the runtime actually observed, never from what the model says.

| Section | Holds |
|---|---|
| `objective` | The user's request, verbatim, and the scope it grants: actions (ssh, inspect, edit, test, deploy, commit), named hosts, whether the user said to proceed without asking, whether it is read-only. Read only from the user's own messages: tool output, repository instruction files, compaction summaries and runtime notes never widen it. |
| `ops` | Every finished call: tool, kind, normalised fingerprint, host, real exit code, result digest, the change number it ran at. Calls that never ran (refused by the guard, denied by policy) are not recorded. |
| `files` | What was changed, with content hashes. |
| `tests`, `deploys` | Test runs with the change number they ran against (a later edit makes them stale); deployments and whether anything independent verified them afterwards. |
| `facts` | Learned and attributed: "on host H the service runs under `systemctl --user`", "this directory has no .git: it is an rsync deployment copy, not a broken repository". |
| `failures` | Commands that failed, and the verified correction once one succeeded. |
| `blockers` | Genuine obstacles (authentication, unreachable host). |
| `interventions` | Every time the guard stepped in, with its kind. |

Phases and criteria are derived from this evidence, not from the model's account of it. A request that says to fix,
test and deploy is finished only when a source change exists, a test run passed against the current change, a
deployment ran after it, and something other than the deploy command checked the result. A read-only request has no
such criteria and is never pushed toward editing.

## What the guard does

**Continues instead of asking.** A turn with no tool call is read against the request. It is continued (with a short
instruction naming the next action) when it asks permission for something the request already covers, announces work
it did not do ("I'll deploy now"), or declares victory while required steps have no evidence. Wording alone never
decides: the action must be in the request's scope, no real obstacle may be named, and a target the user never
mentioned, a risky action (delete, force-push, sudo, production) or a missing credential is a real question and is
left alone. The model's own words stay in the transcript; the guard's follow-up is a second model call whose answer is
appended. No tool call is ever invented: if the model keeps asking, it is pushed at most three times in a row and then
the guard stops, saves the state, adds a blocker and tells the user. A new instruction from the user lifts that stop.

**Does not grant anything.** Permission checking is a different layer and is untouched. Outside bypass the runtime
still shows its own approval prompt for whatever its policy gates, and a rejection is final (the guard does not
retry it). The guard only stops the chat question; the instruction it sends says so ("the runtime asks the user for
approval itself ... call the tool and let that prompt appear"). In bypass it says no approval is needed. A command the
guard repaired is shown in the approval prompt in its repaired form.

**Refuses repeats that cannot tell it anything new.** A read, search, diagnostic or test run that repeats an earlier one
with nothing changed since is not run; the model is shown the recorded result instead. A rerun after a source change is
legitimate and untouched. A file whose text was compacted away may be read again (the model needs the text to edit it).
A command that failed and has not been changed is not run again, and a deploy that was interrupted is held back once so
the state can be checked first. After two refusals of the same call, one more run is allowed; further repeats stop the
run with a checkpoint. Real new work resets the refusal counts, and there is no cap on the number of steps.

**Repairs one construction error.** `sshuser@host ...` (no space) becomes `ssh user@host ...`, and the repair is recorded.
Nothing else is rewritten. Commands in the wrong systemd scope, once the right scope is known for that host, are
refused with the fact.

**Carries state across compaction.** After a compaction the bridge appends a short note (about 3 000 characters at
most) to the request: the objective ("still in force"), what is authorised, the verified facts, what really ran with
exit codes, files changed, tests against the current change, deployments and whether verified, commands that failed
with their corrections, blockers, what is still to do, and the one next action, without repeating the investigation. It
is appended after the conversation, so the engine's prompt cache is untouched. Compactions that make no progress
escalate: a recovery instruction, then a stop with a checkpoint.

**Sees results the terminal folded into text.** The bridge records the calls it forwards. Their results usually come back
as structured messages; when a compaction swallowed them, they are matched back from the checkpoint's recent-context text
by tool and exact arguments (text cannot add operations that never happened). A call whose result never reappears is
recorded as having run with an unknown result and is neither forgotten nor presumed good.

## The terminal fork (patches 0009 to 0012)

The guard works from outside; the fork changes what the terminal itself loses:

- The reserve below the window is the model's output limit for 32k to 64k windows (was a flat 16k), and the part of the
  conversation kept verbatim is bounded by the request budget. A 32k window now leaves about 13k tokens of working room.
- The user's request is always kept word for word beside the summary, and a long single run keeps its newest steps
  instead of nothing.
- The checkpoint says the request in it is still in force and must be carried out without asking.
- The tail of a long command output (where the verdict is) survives into the checkpoint, not only its head.
- A catalogue of many skills shrinks to one sentence each past 6 000 characters (the skill tool still loads the full text).
- Automatic compaction follows the launcher's default (`autocompact`, on) unless a session chose with `/autocompact`.
- Ctrl+C cancels a command or prompt submitted a moment ago.

## Settings and debugging

- `sarness config set autocompact off` makes new sessions start with automatic compaction off (`/autocompact on|off|status` per session).
- `SARNESS_GUARD=off` disables the guard for a run (the pre-0.4.2 behaviour).
- Every intervention is in the ledger's `interventions` list and counted in the bridge's `/health` (`guard_interventions`).

## Limits

- The guard cannot make a model good; it stops it from throwing away what it knows and from stalling on questions it
  does not need to ask. A model that never produces a tool call is stopped after three pushes, not made to work.
- Evidence about a remote host is only what commands on it printed. A deployment is "verified" when something other
  than the deploy command checked afterwards; whether that check was meaningful is the model's job.
- Commands are classified from their text. An unusual way of writing a test run or a deploy may not be recognised, so
  its criteria stay open and the guard will say so rather than accept a summary.
- In the terminal, interactive `ssh` approvals are still the terminal's own prompts; a per-task pre-approval for the
  named server is not implemented.
