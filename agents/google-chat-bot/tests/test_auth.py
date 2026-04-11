"""Tests for the Google Chat bot authentication."""

from unittest.mock import patch

from google_chat_bot.auth import verify_google_chat_token

# For testing, we use dummy identities to verify the multi-identity logic.
_TEST_IDENTITIES = frozenset(
    {
        "chat-system@example.com",
        "addon-system@example.com",
    }
)


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_verify_oidc_format(mock_verify):
    """Modern OIDC delivery: iss=accounts.google.com, email=chat SA."""
    mock_verify.return_value = {
        "iss": "https://accounts.google.com",
        "email": "chat-system@example.com",
        "email_verified": True,
        "sub": "user@example.com",
    }
    result = verify_google_chat_token("fake-token", "fake-audience", _TEST_IDENTITIES)
    assert result is not None
    assert result["sub"] == "user@example.com"


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_verify_legacy_service_account_iss(mock_verify):
    """Legacy direct-service-account signing: iss=chat SA, no email claim."""
    mock_verify.return_value = {
        "iss": "chat-system@example.com",
        "sub": "user@example.com",
    }
    result = verify_google_chat_token("fake-token", "fake-audience", _TEST_IDENTITIES)
    assert result is not None


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_verify_workspace_addon_identity(mock_verify):
    """Workspace Add-on identity should be accepted."""
    mock_verify.return_value = {
        "iss": "https://accounts.google.com",
        "email": "addon-system@example.com",
        "email_verified": True,
        "sub": "user@example.com",
    }
    result = verify_google_chat_token("fake-token", "fake-audience", _TEST_IDENTITIES)
    assert result is not None


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_verify_rejects_non_chat_oidc_token(mock_verify):
    """A valid Google OIDC token from some other identity must not pass."""
    mock_verify.return_value = {
        "iss": "https://accounts.google.com",
        "email": "attacker@gmail.com",
        "email_verified": True,
    }
    assert verify_google_chat_token("fake-token", "fake-audience", _TEST_IDENTITIES) is None


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_verify_rejects_unverified_email(mock_verify):
    mock_verify.return_value = {
        "iss": "https://accounts.google.com",
        "email": "chat-system@example.com",
        "email_verified": False,
    }
    assert verify_google_chat_token("fake-token", "fake-audience", _TEST_IDENTITIES) is None


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_verify_rejects_unknown_issuer(mock_verify):
    mock_verify.return_value = {
        "iss": "https://attacker.example.com",
        "email": "chat-system@example.com",
    }
    assert verify_google_chat_token("fake-token", "fake-audience", _TEST_IDENTITIES) is None


@patch("google.oauth2.id_token.verify_oauth2_token")
def test_verify_signature_error(mock_verify):
    mock_verify.side_effect = ValueError("Token expired")
    assert verify_google_chat_token("fake-token", "fake-audience", _TEST_IDENTITIES) is None
