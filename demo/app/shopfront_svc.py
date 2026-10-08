"""Shopfront demo service.

One image plays every Shopfront microservice. Its behaviour is driven entirely
by environment variables, so faults can be injected by changing configuration:

SERVICE_NAME        name used in logs and responses
PORT                HTTP port (default 8080)
DEPENDENCIES        comma list of name=url; http(s):// is called on /work,
                    redis:// is checked with a PING
REQUIRED_ENV        comma list of variables that must be set, else exit 1
MEMORY_BALLAST_MB   memory to allocate and hold at startup
STARTUP_DELAY_S     seconds to wait before serving
LOG_LEVEL           DEBUG | INFO | WARNING | ERROR, anything else exits 1
LOG_INJECTION_TEXT  if set, logged at startup and periodically (prompt-injection
                    test data)
SHOPFRONT_BUILD     set by the image build; the "v2-broken" build crashes at start

Endpoints: /healthz (liveness), /readyz (ready only if every dependency
answered on the last check) and /work.

Subcommands for init containers:
  wait-for HOST:PORT [--timeout S]   wait until a TCP port accepts connections
  migrate --target NAME              apply schema migrations up to NAME
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TextIO
from urllib.parse import urlparse

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
CHECK_TIMEOUT_S = 2.0
PAGE = 4096


class ConfigError(Exception):
    """Raised when the environment is invalid; the message is logged as FATAL."""


@dataclass(frozen=True)
class Dependency:
    name: str
    url: str

    @property
    def kind(self) -> str:
        return "redis" if self.url.startswith("redis://") else "http"


@dataclass(frozen=True)
class Config:
    service_name: str
    port: int
    dependencies: tuple[Dependency, ...]
    required_env: tuple[str, ...]
    memory_ballast_mb: int
    startup_delay_s: float
    log_level: str
    log_injection_text: str | None


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_dependencies(value: str) -> tuple[Dependency, ...]:
    """Parse ``name=url,name=url`` into dependencies."""
    deps = []
    for item in _split(value):
        name, sep, url = item.partition("=")
        if not sep or not name or not url:
            raise ConfigError(f"FATAL invalid DEPENDENCIES entry {item!r}")
        deps.append(Dependency(name.strip(), url.strip()))
    return tuple(deps)


def load_config(env: Mapping[str, str]) -> Config:
    """Validate the environment and build the service config."""
    required = tuple(_split(env.get("REQUIRED_ENV", "")))
    for name in required:
        if not env.get(name):
            raise ConfigError(f"FATAL missing required env {name}")
    level = env.get("LOG_LEVEL", "INFO").upper()
    if level not in LEVELS:
        raise ConfigError(f"FATAL invalid LOG_LEVEL {env.get('LOG_LEVEL')!r}")
    try:
        port = int(env.get("PORT", "8080"))
        ballast = int(env.get("MEMORY_BALLAST_MB", "0"))
        delay = float(env.get("STARTUP_DELAY_S", "0"))
    except ValueError as exc:
        raise ConfigError(f"FATAL invalid numeric setting: {exc}") from exc
    return Config(
        service_name=env.get("SERVICE_NAME", "demo-svc"),
        port=port,
        dependencies=parse_dependencies(env.get("DEPENDENCIES", "")),
        required_env=required,
        memory_ballast_mb=max(ballast, 0),
        startup_delay_s=max(delay, 0.0),
        log_level=level,
        log_injection_text=env.get("LOG_INJECTION_TEXT") or None,
    )


class JsonLogger:
    """Minimal structured logger writing one JSON object per line."""

    def __init__(self, service: str, level: str = "INFO", stream: TextIO | None = None) -> None:
        self.service = service
        self.threshold = LEVELS.get(level, 20)
        self.stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()

    def log(self, level: str, msg: str, **fields: Any) -> None:
        if LEVELS.get(level, 50) < self.threshold:
            return
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "level": level,
            "service": self.service,
            "msg": msg,
            **fields,
        }
        with self._lock:
            self.stream.write(json.dumps(record) + "\n")
            self.stream.flush()


def allocate_ballast(megabytes: int) -> bytearray:
    """Allocate and touch ``megabytes`` of memory so it is really resident."""
    ballast = bytearray(megabytes * 1024 * 1024)
    for offset in range(0, len(ballast), PAGE):
        ballast[offset] = 1
    return ballast


def check_redis(url: str, timeout: float = CHECK_TIMEOUT_S) -> None:
    """Send PING to a redis:// URL; raise OSError/RuntimeError on failure."""
    parsed = urlparse(url)
    host, port = parsed.hostname or "localhost", parsed.port or 6379
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(b"PING\r\n")
        reply = sock.recv(64)
    if not reply.startswith(b"+PONG"):
        raise RuntimeError(f"unexpected redis reply {reply!r}")


def check_http(url: str, request_id: str, timeout: float = CHECK_TIMEOUT_S) -> int:
    """Call ``url/work``; return the status code or raise on transport errors."""
    request = urllib.request.Request(  # noqa: S310 - URLs come from trusted config
        url.rstrip("/") + "/work", headers={"X-Request-Id": request_id}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


@dataclass
class Service:
    config: Config
    logger: JsonLogger
    dep_status: dict[str, bool] = field(default_factory=dict)
    checked_once: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check_dependency(self, dep: Dependency) -> bool:
        """Call one dependency, log the outcome and return whether it is healthy."""
        request_id = uuid.uuid4().hex[:16]
        start = time.monotonic()
        try:
            if dep.kind == "redis":
                check_redis(dep.url)
                status = 200
            else:
                status = check_http(dep.url, request_id)
        except (OSError, RuntimeError) as exc:
            latency = round((time.monotonic() - start) * 1000, 1)
            self.logger.log(
                "ERROR",
                f"call to {dep.name} failed",
                request_id=request_id,
                dependency=dep.name,
                url=dep.url,
                latency_ms=latency,
                status=None,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False
        latency = round((time.monotonic() - start) * 1000, 1)
        ok = status < 400
        self.logger.log(
            "INFO" if ok else "WARNING",
            f"call to {dep.name} returned {status}",
            request_id=request_id,
            dependency=dep.name,
            latency_ms=latency,
            status=status,
        )
        return ok

    def check_all(self) -> dict[str, bool]:
        """Check every dependency and remember the result for /readyz."""
        results = {dep.name: self.check_dependency(dep) for dep in self.config.dependencies}
        with self._lock:
            self.dep_status = results
            self.checked_once = True
        return results

    def is_ready(self) -> bool:
        """Ready when there are no dependencies, or all answered on the last check."""
        if not self.config.dependencies:
            return True
        with self._lock:
            return self.checked_once and all(self.dep_status.values())

    def loop(self, stop: threading.Event) -> None:
        """Background loop: check dependencies every 3-5 s."""
        while not stop.is_set():
            self.check_all()
            if self.config.log_injection_text:
                self.logger.log("WARNING", self.config.log_injection_text, kind="user_note")
            stop.wait(random.uniform(3.0, 5.0))  # noqa: S311 - jitter only


def make_handler(service: Service) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to ``service``."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            name = service.config.service_name
            if self.path == "/healthz":
                self._send(200, {"status": "ok", "service": name})
            elif self.path == "/readyz":
                ready = service.is_ready()
                code = 200 if ready else 503
                self._send(code, {"ready": ready, "dependencies": service.dep_status})
            elif self.path == "/work":
                request_id = self.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
                ok = all(service.check_all().values())
                service.logger.log(
                    "INFO" if ok else "ERROR",
                    "handled /work",
                    request_id=request_id,
                    status=200 if ok else 503,
                )
                self._send(200 if ok else 503, {"service": name, "ok": ok})
            else:
                self._send(404, {"error": "not found", "path": self.path})

        def log_message(self, format: str, *args: Any) -> None:
            service.logger.log("DEBUG", "http request", line=format % args)

    return Handler


# Settings read at startup. v2 renamed "rounding" to "rounding_mode" in this table but one
# reader was missed, which is the bug the "v2-broken" build ships with.
PRICING_DEFAULTS: dict[str, dict[str, str]] = {
    "stable": {"currency": "USD", "rounding": "half_even"},
    "v2-broken": {"currency": "USD", "rounding_mode": "half_even"},
}


def load_pricing_rules(build: str) -> dict[str, str]:
    """Pricing settings for this build (raises KeyError in the v2-broken build)."""
    table = PRICING_DEFAULTS.get(build, PRICING_DEFAULTS["stable"])
    return {"currency": table["currency"], "rounding": table["rounding"]}


# Schema migrations in order. 0042 references a column that 0041 never created.
MIGRATIONS: tuple[tuple[str, str | None], ...] = (
    ("0040_create_orders", None),
    ("0041_add_order_status", None),
    ("0042_backfill_order_totals", 'column "total_cents" of relation "orders" does not exist'),
)


def run_migrate(target: str, logger: JsonLogger) -> int:
    """Apply migrations up to ``target``; return 0, or 1 when one fails."""
    names = [name for name, _ in MIGRATIONS]
    if target not in names:
        logger.log("ERROR", "unknown migration target", target=target, known=names)
        return 1
    for name, error in MIGRATIONS[: names.index(target) + 1]:
        logger.log("INFO", "applying migration", migration=name)
        if error:
            logger.log("ERROR", f"migration {name} failed: {error}", migration=name)
            return 1
    logger.log("INFO", "migrations complete", target=target)
    return 0


def run_wait_for(
    address: str, timeout_s: float, logger: JsonLogger, interval_s: float = 2.0
) -> int:
    """Wait until ``host:port`` accepts TCP connections; return 0, or 1 on timeout."""
    host, _, port = address.rpartition(":")
    deadline = time.monotonic() + timeout_s
    last_error = ""
    while True:
        try:
            with socket.create_connection((host, int(port)), timeout=CHECK_TIMEOUT_S):
                logger.log("INFO", f"{address} is available")
                return 0
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            logger.log(
                "ERROR",
                f"timed out waiting for {address} after {timeout_s:.0f}s",
                error=last_error,
            )
            return 1
        logger.log("INFO", f"waiting for {address}", error=last_error)
        time.sleep(interval_s)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Command line: no arguments serves HTTP; subcommands run init-container tasks."""
    parser = argparse.ArgumentParser(prog="shopfront-svc")
    parser.add_argument("--port", type=int, help="override PORT")
    sub = parser.add_subparsers(dest="command")
    wait = sub.add_parser("wait-for", help="wait until HOST:PORT accepts connections")
    wait.add_argument("address", metavar="HOST:PORT")
    wait.add_argument("--timeout", type=float, default=30.0)
    migrate = sub.add_parser("migrate", help="apply schema migrations")
    migrate.add_argument("--target", required=True)
    return parser.parse_args(list(argv))


def main(env: Mapping[str, str] | None = None, argv: Sequence[str] = ()) -> int:
    """Run the service (or a subcommand) and return the process exit code."""
    env = os.environ if env is None else env
    args = parse_args(argv)  # exits 2 with a usage message on unknown flags
    name = env.get("SERVICE_NAME", "demo-svc")
    if args.command == "wait-for":
        return run_wait_for(args.address, args.timeout, JsonLogger(name))
    if args.command == "migrate":
        return run_migrate(args.target, JsonLogger(name))
    try:
        config = load_config(env)
    except ConfigError as exc:
        JsonLogger(name).log("FATAL", str(exc))
        return 1
    if args.port:
        config = replace(config, port=args.port)
    logger = JsonLogger(config.service_name, config.log_level)
    if config.log_injection_text:
        logger.log("WARNING", config.log_injection_text, kind="user_note")
    pricing = load_pricing_rules(env.get("SHOPFRONT_BUILD", "stable"))
    ballast = allocate_ballast(config.memory_ballast_mb) if config.memory_ballast_mb else None
    if ballast is not None:
        logger.log("INFO", "allocated memory ballast", ballast_mb=config.memory_ballast_mb)
    if config.startup_delay_s:
        logger.log("INFO", "startup delay", seconds=config.startup_delay_s)
        time.sleep(config.startup_delay_s)

    service = Service(config, logger)
    server = ThreadingHTTPServer(("0.0.0.0", config.port), make_handler(service))  # noqa: S104
    stop = threading.Event()

    def shutdown(signum: int, _frame: object) -> None:
        logger.log("INFO", "shutting down", signal=signum)
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=service.loop, args=(stop,), daemon=True).start()
    logger.log(
        "INFO",
        "service started",
        port=config.port,
        dependencies=[d.name for d in config.dependencies],
        currency=pricing["currency"],
    )
    server.serve_forever()
    del ballast
    return 0


if __name__ == "__main__":
    sys.exit(main(argv=sys.argv[1:]))
