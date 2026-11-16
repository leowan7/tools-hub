"""Run history people can act on: find, cost, re-run, export (2026-11-16).

/jobs filters (tool, status, date range, group-or-job-id search), a Re-run
link per row, the run page's cost line, and ``GET /jobs/export.csv``. The
ledger fake is the one tests/test_jobs_list_columns.py uses, so the spend
arithmetic in ``shared.wallet.job_spend_by_hold`` runs for real.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared import jobs as jobs_mod
from tests.test_jobs_list_columns import NOW, _Client, _iso, _job, _row, _tx, client  # noqa: F401

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_CTX = SimpleNamespace(user_id="u-1", tier="free", balance=100, email="u@example.com")


def _get(client, url, jobs, ledger=(), labels=()):  # noqa: F811
    seen = []

    def _list(user_id, **kw):
        seen.append(kw)
        return list(jobs), len(jobs)

    with patch("blueprints.jobs.load_user_context", return_value=_CTX), \
         patch("blueprints.jobs.list_jobs_paginated", side_effect=_list), \
         patch("blueprints.jobs.list_campaign_labels_for_user", return_value=list(labels)), \
         patch("shared.wallet.get_service_client", return_value=_Client(list(ledger))):
        resp = client.get(url)
    return resp, seen


# --- cost wording ----------------------------------------------------------

def test_a_refunded_class_that_still_nets_a_charge_is_not_called_refunded(client):  # noqa: F811
    """release_hold returns nothing once the hold has a child
    (supabase/migrations/0018_wallet_rpcs.sql), so a refunded class with a
    net above zero is a charge that stood. "$4.50 refunded" said the opposite."""
    job = _job("ref-2", "failed", hold="40", failure_class="infra_crash")
    resp, _ = _get(client, "/jobs", [job], [_tx(40, "-12.50"), _tx(41, "8.00", parent=40)])
    row = _row(resp.get_data(as_text=True), "ref-2")
    assert "$4.50" in row
    assert "refunded" not in row and "not charged" not in row


def test_the_run_page_shows_the_same_cost_as_its_list_row(client):  # noqa: F811
    job = _job("done-2", "succeeded", hold="50", failure_class="succeeded")
    ledger = [_tx(50, "-12.50"), _tx(51, "8.00", parent=50)]
    with patch("blueprints.jobs.get_job", return_value=job):
        page, _ = _get(client, "/jobs/done-2", [job], ledger)
    html = page.get_data(as_text=True)
    assert re.search(r"cost <strong data-run-cost>\$4\.50</strong>", html)
    lst, _ = _get(client, "/jobs", [job], ledger)
    assert "$4.50" in _row(lst.get_data(as_text=True), "done-2")


def test_the_run_page_relabels_the_blank_form_button(client):  # noqa: F811
    job = _job("done-3", "succeeded")
    with patch("blueprints.jobs.get_job", return_value=job):
        html = _get(client, "/jobs/done-3", [job])[0].get_data(as_text=True)
    assert re.search(r'href="/tools/bindcraft"[^>]*>New run</a>', html)
    assert ">Run again</a>" not in html
    assert "data-run-cost" not in html  # no hold, no cost line


# --- re-run ----------------------------------------------------------------

def test_every_row_links_to_the_prefilled_form(client):  # noqa: F811
    jobs = [_job("a-1", "failed"), _job("b-1", "succeeded")]
    html = _get(client, "/jobs", jobs)[0].get_data(as_text=True)
    for jid in ("a-1", "b-1"):
        assert re.search(rf'href="/tools/bindcraft\?clone_from={jid}"[^>]*data-rerun>Re-run</a>',
                         _row(html, jid))


# --- filters ---------------------------------------------------------------

def test_the_route_turns_the_query_string_into_list_kwargs(client):  # noqa: F811
    _, seen = _get(client, "/jobs?tool=boltz2&status=failed&from=2026-11-01"
                           "&to=2026-11-15&q=abc&campaign=__uncategorized__", [_job("x-1", "failed")])
    assert seen[0] == {
        "page": 1, "page_size": 25, "tool": "boltz2", "status": "failed",
        "created_from": "2026-11-01T00:00:00+00:00",
        # The "to" day is inclusive: the bound is the next midnight.
        "created_before": "2026-11-16T00:00:00+00:00",
        "q": "abc", "campaign_label": "",
    }


def test_an_unknown_status_or_bad_day_is_dropped(client):  # noqa: F811
    _, seen = _get(client, "/jobs?status=bogus&from=11/01/2026&to=", [_job("x-2", "failed")])
    assert seen[0] == {"page": 1, "page_size": 25}


def test_the_pager_keeps_the_filters(client):  # noqa: F811
    jobs = [_job(f"p-{i}", "failed") for i in range(3)]

    def _list(user_id, **kw):
        return jobs, 60

    with patch("blueprints.jobs.load_user_context", return_value=_CTX), \
         patch("blueprints.jobs.list_jobs_paginated", side_effect=_list), \
         patch("blueprints.jobs.list_campaign_labels_for_user", return_value=[]), \
         patch("shared.wallet.get_service_client", return_value=_Client([])):
        html = client.get("/jobs?page=2&tool=boltz2&q=abc").get_data(as_text=True)
    for page in (1, 3):
        link = re.search(rf'href="(/jobs\?page={page}[^"]*)"', html).group(1)
        assert "tool=boltz2" in link and "q=abc" in link
    assert re.search(r'href="/jobs/export\.csv\?[^"]*tool=boltz2', html)


def test_no_match_says_so_instead_of_the_first_run_ladder(client):  # noqa: F811
    html = _get(client, "/jobs?status=failed", [])[0].get_data(as_text=True)
    assert "data-no-match" in html
    assert "Run your first job" not in html


class _Recorder:
    """A PostgREST builder that records each filter call."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            self.calls.append((name, args))
            return self
        return _call

    def execute(self):
        return SimpleNamespace(data=[], count=0)


def _query_calls(**kw):
    rec = _Recorder()
    with patch.object(jobs_mod, "get_service_client",
                      return_value=SimpleNamespace(table=lambda _n: rec)):
        jobs_mod.list_jobs_paginated("u-1", **kw)
    return [c for c in rec.calls if c[0] not in ("select", "order", "range")]


def test_list_jobs_paginated_applies_each_filter():
    calls = _query_calls(tool="boltz2", status="failed",
                         created_from="2026-11-01T00:00:00+00:00",
                         created_before="2026-11-16T00:00:00+00:00", q="lib 50%")
    assert calls == [
        ("eq", ("user_id", "u-1")),
        ("eq", ("tool", "boltz2")),
        ("eq", ("status", "failed")),
        ("gte", ("created_at", "2026-11-01T00:00:00+00:00")),
        ("lt", ("created_at", "2026-11-16T00:00:00+00:00")),
        # Not hex, so the group name only, with the % escaped.
        ("ilike", ("campaign_label", "*lib 50\\%*")),
    ]


def test_a_hex_search_also_matches_a_job_id_prefix():
    calls = _query_calls(q="3fa8")
    assert calls[-1] == ("or_", (
        "campaign_label.ilike.*3fa8*,"
        "and(id.gte.3fa80000-0000-0000-0000-000000000000,"
        "id.lte.3fa8ffff-ffff-ffff-ffff-ffffffffffff)",
    ),)


@pytest.mark.parametrize("q, expected", [
    ("3FA8C1D2-77", ("3fa8c1d2-7700-0000-0000-000000000000",
                     "3fa8c1d2-77ff-ffff-ffff-ffffffffffff")),
    ("my-group", None),
    ("-", None),
    ("a" * 33, None),
])
def test_job_id_prefix_range(q, expected):
    assert jobs_mod._job_id_prefix_range(q) == expected


# --- history CSV -----------------------------------------------------------

def test_the_history_csv_has_no_money_and_counts_designs(client):  # noqa: F811
    done = dataclasses.replace(
        _job("c-1", "succeeded", hold="60"),
        result={"candidates": [{"rank": 1}, {"rank": 2}]},
        campaign_label="=HYPERLINK(1)",
    )
    failed = _job("c-2", "failed")
    resp, seen = _get(client, "/jobs/export.csv?status=succeeded&tool=bindcraft",
                      [done, failed], [_tx(60, "-12.50")])
    assert resp.status_code == 200 and resp.mimetype == "text/csv"
    assert seen[0]["status"] == "succeeded" and seen[0]["page_size"] == 100
    rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
    assert rows[0] == ["submitted_utc", "completed_utc", "job_id", "tool", "preset",
                       "status", "group", "designs", "url"]
    assert "$" not in resp.get_data(as_text=True)
    assert rows[1][2] == "c-1" and rows[1][7] == "2" and rows[1][6] == "'=HYPERLINK(1)"
    assert rows[1][8].endswith("/jobs/c-1")
    assert rows[2][2] == "c-2" and rows[2][7] == ""
