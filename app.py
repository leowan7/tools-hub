"""Flask application for the Ranomics tools hub.

Hosts Ranomics' free scientific tools as lead magnets under
``tools.ranomics.com``. Today:

    /                     — hub index with tool cards
    /login, /signup,
    /forgot-password,
    /logout               — Supabase auth (shares Scout's project)
    /account              — simple logged-in user dashboard
    /health               — unauthenticated health check
    /developability       — Binder Developability Scout (form)
    /developability/score — Binder Developability Scout (results)
    /library-planner      — coming-soon placeholder

Auth helpers live in ``shared.auth``. Tool modules live under
``tools/<name>/`` — each one exposes a small stable API that the hub
imports lazily (scoring/analysis only, no Flask coupling inside tools).

Runs with:
    gunicorn app:app
or:
    flask --app app run
"""

import logging
import os

# Load .env for local dev. In production (Railway) env vars come from the
# platform, so load_dotenv is a silent no-op when no .env file is present.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_compress import Compress
from werkzeug.middleware.proxy_fix import ProxyFix

from gpu.modal_client import ModalClient
from shared.credits import (
    load_user_context,
    record_spend,
    recent_ledger,
    requires_credits,
)
from shared.feature_flags import tool_enabled
from shared.idempotency import idempotent
from shared.handoffs import get_handoff, mark_consumed
from shared.jobs import (
    cancel_job,
    complete_job,
    create_job,
    get_job,
    list_jobs_for_user,
    list_jobs_paginated,
    mark_failed,
    mark_running,
    set_modal_call,
    update_inputs,
)
from shared.metrics import register_metrics
from shared.pdb_inspect import (
    inspect_pdb_bytes,
    summarize_for_log,
    validate_hotspots,
    validate_target_chain,
)
from shared.storage import (
    StorageError,
    copy_input,
    presigned_input_url,
    stage_campaign_candidates,
    upload_input,
)
from shared import metric_glossary as _metric_glossary
from tools import base as tool_base
import tools.af2         # noqa: F401 — import to register adapter (D2 atomic)
import tools.bindcraft   # noqa: F401 — import to register adapter
import tools.boltzgen    # noqa: F401 — import to register adapter
import tools.colabfold   # noqa: F401 — import to register adapter (D3 atomic)
import tools.esmfold     # noqa: F401 — import to register adapter (D4 atomic)
import tools.mpnn        # noqa: F401 — import to register adapter (D1 atomic)
import tools.pxdesign    # noqa: F401 — import to register adapter
import tools.rfantibody  # noqa: F401 — import to register adapter
import tools.rfdiffusion # noqa: F401 — import to register adapter
from scout import scout_bp
from webhooks.modal import register_modal_webhooks
from webhooks.stripe import register_stripe_webhook

logger = logging.getLogger(__name__)


# Static taglines for the hardcoded (non-adapter) tools. These three tools
# are not part of the GPU tool_base registry, so they are added to the
# catalog directly. Pricing strings are shown verbatim as the credit_band
# in tile renders.
_HARDCODED_TOOLS: tuple[dict, ...] = (
    {
        "slug": "epitope-scout",
        "name": "Epitope Scout",
        "tagline": (
            "Score a target's surface for binder-design feasibility "
            "before committing GPU time."
        ),
        "comparison_one_liner": (
            "Pick Epitope Scout first to identify candidate epitopes "
            "and per-dimension feasibility for any target."
        ),
        "category": "Scope the target",
        "smoke_runtime": "~30 s",
        "pilot_runtime": "—",
        "smoke_credits": "Free",
        "pilot_credits": "Free",
        "credit_band": "Free",
        "runtime_band": "~30 s",
        "is_free": True,
        "paper_citation": "—",
        "paper_url": "",
        "github_url": "",
        "endpoint": "scout.index",
        "external": False,
        "status": "live",
    },
    {
        "slug": "developability",
        "name": "Binder Developability Scout",
        "tagline": (
            "Flag developability liabilities in antibody and nanobody "
            "sequences before you order them."
        ),
        "comparison_one_liner": (
            "Pick Developability Scout when you have a sequence and "
            "need a quick liability scan (CDR length, hydrophobic "
            "patches, charge balance, isoelectric point)."
        ),
        "category": "Check developability",
        "smoke_runtime": "<5 s",
        "pilot_runtime": "—",
        "smoke_credits": "Free",
        "pilot_credits": "Free",
        "credit_band": "Free",
        "runtime_band": "<5 s",
        "is_free": True,
        "paper_citation": "—",
        "paper_url": "",
        "github_url": "",
        "endpoint": "developability",
        "external": False,
        "status": "live",
    },
    {
        "slug": "library-planner",
        "name": "Yeast Display Library Planner",
        "tagline": (
            "Plan yeast display libraries with realistic diversity and "
            "screen-size estimates for your scaffold and Kd target."
        ),
        "comparison_one_liner": (
            "Pick the Library Planner when you have a binder design "
            "shortlist and need to scope library size, diversification "
            "scheme, and screening throughput before ordering DNA."
        ),
        "category": "Scope the target",
        "smoke_runtime": "<5 s",
        "pilot_runtime": "—",
        "smoke_credits": "Free",
        "pilot_credits": "Free",
        "credit_band": "Free",
        "runtime_band": "<5 s",
        "is_free": True,
        "paper_citation": "—",
        "paper_url": "",
        "github_url": "",
        "endpoint": "library_planner",
        "external": False,
        "status": "live",
    },
)


# Maps each GPU tool slug to a workflow-stage category. The four
# categories track the iteration loop scientists actually walk through:
# scope a target, design binders against it, predict structures to
# validate in silico, then check developability before ordering.
# A flat 12-tile grid was visually noisy; grouping by stage makes the
# workflow visible at a glance.
_TOOL_CATEGORIES: dict[str, str] = {
    "mpnn": "Design binders",
    "bindcraft": "Design binders",
    "rfantibody": "Design binders",
    "rfdiffusion": "Design binders",
    "boltzgen": "Design binders",
    "pxdesign": "Design binders",
    "af2": "Predict structures",
    "colabfold": "Predict structures",
    "esmfold": "Predict structures",
}


def _build_tools_catalog() -> list[dict]:
    """Return the unified tool catalog used by the homepage tile grid
    and the ``/tools`` discovery page.

    Each entry includes display name, tagline, category, route, and
    runtime / credit bands so a single template can render the tile
    layout, the comparison matrix, and the homepage cards from one
    source of truth. Hardcoded tools (Epitope Scout, Developability,
    Library Planner) are included regardless of feature flags;
    GPU adapters are filtered through ``tool_enabled`` so a flag-off
    tool stays invisible to the catalog.
    """
    import importlib  # noqa: PLC0415

    catalog: list[dict] = []

    # Hardcoded tools (not part of tool_base registry). External Scout
    # link is resolved at request time so the URL adapts to the current
    # host (covers local dev vs production).
    for entry in _HARDCODED_TOOLS:
        item = dict(entry)
        endpoint = item.pop("endpoint", None)
        try:
            item["route"] = url_for(endpoint) if endpoint else "#"
        except Exception:  # noqa: BLE001 — outside request context
            item["route"] = "#"
        catalog.append(item)

    # GPU adapters (flag-gated).
    for adapter in tool_base.all_adapters():
        if not tool_enabled(adapter.slug):
            continue

        meta = None
        try:
            meta = importlib.import_module(f"tools.{adapter.slug}.meta")
        except ImportError:
            pass

        smoke_runtime = "—"
        pilot_runtime = "—"
        if meta is not None:
            runtime_map = getattr(meta, "PRESET_RUNTIME", None)
            if runtime_map:
                smoke_entry = runtime_map.get("smoke") or {}
                pilot_entry = runtime_map.get("pilot") or {}
                if smoke_entry.get("typical_minutes"):
                    smoke_runtime = f"{smoke_entry['typical_minutes']} min"
                if pilot_entry.get("typical_minutes"):
                    pilot_runtime = f"{pilot_entry['typical_minutes']} min"
            else:
                legacy_rows = getattr(meta, "preset_runtime_rows", None) or ()
                for legacy in legacy_rows:
                    if legacy.get("slug") == "smoke" and legacy.get("runtime"):
                        smoke_runtime = legacy["runtime"]
                    if legacy.get("slug") == "pilot" and legacy.get("runtime"):
                        pilot_runtime = legacy["runtime"]

        # Smoke / pilot are the conventional preset slugs for composite
        # tools. Atomic primitives (MPNN, AF2, ColabFold, ESMFold) use
        # ``smoke`` + ``standalone`` instead. Compute the band by looking
        # at every preset on the adapter so the catalog stays correct
        # regardless of preset naming.
        smoke_preset = adapter.preset_for("smoke")
        pilot_preset = adapter.preset_for("pilot")
        smoke_credits_int = smoke_preset.credits_cost if smoke_preset else None
        pilot_credits_int = pilot_preset.credits_cost if pilot_preset else None
        smoke_credits = (
            "Free" if smoke_credits_int == 0
            else (str(smoke_credits_int) if smoke_credits_int is not None else "—")
        )
        pilot_credits = (
            str(pilot_credits_int) if pilot_credits_int is not None else "—"
        )

        all_costs = [
            p.credits_cost for p in adapter.presets
            if p.credits_cost is not None
        ]
        non_zero_costs = [c for c in all_costs if c > 0]
        has_free_tier = any(c == 0 for c in all_costs)
        if non_zero_costs:
            min_cost = min(non_zero_costs)
            max_cost = max(non_zero_costs)
            if has_free_tier and min_cost == max_cost:
                credit_band = f"Free or {max_cost} cr"
            elif has_free_tier:
                credit_band = f"Free · {min_cost}–{max_cost} cr"
            elif min_cost == max_cost:
                credit_band = f"{max_cost} cr"
            else:
                credit_band = f"{min_cost}–{max_cost} cr"
        elif has_free_tier:
            credit_band = "Free"
        else:
            credit_band = "—"

        if smoke_runtime != "—" and pilot_runtime != "—" and smoke_runtime != pilot_runtime:
            runtime_band = f"{smoke_runtime} – {pilot_runtime}"
        elif pilot_runtime != "—":
            runtime_band = pilot_runtime
        elif smoke_runtime != "—":
            runtime_band = smoke_runtime
        else:
            runtime_band = "—"

        display_name = adapter.label.split("—")[0].strip() or adapter.label
        try:
            route = url_for("tool_form", tool=adapter.slug)
        except Exception:  # noqa: BLE001
            route = f"/tools/{adapter.slug}"

        catalog.append(
            {
                "slug": adapter.slug,
                "name": display_name,
                "tagline": adapter.blurb,
                "comparison_one_liner": getattr(
                    meta, "comparison_one_liner", "—"
                ) if meta is not None else "—",
                "category": _TOOL_CATEGORIES.get(adapter.slug, "Other"),
                "smoke_runtime": smoke_runtime,
                "pilot_runtime": pilot_runtime,
                "smoke_credits": smoke_credits,
                "pilot_credits": pilot_credits,
                "runtime_band": runtime_band,
                "credit_band": credit_band,
                "is_free": smoke_credits_int == 0,
                "paper_citation": getattr(
                    meta, "paper_citation", "—"
                ) if meta is not None else "—",
                "paper_url": getattr(
                    meta, "paper_url", ""
                ) if meta is not None else "",
                "github_url": getattr(
                    meta, "github_url", ""
                ) if meta is not None else "",
                "route": route,
                "external": False,
                "status": "live",
            }
        )

    return catalog


def create_app() -> Flask:
    """Create and configure the tools-hub Flask application.

    Returns:
        Flask: Configured Flask application instance.
    """
    flask_app = Flask(__name__)

    # Trust the X-Forwarded-Proto/X-Forwarded-Host headers Railway sets.
    # Without this, Flask sees the internal http:// hop and url_for(_external=True)
    # generates http:// URLs — which Railway 405s when the Modal pipeline tries
    # to POST a webhook back to /webhooks/modal/<job_id>/<token>. PREFERRED_URL_SCHEME
    # is the belt-and-suspenders fallback if the header is ever stripped upstream.
    flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_proto=1, x_host=1)
    flask_app.config["PREFERRED_URL_SCHEME"] = os.environ.get(
        "PREFERRED_URL_SCHEME", "https"
    )

    # Enable gzip/brotli compression on text responses (HTML, CSS, JS, JSON).
    # Reduces transfer size 70-90% on repeat-heavy pages and speeds up first paint.
    Compress(flask_app)

    # Secret key for signing Flask session cookies. Set SESSION_SECRET_KEY
    # in the deployment environment. Random fallback means sessions do not
    # survive restarts, which is acceptable for an internal tool.
    flask_app.config["SECRET_KEY"] = os.environ.get(
        "SESSION_SECRET_KEY", os.urandom(32)
    )

    # Metric glossary available in all templates (candidate_table macro reads it).
    flask_app.jinja_env.globals["metric_glossary"] = _metric_glossary.GLOSSARY

    # ``tool_about(adapter)`` returns the structured About-panel dict
    # from ``tools/<slug>/meta.py``. Lets refactored form templates
    # render the shared about_panel macro without every render_template
    # call site needing to pass an explicit ``about=`` kwarg.
    def _tool_about(adapter):
        import importlib  # noqa: PLC0415
        if adapter is None:
            return {}
        try:
            meta = importlib.import_module(f"tools.{adapter.slug}.meta")
        except ImportError:
            return {}
        return getattr(meta, "about", {}) or {}
    flask_app.jinja_env.globals["tool_about"] = _tool_about

    # Inject Workspace context into every template so the shared header
    # can render the "Active Workspaces (N)" badge. Replaces the legacy
    # ranomics_tier / ranomics_credits injection from the subscription
    # model. ``now`` is also injected so workspace templates can render
    # "N days remaining" without each view recomputing it.
    @flask_app.context_processor
    def inject_workspace_context():
        from datetime import datetime, timezone  # noqa: PLC0415
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.workspaces import active_workspaces_count  # noqa: PLC0415

        email = session.get("user_email") or ""
        base = {
            "now": datetime.now(timezone.utc),
            "active_workspaces_count": 0,
            "ranomics_user_id": None,
            "is_staff": email in STAFF_EMAILS,
        }
        if not email:
            return base
        ctx = load_user_context()
        if ctx is None:
            return base
        base["active_workspaces_count"] = active_workspaces_count(ctx.user_id)
        base["ranomics_user_id"] = ctx.user_id
        return base

    # Stripe webhook — mounted at /webhooks/stripe. Signature verification
    # + event_id idempotency live inside webhooks/stripe.py.
    register_stripe_webhook(flask_app)

    # Prometheus /metrics (IP-allowlisted) + /healthz readiness probe.
    # The existing /health liveness probe below stays as a dumb 200.
    register_metrics(flask_app)

    # Modal pipeline callbacks — /webhooks/modal/<job_id>/<token> + /webhooks/heartbeat.
    register_modal_webhooks(flask_app)

    # Scout (free tier) blueprint — everything under /scout.
    from pathlib import Path as _Path  # noqa: PLC0415
    _Path("tmp").mkdir(exist_ok=True)
    flask_app.config.setdefault("MAX_CONTENT_LENGTH", 20 * 1024 * 1024)
    flask_app.register_blueprint(scout_bp)

    # Single Modal client shared across stub tool routes.
    modal_client = ModalClient()

    # ------------------------------------------------------------------
    # Auth routes
    # ------------------------------------------------------------------

    @flask_app.route("/login", methods=["GET", "POST"])
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
            # Restrict redirect to same-origin paths to prevent open redirect.
            if not next_url.startswith("/"):
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

    @flask_app.route("/signup", methods=["GET", "POST"])
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

        if request.method == "GET":
            return render_template(
                "login.html",
                mode="signup",
                error=None,
                signup_email=None,
                signup_purpose=None,
                signup_token=issue_signup_token(),
                next="/",
            )

        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        purpose = request.form.get("purpose", "").strip()
        honeypot = request.form.get("website", "").strip()
        token = request.form.get("signup_token", "")
        client_ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                     or request.remote_addr)
        user_agent = request.headers.get("User-Agent")

        # Password-pair check runs before register_user so the user sees
        # this error without re-typing their email + purpose. The
        # honeypot / timing checks still happen below — confirmation
        # mismatch is a UX problem, not a junk-filter problem.
        if password and password2 and password != password2:
            return render_template(
                "login.html",
                mode="signup",
                error="Passwords do not match.",
                signup_email=email,
                signup_purpose=purpose,
                signup_token=issue_signup_token(),
                next="/",
            )
        if password and len(password) < 8:
            return render_template(
                "login.html",
                mode="signup",
                error="Password must be at least 8 characters.",
                signup_email=email,
                signup_purpose=purpose,
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
            return render_template(
                "login.html",
                mode="signup",
                error=result.error_message,
                signup_email=email,
                signup_purpose=purpose,
                signup_token=issue_signup_token(),
                next="/",
            )

        # Grant 10 signup-bonus credits immediately. The row lands
        # even if Supabase requires email confirmation before sign-in;
        # the balance is waiting when the user first logs in.
        if result.user_id:
            from shared.credits import record_grant  # noqa: PLC0415
            try:
                record_grant(
                    result.user_id,
                    10,
                    reason="signup bonus",
                    metadata={
                        "source": "signup",
                        "signup_quality": result.signup_quality,
                    },
                )
            except Exception:
                logger.warning(
                    "Signup-bonus grant failed for %s", email,
                    exc_info=True,
                )

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

        return render_template(
            "login.html",
            mode="signin",
            error=None,
            email=email,
            next="/",
            success_msg=(
                "Account created with 10 free credits. "
                "Sign in to get started."
            ),
        )

    @flask_app.route("/api/track", methods=["POST"])
    def api_track():
        """Append a behavioural event to ``public.user_events``.

        Body: JSON ``{event_type, path?, props?, session_id?}``.
        Returns 204 always (best-effort).
        """
        from shared.events import log_event  # noqa: PLC0415

        try:
            payload = request.get_json(silent=True) or {}
        except Exception:
            payload = {}
        event_type = str(payload.get("event_type") or "").strip()[:64]
        if not event_type:
            return ("", 204)
        path = payload.get("path")
        props_raw = payload.get("props") or {}
        props = props_raw if isinstance(props_raw, dict) else {}
        session_id = (payload.get("session_id") or "").strip() or None
        if session_id:
            session["anon_session_id"] = session_id[:64]
        elif session.get("anon_session_id"):
            session_id = session["anon_session_id"]

        log_event(
            event_type=event_type,
            user_id=session.get("user_id"),
            session_id=session_id,
            path=path if isinstance(path, str) else None,
            props=props,
            ip=(request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or request.remote_addr),
            user_agent=request.headers.get("User-Agent"),
        )
        return ("", 204)

    @flask_app.route("/forgot-password", methods=["GET", "POST"])
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

    @flask_app.route("/reset-password", methods=["GET", "POST"])
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

    @flask_app.route("/logout", methods=["POST"])
    def logout():
        """Clear the session and redirect to the login page."""
        session.clear()
        return redirect(url_for("login"))

    @flask_app.route("/health", methods=["GET"])
    def health():
        """Unauthenticated health check for Railway port scanner."""
        return jsonify({"status": "ok"}), 200

    # ------------------------------------------------------------------
    # Protected routes
    # ------------------------------------------------------------------

    from shared.auth import login_required  # noqa: PLC0415

    @flask_app.route("/", methods=["GET"])
    def index():
        """Landing page.

        For anonymous visitors: marketing hero + tool catalog tiles
        with sign-in CTAs, so first-time visitors can see what runs on
        the platform without signing up.

        For authenticated users: a "Recent runs" dashboard strip on top
        (top 3 jobs with clone shortcuts), then the tool catalog tiles
        for new runs.
        """
        catalog = _build_tools_catalog()

        # Match the grouped layout used by /tools — same categories,
        # same order, just rendered as wide tile sections instead of a
        # comparison matrix.
        category_order = (
            "Scope the target",
            "Design binders",
            "Predict structures",
            "Check developability",
            "Other",
        )
        grouped: list[tuple[str, list[dict]]] = []
        for category in category_order:
            members = [t for t in catalog if t.get("category") == category]
            if members:
                grouped.append((category, members))

        recent_jobs: list = []
        if session.get("user_email"):
            ctx = load_user_context()
            if ctx is not None:
                try:
                    recent_jobs = list(
                        list_jobs_for_user(ctx.user_id, limit=3)
                    )
                except Exception:  # noqa: BLE001 — never block the homepage
                    logger.exception("Failed to load recent jobs for homepage")
                    recent_jobs = []

        return render_template(
            "index.html",
            tools=catalog,
            grouped=grouped,
            recent_jobs=recent_jobs,
            authenticated=bool(session.get("user_email")),
        )

    @flask_app.route("/pricing", methods=["GET"])
    def pricing():
        """Public pricing page — logged-out visitors can reach it."""
        return render_template("pricing.html")

    @flask_app.route("/billing/checkout", methods=["GET"])
    @login_required
    def billing_checkout():
        """Create a Stripe Checkout Session for a Workspace SKU.

        Accepts ``?sku=workspace_standard|workspace_xl`` plus a
        ``target_pdb_id`` query param (set by /workspaces/new POST
        after the user has uploaded their target PDB to storage).
        """
        from billing.checkout import create_checkout_session  # noqa: PLC0415
        from billing.tiers import SKU_NAMES  # noqa: PLC0415

        sku = request.args.get("sku", "").strip()
        target_pdb_id = request.args.get("target_pdb_id", "").strip()
        target_label = request.args.get("target_label", "").strip() or None
        if sku not in SKU_NAMES:
            return redirect(url_for("pricing"))
        if not target_pdb_id:
            # Bounce back to the new-Workspace form so the user can
            # upload a target before paying.
            return redirect(f"/workspaces/new?sku={sku}")

        base = request.url_root.rstrip("/")
        success_url = base + "/workspaces?success=1"
        cancel_url = base + url_for("pricing") + "?cancelled=1"

        url, error = create_checkout_session(
            sku,
            target_pdb_id=target_pdb_id,
            target_label=target_label,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        if error or not url:
            logger.warning("Checkout creation failed: %s", error)
            return redirect(url_for("pricing") + "?checkout_error=1")
        return redirect(url, code=303)

    # ------------------------------------------------------------------
    # Workspace routes (new — replaces the legacy subscription tier
    # gating). See shared/workspaces.py for the lifecycle module.
    # ------------------------------------------------------------------

    @flask_app.route("/workspaces", methods=["GET"])
    @login_required
    def workspaces_list():
        """List active + past Workspaces for the signed-in user."""
        from shared.workspaces import (  # noqa: PLC0415
            list_active_workspaces,
            list_workspace_history,
        )

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        active = list_active_workspaces(ctx.user_id)
        history = list_workspace_history(ctx.user_id, limit=20)
        # Hide currently-active rows from the history view (avoid dupes).
        active_ids = {ws.id for ws in active}
        expired_or_refunded = [
            ws for ws in history if ws.id not in active_ids
        ]
        return render_template(
            "workspaces/list.html",
            active_workspaces=active,
            expired_workspaces=expired_or_refunded,
        )

    @flask_app.route("/workspaces/new", methods=["GET"])
    @login_required
    def workspaces_new():
        """Render the new-Workspace form (upload PDB + confirm SKU)."""
        from billing.tiers import SKU_NAMES  # noqa: PLC0415

        sku = request.args.get("sku", "workspace_standard").strip()
        if sku not in SKU_NAMES:
            sku = "workspace_standard"
        return render_template("workspaces/new.html", sku=sku)

    @flask_app.route("/workspaces/new", methods=["POST"])
    @login_required
    def workspaces_new_submit():
        """Handle PDB upload, stage it in storage, redirect to Stripe.

        On success: redirects to /billing/checkout with the resolved
        target_pdb_id baked into the URL so the Stripe checkout session
        carries it through to the activation webhook.
        """
        from billing.tiers import SKU_NAMES  # noqa: PLC0415
        from shared.storage import upload_input  # noqa: PLC0415
        from shared.pdb_inspect import inspect_pdb  # noqa: PLC0415

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        sku = (request.form.get("sku") or "workspace_standard").strip()
        if sku not in SKU_NAMES:
            sku = "workspace_standard"
        target_label = (request.form.get("target_label") or "").strip()

        uploaded = request.files.get("target_pdb_file")
        if not uploaded or not uploaded.filename:
            return redirect(f"/workspaces/new?sku={sku}&error=missing_pdb")

        data = uploaded.read()
        if not data:
            return redirect(f"/workspaces/new?sku={sku}&error=empty_pdb")

        # Stash under workspace-targets/{user_id}/{timestamp}-{filename}.
        # We use a synthetic "job_id" slot to namespace the upload; the
        # actual Workspace id doesn't exist yet (created by the Stripe
        # webhook after payment).
        import secrets  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415
        upload_token = (
            datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            + "-" + secrets.token_hex(4)
        )
        try:
            object_path = upload_input(
                user_id=ctx.user_id,
                job_id=f"workspace-target-{upload_token}",
                filename=uploaded.filename,
                data=data,
                content_type="chemical/x-pdb",
            )
        except Exception:
            logger.exception(
                "Failed to stage target PDB for user=%s sku=%s",
                ctx.user_id, sku,
            )
            return redirect(f"/workspaces/new?sku={sku}&error=upload_failed")

        # Optional: sanity-check the PDB structure (chains, residues).
        # Failure here is non-fatal — we'd rather let a quirky PDB
        # through than block a paying customer.
        try:
            inspect_pdb(data)
        except Exception:
            logger.info(
                "PDB inspection failed for upload by %s; continuing.",
                ctx.user_id, exc_info=True,
            )

        # The "target_pdb_id" we pass through to Stripe metadata is the
        # storage object path itself — it's globally unique, durable, and
        # the Modal pipeline can fetch the PDB via presigned URL when
        # actually running a tool inside this Workspace.
        target_pdb_id = object_path
        from urllib.parse import quote_plus  # noqa: PLC0415
        return redirect(
            "/billing/checkout"
            f"?sku={sku}"
            f"&target_pdb_id={quote_plus(target_pdb_id)}"
            f"&target_label={quote_plus(target_label)}"
        )

    @flask_app.route("/workspaces/<workspace_id>", methods=["GET"])
    @login_required
    def workspace_detail(workspace_id: str):
        """Show a single Workspace dashboard with cap meter + tool buttons."""
        from shared.workspaces import get_workspace  # noqa: PLC0415

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        ws = get_workspace(workspace_id)
        if ws is None or ws.user_id != ctx.user_id:
            return redirect(url_for("workspaces_list"))

        # Pull recent jobs scoped to this Workspace's target. (The
        # tool_jobs table doesn't yet have a workspace_id column — we
        # match on target via job metadata when the route handlers add
        # it; for now show all the user's recent jobs as a fallback.)
        workspace_jobs: list = []
        try:
            from shared.jobs import list_jobs_for_user  # noqa: PLC0415
            workspace_jobs = list_jobs_for_user(ctx.user_id, limit=10)
        except Exception:
            workspace_jobs = []

        return render_template(
            "workspaces/detail.html",
            workspace=ws,
            workspace_jobs=workspace_jobs,
        )

    @flask_app.route("/billing/portal", methods=["GET"])
    @login_required
    def billing_portal():
        """Redirect the user to their Stripe Billing Portal session."""
        from billing.checkout import create_portal_session  # noqa: PLC0415

        base = request.url_root.rstrip("/")
        return_url = base + url_for("account")

        url, error = create_portal_session(return_url=return_url)
        if error or not url:
            logger.warning("Portal creation failed: %s", error)
            return redirect(url_for("account") + "?portal_error=1")
        return redirect(url, code=303)

    @flask_app.route("/account", methods=["GET"])
    @login_required
    def account():
        """Account dashboard: active Workspaces + Workspace history."""
        from shared.workspaces import (  # noqa: PLC0415
            list_active_workspaces,
            list_workspace_history,
        )

        ctx = load_user_context()
        active: list = []
        history: list = []
        if ctx is not None:
            active = list_active_workspaces(ctx.user_id)
            history = list_workspace_history(ctx.user_id, limit=20)
        return render_template(
            "account.html",
            user_email=session.get("user_email", ""),
            active_workspaces=active,
            workspace_history=history,
        )

    # ------------------------------------------------------------------
    # Stub tool route — proves credits middleware + Modal client contract
    # work end-to-end without a real GPU call. Stream C/D tools follow
    # the same pattern: @login_required, @requires_credits, render a
    # response, let the decorator debit on success.
    # ------------------------------------------------------------------

    @flask_app.route("/tools/example-gpu", methods=["GET"])
    @login_required
    def example_gpu():
        """Render the example-gpu form."""
        return render_template("example_gpu.html", submission=None)

    @flask_app.route("/tools/example-gpu/submit", methods=["POST"])
    @login_required
    @idempotent()
    @requires_credits(
        1, tool="example-gpu", reason="example-gpu smoke submission"
    )
    def example_gpu_submit():
        """Submit the stub job via ModalClient.

        Returns the fake FunctionCall id from the Wave-0 stub so we can
        verify the credits decorator debits the user on success.
        """
        preset = request.form.get("preset", "smoke")
        submission = modal_client.submit(
            tool="example-gpu",
            preset=preset,
            inputs={"_wave0_stub": True},
        )
        submission["preset"] = preset
        return render_template(
            "example_gpu.html", submission=submission
        )

    @flask_app.route("/developability", methods=["GET"])
    @login_required
    def developability():
        """Render the Binder Developability Scout input form."""
        return render_template(
            "developability_form.html",
            error=None,
            sequence="",
            chain_type="VH",
        )

    @flask_app.route("/developability/score", methods=["POST"])
    @login_required
    @idempotent()
    def developability_score():
        """Validate input and render the developability results page."""
        from tools.developability import score_developability  # noqa: PLC0415

        raw_sequence = request.form.get("sequence", "")
        chain_type = request.form.get("chain_type", "VH").strip() or "VH"

        # Strip FASTA headers (lines starting with '>') and whitespace.
        lines = [
            line.strip()
            for line in raw_sequence.splitlines()
            if line and not line.lstrip().startswith(">")
        ]
        cleaned_sequence = "".join(lines).replace(" ", "").upper()

        # Allowed chain types for the UI select; scoring accepts broader set.
        allowed_chains = {"VH", "VL", "VK", "SCFV", "VHH", "OTHER"}
        if chain_type.upper() not in allowed_chains:
            chain_type = "VH"
        chain_type = chain_type.upper()

        # Sequence validation.
        valid_aa = set("ACDEFGHIKLMNPQRSTVWY")
        error = None
        if not cleaned_sequence:
            error = "Paste a sequence before submitting."
        elif not (10 <= len(cleaned_sequence) <= 2000):
            error = (
                f"Sequence length must be between 10 and 2000 residues "
                f"(got {len(cleaned_sequence)})."
            )
        else:
            bad = sorted(set(cleaned_sequence) - valid_aa)
            if bad:
                error = (
                    "Sequence contains non-canonical residues: "
                    + ", ".join(bad)
                    + ". Only the 20 standard amino acids are accepted."
                )

        if error:
            return render_template(
                "developability_form.html",
                error=error,
                sequence=raw_sequence,
                chain_type=chain_type,
            )

        try:
            result = score_developability(
                cleaned_sequence,
                chain_type=chain_type,
            )
        except ValueError as exc:
            return render_template(
                "developability_form.html",
                error=str(exc),
                sequence=raw_sequence,
                chain_type=chain_type,
            )

        return render_template(
            "developability_results.html",
            result=result,
        )

    @flask_app.route("/library-planner", methods=["GET"])
    @login_required
    def library_planner():
        """Render the Yeast Display Library Planner input form."""
        return render_template(
            "library_planner_form.html",
            error=None,
            form_values=None,
        )

    @flask_app.route("/library-planner/plan", methods=["POST"])
    @login_required
    @idempotent()
    def library_planner_plan():
        """Validate inputs and render the library planner results page."""
        from tools.library_planner import plan_library  # noqa: PLC0415

        raw = {
            "scaffold": request.form.get("scaffold", "").strip(),
            "positions": request.form.get("positions", "").strip(),
            "scheme": request.form.get("scheme", "").strip(),
            "kd_nm": request.form.get("kd_nm", "").strip(),
            "starting_material": request.form.get(
                "starting_material", ""
            ).strip(),
            "coverage_pct": request.form.get("coverage_pct", "90").strip(),
        }

        error = None
        try:
            positions = int(raw["positions"])
        except ValueError:
            positions = None
            error = "Diversified positions must be a whole number."
        try:
            kd_nm = float(raw["kd_nm"])
        except ValueError:
            kd_nm = None
            if error is None:
                error = "Target KD must be a number in nanomolar."
        try:
            coverage_pct = float(raw["coverage_pct"])
        except ValueError:
            coverage_pct = 90.0

        if coverage_pct <= 0 or coverage_pct >= 100:
            coverage_pct = 90.0

        if error is None and (positions is None or positions < 1):
            error = "Diversified positions must be at least 1."
        if error is None and positions is not None and positions > 40:
            error = (
                "Diversified positions capped at 40 for this tool. "
                "For combinatorial libraries beyond 40 positions, please "
                "reach out to the Ranomics team."
            )
        if error is None and (kd_nm is None or kd_nm <= 0):
            error = "Target KD must be greater than zero."

        if error:
            return render_template(
                "library_planner_form.html",
                error=error,
                form_values=raw,
            )

        try:
            plan = plan_library(
                scaffold=raw["scaffold"],
                diversification_positions=positions,
                diversification_scheme=raw["scheme"],
                target_kd_nm=kd_nm,
                starting_material=raw["starting_material"],
                target_coverage=coverage_pct / 100.0,
            )
        except ValueError as exc:
            return render_template(
                "library_planner_form.html",
                error=str(exc),
                form_values=raw,
            )

        return render_template(
            "library_planner_results.html",
            plan=plan,
        )

    # ------------------------------------------------------------------
    # GPU tool routes — one form/submit pair per registered adapter,
    # plus shared jobs routes. FLAG_TOOL_<NAME>=off hides a tool at the
    # route level so the UI can ship in one commit and the operator
    # flips the flag after verifying an end-to-end production run.
    # ------------------------------------------------------------------

    def _require_tool(tool_slug: str):
        """Return (adapter, error_response). ``error_response`` is non-None on fail."""
        adapter = tool_base.get(tool_slug)
        if adapter is None:
            return None, (render_template("404.html"), 404)
        if not tool_enabled(tool_slug):
            return None, (render_template("coming_soon.html"), 404)
        return adapter, None

    @flask_app.route("/tools/<tool>", methods=["GET"])
    @login_required
    def tool_form(tool: str):
        """Render a GPU tool's submission form.

        Pre-fill sources (query params, owner-scoped):
          * ``clone_from=<job_id>`` — reuse all inputs of an earlier job.
            Same-tool only (exact parameter fidelity).
          * ``from_job=<job_id>`` — Phase 4 cross-tool handoff. Copies
            only the target fields (target PDB reuse token, target_chain,
            hotspot_residues) and defaults preset='pilot'. Works across
            tools so a user can refine RFantibody output with BindCraft,
            validate BoltzGen output with PXDesign, etc.
          * ``handoff=<handoff_id>`` — target PDB + chain + hotspots from
            Epitope Scout via ``public.scout_handoffs``.
          * ``workspace_id=<ws_id>&target_pdb_id=<storage_path>`` —
            Workspace-funded run. The detail page at
            /workspaces/<id> emits these together so the POST gate
            (``workspace_preflight``) can verify the run is funded by
            an active Workspace and bill the actual Modal cost back.
        """
        adapter, err = _require_tool(tool)
        if err:
            return err

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        # Workspace context (Wave-2 launch). Forwarded as hidden form
        # inputs by templates/tools/_prefill.html::workspace_hidden_inputs
        # so the POST handler can re-read and gate.
        workspace_ctx: dict | None = None
        ws_id_q = (request.args.get("workspace_id") or "").strip()
        ws_target_q = (request.args.get("target_pdb_id") or "").strip()
        if ws_id_q and ws_target_q:
            workspace_ctx = {
                "workspace_id": ws_id_q,
                "target_pdb_id": ws_target_q,
            }

        pre_fill: dict = {}
        pdb_source = None  # dict describing a reusable PDB, or None

        clone_from = request.args.get("clone_from", "").strip()
        if clone_from:
            prior = get_job(clone_from, user_id=ctx.user_id)
            if prior is not None and prior.tool == adapter.slug:
                pre_fill = {
                    k: v for k, v in (prior.inputs or {}).items()
                    if not k.startswith("_")
                }
                # Normalize list-typed inputs back to form-friendly strings.
                hs = pre_fill.get("hotspot_residues")
                if isinstance(hs, list):
                    pre_fill["hotspot_residues"] = ",".join(str(x) for x in hs)
                stored_path = (prior.inputs or {}).get("_pdb_storage_path")
                stored_name = (prior.inputs or {}).get("_pdb_filename")
                if stored_path and stored_name:
                    pdb_source = {
                        "label": f"PDB from job {prior.id[:8]} ({stored_name})",
                        "filename": stored_name,
                        "token": f"job:{prior.id}",
                    }

        from_job = request.args.get("from_job", "").strip()
        if from_job and not pre_fill:
            # Cross-tool handoff: copy only target fields, default to pilot.
            # Unlike clone_from this works across tools — the binder /
            # parameter shape differs, but target_pdb + target_chain +
            # hotspots are shared across BindCraft / RFantibody /
            # BoltzGen / PXDesign.
            src = get_job(from_job, user_id=ctx.user_id)
            if src is not None:
                src_inputs = src.inputs or {}
                for key in ("target_chain", "hotspot_residues"):
                    val = src_inputs.get(key)
                    if val is None:
                        continue
                    if isinstance(val, list):
                        val = ",".join(str(x) for x in val)
                    pre_fill[key] = val
                pre_fill["preset"] = "pilot"
                stored_path = src_inputs.get("_pdb_storage_path")
                stored_name = src_inputs.get("_pdb_filename")
                if stored_path and stored_name:
                    pdb_source = {
                        "label": (
                            f"Target PDB from {src.tool} job {src.id[:8]} "
                            f"({stored_name})"
                        ),
                        "filename": stored_name,
                        "token": f"job:{src.id}",
                    }

        handoff_id = request.args.get("handoff", "").strip()
        if handoff_id:
            ho = get_handoff(handoff_id, user_id=ctx.user_id)
            if ho is not None:
                pre_fill.setdefault("target_chain", ho.target_chain)
                pre_fill.setdefault(
                    "hotspot_residues",
                    ",".join(str(r) for r in ho.hotspot_residues),
                )
                pre_fill["preset"] = "pilot"
                pdb_source = {
                    "label": f"Target PDB from Epitope Scout ({ho.pdb_filename})",
                    "filename": ho.pdb_filename,
                    "token": f"handoff:{ho.id}",
                }

        return render_template(
            adapter.form_template,
            adapter=adapter,
            error=None,
            pre_fill=pre_fill,
            pdb_source=pdb_source,
            workspace_ctx=workspace_ctx,
        )

    @flask_app.route("/tools/<tool>/submit", methods=["POST"])
    @login_required
    @idempotent()
    def tool_submit(tool: str):
        """Validate, debit credits, upload PDB, spawn Modal, redirect to job detail."""
        adapter, err = _require_tool(tool)
        if err:
            return err

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        # Workspace context (Wave-2). The /workspaces/<id> detail page
        # links to /tools/<slug>?workspace_id=...&target_pdb_id=... and
        # the form template forwards both as hidden inputs (see
        # ``workspace_hidden_inputs`` macro in
        # ``templates/tools/_prefill.html``). When present, the
        # workspace_preflight gate below rejects expired or
        # cap-exhausted workspaces BEFORE we create the job row, and the
        # IDs flow through to ``create_job`` so the completion-side
        # ``charge_for_job`` wiring (item #6) can bill the right cap.
        ws_id_form = (request.form.get("workspace_id") or "").strip()
        ws_target_form = (request.form.get("target_pdb_id") or "").strip()
        workspace_ctx: dict | None = None
        if ws_id_form and ws_target_form:
            workspace_ctx = {
                "workspace_id": ws_id_form,
                "target_pdb_id": ws_target_form,
            }

        inputs, error_msg = adapter.validate(request.form, request.files)
        if inputs is None:
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=error_msg,
                pre_fill=dict(request.form.items()),
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        preset = adapter.preset_for(inputs["preset"])
        if preset is None:
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error="Unknown preset.",
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        # Workspace gate (when context present). Rejects expired,
        # refunded, or cap-exhausted workspaces BEFORE the job row is
        # written, BEFORE PDB upload, BEFORE the Modal call. Submissions
        # without workspace context fall through to the legacy credits
        # gate below — that path remains during transition; once Phase 4
        # validation is clean and all entry points (Scout handoff,
        # direct URLs) route through Workspace activation, this fall-
        # through can be flipped to a hard reject.
        if workspace_ctx is not None:
            from shared.workspaces import workspace_preflight  # noqa: PLC0415
            preflight = workspace_preflight(
                ctx.user_id, workspace_ctx["target_pdb_id"]
            )
            if not preflight.allow:
                if preflight.reason == "no_workspace":
                    return redirect(url_for("workspaces_new"))
                # cap_exceeded / expired: send the user to the workspace
                # detail so the cap meter + upgrade CTA explain why.
                return redirect(
                    url_for(
                        "workspace_detail",
                        workspace_id=workspace_ctx["workspace_id"],
                    )
                )
            # Sanity-check: the user's active workspace for this target
            # may differ from the one the form claims (e.g. if they
            # bought a second workspace mid-session). Trust the form ID
            # for charge attribution; preflight already confirmed an
            # active workspace exists for this user+target.
            workspace_ctx["workspace_id"] = preflight.workspace.id

        if ctx.balance < preset.credits_cost:
            return redirect(url_for("account", insufficient_credits=1))

        # Per-preset PDB requirement (Wave 2): pilot tier needs an upload,
        # smoke / preview do not. Falls back to the adapter-level flag for
        # legacy single-tier tools (e.g. BindCraft pilot-only).
        needs_pdb = bool(getattr(preset, "requires_pdb", False)) or adapter.requires_pdb
        uploaded = request.files.get("target_pdb")
        reuse_token = (request.form.get("reuse_pdb_token") or "").strip()

        # Gate "no PDB attached" BEFORE create_job. Otherwise the row gets
        # written, the upload check fails further down, and the row sits
        # in 'pending' forever as an orphan with no Modal call and no
        # spend ledger entry. Production incident 2026-04-30: a pxdesign
        # pilot submit with no file attached created job d2d421ad which
        # showed PENDING for 2.5 hours until manually cancelled.
        if needs_pdb and not (
            (uploaded is not None and uploaded.filename)
            or reuse_token.startswith("job:")
            or reuse_token.startswith("handoff:")
        ):
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error="Upload a target PDB file.",
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        # ---- PDB pre-flight inspection (Bug 9 follow-on) ----
        # Run a fast Biopython inspection on freshly-uploaded files so we
        # can reject obvious garbage (no protein, no ATOM records, malformed
        # parse) and validate user-typed target_chain + hotspots BEFORE
        # debiting credits or spinning up Modal. Reuse-token paths are not
        # re-inspected (the source job's PDB has already passed this gate).
        # Bytes are read here ONCE; we pass them through to upload_input
        # below so we don't need to seek the file pointer back.
        pdb_bytes: bytes | None = None
        inspection = None
        if needs_pdb and uploaded is not None and uploaded.filename:
            pdb_bytes = uploaded.read()
            inspection = inspect_pdb_bytes(pdb_bytes, filename=uploaded.filename)
            logger.info(
                "pdb_inspect %s/%s: %s",
                adapter.slug, preset.slug, summarize_for_log(inspection),
            )
            if not inspection.ok:
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=inspection.error,
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )
            target_chain = (inputs.get("target_chain") or "").strip()
            if target_chain:
                chain_err = validate_target_chain(inspection, target_chain)
                if chain_err:
                    return render_template(
                        adapter.form_template, adapter=adapter,
                        error=chain_err, pre_fill=inputs, pdb_source=None,
                        workspace_ctx=workspace_ctx,
                    )
                hotspots = inputs.get("hotspot_residues") or []
                if hotspots:
                    in_range, out_of_range = validate_hotspots(
                        inspection, target_chain, hotspots,
                    )
                    if out_of_range:
                        chain = inspection.chain(target_chain)
                        return render_template(
                            adapter.form_template, adapter=adapter,
                            error=(
                                f"Hotspot residue(s) {out_of_range} are not "
                                f"in chain {target_chain} "
                                f"(residue range: "
                                f"{chain.min_resnum}..{chain.max_resnum}). "
                                f"Use original PDB numbering."
                            ),
                            pre_fill=inputs, pdb_source=None,
                            workspace_ctx=workspace_ctx,
                        )

        # Create the tool_jobs row so we have job_id + job_token for the
        # Modal payload and a persistent handle even if Modal submit
        # raises. Credits debit happens only on successful Modal submit.
        # Workspace IDs (when present) are stashed in inputs._workspace
        # so the completion-side ``charge_for_job`` (item #6) bills the
        # right cap.
        ws_target = workspace_ctx["target_pdb_id"] if workspace_ctx else None
        ws_id = workspace_ctx["workspace_id"] if workspace_ctx else None
        job = create_job(
            user_id=ctx.user_id,
            tool=adapter.slug,
            preset=preset.slug,
            inputs=inputs,
            credits_cost=preset.credits_cost,
            target_pdb_id=ws_target,
            workspace_id=ws_id,
        )
        if job is None:
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=(
                    "Could not create job record — Supabase is unreachable. "
                    "Try again in a moment."
                ),
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        presigned_url = ""
        staged_path = ""
        staged_filename = ""
        if needs_pdb:
            try:
                if uploaded is not None and uploaded.filename:
                    staged_filename = uploaded.filename
                    # pdb_bytes was read during pre-flight inspection above;
                    # reuse it instead of double-reading the upload.
                    file_data = pdb_bytes if pdb_bytes is not None else uploaded.read()
                    staged_path = upload_input(
                        user_id=ctx.user_id,
                        job_id=job.id,
                        filename=uploaded.filename,
                        data=file_data,
                        content_type=uploaded.mimetype or "chemical/x-pdb",
                    )
                elif reuse_token.startswith("job:"):
                    # Wave 3A clone: copy PDB from the original job's prefix.
                    prior_job_id = reuse_token.split(":", 1)[1]
                    prior = get_job(prior_job_id, user_id=ctx.user_id)
                    if prior is None:
                        raise StorageError("source job not found")
                    src_path = (prior.inputs or {}).get("_pdb_storage_path")
                    src_name = (prior.inputs or {}).get("_pdb_filename")
                    if not src_path or not src_name:
                        raise StorageError("source job has no stored PDB")
                    staged_filename = src_name
                    staged_path = copy_input(
                        source_path=src_path,
                        dest_user_id=ctx.user_id,
                        dest_job_id=job.id,
                        filename=src_name,
                    )
                elif reuse_token.startswith("handoff:"):
                    # Wave 3C Scout handoff: copy PDB staged by Scout.
                    ho_id = reuse_token.split(":", 1)[1]
                    ho = get_handoff(ho_id, user_id=ctx.user_id)
                    if ho is None:
                        raise StorageError(
                            "handoff not found or already consumed"
                        )
                    staged_filename = ho.pdb_filename
                    staged_path = copy_input(
                        source_path=ho.pdb_storage_path,
                        dest_user_id=ctx.user_id,
                        dest_job_id=job.id,
                        filename=ho.pdb_filename,
                    )
                    mark_consumed(ho.id)

                presigned_url = presigned_input_url(
                    staged_path, expires_seconds=7200
                )
                # Persist the storage path + filename on the job row so a
                # future clone can re-use the file without re-uploading.
                update_inputs(
                    job.id,
                    {
                        **inputs,
                        "_pdb_storage_path": staged_path,
                        "_pdb_filename": staged_filename,
                    },
                )
            except StorageError as exc:
                mark_failed(
                    job.id,
                    error={"bucket": "storage", "detail": str(exc)},
                )
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=f"Upload failed: {exc}",
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )

        job_spec = adapter.build_payload(inputs, presigned_url)
        webhook_url = url_for(
            "modal_result",
            job_id=job.id,
            job_token=job.job_token,
            _external=True,
        )

        try:
            submit_result = modal_client.submit(
                adapter.slug,
                preset.slug,
                inputs={
                    **job_spec,
                    "_input_pdb_url": presigned_url,
                    "_input_presigned_url": presigned_url,
                },
                job_id=job.id,
                job_token=job.job_token,
                webhook_url=webhook_url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Modal submit failed for job %s", job.id)
            mark_failed(
                job.id,
                error={"bucket": "modal-submit", "detail": str(exc)},
            )
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=(
                    "Could not submit to the GPU pool. No credits were "
                    "charged. Try again or contact support."
                ),
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        set_modal_call(job.id, submit_result["function_call_id"])
        # Smoke / preview presets cost 0 credits — skip the ledger write,
        # which otherwise raises ValueError (record_spend rejects amount<=0)
        # and 500s the redirect even though the Modal job is already running.
        if preset.credits_cost > 0:
            record_spend(
                ctx.user_id,
                preset.credits_cost,
                tool=adapter.slug,
                reason=f"{adapter.slug} {preset.slug}",
                job_id=job.id,
            )

        return redirect(url_for("job_detail", job_id=job.id))

    @flask_app.route("/jobs", methods=["GET"])
    @login_required
    def jobs_list():
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            page = 1
        page_size = 25
        jobs, total = list_jobs_paginated(
            ctx.user_id, page=page, page_size=page_size
        )
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            return redirect(url_for("jobs_list", page=total_pages))
        return render_template(
            "jobs_list.html",
            jobs=jobs,
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
        )

    @flask_app.route("/jobs/compare", methods=["GET"])
    @login_required
    def jobs_compare():
        """Wave 3B cross-run compare: render selected jobs side-by-side.

        Accepts ``ids=a,b,c`` or repeated ``ids=a&ids=b``. Owner-scoped.
        """
        from shared.jobs import list_jobs_by_ids  # local import avoids cycle
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        raw = request.args.getlist("ids")
        if len(raw) == 1 and "," in raw[0]:
            raw = [x.strip() for x in raw[0].split(",") if x.strip()]
        raw = [x for x in raw if x]
        if len(raw) < 2:
            return redirect(url_for("jobs_list"))
        jobs = list_jobs_by_ids(ctx.user_id, raw[:6])
        columns = []
        for j in jobs:
            adapter = tool_base.get(j.tool)
            columns.append({
                "job": j,
                "tool_label": adapter.label if adapter else j.tool,
            })
        return render_template("jobs_compare.html", columns=columns)

    @flask_app.route("/jobs/<job_id>", methods=["GET"])
    @login_required
    def job_detail(job_id: str):
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return render_template("404.html"), 404
        adapter = tool_base.get(job.tool)
        preset_obj = adapter.preset_for(job.preset) if adapter else None

        # Phase 4 cross-tool handoff: only offer the buttons when the
        # source job staged a reusable PDB (has _pdb_storage_path) and
        # finished successfully. Skip the current tool — "send to self"
        # is what Clone is for. Skip any adapter whose input contract is
        # not PDB-based — e.g. D2 AF2, which takes FASTA. The generic
        # ``from_job`` flow only ports a PDB reuse token + chain +
        # hotspots; offering AF2 as a handoff target would drop the user
        # on a form that cannot consume the handoff (Codex P2).
        NON_PDB_INPUT_TOOLS = frozenset({"af2"})
        send_target_tools: list[dict] = []
        if (
            job.status == "succeeded"
            and (job.inputs or {}).get("_pdb_storage_path")
        ):
            for other in tool_base.all_adapters():
                if other.slug == job.tool:
                    continue
                if other.slug in NON_PDB_INPUT_TOOLS:
                    continue
                if not tool_enabled(other.slug):
                    continue
                send_target_tools.append({
                    "slug": other.slug,
                    "label": other.label,
                    "url": url_for(
                        "tool_form", tool=other.slug, from_job=job.id
                    ),
                })

        return render_template(
            "job_detail.html",
            job=job,
            tool_label=adapter.label if adapter else job.tool,
            tool_results_partial=(
                adapter.results_partial
                if adapter and adapter.results_partial
                else "tools/_default_results.html"
            ),
            is_long_running=bool(preset_obj and preset_obj.long_running),
            user_email=session.get("user_email") or "",
            send_target_tools=send_target_tools,
        )

    @flask_app.route("/jobs/<job_id>/status.json", methods=["GET"])
    @login_required
    def job_status(job_id: str):
        ctx = load_user_context()
        if ctx is None:
            return jsonify({"error": "unauthenticated"}), 401
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return jsonify({"error": "not_found"}), 404

        # If the job still thinks it is pending/running, poll Modal once
        # so terminal transitions are detected even when the webhook
        # callback has not fired (e.g. inline smoke-tier returns).
        if job.status in ("pending", "running") and job.modal_function_call_id:
            poll = modal_client.poll(job.modal_function_call_id)
            if poll["status"] == "succeeded":
                complete_job(
                    job.id,
                    terminal_status="succeeded",
                    result=poll["result"] or {},
                    gpu_seconds_used=poll.get("gpu_seconds_used"),
                )
                job = get_job(job_id, user_id=ctx.user_id)
            elif poll["status"] == "failed":
                complete_job(
                    job.id,
                    terminal_status="failed",
                    error={"bucket": "pipeline", "detail": poll.get("error") or ""},
                    gpu_seconds_used=poll.get("gpu_seconds_used"),
                )
                job = get_job(job_id, user_id=ctx.user_id)
            elif poll["status"] == "running" and job.status == "pending":
                mark_running(job.id)
                job = get_job(job_id, user_id=ctx.user_id)

        return jsonify(
            {
                "id": job.id,
                "status": job.status,
                "tool": job.tool,
                "preset": job.preset,
                "progress": (job.inputs or {}).get("_progress") or {},
                "gpu_seconds_used": job.gpu_seconds_used,
            }
        )

    @flask_app.route("/jobs/<job_id>/cancel", methods=["POST"])
    @login_required
    @idempotent()
    def job_cancel(job_id: str):
        """User-initiated cancel of a pending/running job.

        Best-effort Modal cancel, full credit refund, row transitions
        to status='cancelled'. Safe to call repeatedly — terminal jobs
        return an error_code without mutating state.
        """
        ctx = load_user_context()
        if ctx is None:
            return jsonify({"error": "unauthenticated"}), 401
        job, err = cancel_job(
            job_id, user_id=ctx.user_id, modal_client=modal_client
        )
        if job is None:
            code = 404 if err == "not_found" else 409
            return jsonify({"error": err or "cancel_failed"}), code
        return jsonify(
            {
                "id": job.id,
                "status": job.status,
                "credits_refunded": job.credits_cost,
            }
        )

    # ------------------------------------------------------------------
    # Public tool comparison matrix + campaign intake stub
    # ------------------------------------------------------------------

    @flask_app.route("/tools", methods=["GET"])
    def tools_comparison():
        """Public discovery hub for the full tool catalog.

        Renders the iteration-loop framing, a category-grouped tile
        grid, and the comparison matrix at the bottom for power users.
        Catalog includes both hardcoded tools (Epitope Scout, Binder
        Developability Scout, Library Planner) and flag-enabled GPU
        adapters.
        """
        catalog = _build_tools_catalog()

        # Group catalog into workflow-stage sections in a stable order.
        # The order mirrors the iteration loop a scientist walks through.
        category_order = (
            "Scope the target",
            "Design binders",
            "Predict structures",
            "Check developability",
            "Other",
        )
        grouped: list[tuple[str, list[dict]]] = []
        for category in category_order:
            members = [t for t in catalog if t.get("category") == category]
            if members:
                grouped.append((category, members))

        return render_template(
            "tools/comparison.html",
            tools=catalog,
            grouped=grouped,
            authenticated=bool(session.get("user_email")),
        )

    # ------------------------------------------------------------------
    # Export routes — /jobs/<id>/export.{csv,fasta,zip}
    # ------------------------------------------------------------------

    @flask_app.route("/jobs/<job_id>/export.csv", methods=["GET"])
    @login_required
    def export_csv(job_id: str):
        import csv  # noqa: PLC0415
        import io   # noqa: PLC0415
        from flask import Response  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return render_template("404.html"), 404
        candidates = (job.result or {}).get("candidates", [])
        buf = io.StringIO()
        all_score_keys: list[str] = []
        for cand in candidates:
            for k in (cand.get("scores") or {}):
                if k not in all_score_keys:
                    all_score_keys.append(k)
        writer = csv.DictWriter(buf, fieldnames=["rank", "pdb_key"] + all_score_keys,
                                extrasaction="ignore")
        writer.writeheader()
        for i, cand in enumerate(candidates):
            scores = cand.get("scores") or {}
            row = {"rank": cand.get("rank", i + 1), "pdb_key": cand.get("pdb_key", "")}
            row.update(scores)
            writer.writerow(row)
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}_scores.csv"},
        )

    @flask_app.route("/jobs/<job_id>/export.fasta", methods=["GET"])
    @login_required
    def export_fasta(job_id: str):
        from flask import Response  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return render_template("404.html"), 404
        result = job.result or {}
        candidates = result.get("candidates", [])
        mpnn_sequences = result.get("sequences", [])
        lines: list[str] = []
        # Binder-design tools (rfantibody/bindcraft/boltzgen/pxdesign)
        # return ``candidates`` (PDB + docked pose + scores). MPNN is a
        # sequence-design primitive and returns ``sequences`` (seq +
        # score + recovery), so the header+body shape has to differ.
        for i, cand in enumerate(candidates):
            seq = cand.get("sequence") or cand.get("binder_sequence") or ""
            if not seq:
                continue
            pdb_key = cand.get("pdb_key", f"candidate_{i + 1}")
            rank = cand.get("rank", i + 1)
            lines.append(f">rank{rank}_{pdb_key}")
            # wrap at 80 chars
            for start in range(0, len(seq), 80):
                lines.append(seq[start:start + 80])
        for i, seq_obj in enumerate(mpnn_sequences):
            seq = seq_obj.get("seq") or ""
            if not seq:
                continue
            header_parts = [f">mpnn_rank{i + 1}"]
            score = seq_obj.get("score")
            recovery = seq_obj.get("recovery")
            if score is not None:
                header_parts.append(f"score={score}")
            if recovery is not None:
                header_parts.append(f"recovery={recovery}")
            lines.append(" ".join(header_parts))
            for start in range(0, len(seq), 80):
                lines.append(seq[start:start + 80])
        if not lines:
            return Response(
                "# No sequences found in this job's output.\n",
                mimetype="text/plain",
                headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}.fasta"},
            )
        return Response(
            "\n".join(lines) + "\n",
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}.fasta"},
        )

    @flask_app.route("/jobs/<job_id>/af2.pdb", methods=["GET"])
    @login_required
    def af2_download_pdb(job_id: str):
        """Stream the AF2 predicted structure as a .pdb download.

        D2 atomic tool. Result payload carries ``pdb_b64`` (base64-encoded
        PDB text); decode and return as text/plain for browser-friendly
        Save As. Owner-scoped via the get_job RLS wrapper.
        """
        import base64  # noqa: PLC0415
        from flask import Response  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None or job.tool != "af2":
            return render_template("404.html"), 404
        pdb_b64 = (job.result or {}).get("pdb_b64") or ""
        if not pdb_b64:
            return Response(
                "# No PDB in this job's result.\n",
                mimetype="text/plain",
                status=404,
            )
        try:
            pdb_bytes = base64.b64decode(pdb_b64, validate=True)
        except Exception:
            return Response(
                "# Malformed PDB payload.\n",
                mimetype="text/plain",
                status=500,
            )
        return Response(
            pdb_bytes,
            mimetype="chemical/x-pdb",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=af2_{job_id[:8]}.pdb"
                )
            },
        )

    @flask_app.route("/jobs/<job_id>/af2_pae.npy", methods=["GET"])
    @login_required
    def af2_download_pae(job_id: str):
        """Stream the AF2 PAE matrix as a .npy download.

        D2 atomic tool. Result payload carries ``pae_matrix_b64`` which
        is a base64-encoded numpy .npy file (written by run_pipeline.py
        via ``numpy.save``). We hand it back as-is — the client can
        ``numpy.load`` it directly.
        """
        import base64  # noqa: PLC0415
        from flask import Response  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None or job.tool != "af2":
            return render_template("404.html"), 404
        pae_b64 = (job.result or {}).get("pae_matrix_b64") or ""
        if not pae_b64:
            return Response(
                "# No PAE matrix in this job's result.\n",
                mimetype="text/plain",
                status=404,
            )
        try:
            pae_bytes = base64.b64decode(pae_b64, validate=True)
        except Exception:
            return Response(
                "# Malformed PAE payload.\n",
                mimetype="text/plain",
                status=500,
            )
        return Response(
            pae_bytes,
            mimetype="application/octet-stream",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=af2_{job_id[:8]}_pae.npy"
                )
            },
        )

    @flask_app.route("/jobs/<job_id>/export.zip", methods=["GET"])
    @login_required
    def export_zip(job_id: str):
        import base64   # noqa: PLC0415
        import io       # noqa: PLC0415
        import zipfile  # noqa: PLC0415
        from flask import Response  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return render_template("404.html"), 404
        candidates = (job.result or {}).get("candidates", [])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, cand in enumerate(candidates):
                encoded = cand.get("pdb_content_b64")
                if not encoded:
                    continue
                try:
                    data = base64.b64decode(encoded)
                except Exception:
                    continue
                filename = cand.get("pdb_key") or f"candidate_{i + 1}.pdb"
                zf.writestr(filename, data)
        buf.seek(0)
        return Response(
            buf.read(),
            mimetype="application/zip",
            headers={"Content-Disposition": f"attachment; filename=job_{job_id[:8]}_pdbs.zip"},
        )

    # ------------------------------------------------------------------
    # Campaign routes — /campaigns/*
    # ------------------------------------------------------------------

    @flask_app.route("/campaigns/submit", methods=["POST"])
    @login_required
    def campaigns_submit():
        import json  # noqa: PLC0415
        from shared.campaigns import create_campaign  # noqa: PLC0415
        from shared.email import send_campaign_submitted_emails  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))

        source_job_id = request.form.get("source_job_id", "").strip()
        target_name   = request.form.get("target_name", "").strip()
        assay_type    = request.form.get("assay_type", "yeast_display").strip()
        budget_band   = request.form.get("budget_band", "pilot").strip()
        target_context = request.form.get("target_context", "").strip()

        raw_kd = request.form.get("affinity_goal_kd_nm", "").strip()
        affinity_goal_kd_nm = float(raw_kd) if raw_kd else None

        raw_weeks = request.form.get("timeline_weeks", "").strip()
        timeline_weeks = int(raw_weeks) if raw_weeks else None

        raw_indices = request.form.get("candidate_indices", "[]").strip()
        try:
            candidate_indices = [int(i) for i in json.loads(raw_indices)]
        except Exception:
            candidate_indices = []

        if not source_job_id or not target_name or not candidate_indices:
            return redirect(url_for("jobs_list"))

        job = get_job(source_job_id, user_id=ctx.user_id)
        if job is None:
            return redirect(url_for("jobs_list"))

        try:
            campaign = create_campaign(
                user_id=ctx.user_id,
                source_job_id=source_job_id,
                candidate_indices=candidate_indices,
                target_name=target_name,
                target_context=target_context,
                assay_type=assay_type,
                budget_band=budget_band,
                affinity_goal_kd_nm=affinity_goal_kd_nm,
                timeline_weeks=timeline_weeks,
            )
        except ValueError:
            return redirect(url_for("jobs_list"))

        if campaign is None:
            return redirect(url_for("jobs_list"))

        # Copy candidate PDBs into durable campaign bucket.
        candidates = (job.result or {}).get("candidates", [])
        try:
            stage_campaign_candidates(
                campaign_id=campaign.id,
                candidates=candidates,
                indices=candidate_indices,
            )
        except StorageError:
            logger.warning("stage_campaign_candidates failed for %s", campaign.id)

        # Emails — best-effort.
        try:
            send_campaign_submitted_emails(
                campaign=campaign,
                user_email=session.get("user_email", ""),
            )
        except Exception:
            logger.warning("campaign submit emails failed", exc_info=True)

        return redirect(url_for("campaign_detail", campaign_id=campaign.id) + "?submitted=1")

    @flask_app.route("/campaigns", methods=["GET"])
    @login_required
    def campaigns_dashboard():
        from shared.campaigns import list_user_campaigns  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        campaigns = list_user_campaigns(ctx.user_id)
        return render_template("campaigns/dashboard.html", campaigns=campaigns)

    @flask_app.route("/campaigns/<campaign_id>", methods=["GET"])
    @login_required
    def campaign_detail(campaign_id: str):
        from shared.campaigns import get_campaign  # noqa: PLC0415
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        campaign = get_campaign(campaign_id, user_id=ctx.user_id)
        if campaign is None:
            return render_template("404.html"), 404
        submitted_flash = request.args.get("submitted") == "1"
        return render_template(
            "campaigns/detail.html",
            campaign=campaign,
            submitted_flash=submitted_flash,
        )

    # Legacy stub redirect — old results pages linked here.
    @flask_app.route("/campaigns/new", methods=["GET"])
    @login_required
    def campaigns_new_stub():
        from_job = request.args.get("from_job", "")
        if from_job:
            return redirect(url_for("job_detail", job_id=from_job))
        return redirect(url_for("campaigns_dashboard"))

    # ------------------------------------------------------------------
    # Admin routes — /admin/campaigns/*
    # ------------------------------------------------------------------

    @flask_app.route("/admin/campaigns", methods=["GET"])
    def admin_campaigns_list():
        from shared.auth import require_staff, STAFF_EMAILS  # noqa: PLC0415
        from shared.campaigns import list_all_campaigns, STATUSES  # noqa: PLC0415
        email = session.get("user_email", "")
        if not email:
            return redirect(url_for("login", next=request.path))
        if email not in STAFF_EMAILS:
            return render_template("404.html"), 404
        status_filter = request.args.get("status") or None
        campaigns = list_all_campaigns(status=status_filter)
        return render_template(
            "admin/campaigns_list.html",
            campaigns=campaigns,
            statuses=list(STATUSES),
            current_status=status_filter,
        )

    @flask_app.route("/admin/campaigns/<campaign_id>", methods=["GET"])
    def admin_campaign_detail(campaign_id: str):
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.campaigns import get_campaign, STATUSES  # noqa: PLC0415
        email = session.get("user_email", "")
        if not email:
            return redirect(url_for("login", next=request.path))
        if email not in STAFF_EMAILS:
            return render_template("404.html"), 404
        campaign = get_campaign(campaign_id)
        if campaign is None:
            return render_template("404.html"), 404
        flash_msg = request.args.get("updated") == "1" and "Status updated."
        return render_template(
            "admin/campaign_detail.html",
            campaign=campaign,
            statuses=list(STATUSES),
            flash_msg=flash_msg or None,
        )

    @flask_app.route("/admin/campaigns/<campaign_id>/status", methods=["POST"])
    def admin_campaign_update_status(campaign_id: str):
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.campaigns import get_campaign, update_status  # noqa: PLC0415
        from shared.email import send_campaign_status_email  # noqa: PLC0415
        email = session.get("user_email", "")
        if not email:
            return redirect(url_for("login"))
        if email not in STAFF_EMAILS:
            return render_template("404.html"), 404

        campaign = get_campaign(campaign_id)
        if campaign is None:
            return render_template("404.html"), 404

        prev_status     = campaign.status
        new_status      = request.form.get("status", "").strip()
        contact         = request.form.get("ranomics_contact", "").strip() or None
        notes_internal  = request.form.get("notes_internal", "").strip() or None

        try:
            updated = update_status(
                campaign_id,
                status=new_status,
                ranomics_contact=contact,
                notes_internal=notes_internal,
            )
        except ValueError:
            return redirect(url_for("admin_campaign_detail", campaign_id=campaign_id))

        if updated and updated.status != prev_status:
            # Look up submitter email via service client.
            from shared.credits import get_service_client  # noqa: PLC0415
            client = get_service_client()
            user_email_for_notify = None
            if client:
                try:
                    resp = client.auth.admin.get_user_by_id(updated.user_id)
                    user_email_for_notify = getattr(resp.user, "email", None)
                except Exception:
                    pass
            if user_email_for_notify:
                try:
                    send_campaign_status_email(
                        campaign=updated,
                        user_email=user_email_for_notify,
                        prev_status=prev_status,
                    )
                except Exception:
                    logger.warning("campaign status email failed", exc_info=True)

        return redirect(
            url_for("admin_campaign_detail", campaign_id=campaign_id) + "?updated=1"
        )

    # ------------------------------------------------------------------
    # Admin routes — /admin/users/* and /admin/signups/rejected
    # ------------------------------------------------------------------

    @flask_app.route("/admin/users", methods=["GET"])
    def admin_users_list():
        """Per-user activity dashboard: signup quality, runs, last seen.

        Pulls auth.users via service role (50-row first page), joins
        ``public.user_profiles``, ``credits_balance``, and the trailing
        30-day count from ``public.user_events`` + ``public.tool_jobs``.
        Sorts by last-activity DESC so the most engaged users surface
        first.
        """
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.credits import get_service_client  # noqa: PLC0415

        email = session.get("user_email", "")
        if not email:
            return redirect(url_for("login", next=request.path))
        if email not in STAFF_EMAILS:
            return render_template("404.html"), 404

        client = get_service_client()
        users: list[dict] = []
        if client is not None:
            try:
                from datetime import datetime, timedelta, timezone  # noqa: PLC0415
                window_start = (
                    datetime.now(timezone.utc) - timedelta(days=30)
                ).isoformat()

                page = client.auth.admin.list_users()
                auth_users = getattr(page, "users", None) or page

                profile_rows = (
                    client.table("user_profiles").select("*").execute().data or []
                )
                profiles_by_id = {r["user_id"]: r for r in profile_rows}

                balance_rows = (
                    client.table("credits_balance")
                    .select("user_id,balance")
                    .execute()
                    .data
                    or []
                )
                balance_by_id = {r["user_id"]: r.get("balance", 0) for r in balance_rows}

                event_rows = (
                    client.table("user_events")
                    .select("user_id,event_type,created_at")
                    .gte("created_at", window_start)
                    .execute()
                    .data
                    or []
                )
                run_rows = (
                    client.table("tool_jobs")
                    .select("user_id,created_at,status")
                    .gte("created_at", window_start)
                    .execute()
                    .data
                    or []
                )

                from collections import defaultdict  # noqa: PLC0415
                event_count: dict = defaultdict(int)
                last_event: dict = {}
                for r in event_rows:
                    uid = r.get("user_id")
                    if not uid:
                        continue
                    event_count[uid] += 1
                    ts = r.get("created_at") or ""
                    if ts > last_event.get(uid, ""):
                        last_event[uid] = ts

                run_count: dict = defaultdict(int)
                last_run: dict = {}
                for r in run_rows:
                    uid = r.get("user_id")
                    if not uid:
                        continue
                    run_count[uid] += 1
                    ts = r.get("created_at") or ""
                    if ts > last_run.get(uid, ""):
                        last_run[uid] = ts

                for u in auth_users:
                    uid = getattr(u, "id", None) or (u.get("id") if isinstance(u, dict) else None)
                    if not uid:
                        continue
                    user_email = (
                        getattr(u, "email", None)
                        or (u.get("email") if isinstance(u, dict) else None)
                    )
                    created_at = (
                        getattr(u, "created_at", None)
                        or (u.get("created_at") if isinstance(u, dict) else None)
                    )
                    profile = profiles_by_id.get(uid, {})
                    last_activity = max(
                        last_event.get(uid, ""),
                        last_run.get(uid, ""),
                    ) or created_at or ""
                    users.append({
                        "user_id": uid,
                        "email": user_email,
                        "created_at": str(created_at)[:19] if created_at else "",
                        "signup_quality": profile.get("signup_quality") or "legacy",
                        "domain_class": profile.get("domain_class") or "",
                        "purpose": profile.get("purpose"),
                        "balance": balance_by_id.get(uid, 0),
                        "runs_30d": run_count.get(uid, 0),
                        "events_30d": event_count.get(uid, 0),
                        "last_activity": str(last_activity)[:19] if last_activity else "",
                    })
                users.sort(key=lambda u: u.get("last_activity") or "", reverse=True)
            except Exception:
                logger.warning("admin_users_list query failed", exc_info=True)

        return render_template("admin/users_list.html", users=users)

    @flask_app.route("/admin/users/<user_id>", methods=["GET"])
    def admin_user_detail(user_id: str):
        """Per-user activity timeline: events + tool runs + credits."""
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.credits import get_service_client  # noqa: PLC0415

        viewer = session.get("user_email", "")
        if not viewer:
            return redirect(url_for("login", next=request.path))
        if viewer not in STAFF_EMAILS:
            return render_template("404.html"), 404

        client = get_service_client()
        target = {
            "user_id": user_id,
            "email": None,
            "created_at": "",
            "profile": {},
            "balance": 0,
            "timeline": [],
        }
        if client is None:
            return render_template("admin/user_detail.html", target=target)

        try:
            user_resp = client.auth.admin.get_user_by_id(user_id)
            user_obj = getattr(user_resp, "user", None)
            if user_obj is not None:
                target["email"] = getattr(user_obj, "email", None)
                target["created_at"] = (
                    str(getattr(user_obj, "created_at", "") or "")[:19]
                )
        except Exception:
            logger.warning("get_user_by_id failed for %s", user_id, exc_info=True)

        try:
            prof = (
                client.table("user_profiles")
                .select("*")
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
            target["profile"] = getattr(prof, "data", None) or {}
        except Exception:
            target["profile"] = {}

        try:
            bal = (
                client.table("credits_balance")
                .select("balance")
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
            data = getattr(bal, "data", None) or {}
            target["balance"] = int(data.get("balance") or 0)
        except Exception:
            target["balance"] = 0

        # Build a unified timeline by interleaving three sources.
        timeline: list[dict] = []
        try:
            events = (
                client.table("user_events")
                .select("event_type,path,props,created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(200)
                .execute()
                .data
                or []
            )
            for e in events:
                timeline.append({
                    "kind": "event",
                    "label": e.get("event_type"),
                    "detail": e.get("path") or "",
                    "props": e.get("props") or {},
                    "created_at": e.get("created_at"),
                })
        except Exception:
            logger.warning("user_events query failed", exc_info=True)
        try:
            runs = (
                client.table("tool_jobs")
                .select("id,tool,preset,status,credits_cost,created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
                .data
                or []
            )
            for r in runs:
                timeline.append({
                    "kind": "run",
                    "label": f"{r.get('tool')} run · {r.get('status')}",
                    "detail": f"preset={r.get('preset')} · {r.get('credits_cost')} credits",
                    "job_id": r.get("id"),
                    "created_at": r.get("created_at"),
                })
        except Exception:
            logger.warning("tool_jobs query failed", exc_info=True)
        try:
            ledger = (
                client.table("credits_ledger")
                .select("kind,delta,reason,tool,created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
                .data
                or []
            )
            for l in ledger:
                timeline.append({
                    "kind": "credit",
                    "label": f"{l.get('kind')} ({l.get('delta')})",
                    "detail": l.get("reason") or "",
                    "tool": l.get("tool"),
                    "created_at": l.get("created_at"),
                })
        except Exception:
            logger.warning("credits_ledger query failed", exc_info=True)

        timeline.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        target["timeline"] = timeline
        return render_template("admin/user_detail.html", target=target)

    @flask_app.route("/admin/signups/rejected", methods=["GET"])
    def admin_signups_rejected():
        """Last 30 days of /signup rejections, grouped by reason."""
        from shared.auth import STAFF_EMAILS  # noqa: PLC0415
        from shared.credits import get_service_client  # noqa: PLC0415

        viewer = session.get("user_email", "")
        if not viewer:
            return redirect(url_for("login", next=request.path))
        if viewer not in STAFF_EMAILS:
            return render_template("404.html"), 404

        from collections import defaultdict  # noqa: PLC0415
        from datetime import datetime, timedelta, timezone  # noqa: PLC0415

        rows: list[dict] = []
        client = get_service_client()
        if client is not None:
            try:
                window_start = (
                    datetime.now(timezone.utc) - timedelta(days=30)
                ).isoformat()
                rows = (
                    client.table("signup_rejections")
                    .select("*")
                    .gte("created_at", window_start)
                    .order("created_at", desc=True)
                    .limit(500)
                    .execute()
                    .data
                    or []
                )
            except Exception:
                logger.warning(
                    "admin_signups_rejected query failed", exc_info=True
                )

        grouped: dict = defaultdict(list)
        for r in rows:
            grouped[r.get("reason") or "unknown"].append(r)
        groups = sorted(
            (
                {
                    "reason": reason,
                    "count": len(entries),
                    "entries": entries[:25],
                }
                for reason, entries in grouped.items()
            ),
            key=lambda g: g["count"],
            reverse=True,
        )

        return render_template(
            "admin/signups_rejected.html",
            groups=groups,
            total=len(rows),
        )

    @flask_app.errorhandler(404)
    def not_found(_):
        """Render the branded 404 page for unknown routes."""
        return render_template("404.html"), 404

    # ------------------------------------------------------------------
    # CLI commands — invoked by Railway cron or local `flask` runner
    # ------------------------------------------------------------------

    @flask_app.cli.command("digest:send")
    def cli_digest_send():
        """Build + send the daily digest to STAFF_NOTIFY_EMAIL.

        Usage::

            flask digest:send

        Override the trailing window with DIGEST_WINDOW_HOURS (default 24).
        """
        from cron.daily_digest import send_digest  # noqa: PLC0415

        with flask_app.app_context():
            ok = send_digest()
        click_msg = "sent" if ok else "failed (see logs)"
        # Use stdout so Railway cron logs show the outcome line.
        print(f"digest:send {click_msg}", flush=True)

    return flask_app


# ---------------------------------------------------------------------------
# Logging configuration — runs before create_app() so all loggers output
# to gunicorn's stderr in production.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Module-level app instance required for `gunicorn app:app`.
app = create_app()


if __name__ == "__main__":
    # Local dev entry point. Production uses gunicorn via Procfile.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True)
