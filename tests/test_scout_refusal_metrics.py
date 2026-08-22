"""Every way Scout says no to an anonymous caller moves ONE counter.

Phase 6. Before this, "we refused an anonymous caller" left the app as three
different HTTP status codes and nothing counted it: ``REQUESTS_TOTAL`` labels
only ``(route, status_class)``, so the 429s, the 503s and — worst —
the ``text/event-stream`` refusals that answer **HTTP 200** all collapsed into
status classes, and the SSE ones collapsed into *successes*.

``test_the_sse_busy_refusal_is_counted_even_though_it_answers_200`` is the
headline: it asserts the refusal counter AND the 2xx request counter both move
on the same request, which is the whole reason a status-code-derived refusal
rate cannot work here.

Every test drives a real request through a Flask test client. The counter is
read as a DELTA — the prometheus registry is process-global and lives for the
whole session, so an absolute value would depend on which tests ran first.

    pytest tests/test_scout_refusal_metrics.py -v
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from scout import ratelimit
from scout import routes as scout_routes

BOGUS_JOB = "3f8e0c92-0000-4000-8000-abc"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture(autouse=True)
def _clean_windows():
    ratelimit.reset()
    yield
    ratelimit.reset()


def _refusals(reason: str, route: str) -> float:
    from prometheus_client import REGISTRY  # noqa: PLC0415

    value = REGISTRY.get_sample_value(
        "tools_hub_scout_refusals_total", {"reason": reason, "route": route}
    )
    return value if value is not None else 0.0


def _requests(route: str, status_class: str) -> float:
    from prometheus_client import REGISTRY  # noqa: PLC0415

    value = REGISTRY.get_sample_value(
        "tools_hub_requests_total", {"route": route, "status_class": status_class}
    )
    return value if value is not None else 0.0


def _client_with_session(app, label: str):
    """A client carrying its own anonymous session id.

    NOT the same as a bare ``app.test_client()``: that one sends no cookie and
    therefore lands in the shared no-session bucket, which is a different
    refusal reason.
    """
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[ratelimit.ANON_SESSION_KEY] = f"anon:{label}"
    return client


def _analyze(client, job_id=BOGUS_JOB, chain="A"):
    return client.post("/scout/analyze", json={"job_id": job_id, "chain": chain})


@contextmanager
def _no_slot():
    """Stand in for a full compute cap without waiting out the real queue."""
    yield False


@pytest.fixture
def shed(monkeypatch):
    monkeypatch.setattr(scout_routes, "anon_compute_slot", lambda *a, **k: _no_slot())


@pytest.fixture
def staged_job(monkeypatch):
    """Get past job resolution so the compute-slot branch is what answers."""
    monkeypatch.setattr(scout_routes, "_resolve_job_dir", lambda job_id: Path("."))
    monkeypatch.setattr(scout_routes, "_find_input_file", lambda job_dir: Path("input.pdb"))


# ---------------------------------------------------------------------------
# Site 1 — scout/ratelimit.py _refuse(), the single chokepoint for three reasons
# ---------------------------------------------------------------------------


def test_the_per_ip_refusal_is_counted(app):
    """A whole institution over the shared ceiling. The plan's outage."""
    # A fresh cookie each time so the tighter session tier never fires and the
    # refusal comes from the per-IP bucket.
    for i in range(scout_routes.ANON_ANALYZE_LIMIT):
        _analyze(_client_with_session(app, f"burn{i}"))

    before = _refusals("rate_limited", "scout.analyze")
    response = _analyze(_client_with_session(app, "victim"))

    assert response.status_code == 429
    assert response.get_json()["reason"] == "rate_limited"
    assert _refusals("rate_limited", "scout.analyze") == before + 1


def test_the_per_session_refusal_is_counted_separately(app):
    """One caller over their own allowance — the Phase 5 conversion moment,
    NOT an outage. A counter that merged it with the per-IP tier could not
    tell the two apart, which is the whole point of the reason label."""
    client = _client_with_session(app, "chatty")
    for _ in range(scout_routes.ANON_ANALYZE_SESSION_LIMIT):
        _analyze(client)

    before = _refusals("session_rate_limited", "scout.analyze")
    before_ip = _refusals("rate_limited", "scout.analyze")
    response = _analyze(client)

    assert response.status_code == 429
    assert response.get_json()["reason"] == "session_rate_limited"
    assert _refusals("session_rate_limited", "scout.analyze") == before + 1
    assert _refusals("rate_limited", "scout.analyze") == before_ip, (
        "the per-session refusal also moved the per-IP counter"
    )


def test_the_cookieless_refusal_is_counted(app):
    """Every caller with no session id shares one bucket, so this one fires
    for someone else's spending and signing in cannot help. It needs its own
    number for the same reason it needs its own message."""
    bare = app.test_client()
    for _ in range(scout_routes.ANON_ANALYZE_SESSION_LIMIT):
        _analyze(bare)

    before = _refusals("no_session", "scout.analyze")
    response = _analyze(app.test_client())

    assert response.status_code == 429
    assert response.get_json()["reason"] == "no_session"
    assert _refusals("no_session", "scout.analyze") == before + 1


# ---------------------------------------------------------------------------
# Sites 2 and 3 — scout/routes.py _anon_capacity_error(), both branches
# ---------------------------------------------------------------------------


def test_the_fleet_live_job_cap_is_counted(app, monkeypatch):
    monkeypatch.setattr(
        scout_routes, "count_job_dirs", lambda prefix: scout_routes.ANON_MAX_LIVE_JOBS
    )
    before = _refusals("at_capacity", "scout.example")

    response = app.test_client().get("/scout/example")

    assert response.status_code == 503
    assert response.get_json()["reason"] == "at_capacity"
    assert _refusals("at_capacity", "scout.example") == before + 1


def test_the_per_session_live_job_cap_is_counted(app, monkeypatch):
    """Same reason, different status code — 429, not 503. A refusal metric
    keyed on the status class would file these two apart; they are one thing."""
    monkeypatch.setattr(
        scout_routes,
        "count_job_dirs",
        lambda prefix: (
            0 if prefix == scout_routes.ANON_OWNER_PREFIX
            else scout_routes.ANON_MAX_LIVE_JOBS_PER_SESSION
        ),
    )
    client = _client_with_session(app, "hoarder")
    before = _refusals("at_capacity", "scout.example")

    response = client.get("/scout/example")

    assert response.status_code == 429
    assert response.get_json()["reason"] == "at_capacity"
    assert _refusals("at_capacity", "scout.example") == before + 1


# ---------------------------------------------------------------------------
# Site 4 — the JSON compute-slot shed on POST /scout/analyze
# ---------------------------------------------------------------------------


def test_the_json_busy_shed_is_counted(app, shed, staged_job):
    before = _refusals("busy", "scout.analyze")

    response = _analyze(app.test_client())

    assert response.status_code == 503
    assert response.get_json()["reason"] == "busy"
    assert _refusals("busy", "scout.analyze") == before + 1


# ---------------------------------------------------------------------------
# Site 5 — THE headline. The SSE compute-slot shed, which answers HTTP 200.
# ---------------------------------------------------------------------------


def test_the_sse_busy_refusal_is_counted_even_though_it_answers_200(
    app, shed, staged_job
):
    """The refusal a status-code metric structurally cannot see.

    EventSource cannot read a non-2xx body, so this refusal leaves as HTTP 200
    ``text/event-stream``. Both counters are asserted on the SAME request: the
    request counter files it as a SUCCESS while the refusal counter files it as
    a refusal. That contradiction is why the new counter exists.

    It also pins the generator question. The shed decision is made INSIDE the
    generator — ``anon_compute_slot`` is entered when the response starts being
    iterated — so the increment has to live there too. Hoisting it out of the
    generator would count a refusal that never happened; leaving it out of the
    code path entirely would move nothing at all, which is the silent failure
    Phase 6 exists to prevent.
    """
    before = _refusals("busy", "scout.progress")
    before_2xx = _requests("scout.progress", "2xx")

    response = app.test_client().get(f"/scout/progress?job_id={BOGUS_JOB}&chain=A")
    body = response.get_data(as_text=True)
    response.close()

    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert '"reason": "busy"' in body
    assert _requests("scout.progress", "2xx") == before_2xx + 1, (
        "this refusal is filed as a 2xx success by the status-code counter"
    )
    assert _refusals("busy", "scout.progress") == before + 1, (
        "the SSE shed did not move the refusal counter — the increment is "
        "either outside the generator's live path or absent"
    )


# ---------------------------------------------------------------------------
# Site 6 — the SSE error stream: bad_request and job_expired
# ---------------------------------------------------------------------------


def test_the_sse_bad_request_is_counted(app):
    before = _refusals("bad_request", "scout.progress")

    response = app.test_client().get("/scout/progress?chain=A")
    body = response.get_data(as_text=True)
    response.close()

    assert response.status_code == 200
    assert '"reason": "bad_request"' in body
    assert _refusals("bad_request", "scout.progress") == before + 1


def test_the_sse_expired_job_is_counted(app):
    """Same call site, other branch — the conditional that picks the reason is
    not duplicated, so this proves the increment follows it."""
    before = _refusals("job_expired", "scout.progress")

    response = app.test_client().get(f"/scout/progress?job_id={BOGUS_JOB}&chain=A")
    body = response.get_data(as_text=True)
    response.close()

    assert response.status_code == 200
    assert '"reason": "job_expired"' in body
    assert _refusals("job_expired", "scout.progress") == before + 1


# ---------------------------------------------------------------------------
# The counter must never break the app
# ---------------------------------------------------------------------------


def test_a_broken_counter_does_not_break_a_refusal(app, monkeypatch):
    """Metrics are observation, not policy. If the counter raises, the caller
    must still be refused correctly."""
    import shared.metrics as metrics

    class _Exploding:
        def labels(self, *_a, **_kw):
            raise RuntimeError("registry on fire")

    monkeypatch.setattr(metrics, "SCOUT_REFUSALS", _Exploding())

    response = app.test_client().get("/scout/progress?chain=A")
    body = response.get_data(as_text=True)
    response.close()

    assert response.status_code == 200
    assert '"reason": "bad_request"' in body


def test_the_refusal_counter_is_registered_with_both_labels():
    """Bounded cardinality is the reason this counter is safe to add: seven
    fixed reasons times the Scout routes, and nothing caller-controlled."""
    from shared.metrics import SCOUT_REFUSALS

    assert set(SCOUT_REFUSALS._labelnames) == {"reason", "route"}
