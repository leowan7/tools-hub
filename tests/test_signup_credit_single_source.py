"""The signup credit is quoted in one place and rendered from it.

The grant amount was hardcoded as the literal "$5" in ~18 templates,
including legal/terms.html. Raising the grant meant editing all of them,
and missing one leaves the site advertising an amount it does not pay --
in the legal terms, an amount it has promised.

So these tests do not check for "$15". They check the *structural*
property that makes the amount safe to change: copy renders from
shared.wallet.SIGNUP_CREDIT_USD via the ``signup_credit`` jinja global,
and no template hardcodes a dollar figure for it.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from app import create_app
from shared.wallet import SIGNUP_CREDIT_USD

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"

# The one regex both the template and the Python guards use. It matches a
# dollar figure standing in signup-credit context, and only there: a
# genuinely different $5 (LOW_BALANCE_EMAIL_THRESHOLD in _header.html and
# send_low_balance.html, the top-up denominations in topup.html, the hold
# rounding worked example in launch.html, "the nearest $5" in wallet.py)
# never reaches one of these phrases.
#
# There is deliberately NO per-file allowlist. The previous version had one,
# and templates/tools/_preview.html sat on it carrying two live "$5" strings
# -- one of them feeding the FAQPage JSON-LD -- for the whole time the wallet
# was granting $15. An exclusion list is how this guard fails.
SIGNUP_MONEY = re.compile(
    # "New accounts start with a $5 wallet balance"
    r"start(?:s|ing)?\s+with\s+(?:an?\s+)?\$\d"
    # "$5 of balance", but not "a $9.1800 balance against ..."
    r"|\$\d[\d,.]*\s+of\s+balance"
    # "$5 in your wallet", "$5 starting balance", "$5 of compute credit"
    r"|\$\d[\d,.]*\s*(?:USD\s+)?(?:of\s+)?"
    r"(?:in\s+(?:their|your)\s+wallet|wallet\s+balance|starting\s+balance"
    r"|signup\s+balance|signup\s+credit|compute\s+credit|credit|grant)",
    re.IGNORECASE,
)


def _templates():
    for p in TEMPLATES.rglob("*.html"):
        yield p


def test_no_template_hardcodes_the_signup_amount():
    offenders = []
    for p in _templates():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if SIGNUP_MONEY.search(line):
                offenders.append(f"{p.relative_to(TEMPLATES)}:{i}: {line.strip()}")
    assert not offenders, (
        "these quote a hardcoded signup credit instead of rendering "
        "{{ signup_credit() }}; change the grant and they silently lie:\n  "
        + "\n  ".join(offenders)
    )


def test_global_reflects_the_constant():
    app = create_app()
    fn = app.jinja_env.globals["signup_credit"]
    assert fn() == f"{SIGNUP_CREDIT_USD:.0f}"
    assert fn(True) == f"{SIGNUP_CREDIT_USD:.2f}"


def test_rendered_pages_quote_the_real_grant():
    """End to end: the number a visitor reads is the number granted."""
    app = create_app()
    app.config["TESTING"] = True
    expected = f"${SIGNUP_CREDIT_USD:.0f}"
    with app.test_client() as c:
        for route in ("/", "/pricing", "/login"):
            body = c.get(route).get_data(as_text=True)
            assert expected in body, f"{route} does not quote {expected}"


def test_legal_terms_quote_the_real_grant():
    """Terms state a courtesy balance -- it must be the amount actually paid."""
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        body = c.get("/terms").get_data(as_text=True)
    assert f"${SIGNUP_CREDIT_USD:.2f} USD" in body


@pytest.mark.parametrize("route", ["/", "/pricing"])
def test_no_stale_five_dollar_promise(route):
    """Guard the specific way this broke: the old literal surviving."""
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        body = c.get(route).get_data(as_text=True)
    if SIGNUP_CREDIT_USD != 5:
        assert "$5 in your wallet" not in body
        assert "$5 in their wallet" not in body


# ---------------------------------------------------------------------------
# Python sources.
#
# Everything above this line checks templates. That was the first hole: when
# the grant went 5.00 -> 15.00, shared/email.py was still reading a separate
# WALLET_SIGNUP_CREDIT_USD env var for the welcome email, defaulting to the
# old figure, and the template-only guard passed the whole time.
#
# The second hole was subtler and shipped further. The first Python guard
# looked for exactly one spelling -- os.environ.get / os.getenv with a
# constant first positional argument -- across exactly five roots. It missed
# os.environ[...] subscripts, a key built by concatenation, a key held in a
# variable, os.getenv(key=..., default=...), a renamed parallel constant,
# and, most of all, a plain hardcoded "$5" in prose. That last one is how
# blueprints/auth.py told every new user "$5 of compute credit" while the
# wallet deposited $15, and how three tools/*/meta.py FAQ answers put "$5"
# into FAQPage JSON-LD.
#
# So these do not look for a spelling. They assert the property:
#   1. no Python source names a signup-credit environment variable,
#   2. no Python source outside shared/wallet.py holds the figure in a
#      second constant,
#   3. no Python source retypes the figure into user-facing prose.
# All three walk every Python file in the repo, not a curated root list --
# billing/checkout.py already reads os.environ for a different wallet knob,
# so "the modules that read env vars" is not a closed set.
# ---------------------------------------------------------------------------

# The only directory held back. Everything tracked is walked, including
# tools/, billing/, webhooks/, scripts/ and gunicorn.conf.py. tests/ is
# skipped because this file's own prose is full of the strings below.
_SKIP_DIRS = {"tests"}

# The single home. Everything else must derive from it.
_GRANT_HOME = "shared/wallet.py"

# An environment-variable name for the signup credit. Env names are upper
# snake case, which is also what keeps the codebase's own lower case
# "signup_credit" identifiers (the jinja global, the ledger row kind) out.
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")

# A second constant holding the figure: a signup-credit-ish name bound to a
# literal amount. A name bound to a derived value -- f"${SIGNUP_CREDIT_USD:.0f}"
# in tools/*/meta.py -- is fine and does not match, because the value is not
# a literal.
_PARALLEL_NAME = re.compile(
    r"^_?(?:SIGNUP|WELCOME|NEW_?ACCOUNT|STARTING|INITIAL|FREE|BONUS|GRANT)"
    r"[A-Z0-9_]*CREDIT[A-Z0-9_]*$"
    r"|^_?CREDIT[A-Z0-9_]*(?:SIGNUP|WELCOME|GRANT)[A-Z0-9_]*$"
)


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
    """A guard over zero files passes. Assert the walk is real.

    The previous version listed five roots by hand and was green while
    tools/*/meta.py and billing/checkout.py sat outside it -- and
    billing/checkout.py already reads os.environ for a wallet knob, so
    that omission was the whole class.
    """
    walked = {rel for rel, _ in _parsed_sources()}
    assert len(walked) > 100, len(walked)
    for expected in (
        "app.py",
        "gunicorn.conf.py",
        _GRANT_HOME,
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


def test_no_python_source_names_a_signup_credit_env_var():
    """No module reads the grant from the environment, however spelled.

    Asserted over the name rather than the call: a string that looks like a
    signup-credit env var has no legitimate reason to exist in this repo, so
    os.environ[K], os.getenv(key=K), K = "..." then os.getenv(K), and
    "WALLET_SIGNUP" + "_CREDIT_USD" all fail on the same line of code.
    """
    offenders = []
    for rel, tree in _parsed_sources():
        for node in ast.walk(tree):
            value = _fold(node)
            if value is None or not _ENV_NAME.fullmatch(value):
                continue
            if "SIGNUP" in value and "CREDIT" in value:
                offenders.append(f"{rel}:{node.lineno} names {value!r}")
    assert not offenders, (
        "the signup credit must come from shared.wallet.SIGNUP_CREDIT_USD, "
        "not an environment variable:\n  " + "\n  ".join(sorted(set(offenders)))
    )


def test_no_python_source_defines_a_parallel_signup_credit_constant():
    """The figure lives in one binding. A second one is a second truth."""
    offenders = []
    for rel, tree in _parsed_sources():
        if rel == _GRANT_HOME:
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
        "these hold the signup credit as their own literal instead of "
        f"importing it from {_GRANT_HOME}:\n  " + "\n  ".join(offenders)
    )


def test_no_python_source_hardcodes_the_signup_amount():
    """The one that actually caught things: a retyped "$5" in prose.

    Same regex as the template guard, run over string constants so a
    hardcoded figure in blueprints/auth.py's signup flash message or in a
    tools/*/meta.py FAQ answer -- which reaches FAQPage JSON-LD -- fails
    exactly like a hardcoded figure in a template does.
    """
    offenders = []
    for rel, tree in _parsed_sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str):
                continue
            match = SIGNUP_MONEY.search(node.value)
            if match:
                offenders.append(f"{rel}:{node.lineno}: {match.group(0)!r}")
    assert not offenders, (
        "these quote a hardcoded signup credit instead of formatting "
        "shared.wallet.SIGNUP_CREDIT_USD; change the grant and they "
        "silently lie:\n  " + "\n  ".join(offenders)
    )


def test_signup_credit_email_states_the_real_grant():
    """The welcome email quotes the amount the wallet actually grants.

    The goal-level version of the test above: even if some future override
    is reintroduced by a route this guard does not walk, the email itself
    must still agree with the balance.
    """
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
        assert email_mod.send_signup_credit_email(user_id="u1") is True

    expected = f"${SIGNUP_CREDIT_USD:.0f}"
    assert expected in captured["subject"], captured["subject"]
    assert expected in captured["html"]
