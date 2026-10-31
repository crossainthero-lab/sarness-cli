# Changelog

## 0.3.0 - 2026-10-31

Integrates PR #1 (stream recovery, 0.2.4), PR #2 (unlimited turns, large files) and PR #3 (OpenCode bridge),
with the defects found in review and live testing fixed.

### Added
- `sarness code [task]` (alias `sarness opencode`): OpenCode 2.x as agent runtime with inference on Sarness Engine,
  through a loopback, per-launch-authenticated bridge. `runtime` setting (`native` | `opencode`). See
  docs/OPENCODE.md.
- Bridge: streamed reasoning, usage, real finish reasons, HTTP status for pre-stream errors, retries before the
  first byte, cancellation on client disconnect, loaded-ctx context limits, effort variants.

### Changed
- `max_steps` defaults to 0 (unlimited); loop/repetition guards and checkpoints still apply. `--max-steps N` and
  `sarness config set max_steps N` restore a ceiling.
- `read_file`/`search` stream files of any size; `read_file` tolerates invalid UTF-8 and clips giant lines;
  `search` skips binary files and says how many.
- The unfulfilled-action nudge only looks at the closing sentences and ignores offers.

### Fixed
- `sarness resume ID --mode auto "text"` failed on Python <= 3.12 (argparse); the prompt is now collected on every
  Python version.
- The run summary marks files whose net diff against git is empty (e.g. a reverted temporary edit) as
  `net unchanged vs git` instead of listing them as changed.
- A `done` event without `finish_reason` (Engine 0.4 sends this when llama-server's stream breaks off) was accepted
  as a finished turn; it is now a retryable stream error and its tool calls are never run.
- PR #3 launcher: passed `--model` to the OpenCode TUI (unsupported), guessed the v2 variant schema, advertised the
  model's maximum context instead of the loaded one, and did not set `$PWD` (OpenCode then acted in the parent
  shell's directory).

### Rollback
`pipx install --force 'git+https://github.com/crossainthero-lab/sarness-cli@03d2d25'` (the 0.2.4 release commit; or install the 0.2.4
wheel). Config and native sessions are unchanged by 0.3.0; the only new setting (`runtime`) is ignored by 0.2.x.
