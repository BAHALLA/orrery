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
import orrery_assistant.app
import slack_bot.app
import google_chat_bot.app
import orrery_core.studio.api
import orrery_core.serving.a2a_server
# learning_gate is the always-importable refusal check; the package is not.
leaked = sorted(
    m for m in sys.modules
    if m == "orrery_core.learning" or m.startswith("orrery_core.learning.")
)
print("LEAKED=" + ",".join(leaked))
"""


def test_a_normal_process_never_imports_the_learning_package():
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ORRERY_LEARNING", "ORRERY_SCENARIO"))
    }
    env["OTEL_TRACING_ENABLED"] = "false"
    # Importing the HTTP app builds the server, which needs an auth setup.
    env["AUTH_ENABLED"] = "false"
    # The Slack app validates its credentials at import; CI has no .env.
    env.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
    env.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")
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
