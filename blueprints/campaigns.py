"""Compute campaign routes (blueprint refactor, Commit 4).

Self-serve batched design at /campaigns/* + /api/campaigns/* plus the
/runs/* and /admin/campaigns/* legacy 301 redirects. Lifted verbatim from
``create_app()``; only ``@flask_app.route`` -> ``@campaigns_bp.route`` and
compute-campaign endpoint self-refs -> ``campaigns.*``. The three helpers
(_campaign_preauth_message, _campaign_passed_filters, _cutover_redirect)
move in with the routes.
"""

from __future__ import annotations

import logging

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
from shared.pdb_inspect import (
    CifConversionError,
    convert_cif_to_pdb_bytes,
    inspect_pdb_bytes,
    validate_target_chain,
)
from shared.storage import StorageError, upload_input
from tools import base as tool_base

logger = logging.getLogger(__name__)

campaigns_bp = Blueprint("campaigns", __name__)


# -- Compute campaigns ("Campaigns") ---------------------------------
# Self-serve batched design: split a large request into many sub-jobs.
# Served at /campaigns/* (the customer-facing product noun). The older
# wet-lab funnel moved to /lab-projects/* in the launch cutover; the old
# /runs/* compute paths 301-redirect here for already-sent email links.

_CAMPAIGN_PREAUTH_MESSAGES = {
    "wallet_unavailable": "Your wallet is unavailable. Try again in a moment.",
    "wallet_frozen": "Your wallet is on hold. Contact support to resume.",
    "insufficient_balance": (
        "Your balance does not cover the first batch of this campaign "
        "(about ${required} to start). Top up your wallet and try again. "
        "You only pay for compute that runs, and the campaign pauses if "
        "your balance runs low."
    ),
    "verification_required": (
        "Campaigns above ${threshold} need an approved account. "
        "Contact us to raise your limit."
    ),
    "daily_campaign_cap": (
        "This would exceed your daily campaign spending limit. "
        "Try again tomorrow or with a smaller campaign."
    ),
}

def _campaign_preauth_message(pre) -> str:
    from shared import compute_campaigns as _cc  # noqa: PLC0415
    msg = _CAMPAIGN_PREAUTH_MESSAGES.get(
        pre.reason, "This campaign cannot start right now."
    )
    required = getattr(pre, "required_usd", None)
    return (
        msg.replace("${threshold}", str(_cc.VERIFICATION_THRESHOLD_USD))
           .replace("${required}", f"{required:.2f}" if required else "the first batch")
    )


# Campaign tools that ship behind a FLAG_TOOL_<NAME> gate: hidden from the
# create form + rejected on POST/estimate until the operator flips the flag in
# prod (mirrors the atomic-tool flag-gating). The 5 original campaign tools are
# unconditionally live and are NOT filtered by tool_enabled (which is
# fail-closed — a tool with no flag env reads as off — so filtering the whole
# list would wrongly hide them).
_FLAG_GATED_CAMPAIGN_TOOLS = frozenset({"proteina", "iggm"})


def _visible_campaign_tools() -> tuple:
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    return tuple(
        t for t in cc.SUPPORTED_TOOLS
        if t not in _FLAG_GATED_CAMPAIGN_TOOLS or tool_enabled(t)
    )


def _campaign_tool_gated_off(tool: str) -> bool:
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    return tool in _FLAG_GATED_CAMPAIGN_TOOLS and not tool_enabled(tool)


def _sdf_sanity(data: bytes, filename: str) -> str | None:
    """Cheap pre-GPU check that an uploaded SDF is a plausible molfile.

    Full RDKit sanitization + the SDF -> chain-A HETATM/CONECT PDB conversion
    run in-container (RDKit is not in the web tier), so this only rejects
    obvious junk before staging: a size bound plus a parseable V2000/V3000
    counts line declaring at least one atom. Returns an error string or None.
    """
    if len(data) > 2_000_000:
        return "SDF file is too large (max 2 MB)."
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    if len(lines) < 4:
        return "SDF file is too short to be a valid molfile."
    counts = lines[3]
    if "V3000" in counts:
        natoms = None
        for ln in lines:
            if "V30 COUNTS" in ln:
                parts = ln.split()
                try:
                    natoms = int(parts[parts.index("COUNTS") + 1])
                except (ValueError, IndexError):
                    natoms = None
                break
        if not natoms or natoms < 1:
            return "SDF has no atoms (empty V3000 molfile)."
    else:
        try:
            natoms = int(counts[:3])
        except ValueError:
            return "SDF counts line is malformed (not a V2000/V3000 molfile)."
        if natoms < 1:
            return "SDF has no atoms (empty molfile)."
    return None

@campaigns_bp.route("/campaigns", methods=["GET"])
@login_required
def compute_campaigns_list():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    from shared import compute_campaigns as cc  # noqa: PLC0415
    campaigns = cc.list_campaigns_for_user(ctx.user_id)
    return render_template("runs/list.html", campaigns=campaigns)

@campaigns_bp.route("/campaigns/new", methods=["GET"])
@login_required
def compute_campaign_new():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    from shared import compute_campaigns as cc  # noqa: PLC0415
    return render_template(
        "runs/new.html",
        supported_tools=_visible_campaign_tools(),
        max_subjobs=cc.MAX_SUBJOBS_PER_CAMPAIGN,
        verification_threshold=str(cc.VERIFICATION_THRESHOLD_USD),
        pre_fill={},
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
    if _campaign_tool_gated_off(tool):
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
        "per_chunk_usd": str(plan.est_cost_per_chunk),
        "budget_usd": str(plan.budget_usd),
        "first_wave_usd": str(first_wave),
        "balance_usd": str(pre.balance_usd),
        "affordable": pre.ok,
        "reason": pre.reason,
        "needs_verification": cc.CAMPAIGN_KYC_ENABLED and (plan.budget_usd > cc.VERIFICATION_THRESHOLD_USD),
    })

@campaigns_bp.route("/campaigns", methods=["POST"])
@login_required
def compute_campaign_create():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    from shared import compute_campaigns as cc  # noqa: PLC0415

    tool = (request.form.get("tool") or "").strip()
    name = (request.form.get("name") or "").strip()
    preset = (request.form.get("preset") or "pilot").strip() or "pilot"
    try:
        requested = int(request.form.get("requested_designs") or "0")
    except ValueError:
        requested = 0

    def _err(msg, code=400):
        return render_template(
            "runs/new.html",
            # Filtered so a flag-gated tool (proteina) is not leaked into the
            # dropdown on a validation-error re-render, matching the GET form.
            supported_tools=_visible_campaign_tools(),
            max_subjobs=cc.MAX_SUBJOBS_PER_CAMPAIGN,
            verification_threshold=str(cc.VERIFICATION_THRESHOLD_USD),
            error=msg,
            pre_fill=request.form.to_dict(),
        ), code

    # 0. Resolve the adapter + preset up front. The 5 live campaign tools each
    #    carry a single "pilot" preset (the default here), so their behaviour is
    #    unchanged; proteina posts one of its design variants. An unknown preset
    #    would mis-size the chunk plan, so reject it before planning. A
    #    flag-gated tool that is still off is treated as unknown (don't reveal a
    #    hidden tool exists) — defense-in-depth behind the dropdown filter.
    adapter = tool_base.get(tool)
    if adapter is None or _campaign_tool_gated_off(tool):
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
    validated, verr = adapter.validate(form_for_validate, request.files)
    if validated is None:
        return _err(verr or "Invalid parameters.")

    # 3. Resolve the campaign target. The live tools + proteina's protein/motif
    #    variants take a PDB (inspected + chain-validated); proteina's ligand
    #    variant takes an SDF (cheap sanity only; the RDKit -> chain-A PDB
    #    conversion happens in-container). For proteina a target is OPTIONAL — a
    #    curated benchmark task carries its own target, so no upload means "run
    #    the task's built-in target". The 5 live tools keep the mandatory-PDB
    #    path exactly as before.
    is_proteina = tool == "proteina"
    is_ligand = is_proteina and validated.get("preset") == "ligand_binder"
    uploaded = (
        request.files.get("target_sdf") if is_ligand
        else request.files.get("target_pdb")
    )
    staged_bytes = b""
    staged_filename = ""
    staged_content_type = "chemical/x-pdb"

    if uploaded is None or not uploaded.filename:
        if not is_proteina:
            return _err("Upload a target PDB file.")
        # proteina + no upload: curated-task path, no staged target file.
    elif is_ligand:
        sdf_bytes = uploaded.read()
        sdf_err = _sdf_sanity(sdf_bytes, uploaded.filename)
        if sdf_err:
            return _err(sdf_err)
        staged_bytes = sdf_bytes
        staged_filename = uploaded.filename
        staged_content_type = "chemical/x-mdl-sdfile"
    else:
        pdb_bytes = uploaded.read()
        inspection = inspect_pdb_bytes(pdb_bytes, filename=uploaded.filename)
        if not inspection.ok:
            return _err(inspection.error)
        # iggm names its antigen chain ``antigen_chain`` (it reads the form's
        # ``target_chain`` but stores it under that key); the other PDB tools use
        # ``target_chain``. Check whichever the adapter produced so the antigen
        # chain is validated against the uploaded PDB before any GPU spend.
        target_chain = (
            validated.get("target_chain") or validated.get("antigen_chain") or ""
        ).strip()
        if target_chain:
            chain_err = validate_target_chain(inspection, target_chain)
            if chain_err:
                return _err(chain_err)
        staged_filename = uploaded.filename
        fl = uploaded.filename.lower()
        if fl.endswith(".cif") or fl.endswith(".mmcif"):
            try:
                pdb_bytes = convert_cif_to_pdb_bytes(pdb_bytes, uploaded.filename)
            except CifConversionError as exc:
                return _err(str(exc))
            staged_filename = uploaded.filename.rsplit(".", 1)[0] + ".pdb"
        staged_bytes = pdb_bytes

    # 4. Prepaid START gate (checks, never debits): the wallet only has to
    #    cover the first wave; the rest funds as the campaign drains, and it
    #    pauses/resumes on balance (fund-and-drain).
    pre = cc.campaign_preauth(
        ctx.user_id, plan.budget_usd,
        cc.first_wave_hold_usd(plan, cc.launch_concurrency_for(tool)),
    )
    if not pre.ok:
        return _err(_campaign_preauth_message(pre))

    # 5. Stage the shared target once (when one was provided), then create +
    #    fund + first wave. A proteina curated-task run stages nothing.
    staged_path = None
    if staged_filename:
        import uuid as _uuid  # noqa: PLC0415
        target_key = f"campaign-{_uuid.uuid4().hex}"
        try:
            staged_path = upload_input(
                user_id=ctx.user_id, job_id=target_key,
                filename=staged_filename, data=staged_bytes,
                content_type=staged_content_type,
            )
        except StorageError as exc:
            return _err(f"Upload failed: {exc}")

    campaign = cc.create_campaign(
        user_id=ctx.user_id, tool=tool, params=validated,
        requested_designs=requested, preset=preset, name=name or None,
        target_storage_path=staged_path,
        target_name=(request.form.get("target_name") or "").strip() or None,
    )
    if campaign is None:
        return _err("Could not create the campaign. Try again in a moment.")

    cc.fund_campaign(campaign.id)
    # Kick the first wave off the request path (daemon thread); the cron
    # tick backstops if the thread dies. At the raised concurrency an inline
    # drive would make many Modal + Supabase round-trips before responding.
    cc.drive_campaign_async(campaign.id)
    return redirect(url_for("campaigns.compute_campaign_detail", campaign_id=campaign.id))

@campaigns_bp.route("/campaigns/<campaign_id>", methods=["GET"])
@login_required
def compute_campaign_detail(campaign_id):
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    from shared import compute_campaigns as cc  # noqa: PLC0415
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
    return render_template("runs/detail.html", campaign=campaign, counts=counts)

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
    try:
        rows = (
            client.table("tool_jobs")
            .select("result")
            .eq("campaign_id", campaign_id)
            .eq("status", "succeeded")
            .execute()
            .data
            or []
        )
    except Exception:
        return 0
    from shared.jobs import count_passed_candidates  # noqa: PLC0415
    return sum(count_passed_candidates(r.get("result")) for r in rows)

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
