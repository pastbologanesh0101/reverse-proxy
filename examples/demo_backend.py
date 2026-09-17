"""A tiny demo backend server, useful for manually trying out the proxy.

Usage:
    python examples/demo_backend.py 9001
    python examples/demo_backend.py 9002

Then, in another terminal:
    python -m reverseproxy --port 8080 --backends 127.0.0.1:9001,127.0.0.1:9002

And:
    curl http://127.0.0.1:8080/
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DemoHandler(BaseHTTPRequestHandler):
    def _respond(self):
        if self.path == "/health":
            body = b"OK"
        else:
            body = f"Hello from backend on port {self.server.server_port}\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9001
    server = ThreadingHTTPServer(("127.0.0.1", port), DemoHandler)
    print(f"Demo backend listening on 127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
