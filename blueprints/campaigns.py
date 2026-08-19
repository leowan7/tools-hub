"""Compute campaign routes (blueprint refactor, Commit 4).

Self-serve batched design at /campaigns/* + /api/campaigns/* plus the
/runs/* and /admin/campaigns/* legacy 301 redirects. Lifted verbatim from
``create_app()``; only ``@flask_app.route`` -> ``@campaigns_bp.route`` and
compute-campaign endpoint self-refs -> ``campaigns.*``. The helpers
(_campaign_passed_filters, _cutover_redirect) move in with the routes.

The preauth copy and the campaign-tool flag gate are NOT here: Phase 2 moved
them to shared/compute_campaigns.py so the multi-tool launch screen in
blueprints/targets.py applies the identical gate without importing this module.
"""

from __future__ import annotations

import logging
import time

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from shared.auth import login_required
from shared.credits import get_service_client, load_user_context
from shared.idempotency import idempotent
from shared.pdb_intake import _parse_preflight_size_params, resolve_target_upload
from shared.storage import StorageError, upload_input
from tools import base as tool_base

logger = logging.getLogger(__name__)

campaigns_bp = Blueprint("campaigns", __name__)

# Why the lab handoff sent the user back to this run's page.
# `compute_campaign_detail` whitelists these and hands the survivor to the
# template, which has one branch per reason.
#
# PUBLIC AND MODULE-LEVEL SO THE BANNER TESTS CAN IMPORT IT, for the reason
# blueprints/targets.py records beside its own copy: a hand-written copy of
# these keys in a test file does not couple to anything, so a sixth reason added
# here renders the `{% else %}` arm's copy -- "your request could not be
# submitted" -- for an unrelated cause with the whole suite green.
#
# SAME FIVE KEYS AS blueprints/targets.py::HANDOFF_REASONS, and three of the
# five sentences (`none`, `noname`, `failed`) are word for word the target
# page's. TWO OF THE FIVE describe a cause that differs by arm, and reading it
# as one is how the two arms get treated as interchangeable. `rejected` is the
# first: where this route's `rejected` tests parentage it asks "child of this
# run" rather than "on this target" -- though parentage is not its only ground,
# since a ref naming an index past the end of a job that IS a child lands there
# too. `unverified` is the second, and the paragraphs below say how: the two
# arms reach it from different causes and `cc.read_campaign` has a ground
# `read_target` does not (register item A97). The SENTENCE is now shared; the
# CAUSE SETS are not, and nothing here licenses merging the banner suites --
# two when this was written, three since A91 added blueprints/jobs.py.
#
# THE COPY IS NOW ONE PARTIAL AND THE PAGES ARE NOT. A90 lifted the five
# sentences into templates/components/lab_handoff_banner.html, which takes the
# arm's noun -- each page keeps its own wrapper, its own whitelist and its own
# suite. Two inline copies were how one of these sentences went stale unnoticed,
# and the partial now has FOUR importers rather than two:
# templates/unavailable.html and templates/job_detail.html render them as well.
# The 503 page is reached only from the TARGET arm's detail route, never from
# this one (see `compute_campaign_detail` below and register item A94), so the
# run-noun rendering of it does not occur in production -- the macro nonetheless takes the noun, because a partial whose
# correctness depends on which caller reaches it is the duplication again.
#
# `unverified` USED TO differ too, and no longer does beyond the noun. It named
# the sub-job read here and the paged run-list read on the target page. HERE
# that was the whole set of causes this arm then had, which was one. THERE IT
# WAS NOT: that arm has always ALSO set `unresolved` on a `read_job` that came
# back unavailable, so its sentence named one of two causes and was already
# false for the other before A90 touched anything -- the correction is written
# up in this commit's register entry.
#
# Register item A90 then gave each arm one more cause: the PARENT read at the
# top of the SUBMIT gate (`cc.read_campaign` here, `read_target` there, both in
# blueprints/lab_projects.py) now reports "unreadable" separately from "absent"
# and refuses to this same reason. That is the submit gate and not this detail
# route, which still reads through the two-outcome `get_campaign`. Neither
# sentence names a read any more, and the copy for both pages now lives in
# templates/components/lab_handoff_banner.html; see the comment above the banner
# in templates/runs/detail.html.
LAB_HANDOFF_REASONS = ("none", "noname", "rejected", "unverified", "failed")


# The campaign detail page polls the status endpoint every ~5s per open tab,
# and the status endpoint reconciles in-flight children (poll Modal + settle +
# dispatch the next wave). Collapse repeat polls — many tabs, fast interval —
# to at most one reconcile per campaign per interval so N tabs cannot fan out
# N x synchronous Modal polls (and job dispatch) on a 5-second GET. The cron
# tick (~60-90s) reconciles every campaign regardless, so this throttle only
# trades a little settle latency for load; it never blocks progress.
_STATUS_RECONCILE_MIN_INTERVAL_S = 20.0
_last_status_reconcile: dict = {}


def _status_reconcile_due(campaign_id: str) -> bool:
    """True (and stamps ``now``) when this campaign is due a status-path
    reconcile. In-process and best-effort: under W web workers at most ~W
    reconciles fire per interval no matter how many tabs poll. The map is
    coarse-pruned so it cannot grow without bound over an instance's life."""
    now = time.monotonic()
    last = _last_status_reconcile.get(campaign_id)
    if last is not None and (now - last) < _STATUS_RECONCILE_MIN_INTERVAL_S:
        return False
    if len(_last_status_reconcile) > 4096:
        cutoff = now - _STATUS_RECONCILE_MIN_INTERVAL_S
        for cid in [c for c, t in _last_status_reconcile.items() if t < cutoff]:
            _last_status_reconcile.pop(cid, None)
    _last_status_reconcile[campaign_id] = now
    return True


# -- Compute campaigns ("Campaigns") ---------------------------------
# Self-serve batched design: split a large request into many sub-jobs.
# Served at /campaigns/* (the customer-facing product noun). The older
# wet-lab funnel moved to /lab-projects/* in the launch cutover; the old
# /runs/* compute paths 301-redirect here for already-sent email links.

# The preauth copy and the campaign-tool flag gate now live in
# shared/compute_campaigns.py (preauth_message, visible_campaign_tools,
# campaign_tool_gated_off) so blueprints/targets.py can apply the same gate to
# the multi-tool launch screen without importing this blueprint. One definition,
# two callers.


@campaigns_bp.route("/campaigns", methods=["GET"])
@login_required
def compute_campaigns_list():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.jobs import list_jobs_paginated  # noqa: PLC0415
    # Unified feed: real campaigns + standalone single runs (campaigns of one),
    # newest first. Campaign children (campaign_id set) are excluded from the
    # standalone list — they live inside their campaign. Each source is capped,
    # then merged in memory; fine at these sizes (see plan note on union
    # pagination). ISO created_at strings sort chronologically as text.
    campaigns = cc.list_campaigns_for_user(ctx.user_id, limit=100)
    standalone, _ = list_jobs_paginated(
        ctx.user_id, page=1, page_size=100, standalone_only=True,
    )
    entries = [("campaign", c, c.created_at or "") for c in campaigns]
    entries += [("job", j, j.created_at or "") for j in standalone]
    entries.sort(key=lambda e: e[2], reverse=True)
    return render_template("runs/list.html", entries=entries)

@campaigns_bp.route("/campaigns/new", methods=["GET"])
@login_required
def compute_campaign_new():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.targets import get_target, target_defaults_for_form  # noqa: PLC0415
    # ?target_id= swaps the file input for a target chip and prefills the
    # target's stored chain + hotspots. An unknown or unowned id silently
    # falls back to the plain upload form rather than confirming the id
    # exists for someone else.
    target = None
    target_id = (request.args.get("target_id") or "").strip()
    if target_id:
        target = get_target(target_id, user_id=ctx.user_id)
        if target is not None and (target.is_archived or not target.storage_path):
            target = None
    return render_template(
        "runs/new.html",
        supported_tools=cc.visible_campaign_tools(),
        max_subjobs=cc.MAX_SUBJOBS_PER_CAMPAIGN,
        verification_threshold=str(cc.VERIFICATION_THRESHOLD_USD),
        target=target,
        pre_fill=target_defaults_for_form(target),
    )

@campaigns_bp.route("/api/campaigns/estimate", methods=["GET"])
@login_required
def api_runs_estimate():
    """Live budget + chunk-plan preview for the campaign create form."""
    from shared import compute_campaigns as cc  # noqa: PLC0415
    tool = (request.args.get("tool") or "").strip()
    preset = (request.args.get("preset") or "pilot").strip() or "pilot"
    try:
        requested = int(request.args.get("requested_designs") or "0")
    except ValueError:
        requested = 0
    if cc.campaign_tool_gated_off(tool):
        return jsonify({"ok": False, "error": "That tool is not available yet."})
    if preset == "validate":
        # The free pre-flight is not a paid campaign — mirror the create route.
        return jsonify({"ok": False, "error": "The validate tier is a free pre-flight, not a campaign."})
    try:
        # Thread the real variant so the estimate matches the create path (the
        # 5 live tools default to "pilot"); today proteina is fixed-container so
        # the figures coincide, but this stops a silent divergence if pricing
        # ever becomes preset-dependent.
        plan = cc.plan_chunks(tool, requested, preset)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)})
    first_wave = cc.first_wave_hold_usd(plan, cc.launch_concurrency_for(tool))
    pre = cc.campaign_preauth(session.get("user_id"), plan.budget_usd, first_wave)
    return jsonify({
        "ok": True,
        "tool": tool,
        "requested_designs": plan.requested_designs,
        "chunk_size": plan.chunk_size,
        "total_subjobs": plan.total_subjobs,
        # The exact 4dp values stay on the wire for anything that computes with
        # them; the *_display strings are what the page renders. The page used to
        # do its own 2dp rounding to NEAREST, which put a figure BELOW the real
        # hold directly above the consent checkbox: rfdiffusion at 1 design holds
        # $2.6219 and displayed "$2.62". Costs round UP, the balance rounds DOWN,
        # both in Decimal, so neither direction can flatter the user.
        "per_chunk_usd": str(plan.est_cost_per_chunk),
        "per_chunk_usd_display": cc.display_cost_usd(plan.est_cost_per_chunk),
        "budget_usd": str(plan.budget_usd),
        "budget_usd_display": cc.display_cost_usd(plan.budget_usd),
        "first_wave_usd": str(first_wave),
        "first_wave_usd_display": cc.display_cost_usd(first_wave),
        "balance_usd": str(pre.balance_usd),
        "balance_usd_display": cc.display_balance_usd(pre.balance_usd),
        "affordable": pre.ok,
        "reason": pre.reason,
        "needs_verification": cc.CAMPAIGN_KYC_ENABLED and (plan.budget_usd > cc.VERIFICATION_THRESHOLD_USD),
    })

@campaigns_bp.route("/campaigns", methods=["POST"])
@login_required
@idempotent()
def compute_campaign_create():
    # A58. This was the ONLY money-spending POST in the app without this
    # decorator, and the omission was live: a double-submit funded TWO campaigns
    # against one consent, gating the same first wave twice against the same
    # balance. Measured at two identical POSTs -> created=2, funded=2. There is
    # no client-side guard either (runs/new.html registers no submit handler),
    # the CSRF token is session-scoped and reusable, and the POST takes seconds.
    #
    # Every sibling already had it: POST /targets, POST /targets/<id>/launch,
    # POST /tools/<tool>/submit, /campaigns/<id>/refold, /developability/score,
    # /library-planner/plan. This repo's own docs name this exact failure mode
    # as a defect, for a route that HAS the decorator.
    #
    # Safe to add only because of the hardening in this same branch: the key
    # falls back to a canonical encoding of request.form when _enforce_csrf has
    # already consumed the raw body (otherwise every submission to this route
    # would share one key and a genuine second campaign would vanish), and 4xx
    # responses release the claim so a corrected resubmission is not answered
    # with the stale rejection.
    #
    # Scope, stated because the copy above is easy to over-read. This dedups
    # SEQUENTIAL resubmissions -- refresh, back button, network retry, the
    # second click of a slow double-click. It now serialises genuinely
    # concurrent ones too: `_claim_key` claims with a plain `insert`, so the
    # PRIMARY KEY makes exactly one racing caller win and the rest are answered
    # from its result (audit A42, resolved). This comment previously said the
    # opposite, and misfiled the residue as A41 besides.
    #
    # And the discriminator for THIS route is weaker than it looks: the key
    # folds the upload's filename and BYTE LENGTH, never its content. Two posts
    # whose form fields all match, carrying same-named files of identical
    # length, collapse inside the 60s TTL and the second structure is silently
    # discarded. Distinct proteins almost never collide; an in-place
    # single-character edit of the same file does.
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.targets import (  # noqa: PLC0415
        enrich_target_hotspot_spec, get_target, touch_target,
    )

    tool = (request.form.get("tool") or "").strip()
    name = (request.form.get("name") or "").strip()
    preset = (request.form.get("preset") or "pilot").strip() or "pilot"
    try:
        requested = int(request.form.get("requested_designs") or "0")
    except ValueError:
        requested = 0

    # Resolved before _err so an error re-render keeps the target chip instead
    # of dropping back to a file input the user cannot satisfy.
    target = None
    target_id = (request.form.get("target_id") or "").strip()
    if target_id:
        # Owner-scoped fetch is the WHOLE boundary here: copy_input and
        # download_input take user_id as a path component, not an authz check,
        # so resolving this id to a storage path any other way is a
        # cross-tenant structure read.
        target = get_target(target_id, user_id=ctx.user_id)

    def _err(msg, code=400):
        return render_template(
            "runs/new.html",
            # Filtered so a flag-gated tool (proteina) is not leaked into the
            # dropdown on a validation-error re-render, matching the GET form.
            supported_tools=cc.visible_campaign_tools(),
            max_subjobs=cc.MAX_SUBJOBS_PER_CAMPAIGN,
            verification_threshold=str(cc.VERIFICATION_THRESHOLD_USD),
            error=msg,
            target=target,
            pre_fill=request.form.to_dict(),
        ), code

    if target_id and target is None:
        return _err("That target could not be found.")
    if target is not None:
        if target.is_archived:
            return _err(
                "That target is archived. Pick another one or upload a new "
                "structure."
            )
        if not target.storage_path:
            return _err("That target has no stored structure to run against.")

    # 0. Resolve the adapter + preset up front. The 5 live campaign tools each
    #    carry a single "pilot" preset (the default here), so their behaviour is
    #    unchanged; proteina posts one of its design variants. An unknown preset
    #    would mis-size the chunk plan, so reject it before planning. A
    #    flag-gated tool that is still off is treated as unknown (don't reveal a
    #    hidden tool exists) — defense-in-depth behind the dropdown filter.
    adapter = tool_base.get(tool)
    if adapter is None or cc.campaign_tool_gated_off(tool):
        return _err("Unknown tool.")
    if adapter.preset_for(preset) is None:
        return _err("Unknown preset for this tool.")
    # The free `validate` tier is a CPU-only pre-flight, not a paid campaign; it
    # is omitted from the form and routed separately. Reject it on the paid path
    # so a crafted request can't open a priced campaign on a config-less variant.
    if preset == "validate":
        return _err("The validate tier is a free pre-flight, not a campaign.")
    # IgGM affinity_maturation runs one design PER masked position PER sample, so
    # the delivered count != the per-chunk num_samples the driver injects, which
    # breaks the campaign's delivered-count==chunk-size invariant (holds, progress
    # counts, and finalize all assume equality). Keep it on the atomic tier only.
    if tool == "iggm" and preset == "affinity_maturation":
        return _err(
            "Affinity maturation is not available as a campaign (its design "
            "count expands per masked position). Use the single-run IgGM form."
        )

    # 1. Plan (validates tool + count + sub-job cap).
    try:
        plan = cc.plan_chunks(tool, requested, preset)
    except ValueError as exc:
        return _err(str(exc))

    # 2. Validate the tool params by reusing the adapter validator with
    #    an in-cap placeholder design count (the real per-chunk count is
    #    injected by the driver).
    form_for_validate = dict(request.form)
    form_for_validate[plan.design_param_key] = "1"
    # The route's own resolved preset, not the raw form value. The form's two
    # `name="preset"` selects are BOTH disabled for the five pilot tools, so
    # nothing posts the field for them, and bindcraft is the one adapter that
    # does not default it -- it reads `(form.get("preset") or "").strip()` and
    # rejects anything but "pilot". Every bindcraft campaign therefore failed
    # validation with "Pick a preset." before this line existed. Safe for the
    # others: `preset` was already validated against this adapter at step 0.
    form_for_validate["preset"] = preset
    # Declare whether a structure actually exists for this run, so the adapter
    # never has to infer it. Assigned OVER the form dict, after construction, so
    # a crafted `_has_custom_target` post cannot forge a custom run with nothing
    # staged behind it. `target` is resolved above; the file is checked by
    # filename because an empty part still arrives as a FileStorage.
    _uploaded_now = (
        request.files.get("target_sdf") if preset == "ligand_binder"
        else request.files.get("target_pdb")
    )
    form_for_validate["_has_custom_target"] = (
        "1" if (target is not None or (_uploaded_now is not None and _uploaded_now.filename))
        else ""
    )
    validated, verr = adapter.validate(form_for_validate, request.files)
    if validated is None:
        return _err(verr or "Invalid parameters.")

    # 2b. Can this tool's MODEL, and the IMAGE we dispatch to, take the number
    #     of chains just named? This route has never asked. `preflight_for_tool`
    #     owns that gate and is called only from the atomic submit route, its
    #     AJAX panel, and the reuse-token path, so a two-chain campaign was
    #     created, funded and driven here for tools whose container cannot
    #     parse the target -- and for rfantibody, whose MODEL cannot
    #     (multi_chain_supported=False; its adapter accepts "A,B" because it
    #     only length-checks the field at 4 characters). Verified by executing
    #     this route against a two-chain stored target.
    #
    #     Placed BEFORE the target/upload split so both branches are covered by
    #     one call: the fresh-upload branch spends exactly as much as the
    #     target-bound one. Ahead of campaign_preauth and create_campaign, so a
    #     refusal costs a message rather than a funded wave.
    #
    #     Capability ONLY, deliberately -- see multi_chain_refusal and
    #     DesignTarget.size_error for why the rest of the preflight does not
    #     belong on a route that never downloads the structure.
    #     iggm names its antigen chain `antigen_chain`; the PDB tools use
    #     `target_chain`. proteina replaces target_chain with its contig's
    #     chains, which is the right string to judge.
    run_chain = (
        validated.get("target_chain") or validated.get("antigen_chain") or ""
    )
    from shared.pdb_preflight import multi_chain_refusal  # noqa: PLC0415
    capability_err = multi_chain_refusal(tool, run_chain)
    if capability_err:
        return _err(capability_err)

    # 3. Resolve the campaign target. The live tools + proteina's protein/motif
    #    variants take a PDB (inspected + chain-validated); proteina's ligand
    #    variant takes an SDF (cheap sanity only; the RDKit -> chain-A PDB
    #    conversion happens in-container). For proteina a target is OPTIONAL — a
    #    curated benchmark task carries its own target, so no upload means "run
    #    the task's built-in target". The 5 live tools keep the mandatory-PDB
    #    path exactly as before.
    #    A run launched from a stored target skips all of this: the structure
    #    is already staged and validated, so re-uploading it would be the exact
    #    duplication targets exist to remove.
    is_proteina = tool == "proteina"
    is_ligand = is_proteina and validated.get("preset") == "ligand_binder"
    uploaded = (
        request.files.get("target_sdf") if is_ligand
        else request.files.get("target_pdb")
    )
    upload = None

    # An attached file OVERRIDES the target, and drops the target link with it.
    # Same rule as the atomic form's reuse tokens, which document override-by-
    # upload verbatim. The form disables both file inputs when a target is
    # bound, so this only fires on a crafted or stale POST — but silently
    # discarding an attached file (the previous behaviour) meant paying for a
    # campaign against a structure the user did not send, with no warning.
    # Dropping the link too is what stops a design produced from structure Y
    # appearing in target X's merged ranking.
    if target is not None and uploaded is not None and uploaded.filename:
        target = None

    if target is not None:
        # A target's structure has to be the format this tool consumes.
        # Unreachable today (every target is created as ``pdb``), but the
        # ligand path silently accepting a PDB is the kind of latent mismatch
        # that only shows up as a container failure after the money is spent.
        if is_ligand != (target.kind == "sdf"):
            return _err(
                "That target is a "
                f"{'small molecule' if target.kind == 'sdf' else 'structure'}, "
                "which this tool cannot use. Pick another target."
            )
        # The chain and hotspots are per-RUN and may override the target's
        # defaults, so they still have to be checked — against the inspection
        # persisted at upload time, so no download is needed. ``run_chain`` is
        # resolved above (step 2b) from the same two keys; it used to be
        # recomputed here from the identical expression.
        chain_err = target.chain_error(run_chain)
        if chain_err:
            return _err(chain_err)
        # Both keys are original PDB author numbering, so both are range-
        # checkable against the target's chain. iggm calls its epitope
        # ``epitope_pdb_resnums``; every other campaign tool calls its
        # hotspots ``hotspot_residues``. ``shipped_hotspots`` is what reads
        # the pair, and it prefers proteina's chain-prefixed ``hotspot_spec``
        # over the bare copy — see that function for why the bare one cannot
        # be range-checked without refusing correct multi-chain runs.
        from shared.pdb_preflight import shipped_hotspots  # noqa: PLC0415
        hotspot_err = target.hotspot_error(
            run_chain, shipped_hotspots(validated),
        )
        if hotspot_err:
            return _err(hotspot_err)
        # Chain/residue ranges, for adapters that declare them (proteina's
        # target_input today). Same persisted-summary check, no download.
        segment_err = target.segment_error(validated.get("_target_segments") or [])
        if segment_err:
            return _err(segment_err)
        # Size cap. Runs BEFORE the wallet gate below, so an oversized launch
        # costs an error message rather than a wave of shards that bill to the
        # session wall for zero designs. Size only — see DesignTarget.size_error
        # for why the full preflight does not belong on this route.
        # binder_max_aa arms the COMBINED cap (target + binder). Without it
        # only the target half of the envelope ran here, so a 400 aa target
        # with a 300 aa max binder — 700 against proteina's 620 budget — was
        # refused by /tools/proteina/submit and funded by this route. Read via
        # _parse_preflight_size_params because the validated binder shape is
        # per-tool ({min,max} dict, [min,max] list, bare int, or a separate
        # binder_length_max key) and that helper already reads all four.
        size_err = target.size_error(
            tool, run_chain, validated.get("_target_segments") or [],
            binder_max_aa=_parse_preflight_size_params(validated)[0],
        )
        if size_err:
            return _err(size_err)
    elif uploaded is None or not uploaded.filename:
        if not is_proteina:
            return _err("Upload a target PDB file.")
        # proteina + no upload: curated-task path, no staged target file.
    else:
        # iggm names its antigen chain ``antigen_chain`` (it reads the form's
        # ``target_chain`` but stores it under that key); the other PDB tools
        # use ``target_chain``. Pass whichever the adapter produced so the
        # antigen chain is validated against the upload before any GPU spend.
        upload, upload_err = resolve_target_upload(
            uploaded,
            target_chain=(
                validated.get("target_chain")
                or validated.get("antigen_chain")
                or ""
            ),
            kind="sdf" if is_ligand else "pdb",
        )
        if upload is None:
            return _err(upload_err or "Upload a target PDB file.")
        # Same size cap on the fresh-upload branch, from the inspection just
        # produced. Placed here so it is still ahead of campaign_preauth and
        # create_campaign: nothing has moved money or written a row yet, so
        # returning an error is clean.
        from shared.pdb_preflight import size_only_refusal  # noqa: PLC0415
        from shared.targets import (  # noqa: PLC0415
            _segments_label, selection_residue_count,
        )
        upload_segments = validated.get("_target_segments") or []
        # getattr, not attribute access: an SDF upload carries no inspection
        # (and so no summary), and this must not turn a ligand campaign into a
        # 500. A missing summary means "cannot say", which skips the gate —
        # the same posture selection_residue_count takes for a target that
        # predates the summary column.
        upload_aa = selection_residue_count(
            getattr(upload, "chain_summary", None),
            validated.get("target_chain") or validated.get("antigen_chain") or "",
            upload_segments,
        )
        if upload_aa is not None:
            size_err = size_only_refusal(
                tool, upload_aa,
                # Same combined-cap arming as the target-bound branch above.
                # This branch and that one take different kwargs, so a fix
                # applied to one of them leaves the other blind.
                binder_max_aa=_parse_preflight_size_params(validated)[0],
                selection_label=_segments_label(upload_segments),
            )
            if size_err:
                return _err(size_err)

    # 4. Prepaid START gate (checks, never debits): the wallet only has to
    #    cover the first wave; the rest funds as the campaign drains, and it
    #    pauses/resumes on balance (fund-and-drain).
    first_wave = cc.first_wave_hold_usd(plan, cc.launch_concurrency_for(tool))
    pre = cc.campaign_preauth(ctx.user_id, plan.budget_usd, first_wave)
    if not pre.ok:
        # Passed explicitly even though the default derives the same string
        # today, because "the same by coincidence" is how the multi-tool route
        # ended up printing $9.18 in this sentence over a $9.19 panel. This page
        # ships `first_wave_usd_display` from the same helper, so the sentence
        # and the panel are now the same string by construction.
        return _err(cc.preauth_message(
            pre, required_display=cc.display_cost_usd(first_wave),
        ))

    # 5. Stage the shared target once (when one was provided), then create +
    #    fund + first wave. A proteina curated-task run stages nothing.
    #    A run launched from a target stages nothing either: it DENORMALIZES
    #    the target's existing path onto the campaign row, which is what keeps
    #    the driver unchanged — _dispatch_chunk keeps re-minting its presigned
    #    URL from target_storage_path every wave and never learns about
    #    design_targets at all.
    staged_path = None
    if target is not None:
        staged_path = target.storage_path
    elif upload is not None:
        import uuid as _uuid  # noqa: PLC0415
        target_key = f"campaign-{_uuid.uuid4().hex}"
        try:
            staged_path = upload_input(
                user_id=ctx.user_id, job_id=target_key,
                filename=upload.filename, data=upload.data,
                content_type=upload.content_type,
            )
        except StorageError as exc:
            return _err(f"Upload failed: {exc}")

    # Layer 2 of the target-source invariant: a run that declared a custom
    # target must have one staged. Checked HERE, before create_campaign and
    # before any wallet movement, because the alternative is the failure this
    # whole path exists to remove — campaign created, hold placed, shards
    # dispatched, every one of them refused in-container for a structure that
    # was never staged.
    if validated.get("target_source") == "custom" and not staged_path:
        return _err(
            "This run is set up to design against your own target, but no "
            "structure was staged for it. Attach a target file or pick a "
            "curated benchmark task."
        )

    campaign = cc.create_campaign(
        user_id=ctx.user_id, tool=tool, params=validated,
        requested_designs=requested, preset=preset, name=name or None,
        target_storage_path=staged_path,
        target_name=(
            (request.form.get("target_name") or "").strip()
            or (target.display_name if target is not None else None)
        ),
        target_id=(target.id if target is not None else None),
    )
    if campaign is None:
        return _err("Could not create the campaign. Try again in a moment.")

    if target is not None:
        touch_target(target.id)
        # Same enrichment the multi-tool launch route performs, at the same
        # seam, for the same reason: this is the OTHER route that runs a tool
        # against a saved target, and a target enriched by one screen and not
        # the other would depend on which form the user happened to use.
        # ENRICH-ONLY — see shared.targets.enrich_target_hotspot_spec for the
        # three conditions and why each one is load-bearing. Only proteina's
        # adapter emits `hotspot_spec`; the helper answers False for everything
        # else without touching the database.
        enrich_target_hotspot_spec(target, validated.get("hotspot_spec"))

    # A59. The return value used to be discarded, so a failed fund redirected as
    # success and left the row at `draft` forever -- `cron/tick_campaigns.py`
    # excludes draft from _ACTIVE_STATES, so nothing ever picks it up. That is
    # the round-5 inversion in the other direction: round 5 told a charged user
    # nothing was charged; this told an uncharged user their campaign started.
    #
    # `fund_campaign` is three-valued, not two. It cannot raise (_cas_transition
    # swallows everything and returns False), so False means EITHER "the row was
    # not in draft" OR "the UPDATE raised and I cannot tell" -- and a write that
    # commits in Postgres while the response times out lands in the second
    # bucket. So False alone is not grounds for claiming nothing was charged;
    # it is confirmed by an owner-scoped read, exactly as target_launch_submit
    # does. Keep the two routes' policies identical.
    if not cc.fund_campaign(campaign.id):
        row = cc.get_campaign(campaign.id, user_id=ctx.user_id)
        if row is not None and row.status == "draft":
            # Confirmed inert: drive_campaign early-returns on draft,
            # _campaign_spend_today skips it, no hold was ever placed. Saying
            # "nothing was charged" here is TRUE, which matters because this
            # route is @idempotent and idempotency releases its claim on any
            # 4xx -- so the retry this invites must be safe. It is safe about
            # MONEY, which is the claim the copy makes. It is not free: each
            # retry strands another `draft` row and another staged PDB object,
            # and nothing reclaims either (`cron/tick_campaigns.py` excludes
            # draft, and there is no delete path). Those ghosts are visible in
            # the user's campaign list. Filed as A62.
            logger.warning(
                "compute_campaign_create: %s is still draft after fund; not "
                "driven", campaign.id,
            )
            return _err(
                "That campaign could not be started, and nothing was charged. "
                "Try again in a moment."
            )
        # Either it did move (the fund succeeded) or the read could not tell.
        # Fall through and treat it as started: claiming "not charged" about
        # money that may be committed is the more expensive error, and it is the
        # one that produces a duplicate.
        logger.warning(
            "compute_campaign_create: fund_campaign reported False for %s but "
            "its status is %s; treating it as started",
            campaign.id, getattr(row, "status", "unreadable"),
        )

    # Kick the first wave off the request path (daemon thread); the cron
    # tick backstops if the thread dies. At the raised concurrency an inline
    # drive would make many Modal + Supabase round-trips before responding.
    try:
        cc.drive_campaign_async(campaign.id)
    except Exception:
        # A60. `threading.Thread(...).start()` is OUTSIDE the try inside
        # `drive_campaign_async` (the try wraps the drive, not the spawn), so
        # this call is fallible: it raises RuntimeError when the process cannot
        # start another thread. Nothing may propagate past here. The campaign is
        # funded and `cron/tick_campaigns.py::_ACTIVE_STATES` will drive and
        # bill it, so a 500 out of this line would ALSO release the idempotency
        # claim -- and the retry it invites re-runs the whole handler, creating
        # and funding a SECOND campaign against one consent. That is verbatim
        # the A58 failure this route's decorator exists to stop, reachable
        # through the fix's own error path. `shared/idempotency.py` states the
        # rule this restores: a handler that spends money must not raise after
        # its first write. `target_launch_submit` already guards the same call.
        logger.exception(
            "compute_campaign_create: could not spawn the first-wave drive "
            "for %s; it is funded and the campaign tick will drive it",
            campaign.id,
        )
    return redirect(url_for("campaigns.compute_campaign_detail", campaign_id=campaign.id))

@campaigns_bp.route("/campaigns/<campaign_id>", methods=["GET"])
@login_required
def compute_campaign_detail(campaign_id):
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    from shared import compute_campaigns as cc  # noqa: PLC0415
    # STILL THE TWO-OUTCOME `get_campaign`, and A90 deliberately left it that
    # way after building the three-outcome read this arm's submit gate uses.
    # This route's None arm is not the defect A90 is about: an unreadable run
    # falls through to the cutover fallback and then to the runs list, HTTP 200,
    # exactly as it did before the item -- benign, if uninformative. The target
    # arm's None arm rendered 404, which is a false verdict about the row, and
    # that is the one that had to change (`blueprints/targets.py::target_detail`).
    #
    # MIRRORING THE TARGET ARM'S 503 HERE WAS TRIED AND REVERTED; the residual
    # that leaves is register item A94, and the cost that decided it is COUNTED
    # rather than felt. A REDIRECT NEVER RENDERS A TEMPLATE, so it never runs
    # `app.py::inject_workspace_context`. Under a total read outage this request
    # as written issues three Supabase reads and then redirects -- `get_tier`
    # inside the `load_user_context` above, `cc.get_campaign`, and the wet-lab
    # `get_campaign` below. A rendered 503 instead issues five: that same
    # `get_tier`, the campaign read, and then the context processor's own
    # `load_user_context` -> `get_tier`, `active_workspaces_count`, and the
    # navbar wallet chip. (Its fifth read, the onboarding ribbon, is guarded on
    # a wallet balance the failed chip read leaves None, so it does not fire.)
    # Every one of those five FAILS OPEN, which is why the page renders at all
    # -- but in the hang-shaped outage this exit exists for, failing open still
    # costs the full client read timeout each, serially, against
    # `gunicorn.conf.py`'s `timeout = 120` and its default `workers = 2`. The
    # user gets the gateway's 502 in place of our 503, and two such requests
    # occupy both workers. The target arm pays none of this: its ABSENT answer
    # already rendered `404.html`, so its 503 swapped one render for another.
    #
    # A94 also records what is therefore NOT delivered on this arm: a
    # `?handoff=unverified` redirect from the submit gate arrives here, and if
    # the fault outlived the redirect the user gets the runs list rather than
    # the banner. The two arms are asymmetric, and deliberately.
    campaign = cc.get_campaign(campaign_id, user_id=ctx.user_id)
    if campaign is None:
        # Launch-cutover fallback: /campaigns/<id> used to be the wet-lab
        # detail route (now /lab-projects/<id>). Already-sent wet-lab emails
        # link here, so if this id is one of the user's wet-lab campaigns,
        # forward it to its new home; otherwise fall back to the list.
        from shared.campaigns import get_campaign as _get_lab_campaign  # noqa: PLC0415
        if _get_lab_campaign(campaign_id, user_id=ctx.user_id) is not None:
            return redirect(
                url_for("lab_projects.campaign_detail", campaign_id=campaign_id), code=301
            )
        return redirect(url_for("campaigns.compute_campaigns_list"))
    counts = cc.get_progress_counts(campaign_id)
    # Why the lab handoff sent the user back here. Four of the five were a bare
    # `redirect(detail)` with no banner and nothing changed, which on the one
    # action that hands work to the wet lab reads as a dead button (register
    # item A-8, filed as A88 for this arm); the fifth, `unverified`, had no exit
    # at all -- the arm silently shipped a short paid order instead.
    # Whitelisted so an unknown or crafted value renders nothing at all rather
    # than an empty alert.
    handoff = (request.args.get("handoff") or "").strip()
    if handoff not in LAB_HANDOFF_REASONS:
        handoff = ""
    # Fan every succeeded sub-job's designs into one ranked table (top 300).
    agg = cc.aggregate_campaign_candidates(
        campaign_id, user_id=ctx.user_id, limit=300,
    )
    terminal = campaign.status in (
        "completed", "completed_with_failures", "failed", "cancelled",
    )
    return render_template(
        "runs/detail.html",
        campaign=campaign,
        counts=counts,
        candidates=agg.get("candidates", []),
        result_columns=agg.get("columns", []),
        candidates_total=agg.get("total", 0),
        candidates_capped=agg.get("capped", False),
        was_running=not terminal,
        handoff=handoff,
    )

@campaigns_bp.route("/campaigns/<campaign_id>/status.json", methods=["GET"])
@login_required
def compute_campaign_status(campaign_id):
    ctx = load_user_context()
    if ctx is None:
        return jsonify({"error": "auth"}), 401
    from shared import compute_campaigns as cc  # noqa: PLC0415
    campaign = cc.get_campaign(campaign_id, user_id=ctx.user_id)
    if campaign is None:
        return jsonify({"error": "not_found"}), 404
    # Terminalise any in-flight sub-job whose Modal FunctionCall already
    # returned an inline result but posted no terminal webhook (atomic-pattern
    # tools: proteina / iggm). Without this the counts on a *watched* campaign
    # would not advance — and its wallet hold would not settle — until the next
    # cron reconcile (~60-90s), or, absent the cron, the 6-hour stuck-job
    # sweeper. Doing it here settles a watched campaign near-instantly. Throttled
    # (see _status_reconcile_due) and best-effort; a poll fault must not break
    # the status read. Re-fetch so a just-finalised campaign reports terminal now.
    if campaign.status not in (
        "completed", "completed_with_failures", "failed", "cancelled",
    ) and _status_reconcile_due(campaign_id):
        try:
            cc.reconcile_campaign_children(campaign_id)
        except Exception:
            logger.warning(
                "campaign status: reconcile raised for %s",
                campaign_id, exc_info=True,
            )
        campaign = cc.get_campaign(campaign_id, user_id=ctx.user_id) or campaign
    counts = cc.get_progress_counts(campaign_id)
    payload = campaign.to_dict()
    payload["subjobs"] = counts
    # Terminal sub-jobs (succeeded + failed + timeout) are the accurate
    # progress signal: every sub-job that finished has a downloadable
    # result regardless of how many candidates passed the quality filter.
    payload["subjobs_complete"] = (
        counts.get("succeeded", 0)
        + counts.get("failed", 0)
        + counts.get("timeout", 0)
    )
    payload["subjobs_total"] = campaign.total_subjobs
    # ``hits`` is the number of candidates that PASSED the quality filter,
    # summed over succeeded children. It is NOT the number of designs
    # produced (small batches often pass nothing). ``designs_delivered``
    # is kept as a back-compat alias for the same value.
    hits = _campaign_passed_filters(campaign_id)
    payload["hits"] = hits
    payload["designs_delivered"] = hits
    payload["terminal"] = campaign.status in (
        "completed", "completed_with_failures", "failed", "cancelled",
    )
    # Paused = the wallet cannot fund the next chunk, so undispatched work
    # waits for a top-up. The driver sets this explicitly and resumes
    # automatically once the balance is restored, so the status is
    # authoritative. (Deliberately not inferred from a "nothing in flight +
    # chunks undispatched" heuristic, which also matches a transient
    # dispatch blip and would show a false "add funds" prompt for one tick.)
    payload["paused"] = campaign.status == "paused_insufficient_funds"
    return jsonify(payload)

def _campaign_passed_filters(campaign_id: str) -> int:
    """Sum candidates that PASSED the default quality filter across a
    campaign's succeeded children.

    Feeds the campaign detail page's "Passed filters" card, which used to
    under-report because it summed ``len(result.candidates)`` — every
    candidate, not just the passing ones — and only ever read
    ``result["candidates"]``, missing tools that persist rows under
    ``result["designs"]`` or nest ``filter_status`` under
    ``candidate["scores"]``.

    ``count_passed_candidates`` handles every shape and, per child, filters
    by ``filter_status`` when the records carry one (pxdesign, rfdiffusion)
    and falls back to the delivered count when they don't (the pre-filtered
    bindcraft / rfantibody, and boltzgen) — so the total equals the sum of
    what each child's own job page shows and no tool collapses to zero.
    """
    client = get_service_client()
    if client is None:
        return 0
    from shared.compute_campaigns import iter_succeeded_children  # noqa: PLC0415
    try:
        # Paged: this runs on every 5s status poll for the life of a running
        # campaign, and an unpaged select is clamped at PostgREST's max_rows
        # (1000), so a large campaign under-reported its passing designs for
        # the same reason the merged table under-reported its rows.
        rows = list(
            iter_succeeded_children(campaign_id, client, columns="result")
        )
    except Exception:
        return 0
    from shared.jobs import count_passed_candidates  # noqa: PLC0415
    return sum(count_passed_candidates(r.get("result")) for r in rows)

# CSV and FASTA are cheap ranked text, so they export the campaign's FULL set
# (limit=None). The ZIP keeps a top-N cap because it pulls every candidate's
# PDB bytes into web-process memory, and an unbounded campaign could OOM the
# process — that cap is load-bearing (decision 6).
#
# "Uncapped" is now literally true: the aggregator's sub-job fetch pages via
# shared.compute_campaigns.iter_succeeded_children, so the PostgREST max_rows
# (1000) clamp that used to silently truncate these exports no longer applies.
_CAMPAIGN_ZIP_EXPORT_LIMIT = 300


def _campaign_export(campaign_id: str, fmt: str):
    """Pooled CSV / FASTA / ZIP across a campaign's sub-jobs (ownership-gated).

    The aggregator resolves ownership (returns an empty ``tool`` when the
    campaign is not the caller's), so a foreign id 404s here rather than
    leaking designs. ZIP namespaces each PDB by its source sub-job.

    CSV / FASTA aggregate the full ranked set (``limit=None``); the ZIP is
    capped at :data:`_CAMPAIGN_ZIP_EXPORT_LIMIT` to bound memory. See that
    constant for the residual PostgREST-1000 caveat on the "full" text exports.
    """
    from flask import Response  # noqa: PLC0415
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.exports import (  # noqa: PLC0415
        candidates_to_csv, candidates_to_fasta, candidates_to_zip,
    )
    from shared.storage import download_output  # noqa: PLC0415

    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    export_limit = _CAMPAIGN_ZIP_EXPORT_LIMIT if fmt == "zip" else None
    agg = cc.aggregate_campaign_candidates(
        campaign_id, user_id=ctx.user_id, limit=export_limit,
    )
    if agg.get("tool") is None:
        return render_template("404.html"), 404
    candidates = agg.get("candidates", [])
    stem = "campaign_" + campaign_id[:8]

    if fmt == "csv":
        return Response(
            candidates_to_csv(candidates),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={stem}_scores.csv"},
        )
    if fmt == "fasta":
        body = candidates_to_fasta(candidates) or (
            "# No sequences found in this campaign's output.\n"
        )
        return Response(
            body,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={stem}.fasta"},
        )

    def _fetch(src_job_id: str, filename: str):
        try:
            return download_output(
                user_id=ctx.user_id, job_id=src_job_id, filename=filename,
            )
        except StorageError:
            logger.warning(
                "campaign export_zip: storage miss for %s/%s",
                src_job_id, filename, exc_info=True,
            )
            return None

    data = candidates_to_zip(candidates, _fetch, namespace=True)
    # When the ZIP is truncated, name the artifact so the "top N of M"
    # limitation travels with the file (the CSV / FASTA carry the full set).
    if agg.get("capped"):
        total = agg.get("total", len(candidates))
        zip_name = f"{stem}_pdbs_top{len(candidates)}of{total}.zip"
    else:
        zip_name = f"{stem}_pdbs.zip"
    return Response(
        data,
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={zip_name}"},
    )


@campaigns_bp.route("/campaigns/<campaign_id>/export.csv", methods=["GET"])
@login_required
def compute_campaign_export_csv(campaign_id):
    return _campaign_export(campaign_id, "csv")


@campaigns_bp.route("/campaigns/<campaign_id>/export.fasta", methods=["GET"])
@login_required
def compute_campaign_export_fasta(campaign_id):
    return _campaign_export(campaign_id, "fasta")


@campaigns_bp.route("/campaigns/<campaign_id>/export.zip", methods=["GET"])
@login_required
def compute_campaign_export_zip(campaign_id):
    return _campaign_export(campaign_id, "zip")


@campaigns_bp.route("/campaigns/<campaign_id>/refold", methods=["POST"])
@login_required
@idempotent()
def compute_campaign_refold(campaign_id):
    """Second-opinion fold across the whole campaign: refold its global top-N
    designs (drawn from any sub-job) in an orthogonal predictor and route to
    /jobs/compare. The campaign parallel of /jobs/<id>/refold; each design
    still cofolds against its own source sub-job's antigen (Boltz-2 path).
    """
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.refold import (  # noqa: PLC0415
        DEFAULT_REFOLD_N, MAX_REFOLD_N, can_refold, candidate_seq_from_record,
    )
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    from shared.jobs import get_job  # noqa: PLC0415
    from blueprints.jobs import _spawn_refold_job  # noqa: PLC0415

    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    campaign = cc.get_campaign(campaign_id, user_id=ctx.user_id)
    if campaign is None:
        return render_template("404.html"), 404

    dest_tool = (request.form.get("dest_tool") or "").strip()
    try:
        n_raw = int(request.form.get("n") or DEFAULT_REFOLD_N)
    except ValueError:
        n_raw = DEFAULT_REFOLD_N
    n = max(1, min(n_raw, MAX_REFOLD_N))

    detail = url_for("campaigns.compute_campaign_detail", campaign_id=campaign_id)
    if not can_refold(campaign.tool, dest_tool) or not tool_enabled(dest_tool):
        return redirect(detail)
    dest_adapter = tool_base.get(dest_tool)
    if dest_adapter is None:
        return redirect(detail)

    # The global top-N designs with a sequence (headroom slice because a top
    # design could in theory lack one). "Top N" = by the campaign's ranking,
    # so we attempt exactly those, not backfill from lower ranks.
    agg = cc.aggregate_campaign_candidates(
        campaign_id, user_id=ctx.user_id, limit=max(n * 2, MAX_REFOLD_N),
    )
    seqs_with_src: list[tuple] = []
    for idx, cand in enumerate(agg.get("candidates", [])):
        if len(seqs_with_src) >= n:
            break
        cs = candidate_seq_from_record(cand, idx)
        if cs is not None:
            seqs_with_src.append((cs, cand.get("_source_job_id")))

    campaign_label = f"validation-of-campaign-{campaign_id[:8]}"
    src_cache: dict = {}
    spawned: list[str] = []
    for cs, src_job_id in seqs_with_src:
        if not src_job_id:
            continue
        src_job = src_cache.get(src_job_id)
        if src_job is None:
            src_job = get_job(src_job_id, user_id=ctx.user_id)
            if src_job is None:
                continue
            src_cache[src_job_id] = src_job
        jid = _spawn_refold_job(
            ctx, dest_adapter, dest_tool, cs, src_job, campaign_label,
            antigen_storage_path=campaign.target_storage_path,
        )
        if jid is not None:
            spawned.append(jid)

    if not spawned:
        return redirect(detail)

    from shared.events import EVENTS, emit  # noqa: PLC0415
    emit(
        EVENTS.REFOLD_SPAWNED,
        user_id=ctx.user_id,
        properties={
            "source_tool": campaign.tool,
            "dest_tool": dest_tool,
            "n": len(spawned),
            "campaign_id": campaign_id,
        },
    )
    return redirect(url_for("jobs.jobs_compare", ids=",".join(spawned)))


@campaigns_bp.route("/campaigns/<campaign_id>/cancel", methods=["POST"])
@login_required
def compute_campaign_cancel(campaign_id):
    ctx = load_user_context()
    if ctx is None:
        return jsonify({"error": "auth"}), 401
    from shared import compute_campaigns as cc  # noqa: PLC0415
    ok = cc.cancel_campaign(campaign_id, ctx.user_id)
    if request.is_json or request.headers.get("X-CSRF-Token"):
        return jsonify({"ok": ok})
    return redirect(url_for("campaigns.compute_campaign_detail", campaign_id=campaign_id))

# -- Legacy URL redirects (launch cutover 2026-07) -------------------
# The compute product moved /runs/* -> /campaigns/* and the wet-lab
# funnel moved /campaigns/* -> /lab-projects/* (admin too). Links live in
# already-sent emails and user bookmarks, so the vacated GET paths 301 to
# their new homes, preserving any query string. JS-only poll/cancel paths
# are intentionally not stubbed (a stale tab self-heals on refresh). The
# old wet-lab /campaigns/<id> path is now the compute detail route; that
# collision is resolved inside compute_campaign_detail.

def _cutover_redirect(endpoint, **values):
    target = url_for(endpoint, **values)
    qs = request.query_string.decode("utf-8")
    if qs:
        target = f"{target}?{qs}"
    return redirect(target, code=301)

@campaigns_bp.route("/runs", methods=["GET"])
def legacy_runs_list():
    return _cutover_redirect("campaigns.compute_campaigns_list")

@campaigns_bp.route("/runs/new", methods=["GET"])
def legacy_runs_new():
    return _cutover_redirect("campaigns.compute_campaign_new")

@campaigns_bp.route("/api/runs/estimate", methods=["GET"])
def legacy_runs_estimate():
    return _cutover_redirect("campaigns.api_runs_estimate")

@campaigns_bp.route("/runs/<campaign_id>", methods=["GET"])
def legacy_runs_detail(campaign_id):
    return _cutover_redirect("campaigns.compute_campaign_detail", campaign_id=campaign_id)

@campaigns_bp.route("/admin/campaigns", methods=["GET"])
def legacy_admin_campaigns_list():
    return _cutover_redirect("admin.admin_campaigns_list")

@campaigns_bp.route("/admin/campaigns/<campaign_id>", methods=["GET"])
def legacy_admin_campaign_detail(campaign_id):
    return _cutover_redirect("admin.admin_campaign_detail", campaign_id=campaign_id)
