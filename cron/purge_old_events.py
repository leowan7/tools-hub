"""PII retention sweeper for the append-only event logs.

cso audit L5: ``public.user_events`` (ip, user_agent) and
``public.signup_rejections`` (email, ip) accumulate personal data with no
retention policy. This module deletes rows older than a configurable
window so the PII does not live forever.

Scope: the two append-only EVENT logs only. ``user_profiles`` is the
per-user account record (keyed by ``user_id``), not an event log, so it is
intentionally left alone — removing it would delete the profile, not just
age out a log line.

Configuration
-------------
``PII_RETENTION_DAYS``  default 365 — rows with ``created_at`` older than
                        this are purged.

Deletes are batched (1000 ids per statement) so a large first run does not
build one giant statement or response. Both tables have an indexed
``created_at`` (migrations 0015/0016), so the age filter is cheap.

CLI entry point::

    flask pii:purge-old            # delete
    flask pii:purge-old --dry-run  # count only, delete nothing

Scheduled separately via Railway cron; see the daily_digest / sweep-stuck
modules for the call pattern. NOT scheduled by default — enabling deletion
of production data is an operator decision.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# The event logs that carry PII and are safe to age out row-by-row.
_PII_EVENT_TABLES = ("user_events", "signup_rejections")

_BATCH = 1000
# Backstop so a pathological delete loop cannot spin forever. 100k batches
# * 1000 = 100M rows, far above any real purge; hitting it signals a fault.
_MAX_BATCHES = 100_000


def purge_old_events(
    *,
    retention_days: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Delete (or, when ``dry_run``, count) PII event rows past the window.

    Returns a summary dict suitable for logging::

        {"retention_days": N, "cutoff": "...", "dry_run": bool,
         "user_events": deleted_or_counted, "signup_rejections": ...,
         "errors": [...]}
    """
    from shared.credits import get_service_client  # noqa: PLC0415

    days = retention_days
    if days is None:
        days = int(os.environ.get("PII_RETENTION_DAYS", "365") or 365)
    # Guardrail: a stray 0/negative window would wipe the whole table.
    # Floor at 30 days so a misconfiguration cannot nuke recent data.
    days = max(30, days)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    summary: dict = {
        "retention_days": days,
        "cutoff": cutoff,
        "dry_run": dry_run,
        "errors": [],
    }
    for table in _PII_EVENT_TABLES:
        summary[table] = 0

    client = get_service_client()
    if client is None:
        summary["errors"].append("no service client")
        return summary

    for table in _PII_EVENT_TABLES:
        try:
            if dry_run:
                resp = (
                    client.table(table)
                    .select("id", count="exact")
                    .lt("created_at", cutoff)
                    .limit(1)
                    .execute()
                )
                summary[table] = getattr(resp, "count", None) or 0
                continue

            deleted = 0
            # Absolute iteration cap as a backstop: even though a failed
            # delete raises (caught below), never let a pathological
            # "same batch keeps returning" spin unbounded on a live table.
            for _ in range(_MAX_BATCHES):
                rows = (
                    client.table(table)
                    .select("id")
                    .lt("created_at", cutoff)
                    .limit(_BATCH)
                    .execute()
                    .data
                    or []
                )
                if not rows:
                    break
                ids = [r["id"] for r in rows if r.get("id")]
                if not ids:
                    break
                client.table(table).delete().in_("id", ids).execute()
                deleted += len(ids)
                if len(rows) < _BATCH:
                    break
            else:
                summary["errors"].append(
                    f"{table}:hit _MAX_BATCHES cap; more rows may remain"
                )
            summary[table] = deleted
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pii-purge: %s sweep failed", table, exc_info=True
            )
            summary["errors"].append(f"{table}:{exc}")

    return summary
