"""Every literal ``url_for()`` endpoint in a template must actually exist.

Regression guard for a real outage: IgGM shipped with
``url_for('tool_submit', ...)`` in its form and ``url_for('tool_form', ...)``
in its results partial. Both routes had moved into
``Blueprint("tools", __name__)`` during the blueprint refactor and been renamed
``tools.tool_submit`` / ``tools.tool_form``. The IgGM branch was cut before
that refactor, so the stale names survived the merge and every render of
/tools/iggm raised ``BuildError`` -> HTTP 500: the tool was listed in the
catalog but unusable.

Rendering each form catches the form case, but not the results-partial case:
results partials are only included by job_detail.html once a job succeeds, so a
broken endpoint there stays latent until the first real (paid, GPU) run
finishes and then 500s the results page. A results partial CAN be rendered
directly against a fake job (see tests/test_esmfold_smoke.py and
test_colabfold_smoke.py), but that is opt-in per tool and IgGM's had none.
Scanning statically needs no per-tool fixture, so it covers every template —
including ones nobody thought to write a render test for. That is the point:
this bug class comes from branches cut before a refactor, not from anything
IgGM-specific.

The app is booted in the maximal posture (ENABLE_PLATFORM_API=1) so that
conditionally-registered blueprints are present. Without it, the
``platform_account.*`` endpoints in account.html / account_api_keys.html look
missing when they are in fact real and correctly guarded by
``{% if platform_api_enabled %}``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"

# First arg of url_for(...) when it is a plain literal. Dynamic endpoints
# (url_for(some_var), url_for(a ~ b)) are skipped — not statically checkable.
_URL_FOR_LITERAL = re.compile(r"""url_for\(\s*(['"])([^'"]+)\1""")


# Jinja renders more than .html here (the email bodies are .txt). Scan every
# template file so a url_for() added to one later is covered automatically.
_TEMPLATE_SUFFIXES = {".html", ".txt", ".xml", ".j2", ".jinja", ".jinja2"}


def _iter_template_endpoints():
    """Yield (relative_path, lineno, endpoint) for every literal url_for."""
    paths = sorted(
        p for p in TEMPLATES_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in _TEMPLATE_SUFFIXES
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _URL_FOR_LITERAL.finditer(line):
                rel = path.relative_to(TEMPLATES_DIR).as_posix()
                yield rel, lineno, match.group(2)


@pytest.fixture
def all_endpoints(monkeypatch):
    """Boot the app with every conditional blueprint registered and return
    the full set of endpoint names it knows about."""
    monkeypatch.setenv("ENABLE_PLATFORM_API", "1")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "test-webhook-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")

    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return set(flask_app.view_functions.keys())


def test_templates_reference_only_real_endpoints(all_endpoints):
    """No template may url_for() an endpoint the app does not register.

    A failure here is a guaranteed BuildError -> 500 on any page that renders
    the offending template.
    """
    broken: list[str] = []
    for rel, lineno, endpoint in _iter_template_endpoints():
        if endpoint not in all_endpoints:
            suffix = endpoint.rsplit(".", 1)[-1]
            suggestions = sorted(
                ep for ep in all_endpoints if ep.rsplit(".", 1)[-1] == suffix
            )
            hint = f" (did you mean {' / '.join(suggestions)}?)" if suggestions else ""
            broken.append(f"templates/{rel}:{lineno} -> url_for({endpoint!r}){hint}")

    assert not broken, (
        "Template(s) reference endpoints that do not exist; these raise "
        "BuildError and 500 the page:\n  " + "\n  ".join(broken)
    )


def test_platform_account_endpoints_are_registered(all_endpoints):
    """Pin the fixture's maximal posture.

    account.html legitimately references ``platform_account.*`` behind
    ``{% if platform_api_enabled %}``. If the fixture ever stops registering
    that blueprint, the test above would report those as broken and someone
    would 'fix' working code. Fail here instead, with the real reason.
    """
    assert "platform_account.account_api_keys" in all_endpoints, (
        "ENABLE_PLATFORM_API=1 no longer registers platform_account_bp; the "
        "endpoint scan above would now report false positives in account.html."
    )


def test_scanner_actually_finds_url_for_calls():
    """Guard the guard: if the regex silently matched nothing, the endpoint
    test would pass vacuously and provide no protection at all."""
    found = list(_iter_template_endpoints())
    assert len(found) > 50, (
        f"Only found {len(found)} url_for() calls across templates/ — the "
        "scanner is probably broken, making the endpoint check vacuous."
    )
