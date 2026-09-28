"""The Create-account panel is on every login.html render, not only /signup.

/login and /forgot-password rendered it with an empty signup_token, so a
visitor who clicked the "Create account" tab there was rejected as
``timing`` ("session expired") however fast they submitted.
"""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


@pytest.mark.parametrize("path", ["/login", "/forgot-password"])
def test_signup_panel_on_other_pages_passes_the_timing_check(client, path):
    html = client.get(path).data
    token = re.search(rb'name="signup_token" value="([^"]*)"', html).group(1)
    rejections = []
    with patch("shared.events.log_signup_rejection",
               lambda **k: rejections.append(k["reason"])), \
         patch("shared.events.log_event", lambda **k: None):
        client.post("/signup", data={
            "email": "someone@gmail.com",
            "password": "abcdefgh1",
            "password2": "abcdefgh1",
            "terms_accepted": "on",
            "signup_token": token.decode(),
        })
    # No purpose on a personal address: the next gate must be the one that
    # fires, proving the token got past the timing check.
    assert rejections == ["purpose_missing"]
