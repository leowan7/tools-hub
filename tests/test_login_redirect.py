"""Open-redirect guard on the /login ``next`` parameter (cso audit L1).

The original guard was a shape-based blocklist: startswith "/", not "//",
not "/\\". ``/\\t/evil.com`` passed all three, and the header layer then
STRIPPED the tab, emitting ``Location: //evil.com`` — a protocol-relative
off-site redirect. Reproduced end to end on 2c057fc before the fix.

These tests assert the property the docstring claims, at the level where
the bug actually appeared: the SERIALISED Location header, not the
pre-serialisation string. Every hostile case below is one that at least one
layer of ``safe_next`` uniquely catches.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from blueprints.auth import safe_next


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


# Values that must never survive. Grouped by the layer that catches them so
# a mutation to any single layer turns this suite red.
HOSTILE = [
    # -- scheme / netloc (urlsplit allowlist) --
    "//evil.com",
    "//evil.com/a",
    "https://evil.com",
    "http://evil.com",
    "//user@evil.com",
    "evil.com",
    # -- control characters --
    # TAB is the reproduced exploit: stripped during header serialisation,
    # turning "/\t/evil.com" into "//evil.com".
    "/\t/evil.com",
    "/\n/evil.com",
    "/\r/evil.com",
    # urlsplit strips ONLY tab/CR/LF; these reach the browser raw, so the
    # explicit control-char reject is what stops them.
    "/\x0b/evil.com",
    "/\x0c/evil.com",
    "/\x00/evil.com",
    "/\x7f/evil.com",
    "/\x85/evil.com",
    # -- raw leading "//" that urlsplit parses as an empty netloc --
    "///evil.com",
    "////evil.com",
    # -- backslash forms urlsplit treats as an innocent path --
    "/\\evil.com",
    "/\\\\evil.com",
    "/\\/evil.com",
]

SAFE = [
    "/",
    "/jobs",
    "/account/wallet",
    "/tools/mpnn",
    "/tools/rfdiffusion?pilot=1",
    "/account/wallet?a=1&b=2",
    "/jobs?page=2&status=succeeded",
    "/tools/x#frag",
]


@pytest.mark.parametrize("hostile", HOSTILE)
def test_safe_next_rejects_offsite(hostile):
    """Unit level: every hostile shape collapses to the fallback."""
    assert safe_next(hostile) == "/"


@pytest.mark.parametrize("safe", SAFE)
def test_safe_next_preserves_internal(safe):
    assert safe_next(safe) == safe


@pytest.mark.parametrize("empty", [None, ""])
def test_safe_next_defaults(empty):
    assert safe_next(empty) == "/"


@pytest.mark.parametrize("hostile", HOSTILE)
def test_login_post_rejects_offsite_next(client, hostile):
    """End to end: assert the SERIALISED header, where the bug appeared.

    ``Location: //evil.com`` is the exact string the pre-fix code emitted
    for ``/\\t/evil.com``; anything with a netloc must never appear here.
    """
    resp = _login(client, hostile)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location in ("/", "http://localhost/"), location
    # Belt and braces: no off-origin host may appear in the header at all.
    assert "evil.com" not in location


@pytest.mark.parametrize("safe", SAFE)
def test_login_post_preserves_safe_next(client, safe):
    resp = _login(client, safe)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(safe)


@pytest.mark.parametrize("hostile", HOSTILE)
def test_login_get_sanitises_hidden_next(client, hostile):
    """The GET branch must not render a value the POST would reject.

    Pre-fix, GET echoed ``next`` into the hidden field unvalidated and
    leaned on Jinja escaping plus the POST-side re-check.
    """
    resp = client.get("/login", query_string={"next": hostile})
    assert resp.status_code == 200
    fields = re.findall(rb'name="next"\s+value="([^"]*)"', resp.data)
    assert fields, "hidden next field missing"
    for value in fields:
        assert value == b"/", value


@pytest.mark.parametrize("safe", ["/tools/rfdiffusion?pilot=1", "/jobs"])
def test_login_get_preserves_safe_next(client, safe):
    resp = client.get("/login", query_string={"next": safe})
    assert resp.status_code == 200
    # Jinja escapes "&" in the query string; compare on the unescaped value.
    assert safe.encode().replace(b"&", b"&amp;") in resp.data
