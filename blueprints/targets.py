"""Design target routes: upload a structure once, run many tools against it.

Phases 1 and 2 of the target-first rework. A target is created, listed,
viewed, and archived here, and :func:`target_launch` fans up to seven tools at
one target in a single gated action. The single-tool create form
(``/campaigns/new?target_id=``) still exists and still works; it skips staging
and inherits the target's already-staged path.

Ownership: every route resolves its target through
``shared.targets.get_target(..., user_id=ctx.user_id)`` BEFORE touching a
storage path. ``copy_input`` / ``download_input`` take ``user_id`` as a path
component and perform no authorization of their own, so that owner-scoped
fetch is the entire tenancy boundary.

Money: the launch route passes ONE summed start gate for the whole selection
(``shared.target_launch``). It never loops ``campaign_preauth``, which is a
pure check with no debit and would therefore pass N times on a balance that
funds one. Nothing is created until every selected tool has cleared its own
``adapter.validate()``, and everything is created ``draft`` before anything is
funded, so a failure part way leaves rows that were never funded, never
dispatched and never billed.
"""

from __future__ import annotations

import logging
import uuid

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from shared.auth import login_required
from shared.credits import load_user_context
from shared.idempotency import idempotent
from shared.pdb_intake import resolve_target_upload
from shared.storage import StorageError
from shared.targets import (
    archive_target,
    create_target,
    find_target_by_sha256,
    get_target,
    list_targets_for_user,
    target_defaults_for_form,
    touch_target,
    unarchive_target,
)
from tools import base as tool_base

logger = logging.getLogger(__name__)

targets_bp = Blueprint("targets", __name__)

# Generous: a real epitope is ~15-25 residues and a hotspot set smaller still.
# The cap only exists so a pasted spreadsheet column cannot become a 10k-element
# Postgres array.
_MAX_RESIDUES = 256

# Rows per section on the targets page. The page does not paginate, so this is
# also the point past which a target is reachable only by URL; the template
# says so when a section hits it rather than truncating in silence.
_LIST_LIMIT = 100


# ---------------------------------------------------------------------------
# Multi-tool launch (Phase 2)
# ---------------------------------------------------------------------------

# Form fields shared by every tool on the launch screen. Everything else is
# namespaced ``<tool>__<field>`` so two tools can want different values for the
# same concept without colliding.
_SHARED_LAUNCH_FIELDS = ("target_chain", "hotspot_residues")

# Tools whose preset is a real user choice (a design VARIANT, not a tier). For
# every other campaign tool the server fixes the preset; see _resolve_preset.
_VARIANT_PRESET_TOOLS = frozenset({"proteina", "iggm"})

# The tier the five non-variant campaign tools all carry.
_PILOT_PRESET = "pilot"

_DEFAULT_VARIANT_PRESET = {
    "proteina": "protein_binder",
    "iggm": "complex_prediction",
}

# Presets refused on the launch path, with the reason. Both are refused on the
# single-tool create route too; repeated here because this route does not go
# through it.
_REFUSED_PRESETS = {
    ("iggm", "affinity_maturation"): (
        "affinity maturation is not available as a campaign (it runs one "
        "design per masked position, so the delivered count stops matching "
        "the chunk size). Use the single-run IgGM form."
    ),
    ("proteina", "ligand_binder"): (
        "the ligand variant needs a small-molecule SDF, and this target is a "
        "protein structure."
    ),
}


def _tool_label(adapter) -> str:  # noqa: ANN001
    """Short display name for an error message.

    Adapter labels carry a trailing "— one line about the tool"; keep the name
    only, matching ``shared.tools_catalog``. A seven-tool form answering
    "Invalid parameters." with no idea which tool is unusable.
    """
    return str(adapter.label or "").split("—")[0].strip() or adapter.slug


def _resolve_preset(tool: str, form) -> str:  # noqa: ANN001
    """The preset this run will use. Read from the form only where it is a
    real choice.

    For the five pilot tools the server SETS it and never trusts the client.
    That is not defensive dressing: the launch form renders no preset control
    for those tools at all, and bindcraft's validator has no internal default
    -- it reads ``(form.get("preset") or "").strip()`` and rejects anything
    that is not exactly "pilot" (``tools/bindcraft/__init__.py:25-27``).
    Deriving it here is what makes bindcraft launchable from this screen, and
    it closes the same hole for any future adapter that omits a default. The
    single-tool create route had the same gap and returned 400 on every
    bindcraft campaign until Phase 2 fixed it there too.
    """
    if tool in _VARIANT_PRESET_TOOLS:
        raw = (form.get(f"{tool}__preset") or "").strip()
        return raw or _DEFAULT_VARIANT_PRESET.get(tool, _PILOT_PRESET)
    return _PILOT_PRESET


def _tool_form(tool: str, form) -> dict:  # noqa: ANN001
    """Build one tool's validation dict: shared block + its own fields.

    The caller strips the ``<tool>__`` prefix here and nowhere else, so an
    adapter never learns it was part of a multi-tool submission.

    This is also the security boundary, NOT the template's ``disabled``
    attributes: a dict is built only for a tool the user actually selected, so
    a crafted post carrying ``iggm__fasta`` while selecting only rfdiffusion
    contributes nothing. Each adapter then returns a freshly built whitelist,
    so unknown extras cannot reach the stored params either.
    """
    out = {key: form.get(key, "") for key in _SHARED_LAUNCH_FIELDS}
    prefix = f"{tool}__"
    for key in form.keys():
        if key.startswith(prefix):
            out[key[len(prefix):]] = form.get(key)
    return out


def _collect_launch_specs(target, form) -> "tuple[list, str | None]":  # noqa: ANN001
    """Validate every selected tool. Returns ``(specs, error)``.

    All or nothing: the first failure returns ``(None, message)`` and the
    caller creates nothing. This, not the form UI, is the guard against paying
    for a mis-configured GPU run, so every check that the single-tool create
    route performs has to happen here too.
    """
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.target_launch import ToolLaunchSpec  # noqa: PLC0415

    tools = [t.strip() for t in form.getlist("tools") if t.strip()]
    if not tools:
        return None, "Pick at least one tool to run against this target."
    if len(set(tools)) != len(tools):
        # The form cannot produce a duplicate, so this is a crafted post.
        # Dropping it silently would hide a doubled bill; honouring it would
        # create one.
        return None, "That request listed the same tool twice."
    if len(tools) > len(cc.SUPPORTED_TOOLS):
        return None, "Too many tools selected."

    specs = []
    for tool in tools:
        adapter = tool_base.get(tool)
        # A gated-off tool answers exactly as an unknown one does, so a probe
        # cannot learn that a hidden tool exists.
        if adapter is None or tool not in cc.SUPPORTED_TOOLS or (
            cc.campaign_tool_gated_off(tool)
        ):
            return None, "Unknown tool."
        label = _tool_label(adapter)

        preset = _resolve_preset(tool, form)
        if adapter.preset_for(preset) is None:
            return None, f"{label}: unknown preset for this tool."
        if preset == "validate":
            return None, (
                f"{label}: the validate tier is a free pre-flight, not a "
                "paid run."
            )
        refusal = _REFUSED_PRESETS.get((tool, preset))
        if refusal:
            return None, f"{label}: {refusal}"

        raw_designs = (form.get(f"{tool}__designs") or "").strip()
        try:
            designs = int(raw_designs)
        except ValueError:
            return None, f"{label}: number of designs must be a whole number."

        # Planned per tool even though plan_multi_launch plans again below.
        # Not redundant: `design_param_key` is needed right here to inject the
        # placeholder count before validate(), and planning per tool is what
        # lets a sizing error name the tool that caused it. It is not free
        # either -- plan_chunks reaches the historical-p90 read -- but this is
        # a POST, not the keystroke path, and a mis-sized launch that says
        # only "too many sub-jobs" across seven tools is unactionable.
        try:
            plan = cc.plan_chunks(tool, designs, preset)
        except ValueError as exc:
            return None, f"{label}: {exc}"

        tool_form = _tool_form(tool, form)
        tool_form["preset"] = preset
        # The driver injects the real per-chunk count; the adapter only needs
        # an in-cap placeholder to get past its own bounds check.
        tool_form[plan.design_param_key] = "1"
        validated, verr = adapter.validate(tool_form, request.files)
        if validated is None:
            return None, f"{label}: {verr or 'invalid parameters.'}"

        # Chain and hotspots are per-RUN overrides of the target's defaults, so
        # they still have to be checked -- against the inspection persisted at
        # upload time, so no download and no re-parse. Nothing else on this
        # path validates them: the structure is never re-uploaded, so
        # resolve_target_upload never runs.
        run_chain = (
            validated.get("target_chain") or validated.get("antigen_chain") or ""
        )
        chain_err = target.chain_error(run_chain)
        if chain_err:
            return None, f"{label}: {chain_err}"
        # Both keys are original PDB author numbering. iggm calls its epitope
        # ``epitope_pdb_resnums``; every other campaign tool calls its hotspots
        # ``hotspot_residues``.
        hotspot_err = target.hotspot_error(
            run_chain,
            (validated.get("hotspot_residues") or [])
            + (validated.get("epitope_pdb_resnums") or []),
        )
        if hotspot_err:
            return None, f"{label}: {hotspot_err}"

        specs.append(
            ToolLaunchSpec(
                tool=tool,
                preset=preset,
                requested_designs=designs,
                params=validated,
            )
        )
    return specs, None


def _launch_context(target, **overrides) -> dict:  # noqa: ANN001
    """Everything ``targets/launch.html`` needs, for both GET and re-render."""
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.target_launch import PACE_BURST  # noqa: PLC0415

    tools = cc.visible_campaign_tools()
    context = {
        "target": target,
        "tools": tools,
        "labels": {t: _tool_label(tool_base.get(t)) for t in tools},
        # One chunk each: the smallest launch that still produces a full
        # container's worth of designs per tool, and the honest default for a
        # screen whose first-wave gate scales with the selection.
        "chunk_sizes": {t: cc.single_container_ceiling(t) for t in tools},
        # Intersected with the visible set, not the raw constant. This goes
        # into the page as JSON for the estimate JS, so shipping the constant
        # would print the slug of every flag-gated tool to users who cannot see
        # it -- undoing, in a script tag, the whole reason a gated tool is
        # answered as "unknown" everywhere else.
        "variant_preset_tools": sorted(_VARIANT_PRESET_TOOLS & set(tools)),
        "pace_default": PACE_BURST,
        "max_subjobs": cc.MAX_SUBJOBS_PER_CAMPAIGN,
        "pre_fill": target_defaults_for_form(target),
        "selected_tools": [],
        "error": None,
        "blocked": None,
    }
    context.update(overrides)
    return context


def _launch_blocker(target) -> "str | None":  # noqa: ANN001
    """Why this target cannot be launched against at all, or None.

    Distinct from the archived case, which redirects: the detail page renders
    an archived explainer and a Restore button, so it has somewhere useful to
    send the user. It has nothing to say about a live target with no staged
    structure outside its archived branch, so that one is explained here.
    """
    if not target.storage_path:
        return (
            "This target has no stored structure, so there is nothing to run "
            "a tool against. Upload the structure as a new target."
        )
    if target.kind != "pdb":
        return (
            "The campaign tools all take a protein structure, and this target "
            "is a small molecule."
        )
    return None


def _parse_residue_list(raw: str) -> "tuple[list, str | None]":
    """Parse "32, 45, 58" into ``[32, 45, 58]``.

    Returns ``(residues, error)``. Rejects rather than silently dropping a
    non-numeric entry: a typo'd hotspot that vanishes here would be a target
    that quietly aims somewhere else than the user asked for.
    """
    text = (raw or "").replace(";", ",").strip()
    if not text:
        return [], None
    out: list = []
    for piece in text.replace(",", " ").split():
        try:
            out.append(int(piece))
        except ValueError:
            return [], f"'{piece}' is not a residue number."
    if len(out) > _MAX_RESIDUES:
        return [], f"Too many residues (max {_MAX_RESIDUES})."
    return out, None


@targets_bp.route("/targets", methods=["GET"])
@login_required
def targets_list():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    # Two reads, not one mixed read: the live query is what migration 0039's
    # partial index covers, and putting archived rows in the same capped page
    # would let a user with many archived targets push their live ones off it.
    #
    # Both reads are capped and neither paginates, so a user past the cap has
    # targets this page cannot show. That is disclosed rather than hidden:
    # this section is otherwise the only route to an archived target, so a
    # silent truncation here is indistinguishable from a deleted target.
    #
    # Both reads ask for one row MORE than they render. A page holding exactly
    # _LIST_LIMIT rows is ambiguous -- it is either all of them or the first
    # of many -- so testing len() against the limit itself makes the banner
    # claim there are older targets when there are none. The extra row is the
    # only thing that distinguishes the two, and it is dropped before render.
    live = list_targets_for_user(ctx.user_id, limit=_LIST_LIMIT + 1)
    archived = list_targets_for_user(
        ctx.user_id, archived_only=True, limit=_LIST_LIMIT + 1
    )
    return render_template(
        "targets/list.html",
        targets=live[:_LIST_LIMIT],
        archived=archived[:_LIST_LIMIT],
        targets_capped=len(live) > _LIST_LIMIT,
        archived_capped=len(archived) > _LIST_LIMIT,
        list_limit=_LIST_LIMIT,
    )


@targets_bp.route("/targets/new", methods=["GET"])
@login_required
def target_new():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    return render_template("targets/new.html", pre_fill={})


@targets_bp.route("/targets", methods=["POST"])
@login_required
@idempotent()
def target_create():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    def _err(msg, code=400, duplicate=None):
        return render_template(
            "targets/new.html",
            error=msg,
            duplicate=duplicate,
            pre_fill=request.form.to_dict(),
        ), code

    target_chain = (request.form.get("target_chain") or "").strip()
    hotspots, hs_err = _parse_residue_list(request.form.get("hotspot_residues"))
    if hs_err:
        return _err(f"Hotspot residues: {hs_err}")
    epitope, ep_err = _parse_residue_list(request.form.get("epitope_residues"))
    if ep_err:
        return _err(f"Epitope residues: {ep_err}")

    uploaded = request.files.get("target_pdb")
    if uploaded is None or not uploaded.filename:
        return _err("Upload a target structure (.pdb / .cif).")

    upload, upload_err = resolve_target_upload(
        uploaded, target_chain=target_chain, kind="pdb",
    )
    if upload is None:
        return _err(upload_err or "That structure could not be read.")

    # Offer an existing target with the same content rather than splitting one
    # protein's results across two unlinked targets, which is exactly what the
    # combined table exists to prevent. The user can still insist: posting
    # allow_duplicate=1 creates the second target.
    if not request.form.get("allow_duplicate"):
        existing = find_target_by_sha256(ctx.user_id, upload.sha256)
        if existing is not None:
            return _err(
                "You have already uploaded this structure.", 400, existing,
            )

    try:
        target = create_target(
            user_id=ctx.user_id,
            upload=upload,
            name=request.form.get("name"),
            target_chain=target_chain,
            hotspot_residues=hotspots,
            epitope_residues=epitope,
            notes=request.form.get("notes"),
            source="upload",
        )
    except StorageError as exc:
        return _err(f"Upload failed: {exc}")
    if target is None:
        return _err("Could not save the target. Try again in a moment.")

    return redirect(url_for("targets.target_detail", target_id=target.id))


@targets_bp.route("/targets/<target_id>", methods=["GET"])
@login_required
def target_detail(target_id):
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    target = get_target(target_id, user_id=ctx.user_id)
    if target is None:
        return render_template("404.html"), 404

    from shared import compute_campaigns as cc  # noqa: PLC0415
    # Phase 1 shows this target's COMPUTE-CAMPAIGN runs. Standalone jobs that
    # carry target_id with campaign_id NULL (the `target:` reuse token, and
    # Phase 4's yardstick refolds) are not shown: reading both tables is
    # Phase 3's fan-in. Currently invisible rather than wrong, because no
    # template mints a `target:` token yet. The combined ranked table over all
    # of them is also Phase 3; until then each run links to its own results
    # page.
    #
    # One server-side read filtered on target_id. This previously fetched the
    # target's run ids and then intersected them with the user's 200 most
    # recent campaigns, which is capped over their ENTIRE campaign history: a
    # target whose runs all fell outside that window rendered the empty state,
    # telling the user nothing had ever been run against a target they had paid
    # to run against.
    # Drafts are excluded (see list_campaigns_for_target): a stranded draft was
    # never funded, dispatched or billed, so it is not a run, and there is no
    # action the page could offer on it.
    runs = cc.list_campaigns_for_target(target.id, user_id=ctx.user_id)

    # "You just launched N runs" after a redirect from the launch screen. This
    # app has no flash(), so the result rides the query string. Counted from
    # rows already loaded, so it costs no extra read. An unknown or foreign
    # group matches nothing and the banner simply does not render; a crafted
    # `stalled` only misinforms whoever crafted it.
    launched_group = (request.args.get("launched") or "").strip()
    launched_runs = (
        [r for r in runs if launched_group and r.launch_group_id == launched_group]
        if launched_group else []
    )
    try:
        stalled_count = max(0, int(request.args.get("stalled") or 0))
    except ValueError:
        stalled_count = 0
    return render_template(
        "targets/detail.html",
        target=target,
        runs=runs,
        launched_count=len(launched_runs),
        stalled_count=stalled_count,
    )


@targets_bp.route("/targets/<target_id>/launch", methods=["GET"])
@login_required
def target_launch(target_id):
    """The multi-tool launch screen: pick tools, see one itemised estimate."""
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    target = get_target(target_id, user_id=ctx.user_id)
    if target is None:
        return render_template("404.html"), 404
    # An archived target is not launchable anywhere (the run-create route and
    # the atomic form both reject it), and its structure is excluded from the
    # retention sweeper's protected set, so it may already be gone. The detail
    # page renders that state and offers Restore, which is the only useful
    # action, so send the user there rather than to a form that will refuse.
    if target.is_archived:
        return redirect(url_for("targets.target_detail", target_id=target.id))
    return render_template(
        "targets/launch.html",
        **_launch_context(target, blocked=_launch_blocker(target)),
    )


@targets_bp.route("/api/targets/<target_id>/launch-estimate", methods=["GET"])
@login_required
def api_target_launch_estimate(target_id):
    """Itemised per-tool estimate for the launch screen.

    Only three scalars per tool are priced -- tool, preset, and design count --
    because that is all ``plan_multi_launch`` consumes; the validated params
    ride along on the spec but never affect the figures. So this stays a GET
    with three index-aligned repeated params rather than serialising seven
    parameter panels.

    Always HTTP 200 with ``{"ok": bool}``, and money as ``str(Decimal)``,
    mirroring the single-tool estimate endpoint so two sibling money APIs
    cannot disagree about either shape.
    """
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.target_launch import (  # noqa: PLC0415
        PACE_BURST,
        PACE_STEADY,
        ToolLaunchSpec,
        concurrency_note,
        first_wave_at_pace,
        plan_multi_launch,
        preauth_multi_launch,
    )

    ctx = load_user_context()
    if ctx is None:
        return jsonify({"ok": False, "error": "Sign in to see an estimate."})
    target = get_target(target_id, user_id=ctx.user_id)
    if target is None:
        return jsonify({"ok": False, "error": "That target could not be found."})

    tools = request.args.getlist("tool")
    designs = request.args.getlist("designs")
    presets = request.args.getlist("preset")
    # Index-aligned, so a length mismatch cannot be zipped away: zip() would
    # silently price fewer tools than the POST goes on to launch, which is the
    # one failure mode that ends in a bill the user never saw.
    if not (len(tools) == len(designs) == len(presets)):
        return jsonify({"ok": False, "error": "Malformed estimate request."})
    if not tools:
        return jsonify({"ok": False, "error": "Pick at least one tool."})
    if len(tools) > len(cc.SUPPORTED_TOOLS) or len(set(tools)) != len(tools):
        return jsonify({"ok": False, "error": "Malformed estimate request."})

    pace = request.args.get("pace") or PACE_BURST
    if pace not in (PACE_BURST, PACE_STEADY):
        pace = PACE_BURST

    specs = []
    for tool, raw_designs, preset in zip(tools, designs, presets):
        # The same rejections the POST applies. An estimate that prices a
        # combination the launch will refuse is worse than no estimate.
        adapter = tool_base.get(tool)
        if adapter is None or tool not in cc.SUPPORTED_TOOLS or (
            cc.campaign_tool_gated_off(tool)
        ):
            return jsonify({"ok": False, "error": "That tool is not available."})
        label = _tool_label(adapter)
        if adapter.preset_for(preset) is None or preset == "validate":
            return jsonify(
                {"ok": False, "error": f"{label}: unknown preset for this tool."}
            )
        refusal = _REFUSED_PRESETS.get((tool, preset))
        if refusal:
            return jsonify({"ok": False, "error": f"{label}: {refusal}"})
        try:
            count = int(raw_designs)
        except ValueError:
            return jsonify(
                {"ok": False,
                 "error": f"{label}: number of designs must be a whole number."}
            )
        specs.append(
            ToolLaunchSpec(
                tool=tool, preset=preset, requested_designs=count, params={},
            )
        )

    try:
        plan = plan_multi_launch(specs, pace)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)})

    pre = preauth_multi_launch(ctx.user_id, plan)

    payload = {
        "ok": True,
        "pace": plan.pace,
        "rows": plan.rows(),
        "total_designs": plan.total_designs,
        "total_subjobs": plan.total_subjobs,
        "budget_usd": str(plan.budget_usd),
        "first_wave_usd": str(plan.first_wave_usd),
        "balance_usd": str(pre.balance_usd),
        "affordable": pre.ok,
        "reason": pre.reason,
        "needs_verification": (
            cc.CAMPAIGN_KYC_ENABLED
            and plan.budget_usd > cc.VERIFICATION_THRESHOLD_USD
        ),
        "concurrency_note": concurrency_note(plan),
    }
    # Offer the narrower start when the wide one is unaffordable, so a refused
    # user is told the thing that would actually work rather than just "top
    # up". Re-priced from the plan already in hand, NOT re-planned: this
    # endpoint is a debounced keystroke handler and pricing reaches Supabase
    # (see the shared.target_launch module docstring). Measured at 7 tools: 28
    # reads per estimate with the shortcut, 35 with a second plan_multi_launch.
    #
    # The comparison is exactly the BALANCE test campaign_preauth applies, so
    # the alternative cannot be offered on a balance that would not cover it.
    # It does not re-run the other two gates: a launch also refused by the
    # daily velocity cap would be offered a narrower start and then refused
    # again, because the budget is pace-independent. Reachable only when that
    # cap binds, and the alternative is still true about the balance.
    if not pre.ok and pre.reason == cc.PREAUTH_INSUFFICIENT and (
        plan.pace != PACE_STEADY
    ):
        steady_first_wave = first_wave_at_pace(plan, PACE_STEADY)
        if steady_first_wave <= pre.balance_usd:
            payload["alternative"] = {
                "pace": PACE_STEADY,
                "first_wave_usd": str(steady_first_wave),
            }
    return jsonify(payload)


@targets_bp.route("/targets/<target_id>/launch", methods=["POST"])
@login_required
@idempotent()
def target_launch_submit(target_id):
    """Create and fund one run per selected tool, against one target.

    Ordering is the whole design and is not interchangeable:

    1. Validate EVERY tool. Any failure creates nothing at all.
    2. One summed start gate for the whole selection.
    3. Create every run as ``draft``.
    4. Only then fund, then drive.

    A draft is inert in every sense that costs money: ``drive_campaign``
    refuses it, ``_campaign_spend_today`` skips it, and no hold is ever placed.
    So a failure between 3 and 4 leaves rows that were never charged, and there
    is nothing to roll back and no cleanup job to write.
    """
    from shared import compute_campaigns as cc  # noqa: PLC0415
    from shared.target_launch import (  # noqa: PLC0415
        PACE_BURST,
        PACE_STEADY,
        plan_multi_launch,
        preauth_multi_launch,
    )

    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    target = get_target(target_id, user_id=ctx.user_id)
    if target is None:
        return render_template("404.html"), 404
    if target.is_archived:
        return redirect(url_for("targets.target_detail", target_id=target.id))

    def _err(message, code=400):
        return render_template(
            "targets/launch.html",
            **_launch_context(
                target,
                error=message,
                blocked=_launch_blocker(target),
                pre_fill=request.form.to_dict(),
                # to_dict() keeps only the FIRST value of a multi-valued field,
                # so without this the user's tool selection collapses to one
                # checkbox on every validation error.
                selected_tools=request.form.getlist("tools"),
            ),
        ), code

    blocked = _launch_blocker(target)
    if blocked:
        return _err(blocked)

    specs, error = _collect_launch_specs(target, request.form)
    if specs is None:
        return _err(error)

    # Coerced here rather than left to plan_multi_launch's internal fallback,
    # so the concurrency written to the rows and the note shown to the user
    # cannot describe a different pace than the one that was applied.
    pace = request.form.get("pace") or PACE_BURST
    if pace not in (PACE_BURST, PACE_STEADY):
        pace = PACE_BURST

    try:
        plan = plan_multi_launch(specs, pace)
    except ValueError as exc:
        return _err(str(exc))

    pre = preauth_multi_launch(ctx.user_id, plan)
    if not pre.ok:
        return _err(cc.preauth_message(pre, count=len(specs)))

    # After the gate, before the first insert. Not earlier, because a group id
    # in scope during validation invites persisting partial state; not later,
    # because every insert needs it.
    launch_group_id = str(uuid.uuid4())
    name = (request.form.get("name") or "").strip()

    created = []
    # concurrency is index-aligned with specs, not keyed by tool, so the same
    # tool selected twice would get its own slot rather than sharing one.
    for spec, concurrency in zip(plan.specs, plan.concurrency):
        campaign = cc.create_campaign(
            user_id=ctx.user_id,
            tool=spec.tool,
            params=spec.params,
            requested_designs=spec.requested_designs,
            preset=spec.preset,
            name=name or None,
            # Denormalized, not re-staged. The driver re-mints a presigned URL
            # from this column every wave and never learns about design_targets.
            target_storage_path=target.storage_path,
            target_name=target.display_name,
            target_id=target.id,
            launch_group_id=launch_group_id,
            # Guaranteed >= 1 by divide_concurrency, which matters because
            # create_campaign treats 0 as falsy and would silently restore the
            # tool default, undoing the division exactly when it is needed most.
            concurrency_target=concurrency,
        )
        if campaign is None:
            # Earlier drafts stay draft: inert, unfunded, unbilled. Deleting
            # them would mean inventing a delete path over the table that holds
            # the money rows, to reclaim a row that costs nothing.
            logger.warning(
                "target_launch: create_campaign failed for %s on target %s; "
                "%d earlier draft(s) left unfunded in group %s",
                spec.tool, target.id, len(created), launch_group_id,
            )
            return _err(
                "Something went wrong starting these runs. Nothing was "
                "started and nothing was charged. Try again in a moment."
            )
        created.append(campaign)

    touch_target(target.id)

    started, stalled = [], []
    for campaign in created:
        # fund_campaign is a CAS that reports whether the row actually moved.
        # Driving an unfunded campaign is a silent no-op (drive_campaign
        # early-returns on draft), so an unchecked fund would leave a run the
        # user believes is going parked forever with nothing to see.
        if cc.fund_campaign(campaign.id):
            cc.drive_campaign_async(campaign.id)
            started.append(campaign)
        else:
            stalled.append(campaign)
            logger.warning(
                "target_launch: fund_campaign did not move %s (%s) out of "
                "draft; it will not be driven", campaign.id, campaign.tool,
            )

    if not started:
        return _err(
            "None of those runs could be started. Your wallet was not charged."
        )
    return redirect(
        url_for(
            "targets.target_detail",
            target_id=target.id,
            launched=launch_group_id,
            # url_for drops a None, so a clean launch carries no stalled param.
            stalled=len(stalled) or None,
        )
    )


@targets_bp.route("/targets/<target_id>/archive", methods=["POST"])
@login_required
def target_archive(target_id):
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    # Archive only ever sets a timestamp. It must NOT delete the staged
    # structure: _dispatch_chunk re-mints a presigned URL from it on every
    # wave, so removing the object would break every chunk of every run still
    # in flight. See shared.targets.archive_target.
    archive_target(target_id, ctx.user_id)
    return redirect(url_for("targets.targets_list"))


@targets_bp.route("/targets/<target_id>/unarchive", methods=["POST"])
@login_required
def target_unarchive(target_id):
    """Restore an archived target.

    Owner scope is enforced inside ``unarchive_target``'s query (it filters on
    user_id), so an unowned id updates nothing and lands back on the list
    without confirming the id exists. A target that was already live takes the
    same path: nothing was restored, so nothing is claimed.

    ``return_to`` decides where a success goes, because the two callers want
    different things: from the detail page the user restored this target to
    use it, and from the list they are likely restoring several. It is matched
    against one literal and never used as a URL, so it cannot become an open
    redirect.
    """
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    if unarchive_target(target_id, ctx.user_id):
        if request.form.get("return_to") == "list":
            return redirect(url_for("targets.targets_list"))
        return redirect(url_for("targets.target_detail", target_id=target_id))
    return redirect(url_for("targets.targets_list"))
