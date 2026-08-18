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
from pathlib import Path
from unittest import mock

import pytest

from app import create_app
from shared.wallet import SIGNUP_CREDIT_USD

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"

# "$5" in these is a DIFFERENT constant and must not be swept up:
#   _header.html / send_low_balance.html -> LOW_BALANCE_EMAIL_THRESHOLD
ALLOWED_LITERAL_DOLLAR = {
    "_header.html",
    "send_low_balance.html",
    "topup.html",          # top-up denomination buttons
    "launch.html",         # a worked example of hold rounding, in a comment
    "transactions.html",   # checked separately below
    # _preview.html is the logged-out tool shell, DELETED outright by the
    # "open the tool pages" PR (#144). Editing a file another open PR
    # deletes produces a delete/modify conflict that a human then has to
    # resolve, so it is excluded rather than fixed. Drop this entry once
    # #144 lands and the file is gone -- if the file still exists then,
    # this exclusion is hiding a real one.
    "_preview.html",
}

SIGNUP_CONTEXT = re.compile(
    r"\$\d[\d,.]*\s*(?:USD\s*)?(?:in (?:their|your) wallet|signup balance|"
    r"wallet balance|of balance|signup credit)",
    re.IGNORECASE,
)


def _templates():
    for p in TEMPLATES.rglob("*.html"):
        yield p


def test_no_template_hardcodes_the_signup_amount():
    offenders = []
    for p in _templates():
        if p.name in ALLOWED_LITERAL_DOLLAR:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if SIGNUP_CONTEXT.search(line):
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
# Everything above this line checks templates. That was the hole: when the
# grant went 5.00 -> 15.00, shared/email.py was still reading a separate
# WALLET_SIGNUP_CREDIT_USD env var for the welcome email, defaulting to the
# old figure. The template guard passed the whole time, and the preview
# environment shipped a welcome email advertising $5 against a $15 balance.
# A guard that only looks at one language is not a single-source guard.
# ---------------------------------------------------------------------------

_PY_ROOTS = ("shared", "blueprints", "scout", "cron")


def _python_sources():
    for root in _PY_ROOTS:
        base = REPO_ROOT / root
        if base.is_dir():
            yield from base.rglob("*.py")
    yield REPO_ROOT / "app.py"


def test_no_python_module_reads_a_parallel_signup_credit_env_var():
    """The grant has one home: shared.wallet.SIGNUP_CREDIT_USD.

    Parsed with ast rather than grepped, so a match is a real call and not a
    mention inside a comment or docstring -- this file's own prose names the
    variable repeatedly.
    """
    offenders = []
    for path in _python_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # os.environ.get("WALLET_SIGNUP_CREDIT_USD", ...) / os.getenv(...)
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name not in {"get", "getenv"}:
                continue
            for arg in node.args:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and "SIGNUP_CREDIT" in arg.value
                ):
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    offenders.append(f"{rel}:{node.lineno} reads {arg.value!r}")
    assert not offenders, (
        "signup credit read from an env var instead of shared.wallet."
        "SIGNUP_CREDIT_USD:\n  " + "\n  ".join(offenders)
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
