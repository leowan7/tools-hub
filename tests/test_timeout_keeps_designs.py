"""A timed-out run keeps the designs it already streamed (Leo 2026-11-04).

The row stays ``timeout`` / ``no_progress_timeout`` and its result carries the
designs ``job_recovery.reconstruct`` rebuilt, flagged ``partial`` -- the same
second write ``cancel_job`` has made since PR #483
(shared/jobs.py::_keep_streamed_designs). What the run charges is untouched:
``test_keeping_designs_does_not_change_the_settle`` compares the settle calls
against the ones a timeout made before this change.
"""
from __future__ import annotations

import csv
import io
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from shared import jobs as jobs_mod
from tests.test_cancel_keeps_designs import CAND_ROW, _rows, client  # noqa: F401
from tests.test_live_charging import DESIGNS, _Jobs, _job_row

pytestmark = pytest.mark.usefixtures("isolate_supabase")

KEPT = {"candidates": DESIGNS, "candidate_count": 2, "partial": True}


def _timeout(store, *, candidates=(), reconstruct_exc=None, recovered=None):
    """Drive the real timeout writer, ``timeout_stuck_job``.

    ``recover_stuck_job_result`` is stubbed to ``recovered``; None means
    nothing was recoverable, which is the only path that reaches the timeout
    write (shared/jobs.py::timeout_stuck_job).
    """
    rec = MagicMock(side_effect=reconstruct_exc, return_value=list(candidates))
    with patch.object(jobs_mod, "get_service_client", store.client), \
         patch.object(jobs_mod, "_charge_workspace_for_completed_job"), \
         patch.object(jobs_mod, "_send_completion_email"), \
         patch("shared.job_recovery.recover_stuck_job_result", return_value=recovered), \
         patch("shared.job_recovery.reconstruct", rec), \
         patch("shared.wallet.settle_live_run") as settle_live, \
         patch("shared.wallet.settle_hold") as settle_hold, \
         patch("shared.wallet.release_hold") as release:
        outcome = jobs_mod.timeout_stuck_job("job-1", probe_modal=False)
    return SimpleNamespace(outcome=outcome, rec=rec, settle_live=settle_live,
                           settle_hold=settle_hold, release=release)


def test_a_timed_out_run_keeps_the_streamed_designs():
    store = _Jobs(_job_row(gpu_seconds_used=120))
    r = _timeout(store, candidates=DESIGNS)
    row = store.rows["job-1"]
    assert r.outcome == "timed_out"
    assert (row["status"], row["failure_class"]) == ("timeout", "no_progress_timeout")
    assert row["result"] == KEPT


def _settle_calls(r):
    return [m.call_args_list for m in (r.settle_live, r.settle_hold, r.release)]


@pytest.mark.parametrize("wallet", [
    None,  # live run
    {"hold_tx_id": "hold-1", "estimate_usd": "2.00", "tool_slug": "boltz2"},
])
@pytest.mark.parametrize("seconds", [None, 120])
def test_keeping_designs_does_not_change_the_settle(wallet, seconds):
    """The settle with designs kept is the settle of a timeout that kept none,
    which is what every timeout did before this change. Exactly one settle."""
    kept = _timeout(_Jobs(_job_row(wallet=wallet, gpu_seconds_used=seconds)),
                    candidates=DESIGNS)
    today = _timeout(_Jobs(_job_row(wallet=wallet, gpu_seconds_used=seconds)),
                     candidates=())
    assert _settle_calls(kept) == _settle_calls(today)
    assert sum(m.call_count for m in (kept.settle_live, kept.settle_hold,
                                      kept.release)) == 1


@pytest.mark.parametrize("candidates,exc", [
    ((), None),
    (DESIGNS, RuntimeError("storage down")),
])
def test_a_rebuild_with_nothing_or_an_error_still_times_out(candidates, exc):
    store = _Jobs(_job_row(gpu_seconds_used=120))
    r = _timeout(store, candidates=candidates, reconstruct_exc=exc)
    assert r.outcome == "timed_out"
    assert store.rows["job-1"]["status"] == "timeout"
    assert store.rows["job-1"]["result"] is None
    r.rec.assert_called_once()
    r.settle_live.assert_called_once()


def test_no_rebuild_when_the_work_was_recoverable():
    """A recovered run finalises as succeeded through complete_job with its
    real result, so there is nothing to rebuild (the recovery gate in
    shared/jobs.py::timeout_stuck_job)."""
    store = _Jobs(_job_row(gpu_seconds_used=120))
    r = _timeout(store, candidates=DESIGNS, recovered={"candidates": DESIGNS[:1]})
    assert r.outcome == "recovered"
    assert store.rows["job-1"]["status"] == "succeeded"
    r.rec.assert_not_called()


def test_complete_job_keeps_them_on_the_timeout_arm():
    """The second ``mark_timeout`` call site. No non-test caller passes
    ``terminal_status="timeout"`` today, so this covers the arm rather than a
    live path (shared/jobs.py::complete_job)."""
    store = _Jobs(_job_row(gpu_seconds_used=120))
    rec = MagicMock(return_value=list(DESIGNS))
    with patch.object(jobs_mod, "get_service_client", store.client), \
         patch.object(jobs_mod, "_charge_workspace_for_completed_job"), \
         patch.object(jobs_mod, "_send_completion_email"), \
         patch("shared.job_recovery.reconstruct", rec), \
         patch("shared.wallet.settle_live_run"):
        fresh = jobs_mod.complete_job("job-1", terminal_status="timeout")
    assert store.rows["job-1"]["result"] == KEPT
    assert fresh.status == "timeout" and fresh.result == KEPT


# ---------------------------------------------------------------------------
# The job page
# ---------------------------------------------------------------------------

HOLD = "hold-1"


def _timed_out(result=None):
    from tests.test_run_notices import _job

    return replace(_job(status="timeout", tool="boltz2"), result=result,
                   failure_class="no_progress_timeout")


def _html(client, job, monkeypatch):
    monkeypatch.setattr(
        "shared.wallet.job_spend_by_hold",
        lambda user_id, holds: {HOLD: {"usd": Decimal("0"), "settled": True,
                                       "held": Decimal("1.00")}},
    )
    ctx = SimpleNamespace(user_id="u-1", tier="free", balance=100,
                          email="u@example.com")
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.jobs.load_user_context", return_value=ctx), \
         patch("blueprints.jobs.get_job", return_value=job), \
         patch("shared.jobs.resolve_user_email_and_meta",
               return_value=("u@example.com", {})):
        resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def test_the_page_says_designs_were_kept(client, monkeypatch):
    html = _html(client, _timed_out(_rows(2)), monkeypatch)
    assert len(CAND_ROW.findall(html)) == 2
    assert ("Stopped at its time limit, 2 designs kept. They are in your "
            "results and downloads.") in html
    # partial_line fires only on a succeeded job, so there is exactly one
    # notice and not two saying the same thing
    # (shared/run_notices.py::partial_line's first guard).
    assert html.count("designs kept") == 1
    assert "This run stopped" not in html


def test_the_page_no_longer_says_it_produced_no_result(client, monkeypatch):
    html = _html(client, _timed_out(_rows(2)), monkeypatch)
    assert "timed out before producing a result" not in html
    assert "Job timed out before it finished." in html


def test_the_page_keeps_the_refund_sentence(client, monkeypatch):
    """Showing results must not hide what happened to the money: the failure
    panel still renders for a timeout that kept designs (the two sibling
    blocks in templates/job_detail.html's results-container)."""
    html = _html(client, _timed_out(_rows(2)), monkeypatch)
    assert "data-failure-money" in html
    assert "You were not charged for this run." in html
    assert "data-retry-link" in html


def test_a_timeout_that_kept_nothing_is_unchanged(client, monkeypatch):
    html = _html(client, _timed_out(), monkeypatch)
    assert CAND_ROW.search(html) is None
    assert "designs kept" not in html
    assert "Job timed out before producing a result." in html
    assert "data-failure-money" in html


@pytest.mark.parametrize("fmt", ["csv", "fasta"])
def test_exports_carry_the_kept_designs(client, fmt):
    """The export routes never gated on status, so this is a guard that the
    kept result is the shape they read (blueprints/jobs.py::export_csv reads
    page_ordered_records(job.tool, job.result))."""
    job = _timed_out(_rows(2))
    ctx = SimpleNamespace(user_id="u-1", tier="free", balance=100,
                          email="u@example.com")
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.jobs.load_user_context", return_value=ctx), \
         patch("blueprints.jobs.get_job", return_value=job):
        body = client.get(f"/jobs/{job.id}/export.{fmt}").get_data(as_text=True)
    if fmt == "csv":
        assert len(list(csv.DictReader(io.StringIO(body)))) == 2
    else:
        assert body.count(">") == 2


# ---------------------------------------------------------------------------
# Campaign fan-in and the target page
# ---------------------------------------------------------------------------


def test_a_campaign_aggregates_a_timed_out_childs_designs(monkeypatch):
    """A timed-out child's kept designs reach the campaign page and its
    exports, which both read ``aggregate_campaign_candidates``. The kept
    result carries no ``search`` key, so the timeout count has to come from
    the row's status.
    """
    import shared.compute_campaigns as cc
    from tests.test_campaign_results import _cand, _patch

    rows = [
        {"id": "j0", "campaign_id": "C", "status": "succeeded", "chunk_index": 0,
         "attempt": 1, "result": {"candidates": [_cand(0, 0.7)]}},
        {"id": "j1", "campaign_id": "C", "status": "timeout", "chunk_index": 1,
         "attempt": 1, "result": {"candidates": [_cand(0, 0.9), _cand(1, 0.8)],
                                  "candidate_count": 2, "partial": True}},
        # Timed out before any design finished: no result, nothing to show.
        {"id": "j2", "campaign_id": "C", "status": "timeout", "chunk_index": 2,
         "attempt": 1, "result": None},
    ]
    _patch(monkeypatch, rows)
    out = cc.aggregate_campaign_candidates("C", user_id="u", limit=None)

    assert out["total"] == 3
    assert sorted(c["_source_job_id"] for c in out["candidates"]) == ["j0", "j1", "j1"]
    assert (out["partial_chunks"], out["timeout_chunks"]) == (1, 1)


def test_the_campaign_notice_names_the_time_limit():
    """With ``timeout_chunks == partial_chunks`` the campaign line says "time
    limit" rather than "stopped early"
    (shared/run_notices.py::campaign_partial_line)."""
    from shared.run_notices import campaign_partial_line

    campaign = SimpleNamespace(requested_designs=8)
    assert campaign_partial_line(campaign, 3, 1, 1) == (
        "Part of this run stopped at its time limit: 3 of 8 designs came back."
    )


def test_the_target_page_shows_a_timed_out_runs_designs(monkeypatch):
    """A timed-out standalone run's kept designs reach the target table and
    its count; one that kept nothing contributes nothing."""
    from shared import target_results
    from tests.test_aggregate_target import (
        OWNER,
        _install,
        _job_row as _target_row,
        _keys,
        _standalone_queries,
    )

    ok = {"ipTM": 0.95, "pLDDT": 0.95, "i_pAE": 5.0}
    rows = [
        _target_row("done-1", tool="bindcraft", preset="default", target_id="T",
                    candidates=[{"pdb_key": "done.pdb", "scores": dict(ok)}]),
        _target_row("timeout-kept", tool="bindcraft", preset="default",
                    target_id="T", status="timeout",
                    candidates=[{"pdb_key": "kept.pdb", "scores": dict(ok)}],
                    result_extra={"partial": True}),
        {**_target_row("timeout-empty", tool="bindcraft", preset="default",
                       target_id="T", status="timeout"), "result": None},
    ]
    fake = _install(monkeypatch, rows=rows)

    agg = target_results.aggregate_target_candidates("T", user_id=OWNER)

    assert agg["partial"] is False
    assert _keys(agg) == ["done.pdb", "kept.pdb"]
    assert agg["standalone_jobs"] == 2
    for query in _standalone_queries(fake):
        assert ("status", ["succeeded", "cancelled", "timeout"]) in query._in
