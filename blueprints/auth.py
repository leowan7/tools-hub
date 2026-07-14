"""Authentication + account routes (blueprint refactor, Commit 7a).

login / signup / forgot-password / reset-password / logout and the /account
dashboard. Lifted verbatim from ``create_app()``; only ``@flask_app.route``
-> ``@auth_bp.route`` and self-refs -> ``auth.*``.
"""

from __future__ import annotations

import logging
import os

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from shared.auth import login_required

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


# ------------------------------------------------------------------
# Auth routes
# ------------------------------------------------------------------

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Render the login form (GET) or handle credential submission (POST)."""
    from shared.auth import verify_login  # noqa: PLC0415

    if request.method == "GET":
        next_url = request.args.get("next", "/")
        return render_template(
            "login.html",
            mode="signin",
            error=None,
            email=None,
            next=next_url,
        )

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    next_url = request.form.get("next", "/")

    success, error_msg, user_id = verify_login(email, password)
    if success:
        session["user_email"] = email
        if user_id:
            session["user_id"] = user_id
        # Restrict redirect to same-origin paths to prevent open
        # redirect. A leading "/" alone is not enough: browsers treat
        # "//host" (protocol-relative) and "/\host" as absolute URLs
        # to another origin, so reject those too.
        if (
            not next_url.startswith("/")
            or next_url.startswith("//")
            or next_url.startswith("/\\")
        ):
            next_url = "/"
        try:
            from shared.events import log_event  # noqa: PLC0415
            log_event(
                event_type="login",
                user_id=user_id,
                session_id=session.get("anon_session_id"),
                path="/login",
                ip=(request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                    or request.remote_addr),
                user_agent=request.headers.get("User-Agent"),
            )
        except Exception:
            logger.warning("login event log failed", exc_info=True)
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

    if request.method == "GET":
        return render_template(
            "login.html",
            mode="signup",
            error=None,
            signup_email=None,
            signup_purpose=None,
            signup_terms=False,
            signup_token=issue_signup_token(),
            next="/",
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
            next="/",
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
            next="/",
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
            next="/",
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
            next="/",
        )

    # Wallet signup credit ($5 by default) is granted lazily when the
    # user_wallets row is first created on sign-in
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
        next="/",
        success_msg=(
            "Account created with $5 of compute credit. "
            "Sign in to get started."
        ),
    )

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
