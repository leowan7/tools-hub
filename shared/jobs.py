"""Tool-job CRUD helpers backed by ``public.tool_jobs``.

Stream C (Wave-2 launch prep). A single tool_jobs row is the source of
truth for one GPU submission: status, Modal FunctionCall id, inputs,
result, error. The Flask routes, the job-status AJAX endpoint, and the
Modal callback webhook all read and write through this module.

Status transitions
------------------
    pending   -> running | succeeded | failed | timeout
    running   -> succeeded | failed | timeout

``pending`` means the row is inserted but Modal has not been polled yet.
``running`` is set on the first poll that returns "not ready".

Service-role writes bypass RLS (matches shared.credits). Anon reads go
through the self-read policy from migration 0005.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.credits import get_service_client

logger = logging.getLogger(__name__)

_TABLE = "tool_jobs"

VALID_STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", "timeout"}
)


@dataclass(frozen=True)
class ToolJob:
    """Immutable view of a tool_jobs row. Use ``to_dict()`` for templates."""

    id: str
    user_id: str
    tool: str
    preset: str
    status: str
    inputs: dict
    result: Optional[dict]
    error: Optional[dict]
    credits_cost: int
    modal_function_call_id: Optional[str]
    job_token: str
    gpu_seconds_used: Optional[int]
    created_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]

    @classmethod
    def from_row(cls, row: dict) -> "ToolJob":
        return cls(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            tool=row["tool"],
            preset=row["preset"],
            status=row["status"],
            inputs=row.get("inputs") or {},
            result=row.get("result"),
            error=row.get("error"),
            credits_cost=int(row.get("credits_cost") or 0),
            modal_function_call_id=row.get("modal_function_call_id"),
            job_token=row["job_token"],
            gpu_seconds_used=row.get("gpu_seconds_used"),
            created_at=row.get("created_at"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tool": self.tool,
            "preset": self.preset,
            "status": self.status,
            "credits_cost": self.credits_cost,
            "result": self.result,
            "error": self.error,
            "gpu_seconds_used": self.gpu_seconds_used,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def generate_job_token() -> str:
    """Return a 64-char hex token used to authenticate the Modal callback."""
    return secrets.token_hex(32)


def create_job(
    *,
    user_id: str,
    tool: str,
    preset: str,
    inputs: dict,
    credits_cost: int,
) -> Optional[ToolJob]:
    """Insert a new tool_jobs row in pending status. Returns None on failure."""
    client = get_service_client()
    if client is None:
        logger.error("Cannot create job: Supabase service client unavailable.")
        return None
    row = {
        "user_id": user_id,
        "tool": tool,
        "preset": preset,
        "status": "pending",
        "inputs": inputs,
        "credits_cost": credits_cost,
        "job_token": generate_job_token(),
    }
    try:
        response = client.table(_TABLE).insert(row).execute()
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return None
        return ToolJob.from_row(rows[0])
    except Exception:
        logger.error("Failed to insert tool_jobs row.", exc_info=True)
        return None


def get_job(job_id: str, *, user_id: Optional[str] = None) -> Optional[ToolJob]:
    """Fetch a job by id. Pass ``user_id`` to enforce owner scope."""
    client = get_service_client()
    if client is None:
        return None
    try:
        query = client.table(_TABLE).select("*").eq("id", job_id)
        if user_id is not None:
            query = query.eq("user_id", user_id)
        response = query.single().execute()
    except Exception:
        # single() raises when zero rows — treat as "not found"
        return None
    data = getattr(response, "data", None)
    if not data:
        return None
    return ToolJob.from_row(data)


def set_modal_call(job_id: str, function_call_id: str) -> bool:
    """Attach the Modal FunctionCall id to the job and move to pending->pending."""
    return _update(job_id, {"modal_function_call_id": function_call_id})


def mark_running(job_id: str) -> bool:
    return _update(
        job_id,
        {
            "status": "running",
            "started_at": _now_iso(),
        },
    )


def mark_succeeded(
    job_id: str,
    *,
    result: dict,
    gpu_seconds_used: Optional[int] = None,
) -> bool:
    return _update(
        job_id,
        {
            "status": "succeeded",
            "result": result,
            "gpu_seconds_used": gpu_seconds_used,
            "completed_at": _now_iso(),
        },
    )


def mark_failed(
    job_id: str,
    *,
    error: dict,
    gpu_seconds_used: Optional[int] = None,
) -> bool:
    return _update(
        job_id,
        {
            "status": "failed",
            "error": error,
            "gpu_seconds_used": gpu_seconds_used,
            "completed_at": _now_iso(),
        },
    )


def mark_timeout(job_id: str) -> bool:
    return _update(
        job_id,
        {
            "status": "timeout",
            "completed_at": _now_iso(),
        },
    )


# ---------------------------------------------------------------------------
# Terminal-state orchestration: prorated refund + email notification
# ---------------------------------------------------------------------------


def complete_job(
    job_id: str,
    *,
    terminal_status: str,
    result: Optional[dict] = None,
    error: Optional[dict] = None,
    gpu_seconds_used: Optional[int] = None,
) -> Optional["ToolJob"]:
    """Move a job to its terminal state and run the post-completion side
    effects: prorated credit refund (if the actual GPU time came in
    under the preset cap) and the job-complete email.

    Idempotent — calling this on a job that's already terminal is a
    no-op (returns the existing row). Webhook + AJAX-poll callers can
    both fire without worrying about race conditions.
    """
    if terminal_status not in {"succeeded", "failed", "timeout"}:
        raise ValueError(f"complete_job got non-terminal status {terminal_status!r}")

    job = get_job(job_id)
    if job is None:
        return None
    if job.status in {"succeeded", "failed", "timeout"}:
        # Already terminal — refund + email already happened (or were
        # explicitly skipped). Don't double up.
        return job

    # Pull gpu_seconds out of the inline result payload if not given.
    if gpu_seconds_used is None and isinstance(result, dict):
        for key in ("gpu_seconds", "runtime_seconds"):
            v = result.get(key)
            if isinstance(v, (int, float)) and v > 0:
                gpu_seconds_used = int(v)
                break

    if terminal_status == "succeeded":
        mark_succeeded(job_id, result=result or {}, gpu_seconds_used=gpu_seconds_used)
    elif terminal_status == "failed":
        mark_failed(
            job_id,
            error=error or {"detail": "unspecified failure"},
            gpu_seconds_used=gpu_seconds_used,
        )
    else:
        mark_timeout(job_id)

    # Re-fetch the now-terminal row to seed refund + email payload.
    fresh = get_job(job_id)
    if fresh is None:
        return None

    _refund_unused_credits(fresh)
    _send_completion_email(fresh)
    return fresh


def _refund_unused_credits(job: "ToolJob") -> None:
    """Refund credits proportional to unused GPU seconds.

    Pre-authorisation: ``credits_cost`` was debited on submit. If the
    job actually used less GPU time than the preset's cap, the user
    keeps the difference. Failed and timed-out runs get no refund —
    we still spent the GPU time.
    """
    if job.status != "succeeded":
        return
    if not job.gpu_seconds_used or job.gpu_seconds_used <= 0:
        return
    if job.credits_cost <= 0:
        return

    from gpu.modal_client import preset_gpu_seconds  # noqa: PLC0415
    cap_seconds = preset_gpu_seconds(job.tool, job.preset)
    if cap_seconds <= 0:
        return

    used_fraction = min(1.0, job.gpu_seconds_used / cap_seconds)
    used_credits = max(1, int(round(job.credits_cost * used_fraction)))
    refund_credits = job.credits_cost - used_credits
    if refund_credits <= 0:
        return

    try:
        from shared.credits import record_refund  # noqa: PLC0415
        record_refund(
            job.user_id,
            refund_credits,
            tool=job.tool,
            reason=(
                f"{job.tool} {job.preset} prorated refund: "
                f"{job.gpu_seconds_used}s of {cap_seconds}s"
            ),
            job_id=job.id,
            metadata={
                "preset_cap_seconds": cap_seconds,
                "used_seconds": job.gpu_seconds_used,
            },
        )
        logger.info(
            "Refunded %d credit(s) for job %s (used %ds / %ds cap)",
            refund_credits,
            job.id,
            job.gpu_seconds_used,
            cap_seconds,
        )
    except Exception:
        logger.warning(
            "Prorated refund failed for job %s", job.id, exc_info=True
        )


def _send_completion_email(job: "ToolJob") -> None:
    """Send the job-done email if we can resolve the user's email address."""
    if job.status not in {"succeeded", "failed"}:
        return
    user_email = _resolve_email_for_user(job.user_id)
    if not user_email:
        return
    try:
        from shared.email import send_job_complete_email  # noqa: PLC0415
        send_job_complete_email(user_email=user_email, job=job)
    except Exception:
        logger.warning(
            "Email notification failed for job %s", job.id, exc_info=True
        )


def _resolve_email_for_user(user_id: str) -> Optional[str]:
    """Look up the auth.users email for the given user id via service-role client."""
    client = get_service_client()
    if client is None:
        return None
    try:
        page = client.auth.admin.list_users()
        users = getattr(page, "users", None) or page
        for u in users:
            uid = getattr(u, "id", None) or (
                u.get("id") if isinstance(u, dict) else None
            )
            if uid == user_id:
                email = getattr(u, "email", None) or (
                    u.get("email") if isinstance(u, dict) else None
                )
                return email
    except Exception:
        logger.warning("Could not resolve email for user %s", user_id, exc_info=True)
    return None


def list_jobs_for_user(user_id: str, *, limit: int = 20) -> list[ToolJob]:
    client = get_service_client()
    if client is None:
        return []
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [
            ToolJob.from_row(r)
            for r in (getattr(response, "data", None) or [])
        ]
    except Exception:
        logger.warning("Failed to list jobs for user %s", user_id, exc_info=True)
        return []


def _update(job_id: str, payload: dict) -> bool:
    client = get_service_client()
    if client is None:
        return False
    try:
        client.table(_TABLE).update(payload).eq("id", job_id).execute()
        return True
    except Exception:
        logger.error(
            "Failed to update tool_jobs row %s", job_id, exc_info=True
        )
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
