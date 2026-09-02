"""Job lifecycle + export routes (blueprint refactor, Commit 3).

List / compare / detail / status / refold / cancel / share and the CSV /
FASTA / ZIP / PDB / AF2 export + download routes at /jobs/* and
/api/jobs/*. Lifted verbatim from ``create_app()``; only
``@flask_app.route`` -> ``@jobs_bp.route`` and the factory-local
``modal_client`` singleton -> ``current_app.modal_client`` (the factory
stashes it on the app). The two jobs-only helpers _share_allowed /
_top_score_for_share move in at module level.
"""

from __future__ import annotations

import logging
import os
import re

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from shared import metric_glossary as _metric_glossary
from shared import pdb_bfactors as _pdb_bfactors
from shared.auth import login_required
from shared.credits import load_user_context
from shared.feature_flags import tool_enabled
from shared.idempotency import idempotent
from shared import score_legends
from shared.jobs import (
    cancel_job,
    candidate_records,
    complete_job,
    create_job,
    get_job,
    list_campaign_labels_for_user,
    list_jobs_paginated,
    mark_failed,
    mark_running,
)
from shared.storage import (
    StorageError,
    download_output,
    output_exists,
    presigned_input_url,
)
from tools import base as tool_base

logger = logging.getLogger(__name__)

jobs_bp = Blueprint("jobs", __name__)


# The `?handoff=` reasons /jobs/<id> renders a banner for (register item A91).
# Whitelisted so an unknown or crafted value renders nothing at all rather than
# an empty alert.
#
# PUBLIC AND MODULE-LEVEL SO THE BANNER SUITE CAN IMPORT IT, the same reason
# blueprints/targets.py::HANDOFF_REASONS and
# blueprints/campaigns.py::LAB_HANDOFF_REASONS are, and the note beside the
# first of those records what a hand-written copy of these keys in a test file
# cost: it couples to nothing, so a sixth reason added to a route renders the
# shared macro's `{% else %}` arm -- "your request could not be submitted" --
# for an unrelated cause with the whole suite green.
#
# A THIRD LITERAL TUPLE AND NOT A LIFTED ONE. blueprints/campaigns.py states
# beside its own copy that the duplication is deliberate -- the five KEYS are
# common to the arms while the CAUSE SETS behind them are not -- and declines to
# license merging the banner suites. That reasoning covers this arm as well, so
# it gets its own tuple and its own suite (tests/test_job_handoff_banners.py,
# which asserts set equality against this name in both directions) rather than
# an import from either of the other two.
JOB_HANDOFF_REASONS = ("none", "noname", "rejected", "unverified", "failed")


def _share_allowed(user_metadata) -> bool:  # noqa: ANN001
    """Return True when ``user_metadata.allow_share`` is explicitly True.

    Default is False — the D4 share endpoint never emits a URL for an
    account that has not actively turned share-out on. Used by
    ``/jobs/<id>/share`` to gate the JSON response.
    """
    if not isinstance(user_metadata, dict):
        return False
    value = user_metadata.get("allow_share")
    return isinstance(value, bool) and value is True


def _top_score_for_share(job) -> str | None:  # noqa: ANN001
    """Pull a formatted top-candidate score for the share og_title.

    Returns None when the job has no candidate scores to surface (a
    failed run, a sequence-design tool, a job without a result yet).
    The caller composes ``og_title`` without the trailing score clause
    when this returns None.
    """
    if getattr(job, "status", None) != "succeeded":
        return None
    result = getattr(job, "result", None) or {}
    if not isinstance(result, dict):
        return None
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    top = candidates[0]
    if not isinstance(top, dict):
        return None
    scores = top.get("scores")
    if not isinstance(scores, dict):
        # Some adapters inline the score at the candidate root.
        flat = {
            k: top.get(k) for k in ("iptm", "ipTM", "plddt", "pLDDT")
            if isinstance(top.get(k), (int, float))
        }
        scores = flat or {}
    for col in scores:
        val = scores.get(col)
        if col in _metric_glossary.PLDDT_COLUMNS:
            # This string goes into og:title on a PUBLIC share card, so it
            # is read with no page around it to give the scale.
            val = _metric_glossary.plddt_on_100(val)
        if isinstance(val, (int, float)):
            return f"{col} {val:.3f}" if isinstance(val, float) else f"{col} {val}"
    return None


@jobs_bp.route("/jobs", methods=["GET"])
@login_required
def jobs_list():
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page_size = 25
    # C4 — campaign filter. ``?campaign=<label>`` narrows the list to
    # a single campaign; ``?campaign=__uncategorized__`` selects the
    # rows where campaign_label IS NULL. Missing means "all rows".
    raw_campaign = request.args.get("campaign")
    campaign_filter: str | None
    if raw_campaign is None:
        campaign_filter = None
    elif raw_campaign == "__uncategorized__":
        campaign_filter = ""
    else:
        campaign_filter = raw_campaign.strip() or None
    jobs, total = list_jobs_paginated(
        ctx.user_id,
        page=page,
        page_size=page_size,
        campaign_label=campaign_filter,
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page > total_pages:
        redirect_args = {"page": total_pages}
        if raw_campaign is not None:
            redirect_args["campaign"] = raw_campaign
        return redirect(url_for("jobs.jobs_list", **redirect_args))
    # Group rows by campaign_label for the per-campaign h3 headers.
    # Uncategorized rows sort last. Preserves the newest-first order
    # within each group since list_jobs_paginated already ordered by
    # created_at DESC.
    groups: dict[str, list] = {}
    for j in jobs:
        key = j.campaign_label or "__uncategorized__"
        groups.setdefault(key, []).append(j)
    campaign_groups = []
    for key in sorted(
        (k for k in groups if k != "__uncategorized__"),
        key=str.lower,
    ):
        campaign_groups.append({"label": key, "jobs": groups[key]})
    if "__uncategorized__" in groups:
        campaign_groups.append({
            "label": None, "jobs": groups["__uncategorized__"]
        })
    all_campaign_labels = list_campaign_labels_for_user(ctx.user_id)
    return render_template(
        "jobs_list.html",
        jobs=jobs,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        campaign_groups=campaign_groups,
        all_campaign_labels=all_campaign_labels,
        selected_campaign=raw_campaign,
    )


@jobs_bp.route("/jobs/compare", methods=["GET"])
@login_required
def jobs_compare():
    """Wave 3B cross-run compare: render selected jobs side-by-side.

    Accepts ``ids=a,b,c`` or repeated ``ids=a&ids=b``. Owner-scoped.
    """
    from shared.jobs import list_jobs_by_ids  # local import avoids cycle
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    raw = request.args.getlist("ids")
    if len(raw) == 1 and "," in raw[0]:
        raw = [x.strip() for x in raw[0].split(",") if x.strip()]
    raw = [x for x in raw if x]
    if len(raw) < 2:
        return redirect(url_for("jobs.jobs_list"))
    # Bumped from 6 to 10 so a C3 "Re-fold top 10" lands cleanly in
    # a single comparison view.
    jobs = list_jobs_by_ids(ctx.user_id, raw[:10])
    columns = []
    for j in jobs:
        adapter = tool_base.get(j.tool)
        columns.append({
            "job": j,
            "tool_label": adapter.label if adapter else j.tool,
        })
    return render_template("jobs_compare.html", columns=columns)

@jobs_bp.route("/jobs/<job_id>", methods=["GET"])
@login_required
def job_detail(job_id: str):
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None:
        return render_template("404.html"), 404
    # The reason a failed lab handoff carries, read from the query string.
    # Whitelisted so an unknown or crafted value renders nothing at all rather
    # than an empty alert -- the wording both sibling routes use. It does NOT
    # stop a hand-pasted WHITELISTED value from rendering the full banner; that
    # is true of all three pages and is what this suite's per-reason render
    # tests drive.
    handoff = (request.args.get("handoff") or "").strip()
    if handoff not in JOB_HANDOFF_REASONS:
        handoff = ""
    adapter = tool_base.get(job.tool)
    preset_obj = adapter.preset_for(job.preset) if adapter else None

    # Phase 4 cross-tool handoff: only offer the buttons when the
    # source job staged a reusable PDB (has _pdb_storage_path) and
    # finished successfully. Skip the current tool — "send to self"
    # is what Clone is for. Skip any adapter whose input contract is
    # not PDB-based — e.g. D2 AF2, which takes FASTA. The generic
    # ``from_job`` flow only ports a PDB reuse token + chain +
    # hotspots; offering AF2 as a handoff target would drop the user
    # on a form that cannot consume the handoff (Codex P2).
    NON_PDB_INPUT_TOOLS = frozenset({"af2"})
    send_target_tools: list[dict] = []
    if (
        job.status == "succeeded"
        and (job.inputs or {}).get("_pdb_storage_path")
    ):
        for other in tool_base.all_adapters():
            if other.slug == job.tool:
                continue
            if other.slug in NON_PDB_INPUT_TOOLS:
                continue
            if not tool_enabled(other.slug):
                continue
            send_target_tools.append({
                "slug": other.slug,
                "label": other.label,
                "url": url_for(
                    "tools.tool_form", tool=other.slug, from_job=job.id
                ),
            })

    # D4 — share button gating. Only resolve user_metadata for the
    # terminal-success branch where the share button could render;
    # the admin.list_users round-trip is wasted on pending/running.
    share_allowed = False
    if job.status == "succeeded":
        from shared.jobs import resolve_user_email_and_meta  # noqa: PLC0415
        _email, user_meta = resolve_user_email_and_meta(ctx.user_id)
        share_allowed = _share_allowed(user_meta)

    return render_template(
        "job_detail.html",
        job=job,
        tool_label=adapter.label if adapter else job.tool,
        tool_results_partial=(
            adapter.results_partial
            if adapter and adapter.results_partial
            else "tools/_default_results.html"
        ),
        is_long_running=bool(preset_obj and preset_obj.long_running),
        user_email=session.get("user_email") or "",
        send_target_tools=send_target_tools,
        share_allowed=share_allowed,
        handoff=handoff,
    )

@jobs_bp.route("/jobs/<job_id>/status.json", methods=["GET"])
@login_required
def job_status(job_id: str):
    ctx = load_user_context()
    if ctx is None:
        return jsonify({"error": "unauthenticated"}), 401
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None:
        return jsonify({"error": "not_found"}), 404

    # If the job still thinks it is pending/running, poll Modal once
    # so terminal transitions are detected even when the webhook
    # callback has not fired (e.g. inline smoke-tier returns).
    if job.status in ("pending", "running") and job.modal_function_call_id:
        poll = current_app.modal_client.poll(job.modal_function_call_id)
        if poll["status"] == "succeeded":
            complete_job(
                job.id,
                terminal_status="succeeded",
                result=poll["result"] or {},
                gpu_seconds_used=poll.get("gpu_seconds_used"),
            )
            job = get_job(job_id, user_id=ctx.user_id)
        elif poll["status"] == "failed":
            complete_job(
                job.id,
                terminal_status="failed",
                error={"bucket": "pipeline", "detail": poll.get("error") or ""},
                gpu_seconds_used=poll.get("gpu_seconds_used"),
            )
            job = get_job(job_id, user_id=ctx.user_id)
        elif poll["status"] == "running" and job.status == "pending":
            mark_running(job.id)
            job = get_job(job_id, user_id=ctx.user_id)

    inputs = job.inputs or {}
    partials = inputs.get("_partial_candidates") or []
    if not isinstance(partials, list):
        partials = []
    # Live count, derived from each partial's own measurements. This used to
    # read the streamed ``filter_status`` and match it against the single
    # string "pass", which undercounted every tool with a different vocabulary
    # ("strict_pass") on top of being a stored verdict.
    #
    # None MEANS "CANNOT SAY YET", AND IT IS NOT THE SAME AS ZERO. A streamed
    # partial is not a finished candidate. The limit is what each CONTAINER
    # chooses to put in its heartbeat, not the webhook schema: boltzgen builds
    # its new_candidate from ipTM, pLDDT and i_pae alone, so the refolding RMSD
    # its bar needs never arrives (the refold runs at the end anyway), and
    # rfantibody sends no global pAE. webhooks/modal._sanitize_candidate does
    # have an ``rmsd`` field; nothing fills it for boltzgen, and
    # _COLUMN_ALIASES does not treat it as a refolding RMSD, because RMSD means
    # a different comparison for bindcraft. Reporting 0 here would assert that
    # nothing met the bar, which is a claim the run has not made; the template
    # omits the clause entirely on None.
    #
    # ``bar_is_answerable`` rather than "some partial is not unjudged": the
    # second predicate shipped once and was wrong for exactly these two tools,
    # because one partial short on a leg that DOES stream flips the set out of
    # all-unjudged and pins the counter at a 0 it can never leave.
    rows = [c for c in partials if isinstance(c, dict)]
    if not score_legends.tool_has_bar(job.tool):
        # No bar to meet, so this is a delivered count and the template says
        # so. It must NOT be described as meeting a bar: esmfold2-design has
        # no bar, and its own worked example turns on a design that folds
        # beautifully and must not be ordered.
        passed = len(rows)
    elif score_legends.bar_is_answerable(job.tool, rows):
        passed = sum(
            1 for c in rows
            if score_legends.judge(job.tool, c).verdict == "meets"
        )
    else:
        passed = None

    # The live table's quality cell is derived HERE and not in the browser, so
    # there is exactly one implementation of the bar. Shallow copies: the
    # partials belong to job.inputs and this is a read path.
    live = []
    for cand in partials:
        if not isinstance(cand, dict):
            live.append(cand)
            continue
        verdict = score_legends.judge(job.tool, cand)
        row = dict(cand)
        row["bar_verdict"] = verdict.verdict
        row["bar_text"] = score_legends.verdict_text(job.tool, verdict)
        live.append(row)
    return jsonify(
        {
            "id": job.id,
            "status": job.status,
            "tool": job.tool,
            "preset": job.preset,
            "progress": inputs.get("_progress") or {},
            "partial_candidates": live,
            "passed_count": passed,
            # Whether that number is "met the bar" or just "delivered".
            "has_bar": score_legends.tool_has_bar(job.tool),
            "gpu_seconds_used": job.gpu_seconds_used,
            "started_at": getattr(job, "started_at", None),
        }
    )

def _refold_hotspot_ints(raw) -> list:
    """Coerce a source job's ``hotspot_residues`` to ``list[int]``, never raising.

    Boltz-2 hotspots are 1-indexed SEQUENCE positions, so the chain a source
    hotspot named cannot be carried across anyway — the number is all that
    survives the hop, and dropping the prefix here is a conversion, not a loss
    this function is hiding.

    The comment this replaces said "SOURCE_TOOLS all persist hotspot_residues
    as list[int]" and then did ``[int(x) for x in raw]``. That claim is FALSE:
    rfdiffusion, bindcraft, pxdesign and boltzgen are all in SOURCE_TOOLS, all
    four parse their hotspots through ``tools/base.py::parse_hotspot_residues``,
    and that function returns ``["A296", "B264"]`` for any multi-chain target.
    ``int("A296")`` raises ValueError, which here is a 500 on a Refold click.
    Proteina emits BARE ints in ``hotspot_residues`` (its chain-qualified form
    lives in ``hotspot_spec``) and is not in SOURCE_TOOLS either way, so it is
    not what makes this reachable — the four above already are.

    Unparseable entries are dropped rather than raised on: the alternative is
    failing a refold the user asked for over a field boltz2 treats as optional.
    """
    if isinstance(raw, str):
        items = [t for t in raw.replace(";", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    elif raw is None:
        items = []
    else:
        items = [raw]

    out: list = []
    for item in items:
        if item is None or not str(item).strip():
            continue
        try:
            out.append(int(item))
            continue
        except (TypeError, ValueError):
            pass
        # Chain-prefixed ("A296"): keep the number. split_hotspot needs a chain
        # list to recognise a prefix, and this path has no target to read one
        # from, so match the token shape directly.
        m = re.match(r"^[A-Za-z]{1,4}(-?\d+)$", str(item).strip())
        if m:
            out.append(int(m.group(1)))
    return out


def _spawn_refold_job(ctx, dest_adapter, dest_tool, seq, src, campaign_label,
                      antigen_storage_path=None):
    """Spawn one orthogonal second-opinion fold of ``seq`` (a CandidateSeq from
    completed job ``src``) in ``dest_tool``, returning the new job id or None.

    Shared by the per-job (/jobs/<id>/refold) and campaign
    (/campaigns/<id>/refold) refold paths. Every sequence here came out of an
    already-validated job's result candidates, so inputs are built directly and
    the destination adapter's validate() is bypassed. Boltz-2 cofolds against
    ``src``'s own already-staged antigen (same target, orthogonal predictor).

    The new job inherits ``src.target_id``. Read from the source job rather
    than passed in, because both call sites hand over a ``ToolJob`` and a
    campaign sub-job already carries its campaign's target_id (stamped by
    ``_dispatch_chunk``); there is no path where the caller knows a target the
    source job does not. It is NULL for a refold of an untargeted run, which is
    correct -- there is no target to attribute it to.

    This is what makes a yardstick refold findable: it lands with campaign_id
    NULL, so ``target_id`` is its only link back, and Phase 4's whole premise
    is re-ranking every tool's designs on one predictor's numbers. Without it
    the fan-in cannot see these rows at all.
    """
    if dest_tool == "colabfold":
        inputs = {
            "preset": "standalone",
            "fasta_text": f">{seq.fasta_header}\n{seq.sequence}",
            "num_recycles": 1,
            "use_templates": False,
            "target": f"Refold of {src.tool} job {src.id[:8]}, rank {seq.rank}",
            "_refold_of_job_id": src.id,
            "_campaign_label": campaign_label,
        }
    elif dest_tool == "esmfold":
        inputs = {
            "preset": "standalone",
            "fasta_text": f">{seq.fasta_header}\n{seq.sequence}",
            "target": f"Refold of {src.tool} job {src.id[:8]}, rank {seq.rank}",
            "_refold_of_job_id": src.id,
            "_campaign_label": campaign_label,
        }
    elif dest_tool == "boltz2":
        # Boltz-2 cofold against the SOURCE job's original antigen. Reuses the
        # source's already-staged target PDB rather than re-uploading. The
        # source job has requires_pdb=True (all SOURCE_TOOLS do), so
        # _pdb_storage_path is normally present.
        src_inputs = src.inputs or {}
        staged_path = (src_inputs.get("_pdb_storage_path") or "").strip()
        if not staged_path and antigen_storage_path:
            # Campaign sub-jobs don't persist _pdb_storage_path on their row
            # (create strips underscore-prefixed shared params; the antigen
            # path lives on compute_campaigns.target_storage_path). The campaign
            # refold passes it in so the cofold still targets the right antigen.
            staged_path = str(antigen_storage_path).strip()
        if not staged_path:
            logger.warning(
                "refold->boltz2: source job %s has no _pdb_storage_path",
                src.id,
            )
            return None
        try:
            src_presigned = presigned_input_url(staged_path, expires_seconds=7200)
        except Exception:
            logger.exception(
                "refold->boltz2: presigned_input_url failed for %s",
                staged_path,
            )
            return None
        src_chain = str(src_inputs.get("target_chain") or "A").strip() or "A"
        hotspot_list = _refold_hotspot_ints(
            src_inputs.get("hotspot_residues"))
        inputs = {
            "preset": "standalone",
            "target_chain": src_chain,
            "hotspot_residues": hotspot_list,
            "binder_sequences": [
                {"name": seq.fasta_header, "sequence": seq.sequence},
            ],
            "parameters": {"n_designs_total": 1},
            "target": f"Refold of {src.tool} job {src.id[:8]}, rank {seq.rank}",
            "_refold_of_job_id": src.id,
            "_campaign_label": campaign_label,
            "_pdb_storage_path": staged_path,
            "_input_pdb_url": src_presigned,
            "_input_presigned_url": src_presigned,
        }
    else:
        return None

    job = create_job(
        user_id=ctx.user_id,
        tool=dest_adapter.slug,
        preset="standalone",
        inputs=inputs,
        campaign_label=campaign_label,
        target_id=src.target_id,
    )
    if job is None:
        logger.warning(
            "refold: create_job failed for rank %s (%s -> %s)",
            seq.rank, src.tool, dest_tool,
        )
        return None

    try:
        job_spec = dest_adapter.build_payload(inputs, "")
        webhook_url = url_for(
            "modal_result",
            job_id=job.id,
            job_token=job.job_token,
            _external=True,
        )
        # Boltz-2 needs the antigen presigned URL and the per-design upload
        # endpoint (partial-results streaming). ColabFold/ESMFold ignore both
        # because their FASTA travels inline in job_spec.
        submit_inputs: dict = dict(job_spec)
        if dest_tool == "boltz2":
            submit_inputs["_input_pdb_url"] = inputs.get("_input_presigned_url", "")
            submit_inputs["_input_presigned_url"] = inputs.get("_input_presigned_url", "")
            submit_inputs["_upload_urls_endpoint"] = url_for(
                "upload_urls",
                job_id=job.id,
                job_token=job.job_token,
                _external=True,
            )
        current_app.modal_client.submit(
            dest_adapter.slug,
            "standalone",
            inputs=submit_inputs,
            job_id=job.id,
            job_token=job.job_token,
            webhook_url=webhook_url,
        )
        return job.id
    except Exception:
        logger.exception("refold: modal submit failed for job %s", job.id)
        mark_failed(
            job.id,
            error={"bucket": "modal-submit", "detail": "refold spawn failed"},
        )
        return None


@jobs_bp.route("/jobs/<job_id>/refold", methods=["POST"])
@login_required
@idempotent()
def job_refold(job_id: str):
    """C3 — spawn N orthogonal second-opinion folds on the top N
    designs from a binder-design job and redirect the user to
    /jobs/compare with the new IDs.

    Form fields:
      dest_tool  — "colabfold" or "esmfold"
      n          — number of top designs to refold (clamped to
                   refold.MAX_REFOLD_N, default refold.DEFAULT_REFOLD_N).

    Each spawned job runs in the destination tool's "standalone"
    preset with the candidate's binder sequence as a single-monomer
    FASTA. Per-job wallet billing happens on the existing
    completion-side path (charge_for_job); a fresh signup credit
    covers a top-5 refold many times over.
    """
    from shared.refold import (  # noqa: PLC0415
        DEFAULT_REFOLD_N, MAX_REFOLD_N, can_refold,
        extract_top_n_sequences,
    )
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    src = get_job(job_id, user_id=ctx.user_id)
    if src is None:
        return render_template("404.html"), 404
    if src.status != "succeeded":
        return redirect(url_for("jobs.job_detail", job_id=job_id))

    dest_tool = (request.form.get("dest_tool") or "").strip()
    try:
        n_raw = int(request.form.get("n") or DEFAULT_REFOLD_N)
    except ValueError:
        n_raw = DEFAULT_REFOLD_N
    n = max(1, min(n_raw, MAX_REFOLD_N))

    if not can_refold(src.tool, dest_tool):
        return redirect(url_for("jobs.job_detail", job_id=job_id))
    if not tool_enabled(dest_tool):
        return redirect(url_for("jobs.job_detail", job_id=job_id))
    dest_adapter = tool_base.get(dest_tool)
    if dest_adapter is None:
        return redirect(url_for("jobs.job_detail", job_id=job_id))

    seqs = extract_top_n_sequences(src.result or {}, n)
    if not seqs:
        # Source job has no extractable sequences. Bail back to the
        # source detail page; the calling button is only rendered
        # when the candidate table is non-empty, so this should be
        # rare (e.g. partially failed runs that completed early).
        return redirect(url_for("jobs.job_detail", job_id=job_id))

    # Spawn one job per sequence. The shared campaign_label lets
    # /jobs/compare and future C4 campaign work group them later.
    campaign_label = f"validation-of-{src.id[:8]}"
    spawned: list[str] = []
    for seq in seqs:
        jid = _spawn_refold_job(
            ctx, dest_adapter, dest_tool, seq, src, campaign_label,
        )
        if jid is not None:
            spawned.append(jid)

    if not spawned:
        return redirect(url_for("jobs.job_detail", job_id=job_id))

    # D3 funnel fire. The refold is a per-batch handoff from the
    # source tool to the destination predictor; ``n`` is the actual
    # number of jobs that landed in Supabase, not the form-requested
    # count (a few candidates may have lacked extractable sequences).
    from shared.events import EVENTS, emit  # noqa: PLC0415
    emit(
        EVENTS.REFOLD_SPAWNED,
        user_id=ctx.user_id,
        properties={
            "source_tool": src.tool,
            "dest_tool": dest_tool,
            "n": len(spawned),
            "source_job_id": src.id,
        },
    )

    return redirect(
        url_for("jobs.jobs_compare", ids=",".join(spawned))
    )

@jobs_bp.route("/jobs/<job_id>/cancel", methods=["POST"])
@login_required
@idempotent()
def job_cancel(job_id: str):
    """User-initiated cancel of a pending/running job.

    Best-effort Modal cancel, wallet hold released, row transitions
    to status='cancelled'. Safe to call repeatedly — terminal jobs
    return an error_code without mutating state.
    """
    ctx = load_user_context()
    if ctx is None:
        return jsonify({"error": "unauthenticated"}), 401
    job, err = cancel_job(
        job_id, user_id=ctx.user_id, modal_client=current_app.modal_client
    )
    if job is None:
        code = 404 if err == "not_found" else 409
        return jsonify({"error": err or "cancel_failed"}), code
    return jsonify(
        {
            "id": job.id,
            "status": job.status,
        }
    )

@jobs_bp.route("/jobs/<job_id>/share", methods=["POST"])
@login_required
def job_share(job_id: str):
    """D4 share payload for a finished job.

    Returns ``{url, og_title, og_description, og_image}`` for the
    caller to drop into a LinkedIn / X compose box. The URL points
    back at ``/jobs/<id>`` with a ``utm_source=share`` trio so the
    cross-domain analytics report can attribute the inbound click.

    Gated on the per-user opt-in
    ``auth.users.user_metadata.allow_share`` (default False) so a
    share link is never generated for an account that has not
    explicitly enabled the feature.
    """
    from shared.jobs import resolve_user_email_and_meta  # noqa: PLC0415

    ctx = load_user_context()
    if ctx is None:
        return jsonify({"error": "unauthenticated"}), 401
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None:
        return jsonify({"error": "not_found"}), 404
    _email, user_meta = resolve_user_email_and_meta(ctx.user_id)
    if not _share_allowed(user_meta):
        return jsonify({"error": "share_not_enabled"}), 403

    base_url = os.environ.get(
        "PUBLIC_BASE_URL", "https://tools.ranomics.com"
    ).rstrip("/")
    tool_slug = job.tool or ""
    share_url = (
        f"{base_url}/jobs/{job.id}"
        f"?utm_source=share&utm_medium=user-share"
        f"&utm_campaign={tool_slug}"
    )
    adapter = tool_base.get(tool_slug)
    tool_label = adapter.label if adapter else (tool_slug or "tool")
    top_score = _top_score_for_share(job)
    if top_score is None:
        og_title = (
            f"I designed a binder with {tool_label} on "
            f"tools.ranomics.com"
        )
    else:
        og_title = (
            f"I designed a binder with {tool_label} on "
            f"tools.ranomics.com. Top score {top_score}."
        )
    og_description = (
        "Ranomics tools-hub runs the same GPU pipelines used in "
        "production protein design."
    )
    og_image = url_for(
        "static", filename="og-image.png", _external=True,
    )
    return jsonify({
        "url":            share_url,
        "og_title":       og_title,
        "og_description": og_description,
        "og_image":       og_image,
    })


# ------------------------------------------------------------------
# Export routes — /jobs/<id>/export.{csv,fasta,zip}
# ------------------------------------------------------------------

@jobs_bp.route("/jobs/<job_id>/export.csv", methods=["GET"])
@login_required
def export_csv(job_id: str):
    from flask import Response  # noqa: PLC0415
    from shared.exports import candidates_to_csv  # noqa: PLC0415
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None:
        return render_template("404.html"), 404
    # candidate_records, not a raw ["candidates"] read: the designs-only tools
    # (af2/colabfold/esmfold/boltz2/iggm) persist rows under "designs" and
    # would otherwise export a header-only CSV.
    candidates = candidate_records(job.result)
    return Response(
        candidates_to_csv(candidates),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}_scores.csv"},
    )

@jobs_bp.route("/jobs/<job_id>/export.fasta", methods=["GET"])
@login_required
def export_fasta(job_id: str):
    from flask import Response  # noqa: PLC0415
    from shared.exports import candidates_to_fasta  # noqa: PLC0415
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None:
        return render_template("404.html"), 404
    result = job.result or {}
    body = candidates_to_fasta(
        candidate_records(job.result), sequences=result.get("sequences", []),
    )
    if not body:
        body = "# No sequences found in this job's output.\n"
    return Response(
        body,
        mimetype="text/plain",
        headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}.fasta"},
    )

@jobs_bp.route("/api/jobs/<job_id>/pdb/<path:filename>", methods=["GET"])
@login_required
def job_candidate_pdb(job_id: str, filename: str):
    """Serve a candidate PDB by filename — Storage first, inline b64 fallback.

    Two resolution paths, owner-scoped via ``get_job(user_id=...)``:

    1. ``tool-outputs/{user_id}/{job_id}/designs/<filename>`` — bytes
       served from Storage (server-side proxy, not 302, to keep the
       3D viewer's JS fetch on a same-origin URL).
    2. Inline ``tool_jobs.result.candidates[?].pdb_content_b64`` — scan
       candidates for one whose ``pdb_key`` matches and return the
       decoded bytes.

    Returns 404 with a plain-text body when neither resolves. The
    ``Content-Disposition`` header lets ``<a download="...">`` render
    the right filename on save.
    """
    import base64  # noqa: PLC0415
    from flask import Response  # noqa: PLC0415
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None:
        return render_template("404.html"), 404

    # Path 1: tool-outputs Storage.
    try:
        if output_exists(
            user_id=ctx.user_id, job_id=job_id, filename=filename
        ):
            data = download_output(
                user_id=ctx.user_id,
                job_id=job_id,
                filename=filename,
            )
            # Storage is the PRIMARY structure path: for any
            # designs/ key _slim_result_for_persist drops the inline
            # copy, so every modern job resolves here and the
            # template's converted value is never reached.
            # Converting only there left this on the old scale.
            data = _pdb_bfactors.bfactors_on_100_bytes(data)
            return Response(
                data,
                mimetype="chemical/x-pdb",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{filename}"'
                    )
                },
            )
    except StorageError:
        logger.warning(
            "Storage resolve failed for %s/%s; falling back to inline.",
            job_id, filename, exc_info=True,
        )

    # Path 2: inline pdb_content_b64 fallback (legacy / boltzgen path).
    # Compare on basename so a pdb_key of "designs/design_0.pdb" matches
    # a request URL of either "designs/design_0.pdb" or "design_0.pdb".
    import posixpath  # noqa: PLC0415
    target_basename = posixpath.basename(filename) or filename
    candidates = candidate_records(job.result)
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        cand_basename = posixpath.basename(cand.get("pdb_key") or "")
        if cand_basename != target_basename:
            continue
        encoded = cand.get("pdb_content_b64")
        if not encoded:
            break
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception:
            return Response(
                "# Malformed PDB payload.\n",
                mimetype="text/plain",
                status=500,
            )
        # Same route, legacy inline payload.
        data = _pdb_bfactors.bfactors_on_100_bytes(data)
        return Response(
            data,
            mimetype="chemical/x-pdb",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"'
                )
            },
        )

    return Response(
        "# Candidate PDB not found.\n",
        mimetype="text/plain",
        status=404,
    )

@jobs_bp.route("/jobs/<job_id>/af2.pdb", methods=["GET"])
@login_required
def af2_download_pdb(job_id: str):
    """Stream the AF2 predicted structure as a .pdb download.

    D2 atomic tool. Result payload carries ``pdb_b64`` (base64-encoded
    PDB text); decode and return as text/plain for browser-friendly
    Save As. Owner-scoped via the get_job RLS wrapper.
    """
    import base64  # noqa: PLC0415
    from flask import Response  # noqa: PLC0415
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None or job.tool != "af2":
        return render_template("404.html"), 404
    pdb_b64 = (job.result or {}).get("pdb_b64") or ""
    if not pdb_b64:
        return Response(
            "# No PDB in this job's result.\n",
            mimetype="text/plain",
            status=404,
        )
    try:
        pdb_bytes = base64.b64decode(pdb_b64, validate=True)
    except Exception:
        return Response(
            "# Malformed PDB payload.\n",
            mimetype="text/plain",
            status=500,
        )
    # pLDDT lives in the B-factor column and the field reads it on
    # 0-100. Converted on the way out for the same reason the
    # numbers are, and gated whole-file so a structure that is not a
    # fractional confidence is streamed untouched. See
    # shared/pdb_bfactors.
    pdb_bytes = _pdb_bfactors.bfactors_on_100_bytes(pdb_bytes)
    return Response(
        pdb_bytes,
        mimetype="chemical/x-pdb",
        headers={
            "Content-Disposition": (
                f"attachment; filename=af2_{job_id[:8]}.pdb"
            )
        },
    )

@jobs_bp.route("/jobs/<job_id>/af2_pae.npz", methods=["GET"])
@login_required
def af2_download_pae(job_id: str):
    """Stream the AF2 PAE matrix as a compressed .npz download.

    D2 atomic tool. Result payload carries ``pae_matrix_b64`` which
    is a base64-encoded numpy .npz file (written by run_pipeline.py
    via ``numpy.savez_compressed``). We hand it back as-is — the client
    loads it with ``numpy.load(...)["pae"]``.
    """
    import base64  # noqa: PLC0415
    from flask import Response  # noqa: PLC0415
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None or job.tool != "af2":
        return render_template("404.html"), 404
    pae_b64 = (job.result or {}).get("pae_matrix_b64") or ""
    if not pae_b64:
        return Response(
            "# No PAE matrix in this job's result.\n",
            mimetype="text/plain",
            status=404,
        )
    try:
        pae_bytes = base64.b64decode(pae_b64, validate=True)
    except Exception:
        return Response(
            "# Malformed PAE payload.\n",
            mimetype="text/plain",
            status=500,
        )
    return Response(
        pae_bytes,
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": (
                f"attachment; filename=af2_{job_id[:8]}_pae.npz"
            )
        },
    )

@jobs_bp.route("/jobs/<job_id>/export.zip", methods=["GET"])
@login_required
def export_zip(job_id: str):
    """Bundle every candidate PDB into a ZIP.

    Two resolution paths per candidate, mirroring the per-design
    endpoint:

    1. Inline ``pdb_content_b64`` — decoded and written directly.
    2. ``tool-outputs`` Storage — bytes fetched server-side and
       written. Used when the pipeline POSTed to the upload-URLs
       endpoint rather than emitting b64 in the result row.

    Candidates that resolve via neither path are silently skipped
    (rather than failing the whole archive).
    """
    from flask import Response  # noqa: PLC0415
    from shared.exports import candidates_to_zip  # noqa: PLC0415
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None:
        return render_template("404.html"), 404
    candidates = candidate_records(job.result)

    def _fetch(src_job_id: str, filename: str):
        try:
            return download_output(
                user_id=ctx.user_id, job_id=src_job_id, filename=filename,
            )
        except StorageError:
            logger.warning(
                "export_zip: storage miss for %s/%s",
                src_job_id, filename, exc_info=True,
            )
            return None

    data = candidates_to_zip(candidates, _fetch, default_job_id=job_id)
    return Response(
        data,
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}_pdbs.zip"},
    )
