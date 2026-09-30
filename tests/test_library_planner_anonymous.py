"""The Yeast Display Library Planner is reachable without an account.

Ten posts on ranomics.com embed a "try the tool" callout pointing at
https://tools.ranomics.com/library-planner (grep the marketing repo's
``src/content/blog/*.mdx`` for ``tool="library-planner"``). Both routes
carried ``@login_required``, so every reader arriving from a blog post
was bounced to /login?next=/library-planner.

Safe to open because the handler spends nothing and persists nothing:
``plan_library`` is pure arithmetic over the posted form
(tools/library_planner/planner.py::plan_library) with no GPU call, no wallet
charge, no job row and no storage write. The same reasoning opened
/developability (blueprints/tools.py::developability) and these tests mirror
tests/test_developability_anonymous.py.

Both halves are pinned: the routes answer anonymously, AND the input
validation that bounds what an anonymous request can ask for
(tools/library_planner/planner.py:_validate_inputs, plus the 40-position
cap in the route) still rejects.
"""

from __future__ import annotations

import pytest

from app import create_app

# ``app.py`` calls load_dotenv() at import and the repo-root .env carries
# real service-role credentials, so route tests can transact against
# production. Nothing below should reach a database -- this fixture makes
# that a guarantee rather than an expectation.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

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
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_get_is_anonymous(client):
    """No session, no redirect."""
    resp = client.get("/library-planner")
    assert resp.status_code == 200, (
        f"anonymous GET /library-planner returned {resp.status_code}; "
        "a 302 means the login gate is back and every blog-referred "
        "reader hits /login again"
    )


def test_post_plans_anonymously(client):
    """Opening the form but gating submit is the promise-not-kept case."""
    resp = client.post("/library-planner/plan", data=dict(VALID_PLAN))
    assert resp.status_code == 200, (
        f"anonymous POST /library-planner/plan returned {resp.status_code}"
    )
    assert "login" not in resp.request.path


def test_post_offers_macs_for_a_naive_library(client):
    """The MACS pre-enrichment round has to be reachable through the route.

    VALID_PLAN is 8 NNK positions on naive material, ~8.5e11 stop-free
    variants, which is four orders above the ``library_size > 1e8`` MACS
    trigger in
    ``tools/library_planner/sort_strategy.py::recommend_sort_rounds``. The
    route used to
    hand that test a size already capped at the 1e8 transformation ceiling,
    so the trigger could never fire and every plan the web tool returned was
    three FACS rounds.

    ``None%`` catches the other half: a MACS round carries
    ``gate_percent: None`` (bulk magnetic capture has no sort gate), and
    templates/library_planner_results.html prints the gate in two places.
    """
    resp = client.post("/library-planner/plan", data=dict(VALID_PLAN))
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "MACS" in body
    assert "Round 1 &mdash; MACS" in body, (
        "the plan the route rendered does not open on a MACS round"
    )
    assert "None%" not in body, (
        "a MACS round has no sort gate; the results page printed the "
        "missing gate_percent verbatim"
    )


# --- the trust boundary that makes anonymous access safe ------------------
# These bound the work an anonymous request can ask for. If any of these
# stops rejecting, opening the routes stops being safe.


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"positions": "41"}, "above the 40-position cap"),
        ({"positions": "0"}, "below the 1-position floor"),
        ({"positions": "abc"}, "non-numeric positions"),
        ({"kd_nm": "0"}, "non-positive KD"),
        ({"kd_nm": "abc"}, "non-numeric KD"),
        ({"scaffold": "../../etc/passwd"}, "scaffold outside the allowlist"),
        ({"scheme": "NNZ"}, "codon scheme outside the allowlist"),
        ({"starting_material": "whatever"}, "starting material outside the allowlist"),
    ],
)
def test_invalid_input_is_rejected(client, override, reason):
    data = dict(VALID_PLAN)
    data.update(override)
    resp = client.post("/library-planner/plan", data=data)
    assert resp.status_code == 200, f"expected a re-rendered form for {reason}"
    body = resp.get_data(as_text=True).lower()
    assert "error" in body or "must be" in body or "valid:" in body, (
        f"input {reason} was not rejected -- these bounds are what keep the "
        "anonymous cost of a request fixed"
    )


def test_planning_writes_nothing(monkeypatch, client):
    """The claim that justifies anonymous access: no persistence.

    If a future change adds a DB write here, it needs an identity and
    these routes should not have been left open.
    """
    import shared.credits as credits

    def _boom(*a, **k):
        raise AssertionError(
            "library planning reached the database; it is supposed to be "
            "pure arithmetic, which is why the routes are anonymous"
        )

    monkeypatch.setattr(credits, "get_service_client", _boom, raising=False)
    resp = client.post("/library-planner/plan", data=dict(VALID_PLAN))
    assert resp.status_code == 200
