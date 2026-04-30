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
    {"pending", "running", "succeeded", "failed", "timeout", "cancelled"}
)

TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "timeout", "cancelled"}
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


# Default set of statuses from which a terminal transition is legal. Used
# as the compare-and-swap guard on every ``mark_*`` terminal helper so
# concurrent writers (user cancel vs. Modal webhook vs. inline poll)
# cannot clobber each other's terminal state or double-refund.
_NON_TERMINAL: tuple[str, ...] = ("pending", "running")


def mark_running(job_id: str) -> bool:
    """Transition pending -> running. No-op if already past pending."""
    return _cas_update(
        job_id,
        {
            "status": "running",
            "started_at": _now_iso(),
        },
        allowed_current=("pending",),
    )


def mark_succeeded(
    job_id: str,
    *,
    result: dict,
    gpu_seconds_used: Optional[int] = None,
    allowed_current: tuple[str, ...] = _NON_TERMINAL,
) -> bool:
    """CAS-style success transition. Returns True iff the row actually moved."""
    return _cas_update(
        job_id,
        {
            "status": "succeeded",
            "result": result,
            "gpu_seconds_used": gpu_seconds_used,
            "completed_at": _now_iso(),
        },
        allowed_current=allowed_current,
    )


def mark_failed(
    job_id: str,
    *,
    error: dict,
    gpu_seconds_used: Optional[int] = None,
    allowed_current: tuple[str, ...] = _NON_TERMINAL,
) -> bool:
    """CAS-style failed transition. Returns True iff the row actually moved."""
    return _cas_update(
        job_id,
        {
            "status": "failed",
            "error": error,
            "gpu_seconds_used": gpu_seconds_used,
            "completed_at": _now_iso(),
        },
        allowed_current=allowed_current,
    )


def mark_timeout(
    job_id: str,
    *,
    allowed_current: tuple[str, ...] = _NON_TERMINAL,
) -> bool:
    """CAS-style timeout transition. Returns True iff the row actually moved."""
    return _cas_update(
        job_id,
        {
            "status": "timeout",
            "completed_at": _now_iso(),
        },
        allowed_current=allowed_current,
    )


def mark_cancelled(
    job_id: str,
    *,
    reason: str = "user_cancelled",
    allowed_current: tuple[str, ...] = _NON_TERMINAL,
) -> bool:
    """CAS-style cancel transition.

    Returns True iff this caller actually flipped the row to 'cancelled'.
    When False, another writer (Modal webhook, inline poll) already wrote
    a terminal status; the caller MUST NOT issue a refund.
    """
    return _cas_update(
        job_id,
        {
            "status": "cancelled",
            "error": {"bucket": "cancelled", "detail": reason},
            "completed_at": _now_iso(),
        },
        allowed_current=allowed_current,
    )


def cancel_job(
    job_id: str,
    *,
    user_id: str,
    modal_client,  # noqa: ANN001 — avoid circular import of gpu.modal_client
) -> tuple[Optional["ToolJob"], Optional[str]]:
    """Cancel a pending/running job. Owner-scoped, full credit refund.

    Flow:
      1. Owner-scope fetch; reject if missing or already terminal.
      2. Best-effort Modal FunctionCall cancel (non-fatal if Modal flakes —
         the tool_jobs row is the authoritative state and a stray Modal
         run terminates harmlessly once the tools-hub side is terminal).
      3. Mark the job 'cancelled' with an error bucket of the same name.
      4. Refund the full ``credits_cost`` to the user's ledger.

    Returns ``(job, None)`` on success, ``(None, error_message)`` on
    refusal. Safe to call repeatedly — once the row is terminal, the
    second call returns the row unchanged with a descriptive error.
    """
    job = get_job(job_id, user_id=user_id)
    if job is None:
        return None, "not_found"
    if job.status in TERMINAL_STATUSES:
        return None, f"already_{job.status}"

    if job.modal_function_call_id:
        try:
            modal_client.cancel(job.modal_function_call_id)
        except Exception:
            logger.warning(
                "Modal cancel raised for job %s; proceeding with local cancel.",
                job_id,
                exc_info=True,
            )

    # Compare-and-swap the terminal transition. If this returns False the
    # Modal webhook (or an inline-poll writer) wrote a terminal status
    # between our SELECT and this UPDATE. Skip the refund — it is the
    # winner's responsibility (for cancel, this caller is always the one
    # issuing a refund; for succeeded/failed/timeout the prorated refund
    # path inside complete_job has already run or is about to).
    transitioned = mark_cancelled(job_id, allowed_current=_NON_TERMINAL)
    if not transitioned:
        fresh = get_job(job_id, user_id=user_id)
        current = fresh.status if fresh else "unknown"
        logger.info(
            "cancel_job: CAS lost for job %s; already %s, skipping refund.",
            job_id,
            current,
        )
        return None, f"already_{current}"

    if job.credits_cost > 0:
        try:
            from shared.credits import record_refund  # noqa: PLC0415
            record_refund(
                job.user_id,
                job.credits_cost,
                tool=job.tool,
                reason=f"{job.tool} {job.preset} cancelled by user",
                job_id=job.id,
                metadata={"cancelled_from_status": job.status},
            )
        except Exception:
            logger.warning(
                "Cancel refund failed for job %s (credits=%d)",
                job.id,
                job.credits_cost,
                exc_info=True,
            )

    fresh = get_job(job_id, user_id=user_id)
    return fresh, None


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
    if job.status in TERMINAL_STATUSES:
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

    # CAS transition — the update is constrained to rows where status is
    # still non-terminal. If it returns False, a concurrent writer (user
    # cancel, inline poll, heartbeat-driven state machine) beat us to
    # the row and already scheduled its own refund/email side effects.
    # We return the now-terminal row without re-running refund or email.
    if terminal_status == "succeeded":
        transitioned = mark_succeeded(
            job_id,
            result=result or {},
            gpu_seconds_used=gpu_seconds_used,
            allowed_current=_NON_TERMINAL,
        )
    elif terminal_status == "failed":
        transitioned = mark_failed(
            job_id,
            error=error or {"detail": "unspecified failure"},
            gpu_seconds_used=gpu_seconds_used,
            allowed_current=_NON_TERMINAL,
        )
    else:
        transitioned = mark_timeout(job_id, allowed_current=_NON_TERMINAL)

    # Re-fetch the now-terminal row to seed refund + email payload.
    fresh = get_job(job_id)
    if fresh is None:
        return None

    if not transitioned:
        # Lost the CAS race — another writer terminalised this row. Do
        # not double-refund or re-email.
        logger.info(
            "complete_job: CAS lost for job %s (target=%s actual=%s); "
            "skipping refund and email.",
            job_id,
            terminal_status,
            fresh.status,
        )
        return fresh

    _refund_unused_credits(fresh)
    _send_completion_email(fresh)
    return fresh


def _refund_unused_credits(job: "ToolJob") -> None:
    """Refund credits for partially-used or never-started runs.

    Pre-authorisation: ``credits_cost`` was debited on submit. Two refund
    paths:

    * ``succeeded`` with ``gpu_seconds_used`` under the preset cap →
      prorated refund of the unused fraction.
    * ``failed`` with no ``gpu_seconds_used`` → full refund. The pipeline
      crashed before doing real work (e.g. early input-parse failure or
      webhook-delivery failure) so the customer should not be charged.

    Other failure modes (real GPU work that crashed late, where
    ``gpu_seconds_used`` is set) and ``timeout`` keep the credits — the
    GPU time was actually consumed.
    """
    if job.credits_cost <= 0:
        return

    if job.status == "failed":
        if job.gpu_seconds_used and job.gpu_seconds_used > 0:
            return
        try:
            from shared.credits import record_refund  # noqa: PLC0415
            record_refund(
                job.user_id,
                job.credits_cost,
                tool=job.tool,
                reason=(
                    f"{job.tool} {job.preset} system-failure refund: "
                    "pipeline produced no result"
                ),
                job_id=job.id,
                metadata={"refund_kind": "system_failure"},
            )
            logger.info(
                "Full-refunded %d credit(s) for failed job %s (no GPU time)",
                job.credits_cost,
                job.id,
            )
        except Exception:
            logger.warning(
                "System-failure refund failed for job %s", job.id, exc_info=True
            )
        return

    if job.status != "succeeded":
        return
    if not job.gpu_seconds_used or job.gpu_seconds_used <= 0:
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


def update_inputs(job_id: str, inputs: dict) -> bool:
    """Overwrite the inputs jsonb for a job. Wave 3 uses this to record
    the staged PDB's filename + storage path after upload/copy so a
    future clone can reuse the same file without re-uploading."""
    return _update(job_id, {"inputs": inputs})


def list_jobs_by_ids(user_id: str, job_ids: list[str]) -> list[ToolJob]:
    """Fetch multiple jobs by id, scoped to ``user_id``. Used by the
    Wave 3B cross-run compare route. Returns rows in the same order as
    the ids list; missing/foreign ids are skipped."""
    client = get_service_client()
    if client is None or not job_ids:
        return []
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .in_("id", job_ids)
            .execute()
        )
        rows = {
            str(r["id"]): ToolJob.from_row(r)
            for r in (getattr(response, "data", None) or [])
        }
        return [rows[j] for j in job_ids if j in rows]
    except Exception:
        logger.warning("Failed to fetch jobs by ids for %s", user_id, exc_info=True)
        return []


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


def list_jobs_paginated(
    user_id: str,
    *,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[ToolJob], int]:
    """Paginated owner-scoped job list. Returns (rows, total_count).

    Uses PostgREST ``range()`` for offset/limit and ``count="exact"`` on
    the select so the template can render page controls without a
    separate count round-trip.
    """
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    client = get_service_client()
    if client is None:
        return [], 0
    start = (page - 1) * page_size
    end = start + page_size - 1
    try:
        response = (
            client.table(_TABLE)
            .select("*", count="exact")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .range(start, end)
            .execute()
        )
        rows = [
            ToolJob.from_row(r)
            for r in (getattr(response, "data", None) or [])
        ]
        total = int(getattr(response, "count", None) or 0)
        return rows, total
    except Exception:
        logger.warning(
            "Failed to paginate jobs for user %s (page=%d)",
            user_id,
            page,
            exc_info=True,
        )
        return [], 0


def _update(job_id: str, payload: dict) -> bool:
    """Unconditional update — used only for metadata (modal_function_call_id,
    inputs) where the write is never part of a status race. Terminal
    status transitions MUST go through ``_cas_update`` instead."""
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


def _cas_update(
    job_id: str,
    payload: dict,
    *,
    allowed_current: tuple[str, ...],
) -> bool:
    """Compare-and-swap update constrained by current status.

    Emits ``UPDATE ... WHERE id = :job_id AND status IN :allowed_current``
    and returns True iff the row was actually updated. PostgREST returns
    the updated rows in ``response.data`` (when the default Prefer:
    return=representation is in effect), which we use as the rowcount.

    This is the only safe way to do terminal transitions when more than
    one code path can terminalise the same row — user cancel, Modal
    webhook, inline poll. Whoever loses the race gets ``False`` back
    and MUST NOT issue side effects (refund, email) that the winner
    already owns.
    """
    client = get_service_client()
    if client is None:
        return False
    if not allowed_current:
        # Unconstrained CAS is a bug — refuse to emit a status write
        # without a guard. Use ``_update`` for metadata-only writes.
        raise ValueError("_cas_update requires a non-empty allowed_current")
    try:
        response = (
            client.table(_TABLE)
            .update(payload)
            .eq("id", job_id)
            .in_("status", list(allowed_current))
            .execute()
        )
    except Exception:
        logger.error(
            "CAS update failed for tool_jobs row %s (target payload=%s)",
            job_id,
            {k: payload.get(k) for k in ("status",)},
            exc_info=True,
        )
        return False
    rows = getattr(response, "data", None) or []
    return len(rows) > 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
