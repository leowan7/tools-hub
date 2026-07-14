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
from typing import Optional

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

# Tools whose form template renders the rich preflight panel (the JS
# verdict UI). All PDB-input design tools now carry the panel, so this set
# covers every slug in PREFLIGHT_TOOLS (enforced by
# test_every_preflight_tool_has_a_panel). It stays a separate constant
# because it guards a different thing (panel markup present in the template)
# than PREFLIGHT_TOOLS (an evaluator exists). The plain ``error`` string
# fallback in tool_submit is kept as a defensive net.
_PREFLIGHT_PANEL_FORMS: frozenset = frozenset(
    {"rfantibody", "rfdiffusion", "bindcraft", "boltzgen", "pxdesign", "boltz2"}
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

        from tools.platform_api import platform_api_bp  # noqa: PLC0415

        flask_app.register_blueprint(platform_api_bp)

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

        @flask_app.route("/.well-known/ai-plugin.json", methods=["GET"])
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

        # --- /account/api-keys management page ---
        from shared.api_keys import (  # noqa: PLC0415
            VALID_ROLES,
            list_keys,
            mint_token,
            revoke_key,
        )
        from shared.auth import login_required  # noqa: PLC0415

        # FIX HI-03 (fresh-review): per-session CSRF token for the
        # /account/api-keys/* POST handlers. The session cookie is now
        # SameSite=Lax (set unconditionally in create_app — FIX M2), which
        # blocks the cross-site form/AJAX POST vector browser-side; this
        # token additionally guards against same-site XSS-leveraged
        # forgeries and is what lets us use Lax rather than Strict here
        # without exposing this surface. Stored in session as a hex string;
        # rotated when the cookie rotates (login/logout/secret change).
        import hmac as _hmac  # noqa: PLC0415
        import secrets as _secrets  # noqa: PLC0415

        _CSRF_SESSION_KEY = "_platform_api_csrf"

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

        @flask_app.route("/account/api-keys", methods=["GET"])
        @login_required
        def account_api_keys():
            user_ctx = load_user_context()
            if user_ctx is None:
                return redirect(url_for("auth.login"))
            return _render_api_keys_page(user_ctx.user_id)

        @flask_app.route("/account/api-keys/create", methods=["POST"])
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

        @flask_app.route(
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

        @flask_app.route(
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
            return redirect(url_for("account_api_keys"))

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

    from shared.auth import login_required  # noqa: PLC0415

    # Register the IndexNow verification file route only when the env
    # var is set. IndexNow requires a key.txt file at the site root
    # whose body is the same key sent in the submission payload.
    _indexnow_key = os.environ.get("INDEXNOW_KEY", "").strip()
    if _indexnow_key:
        @flask_app.route(f"/{_indexnow_key}.txt", methods=["GET"])
        def indexnow_key_file():
            """Serve the IndexNow ownership-verification key as plain text."""
            return Response(_indexnow_key, mimetype="text/plain")

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

    # ------------------------------------------------------------------
    # B2 — public preview page helpers. Used by the logged-out branch
    # of /tools/<slug> and only there.
    # ------------------------------------------------------------------

    # SEO phrase pairs per tool slug. ``seo_phrase`` is a short natural
    # phrase reused in the page title and lede; ``seo_long`` is a longer
    # phrase used once in body copy. Pulled into one map so the shared
    # preview shell stays free of per-tool branching.
    _PREVIEW_SEO_PHRASES: dict[str, tuple[str, str]] = {
        "mpnn": (
            "free online ProteinMPNN tool",
            "Run ProteinMPNN sequence design on a backbone PDB with no "
            "install and no local GPU"
        ),
        "af2": (
            "AlphaFold2 multimer without a local GPU",
            "Fold complexes through your browser with full MSA and "
            "templates, results land at /jobs"
        ),
        "colabfold": (
            "ColabFold online without Colab",
            "Fast no-MSA folds in 1 to 2 minutes per run, no MMseqs2 "
            "round-trip on your laptop"
        ),
        "esmfold": (
            "ESMFold online single-sequence fold",
            "Fastest monomer fold from the ESM-2 language model with no "
            "MSA, no multimer, no install"
        ),
        "bindcraft": (
            "BindCraft de novo binder design no install",
            "Hallucinate 60 to 150 residue protein binders against a "
            "target PDB on a dedicated GPU"
        ),
        "rfantibody": (
            "RFantibody nanobody design online",
            "Generate VHH scaffolds against a target PDB without setting "
            "up RoseTTAFold or Rosetta locally"
        ),
        "rfdiffusion": (
            "RFdiffusion de novo binder design online",
            "Run RFdiffusion plus AF2 multimer scoring through your "
            "browser without an A100"
        ),
        "boltzgen": (
            "BoltzGen multi-modality binder design online",
            "Design mini-proteins, nanobodies, antibodies, or peptides "
            "against the same target with glycan and PTM support"
        ),
        "boltz2": (
            "Boltz-2 cofold validation online",
            "Validate a designed binder against your antigen with "
            "single-sequence cofold and interface confidence"
        ),
        "pxdesign": (
            "PXDesign AF2-IG binder design online",
            "AF2-initial-guess binder generation with real ipTM, pLDDT, "
            "and pAE on every candidate"
        ),
    }

    def _preview_seo_phrases(slug: str) -> tuple[str, str]:
        """Return (short, long) SEO phrase pair for a tool slug.

        Falls back to a generic pair so newly registered tools still get
        sensible copy without an explicit entry in the map.
        """
        return _PREVIEW_SEO_PHRASES.get(
            slug,
            (
                f"free {slug} tool online",
                "Run it through your browser on a dedicated GPU with no "
                "install"
            ),
        )

    # Title-only phrases. Kept separate from ``_PREVIEW_SEO_PHRASES`` so the
    # body lede stays grammatical ("X is a <seo_phrase> you can run") while
    # the <title> stays under the 65-char SERP cap.
    _PREVIEW_TITLE_PHRASES: dict[str, str] = {
        "mpnn": "Free Sequence Design",
        "af2": "AF2 Multimer No-Install",
        "colabfold": "No Colab Required",
        "esmfold": "Single-Sequence Folding",
        "bindcraft": "De Novo Binder Design",
        "rfantibody": "Nanobody Design",
        "rfdiffusion": "De Novo Binder Design",
        "boltzgen": "Multi-Modal Binder Design",
        "boltz2": "Cofold Validation",
        "pxdesign": "AF2-IG Binder Design",
        "esmfold2-design": "scFv CDR Design",
    }

    def _preview_title_phrase(slug: str) -> str:
        return _PREVIEW_TITLE_PHRASES.get(slug, "GPU-Backed Protein Design")

    # Map tools-hub slug -> ranomics.com /technology/<slug> page slug.
    # Used to emit a cross-site "Learn how X works" link on each public
    # preview so the two co-owned sites reinforce each other for
    # algorithm-name search intent.
    _RANOMICS_TECHNOLOGY_SLUGS: dict[str, str] = {
        "mpnn": "proteinmpnn",
        "af2": "alphafold2",
        "colabfold": "colabfold",
        "esmfold": "esmfold",
        "rfdiffusion": "rfdiffusion",
        "rfantibody": "rfantibody",
        "bindcraft": "bindcraft",
        "boltzgen": "boltzgen",
        "pxdesign": "pxdesign",
    }

    # 2-3 related tools per slug, ordered by closest sibling first.
    # Powers an internal-linking "Related tools" block on each preview
    # page so a searcher comparing algorithms gets surfaced the next
    # logical option from the same workflow stage.
    _RELATED_TOOLS: dict[str, tuple[str, ...]] = {
        "rfdiffusion": ("bindcraft", "pxdesign", "boltzgen"),
        "bindcraft":   ("rfdiffusion", "boltzgen", "pxdesign"),
        "pxdesign":    ("rfdiffusion", "bindcraft", "boltzgen"),
        "boltzgen":    ("rfdiffusion", "rfantibody", "bindcraft"),
        "rfantibody":  ("boltzgen", "rfdiffusion", "bindcraft"),
        "mpnn":        ("af2", "colabfold", "esmfold"),
        "af2":         ("colabfold", "esmfold", "mpnn"),
        "colabfold":   ("af2", "esmfold", "mpnn"),
        "esmfold":     ("af2", "colabfold", "mpnn"),
        "boltz2":      ("af2", "colabfold", "boltzgen"),
    }

    def _related_tool_cards(slug: str) -> list[dict]:
        """Build the related-tools card list for the preview page.

        Each card carries slug, short_name, one-line description, and
        the tool_form URL so the template stays declarative.
        """
        import importlib  # noqa: PLC0415
        out: list[dict] = []
        for related_slug in _RELATED_TOOLS.get(slug, ()):
            related_adapter = tool_base.get(related_slug)
            if related_adapter is None or not tool_enabled(related_slug):
                continue
            blurb = related_adapter.blurb or ""
            try:
                rmeta = importlib.import_module(f"tools.{related_slug}.meta")
                one_liner = getattr(rmeta, "comparison_one_liner", None)
                if one_liner:
                    blurb = one_liner
            except ImportError:
                pass
            out.append({
                "slug": related_slug,
                "short_name": _short_name_for_label(related_adapter.label),
                "blurb": blurb,
                "url": url_for("tool_form", tool=related_slug),
            })
        return out

    def _runtime_band_for_adapter(adapter, meta) -> str:
        """Compute the same runtime band string used on the homepage cards.

        Mirrors the inline logic in :func:`_build_tools_catalog` so the
        preview page reports the same band as the homepage. Falls back
        to '—' when the adapter has no PRESET_RUNTIME entries.
        """
        if meta is None:
            return "—"
        runtime_map = getattr(meta, "PRESET_RUNTIME", None) or {}
        legacy_rows = getattr(meta, "preset_runtime_rows", None) or ()
        legacy_by_slug = {
            r.get("slug"): r.get("runtime")
            for r in legacy_rows
            if r.get("slug") and r.get("runtime")
        }
        runtimes: list[str] = []
        for preset in adapter.presets:
            entry = runtime_map.get(preset.slug) or {}
            if entry.get("typical_minutes"):
                rt = f"{entry['typical_minutes']} min"
            else:
                rt = legacy_by_slug.get(preset.slug)
            if rt and rt not in runtimes:
                runtimes.append(rt)
        if len(runtimes) >= 2:
            return f"{runtimes[0]} to {runtimes[-1]}"
        if len(runtimes) == 1:
            return runtimes[0]
        return "—"

    def _template_exists(template_name: str) -> bool:
        """True if Jinja can resolve ``template_name`` via the loader."""
        try:
            flask_app.jinja_env.get_template(template_name)
            return True
        except Exception:
            return False

    @flask_app.route("/tools/<tool>", methods=["GET"])
    def tool_form(tool: str):
        """Render a GPU tool's submission form, or a public preview if logged out.

        Logged-out branch (B2): render ``tools/<slug>_preview.html`` if
        present, falling back to the shared ``tools/_preview.html`` shell.
        The preview is indexable; the underlying run form is not. The
        POST handler at /tools/<slug>/submit stays @login_required so
        logged-out visitors cannot spawn jobs.

        Logged-in pre-fill sources (query params, owner-scoped):
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

        # Logged-out: render the public preview shell. Per-tool override
        # at templates/tools/<slug>_preview.html wins if present;
        # otherwise fall through to the shared shell. The shared shell
        # extends base.html and renders About + score legend + paper +
        # "Sign in to run" CTA.
        if not session.get("user_email"):
            import importlib  # noqa: PLC0415
            preview_meta = None
            try:
                preview_meta = importlib.import_module(
                    f"tools.{adapter.slug}.meta"
                )
            except ImportError:
                pass
            runtime_band = _runtime_band_for_adapter(adapter, preview_meta)
            seo_phrase, seo_long = _preview_seo_phrases(adapter.slug)
            title_phrase = _preview_title_phrase(adapter.slug)
            login_next = url_for("tool_form", tool=adapter.slug)
            per_tool_template = f"tools/{adapter.slug}_preview.html"
            template_name = per_tool_template if _template_exists(
                per_tool_template
            ) else "tools/_preview.html"
            short_name = _short_name_for_label(adapter.label)
            tech_slug = _RANOMICS_TECHNOLOGY_SLUGS.get(adapter.slug)
            learn_more_url = (
                f"https://www.ranomics.com/technology/{tech_slug}"
                if tech_slug else None
            )
            breadcrumbs = [
                {"name": "Home", "url": url_for("public.index", _external=True)},
                {"name": "Tools", "url": url_for(
                    "tools_comparison", _external=True
                )},
                {"name": short_name, "url": url_for(
                    "tool_form", tool=adapter.slug, _external=True
                )},
            ]
            return render_template(
                template_name,
                adapter=adapter,
                meta=preview_meta,
                runtime_band=runtime_band,
                login_next=login_next,
                seo_phrase=seo_phrase,
                seo_long=seo_long,
                title_phrase=title_phrase,
                short_name=short_name,
                learn_more_url=learn_more_url,
                related_tools=_related_tool_cards(adapter.slug),
                breadcrumbs=breadcrumbs,
            )

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("auth.login"))

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

        # AF2-resample chain: when the user lands on the MPNN form via a
        # "Resample with MPNN" button on an AF2 / ColabFold / ESMFold
        # result page, prefill the MPNN form with the source job's
        # predicted PDB and sensible diversification defaults
        # (sampling_temp=0.5, num_seq_per_target=16). The PDB itself is
        # not staged here — that happens at submit time when the
        # ``resample:<job_id>`` token is resolved (the submit-side
        # branch decodes pdb_b64 from the source job's result and
        # uploads it like a fresh PDB).
        resample_from = request.args.get("resample_from", "").strip()
        if (
            resample_from
            and adapter.slug == _resample.RESAMPLE_DESTINATION
            and not pre_fill
        ):
            src = get_job(resample_from, user_id=ctx.user_id)
            if (
                src is not None
                and _resample.can_resample(src.tool)
                and src.status == "succeeded"
                and ((src.result or {}).get("pdb_b64") or "").strip()
            ):
                for k, v in _resample.RESAMPLE_MPNN_DEFAULTS.items():
                    pre_fill[k] = v
                pdb_source = {
                    "label": (
                        f"Predicted PDB from {src.tool} job {src.id[:8]}"
                    ),
                    "filename": (
                        f"predicted-{src.tool}-{src.id[:8]}.pdb"
                    ),
                    "token": f"resample:{src.id}",
                }
                from shared.events import EVENTS, emit  # noqa: PLC0415
                emit(
                    EVENTS.RESAMPLE_LOADED,
                    user_id=ctx.user_id,
                    properties={
                        "source_tool": src.tool,
                        "source_job_id": src.id,
                    },
                )

        # The wallet estimate partial reads balance_usd for first paint
        # so the form lights up with the user's real balance even before
        # the /api/wallet/estimate call returns. Falls back to 0 if the
        # service client is misconfigured.
        wallet_for_form = get_or_create_wallet(ctx.user_id) or {}

        from shared import compute_campaigns as cc  # noqa: PLC0415
        # D1: single-container design ceiling. When a campaign-supported tool's
        # requested design count exceeds this, the form re-points the submit to
        # the campaign chunker (client-side) and the tool_submit backstop rejects
        # a doomed single job. None for tools without a campaign path.
        campaign_ceiling = (
            cc.single_container_ceiling(adapter.slug)
            if adapter.slug in cc.SUPPORTED_TOOLS
            else None
        )
        return render_template(
            adapter.form_template,
            adapter=adapter,
            error=None,
            pre_fill=pre_fill,
            pdb_source=pdb_source,
            workspace_ctx=workspace_ctx,
            wallet=wallet_for_form,
            single_container_ceiling=campaign_ceiling,
        )

    @flask_app.route("/tools/<tool>/preflight", methods=["POST"])
    @login_required
    def tool_preflight(tool: str):
        """Run the per-tool PDB preflight and return a JSON verdict.

        Fired by ``static/js/preflight.js`` when the user attaches a PDB
        (or clicks "Use AlphaFold model instead"). No wallet hold, no
        job row, no Modal call — this is purely a "would this work?"
        check. The same logic re-runs at submit time as the actual gate.

        Accepts EITHER:
          - ``target_pdb`` file upload (multipart) + form fields, OR
          - ``alphafold_accession`` form field with a UniProt id like
            ``P25779`` (we fetch the AF model and run preflight on it).

        Returns JSON; see ``_verdict_to_json`` for the shape.
        """
        adapter, err = _require_tool(tool)
        if err:
            return ({"error": "Unknown tool"}, 404)
        if adapter.slug not in PREFLIGHT_TOOLS:
            return ({
                "kind": "ready", "ok": True,
                "tool_slug": adapter.slug,
                "cleanup_items": [], "hotspots": {"surviving": [], "dropped": []},
                "alphafold": None,
            }, 200)

        target_chain = (request.form.get("target_chain") or "A").strip()
        raw_hotspots = (request.form.get("hotspot_residues") or "").strip()
        hotspots: list = []
        if raw_hotspots:
            for tok in raw_hotspots.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    hotspots.append(int(tok))
                except ValueError:
                    # Non-integer hotspot entries are surfaced through the
                    # form validator on submit; for preflight purposes we
                    # ignore them so the panel renders something useful.
                    pass

        # Source the bytes: file upload OR AlphaFold fetch.
        af_accession = (request.form.get("alphafold_accession") or "").strip()
        uploaded = request.files.get("target_pdb")
        pdb_bytes: Optional[bytes] = None
        source_label: str = ""

        if af_accession:
            fetched = _fetch_alphafold_bytes(af_accession)
            if fetched is None:
                return ({
                    "kind": "needs_fix", "ok": False,
                    "tool_slug": adapter.slug,
                    "reason": (
                        f"Couldn't fetch AlphaFold model for {af_accession}. "
                        f"The AlphaFold-DB may not have this UniProt entry."
                    ),
                    "suggested_fix": (
                        "Pick a different target or upload a cleaned PDB manually."
                    ),
                    "cleanup_items": [],
                    "hotspots": {"surviving": [], "dropped": []},
                    "alphafold": None,
                }, 200)
            pdb_bytes = fetched
            source_label = f"AF-{af_accession}"
        elif uploaded and uploaded.filename:
            pdb_bytes = uploaded.read()
            # If the upload is CIF, convert before preflight (the
            # downstream pipeline_normalize assumes PDB-or-CIF, but the
            # normalizer's extension routing keys off filename; safer to
            # convert here once so the preflight matches the submit-side
            # cleanup pass exactly).
            fname_lower = (uploaded.filename or "").lower()
            if fname_lower.endswith((".cif", ".mmcif")):
                try:
                    pdb_bytes = convert_cif_to_pdb_bytes(pdb_bytes, uploaded.filename)
                except CifConversionError as exc:
                    return ({
                        "kind": "needs_fix", "ok": False,
                        "tool_slug": adapter.slug,
                        "reason": str(exc),
                        "suggested_fix": (
                            "Save the structure as PDB format and re-upload."
                        ),
                        "cleanup_items": [],
                        "hotspots": {"surviving": [], "dropped": []},
                        "alphafold": None,
                    }, 200)
            source_label = uploaded.filename
        else:
            return ({
                "kind": "needs_fix", "ok": False,
                "tool_slug": adapter.slug,
                "reason": "No PDB uploaded.",
                "suggested_fix": "Attach a target PDB above.",
                "cleanup_items": [],
                "hotspots": {"surviving": [], "dropped": []},
                "alphafold": None,
            }, 200)

        # Cheap inspection first — catches "this isn't a PDB at all".
        inspection = inspect_pdb_bytes(pdb_bytes, filename=source_label)
        if not inspection.ok:
            return ({
                "kind": "needs_fix", "ok": False,
                "tool_slug": adapter.slug,
                "reason": inspection.error or "Couldn't parse upload as PDB.",
                "suggested_fix": (
                    "Confirm the file is a PDB or mmCIF protein structure."
                ),
                "cleanup_items": [],
                "hotspots": {"surviving": [], "dropped": []},
                "alphafold": None,
            }, 200)

        binder_max_aa, num_designs = _parse_preflight_size_params(request.form)
        verdict = preflight_for_tool(
            adapter.slug, pdb_bytes,
            target_chain=target_chain, hotspots=hotspots,
            binder_max_aa=binder_max_aa, num_designs=num_designs,
        )
        return (_verdict_to_json(verdict, source_label), 200)

    @flask_app.route("/tools/<tool>/submit", methods=["POST"])
    @login_required
    @idempotent()
    @requires_wallet
    def tool_submit(tool: str):
        """Validate, place a wallet hold, upload PDB, spawn Modal, redirect to job detail."""
        adapter, err = _require_tool(tool)
        if err:
            return err

        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("auth.login"))

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

        # D1 backstop: a count above one container's worth for a
        # campaign-supported tool must not run as a doomed single job. The
        # form re-points such submits to the campaign chunker client-side; this
        # catches the JS-off / reuse-token path. Returning here (before
        # create_job) leaves g.wallet_hold_consumed False, so requires_wallet
        # auto-releases the hold — no money-path change. boltzgen has no
        # num_designs key (its budget maxes at one chunk), so it is skipped.
        from shared import compute_campaigns as cc  # noqa: PLC0415
        if tool in cc.SUPPORTED_TOOLS:
            requested_n = inputs.get("num_designs")
            ceiling = cc.single_container_ceiling(tool)
            if isinstance(requested_n, int) and requested_n > ceiling:
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=(
                        f"{requested_n} designs is more than one GPU container "
                        f"runs for {tool} (max {ceiling} per single job). "
                        f"Large requests run as a campaign: open /campaigns/new "
                        f"to fan this out across GPUs with no per-job ceiling. "
                        f"Your wallet was not charged."
                    ),
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                    single_container_ceiling=ceiling,
                )

        # Workspace gate (when context present). Rejects expired,
        # refunded, or cap-exhausted workspaces BEFORE the job row is
        # written, BEFORE PDB upload, BEFORE the Modal call. Submissions
        # without workspace context are gated by the wallet alone — the
        # requires_wallet decorator placed a hold before this handler ran.
        if workspace_ctx is not None:
            from shared.workspaces import workspace_preflight  # noqa: PLC0415
            preflight = workspace_preflight(
                ctx.user_id, workspace_ctx["target_pdb_id"]
            )
            if not preflight.allow:
                if preflight.reason == "no_workspace":
                    return redirect(url_for("wallet.workspaces_new"))
                # cap_exceeded / expired: send the user to the workspace
                # detail so the cap meter + upgrade CTA explain why.
                return redirect(
                    url_for(
                        "wallet.workspace_detail",
                        workspace_id=workspace_ctx["workspace_id"],
                    )
                )
            # Sanity-check: the user's active workspace for this target
            # may differ from the one the form claims (e.g. if they
            # bought a second workspace mid-session). Trust the form ID
            # for charge attribution; preflight already confirmed an
            # active workspace exists for this user+target.
            workspace_ctx["workspace_id"] = preflight.workspace.id

        # Per-preset PDB requirement: paid presets need an upload, smoke
        # and preview do not. Falls back to the adapter-level flag for
        # tools that require a PDB on every paid run (e.g. BindCraft).
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
            or reuse_token.startswith("resample:")
            or reuse_token.startswith("alphafold:")
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
        # spinning up Modal. Reuse-token paths are not
        # re-inspected (the source job's PDB has already passed this gate).
        # Bytes are read here ONCE; we pass them through to upload_input
        # below so we don't need to seek the file pointer back.
        pdb_bytes: bytes | None = None
        inspection = None
        converted_filename: str | None = None
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
                # boltz2 hotspots are 1-indexed SEQUENCE positions, not
                # original PDB numbering; they are range-checked against the
                # antigen length in boltz2's own preflight, so skip the
                # original-numbering check here (it would false-reject an
                # antigen whose numbering does not start at 1).
                if hotspots and adapter.slug != "boltz2":
                    in_range, out_of_range = validate_hotspots(
                        inspection, target_chain, hotspots,
                    )
                    if out_of_range:
                        return render_template(
                            adapter.form_template, adapter=adapter,
                            error=hotspot_range_message(
                                inspection, target_chain, out_of_range,
                            ),
                            pre_fill=inputs, pdb_source=None,
                            workspace_ctx=workspace_ctx,
                        )

            # ---- CIF -> PDB conversion (fleet-wide fix for
            # MPNN/RFdiff/BindCraft/RFantibody) ----
            # ProteinMPNN's parser and the rfdiffusion / bindcraft /
            # rfantibody docker pipelines are PDB-column-strict and
            # crash on CIF text (ValueError on byte-slice float
            # conversion). Convert here, before storage upload, so
            # Modal workers always see real PDB content. Pxdesign and
            # Boltzgen accept PDB just as well as CIF, so this is
            # universally safe across the tool set.
            fname_lower = uploaded.filename.lower()
            if fname_lower.endswith(".cif") or fname_lower.endswith(".mmcif"):
                try:
                    pdb_bytes = convert_cif_to_pdb_bytes(
                        pdb_bytes, uploaded.filename,
                    )
                except CifConversionError as exc:
                    return render_template(
                        adapter.form_template, adapter=adapter,
                        error=str(exc), pre_fill=inputs, pdb_source=None,
                        workspace_ctx=workspace_ctx,
                    )
                converted_filename = (
                    uploaded.filename.rsplit(".", 1)[0] + ".pdb"
                )
                logger.info(
                    "cif_convert %s/%s: %s -> %s (%d bytes)",
                    adapter.slug, preset.slug,
                    uploaded.filename, converted_filename, len(pdb_bytes),
                )
            else:
                converted_filename = uploaded.filename

        # ---- AlphaFold reuse_token: fetch the AF model + use as PDB ----
        # When the user clicked "Use AlphaFold model instead" in the
        # preflight panel, the form replaces the file upload with
        # reuse_pdb_token="alphafold:<accession>". Fetch the model now,
        # treat the bytes as the upload for the rest of the submit path,
        # and let the hard-gate preflight below decide if the hotspots
        # still resolve on the AF model.
        af_accession_for_reuse: str | None = None
        if reuse_token.startswith("alphafold:"):
            af_accession_for_reuse = reuse_token.split(":", 1)[1].strip()
            af_bytes = _fetch_alphafold_bytes(af_accession_for_reuse)
            if af_bytes is None:
                if hold_tx_id_from_g := getattr(g, "wallet_hold_tx_id", None):
                    try:
                        wallet_release_hold(
                            hold_tx_id_from_g, reason="alphafold_fetch_failed",
                        )
                    except Exception:
                        logger.warning(
                            "tool_submit: release_hold after AF fetch fail "
                            "raised for hold=%s", hold_tx_id_from_g,
                            exc_info=True,
                        )
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=(
                        f"Couldn't fetch AlphaFold model AF-{af_accession_for_reuse}. "
                        f"Try uploading a target PDB directly."
                    ),
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )
            pdb_bytes = af_bytes
            converted_filename = f"AF-{af_accession_for_reuse}.pdb"

        # ---- Hard-gate preflight (the rfantibody / hcruz fix) ----
        # For binder design tools, re-run the per-tool normalizer in
        # dry-run mode against the bytes we're about to ship to Modal
        # and BLOCK the submit on NEEDS_FIX. The exact same logic powers
        # the /tools/<tool>/preflight AJAX endpoint that drives the panel
        # above the Run button, so the user has already seen this verdict
        # before clicking. The gate here is the safety net for direct-POST
        # / curl / form-resubmit-without-JS paths.
        if (
            adapter.slug in PREFLIGHT_TOOLS
            and pdb_bytes is not None
        ):
            preflight_target_chain = (inputs.get("target_chain") or "").strip()
            preflight_hotspots = inputs.get("hotspot_residues") or []
            preflight_binder_max, preflight_num_designs = (
                _parse_preflight_size_params(inputs)
            )
            try:
                preflight_verdict = preflight_for_tool(
                    adapter.slug, pdb_bytes,
                    target_chain=preflight_target_chain,
                    hotspots=preflight_hotspots,
                    binder_max_aa=preflight_binder_max,
                    num_designs=preflight_num_designs,
                )
            except Exception:
                # Defensive: a preflight crash must not block submit on
                # otherwise-valid uploads. Log and let the existing
                # server-side normalizer in the Modal pipeline handle it.
                logger.exception("preflight unexpected error tool=%s",
                                 adapter.slug)
                preflight_verdict = None
            if preflight_verdict is not None and not preflight_verdict.ok:
                if hold_for_release := getattr(g, "wallet_hold_tx_id", None):
                    try:
                        wallet_release_hold(
                            hold_for_release, reason="preflight_failed",
                        )
                    except Exception:
                        logger.warning(
                            "tool_submit: release_hold on preflight "
                            "failure raised for hold=%s",
                            hold_for_release, exc_info=True,
                        )
                source_label = converted_filename or (
                    uploaded.filename if uploaded is not None else None
                ) or ""
                if adapter.slug not in _PREFLIGHT_PANEL_FORMS:
                    # pxdesign / boltz2 forms have no rich panel — surface a
                    # plain actionable message so the rejection is visible.
                    plain = (
                        preflight_verdict.reason
                        or "This target can't run as-is."
                    )
                    if preflight_verdict.suggested_fix:
                        plain = f"{plain} {preflight_verdict.suggested_fix}"
                    return render_template(
                        adapter.form_template,
                        adapter=adapter,
                        error=plain,
                        pre_fill=inputs,
                        pdb_source=None,
                        workspace_ctx=workspace_ctx,
                    )
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=None,
                    preflight_verdict=_verdict_to_json(
                        preflight_verdict, source_label,
                    ),
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )
            # Verdict is OK — stash the JSON shape on inputs._preflight so
            # the /jobs/<id> page can replay the same panel ("we cleaned X,
            # Y, Z"; "swapped in AlphaFold AF-Pxxxxxx"). Persists with the
            # job row so the panel survives a page refresh and shows up on
            # completed jobs too — useful for "what did the cleanup change
            # before this design pool was generated?" provenance later.
            if preflight_verdict is not None and preflight_verdict.ok:
                ok_source_label = converted_filename or (
                    uploaded.filename if uploaded is not None else None
                ) or (
                    f"AF-{af_accession_for_reuse}.pdb"
                    if af_accession_for_reuse else ""
                )
                inputs = dict(inputs)
                inputs["_preflight"] = _verdict_to_json(
                    preflight_verdict, ok_source_label,
                )
                # Record explicitly when the user actually accepted the AF
                # swap (vs the verdict merely surfacing the suggestion).
                if af_accession_for_reuse:
                    inputs["_preflight"]["used_alphafold"] = True
                    inputs["_preflight"]["alphafold_accession_used"] = \
                        af_accession_for_reuse

        # Create the tool_jobs row so we have job_id + job_token for the
        # Modal payload and a persistent handle even if Modal submit
        # raises. Workspace IDs (when present) are stashed in inputs._workspace
        # so the completion-side ``charge_for_job`` (item #6) bills the
        # right cap.
        ws_target = workspace_ctx["target_pdb_id"] if workspace_ctx else None
        ws_id = workspace_ctx["workspace_id"] if workspace_ctx else None
        # Stash the wallet hold id (from the requires_wallet decorator)
        # on the job's inputs so the settle path in shared.jobs can
        # close it out on completion. None when the estimate was zero
        # (smoke runs); the settle hook short circuits in that case.
        hold_tx_id = getattr(g, "wallet_hold_tx_id", None)
        wallet_estimate = getattr(g, "wallet_estimate_usd", None)
        if hold_tx_id or wallet_estimate is not None:
            inputs = dict(inputs)
            wallet_ctx = dict(inputs.get("_wallet") or {})
            if hold_tx_id:
                wallet_ctx["hold_tx_id"] = hold_tx_id
            if wallet_estimate is not None:
                wallet_ctx["estimate_usd"] = str(wallet_estimate)
            wallet_ctx["tool_slug"] = adapter.slug
            inputs["_wallet"] = wallet_ctx

        # C4 — free-form campaign label. Trimmed + length-capped in
        # create_job so power users running 50 variations of one target
        # see them grouped on /jobs instead of 50 flat rows.
        form_campaign_label = (request.form.get("campaign_label") or "").strip()

        job = create_job(
            user_id=ctx.user_id,
            tool=adapter.slug,
            preset=preset.slug,
            inputs=inputs,
            target_pdb_id=ws_target,
            workspace_id=ws_id,
            campaign_label=form_campaign_label or None,
        )
        if job is None:
            # Release the hold so we don't leave a stranded reservation.
            if hold_tx_id:
                try:
                    wallet_release_hold(
                        hold_tx_id, reason="job_create_failed"
                    )
                except Exception:
                    logger.warning(
                        "tool_submit: release_hold after create_job "
                        "failure raised for hold=%s",
                        hold_tx_id, exc_info=True,
                    )
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=(
                    "Could not create job record. Supabase is unreachable. "
                    "Try again in a moment."
                ),
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        # create_job succeeded and the hold_tx_id is now stashed on
        # inputs._wallet. shared.jobs._settle_wallet_hold_for_completed_job
        # owns the hold lifecycle from here on. Tell the requires_wallet
        # decorator not to fire its auto-release: if the storage upload
        # or the Modal submit fails below, those paths release the hold
        # explicitly (release_hold is idempotent, so a follow-up
        # auto-release would no-op, but mark it consumed anyway for
        # clarity).
        g.wallet_hold_consumed = True

        presigned_url = ""
        staged_path = ""
        staged_filename = ""
        # Bytes resolved in-memory by a reuse token (resample:),
        # captured so the reuse verification below need not re-download them.
        reuse_resolved_bytes: bytes | None = None
        if needs_pdb:
            try:
                if uploaded is not None and uploaded.filename:
                    # converted_filename is the original name with a .pdb
                    # extension after CIF conversion (set above), or the
                    # original .pdb filename unchanged. Storage + Modal
                    # always see .pdb because pdb_bytes is always PDB by
                    # the time we get here.
                    staged_filename = converted_filename or uploaded.filename
                    # pdb_bytes was read (and possibly converted) during
                    # pre-flight; reuse instead of double-reading.
                    file_data = pdb_bytes if pdb_bytes is not None else uploaded.read()
                    staged_path = upload_input(
                        user_id=ctx.user_id,
                        job_id=job.id,
                        filename=staged_filename,
                        data=file_data,
                        content_type="chemical/x-pdb",
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
                elif reuse_token.startswith("alphafold:"):
                    # AlphaFold fallback: pdb_bytes was already populated by
                    # the AF fetch above the preflight gate (so the gate
                    # could vote on the actual model). Stage those bytes as
                    # if the user had uploaded them.
                    if pdb_bytes is None:
                        raise StorageError("alphafold fetch produced no bytes")
                    staged_filename = (
                        converted_filename or f"AF-{af_accession_for_reuse}.pdb"
                    )
                    staged_path = upload_input(
                        user_id=ctx.user_id,
                        job_id=job.id,
                        filename=staged_filename,
                        data=pdb_bytes,
                        content_type="chemical/x-pdb",
                    )
                elif reuse_token.startswith("resample:"):
                    # AF2-resample chain: decode the source fold job's
                    # predicted PDB (stored as base64 in
                    # ``result.pdb_b64`` across AF2/ColabFold/ESMFold)
                    # and stage it as a fresh MPNN input PDB. The
                    # source-tool gate prevents stuffing a non-fold
                    # job id into the token to read its result blob.
                    import base64  # noqa: PLC0415
                    src_job_id = reuse_token.split(":", 1)[1]
                    src = get_job(src_job_id, user_id=ctx.user_id)
                    if src is None:
                        raise StorageError("source fold job not found")
                    if not _resample.can_resample(src.tool):
                        raise StorageError(
                            "source job is not a fold predictor"
                        )
                    src_pdb_b64 = (
                        (src.result or {}).get("pdb_b64") or ""
                    ).strip()
                    if not src_pdb_b64:
                        raise StorageError(
                            "source job has no predicted PDB"
                        )
                    try:
                        src_pdb_bytes = base64.b64decode(
                            src_pdb_b64, validate=True
                        )
                    except Exception as exc:
                        raise StorageError(
                            f"predicted PDB decode failed: {exc}"
                        )
                    reuse_resolved_bytes = src_pdb_bytes
                    staged_filename = (
                        f"predicted-{src.tool}-{src.id[:8]}.pdb"
                    )
                    staged_path = upload_input(
                        user_id=ctx.user_id,
                        job_id=job.id,
                        filename=staged_filename,
                        data=src_pdb_bytes,
                        content_type="chemical/x-pdb",
                    )

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
                if hold_tx_id:
                    try:
                        wallet_release_hold(
                            hold_tx_id, reason="storage_failure"
                        )
                    except Exception:
                        logger.warning(
                            "tool_submit: release_hold on storage error "
                            "raised for hold=%s",
                            hold_tx_id, exc_info=True,
                        )
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error=f"Upload failed: {exc}",
                    pre_fill=inputs,
                    pdb_source=None,
                    workspace_ctx=workspace_ctx,
                )

        # ---- Reuse-token inspection + hard-gate (gap 2) ----
        # Fresh uploads are inspected + gated at the boundary above, but the
        # reuse tokens (job:/handoff:/resample:) stage bytes that
        # skipped both. Re-check the RESOLVED bytes here before any Modal
        # call so a mismatch (wrong chain, oversized, corrupt predicted PDB
        # piped into MPNN) is flagged upfront. alphafold: already populated
        # pdb_bytes and ran the hard-gate above, so it is excluded.
        if (
            needs_pdb
            and pdb_bytes is None
            and reuse_token
            and not reuse_token.startswith("alphafold:")
            and staged_path
        ):
            check_bytes = reuse_resolved_bytes
            if check_bytes is None:
                # job: / handoff: copied storage-to-storage; read the staged
                # object back to verify it. Best-effort: a verification-only
                # download hiccup must not block an already-staged reuse.
                try:
                    check_bytes = download_input(staged_path)
                except StorageError:
                    logger.warning(
                        "tool_submit: could not download staged reuse PDB "
                        "for verification job=%s path=%s",
                        job.id, staged_path, exc_info=True,
                    )
                    check_bytes = None
            if check_bytes is not None:
                reuse_binder_max, reuse_num_designs = (
                    _parse_preflight_size_params(inputs)
                )
                reuse_err = _verify_reuse_pdb_bytes(
                    adapter, check_bytes,
                    target_chain=(inputs.get("target_chain") or "").strip(),
                    hotspots=inputs.get("hotspot_residues") or [],
                    filename=staged_filename or "input.pdb",
                    binder_max_aa=reuse_binder_max,
                    num_designs=reuse_num_designs,
                )
                if reuse_err:
                    mark_failed(
                        job.id,
                        error={"bucket": "preflight", "detail": reuse_err},
                    )
                    if hold_tx_id:
                        try:
                            wallet_release_hold(
                                hold_tx_id, reason="reuse_preflight_failed",
                            )
                        except Exception:
                            logger.warning(
                                "tool_submit: release_hold on reuse preflight "
                                "failure raised for hold=%s",
                                hold_tx_id, exc_info=True,
                            )
                    return render_template(
                        adapter.form_template,
                        adapter=adapter,
                        error=reuse_err,
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
        upload_urls_endpoint = url_for(
            "upload_urls",
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
                    "_upload_urls_endpoint": upload_urls_endpoint,
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
            if hold_tx_id:
                try:
                    wallet_release_hold(
                        hold_tx_id, reason="modal_submit_failure"
                    )
                except Exception:
                    logger.warning(
                        "tool_submit: release_hold on modal submit "
                        "failure raised for hold=%s",
                        hold_tx_id, exc_info=True,
                    )
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=(
                    "Could not submit to the GPU pool. Your wallet was "
                    "not charged. Try again or contact support at "
                    + (
                        os.environ.get("SUPPORT_EMAIL", "info@ranomics.com").strip()
                        or "info@ranomics.com"
                    )
                    + "."
                ),
                pre_fill=inputs,
                pdb_source=None,
                workspace_ctx=workspace_ctx,
            )

        set_modal_call(job.id, submit_result["function_call_id"])

        # D3 funnel fire. Distinguish the user's first-ever submission
        # from their nth so the dashboard can read activation rate
        # directly. list_jobs_paginated returns a count that already
        # includes the row we just created, so total == 1 -> first.
        # Best-effort: a count failure must not stall the redirect.
        try:
            _, total_jobs = list_jobs_paginated(
                ctx.user_id, page=1, page_size=1,
            )
            is_first = total_jobs == 1
        except Exception:
            is_first = False
        from shared.events import EVENTS, emit  # noqa: PLC0415
        emit(
            EVENTS.FIRST_JOB_SUBMITTED if is_first
            else EVENTS.NTH_JOB_SUBMITTED,
            user_id=ctx.user_id,
            properties={
                "tool": adapter.slug,
                "preset": preset.slug,
                "is_pilot": preset.slug == "pilot",
                "job_id": job.id,
            },
        )

        return redirect(url_for("jobs.job_detail", job_id=job.id))

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
        # The order mirrors the iteration loop a scientist walks through:
        # scope → design (4 scaffold-class buckets) → predict → QC.
        category_order = (
            "Scope the target",
            "De novo minibinders",
            "Antibodies (VHH)",
            "Dual capabilities (minibinder + antibody scaffolds)",
            "Sequence on a backbone",
            "Structure prediction",
            "Check developability",
            "Other",
        )
        grouped: list[tuple[str, list[dict]]] = []
        for category in category_order:
            members = [t for t in catalog if t.get("category") == category]
            if members:
                grouped.append((category, members))

        breadcrumbs = [
            {"name": "Home", "url": url_for("public.index", _external=True)},
            {"name": "All tools", "url": url_for(
                "tools_comparison", _external=True
            )},
        ]
        return render_template(
            "tools/comparison.html",
            tools=catalog,
            grouped=grouped,
            authenticated=bool(session.get("user_email")),
            breadcrumbs=breadcrumbs,
        )

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
