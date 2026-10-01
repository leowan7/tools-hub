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
    # tests/conftest.py sets. With enforcement off, the POST case below
    # passes while production answers 403 (app.py::_enforce_csrf defaults
    # the switch to "1"), so the status codes here would certify a
    # behaviour the live app never returns. The exemption that makes the
    # redirect reachable is app.py::_csrf_request_is_exempt.
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
