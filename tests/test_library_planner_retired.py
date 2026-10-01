"""The retired Yeast Display Library Planner 301s to /tools, and never 404s.

The tool was delisted from the catalog on 2026-08-17 (shared/tools_catalog.py)
and retired on 2026-09-30 at Leo's request. This file replaces
tests/test_library_planner_anonymous.py, which pinned the opposite property --
that both routes answered 200 to an anonymous visitor.

Why a 301 and not a 404: "try the tool" callouts in the ranomics.com marketing
site link at https://tools.ranomics.com/library-planner (grep that repo for
``library-planner``; it is a separate repository, so no count is asserted
here), and that path answered 200 up to this commit. A 404 would turn every
one of those into a dead end, so both endpoints stay registered and redirect
(blueprints/tools.py::library_planner, ::library_planner_plan).

The POST redirect is a 301 rather than a 308 on purpose: 308 preserves the
method, and a re-POST to /tools -- registered GET-only at
blueprints/tools.py::tools_comparison -- would answer 405.
"""

from __future__ import annotations

import pytest

from app import create_app

# ``app.py`` calls load_dotenv() at import and the repo-root .env carries
# real service-role credentials, so route tests can transact against
# production. Nothing below should reach a database -- this fixture makes
# that a guarantee rather than an expectation.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

# The body a stale bookmarked form or a blog reader's resubmit would send.
VALID_PLAN = {
    "scaffold": "VHH",
    "positions": "8",
    "scheme": "NNK",
    "kd_nm": "10",
    "starting_material": "naive",
    "coverage_pct": "90",
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    # Production default, not the suite-wide CSRF_PROTECT=0 that
    # tests/conftest.py sets (app.py::_enforce_csrf defaults the switch to
    # "1"). With enforcement off, the anonymous POST below passes whether or
    # not the path is exempt, so the status codes here would not be the ones
    # the live app returns. What makes the POST reachable under enforcement
    # is app.py::_csrf_request_is_exempt; why it needs to be exempt, and the
    # control showing a non-exempt POST is refused the same way, are in
    # tests/test_csrf_protection.py::test_retired_library_planner_post_is_exempt.
    monkeypatch.setenv("CSRF_PROTECT", "1")
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.mark.parametrize(
    "method, path, data",
    [
        ("GET", "/library-planner", None),
        ("POST", "/library-planner/plan", dict(VALID_PLAN)),
    ],
)
def test_retired_routes_301_to_tools(client, method, path, data):
    """Both endpoints answer 301 -> /tools, anonymously, with no body work."""
    resp = client.open(path, method=method, data=data)
    assert resp.status_code == 301, (
        f"anonymous {method} {path} returned {resp.status_code}, not 301. "
        "A 404 strands the ranomics.com callouts; a 200 means the retired "
        "planner is serving again; a 302 is not the permanent signal search "
        "engines need to move the URL."
    )
    assert resp.headers["Location"].endswith("/tools"), (
        f"{method} {path} redirected to {resp.headers['Location']!r}, "
        "not /tools"
    )


def test_following_the_redirect_lands_on_a_200_catalog(client):
    """The GET redirect resolves, so a blog reader is not left on an error."""
    resp = client.get("/library-planner", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.request.path == "/tools"


def test_post_redirect_is_followed_as_a_get(client):
    """A 301 is downgraded to GET, so the stale form body lands on /tools.

    This is the behaviour a 308 would break: /tools takes GET only
    (blueprints/tools.py::tools_comparison), so a method-preserving redirect
    would answer 405 to anyone resubmitting a bookmarked plan.
    """
    resp = client.post(
        "/library-planner/plan",
        data=dict(VALID_PLAN),
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert resp.request.path == "/tools"
    assert resp.request.method == "GET"


@pytest.mark.parametrize(
    "method, path, data",
    [
        ("GET", "/library-planner", None),
        ("POST", "/library-planner/plan", dict(VALID_PLAN)),
    ],
)
def test_query_string_is_carried_across(client, method, path, data):
    """Parameters survive the redirect, so an inbound click is attributable.

    A bare redirect would strip them and land every arrival on /tools as
    direct traffic. Two consequences, one of which needs no other
    repository: tools_comparison reads ``asked``, ``have`` and ``shape`` off
    request.args, so a parameterised catalog link through this path would be
    truncated on arrival. The other is attribution for the marketing site's
    inbound links, which are reported to be UTM-decorated at render; that
    report is not reproducible from this repo, and the parameters below
    stand in for it rather than asserting it. Same behaviour as
    blueprints/tools.py::proteinmpnn_slug_redirect, which carries the query
    string for the same reason.
    """
    resp = client.open(
        f"{path}?utm_source=blog&utm_campaign=library-planner&asked=binder",
        method=method,
        data=data,
    )

    assert resp.status_code == 301
    assert resp.headers["Location"].endswith(
        "/tools?utm_source=blog&utm_campaign=library-planner&asked=binder"
    ), (
        f"{method} {path} redirected to {resp.headers['Location']!r}: the "
        "query string was dropped, so an inbound click arrives as direct "
        "traffic and a parameterised catalog link is truncated"
    )


def test_malformed_query_string_still_redirects(client):
    """A non-UTF-8 byte must not turn the redirect into a 500.

    WSGI hands QUERY_STRING over as a latin-1 str, so a raw 0xFF byte in the
    request line reaches the redirect undecoded. Strict decoding would raise
    UnicodeDecodeError inside the view; errors="replace" in
    blueprints/tools.py::_retired_planner_redirect is what keeps this a 301.
    Copied from tests/test_tool_slug_redirects.py, which pins the same
    property on the sibling redirect.
    """
    resp = client.get(
        "/library-planner",
        environ_overrides={"QUERY_STRING": "utm_source=" + chr(0xFF)},
    )

    assert resp.status_code == 301
    assert resp.headers["Location"].startswith("/tools?utm_source=")


def test_the_planner_is_no_longer_offered_anywhere_reachable(client):
    """No reachable page links or names the retired tool.

    Guards the surfaces this retirement had to clean: the /tools hero copy
    (templates/tools/comparison.html), the sitemap
    (blueprints/public.py::sitemap_xml) and the site-wide default meta
    description (templates/base.html). Scoped to pages an anonymous visitor
    can load; the signed-in copy on templates/wallet/topup.html and
    templates/jobs_list.html, and the signup-credit email body, are not
    reachable from here.
    """
    for path in ("/tools", "/sitemap.xml", "/"):
        body = client.get(path).get_data(as_text=True).lower()
        assert "library-planner" not in body, (
            f"{path} still links the retired Library Planner"
        )
        assert "library plan" not in body, (
            f"{path} still offers the retired Library Planner by name"
        )


def test_planning_writes_nothing(client):
    """A redirect reaches no database.

    Two layers make this an assertion rather than a hope: the module-level
    ``isolate_supabase`` mark blanks the credentials so a client build
    returns None (tests/conftest.py::isolate_supabase), and the session-wide
    ``_forbid_real_supabase_clients`` guard raises
    ``_RealSupabaseClientRefused`` -- a ``BaseException``, so no in-app
    ``except Exception`` swallows it -- if one is built anyway. A handler
    re-added here that logged the retired hit would surface as a failure in
    this test, not as a silent production write.
    """
    resp = client.post("/library-planner/plan", data=dict(VALID_PLAN))
    assert resp.status_code == 301
