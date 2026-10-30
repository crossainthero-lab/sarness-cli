"""Sarness Engine client: authenticated HTTP + SSE, strict transport rules, classified errors.

The engine's agent route is stateless inference only (`POST /v1/agent/completions`). Transport
rules: plaintext HTTP is accepted only for loopback (an SSH tunnel endpoint) unless the profile
carries an explicit opt-in; HTTPS verifies certificates (or a pinned SHA-256 fingerprint).
"""
from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
SUPPORTED_API_MAJOR = 1


class EngineError(RuntimeError):
    """kind: auth | http | network | timeout | stream | context | busy | protocol | unsupported"""

    def __init__(self, message: str, kind: str = "http", status: int | None = None, retryable: bool = False, subkind: str | None = None):
        super().__init__(message)
        self.kind, self.status, self.retryable = kind, status, retryable
        self.subkind = subkind or kind


def parse_sse(lines: Any, on_event: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Decode the engine's SSE stream and return the final `done` event.

    Events: delta{content,reasoning}, tool_call{index,name} (progress only), done{message,...},
    error{message}. Raises if the stream ends without `done`, so a half-received turn can
    never be mistaken for a complete one."""
    payload: list[str] = []
    done: dict[str, Any] | None = None

    def accept() -> None:
        nonlocal done
        if not payload:
            return
        text = "\n".join(payload)
        payload.clear()
        try:
            ev = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EngineError(f"Engine sent a malformed stream event: {exc}", "stream", retryable=True) from exc
        kind = ev.get("type")
        if kind == "error":
            msg = str(ev.get("message"))
            raise EngineError("Engine stream error: " + msg, "context" if is_context_error(msg) else "stream",
                              retryable=not is_context_error(msg))
        if on_event:
            on_event(ev)
        if kind == "done":
            done = ev

    for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n") if isinstance(raw, bytes) else raw.rstrip("\r\n")
        if not line:
            accept()
        elif line.startswith("data:"):
            payload.append(line[5:].lstrip(" "))
    accept()
    if done is None:
        raise EngineError("Engine stream ended without a complete 'done' event; no tools were executed",
                          "stream", retryable=True)
    if not isinstance(done.get("message"), dict):
        raise EngineError("Engine returned an invalid final message", "protocol")
    return done


def is_context_error(text: str) -> bool:
    import re
    return bool(re.search(r"context|n_ctx|too many tokens|exceed.*(tokens|size)|prompt.*(long|large)", text, re.I))


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS connection that trusts exactly one certificate fingerprint (like SSH known_hosts)."""
    pin = ""

    def connect(self) -> None:
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        got = hashlib.sha256(der or b"").hexdigest()
        if got != self.pin:
            self.sock.close()
            raise ssl.SSLError(f"certificate fingerprint mismatch: expected {self.pin[:16]}..., got {got[:16]}... "
                               "(refusing to connect; re-run 'sarness setup' only if you trust the new certificate)")


def _normalize_pin(pin: str) -> str:
    return pin.lower().replace(":", "").replace("sha256/", "").strip()


class Engine:
    def __init__(self, url: str, token: str, allow_http_remote: bool = False, *, tls_pin: str | None = None,
                 request_timeout: int = 300):
        self.url = url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Engine URL must be http:// or https://")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Do not place credentials or query strings in engine URL")
        if parsed.scheme == "http" and parsed.hostname not in LOOPBACK and not allow_http_remote:
            raise ValueError("Remote HTTP would expose your token. Use HTTPS or an SSH localhost tunnel, "
                             "or explicitly --allow-insecure-http")
        if not token:
            raise ValueError("No Sarness token. Run 'sarness setup' or set SARNESS_TOKEN")
        self.token = token
        self.scheme = parsed.scheme
        self.tls_pin = _normalize_pin(tls_pin) if tls_pin else None
        self.timeout = request_timeout
        self._opener = self._build_opener()

    def _build_opener(self) -> urllib.request.OpenerDirector:
        if self.scheme != "https":
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never proxy engine traffic
        if not self.tls_pin:
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        pin = self.tls_pin
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # trust is the pinned fingerprint, checked in connect()

        class Conn(_PinnedHTTPS):
            pass
        Conn.pin = pin

        class Handler(urllib.request.HTTPSHandler):
            def https_open(self, req):  # noqa: ANN001
                return self.do_open(lambda host, **kw: Conn(host, context=ctx, **{k: v for k, v in kw.items() if k != "context"}), req)
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), Handler())

    # ------------------------------------------------------------------ transport
    def request(self, method: str, path: str, data: Any = None, *, stream: bool = False, auth: bool = True,
                timeout: float | None = None) -> Any:
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream" if stream else "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url + path, body, method=method, headers=headers)
        try:
            return self._opener.open(req, timeout=timeout or (self.timeout if stream else 30))
        except urllib.error.HTTPError as exc:
            message = exc.read(8000).decode("utf-8", "replace")
            raise classify_http(exc.code, message) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise EngineError(f"Timed out talking to the engine ({self.url})", "timeout", retryable=True) from exc
        except ssl.SSLError as exc:
            raise EngineError(f"TLS verification failed: {exc}", "protocol", subkind="tls") from exc
        except urllib.error.URLError as exc:
            r = getattr(exc, "reason", None)
            if isinstance(r, ssl.SSLError) or "SSL" in str(r):
                raise EngineError(f"TLS verification failed: {r}", "protocol", subkind="tls") from exc
            if isinstance(r, ConnectionRefusedError) or "Connection refused" in str(r):
                raise EngineError(f"Connection refused at {self.url} (engine server is not running or port is closed)", "network", retryable=True, subkind="connection_refused") from exc
            if isinstance(r, socket.gaierror) or "getaddrinfo" in str(r) or "Name or service not known" in str(r):
                raise EngineError(f"DNS lookup failed for {self.url} (host could not be resolved)", "network", retryable=True, subkind="dns") from exc
            if isinstance(r, (socket.timeout, TimeoutError)):
                raise EngineError(f"Connection timed out reaching {self.url}", "timeout", retryable=True) from exc
            raise EngineError(f"Cannot reach the engine at {self.url}: {r or exc}", "network", retryable=True) from exc
        except (ConnectionRefusedError, ConnectionResetError) as exc:
            raise EngineError(f"Connection refused at {self.url} (engine server is not running or port is closed)", "network", retryable=True, subkind="connection_refused") from exc
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            if "Connection refused" in str(exc):
                raise EngineError(f"Connection refused at {self.url} (engine server is not running or port is closed)", "network", retryable=True, subkind="connection_refused") from exc
            raise EngineError(f"Connection to the engine failed: {exc}", "network", retryable=True) from exc

    def get_json(self, path: str, *, auth: bool = True) -> Any:
        try:
            with self.request("GET", path, auth=auth) as res:
                return json.load(res)
        except (json.JSONDecodeError, http.client.HTTPException, OSError) as exc:
            if isinstance(exc, EngineError):
                raise
            raise EngineError(f"Engine returned an unreadable response for {path}: {exc}", "protocol") from exc

    def post_json(self, path: str, data: Any = None, *, auth: bool = True) -> Any:
        try:
            with self.request("POST", path, data if data is not None else {}, auth=auth) as res:
                return json.load(res)
        except (json.JSONDecodeError, http.client.HTTPException, OSError) as exc:
            if isinstance(exc, EngineError):
                raise
            raise EngineError(f"Engine returned an unreadable response for {path}: {exc}", "protocol") from exc

    # ------------------------------------------------------------------ API
    def health(self) -> dict[str, Any]:
        value = self.get_json("/v1/health", auth=False)
        if not isinstance(value, dict) or value.get("service") != "sarness-engine":
            raise EngineError("That server answered, but it is not a Sarness Engine", "protocol")
        major = str(value.get("api_version", "0")).split(".")[0]
        if major != str(SUPPORTED_API_MAJOR):
            raise EngineError(f"Engine API {value.get('api_version')} is not supported (need {SUPPORTED_API_MAJOR}.x)", "unsupported")
        return value

    def system(self) -> dict[str, Any]:
        value = self.get_json("/v1/system")
        return value if isinstance(value, dict) else {}

    def installed_models(self, category: str | None = None) -> list[dict[str, Any]]:
        path = f"/v1/models?category={category}" if category else "/v1/models"
        value = self.get_json(path)
        return value if isinstance(value, list) else []

    def llm_status(self) -> dict[str, Any]:
        value = self.get_json("/v1/llm/status")
        return value if isinstance(value, dict) else {}

    def llm_load(self, model_id: str, ctx: int | None = None, gpu_layers: int | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {"model_id": model_id}
        if ctx is not None:
            data["ctx"] = ctx
        if gpu_layers is not None:
            data["gpu_layers"] = gpu_layers
        return self.post_json("/v1/llm/load", data)

    def llm_unload(self) -> dict[str, Any]:
        return self.post_json("/v1/llm/unload", {})

    def worker_release(self) -> dict[str, Any]:
        return self.post_json("/v1/worker/release", {})

    def worker_stop(self) -> dict[str, Any]:
        return self.post_json("/v1/worker/stop", {})

    def models(self) -> list[dict[str, Any]]:
        """Chat models for the agent, enriched with registry facts (reasoning, loaded) when available."""
        try:
            base = self.get_json("/v1/agent/models")
        except EngineError as exc:
            if exc.status == 404:
                raise EngineError("This engine has no /v1/agent/models; Sarness Engine 0.2.0 or newer is required",
                                  "unsupported", 404) from exc
            raise
        if not isinstance(base, list):
            raise EngineError("Engine returned unexpected models response", "protocol")
        extra: dict[str, dict[str, Any]] = {}
        try:
            for m in self.get_json("/v1/models?category=llm"):
                extra[f"local:{m['id']}"] = m
        except (EngineError, KeyError, TypeError):
            pass
        out = []
        for m in base:
            info = extra.get(m.get("ref"), {})
            caps = info.get("capabilities") or {}
            out.append({**m, "reasoning": caps.get("reasoning") if info else None,
                        "arch": (info.get("meta") or {}).get("architecture"), "tags": info.get("tags") or [],
                        "quant": info.get("quantization"), "supported": info.get("supported", True),
                        "unsupported_reason": info.get("unsupported_reason")})
        return out

    def completion(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                   params: dict[str, Any], on_event: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        data = {"model_ref": model, "messages": messages, "tools": tools, "tool_choice": "auto", "params": params}
        try:
            with self.request("POST", "/v1/agent/completions", data, stream=True) as stream:
                try:
                    return parse_sse(stream, on_event)
                except (socket.timeout, TimeoutError) as exc:
                    raise EngineError("The engine stopped sending data mid-response", "timeout", retryable=True) from exc
                except (ConnectionError, OSError, http.client.HTTPException) as exc:
                    raise EngineError(f"Connection lost mid-response: {exc}", "network", retryable=True) from exc
        except http.client.IncompleteRead as exc:  # pragma: no cover - transport detail
            raise EngineError("Connection lost mid-response", "network", retryable=True) from exc


def classify_http(code: int, message: str) -> EngineError:
    detail = message
    try:
        detail = json.loads(message).get("detail", message)
    except (json.JSONDecodeError, AttributeError):
        pass
    detail = str(detail)[:600]
    if code in (401, 403):
        return EngineError(f"Engine rejected the token (HTTP {code}). Run 'sarness setup' to re-authenticate.", "auth", code)
    if code == 409:
        return EngineError(f"Engine is busy: {detail}", "busy", code, retryable=False)
    if code == 404:
        return EngineError(f"Engine HTTP 404: {detail}", "http", code)
    if is_context_error(detail):
        return EngineError(f"Engine HTTP {code}: {detail}", "context", code)
    if code in (429, 500, 502, 503, 504):
        return EngineError(f"Engine HTTP {code}: {detail}", "http", code, retryable=True, subkind="server_unavailable")
    return EngineError(f"Engine HTTP {code}: {detail}", "http", code)


def explain_engine_error(exc: Exception | None, url: str = "", server: str = "") -> dict[str, str]:
    """Produce a clear, user-friendly diagnostic breakdown for any engine/connection error."""
    if exc is None:
        return {
            "category": "Connected",
            "summary": "Server is reachable and responsive",
            "detail": f"Active connection to {url or 'configured endpoint'}.",
            "suggestion": "No action needed.",
        }

    msg = str(exc)
    kind = getattr(exc, "kind", "unknown")
    subkind = getattr(exc, "subkind", kind)
    status = getattr(exc, "status", None)

    # Specific category detection
    if subkind == "connection_refused" or "Connection refused" in msg or "111" in msg:
        return {
            "category": "Connection Refused",
            "summary": "No process is listening on the server port",
            "detail": f"Connection refused at {url or 'configured endpoint'}. The Sarness server is either not running or listening on a different port/interface.",
            "suggestion": "Verify your Sarness Engine is running on the host, check port forwarding / SSH tunnel, or run /server to check your configuration.",
        }

    if subkind == "dns" or "Name or service not known" in msg or "getaddrinfo" in msg or "DNS lookup" in msg:
        return {
            "category": "DNS Resolution Failure",
            "summary": "Host name could not be resolved",
            "detail": f"Could not resolve the hostname for {url or 'configured endpoint'}.",
            "suggestion": "Check your internet/network connection, DNS resolver settings, or hostname spelling in /server.",
        }

    if subkind == "timeout" or kind == "timeout" or "Timed out" in msg or "timed out" in msg:
        return {
            "category": "Connection Timeout",
            "summary": "The server did not respond within the timeout limit",
            "detail": f"Request to {url or 'the server'} timed out without a response.",
            "suggestion": "Check if the remote host is reachable, ping the host, and verify firewall rules.",
        }

    if subkind == "tls" or "TLS" in msg or "SSL" in msg or "certificate" in msg:
        return {
            "category": "TLS / Certificate Error",
            "summary": "Secure handshake or certificate verification failed",
            "detail": f"TLS verification failed: {msg}",
            "suggestion": "If using a self-signed certificate, ensure the SHA-256 fingerprint is pinned using 'sarness setup --tls-pin <fingerprint>'.",
        }

    if kind == "auth" or status in (401, 403) or "unauthorized" in msg.lower() or "token" in msg.lower():
        return {
            "category": "Authentication Failure",
            "summary": "Server rejected credentials or token",
            "detail": f"The engine rejected access (HTTP {status or '401/403'}): {msg}",
            "suggestion": "Run 'sarness setup' to re-import or update the token, or check the SARNESS_TOKEN environment variable.",
        }

    if kind == "protocol" or "not a Sarness Engine" in msg or "api_version" in msg:
        return {
            "category": "Incompatible Server / Protocol Error",
            "summary": "Endpoint answered but is not a compatible Sarness Engine",
            "detail": f"Protocol incompatibility: {msg}",
            "suggestion": f"Check that the server at {url} is running Sarness Engine v1.x.",
        }

    if kind == "busy" or status == 409 or "busy" in msg.lower():
        return {
            "category": "Server Busy",
            "summary": "Engine is currently busy with another client or model load",
            "detail": f"HTTP 409: {msg}",
            "suggestion": "Wait for the ongoing generation or model load to complete, then retry with /reconnect.",
        }

    if kind == "server_unavailable" or status in (502, 503, 504):
        return {
            "category": "Server Unavailable",
            "summary": f"Server gateway or service error (HTTP {status})",
            "detail": f"Engine reported: {msg}",
            "suggestion": "Check engine daemon logs on the host machine to diagnose service failure.",
        }

    if "tunnel" in kind.lower() or "ssh" in msg.lower():
        return {
            "category": "SSH Tunnel Failure",
            "summary": "Could not establish or maintain the SSH tunnel",
            "detail": f"Tunnel error: {msg}",
            "suggestion": "Verify your SSH key, test 'ssh <target>' in your terminal, and confirm the remote engine port is listening.",
        }

    return {
        "category": "Network Error",
        "summary": "Unable to communicate with the Sarness Engine",
        "detail": msg,
        "suggestion": "Check network connectivity and run /server to inspect or switch configuration.",
    }
