#!/usr/bin/env python3
"""Opt-in LIVE acceptance for output-limit / broken-stream recovery, against the real engine and the real model.

    python3 tests/live_reliability_acceptance.py --workdir ~/sarness-live-rel --scenario all

Uses the installed `sarness` binary with an ISOLATED config/state copy (your real sessions are never touched). A thin
fault-injecting proxy sits between the CLI and the real engine and, on a schedule, drops the connection mid-stream,
corrupts an SSE event, rewrites a reply into a truncated `finish_reason=length` tool call, returns HTTP 500, and the
harness sends SIGINT mid-run and resumes the saved session. Everything else (model, tools, files) is real.
"""
import argparse
import http.client
import json
import os
import py_compile
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Proxy:
    def __init__(self, upstream, faults):
        self.up = urllib.parse.urlsplit(upstream)
        self.faults = dict(faults)          # completion number -> fault name
        self.n = 0
        self.log = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *a): pass

            def _forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "POST" else None
                is_c = self.path == "/v1/agent/completions"
                fault = None
                if is_c:
                    with outer.lock:
                        outer.n += 1
                        n = outer.n
                        fault = outer.faults.get(n)
                        if fault == "length":                       # applies to the first reply with a tool call from here on
                            outer.pending_length = True
                        outer.faults.pop(n, None)
                    if fault == "http500":
                        outer.log.append((n, "http500"))
                        self.send_response(500); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", "17")
                        self.end_headers(); self.wfile.write(b'{"detail":"boom"}\n'[:17]); return
                conn = http.client.HTTPConnection(outer.up.hostname, outer.up.port, timeout=1800)
                hdrs = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "content-length", "connection"}}
                conn.request(self.command, self.path, body, hdrs)
                r = conn.getresponse()
                if not is_c:
                    data = r.read()
                    self.send_response(r.status); self.send_header("Content-Type", r.getheader("Content-Type") or "application/json")
                    self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
                self.send_response(r.status); self.send_header("Content-Type", r.getheader("Content-Type") or "text/event-stream")
                self.send_header("Connection", "close"); self.end_headers()
                self.close_connection = True
                lines = 0
                for raw in r:
                    line = raw.decode("utf-8", "replace")
                    lines += 1
                    if fault == "drop" and lines >= 3:
                        outer.log.append((n, "drop")); self.connection.shutdown(2); return
                    if fault == "corrupt" and lines >= 3:
                        outer.log.append((n, "corrupt")); self.wfile.write(b'data: {"type": "done", "message": {"role": "assis'); self.wfile.flush()
                        self.connection.shutdown(2); return
                    if line.startswith("data:") and getattr(outer, "pending_length", False):
                        try:
                            ev = json.loads(line[5:])
                        except ValueError:
                            ev = None
                        calls = ev and ev.get("type") == "done" and (ev.get("message") or {}).get("tool_calls")
                        if calls and (calls[-1]["function"].get("arguments") or "").__len__() > 40:
                            args = calls[-1]["function"]["arguments"]
                            calls[-1]["function"]["arguments"] = args[: int(len(args) * 0.55)]
                            ev["finish_reason"] = "length"
                            outer.pending_length = False
                            outer.log.append((n, f"length(cut {calls[-1]['function']['name']} to {len(calls[-1]['function']['arguments'])}/{len(args)} chars)"))
                            line = "data: " + json.dumps(ev) + "\n"
                    self.wfile.write(line.encode()); self.wfile.flush()

            do_GET = do_POST = _forward

        self.pending_length = False

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, request, client_address):   # clients that hang up on purpose are expected
                pass
        self.srv = Quiet(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.srv.server_address[1]}"


def isolated_env(workdir: Path, proxy_url: str):
    real_cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sarness-cli"
    cfg_root = workdir / "cfg"
    shutil.rmtree(cfg_root, ignore_errors=True)
    shutil.copytree(real_cfg, cfg_root / "sarness-cli")
    for f in (cfg_root / "sarness-cli").glob("*.json"):
        f.chmod(0o600)
    conf = json.loads((cfg_root / "sarness-cli/config.json").read_text())
    upstream = None
    for prof in conf["profiles"].values():
        if prof.get("url"):
            upstream = upstream or prof["url"]
            prof["url"] = proxy_url
    (cfg_root / "sarness-cli/config.json").write_text(json.dumps(conf, indent=2))
    (cfg_root / "sarness-cli/config.json").chmod(0o600)
    env = dict(os.environ, XDG_CONFIG_HOME=str(cfg_root), XDG_STATE_HOME=str(workdir / "state"), NO_COLOR="1", TERM="dumb")
    return env, upstream


def sessions(env):
    d = Path(env["XDG_STATE_HOME"]) / "sarness-cli" / "sessions"
    return sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime) if d.exists() else []


def run_cli(exe, env, ws, args, out, timeout, sigint_after=None, proxy=None):
    cmd = [exe, "-C", str(ws), "run", "--dangerously-skip-permissions", "--verbose", *args]
    with open(out, "ab") as f:
        f.write(f"\n$ {' '.join(cmd[:6])} ...\n".encode())
        p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
        start = time.time()
        sent = False
        while p.poll() is None:
            time.sleep(1)
            if sigint_after and not sent and proxy.n >= sigint_after:
                time.sleep(4)
                p.send_signal(signal.SIGINT); sent = True
            if time.time() - start > timeout:
                p.kill(); raise TimeoutError("CLI run exceeded timeout")
        return p.returncode


def check(label, ok, detail=""):
    ok = bool(ok)
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""), flush=True)
    return ok


def events(env, sid):
    f = Path(env["XDG_STATE_HOME"]) / "sarness-cli" / "sessions" / sid / "events.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def scenario_natural(exe, workdir, upstream_cfg):
    """Real model, real output limit: ask for a file far above what fits in a tiny output budget."""
    print("\n=== scenario: natural truncation (no injection) ===")
    ws = workdir / "ws-natural"; shutil.rmtree(ws, ignore_errors=True); ws.mkdir(parents=True)
    px = Proxy(upstream_cfg, {})
    env, _ = isolated_env(workdir, px.url)
    prompt = ("Create sarness_big.py: valid Python with at least 33000 characters. It must define 300 distinct functions fn_001..fn_300, each with "
              "a docstring and 3 lines of real arithmetic logic, and end with a main block that calls all of them and prints the total. "
              "Write it with write_file (try to do it in as few calls as you can), then run it with python3 and report the printed total.")
    rc = run_cli(exe, env, ws, ["--max-tokens", "2500", "--thinking", "off", "--max-steps", "60", prompt], workdir / "natural.log", 2400)
    sid = sessions(env)[-1].stem
    data = json.loads(sessions(env)[-1].read_text())
    ev = events(env, sid)
    trunc = [e for e in ev if e["event"] == "truncation"]
    f = ws / "sarness_big.py"
    ok = check("exit code 0 / session completed", rc == 0 and data["status"] == "completed", f"rc={rc} status={data['status']}")
    ok &= check("file >= 33000 chars and compiles", f.exists() and f.stat().st_size >= 33000 and bool(py_compile.compile(str(f), doraise=True)), f"{f.stat().st_size if f.exists() else 0} bytes")
    print(f"INFO truncation events: {len(trunc)}  recoveries={data.get('recoveries')}  chunk_limit={data.get('chunk_limit')}")
    return ok


def scenario_injected(exe, workdir, upstream_cfg):
    print("\n=== scenario: multi-file project with injected failures + Ctrl-C + resume ===")
    ws = workdir / "ws-acceptance"; shutil.rmtree(ws, ignore_errors=True); ws.mkdir(parents=True)
    px = Proxy(upstream_cfg, {3: "drop", 5: "corrupt", 7: "length", 9: "http500"})
    env, _ = isolated_env(workdir, px.url)
    prompt = ("Build the Python package `schemra` in the workspace, as a real multi-file project:\n"
              "1. schemra/__init__.py exporting CORE_VERSION = '1.0' and TABLE_ROWS.\n"
              "2. schemra/core.py: AT LEAST 35000 characters of valid Python containing 200 distinct functions rule_001..rule_200, each with a docstring and "
              "3 lines of real logic, plus a function run_all(x) returning the sum of rule_NNN(x). Write this file directly with write_file then append_file chunks "
              "(do NOT use a generator script for it).\n"
              "3. tools/gen_tables.py: a generator script that writes schemra/tables.py (AT LEAST 100000 characters: a python module defining TABLE_ROWS = N "
              "and a dict LOOKUP with many entries). Run the generator with python3.\n"
              "4. tests/test_schemra.py: unittest tests that import schemra.core and schemra.tables and check run_all(1) is an int and len(LOOKUP) > 100.\n"
              "Finally run `python3 -m unittest discover -s tests -v` and fix failures until it passes. Report the file sizes.")
    rc = run_cli(exe, env, ws, ["--max-steps", "120", prompt], workdir / "acceptance.log", 3600, sigint_after=12, proxy=px)
    print(f"INFO first run exit code {rc} (130 = Ctrl-C)")
    sid = sessions(env)[-1].stem
    d1 = json.loads(sessions(env)[-1].read_text())
    ok = check("Ctrl-C checkpointed the session", rc == 130 and d1["status"] == "interrupted", f"rc={rc} status={d1['status']}")
    msgs_before = len(d1["messages"])
    cmd = [exe, "-C", str(ws), "resume", "--dangerously-skip-permissions", "--max-steps", "120", sid, "Continue the task from the checkpoint; do not redo completed work."]
    with open(workdir / "acceptance.log", "ab") as f:
        f.write(b"\n$ resume\n")
        rc2 = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, timeout=3600).returncode
    data = json.loads(sessions(env)[-1].read_text())
    ok &= check("resumed run finished / session completed", rc2 == 0 and data["status"] == "completed", f"rc={rc2} status={data['status']}")
    ok &= check("original instruction kept exactly once", sum(1 for m in data["messages"] if m["role"] == "user" and m["content"].startswith("Build the Python package")) == 1)
    core, tables = ws / "schemra/core.py", ws / "schemra/tables.py"
    ok &= check("core.py >= 35000 chars", core.exists() and core.stat().st_size >= 35000, f"{core.stat().st_size if core.exists() else 0}")
    ok &= check("tables.py >= 100000 chars", tables.exists() and tables.stat().st_size >= 100000, f"{tables.stat().st_size if tables.exists() else 0}")
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ws, capture_output=True, text=True)
    ok &= check("independent unittest run passes", r.returncode == 0, r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "")
    ids = [c["id"] for m in data["messages"] if m["role"] == "assistant" for c in m.get("tool_calls", [])]
    ok &= check("no duplicate tool-call ids", len(ids) == len(set(ids)))
    unknown = sum(1 for m in data["messages"] if m["role"] == "tool" and "status unknown" in m["content"])
    print(f"INFO faults injected: {px.log}  completions served: {px.n}  recoveries={data.get('recoveries')}  unknown-status calls after Ctrl-C: {unknown}")
    ok &= check("proxy really injected drop/corrupt/length/http500", {x[1].split('(')[0] for x in px.log} >= {"drop", "corrupt", "length", "http500"}, str(px.log))
    log = (workdir / "acceptance.log").read_text(errors="replace")
    ok &= check("no approval prompt in bypass", "Approve?" not in log and "Allow?" not in log)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--scenario", choices=["natural", "injected", "all"], default="all")
    ap.add_argument("--exe", default=shutil.which("sarness") or str(Path.home() / ".local/bin/sarness"))
    a = ap.parse_args()
    workdir = Path(a.workdir).expanduser().resolve(); workdir.mkdir(parents=True, exist_ok=True)
    real = json.loads((Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sarness-cli/config.json").read_text())
    upstream = next(p["url"] for p in real["profiles"].values() if p.get("url"))
    print("sarness:", subprocess.run([a.exe, "--version"], capture_output=True, text=True).stdout.strip(), "engine:", upstream)
    ok = True
    if a.scenario in ("natural", "all"):
        ok &= scenario_natural(a.exe, workdir, upstream)
    if a.scenario in ("injected", "all"):
        ok &= scenario_injected(a.exe, workdir, upstream)
    print("\nRESULT:", "ALL PASSED" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
