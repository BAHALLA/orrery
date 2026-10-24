"""Tests for PIIRedactionPlugin (AEP-013)."""

from unittest.mock import MagicMock, patch

import pytest

import orrery_core.plugins.pii_plugin as pii_plugin
from orrery_core.plugins import PIIRedactionPlugin, default_plugins
from orrery_core.plugins.pii_plugin import REDACTED, REDACTED_IP, redact_structure

# ── redact_structure: key-based ──────────────────────────────────────


class TestKeyBasedRedaction:
    def test_credential_keys_redacted(self):
        data = {
            "password": "hunter2",
            "access_token": "abc123",
            "api_key": "xyz",
            "client_secret": "shh",
            "PRIVATE_KEY": "----",
        }
        count = redact_structure(data)
        assert count == 5
        assert all(v == REDACTED for v in data.values())

    def test_non_credential_keys_untouched(self):
        data = {"status": "ok", "broker_count": 3, "token_count": 512, "tokens_used": 90}
        assert redact_structure(data) == 0
        assert data["token_count"] == 512

    def test_pagination_cursors_allowlisted(self):
        data = {"next_page_token": "opaque-cursor", "page_token": "abc"}
        assert redact_structure(data) == 0
        assert data["next_page_token"] == "opaque-cursor"

    def test_camelcase_and_pascalcase_keys_redacted(self):
        data = {
            "dbPassword": "pw",
            "accessToken": "tok",
            "AccessToken": "tok",
            "clientSecret": "shh",
            "APIKey": "k",
        }
        count = redact_structure(data)
        assert count == 5
        assert all(v == REDACTED for v in data.values())

    def test_camelcase_pagination_cursor_allowlisted(self):
        """camelCase cursors normalize onto the snake_case allowlist."""
        data = {"nextPageToken": "opaque-cursor", "pageToken": "abc"}
        assert redact_structure(data) == 0
        assert data["nextPageToken"] == "opaque-cursor"

    def test_camelcase_non_credentials_untouched(self):
        data = {"tokenCount": 5, "monKey": "x"}
        assert redact_structure(data) == 0

    def test_llm_usage_counts_untouched(self):
        """Plural token endings are usage counts, not credentials."""
        data = {"maxTokens": 100, "prompt_tokens": 10, "total_tokens": 15}
        assert redact_structure(data) == 0
        assert data == {"maxTokens": 100, "prompt_tokens": 10, "total_tokens": 15}

    def test_nested_structures(self):
        data = {"pods": [{"env": {"DB_PASSWORD": "pw", "DB_HOST": "postgres"}}]}
        assert redact_structure(data) == 1
        assert data["pods"][0]["env"]["DB_PASSWORD"] == REDACTED
        assert data["pods"][0]["env"]["DB_HOST"] == "postgres"

    def test_empty_values_left_alone(self):
        data = {"password": "", "token": None}
        assert redact_structure(data) == 0


# ── redact_structure: value patterns ─────────────────────────────────


class TestValuePatternRedaction:
    def test_kv_pair_in_log_line(self):
        data = {"logs": "2026-01-01 conn ok password=hunter2 retry=3"}
        assert redact_structure(data) == 1
        assert "hunter2" not in data["logs"]
        assert "retry=3" in data["logs"]

    def test_pem_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
        data = {"config": f"cert follows: {pem}"}
        redact_structure(data)
        assert "MIIEow" not in data["config"]

    def test_provider_token_shapes(self):
        data = {
            "lines": [
                "aws key AKIAIOSFODNN7EXAMPLE found",
                "gh token ghp_abcdefghijklmnopqrstuvwxyz012345 in env",
                "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpM",
            ]
        }
        assert redact_structure(data) == 3
        assert all(REDACTED in line for line in data["lines"])

    def test_plain_text_untouched(self):
        data = {"summary": "3 brokers healthy, ISR stable, no under-replicated partitions"}
        assert redact_structure(data) == 0


# ── IP redaction (opt-in) ────────────────────────────────────────────


class TestIPRedaction:
    def test_ips_kept_by_default(self):
        data = {"endpoint": "broker at 10.0.0.12:9092"}
        redact_structure(data)
        assert "10.0.0.12" in data["endpoint"]

    def test_ips_redacted_when_opted_in(self):
        data = {"endpoint": "broker at 10.0.0.12:9092"}
        redact_structure(data, redact_ips=True)
        assert REDACTED_IP in data["endpoint"]
        assert "10.0.0.12" not in data["endpoint"]


# ── Plugin behavior ──────────────────────────────────────────────────


class TestPIIRedactionPlugin:
    @pytest.mark.asyncio
    async def test_mutates_in_place_and_returns_none(self):
        """Non-None would early-exit ADK's after-tool chain and skip observers."""
        plugin = PIIRedactionPlugin()
        result = {"password": "hunter2", "status": "ok"}
        returned = await plugin.after_tool_callback(
            tool=MagicMock(name="tool"), tool_args={}, tool_context=MagicMock(), result=result
        )
        assert returned is None
        assert result["password"] == REDACTED
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_non_dict_result_is_safe(self):
        plugin = PIIRedactionPlugin()
        returned = await plugin.after_tool_callback(
            tool=MagicMock(), tool_args={}, tool_context=MagicMock(), result="plain string"
        )
        assert returned is None


# ── default_plugins wiring ───────────────────────────────────────────


class TestDefaultPluginsWiring:
    def test_registered_by_default(self, monkeypatch):
        monkeypatch.delenv("ORRERY_PII_REDACTION", raising=False)
        plugins = default_plugins()
        assert any(isinstance(p, PIIRedactionPlugin) for p in plugins)

    def test_env_flag_disables(self, monkeypatch):
        monkeypatch.setenv("ORRERY_PII_REDACTION", "false")
        plugins = default_plugins()
        assert not any(isinstance(p, PIIRedactionPlugin) for p in plugins)

    def test_registered_before_audit(self, monkeypatch):
        """Audit must observe redacted results, so PII comes first."""
        from orrery_core.plugins import AuditPlugin

        monkeypatch.delenv("ORRERY_PII_REDACTION", raising=False)
        plugins = default_plugins()
        names = [type(p).__name__ for p in plugins]
        assert names.index("PIIRedactionPlugin") < names.index(AuditPlugin.__name__)


# ── Event-loop safety ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_large_payload_redaction_leaves_the_event_loop():
    """Scanning costs ~60 ms per MiB and this callback is async with a pure-CPU
    body, so on a big result it would hold the loop and stall every other
    in-flight request. It runs on the *uncapped* result, because the output cap
    must stay last in the chain."""
    import threading

    plugin = PIIRedactionPlugin()
    caller = threading.current_thread().name
    seen: list[str] = []

    real = pii_plugin.redact_structure

    def spy(obj, **kwargs):
        seen.append(threading.current_thread().name)
        return real(obj, **kwargs)

    result = {"status": "ok", "logs": "password=hunter2\n" + ("x" * 400_000)}
    with patch.object(pii_plugin, "redact_structure", spy):
        await plugin.after_tool_callback(
            tool=MagicMock(), tool_args={}, tool_context=MagicMock(), result=result
        )

    assert seen and seen[0] != caller  # ran on a worker, not the loop thread
    assert "hunter2" not in result["logs"]  # and still redacted in place


@pytest.mark.asyncio
async def test_small_payload_stays_inline():
    """A thread hop would cost more than the scan for an ordinary status dict."""
    import threading

    plugin = PIIRedactionPlugin()
    caller = threading.current_thread().name
    seen: list[str] = []

    real = pii_plugin.redact_structure

    def spy(obj, **kwargs):
        seen.append(threading.current_thread().name)
        return real(obj, **kwargs)

    with patch.object(pii_plugin, "redact_structure", spy):
        await plugin.after_tool_callback(
            tool=MagicMock(),
            tool_args={},
            tool_context=MagicMock(),
            result={"status": "ok", "password": "hunter2"},
        )

    assert seen == [caller]


@pytest.mark.asyncio
async def test_offloaded_redaction_still_mutates_in_place():
    """The in-place + return-None contract is what keeps the rest of ADK's
    after-tool chain running; moving to a thread must not change it."""
    plugin = PIIRedactionPlugin()
    result = {"entries": [{"api_key": "AKIA1234567890ABCDEF"}], "pad": "z" * 400_000}

    returned = await plugin.after_tool_callback(
        tool=MagicMock(), tool_args={}, tool_context=MagicMock(), result=result
    )

    assert returned is None
    assert result["entries"][0]["api_key"] == REDACTED


# ── Pattern pre-filter ────────────────────────────────────────────────

# One representative secret per pattern, used to prove the literal pre-filter
# never skips a pattern that would have matched.
_SAMPLE_SECRETS = [
    "db password=hunter2",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIB\n-----END RSA PRIVATE KEY-----",
    "aws AKIA1234567890ABCDEF",
    "gh ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaabbbb",
    "slack xoxb-1234567890-abcdefghij",
    "openai sk-abcdefghijklmnopqrstuvwxyz0123",
    "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3In0.abcdefghijkl",
    "DATABASE_URL=postgres://admin:P@ssw0rd@db:5432/prod",
    "gemini AIzaSyA1234567890abcdefghijklmnopqrstuv",
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Only the userinfo goes: the scheme and host stay for the diagnosis.
        ("postgres://admin:P@ssw0rd@db:5432/prod", "postgres://[REDACTED]@db:5432/prod"),
        ("redis://:s3cret@cache:6379/0", "redis://[REDACTED]@cache:6379/0"),
        # No password, nothing to hide.
        ("https://example.com:443/a?b=c", "https://example.com:443/a?b=c"),
        ("ssh://git@github.com/org/repo", "ssh://git@github.com/org/repo"),
        # A port, then a later "@" in the same line, is not a credential.
        (
            '{"upstream":"http://svc:8080","user":"bob@example.com"}',
            '{"upstream":"http://svc:8080","user":"bob@example.com"}',
        ),
        (
            "GET http://localhost:9200?q=user@example.com",
            "GET http://localhost:9200?q=user@example.com",
        ),
        ("url=http://a:80,next=b@c", "url=http://a:80,next=b@c"),
    ],
)
def test_url_credentials_are_redacted_and_the_host_kept(text, expected):
    data = {"line": text}
    redact_structure(data)
    assert data["line"] == expected


@pytest.mark.parametrize("secret", _SAMPLE_SECRETS)
def test_every_pattern_still_fires_after_prefiltering(secret):
    data = {"line": secret}
    assert redact_structure(data) > 0
    assert REDACTED in data["line"]


@pytest.mark.parametrize("secret", _SAMPLE_SECRETS)
def test_secrets_are_found_when_buried_in_a_large_clean_payload(secret):
    """The pre-filter runs against the whole string, so a secret hiding in the
    middle of megabytes of ordinary log output must still trip its literal."""
    noise = "2026-07-24T10:15:03Z INFO NetworkClient - node 3 reconnecting\n" * 20_000
    data = {"logs": noise + secret + "\n" + noise}
    assert redact_structure(data) > 0
    assert REDACTED in data["logs"]


def test_every_declared_trigger_is_actually_required_by_its_pattern():
    """A trigger literal is a correctness claim: the pattern must not be able to
    match text that lacks it. Guard it against a future pattern edit."""
    for pattern, triggers in pii_plugin.SECRET_VALUE_PATTERNS:
        for sample in _SAMPLE_SECRETS:
            if pattern.search(sample) and triggers:
                assert any(trigger in sample for trigger in triggers), (
                    f"{pattern.pattern} matches {sample!r} but none of {triggers} appear in it"
                )


def test_prefilter_skips_work_it_cannot_need():
    """Clean text must not pay for the provider-token patterns at all."""
    clean = "plain log line without any credentials\n" * 50_000
    redacted, count = pii_plugin._redact_text(clean, redact_ips=False)
    assert count == 0
    assert redacted == clean


# ── Results that are not dicts (the shape the walk used to skip) ──────
#
# ADK does not require a tool to return a dict: `FunctionTool.run_async` returns
# whatever the function returned, and the `{"result": ...}` normalization happens
# in `__build_response_event` — *after* this chain. A dict/list-only walk
# therefore skipped bare strings and Pydantic models entirely. `load_memory`,
# wired into the shipped chat root, returns a Pydantic LoadMemoryResponse.


def _plugin():
    return PIIRedactionPlugin()


async def _after(result, plugin=None):
    return await (plugin or _plugin()).after_tool_callback(
        tool=MagicMock(name="a_tool"), tool_args={}, tool_context=MagicMock(), result=result
    )


class TestNonDictResults:
    @pytest.mark.asyncio
    async def test_bare_string_result_is_redacted_via_replacement(self):
        """A string cannot be mutated, so the only scrub is a returned copy."""
        replacement = await _after("connect with password=hunter2")
        assert replacement == "connect with [REDACTED]"

    @pytest.mark.asyncio
    async def test_clean_string_result_keeps_the_chain_intact(self):
        """No secret means no replacement, so later observers still run."""
        assert await _after("3/3 replicas ready") is None

    @pytest.mark.asyncio
    async def test_bytes_result_is_decoded_and_redacted(self):
        """The whole key=value pair is replaced, and bytes come back as text —
        which is what ADK would have shown the model anyway."""
        assert await _after(b"token=abc123") == REDACTED

    @pytest.mark.asyncio
    async def test_pydantic_result_is_redacted_in_place_chain_intact(self):
        """The load_memory shape: mutable attributes, so no early exit needed."""
        from google.adk.memory.memory_entry import MemoryEntry
        from google.adk.tools.load_memory_tool import LoadMemoryResponse
        from google.genai import types as genai_types

        response = LoadMemoryResponse(
            memories=[
                MemoryEntry(
                    content=genai_types.Content(
                        role="user",
                        parts=[genai_types.Part(text="prod key AKIAIOSFODNN7EXAMPLE")],
                    )
                )
            ]
        )

        assert await _after(response) is None, "must not early-exit for a mutable result"
        parts = response.memories[0].content.parts
        assert parts is not None
        assert parts[0].text == "prod key [REDACTED]"

    def test_sensitive_attribute_names_are_redacted_like_dict_keys(self):
        class Credentials:
            def __init__(self):
                self.username = "svc-account"
                self.api_key = "abc123xyz"

        creds = Credentials()
        assert redact_structure(creds) == 1
        assert creds.api_key == REDACTED
        assert creds.username == "svc-account"

    def test_scalars_are_not_mistaken_for_attribute_holders(self):
        """Guards the walk's type gate: ints and None have no fields to scrub."""
        for scalar in (1, 1.5, True, None, complex(1, 2)):
            assert redact_structure(scalar) == 0
