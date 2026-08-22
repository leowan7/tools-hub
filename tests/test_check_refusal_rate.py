"""Guards for the /metrics consumer added by Phase 6.

``scripts/check_refusal_rate.py`` is the only thing that reads ``/metrics``.
It runs unattended every 6h and its whole job is to turn a number into an exit
code, so the parsing, the ratio and the noise floor are tested here rather than
discovered on a Sunday.

    pytest tests/test_check_refusal_rate.py -v
"""

from __future__ import annotations

import pytest

from scripts import check_refusal_rate
from scripts.check_refusal_rate import (
    DEFAULT_MIN_SAMPLES,
    DEFAULT_THRESHOLD,
    evaluate,
    parse_exposition,
    totals,
)

# A scrape in the shape prometheus_client actually renders one, including the
# HELP/TYPE lines, a _created gauge, and routes that are not Scout's.
EXPOSITION = """\
# HELP tools_hub_requests_total HTTP requests handled, by route and status class.
# TYPE tools_hub_requests_total counter
tools_hub_requests_total{route="scout.analyze",status_class="2xx"} 60.0
tools_hub_requests_total{route="scout.analyze",status_class="4xx"} 20.0
tools_hub_requests_total{route="scout.progress",status_class="2xx"} 20.0
tools_hub_requests_total{route="scout.index",status_class="2xx"} 900.0
tools_hub_requests_total{route="healthz",status_class="2xx"} 5000.0
tools_hub_requests_created{route="scout.analyze",status_class="2xx"} 1.7e+09
# HELP tools_hub_scout_refusals_total Anonymous Epitope Scout requests refused.
# TYPE tools_hub_scout_refusals_total counter
tools_hub_scout_refusals_total{reason="rate_limited",route="scout.analyze"} 4.0
tools_hub_scout_refusals_total{reason="rate_limited",route="scout.progress"} 2.0
tools_hub_scout_refusals_total{reason="job_expired",route="scout.progress"} 30.0
"""


def test_parse_reads_names_labels_and_values():
    samples = parse_exposition(EXPOSITION)
    assert (
        "tools_hub_scout_refusals_total",
        {"reason": "rate_limited", "route": "scout.analyze"},
        4.0,
    ) in samples


def test_parse_skips_comments_blanks_and_junk():
    samples = parse_exposition(
        "# HELP x y\n# TYPE x counter\n\n   \nnot a sample line at all!\nx_total 3\n"
    )
    assert samples == [("x_total", {}, 3.0)]


def test_parse_drops_non_finite_values():
    """NaN is legal exposition and meaningless as a denominator."""
    assert parse_exposition('x_total{a="b"} NaN\ny_total +Inf\n') == []


def test_totals_counts_only_the_metered_scout_routes():
    """The denominator must exclude page loads and /healthz.

    Folding those in dilutes the ratio until the outage stops showing: 900
    index hits alone would drop a 6% refusal share to 0.6%.
    """
    requests, _ = totals(parse_exposition(EXPOSITION))
    assert requests == 100.0  # 60 + 20 + 20; index and healthz excluded


def test_totals_sums_a_reason_across_routes():
    _, refusals = totals(parse_exposition(EXPOSITION))
    assert refusals["rate_limited"] == 6.0
    assert refusals["job_expired"] == 30.0


def test_a_healthy_sample_passes():
    code, lines = evaluate(1000.0, {"rate_limited": 10.0})
    assert code == 0
    assert any(line == "OK" for line in lines)


def test_over_the_threshold_fails_the_job():
    code, lines = evaluate(1000.0, {"rate_limited": 440.0})
    assert code == 1
    assert any("FAIL" in line for line in lines)
    assert any("44.0%" in line for line in lines)


def test_exactly_at_the_threshold_does_not_fire():
    """`>` not `>=`: a threshold that fires at its own value has no headroom."""
    code, _ = evaluate(1000.0, {"rate_limited": 1000.0 * DEFAULT_THRESHOLD})
    assert code == 0


def test_a_tiny_denominator_skips_instead_of_firing():
    """Counters reset on deploy. Right after one, 3 of 4 refused is noise.

    Without the floor this is a 75% refusal share and a failed job every time
    the app deploys shortly before the cron.
    """
    code, lines = evaluate(4.0, {"rate_limited": 3.0})
    assert code == 0
    assert any("SKIP" in line for line in lines)


def test_the_floor_stops_applying_at_the_floor():
    code, _ = evaluate(DEFAULT_MIN_SAMPLES, {"rate_limited": DEFAULT_MIN_SAMPLES})
    assert code == 1, "at the floor the threshold must apply, not skip"


def test_the_two_non_refusals_never_fire_the_alert():
    """bad_request and job_expired are not us saying no.

    A front end sending a malformed URL, or a reaper ahead of a user, must not
    read as a limiter refusing the institution.
    """
    code, lines = evaluate(
        1000.0, {"bad_request": 500.0, "job_expired": 400.0, "rate_limited": 1.0}
    )
    assert code == 0
    assert any("job_expired" in line and "reported only" in line for line in lines)


def test_a_reason_this_script_has_never_heard_of_counts_against_the_threshold():
    """Fail-safe direction: a reason added in scout/ratelimit.py after this
    file was written must land INSIDE the alert, not silently outside it."""
    code, lines = evaluate(1000.0, {"invented_in_phase_7": 900.0})
    assert code == 1
    assert any("invented_in_phase_7" in line for line in lines)


def test_zero_refusals_is_reported_not_crashed():
    code, lines = evaluate(1000.0, {})
    assert code == 0
    assert any("rate_limited" in line for line in lines)


# ---------------------------------------------------------------------------
# main() — the exit codes the workflow reads
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_fetch(monkeypatch):
    """Replace the HTTP call. Returns a setter for (status, body)."""
    box: dict = {"status": 200, "body": EXPOSITION}
    monkeypatch.setattr(
        check_refusal_rate, "fetch", lambda *a, **k: (box["status"], box["body"])
    )
    monkeypatch.setenv("METRICS_TOKEN", "scrape-me")
    monkeypatch.delenv("REFUSAL_RATE_THRESHOLD", raising=False)
    monkeypatch.delenv("REFUSAL_RATE_MIN_SAMPLES", raising=False)
    return box


def test_main_refuses_to_run_without_a_token(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "")
    assert check_refusal_rate.main([]) == 2


def test_main_fails_the_job_when_metrics_is_forbidden(stub_fetch):
    """DECIDED: a 403 FAILS rather than skips.

    403 means METRICS_TOKEN was dropped or rotated on the Railway side, which
    silently disables the only refusal-rate alarm there is. A monitor that
    goes quiet when its own plumbing breaks is the exact failure Phase 6 was
    written to stop. The counter-argument — noise from a transient edge 5xx —
    does not apply: this job already fails wholesale when Railway is
    unreachable, so no new class of false alarm is introduced.
    """
    stub_fetch["status"] = 403
    assert check_refusal_rate.main([]) == 1


def test_main_fails_on_a_200_with_an_empty_exposition(stub_fetch):
    """/metrics answering 200 with a zero-byte body has shipped here before
    (PROMETHEUS_MULTIPROC_DIR set after --preload imported the app). Reading
    that as 'no refusals' would be the quietest possible failure."""
    stub_fetch["body"] = ""
    assert check_refusal_rate.main([]) == 1


def test_main_fails_when_it_cannot_reach_the_host(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "scrape-me")

    def _boom(*_a, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr(check_refusal_rate, "fetch", _boom)
    assert check_refusal_rate.main([]) == 1


def test_main_passes_on_a_healthy_scrape(stub_fetch):
    # EXPOSITION is 6 refusals out of 100 metered requests = 6%.
    assert check_refusal_rate.main([]) == 0


def test_main_honours_the_threshold_override(stub_fetch, monkeypatch):
    monkeypatch.setenv("REFUSAL_RATE_THRESHOLD", "0.05")
    assert check_refusal_rate.main([]) == 1


def test_main_ignores_a_junk_threshold_rather_than_crashing(stub_fetch, monkeypatch):
    monkeypatch.setenv("REFUSAL_RATE_THRESHOLD", "twenty percent")
    assert check_refusal_rate.main([]) == 0


# ---------------------------------------------------------------------------
# Producer and consumer must agree
# ---------------------------------------------------------------------------


def test_it_reads_the_real_exposition_this_app_renders(monkeypatch):
    """Everything above feeds the parser a fixture I wrote. This one scrapes
    the app.

    The gap it closes: a typo in a metric name, a route label that is not the
    Flask endpoint name, or a rename on either side would leave every test
    above green while the deployed check silently measured 0 refusals out of 0
    requests forever. Nothing else in the repo compares the two ends.
    """
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    monkeypatch.setenv("METRICS_TOKEN", "scrape-me")
    from app import create_app  # noqa: PLC0415

    from scout import ratelimit  # noqa: PLC0415

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    ratelimit.reset()
    try:
        client = flask_app.test_client()
        # One real refusal on a real metered route, driven the way a browser
        # would: a /scout/progress call with no job_id.
        client.get("/scout/progress?chain=A").close()

        scrape = client.get(
            "/metrics", headers={"Authorization": "Bearer scrape-me"}
        )
        assert scrape.status_code == 200
        requests, refusals = totals(
            parse_exposition(scrape.data.decode("utf-8"))
        )
    finally:
        ratelimit.reset()

    assert requests >= 1, (
        "the denominator saw no scout.progress traffic — METERED_ROUTES no "
        "longer matches the Flask endpoint names in the route label"
    )
    assert refusals.get("bad_request", 0.0) >= 1, (
        "the refusal counter rendered nothing this script can read — the "
        "metric name or the reason label has drifted"
    )
