"""The minimum top-up is quoted in one place and rendered from it.

Every page that names the minimum renders the ``min_topup`` jinja global,
which app.py formats from shared.wallet.MIN_TOPUP_USD. Checkout reads the
same constant (billing/checkout.py ``_default_min_topup_usd``), with no
environment override, so the figure a page prints is the floor Checkout
enforces.

These tests check that structural property rather than "$5": copy renders
from the constant, no template or Python string retypes the figure, and no
module reads it from the environment. They also check that no public page
still promises a signup credit, which new accounts stopped receiving on
2026-10-23.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from app import create_app
from shared.wallet import MIN_AUTO_RELOAD_USD, MIN_TOPUP_USD

pytestmark = pytest.mark.usefixtures("isolate_supabase")

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"

# A dollar figure standing in minimum-top-up or signup-credit context. A
# different $5 (LOW_BALANCE_EMAIL_THRESHOLD, the top-up chip labels in
# topup.html, "the nearest $5" in wallet.py) never reaches one of these
# phrases. There is deliberately no per-file allowlist.
MONEY_COPY = re.compile(
    # "Start with as little as $5", "New accounts start with a $5 balance"
    r"start(?:s|ing)?\s+with\s+(?:an?\s+)?\$\d"
    r"|as\s+little\s+as\s+\$\d"
    # "Top up from $5", "minimum top-up is $5"
    r"|top[\s-]?up\s+from\s+\$\d"
    r"|minimum\s+top[\s-]?up(?:\s+is)?\s+\$\d"
    # "$5 of balance", but not "a $9.1800 balance against ..."
    r"|\$\d[\d,.]*\s+of\s+balance"
    # "$5 in your wallet", "$5 starting balance", "$5 of compute credit"
    r"|\$\d[\d,.]*\s*(?:USD\s+)?(?:of\s+)?"
    r"(?:in\s+(?:their|your)\s+wallet|wallet\s+balance|starting\s+balance"
    r"|signup\s+balance|signup\s+credit|compute\s+credit|credit|grant)",
    re.IGNORECASE,
)

# Pages a new visitor reads before paying.
_PUBLIC_ROUTES = ("/", "/pricing", "/login", "/help/faq", "/help/getting-started")


def _templates():
    for p in TEMPLATES.rglob("*.html"):
        yield p


def _client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_no_template_hardcodes_the_amount():
    offenders = []
    for p in _templates():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if MONEY_COPY.search(line):
                offenders.append(f"{p.relative_to(TEMPLATES)}:{i}: {line.strip()}")
    assert not offenders, (
        "these hardcode a minimum top-up or signup credit instead of "
        "rendering {{ min_topup }}:\n  " + "\n  ".join(offenders)
    )


def test_globals_reflect_the_constants():
    globals_ = create_app().jinja_env.globals
    assert globals_["min_topup"] == f"{MIN_TOPUP_USD:.0f}"
    assert globals_["min_auto_reload"] == f"{MIN_AUTO_RELOAD_USD:.0f}"
    assert "signup_credit" not in globals_


@pytest.mark.parametrize("route", _PUBLIC_ROUTES)
def test_public_pages_quote_the_minimum_and_no_signup_credit(route):
    """End to end: a visitor reads the real minimum and no free credit."""
    with _client() as c:
        body = c.get(route).get_data(as_text=True)
    assert f"${MIN_TOPUP_USD:.0f}" in body, f"{route} does not quote the minimum"
    for phrase in ("signup credit", "free credit", "signup balance", "compute credit"):
        assert phrase not in body.lower(), f"{route} still promises {phrase!r}"


def test_legal_terms_say_new_accounts_get_no_credit():
    with _client() as c:
        body = c.get("/terms").get_data(as_text=True)
    assert "new accounts receive none" in body


# ---------------------------------------------------------------------------
# Python sources: no module reads the figure from the environment, holds it
# in a second constant, or retypes it into user-facing prose. They walk every
# tracked Python file, not a curated root list.
# ---------------------------------------------------------------------------

# The only directory held back. Everything tracked is walked, including
# tools/, billing/, webhooks/, scripts/ and gunicorn.conf.py. tests/ is
# skipped because this file's own prose is full of the strings below.
_SKIP_DIRS = {"tests"}

# The single home. Everything else must derive from it.
_HOME = "shared/wallet.py"

# An environment-variable name. Env names are upper snake case, which keeps
# the lower case "signup_credit" identifiers (the ledger row kind) out.
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")

# A second constant holding the figure: a minimum-top-up name bound to a
# literal amount. A name bound to a derived value -- f"${MIN_TOPUP_USD:.0f}"
# in tools/*/meta.py -- does not match, because the value is not a literal.
_PARALLEL_NAME = re.compile(r"^_?(?:MIN(?:IMUM)?_?TOP_?UP|TOP_?UP_?MIN)[A-Z0-9_]*$")


def _python_sources():
    """Every shipped Python file in the repo.

    Tracked files, so the walk is exactly what ships -- no venv, no
    worktree checkouts under .claude/, and no developer's untracked
    scratch file turning the suite red. Falls back to a filesystem walk
    where git is unavailable.
    """
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.split("\0")
        paths = (REPO_ROOT / rel for rel in listed if rel)
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover
        paths = REPO_ROOT.rglob("*.py")
    for path in paths:
        parts = path.relative_to(REPO_ROOT).parts
        if any(part.startswith(".") or part in _SKIP_DIRS for part in parts):
            continue
        if path.is_file():
            yield path


def _parsed_sources():
    for path in _python_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        yield path.relative_to(REPO_ROOT).as_posix(), tree


def _fold(node):
    """Constant-fold a string expression, or return None.

    Covers the literal, adjacent/`+` concatenation, and the constant parts
    of an f-string -- the three ways a name gets spelled without ever
    appearing as one literal. A name bound elsewhere is caught at its
    binding site, since the literal has to exist somewhere in the file.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return "".join(parts) or None
    return None


def _is_literal_amount(node) -> bool:
    """True for 5, 5.0, "5.00", Decimal("5.00") -- a figure typed out."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return False
        if isinstance(node.value, (int, float)):
            return True
        return isinstance(node.value, str) and any(c.isdigit() for c in node.value)
    if isinstance(node, ast.Call):
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in {"Decimal", "float", "int", "str"}:
            return any(_is_literal_amount(a) for a in node.args)
    if isinstance(node, ast.UnaryOp):
        return _is_literal_amount(node.operand)
    return False


def test_the_guard_walks_every_module_that_could_drift():
    """A guard over zero files passes. Assert the walk is real."""
    walked = {rel for rel, _ in _parsed_sources()}
    assert len(walked) > 100, len(walked)
    for expected in (
        "app.py",
        "gunicorn.conf.py",
        _HOME,
        "shared/email.py",
        "blueprints/auth.py",
        "billing/checkout.py",
        "webhooks/stripe.py",
        "cron/daily_digest.py",
        "scout/routes.py",
        "tools/af2/meta.py",
        "tools/mpnn/meta.py",
        "tools/rfdiffusion/meta.py",
    ):
        assert expected in walked, f"{expected} is not walked by the guard"
    assert not any(rel.startswith("tests/") for rel in walked)


def test_no_python_source_names_a_minimum_topup_or_signup_credit_env_var():
    """Neither figure is read from the environment, however spelled.

    Asserted over the name rather than the call, so os.environ[K],
    os.getenv(key=K) and "WALLET_MIN" + "_TOPUP_USD" all fail.
    """
    offenders = []
    for rel, tree in _parsed_sources():
        for node in ast.walk(tree):
            value = _fold(node)
            if value is None or not _ENV_NAME.fullmatch(value):
                continue
            if ("SIGNUP" in value and "CREDIT" in value) or "MIN_TOPUP" in value:
                offenders.append(f"{rel}:{node.lineno} names {value!r}")
    assert not offenders, (
        f"the minimum top-up must come from {_HOME}, not an environment "
        "variable:\n  " + "\n  ".join(sorted(set(offenders)))
    )


def test_no_python_source_defines_a_parallel_minimum_topup_constant():
    """The figure lives in one binding. A second one is a second truth."""
    offenders = []
    for rel, tree in _parsed_sources():
        if rel == _HOME:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            for target in targets:
                name = getattr(target, "id", None)
                if not name or not _PARALLEL_NAME.match(name):
                    continue
                if _is_literal_amount(value):
                    offenders.append(f"{rel}:{node.lineno} binds {name}")
    assert not offenders, (
        "these hold the minimum top-up as their own literal instead of "
        f"importing it from {_HOME}:\n  " + "\n  ".join(offenders)
    )


def test_no_python_source_hardcodes_the_amount():
    """Same regex as the template guard, over every Python string constant.

    A tools/*/meta.py FAQ answer reaches FAQPage JSON-LD, and the signup
    flash in blueprints/auth.py reaches every new user.
    """
    offenders = []
    for rel, tree in _parsed_sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str):
                continue
            match = MONEY_COPY.search(node.value)
            if match:
                offenders.append(f"{rel}:{node.lineno}: {match.group(0)!r}")
    assert not offenders, (
        "these hardcode a minimum top-up or signup credit instead of "
        "formatting shared.wallet.MIN_TOPUP_USD:\n  " + "\n  ".join(offenders)
    )


def test_welcome_email_states_the_minimum_and_no_credit():
    import shared.email as email_mod

    captured = {}

    def _fake_post(*, to_email, subject, html_body, log_tag):
        captured.update(subject=subject, html=html_body)
        return True

    with (
        mock.patch.object(email_mod, "_post_resend", _fake_post),
        mock.patch.object(
            email_mod, "_resolve_user_email", return_value="x@example.com"
        ),
    ):
        assert email_mod.send_welcome_email(user_id="u1") is True

    assert f"${MIN_TOPUP_USD:.0f}" in captured["html"]
    assert "credit" not in captured["subject"].lower()
    assert "credit" not in captured["html"].lower()


def test_auto_reload_amount_keeps_its_own_floor(monkeypatch):
    """Auto reload clamps to MIN_AUTO_RELOAD_USD, not the $5 top-up minimum."""
    from types import SimpleNamespace

    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    assert MIN_AUTO_RELOAD_USD > MIN_TOPUP_USD
    client = mock.MagicMock()
    with _client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = "u-1"
            sess["user_email"] = "u@example.com"
        with (
            mock.patch(
                "blueprints.wallet.load_user_context",
                return_value=SimpleNamespace(user_id="u-1", email="u@example.com"),
            ),
            mock.patch("shared.credits.get_service_client", return_value=client),
        ):
            resp = c.post(
                "/account/wallet/auto-reload",
                data={"auto_reload_amount_usd": str(MIN_TOPUP_USD)},
            )
    assert resp.status_code == 302
    row = client.table.return_value.update.call_args.args[0]
    assert row["auto_reload_amount_usd"] == float(MIN_AUTO_RELOAD_USD)
