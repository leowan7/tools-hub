"""Request idempotency for the Ranomics tools-hub.

Stream G.1 (Wave-0 hardening). Wraps mutating tool routes so that
accidental or deliberate retries return a cached response instead of
re-running the handler — no double-charges, no duplicate GPU jobs.

Usage
-----
    from shared.credits import requires_credits
    from shared.idempotency import idempotent

    @flask_app.route("/tools/example-gpu/submit", methods=["POST"])
    @login_required
    @idempotent(ttl_seconds=60)
    @requires_credits(1, tool="example-gpu")
    def example_gpu_submit():
        ...

Decorator order matters: ``@idempotent`` is placed ABOVE
``@requires_credits`` so a replay short-circuits without touching the
credits ledger. The first request pays; cached replays do not.

Key scheme
----------
If the client sends an ``Idempotency-Key`` header, that value (prefixed
with the route) is used verbatim. Otherwise the key is
``sha256(user_id || route || body_bytes)``. Keys are scoped to a single
user — different users posting identical bodies get different keys.

TTL
---
Default 60 s. Wide enough to absorb double-clicks and network retries,
short enough that a legitimate re-submission a minute later works.

Failure modes
-------------
- Supabase unreachable: the middleware fails OPEN — the handler runs
  without dedup. Logs a warning so the outage is visible.
- Row exists with ``response_status IS NULL``: a prior request for the
  same key is still in flight. Returns HTTP 409 "request in progress".
- Row exists with a non-NULL status: replay the cached status + body.

Environment
-----------
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — same vars the rest of the
    app uses. No new configuration.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable, Optional

from flask import Response, jsonify, request

from shared.credits import get_service_client, load_user_context

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60
IDEMPOTENCY_HEADER = "Idempotency-Key"
_TABLE = "request_idempotency"


def _compute_key(user_id: str, route: str, body: bytes) -> str:
    """Derive an idempotency key from the request.

    Honours a client-supplied ``Idempotency-Key`` header if present so
    well-behaved integrations can retry on their own terms. Otherwise
    falls back to a content hash so replays of the same payload dedup
    automatically.
    """
    header_value = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
    if header_value:
        # Namespace with route + user so a client that reuses the same
        # header across endpoints (or users) doesn't cross-collide.
        raw = f"{user_id}:{route}:{header_value}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    hasher = hashlib.sha256()
    hasher.update(user_id.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(route.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(body)
    return hasher.hexdigest()


def _claim_key(
    key: str, user_id: str, route: str, ttl_seconds: int
) -> tuple[str, Optional[dict]]:
    """Try to claim the idempotency key for this request.

    Returns a tuple ``(state, row)``:
      - ``"claimed"``  — we hold the lock; run the handler
      - ``"replay"``   — ``row`` has a cached response to return
      - ``"in_flight"`` — another request is still processing
      - ``"open"``     — Supabase unavailable; proceed without dedup

    The "open" case intentionally mirrors quota.py's fail-open stance.
    Pre-revenue we would rather occasionally double-run than lock users
    out due to an infra blip.
    """
    client = get_service_client()
    if client is None:
        logger.warning(
            "Idempotency service client unavailable — proceeding without dedup."
        )
        return ("open", None)

    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)

    # Fast path: is there a live row for this key already?
    try:
        response = (
            client.table(_TABLE)
            .select("key,response_status,response_body,content_type,expires_at")
            .eq("key", key)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
    except Exception:
        logger.warning("Idempotency lookup failed — failing open.", exc_info=True)
        return ("open", None)

    live = [r for r in rows if _row_still_live(r, now)]
    if live:
        row = live[0]
        if row.get("response_status") is None:
            return ("in_flight", row)
        return ("replay", row)

    # Not claimed (or existing rows are all stale) — claim it. The PK
    # guarantees only one of concurrent callers wins.
    claim_row = {
        "key": key,
        "user_id": user_id,
        "route": route,
        "response_status": None,
        "response_body": None,
        "content_type": None,
        "expires_at": expires.isoformat(),
    }
    try:
        # Upsert so a stale row with the same key (expired) gets replaced.
        client.table(_TABLE).upsert(claim_row, on_conflict="key").execute()
    except Exception:
        logger.warning(
            "Idempotency claim insert failed — failing open.", exc_info=True
        )
        return ("open", None)

    return ("claimed", None)


def _row_still_live(row: dict, now: datetime) -> bool:
    """Return True if the row's expires_at is still in the future."""
    raw = row.get("expires_at")
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return expires > now


def _store_response(key: str, response: Response) -> None:
    """Persist the handler's response for future replays."""
    client = get_service_client()
    if client is None:
        return
    try:
        body_text = response.get_data(as_text=True)
    except Exception:
        body_text = ""
    try:
        client.table(_TABLE).update(
            {
                "response_status": int(response.status_code),
                "response_body": body_text,
                "content_type": response.headers.get("Content-Type"),
            }
        ).eq("key", key).execute()
    except Exception:
        logger.warning(
            "Failed to cache idempotent response for key %s", key, exc_info=True
        )


def _replay_response(row: dict) -> Response:
    """Reconstruct a Flask Response from a cached row."""
    status = int(row.get("response_status") or 200)
    body = row.get("response_body") or ""
    content_type = row.get("content_type") or "application/json"
    resp = Response(response=body, status=status, content_type=content_type)
    resp.headers["Idempotent-Replay"] = "true"
    return resp


def idempotent(
    *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> Callable:
    """Flask decorator — dedup replays of a mutating route.

    Place ABOVE ``@requires_credits`` so cached replays do not burn
    additional credits. The first request records the spend; subsequent
    replays return the stored response untouched.
    """

    if ttl_seconds <= 0:
        raise ValueError("idempotent ttl_seconds must be positive.")

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapped(*args: Any, **kwargs: Any):
            ctx = load_user_context()
            if ctx is None:
                # login_required should have intercepted; if not, let the
                # wrapped handler produce its own auth response.
                return f(*args, **kwargs)

            route = request.path
            body = request.get_data(cache=True) or b""
            key = _compute_key(ctx.user_id, route, body)

            state, row = _claim_key(key, ctx.user_id, route, ttl_seconds)
            _observe(state)
            if state == "replay" and row is not None:
                return _replay_response(row)
            if state == "in_flight":
                return (
                    jsonify(
                        {
                            "status": "in_progress",
                            "detail": (
                                "An earlier request with the same key "
                                "is still running. Retry in a moment."
                            ),
                        }
                    ),
                    409,
                )

            response = f(*args, **kwargs)

            # Flask handler may return tuple (body, status) or a Response.
            flask_response = _as_flask_response(response)
            if state == "claimed":
                _store_response(key, flask_response)
            return flask_response

        return wrapped

    return decorator


def _as_flask_response(returned: Any) -> Response:
    """Normalise a Flask handler return value to a Response object."""
    if isinstance(returned, Response):
        return returned
    if isinstance(returned, tuple):
        body = returned[0]
        status = returned[1] if len(returned) > 1 else 200
        if isinstance(body, Response):
            body.status_code = status
            return body
        return Response(response=body, status=status)
    return Response(response=returned)


def _observe(outcome: str) -> None:
    """Lazy-imported metrics hook. Never raises."""
    try:
        from shared.metrics import observe_idempotency_outcome  # noqa: PLC0415
        observe_idempotency_outcome(outcome)
    except Exception:  # pragma: no cover
        pass
