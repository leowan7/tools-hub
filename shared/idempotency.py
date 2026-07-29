"""Request idempotency for the Ranomics tools-hub.

Stream G.1 (Wave-0 hardening). Wraps mutating tool routes so that
accidental or deliberate retries return a cached response instead of
re-running the handler — no double-charges, no duplicate GPU jobs.

Usage
-----
    from shared.idempotency import idempotent

    @flask_app.route("/tools/mpnn/submit", methods=["POST"])
    @login_required
    @idempotent(ttl_seconds=60)
    @requires_wallet(tool_slug="mpnn")
    def mpnn_submit():
        ...

Decorator order matters: ``@idempotent`` is placed ABOVE
``@requires_wallet`` so a replay short-circuits without touching the
wallet ledger. The first request pays; cached replays do not.

Key scheme
----------
If the client sends an ``Idempotency-Key`` header, that value (prefixed
with the route) is used verbatim. Otherwise the key is
``sha256(user_id || route || content)``, where ``content`` is the raw body
when one is readable and a canonical encoding of the parsed form when it is
not. Keys are scoped to a single user — different users posting identical
bodies get different keys.

The form fallback is load-bearing, not a nicety. ``_enforce_csrf`` in app.py
is a ``before_request`` that reads ``request.form`` on every protected POST,
which consumes the stream, so ``request.get_data()`` here returns ``b""`` for
essentially every browser form submission. Hashing that empty body reduces the
key to ``(user, route)`` and makes any two different submissions to the same
route inside the TTL collide.

TTL
---
Default 60 s. Wide enough to absorb double-clicks and network retries,
short enough that a legitimate re-submission a minute later works.

Failures are not cached
-----------------------
A handler response of 4xx/5xx releases the claim instead of storing it. The
request did not happen, so there is nothing to deduplicate, and caching it
would block the user's corrected retry for the rest of the TTL.

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


# Form fields excluded from the content hash. The CSRF token is rotated
# independently of what the user typed, so including it would make two
# genuinely identical submissions hash differently and defeat the dedup.
_KEY_EXCLUDED_FIELDS = frozenset({"_csrf"})


def _form_fingerprint() -> bytes:
    """A canonical, order-independent encoding of the parsed form.

    Used when the raw body has already been consumed. Fully sorted, values and
    file parts alike, so two identical submissions hash identically regardless
    of the order the browser serialised them in. Built from ``lists()`` rather
    than ``to_dict()`` so a multi-valued field (the launch screen's ``tools``
    checkboxes) contributes every value: collapsing to the first would make
    "run rfdiffusion" and "run rfdiffusion + 6 more" the same request.

    File parts contribute field name, filename, and byte length only. The bytes
    themselves are deliberately not hashed: reading them here would either cost
    a full pass over every upload on every request or consume the stream the
    handler needs. The honest consequence is that two submissions identical in
    every form field AND uploading files of the same name and size within the
    TTL still collide. That combination is a double-click far more often than
    it is two distinct jobs.
    """
    parts: list[bytes] = []
    try:
        for field, values in sorted(request.form.lists()):
            if field in _KEY_EXCLUDED_FIELDS:
                continue
            for value in sorted(values):
                parts.append(f"{field}={value}".encode("utf-8"))
    except Exception:  # pragma: no cover - defensive; form parsing already ran
        logger.warning("Idempotency form fingerprint failed.", exc_info=True)
        return b""
    try:
        # Build the descriptors first, then sort THOSE. Sorting the raw
        # (name, FileStorage) pairs raises TypeError the moment one field name
        # repeats, because Python falls through to comparing two FileStorage
        # objects; sorting by name alone avoids the crash but leaves repeated
        # parts in wire order, so the same two files posted in the other order
        # would hash differently and fail to dedup. Sorting the finished
        # strings gets both.
        file_parts = []
        for field, storage in request.files.items(multi=True):
            size = -1
            try:
                stream = storage.stream
                pos = stream.tell()
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(pos)
            except Exception:
                size = -1
            file_parts.append(
                f"{field}@{storage.filename or ''}:{size}".encode("utf-8")
            )
        parts.extend(sorted(file_parts))
    except Exception:  # pragma: no cover
        logger.warning("Idempotency file fingerprint failed.", exc_info=True)
    return b"\0".join(parts)


def _compute_key(user_id: str, route: str, body: bytes) -> str:
    """Derive an idempotency key from the request.

    Honours a client-supplied ``Idempotency-Key`` header if present so
    well-behaved integrations can retry on their own terms. Otherwise
    falls back to a content hash so replays of the same payload dedup
    automatically.

    When the raw body is empty the content hash falls back to the parsed form.
    This is the normal case for every cookie-authenticated form POST in this
    app, not an edge case: ``app.py``'s ``_enforce_csrf`` ``before_request``
    reads ``request.form``, which consumes the stream, so by the time this runs
    ``request.get_data()`` returns ``b""``. Without the fallback the key
    degenerates to ``sha256(user_id + route)`` and any two DIFFERENT
    submissions to the same route inside the TTL collide: the second is treated
    as a duplicate, never runs, and replays the first one's response. Do not
    "simplify" this back to hashing ``body`` alone.
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
    hasher.update(body if body else _form_fingerprint())
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
    #
    # select("*"), not an explicit column list. PostgREST projects exactly the
    # columns asked for, so an explicit list silently drops any column added
    # later -- which is what made the `location` replay fix a no-op until this
    # changed. Naming `location` explicitly is worse than the wildcard: before
    # migration 0038 is applied PostgREST 400s on the unknown column, and the
    # bare except below fails OPEN, so every double-submit would re-run its
    # handler and place a SECOND wallet hold plus a SECOND Modal job.
    try:
        response = (
            client.table(_TABLE)
            .select("*")
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
    fields = {
        "response_status": int(response.status_code),
        "response_body": body_text,
        "content_type": response.headers.get("Content-Type"),
    }
    # Redirects carry their destination in a header, not the body, so a replay
    # that drops it returns a 302 to nowhere. Only set the column when there is
    # one, so non-redirect routes are untouched.
    location = response.headers.get("Location")
    if location:
        fields["location"] = location

    try:
        client.table(_TABLE).update(fields).eq("key", key).execute()
    except Exception:
        # Tolerate a deploy that lands before migration 0038: retry without the
        # new column rather than losing the cache entirely, because an
        # uncached response means the guarded handler re-runs on replay.
        if "location" not in fields:
            logger.warning(
                "Failed to cache idempotent response for key %s", key, exc_info=True
            )
            return
        fields.pop("location", None)
        try:
            client.table(_TABLE).update(fields).eq("key", key).execute()
        except Exception:
            logger.warning(
                "Failed to cache idempotent response for key %s", key, exc_info=True
            )


def _release_key(key: str) -> bool:
    """Drop a claim so a corrected retry can run. True iff the row is gone.

    Called instead of :func:`_store_response` when the handler failed. The row
    MUST go rather than simply be left unwritten: a claim with
    ``response_status IS NULL`` reads as "in flight" to the next request, which
    would answer every retry with a 409 for the rest of the TTL.
    """
    client = get_service_client()
    if client is None:
        return False
    try:
        response = client.table(_TABLE).delete().eq("key", key).execute()
    except Exception:
        logger.warning(
            "Failed to release idempotency claim for key %s", key, exc_info=True
        )
        return False
    # The deleted rows, not merely "the call did not raise". A delete that
    # matched nothing leaves the claim in place with response_status NULL, and
    # reporting success for that would skip the caller's cache fallback and
    # hand every retry a 409 until the TTL expired -- the outcome this
    # function exists to prevent.
    return bool(getattr(response, "data", None))


def _replay_response(row: dict) -> Response:
    """Reconstruct a Flask Response from a cached row."""
    status = int(row.get("response_status") or 200)
    body = row.get("response_body") or ""
    content_type = row.get("content_type") or "application/json"
    resp = Response(response=body, status=status, content_type=content_type)
    location = row.get("location")
    if location:
        resp.headers["Location"] = location
    resp.headers["Idempotent-Replay"] = "true"
    return resp


def idempotent(
    *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> Callable:
    """Flask decorator — dedup replays of a mutating route.

    Place ABOVE ``@requires_wallet`` so cached replays do not place a
    second wallet hold. The first request places the hold; subsequent
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
                if flask_response.status_code >= 400:
                    # A failed request did not happen, so there is nothing to
                    # be idempotent about. Caching it means a user refused for
                    # insufficient balance, who then tops up in another tab,
                    # gets the same refusal replayed for the rest of the TTL.
                    # If the release fails, fall back to caching: replaying a
                    # 400 is a worse experience than a fresh attempt, but it is
                    # better than the 409 "still in progress" that an orphaned
                    # claim would produce until it expired.
                    if not _release_key(key):
                        _store_response(key, flask_response)
                else:
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
