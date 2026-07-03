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


def _normalize_result_shape(result: Optional[dict]) -> Optional[dict]:
    """Unwrap the legacy smoke/mini_pilot result wrapper at read time.

    The inline smoke/mini_pilot path returned a `{"status": "COMPLETED",
    "output": {"candidates": [...]}, "tier": ..., "gpu_seconds": ...}`
    dict, and the old `_interpret_pipeline_return` stored it raw as
    `tool_jobs.result`. Every template and helper that reads
    `job.result.get("candidates")` then saw nothing because candidates
    were nested under `result.output.candidates`. `_interpret_pipeline_return`
    now unwraps for new jobs, but rows persisted before that fix still
    have the wrapped shape. Normalize on read so every consumer (template
    render, PDB resolver, CSV/FASTA export, completion email) sees the
    flat shape regardless of when the row was written.
    """
    if not isinstance(result, dict):
        return result
    if result.get("candidates"):
        return result
    nested = result.get("output")
    if not isinstance(nested, dict) or not nested.get("candidates"):
        return result
    merged = dict(nested)
    for key in ("tier", "gpu_seconds", "runtime_seconds"):
        if key in result and key not in merged:
            merged[key] = result[key]
    return merged


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
    modal_function_call_id: Optional[str]
    job_token: str
    gpu_seconds_used: Optional[int]
    created_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    campaign_label: Optional[str] = None
    failure_class: Optional[str] = None
    # Compute-campaign sub-job linkage (migration 0034). NULL on ordinary
    # single jobs; set only on sub-jobs created by the campaign driver.
    campaign_id: Optional[str] = None
    chunk_index: Optional[int] = None
    attempt: Optional[int] = None

    @classmethod
    def from_row(cls, row: dict) -> "ToolJob":
        return cls(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            tool=row["tool"],
            preset=row["preset"],
            status=row["status"],
            inputs=row.get("inputs") or {},
            result=_normalize_result_shape(row.get("result")),
            error=row.get("error"),
            modal_function_call_id=row.get("modal_function_call_id"),
            job_token=row["job_token"],
            gpu_seconds_used=row.get("gpu_seconds_used"),
            created_at=row.get("created_at"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            campaign_label=row.get("campaign_label"),
            failure_class=row.get("failure_class"),
            campaign_id=(str(row["campaign_id"]) if row.get("campaign_id") else None),
            chunk_index=row.get("chunk_index"),
            attempt=row.get("attempt"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tool": self.tool,
            "preset": self.preset,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "gpu_seconds_used": self.gpu_seconds_used,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "campaign_label": self.campaign_label,
            "failure_class": self.failure_class,
            "campaign_id": self.campaign_id,
            "chunk_index": self.chunk_index,
            "attempt": self.attempt,
        }


# ---------------------------------------------------------------------------
# Failure classifier (drives the wallet settlement branch)
# ---------------------------------------------------------------------------
#
# Maps (status, error bucket, result) -> failure_class enum value, where
# enum values match the CHECK constraint in
# supabase/migrations/0029_tool_jobs_failure_class.sql.
#
# Refund policy by class:
#   succeeded, completed_no_yield, user_cancelled, safety_kill
#       -> charge actual GPU consumed (settle_hold)
#   infra_crash, tool_error, preflight_miss, no_progress_timeout, unclassified
#       -> full refund (release_hold)
#
# The unclassified bucket is the deliberate judgment-case fallback per
# the tier-collapse spec: ambiguous failures default to refund so the
# user is never billed for a case we cannot confidently attribute.


# Failure classes that bill the user for consumed GPU time.
_BILLED_FAILURE_CLASSES: frozenset[str] = frozenset({
    "succeeded",
    "completed_no_yield",
    "user_cancelled",
    "safety_kill",
})

# Failure classes that refund the full hold (no charge to the user).
_REFUNDED_FAILURE_CLASSES: frozenset[str] = frozenset({
    "infra_crash",
    "tool_error",
    "preflight_miss",
    "no_progress_timeout",
    "unclassified",
})

# Error buckets that map to specific failure classes. Anything not in
# this table on a 'failed' row defaults to 'unclassified' (refund).
_ERROR_BUCKET_TO_FAILURE_CLASS: dict[str, str] = {
    # Real production bucket strings (verified by grepping the repo):
    "pipeline":                "tool_error",          # docker run_pipeline crashed (app.py:4652)
    "storage":                 "infra_crash",         # Supabase Storage upload failed (app.py:4353)
    "modal-submit":            "infra_crash",         # Modal SDK submit raised before GPU pod started (app.py:4423, 4916)
    "preflight":               "preflight_miss",      # docker-side preflight check failed (ATOMIC-TOOLS.md)
    "cancelled":               "user_cancelled",      # belt-and-suspenders; status="cancelled" path normally catches first (jobs.py:360)
    "overrun_safety_kill":     "safety_kill",         # server-side overrun kill (jobs.py:843)
    # Reserved Modal-side buckets (not yet emitted; keep for future webhook payloads):
    "modal_crash":             "infra_crash",
    "modal_oom":               "infra_crash",
    "modal_timeout":           "no_progress_timeout",
}


def classify_terminal_state(
    *,
    status: str,
    error: Optional[dict] = None,
    result: Optional[dict] = None,
) -> Optional[str]:
    """Map a terminal job row to its failure_class enum value.

    Pure function. Returns NULL for non-terminal statuses so callers can
    distinguish "not yet classified" from "explicitly classified".

    The mapping is conservative: any 'failed' row whose error bucket is
    not in the known table classifies as 'unclassified', which routes
    to a full refund. This implements the spec's "judgment cases default
    to refund" policy without surfacing every novel bucket as a billed
    case.
    """
    if status == "succeeded":
        # A succeeded run that produced zero passing designs is
        # technically billable (we ran the GPU as ordered) but is
        # surfaced separately so ops can monitor the yield rate. The
        # zero-yield detection is best-effort because not every tool
        # emits a candidates array.
        candidates = None
        if isinstance(result, dict):
            candidates = result.get("candidates")
        if isinstance(candidates, list) and len(candidates) == 0:
            return "completed_no_yield"
        return "succeeded"

    if status == "cancelled":
        return "user_cancelled"

    if status == "timeout":
        return "no_progress_timeout"

    if status == "failed":
        if isinstance(error, dict):
            bucket = error.get("bucket")
            if isinstance(bucket, str):
                mapped = _ERROR_BUCKET_TO_FAILURE_CLASS.get(bucket)
                if mapped:
                    return mapped
        return "unclassified"

    # Non-terminal: leave NULL so the column reflects no decision yet.
    return None


def is_billed_failure_class(failure_class: Optional[str]) -> bool:
    """Return True iff the class bills the user for consumed GPU time.

    NULL (legacy / unclassified) returns False so unfamiliar paths
    default to refund. The caller is expected to pair this with the
    legacy fallback in ``_settle_wallet_hold_for_completed_job`` when
    the column is NULL on a known-good job row.
    """
    if failure_class is None:
        return False
    return failure_class in _BILLED_FAILURE_CLASSES


def generate_job_token() -> str:
    """Return a 64-char hex token used to authenticate the Modal callback."""
    return secrets.token_hex(32)


def create_job(
    *,
    user_id: str,
    tool: str,
    preset: str,
    inputs: dict,
    target_pdb_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    campaign_label: Optional[str] = None,
    campaign_id: Optional[str] = None,
    chunk_index: Optional[int] = None,
    attempt: Optional[int] = None,
) -> Optional[ToolJob]:
    """Insert a new tool_jobs row in pending status. Returns None on failure.

    ``target_pdb_id`` and ``workspace_id`` are optional Workspace-binding
    hints. When set, they are stashed in ``inputs._workspace`` so the
    completion path (``complete_job`` -> ``_charge_workspace_for_completed_job``)
    can deduct the actual Modal cost from the right Workspace cap. Stored
    inside the jsonb column rather than as dedicated columns to avoid a
    schema migration — same pattern as ``inputs._progress`` from heartbeats.

    ``campaign_label`` is the C4 free-form user-typed string that groups
    related submissions on /jobs (e.g. "HER2-binder-v3"). NULL when the
    user left the field blank. Empty/whitespace strings are normalized to
    NULL so the index stays tight.

    ``campaign_id`` / ``chunk_index`` / ``attempt`` are the compute-campaign
    linkage (migration 0034), set only by the campaign driver. Unlike the
    cosmetic ``campaign_label``, ``campaign_id`` is load-bearing (it wires
    the sub-job to its coordinator + admission accounting), so it is NOT
    dropped-and-retried on a schema gap: a campaign simply cannot run until
    0034 is applied, and failing the insert is the correct, loud behaviour.
    """
    client = get_service_client()
    if client is None:
        logger.error("Cannot create job: Supabase service client unavailable.")
        return None
    if target_pdb_id or workspace_id:
        # Copy so we don't mutate the caller's dict.
        inputs = dict(inputs)
        ws_ctx = dict(inputs.get("_workspace") or {})
        if target_pdb_id:
            ws_ctx["target_pdb_id"] = target_pdb_id
        if workspace_id:
            ws_ctx["workspace_id"] = workspace_id
        inputs["_workspace"] = ws_ctx
    # Normalize campaign_label: trim whitespace, cap length, drop empties.
    label: Optional[str] = None
    if isinstance(campaign_label, str):
        stripped = campaign_label.strip()
        if stripped:
            # 80 char cap mirrors the form-field maxlength so server +
            # client agree on the truncation rule.
            label = stripped[:80]
    row = {
        "user_id": user_id,
        "tool": tool,
        "preset": preset,
        "status": "pending",
        "inputs": inputs,
        # Dead column kept at 0 to satisfy NOT NULL on `tool_jobs.credits_cost`
        # (migration 0005). The Preset.credits_cost field was retired with the
        # wallet pivot; pricing lives in shared/wallet_estimates.py now.
        "credits_cost": 0,
        "job_token": generate_job_token(),
    }
    # Only include campaign_label when the user actually set one. The
    # column ships in migration 0022; older databases that have not yet
    # had that migration applied 400 on any row that includes the key.
    # When ``label`` is None the column default (NULL) applies once 0022
    # lands, so skipping the key is semantically identical.
    if label is not None:
        row["campaign_label"] = label
    # Campaign sub-job linkage (migration 0034). Included only for
    # driver-created sub-jobs; single jobs never set these, so their
    # inserts are unaffected until the migration lands. campaign_id is
    # load-bearing and deliberately NOT part of the schema-gap retry below.
    if campaign_id is not None:
        row["campaign_id"] = campaign_id
        if chunk_index is not None:
            row["chunk_index"] = chunk_index
        if attempt is not None:
            row["attempt"] = attempt
    try:
        response = client.table(_TABLE).insert(row).execute()
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return None
        return ToolJob.from_row(rows[0])
    except Exception as exc:
        # 0022 schema gap: if the row carries campaign_label but the column
        # is missing in prod, retry without it so labeled submissions still
        # land (just under the "Uncategorized" group on /jobs) until the
        # migration runs. PGRST204 / 42703 are the matching error codes.
        msg = repr(exc)
        if "campaign_label" in msg and "label" in row:
            logger.warning(
                "tool_jobs.campaign_label missing in prod schema — "
                "retrying insert without it (migration 0022 pending)."
            )
            row.pop("campaign_label", None)
            try:
                response = client.table(_TABLE).insert(row).execute()
                rows = list(getattr(response, "data", None) or [])
                if rows:
                    return ToolJob.from_row(rows[0])
            except Exception:
                logger.error(
                    "Failed to insert tool_jobs row on retry.", exc_info=True,
                )
                return None
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
    """CAS-style success transition. Returns True iff the row actually moved.

    Sets ``failure_class`` from :func:`classify_terminal_state` so the
    wallet settle path can route by typed classification rather than
    re-deriving it from the error bucket on every read.
    """
    return _cas_update(
        job_id,
        {
            "status": "succeeded",
            "result": result,
            "gpu_seconds_used": gpu_seconds_used,
            "failure_class": classify_terminal_state(
                status="succeeded", result=result,
            ),
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
    """CAS-style failed transition. Returns True iff the row actually moved.

    Sets ``failure_class`` based on the error bucket. Unknown buckets
    classify as ``unclassified`` which routes to a full refund (the
    deliberate judgment-case fallback from the tier-collapse spec).
    """
    return _cas_update(
        job_id,
        {
            "status": "failed",
            "error": error,
            "gpu_seconds_used": gpu_seconds_used,
            "failure_class": classify_terminal_state(
                status="failed", error=error,
            ),
            "completed_at": _now_iso(),
        },
        allowed_current=allowed_current,
    )


def mark_timeout(
    job_id: str,
    *,
    allowed_current: tuple[str, ...] = _NON_TERMINAL,
) -> bool:
    """CAS-style timeout transition. Returns True iff the row actually moved.

    Timeouts classify as ``no_progress_timeout`` (infra-side stall),
    which routes to a full refund.
    """
    return _cas_update(
        job_id,
        {
            "status": "timeout",
            "failure_class": classify_terminal_state(status="timeout"),
            "completed_at": _now_iso(),
        },
        allowed_current=allowed_current,
    )


def timeout_stuck_job(job_id: str) -> bool:
    """CAS-timeout a stuck job and release its wallet hold.

    Bundles ``mark_timeout`` with ``_settle_wallet_hold_for_completed_job``
    so the cron sweeper does not reach into private internals. Returns
    True iff this caller actually moved the row — a concurrent webhook
    or user cancel that lands first leaves the wallet path to that
    writer (it is CAS-guarded the same way ``cancel_job`` is).
    """
    if not mark_timeout(job_id):
        return False
    fresh = get_job(job_id)
    if fresh is not None:
        _settle_wallet_hold_for_completed_job(fresh)
    return True


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

    Cancellations classify as ``user_cancelled``, which bills for
    consumed GPU time (no refund of time already used).
    """
    return _cas_update(
        job_id,
        {
            "status": "cancelled",
            "error": {"bucket": "cancelled", "detail": reason},
            "failure_class": classify_terminal_state(status="cancelled"),
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
    """Cancel a pending/running job. Owner-scoped; bills consumed GPU.

    Flow:
      1. Owner-scope fetch; reject if missing or already terminal.
      2. Best-effort Modal FunctionCall cancel (non-fatal if Modal flakes —
         the tool_jobs row is the authoritative state and a stray Modal
         run terminates harmlessly once the tools-hub side is terminal).
      3. Mark the job 'cancelled' with failure_class='user_cancelled'.
      4. Settle the wallet hold against the consumed GPU time persisted
         by the most recent heartbeat (``mid_run_monitor_check``
         updates ``gpu_seconds_used`` on every check). The user is
         charged for actual GPU consumed up to the cancel point and
         refunded any surplus. Cancellations BEFORE the first heartbeat
         settle at zero consumption (full refund).

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
    # between our SELECT and this UPDATE. Skip the hold release — it is
    # the winner's responsibility (for succeeded/failed/timeout the
    # wallet settle inside complete_job has already run or is about to).
    transitioned = mark_cancelled(job_id, allowed_current=_NON_TERMINAL)
    if not transitioned:
        fresh = get_job(job_id, user_id=user_id)
        current = fresh.status if fresh else "unknown"
        logger.info(
            "cancel_job: CAS lost for job %s; already %s, skipping "
            "hold release.",
            job_id,
            current,
        )
        return None, f"already_{current}"

    # Settle the wallet hold against consumed GPU time. After the
    # tier-collapse migration, _settle_wallet_hold_for_completed_job
    # routes by failure_class: user_cancelled -> settle_hold (bill
    # actual gpu_seconds_used). Cancellations before the first heartbeat
    # have gpu_seconds_used=0 on the row and naturally settle at zero
    # (full refund). Idempotent and a no-op for jobs that never carried
    # a hold (smoke runs, pre-wallet rows).
    fresh = get_job(job_id, user_id=user_id)
    if fresh is not None:
        _settle_wallet_hold_for_completed_job(fresh)
        if fresh.campaign_id:
            _drive_campaign_after_terminal(fresh)
    return fresh, None


# ---------------------------------------------------------------------------
# Terminal-state orchestration: prorated refund + email notification
# ---------------------------------------------------------------------------

def _slim_result_for_persist(result: Optional[dict]) -> Optional[dict]:
    """Drop redundant inline ``pdb_content_b64`` before persisting a result.

    Composite-tool webhooks inline a base64 PDB per candidate AND upload the
    same structure to tool-outputs Storage. With ~50 designs the result blob
    is multi-MB, so the single PostgREST UPDATE in ``_cas_update`` throws and
    the job never leaves "running" even though the webhook returned 200. Those
    structures resolve from Storage via a ``designs/<file>`` ``pdb_key`` (the
    candidate PDB route + export.zip), so the inline copy is dead weight and is
    dropped. Candidates that are NOT Storage-backed keep their inline copy (the
    only one): smoke/mini_pilot tiers carry a bare-filename ``pdb_key`` with no
    upload, and any design whose upload failed is listed in ``failed_uploads``.
    Returns a shallow copy; the input is never mutated.
    """
    if not isinstance(result, dict):
        return result
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return result

    import posixpath  # noqa: PLC0415

    failed = {
        posixpath.basename(str(name))
        for name in (result.get("failed_uploads") or [])
        if name
    }
    slimmed = []
    for cand in candidates:
        pdb_key = cand.get("pdb_key") if isinstance(cand, dict) else None
        if (
            isinstance(pdb_key, str)
            and pdb_key.startswith("designs/")
            and cand.get("pdb_content_b64")
            and posixpath.basename(pdb_key) not in failed
        ):
            cand = {k: v for k, v in cand.items() if k != "pdb_content_b64"}
        slimmed.append(cand)
    out = dict(result)
    out["candidates"] = slimmed
    return out


def complete_job(
    job_id: str,
    *,
    terminal_status: str,
    result: Optional[dict] = None,
    error: Optional[dict] = None,
    gpu_seconds_used: Optional[int] = None,
) -> Optional["ToolJob"]:
    """Move a job to its terminal state and run the post-completion side
    effects: the workspace cap charge, the wallet hold settle, and the
    job-complete email.

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
    # Kendrew pilot pipelines (bindcraft/boltzgen/pxdesign/rfantibody) emit
    # `runtime_minutes` in their webhook payload rather than `gpu_seconds`,
    # so accept either and convert minutes->seconds. Without this fallback,
    # the job_detail.html template renders "Completed in — GPU-seconds." with
    # a literal em-dash because `gpu_seconds_used` ends up NULL in Supabase.
    if gpu_seconds_used is None and isinstance(result, dict):
        for key in ("gpu_seconds", "runtime_seconds"):
            v = result.get(key)
            if isinstance(v, (int, float)) and v > 0:
                gpu_seconds_used = int(v)
                break
        if gpu_seconds_used is None:
            v = result.get("runtime_minutes")
            if isinstance(v, (int, float)) and v > 0:
                gpu_seconds_used = int(v * 60)

    # CAS transition — the update is constrained to rows where status is
    # still non-terminal. If it returns False, a concurrent writer (user
    # cancel, inline poll, heartbeat-driven state machine) beat us to
    # the row and already scheduled its own refund/email side effects.
    # We return the now-terminal row without re-running refund or email.
    if terminal_status == "succeeded":
        transitioned = mark_succeeded(
            job_id,
            result=_slim_result_for_persist(result or {}),
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

    _charge_workspace_for_completed_job(fresh)
    _settle_wallet_hold_for_completed_job(fresh)
    if fresh.campaign_id:
        # Compute-campaign sub-job: suppress the per-child completion email
        # (the campaign owns its own summary) and re-drive the campaign so
        # the just-freed slot pulls the next chunk. Best-effort — never let
        # the drive hook break the terminal write.
        _drive_campaign_after_terminal(fresh)
    else:
        _send_completion_email(fresh)
    return fresh


def _drive_campaign_after_terminal(job: "ToolJob") -> None:
    """Fire the compute-campaign driver after a sub-job reaches terminal.

    Lazily imported to avoid an import cycle; swallows every error so a
    campaign-side fault can never corrupt the child's terminal write.
    """
    try:
        from shared.compute_campaigns import (  # noqa: PLC0415
            maybe_drive_campaign_for_job,
        )
        maybe_drive_campaign_for_job(job)
    except Exception:
        logger.warning(
            "campaign drive hook raised for job %s", job.id, exc_info=True
        )


def _charge_workspace_for_completed_job(job: "ToolJob") -> None:
    """Deduct actual Modal compute cost from the active Workspace.

    Runs after a job reaches a terminal state with measured GPU time.
    Workspace context (``target_pdb_id``, optional ``gpu_sku``) is read
    from ``inputs._workspace`` — stashed at submission time by
    ``create_job``. Legacy/orphan jobs without that context are skipped;
    the wallet hold settle in ``_settle_wallet_hold_for_completed_job``
    runs independently for both cases.

    On crossing the 80% cap warning threshold, dispatches the
    ``send_workspace_cap_warning`` email best-effort. Email and charge
    are wrapped in try/except so a flaky transactional-email provider
    never aborts terminal-state finalisation.
    """
    if job.status not in ("succeeded", "failed"):
        return
    if not job.gpu_seconds_used or job.gpu_seconds_used <= 0:
        return

    ws_ctx = (job.inputs or {}).get("_workspace") or {}
    if not isinstance(ws_ctx, dict):
        return
    target_pdb_id = ws_ctx.get("target_pdb_id")
    if not target_pdb_id:
        return  # Pre-Workspace job — never went through workspace_preflight.

    # Resolve GPU SKU: prefer the pipeline's own report (in the result
    # payload), then the value stashed at submission time, else None
    # (charge_for_job falls back to a conservative DEFAULT_USD_PER_SECOND).
    gpu_sku: Optional[str] = None
    if isinstance(job.result, dict):
        candidate = job.result.get("gpu_sku")
        if isinstance(candidate, str) and candidate:
            gpu_sku = candidate
    if not gpu_sku:
        candidate = ws_ctx.get("gpu_sku")
        if isinstance(candidate, str) and candidate:
            gpu_sku = candidate

    try:
        from shared.workspaces import (  # noqa: PLC0415
            charge_for_job,
            crossed_warn_threshold,
            get_active_workspace,
        )
    except Exception:
        logger.warning(
            "Workspace charge skipped for job %s: workspaces module import failed.",
            job.id, exc_info=True,
        )
        return

    # Snapshot the before-state so we can detect a 80% threshold crossing
    # without changing charge_for_job's signature (locked by tests).
    ws_before = get_active_workspace(job.user_id, target_pdb_id)
    if ws_before is None:
        # Workspace expired / refunded / never existed for this target.
        # charge_for_job would no-op too — short-circuit.
        return

    try:
        ws_after = charge_for_job(
            job.user_id,
            target_pdb_id,
            gpu_seconds=job.gpu_seconds_used,
            gpu_sku=gpu_sku,
            tool=job.tool,
            job_id=job.id,
        )
    except Exception:
        logger.warning(
            "charge_for_job raised for job %s; spend not recorded.",
            job.id, exc_info=True,
        )
        return
    if ws_after is None:
        return

    if not crossed_warn_threshold(
        ws_before.modal_spent_usd,
        ws_after.modal_spent_usd,
        ws_after.modal_cap_usd,
    ):
        return

    user_email = _resolve_email_for_user(job.user_id)
    if not user_email:
        return
    try:
        from shared.email import send_workspace_cap_warning  # noqa: PLC0415
        send_workspace_cap_warning(user_email=user_email, workspace=ws_after)
    except Exception:
        logger.warning(
            "Workspace cap-warning email failed for ws=%s job=%s",
            ws_after.id, job.id, exc_info=True,
        )


def _settle_wallet_hold_for_completed_job(job: "ToolJob") -> None:
    """Close out the wallet hold for a job that has reached a terminal state.

    Reads ``inputs._wallet.hold_tx_id`` (stashed at submission time by
    the tools-hub route handler) and routes by ``job.failure_class``:

    * **Billed classes** (succeeded, completed_no_yield, user_cancelled,
      safety_kill) -> ``settle_hold`` with actual GPU consumed. The SQL
      function releases surplus, charges variance up to the parameter
      scaled hard cap, or records absorbed_variance if the wallet has
      no slack to cover the deficit.
    * **user_cancelled with zero gpu_seconds** is a scoped fast path:
      routes to ``release_hold`` (typed reason) instead of
      ``settle_hold(0, ...)`` for the richer audit row. Other BILLED
      classes with zero consumption still settle at zero so a runtime
      free webhook payload cannot silently refund.
    * **Refunded classes** (infra_crash, tool_error, preflight_miss,
      no_progress_timeout, unclassified) -> ``release_hold`` (full
      refund). These are system-side failures that consumed time but
      the user is not billed.
    * **NULL failure_class** (legacy rows pre-0029, or a row that
      reached terminal without going through a mark_* helper) -> fall
      back to the legacy heuristic: refund if gpu_seconds <= 0, else
      settle.

    Idempotent. The underlying SQL functions both no-op on a second
    call against the same hold id.
    """
    ws_ctx = (job.inputs or {}).get("_wallet") or {}
    if not isinstance(ws_ctx, dict):
        return
    hold_tx_id = ws_ctx.get("hold_tx_id")
    if not hold_tx_id:
        return

    if job.status not in {"succeeded", "failed", "timeout", "cancelled"}:
        return

    gpu_seconds = max(0.0, float(job.gpu_seconds_used or 0))
    gpu_class: Optional[str] = ws_ctx.get("gpu_class")
    if isinstance(job.result, dict):
        candidate = job.result.get("gpu_class") or job.result.get("gpu_sku")
        if isinstance(candidate, str) and candidate:
            gpu_class = candidate

    # Params used for the parameter-scaled hard cap. Drop the private
    # underscore keys we stashed at submit time so the cap math only
    # sees real tool parameters.
    params = {
        k: v
        for k, v in (job.inputs or {}).items()
        if isinstance(k, str) and not k.startswith("_")
    }

    failure_reason: Optional[str] = None
    if job.status == "failed":
        if isinstance(job.error, dict):
            bucket = job.error.get("bucket")
            detail = job.error.get("detail")
            failure_reason = bucket or detail or "failed"
        else:
            failure_reason = "failed"
    elif job.status == "timeout":
        failure_reason = "timeout"
    elif job.status == "cancelled":
        failure_reason = "cancelled"

    try:
        from shared.wallet import release_hold, settle_hold  # noqa: PLC0415
    except Exception:
        logger.warning(
            "Wallet settle skipped for job %s: shared.wallet import failed.",
            job.id, exc_info=True,
        )
        return

    # ----- Classifier-driven routing (post-0029 rows) -------------------
    if job.failure_class is not None:
        if job.failure_class in _REFUNDED_FAILURE_CLASSES:
            try:
                release_hold(
                    hold_tx_id,
                    reason=failure_reason or job.failure_class,
                )
            except Exception:
                logger.warning(
                    "release_hold raised for job %s hold=%s class=%s",
                    job.id, hold_tx_id, job.failure_class, exc_info=True,
                )
            return
        if job.failure_class in _BILLED_FAILURE_CLASSES:
            # Zero consumption user cancel: route to release_hold for the
            # richer audit row (typed reason vs settle_hold's notes string).
            # Other BILLED classes (succeeded, completed_no_yield,
            # safety_kill) with zero gpu_seconds fall through to
            # settle_hold(0, ...) so the row records an authoritative zero
            # settlement instead of a refund. A succeeded webhook payload
            # arriving without runtime fields must not silently refund.
            if job.failure_class == "user_cancelled" and gpu_seconds <= 0:
                try:
                    release_hold(
                        hold_tx_id,
                        reason=failure_reason or job.failure_class,
                    )
                except Exception:
                    logger.warning(
                        "release_hold raised for job %s hold=%s class=%s "
                        "(zero-consumption fast path)",
                        job.id, hold_tx_id, job.failure_class, exc_info=True,
                    )
                return
            try:
                settle_hold(
                    hold_tx_id,
                    gpu_seconds=gpu_seconds,
                    gpu_class=gpu_class,
                    params=params,
                    failure_reason=failure_reason,
                )
            except Exception:
                logger.warning(
                    "settle_hold raised for job %s hold=%s class=%s",
                    job.id, hold_tx_id, job.failure_class, exc_info=True,
                )
            return
        # Unknown class string survived the CHECK constraint somehow.
        # Defensive: log and fall through to legacy heuristic.
        logger.warning(
            "Unknown failure_class=%s on job %s; using legacy settle heuristic.",
            job.failure_class, job.id,
        )

    # ----- Legacy heuristic (pre-0029 rows, NULL failure_class) ---------
    # No real GPU time consumed on a failure path: release the hold
    # without charging. Otherwise true-up against actual GPU.
    if gpu_seconds <= 0 and job.status in {"failed", "timeout", "cancelled"}:
        try:
            release_hold(hold_tx_id, reason=failure_reason or "no_compute")
        except Exception:
            logger.warning(
                "release_hold raised for job %s hold=%s",
                job.id, hold_tx_id, exc_info=True,
            )
        return

    try:
        settle_hold(
            hold_tx_id,
            gpu_seconds=gpu_seconds,
            gpu_class=gpu_class,
            params=params,
            failure_reason=failure_reason,
        )
    except Exception:
        logger.warning(
            "settle_hold raised for job %s hold=%s",
            job.id, hold_tx_id, exc_info=True,
        )


# Mid run progress monitoring interval. Modal pipelines emit a heartbeat
# roughly every 15 minutes; the monitor reads cumulative gpu_seconds from
# the heartbeat payload and decides whether to issue a soft warning or
# trigger a safety kill.
MID_RUN_MONITOR_INTERVAL_MINUTES = 15

# Ratios used by the mid run monitor. The 1.5x warning is non blocking;
# the 2.0x ratio triggers a hard kill so a catastrophically wrong estimate
# does not run unbounded.
_MID_RUN_WARN_RATIO = 1.5
_MID_RUN_KILL_RATIO = 2.0

# Upper bound for a single job's persisted GPU seconds (24h). Guards the
# billing column against a malformed/NaN/inf heartbeat value being
# int()-cast into the ledger.
_MAX_GPU_SECONDS = 24 * 60 * 60


def _safe_gpu_seconds_int(value) -> int:  # noqa: ANN001
    """Coerce a heartbeat GPU-seconds value to a bounded, finite int.

    NaN/inf/negative/garbage collapse to 0; anything above the 24h
    ceiling is clamped. Prevents a bad heartbeat from corrupting the
    ``gpu_seconds_used`` billing column or raising on ``int()``.
    """
    import math  # noqa: PLC0415

    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(f) or f <= 0:
        return 0
    return int(min(f, _MAX_GPU_SECONDS))


def mid_run_monitor_check(
    job_id: str,
    cumulative_gpu_seconds: float,
    *,
    modal_client=None,  # noqa: ANN001 avoid circular import of gpu.modal_client
) -> Optional[str]:
    """Inspect a running job's cumulative cost and act on overrun ratios.

    Called by the Modal heartbeat handler (or a scheduler) every 15
    minutes for any still-running job that owns a wallet hold. Returns
    one of:

    * ``None``: no action taken (ratio under the warn threshold, or
      no hold on this job, or the job is no longer running).
    * ``"warned"``: soft warning email dispatched. Idempotent on the
      stashed ``_wallet.overrun_warned`` flag in the job inputs.
    * ``"killed"``: projected cost exceeded the hard cap; the Modal
      function call was cancelled, the job row was flipped to 'failed'
      with failure_class='safety_kill', and the wallet hold was
      settled inline against consumed GPU time. Cancel is best effort:
      if Modal flakes the local terminal status still wins.

    On the ``killed`` path the monitor settles the hold inline because
    the kill closes the Modal connection and the terminal webhook may
    never land. Settlement runs through the shared
    ``_settle_wallet_hold_for_completed_job`` hook (idempotent), so a
    follow-up ``complete_job`` triggered by a late webhook still
    no-ops on the already-terminal status. The 'warned' path leaves
    settlement to ``complete_job`` as usual.

    Side effect: on every check, persists ``cumulative_gpu_seconds`` to
    ``tool_jobs.gpu_seconds_used`` so a user-initiated cancel can bill
    consumed time without waiting for a terminal Modal webhook. The
    value is a heartbeat-resolution snapshot (last value reported), so
    a cancel between heartbeats undercharges by at most one interval.
    The persist is CAS-guarded on status IN (pending, running) so a
    terminal webhook landing between the read and the write wins; the
    heartbeat's older snapshot cannot clobber the authoritative
    settle amount.
    """
    job = get_job(job_id)
    if job is None:
        return None
    if job.status not in {"pending", "running"}:
        return None

    # Persist the heartbeat-reported consumption to the row so a cancel
    # between now and the next check can bill against actual GPU spent.
    # Best-effort: a flaky update here does not gate the rest of the
    # monitor logic (warning + kill still fire from the heartbeat value).
    if cumulative_gpu_seconds and cumulative_gpu_seconds > 0:
        try:
            # CAS-guarded: skip the persist if the row terminalised
            # between the get_job read above and this write. Without the
            # guard the heartbeat's older snapshot can clobber the
            # authoritative gpu_seconds_used the terminal webhook wrote.
            _cas_update(
                job_id,
                {"gpu_seconds_used": _safe_gpu_seconds_int(cumulative_gpu_seconds)},
                allowed_current=("pending", "running"),
            )
        except Exception:
            logger.warning(
                "mid_run_monitor_check: gpu_seconds_used persist failed "
                "for job %s",
                job_id, exc_info=True,
            )

    ws_ctx = (job.inputs or {}).get("_wallet") or {}
    if not isinstance(ws_ctx, dict):
        return None
    hold_tx_id = ws_ctx.get("hold_tx_id")
    if not hold_tx_id:
        return None
    estimate_str = ws_ctx.get("estimate_usd")
    if not estimate_str:
        return None

    from decimal import Decimal  # noqa: PLC0415

    try:
        estimate = Decimal(str(estimate_str))
    except Exception:
        return None
    if estimate <= 0:
        return None

    try:
        from shared.wallet import compute_charge_usd  # noqa: PLC0415
        from shared.wallet_estimates import compute_hard_cap  # noqa: PLC0415
    except Exception:
        logger.warning(
            "mid_run_monitor_check: wallet import failed for job %s",
            job_id, exc_info=True,
        )
        return None

    gpu_class: Optional[str] = ws_ctx.get("gpu_class")
    cumulative_cost = compute_charge_usd(
        cumulative_gpu_seconds or 0, gpu_class
    )
    if cumulative_cost <= 0:
        return None

    ratio = cumulative_cost / estimate

    params = {
        k: v
        for k, v in (job.inputs or {}).items()
        if isinstance(k, str) and not k.startswith("_")
    }
    hard_cap = compute_hard_cap(job.tool, params)

    # Soft warning at 1.5x estimate. Fires once per job, gated by the
    # overrun_warned flag.
    already_warned = bool(ws_ctx.get("overrun_warned"))
    if (
        _MID_RUN_WARN_RATIO <= ratio < _MID_RUN_KILL_RATIO
        and not already_warned
    ):
        _send_overrun_warning(job, cumulative_cost, estimate)
        _stash_wallet_flag(job, "overrun_warned", True)
        return "warned"

    # Hard kill at 2x estimate. The kill is gated on the projected
    # actual exceeding the parameter-scaled hard cap so legitimate
    # tail-of-distribution runs are not aborted just for crossing the
    # 2x bar. Without the cap gate, a $0.05 MPNN whose estimate is bad
    # would be killed at $0.10 (still well below the $150 cap).
    if ratio >= _MID_RUN_KILL_RATIO and cumulative_cost >= hard_cap:
        _send_overrun_kill_notice(job, cumulative_cost, hard_cap)
        if modal_client is not None and job.modal_function_call_id:
            try:
                modal_client.cancel(job.modal_function_call_id)
            except Exception:
                logger.warning(
                    "mid_run_monitor_check: modal cancel raised for job %s",
                    job_id, exc_info=True,
                )
        # Mark the job 'failed' with a known failure_reason so the
        # terminal callback (or a follow-up call) settles the hold at
        # the cap. We use CAS so a concurrent succeeded/failed webhook
        # still wins. mark_failed writes failure_class='safety_kill'
        # via classify_terminal_state, which is a BILLED class per
        # _BILLED_FAILURE_CLASSES.
        try:
            mark_failed(
                job_id,
                error={
                    "bucket": "overrun_safety_kill",
                    "detail": (
                        "projected cost exceeded the per tool hard cap; "
                        "job cancelled by the mid run monitor"
                    ),
                },
                gpu_seconds_used=_safe_gpu_seconds_int(cumulative_gpu_seconds),
            )
        except Exception:
            logger.warning(
                "mid_run_monitor_check: mark_failed raised for job %s",
                job_id, exc_info=True,
            )
        # Settle the wallet hold inline against consumed GPU time. The
        # classifier writes failure_class='safety_kill' which is BILLED:
        # settle_hold charges the user for actual consumed GPU up to
        # the parameter scaled hard cap and refunds the surplus. We
        # mirror cancel_job / timeout_stuck_job here: re-fetch the
        # now-terminal row and hand it to the shared settle hook.
        # Doing this inline (instead of waiting for a Modal webhook
        # that may never land after the kill closes the connection)
        # closes a money leak where the hold would otherwise sit
        # unsettled. _settle_wallet_hold_for_completed_job is
        # idempotent, so a follow-up complete_job from a late webhook
        # naturally no-ops on the already-terminal status.
        fresh = get_job(job_id)
        if fresh is not None:
            _settle_wallet_hold_for_completed_job(fresh)
        return "killed"

    return None


def _stash_wallet_flag(job: "ToolJob", key: str, value) -> None:  # noqa: ANN001
    """Merge a flag into ``inputs._wallet`` and persist.

    Re-read the row's current inputs first rather than merging onto the
    (possibly stale) ``job`` snapshot: this whole-blob write is reachable
    from the same heartbeat request that just ran ``_append_heartbeat_state``,
    so merging onto a pre-heartbeat snapshot would clobber the freshly
    written ``_progress`` / ``_partial_candidates`` / ``_hb_version`` that
    REVIEW #16 protects. This fires at most once per job, so a bounded
    re-read (not a full CAS) is the proportionate guard.
    """
    fresh = get_job(job.id)
    base = (fresh.inputs if fresh is not None else None) or job.inputs or {}
    new_inputs = dict(base)
    wallet_ctx = dict(new_inputs.get("_wallet") or {})
    wallet_ctx[key] = value
    new_inputs["_wallet"] = wallet_ctx
    update_inputs(job.id, new_inputs)


def _send_overrun_warning(
    job: "ToolJob", cumulative_cost, estimate
) -> None:  # noqa: ANN001
    """Send the 1.5x soft warning email; best effort, never raises."""
    # The sender resolves the email via the service role client; passing
    # user_id keeps the call site decoupled from the auth.users lookup.
    if not job.user_id:
        return
    try:
        from shared.email import send_overrun_warning_email  # noqa: PLC0415
        send_overrun_warning_email(
            user_id=job.user_id,
            tool_slug=job.tool,
            attempted_usd=cumulative_cost,
            cap_usd=estimate,
        )
    except Exception:
        logger.warning(
            "overrun warning email failed for job %s", job.id, exc_info=True
        )


def _send_overrun_kill_notice(
    job: "ToolJob", cumulative_cost, hard_cap
) -> None:  # noqa: ANN001
    """Send the 2x safety kill notice; best effort, never raises."""
    if not job.user_id:
        return
    try:
        from shared.email import send_overrun_kill_email  # noqa: PLC0415
        send_overrun_kill_email(
            user_id=job.user_id,
            tool_slug=job.tool,
            attempted_usd=cumulative_cost,
            cap_usd=hard_cap,
        )
    except Exception:
        logger.warning(
            "overrun kill notice email failed for job %s",
            job.id, exc_info=True,
        )


def _send_completion_email(job: "ToolJob") -> None:
    """Send the job-done email if we can resolve the user's email address.

    Per-user opt-out (C5): respects
    ``auth.users.user_metadata.email_preferences.job_complete_email``.
    Missing key defaults to True; an explicit ``False`` skips the send.
    """
    if job.status not in {"succeeded", "failed"}:
        return
    user_email, user_meta = resolve_user_email_and_meta(job.user_id)
    if not user_email:
        return
    if not _email_pref_enabled(user_meta, "job_complete_email", default=True):
        logger.info(
            "Skipping job-complete email for job %s: user opt-out",
            job.id,
        )
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
    email, _meta = resolve_user_email_and_meta(user_id)
    return email


def resolve_user_email_and_meta(
    user_id: str,
) -> tuple[Optional[str], dict]:
    """Return ``(email, user_metadata)`` for ``user_id`` in one round-trip.

    Used by the completion-email path so the opt-out check does not cost
    a second ``admin.list_users()`` call after the email lookup. Returns
    ``(None, {})`` when the user cannot be found or the service client
    is unavailable.
    """
    client = get_service_client()
    if client is None:
        return None, {}
    try:
        page = client.auth.admin.list_users()
        users = getattr(page, "users", None) or page
        for u in users:
            uid = getattr(u, "id", None) or (
                u.get("id") if isinstance(u, dict) else None
            )
            if uid != user_id:
                continue
            email = getattr(u, "email", None) or (
                u.get("email") if isinstance(u, dict) else None
            )
            meta = getattr(u, "user_metadata", None)
            if meta is None and isinstance(u, dict):
                meta = u.get("user_metadata")
            return email, meta if isinstance(meta, dict) else {}
    except Exception:
        logger.warning("Could not resolve email for user %s", user_id, exc_info=True)
    return None, {}


def _email_pref_enabled(
    user_metadata: dict, key: str, *, default: bool,
) -> bool:
    """Read ``user_metadata.email_preferences[key]`` as a strict bool.

    Missing key or non-bool value falls back to ``default``. Used by the
    job-complete opt-out gate; the same shape is reused by the C6
    re-engagement sweep.
    """
    if not isinstance(user_metadata, dict):
        return default
    prefs = user_metadata.get("email_preferences")
    if not isinstance(prefs, dict):
        return default
    value = prefs.get(key)
    if isinstance(value, bool):
        return value
    return default


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
    campaign_label: Optional[str] = None,
) -> tuple[list[ToolJob], int]:
    """Paginated owner-scoped job list. Returns (rows, total_count).

    Uses PostgREST ``range()`` for offset/limit and ``count="exact"`` on
    the select so the template can render page controls without a
    separate count round-trip.

    ``campaign_label`` filters rows to a single campaign. The special
    value ``""`` (empty string) selects only the uncategorized rows
    (``campaign_label IS NULL``); a non-empty string filters by equality.
    Pass ``None`` (default) to skip filtering.
    """
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    client = get_service_client()
    if client is None:
        return [], 0
    start = (page - 1) * page_size
    end = start + page_size - 1
    try:
        query = (
            client.table(_TABLE)
            .select("*", count="exact")
            .eq("user_id", user_id)
        )
        if campaign_label is not None:
            if campaign_label == "":
                query = query.is_("campaign_label", "null")
            else:
                query = query.eq("campaign_label", campaign_label)
        response = (
            query
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


def list_campaign_labels_for_user(user_id: str) -> list[str]:
    """Return distinct non-null campaign labels for the user, sorted A-Z.

    Powers the campaign filter ``<select>`` on /jobs. Capped at 200
    distinct labels so a runaway batch script cannot blow up the page.
    """
    client = get_service_client()
    if client is None:
        return []
    try:
        response = (
            client.table(_TABLE)
            .select("campaign_label")
            .eq("user_id", user_id)
            .not_.is_("campaign_label", "null")
            .limit(2000)
            .execute()
        )
    except Exception:
        logger.warning(
            "Failed to list campaign labels for user %s",
            user_id,
            exc_info=True,
        )
        return []
    rows = getattr(response, "data", None) or []
    seen: set[str] = set()
    for r in rows:
        label = r.get("campaign_label")
        if isinstance(label, str) and label.strip():
            seen.add(label.strip())
        if len(seen) >= 200:
            break
    return sorted(seen, key=str.lower)


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
