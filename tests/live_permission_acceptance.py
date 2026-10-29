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
    parser.add_argument('--remote-target', default='fylume@192.168.20.193')
    parser.add_argument('--known-hosts', type=Path)
    args = parser.parse_args()
    assert args.executable and Path(args.executable).exists()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    # Copy configuration privately; never mutate existing profiles or session state.
    config_base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    isolated = args.output / 'config' / 'sarness-cli'
    isolated.mkdir(parents=True, mode=0o700)
    for name in ('config.json', 'secrets.json'):
        source = config_base / 'sarness-cli' / name
        if source.exists():
            shutil.copy2(source, isolated / name)
    cfg_path = isolated / 'config.json'
    cfg = json.loads(cfg_path.read_text())
    cfg['settings']['session_dir'] = str(args.output / 'sessions')
    cfg_path.write_text(json.dumps(cfg))
    cfg_path.chmod(0o600)
    os.environ['XDG_CONFIG_HOME'] = str(args.output / 'config')
    os.environ['XDG_STATE_HOME'] = str(args.output / 'state')
    workspace = Path(tempfile.mkdtemp(prefix='sarness-unrestricted-'))
    outside = Path(tempfile.mkdtemp(prefix='sarness-outside-'))
    locked = outside / 'os-denied.txt'
    locked.write_text('synthetic OS-denied fixture')
    locked.chmod(0)
    (workspace / 'outside-link').symlink_to(outside, target_is_directory=True)
    report = {'workspace': str(workspace), 'outside': str(outside), 'executable': args.executable,
              'version': subprocess.check_output([args.executable, '--version'], text=True).strip(),
              'status': 'running', 'unexpected_approval_prompts': 0, 'policy_refusals': 0,
              'unexpected_downgrades': 0, 'stages': []}
    terminal = None
    try:
        terminal = Terminal(args.executable, workspace, args.output / '01-bypass.raw.log', '--dangerously-skip-permissions')
        terminal.task('Use write_file to create ordinary.txt with the exact content alpha, then read_file, edit_file alpha to beta, append_file ! with expected_length 4, move_path to ordinary-moved.txt, read it and run cat ordinary-moved.txt. Execute now.')
        assert (workspace / 'ordinary-moved.txt').read_text() == 'beta!'
        report['stages'].append('interactive file create/read/edit/append/move and shell execution')
        terminal.task('Use write_file to create .ssh/synthetic.txt, .gnupg/synthetic.txt, .env, and .git/synthetic.txt, each containing synthetic-needle only; these are harmless test fixtures, not real secrets or an actual repository. Use read_file on every fixture. Use list_dir, find_files and search to show these paths and synthetic-needle. Run exactly: find . -type f -not -path \'./.git/*\'. Do not omit any tool operation.')
        for name in ('.ssh/synthetic.txt', '.gnupg/synthetic.txt', '.env', '.git/synthetic.txt'):
            assert (workspace / name).read_text().strip() == 'synthetic-needle'
        report['stages'].append('synthetic .ssh/.gnupg/.env/Git internals; file tools and discovery; Git exclusion pattern')
        terminal.task('Use write_file with the absolute path ' + str(outside / 'absolute.txt') + ' and content outside-alpha. Use edit_file there to change alpha to beta. Use read_file and list_dir there, then search for outside-beta in ' + str(outside) + '. Use write_file to create outside-link/through-link.txt containing symlink-ok. Use move_path to move that file to ' + str(outside / 'moved.txt') + '. Use delete_path to delete ' + str(outside / 'absolute.txt') + '. Read ' + str(outside / 'moved.txt') + ' and run cat ' + shlex.quote(str(outside / 'moved.txt')) + '. Do not read os-denied.txt yet.')
        assert not (outside / 'absolute.txt').exists()
        assert (outside / 'moved.txt').read_text().strip() == 'symlink-ok'
        report['stages'].append('outside absolute paths, escaping symlink, read/write/edit/list/search/move/delete')
        destructive = "mkdir -p disposable/tree && printf fixture > disposable/tree/note && rm -rf disposable; mkdir -p delete-tool/tree; printf fixture > delete-tool/tree/note; git init -q disposable-git && cd disposable-git && git -c user.name=Acceptance -c user.email=acceptance@example.invalid commit --allow-empty -qm initial && printf discard > untracked && git reset --hard HEAD && git clean -fd && cd .. && test ! -e disposable && test ! -e disposable-git/untracked"
        terminal.task('Run exactly this disposable destructive/Git command using run_command: ' + destructive + '. Then use delete_path to recursively delete delete-tool. Leave the synthetic credential fixtures intact.')
        assert not (workspace / 'disposable').exists() and not (workspace / 'delete-tool').exists()
        assert not (workspace / 'disposable-git/untracked').exists()
        report['stages'].append('disposable rm -rf, git reset --hard, git clean -fd and recursive delete_path')
        elevated = "sudo -n true >sudo.stdout 2>sudo.stderr; rc=$?; printf '%s\\n' \"$rc\" > sudo.exit; cat sudo.stderr; printf 'sudo exit=%s\\n' \"$rc\""
        terminal.task('First call read_file on ' + str(locked) + ' to demonstrate a genuine OS permission failure. Do not chmod it or elevate to read it. Then execute exactly this harmless noninteractive elevation check with run_command: ' + elevated + '. Read sudo.exit and sudo.stderr. Report the real OS result, then use write_file to create after-os-error.txt containing continued. No privilege configuration changes.')
        assert (workspace / 'after-os-error.txt').read_text().strip() == 'continued'
        report['sudo_exit'] = int((workspace / 'sudo.exit').read_text())
        report['sudo_stderr'] = (workspace / 'sudo.stderr').read_text().strip()
        report['stages'].append('genuine OS-denied file read; harmless sudo -n check; continued execution after failures')
        ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes']
        if args.known_hosts:
            ssh += ['-o', 'UserKnownHostsFile=' + str(args.known_hosts.resolve())]
        remote_command = "d=$(mktemp -d /tmp/sarness-remote-XXXXXX) && mkdir -p \"$d/.ssh\" \"$d/.gnupg\" \"$d/.git\" && printf remote-ok > \"$d/.ssh/synthetic\" && cp \"$d/.ssh/synthetic\" \"$d/.gnupg/synthetic\" && cp \"$d/.ssh/synthetic\" \"$d/.git/synthetic\" && cat \"$d/.gnupg/synthetic\" && rm -rf \"$d\" && printf '\\nremote-complete\\n'"
        remote = shlex.join(ssh + [args.remote_target, remote_command]) + ' >remote.stdout 2>remote.stderr; rc=$?; cat remote.stdout remote.stderr; printf "%s\\n" "$rc" > remote.exit'
        terminal.task('Run this exact SSH command using run_command, without changing it: ' + remote + '. Then read remote.exit and remote.stdout. This writes and removes only remote temporary synthetic fixtures. Also use run_command to execute: curl -fsS --max-time 10 http://192.168.20.193:8742/v1/health > network.json. Report actual results.')
        assert (workspace / 'remote.exit').read_text().strip() == '0', (workspace / 'remote.stderr').read_text()
        assert 'remote-complete' in (workspace / 'remote.stdout').read_text()
        assert json.loads((workspace / 'network.json').read_text())
        report['stages'].append('actual SSH execution combining remote/sensitive/Git/destructive categories; network request')
        sid = terminal.finish()
        terminal = Terminal(args.executable, workspace, args.output / '02-resume.raw.log', 'resume', sid)
        terminal.task('This is a resumed session. Use write_file to create .gnupg/resumed.txt containing resume-ok, read it and run cat .gnupg/resumed.txt.')
        assert (workspace / '.gnupg/resumed.txt').read_text().strip() == 'resume-ok'
        report['stages'].append('fresh installed process resumed without bypass flag; protected file and shell operations')
        child_prompt = 'Use write_file to create .ssh/delegated.txt containing delegated-ok. Read it and run cat .ssh/delegated.txt. Execute now.'
        child = shlex.join([args.executable, '-C', str(outside), '--effort', 'low', 'run', child_prompt])
        terminal.task('Delegate by running this exact nested installed CLI command using run_command with timeout_s=300, without adding a bypass flag: ' + child + '. The child workspace is outside this workspace. After it exits read ' + str(outside / '.ssh/delegated.txt') + ' and run cat ' + str(outside / '.ssh/delegated.txt') + '.')
        assert (outside / '.ssh/delegated.txt').read_text().strip() == 'delegated-ok'
        report['stages'].append('model-driven delegated nested installed CLI, outside workspace, no bypass flag')
        terminal.task('/permissions bypass')
        terminal.task('/new')
        terminal.task('/resume ' + sid)
        terminal.task('Use edit_file to replace resume-ok with resumed-again in .gnupg/resumed.txt. Run cat .gnupg/resumed.txt.')
        assert (workspace / '.gnupg/resumed.txt').read_text().strip() == 'resumed-again'
        terminal.finish()
        terminal = None
        report['stages'].append('idempotent permissions; new session and same-process resume retained bypass')
        sessions = [json.loads(p.read_text()) for p in (args.output / 'sessions').glob('*.json')]
        assert sessions and all(s.get('permissions') == 'bypass' for s in sessions)
        report['bypass_sessions'] = len(sessions)
        tool_results = [m['content'] for s in sessions for m in s['messages'] if m['role'] == 'tool']
        assert any(r.startswith('OS PERMISSION ERROR:') for r in tool_results), 'OS read failure was not exercised'
        refusals = [r for r in tool_results if r.startswith('DENIED:')]
        report['policy_refusals'] = len(refusals)
        assert not refusals, refusals
        events = [json.loads(line) for p in (args.output / 'sessions').glob('*/events.jsonl') for line in p.read_text().splitlines()]
        decisions = [e for e in events if e.get('event') == 'permission']
        assert decisions and all(e['mode'] == 'bypass' and e['verdict'] == 'allow' for e in decisions)
        report['allowed_tool_decisions'] = len(decisions)
        assert any('ssh ' in c['command'] for s in sessions for c in s.get('commands', []))
        assert any('sudo -n true' in c['command'] for s in sessions for c in s.get('commands', []))
        used_tools = {c['function']['name'] for s in sessions for m in s['messages'] for c in m.get('tool_calls', [])}
        required = {'write_file', 'read_file', 'edit_file', 'append_file', 'move_path', 'delete_path', 'list_dir', 'find_files', 'search', 'run_command'}
        assert required <= used_tools, required - used_tools
        report['tools_exercised'] = sorted(used_tools)
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
        report['stages'].append('normal-mode actual approval; declined; file not written')
        report['status'] = 'passed'
        report['session_id'] = sid
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
        if terminal and APPROVAL.search(terminal.text) and terminal.bypass:
            report['unexpected_approval_prompts'] = len(APPROVAL.findall(terminal.text))
        raise
    finally:
        locked.chmod(0o600)
        if terminal and terminal.proc.poll() is None:
            terminal.close()
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
