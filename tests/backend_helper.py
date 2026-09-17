"""A small, controllable HTTP backend used only by the test suite.

Each instance can be told (at runtime) to report itself as healthy/unhealthy
on ``/health``, to respond slowly, to hard-fail a request (to exercise
passive failure detection), or to return a specific status/headers/body -
all of which the real test cases flip on and off to exercise the proxy.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class TestBackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        return self.rfile.read(int(length))

    def _handle(self):
        server: TestBackendServer = self.server  # type: ignore[assignment]

        if self.path == "/health":
            body = self._read_body()
            if server.healthy:
                self._send(200, b"OK")
            else:
                self._send(503, b"unhealthy")
            return

        if server.fail:
            # Simulate a hard backend crash: drop the connection with no
            # response at all, so the proxy's request raises an exception.
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return

        body = self._read_body()

        with server.lock:
            server.request_count += 1
            server.last_request = {
                "method": self.command,
                "path": self.path,
                "headers": {k: v for k, v in self.headers.items()},
                "body": body.decode("utf-8", "replace"),
            }

        if server.delay:
            time.sleep(server.delay)

        payload = {
            "backend_port": server.server_port,
            "method": self.command,
            "path": self.path,
            "body": body.decode("utf-8", "replace"),
        }
        response_body = json.dumps(payload).encode()

        self.send_response(server.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("X-Backend-Port", str(server.server_port))
        for key, value in server.extra_headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response_body)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle
    do_HEAD = _handle

    def _send(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass


class TestBackendServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        healthy: bool = True,
        fail: bool = False,
        delay: float = 0.0,
        response_status: int = 200,
        extra_headers: dict | None = None,
    ):
        super().__init__((host, port), TestBackendHandler)
        self.healthy = healthy
        self.fail = fail
        self.delay = delay
        self.response_status = response_status
        self.extra_headers: dict = extra_headers or {}
        self.request_count = 0
        self.last_request = None
        self.lock = threading.Lock()
        self._thread = None

    @property
    def port(self) -> int:
        return self.server_port

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def start_backend(**kwargs) -> TestBackendServer:
    server = TestBackendServer(**kwargs)
    server.start()
    return server
