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

    {"job_id": ..., "stage": "...", "designs_completed": N, "designs_total": M}

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

Idempotency
-----------
A replay of the same COMPLETED/FAILED POST is a no-op — we refuse to
move a terminal-state job back to a different terminal state. The
PostgREST update is constrained by a check on ``status IN
('pending','running')`` inside ``_apply_terminal``.

Registering
-----------
    from webhooks.modal import register_modal_webhooks
    register_modal_webhooks(flask_app)

Mounts both endpoints on the given Flask app.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from flask import Flask, Response, jsonify, request

from shared.credits import get_service_client
from shared.jobs import (
    ToolJob,
    complete_job,
    get_job,
    mark_running,
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

    if job.status in ("succeeded", "failed", "timeout"):
        # Terminal state already reached — replay is a no-op.
        logger.info(
            "Modal webhook: ignoring replay on terminal job %s (current=%s)",
            job_id,
            job.status,
        )
        return jsonify({"status": "already_terminal", "current": job.status})

    if status_raw == "COMPLETED":
        _apply_terminal(
            job,
            terminal_status="succeeded",
            result=payload.get("output") or {},
            error=None,
        )
        return jsonify({"status": "recorded", "terminal": "succeeded"})

    if status_raw == "FAILED":
        err = payload.get("error") or {
            "category": "unknown",
            "message": "Pipeline reported FAILED with no error detail.",
        }
        _apply_terminal(
            job,
            terminal_status="failed",
            result=None,
            error=err,
        )
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
) -> None:
    """Move a job to its terminal state, refund unused credits, send email."""
    complete_job(
        job.id,
        terminal_status=terminal_status,
        result=result if isinstance(result, dict) else None,
        error=error if isinstance(error, dict) else (
            {"detail": str(error)} if error else None
        ),
    )
    _observe_terminal(job.tool, terminal_status)


# ---------------------------------------------------------------------------
# Heartbeat receiver
# ---------------------------------------------------------------------------


def _handle_heartbeat() -> Any:
    """Record a progress heartbeat from a running Kendrew pipeline.

    Heartbeats are fire-and-forget telemetry — we always return 200 so
    the pipeline does not waste GPU time on retries. If the body is
    malformed or the job is unknown we log and move on.
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
    if job.status == "pending":
        mark_running(job.id)

    # Persist the latest stage string in the inputs jsonb so the status
    # page can render a progress line. We avoid a dedicated column to
    # keep the schema small; the jsonb append is cheap.
    _append_heartbeat_state(
        job_id=job_id,
        stage=str(body.get("stage") or ""),
        designs_completed=int(body.get("designs_completed") or 0),
        designs_total=int(body.get("designs_total") or 0),
    )
    return jsonify({"status": "ok"})


def _append_heartbeat_state(
    *,
    job_id: str,
    stage: str,
    designs_completed: int,
    designs_total: int,
) -> None:
    """Merge the latest heartbeat into the job row's inputs.progress key.

    Keeping progress inside the inputs blob avoids a schema change while
    still giving the UI a single place to render "Running BindCraft
    (2/10)" progress lines.
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
