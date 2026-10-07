"""Authentication + account routes (blueprint refactor, Commit 7a).

login / signup / forgot-password / reset-password / logout and the /account
dashboard. Lifted verbatim from ``create_app()``; only ``@flask_app.route``
-> ``@auth_bp.route`` and self-refs -> ``auth.*``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import requests
from flask import (
    Blueprint,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from shared.auth import login_required
from shared.wallet import SIGNUP_CREDIT_EXPIRY_DAYS, SIGNUP_CREDIT_USD

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


def safe_next(value: str | None, fallback: str = "/") -> str:
    """Return ``value`` if it is a same-origin path, else ``fallback``.

    This is an ALLOWLIST, not a blocklist. The previous shape-based
    blocklist (startswith "/" and not "//" and not "/\\") was bypassable:
    ``/\\t/evil.com`` passed all three checks, and the header layer then
    STRIPPED the tab, shipping ``Location: //evil.com`` — a
    protocol-relative URL, i.e. an off-site redirect. The stripping is
    server-side, so no browser quirk is required.

    Four layers. Which of them are UNIQUELY load-bearing was measured, not
    assumed, by deleting each and re-running tests/test_login_redirect.py;
    the honest result is recorded per layer below.

    0. urlsplit RAISES ValueError on some inputs — ``//[`` and ``//%5B``
       ("Invalid IPv6 URL"), ``//[]`` (bad bracketed host), ``//\\uff03e``
       (NFKC netloc check). Letting that escape turned an unauthenticated
       GET into a 500, so a parse failure is treated as unsafe and falls
       back. Caught broadly on purpose: urlsplit's raise set differs
       between CPython versions, so enumerating it would pin us to 3.13.
    1. urlsplit allowlist (primary): accept only a relative reference with
       NO scheme and NO netloc, whose path starts with "/".
       - ``parts.path.startswith("/")`` is uniquely load-bearing:
         ``evil.com/a`` is rejected by nothing else.
       - ``parts.scheme`` is uniquely load-bearing: ``javascript:/a`` and
         ``data:/a`` split to an empty netloc and the path "/a", so every
         other layer waves them through.
       - ``parts.netloc`` is NOT uniquely load-bearing, and this docstring
         previously claimed otherwise. urlsplit only produces a netloc when
         the value (after its own TAB/CR/LF stripping) has "//" following
         the optional scheme; that means either a scheme is present
         (caught above), or the raw value starts with "//" (layer 3), or it
         leads with a stripped control character (layer 2). Kept as
         defence in depth — it is the check that states the actual intent,
         and it stops depending on the other three staying exactly as they
         are — but no test can single it out, because nothing reaches it
         first.
    2. Reject C0 controls, DEL and C1 controls outright. NOTE: an earlier
       version of this comment claimed VT, FF, NUL, DEL and NEL "pass
       through Werkzeug raw". That is FALSE — measured on the merge base,
       Werkzeug percent-encodes all of them exactly like SPACE
       (``/%0B/``, ``/%0C/``, ``/%00/``, ``/%7F/``, ``/%C2%85/``), and CR
       and LF are rejected outright with a 500. TAB is the only one that is
       silently STRIPPED, and TAB is already caught by layer 1. So layer 2
       is not the barrier it was described as. It is still the only layer
       that rejects ``/\\x0b/evil.com`` and friends (deleting it turns the
       suite red), so the CHECK is load-bearing; it is the stated REASON
       that was wrong. The real reason to keep it: it stops the guard's
       correctness resting on urlsplit's and Werkzeug's normalisation
       tables, which are implementation details of two dependencies.
       Nothing legitimate needs those bytes anyway.
       SPACE is deliberately NOT rejected: it survives as "%20" rather than
       being normalised away, so it is provably safe, and rejecting it
       could break a legitimate link.
    3. Reject a raw leading "//". Uniquely load-bearing:
       urlsplit("///evil.com") yields an EMPTY netloc and the path
       "/evil.com", so layer 1 waves it through while browsers resolve it
       off-origin.
    4. Keep the explicit "/\\" check. Uniquely load-bearing: urlsplit does
       NOT treat a backslash as a separator, so ``/\\evil.com`` parses as an
       innocent path while browsers normalise it to ``//evil.com``.
    """
    if not value:
        return fallback
    try:
        parts = urlsplit(value)
    except Exception:
        # Unparseable is unsafe. See layer 0 above.
        return fallback
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return fallback
    if any(c < "\x20" or "\x7f" <= c <= "\x9f" for c in value):
        return fallback
    if value.startswith("//") or value.startswith("/\\"):
        return fallback
    return value


def _client_ip() -> str | None:
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr)


def _start_session(email: str, user_id: str | None, props: dict | None = None) -> None:
    """Sign the browser in and log a ``login`` user_event."""
    session["user_email"] = email
    if user_id:
        session["user_id"] = user_id
    try:
        from shared.events import log_event  # noqa: PLC0415
        log_event(
            event_type="login",
            user_id=user_id,
            session_id=session.get("anon_session_id"),
            path=request.path,
            props=props,
            ip=_client_ip(),
            user_agent=request.headers.get("User-Agent"),
        )
    except Exception:
        logger.warning("login event log failed", exc_info=True)


# ------------------------------------------------------------------
# Auth routes
# ------------------------------------------------------------------

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Render the login form (GET) or handle credential submission (POST)."""
    from shared.auth import verify_login  # noqa: PLC0415

    if request.method == "GET":
        # Validate on GET too, so the hidden form field can never be
        # populated with a value the POST would later reject.
        next_url = safe_next(request.args.get("next"))
        return render_template(
            "login.html",
            mode="signin",
            error=None,
            email=None,
            next=next_url,
        )

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    # Same-origin allowlist — see safe_next(). Applied before the field is
    # re-rendered on the failure path as well as before the redirect.
    next_url = safe_next(request.form.get("next"))

    success, error_msg, user_id = verify_login(email, password)
    if success:
        # Set by google_callback when Google's verified email matched this
        # account: the password now proves ownership, so connect Google.
        link = session.pop("google_link", None)
        if link and user_id and link.get("user_id") == user_id:
            _mark_google_linked(user_id)
        _start_session(email, user_id)
        return redirect(next_url)

    return render_template(
        "login.html",
        mode="signin",
        error=error_msg,
        email=email,
        next=next_url,
    )

@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    """Render the sign-up form (GET) or handle new account creation (POST).

    On POST, four guards run before Supabase Auth is touched:
    honeypot, signed-timestamp timing, email-domain classification,
    and (for personal domains) the "what are you working on" note.
    Failures are logged to public.signup_rejections so the daily
    digest can flag false positives.
    """
    from shared.auth import (  # noqa: PLC0415
        SignupContext,
        issue_signup_token,
        register_user,
    )
    from shared.events import log_event, log_signup_rejection  # noqa: PLC0415

    def _log_signup_failed(reason: str, email_value: str) -> None:
        """Fire a signup_failed user_event so every failure mode is funnel-visible.

        Coexists with signup_rejections: that table stays the
        bot-filter audit log; this event is the UX funnel feed.
        """
        domain = email_value.rsplit("@", 1)[1].lower() if "@" in email_value else ""
        log_event(
            event_type="signup_failed",
            session_id=session.get("anon_session_id"),
            path="/signup",
            props={
                "reason": reason,
                "email": email_value.strip().lower()[:320] if email_value else None,
                "email_domain": domain or None,
            },
            ip=client_ip,
            user_agent=user_agent,
        )

    # Honour ?next= / the hidden field instead of pinning every render to "/".
    # request.values covers the GET render and the POST re-renders in one read,
    # so a validation failure does not silently drop where the visitor was
    # heading. safe_next is the SAME allowlist login uses; anything not a
    # same-origin path falls back to "/", which is the old behaviour.
    next_url = safe_next(request.values.get("next"))

    if request.method == "GET":
        return render_template(
            "login.html",
            mode="signup",
            error=None,
            signup_email=None,
            signup_purpose=None,
            signup_terms=False,
            signup_token=issue_signup_token(),
            next=next_url,
        )

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")
    purpose = request.form.get("purpose", "").strip()
    honeypot = request.form.get("website", "").strip()
    token = request.form.get("signup_token", "")
    terms_accepted = request.form.get("terms_accepted") == "on"
    client_ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                 or request.remote_addr)
    user_agent = request.headers.get("User-Agent")

    # Password-pair check runs before register_user so the user sees
    # this error without re-typing their email + purpose. The
    # honeypot / timing checks still happen below — confirmation
    # mismatch is a UX problem, not a junk-filter problem.
    if password and password2 and password != password2:
        _log_signup_failed("password_mismatch", email)
        return render_template(
            "login.html",
            mode="signup",
            error="Passwords do not match.",
            signup_email=email,
            signup_purpose=purpose,
            signup_terms=terms_accepted,
            signup_token=issue_signup_token(),
            next=next_url,
        )
    if password and len(password) < 8:
        _log_signup_failed("password_short", email)
        return render_template(
            "login.html",
            mode="signup",
            error="Password must be at least 8 characters.",
            signup_email=email,
            signup_purpose=purpose,
            signup_terms=terms_accepted,
            signup_token=issue_signup_token(),
            next=next_url,
        )
    if not terms_accepted:
        _log_signup_failed("terms_not_accepted", email)
        return render_template(
            "login.html",
            mode="signup",
            error="You must accept the Terms of Service and Privacy Policy to create an account.",
            signup_email=email,
            signup_purpose=purpose,
            signup_terms=False,
            signup_token=issue_signup_token(),
            next=next_url,
        )

    # Send the confirmation email's "click here" link back to tools-hub
    # explicitly. Otherwise Supabase falls back to the project Site URL,
    # which on the shared Scout/tools-hub project points at scout.
    public_base = os.environ.get(
        "PUBLIC_BASE_URL", "https://tools.ranomics.com"
    ).rstrip("/")

    ctx = SignupContext(
        email=email,
        password=password,
        purpose=purpose,
        honeypot=honeypot,
        token=token,
        ip=client_ip,
        user_agent=user_agent,
    )
    result = register_user(ctx, email_redirect_to=f"{public_base}/login")

    if not result.success:
        # Honeypot hits never see an error — the bot got the same
        # generic page as a real user re-fetching after a failure.
        # Everyone else sees the per-reason message.
        if result.rejection_reason:
            log_signup_rejection(
                email=email,
                reason=result.rejection_reason,
                ip=client_ip,
                user_agent=user_agent,
            )
        # Always emit the user-event so the funnel sees every miss,
        # including the silent register_user paths (existing_account,
        # weak_password, auth_error, etc.) that don't land in
        # signup_rejections.
        _log_signup_failed(result.failure_code or "unknown", email)
        return render_template(
            "login.html",
            mode="signup",
            error=result.error_message,
            signup_email=email,
            signup_purpose=purpose,
            signup_terms=terms_accepted,
            signup_token=issue_signup_token(),
            next=next_url,
        )

    # The wallet signup credit is granted lazily when the user_wallets
    # row is first created on sign-in
    # (shared.wallet._create_wallet_with_signup_credit). No legacy
    # credits-ledger grant on this path.

    log_event(
        event_type="signup_completed",
        user_id=result.user_id,
        session_id=session.get("anon_session_id"),
        path="/signup",
        props={
            "domain_class": result.classification,
            "signup_quality": result.signup_quality,
        },
        ip=client_ip,
        user_agent=user_agent,
    )

    # D3 funnel fire. The Supabase audit row above is the source of
    # truth; this is the PostHog mirror that drives the funnel
    # dashboard. emit() is a no-op when PUBLIC_POSTHOG_KEY is unset.
    from shared.events import EVENTS, emit  # noqa: PLC0415
    emit(
        EVENTS.SIGNUP_COMPLETE,
        user_id=result.user_id,
        properties={
            "domain_class": result.classification,
            "signup_quality": result.signup_quality,
        },
    )

    return render_template(
        "login.html",
        mode="signin",
        error=None,
        email=email,
        next=next_url,
        # Quote the grant, not a retyped figure: this line told every new
        # user "$5" for the whole time the wallet was depositing $15.
        success_msg=(
            f"Account created with ${SIGNUP_CREDIT_USD:.0f} of compute "
            f"credit, usable for {SIGNUP_CREDIT_EXPIRY_DAYS} days. Sign in "
            "to get started."
        ),
    )

# ------------------------------------------------------------------
# Continue with Google: OAuth 2.0 authorization code flow with PKCE.
# Supabase's own Google provider is not used because "Allow new users to
# sign up" is off in the Supabase dashboard (docs/SESSION-HANDOFF-2026-05-13.md);
# new accounts are created with the service-role admin API, as /signup does.
# ------------------------------------------------------------------

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_GOOGLE_FAILED = "Google sign-in did not complete. Please try again."
_GOOGLE_UNAVAILABLE = "Sign-in is unavailable right now. Please try again shortly."


def _google_credentials() -> tuple[str, str] | None:
    """(client_id, client_secret), or None when either env var is unset."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    return (client_id, client_secret) if client_id and client_secret else None


def _google_redirect_uri() -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "https://tools.ranomics.com").rstrip("/")
    return base + url_for("auth.google_callback")


@auth_bp.context_processor
def _google_signin_flag() -> dict:
    return {"google_signin_enabled": _google_credentials() is not None}


def _is_banned(user: dict) -> bool:
    """True while ``banned_until`` is in the future. Unparseable counts as banned."""
    raw = user.get("banned_until")
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


def _mark_google_linked(user_id: str) -> None:
    """Set ``app_metadata.google_linked`` so Google signs this account in directly."""
    from shared.credits import get_service_client  # noqa: PLC0415

    try:
        get_service_client().auth.admin.update_user_by_id(
            user_id, {"app_metadata": {"google_linked": True}}
        )
    except Exception:
        logger.warning("could not record google_linked for %s", user_id, exc_info=True)


def _google_refused(message: str, next_url: str):
    return render_template(
        "login.html", mode="signin", error=message, email=None, next=next_url
    )


@auth_bp.route("/auth/google")
def google_start():
    """Send the browser to Google's account chooser."""
    creds = _google_credentials()
    if creds is None:
        abort(404)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    session["google_oauth"] = {
        "state": state,
        "verifier": verifier,
        "next": safe_next(request.args.get("next")),
    }
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    params = {
        "client_id": creds[0],
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return redirect(f"{_GOOGLE_AUTH_URL}?{urlencode(params)}")


@auth_bp.route("/auth/google/callback")
def google_callback():
    """Finish Google sign-in: sign in the account with that email, or create one."""
    creds = _google_credentials()
    if creds is None:
        abort(404)
    pending = session.pop("google_oauth", None) or {}
    next_url = safe_next(pending.get("next"))
    code = request.args.get("code")
    if (
        not code
        or not pending.get("state")
        or not hmac.compare_digest(request.args.get("state", ""), pending["state"])
    ):
        return _google_refused(_GOOGLE_FAILED, next_url)

    try:
        response = requests.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": creds[0],
                "client_secret": creds[1],
                "redirect_uri": _google_redirect_uri(),
                "grant_type": "authorization_code",
                "code_verifier": pending["verifier"],
            },
            timeout=10,
        )
        response.raise_for_status()
        # The ID token's signature is not checked: it was received directly
        # from Google's token endpoint over HTTPS, which Google's OpenID
        # Connect guide allows ("Obtain user information from the ID token").
        payload = response.json()["id_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        logger.warning("Google token exchange failed", exc_info=True)
        return _google_refused(_GOOGLE_FAILED, next_url)

    if claims.get("aud") != creds[0] or claims.get("iss") not in _GOOGLE_ISSUERS:
        return _google_refused(_GOOGLE_FAILED, next_url)
    email = (claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") not in (True, "true"):
        return _google_refused(
            "Google has not verified the email on that account, so it can't "
            "be used to sign in.",
            next_url,
        )

    from shared.auth import find_auth_user_by_email, register_google_user  # noqa: PLC0415
    from shared.credits import get_service_client  # noqa: PLC0415
    from shared.events import EVENTS, emit, log_event, log_signup_rejection  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return _google_refused(_GOOGLE_UNAVAILABLE, next_url)
    try:
        user = find_auth_user_by_email(client, email)
    except Exception:
        logger.warning("Google sign-in: auth user lookup failed", exc_info=True)
        return _google_refused(_GOOGLE_UNAVAILABLE, next_url)

    if user is not None:
        # GoTrue's password grant refuses a banned user inside verify_login;
        # this path never calls it, so it checks banned_until itself.
        if _is_banned(user):
            return _google_refused("This account can't sign in.", next_url)
        if (user.get("app_metadata") or {}).get("google_linked"):
            _start_session(user.get("email") or email, user["id"], {"method": "google"})
            return redirect(next_url)
        # /signup creates accounts pre-confirmed without proving the address
        # (register_user passes email_confirm=True), so anyone could have
        # registered this email with their own password. Ask for that password
        # once; login() connects Google when it succeeds.
        session["google_link"] = {"user_id": user["id"]}
        return render_template(
            "login.html",
            mode="signin",
            error=None,
            email=user.get("email") or email,
            next=next_url,
            success_msg=(
                "You already have an account with this email. Sign in with its "
                "password once to connect Google; after that, Continue with "
                "Google signs you straight in."
            ),
        )

    ip = _client_ip()
    user_agent = request.headers.get("User-Agent")
    result = register_google_user(email, ip=ip, user_agent=user_agent)
    if not result.success:
        if result.rejection_reason:
            log_signup_rejection(
                email=email, reason=result.rejection_reason, ip=ip, user_agent=user_agent
            )
        log_event(
            event_type="signup_failed",
            session_id=session.get("anon_session_id"),
            path=request.path,
            props={
                "reason": result.failure_code or "unknown",
                "method": "google",
                "email": email[:320],
                "email_domain": email.rsplit("@", 1)[-1],
            },
            ip=ip,
            user_agent=user_agent,
        )
        return _google_refused(result.error_message, next_url)

    props = {
        "domain_class": result.classification,
        "signup_quality": result.signup_quality,
        "method": "google",
    }
    log_event(
        event_type="signup_completed",
        user_id=result.user_id,
        session_id=session.get("anon_session_id"),
        path=request.path,
        props=props,
        ip=ip,
        user_agent=user_agent,
    )
    emit(EVENTS.SIGNUP_COMPLETE, user_id=result.user_id, properties=props)
    _start_session(email, result.user_id, {"method": "google"})
    return redirect(next_url)


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Handle password reset requests.

    On POST, the same anti-bot gauntlet that gates /signup runs
    before any Supabase recovery email is sent: honeypot, signed
    timing token, email-domain classification, and an existence
    check against auth.users. Every failure path renders the same
    generic success copy a legit user sees, so a bot cannot
    enumerate valid recovery emails by probing here.
    """
    import time as _time  # noqa: PLC0415

    from shared.auth import (  # noqa: PLC0415
        ResetContext,
        issue_reset_token,
        process_reset_request,
        reset_password,
    )
    from shared.credits import get_service_client  # noqa: PLC0415

    # Single success-copy used by every outcome — bots and humans
    # see identical text.
    SUCCESS_COPY = (
        "If an account exists for that email, a reset link has "
        "been sent."
    )

    if request.method == "GET":
        return render_template(
            "login.html",
            mode="reset",
            error=None,
            email=None,
            next="/",
            reset_success=None,
            reset_token=issue_reset_token(),
        )

    email = request.form.get("email", "").strip()
    honeypot = request.form.get("website", "").strip()
    token = request.form.get("reset_token", "")

    ctx = ResetContext(
        email=email,
        reset_token=token,
        honeypot_value=honeypot,
        now_unix=int(_time.time()),
    )
    result = process_reset_request(ctx, get_service_client())

    if result.should_send_email:
        # Send the recovery email's "click here" link to tools-hub's
        # /reset-password route. Otherwise Supabase falls back to
        # the project Site URL, which on the shared Scout/tools-hub
        # project points at scout.
        public_base = os.environ.get(
            "PUBLIC_BASE_URL", "https://tools.ranomics.com"
        ).rstrip("/")
        reset_password(
            email, redirect_to=f"{public_base}/reset-password"
        )

    # Always render the same success copy — gauntlet drops and real
    # sends are indistinguishable to the caller.
    return render_template(
        "login.html",
        mode="reset",
        error=None,
        email=email,
        next="/",
        reset_success=SUCCESS_COPY,
        reset_token=issue_reset_token(),
    )

@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password_update():
    """Land Supabase recovery clicks and apply the new password.

    The recovery URL hash fragment (#access_token=...&refresh_token=...
    &type=recovery) is read client-side by JS in login.html and
    round-tripped via hidden form fields on POST.
    """
    from shared.auth import update_password  # noqa: PLC0415

    if request.method == "GET":
        return render_template(
            "login.html",
            mode="update_password",
            error=None,
            next="/",
        )

    access_token = request.form.get("access_token", "").strip()
    refresh_token = request.form.get("refresh_token", "").strip()
    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")

    if not access_token or not refresh_token:
        return render_template(
            "login.html",
            mode="update_password",
            error=(
                "Reset link is invalid or has expired. "
                "Request a new password reset email."
            ),
            next="/",
        )

    # Validation errors re-render with the tokens preserved so the user
    # can fix and resubmit without going back to their email.
    if not password:
        return render_template(
            "login.html",
            mode="update_password",
            error="Password is required.",
            access_token=access_token,
            refresh_token=refresh_token,
            next="/",
        )

    if len(password) < 8:
        return render_template(
            "login.html",
            mode="update_password",
            error="Password must be at least 8 characters.",
            access_token=access_token,
            refresh_token=refresh_token,
            next="/",
        )

    if password != password2:
        return render_template(
            "login.html",
            mode="update_password",
            error="Passwords do not match.",
            access_token=access_token,
            refresh_token=refresh_token,
            next="/",
        )

    success, error_msg = update_password(
        access_token, refresh_token, password
    )

    if success:
        return render_template(
            "login.html",
            mode="signin",
            error=None,
            email=None,
            next="/",
            success_msg=(
                "Password updated. Sign in with your new password."
            ),
        )

    # Supabase rejected the update (e.g. weak password). The recovery
    # session was consumed by set_session, so a retry needs a fresh
    # email link — don't preserve tokens here.
    return render_template(
        "login.html",
        mode="update_password",
        error=error_msg,
        next="/",
    )

@auth_bp.route("/logout", methods=["POST"])
def logout():
    """Clear the session and redirect to the login page."""
    session.clear()
    return redirect(url_for("auth.login"))


def _record_marketing_opt_out(user_id: str) -> bool:
    """Set ``user_metadata.email_preferences.marketing_email = False``.

    The crons in ``cron.reengagement`` and ``cron.signup_credit`` read this key.
    """
    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is None:
        return False
    try:
        current = client.auth.admin.get_user_by_id(user_id)
        user = getattr(current, "user", None) or current
        meta = getattr(user, "user_metadata", None)
        if meta is None and isinstance(user, dict):
            meta = user.get("user_metadata")
        meta = dict(meta) if isinstance(meta, dict) else {}
        prefs = meta.get("email_preferences")
        prefs = dict(prefs) if isinstance(prefs, dict) else {}
        prefs["marketing_email"] = False
        meta["email_preferences"] = prefs
        client.auth.admin.update_user_by_id(user_id, {"user_metadata": meta})
        return True
    except Exception:
        logger.warning("unsubscribe: could not record opt-out for %s", user_id, exc_info=True)
        return False


@auth_bp.route("/email/unsubscribe/<token>", methods=["GET", "POST"])
def email_unsubscribe(token: str):
    """Signed opt-out link from the marketing email footer and List-Unsubscribe.

    GET only shows a confirm button, so link scanners that prefetch the URL do
    not unsubscribe anyone; POST (the button, or a mail client's RFC 8058
    one-click POST) records the opt-out. CSRF-exempt in app.py: the signed
    token is the credential.
    """
    from shared.email import read_unsubscribe_token  # noqa: PLC0415

    user_id = read_unsubscribe_token(token)
    if not user_id:
        return render_template("email_unsubscribe.html", state="invalid"), 400
    if request.method == "GET":
        return render_template("email_unsubscribe.html", state="confirm")
    if not _record_marketing_opt_out(user_id):
        return render_template("email_unsubscribe.html", state="error"), 503
    return render_template("email_unsubscribe.html", state="done")


@auth_bp.route("/account", methods=["GET"])
@login_required
def account():
    """Account dashboard (wallet model).

    The legacy Workspace panels were retired with the wallet pivot;
    the wallet balance renders in the nav (see base.html) and the
    design entry point is a wallet-funded campaign.
    """
    return render_template(
        "account.html",
        user_email=session.get("user_email", ""),
    )
