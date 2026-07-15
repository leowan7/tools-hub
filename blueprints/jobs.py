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

from shared.auth import login_required
from shared.credits import load_user_context
from shared.feature_flags import tool_enabled
from shared.idempotency import idempotent
from shared.jobs import (
    cancel_job,
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
    passed = 0
    for cand in partials:
        if not isinstance(cand, dict):
            continue
        fs = str(cand.get("filter_status") or "").strip().lower()
        if fs == "pass":
            passed += 1
    return jsonify(
        {
            "id": job.id,
            "status": job.status,
            "tool": job.tool,
            "preset": job.preset,
            "progress": inputs.get("_progress") or {},
            "partial_candidates": partials,
            "passed_count": passed,
            "gpu_seconds_used": job.gpu_seconds_used,
            "started_at": getattr(job, "started_at", None),
        }
    )

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
        # Build the inputs in the shape the destination adapter's
        # validate() would produce after parsing the form. This
        # bypasses validate() since we control the FASTA content
        # entirely — every sequence here came out of a previously
        # validated job's result candidates.
        if dest_tool == "colabfold":
            inputs = {
                "preset": "standalone",
                "fasta_text": (
                    f">{seq.fasta_header}\n{seq.sequence}"
                ),
                "num_recycles": 1,
                "use_templates": False,
                "target": (
                    f"Refold of {src.tool} job {src.id[:8]}, "
                    f"rank {seq.rank}"
                ),
                "_refold_of_job_id": src.id,
                "_campaign_label": campaign_label,
            }
        elif dest_tool == "esmfold":
            inputs = {
                "preset": "standalone",
                "fasta_text": (
                    f">{seq.fasta_header}\n{seq.sequence}"
                ),
                "target": (
                    f"Refold of {src.tool} job {src.id[:8]}, "
                    f"rank {seq.rank}"
                ),
                "_refold_of_job_id": src.id,
                "_campaign_label": campaign_label,
            }
        elif dest_tool == "boltz2":
            # Boltz-2 cofold against the SOURCE job's original antigen.
            # Reuses the source's already-staged target PDB rather than
            # re-uploading. The source job must have requires_pdb=True
            # (all SOURCE_TOOLS do), so _pdb_storage_path is guaranteed.
            src_inputs = src.inputs or {}
            staged_path = (src_inputs.get("_pdb_storage_path") or "").strip()
            if not staged_path:
                logger.warning(
                    "refold->boltz2: source job %s has no _pdb_storage_path",
                    src.id,
                )
                continue
            try:
                src_presigned = presigned_input_url(
                    staged_path, expires_seconds=7200,
                )
            except Exception:
                logger.exception(
                    "refold->boltz2: presigned_input_url failed for %s",
                    staged_path,
                )
                continue
            src_chain = str(src_inputs.get("target_chain") or "A").strip() or "A"
            # SOURCE_TOOLS all persist hotspot_residues as list[int] in
            # their validate() output; tolerate a string from any
            # future adapter that drops the parsing.
            raw_hotspots = src_inputs.get("hotspot_residues") or []
            if isinstance(raw_hotspots, str):
                parsed: list[int] = []
                for tok in raw_hotspots.replace(";", ",").split(","):
                    tok = tok.strip()
                    if tok:
                        try:
                            parsed.append(int(tok))
                        except ValueError:
                            pass
                raw_hotspots = parsed
            hotspot_list = [int(x) for x in raw_hotspots if str(x).strip()]
            inputs = {
                "preset": "standalone",
                "target_chain": src_chain,
                "hotspot_residues": hotspot_list,
                "binder_sequences": [
                    {"name": seq.fasta_header, "sequence": seq.sequence},
                ],
                "parameters": {"n_designs_total": 1},
                "target": (
                    f"Refold of {src.tool} job {src.id[:8]}, "
                    f"rank {seq.rank}"
                ),
                "_refold_of_job_id": src.id,
                "_campaign_label": campaign_label,
                "_pdb_storage_path": staged_path,
                "_input_pdb_url": src_presigned,
                "_input_presigned_url": src_presigned,
            }
        else:
            # can_refold gate above should make this unreachable.
            continue

        # C4 — promote the per-batch label to the first-class column so
        # /jobs can group the refold batch without sniffing inputs JSON.
        # The legacy inputs._campaign_label key is kept for backward
        # compatibility with rows older than the C4 migration.
        job = create_job(
            user_id=ctx.user_id,
            tool=dest_adapter.slug,
            preset="standalone",
            inputs=inputs,
            campaign_label=campaign_label,
        )
        if job is None:
            logger.warning(
                "refold: create_job failed for rank %s (%s -> %s)",
                seq.rank, src.tool, dest_tool,
            )
            continue

        try:
            job_spec = dest_adapter.build_payload(inputs, "")
            webhook_url = url_for(
                "modal_result",
                job_id=job.id,
                job_token=job.job_token,
                _external=True,
            )
            # Boltz-2 needs the antigen presigned URL and the
            # per-design upload endpoint (partial-results streaming).
            # ColabFold/ESMFold ignore both because their FASTA
            # travels inline in job_spec.
            submit_inputs: dict = dict(job_spec)
            if dest_tool == "boltz2":
                submit_inputs["_input_pdb_url"] = inputs.get(
                    "_input_presigned_url", ""
                )
                submit_inputs["_input_presigned_url"] = inputs.get(
                    "_input_presigned_url", ""
                )
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
            spawned.append(job.id)
        except Exception:
            logger.exception(
                "refold: modal submit failed for job %s", job.id,
            )
            mark_failed(
                job.id,
                error={
                    "bucket": "modal-submit",
                    "detail": "refold spawn failed",
                },
            )

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
    import csv  # noqa: PLC0415
    import io   # noqa: PLC0415
    from flask import Response  # noqa: PLC0415
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None:
        return render_template("404.html"), 404
    candidates = (job.result or {}).get("candidates", [])
    buf = io.StringIO()
    all_score_keys: list[str] = []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        for k in (cand.get("scores") or {}):
            if k not in all_score_keys:
                all_score_keys.append(k)
    writer = csv.DictWriter(buf, fieldnames=["rank", "pdb_key"] + all_score_keys,
                            extrasaction="ignore")
    writer.writeheader()
    for i, cand in enumerate(candidates):
        if not isinstance(cand, dict):
            continue
        scores = cand.get("scores") or {}
        row = {"rank": cand.get("rank", i + 1), "pdb_key": cand.get("pdb_key", "")}
        row.update(scores)
        writer.writerow(row)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}_scores.csv"},
    )

@jobs_bp.route("/jobs/<job_id>/export.fasta", methods=["GET"])
@login_required
def export_fasta(job_id: str):
    from flask import Response  # noqa: PLC0415
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None:
        return render_template("404.html"), 404
    result = job.result or {}
    candidates = result.get("candidates", [])
    mpnn_sequences = result.get("sequences", [])
    lines: list[str] = []
    # Binder-design tools (rfantibody/bindcraft/boltzgen/pxdesign)
    # return ``candidates`` (PDB + docked pose + scores). MPNN is a
    # sequence-design primitive and returns ``sequences`` (seq +
    # score + recovery), so the header+body shape has to differ.
    for i, cand in enumerate(candidates):
        if not isinstance(cand, dict):
            continue
        seq = cand.get("sequence") or cand.get("binder_sequence") or ""
        if not seq:
            continue
        pdb_key = cand.get("pdb_key", f"candidate_{i + 1}")
        rank = cand.get("rank", i + 1)
        lines.append(f">rank{rank}_{pdb_key}")
        # wrap at 80 chars
        for start in range(0, len(seq), 80):
            lines.append(seq[start:start + 80])
    for i, seq_obj in enumerate(mpnn_sequences):
        if not isinstance(seq_obj, dict):
            continue
        seq = seq_obj.get("seq") or ""
        if not seq:
            continue
        header_parts = [f">mpnn_rank{i + 1}"]
        score = seq_obj.get("score")
        recovery = seq_obj.get("recovery")
        if score is not None:
            header_parts.append(f"score={score}")
        if recovery is not None:
            header_parts.append(f"recovery={recovery}")
        lines.append(" ".join(header_parts))
        for start in range(0, len(seq), 80):
            lines.append(seq[start:start + 80])
    if not lines:
        return Response(
            "# No sequences found in this job's output.\n",
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}.fasta"},
        )
    return Response(
        "\n".join(lines) + "\n",
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
    candidates = (job.result or {}).get("candidates", []) or []
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
    import base64   # noqa: PLC0415
    import io       # noqa: PLC0415
    import zipfile  # noqa: PLC0415
    from flask import Response  # noqa: PLC0415
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))
    job = get_job(job_id, user_id=ctx.user_id)
    if job is None:
        return render_template("404.html"), 404
    candidates = (job.result or {}).get("candidates", []) or []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, cand in enumerate(candidates):
            if not isinstance(cand, dict):
                continue
            filename = cand.get("pdb_key") or f"candidate_{i + 1}.pdb"
            data = None

            # Path 1: inline b64.
            encoded = cand.get("pdb_content_b64")
            if encoded:
                try:
                    data = base64.b64decode(encoded, validate=True)
                except Exception:
                    data = None

            # Path 2: tool-outputs Storage.
            if data is None and cand.get("pdb_key"):
                try:
                    data = download_output(
                        user_id=ctx.user_id,
                        job_id=job_id,
                        filename=cand["pdb_key"],
                    )
                except StorageError:
                    logger.warning(
                        "export_zip: storage miss for %s/%s",
                        job_id, filename, exc_info=True,
                    )
                    data = None

            if data is None:
                continue
            zf.writestr(filename, data)
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}_pdbs.zip"},
    )
