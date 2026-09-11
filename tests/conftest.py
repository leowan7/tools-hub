"""Shared pytest configuration for tools-hub.

The web-UI CSRF guard (FIX M2, cso audit 2026-06-17) is ON by default in
every real deployment. The existing route-level tests POST to protected
endpoints without a CSRF token, so enforcement is disabled process-wide for
the test session here. The dedicated suite (tests/test_csrf_protection.py)
re-enables it per-test via monkeypatch to exercise the guard directly.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _disable_csrf_enforcement_for_tests():
    prev = os.environ.get("CSRF_PROTECT")
    os.environ["CSRF_PROTECT"] = "0"
    yield
    if prev is None:
        os.environ.pop("CSRF_PROTECT", None)
    else:
        os.environ["CSRF_PROTECT"] = prev


# Credentials `shared.credits` reads at CALL time to build a client. Blanking
# them is what makes get_service_client() return None.
_SUPABASE_ENV = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_KEY",
    "SUPABASE_ANON_KEY",
)


@pytest.fixture
def isolate_supabase(monkeypatch):
    """Cut a test off from the live database.

    ``app.py`` calls ``load_dotenv()`` at import, and the repo-root ``.env``
    carries real service-role credentials — so any test that imports ``app``
    and exercises a route runs REAL writes against production. `@idempotent()`
    routes are the worst case: they INSERT into ``idempotency_keys`` and then
    replay those cached responses into later runs, which made three
    cross-tenant isolation assertions fail intermittently (a target owned by
    u-1 came back for u-2) while passing most of the time.

    A test whose subject is ownership cannot be allowed to consult a real
    database it does not control. Opt in with::

        pytestmark = pytest.mark.usefixtures("isolate_supabase")

    This is deliberately opt-in rather than autouse: making the whole suite
    hermetic in one move would change the environment of ~1500 existing tests,
    which is its own change with its own blast radius.
    """
    for name in _SUPABASE_ENV:
        monkeypatch.setenv(name, "")
    yield


@pytest.fixture(scope="module")
def all_tools_app():
    """Every registered adapter flagged on, not a remembered subset."""
    import app as app_module  # noqa: PLC0415  (populates tools.base registry)
    from shared.feature_flags import flag_name  # noqa: PLC0415
    from tools import base as tool_base  # noqa: PLC0415

    slugs = sorted(a.slug for a in tool_base.all_adapters())
    assert len(slugs) >= 14, (
        f"adapter registry holds {len(slugs)} tools; tools.base._REGISTRY "
        "is empty until `import app` populates it, and a registry that did "
        "not populate would leave every per-adapter test iterating nothing"
    )
    # Module-scoped so a module's page-render sweeps happen once for the
    # module rather than once per calling test. That rules out the
    # function-scoped `monkeypatch` fixture; MonkeyPatch.context() is the
    # same setenv/undo over the module's lifetime.
    #
    # Ordering note: a module-scoped fixture is built before the
    # function-scoped isolate_supabase blanks the credentials. That is
    # safe here because create_app() captures no Supabase client --
    # get_service_client() is called inside inject_workspace_context,
    # a context processor, so it runs per request instead.
    with pytest.MonkeyPatch.context() as mp:
        for slug in slugs:
            mp.setenv(flag_name(slug), "on")
        mp.setenv("SESSION_SECRET_KEY", "test-secret")
        flask_app = app_module.create_app()
        flask_app.config["TESTING"] = True
        yield flask_app, slugs
