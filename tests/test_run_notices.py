"""The overrun and partial-result notices on the results page and completion email.

``shared.run_notices`` derives both at render time. The wallet ledger read is
stubbed at ``shared.wallet.job_spend_by_hold``, which both ``overrun_line`` and
``shared.email._cost_breakdown_line`` import at call time.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import render_template

from shared import run_notices as rn
from shared.jobs import ToolJob
from tests.test_job_complete_email_headline import _bodies, _sent

pytestmark = pytest.mark.usefixtures("isolate_supabase")

HOLD = "hold-1"
OVER = "This run used more GPU time than estimated: estimated $1.00, charged $1.40."


def _job(*, result=None, inputs=None, status="succeeded", tool="proteina") -> ToolJob:
    base_inputs = {"_wallet": {"hold_tx_id": HOLD, "estimate_usd": "1.00", "tool_slug": tool}}
    base_inputs.update(inputs or {})
    return ToolJob.from_row({
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "tool": tool,
        "preset": "pilot",
        "status": status,
        "inputs": base_inputs,
        "result": result if result is not None else {"candidates": []},
        "error": None,
        "modal_function_call_id": "fc-stub",
        "job_token": "t" * 64,
        "gpu_seconds_used": 600,
        "created_at": "2026-09-30T12:00:00Z",
        "started_at": "2026-09-30T12:00:01Z",
        "completed_at": "2026-09-30T12:30:00Z",
    })


def _ledger(monkeypatch, usd, settled=True):
    monkeypatch.setattr(
        "shared.wallet.job_spend_by_hold",
        lambda user_id, holds: {HOLD: {"usd": Decimal(usd), "settled": settled, "held": Decimal("1.00")}},
    )


def _partial_result(n=3, timed_out=True):
    search = {"exit_code": 124, "n_parsed": n, "n_scored": n}
    if timed_out:
        search.update(status="timeout", timeout_s=3600)
    return {
        "partial": True,
        "search": search,
        "candidates": [{"rank": i + 1, "sequence": "ACDE", "scores": {}} for i in range(n)],
    }


def test_charge_over_estimate_shows_line_on_page_helper_and_email(monkeypatch):
    _ledger(monkeypatch, "1.40")
    job = _job()
    assert rn.run_notices(job) == [OVER]
    bodies = _bodies(_sent(job))
    assert OVER in bodies["text"]
    assert OVER in bodies["html"]
    # The cost line reads the same settled figure, so the email does not
    # print two different charges.
    assert "charged $1.40 (" in bodies["text"]


@pytest.mark.parametrize("usd", ["1.00", "0.80", "1.004"])
def test_charge_at_or_under_estimate_shows_no_line(monkeypatch, usd):
    _ledger(monkeypatch, usd)
    job = _job()
    assert rn.overrun_line(job) == ""
    assert "more GPU time than estimated" not in _bodies(_sent(job))["text"]


def test_unsettled_hold_shows_no_line(monkeypatch):
    _ledger(monkeypatch, "5.00", settled=False)
    assert rn.overrun_line(_job()) == ""


def test_partial_timeout_result_shows_n_of_m(monkeypatch):
    _ledger(monkeypatch, "1.00")
    job = _job(result=_partial_result(3), inputs={"num_designs": "8"})
    line = "This run stopped at its time limit after 3 of 8 designs."
    assert rn.run_notices(job) == [line]
    bodies = _bodies(_sent(job))
    assert line in bodies["text"]
    assert line in bodies["html"]


def test_partial_non_timeout_says_stopped_early(monkeypatch):
    _ledger(monkeypatch, "1.00")
    job = _job(result=_partial_result(1, timed_out=False), inputs={"num_designs": "8"})
    assert rn.partial_line(job) == "This run stopped early after 1 of 8 designs."


def test_partial_without_design_count_omits_m(monkeypatch):
    job = _job(result=_partial_result(3))
    assert rn.partial_line(job) == "This run stopped at its time limit after 3 designs."


def test_non_partial_result_shows_no_line(monkeypatch):
    _ledger(monkeypatch, "1.00")
    result = _partial_result(3)
    result["partial"] = False
    job = _job(result=result, inputs={"num_designs": "8"})
    assert rn.partial_line(job) == ""
    assert "stopped" not in _bodies(_sent(job))["text"]


def test_campaign_partial_line():
    camp = SimpleNamespace(requested_designs=24)
    assert rn.campaign_partial_line(camp, 20, 0, 0) == ""
    assert rn.campaign_partial_line(camp, 20, 2, 2) == (
        "Part of this run stopped at its time limit: 20 of 24 designs came back."
    )
    assert rn.campaign_partial_line(camp, 20, 2, 1) == (
        "Part of this run stopped early: 20 of 24 designs came back."
    )


def test_campaign_page_renders_partial_notice(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    notice = "Part of this run stopped at its time limit: 20 of 24 designs came back."
    campaign = SimpleNamespace(
        id="camp-1", name="T", tool="proteina", status="completed",
        requested_designs=24, total_subjobs=3, target_name="T",
        budget_usd=Decimal("12.00"),
    )
    counts = {"pending": 0, "running": 0, "succeeded": 3, "failed": 0,
              "timeout": 0, "cancelled": 0, "total": 3}
    with app.test_request_context("/campaigns/camp-1"):
        html = render_template("runs/detail.html", campaign=campaign, counts=counts,
                               partial_notice=notice)
        plain = render_template("runs/detail.html", campaign=campaign, counts=counts)
    assert notice in html
    assert "stopped" not in plain
