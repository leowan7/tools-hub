"""Compute-campaign driver tick (authoritative backstop).

The campaign driver is normally advanced inline as each sub-job reaches a
terminal state (the hook in ``shared/jobs.complete_job`` / ``cancel_job``).
This cron is the backstop: it re-drives every in-flight campaign so that a
lost inline drive, a modal-submit-failed chunk, or a campaign that stalled
between waves still makes progress and eventually finalizes. Because
``drive_campaign`` is idempotent (DB uniqueness + CAS launch), running the
tick concurrently with the inline hook is safe.

CLI entry point::

    flask campaigns:tick

Scheduled separately via Railway cron (~60-90s), like ``jobs:sweep-stuck``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Campaign states the driver still has work to do on.
_ACTIVE_STATES = ("funded", "running", "completing")


def tick_campaigns() -> dict:
    """Re-drive every in-flight campaign once.

    Returns a summary dict suitable for logging::

        {"driven": N, "errors": [...]}
    """
    from shared.credits import get_service_client  # noqa: PLC0415
    from shared.compute_campaigns import drive_campaign  # noqa: PLC0415

    summary: dict = {"driven": 0, "errors": []}
    client = get_service_client()
    if client is None:
        summary["errors"].append("no service client")
        return summary

    try:
        rows = (
            client.table("compute_campaigns")
            .select("id")
            .in_("status", list(_ACTIVE_STATES))
            .execute()
            .data
            or []
        )
    except Exception:
        logger.warning("tick_campaigns: active-campaign query failed", exc_info=True)
        summary["errors"].append("active-campaign query failed")
        return summary

    for row in rows:
        campaign_id = row.get("id")
        if not campaign_id:
            continue
        try:
            drive_campaign(str(campaign_id))
            summary["driven"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tick_campaigns: drive raised for %s", campaign_id, exc_info=True
            )
            summary["errors"].append(f"{campaign_id}:{exc}")

    return summary
