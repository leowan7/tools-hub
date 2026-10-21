"""Stuck-job sweeper for tool_jobs.

Jobs occasionally get marooned in a non-terminal state:

  pending  — create_job wrote a row but the Modal submit ack never
             came back (network blip, transient Modal queue failure).
             Without intervention the wallet hold placed at submit
             stays on the user's ledger indefinitely.

  running  — a heartbeat fired (which transitioned the row to
             ``running``) but the pipeline died without posting a
             terminal webhook, and no further heartbeats arrived.

Both states leak a wallet hold. This module routes stale rows through the
public ``timeout_stuck_job`` helper, which first tries to RECOVER a job
whose work actually completed but whose terminal webhook was lost (it
finalizes such a job as ``succeeded`` through the normal ``complete_job``
settle path). Only jobs with nothing recoverable are CAS-transitioned to
``timeout`` and full-refunded. The summary counts recoveries separately so
a lost-webhook incident is visible in the cron log.

Configuration
-------------
``STUCK_PENDING_AGE_MINUTES``  default 30 — pending older than this is
                               considered orphaned.
``STUCK_RUNNING_AGE_HOURS``    default 6  — running with no progress
                               this long is considered dead.
                               Campaign pieces with a longer session
                               budget get that budget plus 2 h instead.

CLI entry point::

    flask jobs:sweep-stuck

Scheduled separately via Railway cron; see the daily_digest module for
the call pattern.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# Grace past a campaign piece's session budget before the sweeper calls it
# dead: queue time the poll reports as running, cold start, the design in
# flight when the budget runs out, and the final upload.
_CAMPAIGN_SESSION_SLACK_HOURS = 2

# A hold younger than this may belong to a submit whose job row is not
# written yet.
_STRANDED_HOLD_AGE_HOURS = 2
# ponytail: holds older than this are never retried; page the scan instead if a
# release can stay failing that long.
_STRANDED_HOLD_WINDOW_DAYS = 30
_HOLD_PAGE = 1000
_HOLD_CHUNK = 100


def _sweep_stranded_holds(client, now: datetime, summary: dict) -> None:
    """Retry holds that nothing closed: a failed release or settle is only logged.

    A hold is open while no row other than a ``run_debit`` names it as parent
    (shared/wallet.py::_has_open_hold). Its job is the ``tool_jobs`` row whose
    ``inputs._wallet.hold_tx_id`` is the hold id; ``wallet_transactions.job_id``
    is NULL on holds. A hold with no job is released; a hold whose job ended is
    sent back through the job's own settle route, which charges or refunds by
    the job's outcome. A hold that stays open is retried on the next run.
    """
    from shared.jobs import (  # noqa: PLC0415
        TERMINAL_STATUSES,
        ToolJob,
        _settle_wallet_hold_for_completed_job,
    )
    from shared.wallet import release_hold  # noqa: PLC0415

    hold_ids: list[str] = []
    while True:
        page = (
            client.table("wallet_transactions")
            .select("id")
            .eq("kind", "hold")
            .lt("created_at", (now - timedelta(hours=_STRANDED_HOLD_AGE_HOURS)).isoformat())
            .gte("created_at", (now - timedelta(days=_STRANDED_HOLD_WINDOW_DAYS)).isoformat())
            .order("id")
            .range(len(hold_ids), len(hold_ids) + _HOLD_PAGE - 1)
            .execute()
            .data
            or []
        )
        hold_ids.extend(str(r["id"]) for r in page)
        if len(page) < _HOLD_PAGE:
            break

    for start in range(0, len(hold_ids), _HOLD_CHUNK):
        chunk = hold_ids[start:start + _HOLD_CHUNK]
        closed = {
            str(r["parent_tx_id"])
            for r in client.table("wallet_transactions")
            .select("parent_tx_id")
            .in_("parent_tx_id", chunk)
            .neq("kind", "run_debit")
            .execute()
            .data
            or []
        }
        open_ids = [h for h in chunk if h not in closed]
        if not open_ids:
            continue
        # A failed read raises out of this pass, so a job is called missing
        # only when the read succeeded and matched nothing.
        jobs = {
            str(((r.get("inputs") or {}).get("_wallet") or {}).get("hold_tx_id")): r
            for r in client.table("tool_jobs")
            .select("*")
            .in_("inputs->_wallet->>hold_tx_id", open_ids)
            .execute()
            .data
            or []
        }
        for hold_id in open_ids:
            row = jobs.get(hold_id)
            if row is None:
                if release_hold(hold_id, reason="swept_no_job"):
                    summary["holds_retried"] += 1
                else:
                    summary["errors"].append(f"hold:{hold_id}:release failed")
            elif row.get("status") in TERMINAL_STATUSES:
                _settle_wallet_hold_for_completed_job(ToolJob.from_row(row))
                summary["holds_retried"] += 1


def _within_campaign_session(row: dict, now: datetime) -> bool:
    """True while a campaign piece is still inside its own session budget.

    A bindcraft or rfantibody campaign piece is sent to Modal with a
    ``_total_budget_hours`` of 10
    (shared/compute_campaigns.py::_campaign_session_inputs) and is planned at
    about 8 h (``_chunk_size_for``), longer than the flat running cutoff. Without
    this the sweeper refunds a piece that is still working.
    """
    if not row.get("campaign_id"):
        return False
    from shared.compute_campaigns import _campaign_session_inputs  # noqa: PLC0415

    budget = _campaign_session_inputs(str(row.get("tool") or "")).get(
        "_total_budget_hours"
    )
    started = row.get("started_at")
    if not budget or not started:
        return False
    try:
        started_at = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
    except ValueError:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    limit = timedelta(hours=budget + _CAMPAIGN_SESSION_SLACK_HOURS)
    return now - started_at < limit


def sweep_stuck_jobs(
    *,
    pending_age_minutes: Optional[int] = None,
    running_age_hours: Optional[int] = None,
) -> dict:
    """Find stale pending/running rows and terminalise them.

    Returns a summary dict suitable for logging::

        {"pending_swept": N, "running_swept": M, "recovered": K,
         "holds_retried": H, "errors": [...]}

    ``pending_swept`` / ``running_swept`` count jobs timed out (full
    refund). ``recovered`` counts jobs whose lost-webhook result was
    finalized as succeeded instead of being discarded. ``holds_retried`` counts
    open holds sent to release or settle by :func:`_sweep_stranded_holds`;
    a settle failure is only logged, so a hold still open is counted again
    on the next run.
    """
    from shared.credits import get_service_client  # noqa: PLC0415
    from shared.jobs import timeout_stuck_job  # noqa: PLC0415

    pending_minutes = pending_age_minutes
    if pending_minutes is None:
        pending_minutes = int(
            os.environ.get("STUCK_PENDING_AGE_MINUTES", "30") or 30
        )
    running_hours = running_age_hours
    if running_hours is None:
        running_hours = int(
            os.environ.get("STUCK_RUNNING_AGE_HOURS", "6") or 6
        )

    now = datetime.now(timezone.utc)
    pending_cutoff = (now - timedelta(minutes=pending_minutes)).isoformat()
    running_cutoff = (now - timedelta(hours=running_hours)).isoformat()

    summary: dict = {
        "pending_swept": 0,
        "running_swept": 0,
        "recovered": 0,
        "holds_retried": 0,
        "errors": [],
    }

    client = get_service_client()
    if client is None:
        summary["errors"].append("no service client")
        return summary

    # Pending — orphaned before Modal acked the submit, or before the
    # first heartbeat fired. created_at is the right anchor because
    # started_at is null until mark_running.
    try:
        stuck_pending = (
            client.table("tool_jobs")
            .select("id")
            .eq("status", "pending")
            .lt("created_at", pending_cutoff)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.warning("sweep: stuck-pending query failed", exc_info=True)
        stuck_pending = []

    for row in stuck_pending:
        job_id = row.get("id")
        if not job_id:
            continue
        try:
            outcome = timeout_stuck_job(job_id)
            if outcome == "recovered":
                summary["recovered"] += 1
            elif outcome == "timed_out":
                summary["pending_swept"] += 1
        except Exception as exc:
            logger.warning(
                "sweep: pending timeout raised for %s", job_id, exc_info=True,
            )
            summary["errors"].append(f"pending:{job_id}:{exc}")

    # Running — heartbeats stopped, no terminal webhook ever arrived.
    # started_at is the right anchor here: it is populated when the row
    # first transitions to running, so a row that just moved out of
    # pending will not be timed out prematurely.
    try:
        stuck_running = (
            client.table("tool_jobs")
            .select("id, tool, campaign_id, started_at")
            .eq("status", "running")
            .lt("started_at", running_cutoff)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.warning("sweep: stuck-running query failed", exc_info=True)
        stuck_running = []

    for row in stuck_running:
        job_id = row.get("id")
        if not job_id:
            continue
        if _within_campaign_session(row, now):
            continue
        try:
            outcome = timeout_stuck_job(job_id)
            if outcome == "recovered":
                summary["recovered"] += 1
            elif outcome == "timed_out":
                summary["running_swept"] += 1
        except Exception as exc:
            logger.warning(
                "sweep: running timeout raised for %s", job_id, exc_info=True,
            )
            summary["errors"].append(f"running:{job_id}:{exc}")

    try:
        _sweep_stranded_holds(client, now, summary)
    except Exception as exc:
        logger.warning("sweep: stranded-hold pass failed", exc_info=True)
        summary["errors"].append(f"holds:{exc}")

    return summary
