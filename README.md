# Reverse Proxy

A small, self-contained reverse proxy with load balancing, written in pure
Python standard library (`socket` / `http.server` / `http.client` — no
frameworks). It sits in front of N backend instances of the *same* service
and distributes requests across them, with active health checks and passive
failure detection to keep traffic off broken backends.

This is a load-balancing proxy, not an API gateway: it does not do
path-based routing, authentication, or rate limiting — it focuses purely on
spreading load across a pool of interchangeable backend instances and
routing around failures.

## Architecture

```
                         +-------------------+
   client requests  ---> |   ReverseProxy     |
                         |  (ProxyHandler +    |
                         |   Balancer)         |
                         +---------+-----------+
                                   |
                 +-----------------+-----------------+
                 |                 |                 |
           +-----v----+      +-----v----+      +-----v----+
           | Backend 0|      | Backend 1|      | Backend 2|
           +----------+      +----------+      +----------+
                 ^                 ^                 ^
                 |                 |                 |
           +-----+-----------------+-----------------+-----+
           |         HealthChecker (background thread)      |
           |   periodically GETs /health on every backend    |
           +--------------------------------------------------+
```

* **`Backend`** — tracks a backend's `host`, `port`, `healthy` flag and
  live `active_connections` count.
* **`RoundRobinBalancer`** / **`LeastConnectionsBalancer`** — pick the next
  healthy backend for a request, `select()`-ing from `[b for b in backends
  if b.healthy]`.
* **`HealthChecker`** — a background thread that calls `GET /health` (or a
  path/interval of your choosing) on every backend and flips `Backend.healthy`
  accordingly. Unhealthy backends are excluded from selection until a
  future probe succeeds again (automatic recovery).
* **`ProxyHandler`** — a `BaseHTTPRequestHandler` that, per request:
  1. reads the full request body,
  2. asks the balancer for a backend,
  3. increments that backend's `active_connections`, forwards method,
     headers and body via `http.client`, and streams the response back,
  4. on any exception (connection refused/reset, timeout, ...) marks the
     backend unhealthy **and transparently retries** against a different
     backend (up to `max_retries`) before returning `502 Bad Gateway`,
  5. always decrements `active_connections` when done.

## Load-balancing strategies

* **`round_robin`** — cycles through healthy backends in order, one after
  another. Good default when all backend instances are equally sized and
  requests are roughly uniform in cost.
* **`least_connections`** — routes each request to the healthy backend with
  the fewest requests currently in flight. Better than round-robin when
  request duration varies a lot (a slow backend won't keep collecting new
  requests just because "it's its turn").

Select the strategy with `strategy="round_robin"` /
`strategy="least_connections"` when constructing `ReverseProxyServer`, or
`--strategy` on the CLI.

## Health checking

* **Active**: `HealthChecker` runs on its own thread and, every `interval`
  seconds, issues a `GET` to each backend's `path` (default `/health`).
  Any 2xx/3xx/4xx response counts as healthy (the backend process is up and
  answering); connection errors or 5xx mark it unhealthy. You can also call
  `checker.check_once()` for an on-demand sweep (handy in tests), instead of
  waiting on the timer.
* **Passive**: if a *proxied* request to a backend raises an exception, the
  proxy immediately marks that backend unhealthy and retries the request
  against a different healthy backend (bounded by `max_retries`), so a
  single broken backend never turns into a failed client request as long as
  another instance is up.
* **Recovery**: an unhealthy backend is simply excluded from
  `select()`'s candidate list. As soon as a subsequent active health check
  succeeds (or you flip it back and call `check_once()`), it is
  automatically re-included in the rotation — no restart needed.

## Running it

Start a couple of backend instances (a tiny demo backend is included):

```bash
python examples/demo_backend.py 9001
python examples/demo_backend.py 9002   # in another terminal
```

Start the proxy in front of them:

```bash
python -m reverseproxy \
  --port 8080 \
  --backends 127.0.0.1:9001,127.0.0.1:9002 \
  --strategy round_robin \
  --health-interval 5
```

Then, from a third terminal:

```bash
curl -i http://127.0.0.1:8080/
curl -i http://127.0.0.1:8080/
# alternates between the two backends with round-robin

curl -X POST -H 'Content-Type: application/json' -d '{"hi":"there"}' \
  http://127.0.0.1:8080/anything
```

Use it as a library instead:

```python
from reverseproxy.proxy import Backend, ReverseProxyServer, HealthChecker

backends = [Backend("127.0.0.1", 9001), Backend("127.0.0.1", 9002)]
proxy = ReverseProxyServer(("0.0.0.0", 8080), backends, strategy="least_connections")
checker = HealthChecker(backends, interval=5, path="/health")
checker.start()
proxy.serve_forever()
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite (12 tests, `tests/test_proxy.py`) starts real backend HTTP servers
and a real proxy on ephemeral ports for every test and talks to them over
real HTTP, covering:

* round-robin distributes requests evenly, and in a fixed cyclic order
* least-connections avoids a backend that's busy with an in-flight request
* an active health check marks a failing backend down and excludes it
* a recovered backend is re-included once health checks pass again
* the background `HealthChecker` thread detects down/up transitions on its own
* passive failure detection retries a failed request against another backend
* all backends down yields `502 Bad Gateway`
* request method, headers and body are proxied to the backend intact
* response status, headers and body are returned to the client intact
* GET requests with no body are handled
* per-backend `active_connections` returns to zero after requests finish

CI runs the same suite on Python 3.11 and 3.12 via
`.github/workflows/tests.yml`.

## Project layout

```
reverseproxy/
  proxy.py       # Backend, balancers, HealthChecker, ProxyHandler, ReverseProxyServer
  __main__.py    # CLI: python -m reverseproxy ...
examples/
  demo_backend.py  # minimal backend for manual testing
tests/
  backend_helper.py  # controllable test backend (health/fail/delay/status)
  test_proxy.py       # the test suite
```
