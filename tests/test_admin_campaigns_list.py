"""The admin lab-order queue must count every arm's shortlist.

``templates/admin/campaigns_list.html`` rendered ``candidate_indices | length``
under its "Cands" header. A 'campaign' row (migration 0037) and a 'target' row
(0040) both keep the shortlist in ``candidate_refs`` and leave
``candidate_indices`` at the column's empty default (0011), so ops scanning
/admin/lab-projects read 0 designs on every ref-based order. Filed as A92. The
user-facing sibling list (``templates/campaigns/dashboard.html``) already
branched this way; the admin list did not.

Driven through the real route, against a fake Supabase client that HONOURS the
column projection it is handed. That is deliberate: a template that renders a
column the query never fetched is not a fix, so these tests have to fail if
``list_all_campaigns`` stops selecting ``candidate_refs``, not merely if the
template stops reading it.
"""

from __future__ import annotations

import re
import uuid
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_STAFF = "leo@ranomics.com"


# ---------------------------------------------------------------------------
# A Supabase double that projects
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """Supports the chain ``list_all_campaigns`` actually builds.

    ``select("*")`` returns the seeded rows whole; any explicit column list
    returns only those keys, which is what PostgREST does. ``eq`` records the
    filter and ``execute`` applies it; that is real database behaviour and
    test_a_status_filter_still_reaches_the_query rests on it -- stubbing ``eq``
    out the way ``order`` is stubbed makes that test fail. ``limit`` slices,
    which the route does call, but no fixture here reaches it (the route asks
    for 200 and no test seeds more than four rows). ``order`` is accepted and
    ignored, because these tests key their assertions on target_name rather
    than on row order.
    """

    def __init__(self, rows, record):
        self._rows = rows
        self._record = record
        self._cols = "*"
        self._filters: list[tuple[str, object]] = []
        self._limit = None

    def select(self, cols="*", *_a, **_k):
        self._cols = cols
        self._record.append(cols)
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = [
            r for r in self._rows
            if all(r.get(c) == v for c, v in self._filters)
        ]
        if self._limit is not None:
            rows = rows[: self._limit]
        if str(self._cols).strip() != "*":
            keep = {c.strip() for c in str(self._cols).split(",")}
            rows = [{k: v for k, v in r.items() if k in keep} for r in rows]
        return _Resp([dict(r) for r in rows])


class _Client:
    def __init__(self, rows, record):
        self._rows = rows
        self._record = record

    def table(self, name):
        assert name == "lab_campaigns", name
        return _Query(self._rows, self._record)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = "admin-1"
        sess["user_email"] = _STAFF
    return c


def _row(**over):
    """A lab_campaigns row as the database stores it, before Campaign.from_row.

    Defaults to the 'web' shape. candidate_refs defaults to None, which is what
    the column holds on a 'web' row -- a shape that takes the template's
    candidate_indices arm and so never reaches its ``or []`` guard. That guard
    is exercised only by
    test_a_ref_row_with_null_candidate_refs_renders_a_zero_not_an_error, which
    overrides the source to a ref one and leaves the column NULL.
    """
    row = {
        "id": str(uuid.uuid4()),
        "user_id": "user-abcdef123456",
        "source_job_id": str(uuid.uuid4()),
        "target_name": "HER2",
        "assay_type": "yeast_display",
        "budget_band": "pilot",
        "status": "submitted",
        "submission_source": "web",
        "candidate_indices": [],
        "candidate_refs": None,
        "created_at": "2026-08-01T10:00:00Z",
    }
    row.update(over)
    return row


def _refs(job_id, n):
    return [{"job_id": job_id, "index": i} for i in range(n)]


def _get_list(client, rows, *, query=""):
    """GET the admin list, serving `rows` through the projecting double.

    Returns ``(html, selects)`` where ``selects`` is every column projection
    the route asked for.
    """
    selects: list = []
    with patch("shared.campaigns.get_service_client",
               return_value=_Client(rows, selects)):
        resp = client.get(f"/admin/lab-projects{query}")
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True), selects


def _cands_by_target(html):
    """Map target_name -> the rendered text of that row's "Cands" cell.

    Reads the 1st and 4th <td> of every body row, which is where the template's
    header order puts Target and Cands. Header cells are <th>, so the header
    row yields no <td> and drops out.
    """
    out = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [
            re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        ]
        if len(cells) >= 4:
            out[cells[0]] = cells[3]
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_a_campaign_sourced_row_shows_its_candidate_refs_count(client):
    html, _ = _get_list(client, [
        _row(target_name="CAMPAIGN-ROW",
             submission_source="campaign",
             source_job_id=None,
             source_campaign_id=str(uuid.uuid4()),
             candidate_indices=[],
             candidate_refs=_refs("job-a", 4)),
    ])
    assert _cands_by_target(html)["CAMPAIGN-ROW"] == "4"


def test_a_target_sourced_row_shows_its_candidate_refs_count(client):
    """The arm a 'campaign'-only branch would have left at 0.

    dashboard.html records this mistake being made and fixed once already on
    the user-facing list, which is why it is asserted separately here rather
    than folded into the campaign case.
    """
    html, _ = _get_list(client, [
        _row(target_name="TARGET-ROW",
             submission_source="target",
             source_job_id=None,
             source_target_id=str(uuid.uuid4()),
             candidate_indices=[],
             candidate_refs=_refs("job-b", 2)),
    ])
    assert _cands_by_target(html)["TARGET-ROW"] == "2"


def test_a_web_row_still_counts_candidate_indices(client):
    """'web' keeps its shortlist in candidate_indices and has candidate_refs
    NULL, so routing it to refs would replace one wrong number with another."""
    html, _ = _get_list(client, [
        _row(target_name="WEB-ROW", candidate_indices=[0, 1, 2]),
    ])
    assert _cands_by_target(html)["WEB-ROW"] == "3"


def test_an_api_row_still_counts_candidate_indices(client):
    """create_api_campaign fills candidate_indices with range(len(sequences))
    and leaves candidate_refs NULL, so 'api' was never a broken arm."""
    html, _ = _get_list(client, [
        _row(target_name="API-ROW",
             submission_source="api",
             source_job_id=None,
             sequences={"a": "MK", "b": "MV"},
             candidate_indices=[0, 1]),
    ])
    assert _cands_by_target(html)["API-ROW"] == "2"


def test_all_four_arms_are_counted_correctly_on_one_page(client):
    """The whole point of the column: one queue, four shapes, right number on
    each. Asserted together because ops read them side by side."""
    html, _ = _get_list(client, [
        _row(target_name="WEB-ROW", candidate_indices=[0, 1, 2]),
        _row(target_name="API-ROW", submission_source="api",
             source_job_id=None, candidate_indices=[0, 1]),
        _row(target_name="CAMPAIGN-ROW", submission_source="campaign",
             source_job_id=None, source_campaign_id=str(uuid.uuid4()),
             candidate_refs=_refs("job-a", 4)),
        _row(target_name="TARGET-ROW", submission_source="target",
             source_job_id=None, source_target_id=str(uuid.uuid4()),
             candidate_refs=_refs("job-b", 2)),
    ])
    assert _cands_by_target(html) == {
        "WEB-ROW": "3",
        "API-ROW": "2",
        "CAMPAIGN-ROW": "4",
        "TARGET-ROW": "2",
    }


def test_a_ref_row_with_null_candidate_refs_renders_a_zero_not_an_error(client):
    """Campaign.candidate_refs is Optional and from_row passes row.get straight
    through, so a NULL on a row claiming a ref source reaches the template as
    None. Jinja's length filter raises TypeError on None -- which loses the
    whole page, not one cell -- so the template guards with ``or []``. The
    database CHECK forbids this shape today; the template does not lean on
    that."""
    html, _ = _get_list(client, [
        _row(target_name="NULL-REFS", submission_source="target",
             source_job_id=None, source_target_id=str(uuid.uuid4()),
             candidate_indices=[], candidate_refs=None),
    ])
    assert _cands_by_target(html)["NULL-REFS"] == "0"


def test_the_list_query_fetches_candidate_refs(client):
    """The count above is only real if the route asks for the column.

    Pinned as "either the whole row or an explicit list naming it", so
    narrowing the projection later is allowed but silently dropping this column
    is not. The projecting double above is what makes the other tests fail if
    it ever is dropped; this asserts it directly so the reason is legible.
    """
    _, selects = _get_list(client, [_row()])
    assert selects, "route issued no select"
    for cols in selects:
        assert str(cols).strip() == "*" or "candidate_refs" in str(cols), cols


def test_a_status_filter_still_reaches_the_query(client):
    """The status chips filter server-side: ?status=scoped reaches the query as
    an eq() on status, so a non-matching row is absent from the page rather
    than rendered at all. Asserted together with the Cands count because the
    filtered page is the one ops reads, and both have to be right on the same
    request."""
    html, _ = _get_list(
        client,
        [
            _row(target_name="SUBMITTED-ROW", status="submitted"),
            _row(target_name="SCOPED-ROW", status="scoped",
                 candidate_indices=[0, 1, 2, 3, 4]),
        ],
        query="?status=scoped",
    )
    counts = _cands_by_target(html)
    assert counts == {"SCOPED-ROW": "5"}
