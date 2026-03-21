"""Health and readiness probe endpoints for Kubernetes lifecycle management.

Provides a lightweight HTTP server with ``/healthz`` (liveness) and ``/readyz``
(readiness) endpoints.  Agents register readiness checks; all must pass for the
readiness probe to return 200.

Usage::

    from ai_agents_core.health import HealthServer

    health = HealthServer()
    health.register_check("kafka", lambda: kafka_admin_client is not None)
    health.start(port=8080)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger("ai_agents.health")

_server_started = False
_server_lock = threading.Lock()


class HealthServer:
    """Lightweight health/readiness probe server.

    Args:
        default_port: Fallback port if ``HEALTH_PORT`` env var is not set.
    """

    def __init__(self, default_port: int = 8080) -> None:
        self._default_port = default_port
        self._checks: dict[str, Callable[[], bool]] = {}

    def register_check(self, name: str, check: Callable[[], bool]) -> None:
        """Register a named readiness check.

        Args:
            name: Human-readable name for the check (e.g. "kafka", "database").
            check: Callable returning True if the dependency is ready.
        """
        self._checks[name] = check

    def _run_checks(self) -> tuple[bool, dict[str, bool]]:
        results: dict[str, bool] = {}
        for name, check in self._checks.items():
            try:
                results[name] = check()
            except Exception:
                results[name] = False
        all_ok = all(results.values()) if results else True
        return all_ok, results

    def start(self, port: int | None = None) -> None:
        """Start the health probe server in a daemon thread.

        Safe to call multiple times — only the first call starts the server.

        Args:
            port: TCP port to listen on.  Defaults to the ``HEALTH_PORT``
                environment variable, or the ``default_port`` passed to the
                constructor.
        """
        global _server_started  # noqa: PLW0603
        with _server_lock:
            if _server_started:
                return
            resolved_port = (
                port if port is not None else int(os.getenv("HEALTH_PORT", str(self._default_port)))
            )
            health_server = self

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802
                    if self.path == "/healthz":
                        self._respond(200, {"status": "ok"})
                    elif self.path == "/readyz":
                        ok, details = health_server._run_checks()
                        status = 200 if ok else 503
                        self._respond(status, {"status": "ready" if ok else "not_ready", **details})
                    else:
                        self._respond(404, {"error": "not found"})

                def _respond(self, code: int, body: dict) -> None:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(body).encode())

                def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                    pass  # suppress default stderr logging

            server = HTTPServer(("0.0.0.0", resolved_port), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            _server_started = True
            logger.info("Health probe server started on port %d", resolved_port)
