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

from gpu.modal_client import ModalClient
from shared.credits import (
    load_user_context,
    record_spend,
    recent_ledger,
    requires_credits,
)
from shared.feature_flags import tool_enabled
from shared.idempotency import idempotent
from shared.jobs import (
    create_job,
    get_job,
    list_jobs_for_user,
    mark_failed,
    mark_running,
    mark_succeeded,
    mark_timeout,
    set_modal_call,
)
from shared.metrics import register_metrics
from shared.storage import StorageError, presigned_input_url, upload_input
from tools import base as tool_base
import tools.rfantibody  # noqa: F401 — import to register adapter
from webhooks.modal import register_modal_webhooks
from webhooks.stripe import register_stripe_webhook

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    """Create and configure the tools-hub Flask application.

    Returns:
        Flask: Configured Flask application instance.
    """
    flask_app = Flask(__name__)

    # Secret key for signing Flask session cookies. Set SESSION_SECRET_KEY
    # in the deployment environment. Random fallback means sessions do not
    # survive restarts, which is acceptable for an internal tool.
    flask_app.config["SECRET_KEY"] = os.environ.get(
        "SESSION_SECRET_KEY", os.urandom(32)
    )

    # Inject the current user's tier + credit balance into every template
    # so the shared header can render the tier badge / credits pill without
    # every view recomputing them. Cheap on Wave-0 volume; move to a
    # per-request cache once call counts climb.
    @flask_app.context_processor
    def inject_ranomics_context():
        if not session.get("user_email"):
            return {"ranomics_tier": None, "ranomics_credits": None}
        ctx = load_user_context()
        if ctx is None:
            return {"ranomics_tier": None, "ranomics_credits": None}
        return {
            "ranomics_tier": ctx.tier,
            "ranomics_credits": ctx.balance,
            "ranomics_user_id": ctx.user_id,
        }

    # Stripe webhook — mounted at /webhooks/stripe. Signature verification
    # + event_id idempotency live inside webhooks/stripe.py.
    register_stripe_webhook(flask_app)

    # Prometheus /metrics (IP-allowlisted) + /healthz readiness probe.
    # The existing /health liveness probe below stays as a dumb 200.
    register_metrics(flask_app)

    # Modal pipeline callbacks — /webhooks/modal/<job_id>/<token> + /webhooks/heartbeat.
    register_modal_webhooks(flask_app)

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

        success, error_msg = verify_login(email, password)
        if success:
            session["user_email"] = email
            # Restrict redirect to same-origin paths to prevent open redirect.
            if not next_url.startswith("/"):
                next_url = "/"
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
        """Render the sign-up form (GET) or handle new account creation (POST)."""
        from shared.auth import register_user  # noqa: PLC0415

        if request.method == "GET":
            return render_template(
                "login.html",
                mode="signup",
                error=None,
                signup_email=None,
                next="/",
            )

        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        if not email or not password:
            return render_template(
                "login.html",
                mode="signup",
                error="Email and password are required.",
                signup_email=email,
                next="/",
            )

        if len(password) < 8:
            return render_template(
                "login.html",
                mode="signup",
                error="Password must be at least 8 characters.",
                signup_email=email,
                next="/",
            )

        if password != password2:
            return render_template(
                "login.html",
                mode="signup",
                error="Passwords do not match.",
                signup_email=email,
                next="/",
            )

        success, error_msg = register_user(email, password)
        if success:
            return render_template(
                "login.html",
                mode="signin",
                error=None,
                email=email,
                next="/",
                success_msg=(
                    "Account created. Check your email and click the "
                    "confirmation link before signing in."
                ),
            )

        return render_template(
            "login.html",
            mode="signup",
            error=error_msg,
            signup_email=email,
            next="/",
        )

    @flask_app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        """Handle password reset requests."""
        from shared.auth import reset_password  # noqa: PLC0415

        if request.method == "GET":
            return render_template(
                "login.html",
                mode="reset",
                error=None,
                email=None,
                next="/",
                reset_success=None,
            )

        email = request.form.get("email", "").strip()
        success, error_msg = reset_password(email)

        if success:
            return render_template(
                "login.html",
                mode="reset",
                error=None,
                email=email,
                next="/",
                reset_success=(
                    "If an account exists with this email, you will "
                    "receive a password reset link."
                ),
            )

        return render_template(
            "login.html",
            mode="reset",
            error=error_msg,
            email=email,
            next="/",
            reset_success=None,
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
    @login_required
    def index():
        """Hub index — shows the three tool cards."""
        tools = [
            {
                "id": "epitope-scout",
                "name": "Epitope Scout",
                "tagline": (
                    "Identify candidate surface epitopes for binder "
                    "design campaigns."
                ),
                "status": "live",
                "href": "https://scout.ranomics.com",
                "external": True,
            },
            {
                "id": "developability",
                "name": "Binder Developability Scout",
                "tagline": (
                    "Flag developability liabilities in antibody and "
                    "nanobody sequences before you order them."
                ),
                "status": "live",
                "href": url_for("developability"),
                "external": False,
            },
            {
                "id": "library-planner",
                "name": "Yeast Display Library Planner",
                "tagline": (
                    "Plan yeast display libraries with realistic "
                    "diversity and screen-size estimates."
                ),
                "status": "live",
                "href": url_for("library_planner"),
                "external": False,
            },
            {
                "id": "example-gpu",
                "name": "Example GPU tool (Wave-0 stub)",
                "tagline": (
                    "Internal plumbing fixture. Proves the credits "
                    "middleware and Modal client contract end-to-end."
                ),
                "status": "soon",
                "href": url_for("example_gpu"),
                "external": False,
            },
        ]
        return render_template("index.html", tools=tools)

    @flask_app.route("/account", methods=["GET"])
    @login_required
    def account():
        """Account dashboard: tier, credits, and last 20 ledger entries."""
        ctx = load_user_context()
        ledger = recent_ledger(ctx.user_id, limit=20) if ctx else []
        return render_template(
            "account.html",
            user_email=session.get("user_email", ""),
            ledger=ledger,
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
            return None, (render_template("coming_soon.html"), 404)
        if not tool_enabled(tool_slug):
            return None, (render_template("coming_soon.html"), 404)
        return adapter, None

    @flask_app.route("/tools/<tool>", methods=["GET"])
    @login_required
    def tool_form(tool: str):
        """Render a GPU tool's submission form."""
        adapter, err = _require_tool(tool)
        if err:
            return err
        return render_template(
            adapter.form_template,
            adapter=adapter,
            error=None,
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

        inputs, error_msg = adapter.validate(request.form, request.files)
        if inputs is None:
            return render_template(
                adapter.form_template, adapter=adapter, error=error_msg
            )

        preset = adapter.preset_for(inputs["preset"])
        if preset is None:
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error="Unknown preset.",
            )

        if ctx.balance < preset.credits_cost:
            return redirect(url_for("account", insufficient_credits=1))

        # Create the tool_jobs row FIRST so we have job_id + job_token for
        # the Modal payload and a persistent handle even if Modal submit
        # raises. Credits debit happens only on successful Modal submit.
        job = create_job(
            user_id=ctx.user_id,
            tool=adapter.slug,
            preset=preset.slug,
            inputs=inputs,
            credits_cost=preset.credits_cost,
        )
        if job is None:
            return render_template(
                adapter.form_template,
                adapter=adapter,
                error=(
                    "Could not create job record — Supabase is unreachable. "
                    "Try again in a moment."
                ),
            )

        presigned_url = ""
        if adapter.requires_pdb:
            uploaded = request.files.get("target_pdb")
            if uploaded is None or not uploaded.filename:
                return render_template(
                    adapter.form_template,
                    adapter=adapter,
                    error="Upload a target PDB file.",
                )
            try:
                object_path = upload_input(
                    user_id=ctx.user_id,
                    job_id=job.id,
                    filename=uploaded.filename,
                    data=uploaded.read(),
                    content_type=uploaded.mimetype or "chemical/x-pdb",
                )
                presigned_url = presigned_input_url(
                    object_path, expires_seconds=7200
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
            )

        set_modal_call(job.id, submit_result["function_call_id"])
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
        jobs = list_jobs_for_user(ctx.user_id, limit=50)
        return render_template("jobs_list.html", jobs=jobs)

    @flask_app.route("/jobs/<job_id>", methods=["GET"])
    @login_required
    def job_detail(job_id: str):
        ctx = load_user_context()
        if ctx is None:
            return redirect(url_for("login"))
        job = get_job(job_id, user_id=ctx.user_id)
        if job is None:
            return render_template("coming_soon.html"), 404
        adapter = tool_base.get(job.tool)
        return render_template(
            "job_detail.html",
            job=job,
            tool_label=adapter.label if adapter else job.tool,
            tool_results_partial=(
                adapter.results_partial
                if adapter and adapter.results_partial
                else "tools/_default_results.html"
            ),
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
                mark_succeeded(
                    job.id,
                    result=poll["result"] or {},
                    gpu_seconds_used=poll.get("gpu_seconds_used"),
                )
                job = get_job(job_id, user_id=ctx.user_id)
            elif poll["status"] == "failed":
                mark_failed(
                    job.id,
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
