"""API-key minting, resolution, and revocation for the Platform API.

Stores per-user Bearer tokens used by ``shared.api_auth`` to authenticate
``/api/v1/*`` requests. Plaintext is only ever returned to the caller at
the moment of minting; what we persist is its SHA-256 digest.

Token format
------------
    rk_live_<22 url-safe base64 chars>

Conventions match Stripe / Resend (``rk_live_…``). A future ``rk_test_``
prefix can be added without schema changes.

Usage
-----
    # /account/api-keys
    plaintext, prefix, webhook_secret = mint_token(
        user_id=u, role="member", label="my-laptop"
    )
    # plaintext shown once; prefix stored for display.
    # webhook_secret is the per-tenant HMAC key (CR-01). It is non-None
    # ONLY on the first mint for a user — subsequent mints keep the
    # existing secret and the tuple's third element is None. To force a
    # new secret, call :func:`rotate_webhook_secret`.

    # /api/v1/experiments
    ctx = resolve_token(bearer_value)
    if ctx is None or ctx.revoked_at:
        return jsonify({"error": "invalid_api_key"}), 401
    g.api_user_id = ctx.user_id
    g.api_key_role = ctx.role

Per-tenant webhook signing (CR-01)
----------------------------------
A single ``WEBHOOK_SIGNING_SECRET`` env var used to sign every webhook,
which meant two API customers could each forge events the other would
accept as authentic. CR-01 moves the secret to ``user_profiles.
webhook_signing_secret`` so each tenant gets their own HMAC material.
This module owns:

- :func:`_new_webhook_secret` — mints a ``whsec_<22 url-safe>`` key
- :func:`ensure_webhook_secret` — on first mint, persists + returns it
- :func:`rotate_webhook_secret` — forces a new secret + returns it
- :func:`resolve_webhook_secret` — service-role lookup used at dispatch

Security notes
--------------
- The PRNG is :func:`secrets.token_urlsafe`.
- ``hashed_token`` is the lowercase hex SHA-256 of the plaintext.
- Equality is left to the database index — Postgres compares full
  digests, no leaky early-exit. We never call hmac.compare_digest on
  the plaintext.
- The plaintext is never written to logs, never sent in webhooks, and
  never echoed back after the mint call.
- Revoked keys remain in the table for audit; ``resolve_token`` filters
  them out via ``revoked_at IS NULL``.
- Webhook secrets follow the same plaintext-shown-once rule. Migration
  0028 revokes SELECT on the secret column for anon and authenticated
  roles, so only the service-role path can read it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.credits import get_service_client

logger = logging.getLogger(__name__)

_TABLE = "api_keys"
_USER_PROFILES_TABLE = "user_profiles"

_TOKEN_PREFIX = "rk_live_"
_TOKEN_RAND_BYTES = 16  # token_urlsafe(16) → 22 chars

# CR-01: per-tenant webhook signing secret. Stripe convention is
# ``whsec_…``; mirrored here so receivers already wired for Stripe-style
# verification code recognise the shape. Stored in plaintext on
# user_profiles (HMAC needs the literal key) but column-level grants in
# migration 0028 hide it from anon and authenticated reads.
_WEBHOOK_SECRET_PREFIX = "whsec_"
_WEBHOOK_SECRET_RAND_BYTES = 16  # token_urlsafe(16) → 22 chars
# FIX HI-04 (fresh-review): only persist the literal scheme prefix, not
# any plaintext randomness. Storing 4 random chars (the prior value of
# 12) was inconsistent with the module docstring's "plaintext is never
# persisted" guarantee. Keyspace was still ~2^108 with 4 bits leaked,
# but Stripe-pattern hygiene calls for scheme-only display.
_PREFIX_DISPLAY_LEN = len(_TOKEN_PREFIX)  # "rk_live_" — no plaintext bits

VALID_ROLES = frozenset({"member", "viewer"})


# Throwaway env var for local-dev rate guarding only; the API itself is
# always behind ENABLE_PLATFORM_API which is checked in the blueprint.
_MAX_KEYS_PER_USER = int(os.environ.get("PLATFORM_API_MAX_KEYS_PER_USER", "10"))


@dataclass(frozen=True)
class APIKeyContext:
    """Resolved view of an api_keys row.

    Returned by :func:`resolve_token`. Never carries the plaintext token
    — by the time we have a context, the caller has already authenticated
    successfully.
    """

    key_id: str
    user_id: str
    role: str
    prefix: str
    label: Optional[str]
    created_at: Optional[str]
    last_used_at: Optional[str]
    revoked_at: Optional[str]

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @property
    def can_write(self) -> bool:
        return self.role == "member"


def _read_throttle_seconds() -> int:
    """Read and validate the last_used_at touch throttle.

    FIX ME-06 (fresh-review): a misconfigured 0 or negative value makes
    every call stale (``age >= 0`` always true), defeating the throttle
    and flooding the DB with UPDATEs. Validate at import; fall back to
    the documented default with a warning so operator typos are visible.
    """
    raw = os.environ.get("API_KEY_LAST_USED_THROTTLE_SECONDS", "60")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "API_KEY_LAST_USED_THROTTLE_SECONDS=%r is not an integer; "
            "defaulting to 60",
            raw,
        )
        return 60
    if value < 1:
        logger.warning(
            "API_KEY_LAST_USED_THROTTLE_SECONDS=%d is < 1 (defeats throttle); "
            "defaulting to 60",
            value,
        )
        return 60
    return value


_LAST_USED_THROTTLE_SECONDS = _read_throttle_seconds()


def _last_used_is_stale(last_used_at_raw: Any) -> bool:
    """Return True if last_used_at is None or older than the throttle window.

    Tolerant of supabase ISO 8601 strings and missing values. A bad value
    falls through to ``True`` (stale) so we still update — better to bump
    last_used twice than to miss every update because of a parse error.
    """
    if not last_used_at_raw:
        return True
    if isinstance(last_used_at_raw, datetime):
        last_used = last_used_at_raw
    else:
        try:
            last_used = datetime.fromisoformat(
                str(last_used_at_raw).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            return True
    if last_used.tzinfo is None:
        last_used = last_used.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last_used).total_seconds()
    return age >= _LAST_USED_THROTTLE_SECONDS


# ---------------------------------------------------------------------------
# Token hashing
# ---------------------------------------------------------------------------


def _hash_token(plaintext: str) -> str:
    """Return the lowercase hex SHA-256 of the plaintext token."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _new_plaintext() -> str:
    return _TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_RAND_BYTES)


def _looks_like_platform_token(value: str) -> bool:
    """Cheap structural check to short-circuit obviously-non-Ranomics tokens.

    A real validator would be the DB lookup; this just stops us hashing
    arbitrary user-supplied strings unnecessarily.
    """
    if not value or len(value) < len(_TOKEN_PREFIX) + 8:
        return False
    return value.startswith(_TOKEN_PREFIX)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def _new_webhook_secret() -> str:
    """Generate a fresh per-tenant webhook signing secret.

    Format: ``whsec_<22 url-safe base64 chars>``. The literal ``whsec_``
    prefix matches Stripe / Resend so receivers' Stripe-style verifier
    code recognises it without retraining.
    """
    return _WEBHOOK_SECRET_PREFIX + secrets.token_urlsafe(_WEBHOOK_SECRET_RAND_BYTES)


def _write_webhook_secret(*, user_id: str, secret: str) -> bool:
    """Persist a webhook secret on user_profiles. Service-role only.

    Returns True on a successful single-row update. Logs failures but
    never raises — the caller (``ensure_webhook_secret`` /
    ``rotate_webhook_secret``) decides how to surface the error.
    """
    client = get_service_client()
    if client is None:
        logger.error("write_webhook_secret: service client unavailable")
        return False
    prefix = secret[: len(_WEBHOOK_SECRET_PREFIX)]
    try:
        response = (
            client.table(_USER_PROFILES_TABLE)
            .update(
                {
                    "webhook_signing_secret": secret,
                    "webhook_secret_prefix": prefix,
                    "webhook_secret_rotated_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }
            )
            .eq("user_id", user_id)
            .execute()
        )
    except Exception:
        logger.error(
            "write_webhook_secret: update failed for %s", user_id, exc_info=True
        )
        return False
    return bool(list(getattr(response, "data", None) or []))


def ensure_webhook_secret(*, user_id: str) -> Optional[str]:
    """Mint a webhook secret if the user has none. Returns plaintext on
    a fresh mint; None if a secret already exists (don't expose it).

    Idempotent on the existing-secret path so callers can call once per
    mint_token without inadvertently rotating. Use
    :func:`rotate_webhook_secret` to force a new one.
    """
    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table(_USER_PROFILES_TABLE)
            .select("webhook_signing_secret")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.error(
            "ensure_webhook_secret: lookup failed for %s", user_id, exc_info=True
        )
        return None
    rows = list(getattr(response, "data", None) or [])
    if rows and rows[0].get("webhook_signing_secret"):
        # Existing secret — don't expose. Caller's UI shows the prefix
        # via a separate selector.
        return None
    if not rows:
        # No user_profiles row at all. Shouldn't happen for a logged-in
        # user (created at signup) but log it loud — if a future user
        # path skips signup, we want this noisy, not silent.
        logger.error(
            "ensure_webhook_secret: no user_profiles row for %s; "
            "cannot persist webhook secret",
            user_id,
        )
        return None
    secret = _new_webhook_secret()
    if not _write_webhook_secret(user_id=user_id, secret=secret):
        return None
    return secret


def rotate_webhook_secret(*, user_id: str) -> Optional[str]:
    """Force a new webhook signing secret. Returns the new plaintext.

    All previously-issued secrets become invalid immediately for HMAC
    verification on receivers. Caller's UI must surface this clearly.
    """
    secret = _new_webhook_secret()
    if not _write_webhook_secret(user_id=user_id, secret=secret):
        return None
    return secret


def resolve_webhook_secret(*, user_id: str) -> Optional[str]:
    """Look up a user's webhook signing secret. Service-role only path.

    Returns None if the user has no per-tenant secret yet — the caller
    (``shared.webhooks._signing_secret_for_user``) decides whether to
    fall back to the global env-var secret during the rollout window.
    """
    if not user_id:
        return None
    client = get_service_client()
    if client is None:
        logger.warning("resolve_webhook_secret: service client unavailable")
        return None
    try:
        response = (
            client.table(_USER_PROFILES_TABLE)
            .select("webhook_signing_secret")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.warning(
            "resolve_webhook_secret: lookup failed for %s", user_id, exc_info=True
        )
        return None
    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return None
    secret = rows[0].get("webhook_signing_secret")
    return secret or None


def get_webhook_secret_display(*, user_id: str) -> Optional[dict[str, Any]]:
    """Return display-only metadata for the dashboard.

    Surfaces the prefix and rotated_at without exposing the secret
    plaintext. Returns None if no secret has been minted yet.
    """
    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table(_USER_PROFILES_TABLE)
            .select("webhook_secret_prefix,webhook_secret_rotated_at")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.warning(
            "get_webhook_secret_display: lookup failed for %s",
            user_id,
            exc_info=True,
        )
        return None
    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return None
    prefix = rows[0].get("webhook_secret_prefix")
    if not prefix:
        return None
    return {
        "prefix": prefix,
        "rotated_at": rows[0].get("webhook_secret_rotated_at"),
    }


def mint_token(
    *,
    user_id: str,
    role: str = "member",
    label: Optional[str] = None,
) -> Optional[tuple[str, str, Optional[str]]]:
    """Create a new API key. Returns ``(plaintext, prefix, webhook_secret)``.

    The plaintext is the only side-effect the caller can expose to the
    user — store nothing else. ``webhook_secret`` is non-None ONLY on the
    first mint per user (CR-01) — see :func:`ensure_webhook_secret`.

    Returns None on database failure or if the user has hit
    ``PLATFORM_API_MAX_KEYS_PER_USER`` active keys.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role!r}")

    client = get_service_client()
    if client is None:
        logger.error("mint_token: service client unavailable")
        return None

    # Enforce per-user active-key cap.
    try:
        existing = (
            client.table(_TABLE)
            .select("id")
            .eq("user_id", user_id)
            .is_("revoked_at", "null")
            .execute()
        )
    except Exception:
        logger.error("mint_token: count query failed", exc_info=True)
        return None

    active = list(getattr(existing, "data", None) or [])
    if len(active) >= _MAX_KEYS_PER_USER:
        logger.info(
            "mint_token: user %s already has %d active keys (cap=%d)",
            user_id,
            len(active),
            _MAX_KEYS_PER_USER,
        )
        return None

    plaintext = _new_plaintext()
    prefix = plaintext[:_PREFIX_DISPLAY_LEN]
    hashed = _hash_token(plaintext)

    row = {
        "user_id": user_id,
        "hashed_token": hashed,
        "prefix": prefix,
        "role": role,
        "label": (label or "").strip()[:120] or None,
    }
    try:
        response = client.table(_TABLE).insert(row).execute()
    except Exception:
        logger.error("mint_token: insert failed", exc_info=True)
        return None

    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return None

    # CR-01: mint a per-tenant webhook signing secret on the first
    # key for this user. ensure_webhook_secret is a no-op (returns
    # None) if the user already has one — never silently rotate.
    webhook_secret = ensure_webhook_secret(user_id=user_id)
    return (plaintext, prefix, webhook_secret)


def resolve_token(plaintext: str) -> Optional[APIKeyContext]:
    """Resolve a Bearer plaintext to an active APIKeyContext.

    Returns None for: malformed input, unknown token, or revoked key.
    Updates ``last_used_at`` on success — best-effort, never blocks.
    """
    if not _looks_like_platform_token(plaintext):
        return None

    client = get_service_client()
    if client is None:
        logger.warning("resolve_token: service client unavailable")
        return None

    hashed = _hash_token(plaintext)
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("hashed_token", hashed)
            .is_("revoked_at", "null")
            .limit(1)
            .execute()
        )
    except Exception:
        logger.warning("resolve_token: lookup failed", exc_info=True)
        return None

    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return None
    row = rows[0]

    # Best-effort last_used touch; swallow errors so a hot-path DB blip
    # doesn't 5xx the agent.
    #
    # FIX #9 (validation review): throttle to once per
    # _LAST_USED_THROTTLE_SECONDS so a tight polling loop doesn't issue
    # an UPDATE per request. Also filter ``revoked_at IS NULL`` on the
    # write so a token revoked between SELECT and UPDATE doesn't get
    # touched — would mislead the audit ("revoked key just used?").
    if _last_used_is_stale(row.get("last_used_at")):
        try:
            (
                client.table(_TABLE)
                .update(
                    {"last_used_at": datetime.now(timezone.utc).isoformat()}
                )
                .eq("id", row["id"])
                .is_("revoked_at", "null")
                .execute()
            )
        except Exception:
            logger.debug(
                "resolve_token: last_used_at update failed", exc_info=True
            )

    return APIKeyContext(
        key_id=str(row["id"]),
        user_id=str(row["user_id"]),
        role=row["role"],
        prefix=row["prefix"],
        label=row.get("label"),
        created_at=row.get("created_at"),
        last_used_at=row.get("last_used_at"),
        revoked_at=row.get("revoked_at"),
    )


def list_keys(user_id: str) -> list[APIKeyContext]:
    """Return the user's API keys (active first, then revoked)."""
    client = get_service_client()
    if client is None:
        return []
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception:
        logger.warning("list_keys: query failed", exc_info=True)
        return []

    rows = list(getattr(response, "data", None) or [])
    contexts = [
        APIKeyContext(
            key_id=str(r["id"]),
            user_id=str(r["user_id"]),
            role=r["role"],
            prefix=r["prefix"],
            label=r.get("label"),
            created_at=r.get("created_at"),
            last_used_at=r.get("last_used_at"),
            revoked_at=r.get("revoked_at"),
        )
        for r in rows
    ]
    # Active first, newest first within each group. Two stable passes:
    # newest-first by created_at, then active-first — the stable sort
    # preserves the newest-first ordering inside each group.
    contexts.sort(key=lambda c: c.created_at or "", reverse=True)
    contexts.sort(key=lambda c: c.revoked_at is not None)
    return contexts


def revoke_key(*, key_id: str, user_id: str) -> bool:
    """Mark a key revoked. Scoped to user_id so one user can't revoke
    another's key by id-guessing."""
    client = get_service_client()
    if client is None:
        return False
    try:
        response = (
            client.table(_TABLE)
            .update({"revoked_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", key_id)
            .eq("user_id", user_id)
            .is_("revoked_at", "null")
            .execute()
        )
    except Exception:
        logger.error("revoke_key: update failed", exc_info=True)
        return False
    return bool(list(getattr(response, "data", None) or []))
