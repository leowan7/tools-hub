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
    # These pin the two urlsplit terms that ARE uniquely load-bearing.
    # A scheme with an empty netloc and an absolute path: every other
    # layer waves these through, only `parts.scheme` rejects them.
    "javascript:/a",
    "data:/a",
    "mailto:/x",
    "https://evil.com/a",
    # Path contains "/" but does not START with one: only
    # `parts.path.startswith("/")` rejects it (relaxing it to `"/" in`
    # lets this through).
    "evil.com/a",
    # -- values that make urlsplit RAISE ValueError --
    # Pre-fix these escaped as an unauthenticated 500 on GET /login;
    # 200 on the merge base, 500 on 383760a. See the raise-set tests below.
    "//[",
    "//]",
    "//[]",
    "//[::1",
    "//\uff03evil.com",  # NFKC raise at parse.py:436
    "/\t/[",
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


# ----------------------------------------------------------------------
# urlsplit's raise set: an unauthenticated 500 is a bug too
# ----------------------------------------------------------------------

# Fully URL-safe request lines — no raw control bytes needed, no CSRF token
# needed, no session needed. On 383760a each of these was a 500 on
# GET /login; on the merge base 2c057fc each was a 200.
RAISING_QUERIES = [
    "//[",       # ValueError("Invalid IPv6 URL")
    "//]",       # ValueError("Invalid IPv6 URL")
    "//[]",      # "'' does not appear to be an IPv4 or IPv6 address"
    "//[::1",    # ValueError("Invalid IPv6 URL")
    "//%5B",     # same as "//[" but percent-encoded on the wire
    "/%09/%5B",  # tab-stripped by urlsplit into "//["
    "//%EF%BC%83evil.com",  # NFKC netloc check, parse.py:436
]


@pytest.fixture
def prod_client(monkeypatch):
    """A client on an app with TESTING OFF, so exceptions become 500s.

    With TESTING=True Flask re-raises instead of serving the error page,
    which hides the fact that a real deployment would answer 500.
    """
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    assert not flask_app.config.get("TESTING")
    return flask_app.test_client()


@pytest.mark.parametrize("query", RAISING_QUERIES)
def test_login_get_survives_urlsplit_raises(prod_client, query):
    resp = prod_client.get("/login?next=" + query)
    assert resp.status_code == 200, f"{query!r} -> {resp.status_code}"


@pytest.mark.parametrize("query", RAISING_QUERIES)
def test_login_post_survives_urlsplit_raises(prod_client, query):
    """Same values through the POST branch, asserting a safe redirect."""
    from urllib.parse import unquote
    with patch("shared.auth.verify_login", return_value=(True, None, "u1")):
        resp = prod_client.post(
            "/login",
            data={"email": "a@b.com", "password": "x", "next": unquote(query)},
        )
    assert resp.status_code == 302, f"{query!r} -> {resp.status_code}"
    assert resp.headers["Location"] in ("/", "http://localhost/")


@pytest.mark.parametrize("value", [*RAISING_QUERIES, "//[", "//]"])
def test_safe_next_never_raises(value):
    """Unit level: no input may propagate an exception out of safe_next."""
    from urllib.parse import unquote
    assert safe_next(unquote(value)) == "/"


# ----------------------------------------------------------------------
# The POST failure path re-renders the form; it must re-render the
# SANITISED value, not the raw one. Jinja escaping is not the guard here.
# ----------------------------------------------------------------------

@pytest.mark.parametrize("hostile", HOSTILE)
def test_login_post_failure_rerenders_sanitised_next(client, hostile):
    with patch("shared.auth.verify_login", return_value=(False, "bad creds", None)):
        resp = client.post(
            "/login",
            data={"email": "a@b.com", "password": "wrong", "next": hostile},
        )
    assert resp.status_code == 200
    fields = re.findall(rb'name="next"\s+value="([^"]*)"', resp.data)
    assert fields, "hidden next field missing"
    for value in fields:
        assert value == b"/", value
