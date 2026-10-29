"""A server profile turned into a live, self-healing Engine connection."""
from __future__ import annotations

from typing import Any

from .config import ConfigError, get_secret, load_config
from .engine import Engine, EngineError
from .tunnel import Tunnel, TunnelError


def get_profile(name: str | None) -> tuple[str, dict[str, Any]]:
    cfg = load_config()
    name = name or cfg["settings"].get("server")
    profiles = cfg["profiles"]
    if not profiles:
        raise ConfigError("No Sarness Engine is configured yet. Run: sarness setup")
    if not name:
        if len(profiles) == 1:
            name = next(iter(profiles))
        else:
            raise ConfigError("Several servers are configured; pick one with --server NAME or 'sarness server use NAME'")
    if name not in profiles:
        raise ConfigError(f"Unknown server '{name}'. Configured: {', '.join(sorted(profiles))}")
    return name, profiles[name]


def find_profile(name: str | None = None) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Safe lookup that returns (name, profile, error_msg) without raising."""
    cfg = load_config()
    name = name or cfg["settings"].get("server")
    profiles = cfg.get("profiles", {})
    if not profiles:
        return None, None, "No Sarness Engine is configured yet. Run 'sarness setup' or use /server."
    if not name:
        if len(profiles) == 1:
            name = next(iter(profiles))
        else:
            return None, None, f"Several servers configured: {', '.join(sorted(profiles))}. Select one with /server."
    if name not in profiles:
        return name, None, f"Unknown server '{name}'. Configured: {', '.join(sorted(profiles))}"
    return name, profiles[name], None


class Connection:
    """Owns the (optional) tunnel and rebuilds the Engine when the tunnel is replaced."""

    def __init__(self, name: str, profile: dict[str, Any], *, token: str | None = None, request_timeout: int = 300,
                 allow_insecure_http: bool = False, interactive: bool | None = None, require_token: bool = True):
        self.name, self.profile = name, profile
        self.token = token or get_secret(name) or profile.get("legacy_token")
        if not self.token and require_token:
            raise ConfigError(f"No token stored for server '{name}'. Run: sarness setup --server {name}")
        self.timeout, self.interactive = request_timeout, interactive
        self.allow_http = allow_insecure_http or bool(profile.get("insecure_http"))
        self.tunnel: Tunnel | None = None
        self._engine: Engine | None = None
        transport = profile.get("transport", "direct")
        if transport == "ssh":
            ssh = profile["ssh"]
            self.tunnel = Tunnel(name, ssh["target"], ssh.get("engine_host", "127.0.0.1"), ssh.get("engine_port", 8742),
                                 ssh.get("args"))

    def probe_health(self, timeout: float = 2.0) -> tuple[bool, str | None, dict[str, Any] | None]:
        if not self.token:
            return False, f"No token stored for server '{self.name}'.", None
        try:
            eng = self.connect()
            old_timeout = eng.timeout
            eng.timeout = timeout
            try:
                res = eng.health()
                return True, None, res
            finally:
                eng.timeout = old_timeout
        except Exception as exc:
            return False, str(exc), None

    @property
    def description(self) -> str:
        p = self.profile
        if self.tunnel:
            return f"{self.name}: SSH tunnel via {self.tunnel.target} -> {self.tunnel.engine_host}:{self.tunnel.engine_port}"
        tls = " (pinned certificate)" if p.get("tls_pin") else ""
        return f"{self.name}: {p.get('url')}{tls}"

    def connect(self) -> Engine:
        try:
            url = self.tunnel.open(interactive=self.interactive) if self.tunnel else self.profile["url"]
        except TunnelError as exc:
            raise EngineError(str(exc), "network") from exc
        if self._engine is None or self._engine.url != url.rstrip("/"):
            self._engine = Engine(url, self.token, self.allow_http, tls_pin=self.profile.get("tls_pin"),
                                  request_timeout=self.timeout)
        return self._engine

    @property
    def engine(self) -> Engine:
        return self._engine or self.connect()

    def reconnect(self) -> Engine:
        """Called after a network error: re-establish the tunnel if it died."""
        return self.connect()

    def close(self) -> None:
        if self.tunnel:
            self.tunnel.close()


class LiveEngine:
    """The object the agent loop talks to: completion + reconnect + eviction guard."""

    def __init__(self, conn: Connection, replaces: str | None = None):
        self.conn = conn
        self.replaces = replaces     # a loaded model the user already agreed to unload

    def completion(self, *a: Any, **kw: Any) -> dict[str, Any]:
        return self.conn.engine.completion(*a, **kw)

    def llm_status(self) -> dict[str, Any]:
        return self.conn.engine.llm_status()

    def system(self) -> dict[str, Any]:
        return self.conn.engine.system()

    def installed_models(self, category: str | None = None) -> list[dict[str, Any]]:
        return self.conn.engine.installed_models(category)

    def llm_load(self, model_id: str, ctx: int | None = None, gpu_layers: int | None = None) -> dict[str, Any]:
        return self.conn.engine.llm_load(model_id, ctx, gpu_layers)

    def llm_unload(self) -> dict[str, Any]:
        return self.conn.engine.llm_unload()

    def worker_release(self) -> dict[str, Any]:
        return self.conn.engine.worker_release()

    def worker_stop(self) -> dict[str, Any]:
        return self.conn.engine.worker_stop()

    def reconnect(self) -> None:
        self.conn.reconnect()

    def preflight(self, cfg: Any) -> str | None:
        """Called before every turn. Returns a warning if sending this request would evict a model that
        someone else has loaded since we started; re-syncs our pinned ctx if only that changed."""
        if not cfg.caps.local:
            return None
        try:
            st = self.conn.engine.llm_status()
        except EngineError:
            return None  # the completion call will report connectivity problems properly
        mine = cfg.model.split(":", 1)[1]
        if st.get("state") == "ready" and st.get("model_id") not in (None, mine, self.replaces):
            return (f"The engine now has {st.get('model_name') or st.get('model_id')} loaded (another client switched it). "
                    f"Continuing with {cfg.model} will unload that model.")
        if st.get("state") == "ready" and st.get("model_id") == mine and st.get("ctx"):
            cfg.ctx, cfg.gpu_layers = int(st["ctx"]), st.get("gpu_layers")
            self.replaces = None     # our model is in; from now on any other loaded model is somebody else's
        return None
