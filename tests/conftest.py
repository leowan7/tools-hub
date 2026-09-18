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
    hermetic in one move would change the environment of every test in the
    suite -- ~7,400 of them (7,432 collected as of this commit; run
    ``pytest -q --collect-only | tail -1`` for the current number rather than
    trusting this one). That is its own change with its own blast radius.
    """
    for name in _SUPABASE_ENV:
        monkeypatch.setenv(name, "")
    yield


@pytest.fixture(scope="module")
def isolate_supabase_module():
    """Module-scoped twin of ``isolate_supabase``.

    pytest builds a module-scoped fixture BEFORE the function-scoped
    ``isolate_supabase``, so a fixture that renders a signed-in page
    during its own setup reaches the real database even in a file that
    already carries the module-level mark -- the mark cannot fire early
    enough. Request this from the fixture that builds the app.

    ``MonkeyPatch.context()`` is the same setenv/undo as ``monkeypatch``,
    held open over the module's lifetime instead of one test's.
    """
    with pytest.MonkeyPatch.context() as mp:
        for name in _SUPABASE_ENV:
            mp.setenv(name, "")
        yield


class _RealSupabaseClientRefused(BaseException):
    """Deliberately NOT an ``Exception``.

    Every in-app build site wraps the constructor in ``except Exception:`` and
    returns None -- ``shared.credits.get_service_client``,
    ``shared.supabase_client.get_supabase_client``,
    ``scout.handoff._get_service_client`` and
    ``scout.quota._get_service_client``. An ``Exception`` alarm is swallowed by
    the very code it indicts: the forgetful test then passes holding a ``None``
    client, which is exactly the silent rot this guard exists to stop.
    ``BaseException`` passes through those handlers, and pytest still reports
    it as a failure rather than aborting the session.

    Enforced by ``test_the_alarm_is_not_swallowed_by_app_code`` in
    ``tests/test_supabase_client_guard.py``, which reaches the guard through
    ``get_service_client`` and fails if the alarm is caught on the way out.
    """


_REAL_CLIENT_ALLOWED = ("tests/test_rls.py",)


@pytest.fixture(autouse=True, scope="session")
def _forbid_real_supabase_clients():
    """Fail the test that builds a REAL Supabase client instead of letting it.

    ``isolate_supabase`` blanks the credentials so ``get_service_client()``
    returns None long before a constructor is reached; this is the backstop
    for a file that is missing the mark. All four in-app build sites import
    ``create_client`` INSIDE the function (``shared/credits.py``,
    ``shared/supabase_client.py``, ``scout/handoff.py``, ``scout/quota.py``),
    so the name resolves at call time and patching the module attribute
    intercepts every one of them.

    ``tests/test_rls.py`` is the one deliberate exception: its subject IS a
    live Row-Level-Security check against the real project, and it skips
    itself when the credentials are absent.

    Session scope matters. A function-scoped fixture is built AFTER a
    module-scoped one, so it cannot see a client built during a module-scoped
    fixture's own setup -- the same ordering that let four marked files reach
    production in #296.

    With the supabase package absent no call site can build a client at all,
    so the guard has nothing to protect and steps aside.
    """
    try:
        import supabase  # noqa: PLC0415
    except ImportError:
        yield
        return

    real = supabase.create_client

    def _guarded(*args, **kwargs):
        current = os.environ.get("PYTEST_CURRENT_TEST", "<no active test>")
        if current.startswith(_REAL_CLIENT_ALLOWED):
            return real(*args, **kwargs)
        raise _RealSupabaseClientRefused(
            f"{current} built a REAL Supabase client, which reaches the "
            "production project. Add "
            '`pytestmark = pytest.mark.usefixtures("isolate_supabase")` to the '
            "file; if the app is built by a module-scoped fixture, have that "
            "fixture request `isolate_supabase_module` instead -- a "
            "function-scoped mark fires too late for it."
        )

    supabase.create_client = _guarded
    yield
    supabase.create_client = real


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
        "not populate would leave every test that iterates it covering "
        "nothing"
    )
    # Module-scoped so one app object serves a whole module and the
    # memoised sweep helpers in the consumer modules can key on it.
    # Scope alone renders nothing fewer -- the memo dicts do that, and
    # the un-memoised per-test loops still render once per test. Module
    # scope does rule out the function-scoped `monkeypatch` fixture;
    # MonkeyPatch.context() is the same setenv/undo, held open over the
    # module's lifetime instead of one test's.
    #
    # Consequence of that wider window: every FLAG_TOOL_* stays on for
    # the rest of the module once this fixture is built, where the
    # function-scoped version undid them after each test. A module whose
    # other fixtures flag only a subset on will see all of them from
    # that point. Both consumers happen to define those tests earlier in
    # the file, which is collection order, but nothing enforces it.
    #
    # Ordering note: a module-scoped fixture is built before the
    # function-scoped isolate_supabase blanks the credentials. That is
    # safe because create_app() captures no Supabase client --
    # get_service_client() is called inside inject_workspace_context,
    # a context processor, so it runs per render instead.
    with pytest.MonkeyPatch.context() as mp:
        for slug in slugs:
            mp.setenv(flag_name(slug), "on")
        mp.setenv("SESSION_SECRET_KEY", "test-secret")
        flask_app = app_module.create_app()
        flask_app.config["TESTING"] = True
        yield flask_app, slugs
