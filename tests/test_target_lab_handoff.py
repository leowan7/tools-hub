"""POST /lab-projects/submit with a TARGET-wide shortlist (Phase 5.3).

The target branch is the first shortlist path whose refs can name jobs reached
by two different routes -- compute-campaign sub-jobs and target-tagged
standalone jobs -- so its acceptance test is wider than the campaign branch's
and every widening is a place a foreign design could get in. Most of this file
is that boundary, from both sides: each rejection test is paired with an
acceptance test that the same over-broad or over-narrow gate would fail.

Everything below the route is patched at ITS OWN module, matching how
``blueprints/lab_projects.py`` imports it: ``get_job`` and
``stage_campaign_candidates`` are module-level imports on the blueprint, while
``get_target``, ``campaign_ids_for_target``, ``create_campaign_from_target_refs``
and ``send_campaign_submitted_emails`` are function-local and therefore resolve
against their own modules at call time.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_TID = str(uuid.uuid4())
_OTHER_TID = str(uuid.uuid4())
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
    return app.test_client()


def _ctx(user_id="u-1"):
    return SimpleNamespace(
        user_id=user_id, tier="free", balance=100, email="u@example.com",
    )


def _login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"


def _job(jid, tool, *, target_id=None, campaign_id=None, n=3, owner="u-1"):
    """A succeeded job whose result carries ``n`` candidates.

    Every tool names its first design ``design_1.pdb``; that collision is the
    reason the staging prefix carries the tool slug, so the fixture reproduces
    it rather than making the names unique.

    ``owner`` exists so the fake ``get_job`` can enforce the owner scope the
    real one enforces. A fake that ignores ``user_id`` would let the tenancy
    test pass against a route that had stopped passing it.
    """
    return SimpleNamespace(
        id=jid, tool=tool, status="succeeded", user_id=owner,
        target_id=target_id, campaign_id=campaign_id,
        result={"candidates": [
            {"pdb_key": f"design_{i + 1}.pdb", "scores": {"ipTM": 0.9}}
            for i in range(n)
        ]},
    )


class _Harness:
    """Captured side effects of one submit."""

    def __init__(self):
        self.created: list[dict] = []
        self.staged: list[dict] = []
        self.emails: list[dict] = []
        self.job_lookups: list[str] = []


def _submit(client, jobs, *, form=None, target=object(), campaign_ids=(_CID,)):
    """Drive POST /lab-projects/submit and return (response, harness).

    ``jobs`` maps job id -> job (or None for "not the caller's"), which is
    exactly what an owner-scoped ``get_job`` returns.
    """
    h = _Harness()

    def fake_get_job(jid, *, user_id=None):
        # Models shared.jobs.get_job: `user_id` is applied as a QUERY filter,
        # so a job that exists but belongs to someone else comes back as None,
        # indistinguishable from absent. Enforcing it here is what makes the
        # tenancy test able to fail: a fake that returned the row regardless
        # would stay green against a route that dropped the scope.
        h.job_lookups.append(jid)
        job = jobs.get(jid)
        if job is None:
            return None
        if user_id is not None and job.user_id != user_id:
            return None
        return job

    def fake_create(**kw):
        h.created.append(kw)
        return SimpleNamespace(id="lab-1", **{
            k: v for k, v in kw.items() if k != "user_id"
        })

    def fake_stage(**kw):
        h.staged.append(kw)
        return []

    def fake_email(**kw):
        h.emails.append(kw)

    body = {
        "source_target_id": _TID,
        "candidate_refs": json.dumps([{"job_id": "j-bc", "index": 0}]),
        "target_name": "HER2",
        "assay_type": "yeast_display",
        "budget_band": "pilot",
    }
    body.update(form or {})

    _login(client)
    with patch("blueprints.lab_projects.load_user_context", return_value=_ctx()), \
            patch("blueprints.lab_projects.get_job", side_effect=fake_get_job), \
            patch("blueprints.lab_projects.stage_campaign_candidates",
                  side_effect=fake_stage), \
            patch("shared.targets.get_target", return_value=target), \
            patch("shared.targets.campaign_ids_for_target",
                  return_value=list(campaign_ids)), \
            patch("shared.campaigns.create_campaign_from_target_refs",
                  side_effect=fake_create), \
            patch("shared.email.send_campaign_submitted_emails",
                  side_effect=fake_email):
        resp = client.post("/lab-projects/submit", data=body)
    return resp, h


# ---------------------------------------------------------------------------
# The happy paths. Both routes into a target must be accepted.
# ---------------------------------------------------------------------------

def test_a_campaign_subjob_is_accepted(client):
    """Reached via ``job.campaign_id in campaign_ids_for_target(target)``. This
    is how every design produced by a multi-tool launch arrives."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs)
    assert resp.status_code == 302
    assert len(h.created) == 1
    assert h.created[0]["source_target_id"] == _TID
    assert h.created[0]["candidate_refs"] == [{"job_id": "j-bc", "index": 0}]


def test_a_target_tagged_standalone_job_is_accepted(client):
    """The second route. A run launched from an atomic tool form with a
    ``target:`` reuse token has ``campaign_id`` NULL and ``target_id`` set, and
    ``aggregate_target_candidates`` pools its designs into the same table, so a
    gate that checked only campaign parentage would refuse rows the user can
    see and star."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", target_id=_TID)}
    resp, h = _submit(client, jobs)
    assert resp.status_code == 302
    assert len(h.created) == 1
    assert h.created[0]["candidate_refs"] == [{"job_id": "j-bc", "index": 0}]


# ---------------------------------------------------------------------------
# THE IDOR BOUNDARY
# ---------------------------------------------------------------------------

def test_a_ref_naming_a_job_that_does_not_exist_creates_nothing(client):
    resp, h = _submit(client, {})          # get_job returns None for every id
    assert resp.status_code == 302
    assert h.created == []
    assert h.staged == []


def test_a_ref_naming_another_users_job_creates_nothing(client):
    """TENANCY, and the sharpest test in this file.

    The job EXISTS, is attached to this very target's campaign, and would sail
    through the parentage test below. The only thing standing between it and
    the lab is that ``get_job`` is called with the caller's user_id, so the
    owner filter makes it come back as None. Drop that keyword and this is a
    cross-tenant read of another lab's designs, staged into a folder Ranomics
    staff will open.
    """
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID, owner="u-2")}
    resp, h = _submit(client, jobs)
    assert resp.status_code == 302
    assert h.created == []
    assert h.staged == []


def test_a_ref_naming_the_callers_own_job_on_another_target_creates_nothing(client):
    """Provenance, which tenancy does NOT cover. This job is the caller's own
    and resolves fine; it simply belongs to a different target. Dropping the
    parentage test would stage a design from an unrelated protein into this
    submission's folder and bill a scoping request against it."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", target_id=_OTHER_TID,
                         campaign_id=str(uuid.uuid4()))}
    resp, h = _submit(client, jobs)
    assert resp.status_code == 302
    assert h.created == []
    assert h.staged == []


def test_a_foreign_target_creates_nothing(client):
    """The parent gate. ``get_target`` is owner-scoped, so a target id that is
    not the caller's returns None before any job is read at all."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs, target=None)
    assert resp.status_code == 302
    assert h.created == []
    assert h.job_lookups == []


def test_only_the_accepted_refs_reach_the_insert(client):
    """A mixed body must be filtered, not rejected wholesale and not accepted
    wholesale. Two of these four refs are legitimate."""
    jobs = {
        "j-bc": _job("j-bc", "bindcraft", campaign_id=_CID),
        "j-far": _job("j-far", "boltzgen", target_id=_OTHER_TID),
        # "j-theirs" is absent: an owner-scoped get_job answers None.
    }
    refs = [
        {"job_id": "j-bc", "index": 0},
        {"job_id": "j-far", "index": 1},
        {"job_id": "j-theirs", "index": 0},
        {"job_id": "j-bc", "index": 2},
    ]
    resp, h = _submit(client, jobs,
                      form={"candidate_refs": json.dumps(refs)})
    assert resp.status_code == 302
    assert h.created[0]["candidate_refs"] == [
        {"job_id": "j-bc", "index": 0}, {"job_id": "j-bc", "index": 2},
    ]


def test_a_repeated_rejected_job_id_is_looked_up_once(client):
    """Request amplification. A miss never writes to the job cache, so without
    a negative cache a body naming one foreign job 500 times issues 500
    identical Supabase round trips from a single POST."""
    refs = [{"job_id": "j-theirs", "index": i} for i in range(40)]
    resp, h = _submit(client, {}, form={"candidate_refs": json.dumps(refs)})
    assert resp.status_code == 302
    assert h.job_lookups == ["j-theirs"], h.job_lookups


def test_the_shortlist_is_capped_before_any_lookup(client):
    """The cap is applied at PARSE time, so an oversized body cannot reach the
    per-ref loop at all. 900 distinct job ids, 500 accepted."""
    from blueprints.lab_projects import _MAX_CANDIDATE_REFS
    refs = [{"job_id": f"j-{i}", "index": 0} for i in range(900)]
    resp, h = _submit(client, {}, form={"candidate_refs": json.dumps(refs)})
    assert resp.status_code == 302
    assert len(h.job_lookups) == _MAX_CANDIDATE_REFS


# ---------------------------------------------------------------------------
# Staging: two tools, one file name
# ---------------------------------------------------------------------------

def test_the_staging_prefix_namespaces_by_tool_so_two_tools_do_not_collide(client):
    """Every tool emits ``design_1.pdb``. The campaign branch prefixes with the
    job's first 8 hex digits alone, which is enough there because a campaign
    has one tool; a target pools many, so the slug goes in the prefix and the
    lab receives one object per shortlisted design instead of one object for
    two."""
    jobs = {
        "j-bc": _job("j-bc", "bindcraft", campaign_id=_CID),
        "j-px": _job("j-px", "pxdesign", target_id=_TID),
    }
    refs = [{"job_id": "j-bc", "index": 0}, {"job_id": "j-px", "index": 0}]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)})
    assert resp.status_code == 302

    prefixes = sorted(call["prefix"] for call in h.staged)
    assert prefixes == ["bindcraft-j-bc/", "pxdesign-j-px/"], prefixes
    # The collision this prevents, spelled out: both refs name index 0, and
    # both jobs' index 0 is design_1.pdb.
    staged_names = {
        call["prefix"] + call["candidates"][i]["pdb_key"]
        for call in h.staged for i in call["indices"]
    }
    assert len(staged_names) == 2, staged_names


def test_each_source_job_is_staged_with_its_own_indices(client):
    """Refs are grouped per source job before staging, so one job is staged
    once with all of its indices rather than once per starred design."""
    jobs = {
        "j-bc": _job("j-bc", "bindcraft", campaign_id=_CID),
        "j-px": _job("j-px", "pxdesign", target_id=_TID),
    }
    refs = [{"job_id": "j-bc", "index": 0}, {"job_id": "j-px", "index": 2},
            {"job_id": "j-bc", "index": 1}]
    _, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)})
    by_job = {c["job_id"]: c["indices"] for c in h.staged}
    assert by_job == {"j-bc": [0, 1], "j-px": [2]}, by_job


# ---------------------------------------------------------------------------
# Dispatch order
# ---------------------------------------------------------------------------

def test_the_dispatcher_prefers_the_target_branch(client):
    """A body carrying BOTH parent fields must take the target branch. The
    campaign branch's parentage test is narrower in a different direction (it
    accepts any sub-job of one campaign, including one whose target is not this
    one), so letting a crafted body pick which test to face is the whole reason
    the order is fixed rather than incidental."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(
        client, jobs, form={"source_campaign_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 302
    assert len(h.created) == 1
    # create_campaign_from_TARGET_refs is the only creator patched, so a
    # created row at all proves the branch; the kwarg proves the parent.
    assert h.created[0]["source_target_id"] == _TID


def test_a_campaign_shortlist_is_untouched_by_the_new_branch(client):
    """The pair. Adding a branch above the campaign one must not capture it."""
    captured: list[dict] = []

    def fake_create(**kw):
        captured.append(kw)
        return SimpleNamespace(id="lab-2", **{
            k: v for k, v in kw.items() if k != "user_id"
        })

    body = {
        "source_campaign_id": _CID,
        "candidate_refs": json.dumps([{"job_id": "j-bc", "index": 0}]),
        "target_name": "HER2",
        "assay_type": "yeast_display",
        "budget_band": "pilot",
    }
    _login(client)
    with patch("blueprints.lab_projects.load_user_context", return_value=_ctx()), \
            patch("blueprints.lab_projects.get_job",
                  return_value=_job("j-bc", "bindcraft", campaign_id=_CID)), \
            patch("blueprints.lab_projects.stage_campaign_candidates",
                  return_value=[]), \
            patch("shared.compute_campaigns.get_campaign",
                  return_value=SimpleNamespace(id=_CID)), \
            patch("shared.campaigns.create_campaign_from_refs",
                  side_effect=fake_create), \
            patch("shared.email.send_campaign_submitted_emails"):
        resp = client.post("/lab-projects/submit", data=body)
    assert resp.status_code == 302
    assert len(captured) == 1
    assert captured[0]["source_campaign_id"] == _CID


# ---------------------------------------------------------------------------
# The email's per-tool breakdown
# ---------------------------------------------------------------------------

def test_the_email_reports_designs_per_tool_not_jobs_per_tool(client):
    """"Designs from: bindcraft (2), pxdesign (1)". Counted over the ACCEPTED
    refs, so a rejected ref cannot inflate it and one job contributing two
    starred designs counts as two."""
    jobs = {
        "j-bc": _job("j-bc", "bindcraft", campaign_id=_CID),
        "j-px": _job("j-px", "pxdesign", target_id=_TID),
        "j-far": _job("j-far", "boltzgen", target_id=_OTHER_TID),
    }
    refs = [{"job_id": "j-bc", "index": 0}, {"job_id": "j-bc", "index": 1},
            {"job_id": "j-px", "index": 0}, {"job_id": "j-far", "index": 0}]
    _, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)})
    assert len(h.emails) == 1
    assert h.emails[0]["source_tools"] == {"bindcraft": 2, "pxdesign": 1}


# ---------------------------------------------------------------------------
# The customer's own lab-project page
# ---------------------------------------------------------------------------

def test_the_lab_project_page_counts_a_target_shortlist_and_links_the_target(client):
    """`candidate_indices` is empty by CHECK on a 'target' row, exactly as it
    is on a 'campaign' row, so the page's `if submission_source == 'campaign'`
    test printed "0 shortlisted" for every target handoff and offered no way
    back to the target."""
    from shared.campaigns import Campaign
    campaign = Campaign.from_row({
        "id": "lab-9", "user_id": "u-1", "target_name": "HER2",
        "assay_type": "yeast_display", "budget_band": "pilot",
        "status": "submitted", "submission_source": "target",
        "source_target_id": _TID, "candidate_indices": [],
        "candidate_refs": [{"job_id": "j-bc", "index": 0},
                           {"job_id": "j-px", "index": 1}],
    })
    _login(client)
    with patch("blueprints.lab_projects.load_user_context", return_value=_ctx()), \
            patch("shared.campaigns.get_campaign", return_value=campaign):
        resp = client.get("/lab-projects/lab-9")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "2 shortlisted" in html
    assert f"/targets/{_TID}" in html
    assert "View source target" in html


def test_an_empty_target_shortlist_returns_to_the_target_not_to_jobs(client):
    """Phase 5.2 made the send button live at zero stars, so this body is now
    reachable from the UI. Gated on `and candidate_refs`, it fell through both
    ref branches to the legacy single-job one, which has no source_job_id and
    redirects to /jobs -- an unrelated list."""
    resp, h = _submit(client, {}, form={"candidate_refs": "[]"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{_TID}")
    assert h.created == []
    assert h.job_lookups == []
