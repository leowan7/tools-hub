"""Design target routes: upload a structure once, run many tools against it.

Phases 1 and 2 of the target-first rework. A target is created, listed,
viewed, and archived here, and :func:`target_launch` fans up to seven tools at
one target in a single gated action. The single-tool create form
(``/campaigns/new?target_id=``) still exists and still works; it skips staging
and inherits the target's already-staged path.

Ownership: every route resolves its target owner-scoped -- through
``shared.targets.get_target(..., user_id=ctx.user_id)``, or, on
:func:`target_detail`, through ``read_target(..., user_id=ctx.user_id)``, which
issues the same owner-scoped query and additionally reports WHY it came back
empty (register item A90) -- BEFORE touching a storage path. ``copy_input`` /
``download_input`` take ``user_id`` as a path component and perform no
authorization of their own, so that owner-scoped fetch is the entire tenancy
boundary.

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
from shared.pdb_intake import _parse_preflight_size_params, resolve_target_upload
from shared.storage import StorageError
from shared.target_results import (
    SORT_MODES,
    SORT_PERCENTILE,
    aggregate_target_candidates,
)
from shared.targets import (
    archive_target,
    create_target,
    find_target_by_sha256,
    get_target,
    list_targets_for_user,
    read_target,
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

# Why the lab handoff sent the user back to the target page. `target_detail`
# whitelists these and hands the survivor to the template, which has one branch
# per reason.
#
# PUBLIC AND MODULE-LEVEL SO THE BANNER TESTS CAN IMPORT IT. It used to be a
# literal tuple inside the view, while tests/test_target_handoff_banners.py
# carried a hand-written dict of the same keys under a comment claiming "a
# reason added to that whitelist without a banner of its own shows up here as a
# missing key rather than as silence". Nothing derived one from the other, so
# that was false: adding a sixth reason rendered the `failed` arm's copy --
# "your request could not be submitted" -- for a completely different cause,
# with the whole suite green. Verified by mutation twice (QC round 21 finding
# A-HIGH-3; re-confirmed after round 22 because the fix had been dropped from
# the brief). The test now asserts set equality against this tuple, so the two
# cannot drift apart again.
HANDOFF_REASONS = ("none", "noname", "rejected", "unverified", "failed")


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
    # Every run on this route designs against the STORED target, and upstream
    # resolves an AME task from configs/design_tasks/ame_dict_v2.yaml (a
    # separate registry that `complexa target add` cannot write), so the motif
    # variant can only ever run against a bundled benchmark motif. Offering it
    # here would file designs under a target they were not designed against.
    ("proteina", "motif_ame"): (
        "the motif/enzyme variant can only scaffold a curated benchmark motif, "
        "not your own target. Start it from the campaign form instead."
    ),
}


def _tool_label(adapter) -> str:  # noqa: ANN001
    """Short display name for an error message.

    A seven-tool form answering "Invalid parameters." with no idea which tool
    is unusable, hence the label on every failure message.

    The em-dash split is defensive, not load-bearing. Measured across all 14
    registered adapters: not one label contains an em dash, en dash or double
    dash, so the split is a no-op on every input it will ever see today.
    "Proteina-Complexa" uses an ASCII hyphen, so it survives intact.

    It is kept because ``shared/tools_catalog.py`` carries the same defensive
    splitter (``adapter.label.split("—")[0]`` and ``_short_name_for_label``) for
    a "Name — one line about the tool" convention that nothing currently
    follows. If an adapter ever adopts that style, an error message here should
    not become a sentence. Do not read the split as evidence that any label
    needs it.
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
        # This route ALWAYS launches from a stored target (_launch_blocker has
        # already guaranteed a storage_path), so every run here designs against
        # a caller-supplied structure. Assigned after _tool_form so a crafted
        # `<tool>___has_custom_target` cannot forge the declaration.
        tool_form["_has_custom_target"] = "1"
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
        # Chain/residue ranges, for adapters that declare them (proteina's
        # target_input today). Same persisted-summary check, no download.
        segment_err = target.segment_error(validated.get("_target_segments") or [])
        if segment_err:
            return None, f"{label}: {segment_err}"
        # Size cap, per tool. This route funds one campaign PER SELECTED TOOL,
        # so an oversized target here multiplies across the whole selection --
        # and nothing on this path called the size envelope before. Refusing
        # inside the spec loop means the message names the tool that is too
        # small for this target rather than failing the launch anonymously.
        # binder_max_aa is what arms the COMBINED cap (target + binder against
        # hard_cap_combined_aa). Omitting it left that half of the envelope
        # dead on every money route: a 400 aa target with a 300 aa max binder
        # is 700 against proteina's 620 budget, refused by
        # /tools/proteina/submit and admitted here. Read through
        # _parse_preflight_size_params rather than off a key, because the
        # validated shape differs per tool -- {min,max} dict, [min,max] list,
        # bare int, or a separate binder_length_max -- and that helper is
        # already the single reader of all four at the preflight seam.
        size_err = target.size_error(
            tool, run_chain, validated.get("_target_segments") or [],
            binder_max_aa=_parse_preflight_size_params(validated)[0],
        )
        if size_err:
            return None, f"{label}: {size_err}"

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
        # Whether the error being rendered is safe to describe as having charged
        # nothing. Defaults to FALSE so the claim is never made by omission: only
        # a caller that knows the money state may turn it on.
        "nothing_charged": False,
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


def _target_unavailable(handoff: str):
    """The 503 page for a target read that DID NOT COMPLETE.

    Not 404: "not found" is a claim about the row, and a read that never
    answered is in no position to make it. Not 200 either -- the page carries
    none of the target's content.

    ``handoff`` is passed through because A90's own unavailable exit produces a
    request that carries one: ``_submit_target_shortlist`` refuses an unreadable
    parent by redirecting to this same URL with ``?handoff=unverified``, so
    dropping the query string here would drop the refusal reason on the request
    this route's own gate issued. No claim is made about how often that request
    is the one that lands here. See the comment in :func:`target_detail`.
    """
    return render_template(
        "unavailable.html",
        parent="target",
        handoff=handoff,
        back_url=url_for("targets.targets_list"),
        back_label="My targets",
    ), 503


@targets_bp.route("/targets/<target_id>", methods=["GET"])
@login_required
def target_detail(target_id):
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    # Why the lab handoff sent the user back here. Every one of these was a
    # bare `redirect(detail)` with no banner and nothing changed, which on the
    # one action that hands work to the wet lab reads as a dead button
    # (register items A-7 and A-8). Whitelisted so an unknown or crafted value
    # renders nothing at all rather than an empty alert.
    #
    # READ BEFORE THE TARGET, not after it, because the unavailable arm below
    # renders this banner too. A90's own refusal exit redirects here with
    # `?handoff=unverified` when the parent read at the submit gate did not
    # complete, and the browser follows that redirect within milliseconds --
    # nothing says the fault has passed by then. So a request that arrives here
    # and cannot read the target may be carrying a reason the user has not been
    # told yet, and the reason is in the query string.
    handoff = (request.args.get("handoff") or "").strip()
    # `rejected` is distinct from `none` and the distinction is the whole
    # point: `none` means the request carried no designs, `rejected` means it
    # carried designs and none of them could be attributed to this target.
    # Round 19 collapsed both onto `none`, so a user whose five starred designs
    # were all rejected was told the request "arrived with no designs in it"
    # and advised to retry, which can never work. Two QC reviewers found this
    # independently (round 20, A-H1 / B-F1).
    if handoff not in HANDOFF_REASONS:
        handoff = ""

    # THREE OUTCOMES AND NOT TWO (register item A90). `get_target` answers None
    # for a target that is not there, one that is not this caller's, and a read
    # that never completed, and this route rendered 404 for all three -- so a
    # Supabase blink told a user that a target they were looking at a second ago
    # does not exist. A90's own gate made that a one-click path: it refuses an
    # unreadable parent by redirecting HERE.
    #
    # The 404 is still right for the first two and is kept for them, whole: a
    # completed read that matched no row is a permanent verdict, and it stays
    # one value because telling "no such target" from "not yours" would mean
    # reading a row the owner scope exists to withhold.
    read = read_target(target_id, user_id=ctx.user_id)
    if read.unavailable:
        return _target_unavailable(handoff)
    target = read.target
    if target is None:
        return render_template("404.html"), 404

    from shared import compute_campaigns as cc  # noqa: PLC0415

    # Phase 3's fan-in. ONE call, which reads both tables (this target's
    # compute-campaign runs and its target-tagged standalone jobs) and returns
    # the runs alongside the pooled ranked designs, so this route does NOT also
    # call list_campaigns_for_target: `agg["campaigns"]` is that list.
    #
    # Drafts stay excluded (see list_campaigns_for_target): a stranded draft was
    # never funded, dispatched or billed, so it is not a run. The empty state
    # below counts them separately rather than implying nothing was attempted.
    #
    # Unknown ?sort= falls back rather than 400ing: it arrives from a query
    # string, and a link a user pasted from an older version of this page should
    # render, not error.
    sort_mode = request.args.get("sort") or SORT_PERCENTILE
    if sort_mode not in SORT_MODES:
        sort_mode = SORT_PERCENTILE
    agg = aggregate_target_candidates(
        target.id, user_id=ctx.user_id, sort_mode=sort_mode,
    )
    if not agg["ok"]:
        # 404 AND NOT THE 503 PAGE ABOVE, and the difference is not arbitrary.
        # `ok` False is produced at exactly one place in shared/target_results.py
        # (`_not_found`), and only after a read that COMPLETED reported no owned
        # row; the module's answer for "we could not look" is `_unreadable`,
        # which sets `ok` True and `partial` True precisely so this branch
        # cannot 404 an owner whose database blinked. So reaching here means the
        # target resolved for `read_target` a few lines up and then did not
        # resolve for the aggregate's own ownership read -- a row that went away
        # between two reads, which is absence, which is what 404 means.
        return render_template("404.html"), 404
    runs = agg["campaigns"]

    # A target whose every launch stranded at `draft` has no runs AND no
    # designs, so the empty state would otherwise read "nothing has been run"
    # to someone who tried and was not charged. Counting drafts costs a second
    # query, so it is issued ONLY on the empty path, where there is by
    # definition nothing else to pay for it.
    draft_count = 0
    if not runs:
        draft_count = len(
            [c for c in cc.list_campaigns_for_target(
                target.id, user_id=ctx.user_id, include_drafts=True,
            ) if c.status == "draft"]
        )

    # "You just launched N runs" after a redirect from the launch screen. This
    # app has no flash(), so the result rides the query string. Counted from
    # rows already loaded, so it costs no extra read.
    #
    # An unknown or foreign `launched` group matches nothing, so the launched
    # half stays silent. The stalled half does NOT: it is gated on `stalled`
    # alone, deliberately, because the run query it would otherwise depend on
    # goes empty under the same fault that strands a run. So a crafted `stalled`
    # does render, on its own, with no launched line above it. That only
    # misinforms whoever crafted it, and it is the price of the disclosure
    # surviving the case it exists to report.
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
        agg=agg,
        draft_count=draft_count,
        sort_mode=agg["sort_mode"],
        handoff=handoff,
    )


# The ZIP pulls every PDB's bytes into the web process, so it stays capped
# while CSV and FASTA do not. Mirrors _CAMPAIGN_ZIP_EXPORT_LIMIT
# (blueprints/campaigns.py:659); a target pools MORE tools than a campaign, so
# if anything the bound matters more here.
_TARGET_ZIP_EXPORT_LIMIT = 300


def _starred_refs():
    """``(filter_set, kept, requested)`` for a POSTed export, or
    ``(None, 0, 0)`` on a GET.

    A POST to an export route ALWAYS means "only these designs". A body with
    no ``refs`` field, an unparseable one, or one naming nothing yields an
    EMPTY set, not None: falling back to "everything" would make a malformed
    POST indistinguishable from a GET and hand the user the full file under a
    filename that says ``_starred``.

    The refs are parsed with the same function the TARGET lab-handoff POST
    uses -- ``_parse_candidate_refs_counted``, called from
    ``lab_projects.campaigns_submit`` -- so the two consumers of one star
    selection cannot disagree about the payload shape or about how much of it
    the ceiling removed. It is imported rather than duplicated: a second
    ten-line parser is exactly the kind of thing that drifts.

    NAMED rather than cited by line. This said ``lab_projects.py:521``, which
    landed in an unrelated ``try`` block; the call was six lines further down,
    and moved again -- by an unrelated edit above it -- while that was being
    corrected. A symbol survives an edit above it and a line number does not.

    IT ALSO INHERITS THAT PARSER'S 500-REF CEILING, which it does not announce
    (register item A-2). ``requested > kept`` is how the caller finds out,
    because losing refs to the bound means the file is a prefix of what the
    user asked for and the export otherwise described itself as exact.

    ``kept`` is how many refs the parser returned and ``requested`` how many
    well-formed refs the payload actually carried, both straight from
    ``_parse_candidate_refs_counted``. The earlier version derived the flag
    from ``len(parsed) >= _MAX_CANDIDATE_REFS`` and had to defend the
    over-detection that comes with it -- a selection of EXACTLY 500 is whole
    and was reported as a prefix. There is nothing to defend: ``len(refs)``
    saturates at the cap, which is the whole reason the counted form exists,
    and its own docstring says a caller needing to know what the bound removed
    should call it rather than re-derive it. This one does.

    NEITHER NUMBER IS A ROW COUNT and neither is the size of the returned set.
    Refs may repeat, so the distinct filter set can be far smaller than
    ``kept``; the row shortfall is the caller's to compute against the set it
    actually filtered with.
    """
    from blueprints.lab_projects import (  # noqa: PLC0415
        _parse_candidate_refs_counted,
    )

    if request.method != "POST":
        return None, 0, 0
    parsed, requested = _parse_candidate_refs_counted(request.form.get("refs", ""))
    refs = {(str(r["job_id"]), int(r["index"])) for r in parsed}
    return refs, len(parsed), requested


def _row_ref(cand: dict) -> tuple:
    """A pooled row's identity as the star buttons emit it.

    ``candidate_table.html`` stamps ``data-job`` from ``_source_job_id`` and
    ``data-ref-idx`` from ``_source_index``, and ``shared.target_results``
    stamps both on every pooled row, so this is the same pair on both sides
    with no fallback needed. A row missing either simply matches nothing.
    """
    idx = cand.get("_source_index")
    return (str(cand.get("_source_job_id") or ""), idx if idx is not None else -1)


def _target_export(target_id: str, fmt: str):
    """Pooled CSV / FASTA / ZIP across every run against one target.

    Mirrors :func:`blueprints.campaigns._campaign_export`, with three deliberate
    differences.

    THE OWNERSHIP SENTINEL IS ``ok``, not an empty field. The campaign version
    gates on ``agg.get("tool") is None``, which works there because a campaign
    always has exactly one tool. A target has a LIST, and an owned target with
    no succeeded designs yet has an empty one, so reusing that idiom would 404 a
    paying user's freshly launched work. ``ok`` is False only for missing or
    foreign; owned-and-empty exports an empty file.

    ``?sort=`` is forwarded so the file matches the screen. That is safe because
    the sort mode changes the ORDER of rows and never the SET: the cap is
    applied in canonical order before the display sort, so "top 300" is the same
    300 designs either way and only their order in the CSV differs.

    A PARTIAL READ IS MARKED IN THE FILENAME. A target has many reads behind it
    and any one of them can fail, which yields a short file rather than an error;
    see the note beside ``incomplete`` below. The campaign export has no
    equivalent because it has no equivalent flag.

    The ``rank`` column is NOT the on-screen row number when the page is capped:
    the page ranks with :data:`DEFAULT_LIMIT` and these files rank the whole set,
    so a floor-reserved row sits at a different ordinal in each. The rows carry
    ``source_job`` and ``pdb_key``, which identify a design across both; see
    :func:`shared.exports.export_key`.
    """
    from flask import Response  # noqa: PLC0415
    from shared.exports import (  # noqa: PLC0415
        candidates_to_csv, candidates_to_fasta, candidates_to_zip,
    )
    from shared.storage import download_output  # noqa: PLC0415

    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    sort_mode = request.args.get("sort") or SORT_PERCENTILE
    if sort_mode not in SORT_MODES:
        sort_mode = SORT_PERCENTILE
    export_limit = _TARGET_ZIP_EXPORT_LIMIT if fmt == "zip" else None
    agg = aggregate_target_candidates(
        target_id, user_id=ctx.user_id, limit=export_limit, sort_mode=sort_mode,
    )
    if not agg["ok"]:
        return render_template("404.html"), 404
    candidates = agg.get("candidates", [])
    stem = "target_" + str(target_id)[:8]

    # Starred-only filter. Applied AFTER the aggregate, on rows the user has
    # already been shown, so it can only ever narrow what this same route
    # would otherwise serve -- it is not a second way to address data.
    #
    # Exact for csv/fasta UP TO 500 POSTED REFS. Those aggregate with
    # limit=None, so nothing is lost on the aggregate side, but the selection
    # itself arrives through `_parse_candidate_refs_counted`, which CAPS the
    # list it returns at `_MAX_CANDIDATE_REFS`. It does not stop THERE, and
    # this comment said it did: the loop walks the whole payload and returns
    # `requested` beside the capped list, which is the only reason the
    # overflow is countable and the marker below derivable at all. Describing
    # the parser as stopping is describing the version whose prefix was
    # silent. This comment also claimed "exact" unqualified (register item
    # A-2); the `requested > kept` marker below is what makes the bound
    # visible instead of theoretical.
    #
    # It is NOT offered for the ZIP, which caps at 300 in canonical order: a
    # starred design below that cap would be missing from the archive with
    # nothing to say so. The macro renders the control for CSV only.
    starred, kept, requested = _starred_refs()
    if starred is not None:
        applied = len(starred)
        candidates = [c for c in candidates if _row_ref(c) in starred]
        stem += "_starred"
        # THE SELECTION IS ASSEMBLED IN THE BROWSER, so every way that can go
        # wrong arrives here as the same thing: a POST whose refs name nothing
        # this target has. static/js/candidate_table.js fills the hidden `refs`
        # field at submit time from sessionStorage, and there is no JS harness
        # in this repo, so renaming `.cand-starred-export`, dropping the submit
        # listener, or emitting a different key shape all survive the suite and
        # all land exactly here (register item B-3). Undisclosed, each one
        # ships a header-only CSV at HTTP 200 under a filename saying
        # `_starred`, which reads as "you starred nothing" rather than "the
        # page failed to tell us what you starred".
        #
        # In the filename for the reason `incomplete` and `capped` already are,
        # stated below: the artifact leaves this process and is opened later,
        # out of this page's context, so nothing on the page travels with it.
        # `NofM` mirrors the ZIP's own `_pdbs_top{n}of{total}` rather than
        # inventing a second vocabulary for the same idea.
        #
        # THE TWO MARKERS COMPOSE, and they answer different questions. The
        # first is about the SELECTION -- how much of what the browser posted
        # was applied at all -- and the second about the ROWS that selection
        # resolved to. An `if/elif` chain collapsed three outcomes onto one
        # filename: 600 stale refs (0 rows), 600 refs of which 50 resolved, and
        # 500 refs that all resolved every produced `_starred_first500`, so the
        # `_empty` disclosure this route exists for was deleted at exactly the
        # ref count where a user is most likely to be carrying stale
        # sessionStorage. "The NofM comparison can only understate the loss"
        # was the argument for ordering them; it is not an argument for
        # dropping the other one entirely.
        #
        # `first{kept}of{requested}` counts REFS, not designs. An earlier
        # version wrote `first{len(starred)}`, the DEDUPED filter-set size,
        # while the truncation happened at `_MAX_CANDIDATE_REFS` RAW entries:
        # a 500-entry payload naming 3 distinct designs was named
        # `_starred_first3` over a file holding all 3 of them, where 3 was
        # neither the bound nor the row count. `applied` -- the distinct set
        # actually filtered with -- is the only honest denominator for the row
        # comparison, and it is a different number from both.
        if requested > kept:
            stem += f"_first{kept}of{requested}"
        if not candidates:
            stem += "_empty"
        elif len(candidates) < applied:
            stem += f"_{len(candidates)}of{applied}"

    # A FAILED READ YIELDS A SHORT FILE, NOT AN EMPTY TARGET, and without this
    # the two are byte-indistinguishable. The aggregate sets `partial` precisely
    # so "we could not look" can be told apart from "you have nothing", and
    # target_detail discloses it; this route was written with the flag in hand
    # and dropped it, so a target whose reads failed downloaded as a complete
    # 200 and the FASTA positively asserted there were no sequences.
    #
    # Disclosed in the FILENAME, for the same reason `capped` already is below:
    # the artifact leaves this process and is opened later, out of the page's
    # context, so a banner on the page cannot travel with it. Not disclosed as a
    # leading CSV comment row, which would change a shape every existing
    # consumer parses (`candidates_to_csv` is shared with the campaign export).
    partial = bool(agg.get("partial"))
    incomplete = "_incomplete" if partial else ""

    if fmt == "csv":
        return Response(
            candidates_to_csv(candidates),
            mimetype="text/csv",
            headers={
                "Content-Disposition":
                    f"attachment; filename={stem}_scores{incomplete}.csv",
            },
        )
    if fmt == "fasta":
        body = candidates_to_fasta(candidates)
        if not body:
            # "No sequences found" is a claim about the target. Under `partial`
            # it is a claim about a read that did not happen.
            body = (
                "# Part of this target could not be read, so no sequences could"
                " be listed. Reload the target page and try again.\n"
                if partial
                else "# No sequences found in this target's output.\n"
            )
        return Response(
            body,
            mimetype="text/plain",
            headers={
                "Content-Disposition":
                    f"attachment; filename={stem}{incomplete}.fasta",
            },
        )

    def _fetch(src_job_id: str, filename: str):
        try:
            return download_output(
                user_id=ctx.user_id, job_id=src_job_id, filename=filename,
            )
        except StorageError:
            logger.warning(
                "target export_zip: storage miss for %s/%s",
                src_job_id, filename, exc_info=True,
            )
            return None

    # namespace=True prefixes each entry <tool>/<job8>/ here rather than
    # chunk###/, because every campaign starts at chunk 0 and a bindcraft and a
    # boltzgen chunk000/designs/design_1.pdb would be one arcname. The switch is
    # driven by _source_tool, which only the target aggregate stamps, so the
    # campaign ZIP is unchanged (shared/exports.py::candidates_to_zip).
    data = candidates_to_zip(candidates, _fetch, namespace=True)
    if agg.get("capped"):
        total = agg.get("total", len(candidates))
        zip_name = f"{stem}_pdbs_top{len(candidates)}of{total}{incomplete}.zip"
    else:
        zip_name = f"{stem}_pdbs{incomplete}.zip"
    return Response(
        data,
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={zip_name}"},
    )


# POST is the starred-only variant, and only CSV carries it. See _starred_refs
# for why a POST never falls back to the full export, and the note in
# _target_export for why the ZIP is not offered this way.
@targets_bp.route("/targets/<target_id>/export.csv", methods=["GET", "POST"])
@login_required
def target_export_csv(target_id):
    return _target_export(target_id, "csv")


@targets_bp.route("/targets/<target_id>/export.fasta", methods=["GET"])
@login_required
def target_export_fasta(target_id):
    return _target_export(target_id, "fasta")


@targets_bp.route("/targets/<target_id>/export.zip", methods=["GET"])
@login_required
def target_export_zip(target_id):
    return _target_export(target_id, "zip")


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
        first_wave_display_at_pace,
        plan_multi_launch,
        preauth_multi_launch,
    )

    ctx = load_user_context()
    if ctx is None:
        return jsonify({"ok": False, "error": "Sign in to see an estimate."})
    target = get_target(target_id, user_id=ctx.user_id)
    if target is None:
        return jsonify({"ok": False, "error": "That target could not be found."})
    # The target-level refusals, not just the per-tool ones. Without these the
    # endpoint prices an archived or structure-less target in full detail and
    # answers affordable:true for a launch the POST refuses outright. The
    # browser never asks (the GET renders the blocked panel with no form, and
    # archived redirects before that), so this is for an API caller.
    if target.is_archived:
        return jsonify({"ok": False, "error": "This target is archived."})
    blocked = _launch_blocker(target)
    if blocked:
        return jsonify({"ok": False, "error": blocked})

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

    rows = plan.rows()
    payload = {
        "ok": True,
        "pace": plan.pace,
        "rows": rows,
        "total_designs": plan.total_designs,
        "total_subjobs": plan.total_subjobs,
        "budget_usd": str(plan.budget_usd),
        "first_wave_usd": str(plan.first_wave_usd),
        "balance_usd": str(pre.balance_usd),
        # What the page RENDERS. The exact 4dp values above stay because the
        # anti-drift tests compare them against the planner, but a 2dp
        # conversion done in JS rounds to nearest and so can print a hold below
        # the amount reserved. Costs round up, the balance rounds down.
        #
        # The two totals are summed from the ROW displays rather than ceiled
        # from the exact totals, because the page prints the rows immediately
        # above the totals and ceiling both independently makes the column
        # exceed its own sum. See display_total_usd. The balance is not a total
        # of anything on this page, so it is converted directly.
        "budget_usd_display": cc.display_total_usd(
            r["budget_usd_display"] for r in rows
        ),
        "first_wave_usd_display": cc.display_total_usd(
            r["first_wave_usd_display"] for r in rows
        ),
        "balance_usd_display": cc.display_balance_usd(pre.balance_usd),
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
                # Totalled from the steady ROWS, not ceiled from the steady
                # exact sum. "Starting narrow would need $X" is a promise about
                # the panel the user gets when they act on it, so it has to be
                # that panel's number.
                "first_wave_usd_display": first_wave_display_at_pace(
                    plan, PACE_STEADY
                ),
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
        first_wave_display_at_pace,
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

    # Declared up here so `_err` can read `started` at CALL time. `started` holds
    # exactly the campaigns whose fund was confirmed, so "nothing was charged" is
    # DERIVED from what actually happened rather than asserted: the template used
    # to print that sentence under every error unconditionally, which is the one
    # claim round 5 restricted to a confirmed draft. An error path added after the
    # commit point stops making the claim on its own.
    #
    # This deliberately reuses the list the redirect already counts instead of a
    # separate `funded_any` flag. The flag was the first attempt and it was
    # unpinnable: no error path after the fund loop is reachable today, so its
    # `= True` write could be deleted with the whole suite green, and a tidy-up
    # of "an assignment nobody reads" would have restored the round-5 defect.
    started, stalled = [], []

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
                nothing_charged=not started,
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
        # The re-render carries the estimate panel, which totals its rows' 2dp
        # displays, so the sentence must quote that same total and not a
        # separate rounding of the exact figure. Computed only on the refusal
        # path, because it costs one held-amount lookup per tool.
        return _err(cc.preauth_message(
            pre,
            count=len(specs),
            required_display=first_wave_display_at_pace(plan, plan.pace),
        ))

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
            # The "nothing was charged" half is left to the template, which
            # renders it from whether anything reached `started`. Saying it here
            # too printed it twice in one panel.
            return _err(
                "Something went wrong starting these runs. "
                "Try again in a moment."
            )
        created.append(campaign)

    touch_target(target.id)

    # `started` and `stalled` are bound at the top of the route, next to `_err`.
    # Re-initialising them here would work, but only by accident of `_err` never
    # being called in between, so the binding stays in one place.
    for campaign in created:
        # THE FUND IS THE COMMIT POINT and the only thing that decides started
        # vs stalled. `fund_campaign` is a CAS reporting whether the row moved
        # out of `draft`, and it cannot raise: `_cas_transition` catches
        # everything and returns False.
        #
        # Which is exactly why False on its own is NOT grounds for telling the
        # user nothing was charged. It means EITHER "the row was not in draft"
        # OR "the UPDATE raised and I cannot tell" -- and a write that commits
        # in Postgres while the response read times out lands in the second
        # bucket (see reference_tools_hub_supabase_http2_railway). Reporting
        # that as "not started and not charged" invites a re-launch of a
        # campaign that is funded and billing, which is the same money-
        # reporting inversion as reporting a drive-spawn failure that way, just
        # through the other branch.
        if not cc.fund_campaign(campaign.id):
            row = cc.get_campaign(campaign.id, user_id=ctx.user_id)
            if row is not None and row.status == "draft":
                # Confirmed inert: `drive_campaign` early-returns on draft,
                # `_campaign_spend_today` skips it, no hold was ever placed. It
                # genuinely did not start and was not charged.
                stalled.append(campaign)
                logger.warning(
                    "target_launch: %s (%s) is still draft after fund; not "
                    "driven", campaign.id, campaign.tool,
                )
                continue
            # Either it did move (so the fund actually succeeded) or the read
            # could not tell us. Fall through and treat it as started: claiming
            # "not charged" about money that may be committed is the more
            # expensive error, and it is the one that produces a duplicate.
            logger.warning(
                "target_launch: fund_campaign reported False for %s (%s) but "
                "its status is %s; treating it as started",
                campaign.id, campaign.tool,
                getattr(row, "status", "unreadable"),
            )

        # Funded. Past this line the campaign HAS started and WILL bill,
        # whether or not the thread below ever runs, PROVIDED the campaign tick
        # is scheduled: `funded` is in `cron/tick_campaigns.py::_ACTIVE_STATES`
        # and that module's docstring puts a tick at ~60-90 s, but the SCHEDULE
        # lives outside this repo (the Procfile declares only `release` and
        # `web`, and `campaigns:tick` is a Flask CLI command with no in-repo
        # caller). If it is not scheduled, a campaign whose drive thread never
        # spawned parks at `funded` with no children and nothing to restart it.
        # Filed as A46.
        #
        # `drive_campaign_async` only moves the first wave off the request path;
        # it is an optimisation, not the thing that starts the campaign. So its
        # failure must NOT flip this campaign to stalled. Doing that reported
        # "nothing was started and nothing was charged" about N funded, billing
        # campaigns -- and because that answer is a 400, and
        # `shared/idempotency.py` releases the claim on any status >= 400, the
        # retry the copy invites would create and fund a SECOND full set against
        # a gate the user passed once. Thread exhaustion is process-wide, so
        # every tool in the launch fails together and `started` would be empty.
        # Money is committed for this campaign. `_err` reads this list, so no
        # error rendered from here on can tell the user nothing was charged.
        started.append(campaign)
        try:
            cc.drive_campaign_async(campaign.id)
        except Exception:
            # `threading.Thread(...).start()` is the only statement here that
            # can raise (the drive itself is wrapped inside the thread). Nothing
            # may propagate past this loop: money is committed, and an escaping
            # exception would 500 AND release the claim.
            logger.exception(
                "target_launch: could not spawn the first-wave drive for %s "
                "(%s); it is funded and the campaign tick will drive it",
                campaign.id, campaign.tool,
            )

    if not started:
        # Reached only when EVERY campaign was confirmed still `draft`, so
        # `started` is empty and the template adds the uncharged line. Stating it
        # in the message too printed it twice.
        return _err("None of those runs could be started.")
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
