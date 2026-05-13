"""Daily activity digest for tools.ranomics.com.

A single structured email sent to ``STAFF_NOTIFY_EMAIL`` each morning
covering the trailing 24 hours of activity:

  Headline       counts of signups, rejections, runs, active users
  Who's hot      high-intent users (≥3 runs, pricing view, paywall hit,
                 first-day-active business/academic signups)
  New signups    each new signup with classification + purpose snippet
  Tool activity  per-tool run counts + status breakdown
  Rejections     breakdown by reason with sample emails
  Lapsed users   users active 7-14 days ago, silent in last 24h

The digest is intentionally noisier than a per-event ping — Leo reads
this once with coffee, not throughout the day. The "Who's hot" section
is the centerpiece: it surfaces the small number of users who deserve
a manual reach-out.

CLI entry point::

    flask digest:send                   # uses DIGEST_WINDOW_HOURS (24)
    flask digest:send --window-hours 168
"""

from __future__ import annotations

import logging
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from flask import render_template

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Aggregation: pull rows for the window, fold into a Payload
# ---------------------------------------------------------------------------


@dataclass
class DigestPayload:
    """Structured input to ``templates/email/daily_digest.html``.

    Keep this serialisable: the digest CLI logs the payload as JSON on
    failure so we can replay locally without re-querying Supabase.
    """

    window_start: str = ""
    window_end: str = ""
    window_hours: int = 24

    # Headline
    signups_total: int = 0
    signups_by_quality: Dict[str, int] = field(default_factory=dict)
    rejections_total: int = 0
    rejections_by_reason: Dict[str, int] = field(default_factory=dict)
    runs_total: int = 0
    runs_by_status: Dict[str, int] = field(default_factory=dict)
    active_users: int = 0
    pricing_views: int = 0

    # Sections
    hot_users: List[Dict[str, Any]] = field(default_factory=list)
    new_signups: List[Dict[str, Any]] = field(default_factory=list)
    tool_activity: List[Dict[str, Any]] = field(default_factory=list)
    rejections: List[Dict[str, Any]] = field(default_factory=list)
    lapsed_users: List[Dict[str, Any]] = field(default_factory=list)

    # Metadata
    site_base_url: str = "https://tools.ranomics.com"


def build_payload(window_hours: int = 24) -> DigestPayload:
    """Pull and fold the trailing-``window_hours`` window into a payload.

    Defensive: returns an empty payload if Supabase isn't reachable.
    Each section is independently best-effort so a single failure
    doesn't blank the digest.
    """
    from shared.credits import get_service_client  # noqa: PLC0415

    site_base = os.environ.get(
        "PUBLIC_BASE_URL", "https://tools.ranomics.com"
    ).rstrip("/")

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=window_hours)
    lapsed_start = end - timedelta(days=14)
    lapsed_end = end - timedelta(days=7)

    payload = DigestPayload(
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        window_hours=window_hours,
        site_base_url=site_base,
    )

    client = get_service_client()
    if client is None:
        logger.warning("digest: no service client; payload will be empty")
        return payload

    window_start_iso = start.isoformat()

    # --- 1. New profiles (signups in window) -------------------------------
    profile_rows: List[dict] = []
    try:
        profile_rows = (
            client.table("user_profiles")
            .select("*")
            .gte("created_at", window_start_iso)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.warning("digest: user_profiles query failed", exc_info=True)

    # auth.users for legacy signups (no profile row): include them by
    # listing all users created in the window, then merging.
    auth_in_window: dict = {}
    try:
        page = client.auth.admin.list_users()
        users = getattr(page, "users", None) or page
        for u in users:
            created = (
                getattr(u, "created_at", None)
                or (u.get("created_at") if isinstance(u, dict) else None)
            )
            uid = (
                getattr(u, "id", None)
                or (u.get("id") if isinstance(u, dict) else None)
            )
            email = (
                getattr(u, "email", None)
                or (u.get("email") if isinstance(u, dict) else None)
            )
            if uid and created and str(created) >= window_start_iso:
                auth_in_window[uid] = {"email": email, "created_at": str(created)}
    except Exception:
        logger.warning("digest: auth admin.list_users failed", exc_info=True)

    profile_by_id = {p["user_id"]: p for p in profile_rows}
    signups_by_quality: Counter = Counter()
    new_signups: List[dict] = []
    for uid, info in auth_in_window.items():
        prof = profile_by_id.get(uid, {})
        quality = prof.get("signup_quality") or "legacy"
        signups_by_quality[quality] += 1
        new_signups.append({
            "user_id": uid,
            "email": info["email"] or uid[:8],
            "created_at": info["created_at"][:19],
            "signup_quality": quality,
            "domain_class": prof.get("domain_class") or "",
            "purpose": (prof.get("purpose") or "")[:200],
            "url": f"{site_base}/admin/users/{uid}",
        })
    payload.signups_total = sum(signups_by_quality.values())
    payload.signups_by_quality = dict(signups_by_quality)
    payload.new_signups = sorted(
        new_signups, key=lambda s: s["created_at"], reverse=True
    )

    # --- 2. Rejections in window ------------------------------------------
    rejection_rows: List[dict] = []
    try:
        rejection_rows = (
            client.table("signup_rejections")
            .select("email,reason,created_at")
            .gte("created_at", window_start_iso)
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.warning("digest: signup_rejections query failed", exc_info=True)

    reason_groups: dict = defaultdict(list)
    for r in rejection_rows:
        reason_groups[r.get("reason") or "unknown"].append(r)
    rejection_section: List[dict] = []
    for reason, entries in sorted(
        reason_groups.items(), key=lambda kv: len(kv[1]), reverse=True
    ):
        rejection_section.append({
            "reason": reason,
            "count": len(entries),
            "samples": [e.get("email") for e in entries[:5]],
        })
    payload.rejections_total = len(rejection_rows)
    payload.rejections_by_reason = {r["reason"]: r["count"] for r in rejection_section}
    payload.rejections = rejection_section

    # --- 3. Tool runs in window -------------------------------------------
    run_rows: List[dict] = []
    try:
        run_rows = (
            client.table("tool_jobs")
            .select("user_id,tool,status,gpu_seconds_used,created_at")
            .gte("created_at", window_start_iso)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.warning("digest: tool_jobs query failed", exc_info=True)

    status_counter: Counter = Counter()
    tool_counter: Counter = Counter()
    tool_status: dict = defaultdict(lambda: defaultdict(int))
    tool_gpu_seconds: dict = defaultdict(int)
    runs_by_user: Counter = Counter()
    for r in run_rows:
        status = (r.get("status") or "unknown")
        tool = (r.get("tool") or "unknown")
        status_counter[status] += 1
        tool_counter[tool] += 1
        tool_status[tool][status] += 1
        if r.get("gpu_seconds_used"):
            tool_gpu_seconds[tool] += int(r.get("gpu_seconds_used") or 0)
        if r.get("user_id"):
            runs_by_user[r["user_id"]] += 1

    payload.runs_total = sum(status_counter.values())
    payload.runs_by_status = dict(status_counter)

    tool_activity = []
    for tool, total in tool_counter.most_common():
        tool_activity.append({
            "tool": tool,
            "runs": total,
            "ok": tool_status[tool].get("succeeded", 0),
            "fail": tool_status[tool].get("failed", 0),
            "timeout": tool_status[tool].get("timeout", 0),
            "running": tool_status[tool].get("running", 0) + tool_status[tool].get("pending", 0),
            "gpu_seconds": tool_gpu_seconds.get(tool, 0),
        })
    payload.tool_activity = tool_activity

    # --- 4. user_events in window (page views, pricing, paywall hits) ------
    event_rows: List[dict] = []
    try:
        event_rows = (
            client.table("user_events")
            .select("user_id,event_type,path,created_at")
            .gte("created_at", window_start_iso)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.warning("digest: user_events query failed", exc_info=True)

    events_by_user: dict = defaultdict(lambda: defaultdict(int))
    paths_by_user: dict = defaultdict(set)
    pricing_views = 0
    active_user_ids: set = set()
    for e in event_rows:
        uid = e.get("user_id")
        etype = e.get("event_type") or ""
        if uid:
            active_user_ids.add(uid)
            events_by_user[uid][etype] += 1
            path = e.get("path") or ""
            if path:
                paths_by_user[uid].add(path)
        if etype == "pricing_view":
            pricing_views += 1
    for uid in runs_by_user:
        active_user_ids.add(uid)
    payload.active_users = len(active_user_ids)
    payload.pricing_views = pricing_views

    # --- 5. Who's hot ------------------------------------------------------
    # Build a quick email lookup for the active users so we can label
    # rows without hammering admin.get_user_by_id per uid.
    email_by_id: dict = {info["email"]: info for info in auth_in_window.values()}
    email_lookup: dict = {}
    try:
        page = client.auth.admin.list_users()
        for u in (getattr(page, "users", None) or page):
            uid = (
                getattr(u, "id", None)
                or (u.get("id") if isinstance(u, dict) else None)
            )
            email = (
                getattr(u, "email", None)
                or (u.get("email") if isinstance(u, dict) else None)
            )
            if uid:
                email_lookup[uid] = email
    except Exception:
        logger.warning("digest: email lookup failed", exc_info=True)

    # Pull profile data for active users not already loaded.
    if active_user_ids:
        try:
            extra_profiles = (
                client.table("user_profiles")
                .select("*")
                .in_("user_id", list(active_user_ids))
                .execute()
                .data
                or []
            )
            for p in extra_profiles:
                profile_by_id.setdefault(p["user_id"], p)
        except Exception:
            logger.warning("digest: active-user profile fetch failed", exc_info=True)

    new_signup_ids = set(auth_in_window.keys())
    hot_users: List[dict] = []
    for uid in active_user_ids:
        signals: List[str] = []
        n_runs = runs_by_user.get(uid, 0)
        if n_runs >= 3:
            signals.append(f"{n_runs} runs")
        if events_by_user[uid].get("pricing_view", 0) > 0:
            signals.append("viewed pricing")
        if events_by_user[uid].get("credit_exhausted", 0) > 0:
            signals.append("hit paywall")
        if uid in new_signup_ids and n_runs > 0:
            signals.append("first-day-active")
        if not signals:
            continue
        prof = profile_by_id.get(uid, {})
        tools_touched = sorted({
            p.split("/tools/", 1)[1].split("/")[0]
            for p in paths_by_user.get(uid, set())
            if "/tools/" in p
        })
        hot_users.append({
            "user_id": uid,
            "email": email_lookup.get(uid) or uid[:8],
            "domain_class": prof.get("domain_class") or "",
            "signup_quality": prof.get("signup_quality") or "legacy",
            "signals": signals,
            "tools": tools_touched,
            "runs": n_runs,
            "url": f"{site_base}/admin/users/{uid}",
        })
    hot_users.sort(key=lambda h: (len(h["signals"]), h["runs"]), reverse=True)
    payload.hot_users = hot_users

    # --- 6. Lapsed users ---------------------------------------------------
    # Users with prior activity 7-14d ago, no events or runs in the
    # current window. Useful for re-engagement outreach.
    lapsed_run_rows: List[dict] = []
    lapsed_event_rows: List[dict] = []
    try:
        lapsed_run_rows = (
            client.table("tool_jobs")
            .select("user_id,created_at")
            .gte("created_at", lapsed_start.isoformat())
            .lt("created_at", lapsed_end.isoformat())
            .execute()
            .data
            or []
        )
    except Exception:
        logger.warning("digest: lapsed tool_jobs query failed", exc_info=True)
    try:
        lapsed_event_rows = (
            client.table("user_events")
            .select("user_id,created_at")
            .gte("created_at", lapsed_start.isoformat())
            .lt("created_at", lapsed_end.isoformat())
            .execute()
            .data
            or []
        )
    except Exception:
        logger.warning("digest: lapsed user_events query failed", exc_info=True)

    last_seen: dict = {}
    for r in lapsed_run_rows + lapsed_event_rows:
        uid = r.get("user_id")
        ts = r.get("created_at") or ""
        if uid and ts > last_seen.get(uid, ""):
            last_seen[uid] = ts
    lapsed_users: List[dict] = []
    for uid, last_ts in last_seen.items():
        if uid in active_user_ids:
            continue  # still active in current window
        lapsed_users.append({
            "user_id": uid,
            "email": email_lookup.get(uid) or uid[:8],
            "last_active": last_ts[:19],
            "url": f"{site_base}/admin/users/{uid}",
        })
    lapsed_users.sort(key=lambda x: x["last_active"], reverse=True)
    payload.lapsed_users = lapsed_users[:20]

    return payload


# ---------------------------------------------------------------------------
# Rendering + sending
# ---------------------------------------------------------------------------


def render_digest_html(payload: DigestPayload) -> str:
    """Render the digest into HTML for email delivery.

    The cron context has no HTTP request, but Flask's existing
    template context processors (e.g. ``inject_workspace_context``)
    touch ``session``. Push a synthetic request context so those
    fall back cleanly to their "no session" branch.
    """
    from flask import current_app  # noqa: PLC0415

    with current_app.test_request_context("/admin/digest"):
        return render_template("email/daily_digest.html", p=payload)


def send_digest(*, window_hours: Optional[int] = None) -> bool:
    """Build, render, and send the digest. Returns True on confirmed send."""
    from shared.email import send_daily_digest  # noqa: PLC0415

    hours = window_hours
    if hours is None:
        hours = int(os.environ.get("DIGEST_WINDOW_HOURS", "24") or 24)

    payload = build_payload(window_hours=hours)
    html = render_digest_html(payload)

    when = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"tools.ranomics.com — Daily digest — {when}"

    recipient = os.environ.get("STAFF_NOTIFY_EMAIL", "leo@ranomics.com").strip()
    if not recipient:
        logger.warning("digest: STAFF_NOTIFY_EMAIL unset; skipping send")
        return False
    return send_daily_digest(
        to_email=recipient,
        subject=subject,
        html_body=html,
        payload_summary={
            "signups": payload.signups_total,
            "rejections": payload.rejections_total,
            "runs": payload.runs_total,
            "active_users": payload.active_users,
        },
    )
