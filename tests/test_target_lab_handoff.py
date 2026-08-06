"""POST /lab-projects/submit with a TARGET-wide shortlist (Phase 5.3).

The target branch is the first shortlist path whose refs can name jobs reached
by two different routes -- compute-campaign sub-jobs and target-tagged
standalone jobs -- so its acceptance test is wider than the campaign branch's
and every widening is a place a foreign design could get in. Most of this file
is that boundary, from both sides: each rejection test is paired with an
acceptance test that the same over-broad or over-narrow gate would fail.

Everything below the route is patched at ITS OWN module, matching how
``blueprints/lab_projects.py`` imports it: ``read_job`` and
``stage_campaign_candidates`` are module-level imports on the blueprint, while
``read_target``, ``campaign_ids_for_target``,
``create_campaign_from_target_refs`` and ``send_campaign_submitted_emails`` are
function-local and therefore resolve against their own modules at call time.

THE TARGET BRANCH READS JOBS THROUGH ``read_job``, NOT ``get_job``, and the
difference is the subject of half this file. ``get_job`` answers ``None`` for a
job that is absent, one that is another tenant's, and a read that never
completed; this route has to behave differently in the last case, so it uses the
form that reports which happened. ``_submit`` below therefore fakes ``read_job``
and can be told to make a specific id UNREADABLE, which is the only way to
construct the fault the refusal gate exists for.

IT READS ITS PARENT TARGET THE SAME WAY, through ``read_target`` (register item
A90). The gate used to refuse on ``get_target(...) is None``, which is the same
three facts in one value, and its exit LEAVES this page -- so a two-second
Supabase fault sent the user to /targets with no message at all. ``_submit``
takes ``target_unreadable=`` for that fault, separately from ``target=None``,
because the two outcomes now go to different places and a fixture that could
only build one of them cannot tell whether they are still apart.
"""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared.compute_campaigns import CAMPAIGN_READ_OK, CampaignRead
from shared.jobs import JOB_READ_ABSENT, JOB_READ_OK, JOB_READ_UNAVAILABLE, JobRead
from shared.targets import (
    TARGET_READ_ABSENT,
    TARGET_READ_OK,
    TARGET_READ_UNAVAILABLE,
    TargetRead,
)

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

    ``owner`` exists so the fake ``read_job`` can enforce the owner scope the
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
        # (id, user_id) per call. The user_id half is the point: it is the only
        # thing that fails when the route stops passing the owner scope, and
        # both of these reads previously went through a `return_value=` patch
        # that discarded it (register item A-1).
        self.target_lookups: list[tuple] = []
        self.campaign_id_lookups: list[tuple] = []


_CREATE_OK = object()


def _submit(client, jobs, *, form=None, target=object(), campaign_ids=(_CID,),
            campaign_ids_complete=True, create_result=_CREATE_OK,
            target_owner="u-1", campaign_owner="u-1", unreadable=(),
            target_unreadable=False):
    """Drive POST /lab-projects/submit and return (response, harness).

    ``jobs`` maps job id -> job. An id that is absent from the mapping is a job
    that is not there or is not the caller's, which an owner-scoped read reports
    as ``JOB_READ_ABSENT``.

    ``target_unreadable`` makes the PARENT target read fail -- reported as
    ``TARGET_READ_UNAVAILABLE``. Separate from ``target=None``, which is the
    target being absent, because those are the two outcomes the gate now tells
    apart: a fixture that folded them together could not show that it does.

    ``unreadable`` is the set of ids whose LOOKUP FAILS -- no service client, or
    the query raised -- reported as ``JOB_READ_UNAVAILABLE``. It is a separate
    parameter and not "absent with a flag" because that is the whole
    distinction: an absent job is a verdict about the job, an unreadable one is
    a statement about the database and says nothing about the job at all. An id
    may appear in ``jobs`` AND in ``unreadable`` at once, which is the realistic
    shape of a transient fault: the row is perfectly valid and we did not manage
    to read it.

    ``campaign_ids_complete`` is the second half of what
    ``campaign_ids_for_target`` returns: False means the id read was cut short
    by a fault or the page bound, so the ids are a prefix of the real set.
    Defaults True because that is the only shape a healthy read produces.

    ``create_result`` overrides what ``create_campaign_from_target_refs``
    returns, or -- when it is an ``Exception`` instance -- what it RAISES. The
    real function raises ``ValueError`` on an unknown assay_type or
    budget_band, so the route's ``except ValueError`` arm is live and needs a
    fake that can reach it. It is a parameter rather than an outer ``patch``
    because this helper patches that same name itself, and its patch is
    entered later and therefore wins.
    """
    h = _Harness()

    def fake_read_job(jid, *, user_id=None):
        # Models shared.jobs.read_job, including the two things about it that
        # matter here.
        #
        # 1. `user_id` is applied as a QUERY FILTER, so another tenant's job
        #    comes back as zero rows -- which is ABSENT, not a special
        #    "forbidden" outcome. The real function cannot tell those apart
        #    either, and must not: distinguishing them means reading a row the
        #    owner scope exists to withhold. A fake that returned the row
        #    regardless would stay green against a route that dropped the scope.
        # 2. An unreadable id reports UNAVAILABLE even when the job is present
        #    in `jobs`, because a read that did not complete learned nothing
        #    about the row. A fake that quietly returned the row instead would
        #    make the refusal gate below untestable in the one direction it
        #    exists for.
        h.job_lookups.append(jid)
        if jid in unreadable:
            return JobRead(None, JOB_READ_UNAVAILABLE)
        job = jobs.get(jid)
        if job is None:
            return JobRead(None, JOB_READ_ABSENT)
        if user_id is not None and job.user_id != user_id:
            return JobRead(None, JOB_READ_ABSENT)
        return JobRead(job, JOB_READ_OK)

    def fake_create(**kw):
        h.created.append(kw)
        if isinstance(create_result, Exception):
            raise create_result
        if create_result is not _CREATE_OK:
            return create_result
        return SimpleNamespace(id="lab-1", **{
            k: v for k, v in kw.items() if k != "user_id"
        })

    def fake_stage(**kw):
        h.staged.append(kw)
        return []

    def fake_email(**kw):
        h.emails.append(kw)

    def fake_read_target(tid, *, user_id=None):
        # Models shared.targets.read_target, including the two things about it
        # that matter here.
        #
        # 1. `user_id` is applied as a QUERY FILTER, so another tenant's target
        #    comes back as zero rows -- which is ABSENT, not a distinct
        #    "forbidden" outcome. A `return_value=` patch hands the row back
        #    regardless and so stays green against a route that dropped the
        #    scope.
        # 2. An unreadable parent reports UNAVAILABLE even when `target` is a
        #    perfectly good object, because a read that did not complete learned
        #    nothing about the row. A fake that quietly handed the target back
        #    instead would make the new exit untestable in the one direction it
        #    exists for.
        h.target_lookups.append((tid, user_id))
        if target_unreadable:
            return TargetRead(None, TARGET_READ_UNAVAILABLE)
        if target is None or (user_id is not None and target_owner != user_id):
            return TargetRead(None, TARGET_READ_ABSENT)
        return TargetRead(target, TARGET_READ_OK)

    def fake_campaign_ids(tid, *, user_id=None):
        # Same, for the parentage id set. Owner-scoped inside the real
        # function, so a foreign campaign is simply not in the returned list.
        h.campaign_id_lookups.append((tid, user_id))
        if user_id is not None and campaign_owner != user_id:
            return [], campaign_ids_complete
        return list(campaign_ids), campaign_ids_complete

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
            patch("blueprints.lab_projects.read_job", side_effect=fake_read_job), \
            patch("blueprints.lab_projects.stage_campaign_candidates",
                  side_effect=fake_stage), \
            patch("shared.targets.read_target", side_effect=fake_read_target), \
            patch("shared.targets.campaign_ids_for_target",
                  side_effect=fake_campaign_ids), \
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
    resp, h = _submit(client, {})   # read_job reports ABSENT for every id
    assert resp.status_code == 302
    assert h.created == []
    assert h.staged == []


def test_a_ref_naming_another_users_job_creates_nothing(client):
    """TENANCY, and the sharpest test in this file.

    The job EXISTS, is attached to this very target's campaign, and would sail
    through the parentage test below. The only thing standing between it and
    the lab is that ``read_job`` is called with the caller's user_id, so the
    owner filter makes it come back ABSENT. Drop that keyword and this is a
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


def test_a_missing_target_creates_nothing(client):
    """The parent gate short-circuits BEFORE any job is read, and an ABSENT
    target goes back to the targets list in silence.

    Renamed in round 19. As `test_a_foreign_target_creates_nothing` it claimed
    to prove the read was owner-scoped, but `target=None` only makes the
    patched function return ABSENT: it shows the route handles that answer, not
    that it asks the question. The two tests below are the ones that fail if
    the scope is dropped (register item A-1).

    The silence is asserted rather than assumed (register item A90): the row
    really is gone, so there is no page to land the user on and nothing to say
    beyond returning them to their targets. Without this a later
    "simplification" could route both parent outcomes to `?handoff=unverified`
    -- telling a user whose target was deleted to try again forever -- with
    ``test_an_unreadable_parent_target_refuses_with_a_reason`` still green.
    """
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs, target=None)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/targets")
    assert "handoff" not in resp.headers["Location"]
    assert h.created == []
    assert h.job_lookups == []


def test_an_unreadable_parent_target_refuses_with_a_reason(client):
    """THE OTHER HALF OF THE PARENT GATE (register item A90).

    `get_target` answered None for a target that is not there, one that is not
    the caller's, and a read that never completed, and the gate bounced on all
    three to /targets with no message. A transient Supabase fault now lands the
    user back on THIS target's page with `?handoff=unverified` instead, on the
    one action that hands work to a wet lab.

    Nothing is created and no ref is read: the gate still short-circuits, it
    just says which way it went.
    """
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs, target_unreadable=True)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=unverified")
    assert h.created == []
    assert h.staged == []
    assert h.job_lookups == []


def test_a_target_belonging_to_someone_else_creates_nothing(client):
    """A-1, the half that was missing. The target EXISTS and its campaign
    really is this shortlist's parent; the only thing between the caller and
    another tenant's target is the owner scope on `read_target`.

    The fake models that scope the way `fake_read_job` models `read_job`'s, so
    dropping `user_id=ctx.user_id` at the call site makes this return the row
    and the assertions below fail.
    """
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs, target_owner="u-2")
    assert resp.status_code == 302
    assert h.created == []
    assert h.job_lookups == [], "no job may be read for a target that is not yours"
    # The direct half: the scope has to be ON THE WIRE, not merely honoured by
    # a fake that was never asked.
    assert h.target_lookups == [(_TID, "u-1")], h.target_lookups


def test_the_parentage_read_is_owner_scoped_too(client):
    """The third owner-scoped read, and the one whose absence is silent rather
    than loud: `campaign_ids_for_target` decides which campaigns count as this
    target's parents, so an unscoped read would admit another tenant's campaign
    id as valid provenance.

    Asserted on the kwarg because the behavioural consequence needs a foreign
    campaign that happens to share this target id, which the schema makes
    unreachable -- so the call itself is the only observable.
    """
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs)
    assert resp.status_code == 302
    assert len(h.created) == 1, "fixture assumption: the happy path ran"
    assert h.campaign_id_lookups == [(_TID, "u-1")], h.campaign_id_lookups


def test_only_the_accepted_refs_reach_the_insert(client):
    """A mixed body must be filtered, not rejected wholesale and not accepted
    wholesale. Two of these four refs are legitimate."""
    jobs = {
        "j-bc": _job("j-bc", "bindcraft", campaign_id=_CID),
        "j-far": _job("j-far", "boltzgen", target_id=_OTHER_TID),
        # "j-theirs" is absent: an owner-scoped read matches no row.
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
    """The pair. Adding a branch above the campaign one must not capture it.

    Patches ``read_job`` and not ``get_job``, and ``read_campaign`` and not
    ``get_campaign``: the campaign arm reads its sub-jobs AND its parent run
    through the three-outcome form, for the same reason the target arm does. A
    patch aimed at either ``get_*`` name leaves the ``read_*`` one unpatched, the
    blanked Supabase env makes that read UNAVAILABLE, and the arm turns this into
    a ``?handoff=unverified`` with nothing created. That failure is the fix
    working, not a regression -- do not repair it by restoring the ``get_*``
    patch.
    """
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
            patch("blueprints.lab_projects.read_job",
                  return_value=JobRead(
                      _job("j-bc", "bindcraft", campaign_id=_CID), JOB_READ_OK)), \
            patch("blueprints.lab_projects.stage_campaign_candidates",
                  return_value=[]), \
            patch("shared.compute_campaigns.read_campaign",
                  return_value=CampaignRead(
                      SimpleNamespace(id=_CID), CAMPAIGN_READ_OK)), \
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
    redirects to /jobs -- an unrelated list.

    ROUND 19 (A-8): it now also SAYS so. Landing back on the page you came
    from with nothing changed and no message is indistinguishable from a dead
    button, and this is the action that hands work to the wet lab.
    """
    resp, h = _submit(client, {}, form={"candidate_refs": "[]"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=none")
    assert h.created == []
    assert h.job_lookups == []


# ---------------------------------------------------------------------------
# ROUND 19: what the user is told when the shortlist does not arrive whole
#
# Every one of these was a bare `redirect(detail)`: same page, nothing changed,
# no message. On an action that hands work to a wet lab and bills for it, that
# is indistinguishable from a button that does nothing (register items A-7,
# A-8).
# ---------------------------------------------------------------------------

def _two_refs():
    return json.dumps([{"job_id": "j-bc", "index": 0},
                       {"job_id": "j-gone", "index": 0}])


def test_a_partly_rejected_shortlist_reports_what_was_dropped(client):
    """Two starred, one accepted. The submit proceeds -- correctly, the user
    does want the design that IS valid -- but both the confirmation page and
    the emails otherwise report "1" with nothing to compare it against, and a
    user who starred two reads that as the number they chose."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs, form={"candidate_refs": _two_refs()})
    assert resp.status_code == 302
    assert len(h.created) == 1
    assert h.created[0]["candidate_refs"] == [{"job_id": "j-bc", "index": 0}]
    assert resp.headers["Location"].endswith("?submitted=1&dropped=1")
    # Ops reads the email, not the page, so the count has to reach both.
    assert h.emails and h.emails[0]["dropped"] == 1


def test_a_fully_accepted_shortlist_reports_no_drops(client):
    """The pair. Sending `dropped` unconditionally would satisfy the test above
    while telling every clean submission that something went missing, and would
    change the URL of the overwhelmingly common case."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs)
    assert resp.headers["Location"].endswith("?submitted=1")
    assert h.emails and h.emails[0]["dropped"] == 0


def test_an_incomplete_campaign_read_refuses_rather_than_narrow(client):
    """A-7's sharper half, and THE CENTRAL CASE: a shortlist that is genuinely
    PARTIAL. One ref is accepted by the standalone-job route, the other is
    rejected by the campaign arm under an id set that came back short -- so the
    submission the route would otherwise send is a real shortlist quietly
    missing a design the user selected and paid to compute.

    Round 19's version of this test built no partial at all: both of its refs
    were rejected, so `clean_refs` was empty and the refusal fired for the
    wrong reason. Under that fixture the gate could be narrowed to
    `if not clean_refs and not campaign_ids_complete` with the whole suite
    green, and a ten-ref shortlist with nine dropped would ship.

    Refusing is recoverable: the stars live in sessionStorage and survive the
    redirect, so a retry costs one click.
    """
    jobs = {
        # Accepted: the standalone route does not consult the campaign id set.
        "j-ok": _job("j-ok", "bindcraft", target_id=_TID),
        # Rejected, and rejected BY THE CAMPAIGN ARM -- its campaign is real,
        # it is simply not in a set that came back short.
        "j-cmp": _job("j-cmp", "pxdesign", campaign_id=_CID),
    }
    refs = [{"job_id": "j-ok", "index": 0}, {"job_id": "j-cmp", "index": 0}]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)},
                      campaign_ids=(), campaign_ids_complete=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=unverified")
    assert h.created == [], "a partial shortlist must not reach the lab"
    assert h.staged == []


def test_an_incomplete_read_that_rejected_everything_still_says_unverified(client):
    """Guard ORDER, which the `rejected` reason introduced. Every ref here is
    refused AND the id read was short, so both exits are eligible; the
    unverified one has to win. `rejected` tells the user the selection can
    never work, and under an incomplete read that is precisely what we do not
    know."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs, form={"candidate_refs": _two_refs()},
                      campaign_ids=(), campaign_ids_complete=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=unverified")
    assert h.created == []


def test_an_incomplete_read_does_not_refuse_a_rejection_it_could_not_have_caused(client):
    """The gate is `campaign_arm_rejected`, not `dropped`. This shortlist has a
    real rejection -- `j-gone` resolves to nothing at all -- under a short id
    read, but no campaign id could have rescued a job that does not exist, so
    the read is not implicated and refusing would send the user away from a
    submission that was correct.

    Widening the gate back to `dropped` reds exactly this test.
    """
    jobs = {"j-ok": _job("j-ok", "bindcraft", target_id=_TID)}
    refs = [{"job_id": "j-ok", "index": 0}, {"job_id": "j-gone", "index": 0}]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)},
                      campaign_ids=(), campaign_ids_complete=False)
    assert resp.status_code == 302
    assert len(h.created) == 1, "the read could not have caused this rejection"
    assert h.created[0]["candidate_refs"] == [{"job_id": "j-ok", "index": 0}]
    assert "?submitted=1&dropped=1" in resp.headers["Location"]


def test_an_incomplete_campaign_read_that_rejected_nothing_still_submits(client):
    """The pair, and the reason the refusal is gated on a rejection at all. A
    short id read that changed no decision is not a reason to refuse a
    shortlist every ref of which was accepted by the standalone-job route."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", target_id=_TID)}
    resp, h = _submit(client, jobs, campaign_ids=(),
                      campaign_ids_complete=False)
    assert resp.status_code == 302
    assert len(h.created) == 1
    assert "?submitted=1" in resp.headers["Location"]


def test_a_shortlist_whose_every_ref_was_rejected_says_rejected_not_none(client):
    """The second producer of what round 19 called `handoff=none`.

    `none` is emitted by the early guard, for a POST that carried no designs;
    its banner says the request "arrived with no designs in it" and tells the
    user to press the button again. Reaching the SAME banner from here made
    both of those false: designs did arrive, and pressing the button again
    resubmits the identical refs to the identical checks, forever.
    """
    jobs = {"j-far": _job("j-far", "boltzgen", target_id=_OTHER_TID)}
    refs = [{"job_id": "j-far", "index": 0}, {"job_id": "j-gone", "index": 0}]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=rejected")
    assert h.created == []
    assert h.staged == []


def test_an_unnamed_target_says_so_rather_than_failing_silently(client):
    """The `noname` exit, unpinned until now. The modal's name field is not
    required by the browser, so an empty one is one keystroke away, and the
    remedy ("reopen the form and add one") is the only one of the five nobody
    else can give.

    Ordered AFTER the empty-refs guard on purpose: telling a user with nothing
    starred to name their target answers a question they did not ask.
    """
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs, form={"target_name": "  "})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=noname")
    assert h.created == []
    assert h.job_lookups == [], "no job may be read for a request we will refuse"


def test_a_failed_lab_project_creation_says_so(client):
    """A-8. Until migration 0040 is applied the insert violates 0037's
    `submission_source` CHECK, the exception is swallowed below the call, and
    this returns None. The user was redirected back with no banner at all."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs, create_result=None)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=failed")
    assert h.staged == [], "nothing may be staged for a campaign that failed"


def test_a_rejected_assay_type_says_so_rather_than_failing_silently(client):
    """The OTHER arm of the same exit. `create_campaign_from_target_refs`
    raises ValueError on an assay_type or budget_band outside its enum, and
    both come straight off the POST body, so this arm is live -- but the only
    test covering the exit drove the `is None` return, and deleting the
    `except ValueError:` handler left the suite green while a raised
    ValueError escaped to a 500 on a paid intake.
    """
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(
        client, jobs,
        form={"assay_type": "not_a_real_assay"},
        create_result=ValueError("invalid assay_type: 'not_a_real_assay'"),
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=failed")
    assert len(h.created) == 1, "fixture assumption: the insert was attempted"
    assert h.staged == [], "nothing may be staged for a campaign that raised"


# ---------------------------------------------------------------------------
# ROUND 20: the write path decides what the order CONTAINS
#
# Everything downstream -- `dropped`, both emails, the confirmation banner and
# the ops fulfilment page -- counts the refs this function persisted. Deduping
# and range-checking at READ time (blueprints/admin.py) makes those pages
# disagree with the staff email about the same order; doing it here means the
# disagreement cannot arise.
# ---------------------------------------------------------------------------

def test_the_same_design_named_twice_is_ordered_once(client):
    """A repeated (job_id, index) is ONE physical design. Persisting it twice
    tells ops to order the same structure twice, and counting the repeat as a
    rejection tells the user a design went missing when none did."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    refs = [{"job_id": "j-bc", "index": 0}, {"job_id": "j-bc", "index": 0},
            {"job_id": "j-bc", "index": 1}]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)})
    assert h.created[0]["candidate_refs"] == [
        {"job_id": "j-bc", "index": 0}, {"job_id": "j-bc", "index": 1},
    ]
    assert [c["indices"] for c in h.staged] == [[0, 1]]
    # The repeat is not a shortfall, so nothing may tell the user one occurred.
    assert h.emails[0]["dropped"] == 0
    assert resp.headers["Location"].endswith("?submitted=1")


def test_a_ref_naming_an_index_past_the_end_of_its_job_is_refused(client):
    """`stage_campaign_candidates` silently skips an out-of-range index, so an
    unvalidated ref is persisted, counted on the staff email and counted on the
    customer's page -- and then no PDB reaches the bucket for it. The lab
    receives fewer structures than every number anyone can see."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID, n=3)}
    refs = [{"job_id": "j-bc", "index": 0}, {"job_id": "j-bc", "index": 7}]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)})
    assert h.created[0]["candidate_refs"] == [{"job_id": "j-bc", "index": 0}]
    assert [c["indices"] for c in h.staged] == [[0]]
    # Counted as a shortfall, because it IS one: the user starred something we
    # cannot deliver.
    assert h.emails[0]["dropped"] == 1
    assert resp.headers["Location"].endswith("?submitted=1&dropped=1")


def test_a_job_whose_record_count_is_unknown_keeps_all_its_refs(client):
    """The pair, and the reason the range check is gated on a POSITIVE count.
    `candidate_records` returns [] both for a job with no results and for a
    result shape it cannot read, so zero means "length unknown". Refusing every
    design of such a job would be a louder wrong answer than saying nothing
    about it -- the same rule blueprints/admin.py applies on the read side."""
    job = _job("j-odd", "boltz2", target_id=_TID)
    job.result = {"something_else": [1, 2, 3]}   # candidate_records -> []
    refs = [{"job_id": "j-odd", "index": 0}, {"job_id": "j-odd", "index": 9}]
    resp, h = _submit(client, {"j-odd": job},
                      form={"candidate_refs": json.dumps(refs)})
    assert h.created[0]["candidate_refs"] == refs
    assert h.emails[0]["dropped"] == 0
    assert resp.headers["Location"].endswith("?submitted=1")


# ---------------------------------------------------------------------------
# ROUND 20: what the per-request cap removed
# ---------------------------------------------------------------------------

def test_starred_designs_past_the_cap_are_reported_not_silently_dropped(client):
    """`_MAX_CANDIDATE_REFS` truncates at parse time, so a count derived from
    `len(candidate_refs)` saturates and reports ZERO drops for a shortlist that
    lost designs to the bound. Stars persist in sessionStorage and the pooled
    table renders 300 rows a view, so accumulating past 500 is an ordinary
    path -- and the free CSV export announces its truncation while the PAID
    handoff did not.

    Reported SEPARATELY from `dropped`: these designs were never read, so
    "could not be matched to this target" would be a verdict nobody reached.
    NOT because it has a different remedy -- nothing in this product clears a
    shortlist, so a second request re-posts the identical first 500 refs, which
    is why neither count carries retry advice.
    """
    from blueprints.lab_projects import _MAX_CANDIDATE_REFS
    cap = _MAX_CANDIDATE_REFS
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID, n=cap + 200)}
    refs = [{"job_id": "j-bc", "index": i} for i in range(cap + 120)]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)})
    assert resp.status_code == 302
    assert len(h.created[0]["candidate_refs"]) == cap
    assert resp.headers["Location"].endswith(f"?submitted=1&truncated=120")
    assert h.emails[0]["truncated"] == 120
    # Not a rejection: nothing about these refs was ever judged.
    assert h.emails[0]["dropped"] == 0


def test_a_shortlist_inside_the_cap_reports_no_truncation(client):
    """The pair. Sending `truncated` unconditionally would satisfy the test
    above while telling every ordinary submission that designs were cut, and
    would change the URL of the overwhelmingly common case."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", campaign_id=_CID)}
    resp, h = _submit(client, jobs)
    assert resp.headers["Location"].endswith("?submitted=1")
    assert h.emails[0]["truncated"] == 0


# ---------------------------------------------------------------------------
# ROUND 20: the confirmation page's shortfall banners
#
# Both counts reach the template through `render_template` kwargs, and
# `{% if dropped_count %}` treats a Jinja Undefined as falsy -- so deleting
# either kwarg removes the banner with no error anywhere. These render the page
# and read the result.
# ---------------------------------------------------------------------------

def _lab_project_page(client, query="", refs=None):
    from shared.campaigns import Campaign
    campaign = Campaign.from_row({
        "id": "lab-9", "user_id": "u-1", "target_name": "HER2",
        "assay_type": "yeast_display", "budget_band": "pilot",
        "status": "submitted", "submission_source": "target",
        "source_target_id": _TID, "candidate_indices": [],
        "candidate_refs": ([{"job_id": "j-bc", "index": 0}]
                           if refs is None else refs),
    })
    _login(client)
    with patch("blueprints.lab_projects.load_user_context", return_value=_ctx()), \
            patch("shared.campaigns.get_campaign", return_value=campaign):
        resp = client.get("/lab-projects/lab-9" + query)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_the_confirmation_page_states_how_many_designs_were_refused(client):
    """`?dropped=` is the only place the user learns that their selection was
    not delivered whole, and it has to survive a reload of this URL."""
    html = _lab_project_page(client, "?submitted=1&dropped=3")
    assert "3 starred designs were not included" in html
    # Parent-neutral: one string serves a target-sourced and a campaign-sourced
    # row, so it names the RESULTS the shortlist came from rather than a target.
    assert "could not be matched to a design in the results this shortlist " \
        "was built from" in html


def test_the_confirmation_page_states_how_many_designs_were_over_the_limit(client):
    """The second banner. "Up to", because the number counts REFS: the tail past
    the cap is never parsed into (job, index) pairs, so a repeat hiding in it
    cannot be subtracted and this is an upper bound on the designs missing."""
    html = _lab_project_page(client, "?submitted=1&truncated=120")
    assert "Up to 120 further starred designs were over the per-request limit" \
        in html


def test_the_over_the_limit_page_states_the_fact_and_advises_beside_the_list(client):
    """MEDIUM-4 and register item A89, as behaviour rather than as wording.

    The banner used to say "Star them again on the target page and submit a
    second request". Following it produced a SECOND lab project covering the
    SAME designs: nothing cleared the shortlist, the modal serialises it in
    stored order, so the second POST carried the identical first
    `_MAX_CANDIDATE_REFS` refs. `campaign_detail` now hands the browser the refs
    THIS request covered, on `?submitted=1`, and the browser removes exactly
    those and keeps every other star -- so what is left to send is the
    remainder, which is what makes the advice followable.

    NOT A WIPE OF THE SHORTLIST, and this docstring said it was for one round.
    Removing the whole key destroyed the never-read remainder the advice is
    ABOUT; removing named refs is also idempotent, which is what retired the
    once-per-row marker the earlier text described (register item A89).

    It happens in a browser the server never hears back from, so the advice
    ships with a safety net rather than on trust: the designs this request
    already covers are printed underneath it. That is why the SENTENCE lives
    inside the list's own block while the FACT does not -- the disclosure that
    120 designs never reached the lab is owed to a customer whether or not the
    page can list what did arrive.

    WHAT THE NAME USED TO CLAIM AND THE BODY COULD NOT SHOW. This rendered one
    page whose fixture always carries a valid ref, so the list always rendered
    and every assertion was a presence; hoisting the sentence out of the panel
    left all four true. Both gaps are closed here: the ORDER of the three is
    asserted on the response, which a hoist breaks, and a SECOND row whose
    entries resolve to no designs is rendered, which a loosened list gate
    breaks. The same absences are pinned from the other direction in
    tests/test_lab_project_confirmation.py --
    `test_the_advice_never_renders_without_the_list_it_points_at` and
    `test_the_truncation_fact_survives_a_row_with_no_readable_designs`.

    "Star them again" stays gone: it names the designs already covered.
    """
    html = _lab_project_page(client, "?submitted=1&truncated=120")
    flat = re.sub(r"\s+", " ", html)
    assert "Star them again" not in flat
    assert "Up to 120 further starred designs were over the per-request limit" \
        in flat
    assert "To include anything that was over the limit, star it on the " \
        "source page and send a second request." in flat
    # The list it points at, on the same page: the fixture row names one design.
    assert "Designs in this request" in flat
    assert "Candidate 1" in flat
    # The fact is above the panel, the advice inside it, the designs below the
    # advice. The advice says "below", and only its words were ever asserted.
    assert flat.index("over the per-request limit") \
        < flat.index("Designs in this request") \
        < flat.index("To include anything") \
        < flat.index("Candidate 1")
    # THE ABSENCE, on the same route with the same query. A row whose stored
    # entries resolve to no designs has no list, so the advice must not ship --
    # and the FACT must, because this page is the only place the customer is
    # told what did not reach the lab.
    bare = re.sub(r"\s+", " ", _lab_project_page(
        client, "?submitted=1&truncated=120", refs=["not-a-ref"]))
    assert "To include anything" not in bare
    assert "Up to 120 further starred designs were over the per-request limit" \
        in bare


def test_a_clean_confirmation_page_carries_neither_banner(client):
    """The pair for both. Rendering either unconditionally would satisfy the
    two tests above while telling every clean submission that designs went
    missing."""
    html = _lab_project_page(client, "?submitted=1")
    assert "not included" not in html
    assert "over the per-request limit" not in html
    assert "Scoping request submitted." in html
# ---------------------------------------------------------------------------
# ROUND 21: WHY a ref was refused, and what that entitles anyone to say
#
# Three causes reach this route as one another's twin unless the boundary keeps
# them apart: the job is not there, the job is not yours, and we could not read
# it. Rounds 19 and 20 each picked one guess for the whole bucket and each was
# right about half of it. These tests construct the third cause -- which no
# earlier fixture could express, because `get_job` had no way to report it --
# and pin the two decisions it drives: whether the submission proceeds, and
# whether the copy downstream is allowed to be categorical.
# ---------------------------------------------------------------------------

def test_a_job_read_that_never_completed_refuses_rather_than_narrowing(client):
    """THE CASE ROUND 20 GOT WRONG, in its simplest form.

    `j-slow` is a perfectly valid design of this very target; the read of it
    just did not complete. Round 20 saw only `None`, took it for a verdict, and
    shipped a one-design order for a two-design shortlist while telling the user
    their second design could never be matched.

    The campaign id set is COMPLETE here and `j-slow` carries no campaign_id at
    all, so round 20's `campaign_arm_rejected` flag was not even eligible to
    fire. The refusal has to come from the job read itself or not at all.
    """
    jobs = {
        "j-ok": _job("j-ok", "bindcraft", target_id=_TID),
        "j-slow": _job("j-slow", "pxdesign", target_id=_TID),
    }
    refs = [{"job_id": "j-ok", "index": 0}, {"job_id": "j-slow", "index": 0}]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)},
                      unreadable={"j-slow"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=unverified")
    assert h.created == [], "a shortlist we could not verify must not reach the lab"
    assert h.staged == []


def test_a_correlated_supabase_fault_cannot_ship_a_half_size_order(client):
    """THE REGRESSION ROUND 20 INTRODUCED, reproduced exactly.

    One degraded Supabase produces both faults in the same request:
    `campaign_ids_for_target` returns a short list with complete=False, AND the
    per-ref read times out. Round 20 armed its refusal only when the rejection
    was decided by the campaign arm -- which requires the job to have been READ
    -- so a timed-out read set nothing, the guard was skipped, and a half-size
    paid wet-lab order shipped. Round 19's broader gate caught this; narrowing
    it to fix a misattribution re-opened A-7.
    """
    jobs = {
        "j-ok": _job("j-ok", "bindcraft", target_id=_TID),
        "j-cmp": _job("j-cmp", "pxdesign", campaign_id=_CID),
    }
    refs = [{"job_id": "j-ok", "index": 0}, {"job_id": "j-cmp", "index": 0}]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)},
                      unreadable={"j-cmp"}, campaign_ids=(),
                      campaign_ids_complete=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=unverified")
    assert h.created == [], "the correlated-failure order must not ship"


def test_a_read_we_could_not_complete_never_reaches_the_rejection_wording(client):
    """WHAT THE GATE ABOVE BUYS THE COPY, which is the point of the change.

    The confirmation page, the target page and both emails tell the user that a
    dropped design "will be refused the same way" and to contact us if that
    looks wrong. That sentence is false for a transient fault -- a retry would
    have delivered everything -- so it is only sayable if no transient cause can
    produce a `dropped` count or a `rejected` banner. This asserts that property
    directly rather than trusting the wording to hedge.
    """
    jobs = {
        "j-ok": _job("j-ok", "bindcraft", target_id=_TID),
        "j-slow": _job("j-slow", "pxdesign", target_id=_TID),
    }
    refs = [{"job_id": "j-ok", "index": 0}, {"job_id": "j-slow", "index": 0}]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)},
                      unreadable={"j-slow"})
    location = resp.headers["Location"]
    assert "dropped=" not in location, location
    assert "handoff=rejected" not in location, location
    assert h.emails == [], "no email may report a shortfall we could not decide"


def test_an_unreadable_job_is_read_once_however_many_refs_name_it(client):
    """The negative cache covers the transient outcome too. Under a fault the
    retry pressure is at its worst, so re-reading a timing-out id once per
    starred design is precisely when it costs most."""
    jobs = {"j-slow": _job("j-slow", "bindcraft", target_id=_TID, n=40)}
    refs = [{"job_id": "j-slow", "index": i} for i in range(40)]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)},
                      unreadable={"j-slow"})
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=unverified")
    assert h.job_lookups == ["j-slow"], h.job_lookups


def test_every_ref_naming_one_rejected_job_counts_as_its_own_drop(client):
    """The negative cache must not swallow the COUNT along with the read.

    Ten starred designs from one job that turns out to belong to another target
    are ten designs the user does not get. Dropping the `dropped += 1` on the
    cache-hit path reports 1, and no other test in this file posts two refs
    naming the same rejected job, so that mutation survived the whole suite.
    """
    jobs = {
        "j-far": _job("j-far", "boltzgen", target_id=_OTHER_TID, n=10),
        "j-ok": _job("j-ok", "bindcraft", target_id=_TID),
    }
    refs = [{"job_id": "j-far", "index": i} for i in range(10)]
    refs.append({"job_id": "j-ok", "index": 0})
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)})
    assert resp.status_code == 302
    assert h.emails[0]["dropped"] == 10, h.emails[0]["dropped"]
    assert resp.headers["Location"].endswith("?submitted=1&dropped=10")
    # The read itself still happens once -- the count and the round trip are
    # separate properties and this pins both.
    assert h.job_lookups == ["j-far", "j-ok"], h.job_lookups


def test_a_ref_into_a_job_that_delivered_zero_designs_is_refused(client):
    """A `{"candidates": []}` job has a KNOWN length of zero, not an unknown one.

    `candidate_records` answers `[]` for both, so the old `if n and idx >= n`
    spelling disabled the range check for both -- and for a genuinely empty job
    that meant every ref was accepted, recorded on the row, counted to ops and
    to the customer, and then staged zero PDBs. `candidate_count` reports 0 here
    and None for a shape it cannot read, so only the second waves refs through;
    `test_a_job_whose_record_count_is_unknown_keeps_all_its_refs` is that pair.
    """
    job = _job("j-empty", "bindcraft", target_id=_TID, n=0)
    assert job.result == {"candidates": []}, "fixture: a KNOWN zero, not a gap"
    refs = [{"job_id": "j-empty", "index": 0}]
    resp, h = _submit(client, {"j-empty": job},
                      form={"candidate_refs": json.dumps(refs)})
    assert resp.status_code == 302
    assert h.created == [], "there is no design here to order"
    assert h.staged == []
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=rejected")


# ---------------------------------------------------------------------------
# ROUND 21: the cap's overflow survives the exits that are not the happy one
# ---------------------------------------------------------------------------

def _over_cap_refs(jid, over=120):
    from blueprints.lab_projects import _MAX_CANDIDATE_REFS
    return json.dumps([
        {"job_id": jid, "index": i}
        for i in range(_MAX_CANDIDATE_REFS + over)
    ])


def test_a_wholly_rejected_shortlist_still_reports_what_the_cap_discarded(client):
    """`truncated` was computed after the loop and then thrown away on three of
    the four exits, so on exactly the paths where the user is already being told
    something went wrong, 120 designs they starred went unmentioned."""
    jobs = {"j-far": _job("j-far", "boltzgen", target_id=_OTHER_TID, n=700)}
    resp, h = _submit(client, jobs,
                      form={"candidate_refs": _over_cap_refs("j-far")})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(
        f"/targets/{_TID}?handoff=rejected&truncated=120"
    )
    assert h.created == []


def test_an_unverifiable_shortlist_still_reports_what_the_cap_discarded(client):
    """The same on the refusal exit, where it matters most: the user is about to
    retry, and a retry cuts the selection in the same place."""
    jobs = {"j-slow": _job("j-slow", "bindcraft", target_id=_TID, n=700)}
    resp, h = _submit(client, jobs,
                      form={"candidate_refs": _over_cap_refs("j-slow")},
                      unreadable={"j-slow"})
    assert resp.headers["Location"].endswith(
        f"/targets/{_TID}?handoff=unverified&truncated=120"
    )


def test_an_unreadable_parent_target_also_reports_what_the_cap_discarded(client):
    """And on the newest exit (A90). `truncated` is computed above every guard
    precisely so it rides them all, and a gate added later is exactly where that
    stops being true without anyone noticing."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", target_id=_TID, n=700)}
    resp, h = _submit(client, jobs,
                      form={"candidate_refs": _over_cap_refs("j-bc")},
                      target_unreadable=True)
    assert resp.headers["Location"].endswith(
        f"/targets/{_TID}?handoff=unverified&truncated=120"
    )
    assert h.created == []


def test_a_failed_creation_still_reports_what_the_cap_discarded(client):
    """And on the A-8 exit."""
    jobs = {"j-bc": _job("j-bc", "bindcraft", target_id=_TID, n=700)}
    resp, h = _submit(client, jobs,
                      form={"candidate_refs": _over_cap_refs("j-bc")},
                      create_result=None)
    assert resp.headers["Location"].endswith(
        f"/targets/{_TID}?handoff=failed&truncated=120"
    )


def test_a_refused_shortlist_inside_the_cap_carries_no_truncation(client):
    """The pair for all three. Appending the parameter unconditionally would
    satisfy them while telling every ordinary refusal that designs were cut."""
    jobs = {"j-far": _job("j-far", "boltzgen", target_id=_OTHER_TID)}
    refs = [{"job_id": "j-far", "index": 0}]
    resp, h = _submit(client, jobs, form={"candidate_refs": json.dumps(refs)})
    assert resp.headers["Location"].endswith(f"/targets/{_TID}?handoff=rejected")


# ---------------------------------------------------------------------------
# ROUND 21: the route is behind the idempotency gate
#
# Every other mutating POST in this app is (`tools.tool_submit`,
# `targets.target_create`, `targets.target_launch_submit`, the campaign
# fund/launch pair); this intake was the last one that was not, so a
# double-clicked submit opened two lab projects for one shortlist.
# ---------------------------------------------------------------------------

_REPLAY_ROW = {
    "response_status": 302,
    "response_body": "",
    "content_type": "text/html; charset=utf-8",
    "location": "/lab-projects/lab-earlier?submitted=1",
}


def _submit_with_claim(client, state, row):
    """POST a target shortlist with `_claim_key` forced to one outcome.

    `shared.idempotency.load_user_context` is patched as well as the
    blueprint's: the decorator resolves that name in ITS OWN module, and under
    the blanked Supabase env the real one returns None, which makes the
    decorator a pass-through and would leave this test unable to fail.
    """
    created: list = []

    def fake_create(**kw):
        created.append(kw)
        return SimpleNamespace(id="lab-new")

    body = {
        "source_target_id": _TID,
        "candidate_refs": json.dumps([{"job_id": "j-bc", "index": 0}]),
        "target_name": "HER2",
        "assay_type": "yeast_display",
        "budget_band": "pilot",
    }
    job = _job("j-bc", "bindcraft", target_id=_TID)
    _login(client)
    with patch("shared.idempotency.load_user_context", return_value=_ctx()), \
            patch("shared.idempotency._claim_key", return_value=(state, row)), \
            patch("blueprints.lab_projects.load_user_context",
                  return_value=_ctx()), \
            patch("blueprints.lab_projects.read_job",
                  return_value=JobRead(job, JOB_READ_OK)), \
            patch("blueprints.lab_projects.stage_campaign_candidates",
                  return_value=[]), \
            patch("shared.targets.read_target",
                  return_value=TargetRead(object(), TARGET_READ_OK)), \
            patch("shared.targets.campaign_ids_for_target",
                  return_value=([_CID], True)), \
            patch("shared.campaigns.create_campaign_from_target_refs",
                  side_effect=fake_create), \
            patch("shared.email.send_campaign_submitted_emails"):
        resp = client.post("/lab-projects/submit", data=body)
    return resp, created


def test_a_replayed_submit_does_not_open_a_second_lab_project(client):
    """The duplicate this route could previously produce. A cached claim for the
    identical (user, path, body) replays the first response and the handler must
    not run again -- otherwise a double-click is two paid orders for one
    shortlist, which ops reconciles by hand."""
    resp, created = _submit_with_claim(client, "replay", _REPLAY_ROW)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/lab-projects/lab-earlier?submitted=1"
    assert resp.headers.get("Idempotent-Replay") == "true"
    assert created == [], "the handler ran for a replayed key"


def test_a_first_time_submit_is_not_blocked_by_the_gate(client):
    """The pair. A test that only ever asserts the replay is satisfied by a
    route that refuses every submission, so this drives the same fixture with
    the claim GRANTED and asserts the lab project is created."""
    resp, created = _submit_with_claim(client, "open", None)
    assert resp.status_code == 302
    assert len(created) == 1, "the handler must run when the key is free"
    assert created[0]["source_target_id"] == _TID
    assert resp.headers.get("Idempotent-Replay") is None
# ---------------------------------------------------------------------------
# ROUND 21: the boundary itself
#
# Everything above fakes `read_job`, so everything above would stay green if the
# real one collapsed its three outcomes back into two. These drive the real
# function against a fake PostgREST client.
# ---------------------------------------------------------------------------

class _FakeQuery:
    """The subset of the PostgREST builder chain `read_job` uses.

    IT DELIBERATELY HAS NO `single()`. That is not an omission: `.single()`
    RAISES on zero rows, which is exactly why `read_job` must not use it -- under
    `.single()` a missing job and a read that never completed arrive as the same
    exception and the distinction the function exists for is destroyed before its
    own `except` runs. A `read_job` rewritten to call `.single()` would hit
    AttributeError here, be swallowed by that `except`, and report UNAVAILABLE
    for a row that is plainly present, reddening the OK cases below.

    `eq()` is modelled as a real filter rather than recorded and ignored, so a
    `read_job` that stopped passing `user_id` returns another tenant's row and
    the tenancy case below fails.
    """

    def __init__(self, rows, *, raises=False, calls=None):
        self._rows = rows
        self._raises = raises
        self._filters: dict = {}
        self._limit = None
        self.calls = calls if calls is not None else []

    def select(self, *_a, **_kw):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        self.calls.append((col, val))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        if self._raises:
            raise RuntimeError("read timeout")
        matched = [
            r for r in self._rows
            if all(r.get(k) == v for k, v in self._filters.items())
        ]
        if self._limit is not None:
            matched = matched[:self._limit]
        return SimpleNamespace(data=matched)


class _FakeClient:
    def __init__(self, rows, *, raises=False):
        self.calls: list = []
        self._rows = rows
        self._raises = raises

    def table(self, _name):
        return _FakeQuery(self._rows, raises=self._raises, calls=self.calls)


def _row(jid="j-1", owner="u-1"):
    """A tool_jobs row with every column `ToolJob.from_row` requires."""
    return {
        "id": jid, "user_id": owner, "tool": "bindcraft", "preset": "pilot",
        "status": "succeeded", "inputs": {}, "result": {"candidates": [{}]},
        "error": None, "modal_function_call_id": None, "job_token": "tok",
        "gpu_seconds_used": 10, "created_at": None, "started_at": None,
        "completed_at": None,
    }


def _read(rows, *, raises=False, client=True, **kw):
    from shared import jobs as jobs_mod
    fake = _FakeClient(rows, raises=raises) if client else None
    with patch("shared.jobs.get_service_client", return_value=fake):
        return jobs_mod.read_job("j-1", **kw), fake


def test_read_job_reports_ok_for_a_row_that_is_there():
    read, _ = _read([_row()], user_id="u-1")
    assert read.outcome == JOB_READ_OK
    assert read.job is not None and read.job.id == "j-1"
    assert read.unavailable is False


def test_read_job_reports_absent_for_a_completed_read_that_matched_nothing():
    """A read that RAN and returned no rows is a verdict, and the whole reason
    the query is `.limit(1)` rather than `.single()` is that this case has to be
    observable as an empty list instead of an exception."""
    read, _ = _read([], user_id="u-1")
    assert read.outcome == JOB_READ_ABSENT
    assert read.job is None
    assert read.unavailable is False


def test_read_job_reports_absent_for_another_tenants_row():
    """The owner scope is a QUERY FILTER, so a foreign row is simply not
    returned. Same outcome as missing, and deliberately so: telling the two
    apart would mean reading the row the scope exists to withhold."""
    read, fake = _read([_row(owner="u-2")], user_id="u-1")
    assert read.outcome == JOB_READ_ABSENT
    assert ("user_id", "u-1") in fake.calls, fake.calls


def test_read_job_reports_unavailable_when_the_query_raises():
    """THE OUTCOME `get_job` CANNOT EXPRESS. The row may be perfectly valid; we
    never found out. Reporting ABSENT here is what let a two-second database
    hiccup tell a paying customer their designs were permanently unmatched."""
    read, _ = _read([_row()], raises=True, user_id="u-1")
    assert read.outcome == JOB_READ_UNAVAILABLE
    assert read.job is None
    assert read.unavailable is True


def test_read_job_reports_unavailable_when_there_is_no_service_client():
    """The second transient source, and the one with no exception to catch: an
    unconfigured or failed client is still "we could not look"."""
    read, _ = _read([_row()], client=False, user_id="u-1")
    assert read.outcome == JOB_READ_UNAVAILABLE
    assert read.unavailable is True


def test_read_job_without_a_user_id_applies_no_owner_filter():
    """The unscoped form still exists, because the shortlist route is not the
    only conceivable caller. Pinned so the scope cannot become accidentally
    mandatory and silently change a caller that never passed one."""
    read, fake = _read([_row(owner="u-2")])
    assert read.outcome == JOB_READ_OK
    assert [c for c in fake.calls if c[0] == "user_id"] == [], fake.calls


def test_get_job_is_unchanged_and_still_collapses_the_two():
    """`read_job` is ADDITIVE. `get_job` is called from most blueprints and from
    the terminal/settle path, so it keeps its signature, its `.single()` chain
    and its two-valued answer; this asserts the pair still exists rather than
    one having been rewritten into the other.
    """
    from shared import jobs as jobs_mod
    import inspect
    assert jobs_mod.get_job is not jobs_mod.read_job
    sig = inspect.signature(jobs_mod.get_job)
    assert list(sig.parameters) == ["job_id", "user_id"]
    assert "single" in inspect.getsource(jobs_mod.get_job), (
        "get_job's .single() chain is what several test fakes model"
    )


# --- candidate_count: the same conflation, in the other sentinel -------------

def test_candidate_count_separates_a_known_zero_from_an_unknown_length():
    """`candidate_records` answers `[]` for both, which is what made the range
    check in the shortlist route unable to refuse a ref into a job that
    delivered nothing."""
    from shared.jobs import candidate_count, candidate_records
    empty = {"candidates": []}
    unreadable = {"something_else": [1, 2, 3]}
    assert candidate_records(empty) == candidate_records(unreadable) == []
    assert candidate_count(empty) == 0
    assert candidate_count(unreadable) is None


def test_candidate_count_reads_the_same_array_candidate_records_does():
    """Same keys, same order, same normalisation. If the two ever disagreed
    about WHICH array they describe, an index validated against one would be
    staged out of the other."""
    from shared.jobs import candidate_count, candidate_records
    for result in (
        {"candidates": [{"a": 1}, {"a": 2}]},
        {"designs": [{"a": 1}]},
        {"candidates": [{"a": 1}], "designs": [{"b": 2}, {"b": 3}]},
        {"output": {"candidates": [{"a": 1}, {"a": 2}, {"a": 3}]}},
    ):
        assert candidate_count(result) == len(candidate_records(result)), result
    assert candidate_count(None) is None
    assert candidate_count("not a dict") is None
