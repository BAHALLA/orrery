"""Tests for orrery_core.security.auth."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest

from orrery_core.security.auth import (
    AUTH_STATE_KEY,
    AuthContext,
    AuthError,
    AuthPlugin,
    JWTConfig,
    extract_role,
    verify_token,
)
from orrery_core.security.rbac import USER_ROLE_STATE_KEY

# ── extract_role ─────────────────────────────────────────────────────


class TestExtractRole:
    def test_no_role_claim_defaults_to_viewer(self):
        assert extract_role({}) == "viewer"
        assert extract_role({"sub": "alice"}) == "viewer"

    def test_admin_list_claim(self):
        assert extract_role({"roles": ["admin"]}) == "admin"

    def test_admin_string_claim(self):
        assert extract_role({"roles": "admin"}) == "admin"

    def test_admin_wins_over_operator(self):
        assert extract_role({"roles": ["operator", "admin"]}) == "admin"
        assert extract_role({"roles": "operator,admin"}) == "admin"

    def test_operator_when_admin_absent(self):
        assert extract_role({"roles": ["operator", "viewer"]}) == "operator"

    def test_unknown_roles_default_to_viewer(self):
        assert extract_role({"roles": ["guest", "anon"]}) == "viewer"

    def test_namespace_aliases_accepted(self):
        assert extract_role({"roles": ["orrery-admin"]}) == "admin"
        assert extract_role({"roles": ["orrery_operator"]}) == "operator"

    def test_custom_role_claim(self):
        assert extract_role({"groups": ["admin"]}, role_claim="groups") == "admin"

    def test_case_insensitive(self):
        assert extract_role({"roles": ["ADMIN"]}) == "admin"
        assert extract_role({"roles": "Operator"}) == "operator"

    def test_unsupported_type_logs_and_returns_viewer(self, caplog):
        assert extract_role({"roles": 123}) == "viewer"
        assert "Unsupported type" in caplog.text

    def test_custom_admin_values(self):
        assert (
            extract_role({"roles": ["sre"]}, admin_values={"sre"}, operator_values=set()) == "admin"
        )


# ── JWTConfig ────────────────────────────────────────────────────────


class TestJWTConfig:
    def test_validate_hs256_requires_secret(self):
        cfg = JWTConfig(algorithm="HS256", secret=None)
        with pytest.raises(AuthError, match="JWT_SECRET is required"):
            cfg.validate()

    def test_validate_hs256_with_secret_ok(self):
        JWTConfig(algorithm="HS256", secret="x" * 32).validate()

    @pytest.mark.parametrize(
        ("algorithm", "minimum"), [("HS256", 32), ("HS384", 48), ("HS512", 64)]
    )
    def test_validate_rejects_hmac_secret_shorter_than_the_hash(self, algorithm, minimum):
        """RFC 7518 §3.2: a short HMAC key is brute-forceable offline from one
        captured token — and a forged token here is a forged admin."""
        with pytest.raises(AuthError, match="too short"):
            JWTConfig(algorithm=algorithm, secret="x" * (minimum - 1)).validate()
        JWTConfig(algorithm=algorithm, secret="x" * minimum).validate()

    def test_secret_length_is_measured_in_bytes(self):
        # 16 two-byte characters are 32 bytes of key material.
        JWTConfig(algorithm="HS256", secret="é" * 16).validate()

    def test_validate_rs256_requires_jwks(self):
        cfg = JWTConfig(algorithm="RS256", jwks_url=None)
        with pytest.raises(AuthError, match="JWT_JWKS_URL is required"):
            cfg.validate()

    def test_validate_rejects_unknown_algorithm(self):
        cfg = JWTConfig(algorithm="MD5", secret="x")
        with pytest.raises(AuthError, match="Unsupported JWT_ALGORITHM"):
            cfg.validate()

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("JWT_ALGORITHM", "rs256")
        monkeypatch.setenv("JWT_JWKS_URL", "https://idp/jwks")
        monkeypatch.setenv("JWT_AUDIENCE", "orrery")
        monkeypatch.setenv("JWT_ISSUER", "https://idp")
        monkeypatch.setenv("JWT_LEEWAY_SECONDS", "60")
        cfg = JWTConfig.from_env()
        assert cfg.algorithm == "RS256"
        assert cfg.jwks_url == "https://idp/jwks"
        assert cfg.audience == "orrery"
        assert cfg.issuer == "https://idp"
        assert cfg.leeway_seconds == 60


# ── verify_token (HS256) ─────────────────────────────────────────────


_TEST_SECRET = "x" * 64  # 32+ bytes to satisfy PyJWT HMAC length recommendation


def _hs256_token(claims: dict, secret: str = _TEST_SECRET) -> str:
    return pyjwt.encode(claims, secret, algorithm="HS256")


class TestVerifyTokenHMACVariants:
    @pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
    def test_round_trip(self, algorithm):
        token = pyjwt.encode(
            {"sub": "alice", "roles": ["operator"], "exp": int(time.time()) + 60},
            _TEST_SECRET,
            algorithm=algorithm,
        )
        ctx = verify_token(token, JWTConfig(algorithm=algorithm, secret=_TEST_SECRET))
        assert (ctx.subject, ctx.role) == ("alice", "operator")

    def test_token_signed_with_other_hmac_variant_is_rejected(self):
        token = pyjwt.encode(
            {"sub": "alice", "exp": int(time.time()) + 60}, _TEST_SECRET, algorithm="HS512"
        )
        with pytest.raises(AuthError):
            verify_token(token, JWTConfig(algorithm="HS256", secret=_TEST_SECRET))

    def test_garbage_token_is_an_auth_error(self):
        with pytest.raises(AuthError):
            verify_token("not-a-jwt", JWTConfig(algorithm="HS256", secret=_TEST_SECRET))


class TestVerifyTokenHS256:
    def test_round_trip(self):
        claims = {
            "sub": "alice",
            "roles": ["operator"],
            "exp": int(time.time()) + 600,
            "aud": "orrery",
            "iss": "https://idp",
        }
        token = _hs256_token(claims)
        ctx = verify_token(
            token,
            JWTConfig(
                algorithm="HS256",
                secret=_TEST_SECRET,
                audience="orrery",
                issuer="https://idp",
            ),
        )
        assert isinstance(ctx, AuthContext)
        assert ctx.subject == "alice"
        assert ctx.role == "operator"
        assert ctx.claims["sub"] == "alice"

    def test_empty_token_rejected(self):
        with pytest.raises(AuthError, match="Empty bearer"):
            verify_token("", JWTConfig(algorithm="HS256", secret=_TEST_SECRET))

    def test_bad_signature_rejected(self):
        token = _hs256_token({"sub": "a", "exp": int(time.time()) + 60}, secret="y" * 64)
        with pytest.raises(AuthError, match="Invalid or expired"):
            verify_token(token, JWTConfig(algorithm="HS256", secret=_TEST_SECRET))

    def test_expired_token_rejected(self):
        token = _hs256_token({"sub": "a", "exp": int(time.time()) - 600})
        with pytest.raises(AuthError, match="Invalid or expired"):
            verify_token(token, JWTConfig(algorithm="HS256", secret=_TEST_SECRET))

    def test_missing_sub_rejected(self):
        token = _hs256_token({"exp": int(time.time()) + 60})
        with pytest.raises(AuthError, match="'sub' claim"):
            verify_token(token, JWTConfig(algorithm="HS256", secret=_TEST_SECRET))

    def test_wrong_audience_rejected(self):
        token = _hs256_token({"sub": "a", "exp": int(time.time()) + 60, "aud": "other"})
        with pytest.raises(AuthError, match="Invalid or expired"):
            verify_token(
                token,
                JWTConfig(algorithm="HS256", secret=_TEST_SECRET, audience="orrery"),
            )

    def test_wrong_issuer_rejected(self):
        token = _hs256_token({"sub": "a", "exp": int(time.time()) + 60, "iss": "https://other"})
        with pytest.raises(AuthError, match="Invalid or expired"):
            verify_token(
                token,
                JWTConfig(algorithm="HS256", secret=_TEST_SECRET, issuer="https://idp"),
            )

    def test_missing_exp_rejected(self):
        # Even with all other claims, missing exp must fail (no immortal tokens).
        token = _hs256_token({"sub": "a"})
        with pytest.raises(AuthError):
            verify_token(token, JWTConfig(algorithm="HS256", secret=_TEST_SECRET))

    def test_leeway_allows_recently_expired(self):
        token = _hs256_token({"sub": "a", "exp": int(time.time()) - 10})
        ctx = verify_token(
            token, JWTConfig(algorithm="HS256", secret=_TEST_SECRET, leeway_seconds=30)
        )
        assert ctx.subject == "a"


# ── AuthPlugin ──────────────────────────────────────────────────────


def _make_callback_ctx(state: dict) -> MagicMock:
    ctx = MagicMock()
    ctx.state = state
    return ctx


@pytest.mark.asyncio
class TestAuthPlugin:
    async def test_applies_role_from_auth_state(self):
        plugin = AuthPlugin()
        state: dict = {AUTH_STATE_KEY: {"subject": "alice", "role": "admin", "claims": {}}}
        ctx = _make_callback_ctx(state)

        await plugin.before_agent_callback(agent=MagicMock(), callback_context=ctx)

        assert state[USER_ROLE_STATE_KEY] == "admin"

    async def test_missing_auth_forces_viewer_when_required(self, caplog):
        plugin = AuthPlugin(require_auth=True)
        state: dict = {}
        ctx = _make_callback_ctx(state)

        await plugin.before_agent_callback(agent=MagicMock(), callback_context=ctx)

        assert state[USER_ROLE_STATE_KEY] == "viewer"
        assert "no _auth payload" in caplog.text

    async def test_missing_auth_does_nothing_when_optional(self):
        plugin = AuthPlugin(require_auth=False)
        state: dict = {}
        ctx = _make_callback_ctx(state)

        await plugin.before_agent_callback(agent=MagicMock(), callback_context=ctx)

        assert USER_ROLE_STATE_KEY not in state

    async def test_malformed_auth_payload_forces_viewer(self, caplog):
        plugin = AuthPlugin()
        state: dict = {AUTH_STATE_KEY: {"subject": "alice"}}  # no role
        ctx = _make_callback_ctx(state)

        await plugin.before_agent_callback(agent=MagicMock(), callback_context=ctx)

        assert state[USER_ROLE_STATE_KEY] == "viewer"
        assert "malformed" in caplog.text

    async def test_invalid_role_value_falls_back_to_viewer(self):
        # set_user_role normalises unknown role names to "viewer".
        plugin = AuthPlugin()
        state: dict = {AUTH_STATE_KEY: {"subject": "alice", "role": "superuser"}}
        ctx = _make_callback_ctx(state)

        await plugin.before_agent_callback(agent=MagicMock(), callback_context=ctx)

        assert state[USER_ROLE_STATE_KEY] == "viewer"


# ── AuthContext ─────────────────────────────────────────────────────


def test_auth_context_as_state():
    ctx = AuthContext(subject="alice", role="admin", claims={"sub": "alice", "exp": 1})
    state = ctx.as_state()
    assert state == {"subject": "alice", "role": "admin", "claims": {"sub": "alice", "exp": 1}}
    # as_state returns a copy of the claims, not a reference
    state["claims"]["sub"] = "mallory"
    assert ctx.claims["sub"] == "alice"


class TestExtractRoleDottedClaims:
    """Nested role claims (``realm_access.roles``).

    Keycloak — the reference IdP for the console's SSO mode — nests realm roles
    one level down. A flat lookup silently returns None there, which reads as
    "no roles" and downgrades every SSO user to viewer.
    """

    def test_follows_a_nested_path(self):
        claims = {"realm_access": {"roles": ["admin"]}}
        assert extract_role(claims, role_claim="realm_access.roles") == "admin"

    def test_follows_a_deeper_path(self):
        claims = {"resource_access": {"console": {"roles": ["operator"]}}}
        assert extract_role(claims, role_claim="resource_access.console.roles") == "operator"

    def test_accepts_a_delimited_string_at_a_nested_path(self):
        claims = {"realm_access": {"roles": "operator,other"}}
        assert extract_role(claims, role_claim="realm_access.roles") == "operator"

    def test_unresolvable_path_fails_closed_to_viewer(self):
        assert extract_role({"realm_access": {}}, role_claim="realm_access.roles") == "viewer"
        assert extract_role({}, role_claim="a.b.c") == "viewer"

    def test_non_dict_midway_fails_closed(self):
        claims = {"realm_access": "admin"}
        assert extract_role(claims, role_claim="realm_access.roles") == "viewer"

    def test_flat_claims_are_unchanged(self):
        assert extract_role({"roles": ["admin"]}) == "admin"
        assert extract_role({"groups": ["admin"]}, role_claim="groups") == "admin"

    def test_a_literal_dotted_key_is_not_reachable(self):
        """Documents the trade-off: a claim whose *name* contains a dot is now
        read as a path. No provider we target emits one, and failing closed to
        viewer is the safe direction."""
        assert extract_role({"a.b": ["admin"]}, role_claim="a.b") == "viewer"


# ── Dotted role-claim resolution ─────────────────────────────────────


class TestDottedRoleClaimResolution:
    """Every malformed shape must fail closed to viewer without raising, and
    say so in the log — a mistyped JWT_ROLE_CLAIM silently demoting everyone
    to viewer is otherwise a symptom with nothing pointing at its cause."""

    @pytest.mark.parametrize(
        "claims",
        [
            {"realm_access": "admin"},  # intermediate is a string
            {"realm_access": ["admin"]},  # intermediate is a list
            {"realm_access": 1.5},  # intermediate is a number
            {"realm_access": None},
            {"realm_access": {"roles": {"a": 1}}},  # leaf is a mapping
            {"realm_access": {"roles": 42}},  # leaf is a number
            {},  # path absent entirely
        ],
    )
    def test_malformed_claim_shapes_fail_closed_without_raising(self, claims):
        assert extract_role(claims, role_claim="realm_access.roles") == "viewer"

    def test_well_formed_nested_claims_still_resolve(self):
        assert (
            extract_role({"realm_access": {"roles": ["admin"]}}, role_claim="realm_access.roles")
            == "admin"
        )
        assert (
            extract_role(
                {"resource_access": {"orrery": {"roles": ["operator"]}}},
                role_claim="resource_access.orrery.roles",
            )
            == "operator"
        )

    def test_unresolved_claim_is_logged(self, caplog):
        with caplog.at_level("DEBUG", logger="orrery.auth"):
            extract_role({"sub": "alice"}, role_claim="realm_access.roles")
        assert any("realm_access.roles" in r.getMessage() for r in caplog.records), (
            "an unresolved role claim must leave a trace explaining the viewer fallback"
        )
