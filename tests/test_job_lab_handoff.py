"""POST /lab-projects/submit with a SINGLE-JOB shortlist (register item A91).

The submit-side sibling of ``tests/test_campaign_lab_handoff.py`` and
``tests/test_target_lab_handoff.py``. It is the arm every ordinary tool results
page posts to -- 13 of the 14 per-tool templates under ``templates/tools/``
render the shortlist modal through ``components/results_shell.html``, and only
``mpnn_results.html`` does not, because its output is ``sequences`` rather than
per-candidate rows -- and it is the arm that had no test at all: nothing
anywhere posted ``candidate_indices`` as a form field, so every claim below was
unheld.

WHAT IT LOOKED LIKE. One guard collapsed three causes (no parent id, no target
name, no shortlist) into a single silent redirect to /jobs. There was no dedupe
and no per-index check of any kind, so a repeated design was ordered twice and
an index past the end of the job was persisted, counted on the staff email and
on the customer's page, and then silently skipped by
``stage_campaign_candidates`` -- the lab receiving fewer PDBs than every number
anyone could see. Every failure past that guard also redirected to /jobs
without a reason.

THE ARM READS ITS JOB THROUGH ``read_job``, NOT ``get_job``, and that swap is a
precondition for reporting a drop count rather than a separate improvement.
``get_job`` answers ``None`` for a job that is absent, one that is another
tenant's, and a read that never completed, and this arm sent all three to /jobs.
``send_campaign_submitted_emails`` documents that a caller may call its
``dropped`` count a rejection only because it refuses outright when a refusal
had an undecidable cause, which is not decidable from that ``None``. ``_submit``
below therefore fakes ``read_job`` and can be told to make the job UNREADABLE,
which is the only way to build that fault.

THE PARENT AND THE SOURCE OF THE DESIGNS ARE THE SAME ROW HERE, which is the
one structural difference from both siblings. They read a parent (a run, a
target) and then re-read a job per ref inside the loop; this arm's parent IS its
only job, so the single read above the loop answers tenancy, existence and the
index bound at once, and no refusal inside the loop can have a cause the
database produced.

Everything is patched at ITS OWN module, matching how
``blueprints/lab_projects.py`` imports it: ``read_job`` and
``stage_campaign_candidates`` are module-level imports on the blueprint, while
``create_campaign`` and ``send_campaign_submitted_emails`` are function-local
and therefore resolve against their own modules at call time.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared.jobs import JOB_READ_ABSENT, JOB_READ_OK, JOB_READ_UNAVAILABLE, JobRead

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_JID = "job-" + str(uuid.uuid4())
_OTHER_JID = "job-" + str(uuid.uuid4())


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


def _job(jid=_JID, tool="bindcraft", *, n=3, owner="u-1",
         result=_DEFAULT_RESULT):
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
        target_id=None, campaign_id=None,
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
        # (id, user_id) per call. The user_id half is the point: it is the only
        # thing that fails when the route stops passing the owner scope.
        self.job_lookups: list[tuple] = []


_CREATE_OK = object()


def _submit(client, jobs, *, form=None, create_result=_CREATE_OK,
            unreadable=()):
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

    ``create_result`` overrides what ``create_campaign`` returns, or -- when it
    is an ``Exception`` instance -- what it RAISES. The real function raises
    ``ValueError`` on an unknown assay_type or budget_band, so the route's
    ``except ValueError`` arm is live and needs a fake that reaches it.

    THE DEFAULT BODY CARRIES ``candidate_indices`` AND NO ``candidate_refs``,
    which is what every page posted before A91 and what a page served before
    this deploy still posts. A test that means to exercise the refs payload
    passes it in ``form``; one that means to leave NO shortlist at all has to
    blank BOTH fields, because the arm falls back to the indices whenever the
    refs parse to nothing.
    """
    h = _Harness()

    def fake_read_job(jid, *, user_id=None):
        # Models shared.jobs.read_job, including the two things that matter
        # here: `user_id` is a QUERY FILTER, so another tenant's job comes back
        # as zero rows -- ABSENT, not a distinct "forbidden" outcome, because
        # telling those apart would mean reading a row the owner scope exists
        # to withhold. And an unreadable id reports UNAVAILABLE even when the
        # job is present, because a read that did not complete learned nothing.
        h.job_lookups.append((jid, user_id))
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

    body = {
        "source_job_id": _JID,
        "candidate_indices": json.dumps([0]),
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
            patch("shared.campaigns.create_campaign", side_effect=fake_create), \
            patch("shared.email.send_campaign_submitted_emails",
                  side_effect=fake_email):
        resp = client.post("/lab-projects/submit", data=body)
    return resp, h


def _as_refs(idxs, job_id=_JID):
    """The shortlist as the ``candidate_refs`` payload both other arms post."""
    return {"candidate_refs": json.dumps(
        [{"job_id": job_id, "index": i} for i in idxs])}


def _as_indices(idxs, job_id=_JID):
    """The same shortlist as the bare-int payload this arm has always posted.

    ``job_id`` is accepted and ignored: this shape cannot name a job, which is
    the whole reason the arm had to assume one. Present so the two builders are
    interchangeable under ``_SHAPES``.
    """
    return {"candidate_indices": json.dumps(list(idxs))}


# The behaviours below hold for a shortlist however it arrived, so they are
# driven through BOTH payloads rather than through whichever one the code
# happens to prefer. Testing only the preferred shape would leave the fallback
# -- the shape every page served before this deploy still posts -- unheld.
_SHAPES = [
    pytest.param(_as_refs, id="candidate_refs"),
    pytest.param(_as_indices, id="candidate_indices"),
]


def _no_shortlist():
    return {"candidate_refs": "[]", "candidate_indices": "[]"}


# ---------------------------------------------------------------------------
# Two payload shapes on one arm, and which one wins
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", _SHAPES)
def test_either_payload_creates_the_same_lab_project(client, shape):
    """The happy path, twice. A page served before A91 posts bare ints and a
    page served after it posts refs as well, and both must produce the same
    row: same source job, same indices, in the order the user starred them."""
    resp, h = _submit(client, {_JID: _job()}, form=shape([0, 2]))
    assert resp.status_code == 302
    assert len(h.created) == 1
    assert h.created[0]["source_job_id"] == _JID
    assert h.created[0]["candidate_indices"] == [0, 2]
    assert resp.headers["Location"].endswith("/lab-projects/lab-1?submitted=1")


def test_the_refs_payload_is_preferred_when_both_are_posted(client):
    """The macro's job branch now emits BOTH fields and the JS fills both, so
    the ordinary live request carries two shortlists. They agree in production;
    this asserts WHICH one the arm reads, because a rule that only ever gets
    exercised on agreeing inputs is not a rule anybody can rely on."""
    body = {}
    body.update(_as_refs([1]))
    body.update(_as_indices([0]))
    _, h = _submit(client, {_JID: _job()}, form=body)
    assert h.created[0]["candidate_indices"] == [1]


def test_the_indices_payload_is_read_when_the_refs_payload_is_empty(client):
    """THE PAIR, and the entire reason both fields are emitted for a release. A
    page cached across the deploy posts an empty ``candidate_refs`` (the macro's
    render-time value, which only the JS fills) beside a real
    ``candidate_indices``. Preferring refs unconditionally would read the empty
    one and answer `none` to a user who starred designs."""
    body = {"candidate_refs": "[]"}
    body.update(_as_indices([2]))
    _, h = _submit(client, {_JID: _job()}, form=body)
    assert h.created[0]["candidate_indices"] == [2]


def test_no_candidate_refs_column_is_written_on_this_row(client):
    """``blueprints.admin._ref_shortlist_view`` switches on that column being
    non-empty, so filling it would move the fulfilment page of every single-job
    order off the index list ops reads today --
    ``tests/test_admin_shortlist_fulfilment.py`` calls that "the arm that must
    NOT change". The refs payload is a WIRE format on this arm and nothing
    more."""
    _, h = _submit(client, {_JID: _job()}, form=_as_refs([0]))
    assert "candidate_refs" not in h.created[0]
    assert h.created[0]["candidate_indices"] == [0]


# ---------------------------------------------------------------------------
# The bare-int payload's own sanitizer, which nothing else on this arm repeats
# ---------------------------------------------------------------------------

def test_one_malformed_index_does_not_discard_the_whole_shortlist(client):
    """THE HEADLINE CLAIM of ``_parse_candidate_indices_counted``'s docstring,
    and it is a reachable path rather than a defensive one. ``starRef`` reads
    ``dataset.refIdx`` and falls back to ``dataset.idx``, then ``parseInt``s
    whichever it got, so a star button carrying NEITHER attribute yields
    ``NaN`` -- which ``JSON.stringify`` writes as ``null``.
    ``openCampaignModal`` posts ``sl.map(function (r) { return r.i; })``, so
    that ``null`` arrives here inside an otherwise ordinary shortlist.

    The arm's previous ``[int(i) for i in json.loads(raw)]`` inside a bare
    ``except`` threw the WHOLE list away on the first one, which answers
    `none` -- "you starred nothing" -- to a user who starred three designs, and
    creates nothing. An all-or-nothing pre-pass anywhere in this parser
    restores exactly that.
    """
    body = {"candidate_indices": json.dumps([0, None, 2])}
    resp, h = _submit(client, {_JID: _job(n=3)}, form=body)
    # First, so the all-or-nothing failure reports the redirect it produced
    # rather than an IndexError off an empty `created`.
    assert h.created, resp.headers["Location"]
    assert h.created[0]["candidate_indices"] == [0, 2]
    assert h.staged[0]["indices"] == [0, 2]
    # A client defect is not a design the user chose, so it is neither a drop
    # nor a truncation and the URL is the ordinary one. The parser excludes it
    # from `requested` for the same reason the ref parser does.
    assert resp.headers["Location"].endswith("?submitted=1")
    assert h.emails[0]["dropped"] == 0
    assert h.emails[0]["truncated"] == 0


def test_a_negative_index_never_reaches_the_row(client):
    """THE LOWER BOUND, and this parser is the only thing anywhere that holds
    it. The arm's own range check is ``idx >= n_records`` -- upper bound only
    -- so a ``-1`` that got past here would be persisted on the row, reported
    to nobody as a drop, and then skipped by ``stage_campaign_candidates``:
    the "lab receives fewer PDBs than every number anyone can see" failure, at
    the other end.

    ``_parse_candidate_refs_counted`` carries the same guard and
    ``tests/test_campaign_results.py::test_parse_candidate_refs_sanitizes``
    pins it. This payload's copy had nothing.
    """
    resp, h = _submit(client, {_JID: _job(n=3)}, form=_as_indices([0, -1, 2]))
    assert h.created[0]["candidate_indices"] == [0, 2]
    assert h.staged[0]["indices"] == [0, 2]
    assert resp.headers["Location"].endswith("?submitted=1")
    assert h.emails[0]["dropped"] == 0
    assert h.emails[0]["truncated"] == 0


# ---------------------------------------------------------------------------
# Refs that name no job
# ---------------------------------------------------------------------------

def test_a_ref_that_names_no_job_is_credited_to_the_source_job(client):
    """``loadShortlist`` coerces a legacy bare-int sessionStorage entry to
    ``{j: null, i}``, and ``_parse_candidate_refs_counted`` drops an entry whose
    job_id is empty. In job scope the job IS known -- it is ``source_job_id`` --
    so the design the user starred is resolvable and must not vanish."""
    body = {"candidate_refs": json.dumps(
        [{"job_id": None, "index": 1}, {"index": 2}])}
    resp, h = _submit(client, {_JID: _job()}, form=body)
    assert h.created[0]["candidate_indices"] == [1, 2]
    assert resp.headers["Location"].endswith("?submitted=1")


def test_unattributed_refs_past_the_cap_are_still_counted_as_truncated(client):
    """THE HALF THAT PINS WHERE THE STAMPING HAPPENS. The parser counts an entry
    as requested only when it also accepts it, so an entry stamped AFTER the
    counted parse is missing from the shortlist and from the truncation
    disclosure at once -- the user is told nothing about designs the cap
    removed, because as far as the count is concerned they were never asked
    for."""
    from blueprints.lab_projects import _MAX_CANDIDATE_REFS
    cap = _MAX_CANDIDATE_REFS
    body = {
        "candidate_refs": json.dumps(
            [{"job_id": None, "index": i} for i in range(cap + 120)]),
        "candidate_indices": "[]",
    }
    resp, h = _submit(client, {_JID: _job(n=cap + 200)}, form=body)
    assert len(h.created[0]["candidate_indices"]) == cap
    assert resp.headers["Location"].endswith("?submitted=1&truncated=120")
    assert h.emails[0]["truncated"] == 120


# ---------------------------------------------------------------------------
# Dedupe, parentage and the index check -- none of which this arm had
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape", _SHAPES)
def test_the_same_design_named_twice_is_ordered_once(client, shape):
    """A repeated design names ONE physical structure. Persisting it twice
    tells ops to order the same structure twice, and counting the second as a
    drop tells the user a design went missing when none did."""
    resp, h = _submit(client, {_JID: _job()}, form=shape([0, 1, 0]))
    assert h.created[0]["candidate_indices"] == [0, 1]
    assert h.staged[0]["indices"] == [0, 1]
    assert h.emails[0]["dropped"] == 0
    assert resp.headers["Location"].endswith("?submitted=1")


@pytest.mark.parametrize("shape", _SHAPES)
def test_a_repeated_out_of_range_design_is_one_drop_not_two(client, shape):
    """WHERE THE DEDUPE SITS, which is ABOVE both checks rather than below
    them. The test above pairs with this one and cannot replace it: an
    IN-RANGE repeat is collapsed identically wherever the dedupe runs, because
    the checks it would have to pass first are checks it passes. Only a repeat
    that also FAILS a check tells the two orders apart.

    ``dropped`` is documented as distinct DESIGNS refused, and
    ``shared/email.py`` prints it to ops as the shortfall against the order
    they are about to fulfil. Counted below the checks, one missing design is
    announced as two -- a number that matches nothing on the page ops opens,
    on the arm whose whole A91 fix was making that number honest.
    """
    resp, h = _submit(client, {_JID: _job(n=2)}, form=shape([0, 7, 7]))
    assert h.created[0]["candidate_indices"] == [0]
    assert h.staged[0]["indices"] == [0]
    assert h.emails[0]["dropped"] == 1
    assert resp.headers["Location"].endswith("?submitted=1&dropped=1")


def test_a_foreign_ref_cannot_swallow_the_same_index_in_the_users_own_job(
    client,
):
    """THE DEDUPE KEY, which is the PAIR ``(job_id, index)`` and not the index
    on its own. Design 0 of another job and design 0 of this one are two
    different physical structures, and a key of one field collapses them --
    losing the caller's own, because the foreign entry comes first and the
    parentage check has already counted it as a drop.

    Every entry would then have been refused, so the arm answers `rejected`,
    whose banner promises the same selection "will be refused the same way".
    That is false here: the one design the user actually owns was never judged.

    ``test_a_ref_naming_another_job_is_refused`` cannot catch this -- its two
    entries carry different indices, so a one-field key behaves identically.
    """
    jobs = {_JID: _job(), _OTHER_JID: _job(_OTHER_JID)}
    body = {"candidate_refs": json.dumps([
        {"job_id": _OTHER_JID, "index": 0},
        {"job_id": _JID, "index": 0},
    ])}
    resp, h = _submit(client, jobs, form=body)
    # First, so a one-field key reports the `?handoff=rejected` it produced
    # rather than an IndexError off an empty `created`.
    assert h.created, resp.headers["Location"]
    assert h.created[0]["candidate_indices"] == [0]
    assert h.staged[0]["indices"] == [0]
    assert h.emails[0]["dropped"] == 1
    assert resp.headers["Location"].endswith("?submitted=1&dropped=1")


@pytest.mark.parametrize("shape", _SHAPES)
def test_an_index_past_the_end_of_the_job_is_refused(client, shape):
    """Unvalidated, an out-of-range index is persisted, counted on the staff
    email and on the customer's page -- and then silently skipped by
    ``stage_campaign_candidates``, so the lab receives fewer PDBs than every
    number anyone can see. This arm ran no per-index check at all."""
    resp, h = _submit(client, {_JID: _job(n=2)}, form=shape([0, 7]))
    assert h.created[0]["candidate_indices"] == [0]
    assert h.staged[0]["indices"] == [0]
    assert h.emails[0]["dropped"] == 1
    assert resp.headers["Location"].endswith("?submitted=1&dropped=1")


def test_a_job_whose_record_count_is_unknown_keeps_all_its_indices(client):
    """THE PAIR for the test above. ``candidate_records`` answers ``[]`` both
    for a job that delivered zero designs and for a result shape this app cannot
    read, so a range check built on its length has to be wrong about one of
    them. ``candidate_count`` reports None for the second, and only the second
    is a reason to wave an index through."""
    job = _job(result={"something_else": [1, 2, 3]})
    resp, h = _submit(client, {_JID: job}, form=_as_indices([0, 41]))
    assert h.created[0]["candidate_indices"] == [0, 41]
    assert h.emails[0]["dropped"] == 0
    assert resp.headers["Location"].endswith("?submitted=1")


def test_an_index_into_a_job_that_delivered_zero_designs_is_refused(client):
    """THE PAIR for the test above, in the other direction. ``{"candidates":
    []}`` HAS a known length, it is zero, and every index into it is out of
    range. Under an ``if n and idx >= n`` spelling those are all accepted,
    recorded on the row, counted to ops and to the customer, and stage zero
    PDBs."""
    job = _job(result={"candidates": []})
    resp, h = _submit(client, {_JID: job}, form=_as_indices([0]))
    assert h.created == []
    assert resp.headers["Location"].endswith(f"/jobs/{_JID}?handoff=rejected")


def test_a_ref_naming_another_job_is_refused(client):
    """PROVENANCE. This arm stores BARE INDICES against one ``source_job_id``
    and no CHECK constrains their values, so an accepted foreign ref would be
    persisted as an index into a job the row does not name -- and staged out of
    this job's results, which is a different design or none. The other job here
    is the caller's own and perfectly readable; only the parentage test refuses
    it."""
    jobs = {_JID: _job(), _OTHER_JID: _job(_OTHER_JID)}
    body = {}
    body.update(_as_refs([0]))
    body["candidate_refs"] = json.dumps([
        {"job_id": _JID, "index": 0},
        {"job_id": _OTHER_JID, "index": 1},
    ])
    resp, h = _submit(client, jobs, form=body)
    assert h.created[0]["candidate_indices"] == [0]
    assert h.emails[0]["dropped"] == 1
    assert resp.headers["Location"].endswith("?submitted=1&dropped=1")
    # One read, of the parent. The foreign ref is refused from the request
    # itself, so a body naming 500 other jobs issues no extra round trips.
    assert h.job_lookups == [(_JID, "u-1")]


def test_a_shortlist_of_nothing_but_foreign_refs_says_rejected(client):
    """THE PAIR. Every entry failed a check that ran to completion, so the same
    shortlist will be refused identically -- which is `rejected`, not `none`,
    and not a lab project holding zero designs."""
    jobs = {_JID: _job(), _OTHER_JID: _job(_OTHER_JID)}
    body = {"candidate_refs": json.dumps([{"job_id": _OTHER_JID, "index": 0}])}
    resp, h = _submit(client, jobs, form=body)
    assert h.created == []
    assert h.staged == []
    assert resp.headers["Location"].endswith(f"/jobs/{_JID}?handoff=rejected")


# ---------------------------------------------------------------------------
# The parent read, its three outcomes, and the order they are answered in
# ---------------------------------------------------------------------------

def test_the_job_read_carries_the_callers_owner_scope(client):
    """TENANCY, and it is observable only in what was ASKED. A route that
    stopped passing ``user_id=`` would still get ABSENT for a genuinely absent
    id and still bounce, so the harness records the pair rather than the id."""
    _, h = _submit(client, {_JID: _job()}, form=_as_indices([0]))
    assert h.job_lookups == [(_JID, "u-1")]


def test_a_shortlist_against_another_users_job_creates_nothing(client):
    """The owner scope is applied as a query filter, so another tenant's job
    comes back ABSENT and the arm bounces before anything is created."""
    resp, h = _submit(client, {_JID: _job(owner="u-2")})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/jobs")
    assert h.created == []
    assert h.staged == []


def test_an_unreadable_job_refuses_with_a_reason(client):
    """THE FIX A91 IS BUILT ON. Under ``get_job`` a transient Supabase fault was
    indistinguishable from a deleted job and from another tenant's, and all
    three left the user on an unrelated list with nothing said -- on the one
    action that hands work to a wet lab. It now lands them back on THIS job's
    page with a reason."""
    resp, h = _submit(client, {_JID: _job()}, unreadable=(_JID,))
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{_JID}?handoff=unverified")
    assert h.created == []
    assert h.staged == []


def test_an_absent_job_still_bounces_in_silence(client):
    """THE OTHER HALF, and the one that pins the two outcomes apart. The job
    really is gone or was never this caller's, so there is no page to land them
    on and nothing to say beyond returning them to their jobs. Without this a
    later "simplification" could route both outcomes to `?handoff=unverified` --
    telling a user whose job was deleted to try again forever -- and the test
    above would stay green."""
    resp, h = _submit(client, {})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/jobs")
    assert "handoff" not in resp.headers["Location"]
    assert h.created == []


def test_an_unreadable_job_never_reaches_the_rejection_wording(client):
    """ORDER IS LOAD-BEARING, and this is the test that holds it. `rejected`'s
    banner promises the same selection "will be refused the same way", which is
    false for a fault that will be gone in two seconds. Every entry in this
    shortlist would be refused on provenance if the loop ran, so an arm that
    answered the empty accepted list before consulting the read would send a
    database hiccup down the permanent path.

    Nothing about this shortlist may be reported as a drop and no email may go
    out at all.
    """
    jobs = {_JID: _job(), _OTHER_JID: _job(_OTHER_JID)}
    body = {"candidate_refs": json.dumps([{"job_id": _OTHER_JID, "index": 0}])}
    resp, h = _submit(client, jobs, form=body, unreadable=(_JID,))
    location = resp.headers["Location"]
    assert location.endswith(f"/jobs/{_JID}?handoff=unverified")
    assert "dropped=" not in location
    assert "handoff=rejected" not in location
    assert h.emails == []
    assert h.created == []


# ---------------------------------------------------------------------------
# The five reasons, which were one silent redirect to an unrelated list
# ---------------------------------------------------------------------------

def test_an_empty_shortlist_returns_to_the_job_not_to_jobs(client):
    """The shortlist bar's button carries no ``disabled`` attribute in any scope
    and ``openCampaignModal`` has no zero-star guard, so an empty body is
    reachable from this page by clicking "Send shortlist" with nothing
    starred."""
    resp, h = _submit(client, {_JID: _job()}, form=_no_shortlist())
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{_JID}?handoff=none")
    assert h.created == []
    # Refused before any read: an empty shortlist is decided on the request.
    assert h.job_lookups == []


def test_an_unnamed_target_says_so_rather_than_failing_silently(client):
    """Split from the empty-shortlist cause because telling that user to "name
    your target" would be a lie, and vice versa. They were one guard and one
    silent redirect, so both users were told the same nothing."""
    resp, h = _submit(client, {_JID: _job()}, form={"target_name": ""})
    assert resp.headers["Location"].endswith(f"/jobs/{_JID}?handoff=noname")
    assert h.created == []
    # Refused before any read: the name is checked on the request, not per ref.
    assert h.job_lookups == []


def test_a_body_with_no_parent_id_at_all_still_goes_to_the_jobs_list(client):
    """The third cause the old guard folded in, and the only silent exit left on
    the route. With no parent of any of the three shapes there is no page to
    return this user to and nothing to say on it."""
    resp, h = _submit(client, {_JID: _job()}, form={"source_job_id": ""})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/jobs")
    assert "handoff" not in resp.headers["Location"]
    assert h.created == []
    assert h.job_lookups == []


def test_a_failed_lab_project_creation_says_so(client):
    """Register item A-8 on this arm, fixed on the two ref arms first. The
    insert returning None sent the user to /jobs with no banner, no error and
    nothing changed, which reads as "the button does nothing" rather than "your
    submission failed"."""
    resp, h = _submit(client, {_JID: _job()}, create_result=None)
    assert resp.headers["Location"].endswith(f"/jobs/{_JID}?handoff=failed")
    assert h.staged == []
    assert h.emails == []


def test_a_rejected_assay_type_says_so_rather_than_failing_silently(client):
    """The ``except ValueError`` arm is live: ``create_campaign`` raises on an
    unknown assay_type or budget_band, and on an empty index list. Deleting the
    handler turns a mistyped form field into a 500 on a paid intake path."""
    resp, h = _submit(
        client, {_JID: _job()},
        create_result=ValueError("invalid assay_type: 'x'"),
    )
    assert resp.headers["Location"].endswith(f"/jobs/{_JID}?handoff=failed")
    assert len(h.created) == 1
    assert h.staged == []


# ---------------------------------------------------------------------------
# What the user and ops are told about the shortfall
# ---------------------------------------------------------------------------

def test_a_partly_rejected_shortlist_reports_what_was_dropped(client):
    """The count has to reach BOTH channels. The confirmation page is where the
    user learns their selection was not delivered whole; the staff email is
    where ops learns the order is smaller than the page they will open."""
    resp, h = _submit(client, {_JID: _job(n=2)}, form=_as_indices([0, 9]))
    assert resp.headers["Location"].endswith("?submitted=1&dropped=1")
    assert h.emails[0]["dropped"] == 1


def test_a_fully_accepted_shortlist_reports_no_drops(client):
    """THE PAIR. Appending the count unconditionally would satisfy the test
    above while telling every ordinary submission that designs were refused, and
    would change the URL of the overwhelmingly common case."""
    resp, h = _submit(client, {_JID: _job()})
    assert resp.headers["Location"].endswith("?submitted=1")
    assert h.emails[0]["dropped"] == 0
    assert h.emails[0]["truncated"] == 0


@pytest.mark.parametrize("shape", _SHAPES)
def test_starred_designs_past_the_cap_are_reported_not_silently_dropped(
    client, shape,
):
    """THE HEADLINE OF A91 for this arm. ``candidate_indices`` used to be parsed
    by nothing here: uncapped, so an unbounded array was accepted whole, and
    uncounted, so once a bound existed there was nothing to compare its output
    against. Stars persist in sessionStorage and a results table renders
    hundreds of rows, so accumulating past 500 is an ordinary path.

    Reported SEPARATELY from ``dropped``: these designs were never read, so
    "could not be matched" would be a verdict nobody reached.
    """
    from blueprints.lab_projects import _MAX_CANDIDATE_REFS
    cap = _MAX_CANDIDATE_REFS
    job = _job(n=cap + 200)
    resp, h = _submit(client, {_JID: job}, form=shape(range(cap + 120)))
    assert resp.status_code == 302
    assert len(h.created[0]["candidate_indices"]) == cap
    assert resp.headers["Location"].endswith("?submitted=1&truncated=120")
    assert h.emails[0]["truncated"] == 120
    # Not a rejection: nothing about these entries was ever judged.
    assert h.emails[0]["dropped"] == 0


def test_a_shortlist_inside_the_cap_reports_no_truncation(client):
    """THE PAIR. Sending ``truncated`` unconditionally would satisfy the test
    above while telling every ordinary submission that designs were cut."""
    resp, h = _submit(client, {_JID: _job()})
    assert resp.headers["Location"].endswith("?submitted=1")
    assert "truncated" not in resp.headers["Location"]
    assert h.emails[0]["truncated"] == 0


def test_a_wholly_rejected_shortlist_still_reports_what_the_cap_discarded(client):
    """``truncated`` is computed above the guards so it rides every exit. Derived
    after the loop it is out of reach of the failure paths -- so a 620-star
    shortlist that was refused has 120 designs nobody ever mentions, on precisely
    the paths where the user is already being told something went wrong."""
    from blueprints.lab_projects import _MAX_CANDIDATE_REFS
    cap = _MAX_CANDIDATE_REFS
    job = _job(n=0, result={"candidates": []})
    resp, _ = _submit(client, {_JID: job}, form=_as_indices(range(cap + 120)))
    assert resp.headers["Location"].endswith("?handoff=rejected&truncated=120")


def test_an_unverifiable_shortlist_still_reports_what_the_cap_discarded(client):
    """The same, on the exit that fires when the database is degraded -- the one
    where a user is most likely to retry and least likely to be told why the
    retry will also come up short."""
    from blueprints.lab_projects import _MAX_CANDIDATE_REFS
    cap = _MAX_CANDIDATE_REFS
    resp, _ = _submit(client, {_JID: _job(n=cap + 200)}, unreadable=(_JID,),
                      form=_as_indices(range(cap + 120)))
    assert resp.headers["Location"].endswith("?handoff=unverified&truncated=120")


def test_a_refused_shortlist_inside_the_cap_carries_no_truncation(client):
    """THE PAIR for both. An unconditional suffix would satisfy them while
    telling every ordinary refusal that designs were also cut for size."""
    job = _job(result={"candidates": []})
    resp, _ = _submit(client, {_JID: job}, form=_as_indices([0]))
    assert resp.headers["Location"].endswith("?handoff=rejected")


def test_an_unnamed_target_still_reports_what_the_cap_discarded(client):
    """`noname` is a failure exit like the others and carries the count; `none`
    is the one exit that cannot, because both parsers count an entry as
    requested only when they also accept it, so an empty accepted list means an
    empty requested count."""
    from blueprints.lab_projects import _MAX_CANDIDATE_REFS
    cap = _MAX_CANDIDATE_REFS
    body = {"target_name": ""}
    body.update(_as_indices(range(cap + 120)))
    resp, _ = _submit(client, {_JID: _job(n=cap + 200)}, form=body)
    assert resp.headers["Location"].endswith("?handoff=noname&truncated=120")


# ---------------------------------------------------------------------------
# What this arm hands to staging and to the emails
# ---------------------------------------------------------------------------

def test_the_accepted_indices_are_what_gets_staged(client):
    """The refused ones must not reach the bucket either. Staging an index the
    row does not name would put a PDB in the folder ops opens for a design the
    campaign does not cover."""
    _, h = _submit(client, {_JID: _job(n=3)}, form=_as_indices([0, 2, 9]))
    assert len(h.staged) == 1
    assert h.staged[0]["indices"] == [0, 2]
    assert h.staged[0]["job_id"] == _JID
    assert h.staged[0]["campaign_id"] == "lab-1"
    assert len(h.staged[0]["candidates"]) == 3


def test_the_staging_call_passes_no_prefix(client):
    """Both ref arms namespace the bucket by source job, because their
    shortlists span several. This one has exactly one source job, so there is
    nothing to collide with -- and adding a prefix would split the bucket layout
    of new orders from every single-job folder ops already has open."""
    _, h = _submit(client, {_JID: _job()})
    assert "prefix" not in h.staged[0]


def test_the_staff_email_is_not_given_a_single_tool_breakdown(client):
    """``source_tools`` is a {tool: count} spread over an accepted shortlist.
    This one comes from a single job, so the row would print that job's one tool
    back at ops, and ``send_campaign_submitted_emails`` documents that this arm
    omits it."""
    _, h = _submit(client, {_JID: _job()})
    assert h.emails[0].get("source_tools") is None
