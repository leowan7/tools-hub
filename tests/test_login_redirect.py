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


# ----------------------------------------------------------------------
# /signup carries ``next`` too (Phase 5 of the anonymous rate-limiting
# work). Scout's rate-limit refusal links a bounced visitor to
# /login?next=/scout so they resume where they were; both panels of
# login.html live on one page and switchTab() only toggles CSS, so the
# signup form has to carry the value or clicking "Create account" drops
# it silently.
#
# That gives signup the SAME open-redirect surface login already had, so
# it gets the same corpus rather than a fresh, thinner one. These would
# all have passed before the change too — signup pinned next to "/" — so
# they are guarding the new plumbing, not proving it exists. The
# preserves-internal cases are the ones that would go red on a revert.
# ----------------------------------------------------------------------

def _next_fields(payload: bytes) -> list[bytes]:
    return re.findall(rb'name="next"\s+value="([^"]*)"', payload)


@pytest.mark.parametrize("hostile", HOSTILE)
def test_signup_get_sanitises_next(client, hostile):
    resp = client.get("/signup", query_string={"next": hostile})
    assert resp.status_code == 200
    fields = _next_fields(resp.data)
    assert fields, "hidden next field missing"
    for value in fields:
        assert value == b"/", value


@pytest.mark.parametrize("safe", SAFE)
def test_signup_get_preserves_internal_next(client, safe):
    """The half that actually exercises the change: an internal path has to
    survive to the form, or the visitor still lands on "/" and restarts."""
    from html import unescape

    resp = client.get("/signup", query_string={"next": safe})
    assert resp.status_code == 200
    fields = _next_fields(resp.data)
    assert fields, "hidden next field missing"
    for value in fields:
        assert unescape(value.decode()) == safe, value


@pytest.mark.parametrize("hostile", HOSTILE)
def test_signup_post_failure_rerenders_sanitised_next(client, hostile):
    """The POST re-render path, same property as login's."""
    resp = client.post(
        "/signup",
        data={"email": "", "password": "", "password2": "", "next": hostile},
    )
    assert resp.status_code == 200
    for value in _next_fields(resp.data):
        assert value == b"/", value


def test_both_login_panels_carry_next(client):
    """Sign-in and Create-account are two panels of ONE page. If only the
    sign-in form carries next, a bounced visitor who clicks "Create account"
    loses where they were going and lands on "/" after signing up."""
    resp = client.get("/login", query_string={"next": "/scout"})
    assert resp.status_code == 200
    fields = _next_fields(resp.data)
    assert fields.count(b"/scout") == 2, (
        f"expected the value in BOTH panels, found {fields!r}"
    )


# ----------------------------------------------------------------------
# signup() reads ``next`` from request.values, which MERGES the query
# string and the form body — and Werkzeug resolves that merge with the
# query string FIRST. So an attacker-supplied query param beats the
# form field the page itself rendered. safe_next runs on the result
# either way, which is why this is safe; QC found it guarded but
# untested, and an untested guard is one refactor from being no guard.
# ----------------------------------------------------------------------

@pytest.mark.parametrize("hostile", HOSTILE)
def test_signup_query_string_cannot_smuggle_an_offsite_next_past_the_form(
    client, hostile
):
    resp = client.post(
        "/signup",
        query_string={"next": hostile},
        data={"email": "", "password": "", "password2": "", "next": "/jobs"},
    )
    assert resp.status_code == 200
    for value in _next_fields(resp.data):
        assert value == b"/", (
            f"a query-string next survived to the form: {value!r}"
        )


def test_which_source_wins_is_pinned_so_a_refactor_has_to_notice():
    """Not a security property — a documentation one. Both sources are
    same-origin-validated, so either winning is safe; this exists so that if
    the precedence ever flips, somebody reads the comment above."""
    from blueprints.auth import safe_next
    from werkzeug.datastructures import CombinedMultiDict, MultiDict

    values = CombinedMultiDict([MultiDict([("next", "/query")]),
                                MultiDict([("next", "/form")])])
    assert safe_next(values.get("next")) == "/query"
