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

Heartbeat: best-effort telemetry only. The heartbeat body carries
``job_id``; we look up the job and update its status/stage metadata.
No token verification — the worst case is a spoofed heartbeat writes
a fake stage string, which has no security consequence. If we ever
need stronger guarantees, add the token to the heartbeat URL too.

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
        # CAS race: a user cancel (or inline-poll writer) landed between
        # our SELECT and _apply_terminal's UPDATE. complete_job is a
        # no-op in that case and returns the existing terminal row.
        if fresh is not None and fresh.status != "succeeded":
            return jsonify({
                "status": "already_terminal", "current": fresh.status,
            })
        return jsonify({"status": "recorded", "terminal": "succeeded"})

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
        if fresh is not None and fresh.status != "failed":
            return jsonify({
                "status": "already_terminal", "current": fresh.status,
            })
        return jsonify({"status": "recorded", "terminal": "failed"})

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
    return fresh


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

    # Cost-overrun safety. Kendrew heartbeats do not currently carry an
    # explicit cumulative_gpu_seconds figure, so we fall back to
    # wall-clock since started_at. Modal bills wall-clock on the GPU
    # container, so this is a fair approximation for the warn/kill bands.
    cumulative_secs = float(body.get("cumulative_gpu_seconds") or 0)
    if cumulative_secs <= 0:
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
    }


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
    results as each design completes. Keeping both inside the inputs blob
    avoids a schema change, and a single read-modify-write means progress
    and candidate appends never clobber each other.
    """
    client = get_service_client()
    if client is None:
        return
    try:
        existing = (
            client.table("tool_jobs")
            .select("inputs")
            .eq("id", job_id)
            .single()
            .execute()
        )
        current_inputs = (getattr(existing, "data", None) or {}).get("inputs") or {}
    except Exception:
        current_inputs = {}

    current_inputs["_progress"] = {
        "stage": stage,
        "designs_completed": designs_completed,
        "designs_total": designs_total,
    }

    if isinstance(new_candidate, dict):
        partials = current_inputs.get("_partial_candidates")
        if not isinstance(partials, list):
            partials = []

        def _cand_key(cand: Any) -> Any:
            if not isinstance(cand, dict):
                return None
            return cand.get("pdb_key") or cand.get("rank")

        # Dedup by pdb_key (fallback rank) so a heartbeat retry does not
        # duplicate a candidate row in the live table. Cap as a safety bound.
        seen = {_cand_key(c) for c in partials}
        if _cand_key(new_candidate) not in seen:
            partials.append(new_candidate)
        current_inputs["_partial_candidates"] = partials[:1000]

    try:
        client.table("tool_jobs").update({"inputs": current_inputs}).eq(
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
