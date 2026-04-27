"""Supabase-backed authentication helpers for the Ranomics tools hub.

Lifted from epitope-scout/analysis/auth.py and adapted to use the shared
Supabase client factory in shared.supabase_client. The tools hub shares
Epitope Scout's Supabase project (one user base across all Ranomics tools).

Provides:
  - verify_login(email, password)
  - register_user(email, password)
  - reset_password(email)
  - login_required — Flask route decorator

Requires these environment variables:
  SUPABASE_URL         — Supabase project URL
  SUPABASE_KEY         — Supabase publishable/anon key
                         (SUPABASE_ANON_KEY also accepted)
  SESSION_SECRET_KEY   — Flask session signing secret
"""

import logging
from functools import wraps

from flask import redirect, render_template, request, session, url_for

from shared.supabase_client import get_supabase_client

# Emails that bypass the public-user gate on /admin/* routes.
STAFF_EMAILS: frozenset[str] = frozenset({"leo@ranomics.com"})

logger = logging.getLogger(__name__)


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


def register_user(email: str, password: str) -> tuple:
    """Create a new account via Supabase Auth.

    Args:
        email: User email address.
        password: Plaintext password (Supabase enforces min length server-side).

    Returns:
        Tuple ``(success: bool, error_message: str, user_id: str | None)``.
        On success, error_message is an empty string and user_id is the
        Supabase auth uid. The caller may use this to grant signup-bonus
        credits immediately (see app.py signup route). On failure, user_id
        is None.
    """
    if not email or not password:
        return False, "Email and password are required.", None

    client = get_supabase_client()
    if client is None:
        return False, "Authentication service is not configured.", None

    try:
        response = client.auth.sign_up(
            {"email": email.strip(), "password": password}
        )
        if response.user:
            user_id = getattr(response.user, "id", None)
            if user_id is None and isinstance(response.user, dict):
                user_id = response.user.get("id")
            return True, "", user_id
        return False, "Registration failed. Please try again.", None
    except Exception as exc:
        msg = str(exc)
        if (
            "already registered" in msg.lower()
            or "already exists" in msg.lower()
            or "duplicate" in msg.lower()
        ):
            return False, "An account with this email already exists.", None
        if "password" in msg.lower() and "weak" in msg.lower():
            return False, "Password is too weak. Use at least 8 characters.", None
        logger.warning("Supabase sign-up error: %s", exc)
        return False, f"Registration failed: {msg}", None


def reset_password(email: str) -> tuple:
    """Send a password reset email via Supabase Auth.

    Always returns success to the caller to prevent email enumeration.

    Args:
        email: User email address.

    Returns:
        Tuple (success: bool, error_message: str).
    """
    if not email:
        return False, "Email is required."

    client = get_supabase_client()
    if client is None:
        return False, "Authentication service is not configured."

    try:
        client.auth.reset_password_email(email.strip())
        return True, ""
    except Exception as exc:
        logger.warning("Supabase password reset error: %s", exc)
        return True, ""


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
            return render_template("coming_soon.html"), 403
        return f(*args, **kwargs)
    return decorated_function
