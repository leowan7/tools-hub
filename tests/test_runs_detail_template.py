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
    with app.test_request_context("/runs/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="completed"),
            counts=_counts(succeeded=6),
        )
    assert "0 / 24 designs delivered" not in html
    assert "designs delivered" not in html
    # The filter-passing metric is labeled truthfully.
    assert "Passed filters" in html


def test_subjob_completion_headline_renders(app):
    """The accurate sub-job completion headline is the primary signal."""
    with app.test_request_context("/runs/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="completed"),
            counts=_counts(succeeded=6),
        )
    assert "sub-jobs complete" in html
    assert "6 of 6" in html or ">6</span> of <span" in html


def test_all_succeeded_surfaces_download_pointer(app):
    """Designs are downloadable; a prominent sub-jobs pointer is present."""
    with app.test_request_context("/runs/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="completed"),
            counts=_counts(succeeded=6),
        )
    assert "sub-jobs page" in html
    # All succeeded => the exact requested figure may be stated as generated.
    assert "requested designs were generated" in html


def test_partial_completion_does_not_overstate_generated_count(app):
    """When not all sub-jobs succeeded, do not claim all designs generated."""
    with app.test_request_context("/runs/camp-smoke"):
        html = render_template(
            "runs/detail.html",
            campaign=_campaign_fixture(status="running"),
            counts=_counts(succeeded=3, running=2, pending=1),
        )
    assert "requested designs were generated" not in html
    assert "sub-jobs complete" in html
