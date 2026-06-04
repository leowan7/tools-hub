"""Supabase-backed authentication helpers for the Ranomics tools hub.

Lifted from epitope-scout/analysis/auth.py and adapted to use the shared
Supabase client factory in shared.supabase_client. The tools hub shares
Epitope Scout's Supabase project (one user base across all Ranomics tools).

Provides:
  - verify_login(email, password)
  - register_user(email, password, *, signup_context)
  - reset_password(email)
  - update_password(access_token, refresh_token, new_password)
  - issue_signup_token() / consume_signup_token()
  - login_required — Flask route decorator

Requires these environment variables:
  SUPABASE_URL         — Supabase project URL
  SUPABASE_KEY         — Supabase publishable/anon key
                         (SUPABASE_ANON_KEY also accepted)
  SESSION_SECRET_KEY   — Flask session signing secret
"""

import logging
import os
import time
from dataclasses import dataclass
from functools import wraps
from typing import Optional

from flask import redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from shared.supabase_client import get_supabase_client

# Emails that bypass the public-user gate on /admin/* routes.
STAFF_EMAILS: frozenset[str] = frozenset({"leo@ranomics.com"})

# Form-submit guards on /signup.
#   MIN_FILL_SECONDS  Floor on time-to-submit. Effectively disabled
#                     (0) because the floor was false-positiving real
#                     prospects whose password manager auto-filled and
#                     submitted in <2s. Honeypot + signed token are
#                     the load-bearing bot defenses; the timing floor
#                     was the cheapest one to lose.
#   MAX_FILL_SECONDS  Reject stale tokens. After this, the form is
#                     re-fetched so the timestamp cannot be replayed.
#   MIN_PURPOSE_CHARS Minimum length when a personal-domain signup
#                     is required to attach a "what are you working on"
#                     note. Real notes clear this comfortably; junk
#                     drive-by submits don't.
MIN_FILL_SECONDS: int = 0
MAX_FILL_SECONDS: int = 3600
MIN_PURPOSE_CHARS: int = 30

SIGNUP_TOKEN_SALT: str = "signup-form-render"
RESET_TOKEN_SALT: str = "tools-hub-reset-v1"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signup-form timing token (signed with the Flask session secret)
# ---------------------------------------------------------------------------


def _signup_serializer() -> URLSafeTimedSerializer:
    """Return a serializer keyed on the Flask session secret.

    Falls back to a process-stable random secret in local dev so the
    helper does not crash without env vars; in production
    SESSION_SECRET_KEY is always set.
    """
    secret = os.environ.get("SESSION_SECRET_KEY", "dev-only-insecure-key")
    return URLSafeTimedSerializer(secret, salt=SIGNUP_TOKEN_SALT)


def issue_signup_token() -> str:
    """Return a fresh signed timestamp for the signup form."""
    return _signup_serializer().dumps({"t": int(time.time())})


def consume_signup_token(token: str) -> Optional[int]:
    """Validate a signup_token and return how long the form was open.

    Returns the elapsed seconds between form render and submit, or
    None if the token is missing, tampered, or older than
    MAX_FILL_SECONDS.
    """
    if not token:
        return None
    try:
        payload = _signup_serializer().loads(
            token, max_age=MAX_FILL_SECONDS
        )
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    issued_at = int(payload.get("t", 0)) if isinstance(payload, dict) else 0
    if not issued_at:
        return None
    return max(0, int(time.time()) - issued_at)


# ---------------------------------------------------------------------------
# Reset-form timing token (mirrors signup pair, distinct salt so a signup
# token cannot be replayed at /forgot-password or vice-versa)
# ---------------------------------------------------------------------------


def _reset_serializer() -> URLSafeTimedSerializer:
    """Return a serializer keyed on the Flask session secret, reset salt."""
    secret = os.environ.get("SESSION_SECRET_KEY", "dev-only-insecure-key")
    return URLSafeTimedSerializer(secret, salt=RESET_TOKEN_SALT)


def issue_reset_token() -> str:
    """Return a fresh signed timestamp for the password-reset form."""
    return _reset_serializer().dumps({"t": int(time.time())})


def consume_reset_token(token: str) -> Optional[int]:
    """Validate a reset_token and return how long the form was open.

    Returns the elapsed seconds between form render and submit, or
    None if the token is missing, tampered, or older than
    MAX_FILL_SECONDS.
    """
    if not token:
        return None
    try:
        payload = _reset_serializer().loads(
            token, max_age=MAX_FILL_SECONDS
        )
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    issued_at = int(payload.get("t", 0)) if isinstance(payload, dict) else 0
    if not issued_at:
        return None
    return max(0, int(time.time()) - issued_at)


# ---------------------------------------------------------------------------
# Signup-attempt context (what /signup hands to register_user)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignupContext:
    """Inputs gathered from the /signup request, validated as a unit.

    The /signup route assembles this from request.form / request.remote_addr
    and passes it down. register_user runs honeypot / timing / domain
    checks against it before reaching out to Supabase.
    """

    email: str
    password: str
    purpose: str
    honeypot: str
    token: str
    ip: Optional[str] = None
    user_agent: Optional[str] = None


@dataclass(frozen=True)
class SignupResult:
    """Outcome of a /signup attempt.

    Exactly one of (user_id) and (rejection_reason, error_message) is
    populated. On reject, /signup also writes a row to
    public.signup_rejections — see _log_rejection in app.py.

    ``rejection_reason`` is set only when the failure should be logged
    to the bot-filter audit table (honeypot/timing/disposable/etc).
    ``failure_code`` is set on EVERY failure — including UX-level
    bounces like existing_account/weak_password/auth_error that don't
    belong in the bot-filter table but still need to land in the
    signup_failed user_event so the funnel is complete.
    """

    success: bool
    user_id: Optional[str] = None
    error_message: str = ""
    rejection_reason: Optional[str] = None
    failure_code: Optional[str] = None
    classification: Optional[str] = None
    signup_quality: Optional[str] = None
    purpose_stored: Optional[str] = None


# ---------------------------------------------------------------------------
# Reset-attempt context (what /forgot-password hands to process_reset_request)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResetContext:
    """Inputs gathered from the /forgot-password request, validated as a unit.

    Mirrors SignupContext on the reset side. The /forgot-password route
    assembles this from request.form and passes it to
    process_reset_request, which runs the same honeypot / timing /
    domain gauntlet as /signup before any Supabase recovery email is
    sent.
    """

    email: str
    reset_token: str
    honeypot_value: str
    now_unix: int


@dataclass(frozen=True)
class ResetResult:
    """Outcome of a /forgot-password gauntlet pass.

    Every failure path returns the same shape (ok=True,
    should_send_email=False, error_message=None) so the route can
    render an identical success message regardless of which check
    failed. This denies the bot any signal about whether the email was
    valid, disposable, or unregistered.
    """

    ok: bool
    error_message: Optional[str] = None
    should_send_email: bool = False


def process_reset_request(
    ctx: ResetContext,
    supabase_admin_client,
) -> ResetResult:
    """Run the anti-bot gauntlet for a /forgot-password submission.

    Order of checks (each silently drops on fail — no signal to caller):

      1. honeypot       hidden field non-empty
      2. timing         token missing / expired / submitted <2s after render
      3. classify       INVALID or DISPOSABLE email
      4. existence      no auth.users row for this email

    On every fail path returns (ok=True, should_send_email=False) so
    the route renders the same generic success copy a legit user sees.
    Only the all-pass path returns should_send_email=True.

    Args:
        ctx: Form inputs + render-time stamp, see ``ResetContext``.
        supabase_admin_client: Service-role Supabase client used to
            check whether an account exists for the submitted email.
            Passed in (rather than fetched here) so callers can inject
            for tests.

    Returns:
        ResetResult. The route reads ``should_send_email`` to decide
        whether to invoke ``reset_password``; ``ok`` is always True.
    """
    from shared.email_domain import (  # noqa: PLC0415
        EmailClass,
        classify_email,
    )

    drop = ResetResult(ok=True, should_send_email=False, error_message=None)

    # ----- Layer 1: honeypot ------------------------------------------------
    if ctx.honeypot_value:
        return drop

    # ----- Layer 2: timing token --------------------------------------------
    elapsed = consume_reset_token(ctx.reset_token)
    if elapsed is None:
        return drop
    if elapsed < MIN_FILL_SECONDS or elapsed > MAX_FILL_SECONDS:
        return drop

    # ----- Layer 3: classify -------------------------------------------------
    email = (ctx.email or "").strip()
    if not email:
        return drop

    classification = classify_email(email)
    if classification in (EmailClass.INVALID, EmailClass.DISPOSABLE):
        return drop

    # ----- Layer 4: existence -----------------------------------------------
    # user_profiles has no email column (keyed by user_id → auth.users.id),
    # so we resolve via the admin auth list. Same pattern used by
    # shared.credits._resolve_user_id and several other call sites.
    if supabase_admin_client is None:
        return drop
    try:
        page = supabase_admin_client.auth.admin.list_users()
        users = getattr(page, "users", None) or page
        target = email.lower()
        for user in users:
            candidate = getattr(user, "email", None) or (
                user.get("email") if isinstance(user, dict) else None
            )
            if candidate and candidate.lower() == target:
                return ResetResult(
                    ok=True,
                    error_message=None,
                    should_send_email=True,
                )
    except Exception:
        logger.warning(
            "process_reset_request: auth admin.list_users failed",
            exc_info=True,
        )
        return drop

    return drop


def verify_login(email: str, password: str) -> tuple:
    """Attempt to sign in via Supabase Auth.

    Returns:
        Tuple ``(success: bool, error_message: str, user_id: str | None)``.
        On success, error_message is empty and user_id is the Supabase auth
        uid (the caller stashes it in the Flask session so the navbar can
        render the credit balance without re-resolving on every request).
    """
    if not email or not password:
        return False, "Email and password are required.", None

    client = get_supabase_client()
    if client is None:
        return False, "Authentication service is not configured.", None

    try:
        response = client.auth.sign_in_with_password(
            {"email": email.strip(), "password": password}
        )
        if response.user:
            user_id = getattr(response.user, "id", None)
            if user_id is None and isinstance(response.user, dict):
                user_id = response.user.get("id")
            return True, "", user_id
        return False, "Invalid email or password.", None
    except Exception as exc:
        msg = str(exc)
        if (
            "invalid" in msg.lower()
            or "credentials" in msg.lower()
            or "email" in msg.lower()
        ):
            return False, "Invalid email or password.", None
        logger.warning("Supabase login error: %s", exc)
        return False, f"Login failed: {msg}", None


def register_user(
    ctx: SignupContext,
    *,
    email_redirect_to: str | None = None,
) -> SignupResult:
    """Run the full signup pipeline: filter → classify → create → profile.

    Order of checks (each maps to a ``signup_rejections.reason`` value
    if it fires):

      1. honeypot       hidden field non-empty
      2. timing         token missing / expired / submitted <2s after render
      3. invalid        malformed email
      4. disposable     domain on disposable blocklist
      5. purpose_missing  personal-domain submit without 30+ char purpose

    On pass, calls Supabase ``client.auth.sign_up`` and (best-effort)
    inserts a ``public.user_profiles`` row carrying the classification
    + purpose. The caller is responsible for logging rejected attempts
    to ``signup_rejections`` (kept out of this module so this stays
    importable from worker code that has no DB role).

    Args:
        ctx: Form inputs + request context, see ``SignupContext``.
        email_redirect_to: URL Supabase uses for the confirmation
            link's redirect (otherwise it falls back to the shared
            Scout/tools-hub project's Site URL).

    Returns:
        SignupResult. On reject, ``rejection_reason`` carries the
        machine-readable reason for ``signup_rejections.reason``;
        ``error_message`` carries the human-facing copy for the
        sign-up template.
    """
    from shared.email_domain import (  # noqa: PLC0415
        EmailClass,
        classify_email,
        signup_quality_for,
    )

    # ----- Layer 1: honeypot ------------------------------------------------
    # Bots fill every visible-ish input; a non-empty 'website' field
    # was put there for them. We return a generic message rather than
    # admit we caught the bot.
    if ctx.honeypot:
        return SignupResult(
            success=False,
            error_message="Registration failed. Please try again.",
            rejection_reason="honeypot",
            failure_code="honeypot",
        )

    # ----- Layer 2: timing token --------------------------------------------
    elapsed = consume_signup_token(ctx.token)
    if elapsed is None:
        return SignupResult(
            success=False,
            error_message=(
                "Your session expired. Please reload the page and "
                "try again."
            ),
            rejection_reason="timing",
            failure_code="timing_expired",
        )
    if elapsed < MIN_FILL_SECONDS:
        return SignupResult(
            success=False,
            error_message=(
                "Please reload the page and submit again — the form "
                "was submitted faster than a human can read it."
            ),
            rejection_reason="timing",
            failure_code="timing_fast",
        )

    # ----- Layer 3: classify -------------------------------------------------
    email = (ctx.email or "").strip()
    password = ctx.password or ""
    purpose = (ctx.purpose or "").strip()

    if not email or not password:
        return SignupResult(
            success=False,
            error_message="Email and password are required.",
            rejection_reason="invalid",
            failure_code="missing_required",
        )

    classification = classify_email(email)

    if classification == EmailClass.INVALID:
        return SignupResult(
            success=False,
            error_message="Please enter a valid email address.",
            rejection_reason="invalid",
            failure_code="invalid_email",
        )

    if classification == EmailClass.DISPOSABLE:
        return SignupResult(
            success=False,
            error_message=(
                "We can't accept signups from temporary email services. "
                "Please use your work, school, or personal email."
            ),
            rejection_reason="disposable",
            failure_code="disposable",
            classification=classification.value,
        )

    if classification == EmailClass.PERSONAL and len(purpose) < MIN_PURPOSE_CHARS:
        return SignupResult(
            success=False,
            error_message=(
                "A short note helps us serve you better — please tell "
                "us what you're working on (30+ characters)."
            ),
            rejection_reason="purpose_missing",
            failure_code="purpose_missing",
            classification=classification.value,
        )

    # ----- Layer 4: Supabase create_user (service-role) ----------------------
    # Use the service-role admin API instead of anon-client sign_up. This
    # lets us turn off "Allow new users to sign up" in the Supabase
    # dashboard so bots can't hit POST /auth/v1/signup directly with the
    # public anon key — every account creation must come through this
    # Flask route, which means our honeypot / timing / domain / purpose
    # filters can't be bypassed. The email_redirect_to argument is
    # accepted but no longer used (admin.create_user does not send a
    # confirmation email).
    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return SignupResult(
            success=False,
            error_message="Authentication service is not configured.",
            rejection_reason=None,  # internal misconfig, do not log as rejection
            failure_code="service_misconfigured",
            classification=classification.value,
        )

    _ = email_redirect_to  # consumed by the legacy sign_up path; kept for caller compat

    try:
        response = client.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
        })
    except Exception as exc:
        msg = str(exc)
        low = msg.lower()
        if "already registered" in low or "already exists" in low or "duplicate" in low:
            return SignupResult(
                success=False,
                error_message="An account with this email already exists.",
                rejection_reason=None,
                failure_code="existing_account",
                classification=classification.value,
            )
        if "password" in low and "weak" in low:
            return SignupResult(
                success=False,
                error_message="Password is too weak. Use at least 8 characters.",
                rejection_reason=None,
                failure_code="weak_password",
                classification=classification.value,
            )
        logger.warning("Supabase sign-up error: %s", exc)
        return SignupResult(
            success=False,
            error_message=f"Registration failed: {msg}",
            rejection_reason=None,
            failure_code="auth_error",
            classification=classification.value,
        )

    user_id: Optional[str] = None
    if response and getattr(response, "user", None):
        user_obj = response.user
        user_id = getattr(user_obj, "id", None)
        if user_id is None and isinstance(user_obj, dict):
            user_id = user_obj.get("id")

    if not user_id:
        return SignupResult(
            success=False,
            error_message="Registration failed. Please try again.",
            rejection_reason=None,
            failure_code="no_user_id",
            classification=classification.value,
        )

    purpose_to_store = purpose if purpose else None
    quality = signup_quality_for(classification, purpose_to_store)

    # Best-effort: profile insert is informational, not load-bearing for
    # the user's ability to sign in. Failures are logged but never
    # propagated to the caller.
    try:
        _insert_user_profile(
            user_id=user_id,
            domain_class=classification.value,
            signup_quality=quality,
            purpose=purpose_to_store,
            ip=ctx.ip,
            user_agent=ctx.user_agent,
        )
    except Exception:
        logger.warning(
            "Failed to insert user_profiles row for %s", email, exc_info=True
        )

    return SignupResult(
        success=True,
        user_id=user_id,
        classification=classification.value,
        signup_quality=quality,
        purpose_stored=purpose_to_store,
    )


def _insert_user_profile(
    *,
    user_id: str,
    domain_class: str,
    signup_quality: str,
    purpose: Optional[str],
    ip: Optional[str],
    user_agent: Optional[str],
) -> None:
    """Write the one-time profile row for a newly created user.

    Service-role client — bypasses RLS so we can write the row before
    the user has a session.
    """
    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return
    row: dict = {
        "user_id": user_id,
        "domain_class": domain_class,
        "signup_quality": signup_quality,
        "purpose": purpose,
        "ip": ip,
        "user_agent": user_agent,
    }
    client.table("user_profiles").upsert(row).execute()


def reset_password(
    email: str,
    *,
    redirect_to: str | None = None,
) -> tuple:
    """Send a password reset email via Supabase Auth.

    Always returns success to the caller to prevent email enumeration.

    Args:
        email: User email address.
        redirect_to: URL Supabase appends the recovery hash fragment to.
            Without this, Supabase falls back to the project's Site URL,
            which on this shared project points at scout.ranomics.com —
            sending tools-hub users to the wrong product.

    Returns:
        Tuple (success: bool, error_message: str).
    """
    if not email:
        return False, "Email is required."

    client = get_supabase_client()
    if client is None:
        return False, "Authentication service is not configured."

    options = {"redirect_to": redirect_to} if redirect_to else None

    try:
        client.auth.reset_password_email(email.strip(), options)
        return True, ""
    except Exception as exc:
        logger.warning("Supabase password reset error: %s", exc)
        return True, ""


def update_password(
    access_token: str,
    refresh_token: str,
    new_password: str,
) -> tuple:
    """Apply a new password using a Supabase recovery session.

    Used by the /reset-password handler after the user clicks the email
    link. The recovery URL hash fragment carries access/refresh tokens for
    a one-time recovery session; we install that session on a fresh client
    and then call update_user.

    Args:
        access_token: From the recovery URL hash fragment.
        refresh_token: From the recovery URL hash fragment.
        new_password: Plaintext new password (Supabase enforces min length).

    Returns:
        Tuple (success: bool, error_message: str).
    """
    if not access_token or not refresh_token:
        return False, "Reset link is invalid or has expired."
    if not new_password:
        return False, "Password is required."

    client = get_supabase_client()
    if client is None:
        return False, "Authentication service is not configured."

    try:
        client.auth.set_session(access_token, refresh_token)
    except Exception as exc:
        logger.warning("Supabase set_session error during reset: %s", exc)
        return False, "Reset link is invalid or has expired."

    try:
        response = client.auth.update_user({"password": new_password})
        if response.user:
            return True, ""
        return False, "Password update failed. Please try again."
    except Exception as exc:
        msg = str(exc)
        if "password" in msg.lower() and (
            "weak" in msg.lower() or "short" in msg.lower()
        ):
            return False, "Password is too weak. Use at least 8 characters."
        if "same" in msg.lower() and "password" in msg.lower():
            return False, "New password must differ from your old password."
        logger.warning("Supabase update_user error during reset: %s", exc)
        return False, f"Password update failed: {msg}"


def login_required(f):
    """Flask route decorator that enforces authentication.

    Redirects unauthenticated requests to /login, preserving the original
    destination in the ``next`` query parameter so the user is returned
    there after a successful login.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_email"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated_function


def require_staff(f):
    """Flask route decorator that restricts a route to Ranomics staff.

    Staff membership is determined by ``STAFF_EMAILS``. Returns 403 for
    authenticated non-staff users; redirects to /login for unauthenticated.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        email = session.get("user_email")
        if not email:
            return redirect(url_for("login", next=request.path))
        if email not in STAFF_EMAILS:
            # Staff-only routes return 404 rather than 403 so their
            # existence is not revealed to authenticated non-staff users.
            return render_template("404.html"), 404
        return f(*args, **kwargs)
    return decorated_function
