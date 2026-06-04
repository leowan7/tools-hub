"""Bearer-token auth decorator for the Platform API.

Used on every ``/api/v1/*`` endpoint. Lives in parallel to the existing
``shared.auth.login_required`` (cookie/session for the web UI). The two
auth modes never overlap — cookie routes return HTML redirects on
401/403; this decorator returns JSON. Mixing them would break the contract
agents expect.

Behaviour
---------
- Accepts ``Authorization: Bearer rk_live_…``.
- Resolves via :func:`shared.api_keys.resolve_token`.
- On success populates ``flask.g.api_user_id``, ``g.api_key_role``,
  ``g.api_key_id``.
- ``role="viewer"`` keys may only call endpoints decorated with
  ``read_only=True``. Writes return ``403 forbidden_role``.

Failure responses
-----------------
- Missing header                 → 401 ``missing_credentials``
- Malformed header               → 401 ``invalid_credentials``
- Unknown / revoked token        → 401 ``invalid_api_key``
- Viewer key on a write route    → 403 ``forbidden_role``

All responses are JSON with ``Content-Type: application/json`` and
``X-Robots-Tag: noindex`` so search crawlers never index API surfaces.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Callable

from flask import g, jsonify, request

from shared.api_keys import resolve_token

logger = logging.getLogger(__name__)


def _json_error(status: int, code: str, message: str):
    resp = jsonify({"error": {"code": code, "message": message}})
    resp.status_code = status
    resp.headers["X-Robots-Tag"] = "noindex"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _parse_bearer() -> str | None:
    """Pull a Bearer token off the Authorization header.

    Returns None if missing or malformed. Does not differentiate the two
    so callers can pick a single 401 response message.
    """
    raw = request.headers.get("Authorization", "").strip()
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def api_auth_required(read_only: bool = False) -> Callable:
    """Decorator factory. ``read_only=True`` permits viewer keys.

    Usage::

        @bp.post("/experiments")
        @api_auth_required()                    # member-only write
        def create_experiment():
            ...

        @bp.get("/experiments/<id>")
        @api_auth_required(read_only=True)      # viewer-OK read
        def get_experiment(id):
            ...
    """

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            token = _parse_bearer()
            if token is None:
                return _json_error(
                    401,
                    "missing_credentials",
                    "Authorization header missing or malformed. Expected 'Authorization: Bearer rk_live_...'.",
                )

            ctx = resolve_token(token)
            if ctx is None or not ctx.is_active:
                return _json_error(
                    401,
                    "invalid_api_key",
                    "API key is invalid, revoked, or no longer recognised.",
                )

            if not read_only and not ctx.can_write:
                return _json_error(
                    403,
                    "forbidden_role",
                    "This API key is read-only. Use a 'member' key for write operations.",
                )

            g.api_user_id = ctx.user_id
            g.api_key_id = ctx.key_id
            g.api_key_role = ctx.role
            g.api_key_prefix = ctx.prefix

            return view(*args, **kwargs)

        return wrapper

    return decorator
