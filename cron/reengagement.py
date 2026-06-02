"""7-day re-engagement email for users with credits sitting unused (C6).

Sweep design
------------
Find users that match all three conditions:

  * Most recent ``tool_jobs`` row is older than 7 days.
  * ``user_wallets.balance_usd`` is greater than zero.
  * No re-engagement email has been sent in the last 30 days
    (stamped in ``auth.users.user_metadata.reengagement_email_sent_at``).

For each qualifying user, pick two GPU tools they have NOT yet run and
send one templated email linking each to the public preview at
``/tools/<slug>``. Every link carries a UTM trio so the funnel report
can attribute the return click.

CLI entry::

    flask reengagement:send

No-ops cleanly when the sweep finds nobody (e.g. brand new project, or
the sweep has already covered everyone within the cooldown window).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# Cooldown between re-engagement sends for the same user. Same number on
# both sides of the gate: we filter out rows whose stamp is younger than
# this, and we re-stamp every successful send.
REENGAGEMENT_COOLDOWN_DAYS: int = 30

# How long a user must have been silent before they qualify. Matches the
# "last_job_at < now - 7d" rule in the C6 plan.
INACTIVITY_DAYS: int = 7

# How many candidate tools to surface in one email. Two keeps the body
# scannable on mobile without padding it with marginal suggestions.
SUGGESTIONS_PER_EMAIL: int = 2

# UTM constants. Match the cross-domain analytics convention used by the
# tools-hub funnel reporter (utm_source=email, utm_medium=reengagement,
# utm_campaign=7d).
UTM_SOURCE: str = "email"
UTM_MEDIUM: str = "reengagement"
UTM_CAMPAIGN: str = "7d"


@dataclass
class Candidate:
    """One user that qualifies for the sweep, with their suggested tools."""

    user_id: str
    email: str
    user_metadata: dict = field(default_factory=dict)
    balance_usd: float = 0.0
    last_job_at: str = ""
    tools_tried: set[str] = field(default_factory=set)
    suggestions: list[dict] = field(default_factory=list)


def find_candidates(
    *,
    now: Optional[datetime] = None,
) -> list[Candidate]:
    """Return users matching the sweep criteria. Defensive against missing tables.

    The function never raises. A Supabase outage produces an empty list
    and a logged warning so the CLI entry can no-op gracefully.
    """
    from shared.credits import get_service_client  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    inactivity_cutoff = now - timedelta(days=INACTIVITY_DAYS)
    cooldown_cutoff = now - timedelta(days=REENGAGEMENT_COOLDOWN_DAYS)

    client = get_service_client()
    if client is None:
        logger.warning(
            "reengagement: no service client; sweep returns empty"
        )
        return []

    # 1. Wallets with positive balance. Cheap filter to do first because
    #    most users in the cohort will not qualify.
    funded_user_ids: list[str] = []
    try:
        rows = (
            client.table("user_wallets")
            .select("user_id,balance_usd")
            .gt("balance_usd", 0)
            .execute()
            .data
            or []
        )
        funded_user_ids = [
            r["user_id"] for r in rows if r.get("user_id")
        ]
    except Exception:
        logger.warning(
            "reengagement: user_wallets query failed", exc_info=True,
        )
        return []
    if not funded_user_ids:
        return []
    balance_by_user = {
        r["user_id"]: float(r.get("balance_usd") or 0)
        for r in rows
        if r.get("user_id")
    }

    # 2. Pull every tool_jobs row for those users. We need both the
    #    latest created_at (inactivity gate) and the set of tools they
    #    have already tried (suggestion picker).
    last_job_at: dict[str, str] = {}
    tools_tried: dict[str, set[str]] = {}
    try:
        # Supabase JS/PY clients cap returned rows at 1000 per request.
        # Page until we have everything for the funded cohort.
        offset = 0
        page_size = 1000
        while True:
            batch = (
                client.table("tool_jobs")
                .select("user_id,tool,created_at")
                .in_("user_id", funded_user_ids)
                .order("created_at", desc=True)
                .range(offset, offset + page_size - 1)
                .execute()
                .data
                or []
            )
            if not batch:
                break
            for j in batch:
                uid = j.get("user_id")
                if not uid:
                    continue
                created = j.get("created_at") or ""
                if created > last_job_at.get(uid, ""):
                    last_job_at[uid] = created
                tool = j.get("tool") or ""
                if tool:
                    tools_tried.setdefault(uid, set()).add(tool)
            if len(batch) < page_size:
                break
            offset += page_size
    except Exception:
        logger.warning(
            "reengagement: tool_jobs query failed", exc_info=True,
        )
        return []

    inactivity_iso = inactivity_cutoff.isoformat()
    inactive_user_ids = {
        uid for uid in funded_user_ids
        if last_job_at.get(uid, "") and last_job_at[uid] < inactivity_iso
    }
    if not inactive_user_ids:
        return []

    # 3. Resolve emails + user_metadata in a single admin.list_users()
    #    pass. The 30-day cooldown stamp lives in user_metadata.
    cooldown_iso = cooldown_cutoff.isoformat()
    users_by_id: dict[str, dict] = {}
    try:
        page = client.auth.admin.list_users()
        users = getattr(page, "users", None) or page
        for u in users:
            uid = getattr(u, "id", None) or (
                u.get("id") if isinstance(u, dict) else None
            )
            if uid not in inactive_user_ids:
                continue
            email = getattr(u, "email", None) or (
                u.get("email") if isinstance(u, dict) else None
            )
            meta = getattr(u, "user_metadata", None)
            if meta is None and isinstance(u, dict):
                meta = u.get("user_metadata")
            users_by_id[uid] = {
                "email": email or "",
                "user_metadata": meta if isinstance(meta, dict) else {},
            }
    except Exception:
        logger.warning(
            "reengagement: admin.list_users failed", exc_info=True,
        )
        return []

    # 4. Build the final candidate list. Drop users still in the 30-day
    #    cooldown window from a prior re-engagement send.
    out: list[Candidate] = []
    for uid in inactive_user_ids:
        user_row = users_by_id.get(uid)
        if not user_row or not user_row.get("email"):
            continue
        meta = user_row["user_metadata"]
        last_sent = ""
        if isinstance(meta, dict):
            last_sent = str(meta.get("reengagement_email_sent_at") or "")
        if last_sent and last_sent > cooldown_iso:
            continue
        out.append(Candidate(
            user_id=uid,
            email=user_row["email"],
            user_metadata=meta if isinstance(meta, dict) else {},
            balance_usd=balance_by_user.get(uid, 0.0),
            last_job_at=last_job_at.get(uid, ""),
            tools_tried=tools_tried.get(uid, set()),
        ))
    return out


def _suggested_tools_for(
    candidate: Candidate, *, base_url: str,
) -> list[dict]:
    """Pick up to ``SUGGESTIONS_PER_EMAIL`` tools the user has not tried.

    Uses ``tool_base.all_adapters()`` filtered through ``tool_enabled``
    so a flag-off tool never appears in the body. Adapter registration
    order is stable across deploys, so the first-N pick gives a
    repeatable suggestion without picking favorites in a hidden way.
    """
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    from tools import base as tool_base  # noqa: PLC0415

    out: list[dict] = []
    for adapter in tool_base.all_adapters():
        if adapter.slug in candidate.tools_tried:
            continue
        if not tool_enabled(adapter.slug):
            continue
        out.append({
            "slug":  adapter.slug,
            "label": adapter.label,
            "blurb": adapter.blurb,
            "url":   _with_utm(
                f"{base_url}/tools/{adapter.slug}",
                campaign=UTM_CAMPAIGN,
            ),
        })
        if len(out) >= SUGGESTIONS_PER_EMAIL:
            break
    return out


def _with_utm(url: str, *, campaign: str) -> str:
    """Append the standard re-engagement UTM trio to ``url``."""
    sep = "&" if "?" in url else "?"
    return (
        f"{url}{sep}utm_source={UTM_SOURCE}"
        f"&utm_medium={UTM_MEDIUM}"
        f"&utm_campaign={campaign}"
    )


def _stamp_reengagement(user_id: str, when_iso: str) -> bool:
    """Stamp ``user_metadata.reengagement_email_sent_at`` on auth.users.

    Returns True on confirmed update. The caller treats False as a
    best-effort failure: the email already went out, so worst case we
    re-send next sweep.
    """
    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return False
    try:
        # Preserve any existing user_metadata fields by reading-merging
        # before update. admin.list_users above already pulled the
        # metadata for the cohort, but we re-read to avoid a stale-write
        # if anything mutated between sweep and stamp.
        current = client.auth.admin.get_user_by_id(user_id)
        user_obj = (
            getattr(current, "user", None) or current
        )
        meta = getattr(user_obj, "user_metadata", None)
        if meta is None and isinstance(user_obj, dict):
            meta = user_obj.get("user_metadata")
        meta = dict(meta) if isinstance(meta, dict) else {}
        meta["reengagement_email_sent_at"] = when_iso
        client.auth.admin.update_user_by_id(
            user_id, {"user_metadata": meta},
        )
        return True
    except Exception:
        logger.warning(
            "reengagement: stamping user %s failed", user_id, exc_info=True,
        )
        return False


def send_reengagement() -> dict:
    """Build the sweep, send each qualifying email, return a summary dict.

    Summary shape::

        {"qualified": N, "sent": M, "skipped_no_suggestions": K,
         "errors": L}

    Always returns; failures are logged and counted but never raised so
    the cron entry stays well-behaved.
    """
    from shared.email import send_reengagement_email  # noqa: PLC0415

    base_url = os.environ.get(
        "PUBLIC_BASE_URL", "https://tools.ranomics.com"
    ).rstrip("/")
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    candidates = find_candidates(now=now)
    summary = {
        "qualified": len(candidates),
        "sent": 0,
        "skipped_no_suggestions": 0,
        "errors": 0,
    }
    for cand in candidates:
        suggestions = _suggested_tools_for(cand, base_url=base_url)
        if not suggestions:
            summary["skipped_no_suggestions"] += 1
            continue
        cand.suggestions = suggestions
        try:
            ok = send_reengagement_email(
                user_email=cand.email,
                candidate=cand,
                base_url=base_url,
            )
        except Exception:
            logger.warning(
                "reengagement: send raised for user %s",
                cand.user_id, exc_info=True,
            )
            summary["errors"] += 1
            continue
        if not ok:
            summary["errors"] += 1
            continue
        # Stamp only on confirmed send to avoid skipping a user whose
        # email actually failed to go out.
        _stamp_reengagement(cand.user_id, now_iso)
        summary["sent"] += 1
    return summary
