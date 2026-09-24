"""/jobs runtime, spend and progress columns (ariax work package P2).

Spend is read from the wallet ledger, never estimated: the job's
``inputs._wallet.hold_tx_id`` and every row parented on it. The fake client
below serves ``wallet_transactions`` rows so the ledger arithmetic in
``shared.wallet.job_spend_by_hold`` runs for real.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared.jobs import ToolJob

pytestmark = pytest.mark.usefixtures("isolate_supabase")

NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        return _Query([r for r in self.rows if r.get(col) == val])

    def in_(self, col, vals):
        vals = {str(v) for v in vals}
        return _Query([r for r in self.rows if str(r.get(col)) in vals])

    def execute(self):
        return SimpleNamespace(data=self.rows)


class _Client:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "wallet_transactions"
        return _Query(self.rows)


def _tx(tx_id, amount, parent=None, user="u-1"):
    return {"id": tx_id, "parent_tx_id": parent, "amount_usd": amount, "user_id": user}


def _job(jid, status, *, hold=None, progress=None, started=None, completed=None,
         failure_class=None, inputs=None, created_at=None):
    if inputs is None:
        inputs = {}
        if hold is not None:
            inputs["_wallet"] = {"hold_tx_id": hold}
        if progress is not None:
            inputs["_progress"] = progress
    return ToolJob.from_row({
        "id": jid, "user_id": "u-1", "tool": "bindcraft", "preset": "pilot",
        "status": status, "inputs": inputs, "job_token": "t",
        "created_at": created_at if created_at is not None else _iso(NOW),
        "started_at": started, "completed_at": completed,
        "failure_class": failure_class,
    })


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    return c


def _render(client, jobs, ledger):
    ctx = SimpleNamespace(user_id="u-1", tier="free", balance=100, email="u@example.com")
    with patch("blueprints.jobs.load_user_context", return_value=ctx), \
         patch("blueprints.jobs.list_jobs_paginated", return_value=(jobs, len(jobs))), \
         patch("blueprints.jobs.list_campaign_labels_for_user", return_value=[]), \
         patch("shared.wallet.get_service_client", return_value=_Client(ledger)):
        resp = client.get("/jobs")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _row(html, jid):
    """The <tr> whose job link names ``jid``."""
    for tr in re.findall(r"<tr>.*?</tr>", html, flags=re.S):
        if f"/jobs/{jid}" in tr:
            return re.sub(r"\s+", " ", tr)
    raise AssertionError(f"no row for {jid}")


def test_a_running_job_shows_elapsed_time_reserved_spend_and_real_progress(client):
    job = _job("run-1", "running", hold="10",
               progress={"designs_completed": 3, "designs_total": 8},
               started=_iso(datetime.now(timezone.utc) - timedelta(minutes=5, seconds=3)))
    row = _row(_render(client, [job], [_tx(10, "-12.50")]), "run-1")
    assert re.search(r">\s*5m 0[3-9]s\s*<", row)
    assert "$12.50" in row and "reserved" in row
    assert '<progress class="jobs-progress" max="8" value="3">' in row
    assert "3/8 designs" in row


def test_a_finished_job_shows_total_runtime_and_the_settled_charge(client):
    start = NOW - timedelta(hours=2)
    job = _job("done-1", "succeeded", hold="20", failure_class="succeeded",
               started=_iso(start), completed=_iso(start + timedelta(minutes=62, seconds=9)))
    row = _row(_render(client, [job], [_tx(20, "-12.50"), _tx(21, "8.00", parent=20)]), "done-1")
    assert "1h 02m" in row
    assert "$4.50" in row
    assert "reserved" not in row and "refunded" not in row
    assert "<progress" not in row
    assert 'class="compare-chk"' in row


def test_a_refunded_job_shows_what_was_charged_after_the_refund(client):
    start = NOW - timedelta(minutes=30)
    job = _job("ref-1", "failed", hold="30", failure_class="infra_crash",
               started=_iso(start), completed=_iso(start + timedelta(seconds=41)))
    row = _row(_render(client, [job], [_tx(30, "-12.50"), _tx(31, "12.50", parent=30)]), "ref-1")
    assert "$0.00" in row and "refunded" in row
    assert "$12.50" not in row
    assert "41s" in row
    assert 'class="compare-chk"' not in row


def test_no_progress_signal_shows_the_status_and_no_bar(client):
    pending = _job("pend-1", "pending")
    unknown_total = _job("run-2", "running", progress={"designs_total": 0},
                         started=_iso(NOW - timedelta(seconds=9)))
    html = _render(client, [pending, unknown_total], [])
    for jid, status in (("pend-1", "pending"), ("run-2", "running")):
        row = _row(html, jid)
        assert "<progress" not in row
        assert f'<span class="jobs-muted">{status}</span>' in row
    assert "$" not in _row(html, "pend-1")


def test_a_malformed_row_renders_dashes_and_is_not_dropped(client):
    bad = _job("bad-1", "running", inputs=["not", "a", "dict"],
               started="not a timestamp", created_at=12345)
    bad_ledger = _job("bad-2", "succeeded", hold="40",
                      progress={"designs_total": "eight"})
    bad_wallet = _job("bad-3", "failed", inputs={"_wallet": "junk", "_progress": [1]})
    good = _job("good-1", "succeeded", hold="50",
                started=_iso(NOW - timedelta(seconds=70)), completed=_iso(NOW))
    html = _render(client, [bad, bad_ledger, bad_wallet, good],
                   [_tx(40, "abc"), _tx(50, "-3.00"), _tx(51, "1.00", parent=50)])
    assert len(re.findall(r'href="/jobs/(?:bad|good)-\d"', html)) == 4
    for jid in ("bad-1", "bad-2", "bad-3"):
        row = _row(html, jid)
        assert "$" not in row and "<progress" not in row
        assert row.count("—") >= 2
    assert "$2.00" in _row(html, "good-1")
    assert "1m 10s" in _row(html, "good-1")


def test_a_ledger_outage_leaves_spend_blank_and_the_page_up(client):
    job = _job("run-3", "running", hold="60", started=_iso(NOW))

    class _Down:
        def table(self, _name):
            raise RuntimeError("supabase down")

    ctx = SimpleNamespace(user_id="u-1", tier="free", balance=100, email="u@example.com")
    with patch("blueprints.jobs.load_user_context", return_value=ctx), \
         patch("blueprints.jobs.list_jobs_paginated", return_value=([job], 1)), \
         patch("blueprints.jobs.list_campaign_labels_for_user", return_value=[]), \
         patch("shared.wallet.get_service_client", return_value=_Down()):
        resp = client.get("/jobs")
    assert resp.status_code == 200
    assert "$" not in _row(resp.get_data(as_text=True), "run-3")


def test_another_users_ledger_rows_are_not_counted(client):
    job = _job("run-4", "running", hold="70", started=_iso(NOW))
    row = _row(_render(client, [job], [_tx(70, "-5.00", user="u-2")]), "run-4")
    assert "$" not in row


def test_the_empty_state_ladder_is_unchanged(client):
    html = _render(client, [], [])
    assert "jobs-table" not in html
    assert html.count('counter-increment: start-step') == 3
    assert "Browse all tools" in html
