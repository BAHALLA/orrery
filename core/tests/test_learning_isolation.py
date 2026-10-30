"""Learning sandbox mode is off by default: a normal process never imports it (AEP-032).

Checked in a fresh interpreter, because this pytest run imports the package for
its own tests and a shared ``sys.modules`` would hide an accidental import.
"""

from __future__ import annotations

import os
import subprocess
import sys

_PROBE = """
import sys
import orrery_core
import orrery_core.plugins
import orrery_core.serving.server
import orrery_assistant.agent
leaked = sorted(m for m in sys.modules if m.startswith("orrery_core.learning"))
print("LEAKED=" + ",".join(leaked))
"""


def test_a_normal_process_never_imports_the_learning_package():
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ORRERY_LEARNING", "ORRERY_SCENARIO"))
    }
    env["OTEL_TRACING_ENABLED"] = "false"
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    # JSON logs share stdout, so the probe marks its own line.
    [line] = [x for x in result.stdout.splitlines() if x.startswith("LEAKED=")]
    assert line == "LEAKED="
