"""Customer wet-lab CRO intake routes (blueprint refactor, Commit 5).

The /lab-projects/* surface (submit / dashboard / detail / new stub),
backed by shared.campaigns -- a different subsystem from the compute
/campaigns/* product. Lifted verbatim from ``create_app()``; only
``@flask_app.route`` -> ``@lab_projects_bp.route`` and self-refs ->
``lab_projects.*``.
"""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from shared.auth import login_required
from shared.credits import load_user_context
from shared.idempotency import idempotent
from shared.jobs import candidate_count, candidate_records, read_job
from shared.storage import StorageError, stage_campaign_candidates

logger = logging.getLogger(__name__)

lab_projects_bp = Blueprint("lab_projects", __name__)


# Wet-lab campaign routes — /lab-projects/* (relabelled "Lab projects"
# in the launch cutover; the compute product now owns /campaigns/*).
# ------------------------------------------------------------------

# Hard ceiling on one submitted shortlist. Well above any real scoping request
# (the results table itself renders at most a few hundred rows), and it exists
# for the failure mode rather than the feature: each accepted ref that names an
# unseen job costs one Supabase round trip in the loops below, so an unbounded
# array is a request-amplification lever.
#
# FOUR consumers, not three. Applied at parse time, so the campaign branch and
# the target branch of /lab-projects/submit both inherit it -- and so does
# `blueprints.targets._starred_refs`, which reuses this parser for the
# starred-only CSV export. That third one arrived in the same change as this
# comment and was left out of it, which is how the export came to describe
# itself as "exact" while silently dropping the 501st starred design
# (register item A-2). The fourth is the single-job branch, which takes two
# payload shapes and reaches this bound through BOTH parsers: the ref one for
# `candidate_refs` and `_parse_candidate_indices_counted` for the bare-int
# `candidate_indices`. Anything added here must state what it does about the
# bound.
#
# WHAT THE TARGET SUBMIT DOES ABOUT IT: it takes the requested count from
# `_parse_candidate_refs_counted` and reports the overflow to the user and to
# ops as `truncated`, separately from the refs it rejected on provenance. The
# free CSV export got that disclosure and the PAID handoff did not, so a
# 620-star shortlist was announced to both parties as "500 candidates" with
# nothing anywhere naming the 120 designs the bound had already discarded, and
# its drop count -- derived from the already-truncated list -- read zero.
# Stars persist in sessionStorage and the pooled table renders 300 rows per
# view, so accumulating past 500 is an ordinary path, not an attack.
#
# ALL THREE SUBMIT BRANCHES NOW DO THAT: each takes `requested_refs` from a
# counted parse and reports the overflow. The single-job branch was the
# exception until register item A91, because its shortlist arrived as
# `candidate_indices` -- bare ints, parsed by neither function here, so neither
# capped nor counted, and a `truncated` on that arm could only have printed a
# hardcoded zero. `_parse_candidate_indices_counted` applies this bound to that
# payload and counts what it removed.
_MAX_CANDIDATE_REFS = 500


def _parse_candidate_refs(raw: str) -> list[dict]:
    """Parse the pooled shortlist payload — a JSON array of
    ``{"job_id": str, "index": int}`` — into a sanitized list. Malformed
    entries are dropped; a non-array or unparseable body yields ``[]``.

    Truncated at :data:`_MAX_CANDIDATE_REFS`, not rejected: a shortlist that
    long is not a real submission, and refusing outright would turn a bounded
    read into a silent no-op the user cannot tell from a network failure.

    Thin wrapper over :func:`_parse_candidate_refs_counted` that DISCARDS the
    requested count. Every caller that must know what the bound removed uses
    the counted form directly; this name survives as the parse-only contract
    two test modules pin (``tests/test_campaign_results.py`` and
    ``tests/test_candidate_table_js_contract.py``, which assert the sanitizer's
    accept/reject rules and nothing about the cap).
    """
    return _parse_candidate_refs_counted(raw)[0]


def _parse_candidate_refs_counted(
    raw: str, *, default_job_id: str = "",
) -> tuple[list[dict], int]:
    """``(refs, requested)`` — the sanitized list CAPPED at
    :data:`_MAX_CANDIDATE_REFS`, and how many well-formed refs the payload
    actually carried.

    ``requested`` is what makes the bound observable. ``len(refs)`` saturates
    at the cap, so a count derived from it reports ZERO drops for a shortlist
    that lost designs to truncation.

    Malformed entries are excluded from BOTH numbers: they are a client defect
    rather than a design the user chose, and counting them as dropped would
    tell a user that designs went missing when nothing they starred did.

    ``default_job_id`` supplies the job for an entry that names none. Left
    empty by every caller that has no single such job — both ref branches, and
    the starred-only export through :func:`_parse_candidate_refs`, which does
    not expose the parameter at all — because an unattributed ref there cannot
    be placed and stays malformed. The single-job branch passes its
    ``source_job_id``, because ``loadShortlist`` in
    static/js/candidate_table.js coerces a legacy bare-int store entry to
    ``{j: null, i}`` — and with no default those entries are dropped AND left
    out of ``requested``, so they would vanish from the shortlist and from the
    truncation disclosure at once.

    The loop no longer stops at the cap, because stopping is what made the
    overflow uncountable. That costs one extra pass over an array
    ``json.loads`` has already materialised in full, and it adds no Supabase
    round trips: the cap on ``refs`` -- which is what bounds those -- is
    unchanged, and the request body is bounded by the app's 20 MB
    ``MAX_CONTENT_LENGTH``.
    """
    import json  # noqa: PLC0415
    try:
        parsed = json.loads(raw or "[]")
    except Exception:
        return [], 0
    if not isinstance(parsed, list):
        return [], 0
    out: list[dict] = []
    requested = 0
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        jid = str(entry.get("job_id") or "").strip() or default_job_id
        try:
            idx = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if not jid or idx < 0:
            continue
        requested += 1
        if len(out) < _MAX_CANDIDATE_REFS:
            out.append({"job_id": jid, "index": idx})
    return out, requested


def _parse_candidate_indices_counted(
    raw: str, source_job_id: str,
) -> tuple[list[dict], int]:
    """``(refs, requested)`` for the legacy ``candidate_indices`` payload — a
    JSON array of bare ints, every one of them an index into ``source_job_id``.

    Returns the SAME ``{"job_id", "index"}`` shape
    :func:`_parse_candidate_refs_counted` returns, so the single-job branch has
    one list type to dedupe, range-check and count however the shortlist
    reached it, and so both payloads meet the same
    :data:`_MAX_CANDIDATE_REFS` bound and report the same overflow.

    Malformed entries are skipped, where the arm's previous
    ``[int(i) for i in json.loads(raw)]`` inside a bare ``except`` discarded the
    WHOLE shortlist on the first one. They are excluded from ``requested`` too,
    on the same grounds as the ref parser: a client defect is not a design the
    user chose.
    """
    import json  # noqa: PLC0415
    try:
        parsed = json.loads(raw or "[]")
    except Exception:
        return [], 0
    if not isinstance(parsed, list):
        return [], 0
    out: list[dict] = []
    requested = 0
    for entry in parsed:
        try:
            idx = int(entry)
        except (TypeError, ValueError):
            continue
        if idx < 0:
            continue
        requested += 1
        if len(out) < _MAX_CANDIDATE_REFS:
            out.append({"job_id": source_job_id, "index": idx})
    return out, requested


def _submit_campaign_shortlist(
    ctx, source_campaign_id, candidate_refs, target_name, target_context,
    assay_type, budget_band, affinity_goal_kd_nm, timeline_weeks,
    *, requested_refs,
):
    """Create a lab campaign from a shortlist spanning many sub-jobs of a
    compute campaign, then stage each shortlisted PDB. Every referenced job is
    re-checked against the caller (IDOR-safe) and must be a child of the named
    compute campaign; PDBs are namespaced by source sub-job.

    ``requested_refs`` is how many well-formed refs the POST body carried BEFORE
    :data:`_MAX_CANDIDATE_REFS` truncated it. Required rather than defaulted,
    for the reason :func:`_submit_target_shortlist` gives: a default would mean
    "nothing was truncated" on exactly the request where that is wrong.

    THE PARENTAGE TEST IS ONE EQUALITY, and that is the whole difference from
    the target arm. A design reaches THIS page by one route only -- it is a
    child of this compute campaign -- so the test is
    ``job.campaign_id == source_campaign_id``. The target arm's second clause
    (``job.target_id`` or membership of ``campaign_ids_for_target``) has no
    counterpart here and must not be copied: there is no target id in scope, and
    widening the test is the one change that would admit a foreign design.

    WHY ``read_job`` AND NOT ``get_job``. Every refusal on this route feeds two
    decisions -- whether to submit at all, and what the user is then told --
    and ``get_job`` answers ``None`` for a job that is absent, one that is not
    the caller's, and a read that never completed. Reporting a shortfall from
    that ``None`` tells a paying customer their designs are permanently
    unmatchable because Supabase blinked; refusing on it tells a user to retry a
    selection that can never work. The count and the copy downstream are only
    honest because this reads the cause.
    """
    from collections import defaultdict  # noqa: PLC0415
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.campaigns import create_campaign_from_refs  # noqa: PLC0415
    from shared.email import send_campaign_submitted_emails  # noqa: PLC0415

    detail = url_for(
        "campaigns.compute_campaign_detail", campaign_id=source_campaign_id,
    )

    # Refs the parse-time cap discarded before any check ran, counted in REFS
    # rather than designs: the tail past the bound was never parsed into pairs,
    # so a duplicate hiding in it cannot be subtracted. Computed ABOVE the
    # guards so it rides the failure exits too -- a 620-star shortlist that was
    # refused otherwise has 120 designs nobody ever mentions, on the paths where
    # the user is already being told something went wrong.
    truncated = max(0, requested_refs - len(candidate_refs))
    # `none` cannot carry it: the parser counts a ref as requested only when it
    # also emits one (up to the cap), so an empty accepted list means an empty
    # requested count.
    trunc_qs = f"&truncated={truncated}" if truncated else ""

    # Two causes, two answers. These were one silent `redirect(detail)`.
    if not candidate_refs:
        return redirect(detail + "?handoff=none")
    if not target_name:
        return redirect(detail + "?handoff=noname" + trunc_qs)
    # THE PARENT GATE, and two outcomes rather than one (register item A90).
    # `cc.get_campaign` answered None for a run that is not there, one that is
    # not the caller's, and a read that never completed, so a two-second Supabase
    # fault bounced the user to an unrelated list with no message at all -- on
    # the one action that hands work to a wet lab. `read_campaign` reports which.
    #
    # UNAVAILABLE keeps the user on THIS run's page, which is the page that
    # renders the banners, and says the submission was refused rather than
    # ignored. It rides `trunc_qs` for the reason every other failure exit here
    # does: a 620-star shortlist refused at this gate still lost 120 refs to the
    # cap, and this is a path where the user is already being told something went
    # wrong.
    #
    # THE BANNER IS NOT GUARANTEED TO ARRIVE ON THIS ARM, and that is register
    # item A94 rather than an oversight here. `compute_campaign_detail` reads
    # through the two-outcome `get_campaign`, so a fault that outlives the
    # redirect lands the user on the runs list instead of on this reason. The
    # target arm's detail route does render it, because ITS absent answer was a
    # 404 and it already paid for a template render. A94 has the round-trip
    # count that decided the difference.
    parent = cc.read_campaign(source_campaign_id, user_id=ctx.user_id)
    if parent.unavailable:
        return redirect(detail + "?handoff=unverified" + trunc_qs)
    # ABSENT, and the original silent redirect is the right answer to it: the run
    # really is gone or was never this caller's, so `detail` is not a page we can
    # send them to and there is nothing to say beyond returning them to their
    # runs.
    #
    # THE SAME THREE-WAY SHAPE AS THE TARGET ARM'S GATE BELOW, AND NOT THE SAME
    # BEHAVIOUR ON EVERY INPUT. This comment used to claim they were identical.
    # They diverge on one class of row: `read_campaign` turns the row into a
    # dataclass through `_campaign_or_none` and reports UNAVAILABLE for one it
    # cannot parse, so an unparseable row redirects politely; `read_target` calls
    # `DesignTarget.from_row` OUTSIDE its `try`, exactly where its own module's
    # `get_target` does, so an unparseable row RAISES out of the gate below and
    # 500s the submit. Each read matches its own module's `get_*` sibling, which
    # is the reason neither was changed to match the other, and each docstring
    # says so -- but the pair is not symmetric and `read_target`'s three-outcome
    # contract is therefore not total. REGISTER ITEM A97, not fixed here:
    # closing it means giving shared/targets.py a `_campaign_or_none` equivalent,
    # which changes what `get_target` and the target detail page do with such a
    # row as well.
    if parent.campaign is None:
        return redirect(url_for("jobs.jobs_list"))

    jobs_by_id: dict = {}
    # ``{job_id: candidate_count(job.result)}``, filled from the job already in
    # hand so the index check below costs no extra read. None means the result
    # shape does not state a length -- see the check itself.
    n_records: dict = {}
    # Ids already refused PERMANENTLY, and ids we simply could not read. Two
    # sets rather than one because they are two verdicts and only the second
    # implicates the database. Both short-circuit the re-read: a miss never
    # writes to ``jobs_by_id``, so without this a body naming one job 500 times
    # issues 500 identical Supabase round trips (register item A87).
    rejected: set = set()
    unreadable: set = set()
    # ``(job_id, index)`` pairs already decided, so a repeat is collapsed rather
    # than counted twice.
    seen: set = set()
    refs_by_job = defaultdict(list)
    clean_refs: list[dict] = []
    # Distinct DESIGNS the checks below refused. Not derived by subtraction from
    # the ref count: a repeat and a truncation are neither of them a refusal.
    dropped = 0
    # THE ONE FLAG THAT DECIDES WHETHER THE REFS PERMIT THIS SUBMISSION TO
    # PROCEED: set when a ref was refused for a reason WE COULD NOT ACTUALLY
    # DECIDE. Scoped to the refs because the parent gate above refuses on the
    # same grounds and to the same `?handoff=unverified`, without passing through
    # here -- it runs before any ref is read and has nothing to loop over.
    #
    # EXACTLY ONE SETTER HERE, where the target arm has two, and the second one
    # must not be copied. That arm also refuses when its campaign-id set came
    # back a prefix, because its parentage test consults a SECOND, paged read
    # (`campaign_ids_for_target`) whose completeness is in doubt. This arm has
    # no second read: `job.campaign_id` arrives on the same row as the job, in
    # the same round trip, so once `read` is not `unavailable` the parentage
    # answer is fully determined and nothing else can have narrowed it.
    unresolved = False
    for ref in candidate_refs:
        jid = ref["job_id"]
        idx = ref["index"]
        # DEDUPE FIRST, before ownership and before counting. A repeated
        # (job_id, index) names ONE physical design, so the second occurrence is
        # not a design that went missing and must not reach `dropped`, and
        # persisting it would tell ops to order the same structure twice (the
        # read-side half of this is blueprints/admin.py's `duplicates`).
        if (jid, idx) in seen:
            continue
        seen.add((jid, idx))
        if jid in rejected or jid in unreadable:
            dropped += 1
            continue
        job = jobs_by_id.get(jid)
        if job is None:
            read = read_job(jid, user_id=ctx.user_id)
            if read.unavailable:
                # NOT a rejection. We never learned anything about this job, so
                # the checks below have nothing to apply and the submission as a
                # whole is no longer decidable; see `unresolved`.
                unreadable.add(jid)
                dropped += 1
                unresolved = True
                continue
            # `absent` from here on: the read completed, so `job is None` means
            # the row is genuinely not there or is not this caller's -- a
            # permanent verdict rather than a shrug.
            job = read.job
            # Must be the caller's own job AND a child of THIS compute campaign.
            # One clause, deliberately: see the docstring.
            if job is None or job.campaign_id != source_campaign_id:
                rejected.add(jid)
                dropped += 1
                continue
            jobs_by_id[jid] = job
            n_records[jid] = candidate_count(job.result)
        # The index has to exist in the source job's results. Unvalidated, an
        # out-of-range ref is persisted, counted on the staff email and on the
        # customer's page -- and then silently skipped by
        # `stage_campaign_candidates` (shared/storage.py:218-220), so the lab
        # receives fewer PDBs than every number anyone can see.
        #
        # `candidate_count` and not `len(candidate_records(...))`: the list form
        # answers `[]` both for a job that delivered zero designs and for a
        # result shape this app cannot read. Only the second is a reason to wave
        # a ref through, and it reports None for it; a `{"candidates": []}` job
        # HAS a known length, it is zero, and every index into it is refused.
        n = n_records.get(jid)
        if n is not None and idx >= n:
            dropped += 1
            continue
        refs_by_job[jid].append(idx)
        clean_refs.append({"job_id": jid, "index": idx})

    # A REJECTION WE COULD NOT DECIDE IS NOT A VERDICT. This route stages a PAID
    # order, so proceeding on a shortlist whose refusals we cannot stand behind
    # hands the wet lab a list quietly missing designs the user selected and paid
    # to compute. Refusing costs one click: the stars live in sessionStorage and
    # survive the redirect.
    #
    # Gated on `unresolved`, NOT on `dropped`: `dropped` also counts refs naming
    # a job that provably is not there or is not the caller's, a job belonging to
    # a different run, and an index past the end of its job -- all decided by a
    # read that COMPLETED.
    #
    # ORDER IS LOAD-BEARING. This precedes the empty-`clean_refs` exit below,
    # because `rejected`'s banner says the same selection "will be refused the
    # same way", which is false for a transient fault.
    if unresolved:
        return redirect(detail + "?handoff=unverified" + trunc_qs)

    # NOT `none`. The request DID carry designs; every one failed the tenancy,
    # provenance or index check, and (past the gate above) failed it for a reason
    # we can stand behind. `none` is recoverable by retrying; this is not.
    if not clean_refs:
        return redirect(detail + "?handoff=rejected" + trunc_qs)

    try:
        lab_campaign = create_campaign_from_refs(
            user_id=ctx.user_id,
            source_campaign_id=source_campaign_id,
            candidate_refs=clean_refs,
            target_name=target_name,
            target_context=target_context,
            assay_type=assay_type,
            budget_band=budget_band,
            affinity_goal_kd_nm=affinity_goal_kd_nm,
            timeline_weeks=timeline_weeks,
        )
    # THE SILENT NO-OP THIS ARM SHIPPED WITH (register item A-8). Both returns
    # sent the user back to the page they came from with no banner, no error and
    # nothing changed, which reads as "the button does nothing" rather than "your
    # submission failed" -- on the one action that hands work to the wet lab.
    except ValueError:
        return redirect(detail + "?handoff=failed" + trunc_qs)
    if lab_campaign is None:
        return redirect(detail + "?handoff=failed" + trunc_qs)

    for jid, idxs in refs_by_job.items():
        job = jobs_by_id[jid]
        try:
            stage_campaign_candidates(
                campaign_id=lab_campaign.id,
                # candidate_records: the aggregator indexed these refs with it,
                # so a raw ["candidates"] read here stages ZERO PDBs (silently)
                # for the designs-only tools while still creating the row and
                # sending the confirmation email.
                candidates=candidate_records(job.result),
                indices=idxs,
                user_id=ctx.user_id,
                job_id=jid,
                # NO TOOL SLUG, unlike the target arm. A compute campaign has
                # exactly one tool (`compute_campaigns.tool` is NOT NULL,
                # migration 0034:36), so there is no two-tools-one-filename
                # collision to namespace against -- and changing this prefix
                # would split the bucket layout of new orders from every
                # campaign-sourced folder ops already has open.
                prefix=f"{jid[:8]}/",
            )
        except StorageError:
            logger.warning(
                "stage_campaign_candidates (campaign) failed for %s/%s",
                lab_campaign.id, jid,
            )

    try:
        send_campaign_submitted_emails(
            campaign=lab_campaign, user_email=session.get("user_email", ""),
            # NO `source_tools`. It is a {tool: count} spread over an accepted
            # shortlist, and this one spans sub-jobs of a single-tool campaign,
            # so the row would print one tool back at ops. shared/email.py's own
            # docstring states that contract; passing it here would falsify it.
            dropped=dropped,
            truncated=truncated,
        )
    except Exception:
        logger.warning("campaign submit emails failed", exc_info=True)

    # Both counts ride the query string so the confirmation page can state what
    # was NOT sent. They stay SEPARATE because they are different facts in
    # different units -- a `dropped` design was read and refused, a `truncated`
    # one was never read -- and not because they have different remedies:
    # neither has one the user can carry out, since nothing in this product
    # clears a shortlist. Each is omitted when zero, so the common case keeps
    # exactly today's URL.
    return redirect(
        url_for("lab_projects.campaign_detail", campaign_id=lab_campaign.id)
        + "?submitted=1"
        + (f"&dropped={dropped}" if dropped else "")
        + (f"&truncated={truncated}" if truncated else "")
    )


def _submit_target_shortlist(
    ctx, source_target_id, candidate_refs, target_name, target_context,
    assay_type, budget_band, affinity_goal_kd_nm, timeline_weeks,
    *, requested_refs,
):
    """Create a lab campaign from a shortlist spanning many TOOLS run against
    one design target, then stage each shortlisted PDB.

    ``requested_refs`` is how many well-formed refs the POST body carried
    BEFORE :data:`_MAX_CANDIDATE_REFS` truncated it. Required rather than
    defaulted: a default would silently mean "nothing was truncated", which is
    the exact wrong answer in the one case the argument exists for.

    Same shape as :func:`_submit_campaign_shortlist`, with the parentage test
    widened by exactly one clause, because a target's designs reach it by two
    routes: compute-campaign sub-jobs (``job.campaign_id`` in the target's
    campaign set) and target-tagged standalone jobs (``job.target_id``). Both
    are stamped by migration 0039 and both feed
    ``shared.target_results.aggregate_target_candidates``, so a shortlist that
    accepted only one of them would refuse rows the user can see and star.

    THE OWNERSHIP BOUNDARY IS THE PER-REF RE-FETCH, not the gate above it.
    ``read_job(jid, user_id=ctx.user_id)`` is what makes a ref naming another
    tenant's job come back ``absent``; the parentage test that follows is what
    stops the caller's OWN job from a different target being staged into this
    submission's folder. Neither check subsumes the other and neither may be
    dropped: the first is tenancy, the second is provenance.

    WHY ``read_job`` AND NOT ``get_job``. Every refusal on this route feeds two
    decisions -- whether to submit at all, and what the user is then told -- and
    both of those are answers to "why", not to "how many". ``get_job`` returns
    ``None`` for a job that is not there, for one that is not yours, and for a
    read that never completed, so a caller holding that ``None`` has to GUESS
    which, and the two available guesses are each catastrophic in the other's
    case: treat a real rejection as transient and the user is told to retry a
    selection that can never work, treat a transient fault as a verdict and a
    two-second database hiccup tells a paying customer their designs are
    permanently unmatched. ``read_job`` reports the cause, so nothing below has
    to guess and no sentence downstream has to hedge.
    """
    from collections import defaultdict  # noqa: PLC0415
    from shared.campaigns import create_campaign_from_target_refs  # noqa: PLC0415
    from shared.email import send_campaign_submitted_emails  # noqa: PLC0415
    from shared.targets import campaign_ids_for_target, read_target  # noqa: PLC0415

    detail = url_for("targets.target_detail", target_id=source_target_id)

    # Refs the parse-time cap discarded before any check ran. Counted in REFS,
    # not designs: the tail past the bound was never parsed into pairs, so a
    # duplicate hiding in it cannot be subtracted. Every sentence that renders
    # this number AS DESIGNS therefore says "up to" (the customer email, the
    # confirmation banner, the target page); the staff email prints it under
    # "refs" instead and needs no hedge, which is the whole reason the two read
    # differently. Over-stating is the harmless direction, the same trade
    # `_starred_refs` makes for its own flag.
    #
    # COMPUTED HERE, ABOVE THE GUARDS, so it can ride EVERY exit rather than
    # only the successful one. It used to be derived after the loop, which put
    # it out of reach of the three failure exits -- so a 620-star shortlist that
    # was refused had 120 designs nobody ever mentioned, on precisely the paths
    # where the user is already being told something went wrong.
    truncated = max(0, requested_refs - len(candidate_refs))
    # Appended to the failure redirects below. `none` cannot carry it: the
    # parser counts a ref as requested only when it also emits one (up to the
    # cap), so an empty accepted list means an empty requested count.
    trunc_qs = f"&truncated={truncated}" if truncated else ""

    # Two causes, two answers. `candidate_refs` empty is the observable of
    # every client-side way the star selection can fail to reach us (register
    # item B-3), and telling that user "name your target" would be a lie.
    if not candidate_refs:
        return redirect(detail + "?handoff=none")
    if not target_name:
        return redirect(detail + "?handoff=noname" + trunc_qs)
    # THE PARENT GATE, and two outcomes rather than one (register item A90); see
    # the pair in `_submit_campaign_shortlist` for the full reasoning, and for
    # the one input class on which the two gates do NOT behave alike.
    # UNAVAILABLE keeps the user on THIS target's page with a reason and carries
    # `trunc_qs` like every other failure exit here; ABSENT keeps the original
    # silent return to the targets list, because that row really is gone or was
    # never this caller's, so `detail` is not a page we can send them to.
    parent = read_target(source_target_id, user_id=ctx.user_id)
    if parent.unavailable:
        return redirect(detail + "?handoff=unverified" + trunc_qs)
    if parent.target is None:
        return redirect(url_for("targets.targets_list"))

    # Read ONCE, before the loop. Owner-scoped and paged; membership is all
    # this needs, so its documented arbitrary order does not matter. Fetching
    # it per ref would issue one paged read per shortlisted design.
    #
    # `complete` is load-bearing rather than diagnostic; see the refusal below.
    campaign_id_list, campaign_ids_complete = campaign_ids_for_target(
        source_target_id, user_id=ctx.user_id,
    )
    target_campaign_ids = set(campaign_id_list)

    jobs_by_id: dict = {}
    # ``{job_id: candidate_count(job.result)}``, filled from the job already in
    # hand so the index check below costs no extra read. The value is None when
    # the result shape does not state a length -- see the check itself.
    n_records: dict = {}
    # Ids already refused PERMANENTLY, and ids we simply could not read. Two
    # sets rather than one because they are two verdicts, and only the second
    # implicates the database. Both short-circuit the re-read: without that, a
    # body naming the same job 500 times issues 500 identical Supabase round
    # trips, since a miss never writes to ``jobs_by_id`` and so is never a cache
    # hit.
    rejected: set = set()
    unreadable: set = set()
    # ``(job_id, index)`` pairs already decided, so a repeat is collapsed
    # rather than counted twice. See the dedupe note in the loop.
    seen: set = set()
    refs_by_job = defaultdict(list)
    clean_refs: list[dict] = []
    # Distinct DESIGNS the checks below refused. Not derived by subtraction
    # from the ref count: a repeat and a truncation are neither of them a
    # refusal, and folding all three into one number is what made the previous
    # count unable to say anything true about any of them.
    dropped = 0
    # THE ONE FLAG THAT DECIDES WHETHER THE REFS PERMIT THIS SUBMISSION TO
    # PROCEED, and the whole reason the reads above report causes instead of
    # emptiness: set when at least one ref was refused for a reason WE COULD NOT
    # ACTUALLY DECIDE. Scoped to the refs because the parent gate above refuses
    # on the same grounds and to the same `?handoff=unverified`, without passing
    # through here -- it runs before any ref is read and has nothing to loop
    # over. Exactly two things set it, and they are independent faults that a
    # degraded Supabase produces together:
    #
    #   1. ``read_job`` came back ``unavailable`` -- no service client, or the
    #      query raised. The job may be perfectly valid; we never looked.
    #   2. The campaign arm refused a job whose only possible provenance was the
    #      campaign id set, and that set came back short.
    #
    # Round 20 tracked only (2), and (2) requires the job to have been READ.
    # So under CORRELATED failure -- one degraded Supabase truncating the id
    # read AND timing out the job read in the same request -- a rejection caused
    # by the timed-out read set nothing at all, and wherever that was the only
    # rejection the guard was skipped and a half-size paid wet-lab order
    # shipped. Anything added to this loop that can refuse a ref for a reason
    # the database caused must set this.
    unresolved = False
    for ref in candidate_refs:
        jid = ref["job_id"]
        idx = ref["index"]
        # DEDUPE FIRST, before ownership and before counting. A repeated
        # (job_id, index) names ONE physical design, so the second occurrence
        # is not a design that went missing and must not reach `dropped`, and
        # persisting it would tell ops to order the same structure twice
        # (the read-side half of this is blueprints/admin.py's `duplicates`).
        #
        # HERE AND NOT IN THE PARSER. The parser's other consumer,
        # `blueprints.targets._starred_refs`, needs the ref count rather than
        # the design count -- its export filename says `first{kept}of{
        # requested}` and both of those are ref counts by design. Deduping
        # upstream would silently redefine the 500 bound as 500 distinct
        # designs and make that filename report a number it does not mean.
        if (jid, idx) in seen:
            continue
        seen.add((jid, idx))
        if jid in rejected or jid in unreadable:
            dropped += 1
            continue
        job = jobs_by_id.get(jid)
        if job is None:
            read = read_job(jid, user_id=ctx.user_id)
            if read.unavailable:
                # NOT a rejection. We never learned anything about this job, so
                # the checks below have nothing to apply and the submission as a
                # whole is no longer decidable; see `unresolved`.
                unreadable.add(jid)
                dropped += 1
                unresolved = True
                continue
            # `absent` from here on: the read completed. `job is None` now means
            # the row is genuinely not there or is not this caller's, which is
            # a permanent verdict rather than a shrug.
            job = read.job
            # Must be the caller's own job AND attached to THIS target, by
            # either route. `job.campaign_id in <set>` is checked only when the
            # job carries one: a standalone job's campaign_id is None, and None
            # is not in the set, but spelling that out keeps the two routes
            # legible as two routes.
            owned_by_target = job is not None and (
                job.target_id == source_target_id
                or (
                    job.campaign_id is not None
                    and job.campaign_id in target_campaign_ids
                )
            )
            if not owned_by_target:
                rejected.add(jid)
                dropped += 1
                # A rejection the campaign id set could have decided the other
                # way, under a set we know is a prefix of the real one. The job
                # exists, is the caller's own, and its only possible provenance
                # here is membership of that set.
                if (
                    job is not None
                    and job.campaign_id is not None
                    and not campaign_ids_complete
                ):
                    unresolved = True
                continue
            jobs_by_id[jid] = job
            n_records[jid] = candidate_count(job.result)
        # The index has to exist in the source job's results. Unvalidated, an
        # out-of-range ref is persisted, counted on the staff email, counted on
        # the customer's page -- and then silently skipped by
        # `stage_campaign_candidates`, so the lab receives fewer PDBs than
        # every number anyone can see.
        #
        # `candidate_count` and not `len(candidate_records(...))`, for the same
        # reason this function reads jobs through `read_job`: the list form
        # answers `[]` both for a job that delivered zero designs and for a
        # result shape this app cannot read, so a length taken from it cannot
        # tell "no designs" from "length unknown". It reports None for the
        # second, and only the second is a reason to wave a ref through -- a
        # `{"candidates": []}` job HAS a known length, it is zero, and every
        # index into it is out of range. Under the old `if n and ...` spelling
        # those refs were all accepted, recorded on the row, counted to ops and
        # to the customer, and then staged zero PDBs.
        n = n_records.get(jid)
        if n is not None and idx >= n:
            dropped += 1
            continue
        refs_by_job[jid].append(idx)
        clean_refs.append({"job_id": jid, "index": idx})

    # A REJECTION WE COULD NOT DECIDE IS NOT A VERDICT. This route stages a PAID
    # order, so proceeding on a shortlist whose refusals we cannot stand behind
    # would hand the wet lab a list quietly missing designs the user selected
    # and paid to compute. Refusing is recoverable in one click: the stars live
    # in sessionStorage and survive the redirect.
    #
    # Gated on `unresolved`, NOT on `dropped`. `dropped` also counts refs naming
    # a job that provably is not there or is not the caller's, a job whose own
    # target is a different protein, and an index past the end of its job --
    # every one of them decided by a read that COMPLETED, so refusing on those
    # would blame the database for a verdict it made correctly and send the user
    # away from a submission that was right.
    #
    # This gate is also what lets every sentence downstream stop hedging. Past
    # it, no surviving rejection has a transient cause, so "starring them again
    # will be refused the same way" -- on the confirmation page, on the target
    # page and in both emails -- is true rather than a guess that is wrong every
    # time the database blinks.
    if unresolved:
        return redirect(detail + "?handoff=unverified" + trunc_qs)

    # NOT `none`. The request DID carry designs; every one of them failed the
    # tenancy or provenance or index check, and (having passed the gate above)
    # failed it for a reason we can stand behind. The two cases share nothing
    # but their outcome: `none` is recoverable by retrying (the stars are still
    # in sessionStorage and the likely cause is that they did not reach us),
    # `rejected` is not, because the same refs will be refused the same way.
    # Round 19 collapsed both onto `none` and so told this user to keep
    # pressing a button that can never work.
    if not clean_refs:
        return redirect(detail + "?handoff=rejected" + trunc_qs)

    try:
        lab_campaign = create_campaign_from_target_refs(
            user_id=ctx.user_id,
            source_target_id=source_target_id,
            candidate_refs=clean_refs,
            target_name=target_name,
            target_context=target_context,
            assay_type=assay_type,
            budget_band=budget_band,
            affinity_goal_kd_nm=affinity_goal_kd_nm,
            timeline_weeks=timeline_weeks,
        )
    # THE SILENT NO-OP THIS ROUTE SHIPPED WITH (register item A-8). Both arms
    # land here whenever the lab project cannot be created, and the likeliest
    # of those is entirely predictable: until migration 0040 is applied the
    # insert violates 0037's `submission_source` CHECK, the exception is
    # swallowed below the call, and this returns None. The user was then sent
    # back to the page they came from with no banner, no error and nothing
    # changed, which reads as "the button does nothing" rather than "your
    # submission failed" -- on the one action that hands work to the wet lab.
    #
    # BOTH ref arms now route this pair to `?handoff=failed` on their own
    # parent's page. `_submit_campaign_shortlist` above had a byte-identical
    # pair of bare redirects and was fixed with this one (register item A88);
    # the legacy single-job arm below still redirects to /jobs and is filed.
    except ValueError:
        return redirect(detail + "?handoff=failed" + trunc_qs)
    if lab_campaign is None:
        return redirect(detail + "?handoff=failed" + trunc_qs)

    for jid, idxs in refs_by_job.items():
        job = jobs_by_id[jid]
        try:
            stage_campaign_candidates(
                campaign_id=lab_campaign.id,
                candidates=candidate_records(job.result),
                indices=idxs,
                user_id=ctx.user_id,
                job_id=jid,
                # THE TOOL SLUG IS LOAD-BEARING IN THIS PREFIX, unlike the
                # campaign branch's bare job8. A target pools many tools and
                # they all name their first design `design_1.pdb`, so two jobs
                # whose ids happen to share a leading 8 hex digits would write
                # one object and the lab would silently receive one structure
                # for two shortlisted designs. Cheap insurance, and it also
                # makes the bucket readable: `bindcraft-01c3b3a6/`.
                prefix=f"{job.tool}-{jid[:8]}/",
            )
        except StorageError:
            logger.warning(
                "stage_campaign_candidates (target) failed for %s/%s",
                lab_campaign.id, jid,
            )

    try:
        send_campaign_submitted_emails(
            campaign=lab_campaign, user_email=session.get("user_email", ""),
            source_tools=_source_tool_counts(jobs_by_id, refs_by_job),
            dropped=dropped,
            truncated=truncated,
        )
    except Exception:
        logger.warning("campaign submit emails failed", exc_info=True)

    # Both counts ride the query string so the confirmation page can state what
    # was NOT sent. Without them the page reports the accepted count with
    # nothing to compare it against, and a user who starred ten designs reads
    # "7" as the number they chose (register item A-7).
    #
    # They stay SEPARATE because they are different facts, not because they have
    # different remedies -- rounds 19 and 20 said the second thing and it was
    # never true. A `dropped` design was read and refused; a `truncated` one was
    # never read. Neither has a remedy the user can carry out: nothing in this
    # product clears a shortlist, so "send a second request" re-posts the same
    # refs again (see the note on the truncation copy in shared/email.py).
    # Each is omitted when zero, so the common case keeps exactly today's URL.
    return redirect(
        url_for("lab_projects.campaign_detail", campaign_id=lab_campaign.id)
        + "?submitted=1"
        + (f"&dropped={dropped}" if dropped else "")
        + (f"&truncated={truncated}" if truncated else "")
    )


def _source_tool_counts(jobs_by_id: dict, refs_by_job: dict) -> dict:
    """``{tool_slug: design_count}`` over the ACCEPTED refs.

    Counted from ``refs_by_job`` rather than from ``jobs_by_id``, because the
    question the email answers is "how many designs came from each tool", not
    "how many jobs contributed". One bindcraft job contributing four starred
    designs is ``{"bindcraft": 4}``.
    """
    counts: dict = {}
    for jid, idxs in refs_by_job.items():
        job = jobs_by_id.get(jid)
        slug = str(getattr(job, "tool", "") or "unknown")
        counts[slug] = counts.get(slug, 0) + len(idxs)
    return counts


def _submit_job_shortlist(
    ctx, source_job_id, candidate_refs, target_name, target_context,
    assay_type, budget_band, affinity_goal_kd_nm, timeline_weeks,
    *, requested_refs,
):
    """Create a lab campaign from a shortlist of designs in ONE job, then stage
    each shortlisted PDB. Register item A91.

    ``candidate_refs`` is the same ``{"job_id", "index"}`` list the two ref arms
    take, whichever of the two payloads this arm's page posted; the dispatcher
    normalises both into it. ``requested_refs`` is how many well-formed entries
    the POST body carried BEFORE :data:`_MAX_CANDIDATE_REFS` truncated it,
    required rather than defaulted for the reason the ref arms give: a default
    would mean "nothing was truncated" on exactly the request where that is
    wrong.

    THE PARENTAGE TEST IS ``ref["job_id"] == source_job_id``, and it is not
    hardening that could be dropped for brevity. This arm persists BARE INDICES
    against one ``source_job_id`` — ``lab_campaigns.candidate_indices``, whose
    VALUES no CHECK constrains (0011 and 0037 bound only its emptiness) — so an
    accepted ref naming another job would be stored as an index into a job the
    row does not name, and staged out of this job's results.

    WHY ``read_job`` AND NOT ``get_job``. The parent here IS the job, so one
    read answers tenancy, existence and the index bound at once — and it is the
    only per-design refusal on this arm the database can cause. ``get_job``
    returns
    ``None`` for a job that is absent, one that is not the caller's, and a read
    that never completed, and this arm sent all three to /jobs with nothing
    said. It also cannot support a ``dropped`` count:
    ``send_campaign_submitted_emails`` documents that the count may be called a
    rejection only because its callers refuse outright when a refusal had an
    undecidable cause, and that refusal is undecidable from ``None``.

    NO ``candidate_refs`` COLUMN IS WRITTEN ON THIS ROW, and the refs above are
    not persisted in that shape. ``blueprints.admin._ref_shortlist_view``
    switches on that column being non-empty, so filling it would move the
    fulfilment page of every single-job order off the index list ops reads
    today (``tests/test_admin_shortlist_fulfilment.py`` calls that "the arm that
    must NOT change"). ``create_campaign`` writes ``candidate_indices`` only.
    """
    from shared.campaigns import create_campaign  # noqa: PLC0415
    from shared.email import send_campaign_submitted_emails  # noqa: PLC0415

    detail = url_for("jobs.job_detail", job_id=source_job_id)

    # Entries the parse-time cap discarded before any check ran, counted in
    # ENTRIES rather than designs: the tail past the bound was never parsed into
    # pairs, so a duplicate hiding in it cannot be subtracted. Computed ABOVE
    # the guards so it rides the failure exits too, exactly as in the two ref
    # arms.
    truncated = max(0, requested_refs - len(candidate_refs))
    # `none` cannot carry it: both parsers count an entry as requested only when
    # they also emit one (up to the cap), so an empty accepted list means an
    # empty requested count.
    trunc_qs = f"&truncated={truncated}" if truncated else ""

    # Two causes, two answers. These were one guard collapsing three causes into
    # a single silent redirect to /jobs, so a user who left the target name
    # blank and a user whose selection never reached us were told the same
    # nothing on an unrelated page. The third cause -- no parent id at all -- is
    # answered by the dispatcher, because it is the only one of the three with
    # no page to return to.
    if not candidate_refs:
        return redirect(detail + "?handoff=none")
    if not target_name:
        return redirect(detail + "?handoff=noname" + trunc_qs)

    # THE PARENT GATE, and three outcomes where the whole arm used to have one.
    #
    # ORDER IS LOAD-BEARING, for the reason `_submit_campaign_shortlist` records
    # at its own refusal gate: `rejected`'s banner promises the same selection
    # "will be refused the same way", which is false for a transient fault. So
    # the undecidable outcome is answered HERE, above the loop and above the
    # `rejected` exit under it.
    #
    # THERE IS NO PER-REF `unresolved` FLAG on this arm, and it needs none. The
    # ref arms read a job per ref inside their loops, which is where a fault can
    # strike mid-shortlist; this arm reads its single job once, here. Past this
    # gate every refusal below is decided from the request (a ref naming another
    # job) or from the result row already in hand (an index past its end), so no
    # refusal the database caused can reach the `rejected` exit. Anything added
    # to the loop that CAN be refused for a reason the database caused breaks
    # that and needs the ref arms' flag.
    read = read_job(source_job_id, user_id=ctx.user_id)
    if read.unavailable:
        return redirect(detail + "?handoff=unverified" + trunc_qs)
    # ABSENT: the read completed, so the row is genuinely not there or is not
    # this caller's. `detail` is not a page we can send them to, and the
    # original silent redirect to their jobs is the right answer -- the same one
    # both ref arms give an absent parent.
    if read.job is None:
        return redirect(url_for("jobs.jobs_list"))
    job = read.job

    # `candidate_count` and not `len(candidate_records(...))`: the list form
    # answers `[]` both for a job that delivered zero designs and for a result
    # shape this app cannot read, so a length taken from it cannot tell "no
    # designs" from "length unknown". Only the second is a reason to wave an
    # index through, and it reports None for it.
    n_records = candidate_count(job.result)

    # `(job_id, index)` pairs already decided, so a repeat is collapsed rather
    # than counted twice.
    seen: set = set()
    indices: list[int] = []
    # Distinct DESIGNS the checks below refused. Not derived by subtraction from
    # the entry count: a repeat and a truncation are neither of them a refusal.
    dropped = 0
    for ref in candidate_refs:
        jid = ref["job_id"]
        idx = ref["index"]
        # DEDUPE FIRST, before the checks and before counting. A repeated
        # (job_id, index) names ONE physical design, so the second occurrence is
        # not a design that went missing and must not reach `dropped`, and
        # persisting it would tell ops to order the same structure twice.
        if (jid, idx) in seen:
            continue
        seen.add((jid, idx))
        if jid != source_job_id:
            dropped += 1
            continue
        # The index has to exist in this job's results. Unvalidated, an
        # out-of-range index is persisted, counted on the staff email and on the
        # customer's page -- and then silently skipped by
        # `stage_campaign_candidates` (shared/storage.py:218-220), so the lab
        # receives fewer PDBs than every number anyone can see. This arm ran no
        # per-index check at all before A91.
        if n_records is not None and idx >= n_records:
            dropped += 1
            continue
        indices.append(idx)

    # NOT `none`. The request DID carry designs; every one of them failed the
    # parentage or the index check, and both were decided by a read that
    # completed. `none` is recoverable by retrying; this is not, because the
    # same shortlist will be refused identically.
    if not indices:
        return redirect(detail + "?handoff=rejected" + trunc_qs)

    try:
        campaign = create_campaign(
            user_id=ctx.user_id,
            source_job_id=source_job_id,
            candidate_indices=indices,
            target_name=target_name,
            target_context=target_context,
            assay_type=assay_type,
            budget_band=budget_band,
            affinity_goal_kd_nm=affinity_goal_kd_nm,
            timeline_weeks=timeline_weeks,
        )
    # THE SILENT NO-OP THIS ARM SHIPPED WITH (register item A-8, fixed on the
    # two ref arms first). Both returns sent the user to /jobs with no banner,
    # no error and nothing changed, which reads as "the button does nothing"
    # rather than "your submission failed".
    except ValueError:
        return redirect(detail + "?handoff=failed" + trunc_qs)
    if campaign is None:
        return redirect(detail + "?handoff=failed" + trunc_qs)

    # Copy candidate PDBs into durable campaign bucket.
    try:
        stage_campaign_candidates(
            campaign_id=campaign.id,
            # candidate_records, not a raw ["candidates"] read: a cofold/design
            # result keys its per-candidate array under `designs` and carries no
            # `candidates` key at all, so a raw read stages ZERO PDBs for those
            # tools, silently, while still creating the row and sending the
            # confirmation email.
            candidates=candidate_records(job.result),
            indices=indices,
            user_id=ctx.user_id,
            job_id=source_job_id,
            # NO `prefix`, unlike both ref arms. This shortlist has exactly one
            # source job, so there is no second job to collide with inside the
            # campaign's folder, and adding one would split the bucket layout of
            # new orders from every single-job folder ops already has open.
        )
    except StorageError:
        logger.warning("stage_campaign_candidates failed for %s", campaign.id)

    # Emails — best-effort.
    try:
        send_campaign_submitted_emails(
            campaign=campaign,
            user_email=session.get("user_email", ""),
            # NO `source_tools`. It is a {tool: count} spread over an accepted
            # shortlist, and this one comes from a single job, so the row would
            # print that job's one tool back at ops. shared/email.py's docstring
            # states that contract for this arm.
            dropped=dropped,
            truncated=truncated,
        )
    except Exception:
        logger.warning("campaign submit emails failed", exc_info=True)

    # Both counts ride the query string so the confirmation page can state what
    # was NOT sent, and they stay separate because they are different facts in
    # different units -- a `dropped` design was read and refused, a `truncated`
    # one was never read. Each is omitted when zero, so the common case keeps
    # exactly today's URL.
    return redirect(
        url_for("lab_projects.campaign_detail", campaign_id=campaign.id)
        + "?submitted=1"
        + (f"&dropped={dropped}" if dropped else "")
        + (f"&truncated={truncated}" if truncated else "")
    )


@lab_projects_bp.route("/lab-projects/submit", methods=["POST"])
@login_required
@idempotent()
def campaigns_submit():
    """Create a lab project from a shortlist. Three parent shapes, one route.

    ``@idempotent()`` is NOT applied blanket-wide, so this is a judgment about
    this route rather than a house style being completed: 10 of the app's 28
    POST routes carry it, this one included, while `wallet.wallet_checkout`,
    `targets.target_archive` and the admin status writes do without. The other
    nine are the run and target lifecycle (`campaigns.compute_campaign_create`,
    `campaigns.compute_campaign_refold`, `jobs.job_refold`, `jobs.job_cancel`,
    `targets.target_create`, `targets.target_launch_submit`, `tools.tool_submit`)
    plus the two synchronous compute tools (`tools.developability_score`,
    `tools.library_planner_plan`). What they share is that a replay costs real
    money or real work; this one creates a lab project and stages PDBs into a
    folder Ranomics staff open, which puts it in the same class. The key is
    (user, path, exact body), so two genuinely different scoping requests are
    unaffected and only a REPLAY of the identical body is collapsed.

    It is here because the shortlist copy used to tell a user whose selection
    overflowed the cap to "submit a second request", and the shortlist is never
    cleared -- so following that advice re-posted byte-identical refs and opened
    a SECOND lab_campaigns row for designs already ordered, which ops would have
    had to reconcile by hand. That copy is now gone, and this stops the same
    thing happening to anyone who simply double-clicks a slow submit.

    WHAT IT DOES NOT DO, stated because the withdrawn copy depended on it: the
    TTL is 60 seconds, so this is a double-submit guard and NOT a promise that
    the same shortlist can never be ordered twice. No sentence anywhere may
    claim that.
    """
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    target_name    = request.form.get("target_name", "").strip()
    assay_type     = request.form.get("assay_type", "yeast_display").strip()
    budget_band    = request.form.get("budget_band", "pilot").strip()
    target_context = request.form.get("target_context", "").strip()

    raw_kd = request.form.get("affinity_goal_kd_nm", "").strip()
    try:
        affinity_goal_kd_nm = float(raw_kd) if raw_kd else None
    except ValueError:
        affinity_goal_kd_nm = None

    raw_weeks = request.form.get("timeline_weeks", "").strip()
    try:
        timeline_weeks = int(raw_weeks) if raw_weeks else None
    except ValueError:
        timeline_weeks = None

    # Three shortlist shapes, most specific first. The target branch is tried
    # BEFORE the campaign one: the candidate_table macro emits exactly one
    # parent field per render, but ordering it this way means a body carrying
    # both cannot use the narrower parentage test to smuggle in a job the
    # target branch would have rejected. The single-job form is last.
    source_target_id = request.form.get("source_target_id", "").strip()
    # The counted form, because both ref branches below report what the
    # parse-time cap removed. `requested_refs` is the well-formed ref count
    # BEFORE truncation; `candidate_refs` is capped at _MAX_CANDIDATE_REFS, so
    # its length cannot tell the two apart.
    candidate_refs, requested_refs = _parse_candidate_refs_counted(
        request.form.get("candidate_refs", ""),
    )
    # BOTH ref branches are gated on the parent ALONE, not on
    # `and candidate_refs`. The shortlist bar is one macro
    # (templates/components/candidate_table.html, the .cand-shortlist-bar
    # block) and its button carries no `disabled` attribute in ANY scope, while
    # openCampaignModal (static/js/candidate_table.js) has no zero-star guard --
    # so an empty body is reachable from a target page and from a
    # compute-campaign page alike. Gated on `and candidate_refs` it falls
    # through both ref branches to the legacy single-job one, which finds no
    # source_job_id and redirects to /jobs: a user who clicked "Send shortlist"
    # lands on an unrelated list. Each ref branch's own empty guard returns them
    # to the page they came from and says why. The earlier version of this
    # comment claimed the campaign arm's empty case was "not newly reachable";
    # it was, from the same change.
    if source_target_id:
        return _submit_target_shortlist(
            ctx, source_target_id, candidate_refs, target_name,
            target_context, assay_type, budget_band, affinity_goal_kd_nm,
            timeline_weeks, requested_refs=requested_refs,
        )

    source_campaign_id = request.form.get("source_campaign_id", "").strip()
    if source_campaign_id:
        return _submit_campaign_shortlist(
            ctx, source_campaign_id, candidate_refs, target_name,
            target_context, assay_type, budget_band, affinity_goal_kd_nm,
            timeline_weeks, requested_refs=requested_refs,
        )

    # -- Single-job shortlist ---------------------------------------------
    source_job_id = request.form.get("source_job_id", "").strip()
    if not source_job_id:
        # No parent of any of the three shapes, and the only silent exit left on
        # this route: without a parent id there is no page to return this user
        # to and nothing to say on it.
        return redirect(url_for("jobs.jobs_list"))

    # TWO PAYLOAD SHAPES ON THIS ONE ARM, FOR ONE RELEASE. They are two FIELD
    # NAMES rather than one field with two shapes: this arm's page has always
    # posted `candidate_indices` (bare ints), and the candidate_table macro's
    # job branch now posts `candidate_refs` beside it, so a page served before
    # this deploy and submitted after it still carries a shortlist. Refs win
    # when they parse to anything, because that is the shape the other two arms
    # take and the one that survives dropping the legacy field.
    #
    # PARSED AGAIN HERE rather than reusing the dispatcher's copy above, because
    # `default_job_id` is an ASSUMPTION -- "every unattributed entry belongs to
    # this one job" -- and it is only true where a single source job is known,
    # which is this branch alone.
    #
    # NOT because folding it upward would leak a foreign job into a ref arm. An
    # earlier version of this comment said it would; it does not. A body
    # carrying `source_job_id` beside `source_target_id` dispatches to the
    # target arm, which re-reads every job a ref names through
    # `read_job(jid, user_id=ctx.user_id)` and then applies `owned_by_target`,
    # so a job that is not the caller's, or is attached to neither that target
    # nor any of its campaigns, is refused there however it came to be named.
    #
    # What the separate parse buys is that each ref arm sees the refs the client
    # sent and nothing this dispatcher added. Folded, an unattributed entry
    # would enter BOTH `candidate_refs` and `requested_refs` on an arm that
    # cannot place it -- changing that arm's shortlist and its `truncated`
    # arithmetic on the strength of a field its own dispatch never consults --
    # and its correctness would then rest wholly on the downstream check rather
    # than on an input nobody rewrote. Defence in depth, and it costs one more
    # pass over an array `json.loads` has already materialised in full.
    job_refs, requested_job_refs = _parse_candidate_refs_counted(
        request.form.get("candidate_refs", ""), default_job_id=source_job_id,
    )
    if not job_refs:
        job_refs, requested_job_refs = _parse_candidate_indices_counted(
            request.form.get("candidate_indices", "[]").strip(), source_job_id,
        )
    return _submit_job_shortlist(
        ctx, source_job_id, job_refs, target_name, target_context, assay_type,
        budget_band, affinity_goal_kd_nm, timeline_weeks,
        requested_refs=requested_job_refs,
    )

@lab_projects_bp.route("/lab-projects", methods=["GET"])
@login_required
def campaigns_dashboard():
    from shared.campaigns import list_user_campaigns  # noqa: PLC0415
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    campaigns = list_user_campaigns(ctx.user_id)
    return render_template("campaigns/dashboard.html", campaigns=campaigns)

@lab_projects_bp.route("/lab-projects/<campaign_id>", methods=["GET"])
@login_required
def campaign_detail(campaign_id: str):
    from shared.campaigns import get_campaign  # noqa: PLC0415
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    campaign = get_campaign(campaign_id, user_id=ctx.user_id)
    if campaign is None:
        return render_template("404.html"), 404
    submitted_flash = request.args.get("submitted") == "1"
    # How many starred designs the submit REJECTED, and how many the
    # per-request cap discarded before it ever looked at them. Two counts and
    # not one because they are different facts in different units -- a rejected
    # design was read and refused, a truncated one was never read -- and NOT
    # because they have different remedies: neither has a remedy the user can
    # carry out, since nothing in this product clears a shortlist, so a second
    # request re-posts the identical first 500 refs. (That withdrawn retry
    # advice created duplicate paid orders; see the note in shared/email.py.)
    # Both ref branches send them, and only when non-zero. Clamped rather than
    # validated: these are display counts on a page the user already owns, so
    # a crafted value misinforms nobody but its author.
    try:
        dropped_count = max(0, int(request.args.get("dropped") or 0))
    except ValueError:
        dropped_count = 0
    try:
        truncated_count = max(0, int(request.args.get("truncated") or 0))
    except ValueError:
        truncated_count = 0
    return render_template(
        "campaigns/detail.html",
        campaign=campaign,
        submitted_flash=submitted_flash,
        dropped_count=dropped_count,
        truncated_count=truncated_count,
    )

# Legacy stub redirect — old results pages linked here.
@lab_projects_bp.route("/lab-projects/new", methods=["GET"])
@login_required
def campaigns_new_stub():
    from_job = request.args.get("from_job", "")
    if from_job:
        return redirect(url_for("jobs.job_detail", job_id=from_job))
    return redirect(url_for("lab_projects.campaigns_dashboard"))
