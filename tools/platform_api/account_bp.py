"""Account-facing Platform API routes: the AI-plugin manifest and the
``/account/api-keys`` key-management surface.

Extracted verbatim from ``create_app()`` (Commit 8 of the app.py -> blueprints
refactor). Registered ONLY when ``ENABLE_PLATFORM_API=1`` (exactly like the
``/api/v1`` surface), so with the flag off the whole surface 404s.

CSRF posture is UNCHANGED by the move: the ``/account/api-keys/*`` POSTs are
cookie-authenticated but self-enforce their own per-session token (FIX HI-03)
via :func:`_csrf_ok`, and remain exempt from the global web-UI CSRF guard by
the existing ``path.startswith("/account/api-keys")`` rule in
``app._csrf_request_is_exempt``. This blueprint is therefore deliberately NOT
added to that guard's blueprint allowlist.
"""

from __future__ import annotations

import hmac as _hmac
import secrets as _secrets
from typing import Optional

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from shared.api_keys import (
    VALID_ROLES,
    list_keys,
    mint_token,
    revoke_key,
)
from shared.auth import login_required
from shared.credits import load_user_context

platform_account_bp = Blueprint("platform_account", __name__)

# FIX HI-03 (fresh-review): per-session CSRF token for the
# /account/api-keys/* POST handlers. The session cookie is now
# SameSite=Lax (set unconditionally in create_app - FIX M2), which
# blocks the cross-site form/AJAX POST vector browser-side; this
# token additionally guards against same-site XSS-leveraged
# forgeries and is what lets us use Lax rather than Strict here
# without exposing this surface. Stored in session as a hex string;
# rotated when the cookie rotates (login/logout/secret change).
_CSRF_SESSION_KEY = "_platform_api_csrf"


@platform_account_bp.route("/.well-known/ai-plugin.json", methods=["GET"])
def ai_plugin_manifest():
    from flask import jsonify  # noqa: PLC0415

    payload = {
        "schema_version": "v1",
        "name_for_human": "Ranomics Platform API",
        "name_for_model": "ranomics_platform",
        "description_for_human": (
            "Submit binder candidates for yeast-display triage and "
            "retrieve enrichment results."
        ),
        "description_for_model": (
            "Use this API to triage AI-designed binder libraries via "
            "wet-lab yeast display, mammalian display, or DMS at "
            "Ranomics. POST /api/v1/experiments with a sequences dict "
            "and target spec; poll GET /api/v1/experiments/{id} for "
            "status; fetch results via GET /api/v1/experiments/{id}/"
            "results once results_status != 'none'. Convention-"
            "compatible with Adaptyv Foundry shapes."
        ),
        "auth": {"type": "user_http", "authorization_type": "bearer"},
        "api": {
            "type": "openapi",
            "url": "https://tools.ranomics.com/api/v1/openapi.json",
        },
        "logo_url": "https://ranomics.com/favicon.svg",
        "contact_email": "info@ranomics.com",
        "legal_info_url": "https://ranomics.com/platform",
    }
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp

def _ensure_csrf_token() -> str:
    """Return the session's CSRF token, minting one if absent."""
    token = session.get(_CSRF_SESSION_KEY)
    if not token or not isinstance(token, str):
        token = _secrets.token_urlsafe(32)
        session[_CSRF_SESSION_KEY] = token
    return token

def _csrf_ok() -> bool:
    """Constant-time compare submitted ``_csrf`` against session value."""
    expected = session.get(_CSRF_SESSION_KEY) or ""
    submitted = (request.form.get("_csrf") or "").strip()
    if not expected or not submitted:
        return False
    return _hmac.compare_digest(expected, submitted)

def _format_dt(value):
    if not value:
        return None
    # Supabase returns ISO 8601 strings; trim subseconds + tz for display.
    return str(value)[:19].replace("T", " ") + " UTC"

def _render_api_keys_page(
    user_id: str,
    *,
    just_minted_plaintext: Optional[str] = None,
    just_minted_webhook_secret: Optional[str] = None,
    create_error: Optional[str] = None,
    rotate_notice: Optional[str] = None,
):
    """Shared renderer for the GET and POST handlers.

    Pulled out so the POST path can render the template directly
    with the one-shot plaintext token instead of round-tripping it
    through the session cookie (FIX #3 in the validation review:
    Flask sessions are signed-but-not-encrypted, so storing
    ``rk_live_...`` there leaked the plaintext into the browser
    cookie jar and any proxy log capturing cookies). The
    ``just_minted_webhook_secret`` parameter follows the same
    never-in-session rule for the per-tenant HMAC key (CR-01).
    """
    from shared.api_keys import get_webhook_secret_display

    raw_keys = list_keys(user_id)
    keys = [
        {
            "key_id": k.key_id,
            "prefix": k.prefix,
            "label": k.label,
            "role": k.role,
            "revoked_at": k.revoked_at,
            "created_at_display": _format_dt(k.created_at),
            "last_used_display": _format_dt(k.last_used_at),
        }
        for k in raw_keys
    ]
    webhook_secret_display = get_webhook_secret_display(user_id=user_id)
    return render_template(
        "account_api_keys.html",
        keys=keys,
        just_minted_plaintext=just_minted_plaintext,
        just_minted_webhook_secret=just_minted_webhook_secret,
        webhook_secret_display=webhook_secret_display,
        create_error=create_error,
        rotate_notice=rotate_notice,
        csrf_token=_ensure_csrf_token(),
    )

@platform_account_bp.route("/account/api-keys", methods=["GET"])
@login_required
def account_api_keys():
    user_ctx = load_user_context()
    if user_ctx is None:
        return redirect(url_for("auth.login"))
    return _render_api_keys_page(user_ctx.user_id)

@platform_account_bp.route("/account/api-keys/create", methods=["POST"])
@login_required
def account_api_keys_create():
    user_ctx = load_user_context()
    if user_ctx is None:
        return redirect(url_for("auth.login"))
    if not _csrf_ok():
        # FIX HI-03: defense-in-depth over SameSite=Strict. A 400
        # is fine here — legitimate users hitting this path always
        # POST through the rendered form, which carries the token.
        return _render_api_keys_page(
            user_ctx.user_id,
            create_error=(
                "Form submission failed CSRF check. Refresh this "
                "page and try again."
            ),
        ), 400
    label = (request.form.get("label") or "").strip()[:120] or None
    role = (request.form.get("role") or "member").strip().lower()
    if role not in VALID_ROLES:
        role = "member"
    minted = mint_token(
        user_id=user_ctx.user_id, role=role, label=label
    )
    if minted is None:
        return _render_api_keys_page(
            user_ctx.user_id,
            create_error=(
                "Could not mint a new key. Either you've hit the active-"
                "key cap or the database is temporarily unreachable. "
                "Revoke an unused key and try again, or contact support."
            ),
        )
    plaintext, _prefix, webhook_secret = minted
    # Plaintext is rendered ONCE in the response body. It never
    # touches session, cookies, or storage. Same rule for the
    # per-tenant webhook secret (CR-01) — non-None only on the
    # first mint per user.
    return _render_api_keys_page(
        user_ctx.user_id,
        just_minted_plaintext=plaintext,
        just_minted_webhook_secret=webhook_secret,
    )

@platform_account_bp.route(
    "/account/api-keys/rotate-webhook-secret", methods=["POST"]
)
@login_required
def account_api_keys_rotate_webhook_secret():
    """Rotate the per-tenant webhook signing secret (CR-01).

    Surfaces the new plaintext exactly once. The old secret stops
    being valid for HMAC verification immediately — receivers
    must be updated before this is clicked.
    """
    from shared.api_keys import rotate_webhook_secret

    user_ctx = load_user_context()
    if user_ctx is None:
        return redirect(url_for("auth.login"))
    if not _csrf_ok():
        return _render_api_keys_page(
            user_ctx.user_id,
            create_error=(
                "Rotate request failed CSRF check. Refresh and "
                "try again."
            ),
        ), 400
    new_secret = rotate_webhook_secret(user_id=user_ctx.user_id)
    if not new_secret:
        return _render_api_keys_page(
            user_ctx.user_id,
            create_error=(
                "Could not rotate the webhook secret. The database "
                "is temporarily unreachable; try again in a moment."
            ),
        )
    return _render_api_keys_page(
        user_ctx.user_id,
        just_minted_webhook_secret=new_secret,
        rotate_notice=(
            "Webhook secret rotated. The old secret stopped "
            "verifying as of now. Update your receivers."
        ),
    )

@platform_account_bp.route(
    "/account/api-keys/<key_id>/revoke", methods=["POST"]
)
@login_required
def account_api_keys_revoke(key_id):
    user_ctx = load_user_context()
    if user_ctx is None:
        return redirect(url_for("auth.login"))
    if not _csrf_ok():
        # FIX HI-03: revoke is destructive; refuse without CSRF.
        return _render_api_keys_page(
            user_ctx.user_id,
            create_error=(
                "Revoke request failed CSRF check. Refresh and "
                "try again."
            ),
        ), 400
    revoke_key(key_id=key_id, user_id=user_ctx.user_id)
    return redirect(url_for("platform_account.account_api_keys"))
