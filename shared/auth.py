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

from flask import redirect, request, session, url_for

from shared.supabase_client import get_supabase_client

logger = logging.getLogger(__name__)


def verify_login(email: str, password: str) -> tuple:
    """Attempt to sign in via Supabase Auth.

    Args:
        email: User email address.
        password: User password.

    Returns:
        Tuple (success: bool, error_message: str). On success, error_message
        is an empty string.
    """
    if not email or not password:
        return False, "Email and password are required."

    client = get_supabase_client()
    if client is None:
        return False, "Authentication service is not configured."

    try:
        response = client.auth.sign_in_with_password(
            {"email": email.strip(), "password": password}
        )
        if response.user:
            return True, ""
        return False, "Invalid email or password."
    except Exception as exc:
        msg = str(exc)
        if (
            "invalid" in msg.lower()
            or "credentials" in msg.lower()
            or "email" in msg.lower()
        ):
            return False, "Invalid email or password."
        logger.warning("Supabase login error: %s", exc)
        return False, f"Login failed: {msg}"


def register_user(email: str, password: str) -> tuple:
    """Create a new account via Supabase Auth.

    Args:
        email: User email address.
        password: Plaintext password (Supabase enforces min length server-side).

    Returns:
        Tuple (success: bool, error_message: str). On success, error_message
        is an empty string. Supabase may require email confirmation depending
        on project settings.
    """
    if not email or not password:
        return False, "Email and password are required."

    client = get_supabase_client()
    if client is None:
        return False, "Authentication service is not configured."

    try:
        response = client.auth.sign_up(
            {"email": email.strip(), "password": password}
        )
        if response.user:
            return True, ""
        return False, "Registration failed. Please try again."
    except Exception as exc:
        msg = str(exc)
        if (
            "already registered" in msg.lower()
            or "already exists" in msg.lower()
            or "duplicate" in msg.lower()
        ):
            return False, "An account with this email already exists."
        if "password" in msg.lower() and "weak" in msg.lower():
            return False, "Password is too weak. Use at least 8 characters."
        logger.warning("Supabase sign-up error: %s", exc)
        return False, f"Registration failed: {msg}"


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
