"""Guards for the /metrics consumer added by Phase 6.

``scripts/check_refusal_rate.py`` is the only thing that reads ``/metrics``.
It runs unattended every 6h and its whole job is to turn a number into an exit
code, so the parsing, the ratio and the noise floor are tested here rather than
discovered on a Sunday.

    pytest tests/test_check_refusal_rate.py -v
"""

from __future__ import annotations

from pathlib import Path

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


def test_a_skip_that_swallowed_refusals_says_so():
    """A SKIP exits 0 and renders exactly like a healthy run.

    That is fine on a quiet fleet and misleading the moment something was
    actually refused: the refusals are real, only the denominator is missing.
    Since #189 the per-IP tier binds, so this is the shape a NAT lab hitting
    the shared ceiling takes at low traffic -- refused for real, reported as
    SKIP, rendered green. The marker is what the workflow raises to a run
    annotation.
    """
    code, lines = evaluate(4.0, {"rate_limited": 3.0})
    assert code == 0, "still reported-only; a fail path here can block deploys"
    assert any("UNRATED REFUSALS:" in line for line in lines)
    assert any("3 refusal(s)" in line for line in lines)


def test_a_quiet_skip_stays_quiet():
    """No refusals below the floor is the ordinary quiet fleet. Not worth an
    annotation every six hours -- a warning that always fires is decoration,
    and this repo has enough detectors that certify nothing."""
    code, lines = evaluate(4.0, {})
    assert code == 0
    assert any("SKIP" in line for line in lines)
    assert not any("UNRATED REFUSALS:" in line for line in lines)


def test_the_marker_is_only_for_the_unratable_case():
    """Above the floor the ratio speaks for itself, so the marker must not
    appear -- otherwise the workflow annotates runs that DID evaluate."""
    for requests, refusals in (
        (DEFAULT_MIN_SAMPLES, {"rate_limited": 1.0}),          # rated, passes
        (DEFAULT_MIN_SAMPLES, {"rate_limited": DEFAULT_MIN_SAMPLES}),  # rated, fails
    ):
        _, lines = evaluate(requests, refusals)
        assert not any("UNRATED REFUSALS:" in line for line in lines), (
            f"marker leaked into a rated run ({requests}, {refusals})"
        )


def test_info_only_reasons_do_not_trigger_the_marker():
    """bad_request and job_expired are not the limiter saying no. They are
    excluded from `refused`, so a skip full of them is still a quiet skip --
    the same rule the alert itself already follows."""
    code, lines = evaluate(4.0, {"bad_request": 3.0, "job_expired": 1.0})
    assert code == 0
    assert not any("UNRATED REFUSALS:" in line for line in lines)


def test_the_unrated_marker_never_changes_the_exit_code():
    """The invariant, tested in BOTH directions.

    A one-directional check from an exit-0 baseline can see a pass turn into a
    fail but is structurally blind to the reverse -- the marker swallowing a
    real threshold breach and presenting green.
    """
    scenarios = (
        # (requests, refusals, expected_code, why)
        (4.0, {"rate_limited": 3.0}, 0, "below floor with refusals: still skips"),
        (4.0, {}, 0, "below floor, quiet"),
        (
            DEFAULT_MIN_SAMPLES,
            {"rate_limited": DEFAULT_MIN_SAMPLES},
            1,
            "above floor and over threshold: must STILL fail",
        ),
        (
            1000.0,
            {"rate_limited": 1.0},
            0,
            "above floor and under threshold: must STILL pass",
        ),
    )
    for requests, refusals, expected, why in scenarios:
        code, _ = evaluate(requests, refusals)
        assert code == expected, why


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
        client.get(
            "/scout/progress?chain=A", headers={"X-Real-Ip": "198.51.100.9"}
        ).close()

        scrape = client.get(
            "/metrics", headers={"Authorization": "Bearer scrape-me"}
        )
        assert scrape.status_code == 200
        samples = parse_exposition(scrape.data.decode("utf-8"))
        requests, refusals = totals(samples)

        # Same gap, second producer/consumer pair: a rename on either end of
        # tools_hub_client_ip_source_total leaves ip_sources() returning {},
        # which makes the whole source block VANISH from the report with no
        # message. A detector that disappears looks like a healthy one.
        assert check_refusal_rate.ip_sources(samples).get("x_real_ip", 0.0) >= 1, (
            "the app renders no x_real_ip sample the consumer can read - the "
            "two ends of tools_hub_client_ip_source_total have drifted"
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


# ---------------------------------------------------------------------------
# The rate-limit key's source: reported, never alerted on
# ---------------------------------------------------------------------------


def test_ip_sources_sums_the_family():
    samples = check_refusal_rate.parse_exposition(
        'tools_hub_client_ip_source_total{source="x_real_ip"} 90.0\n'
        'tools_hub_client_ip_source_total{source="forwarded_chain"} 8.0\n'
        'tools_hub_client_ip_source_total{source="peer"} 2.0\n'
        'tools_hub_requests_total{route="scout.upload"} 100.0\n'
    )
    assert check_refusal_rate.ip_sources(samples) == {
        "x_real_ip": 90.0,
        "forwarded_chain": 8.0,
        "peer": 2.0,
    }


def test_the_source_block_never_changes_the_exit_code():
    """THE invariant. This is reported-only by design.

    A new way for the 6-hourly smoke to fail reddens main's check suite, which
    blocks same-commit Railway deploys (ALERTING.md, "A variable change is not
    deploying"). Whatever the sources say, the exit code must come from the
    refusal ratio alone.
    """
    scenarios = (
        ("passing", 200.0, {"rate_limited": 1.0}),
        # The direction the first version of this test could not see. A block
        # returning early on a healthy-looking split would SWALLOW a real
        # refusal-rate failure and present as green - silently disabling the
        # alarm this whole script exists for.
        ("failing", 200.0, {"rate_limited": 100.0}),
        ("below the sample floor", 5.0, {"rate_limited": 4.0}),
        ("zero requests", 0.0, {}),
    )
    for label, requests, refusals in scenarios:
        baseline, _ = check_refusal_rate.evaluate(requests, refusals)
        for sources in (
            None,
            {},
            {"x_real_ip": 200.0},
            {"peer": 200.0},                              # mass-refusal mode
            {"forwarded_chain": 199.0, "x_real_ip": 1.0},  # inert mode
            {"x_real_ip_rejected": 200.0},
            {"totally_unknown": 200.0},
            # Sums to ZERO. Without the guard the share arithmetic raises
            # ZeroDivisionError, and an exception fails the job just as
            # surely as a non-zero exit code does.
            {"x_real_ip": 0.0},
            {"x_real_ip": 0.0, "peer": 0.0},
        ):
            code, _ = check_refusal_rate.evaluate(
                requests, refusals, sources=sources
            )
            assert code == baseline, (
                f"{label}: sources={sources} moved the exit code "
                f"{baseline} -> {code}"
            )
    # And the failing scenario must really fail, or the loop proves nothing in
    # the dangerous direction.
    assert check_refusal_rate.evaluate(200.0, {"rate_limited": 100.0})[0] == 1


def test_a_healthy_source_split_reports_without_the_warning():
    _, lines = check_refusal_rate.evaluate(
        200.0, {}, sources={"x_real_ip": 200.0}
    )
    body = "\n".join(lines)
    assert "rate-limit key RESOLUTIONS" in body
    # These are resolutions, not requests: a paired route resolves twice and
    # signed-in traffic resolves zero times, so the header must not let a
    # reader divide these counts against the metered-request line above.
    assert "not request counts" in body
    assert "x_real_ip" in body
    assert "NOTE:" not in body


def test_the_note_distinguishes_inert_from_mass_refusal():
    """One threshold, two OPPOSITE failures. The note must not conflate them.

    forwarded_chain -> the key is a rotating internal hop, so the limiter
    bounds NOBODY: symptom is no refusals. peer -> the key is Railway's shared
    edge address, so every visitor collapses onto one bucket: symptom is mass
    refusals of legitimate users. An operator told the first during the second
    goes hunting an open limiter in the middle of a lockout.
    """
    _, inert = check_refusal_rate.evaluate(
        200.0, {}, sources={"forwarded_chain": 180.0, "x_real_ip": 20.0}
    )
    inert_body = str(inert)
    assert "INERT" in inert_body and "refusing nobody" in inert_body
    assert "mass refusals" not in inert_body

    _, lockout = check_refusal_rate.evaluate(
        200.0, {}, sources={"peer": 180.0, "x_real_ip": 20.0}
    )
    assert "mass refusals" in str(lockout)

    _, mangled = check_refusal_rate.evaluate(
        200.0, {}, sources={"x_real_ip_rejected": 180.0, "x_real_ip": 20.0}
    )
    assert "mangling it" in str(mangled)


def test_losing_x_real_ip_is_called_out_in_words():
    """A bare percentage would not tell the reader what it means.

    The whole point is that this failure is invisible: the limiter still
    answers 200s while bounding nobody. The report has to say so.
    """
    _, lines = check_refusal_rate.evaluate(
        200.0, {}, sources={"forwarded_chain": 180.0, "x_real_ip": 20.0}
    )
    body = "\n".join(lines)
    assert "NOTE:" in body
    assert "INERT" in body


def test_the_workflow_greps_for_markers_this_script_actually_emits():
    """Producer/consumer agreement. Reword a marker and the annotation VANISHES.

    ``synthetic-smoke.yml`` decides whether to raise a run annotation by
    grepping this script's stdout. A grep that stops matching does not fail --
    it prints nothing, and a run with no annotation looks exactly like a run
    with nothing to say. Silence reading as all-clear is the failure mode this
    repo keeps producing, so the two sides are pinned to EACH OTHER here rather
    than each to itself.

    Both directions matter: the workflow must still grep for the marker, and
    this script must still emit it in the scenario that warrants it.
    """
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "synthetic-smoke.yml"
    ).read_text(encoding="utf-8")

    # Comment lines are stripped before matching, and the assertion below is on
    # the GREP COMMAND rather than on the marker appearing anywhere in the file.
    # An earlier version of this test checked `marker in workflow`: QC broke all
    # three greps, left the string alive in a single YAML comment, and the test
    # stayed green. "NOTE:" is the dangerous one -- it is a word that lands in
    # comments by accident, which would anchor this assertion to nothing.
    code = "\n".join(
        line for line in workflow.splitlines() if not line.strip().startswith("#")
    )

    # marker -> a scrape that must produce it
    _, unrated = evaluate(4.0, {"rate_limited": 3.0})
    _, noted = evaluate(
        1000.0, {"rate_limited": 1.0}, sources={"peer": 10.0}
    )

    for marker, produced in (("UNRATED REFUSALS:", unrated), ("NOTE:", noted)):
        assert f'grep -q "{marker}"' in code, (
            f"synthetic-smoke.yml no longer greps for {marker!r} in an actual "
            "command, so the annotation it drives can never fire again. (A "
            "mention in a comment does not count -- that was the hole.)"
        )
        assert any(marker in line for line in produced), (
            f"{marker!r} is grepped by synthetic-smoke.yml but this script no "
            "longer emits it in the scenario that warrants it -- the grep will "
            "silently match nothing."
        )
