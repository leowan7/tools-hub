"""Abandoned-cart email: a $0 user who opened a form or the top-up page and left.

Runs from the five-minute tick (cron/tick_campaigns.py::tick_campaigns) and
does nothing unless ``ABANDONED_CART_EMAIL_ENABLED`` is on
(shared/feature_flags.py::abandoned_cart_email_enabled).

A user qualifies when all of these hold (``select_candidates``):

  * Their latest ``user_events`` row of the three kinds below is between
    ``MIN_WAIT`` and ``MAX_AGE`` old: ``tool_form_open``, ``credit_exhausted``,
    or a ``page_view`` whose path starts with ``/account/wallet/topup``.
  * Wallet balance is $0 or less (no wallet row counts as $0).
  * No top-up and no job at or after that latest event.
  * Not an internal account (cron/daily_digest.py::_is_internal).
  * Not unsubscribed from marketing email.
  * No abandoned-cart email in the last ``COOLDOWN`` and fewer than
    ``MAX_SENDS`` ever (``user_metadata.abandoned_cart_email_sent_at`` /
    ``abandoned_cart_email_count``).

The stamp is written before the send (``_claim``), so a stamp that fails to
write skips the user instead of re-mailing them on every tick.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

MIN_WAIT = timedelta(hours=1)
MAX_AGE = timedelta(hours=24)
COOLDOWN = timedelta(days=7)
MAX_SENDS = 2

TRIGGER_EVENTS = ("tool_form_open", "credit_exhausted")
TOPUP_PATH = "/account/wallet/topup"
TOPUP_KINDS = ("topup", "auto_reload")

STAMP_KEY = "abandoned_cart_email_sent_at"
COUNT_KEY = "abandoned_cart_email_count"

UTM_SOURCE = "email"
UTM_MEDIUM = "abandoned_cart"
UTM_CAMPAIGN = "1h"


@dataclass
class Candidate:
    user_id: str
    email: str
    tool: str = ""          # adapter slug, "" when the events named no live tool
    tool_label: str = ""


def _event_tool(event: dict) -> str:
    props = event.get("props")
    tool = props.get("tool") if isinstance(props, dict) else None
    if not tool:
        query = parse_qs(urlsplit(event.get("path") or "").query)
        tool = (query.get("tool") or [""])[0]
    return str(tool or "").strip()


def _is_trigger(event: dict) -> bool:
    if event.get("event_type") in TRIGGER_EVENTS:
        return True
    return (event.get("event_type") == "page_view"
            and str(event.get("path") or "").startswith(TOPUP_PATH))


def _count(meta: dict) -> int:
    try:
        return int(meta.get(COUNT_KEY) or 0)
    except (TypeError, ValueError):
        return MAX_SENDS


def _blocked_by_history(meta: dict, now: datetime) -> bool:
    if _count(meta) >= MAX_SENDS:
        return True
    last = str(meta.get(STAMP_KEY) or "")
    return bool(last) and last > (now - COOLDOWN).isoformat()


def select_candidates(
    *,
    events: list[dict],
    balances: dict[str, float],
    topups: list[dict],
    jobs: list[dict],
    get_user,  # noqa: ANN001 - user_id -> {"email", "user_metadata"} or None
    now: datetime,
    tool_label=lambda slug: "",  # noqa: ANN001 - slug -> label, "" if not a live tool
) -> list[Candidate]:
    """Apply the rules in the module docstring to rows already fetched.

    ``get_user`` is called only for users who pass the event, balance, top-up
    and job rules; a None return skips the user.
    """
    from cron.daily_digest import _is_internal  # noqa: PLC0415
    from shared.jobs import _email_pref_enabled  # noqa: PLC0415

    latest: dict[str, str] = {}
    tool_at: dict[str, tuple[str, str]] = {}
    for e in events:
        uid = e.get("user_id")
        ts = str(e.get("created_at") or "")
        if not uid or not ts or not _is_trigger(e):
            continue
        if ts > latest.get(uid, ""):
            latest[uid] = ts
        tool = _event_tool(e)
        if tool and tool_label(tool) and ts > tool_at.get(uid, ("", ""))[0]:
            tool_at[uid] = (ts, tool)

    newest_ok = (now - MIN_WAIT).isoformat()
    oldest_ok = (now - MAX_AGE).isoformat()

    def _after(rows: list[dict], uid: str, since: str) -> bool:
        return any(r.get("user_id") == uid and str(r.get("created_at") or "") >= since
                   for r in rows)

    out: list[Candidate] = []
    for uid, ts in latest.items():
        if not (oldest_ok <= ts <= newest_ok):
            continue
        if float(balances.get(uid) or 0) > 0:
            continue
        if _after(topups, uid, ts) or _after(jobs, uid, ts):
            continue
        user = get_user(uid) or {}
        email = user.get("email") or ""
        meta = user.get("user_metadata") if isinstance(user.get("user_metadata"), dict) else {}
        if not email or _is_internal(email):
            continue
        if not _email_pref_enabled(meta, "marketing_email", default=True):
            continue
        if _blocked_by_history(meta, now):
            continue
        tool = tool_at.get(uid, ("", ""))[1]
        out.append(Candidate(user_id=uid, email=email, tool=tool,
                             tool_label=tool_label(tool) if tool else ""))
    return out


def _live_tool_label(slug: str) -> str:
    from shared.feature_flags import tool_enabled  # noqa: PLC0415
    from tools import base as tool_base  # noqa: PLC0415

    adapter = tool_base.get(slug)
    if adapter is None or not tool_enabled(slug):
        return ""
    return adapter.label


def _rows_since(client, table: str, columns: str, since: str, *filters) -> list[dict]:  # noqa: ANN001
    rows: list[dict] = []
    page = 1000
    while True:
        q = client.table(table).select(columns).gte("created_at", since)
        for f in filters:
            q = f(q)
        batch = q.order("created_at").range(len(rows), len(rows) + page - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page:
            return rows


def _user_row(client, uid: str) -> Optional[dict]:  # noqa: ANN001
    resp = client.auth.admin.get_user_by_id(uid)
    user = getattr(resp, "user", None) or resp
    meta = getattr(user, "user_metadata", None)
    if meta is None and isinstance(user, dict):
        meta = user.get("user_metadata")
    email = getattr(user, "email", None)
    if email is None and isinstance(user, dict):
        email = user.get("email")
    return {"email": email or "", "user_metadata": dict(meta) if isinstance(meta, dict) else {}}


def find_candidates(*, now: datetime) -> list[Candidate]:
    """Fetch the rows ``select_candidates`` needs. Raises on any read failure."""
    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        raise RuntimeError("abandoned_cart: no service client")
    since = (now - MAX_AGE).isoformat()
    cols = "user_id,event_type,path,props,created_at"
    def has_user(q):  # noqa: ANN001, ANN202
        return q.not_.is_("user_id", "null")

    events = _rows_since(client, "user_events", cols, since, has_user,
                         lambda q: q.in_("event_type", list(TRIGGER_EVENTS)))
    events += _rows_since(client, "user_events", cols, since, has_user,
                          lambda q: q.eq("event_type", "page_view"),
                          lambda q: q.like("path", TOPUP_PATH + "%"))
    ids = sorted({e["user_id"] for e in events if e.get("user_id")})
    if not ids:
        return []
    wallets = (client.table("user_wallets").select("user_id,balance_usd")
               .in_("user_id", ids).execute().data or [])
    balances = {w["user_id"]: float(w.get("balance_usd") or 0) for w in wallets}
    topups = _rows_since(client, "wallet_transactions", "user_id,created_at", since,
                         lambda q: q.in_("kind", list(TOPUP_KINDS)),
                         lambda q: q.in_("user_id", ids))
    jobs = _rows_since(client, "tool_jobs", "user_id,created_at", since,
                       lambda q: q.in_("user_id", ids))
    return select_candidates(
        events=events, balances=balances, topups=topups, jobs=jobs,
        get_user=lambda uid: _user_row(client, uid), now=now,
        tool_label=_live_tool_label,
    )


def with_utm(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return (f"{url}{sep}utm_source={UTM_SOURCE}&utm_medium={UTM_MEDIUM}"
            f"&utm_campaign={UTM_CAMPAIGN}")


def _claim(user_id: str, now: datetime) -> bool:
    """Re-read the user, re-check the history rule, then write the stamp.

    Returns True only when the stamp was written; the caller sends only then.
    """
    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return False
    try:
        meta = _user_row(client, user_id)["user_metadata"]
        if _blocked_by_history(meta, now):
            return False
        meta[STAMP_KEY] = now.isoformat()
        meta[COUNT_KEY] = _count(meta) + 1
        client.auth.admin.update_user_by_id(user_id, {"user_metadata": meta})
        return True
    except Exception:
        logger.warning("abandoned_cart: stamping user %s failed", user_id, exc_info=True)
        return False


def send_abandoned_cart(*, now: Optional[datetime] = None) -> dict:
    """One sweep. Returns ``{"enabled", "qualified", "sent", "errors"}``.

    Flag off returns at once with ``enabled`` False and touches nothing.
    """
    from shared.feature_flags import abandoned_cart_email_enabled  # noqa: PLC0415

    summary = {"enabled": abandoned_cart_email_enabled(), "qualified": 0,
               "sent": 0, "errors": 0}
    if not summary["enabled"]:
        return summary
    from shared.email import RecipientRejected, send_abandoned_cart_email  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    base_url = os.environ.get("PUBLIC_BASE_URL", "https://tools.ranomics.com").rstrip("/")
    try:
        candidates = find_candidates(now=now)
    except Exception:
        logger.error("abandoned_cart: sweep failed; nothing sent", exc_info=True)
        summary["errors"] += 1
        return summary
    summary["qualified"] = len(candidates)
    for cand in candidates:
        if not _claim(cand.user_id, now):
            continue
        try:
            ok = send_abandoned_cart_email(candidate=cand, base_url=base_url)
        except RecipientRejected:
            continue  # a bad address; the stamp written by _claim stops a retry
        except Exception:
            logger.warning("abandoned_cart: send raised for %s", cand.user_id, exc_info=True)
            ok = False
        if ok:
            summary["sent"] += 1
        else:
            summary["errors"] += 1
    return summary
