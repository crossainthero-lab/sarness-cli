import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sarness_cli.engine import Engine, EngineError, classify_http, parse_sse
from helpers import call, result


def sse(*events):
    return [("data: " + json.dumps(e) + "\n").encode() if i % 2 == 0 else b"\n" for e in events for i in (0, 1)]


def test_sse_stream_assembled_with_reasoning_and_progress():
    seen = []
    done = parse_sse(sse({"type": "delta", "content": "", "reasoning": "hmm"}, {"type": "tool_call", "index": 0, "name": "write_file"},
                         {"type": "delta", "content": "hi", "reasoning": ""}, result(content="hi")), seen.append)
    assert [e["type"] for e in seen] == ["delta", "tool_call", "delta", "done"]
    assert done["message"]["content"] == "hi"


def test_incomplete_stream_raises_and_is_retryable():
    with pytest.raises(EngineError, match="without a complete") as ei:
        parse_sse(sse({"type": "delta", "content": "hi"}))
    assert ei.value.retryable


def test_error_event_context_is_classified():
    with pytest.raises(EngineError) as ei:
        parse_sse(sse({"type": "error", "message": "request exceeds the available context size"}))
    assert ei.value.kind == "context" and not ei.value.retryable


def test_malformed_event_is_a_stream_error():
    with pytest.raises(EngineError, match="malformed"):
        parse_sse([b"data: {not json\n", b"\n"])


def test_remote_http_rejected_loopback_and_https_allowed():
    with pytest.raises(ValueError, match="Remote HTTP"):
        Engine("http://192.168.1.3:8742", "token")
    Engine("http://127.0.0.1:8742", "token")
    Engine("https://server.example:8742", "token")
    with pytest.raises(ValueError):
        Engine("http://user:pw@127.0.0.1:1", "t")
    with pytest.raises(ValueError, match="No Sarness token"):
        Engine("http://127.0.0.1:1", "")


def test_http_error_classification():
    assert classify_http(401, "x").kind == "auth"
    assert classify_http(409, '{"detail":"GPU busy"}').kind == "busy"
    assert classify_http(502, "bad").retryable
    assert classify_http(400, "prompt exceeds n_ctx").kind == "context"


class Handler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *a):
        pass

    def _auth(self):
        if self.headers["Authorization"] != "Bearer secret-token":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"bad token"}')
            return False
        return True

    def do_GET(self):
        if self.path == "/v1/health":
            body = {"status": "ok", "service": "sarness-engine", "api_version": "1.1", "engine_version": "0.4.0"}
        else:
            if not self._auth():
                return
            body = [{"ref": "local:test", "name": "Qwen Test", "tools": None, "local": True}] if "agent" in self.path else \
                   [{"id": "test", "capabilities": {"reasoning": True}, "meta": {"architecture": "qwen"}, "tags": []}]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        if not self._auth():
            return
        Handler.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for item in [{"type": "delta", "content": "reading ", "reasoning": ""}, result([call("read_file", {"path": "a.txt"})])]:
            self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
            self.wfile.flush()


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_authenticated_real_sse_round_trip(server):
    eng = Engine(f"http://127.0.0.1:{server.server_address[1]}", "secret-token")
    assert eng.health()["engine_version"] == "0.4.0"
    m = eng.models()[0]
    assert m["ref"] == "local:test" and m["reasoning"] is True and m["arch"] == "qwen"
    trace = []
    out = eng.completion("local:test", [{"role": "user", "content": "hi"}], [], {"max_tokens": 4096, "thinking": False, "ctx": 8192}, trace.append)
    assert [t["content"] for t in trace if t["type"] == "delta"] == ["reading "]
    assert out["message"]["tool_calls"][0]["function"]["name"] == "read_file"
    sent = Handler.requests[-1]
    assert sent["params"] == {"max_tokens": 4096, "thinking": False, "ctx": 8192} and sent["model_ref"] == "local:test"


def test_wrong_token_is_auth_error(server):
    eng = Engine(f"http://127.0.0.1:{server.server_address[1]}", "wrong")
    with pytest.raises(EngineError) as ei:
        eng.models()
    assert ei.value.kind == "auth" and "wrong" not in str(ei.value)   # and the token is never echoed back


def test_connection_refused_is_retryable_network_error():
    eng = Engine("http://127.0.0.1:9", "t")
    with pytest.raises(EngineError) as ei:
        eng.health()
    assert ei.value.kind == "network" and ei.value.retryable


def test_tls_pin_accepts_matching_and_rejects_other_certificate(tmp_path):
    cryptography = pytest.importorskip("cryptography")
    import datetime
    import hashlib
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "engine.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]), False)
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    fp = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"https://127.0.0.1:{srv.server_address[1]}"
        assert Engine(url, "secret-token", tls_pin=fp).health()["service"] == "sarness-engine"
        with pytest.raises(EngineError, match="TLS|fingerprint|certificate"):
            Engine(url, "secret-token", tls_pin="00" * 32).health()
        with pytest.raises(EngineError, match="TLS|certificate"):  # no pin -> normal CA verification must fail for a self-signed cert
            Engine(url, "secret-token").health()
    finally:
        srv.shutdown()
        srv.server_close()
