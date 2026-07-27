"""Data-retention sweeper + per-user erasure for the Storage buckets.

Retention policy (repo-owner decision, 2026-07): customer job inputs and
outputs, and CRO-handoff campaign payloads, are kept for
``shared.storage.RETENTION_DAYS`` (30) days and then permanently deleted.
Before this module there was no retention at all — the three buckets
(``tool-inputs`` 0006, ``tool-outputs`` 0021, ``lab-campaigns`` 0011) grew
without bound, and account deletion cascaded the DB rows but left the
Storage objects behind.

This module provides two operations, both routed through the bucket-generic
helpers in ``shared.storage`` (never the Storage client inline):

``purge_old_storage`` — the scheduled AGE sweeper. Walks the age-sweep
    buckets (``shared.storage.AGE_SWEEP_BUCKETS`` = tool-inputs + tool-outputs
    ONLY), classifies every object as expired (older than the window) or
    retained by its Storage ``created_at`` metadata, and deletes the expired
    set. ``lab-campaigns`` is deliberately EXCLUDED — it holds CRO wet-lab
    handoff shortlists (client deliverables on a months-long lifecycle) that
    must not be aged out on the 30-day clock; those are removed only by the
    per-user erasure below. DEFAULTS TO DRY-RUN: it logs what it *would*
    delete and removes nothing unless called with ``dry_run=False`` (the CLI
    exposes this as ``--apply``).

``purge_user_objects`` — per-user erasure. Given a user id, removes that
    user's objects from ALL THREE buckets (including lab-campaigns). This is
    what an account-deletion / data-deletion-request flow must call so
    deletion actually reaches Storage. Also DRY-RUN by default.

Age source
----------
Object age comes from the Storage object's own ``created_at`` metadata (as
returned by ``list()``), not from the owning ``tool_jobs`` / ``lab_campaigns``
row. Rationale:
  * It is attached to the exact object being deleted — no DB join, and no
    risk of misjudging age when the owning row was already removed.
  * It naturally catches ORPHANED objects (no DB row at all): an object with
    no owner still has a Storage ``created_at``, so it ages out on the same
    30-day clock instead of living forever. Objects younger than the window
    are always retained, giving freshly-orphaned data a grace period.
  * ``created_at`` is present on real object list entries in Supabase.
When ``created_at`` is absent we fall back to ``updated_at``; if neither can
be parsed the object is RETAINED (never deleted on unknown age).

CLI entry points (registered in app.py)::

    flask storage:purge-old               # dry run — logs, deletes nothing
    flask storage:purge-old --apply       # actually delete expired objects
    flask storage:purge-user --user-id U  # dry run — per-user erasure preview
    flask storage:purge-user --user-id U --apply

Scheduling: intended as a DAILY Railway cron (a 30-day window does not need
finer cadence, and daily keeps each run small). NOT scheduled by default —
like ``pii:purge-old``, enabling deletion of production data is an operator
decision, and the first scheduled runs should stay in dry-run until the log
output is trusted.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from shared.storage import (
    AGE_SWEEP_BUCKETS,
    CAMPAIGN_BUCKET,
    DATA_BUCKETS,
    RETENTION_DAYS,
    delete_objects,
    list_objects_recursive,
)

logger = logging.getLogger(__name__)

# Floor on the retention window so a stray ``DATA_RETENTION_DAYS=0`` (or a
# negative value) cannot collapse the cutoff onto "now" and delete everything.
# Mirrors the guardrail in cron.purge_old_events; kept well below the 30-day
# policy so an operator can tighten it for cost reasons but never near-zero.
_MIN_RETENTION_DAYS = 7


def _resolve_retention_days(retention_days: Optional[int]) -> int:
    days = retention_days
    if days is None:
        raw = os.environ.get("DATA_RETENTION_DAYS")
        if raw is None or raw.strip() == "":
            days = RETENTION_DAYS
        else:
            try:
                days = int(raw)
            except ValueError:
                logger.warning(
                    "purge-old: ignoring non-numeric DATA_RETENTION_DAYS=%r; "
                    "falling back to %d", raw, RETENTION_DAYS,
                )
                days = RETENTION_DAYS
    return max(_MIN_RETENTION_DAYS, days)


def _parse_ts(value: object) -> Optional[datetime]:
    """Parse a Storage ISO timestamp into an aware UTC datetime, else None."""
    if not value or not isinstance(value, str):
        return None
    txt = value.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _object_expired(entry: dict, cutoff: datetime) -> bool:
    """True if the object's effective age puts it before ``cutoff``.

    Effective timestamp is ``created_at`` when parseable, else ``updated_at``.
    When neither parses, the object is treated as NOT expired — we never
    delete an object whose age we cannot determine.
    """
    ts = _parse_ts(entry.get("created_at")) or _parse_ts(entry.get("updated_at"))
    if ts is None:
        return False
    return ts < cutoff


def select_expired(entries: list[dict], cutoff: datetime) -> tuple[list[dict], list[dict]]:
    """Split ``entries`` into (expired, retained) by ``cutoff``. Pure function."""
    expired: list[dict] = []
    retained: list[dict] = []
    for entry in entries:
        (expired if _object_expired(entry, cutoff) else retained).append(entry)
    return expired, retained


def purge_old_storage(
    *,
    retention_days: Optional[int] = None,
    dry_run: bool = True,
    buckets: Optional[tuple[str, ...]] = None,
    client: Optional[object] = None,
) -> dict:
    """Delete (or, when ``dry_run``, only count) objects past the window.

    Operates on ``AGE_SWEEP_BUCKETS`` (tool-inputs + tool-outputs) by default;
    ``lab-campaigns`` is intentionally NOT age-swept (CRO deliverables). Pass
    ``buckets`` only to narrow further (e.g. a single bucket in a canary).

    DEFAULTS TO DRY-RUN. Returns a summary dict suitable for logging::

        {"retention_days": N, "cutoff": "...", "dry_run": bool,
         "buckets": {bucket: {"scanned": S, "expired": E, "deleted": D}},
         "total_scanned": ..., "total_expired": ..., "total_deleted": ...,
         "errors": [...]}
    """
    days = _resolve_retention_days(retention_days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    target_buckets = buckets or AGE_SWEEP_BUCKETS

    summary: dict = {
        "retention_days": days,
        "cutoff": cutoff.isoformat(),
        "dry_run": dry_run,
        "buckets": {},
        "total_scanned": 0,
        "total_expired": 0,
        "total_deleted": 0,
        "errors": [],
    }

    for bucket in target_buckets:
        stats = {"scanned": 0, "expired": 0, "deleted": 0}
        summary["buckets"][bucket] = stats
        try:
            entries = list_objects_recursive(bucket, "", client=client)
        except Exception as exc:  # noqa: BLE001
            logger.warning("purge-old: list failed for %s", bucket, exc_info=True)
            summary["errors"].append(f"{bucket}:list:{exc}")
            continue

        expired, _retained = select_expired(entries, cutoff)
        stats["scanned"] = len(entries)
        stats["expired"] = len(expired)
        summary["total_scanned"] += len(entries)
        summary["total_expired"] += len(expired)

        if expired and not dry_run:
            try:
                deleted = delete_objects(
                    bucket, [e["path"] for e in expired], client=client
                )
                stats["deleted"] = deleted
                summary["total_deleted"] += deleted
            except Exception as exc:  # noqa: BLE001
                logger.warning("purge-old: delete failed for %s", bucket, exc_info=True)
                summary["errors"].append(f"{bucket}:delete:{exc}")

        logger.info(
            "purge-old %s bucket=%s scanned=%d expired=%d deleted=%d",
            "DRY-RUN" if dry_run else "LIVE",
            bucket, stats["scanned"], stats["expired"], stats["deleted"],
        )

    return summary


def _campaign_ids_for_user(user_id: str, client) -> list[str]:
    """Look up ``lab_campaigns.id`` for a user (lab-campaigns is not user-keyed).

    Must run BEFORE the account-deletion DB cascade removes these rows, or the
    campaign folders become unreachable for enumeration. See the wiring note in
    ``purge_user_objects``.

    Raises on a DB failure — the caller records it as an error so an
    INCOMPLETE erasure (lab-campaigns skipped) is visible in the summary
    rather than being silently reported as a clean run.
    """
    rows = (
        client.table("lab_campaigns")
        .select("id")
        .eq("user_id", user_id)
        .execute()
        .data
        or []
    )
    return [r["id"] for r in rows if r.get("id")]


def purge_user_objects(
    user_id: str,
    *,
    dry_run: bool = True,
    client: Optional[object] = None,
) -> dict:
    """Purge one user's objects from all three buckets (GDPR-style erasure).

    DEFAULTS TO DRY-RUN. This is the function an account-deletion flow should
    call (with ``dry_run=False``) so deletion reaches Storage and not just the
    DB. Because ``lab-campaigns`` objects are keyed by ``campaign_id`` (not
    ``user_id``), erasure MUST run before the ``auth.users`` row is deleted:
    once the DB cascade removes the user's ``lab_campaigns`` rows there is no
    way left to enumerate that user's campaign folders.

    Returns a summary dict::

        {"user_id": U, "dry_run": bool, "campaign_ids": [...],
         "buckets": {bucket: {"found": F, "deleted": D}}, "errors": [...]}

    Enumeration strategy per bucket:
      * tool-inputs / tool-outputs — recurse under the ``{user_id}/`` prefix
        (both encode the owner as the first path segment).
      * lab-campaigns — resolve the user's campaign ids from the DB, then
        recurse under each ``{campaign_id}/`` prefix.
    """
    summary: dict = {
        "user_id": user_id,
        "dry_run": dry_run,
        "campaign_ids": [],
        "buckets": {b: {"found": 0, "deleted": 0} for b in DATA_BUCKETS},
        "errors": [],
    }

    # Safety: validate the id BEFORE any listing. A malformed id would make a
    # bad prefix that silently under-completes a GDPR erasure (or, if empty,
    # collapses to "" and enumerates the ENTIRE bucket). Require: non-empty,
    # no path separator, and a valid UUID shape (matches auth.users.id).
    if user_id is None or not str(user_id).strip():
        summary["errors"].append("empty user_id refused")
        return summary
    user_id = str(user_id).strip()
    if "/" in user_id:
        summary["errors"].append("user_id containing '/' refused")
        return summary
    try:
        uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        summary["errors"].append("non-uuid user_id refused")
        return summary

    resolved = client
    if resolved is None:
        from shared.credits import get_service_client  # noqa: PLC0415
        resolved = get_service_client()
    if resolved is None:
        summary["errors"].append("no service client")
        return summary

    # Build the per-bucket list of prefixes to walk.
    prefix_plan: dict[str, list[str]] = {}
    for bucket in DATA_BUCKETS:
        if bucket == CAMPAIGN_BUCKET:
            try:
                campaign_ids = _campaign_ids_for_user(user_id, resolved)
            except Exception as exc:  # noqa: BLE001
                # WR-04: surface the incomplete erasure instead of hiding it.
                logger.warning(
                    "purge-user: lab_campaigns lookup failed for %s", user_id,
                    exc_info=True,
                )
                summary["errors"].append(f"{CAMPAIGN_BUCKET}:campaign-lookup:{exc}")
                prefix_plan[bucket] = []
                continue
            summary["campaign_ids"] = campaign_ids
            prefix_plan[bucket] = list(campaign_ids)
        else:
            prefix_plan[bucket] = [user_id]

    for bucket, prefixes in prefix_plan.items():
        paths: list[str] = []
        for prefix in prefixes:
            if not prefix:
                continue
            try:
                entries = list_objects_recursive(bucket, prefix, client=resolved)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "purge-user: list failed for %s:%s", bucket, prefix, exc_info=True
                )
                summary["errors"].append(f"{bucket}:{prefix}:list:{exc}")
                continue
            paths.extend(e["path"] for e in entries)

        summary["buckets"][bucket]["found"] = len(paths)
        if paths and not dry_run:
            try:
                deleted = delete_objects(bucket, paths, client=resolved)
                summary["buckets"][bucket]["deleted"] = deleted
            except Exception as exc:  # noqa: BLE001
                logger.warning("purge-user: delete failed for %s", bucket, exc_info=True)
                summary["errors"].append(f"{bucket}:delete:{exc}")

        logger.info(
            "purge-user %s user=%s bucket=%s found=%d deleted=%d",
            "DRY-RUN" if dry_run else "LIVE",
            user_id, bucket,
            summary["buckets"][bucket]["found"],
            summary["buckets"][bucket]["deleted"],
        )

    return summary
