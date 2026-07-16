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
    /library-planner      — Yeast Display Library Planner (form)
    /library-planner/plan — Yeast Display Library Planner (results)

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

import click
from decimal import Decimal, InvalidOperation
from functools import wraps
import json

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_compress import Compress
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix

from gpu.modal_client import ModalClient
from shared.credits import (
    load_user_context,
    recent_ledger,
)
from shared.wallet import (
    MIN_TOPUP_USD,
    REASON_INSUFFICIENT,
    REASON_OK,
    REASON_PER_TOOL_CAP,
    REASON_SELF_SERVE_CEILING,
    REASON_WALLET_FROZEN,
    SELF_SERVE_CEILING_USD,
    get_or_create_wallet,
    release_hold as wallet_release_hold,
    reserve_hold as wallet_reserve_hold,
    wallet_preflight,
    _round_up_topup_amount,
)
from shared.pdb_intake import (
    _fetch_alphafold_bytes,
    _parse_preflight_size_params,
    _verdict_to_json,
    _verify_reuse_pdb_bytes,
)
from shared.tools_catalog import _build_tools_catalog, _short_name_for_label
from shared.wallet_guard import requires_wallet
from shared.wallet_estimates import (
    compute_hard_cap,
    estimated_cost_for_tool,
)
from shared.feature_flags import tool_enabled
from shared.idempotency import idempotent
from shared.handoffs import get_handoff, mark_consumed
from shared.jobs import (
    cancel_job,
    complete_job,
    create_job,
    get_job,
    list_campaign_labels_for_user,
    list_jobs_for_user,
    list_jobs_paginated,
    mark_failed,
    mark_running,
    set_modal_call,
    update_inputs,
)
from shared.metrics import register_metrics

from shared.pdb_inspect import (
    CifConversionError,
    convert_cif_to_pdb_bytes,
    hotspot_range_message,
    inspect_pdb_bytes,
    summarize_for_log,
    validate_hotspots,
    validate_target_chain,
)
from shared.pdb_preflight import (
    PREFLIGHT_TOOLS,
    PreflightVerdict,
    VerdictKind,
    preflight_for_tool,
)

from shared.storage import (
    StorageError,
    copy_input,
    download_input,
    download_output,
    output_exists,
    presigned_input_url,
    stage_campaign_candidates,
    upload_input,
)
from shared import category_glyphs as _category_glyphs
from shared import metric_glossary as _metric_glossary
from shared import resample as _resample
from shared import score_legends as _score_legends
from tools import base as tool_base
import tools.af2         # noqa: F401 — import to register adapter (D2 atomic)
import tools.bindcraft   # noqa: F401 — import to register adapter
import tools.boltz2      # noqa: F401 — import to register adapter (Boltz-2 cofold)
import tools.boltzgen    # noqa: F401 — import to register adapter
import tools.colabfold   # noqa: F401 — import to register adapter (D3 atomic)
import tools.esmfold     # noqa: F401 — import to register adapter (D4 atomic)
import tools.esmfold2_design  # noqa: F401 — import to register adapter (ESMFold2-design)
import tools.iggm        # noqa: F401 — import to register adapter (IgGM antibody design)
import tools.mpnn        # noqa: F401 — import to register adapter (D1 atomic)
import tools.pxdesign    # noqa: F401 — import to register adapter
import tools.rfantibody  # noqa: F401 — import to register adapter
import tools.rfdiffusion # noqa: F401 — import to register adapter
from scout import scout_bp
from blueprints.public import public_bp
from blueprints.jobs import jobs_bp
from blueprints.campaigns import campaigns_bp
from blueprints.lab_projects import lab_projects_bp
from blueprints.admin import admin_bp
from blueprints.auth import auth_bp
from blueprints.wallet import wallet_bp
from blueprints.tools import tools_bp
from webhooks.modal import register_modal_webhooks
from webhooks.stripe import register_stripe_webhook
from webhooks.uploads import register_upload_urls

logger = logging.getLogger(__name__)


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
        "SESSION_SECRET_KEY", os.urandom(32).hex()
    )

    # FIX M2 (cso audit 2026-06-17): harden the Flask session cookie
    # UNCONDITIONALLY. This cookie authenticates the entire web UI
    # (wallet, account, admin) — not just the platform API. These flags
    # were previously set only inside the ENABLE_PLATFORM_API block, so
    # with that flag off (the prod default) every authenticated POST
    # surface ran with Flask defaults: no Secure flag and SameSite unset
    # (browser-default Lax). HttpOnly already defaults True in Flask but
    # is pinned here for intent. Set them here so the hardening is
    # independent of any feature flag.
    #
    # SameSite=Lax (not Strict): Strict drops the session cookie on the
    # first cross-site top-level navigation, which breaks the post-login
    # ``?next=`` redirect (e.g. following an emailed link to a protected
    # page). Lax still blocks cross-site POST/AJAX, which is the CSRF
    # vector that matters here. The /account/api-keys POSTs additionally
    # carry a per-session CSRF token.
    flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
    flask_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # Secure ONLY when actually served over HTTPS. A Secure cookie is
    # never sent back over plain http, so forcing it on unconditionally
    # silently breaks local dev login (127.0.0.1 over http). Neither
    # flask_app.debug (still False here — app.run(debug=True) sets it
    # AFTER create_app under `python app.py`, and prod uses gunicorn) nor
    # a defaulted PUBLIC_BASE_URL read can tell local from prod. Gate on a
    # positive HTTPS signal instead: an explicit https PUBLIC_BASE_URL, or
    # Railway's injected RAILWAY_ENVIRONMENT (present in prod, absent
    # locally). Local dev hits neither and stays Secure=False.
    _public_base = os.environ.get("PUBLIC_BASE_URL", "").strip().lower()
    _serves_https = _public_base.startswith("https://") or bool(
        os.environ.get("RAILWAY_ENVIRONMENT", "").strip()
    )
    flask_app.config["SESSION_COOKIE_SECURE"] = _serves_https

    # ------------------------------------------------------------------
    # FIX M2 (cso audit 2026-06-17, second half): app-wide CSRF protection
    # for the cookie-authenticated web UI.
    #
    # SameSite=Lax (above) blocks the cross-site form/AJAX POST vector in
    # modern browsers, but a per-session CSRF token is the defence-in-depth
    # layer that also covers same-site XSS-leveraged forgeries and older
    # browsers. The token previously existed ONLY for /account/api-keys/*
    # (inside the ENABLE_PLATFORM_API block); every other authenticated POST
    # surface (wallet, account, admin, jobs, tools, campaigns, workspaces)
    # had none. This hoists a single token + a request-time enforcer to the
    # whole app, independent of any feature flag.
    #
    # Model: double-submit via the signed session cookie. A random token is
    # minted into the session on first render (the csrf_input()/
    # csrf_meta_value() Jinja globals) and required back on every state-
    # changing request, either as the ``_csrf`` form field or the
    # ``X-CSRF-Token`` header (for fetch/XHR).
    import hmac as _hmac  # noqa: PLC0415
    import secrets as _secrets  # noqa: PLC0415

    _CSRF_SESSION_KEY = "_csrf_token"
    _CSRF_PROTECT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def _ensure_app_csrf_token() -> str:
        """Return the session CSRF token, minting + persisting one if absent."""
        token = session.get(_CSRF_SESSION_KEY)
        if not isinstance(token, str) or not token:
            token = _secrets.token_urlsafe(32)
            session[_CSRF_SESSION_KEY] = token
        return token

    # Jinja helpers. csrf_input() drops a hidden field into a <form>;
    # csrf_meta_value() feeds the <meta name="csrf-token"> tag that fetch/
    # XHR callers read. Named to avoid colliding with the ``csrf_token``
    # *context variable* that the /account/api-keys templates already pass.
    flask_app.jinja_env.globals["csrf_input"] = lambda: Markup(
        '<input type="hidden" name="_csrf" value="'
        + _ensure_app_csrf_token()
        + '">'
    )
    flask_app.jinja_env.globals["csrf_meta_value"] = _ensure_app_csrf_token

    def _csrf_request_is_exempt() -> bool:
        """True for requests that must NOT be subject to the web-UI CSRF check."""
        # Only these two blueprints self-manage CSRF posture: scout_bp (free
        # tier, owned separately) and platform_api_bp (/api/v1/*, bearer-token
        # auth — not cookie-driven, so structurally CSRF-immune). This is an
        # ALLOWLIST, not a blanket "any blueprint" exemption: as the web UI
        # (login, wallet, tools, jobs, admin) moves into cookie-authenticated
        # blueprints, those state-changing POSTs MUST stay CSRF-enforced, so a
        # newly added blueprint is protected by default unless listed here.
        if request.blueprint in {"scout", "platform_api"}:
            return True
        path = request.path
        # Server-to-server ingress — verified by per-message token/HMAC, no
        # session cookie in play: Modal callbacks, heartbeat, Stripe, and the
        # Modal-facing upload-URL minter.
        if path.startswith("/webhooks/") or path.startswith("/api/upload-urls/"):
            return True
        # Anonymous analytics beacon (navigator.sendBeacon cannot set headers).
        if path == "/api/track":
            return True
        # Side-effect-free input validation: no DB write, no billing, no job.
        if path.startswith("/tools/") and path.endswith("/preflight"):
            return True
        # /account/api-keys/* already enforce their own per-session CSRF token
        # (FIX HI-03) using a distinct field; leave that working path intact.
        if path.startswith("/account/api-keys"):
            return True
        return False

    @flask_app.before_request
    def _enforce_csrf():  # noqa: ANN202
        if request.method not in _CSRF_PROTECT_METHODS:
            return None
        # Operational kill-switch (default ON). Read at request time so an
        # operator can disable enforcement without a redeploy if this blanket
        # control unexpectedly breaks a form in prod. The test suite sets
        # CSRF_PROTECT=0 globally (tests/conftest.py) and the dedicated CSRF
        # suite flips it back on per-test.
        if os.environ.get("CSRF_PROTECT", "1").strip() == "0":
            return None
        # No matched route → let Flask raise its own 404/405 instead of 403.
        if request.endpoint is None:
            return None
        if _csrf_request_is_exempt():
            return None
        expected = session.get(_CSRF_SESSION_KEY)
        submitted = (
            request.form.get("_csrf")
            or request.headers.get("X-CSRF-Token")
            or ""
        )
        if (
            not expected
            or not submitted
            or not _hmac.compare_digest(str(expected), str(submitted))
        ):
            logger.warning(
                "CSRF validation failed: path=%s endpoint=%s "
                "session_token=%s submitted=%s",
                request.path,
                request.endpoint,
                bool(expected),
                bool(submitted),
            )
            abort(403, description="CSRF token missing or invalid.")
        return None

    # GPU label sync: best-effort refresh of TOOL_RULES.gpu from Modal-side
    # metadata. Today this is a stub that always falls back to the hardcoded
    # values; the hook exists so a future Modal API query or vendored
    # gpu_manifest.json can plug in without touching the create_app flow.
    # Wrapped in try/except so a Modal outage cannot stop tools-hub from
    # booting. See shared/modal_gpu_metadata.py for the extension paths.
    try:
        from shared.modal_gpu_metadata import (  # noqa: PLC0415
            sync_tool_rules_gpu_labels,
        )
        sync_tool_rules_gpu_labels()
    except Exception:
        logger.warning(
            "modal_gpu_sync raised at startup; continuing with hardcoded "
            "TOOL_RULES.gpu values.",
            exc_info=True,
        )

    # Metric glossary available in all templates (candidate_table macro reads it).
    flask_app.jinja_env.globals["metric_glossary"] = _metric_glossary.GLOSSARY

    # Per-tool score legends. The candidate_table macro calls
    # ``score_legends_for(tool_slug)`` to render per-column "what counts
    # as good?" tooltips. Returns a {column_key: legend} dict.
    flask_app.jinja_env.globals["score_legends_for"] = (
        _score_legends.score_legends_for
    )

    # Map workflow-stage category labels to SVG glyph slugs. The
    # homepage tile grid and ``/tools`` discovery page render the
    # returned slug into ``static/img/categories/<slug>.svg`` so each
    # category section gets a scannable visual marker.
    flask_app.jinja_env.globals["category_glyph"] = (
        _category_glyphs.category_glyph_slug
    )
    flask_app.jinja_env.globals["inline_category_glyph"] = (
        _category_glyphs.inline_category_glyph
    )

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
        from shared.credits import get_service_client  # noqa: PLC0415
        from shared.workspaces import active_workspaces_count  # noqa: PLC0415

        email = session.get("user_email") or ""
        base = {
            "now": datetime.now(timezone.utc),
            "active_workspaces_count": 0,
            "ranomics_user_id": None,
            "is_staff": email in STAFF_EMAILS,
            "nav_wallet_usd": None,
            "support_email": (
                os.environ.get("SUPPORT_EMAIL", "info@ranomics.com").strip()
                or "info@ranomics.com"
            ),
            # Analytics keys for templates/base.html. Empty in dev/staging
            # so the snippets render no-ops unless the env vars are set.
            "posthog_key": os.environ.get("POSTHOG_KEY", "").strip(),
            "posthog_host": os.environ.get(
                "POSTHOG_HOST", "https://us.i.posthog.com"
            ).strip(),
            "ga4_measurement_id": os.environ.get(
                "GA4_MEASUREMENT_ID", ""
            ).strip(),
            # Per-page canonical URL override. Routes that need a non-default
            # canonical (e.g. a paginated index canonicalized to page 1) can
            # pass canonical_url=... in render_template kwargs.
            "canonical_url": None,
        }
        if not email:
            return base
        ctx = load_user_context()
        if ctx is None:
            return base
        base["active_workspaces_count"] = active_workspaces_count(ctx.user_id)
        base["ranomics_user_id"] = ctx.user_id

        # Wallet balance for the navbar chip. Best-effort: a Supabase
        # hiccup must not break header rendering, so we swallow failures
        # and leave the chip absent.
        try:
            client = get_service_client()
            if client is not None:
                resp = (
                    client.table("user_wallets")
                    .select("balance_usd")
                    .eq("user_id", ctx.user_id)
                    .maybe_single()
                    .execute()
                )
                row = getattr(resp, "data", None) or {}
                if row.get("balance_usd") is not None:
                    base["nav_wallet_usd"] = float(row["balance_usd"])
        except Exception:
            logger.debug(
                "nav wallet read failed for %s", ctx.user_id, exc_info=True
            )

        # Onboarding ribbon (C9): show the welcome strip to first-run users.
        # Conditions: signed in + wallet credit > 0 + zero tool_jobs rows.
        # Cheap check, runs once for fresh users and stops as soon as they
        # submit their first job (the ribbon hides server-side).
        base["show_onboarding_ribbon"] = False
        try:
            if (
                base.get("nav_wallet_usd") is not None
                and base["nav_wallet_usd"] > 0
            ):
                client = get_service_client()
                if client is not None:
                    resp = (
                        client.table("tool_jobs")
                        .select("id")
                        .eq("user_id", ctx.user_id)
                        .limit(1)
                        .execute()
                    )
                    rows = getattr(resp, "data", None) or []
                    if not rows:
                        base["show_onboarding_ribbon"] = True
        except Exception:
            logger.debug(
                "onboarding ribbon check failed for %s",
                ctx.user_id,
                exc_info=True,
            )

        return base

    # Stripe webhook — mounted at /webhooks/stripe. Signature verification
    # + event_id idempotency live inside webhooks/stripe.py.
    register_stripe_webhook(flask_app)

    # Prometheus /metrics (IP-allowlisted) + /healthz readiness probe.
    # The existing /health liveness probe below stays as a dumb 200.
    register_metrics(flask_app)

    # Modal pipeline callbacks — /webhooks/modal/<job_id>/<token> + /webhooks/heartbeat.
    register_modal_webhooks(flask_app)

    # Modal-facing upload-URL minter — /api/upload-urls/<job_id>/<token>.
    # Pipelines call this to obtain presigned PUT URLs for candidate PDBs,
    # which they then write into the tool-outputs Storage bucket directly.
    register_upload_urls(flask_app)

    # Scout (free tier) blueprint — everything under /scout.
    from pathlib import Path as _Path  # noqa: PLC0415
    _Path("tmp").mkdir(exist_ok=True)
    flask_app.config.setdefault("MAX_CONTENT_LENGTH", 20 * 1024 * 1024)
    flask_app.register_blueprint(scout_bp)
    flask_app.register_blueprint(public_bp)
    flask_app.register_blueprint(jobs_bp)
    flask_app.register_blueprint(campaigns_bp)
    flask_app.register_blueprint(lab_projects_bp)
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(wallet_bp)
    flask_app.register_blueprint(tools_bp)

    # ------------------------------------------------------------------
    # Platform API — wet-lab as an API for binder-design agents.
    #
    # Gated behind ENABLE_PLATFORM_API=1 so the entire /api/v1/* surface
    # plus /account/api-keys plus /.well-known/ai-plugin.json return 404
    # in environments where the alpha is not yet live. Toggle on the
    # Railway env var to flip live; remove and restart to remove cleanly.
    # ------------------------------------------------------------------
    if os.environ.get("ENABLE_PLATFORM_API", "").strip() == "1":
        # FIX #7 (validation finding) — refuse to boot if SESSION_SECRET_KEY
        # is unset when the API is enabled. The plaintext-key flow in
        # /account/api-keys/create can't rely on auto-rotating per-process
        # keys: a Railway redeploy would invalidate every in-flight session
        # cookie, silently losing the one-shot reveal banner. WEBHOOK_SIGNING_
        # SECRET is verified inside shared.webhooks, but the operator should
        # set it at the same time as ENABLE_PLATFORM_API or transitions will
        # log noisy errors on first webhook dispatch.
        if not (os.environ.get("SESSION_SECRET_KEY") or "").strip():
            raise RuntimeError(
                "ENABLE_PLATFORM_API=1 requires SESSION_SECRET_KEY to be set "
                "in the process env (the plaintext API-key reveal depends on "
                "stable session signing across redeploys)."
            )
        if not (os.environ.get("WEBHOOK_SIGNING_SECRET") or "").strip():
            logger.warning(
                "ENABLE_PLATFORM_API=1 but WEBHOOK_SIGNING_SECRET is unset; "
                "webhook delivery will fail closed until it is configured."
            )

        # Session-cookie hardening (HttpOnly / SameSite / Secure) now runs
        # UNCONDITIONALLY in create_app above (FIX M2), so it no longer
        # depends on this flag. The cross-site forgery vector for the
        # /account/api-keys/* POSTs is additionally covered by the
        # per-session CSRF token enforced in those handlers below; that is
        # what makes SameSite=Lax (rather than Strict) safe here while
        # keeping the post-login ``?next=`` redirect working.

        from tools.platform_api import (  # noqa: PLC0415
            platform_account_bp,
            platform_api_bp,
        )

        flask_app.register_blueprint(platform_api_bp)
        # The AI-plugin manifest + /account/api-keys management surface
        # (Commit 8). Separate blueprint because platform_api_bp is
        # url_prefix="/api/v1"; these serve absolute paths. Registered here
        # so it exists ONLY under ENABLE_PLATFORM_API=1, same as before.
        flask_app.register_blueprint(platform_account_bp)

        # Surface the flag to Jinja so templates (e.g. account.html) can
        # conditionally show the "Platform API → API Keys" entry point.
        # Without this, the /account/api-keys page exists but is
        # invisible — users had no way to discover it from the in-app nav.
        flask_app.jinja_env.globals["platform_api_enabled"] = True

        # CR-02 (fresh-review): start the webhook-retry sweep.
        # The in-thread sleep model used to lose every retry on a Railway
        # redeploy (the worker dies mid-sleep). The sweep replaces that:
        # ``next_retry_at`` becomes the source of truth, and a 60s tick
        # picks up any due rows. Gated behind WEBHOOK_SWEEP_ENABLED in
        # case an operator needs to disable it (the kill switch on top
        # of the ENABLE_PLATFORM_API kill switch).
        if os.environ.get("WEBHOOK_SWEEP_ENABLED", "1").strip() == "1":
            try:
                from datetime import (  # noqa: PLC0415
                    datetime,
                    timedelta,
                    timezone,
                )

                from apscheduler.schedulers.background import (  # noqa: PLC0415
                    BackgroundScheduler,
                )
                from shared.webhooks import sweep_due_deliveries  # noqa: PLC0415

                sweep_interval = int(
                    os.environ.get("WEBHOOK_SWEEP_INTERVAL_SECONDS", "60")
                )
                if sweep_interval < 10:
                    sweep_interval = 10  # floor; below this we DoS the DB
                _webhook_scheduler = BackgroundScheduler(
                    timezone="UTC",
                    daemon=True,
                    job_defaults={
                        "coalesce": True,  # drop missed ticks, don't pile up
                        "max_instances": 1,  # one sweep at a time per replica
                    },
                )
                _webhook_scheduler.add_job(
                    sweep_due_deliveries,
                    trigger="interval",
                    seconds=sweep_interval,
                    id="webhook-sweep",
                    next_run_time=datetime.now(timezone.utc)
                    + timedelta(seconds=sweep_interval),
                )
                _webhook_scheduler.start()
                logger.info(
                    "Webhook sweep started (interval=%ds)",
                    sweep_interval,
                )
            except Exception:
                logger.error(
                    "Webhook sweep failed to start; "
                    "deliveries that backpressure or fail their first attempt "
                    "will not be retried until a manual sweep runs.",
                    exc_info=True,
                )

        logger.info(
            "Platform API enabled (/api/v1/*, /.well-known/ai-plugin.json, "
            "/account/api-keys)"
        )

    # Single Modal client shared across stub tool routes.
    modal_client = ModalClient()
    flask_app.modal_client = modal_client

    # ------------------------------------------------------------------
    # Protected routes
    # ------------------------------------------------------------------

    # Register the IndexNow verification file route only when the env
    # var is set. IndexNow requires a key.txt file at the site root
    # whose body is the same key sent in the submission payload.
    _indexnow_key = os.environ.get("INDEXNOW_KEY", "").strip()
    if _indexnow_key:
        @flask_app.route(f"/{_indexnow_key}.txt", methods=["GET"])
        def indexnow_key_file():
            """Serve the IndexNow ownership-verification key as plain text."""
            return Response(_indexnow_key, mimetype="text/plain")

    # ------------------------------------------------------------------
    @flask_app.errorhandler(404)
    def not_found(_):
        """Render the branded 404 page for unknown routes."""
        return render_template("404.html"), 404

    @flask_app.errorhandler(500)
    def server_error(_):
        """Render the branded 500 page for unhandled exceptions."""
        return render_template("500.html"), 500

    @flask_app.errorhandler(413)
    def request_too_large(_):
        """Friendly handling for an over-cap upload (MAX_CONTENT_LENGTH).

        The 413 is raised by Werkzeug during multipart parsing, before a
        route body runs, so the results-attach form cannot catch it itself.
        Routing has already happened, so request.endpoint / view_args are
        available: send the operator back to the campaign with the same
        '?results_error=1' path every other results failure uses, instead of
        a raw error page. All other routes get a clean plain-text 413.
        """
        view_args = request.view_args or {}
        if (
            request.endpoint == "admin.admin_campaign_save_results"
            and view_args.get("campaign_id")
        ):
            return redirect(
                url_for(
                    "admin.admin_campaign_detail",
                    campaign_id=view_args["campaign_id"],
                )
                + "?results_error=1"
            )
        return ("Upload too large. The request limit is 20 MB.", 413)

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

    @flask_app.cli.command("reengagement:send")
    def cli_reengagement_send():
        """Sweep for unused-credit users and send the 7-day re-engagement email.

        Usage::

            flask reengagement:send

        No-ops cleanly when no user qualifies.
        """
        from cron.reengagement import send_reengagement  # noqa: PLC0415

        with flask_app.app_context():
            summary = send_reengagement()
        print(
            f"reengagement:send qualified={summary['qualified']} "
            f"sent={summary['sent']} "
            f"skipped_no_suggestions={summary['skipped_no_suggestions']} "
            f"errors={summary['errors']}",
            flush=True,
        )

    @flask_app.cli.command("jobs:sweep-stuck")
    def cli_sweep_stuck():
        """Terminalise stuck pending/running jobs and release their holds.

        Usage::

            flask jobs:sweep-stuck

        Override the age thresholds with STUCK_PENDING_AGE_MINUTES
        (default 30) and STUCK_RUNNING_AGE_HOURS (default 6).
        """
        from cron.sweep_stuck_jobs import sweep_stuck_jobs  # noqa: PLC0415

        with flask_app.app_context():
            summary = sweep_stuck_jobs()
        print(
            f"jobs:sweep-stuck pending={summary['pending_swept']} "
            f"running={summary['running_swept']} "
            f"recovered={summary.get('recovered', 0)} "
            f"errors={len(summary['errors'])}",
            flush=True,
        )
        for err in summary["errors"]:
            print(f"  err: {err}", flush=True)

    @flask_app.cli.command("campaigns:tick")
    def cli_campaigns_tick():
        """Re-drive in-flight compute campaigns (dispatch + reconcile + finalize).

        Backstop for the inline drive hook. Usage::

            flask campaigns:tick

        Scheduled via Railway cron (~60-90s).
        """
        from cron.tick_campaigns import tick_campaigns  # noqa: PLC0415

        with flask_app.app_context():
            summary = tick_campaigns()
        print(
            f"campaigns:tick driven={summary['driven']} "
            f"errors={len(summary['errors'])}",
            flush=True,
        )
        for err in summary["errors"]:
            print(f"  err: {err}", flush=True)

    @flask_app.cli.command("pii:purge-old")
    @click.option(
        "--dry-run", is_flag=True, default=False,
        help="Count matching rows without deleting anything.",
    )
    def cli_pii_purge_old(dry_run: bool):
        """Purge PII event rows older than the retention window (cso L5).

        Deletes public.user_events + public.signup_rejections rows past
        PII_RETENTION_DAYS (default 365, floored at 30). Usage::

            flask pii:purge-old
            flask pii:purge-old --dry-run
        """
        from cron.purge_old_events import purge_old_events  # noqa: PLC0415

        with flask_app.app_context():
            summary = purge_old_events(dry_run=dry_run)
        verb = "would purge" if dry_run else "purged"
        print(
            f"pii:purge-old ({'dry-run' if dry_run else 'live'}) "
            f"cutoff<{summary['cutoff']} retention_days={summary['retention_days']} "
            f"{verb}: user_events={summary['user_events']} "
            f"signup_rejections={summary['signup_rejections']} "
            f"errors={len(summary['errors'])}",
            flush=True,
        )
        for err in summary["errors"]:
            print(f"  err: {err}", flush=True)

    @flask_app.cli.command("indexnow:ping")
    def cli_indexnow_ping():
        """Submit the hub's high-value URLs to IndexNow.

        Usage::

            flask indexnow:ping

        No-ops if INDEXNOW_KEY is unset.
        """
        from cron.indexnow_ping import ping_high_value_urls  # noqa: PLC0415

        with flask_app.app_context():
            result = ping_high_value_urls()
        print(
            f"indexnow:ping status={result['status']} "
            f"submitted={result['submitted']} message={result['message']}",
            flush=True,
        )

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
