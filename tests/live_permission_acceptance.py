#!/usr/bin/env python3
"""Opt-in acceptance against the configured REAL engine and installed CLI.

python3 tests/live_permission_acceptance.py --output /tmp/sarness-live-local
No fake engine, model responses, tool implementations or approval responses.
Only normal-mode control receives 'n', after proving an actual approval appears.
Captures raw PTY output, session transcripts and independently verified effects.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import struct
import subprocess
import tempfile
import termios
import time

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
APPROVAL = re.compile(r'Allow\? \[y\]es / \[N\]o / \[a\]lways|Approve\? \[y/N\]|Type \'bypass\' to enable|approval needed but stdin')
PROMPT = 'sarness ❯ '


class Terminal:
    def __init__(self, executable, workspace, log, *args, bypass=True):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 180, 0, 0))
        self.log = open(log, 'wb')
        self.raw = b''
        self.bypass = bypass
        env = dict(os.environ, TERM='dumb', PROMPT_TOOLKIT_NO_CPR='1', NO_COLOR='1')
        self.proc = subprocess.Popen([executable, '-C', str(workspace), '--effort', 'low', '--verbose', *args],
                                     stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        self.until(PROMPT)
        if bypass:
            assert 'BYPASS PERMISSIONS is ON' in self.text

    @property
    def text(self):
        return ANSI.sub('', self.raw.decode('utf-8', 'replace'))

    def until(self, marker, start=0, timeout=420):
        deadline = time.monotonic() + timeout
        while True:
            text = self.text
            if self.bypass and APPROVAL.search(text):
                raise AssertionError('UNEXPECTED TOOL APPROVAL: ' + text[-3000:])
            if marker in text[start:]:
                return text[start:]
            if time.monotonic() > deadline:
                raise TimeoutError(f'Waiting for {marker!r}: {text[-3000:]}')
            if select.select([self.master], [], [], .2)[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    data = b''
                if not data:
                    raise RuntimeError('CLI exited: ' + text[-3000:])
                self.raw += data
                self.log.write(data)
                self.log.flush()

    def task(self, text):
        self.ready()
        start = len(self.text)
        os.write(self.master, (text + '\r').encode())
        output = self.until(PROMPT, start)
        assert '■ stopped:' not in output and '■ Engine error:' not in output, output[-3000:]
        return output

    def ready(self):
        # PromptSession can render its first prompt before switching the PTY
        # out of canonical mode. Sending CR in that window becomes Ctrl-J
        # (insert newline) instead of Enter. Wait for actual terminal readiness.
        deadline = time.monotonic() + 10
        while termios.tcgetattr(self.master)[3] & termios.ICANON:
            if time.monotonic() > deadline:
                raise TimeoutError('CLI did not enter terminal input mode')
            time.sleep(.02)

    def finish(self):
        self.ready()
        start = len(self.text)
        os.write(self.master, b'/exit\r')
        self.until('saved. Resume:', start)
        self.proc.wait(timeout=15)
        assert self.proc.returncode == 0
        sid = re.findall(r'session ([0-9a-f]{20}) saved', self.text)[-1]
        self.close()
        return sid

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=15)
        self.log.close()
        os.close(self.master)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', default=shutil.which('sarness'))
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    assert args.executable and Path(args.executable).exists()
    args.output.mkdir(parents=True, exist_ok=False)
    workspace = Path(tempfile.mkdtemp(prefix='sarness-acceptance-'))
    (workspace / 'existing.txt').write_text('old value\n')
    (workspace / '.git').mkdir()
    report = {'workspace': str(workspace), 'executable': args.executable, 'status': 'running',
              'unexpected_approval_prompts': 0, 'stages': []}
    terminal = None
    try:
        terminal = Terminal(args.executable, workspace, args.output / '01-bypass.raw.log', '--dangerously-skip-permissions')
        terminal.task('Create a small Python calculator project here using coding tools: calc.py with add(a,b) deliberately implemented as a-b, test_calc.py using unittest with a test that expects add(2,3)==5, main.py that prints add(2,3), and README.md. Read existing.txt and edit old value to new value. Run python3 main.py, then python3 -m unittest -v to prove the test fails. Leave this deliberate failure for my next instruction; do not fix it yet.')
        assert all((workspace / p).is_file() for p in ('calc.py', 'test_calc.py', 'main.py', 'README.md'))
        assert (workspace / 'existing.txt').read_text().strip() == 'new value'
        failed = subprocess.run(['python3', '-B', '-m', 'unittest', '-v'], cwd=workspace, capture_output=True, text=True)
        assert failed.returncode != 0 and 'FAIL' in failed.stderr
        report['stages'].append('created project; executed program; demonstrated deliberately failing test')
        terminal.task('Fix the implementation in calc.py using edit_file so the test passes. Execute python3 main.py, run python3 -m unittest -v again, and run python3 -m compileall -q . as a build check. Also use run_command to execute exactly: find . -type f -not -path \'./.git/*\' -not -path \'./.venv/*\'. Run a shell sequence to create scratch and archive directories, write scratch/note.txt and move it to archive/note.txt.')
        assert (workspace / 'archive/note.txt').is_file()
        report['stages'].append('fixed implementation; reran program/tests/build; inventory exclusion; shell sequence and move')
        terminal.task('Create followup.txt containing followup complete. Then run cat followup.txt and python3 -m unittest -v.')
        assert 'followup complete' in (workspace / 'followup.txt').read_text()
        report['stages'].append('additional file in same interactive session')
        sid = terminal.finish()
        terminal = Terminal(args.executable, workspace, args.output / '02-resume.raw.log', '--dangerously-skip-permissions', 'resume', sid)
        terminal.task('Edit README.md to add the line Resume verified. Then run python3 -m unittest -v and cat README.md.')
        assert 'Resume verified' in (workspace / 'README.md').read_text()
        report['stages'].append('fresh process resumed session and edited file')
        child = shlex.quote(args.executable) + ' run --effort low ' + shlex.quote('Create delegated.txt containing delegated complete using write_file, then run cat delegated.txt and python3 -m unittest -v. Execute now.')
        terminal.task('Delegate a coding task by running exactly this nested CLI command with run_command (timeout_s=300), with no extra flags: ' + child + '. After the child exits inspect delegated.txt and run python3 -m unittest -v yourself.')
        assert 'delegated complete' in (workspace / 'delegated.txt').read_text()
        report['stages'].append('model delegated to nested installed CLI without explicit bypass flag')
        terminal.task('/permissions bypass')  # an already enabled mode must not prompt again
        terminal.task('/new')
        terminal.task('/resume ' + sid)
        terminal.task('Append the line Same-process resume verified to followup.txt, then run cat followup.txt and python3 -m unittest -v.')
        assert 'Same-process resume verified' in (workspace / 'followup.txt').read_text()
        report['stages'].append('idempotent permission command; new session; same-process resume; additional edit')
        terminal.finish()
        terminal = None
        for command in (['python3', '-B', 'main.py'], ['python3', '-B', '-m', 'unittest', '-v'], ['python3', '-m', 'compileall', '-q', '.']):
            result = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
        terminal = Terminal(args.executable, workspace, args.output / '03-normal.raw.log', '--mode', 'ask', bypass=False)
        terminal.ready()
        start = len(terminal.text)
        os.write(terminal.master, b'Use write_file to create normal-control.txt containing normal control, now.\r')
        terminal.until('Allow?', start)
        assert not (workspace / 'normal-control.txt').exists()
        os.write(terminal.master, b'n\n')
        terminal.until(PROMPT, len(terminal.text))
        assert not (workspace / 'normal-control.txt').exists()
        terminal.finish()
        terminal = None
        report['stages'].append('normal mode displayed actual approval; declined; file not written')
        report['status'] = 'passed'
        report['session_id'] = sid
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
        if terminal and APPROVAL.search(terminal.text) and terminal.bypass:
            report['unexpected_approval_prompts'] = len(APPROVAL.findall(terminal.text))
        raise
    finally:
        if terminal and terminal.proc.poll() is None:
            terminal.close()
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
