import os
import signal
import stat
import sys
import textwrap
import time

import pytest

from sarness_cli.connection import Connection
from sarness_cli.engine import Engine
from sarness_cli.tunnel import Tunnel, TunnelError, prune_stale
from fake_engine_server import FakeEngineServer, TOKEN

FAKE_SSH = textwrap.dedent('''\
    #!%(py)s
    """Stand-in for OpenSSH: implements just -f -N -M -S ctl -L and -O check|exit with a real TCP relay."""
    import os, sys, socket, threading, signal
    a = sys.argv[1:]
    ctl = a[a.index("-S") + 1]
    if "-O" in a:
        op = a[a.index("-O") + 1]
        try:
            pid = int(open(ctl).read())
        except OSError:
            sys.exit(255)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            sys.exit(255)
        if op == "exit":
            os.kill(pid, signal.SIGTERM)
            os.unlink(ctl)
        sys.exit(0)
    if "Permission denied" in os.environ.get("FAKE_SSH_FAIL", ""):
        sys.stderr.write("user@host: Permission denied (publickey).\\n"); sys.exit(255)
    lport, host, hport = a[a.index("-L") + 1].split(":")[1:]
    if os.fork():
        for _ in range(50):
            if os.path.exists(ctl): break
            import time; time.sleep(0.05)
        sys.exit(0)
    os.setsid()
    dn = os.open(os.devnull, os.O_RDWR)
    [os.dup2(dn, fd) for fd in (0, 1, 2)]   # like real ssh -f
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", int(lport))); srv.listen(8)
    open(ctl, "w").write(str(os.getpid()))
    def pipe(x, y):
        try:
            while (d := x.recv(65536)): y.sendall(d)
        except OSError: pass
        finally:
            for s in (x, y):
                try: s.shutdown(socket.SHUT_RDWR)
                except OSError: pass
    def handle(c):
        u = socket.create_connection((host, int(hport)))
        threading.Thread(target=pipe, args=(c, u), daemon=True).start(); pipe(u, c)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    while True:
        c, _ = srv.accept(); threading.Thread(target=handle, args=(c,), daemon=True).start()
''')


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    p = bindir / "ssh"
    p.write_text(FAKE_SSH % {"py": sys.executable})
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return p


@pytest.fixture
def engine_server():
    s = FakeEngineServer([])
    yield s
    s.stop()


def test_tunnel_carries_authenticated_engine_traffic_and_cleans_up(fake_ssh, engine_server):
    port = engine_server.srv.server_address[1]
    t = Tunnel("spark", "user@spark.lan", "127.0.0.1", port)
    url = t.open(interactive=False)
    assert url.startswith("http://127.0.0.1:") and t.alive()
    eng = Engine(url, TOKEN)                         # loopback http is allowed only because it is the tunnel's end
    assert eng.health()["service"] == "sarness-engine" and len(eng.models()) == 2
    assert t.open(interactive=False) == url          # idempotent when healthy
    t.close()
    assert not t.alive() and not t.ctl.exists()


def test_connection_reconnects_after_the_tunnel_dies(fake_ssh, engine_server):
    port = engine_server.srv.server_address[1]
    from sarness_cli import config
    config.set_secret("spark", TOKEN)
    prof = {"transport": "ssh", "ssh": {"target": "u@h", "engine_host": "127.0.0.1", "engine_port": port}}
    conn = Connection("spark", prof, interactive=False)
    eng = conn.connect()
    assert eng.health()
    first_url = eng.url
    os.kill(int(conn.tunnel.ctl.read_text()), signal.SIGKILL)       # network blip / ssh killed
    time.sleep(0.2)
    assert not conn.tunnel.alive()
    eng2 = conn.reconnect()
    assert eng2.health() and conn.tunnel.alive()
    conn.close()


def test_failed_ssh_gives_actionable_error_without_password_handling(fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_FAIL", "Permission denied")
    t = Tunnel("x", "u@h", "127.0.0.1", 1)
    with pytest.raises(TunnelError, match="never stores SSH passwords"):
        t.open(interactive=False)


def test_target_injection_is_rejected():
    for bad in ("-oProxyCommand=evil", "a b"):
        with pytest.raises(TunnelError):
            Tunnel("x", bad, "h", 1)


def test_stale_tunnels_from_dead_processes_are_pruned(fake_ssh, tmp_path):
    d = tmp_path / "tun"
    d.mkdir()
    (d / "spark.99999999.sock").write_text("1")
    assert prune_stale(d) == 1 and not list(d.glob("*.sock"))
