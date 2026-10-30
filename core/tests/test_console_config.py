"""Runtime web-console configuration (/console-config.js)."""

import json

import pytest

from orrery_core.serving.console_config import GLOBAL_NAME, ConsoleConfig, render_script


def _payload(script: str) -> dict:
    prefix = f"window.{GLOBAL_NAME} = "
    assert script.startswith(prefix) and script.rstrip().endswith(";")
    return json.loads(script[len(prefix) :].rstrip().rstrip(";"))


def test_unset_config_is_an_empty_object():
    # Empty means "use the build-time values": the token gate unless built with SSO.
    assert _payload(render_script(ConsoleConfig())) == {}


def test_from_env_reads_oidc_and_falls_back_to_the_servers_role_claim(monkeypatch):
    monkeypatch.setenv("ORRERY_CONSOLE_OIDC_ISSUER", "https://id.example/realms/r/")
    monkeypatch.setenv("ORRERY_CONSOLE_OIDC_CLIENT_ID", "orrery-lab")
    monkeypatch.delenv("ORRERY_CONSOLE_OIDC_SCOPE", raising=False)
    monkeypatch.delenv("ORRERY_CONSOLE_OIDC_ROLE_CLAIM", raising=False)
    monkeypatch.setenv("JWT_ROLE_CLAIM", "resource_access.orrery-lab.roles")

    assert _payload(render_script(ConsoleConfig.from_env())) == {
        "oidc": {"issuer": "https://id.example/realms/r", "clientId": "orrery-lab"},
        "roleClaim": "resource_access.orrery-lab.roles",
    }


def test_an_explicit_console_role_claim_wins(monkeypatch):
    monkeypatch.setenv("JWT_ROLE_CLAIM", "roles")
    monkeypatch.setenv("ORRERY_CONSOLE_OIDC_ROLE_CLAIM", "realm_access.roles")
    assert ConsoleConfig.from_env().role_claim == "realm_access.roles"


def test_script_cannot_close_a_script_element():
    script = render_script(ConsoleConfig(oidc_client_id="</script><script>alert(1)</script>"))
    assert "</script>" not in script
    assert _payload(script)["oidc"]["clientId"] == "</script><script>alert(1)</script>"


# ── Startup checks ───────────────────────────────────────────────────


def test_an_issuer_the_server_would_not_accept_stops_startup():
    # Sign-in would succeed and every API call would then get 401.
    cfg = ConsoleConfig(oidc_issuer="https://sso.example.com/realms/a")
    with pytest.raises(ValueError, match="differs from JWT_ISSUER"):
        cfg.validate("https://sso.example.com/realms/b")
    cfg.validate("https://sso.example.com/realms/a/")  # a trailing slash is the same issuer
    cfg.validate(None)  # nothing to compare with


@pytest.mark.parametrize(
    ("issuer", "ok"),
    [
        ("http://localhost:8080/realms/dev", True),
        ("http://127.0.0.1/realms/dev", True),
        ("http://sso.example.com/realms/a", False),
        ("sso.example.com/realms/a", False),
    ],
)
def test_an_issuer_outside_localhost_must_be_https(issuer, ok):
    cfg = ConsoleConfig(oidc_issuer=issuer)
    if ok:
        cfg.validate(None)
    else:
        with pytest.raises(ValueError, match="must be an https URL"):
            cfg.validate(None)


# ── Branding ─────────────────────────────────────────────────────────


def test_branding_from_env(monkeypatch):
    monkeypatch.setenv("ORRERY_CONSOLE_TITLE", "Tirraflow Ops")
    monkeypatch.setenv("ORRERY_CONSOLE_ENVIRONMENT", "Production")
    monkeypatch.setenv("ORRERY_CONSOLE_ACCENT", "RED")
    cfg = ConsoleConfig.from_env()
    cfg.validate(None)
    assert cfg.as_dict()["branding"] == {
        "title": "Tirraflow Ops",
        "environment": "production",
        "accent": "red",
    }


def test_no_branding_sends_nothing(monkeypatch):
    for name in ("ORRERY_CONSOLE_TITLE", "ORRERY_CONSOLE_ENVIRONMENT", "ORRERY_CONSOLE_ACCENT"):
        monkeypatch.delenv(name, raising=False)
    assert "branding" not in ConsoleConfig.from_env().as_dict()


@pytest.mark.parametrize(
    "cfg",
    [
        ConsoleConfig(environment="prod"),
        ConsoleConfig(accent="#ff0000"),
        ConsoleConfig(title="x" * 61),
        ConsoleConfig(title="Ops\nInjected"),
    ],
)
def test_invalid_branding_stops_startup(cfg):
    with pytest.raises(ValueError):
        cfg.validate(None)


def test_a_title_cannot_close_the_script_element():
    script = render_script(ConsoleConfig(title="</script><b>"))
    assert "</script>" not in script
    assert json.loads(script.split("=", 1)[1].strip().rstrip(";"))["branding"]["title"] == (
        "</script><b>"
    )
