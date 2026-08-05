"""POST /lab-projects/submit with a COMPUTE-CAMPAIGN shortlist (item A88).

The sibling of ``tests/test_target_lab_handoff.py``. The campaign arm shipped
with a refusal model the target arm no longer has: every refusal was a silent
``continue``, nothing was counted, and the four failure exits all returned a
bare redirect to the run page with no reason on it.

WHAT THIS FILE IS MOSTLY ABOUT IS WHERE THE TWO ARMS MUST **DIFFER**. The
target arm's loop is 90% of this one and the remaining 10% is a security
boundary in the wrong direction: it accepts a job by EITHER of two routes
(``job.target_id`` or membership of the target's campaign id set), and this
page has exactly one route in -- the design is a child of this compute
campaign. Copying that disjunction here would admit a foreign design, so the
acceptance tests below are each paired with a rejection test that a copied
target-arm clause would fail.

Everything is patched at ITS OWN module, matching how
``blueprints/lab_projects.py`` imports it: ``read_job`` and
``stage_campaign_candidates`` are module-level imports on the blueprint, while
``compute_campaigns.get_campaign``, ``create_campaign_from_refs`` and
``send_campaign_submitted_emails`` are function-local and therefore resolve
against their own modules at call time.

THE ARM READS JOBS THROUGH ``read_job``, NOT ``get_job``, and that swap is a
precondition for reporting a drop count at all rather than a separate
improvement. ``get_job`` answers ``None`` for a job that is absent, one that is
another tenant's, and a read that never completed; a count built on it tells a
paying customer their designs are permanently unmatchable because Supabase
blinked. ``_submit`` below therefore fakes ``read_job`` and can be told to make
a specific id UNREADABLE, which is the only way to build that fault.
"""

from __future__ import annotations

import json
import re
import uuid
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared.jobs import JOB_READ_ABSENT, JOB_READ_OK, JOB_READ_UNAVAILABLE, JobRead

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_CID = str(uuid.uuid4())
_OTHER_CID = str(uuid.uuid4())
_TID = str(uuid.uuid4())


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


# "no override given", distinct from an explicit ``result=None`` -- which is a
# real shape the arm has to handle (a NULL result column) rather than a request
# for the default envelope.
_DEFAULT_RESULT = object()


def _job(jid, tool="bindcraft", *, campaign_id=None, target_id=None, n=3,
         owner="u-1", result=_DEFAULT_RESULT):
    """A succeeded job whose result carries ``n`` candidates.

    ``owner`` exists so the fake ``read_job`` can enforce the owner scope the
    real one enforces: a fake that ignored ``user_id`` would let the tenancy
    test pass against a route that had stopped passing it.

    ``result`` overrides the whole envelope, which is how the two index-check
    tests build a shape whose length is UNKNOWN as against one whose length is
    a known zero. Those are different facts and the arm treats them
    differently.
    """
    return SimpleNamespace(
        id=jid, tool=tool, status="succeeded", user_id=owner,
        target_id=target_id, campaign_id=campaign_id,
        result=({"candidates": [
            {"pdb_key": f"design_{i + 1}.pdb", "scores": {"ipTM": 0.9}}
            for i in range(n)
        ]} if result is _DEFAULT_RESULT else result),
    )


class _Harness:
    """Captured side effects of one submit."""

    def __init__(self):
        self.created: list[dict] = []
        self.staged: list[dict] = []
        self.emails: list[dict] = []
        self.job_lookups: list[str] = []
        # (id, user_id) per call. The user_id half is the point: it is the only
        # thing that fails when the route stops passing the owner scope.
        self.campaign_lookups: list[tuple] = []


_CREATE_OK = object()


def _submit(client, jobs, *, form=None, campaign=object(),
            create_result=_CREATE_OK, campaign_owner="u-1", unreadable=()):
    """Drive POST /lab-projects/submit and return (response, harness).

    ``jobs`` maps job id -> job. An id absent from the mapping is a job that is
    not there or is not the caller's, which an owner-scoped read reports as
    ``JOB_READ_ABSENT``.

    ``unreadable`` is the set of ids whose LOOKUP FAILS -- no service client, or
    the query raised -- reported as ``JOB_READ_UNAVAILABLE``. An id may appear
    in ``jobs`` AND in ``unreadable`` at once, which is the realistic shape of a
    transient fault: the row is perfectly valid and we did not manage to read
    it. A fake that handed the row back anyway would make the refusal gate
    untestable in the one direction it exists for.

    ``create_result`` overrides what ``create_campaign_from_refs`` returns, or
    -- when it is an ``Exception`` instance -- what it RAISES. The real function
    raises ``ValueError`` on an unknown assay_type or budget_band, so the
    route's ``except ValueError`` arm is live and needs a fake that reaches it.
    """
    h = _Harness()

    def fake_read_job(jid, *, user_id=None):
        # Models shared.jobs.read_job, including the two things that matter
        # here: `user_id` is a QUERY FILTER, so another tenant's job comes back
        # as zero rows -- ABSENT, not a distinct "forbidden" outcome, because
        # telling those apart would mean reading a row the owner scope exists
        # to withhold. And an unreadable id reports UNAVAILABLE even when the
        # job is present, because a read that did not complete learned nothing.
        h.job_lookups.append(jid)
        if jid in unreadable:
            return JobRead(None, JOB_READ_UNAVAILABLE)
        job = jobs.get(jid)
        if job is None:
            return JobRead(None, JOB_READ_ABSENT)
        if user_id is not None and job.user_id != user_id:
            return JobRead(None, JOB_READ_ABSENT)
        return JobRead(job, JOB_READ_OK)

    def fake_get_campaign(cid, *, user_id=None):
        # Models shared.compute_campaigns.get_campaign, which applies user_id
        # as a query filter: another tenant's run comes back None,
        # indistinguishable from absent.
        h.campaign_lookups.append((cid, user_id))
        if campaign is None or (user_id is not None and campaign_owner != user_id):
            return None
        return campaign

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

    body = {
        "source_campaign_id": _CID,
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
            patch("shared.compute_campaigns.get_campaign",
                  side_effect=fake_get_campaign), \
            patch("shared.campaigns.create_campaign_from_refs",
                  side_effect=fake_create), \
            patch("shared.email.send_campaign_submitted_emails",
                  side_effect=fake_email):
        resp = client.post("/lab-projects/submit", data=body)
    return resp, h


def _refs(pairs):
    return json.dumps([{"job_id": j, "index": i} for j, i in pairs])


# ---------------------------------------------------------------------------
# The parentage test. ONE equality, and every widening admits a foreign design.
# ---------------------------------------------------------------------------

def test_a_sub_job_of_this_run_is_accepted(client):
    """The happy path: ``job.campaign_id == source_campaign_id``. This is the
    only route by which a design reaches the run page at all."""
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID)}
    resp, h = _submit(client, jobs)
    assert resp.status_code == 302
    assert len(h.created) == 1
    assert h.created[0]["source_campaign_id"] == _CID
    assert h.created[0]["candidate_refs"] == [{"job_id": "j-bc", "index": 0}]


def test_a_ref_naming_another_users_job_creates_nothing(client):
    """TENANCY, on the wire. The job is a genuine child of THIS run, so the
    parentage clause would accept it; only the owner scope on the read refuses
    it. Dropping ``user_id=ctx.user_id`` from ``read_job`` reds this and
    nothing else."""
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID, owner="u-2")}
    resp, h = _submit(client, jobs)
    assert resp.status_code == 302
    assert h.created == []
    assert h.staged == []
    assert resp.headers["Location"].endswith("?handoff=rejected")


def test_a_shortlist_against_another_users_run_creates_nothing(client):
    """TENANCY ON THE PARENT, which the per-ref owner scope does not cover.

    ``cc.get_campaign`` applies ``user_id`` as a query filter, so another
    tenant's run comes back None and this arm bounces before a single ref is
    read.

    Asserted on ``campaign_lookups`` and not only on the redirect, because the
    redirect cannot see the defect: a route that stopped passing ``user_id=``
    would still get None for a genuinely absent id and still bounce here. The
    owner scope is observable only in what was ASKED, which is why the harness
    records the pair rather than the id.
    """
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID)}
    resp, h = _submit(client, jobs, campaign_owner="u-2")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/jobs")
    assert h.created == []
    assert h.staged == []
    # Refused before any ref was read, so the parent gate is what refused it.
    assert h.job_lookups == []
    assert h.campaign_lookups == [(_CID, "u-1")]


def test_a_ref_naming_a_job_from_another_run_creates_nothing(client):
    """PROVENANCE, which tenancy does not cover. The job is the caller's own
    and perfectly readable; it simply belongs to a different compute campaign,
    and staging it here would put a design the user never saw on this page into
    a paid order under this run's name."""
    jobs = {"j-far": _job("j-far", campaign_id=_OTHER_CID)}
    resp, h = _submit(client, jobs, form={"candidate_refs": _refs([("j-far", 0)])})
    assert h.created == []
    assert resp.headers["Location"].endswith("?handoff=rejected")


def test_a_target_tagged_job_is_not_a_child_of_this_run(client):
    """THE CLAUSE THAT MUST NOT BE COPIED FROM THE TARGET ARM. That arm accepts
    a job on ``job.target_id`` as a second route in, because a target pools
    standalone runs as well as campaign sub-jobs. This page has no target in
    scope and one route in, so a target-tagged job with no campaign id is a
    foreign design. Adding any ``or job.target_id == ...`` clause reds this."""
    jobs = {"j-std": _job("j-std", target_id=_TID, campaign_id=None)}
    resp, h = _submit(client, jobs, form={"candidate_refs": _refs([("j-std", 0)])})
    assert h.created == []
    assert h.staged == []
    assert resp.headers["Location"].endswith("?handoff=rejected")


# ---------------------------------------------------------------------------
# Dedupe and the index check
# ---------------------------------------------------------------------------

def test_the_same_design_named_twice_is_ordered_once(client):
    """A repeated (job_id, index) names ONE physical design. Persisting it
    would tell ops to order the same structure twice, and counting it as a drop
    would tell the user a design went missing when none did."""
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID)}
    resp, h = _submit(client, jobs, form={"candidate_refs": _refs(
        [("j-bc", 0), ("j-bc", 1), ("j-bc", 0)])})
    assert h.created[0]["candidate_refs"] == [
        {"job_id": "j-bc", "index": 0}, {"job_id": "j-bc", "index": 1},
    ]
    assert [s["indices"] for s in h.staged] == [[0, 1]]
    assert h.emails[0]["dropped"] == 0
    assert resp.headers["Location"].endswith("?submitted=1")


def test_a_ref_naming_an_index_past_the_end_of_its_job_is_refused(client):
    """Unvalidated, an out-of-range ref is persisted, counted on the staff
    email and on the customer's page -- and then silently skipped by
    ``stage_campaign_candidates``, so the lab receives fewer PDBs than every
    number anyone can see."""
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID, n=2)}
    resp, h = _submit(client, jobs, form={"candidate_refs": _refs(
        [("j-bc", 0), ("j-bc", 7)])})
    assert h.created[0]["candidate_refs"] == [{"job_id": "j-bc", "index": 0}]
    assert h.emails[0]["dropped"] == 1
    assert resp.headers["Location"].endswith("?submitted=1&dropped=1")


def test_a_job_whose_record_count_is_unknown_keeps_all_its_refs(client):
    """THE PAIR for the test above. ``candidate_records`` answers ``[]`` both
    for a job that delivered zero designs and for a result shape this app
    cannot read, so a range check built on its length has to be wrong about one
    of them. ``candidate_count`` reports None for the second, and only the
    second is a reason to wave a ref through."""
    jobs = {"j-odd": _job("j-odd", campaign_id=_CID,
                          result={"something_else": [1, 2, 3]})}
    resp, h = _submit(client, jobs, form={"candidate_refs": _refs(
        [("j-odd", 0), ("j-odd", 41)])})
    assert h.created[0]["candidate_refs"] == [
        {"job_id": "j-odd", "index": 0}, {"job_id": "j-odd", "index": 41},
    ]
    assert h.emails[0]["dropped"] == 0
    assert resp.headers["Location"].endswith("?submitted=1")


def test_a_ref_into_a_job_that_delivered_zero_designs_is_refused(client):
    """THE PAIR for the test above, in the other direction. ``{"candidates":
    []}`` HAS a known length, it is zero, and every index into it is out of
    range. Under an ``if n and idx >= n`` spelling those refs are all accepted,
    recorded on the row, counted to ops and to the customer, and stage zero
    PDBs."""
    jobs = {"j-empty": _job("j-empty", campaign_id=_CID, result={"candidates": []})}
    resp, h = _submit(client, jobs, form={"candidate_refs": _refs(
        [("j-empty", 0)])})
    assert h.created == []
    assert resp.headers["Location"].endswith("?handoff=rejected")


# ---------------------------------------------------------------------------
# The negative cache (register item A87) and what it must not swallow
# ---------------------------------------------------------------------------

def test_a_repeated_rejected_job_id_is_looked_up_once(client):
    """A miss never writes to ``jobs_by_id``, so without a negative cache a
    body naming one foreign job 40 times issues 40 identical Supabase round
    trips. The refs are distinct pairs, so dedupe cannot be what collapses
    them."""
    jobs = {"j-far": _job("j-far", campaign_id=_OTHER_CID, n=64)}
    resp, h = _submit(client, jobs, form={"candidate_refs": _refs(
        [("j-far", i) for i in range(40)])})
    assert h.job_lookups == ["j-far"]
    assert resp.headers["Location"].endswith("?handoff=rejected")


def test_every_ref_naming_one_rejected_job_counts_as_its_own_drop(client):
    """THE PAIR. The cache short-circuits the READ, not the count: ten starred
    designs from a foreign run are ten designs the user does not get, and
    reporting one would understate the shortfall by nine."""
    jobs = {
        "j-bc": _job("j-bc", campaign_id=_CID),
        "j-far": _job("j-far", campaign_id=_OTHER_CID, n=16),
    }
    resp, h = _submit(client, jobs, form={"candidate_refs": _refs(
        [("j-bc", 0)] + [("j-far", i) for i in range(10)])})
    assert h.emails[0]["dropped"] == 10
    assert resp.headers["Location"].endswith("?submitted=1&dropped=10")


def test_an_unreadable_job_is_read_once_however_many_refs_name_it(client):
    """The cache covers the TRANSIENT outcome too. Caching only permanent
    rejections leaves the amplification lever fully open on exactly the request
    where Supabase is already struggling."""
    jobs = {"j-slow": _job("j-slow", campaign_id=_CID, n=16)}
    resp, h = _submit(client, jobs, unreadable=("j-slow",), form={
        "candidate_refs": _refs([("j-slow", i) for i in range(5)])})
    assert h.job_lookups == ["j-slow"]
    assert resp.headers["Location"].endswith("?handoff=unverified")


# ---------------------------------------------------------------------------
# The refusal gate: a rejection we could not decide is not a verdict
# ---------------------------------------------------------------------------

def test_a_job_read_that_never_completed_refuses_rather_than_narrowing(client):
    """This route stages a PAID order. Proceeding on a shortlist whose refusals
    we cannot stand behind hands the wet lab a list quietly missing designs the
    user selected and paid to compute; refusing costs one click, because the
    stars live in sessionStorage and survive the redirect."""
    jobs = {
        "j-bc": _job("j-bc", campaign_id=_CID),
        "j-slow": _job("j-slow", campaign_id=_CID),
    }
    resp, h = _submit(client, jobs, unreadable=("j-slow",), form={
        "candidate_refs": _refs([("j-bc", 0), ("j-slow", 0)])})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("?handoff=unverified")
    assert h.created == []
    assert h.staged == []


def test_a_read_we_could_not_complete_never_reaches_the_rejection_wording(client):
    """THE COPY LICENCE, as behaviour. The confirmation page tells the user a
    dropped design "will be refused the same way", and the customer email calls
    it rejected; both are false for a design nobody ever managed to look at.

    This is what reds the minimal-looking version of A88 -- report ``dropped``
    while still reading through ``get_job`` -- which cannot tell a transient
    fault from a verdict and would send a database hiccup down the permanent
    path. Nothing about this shortlist may be reported as a drop, and no email
    may go out at all.
    """
    jobs = {"j-slow": _job("j-slow", campaign_id=_CID)}
    resp, h = _submit(client, jobs, unreadable=("j-slow",), form={
        "candidate_refs": _refs([("j-slow", 0)])})
    location = resp.headers["Location"]
    assert "dropped=" not in location
    assert "handoff=rejected" not in location
    assert h.emails == []


# ---------------------------------------------------------------------------
# What the user and ops are told about the shortfall
# ---------------------------------------------------------------------------

def test_a_partly_rejected_shortlist_reports_what_was_dropped(client):
    """The count has to reach BOTH channels. The confirmation page is where the
    user learns their selection was not delivered whole; the staff email is
    where ops learns the order is smaller than the page they will open."""
    jobs = {
        "j-bc": _job("j-bc", campaign_id=_CID),
        "j-far": _job("j-far", campaign_id=_OTHER_CID),
    }
    resp, h = _submit(client, jobs, form={"candidate_refs": _refs(
        [("j-bc", 0), ("j-far", 0)])})
    assert resp.headers["Location"].endswith("?submitted=1&dropped=1")
    assert h.emails[0]["dropped"] == 1


def test_a_fully_accepted_shortlist_reports_no_drops(client):
    """THE PAIR. Appending the count unconditionally would satisfy the test
    above while telling every ordinary submission that designs were refused,
    and would change the URL of the overwhelmingly common case."""
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID)}
    resp, h = _submit(client, jobs)
    assert resp.headers["Location"].endswith("?submitted=1")
    assert h.emails[0]["dropped"] == 0


def test_starred_designs_past_the_cap_are_reported_not_silently_dropped(client):
    """THE HEADLINE OF A88. ``_MAX_CANDIDATE_REFS`` truncates at parse time, so
    a count derived from ``len(candidate_refs)`` saturates and reports ZERO
    drops for a shortlist that lost designs to the bound. Stars persist in
    sessionStorage and the pooled table renders 300 rows a view, so
    accumulating past 500 is an ordinary path.

    Reported SEPARATELY from ``dropped``: these designs were never read, so
    "could not be matched" would be a verdict nobody reached. Omitting
    ``requested_refs`` at the call site reds this. Note that DEFAULTING the
    parameter to 0 would not: it is keyword-only and the dispatcher always
    passes it explicitly, so a default is unreachable and changes nothing
    observable.
    """
    from blueprints.lab_projects import _MAX_CANDIDATE_REFS
    cap = _MAX_CANDIDATE_REFS
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID, n=cap + 200)}
    resp, h = _submit(client, jobs, form={"candidate_refs": _refs(
        [("j-bc", i) for i in range(cap + 120)])})
    assert resp.status_code == 302
    assert len(h.created[0]["candidate_refs"]) == cap
    assert resp.headers["Location"].endswith("?submitted=1&truncated=120")
    assert h.emails[0]["truncated"] == 120
    # Not a rejection: nothing about these refs was ever judged.
    assert h.emails[0]["dropped"] == 0


def test_a_shortlist_inside_the_cap_reports_no_truncation(client):
    """THE PAIR. Sending ``truncated`` unconditionally would satisfy the test
    above while telling every ordinary submission that designs were cut."""
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID)}
    resp, h = _submit(client, jobs)
    assert resp.headers["Location"].endswith("?submitted=1")
    assert h.emails[0]["truncated"] == 0


# ---------------------------------------------------------------------------
# The five failure exits, which were four bare redirects and one wrong page
# ---------------------------------------------------------------------------

def test_an_empty_campaign_shortlist_returns_to_the_run_not_to_jobs(client):
    """The dispatcher gate. The shortlist bar's button carries no ``disabled``
    attribute in any scope and ``openCampaignModal`` has no zero-star guard, so
    an empty body is reachable from this page. Gated on ``and candidate_refs``
    it falls through to the legacy single-job arm, finds no ``source_job_id``
    and lands the user on /jobs -- an unrelated list, after they clicked "Send
    shortlist"."""
    resp, h = _submit(client, {}, form={"candidate_refs": "[]"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/campaigns/{_CID}?handoff=none")
    assert h.created == []
    assert h.job_lookups == []


def test_an_unnamed_target_says_so_rather_than_failing_silently(client):
    """The two causes were one guard and one silent redirect, so a user who
    left the name blank was told nothing at all. They are split because telling
    the empty-shortlist user to "name your target" would be a lie, and vice
    versa."""
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID)}
    resp, h = _submit(client, jobs, form={"target_name": ""})
    assert resp.headers["Location"].endswith("?handoff=noname")
    assert h.created == []
    # Refused before any read: the name is checked on the request, not per ref.
    assert h.job_lookups == []


def test_a_shortlist_whose_every_ref_was_rejected_says_rejected_not_none(client):
    """NOT ``none``. The request DID carry designs and every one of them failed
    a check that ran, so the same refs will fail identically. ``none`` tells the
    user to press the button again, which for this shortlist can never work."""
    jobs = {"j-far": _job("j-far", campaign_id=_OTHER_CID)}
    resp, h = _submit(client, jobs, form={"candidate_refs": _refs(
        [("j-far", 0)])})
    assert resp.headers["Location"].endswith("?handoff=rejected")
    assert h.created == []


def test_a_failed_lab_project_creation_says_so(client):
    """Register item A-8 on this arm. The insert returning None sent the user
    back to the run page with no banner, no error and nothing changed, which
    reads as "the button does nothing" rather than "your submission failed"."""
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID)}
    resp, h = _submit(client, jobs, create_result=None)
    assert resp.headers["Location"].endswith("?handoff=failed")
    assert h.staged == []


def test_a_rejected_assay_type_says_so_rather_than_failing_silently(client):
    """The ``except ValueError`` arm is live: ``create_campaign_from_refs``
    raises on an unknown assay_type or budget_band. Deleting the handler turns
    a mistyped form field into a 500 on a paid intake path."""
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID)}
    resp, h = _submit(
        client, jobs, create_result=ValueError("invalid assay_type: 'x'"),
    )
    assert resp.headers["Location"].endswith("?handoff=failed")
    assert len(h.created) == 1
    assert h.staged == []


# ---------------------------------------------------------------------------
# The size cap rides the failure exits too
# ---------------------------------------------------------------------------

def _over_cap_refs(job_id, extra=120):
    from blueprints.lab_projects import _MAX_CANDIDATE_REFS
    return _refs([(job_id, i) for i in range(_MAX_CANDIDATE_REFS + extra)])


def test_a_wholly_rejected_shortlist_still_reports_what_the_cap_discarded(client):
    """Computed above the guards so it rides every exit. Derived after the loop
    it is out of reach of the failure paths -- so a 620-star shortlist that was
    refused has 120 designs nobody ever mentions, on precisely the paths where
    the user is already being told something went wrong."""
    jobs = {"j-far": _job("j-far", campaign_id=_OTHER_CID, n=800)}
    resp, _ = _submit(client, jobs, form={
        "candidate_refs": _over_cap_refs("j-far")})
    assert resp.headers["Location"].endswith("?handoff=rejected&truncated=120")


def test_an_unverifiable_shortlist_still_reports_what_the_cap_discarded(client):
    """The same, on the exit that fires when the database is degraded -- the
    one where a user is most likely to retry and least likely to be told why
    the retry will also come up short."""
    jobs = {"j-slow": _job("j-slow", campaign_id=_CID, n=800)}
    resp, _ = _submit(client, jobs, unreadable=("j-slow",), form={
        "candidate_refs": _over_cap_refs("j-slow")})
    assert resp.headers["Location"].endswith("?handoff=unverified&truncated=120")


def test_a_refused_shortlist_inside_the_cap_carries_no_truncation(client):
    """THE PAIR for both. An unconditional suffix would satisfy them while
    telling every ordinary refusal that designs were also cut for size."""
    jobs = {"j-far": _job("j-far", campaign_id=_OTHER_CID)}
    resp, _ = _submit(client, jobs, form={"candidate_refs": _refs(
        [("j-far", 0)])})
    assert resp.headers["Location"].endswith("?handoff=rejected")


# ---------------------------------------------------------------------------
# The two things this arm must NOT borrow from the target arm's tail
# ---------------------------------------------------------------------------

def test_the_staff_email_is_not_given_a_single_tool_breakdown(client):
    """``source_tools`` is a {tool: count} spread over an accepted shortlist.
    This one spans sub-jobs of a compute campaign, and ``compute_campaigns.
    tool`` is NOT NULL (migration 0034), so the row would print one tool back
    at ops -- and ``send_campaign_submitted_emails`` documents that this arm
    omits it."""
    jobs = {"j-bc": _job("j-bc", campaign_id=_CID)}
    _, h = _submit(client, jobs)
    assert h.emails[0].get("source_tools") is None


def test_the_staging_prefix_is_the_first_8_chars_of_the_job_id(client):
    """The target arm namespaces with ``<tool>-<job8>/`` because a target pools
    many tools that all name their first design design_1.pdb. One compute
    campaign has one tool, so there is no collision to namespace against, and
    changing this prefix would split the bucket layout of new orders from every
    campaign-sourced folder ops already has open.

    The id here is DELIBERATELY longer than 8 characters. A short fake makes
    ``jid[:8]`` and ``jid`` indistinguishable, so the assertion would hold for
    either spelling and the test would endorse whichever one the code happened
    to use -- which is how the earlier name for this test came to say "the bare
    job id" of a prefix that is not the bare job id."""
    jid = "j-bc-0123456789"
    jobs = {jid: _job(jid, campaign_id=_CID)}
    _, h = _submit(client, jobs, form={"candidate_refs": _refs([(jid, 0)])})
    assert h.staged[0]["prefix"] == "j-bc-012/"
    # The pair: not the untruncated id, and not the target arm's tool slug.
    assert h.staged[0]["prefix"] != f"{jid}/"
    assert not h.staged[0]["prefix"].startswith("bindcraft-")


# ---------------------------------------------------------------------------
# The confirmation page's shortfall copy serves BOTH parents
# ---------------------------------------------------------------------------

class _Paragraphs(HTMLParser):
    """Visible text of each ``<p>``, so an assertion can be scoped to the
    shortfall sentence rather than to the whole page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._buf: list = []
        self.paragraphs: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self._depth += 1

    def handle_endtag(self, tag):
        if tag == "p" and self._depth:
            self._depth -= 1
            if not self._depth:
                self.paragraphs.append(re.sub(r"\s+", " ", "".join(self._buf)).strip())
                self._buf = []

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)


def _shortfall_paragraph(client, row):
    from shared.campaigns import Campaign
    campaign = Campaign.from_row(row)
    _login(client)
    with patch("blueprints.lab_projects.load_user_context", return_value=_ctx()), \
            patch("shared.campaigns.get_campaign", return_value=campaign):
        resp = client.get("/lab-projects/lab-9?submitted=1&dropped=3")
    assert resp.status_code == 200, resp.status_code
    parser = _Paragraphs()
    parser.feed(resp.get_data(as_text=True))
    hits = [p for p in parser.paragraphs if "were not included" in p]
    assert len(hits) == 1, parser.paragraphs
    return hits[0]


_BASE_ROW = {
    "id": "lab-9", "user_id": "u-1", "target_name": "HER2",
    "assay_type": "yeast_display", "budget_band": "pilot",
    "status": "submitted", "candidate_indices": [],
    "candidate_refs": [{"job_id": "j-bc", "index": 0}],
}


def test_the_shortfall_banner_reads_the_same_for_a_campaign_sourced_row_as_a_target_one(client):
    """ONE STRING, TWO PARENTS. The sentence used to say the designs "could not
    be matched to a design on the source target", which is simply false for a
    campaign-sourced row: there is no target in that submission and the test
    that refused those designs was "child of this compute campaign".

    Fixed by REWORDING, not by branching on ``submission_source``. A branch
    acquires a stale else-arm the moment a fifth source appears -- the defect
    ``blueprints/targets.py`` records having paid for twice -- and the other
    branches on that field in this template choose LINKS, where a missed branch
    renders nothing rather than a false sentence.
    """
    target_row = dict(_BASE_ROW, submission_source="target",
                      source_target_id=_TID)
    campaign_row = dict(_BASE_ROW, submission_source="campaign",
                        source_campaign_id=_CID)
    from_target = _shortfall_paragraph(client, target_row)
    from_campaign = _shortfall_paragraph(client, campaign_row)
    assert from_target == from_campaign
    assert "a design in the results this shortlist was built from" in from_campaign
    # The parent-specific wording it replaced, in either direction.
    assert "source target" not in from_campaign
    assert "this compute campaign" not in from_campaign
