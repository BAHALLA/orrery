"""Workspace-wide pytest configuration, loaded before any test module is collected.

Agent modules call ``load_agent_env()`` at import time, and some build their
``App`` (and therefore ``default_plugins()``) at import time too — so a value in
the developer's root ``.env`` is applied during *collection*, before any fixture
can undo it. Per-test ``monkeypatch.delenv`` (see ``core/tests/conftest.py``)
cannot reach that far back.

``OTEL_TRACING_ENABLED=true`` is the case that bites: the import-time chat root
installs a real OTLP exporter, and at interpreter shutdown its batch thread
retries ``localhost:4317`` and writes tracebacks over the end of the run. CI has
no ``.env``, so this only ever showed up locally — a local run that does not
look like CI is the failure mode the local gate exists to prevent.

``load_dotenv()`` never overrides a variable that is already set, so pinning it
here, before collection, wins. Tests that exercise tracing set it explicitly.
"""

from __future__ import annotations

import os


def pytest_configure(config):
    os.environ["OTEL_TRACING_ENABLED"] = "false"
