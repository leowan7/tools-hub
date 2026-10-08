"""A user cancel keeps the designs that already finished (shared/jobs.py::cancel_job).

The job stays ``cancelled`` / ``user_cancelled`` and its result carries the
designs ``job_recovery.reconstruct`` rebuilt, flagged ``partial``. The wallet
settle is the one a cancel made before this change.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from shared import jobs as jobs_mod
from tests.test_live_charging import DESIGNS, _Jobs, _job_row, _raise

pytestmark = pytest.mark.usefixtures("isolate_supabase")

KEPT = {"candidates": DESIGNS, "candidate_count": 2, "partial": True}


def _cancel(store, *, candidates=(), reconstruct_exc=None, cancel=None, **kw):
    modal = MagicMock()
    modal.cancel.side_effect = cancel or (lambda _fc: {"ok": True, "error": None})
    rec = MagicMock(side_effect=reconstruct_exc, return_value=list(candidates))
    with patch.object(jobs_mod, "get_service_client", store.client), \
         patch.object(jobs_mod, "_drive_campaign_after_terminal") as drive, \
         patch("shared.job_recovery.reconstruct", rec), \
         patch("shared.wallet.settle_live_run") as settle_live, \
         patch("shared.wallet.settle_hold") as settle_hold, \
         patch("shared.wallet.release_hold") as release:
        job, err = jobs_mod.cancel_job("job-1", user_id="u-1", modal_client=modal, **kw)
    return SimpleNamespace(job=job, err=err, rec=rec, drive=drive, settle_live=settle_live,
                           settle_hold=settle_hold, release=release)


def test_cancel_keeps_the_finished_designs():
    store = _Jobs(_job_row(gpu_seconds_used=120))
    r = _cancel(store, candidates=DESIGNS)
    row = store.rows["job-1"]
    assert r.err is None
    assert (row["status"], row["failure_class"]) == ("cancelled", "user_cancelled")
    assert row["result"] == KEPT
    assert r.job.status == "cancelled" and r.job.result == KEPT


def _settle_calls(r):
    return [m.call_args_list for m in (r.settle_live, r.settle_hold, r.release)]


@pytest.mark.parametrize("wallet", [
    None,  # live run -> settle_live_run
    {"hold_tx_id": "hold-1", "estimate_usd": "2.00", "tool_slug": "boltz2"},  # settle_hold
])
@pytest.mark.parametrize("seconds", [None, 120])
def test_keeping_designs_does_not_change_the_settle(wallet, seconds):
    """The settle with designs kept is the settle of a cancel that kept none,
    which is what every cancel did before this change. Exactly one settle."""
    kept = _cancel(_Jobs(_job_row(wallet=wallet, gpu_seconds_used=seconds)), candidates=DESIGNS)
    today = _cancel(_Jobs(_job_row(wallet=wallet, gpu_seconds_used=seconds)), candidates=())
    assert _settle_calls(kept) == _settle_calls(today)
    assert sum(m.call_count for m in (kept.settle_live, kept.settle_hold, kept.release)) == 1


@pytest.mark.parametrize("candidates,exc", [((), None), (DESIGNS, RuntimeError("storage down"))])
def test_a_rebuild_with_nothing_or_an_error_still_cancels(candidates, exc):
    store = _Jobs(_job_row(gpu_seconds_used=120))
    r = _cancel(store, candidates=candidates, reconstruct_exc=exc)
    assert r.err is None
    assert store.rows["job-1"]["status"] == "cancelled"
    assert store.rows["job-1"]["result"] is None
    r.rec.assert_called_once()
    r.settle_live.assert_called_once()


@pytest.mark.parametrize("cancel", [lambda _fc: {"ok": False, "error": "x"}, _raise])
def test_no_rebuild_when_modal_did_not_stop_the_run(cancel):
    store = _Jobs(_job_row())
    r = _cancel(store, candidates=DESIGNS, cancel=cancel, leave_running_if_cancel_fails=False)
    assert store.rows["job-1"]["status"] == "cancelled"
    assert store.rows["job-1"]["result"] is None
    r.rec.assert_not_called()


def test_no_rebuild_for_a_job_never_dispatched():
    store = _Jobs(_job_row(status="pending", modal_function_call_id=None))
    r = _cancel(store, candidates=DESIGNS)
    assert store.rows["job-1"]["status"] == "cancelled"
    r.rec.assert_not_called()


def test_no_rebuild_when_the_cancel_loses_the_race():
    store = _Jobs(_job_row())

    def webhook_first(_fc):
        store.rows["job-1"].update(status="succeeded", result={"candidates": DESIGNS[:1]})
        return {"ok": True, "error": None}

    r = _cancel(store, candidates=DESIGNS, cancel=webhook_first)
    assert r.err == "already_succeeded"
    assert store.rows["job-1"]["result"] == {"candidates": DESIGNS[:1]}
    r.rec.assert_not_called()


def test_campaign_is_driven_with_the_kept_result():
    store = _Jobs(_job_row(campaign_id="camp-1"))
    r = _cancel(store, candidates=DESIGNS)
    assert r.drive.call_args.args[0].result == KEPT


# ---------------------------------------------------------------------------
# Page and exports
# ---------------------------------------------------------------------------


def _cancelled(result=None):
    from tests.test_run_notices import _job

    return replace(_job(status="cancelled", tool="boltz2"), result=result)


def _rows(n):
    return {"candidates": [{"rank": i + 1, "sequence": "ACDE", "pdb_key": f"designs/d{i}.pdb",
                            "scores": {}} for i in range(n)],
            "candidate_count": n, "partial": True}


def _html(client, job):
    ctx = SimpleNamespace(user_id="u-1", tier="free", balance=100, email="u@example.com")
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.jobs.load_user_context", return_value=ctx), \
         patch("blueprints.jobs.get_job", return_value=job), \
         patch("shared.jobs.resolve_user_email_and_meta", return_value=("u@example.com", {})):
        resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


CAND_ROW = re.compile(r'class="cand-row')


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def test_job_page_shows_the_kept_designs(client):
    html = _html(client, _cancelled(_rows(2)))
    assert len(CAND_ROW.findall(html)) == 2
    assert "Cancelled, 2 designs kept. They are in your results and downloads." in html


def test_job_page_for_a_cancel_that_kept_nothing_is_unchanged(client):
    html = _html(client, _cancelled())
    assert CAND_ROW.search(html) is None
    assert "designs kept" not in html


@pytest.mark.parametrize("fmt", ["csv", "fasta"])
def test_exports_carry_the_kept_designs(client, fmt):
    job = _cancelled(_rows(2))
    ctx = SimpleNamespace(user_id="u-1", tier="free", balance=100, email="u@example.com")
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
