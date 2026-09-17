"""
Reverse Proxy with Load Balancing
==================================

A small, pure-stdlib (``socket`` / ``http.server`` / ``http.client``) reverse
proxy that distributes incoming HTTP requests across multiple backend
instances of the *same* service.

Features
--------
* Two selectable load-balancing strategies: round-robin and least-connections.
* Active health checks: a background thread periodically probes each
  backend's health endpoint and flips a healthy/unhealthy flag.
* Passive failure detection: if a proxied request to a backend raises an
  exception (connection refused, reset, timeout, ...) the backend is marked
  unhealthy immediately and the request is transparently retried against a
  different backend before giving up.
* Faithful proxying of method, headers, and body in both directions.
"""

from __future__ import annotations

import http.client
import http.server
import threading
from typing import List, Optional, Sequence


class Backend:
    """A single backend server instance (host, port) with live state."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.healthy = True
        self.active_connections = 0
        self._lock = threading.Lock()

    @property
    def address(self):
        return (self.host, self.port)

    def inc(self) -> None:
        with self._lock:
            self.active_connections += 1

    def dec(self) -> None:
        with self._lock:
            self.active_connections = max(0, self.active_connections - 1)

    def __repr__(self) -> str:  # pragma: no cover - convenience only
        return (
            f"Backend({self.host}:{self.port}, "
            f"healthy={self.healthy}, active={self.active_connections})"
        )


class RoundRobinBalancer:
    """Cycles through healthy backends in order."""

    def __init__(self):
        self._lock = threading.Lock()
        self._index = 0

    def select(self, backends: Sequence[Backend]) -> Optional[Backend]:
        healthy = [b for b in backends if b.healthy]
        if not healthy:
            return None
        with self._lock:
            backend = healthy[self._index % len(healthy)]
            self._index += 1
        return backend


class LeastConnectionsBalancer:
    """Picks the healthy backend with the fewest in-flight requests."""

    def select(self, backends: Sequence[Backend]) -> Optional[Backend]:
        healthy = [b for b in backends if b.healthy]
        if not healthy:
            return None
        return min(healthy, key=lambda b: b.active_connections)


STRATEGIES = {
    "round_robin": RoundRobinBalancer,
    "least_connections": LeastConnectionsBalancer,
}


class HealthChecker:
    """Periodically (or on demand) probes each backend's health endpoint."""

    def __init__(
        self,
        backends: Sequence[Backend],
        interval: float = 5.0,
        path: str = "/health",
        timeout: float = 1.0,
    ):
        self.backends = backends
        self.interval = interval
        self.path = path
        self.timeout = timeout
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def probe(self, backend: Backend) -> bool:
        """Issue a single health probe against one backend."""
        try:
            conn = http.client.HTTPConnection(
                backend.host, backend.port, timeout=self.timeout
            )
            try:
                conn.request("GET", self.path)
                resp = conn.getresponse()
                resp.read()
                return 200 <= resp.status < 500
            finally:
                conn.close()
        except OSError:
            return False

    def check_once(self) -> None:
        """Probe every backend a single time and update its status."""
        for backend in self.backends:
            backend.healthy = self.probe(backend)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                self.check_once()
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


# Response / request headers that must not be blindly forwarded because the
# proxy (or http.client / http.server) manages them itself.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that forwards every request to a backend pool."""

    protocol_version = "HTTP/1.1"
    server_version = "ReverseProxy/1.0"

    def _read_body(self) -> Optional[bytes]:
        length = self.headers.get("Content-Length")
        if length is None:
            return None
        try:
            return self.rfile.read(int(length))
        except ValueError:
            return None

    def _handle(self) -> None:
        server = self.server  # type: ReverseProxyServer
        body = self._read_body()

        tried: List[Backend] = []
        total_backends = len(server.backends) or 1
        max_attempts = max(1, min(server.max_retries + 1, total_backends))

        for _ in range(max_attempts):
            candidates = [b for b in server.backends if b not in tried]
            backend = server.balancer.select(candidates)
            if backend is None:
                break
            tried.append(backend)
            backend.inc()
            try:
                self._forward(backend, body)
                return
            except Exception:
                # Passive failure detection: this backend just failed a real
                # request, so mark it unhealthy and retry elsewhere.
                backend.healthy = False
            finally:
                backend.dec()

        self._send_bad_gateway()

    def _forward(self, backend: Backend, body: Optional[bytes]) -> None:
        conn = http.client.HTTPConnection(
            backend.host, backend.port, timeout=self.server.backend_timeout
        )
        try:
            headers = {
                k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP
            }
            headers["Connection"] = "close"
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()

        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in _HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Proxied-By", f"{backend.host}:{backend.port}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_bad_gateway(self) -> None:
        payload = b"Bad Gateway: no healthy backend available"
        self.send_response(502)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        # Silence default stderr request logging (kept quiet for tests / CI).
        pass


class ReverseProxyServer(http.server.ThreadingHTTPServer):
    """A ThreadingHTTPServer wired up as a load-balancing reverse proxy."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        backends: Sequence[Backend],
        strategy: str = "round_robin",
        max_retries: int = 2,
        backend_timeout: float = 2.0,
    ):
        super().__init__(server_address, ProxyHandler)
        self.backends: List[Backend] = list(backends)
        self.max_retries = max_retries
        self.backend_timeout = backend_timeout
        if strategy not in STRATEGIES:
            raise ValueError(
                f"Unknown strategy {strategy!r}; choose from {sorted(STRATEGIES)}"
            )
        self.balancer = STRATEGIES[strategy]()

    def serve_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread
