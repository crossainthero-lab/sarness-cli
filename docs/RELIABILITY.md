# Reliability (0.2.4)

Incident: a Qwen3.6-35B-A3B session wrote a ~30k-char `write_file`; the output budget (8192 answer + 4096 thinking
tokens at medium effort) ran out mid-call, the response ended with `finish_reason=length`, and the agent discarded the
whole turn. Three such turns in a row parked the session.

What happens now:

* **Output-limit hit**: the cut-off call is never executed. Complete calls that came before it in the same response
  run. The retry gets a larger output budget (x1.5, then x2, bounded by the context cap), thinking off, and a concrete
  chunk size (`write_file` first piece, then `append_file` with `expected_length`). The learned chunk size is stored in
  the session and fed into the system prompt. Up to 5 attempts, then the session is parked and resumable.
* **Partial output** is kept in `sessions/<id>/drafts/*.partial` for diagnostics; it is never applied.
* **Malformed tool JSON** discards the turn (nothing runs), tells the model the chunk size, 4 strikes.
* **Broken streams / dropped connections / corrupt SSE events / HTTP 500-504**: retried with backoff (default 6 attempts,
  up to 30 s). The interrupted turn was never recorded, so it can not run twice.
* **Context**: file contents inside already-executed `write_file`/`append_file`/`edit_file` arguments are replaced by a
  marker in the prompt (the stored transcript is untouched), so one big write no longer crowds out the window.
* **Duplicates**: tool-call ids are made unique per session (some servers reuse `call_0`), only the newest round is
  repaired after a crash, unfinished calls become "status unknown" and are never replayed; `append_file` is idempotent.
* **Checkpoints**: every save keeps the previous good copy (`<id>.json.bak`) and `Session.load` falls back to it.
* Permissions are untouched; bypass still never prompts (covered by tests).

Tests: `tests/test_reliability.py`.
