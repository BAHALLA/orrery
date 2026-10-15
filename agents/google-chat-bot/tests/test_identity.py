"""Google Chat: the verified sender's email is the identity to act as."""

from google_chat_bot.handler import _caller


def test_a_verified_email_becomes_the_caller():
    caller = _caller("ana@example.com")
    assert caller is not None and caller.username == "ana@example.com"


def test_the_unknown_placeholder_is_never_impersonated():
    assert _caller("unknown") is None
    assert _caller("") is None
