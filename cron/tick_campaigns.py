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

# Campaign states the driver still has work to do on. Includes
# ``paused_insufficient_funds`` so the tick re-drives a starved campaign and
# resumes it the moment a top-up restores the balance (nothing else triggers a
# paused campaign — no in-flight child completes to fire the inline hook).
_ACTIVE_STATES = ("funded", "running", "completing", "paused_insufficient_funds")

# Round-robin fairness (plan section 6a): when ONE user runs several campaigns at
# once, they share a single wallet balance and the per-user in-flight cap. Driving
# them in a fixed order lets the first campaign grab the shared balance every tick
# and starve the others. So we interleave a user's campaigns, letting each launch
# at most _ROUND_ROBIN_DISPATCH_CAP chunks per round, cycling until a full round
# launches nothing (all paused / at capacity / finalized). A lone campaign is
# driven at full concurrency as before. _MAX_ROUNDS bounds the tick; the per-user
# in-flight cap (GLOBAL_USER_INFLIGHT_CAP, enforced inside drive_campaign) binds
# first in practice.
_ROUND_ROBIN_DISPATCH_CAP = 4
_MAX_ROUNDS = 16


def tick_campaigns() -> dict:
    """Re-drive every in-flight campaign once, fair across a user's campaigns.

    Returns a summary dict suitable for logging::

        {"driven": N, "errors": [...]}

    ``driven`` counts distinct campaigns processed this tick (not drive calls,
    since round-robin drives a campaign across several rounds).
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
            .select("id,user_id")
            .in_("status", list(_ACTIVE_STATES))
            .execute()
            .data
            or []
        )
    except Exception:
        logger.warning("tick_campaigns: active-campaign query failed", exc_info=True)
        summary["errors"].append("active-campaign query failed")
        return summary

    # Group active campaigns by owner: a user's concurrent campaigns share the
    # round-robin over their common wallet; users are independent of each other.
    by_user: dict = {}
    for row in rows:
        campaign_id = row.get("id")
        if not campaign_id:
            continue
        by_user.setdefault(row.get("user_id"), []).append(str(campaign_id))

    driven_ids: set = set()

    def _drive(campaign_id: str, max_dispatch=None) -> int:
        driven_ids.add(campaign_id)
        try:
            return drive_campaign(campaign_id, max_dispatch=max_dispatch) or 0
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tick_campaigns: drive raised for %s", campaign_id, exc_info=True
            )
            summary["errors"].append(f"{campaign_id}:{exc}")
            return 0

    for campaign_ids in by_user.values():
        if len(campaign_ids) == 1:
            _drive(campaign_ids[0])
            continue
        for _ in range(_MAX_ROUNDS):
            launched = 0
            for campaign_id in campaign_ids:
                launched += _drive(campaign_id, max_dispatch=_ROUND_ROBIN_DISPATCH_CAP)
            if launched == 0:
                break

    # Housekeeping over paused campaigns: 14-day TTL auto-finalize + durable
    # pause-email retry (a Resend drop at pause time is re-sent here).
    try:
        from shared.compute_campaigns import sweep_paused_campaigns  # noqa: PLC0415
        sweep = sweep_paused_campaigns()
        summary["finalized"] = sweep.get("finalized", 0)
        summary["renotified"] = sweep.get("renotified", 0)
    except Exception:
        logger.warning("tick_campaigns: paused sweep failed", exc_info=True)

    summary["driven"] = len(driven_ids)
    return summary
