"""Daily signup-credit sweep: expiry reminder, then expiry.

For every wallet whose signup credit has not been expired yet:

  * expiry date within ``REMINDER_DAYS_BEFORE`` days and unspent credit left:
    claim ``user_wallets.signup_credit_reminder_sent_at`` and send the
    reminder once;
  * expiry date passed: :func:`shared.wallet.expire_signup_credit`.

CLI entry::

    flask credit:expire

Safe to run more than once a day and concurrently; see
``tests/test_signup_credit_expiry.py`` for the double-run cases.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

REMINDER_DAYS_BEFORE: int = 5
_PAGE = 1000


def _due_wallets(client, until: datetime) -> list[dict]:
    rows: list[dict] = []
    while True:
        page = (
            client.table("user_wallets")
            .select("user_id,signup_credit_expires_at,signup_credit_expired_at,"
                    "signup_credit_reminder_sent_at")
            .is_("signup_credit_expired_at", "null")
            .lte("signup_credit_expires_at", until.isoformat())
            .order("user_id")
            .range(len(rows), len(rows) + _PAGE - 1)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < _PAGE:
            return rows


def _claim_reminder(client, user_id: str, now_iso: str) -> bool:
    resp = (
        client.table("user_wallets")
        .update({"signup_credit_reminder_sent_at": now_iso})
        .eq("user_id", user_id)
        .is_("signup_credit_reminder_sent_at", "null")
        .execute()
    )
    return bool(getattr(resp, "data", None))


def _release_reminder(client, user_id: str, now_iso: str) -> None:
    (
        client.table("user_wallets")
        .update({"signup_credit_reminder_sent_at": None})
        .eq("user_id", user_id)
        .eq("signup_credit_reminder_sent_at", now_iso)
        .execute()
    )


def run(*, now: Optional[datetime] = None) -> dict:
    """Send due reminders and expire due credit. Never raises."""
    from shared import wallet  # noqa: PLC0415
    from shared.credits import get_service_client  # noqa: PLC0415
    from shared.email import send_signup_credit_expiring_email  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    summary = {"reminded": 0, "expired": 0, "skipped": 0, "errors": 0}
    client = get_service_client()
    if client is None:
        logger.warning("signup_credit: no service client; nothing done")
        summary["errors"] += 1
        return summary
    try:
        due = _due_wallets(client, now + timedelta(days=REMINDER_DAYS_BEFORE))
    except Exception:
        logger.warning("signup_credit: wallet query failed", exc_info=True)
        summary["errors"] += 1
        return summary

    for w in due:
        uid = w.get("user_id")
        expires_at = wallet._parse_ts(w.get("signup_credit_expires_at"))
        if not uid or expires_at is None:
            continue
        if expires_at <= now:
            outcome = wallet.expire_signup_credit(uid)
            if outcome in ("expired", "already_expired"):
                summary["expired"] += 1
            elif outcome == "error":
                summary["errors"] += 1
            else:
                logger.info("signup_credit: expiry for %s deferred: %s", uid, outcome)
                summary["skipped"] += 1
            continue
        if w.get("signup_credit_reminder_sent_at"):
            continue
        claimed = False
        try:
            status = wallet.signup_credit_status(uid, wallet=w)
            if status is None:
                continue
            claimed = _claim_reminder(client, uid, now_iso)
            if not claimed:
                continue
            if send_signup_credit_expiring_email(
                user_id=uid,
                remaining_usd=status["remaining_usd"],
                expires_at=status["expires_at"],
            ):
                summary["reminded"] += 1
                continue
            summary["errors"] += 1
        except Exception:
            logger.warning("signup_credit: reminder failed for %s", uid, exc_info=True)
            summary["errors"] += 1
        if claimed:
            try:
                _release_reminder(client, uid, now_iso)
            except Exception:
                logger.warning("signup_credit: claim release failed for %s", uid, exc_info=True)
    return summary
