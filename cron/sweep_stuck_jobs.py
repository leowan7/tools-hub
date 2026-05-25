"""Stuck-job sweeper for tool_jobs.

Jobs occasionally get marooned in a non-terminal state:

  pending  — create_job wrote a row but the Modal submit ack never
             came back (network blip, transient Modal queue failure).
             Without intervention the wallet hold placed at submit
             stays on the user's ledger indefinitely.

  running  — a heartbeat fired (which transitioned the row to
             ``running``) but the pipeline died without posting a
             terminal webhook, and no further heartbeats arrived.

Both states leak a wallet hold. This module CAS-transitions stale rows
to ``timeout`` and routes them through ``_settle_wallet_hold_for_completed_job``
via the public ``timeout_stuck_job`` helper.

Configuration
-------------
``STUCK_PENDING_AGE_MINUTES``  default 30 — pending older than this is
                               considered orphaned.
``STUCK_RUNNING_AGE_HOURS``    default 6  — running with no progress
                               this long is considered dead.

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


def sweep_stuck_jobs(
    *,
    pending_age_minutes: Optional[int] = None,
    running_age_hours: Optional[int] = None,
) -> dict:
    """Find stale pending/running rows and terminalise them.

    Returns a summary dict suitable for logging::

        {"pending_swept": N, "running_swept": M, "errors": [...]}
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
            if timeout_stuck_job(job_id):
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
            .select("id")
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
        try:
            if timeout_stuck_job(job_id):
                summary["running_swept"] += 1
        except Exception as exc:
            logger.warning(
                "sweep: running timeout raised for %s", job_id, exc_info=True,
            )
            summary["errors"].append(f"running:{job_id}:{exc}")

    return summary
