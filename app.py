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

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

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
        """Unauthenticated health check for Render/Railway port scanners."""
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
        ]
        return render_template("index.html", tools=tools)

    @flask_app.route("/account", methods=["GET"])
    @login_required
    def account():
        """Simple account dashboard showing the logged-in user's email."""
        return render_template(
            "account.html",
            user_email=session.get("user_email", ""),
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
