"""Command-line entry point: python -m reverseproxy ..."""

from __future__ import annotations

import argparse
import sys

from .proxy import Backend, HealthChecker, ReverseProxyServer


def parse_backend(spec: str) -> Backend:
    host, _, port = spec.rpartition(":")
    if not host:
        host = "127.0.0.1"
    return Backend(host, int(port))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="reverseproxy",
        description="Run a load-balancing reverse proxy in front of N backends.",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Address the proxy listens on."
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port the proxy listens on."
    )
    parser.add_argument(
        "--backends",
        required=True,
        help="Comma-separated host:port list, e.g. 127.0.0.1:9001,127.0.0.1:9002",
    )
    parser.add_argument(
        "--strategy",
        choices=["round_robin", "least_connections"],
        default="round_robin",
    )
    parser.add_argument(
        "--health-path", default="/health", help="Path used for active health checks."
    )
    parser.add_argument(
        "--health-interval",
        type=float,
        default=5.0,
        help="Seconds between active health check sweeps.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max backends tried per request (passive failover).",
    )
    args = parser.parse_args(argv)

    backends = [parse_backend(spec) for spec in args.backends.split(",")]
    server = ReverseProxyServer(
        (args.host, args.port),
        backends,
        strategy=args.strategy,
        max_retries=args.max_retries,
    )
    checker = HealthChecker(
        backends, interval=args.health_interval, path=args.health_path
    )
    checker.start()

    print(f"Reverse proxy listening on {args.host}:{args.port}")
    print(f"Strategy: {args.strategy}")
    for b in backends:
        print(f"  backend -> {b.host}:{b.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        checker.stop()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
