"""Open-redirect guard on the /login ``next`` parameter (cso audit L1).

A leading "/" is not a sufficient same-origin check: browsers treat
``//host`` (protocol-relative) and ``/\\host`` as absolute URLs to a
foreign origin. The login redirect must collapse those to "/".
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _login(client, next_value):
    with patch("shared.auth.verify_login", return_value=(True, None, "user-123")):
        return client.post(
            "/login",
            data={"email": "a@b.com", "password": "x", "next": next_value},
        )


@pytest.mark.parametrize(
    "hostile",
    ["//evil.com", "/\\evil.com", "https://evil.com", "http://evil.com"],
)
def test_login_rejects_offsite_next(client, hostile):
    resp = _login(client, hostile)
    assert resp.status_code == 302
    # Never redirect to the foreign origin; collapse to root.
    assert resp.headers["Location"] in ("/", "http://localhost/")


@pytest.mark.parametrize("safe", ["/jobs", "/account/wallet", "/tools/mpnn"])
def test_login_preserves_safe_next(client, safe):
    resp = _login(client, safe)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(safe)


# ---------------------------------------------------------------------------
# The pilot round trip. ``signin_url()`` used to build ``next`` from
# ``request.path``, which drops the query string — so a visitor who chose
# /tools/rfdiffusion?pilot=1, signed in, and came back landed on a bare
# form with the pilot silently discarded. The feature evaporated at
# exactly the moment they committed to it.
#
# These walk the whole hop rather than asserting on the helper, because
# the defect lived in the seam between three pieces (the CTA's href, the
# hidden field on the login form, and the POST validator) and each of
# them looked fine on its own.
# ---------------------------------------------------------------------------

import os  # noqa: E402
import re  # noqa: E402


@pytest.fixture
def tools_client(monkeypatch):
    """A client with every GPU tool flagged on."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from shared.feature_flags import flag_name
    from tools import base as tool_base

    from app import create_app
    slugs = [a.slug for a in tool_base.all_adapters()]
    assert len(slugs) >= 14, f"adapter registry holds {len(slugs)} tools"
    for slug in slugs:
        monkeypatch.setenv(flag_name(slug), "on")
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _signin_href(html: str) -> str:
    m = re.search(r'href="(/login\?next=[^"]*)"', html)
    assert m, "no sign-in link on the anonymous tool page"
    return m.group(1).replace("&amp;", "&")


class TestPilotSurvivesTheAuthWall:

    def test_signin_link_carries_the_query_string(self, tools_client):
        html = tools_client.get(
            "/tools/rfdiffusion?pilot=1"
        ).get_data(as_text=True)
        href = _signin_href(html)
        # Werkzeug percent-encodes "=" but not "/" or "?" inside a query
        # VALUE, so the raw href is /login?next=/tools/rfdiffusion?pilot%3D1.
        # Assert on what the server actually parses back out, not on the
        # particular encoding werkzeug happens to choose.
        from urllib.parse import parse_qs, urlparse
        got = parse_qs(urlparse(href).query)["next"][0]
        assert got == "/tools/rfdiffusion?pilot=1", href

    def test_the_whole_round_trip_lands_on_a_prefilled_form(
        self, tools_client
    ):
        """Anonymous pilot page -> sign in -> the pilot params are there."""
        from shared.tool_meta import meta_for
        want = meta_for("rfdiffusion").PILOT["params"]["num_designs"]

        anon = tools_client.get(
            "/tools/rfdiffusion?pilot=1"
        ).get_data(as_text=True)
        # The pilot is loaded before the auth wall...
        assert re.search(
            rf'name="num_designs"[^>]*value="{want}"', anon
        ) or re.search(rf'value="{want}"[^>]*name="num_designs"', anon), \
            "anonymous ?pilot=1 did not prefill the design count"

        login_page = tools_client.get(_signin_href(anon))
        assert login_page.status_code == 200
        hidden = re.search(
            r'name="next"[^>]*value="([^"]*)"',
            login_page.get_data(as_text=True),
        )
        assert hidden, "login form carried no next field"
        next_value = hidden.group(1).replace("&amp;", "&")
        assert next_value == "/tools/rfdiffusion?pilot=1", next_value

        resp = _login(tools_client, next_value)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/tools/rfdiffusion?pilot=1")

        # ...and is still loaded on the other side, which is the point.
        from types import SimpleNamespace
        ctx = SimpleNamespace(
            user_id="user-123", tier="free", balance=100,
            email="a@b.com",
        )
        with patch("blueprints.tools.load_user_context", return_value=ctx), \
                patch(
                    "blueprints.tools.get_or_create_wallet",
                    return_value={"balance_usd": 50, "wallet_frozen": False},
                ):
            after = tools_client.get(
                "/tools/rfdiffusion?pilot=1"
            ).get_data(as_text=True)
        assert re.search(
            rf'name="num_designs"[^>]*value="{want}"', after
        ) or re.search(rf'value="{want}"[^>]*name="num_designs"', after), \
            "signed-in ?pilot=1 did not prefill the design count"

    @pytest.mark.parametrize(
        "hostile",
        [
            "https://evil.com/tools/rfdiffusion?pilot=1",
            "//evil.com/tools/rfdiffusion?pilot=1",
            "/\\evil.com/tools/rfdiffusion?pilot=1",
        ],
    )
    def test_a_query_string_does_not_smuggle_an_offsite_next(
        self, client, hostile
    ):
        """Widening what feeds ``next`` must not widen where it can go."""
        resp = _login(client, hostile)
        assert resp.status_code == 302
        assert resp.headers["Location"] in ("/", "http://localhost/")
