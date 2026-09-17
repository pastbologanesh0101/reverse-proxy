"""Unit tests for the reverse proxy.

Every test spins up real backend HTTP servers (via ``tests.backend_helper``)
and a real ``ReverseProxyServer`` on ephemeral ports, then exercises them
with genuine HTTP requests over the loopback interface. All servers started
by a test are stopped in ``tearDown``.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from reverseproxy.proxy import Backend, HealthChecker, ReverseProxyServer

from tests.backend_helper import start_backend


class ProxyTestCase(unittest.TestCase):
    def setUp(self):
        self._backend_servers = []
        self._proxy_servers = []

    def tearDown(self):
        for checker in getattr(self, "_checkers", []):
            checker.stop()
        for proxy in self._proxy_servers:
            proxy.shutdown()
            proxy.server_close()
        for backend in self._backend_servers:
            backend.stop()

    # -- helpers ----------------------------------------------------------

    def make_backend(self, **kwargs):
        server = start_backend(**kwargs)
        self._backend_servers.append(server)
        return server

    def make_proxy(self, backend_servers, strategy="round_robin", **kwargs):
        backends = [Backend("127.0.0.1", s.port) for s in backend_servers]
        proxy = ReverseProxyServer(
            ("127.0.0.1", 0), backends, strategy=strategy, **kwargs
        )
        proxy.serve_in_background()
        self._proxy_servers.append(proxy)
        return proxy, backends

    def make_checker(self, backends, **kwargs):
        checker = HealthChecker(backends, **kwargs)
        self._checkers = getattr(self, "_checkers", [])
        self._checkers.append(checker)
        return checker

    def proxy_url(self, proxy, path="/"):
        return f"http://127.0.0.1:{proxy.server_port}{path}"

    def get(self, proxy, path="/", **kwargs):
        req = urllib.request.Request(self.proxy_url(proxy, path), **kwargs)
        return urllib.request.urlopen(req, timeout=5)

    # -- tests --------------------------------------------------------

    def test_round_robin_distributes_evenly(self):
        backends = [self.make_backend() for _ in range(3)]
        proxy, _ = self.make_proxy(backends, strategy="round_robin")

        for _ in range(30):
            self.get(proxy).read()

        counts = [b.request_count for b in backends]
        self.assertEqual(sum(counts), 30)
        for count in counts:
            self.assertEqual(count, 10)

    def test_round_robin_cycles_in_fixed_order(self):
        backends = [self.make_backend() for _ in range(3)]
        proxy, _ = self.make_proxy(backends, strategy="round_robin")

        seen_ports = []
        for _ in range(6):
            resp = self.get(proxy)
            seen_ports.append(int(resp.headers["X-Backend-Port"]))

        expected_cycle = [b.port for b in backends]
        self.assertEqual(seen_ports, expected_cycle * 2)

    def test_least_connections_avoids_busy_backend(self):
        busy = self.make_backend(delay=0.5)
        idle = self.make_backend()
        proxy, _ = self.make_proxy([busy, idle], strategy="least_connections")

        # Kick off a slow request in the background; least-connections will
        # pick `busy` first (both start at 0 in-flight, tie -> first in list)
        # and its active_connections count will be 1 while it's in flight.
        slow_thread = threading.Thread(target=lambda: self.get(proxy).read())
        slow_thread.start()
        time.sleep(0.1)  # let the slow request register as in-flight

        resp = self.get(proxy)
        second_port = int(resp.headers["X-Backend-Port"])

        slow_thread.join(timeout=5)

        self.assertEqual(second_port, idle.port)

    def test_health_check_marks_backend_down_and_excludes_it(self):
        good = self.make_backend()
        bad = self.make_backend()
        bad.healthy = False  # backend itself reports unhealthy on /health

        backends_objs = [Backend("127.0.0.1", good.port), Backend("127.0.0.1", bad.port)]
        checker = HealthChecker(backends_objs, path="/health")
        self._checkers = getattr(self, "_checkers", [])
        self._checkers.append(checker)
        checker.check_once()

        self.assertTrue(backends_objs[0].healthy)
        self.assertFalse(backends_objs[1].healthy)

        proxy = ReverseProxyServer(("127.0.0.1", 0), backends_objs, strategy="round_robin")
        proxy.serve_in_background()
        self._proxy_servers.append(proxy)

        for _ in range(5):
            resp = self.get(proxy)
            self.assertEqual(int(resp.headers["X-Backend-Port"]), good.port)

        self.assertEqual(bad.request_count, 0)

    def test_recovered_backend_is_re_included_after_health_checks_pass(self):
        backend = self.make_backend()
        backend.healthy = False

        b = Backend("127.0.0.1", backend.port)
        checker = self.make_checker([b], path="/health")
        checker.check_once()
        self.assertFalse(b.healthy)

        # Backend comes back up.
        backend.healthy = True
        checker.check_once()
        self.assertTrue(b.healthy)

        proxy = ReverseProxyServer(("127.0.0.1", 0), [b], strategy="round_robin")
        proxy.serve_in_background()
        self._proxy_servers.append(proxy)

        resp = self.get(proxy)
        self.assertEqual(resp.status, 200)
        self.assertEqual(backend.request_count, 1)

    def test_background_health_checker_thread_detects_recovery(self):
        backend = self.make_backend()
        backend.healthy = False
        b = Backend("127.0.0.1", backend.port)
        checker = self.make_checker([b], path="/health", interval=0.1)
        checker.start()

        time.sleep(0.3)
        self.assertFalse(b.healthy)

        backend.healthy = True
        time.sleep(0.3)
        self.assertTrue(b.healthy)

    def test_passive_failure_detection_retries_other_backend(self):
        failing = self.make_backend()
        failing.fail = True
        working = self.make_backend()

        backends_objs = [
            Backend("127.0.0.1", failing.port),
            Backend("127.0.0.1", working.port),
        ]
        proxy = ReverseProxyServer(
            ("127.0.0.1", 0), backends_objs, strategy="round_robin", max_retries=2
        )
        proxy.serve_in_background()
        self._proxy_servers.append(proxy)

        resp = self.get(proxy)
        self.assertEqual(resp.status, 200)
        self.assertEqual(int(resp.headers["X-Backend-Port"]), working.port)

        # The failing backend should now be marked unhealthy (passive check).
        self.assertFalse(backends_objs[0].healthy)

    def test_all_backends_down_returns_502(self):
        failing_a = self.make_backend()
        failing_a.fail = True
        failing_b = self.make_backend()
        failing_b.fail = True

        _, backends_objs = self.make_proxy(
            [failing_a, failing_b], strategy="round_robin", max_retries=2
        )
        proxy = self._proxy_servers[-1]

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get(proxy)
        self.assertEqual(ctx.exception.code, 502)

    def test_method_headers_and_body_are_proxied_correctly(self):
        backend = self.make_backend()
        proxy, _ = self.make_proxy([backend], strategy="round_robin")

        payload = json.dumps({"hello": "world"}).encode()
        req = urllib.request.Request(
            self.proxy_url(proxy, "/widgets"),
            data=payload,
            method="POST",
            headers={"X-Custom-Header": "abc123", "Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        body = json.loads(resp.read())

        self.assertEqual(body["method"], "POST")
        self.assertEqual(body["path"], "/widgets")
        self.assertEqual(json.loads(body["body"]), {"hello": "world"})

        seen = backend.last_request
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/widgets")
        self.assertEqual(seen["headers"].get("X-Custom-Header"), "abc123")
        self.assertEqual(json.loads(seen["body"]), {"hello": "world"})

    def test_response_status_body_and_headers_returned_to_client(self):
        backend = self.make_backend()
        backend.response_status = 201
        backend.extra_headers = {"X-Widget-Id": "42"}
        proxy, _ = self.make_proxy([backend], strategy="round_robin")

        resp = self.get(proxy, "/create")
        self.assertEqual(resp.status, 201)
        self.assertEqual(resp.headers.get("X-Widget-Id"), "42")
        body = json.loads(resp.read())
        self.assertEqual(body["path"], "/create")

    def test_get_request_with_no_body_is_proxied(self):
        backend = self.make_backend()
        proxy, _ = self.make_proxy([backend], strategy="round_robin")

        resp = self.get(proxy, "/status")
        body = json.loads(resp.read())
        self.assertEqual(body["method"], "GET")
        self.assertEqual(body["body"], "")

    def test_active_connections_return_to_zero_after_requests_complete(self):
        backend = self.make_backend()
        proxy, backend_objs = self.make_proxy([backend], strategy="least_connections")

        for _ in range(5):
            self.get(proxy).read()

        self.assertEqual(backend_objs[0].active_connections, 0)
        self.assertEqual(backend.request_count, 5)


if __name__ == "__main__":
    unittest.main()
