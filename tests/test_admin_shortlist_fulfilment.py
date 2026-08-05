"""The admin fulfilment page must show WHICH designs a shortlist named.

Found while wiring the target branch, and it is not a target-only problem:
``templates/admin/campaign_detail.html`` rendered
``campaign.candidate_indices | join(', ')`` for every non-API row. A 'campaign'
row -- live since migration 0037 -- has that column empty by its own CHECK
constraint, so every campaign-sourced handoff has been shown to ops as a
scoping request with no candidates and a "Source job" of "—". Filed as A84.

The second half of this file is the follow-up: ``candidate_refs`` is JSON off a
database row, not something the app's own writer is the only possible source
of, and the page promotes ``len(refs)`` to a design count ops fulfils against.
A non-mapping element used to 500 the page (A-5), a mapping with no job_id was
counted in the total but in none of the numbers beneath it (A-4), and repeats
or an index past the end of the source job inflated the total (A-6).

Rendered through the real route so the assertions read the page an operator
actually gets, not the helper in isolation.
"""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_STAFF = "leo@ranomics.com"
_TID = str(uuid.uuid4())
_CID = str(uuid.uuid4())


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


def _campaign(**over):
    from shared.campaigns import Campaign
    row = {
        "id": str(uuid.uuid4()),
        "user_id": "u-1",
        "target_name": "HER2",
        "assay_type": "yeast_display",
        "budget_band": "pilot",
        "status": "submitted",
        "submission_source": "web",
        "candidate_indices": [],
    }
    row.update(over)
    return Campaign.from_row(row)


def _job(jid, tool, n_results=None):
    """A source job as ``shared.jobs.get_job`` returns it.

    ``n_results`` gives the job a result carrying that many candidate records,
    which is what the page checks a ref's index against. Left None where the
    test is not about index validity: a job whose result cannot be read has an
    unknown record count, and the page deliberately says nothing about the
    indices of such a job rather than flagging all of them.
    """
    result = None
    if n_results is not None:
        result = {"candidates": [{"name": f"d{i}"} for i in range(n_results)]}
    return SimpleNamespace(id=jid, tool=tool, result=result)


def _render(client, campaign, jobs=None):
    jobs = jobs or {}
    with patch("shared.campaigns.get_campaign", return_value=campaign), \
            patch("shared.jobs.get_job",
                  side_effect=lambda jid, **kw: jobs.get(jid)):
        resp = client.get(f"/admin/lab-projects/{campaign.id}")
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def _candidates_block(html):
    """The whitespace-collapsed <dd> the Candidates row renders.

    Scoped to that block so the tool-breakdown regex below cannot pick up a
    ``slug (n)`` pair from anywhere else on the fulfilment page.
    """
    after = html.split("Candidates</dt>", 1)[1]
    return " ".join(after.split("</dd>", 1)[0].split())


def _numbers(html):
    """(printed design count, {tool: n}, unresolved, out-of-range) off the page.

    Reads what an operator reads, because "the numbers do not add up" is a
    claim about the rendered page rather than about the helper's return value.
    """
    block = _candidates_block(html)
    total = int(re.search(r"(\d+) designs? from \d+ jobs?", block).group(1))
    breakdown = {m.group(1): int(m.group(2))
                 for m in re.finditer(r"([a-z0-9_]+) \((\d+)\)", block)}
    unresolved = re.search(r"(\d+) designs? could not be attributed", block)
    past_end = re.search(r"(\d+) designs? points? past the end", block)
    return (
        total,
        breakdown,
        int(unresolved.group(1)) if unresolved else 0,
        int(past_end.group(1)) if past_end else 0,
    )


def test_a_target_row_shows_its_design_count_and_tool_breakdown(client):
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[
            {"job_id": "j-bc", "index": 0},
            {"job_id": "j-bc", "index": 3},
            {"job_id": "j-px", "index": 1},
        ],
    )
    html = _render(client, campaign,
                   {"j-bc": _job("j-bc", "bindcraft"),
                    "j-px": _job("j-px", "pxdesign")})
    assert "3 designs from 2 jobs" in " ".join(html.split())
    assert "bindcraft (2)" in html
    assert "pxdesign (1)" in html
    assert f"/targets/{_TID}" in html


def test_a_campaign_row_gets_the_same_treatment(client):
    """The pre-existing half of A84. This shape shipped with 0037 and has been
    rendering an empty candidate list ever since; the target branch renders
    identically because both read candidate_refs."""
    campaign = _campaign(
        submission_source="campaign",
        source_campaign_id=_CID,
        candidate_refs=[{"job_id": "j-bc", "index": i} for i in range(4)],
    )
    html = _render(client, campaign, {"j-bc": _job("j-bc", "bindcraft")})
    assert "4 designs from 1 job" in " ".join(html.split())
    assert "bindcraft (4)" in html
    assert f"/campaigns/{_CID}" in html


def test_a_legacy_single_job_row_still_shows_its_indices(client):
    """The arm that must NOT change. A 'web' row keeps its shortlist in
    candidate_indices, and that rendering is correct for it."""
    campaign = _campaign(
        submission_source="web",
        source_job_id="j-solo",
        candidate_indices=[0, 2, 5],
    )
    html = _render(client, campaign)
    assert "0, 2, 5" in html
    assert "(indices, 0-based)" in html
    assert "/jobs/j-solo" in html


def test_a_design_whose_source_job_cannot_be_read_is_disclosed_not_dropped(client):
    """A tool breakdown that quietly omits rows is worse than one that says so:
    the counts would silently disagree with the design count beside them."""
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[{"job_id": "j-bc", "index": 0},
                        {"job_id": "j-gone", "index": 0}],
    )
    html = _render(client, campaign, {"j-bc": _job("j-bc", "bindcraft")})
    flat = " ".join(html.split())
    assert "2 designs from 2 jobs" in flat
    assert "bindcraft (1)" in html
    assert "1 design could not be attributed to a tool" in flat


def test_job_lookups_are_deduped_by_job_id(client):
    """40 refs across 2 jobs must be 2 reads, not 40. The fulfilment page is a
    staff GET, but a shortlist can legitimately carry hundreds of refs."""
    seen: list[str] = []

    def fake_get_job(jid, **kw):
        seen.append(jid)
        return _job(jid, "bindcraft" if jid == "j-a" else "pxdesign")

    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[
            {"job_id": "j-a" if i % 2 else "j-b", "index": i} for i in range(40)
        ],
    )
    with patch("shared.campaigns.get_campaign", return_value=campaign), \
            patch("shared.jobs.get_job", side_effect=fake_get_job):
        resp = client.get(f"/admin/lab-projects/{campaign.id}")
    assert resp.status_code == 200
    assert sorted(seen) == ["j-a", "j-b"], seen


# ---------------------------------------------------------------------------
# A-5 / A-4 / A-6 — the stored list is untrusted JSON, and the count is a claim
# ---------------------------------------------------------------------------


def test_a_non_mapping_ref_does_not_break_the_page_and_is_disclosed(client):
    """A-5. ``candidate_refs`` comes back from the database, so a bare string
    or an int can appear in it. Both used to reach ``.get`` and raise
    AttributeError, taking down the only page on which ops can see what a
    customer ordered."""
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[{"job_id": "j-bc", "index": 0}, "j-bc:2", 7],
    )
    html = _render(client, campaign, {"j-bc": _job("j-bc", "bindcraft")})
    flat = " ".join(html.split())
    assert "1 design from 1 job" in flat
    assert "bindcraft (1)" in html
    assert "Submitted list held 3 entries; 2 unreadable not counted above" in flat


def test_a_ref_with_no_job_id_is_disclosed_not_silently_dropped(client):
    """A-4. A mapping with no usable job_id reached neither the tool breakdown
    nor the unresolved line, yet was counted in the design total printed above
    both, so the page contradicted itself."""
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[{"job_id": "j-bc", "index": 0},
                        {"index": 5},
                        {"job_id": "", "index": 6}],
    )
    html = _render(client, campaign, {"j-bc": _job("j-bc", "bindcraft")})
    flat = " ".join(html.split())
    assert "1 design from 1 job" in flat
    assert "bindcraft (1)" in html
    assert "Submitted list held 3 entries; 2 unreadable not counted above" in flat


def test_the_same_design_named_twice_is_one_design(client):
    """A-6. A repeated (job_id, index) is the same physical design; counting it
    twice tells ops to order it twice."""
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[{"job_id": "j-bc", "index": 0},
                        {"job_id": "j-bc", "index": 0},
                        {"job_id": "j-bc", "index": 1}],
    )
    html = _render(client, campaign, {"j-bc": _job("j-bc", "bindcraft")})
    flat = " ".join(html.split())
    assert "2 designs from 1 job" in flat
    assert "bindcraft (2)" in html
    assert "Submitted list held 3 entries; 1 duplicate not counted above" in flat


def test_a_ref_past_the_end_of_its_source_job_is_not_a_tool_design(client):
    """A-6. The source job is read anyway for its tool slug, so its record
    count is already in hand: an index past the end names nothing ops can
    pull, and must not be counted under the tool as though it did."""
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[{"job_id": "j-bc", "index": 0},
                        {"job_id": "j-bc", "index": 1},
                        {"job_id": "j-bc", "index": 9}],
    )
    html = _render(client, campaign, {"j-bc": _job("j-bc", "bindcraft", n_results=2)})
    flat = " ".join(html.split())
    assert "3 designs from 1 job" in flat
    assert "bindcraft (2)" in html
    assert "1 design points past the end of the source job's results" in flat


def test_an_index_is_not_flagged_when_the_source_job_has_no_readable_results(client):
    """Pair test for the one above. ``candidate_records`` returns [] both for a
    job with no results and for a result shape it cannot read, so a length of
    zero is 'unknown', not 'everything is out of range'. A fix that flagged on
    ``index >= len(records)`` unconditionally would report all 3 here."""
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[{"job_id": "j-bc", "index": i} for i in range(3)],
    )
    html = _render(client, campaign, {"j-bc": _job("j-bc", "bindcraft")})
    flat = " ".join(html.split())
    assert "3 designs from 1 job" in flat
    assert "bindcraft (3)" in html
    assert "past the end" not in flat


def test_a_tool_with_nothing_pullable_is_not_printed_as_zero(client):
    """A tool whose every ref is past the end contributes no design, and
    ``slug (0)`` in the breakdown would read as a tool ops has work for.
    ``_source_tools_line`` drops zero-count tools from the staff email the same
    way, and the two are supposed to agree."""
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[{"job_id": "j-bc", "index": 0},
                        {"job_id": "j-px", "index": 5}],
    )
    html = _render(client, campaign,
                   {"j-bc": _job("j-bc", "bindcraft", n_results=4),
                    "j-px": _job("j-px", "pxdesign", n_results=2)})
    block = _candidates_block(html)
    assert "2 designs from 2 jobs" in block
    assert "bindcraft (1)" in block
    assert "pxdesign" not in block
    assert "1 design points past the end of the source job's results" in block


def test_a_clean_shortlist_discloses_nothing(client):
    """Pair test for the disclosures. A fix that always prints a duplicate or
    unreadable line, or that drops refs to make the arithmetic total, fails
    here while passing every test above."""
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[{"job_id": "j-bc", "index": 0},
                        {"job_id": "j-px", "index": 1}],
    )
    html = _render(client, campaign,
                   {"j-bc": _job("j-bc", "bindcraft", n_results=4),
                    "j-px": _job("j-px", "pxdesign", n_results=4)})
    flat = " ".join(html.split())
    assert "2 designs from 2 jobs" in flat
    assert "bindcraft (1)" in html
    assert "pxdesign (1)" in html
    assert "Submitted list held" not in flat
    assert "could not be attributed" not in flat
    assert "past the end" not in flat


def test_the_printed_design_count_equals_the_numbers_printed_beneath_it(client):
    """The whole point of A-4 and A-6, on one row carrying every case at once.

    ``count`` is computed from the deduplicated refs and the three buckets are
    accumulated separately, so this equality is a real assertion rather than a
    restatement of how the total is built.
    """
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[
            {"job_id": "j-bc", "index": 0},
            {"job_id": "j-bc", "index": 1},
            {"job_id": "j-bc", "index": 1},      # duplicate
            {"job_id": "j-bc", "index": 9},      # past the end of j-bc
            {"job_id": "j-px", "index": 2},      # tool known, length unknown
            {"job_id": "j-gone", "index": 0},    # source job unreadable
            "j-bc:4",                            # not a mapping
            {"index": 4},                        # no job_id
        ],
    )
    html = _render(client, campaign,
                   {"j-bc": _job("j-bc", "bindcraft", n_results=3),
                    "j-px": _job("j-px", "pxdesign")})
    total, breakdown, unresolved, past_end = _numbers(html)
    assert total == sum(breakdown.values()) + unresolved + past_end
    # Pinned separately, because the equality above also holds for a fix that
    # drops every ref and prints zeroes across the board.
    flat = " ".join(html.split())
    assert "5 designs from 3 jobs" in flat
    assert (
        "Submitted list held 8 entries; 1 duplicate and 2 unreadable "
        "not counted above"
    ) in flat
