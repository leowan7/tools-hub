"""A Supabase weak-password rejection tells the user which rule failed.

On 2026-10-14 an 11-character password at /signup was answered with
"Password is too weak. Use at least 8 characters." Any provider message
containing "password" and "weak" got that text, whichever rule refused it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from supabase import AuthWeakPasswordError

from shared.auth import SignupContext, issue_signup_token, register_user, update_password

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_PROVIDER_TEXT = "PROVIDER-TEXT Password is known to be weak"

_PHRASES = {
    "length": "too short",
    "characters": "upper and lower case letters, numbers and symbols",
    "pwned": "known data breach",
}


def _rejecting_client(reasons):
    client = MagicMock()
    exc = AuthWeakPasswordError(_PROVIDER_TEXT, 422, reasons)
    client.auth.admin.create_user.side_effect = exc
    client.auth.update_user.side_effect = exc
    return client


def _signup(reasons):
    ctx = SignupContext(
        email="someone@acme-bio.com",
        password="Abcdefghij1",
        purpose="",
        honeypot="",
        token=issue_signup_token(),
    )
    with patch("shared.credits.get_service_client", return_value=_rejecting_client(reasons)):
        return register_user(ctx)


def _assert_clean(message):
    assert "8 characters" not in message
    assert "PROVIDER-TEXT" not in message


@pytest.mark.parametrize("reason", sorted(_PHRASES))
def test_signup_names_the_reason_supabase_gave(reason):
    result = _signup([reason])
    assert result.failure_code == "weak_password"
    for code, phrase in _PHRASES.items():
        assert (phrase in result.error_message) == (code == reason), (code, result.error_message)
    _assert_clean(result.error_message)


def test_signup_names_every_reason_when_several_fail():
    result = _signup(["length", "characters"])
    assert _PHRASES["length"] in result.error_message
    assert _PHRASES["characters"] in result.error_message
    assert _PHRASES["pwned"] not in result.error_message


@pytest.mark.parametrize("reasons", [["some_new_rule"], []])
def test_signup_unknown_reason_does_not_blame_length(reasons):
    result = _signup(reasons)
    assert result.failure_code == "weak_password"
    assert "not accepted" in result.error_message
    for phrase in _PHRASES.values():
        assert phrase not in result.error_message
    _assert_clean(result.error_message)


def test_reset_names_the_reason_supabase_gave():
    with patch("shared.auth.get_supabase_client", return_value=_rejecting_client(["pwned"])):
        ok, message = update_password("access", "refresh", "Abcdefghij1")
    assert ok is False
    assert _PHRASES["pwned"] in message
    _assert_clean(message)
