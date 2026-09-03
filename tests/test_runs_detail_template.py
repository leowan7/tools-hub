"""Jinja render smoke tests for templates/runs/detail.html.

Mirrors the render_template + test_request_context pattern in
tests/test_wallet_templates.py. Guards the fix for the misleading
"0 / N designs delivered" headline: a succeeded campaign that produced
downloadable designs but had zero candidates pass the quality filter
must NOT read as a dead run. Sub-job completion is the headline signal;
the filter-passing count is labeled "passed filters", never "delivered".
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import render_template


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _campaign_fixture(status="completed", requested_designs=24, total_subjobs=6):
    """Minimal campaign object matching what detail.html reads."""
    return SimpleNamespace(
        id="camp-smoke",
        name="Smoke Target",
        tool="rfdiffusion",
        status=status,
        requested_designs=requested_designs,
        total_subjobs=total_subjobs,
        target_name="Smoke Target",
        budget_usd=Decimal("12.00"),
    )


def _counts(succeeded=0, failed=0, timeout=0, running=0, pending=0):
    total = succeeded + failed + timeout + running + pending
    return {
        "pending": pending,
        "running": running,
        "succeeded": succeeded,
        "failed": failed,
        "timeout": timeout,
        "cancelled": 0,
        "total": total,
    }


def test_succeeded_zero_hits_does_not_render_dead_run_string(app):
    """A fully succeeded campaign with 0 hits must not say '0 / N delivered'."""
    with app.test_request_context("/campaigns/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="completed"),
            counts=_counts(succeeded=6),
        )
    assert "0 / 24 designs delivered" not in html
    assert "designs delivered" not in html
    # The quality metric is labeled truthfully, and NAMES THE BAR it counted
    # against. It read "Passed filters" over a number derived from a word each
    # pipeline stamped at the end of its run, so a threshold correction could
    # never reach it. A reader who can see the bar can check the number.
    assert "Meet ipTM 0.65, pLDDT 80 and i_pAE" in html


def test_subjob_completion_headline_renders(app):
    """The accurate sub-job completion headline is the primary signal."""
    with app.test_request_context("/campaigns/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="completed"),
            counts=_counts(succeeded=6),
        )
    assert "sub-jobs complete" in html
    assert "6 of 6" in html or ">6</span> of <span" in html


def test_all_succeeded_surfaces_download_pointer(app):
    """The unified campaign page shows designs inline and keeps a pointer to
    inspect the individual sub-jobs. (The merged results table replaced the
    old link-out to the per-sub-job list.)"""
    with app.test_request_context("/campaigns/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="completed"),
            counts=_counts(succeeded=6),
        )
    assert "View individual sub-jobs" in html


def test_merged_results_render_in_campaign_mode(app):
    """When sub-jobs produced designs, the page renders the merged candidate
    table in campaign mode: campaign-scoped exports, per-candidate 3D resolved
    to the SOURCE sub-job, a source-sub-job provenance tag, and a campaign-wide
    refold + lab-submit. Exercises results_panel/candidate_table with the real
    app Jinja globals."""
    cands = [
        {"pdb_key": "d0.pdb", "scores": {"ipTM": 0.91, "filter_status": "pass"},
         "_source_job_id": "job-aaaaaaaa", "_source_chunk": 0, "_source_index": 0},
        {"pdb_key": "d1.pdb", "scores": {"ipTM": 0.74, "filter_status": "pass"},
         "_source_job_id": "job-bbbbbbbb", "_source_chunk": 1, "_source_index": 2},
    ]
    with app.test_request_context("/campaigns/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="completed"),
            counts=_counts(succeeded=6),
            candidates=cands,
            result_columns=["ipTM", "pLDDT", "i_pAE", "filter_status"],
            candidates_total=2,
            candidates_capped=False,
            was_running=False,
        )
    # Exports are campaign-scoped, not per-job.
    assert "/campaigns/camp-smoke/export.zip" in html
    # Per-candidate 3D/download resolves to the candidate's own source sub-job.
    assert "/api/jobs/job-bbbbbbbb/pdb/" in html
    # Provenance tag + campaign-wide refold and lab-submit.
    assert "cand-subjob-tag" in html
    assert "/campaigns/camp-smoke/refold" in html
    assert 'name="source_campaign_id"' in html


def test_capped_note_renders_top_n_of_m(app):
    """A capped merged table shows an explicit 'top N of M' note."""
    cands = [{"pdb_key": "d.pdb", "scores": {"ipTM": 0.8}, "_source_job_id": "j",
              "_source_chunk": 0, "_source_index": 0}]
    with app.test_request_context("/campaigns/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="completed"),
            counts=_counts(succeeded=6),
            candidates=cands,
            result_columns=["ipTM"],
            candidates_total=900,
            candidates_capped=True,
            was_running=False,
        )
    assert "900" in html
    assert "designs by score" in html
    # Banner honesty: CSV/FASTA are the full ranked set, the ZIP is described as
    # limited, and the old false "download all" wording is gone.
    assert "full ranked set" in html
    assert "PDB ZIP is" in html and "limited" in html
    assert "download all" not in html

    # This assertion is inverted from what it used to be, deliberately.
    #
    # `5f1300c` added the qualifier "(up to the first 1000 completed sub-jobs)"
    # to the banner and pinned it here, correctly: the fan-in was an un-paged
    # .select() and PostgREST clamped it at max_rows. `e1311e4`, later the SAME
    # DAY, replaced that read with iter_succeeded_children, which pages with
    # .range() and is itself pinned by
    # test_aggregate_pages_past_the_postgrest_max_rows_clamp. From that commit
    # the qualifier was false and this assertion was pinning it in place.
    #
    # So the export really is the full ranked set now, and saying otherwise
    # understates it. Phase 3 builds the same banner on the target page, which
    # is why this surfaced: the false sentence was about to be copied.
    assert "up to the first 1000" not in html
    assert "1000 completed sub-jobs" not in html


def test_partial_completion_does_not_overstate_generated_count(app):
    """When not all sub-jobs succeeded, do not claim all designs generated."""
    with app.test_request_context("/campaigns/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="running"),
            counts=_counts(succeeded=3, running=2, pending=1),
        )
    assert "requested designs were generated" not in html
    assert "sub-jobs complete" in html


def test_paused_campaign_shows_cancel_button(app):
    """A paused (insufficient-funds) campaign is still cancellable from the page."""
    with app.test_request_context("/campaigns/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="paused_insufficient_funds"),
            counts=_counts(succeeded=2, pending=0, running=0),
        )
    assert 'id="rd-cancel"' in html


def test_terminal_campaign_hides_cancel_button(app):
    """A completed campaign renders no server-side Cancel button."""
    with app.test_request_context("/campaigns/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="completed"),
            counts=_counts(succeeded=6),
        )
    assert 'id="rd-cancel"' not in html
