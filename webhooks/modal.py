"""Modal pipeline webhook receiver.

Stream C (Wave-2 launch prep). Kendrew GPU pipelines POST their final
results (and progress heartbeats) back to tools-hub at:

    POST /webhooks/modal/<job_id>/<job_token>   — terminal status
    POST /webhooks/heartbeat                    — progress update

The main webhook body shape (from
``llm-proteinDesigner/docker/<tool>/run_pipeline.py::post_webhook``)::

    {
        "id": "<kendrew job id, matches our tool_jobs.id>",
        "pod_id": "<Modal FunctionCall id>",
        "status": "COMPLETED" | "FAILED",
        "output": { ... tool-specific result payload ... },
        "timestamp": "...",
        "error": {"category": "...", "message": "..."}
    }

The heartbeat body shape::

    {"job_id": ..., "stage": "...", "designs_completed": N, "designs_total": M,
     # optional live partial result; only accepted when job_token matches:
     "job_token": "<shared secret>",
     "new_candidate": {"rank": 1, "pdb_key": "design_0.pdb",
                       "iptm": 0.74, "plddt": 0.83, "i_pae": 0.27,
                       "filter_status": "pass"}}

Authentication
--------------
Main webhook: ``job_token`` is a shared secret generated at submission
time and written to both (a) the Modal payload as ``job_token``, and
(b) the tool_jobs row. The receiver compares the path-segment token to
the stored token; a mismatch returns 403.

Heartbeat: best-effort telemetry only, and the body itself is NOT
authenticated. Two consequences follow, both defended here:

  * Anything attacker-controlled in the body must be inert. The benign
    stage string is harmless to spoof. Live ``new_candidate`` injection
    is rendered back to the user, so it is gated behind the per-job
    ``job_token`` shared secret (see below).
  * The cost-overrun warning must never trust the body. It uses a
    SERVER-SIDE wall-clock measurement (``_elapsed_running_seconds``)
    only; the request-body ``cumulative_gpu_seconds`` is ignored. The
    mid-run monitor no longer kills a job for cost (the cost-based kill
    was removed; spend is bounded by the prepaid wallet + per-job hold
    and wall-clock by the Modal container hard timeout), so at worst a
    forged figure would trigger one spurious overrun-warning email to
    the victim, never a cancel or a billed charge. Using the server-side
    value keeps even that inert. If we ever need stronger guarantees,
    add the token to the heartbeat URL too.

Idempotency & race safety
-------------------------
A replay of the same COMPLETED/FAILED POST is a no-op — we refuse to
move a terminal-state job back to a different terminal state. Both
layers enforce this:

  * The preflight ``if job.status in terminal`` short-circuits obvious
    replays without touching Supabase.
  * The underlying ``complete_job`` → ``mark_*`` helpers use a CAS-style
    ``UPDATE ... WHERE id = :job_id AND status IN ('pending','running')``
    so a user cancel that lands between our SELECT and our UPDATE still
    wins. When the CAS UPDATE matches zero rows we skip the refund and
    email side effects and return ``already_terminal`` to the caller.

Registering
-----------
    from webhooks.modal import register_modal_webhooks
    register_modal_webhooks(flask_app)

Mounts both endpoints on the given Flask app.
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone
from typing import Any

from flask import Flask, Response, jsonify, request

from shared.credits import get_service_client
from shared.jobs import (
    TERMINAL_STATUSES,
    ToolJob,
    complete_job,
    get_job,
    mark_running,
    mid_run_monitor_check,
)

logger = logging.getLogger(__name__)


def register_modal_webhooks(flask_app: Flask) -> None:
    """Attach the Modal callback + heartbeat endpoints to the given app."""

    @flask_app.route(
        "/webhooks/modal/<job_id>/<job_token>", methods=["POST"]
    )
    def modal_result(job_id: str, job_token: str) -> Any:  # noqa: ANN401
        return _handle_result(job_id, job_token)

    @flask_app.route("/webhooks/heartbeat", methods=["POST"])
    def modal_heartbeat() -> Any:  # noqa: ANN401
        return _handle_heartbeat()


# ---------------------------------------------------------------------------
# Main result receiver
# ---------------------------------------------------------------------------


def _handle_result(job_id: str, job_token: str) -> Any:
    """Apply a terminal status update from a Kendrew pipeline POST."""
    payload = request.get_json(silent=True) or {}
    status_raw = str(payload.get("status") or "").upper()

    job = get_job(job_id)
    if job is None:
        logger.warning("Modal webhook: unknown job id %s", job_id)
        return Response("unknown job", status=404)

    if not hmac.compare_digest(job.job_token, job_token):
        logger.warning(
            "Modal webhook: token mismatch for job %s", job_id
        )
        return Response("forbidden", status=403)

    if job.status in ("succeeded", "failed", "timeout", "cancelled"):
        # Terminal state already reached — replay (or a late pipeline
        # POST after a user cancel) is a no-op.
        logger.info(
            "Modal webhook: ignoring replay on terminal job %s (current=%s)",
            job_id,
            job.status,
        )
        return jsonify({"status": "already_terminal", "current": job.status})

    if status_raw == "COMPLETED":
        fresh = _apply_terminal(
            job,
            terminal_status="succeeded",
            result=payload.get("output") or {},
            error=None,
        )
        return _finalize_response(fresh, "succeeded", job_id)

    if status_raw == "FAILED":
        err = payload.get("error") or {
            "category": "unknown",
            "message": "Pipeline reported FAILED with no error detail.",
        }
        fresh = _apply_terminal(
            job,
            terminal_status="failed",
            result=None,
            error=err,
        )
        return _finalize_response(fresh, "failed", job_id)

    # Anything else — refuse to update state on an ambiguous status.
    logger.warning(
        "Modal webhook: unexpected status %r for job %s", status_raw, job_id
    )
    return jsonify({"status": "ignored", "reason": "unexpected status"}), 202


def _apply_terminal(
    job: ToolJob,
    *,
    terminal_status: str,
    result: Any,
    error: Any,
) -> ToolJob | None:
    """Move a job to its terminal state, settle the wallet hold, send email.

    Returns the post-transition ToolJob row. complete_job is CAS-guarded,
    so if a concurrent writer (user cancel, inline poll) terminalised the
    row between our SELECT and its UPDATE the returned row's ``status``
    will NOT match ``terminal_status`` — the caller should respond with
    ``already_terminal`` in that case.
    """
    fresh = complete_job(
        job.id,
        terminal_status=terminal_status,
        result=result if isinstance(result, dict) else None,
        error=error if isinstance(error, dict) else (
            {"detail": str(error)} if error else None
        ),
    )
    _observe_terminal(job.tool, terminal_status)

    # D3 funnel fire. Only the success path lights up the dashboard
    # event; failures are tracked by _observe_terminal's stripe-events
    # mirror. The CAS race above leaves ``fresh`` on a non-succeeded
    # status when a concurrent writer landed first; in that case the
    # other writer owns the emit.
    if (
        fresh is not None
        and fresh.status == "succeeded"
        and terminal_status == "succeeded"
        and fresh.user_id
    ):
        _emit_job_completed(fresh)

    return fresh


def _finalize_response(fresh: ToolJob | None, target: str, job_id: str) -> Any:
    """Map a post-``complete_job`` row to the webhook HTTP response.

    Reaching ``target`` is a 200 ``recorded``. A *different* terminal state
    means a concurrent writer (user cancel, inline poll) won the CAS — also a
    legitimate 200 ``already_terminal``. But a row that is STILL non-terminal
    (or vanished) means the terminal write itself failed — e.g. an oversized
    ``result`` jsonb that threw inside ``_cas_update``. Surface that as a 500
    so Modal records ``delivered: False`` instead of masking a stuck job.
    """
    if fresh is not None and fresh.status == target:
        return jsonify({"status": "recorded", "terminal": target})
    if fresh is not None and fresh.status in TERMINAL_STATUSES:
        return jsonify({"status": "already_terminal", "current": fresh.status})
    logger.error(
        "Modal webhook: job %s did not finalize (target=%s, current=%s); "
        "the terminal write failed.",
        job_id, target, getattr(fresh, "status", None),
    )
    return jsonify({"status": "error", "reason": "finalize_failed"}), 500


def _count_succeeded_jobs(user_id: str) -> int:
    """Return the user's total succeeded-job count.

    Best-effort: a Supabase hiccup returns 0, which steers the funnel
    emit to ``first_job_completed``. The dashboard tolerates that bias
    better than a missing event would.
    """
    client = get_service_client()
    if client is None:
        return 0
    try:
        response = (
            client.table("tool_jobs")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("status", "succeeded")
            .limit(1)
            .execute()
        )
        return int(getattr(response, "count", None) or 0)
    except Exception:
        logger.warning(
            "succeeded-job count failed for user %s",
            user_id, exc_info=True,
        )
        return 0


def _emit_job_completed(fresh: ToolJob) -> None:
    """Post the funnel event for a freshly completed job.

    Lazy-imports ``shared.events`` so the webhook stays decoupled from
    the analytics module load order.
    """
    try:
        from shared.events import EVENTS, emit  # noqa: PLC0415

        total = _count_succeeded_jobs(fresh.user_id)
        is_first = total == 1
        emit(
            EVENTS.FIRST_JOB_COMPLETED if is_first
            else EVENTS.NTH_JOB_COMPLETED,
            user_id=fresh.user_id,
            properties={
                "tool": fresh.tool,
                "preset": fresh.preset,
                "job_id": fresh.id,
            },
        )
    except Exception:
        logger.warning(
            "funnel emit failed for job %s",
            fresh.id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Heartbeat receiver
# ---------------------------------------------------------------------------


def _handle_heartbeat() -> Any:
    """Record a progress heartbeat from a running Kendrew pipeline.

    Heartbeats are fire-and-forget telemetry — we always return 200 so
    the pipeline does not waste GPU time on retries. If the body is
    malformed or the job is unknown we log and move on.

    The heartbeat also drives the mid-run cost-overrun safety check. If
    cumulative GPU cost passes 1.5x the estimate we email a soft
    warning; past 2x AND the per-tool hard cap we cancel the Modal call
    and mark the job failed (the hold is then released at zero compute).
    """
    body = request.get_json(silent=True) or {}
    job_id = str(body.get("job_id") or "")
    if not job_id:
        return jsonify({"status": "ignored", "reason": "missing job_id"}), 200

    job = get_job(job_id)
    if job is None:
        return jsonify({"status": "ignored", "reason": "unknown job"}), 200

    # On the first heartbeat, transition pending -> running so the UI
    # knows the pipeline is actually executing (vs. queued in Modal).
    # Re-fetch so started_at is populated for the overrun monitor below.
    if job.status == "pending":
        mark_running(job.id)
        fresh = get_job(job_id)
        if fresh is not None:
            job = fresh

    # Optional per-design candidate carried by the heartbeat (live
    # partial-results streaming). Unlike the stage string, candidate data
    # is rendered back to the user, so it is gated behind the job_token
    # shared secret to stop a spoofed heartbeat injecting fake results.
    new_candidate = body.get("new_candidate")
    if isinstance(new_candidate, dict):
        stored_token = job.job_token or ""
        token = str(body.get("job_token") or "")
        # An empty stored token must never match: hmac.compare_digest("", "")
        # returns True, so guard it explicitly before the constant-time check.
        if not stored_token or not hmac.compare_digest(stored_token, token):
            logger.warning(
                "Heartbeat candidate token mismatch for job %s; dropping it",
                job_id,
            )
            new_candidate = None
        else:
            # Project to a fixed, bounded schema so a spoofed or buggy
            # pipeline cannot bloat the inputs jsonb or smuggle markup into
            # the status page.
            new_candidate = _sanitize_candidate(new_candidate)
    else:
        new_candidate = None

    # Persist the latest stage string (and any verified new candidate) in
    # the inputs jsonb so the status page can render a progress line and
    # stream candidates as they complete. We avoid a dedicated column to
    # keep the schema small; the jsonb append is cheap.
    _append_heartbeat_state(
        job_id=job_id,
        stage=str(body.get("stage") or ""),
        designs_completed=_safe_int(body.get("designs_completed")),
        designs_total=_safe_int(body.get("designs_total")),
        new_candidate=new_candidate,
    )

    # Cost-overrun warning. The overrun check MUST use a server-side
    # measurement only. The heartbeat is unauthenticated on this path, so
    # a client-supplied ``cumulative_gpu_seconds`` is attacker-controlled.
    # mid_run_monitor_check no longer kills a job for cost (the cost-based
    # kill was removed); it only emails a one-time overrun warning. Using
    # the wall-clock value keeps even that inert against a forged figure:
    # a spoofed heartbeat cannot cancel a victim's job or settle a billed
    # charge, only (at most) trip a spurious warning off a real elapsed
    # time. We therefore derive cumulative seconds purely from wall-clock
    # since started_at and ignore the request-body value entirely. Modal
    # bills wall-clock on the GPU container, so this is also a fair cost
    # approximation for the warn threshold.
    cumulative_secs = _elapsed_running_seconds(job)
    if cumulative_secs > 0:
        _run_overrun_check(job_id, cumulative_secs)

    return jsonify({"status": "ok"})


def _elapsed_running_seconds(job: ToolJob) -> float:
    """Wall-clock seconds since the job entered the running state."""
    started_raw = getattr(job, "started_at", None)
    if not started_raw:
        return 0.0
    try:
        started = datetime.fromisoformat(
            str(started_raw).replace("Z", "+00:00")
        )
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    except Exception:
        return 0.0


def _run_overrun_check(job_id: str, cumulative_gpu_seconds: float) -> None:
    """Fire mid_run_monitor_check with a lazily-built ModalClient.

    Lazy import keeps gpu.modal_client out of the module-import cycle and
    means a missing modal package does not break heartbeats — the monitor
    will just skip the cancel step.
    """
    try:
        from gpu.modal_client import ModalClient  # noqa: PLC0415
        client = ModalClient()
    except Exception:
        client = None
    try:
        mid_run_monitor_check(
            job_id, cumulative_gpu_seconds, modal_client=client,
        )
    except Exception:
        logger.warning(
            "Mid-run monitor check raised for job %s", job_id, exc_info=True,
        )


def _safe_int(value: Any) -> int:
    """Coerce a heartbeat count to int, defaulting to 0 on bad input.

    Heartbeats are fire-and-forget telemetry; a malformed count must not
    turn the always-200 endpoint into a 500.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _sanitize_candidate(cand: dict) -> dict | None:
    """Project a heartbeat candidate to a fixed, bounded schema.

    Drops unknown keys and caps string lengths so a spoofed or buggy
    pipeline cannot bloat the inputs jsonb or smuggle markup into the
    status page. Returns None when there is no usable integer rank.

    Schema is additive across tools — fields not set by a given pipeline
    come back as None and the results renderer hides them. Boltz-2 adds
    ``name``, ``ptm``, ``complex_plddt``, ``complex_iplddt``,
    ``n_hotspot_contacts``, ``contacted_residues`` on top of the
    composite-pipeline schema (rank/pdb_key/iptm/plddt/i_pae/filter_status).
    """
    try:
        rank = int(cand.get("rank"))
    except (TypeError, ValueError):
        return None

    def _num(value: Any) -> float | None:
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            return None

    pdb_key = cand.get("pdb_key")
    filter_status = cand.get("filter_status")
    name = cand.get("name")

    # contacted_residues: positive 1-indexed ints, cap at 64 to keep the
    # jsonb row bounded. Anything else gets dropped silently.
    raw_contacts = cand.get("contacted_residues")
    contacts_out: list[int] | None = None
    if isinstance(raw_contacts, list):
        cleaned: list[int] = []
        for v in raw_contacts[:64]:
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if 0 < n < 100000:
                cleaned.append(n)
        contacts_out = cleaned

    n_hotspot_contacts: int | None = None
    try:
        if cand.get("n_hotspot_contacts") is not None:
            n_hotspot_contacts = max(0, int(cand.get("n_hotspot_contacts")))
    except (TypeError, ValueError):
        n_hotspot_contacts = None

    # Proteina-Complexa diversity cluster id (bounded non-negative int).
    cluster_id: int | None = None
    try:
        if cand.get("cluster_id") is not None:
            cid = int(cand.get("cluster_id"))
            cluster_id = cid if 0 <= cid < 1_000_000 else None
    except (TypeError, ValueError):
        cluster_id = None

    # Proteina steric-clash flag (bool or None).
    raw_clash = cand.get("has_clash")
    has_clash = bool(raw_clash) if isinstance(raw_clash, bool) else None

    metadata_tag = cand.get("metadata_tag")

    # Proteina: which residue numbering the delivered PDB carries. Only the two
    # values the pipeline emits survive; anything else becomes None rather than
    # being echoed back onto the status page, because this endpoint's body is
    # unauthenticated telemetry and every other string field here is bounded the
    # same way.
    raw_numbering = cand.get("target_numbering")
    target_numbering = (
        raw_numbering if raw_numbering in ("input", "upstream") else None)

    return {
        "rank": rank,
        "name": str(name)[:64] if name else None,
        "pdb_key": str(pdb_key)[:256] if pdb_key else None,
        "iptm": _num(cand.get("iptm")),
        "ptm": _num(cand.get("ptm")),
        "plddt": _num(cand.get("plddt")),
        "complex_plddt": _num(cand.get("complex_plddt")),
        "complex_iplddt": _num(cand.get("complex_iplddt")),
        "i_pae": _num(cand.get("i_pae")),
        "n_hotspot_contacts": n_hotspot_contacts,
        "contacted_residues": contacts_out,
        "filter_status": str(filter_status)[:64] if filter_status else None,
        # Proteina-Complexa reward stack (additive; other tools leave these None
        # and the results renderer hides them). AF2 confidence for protein
        # binders; RF3 score for ligand / motif; force-field energy where it
        # applies; scRMSD self-consistency (binder + ligand); min interface PAE;
        # a composite total reward; and a diversity cluster id + optional tag.
        "af2_plddt": _num(cand.get("af2_plddt")),
        "af2_iptm": _num(cand.get("af2_iptm")),
        "rf3_score": _num(cand.get("rf3_score")),
        "ff_energy": _num(cand.get("ff_energy")),
        "rmsd": _num(cand.get("rmsd")),
        "binder_scrmsd": _num(cand.get("binder_scrmsd")),
        "ligand_scrmsd": _num(cand.get("ligand_scrmsd")),
        "min_ipae": _num(cand.get("min_ipae")),
        "total_reward": _num(cand.get("total_reward")),
        "cluster_id": cluster_id,
        "has_clash": has_clash,
        "metadata_tag": str(metadata_tag)[:64] if metadata_tag else None,
        "target_numbering": target_numbering,
        # OpenDDE confidence-head ranking score (additive; other tools leave it
        # None and the results renderer hides it).
        "ranking_score": _num(cand.get("ranking_score")),
    }


def _hb_merge_inputs(
    current_inputs: dict,
    *,
    stage: str,
    designs_completed: int,
    designs_total: int,
    new_candidate: dict | None,
) -> dict:
    """Return a new inputs dict with this heartbeat's progress folded in.

    Pure merge — no I/O — so the caller can retry it against a fresh read
    under optimistic concurrency. ``_progress`` is last-writer-wins (a
    monotonic snapshot, harmless to overwrite); ``_partial_candidates`` is
    an append that must NOT drop a concurrent sibling's row.
    """
    merged = dict(current_inputs) if isinstance(current_inputs, dict) else {}
    merged["_progress"] = {
        "stage": stage,
        "designs_completed": designs_completed,
        "designs_total": designs_total,
    }
    if isinstance(new_candidate, dict):
        partials = merged.get("_partial_candidates")
        if not isinstance(partials, list):
            partials = []
        else:
            partials = list(partials)

        def _cand_key(cand: Any) -> Any:
            if not isinstance(cand, dict):
                return None
            return cand.get("pdb_key") or cand.get("rank")

        # Dedup by pdb_key (fallback rank) so a heartbeat retry does not
        # duplicate a candidate row in the live table. Cap as a safety bound.
        seen = {_cand_key(c) for c in partials}
        if _cand_key(new_candidate) not in seen:
            partials.append(new_candidate)
        merged["_partial_candidates"] = partials[:1000]
    return merged


# Bounded optimistic-CAS retries for the heartbeat merge. Small on purpose:
# the fallback write below is correct (just last-writer-wins) so we never
# need to spin hard on contention.
_HB_CAS_MAX_RETRIES = 3


def _append_heartbeat_state(
    *,
    job_id: str,
    stage: str,
    designs_completed: int,
    designs_total: int,
    new_candidate: dict | None = None,
) -> None:
    """Merge the latest heartbeat into the job row's inputs jsonb.

    Always writes ``inputs._progress`` (stage + design counts). When the
    heartbeat carries a token-verified ``new_candidate`` dict, appends it
    to ``inputs._partial_candidates`` so the status page can stream
    results as each design completes.

    Concurrency: two heartbeats for the same job (e.g. a per-candidate
    beat overlapping the 15-min monitor beat across gunicorn workers) can
    both read the same ``inputs`` and clobber each other's candidate
    append. We guard the write with an optimistic compare-and-swap on a
    private ``inputs._hb_version`` counter and retry on a lost race, so
    concurrent appends serialise instead of dropping rows. If the CAS
    filter is unavailable, or retries are exhausted, we fall back to a
    plain last-writer-wins write — never worse than the original
    read-modify-write, and the authoritative candidate list still comes
    from the terminal webhook regardless.
    """
    client = get_service_client()
    if client is None:
        return

    def _read_inputs() -> "dict | None":
        try:
            existing = (
                client.table("tool_jobs")
                .select("inputs")
                .eq("id", job_id)
                .single()
                .execute()
            )
            return (getattr(existing, "data", None) or {}).get("inputs") or {}
        except Exception:
            return None

    for _ in range(_HB_CAS_MAX_RETRIES):
        current_inputs = _read_inputs()
        if current_inputs is None:
            break  # read failed — drop to the best-effort fallback write
        old_version = current_inputs.get("_hb_version")
        old_version = old_version if isinstance(old_version, int) else None
        merged = _hb_merge_inputs(
            current_inputs,
            stage=stage,
            designs_completed=designs_completed,
            designs_total=designs_total,
            new_candidate=new_candidate,
        )
        merged["_hb_version"] = (old_version or 0) + 1
        try:
            query = client.table("tool_jobs").update({"inputs": merged}).eq(
                "id", job_id
            )
            # CAS: only land if the version has not moved since we read it.
            # Pre-existing rows have no version yet -> match IS NULL.
            if old_version is None:
                query = query.is_("inputs->>_hb_version", "null")
            else:
                query = query.eq("inputs->>_hb_version", str(old_version))
            resp = query.execute()
        except Exception:
            # jsonb filter unsupported / transient error — stop CASing and
            # fall back to the unconditional write below.
            logger.debug(
                "Heartbeat CAS update failed for %s; falling back", job_id,
                exc_info=True,
            )
            break
        if len(getattr(resp, "data", None) or []) > 0:
            return  # won the race
        # Lost the race: a sibling heartbeat wrote between our read and
        # write. Loop to re-read and re-merge onto the fresher row.

    # Fallback: retries exhausted, read failed, or CAS unavailable. A plain
    # merge write keeps progress advancing (last-writer-wins, exactly the
    # pre-fix behaviour).
    current_inputs = _read_inputs() or {}
    merged = _hb_merge_inputs(
        current_inputs,
        stage=stage,
        designs_completed=designs_completed,
        designs_total=designs_total,
        new_candidate=new_candidate,
    )
    prior_version = current_inputs.get("_hb_version")
    merged["_hb_version"] = (
        prior_version if isinstance(prior_version, int) else 0
    ) + 1
    try:
        client.table("tool_jobs").update({"inputs": merged}).eq(
            "id", job_id
        ).execute()
    except Exception:
        logger.debug("Heartbeat update failed for %s", job_id, exc_info=True)


# ---------------------------------------------------------------------------
# Metrics hook
# ---------------------------------------------------------------------------


def _observe_terminal(tool: str, outcome: str) -> None:
    """Lazy-imported metrics hook so shared.metrics stays optional."""
    try:
        from shared.metrics import STRIPE_EVENTS  # noqa: PLC0415, F401
        # We already track stripe events separately; reuse the tool_jobs
        # counter would be cleaner. Add a dedicated counter to
        # shared/metrics.py if the traffic shape justifies it.
        from shared.metrics import observe_stripe_event  # noqa: PLC0415

        # Repurpose the stripe_events counter with a "tool:<name>" type
        # label so we don't ship a new metric for something we may not
        # need long-term. Swap to a dedicated counter later if we do.
        observe_stripe_event(f"tool:{tool}", outcome)
    except Exception:  # pragma: no cover
        pass
