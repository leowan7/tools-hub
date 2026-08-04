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
from shared.jobs import candidate_records, get_job
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
# THREE consumers, not two. Applied at parse time, so the campaign branch and
# the target branch of /lab-projects/submit both inherit it -- and so does
# `blueprints.targets._starred_refs`, which reuses this parser for the
# starred-only CSV export. That third one arrived in the same change as this
# comment and was left out of it, which is how the export came to describe
# itself as "exact" while silently dropping the 501st starred design
# (register item A-2). Anything added here must state what it does about the
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
# The CAMPAIGN branch still has no equivalent; filed as A88.
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


def _parse_candidate_refs_counted(raw: str) -> tuple[list[dict], int]:
    """``(refs, requested)`` — the sanitized list CAPPED at
    :data:`_MAX_CANDIDATE_REFS`, and how many well-formed refs the payload
    actually carried.

    ``requested`` is what makes the bound observable. ``len(refs)`` saturates
    at the cap, so a count derived from it reports ZERO drops for a shortlist
    that lost designs to truncation.

    Malformed entries are excluded from BOTH numbers: they are a client defect
    rather than a design the user chose, and counting them as dropped would
    tell a user that designs went missing when nothing they starred did.

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
        jid = str(entry.get("job_id") or "").strip()
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


def _submit_campaign_shortlist(
    ctx, source_campaign_id, candidate_refs, target_name, target_context,
    assay_type, budget_band, affinity_goal_kd_nm, timeline_weeks,
):
    """Create a lab campaign from a shortlist spanning many sub-jobs of a
    compute campaign, then stage each shortlisted PDB. Every referenced job is
    re-checked against the caller (IDOR-safe) and must be a child of the named
    compute campaign; PDBs are namespaced by source sub-job."""
    from collections import defaultdict  # noqa: PLC0415
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.campaigns import create_campaign_from_refs  # noqa: PLC0415
    from shared.email import send_campaign_submitted_emails  # noqa: PLC0415

    detail = url_for(
        "campaigns.compute_campaign_detail", campaign_id=source_campaign_id,
    )
    if not target_name or not candidate_refs:
        return redirect(detail)
    if cc.get_campaign(source_campaign_id, user_id=ctx.user_id) is None:
        return redirect(url_for("jobs.jobs_list"))

    jobs_by_id: dict = {}
    refs_by_job = defaultdict(list)
    clean_refs: list[dict] = []
    for ref in candidate_refs:
        jid = ref["job_id"]
        idx = ref["index"]
        job = jobs_by_id.get(jid)
        if job is None:
            job = get_job(jid, user_id=ctx.user_id)
            # Must be the caller's own job AND a child of this campaign.
            if job is None or job.campaign_id != source_campaign_id:
                continue
            jobs_by_id[jid] = job
        refs_by_job[jid].append(idx)
        clean_refs.append({"job_id": jid, "index": idx})

    if not clean_refs:
        return redirect(detail)

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
    except ValueError:
        return redirect(detail)
    if lab_campaign is None:
        return redirect(detail)

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
        )
    except Exception:
        logger.warning("campaign submit emails failed", exc_info=True)

    return redirect(
        url_for("lab_projects.campaign_detail", campaign_id=lab_campaign.id)
        + "?submitted=1"
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
    ``get_job(jid, user_id=ctx.user_id)`` is what makes a ref naming another
    tenant's job return None; the parentage test that follows is what stops the
    caller's OWN job from a different target being staged into this
    submission's folder. Neither check subsumes the other and neither may be
    dropped: the first is tenancy, the second is provenance.
    """
    from collections import defaultdict  # noqa: PLC0415
    from shared.campaigns import create_campaign_from_target_refs  # noqa: PLC0415
    from shared.email import send_campaign_submitted_emails  # noqa: PLC0415
    from shared.targets import campaign_ids_for_target, get_target  # noqa: PLC0415

    detail = url_for("targets.target_detail", target_id=source_target_id)
    # Two causes, two answers. `candidate_refs` empty is the observable of
    # every client-side way the star selection can fail to reach us (register
    # item B-3), and telling that user "name your target" would be a lie.
    if not candidate_refs:
        return redirect(detail + "?handoff=none")
    if not target_name:
        return redirect(detail + "?handoff=noname")
    if get_target(source_target_id, user_id=ctx.user_id) is None:
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
    # ``{job_id: len(candidate_records(job.result))}``, filled from the job
    # already in hand so the index check below costs no extra read.
    n_records: dict = {}
    # Rejected ids are remembered too. Without this, a body naming the same
    # foreign job 500 times issues 500 identical Supabase round trips, because
    # a miss never writes to ``jobs_by_id`` and so is never a cache hit. The
    # campaign branch above still has that shape; filed as A88.
    rejected: set = set()
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
    # Set when a rejection was decided BY THE CAMPAIGN ARM -- the job exists,
    # is the caller's own, carries a campaign_id, and that id was not in the
    # set. Those are exactly the rejections a short id read could have got
    # wrong; see the refusal below.
    campaign_arm_rejected = False
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
        if jid in rejected:
            dropped += 1
            continue
        job = jobs_by_id.get(jid)
        if job is None:
            job = get_job(jid, user_id=ctx.user_id)
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
                if job is not None and job.campaign_id is not None:
                    campaign_arm_rejected = True
                continue
            jobs_by_id[jid] = job
            n_records[jid] = len(candidate_records(job.result))
        # The index has to exist in the source job's results. Unvalidated, an
        # out-of-range ref is persisted, counted on the staff email, counted on
        # the customer's page -- and then silently skipped by
        # `stage_campaign_candidates`, so the lab receives fewer PDBs than
        # every number anyone can see. Same rule as the read side in
        # blueprints/admin.py: applied only when the record count is POSITIVE,
        # because `candidate_records` returns [] both for a job with no results
        # and for a result shape it cannot read, so zero means "length
        # unknown" and refusing every design of such a job would be a louder
        # wrong answer than saying nothing about it.
        n = n_records.get(jid, 0)
        if n and idx >= n:
            dropped += 1
            continue
        refs_by_job[jid].append(idx)
        clean_refs.append({"job_id": jid, "index": idx})

    # Refs the parse-time cap discarded before any check ran. Counted in REFS,
    # not designs: the tail past the bound was never parsed into pairs, so a
    # duplicate hiding in it cannot be subtracted. Over-stating here is the
    # harmless direction, the same trade `_starred_refs` makes for its own
    # truncation flag.
    truncated = max(0, requested_refs - len(candidate_refs))

    # A REJECTION UNDER AN INCOMPLETE READ IS NOT A VERDICT. `owned_by_target`
    # consults `target_campaign_ids`, so when that set is a prefix of the real
    # one, a legitimate campaign-sourced design is indistinguishable from a ref
    # belonging to some other target. This route stages a PAID order, so
    # proceeding would hand the wet lab a shortlist quietly missing designs the
    # user selected and paid to compute.
    #
    # Gated on `campaign_arm_rejected`, NOT on `dropped`. `dropped` also counts
    # refs naming a job that does not exist, one belonging to another tenant,
    # one whose target is a different protein, and an index past the end of its
    # job -- none of which the campaign id set could have decided differently,
    # so refusing on those blames the read for a verdict it did not make and
    # sends the user away from a submission that was correct.
    if campaign_arm_rejected and not campaign_ids_complete:
        return redirect(detail + "?handoff=unverified")

    # NOT `none`. The request DID carry designs; every one of them failed the
    # tenancy or provenance or index check. The two cases share nothing but
    # their outcome: `none` is recoverable by retrying (the stars are still in
    # sessionStorage and the likely cause is that they did not reach us),
    # `rejected` is not, because the same refs will be refused the same way.
    # Round 19 collapsed both onto `none` and so told this user to keep
    # pressing a button that can never work.
    if not clean_refs:
        return redirect(detail + "?handoff=rejected")

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
    # Scoped to the target branch on purpose. `_submit_campaign_shortlist`
    # above has a byte-identical pair of returns and is out of Phase 5's
    # scope; it is filed as A88 in
    # docs/audit-2026-07-22-campaign-rework-open-items.md. (Round 19 claimed
    # it was filed and it was not: the register ended at A87, a different
    # defect.)
    except ValueError:
        return redirect(detail + "?handoff=failed")
    if lab_campaign is None:
        return redirect(detail + "?handoff=failed")

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
    # "7" as the number they chose (register item A-7). They stay SEPARATE
    # because their remedies are opposite: a rejected design will be rejected
    # again, while a truncated one only needs a second, smaller request.
    # Each omitted when zero, so the common case keeps exactly today's URL.
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


@lab_projects_bp.route("/lab-projects/submit", methods=["POST"])
@login_required
def campaigns_submit():
    import json  # noqa: PLC0415
    from shared.campaigns import create_campaign  # noqa: PLC0415
    from shared.email import send_campaign_submitted_emails  # noqa: PLC0415
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
    # target branch would have rejected. The legacy single-job form is last.
    source_target_id = request.form.get("source_target_id", "").strip()
    # The counted form, because the target branch below reports what the
    # parse-time cap removed. `requested_refs` is the well-formed ref count
    # BEFORE truncation; `candidate_refs` is capped at _MAX_CANDIDATE_REFS, so
    # its length cannot tell the two apart.
    candidate_refs, requested_refs = _parse_candidate_refs_counted(
        request.form.get("candidate_refs", ""),
    )
    # Gated on the parent ALONE, not on `and candidate_refs`, unlike the
    # campaign arm below. Phase 5.2 removed the `disabled` attribute from the
    # send button, so a user with nothing starred can now open the modal and
    # submit it. With `and candidate_refs` that body falls through both ref
    # branches to the legacy single-job one, which finds no source_job_id and
    # redirects to /jobs -- a user who clicked "Send shortlist" on a target page
    # would land on an unrelated list. The target branch's own guard already
    # returns them to the target. The campaign arm keeps its shape because its
    # button is unchanged and its empty case is not newly reachable.
    if source_target_id:
        return _submit_target_shortlist(
            ctx, source_target_id, candidate_refs, target_name,
            target_context, assay_type, budget_band, affinity_goal_kd_nm,
            timeline_weeks, requested_refs=requested_refs,
        )

    source_campaign_id = request.form.get("source_campaign_id", "").strip()
    if source_campaign_id and candidate_refs:
        return _submit_campaign_shortlist(
            ctx, source_campaign_id, candidate_refs, target_name,
            target_context, assay_type, budget_band, affinity_goal_kd_nm,
            timeline_weeks,
        )

    # -- Legacy single-job shortlist --------------------------------------
    source_job_id = request.form.get("source_job_id", "").strip()
    raw_indices = request.form.get("candidate_indices", "[]").strip()
    try:
        candidate_indices = [int(i) for i in json.loads(raw_indices)]
    except Exception:
        candidate_indices = []

    if not source_job_id or not target_name or not candidate_indices:
        return redirect(url_for("jobs.jobs_list"))

    job = get_job(source_job_id, user_id=ctx.user_id)
    if job is None:
        return redirect(url_for("jobs.jobs_list"))

    try:
        campaign = create_campaign(
            user_id=ctx.user_id,
            source_job_id=source_job_id,
            candidate_indices=candidate_indices,
            target_name=target_name,
            target_context=target_context,
            assay_type=assay_type,
            budget_band=budget_band,
            affinity_goal_kd_nm=affinity_goal_kd_nm,
            timeline_weeks=timeline_weeks,
        )
    except ValueError:
        return redirect(url_for("jobs.jobs_list"))

    if campaign is None:
        return redirect(url_for("jobs.jobs_list"))

    # Copy candidate PDBs into durable campaign bucket.
    candidates = candidate_records(job.result)
    try:
        stage_campaign_candidates(
            campaign_id=campaign.id,
            candidates=candidates,
            indices=candidate_indices,
            user_id=ctx.user_id,
            job_id=source_job_id,
        )
    except StorageError:
        logger.warning("stage_campaign_candidates failed for %s", campaign.id)

    # Emails — best-effort.
    try:
        send_campaign_submitted_emails(
            campaign=campaign,
            user_email=session.get("user_email", ""),
        )
    except Exception:
        logger.warning("campaign submit emails failed", exc_info=True)

    return redirect(url_for("lab_projects.campaign_detail", campaign_id=campaign.id) + "?submitted=1")

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
    # not one, because a rejected design will be rejected again and a
    # truncated one only needs a second, smaller request. Only the target
    # branch sends either, and only when non-zero. Clamped rather than
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
