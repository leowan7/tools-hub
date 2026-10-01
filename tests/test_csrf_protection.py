"""FIX M2 (cso audit 2026-06-17, second half): app-wide CSRF protection.

The session cookie authenticates the entire web UI. Before this fix, only
/account/api-keys/* carried a CSRF token; every other authenticated POST
(wallet, account, admin, jobs, tools, campaigns, workspaces) had none.

create_app() now installs a before_request guard that rejects any state-
changing request on a non-exempt main-app route unless it presents the
per-session token (form field ``_csrf`` or header ``X-CSRF-Token``).

    pytest tests/test_csrf_protection.py -v
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    # conftest disables CSRF process-wide for the suite; this suite is the
    # one place that must exercise it, so force enforcement back on.
    monkeypatch.setenv("CSRF_PROTECT", "1")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


_TOKEN = "test-csrf-token-abc123"


def _seed_session(client, *, with_token=True):
    with client.session_transaction() as sess:
        sess["user_email"] = "leowan7@gmail.com"
        sess["user_id"] = "u-test"
        if with_token:
            sess["_csrf_token"] = _TOKEN


# ---------------------------------------------------------------------------
# Enforcement: missing / wrong token is rejected on a representative route
# ---------------------------------------------------------------------------


def test_post_without_token_is_rejected(app):
    client = app.test_client()
    _seed_session(client, with_token=False)
    resp = client.post("/lab-projects/submit", data={"source_job_id": "x"})
    assert resp.status_code == 403
    assert b"CSRF" in resp.data


def test_post_with_wrong_token_is_rejected(app):
    client = app.test_client()
    _seed_session(client, with_token=True)
    resp = client.post(
        "/lab-projects/submit", data={"_csrf": "not-the-token", "source_job_id": "x"}
    )
    assert resp.status_code == 403
    assert b"CSRF" in resp.data


def test_post_with_valid_form_token_passes_csrf(app):
    """A matching ``_csrf`` form field clears the CSRF gate. The downstream
    view may still redirect / 400, but it must NOT be the CSRF 403."""
    client = app.test_client()
    _seed_session(client, with_token=True)
    resp = client.post(
        "/lab-projects/submit", data={"_csrf": _TOKEN, "source_job_id": "x"}
    )
    assert resp.status_code != 403


def test_post_with_valid_header_token_passes_csrf(app):
    """AJAX callers present the token via X-CSRF-Token. Exercised against a
    real fetch-driven route (/jobs/<id>/cancel)."""
    client = app.test_client()
    _seed_session(client, with_token=True)
    resp = client.post(
        f"/jobs/{uuid.uuid4()}/cancel", headers={"X-CSRF-Token": _TOKEN}
    )
    assert resp.status_code != 403


def test_ajax_cancel_without_token_is_rejected(app):
    client = app.test_client()
    _seed_session(client, with_token=True)
    resp = client.post(f"/jobs/{uuid.uuid4()}/cancel")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET is never gated; the token is delivered to the page
# ---------------------------------------------------------------------------


def test_get_is_not_blocked_and_renders_token(app):
    client = app.test_client()
    resp = client.get("/login")
    assert resp.status_code == 200
    # Hidden field injected by csrf_input() into every form ...
    assert b'name="_csrf"' in resp.data
    # ... and the meta tag csrf_meta_value() emits for fetch/XHR callers.
    assert b'name="csrf-token"' in resp.data


# ---------------------------------------------------------------------------
# Exempt surfaces: server-to-server + the anonymous beacon are NOT gated
# ---------------------------------------------------------------------------


def test_analytics_beacon_is_exempt(app):
    """/api/track is the unauthenticated sendBeacon endpoint — exempt, so a
    tokenless POST must succeed (204), not 403."""
    client = app.test_client()
    resp = client.post("/api/track", json={"event_type": "unit_test"})
    assert resp.status_code != 403
    assert resp.status_code == 204


def test_webhook_ingress_is_exempt(app):
    """Server-to-server webhook ingress has its own token/HMAC and must not
    be subject to the cookie-CSRF guard (would break Stripe/Modal)."""
    client = app.test_client()
    resp = client.post("/webhooks/heartbeat", json={"job_id": "nope"})
    # Whatever the handler decides (400/200/etc.), it must not be the CSRF 403.
    assert resp.status_code != 403


def test_preflight_is_exempt(app):
    """Side-effect-free validation endpoint is exempt (fetch sends fresh
    FormData with no token)."""
    client = app.test_client()
    _seed_session(client, with_token=True)
    resp = client.post("/tools/mpnn/preflight", data={})
    assert resp.status_code != 403


@pytest.mark.parametrize(
    "label, session_token, sent_token",
    [
        # The planner was anonymous-open, so a reader following a cached copy
        # of the form has no session token at all.
        ("anonymous, no token either side", None, None),
        # A returning reader has a session token, but the one baked into their
        # cached copy of the form belongs to an older session.
        ("session rotated since the form was cached", "fresh", "stale"),
    ],
)
def test_retired_library_planner_post_is_exempt(app, label, session_token, sent_token):
    """The retired planner's POST is a bare 301 and must not 403.

    Its form DOES carry ``csrf_input()``
    (templates/library_planner_form.html), but the form is no longer served --
    the GET half 301s as well (blueprints/tools.py::library_planner) -- so no
    caller can obtain a token matching the current session. Both replay cases
    below are refused without the exemption in
    app.py::_csrf_request_is_exempt, which
    test_a_non_exempt_post_403s_under_the_same_conditions pins as the control.
    The rest of the retirement lives in tests/test_library_planner_retired.py.
    """
    client = app.test_client()
    if session_token is not None:
        with client.session_transaction() as sess:
            sess["_csrf_token"] = session_token
    data = {"scaffold": "VHH"}
    if sent_token is not None:
        data["_csrf"] = sent_token
    resp = client.post("/library-planner/plan", data=data)
    assert resp.status_code == 301, f"{label} was answered {resp.status_code}"
    assert resp.headers["Location"].endswith("/tools")


@pytest.mark.parametrize(
    "session_token, sent_token",
    [(None, None), ("fresh", "stale")],
)
def test_a_non_exempt_post_403s_under_the_same_conditions(
    app, session_token, sent_token
):
    """Control for the exemption above: the same two replay shapes are 403ed.

    Without this, the 301s above could be passing because the guard never
    fires at all rather than because the path is exempt.
    """
    client = app.test_client()
    if session_token is not None:
        with client.session_transaction() as sess:
            sess["_csrf_token"] = session_token
    data = {"source_job_id": "x"}
    if sent_token is not None:
        data["_csrf"] = sent_token
    assert client.post("/lab-projects/submit", data=data).status_code == 403


def test_the_path_alone_does_not_exempt_a_real_handler(app):
    """Re-point the path at a real handler: the exemption must NOT follow it.

    This is the test that makes the fail-closed claim in
    app.py::_csrf_request_is_exempt enforced rather than merely intended.
    review-claims found the gap by reverting the arm to ``path ==
    "/library-planner/plan"`` and observing that every test in this file and
    in tests/test_library_planner_retired.py still passed -- the old,
    path-keyed arm was indistinguishable from the endpoint-keyed one, so a
    revert to it would have gone unnoticed.

    Here the rule for that path is re-pointed at a handler that writes, which
    is what a future un-retirement looks like. Keyed on the endpoint, the
    exemption stops applying and the tokenless POST is refused. Keyed on the
    path it would still apply, the guard would wave the request through, and
    the handler below would run -- which is the assertion that fails.
    """
    rule = next(r for r in app.url_map.iter_rules() if r.rule == "/library-planner/plan")
    reached = []

    def _a_real_handler():  # pragma: no cover - CSRF must block it
        reached.append(True)
        return "wrote something", 200

    rule.endpoint = "tools.library_planner_plan_v2"
    app.view_functions["tools.library_planner_plan_v2"] = _a_real_handler
    # werkzeug indexes rules by endpoint for url_for and consults that index
    # while matching, so re-pointing the rule alone raises KeyError. Flask
    # exposes no public way to re-point or remove a rule, hence the private
    # dict; the alternative is a second create_app() variant in the app
    # factory purely for this test.
    app.url_map._rules_by_endpoint.setdefault(rule.endpoint, []).append(rule)

    resp = app.test_client().post("/library-planner/plan", data={"scaffold": "VHH"})
    assert resp.status_code == 403, (
        "a real handler on the retired path was CSRF-exempt: the exemption in "
        "app.py::_csrf_request_is_exempt is keying on the path, not the endpoint"
    )
    assert not reached, "the handler ran despite the CSRF guard"


def test_one_rule_serves_the_path_and_it_is_the_exempted_endpoint(app):
    """The exempted endpoint owns that path alone, and is still the redirect.

    Named for what it asserts and nothing more: exactly one rule serves
    /library-planner/plan, and its endpoint is the one app.py exempts. It was
    called test_the_exemption_is_keyed_on_the_endpoint, which overclaimed --
    it passes with the arm keyed on either the path or the endpoint. The
    keying itself is pinned by
    test_the_path_alone_does_not_exempt_a_real_handler above. A real handler
    smuggled in under the same endpoint name would instead be caught by the
    301 assertion in test_retired_library_planner_post_is_exempt.
    """
    rules = [r for r in app.url_map.iter_rules() if r.rule == "/library-planner/plan"]
    assert [r.endpoint for r in rules] == ["tools.library_planner_plan"]


# ---------------------------------------------------------------------------
# Unmatched routes 404 (not 403) — the guard defers to Flask routing
# ---------------------------------------------------------------------------


def test_unmatched_post_route_404s_not_403(app):
    client = app.test_client()
    _seed_session(client, with_token=False)
    resp = client.post("/this/route/does/not/exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Blueprint allowlist: a NON-allowlisted blueprint's POST stays CSRF-enforced
# (guards the app.py -> blueprints refactor from silently dropping CSRF)
# ---------------------------------------------------------------------------


def test_non_allowlisted_blueprint_post_is_enforced(app):
    """The CSRF exemption is an allowlist ({scout, platform_api}), not a blanket
    'any blueprint route is exempt' pass. When the cookie-authenticated web UI
    (login, wallet, tools, jobs, admin) moves into blueprints, those
    state-changing POSTs MUST stay CSRF-enforced. Register a fresh cookie-UI
    blueprint and confirm a tokenless POST is rejected."""
    from flask import Blueprint

    bp = Blueprint("dummy_ui", __name__)

    @bp.route("/dummy-ui/save", methods=["POST"])
    def _save():  # pragma: no cover - CSRF blocks it before the body runs
        return "saved", 200

    app.register_blueprint(bp)
    client = app.test_client()
    _seed_session(client, with_token=False)
    resp = client.post("/dummy-ui/save", data={})
    assert resp.status_code == 403
    assert b"CSRF" in resp.data


# ---------------------------------------------------------------------------
# Scout: the handoff form is enforced; the fetch() POSTs stay exempt
# (QA 2026-09-30 P2-3)
# ---------------------------------------------------------------------------


def test_scout_handoff_requires_token(app):
    client = app.test_client()
    _seed_session(client, with_token=False)
    resp = client.post("/scout/handoff/tool", data={"tool": "bindcraft"})
    assert resp.status_code == 403
    assert b"CSRF" in resp.data


def test_scout_handoff_with_token_passes_csrf(app):
    client = app.test_client()
    _seed_session(client, with_token=True)
    resp = client.post(
        "/scout/handoff/tool", data={"_csrf": _TOKEN, "tool": "bindcraft"}
    )
    assert resp.status_code != 403


def test_scout_fetch_post_stays_exempt(app):
    client = app.test_client()
    _seed_session(client, with_token=False)
    resp = client.post("/scout/fetch-pdb", data={})
    assert resp.status_code != 403
