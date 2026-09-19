"""Shared pytest configuration for tools-hub.

The web-UI CSRF guard (FIX M2, cso audit 2026-06-17) is ON by default in
every real deployment. The existing route-level tests POST to protected
endpoints without a CSRF token, so enforcement is disabled process-wide for
the test session here. The dedicated suite (tests/test_csrf_protection.py)
re-enables it per-test via monkeypatch to exercise the guard directly.
"""

from __future__ import annotations

import ast
import os
import pathlib

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

    Opt-in rather than autouse. ``pytest_collection_modifyitems`` at the foot
    of this file is what keeps the opt-in from lapsing: it refuses a run in
    which a test reaches the app without one of these two fixtures.
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
def all_tools_app(isolate_supabase_module):
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
    # function-scoped isolate_supabase blanks the credentials, so this
    # requests isolate_supabase_module to blank them for its own setup
    # too. create_app() does capture no Supabase client of its own --
    # get_service_client() is called inside inject_workspace_context, a
    # context processor, so it runs per render instead -- but that is a
    # property of create_app() today rather than something a fixture
    # should rest on. pytest_collection_modifyitems below requires the
    # twin on every app fixture of a scope wider than function.
    with pytest.MonkeyPatch.context() as mp:
        for slug in slugs:
            mp.setenv(flag_name(slug), "on")
        mp.setenv("SESSION_SECRET_KEY", "test-secret")
        flask_app = app_module.create_app()
        flask_app.config["TESTING"] = True
        yield flask_app, slugs


# --- Collection gate: the isolation mark cannot quietly lapse ------------

_ISOLATION_FIXTURES = frozenset({"isolate_supabase", "isolate_supabase_module"})

# Number of items the gate last inspected. Asserted non-zero by
# `test_the_gate_actually_ran` in tests/test_supabase_isolation_enforced.py:
# a hook pytest never calls would leave this at 0 while every other
# assertion in that file still passed.
_COLLECTION_GATE_SAW = 0


def _builds_app(node: ast.AST) -> bool:
    """True when ``node`` contains a real ``create_app(...)`` call.

    AST rather than substring: files under ``tests/`` name ``create_app`` in a
    docstring or comment without calling it, and a mention reaches no app.

    Enforced by ``test_a_mention_is_not_a_call`` in
    tests/test_supabase_isolation_enforced.py.
    """
    return any(
        isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "create_app")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "create_app")
        )
        for n in ast.walk(node)
    )


def _app_building_fixtures(source: str) -> frozenset[str]:
    """Fixture names defined in ``source`` whose own body builds an app.

    A file that never writes ``create_app`` still reaches one by requesting
    such a fixture -- ``all_tools_app`` above is the current example. A file
    that only requests it would look clean to a caller-only check.

    Enforced by ``test_app_building_fixtures_finds_the_real_one`` and
    ``test_gate_flags_a_fixture_mediated_reacher`` in
    tests/test_supabase_isolation_enforced.py.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any("fixture" in ast.unparse(d) for d in node.decorator_list)
        and _builds_app(node)
    )


def _fixture_scope(node: ast.AST) -> str:
    """The ``scope=`` on a fixture decorator, or pytest's default."""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "scope" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
    return "function"


def _module_functions(source: str) -> dict[str, ast.AST]:
    """Every ``def`` in ``source`` by name, methods and nested defs included.

    The two walks below follow bare names -- a call to ``_build()``, an
    argument named ``tools_app`` -- and this is what those names resolve
    against. A later definition wins, as it does at module level in Python.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_names(node: ast.AST) -> set[str]:
    """Bare function names called anywhere inside ``node``."""
    return {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def _reaches_app(node: ast.AST, funcs: dict, seen=None) -> bool:
    """``_builds_app``, following calls into helpers defined alongside it.

    A fixture whose body is ``return _build()`` reaches the app just as surely
    as one that names ``create_app``, and the gate exists to refuse that shape.
    ``seen`` terminates on a recursive or mutually recursive helper.

    Enforced by ``test_a_delegating_fixture_is_still_early`` in
    tests/test_supabase_isolation_enforced.py.
    """
    seen = set() if seen is None else seen
    if node.name in seen:
        return False
    seen.add(node.name)
    if _builds_app(node):
        return True
    return any(
        _reaches_app(funcs[name], funcs, seen)
        for name in _called_names(node) & funcs.keys()
    )


def _argnames(node: ast.AST) -> set[str]:
    """Every parameter name pytest will resolve as a fixture request.

    Mirrors the rule pytest itself applies in
    ``_pytest.compat.getfuncargnames``: a parameter is a fixture request when
    it is positional-or-keyword or keyword-only AND has no default. Both
    halves of that rule carry weight, in opposite directions.

    Keyword-only must be counted. Missing it would refuse a correctly isolated
    fixture, and since the hook raises ``UsageError`` that refusal aborts the
    whole session rather than one file.

    A DEFAULTED parameter must not be counted, and neither must a
    positional-only one: pytest passes the former its default and rejects the
    latter, so in neither shape does the twin actually run. Counting one is the
    dangerous direction -- it certifies an app fixture that nothing isolates.

    Enforced by ``test_a_keyword_only_twin_counts`` and
    ``test_a_twin_pytest_will_not_resolve_is_not_protection`` in
    tests/test_supabase_isolation_enforced.py.
    """
    args = node.args
    required = args.args[: max(0, len(args.args) - len(args.defaults))]
    required += [k for k, d in zip(args.kwonlyargs, args.kw_defaults) if d is None]
    return {a.arg for a in required}


def _reaches_twin(node: ast.AST, funcs: dict, seen=None) -> bool:
    """True when ``isolate_supabase_module`` is in this fixture's own closure.

    Follows fixture arguments, so a fixture handed the twin through an
    intermediate fixture counts as isolated -- pytest builds the whole chain
    before the fixture that depends on it. Nothing here checks the scope of an
    intermediate: pytest raises ScopeMismatch when a wider-scoped fixture
    requests a narrower one, so every fixture reachable this way is already
    module-scoped or wider.

    Enforced by ``test_the_twin_through_an_intermediate_clears_it`` in
    tests/test_supabase_isolation_enforced.py.
    """
    seen = set() if seen is None else seen
    if node.name in seen:
        return False
    seen.add(node.name)
    args = _argnames(node)
    if "isolate_supabase_module" in args:
        return True
    return any(_reaches_twin(funcs[a], funcs, seen) for a in args & funcs.keys())


def _early_app_fixtures(source: str, shared: dict | None = None) -> frozenset[str]:
    """App-building fixtures a function-scoped mark cannot protect.

    pytest builds a module- or class-scoped fixture BEFORE the function-scoped
    ``isolate_supabase``, so a module-level mark does not cover what that
    fixture does during its own setup -- the #296 mechanism, and the reason
    ``isolate_supabase_module`` exists. The twin only helps as a real
    dependency edge, so the fixture has to reach it through its arguments; a
    mark on the tests cannot order two module-scoped fixtures against each
    other.

    Returned names are the wider-scoped app fixtures DEFINED in ``source`` that
    do not reach the twin. ``shared`` supplies definitions the file uses
    without defining -- conftest's own, when reading a test file -- so a chain
    that leaves the file is still followed, while an offender is still
    reported against the file that declares it.

    Ceiling: both walks resolve bare names against ``source`` plus ``shared``.
    A fixture that reaches the app or the twin through an import, a class
    attribute or a plugin is not seen.
    """
    own = _module_functions(source)
    funcs = {**(shared or {}), **own}
    return frozenset(
        name
        for name, node in own.items()
        if any("fixture" in ast.unparse(d) for d in node.decorator_list)
        and _fixture_scope(node) != "function"
        and _reaches_app(node, funcs)
        and not _reaches_twin(node, funcs)
    )


_TESTS_DIR = pathlib.Path(__file__).parent
_CONFTEST_SRC = pathlib.Path(__file__).read_text(encoding="utf-8")
_CONFTEST_FUNCS = _module_functions(_CONFTEST_SRC)
_APP_BUILDING_FIXTURES = _app_building_fixtures(_CONFTEST_SRC)
_EARLY_APP_FIXTURES = _early_app_fixtures(_CONFTEST_SRC)


def _under_tests(path) -> bool:
    """True when ``path`` sits under this conftest's directory.

    A conftest's fixtures are not visible outside its own directory, and a
    repo-root run also collects ``tools/library_planner/tests``. Flagging a
    file there would print a remedy that cannot resolve. Enforced by
    ``test_a_file_outside_the_tests_directory_is_not_policed``.
    """
    if path is None:
        return False
    try:
        pathlib.Path(path).relative_to(_TESTS_DIR)
    except ValueError:
        return False
    return True


def _unisolated(items, app_fixtures, cache=None, early_fixtures=frozenset()):
    """Map file -> (nodeid, reason) for items that reach the app unisolated.

    Reads ``item.fixturenames`` -- the closure pytest computed -- rather than
    the mark text, so a per-class mark or a fixture chain that requests the
    module twin counts, while a mark whose name resolves to neither fixture
    still reads as missing.

    Two ways of failing, and the early-fixture one is tested FIRST because
    those files DO carry the mark -- it is simply built too late to cover the
    fixture's own setup.

    Enforced by ``test_gate_clears_either_isolation_fixture``,
    ``test_a_misspelled_mark_still_reads_as_missing`` and
    ``test_the_mark_does_not_clear_an_early_fixture`` in
    tests/test_supabase_isolation_enforced.py.
    """
    cache = {} if cache is None else cache
    offenders = {}
    for item in items:
        names = set(getattr(item, "fixturenames", ()))
        path = getattr(item, "path", None)
        key = str(path)
        if path is not None and key not in cache:
            try:
                src = pathlib.Path(path).read_text(encoding="utf-8")
                cache[key] = (
                    _builds_app(ast.parse(src)),
                    _early_app_fixtures(src, _CONFTEST_FUNCS),
                )
            except (OSError, SyntaxError, ValueError):
                cache[key] = (False, frozenset())
        builds, file_early = cache[key] if path is not None else (False, frozenset())

        early = sorted(names & (early_fixtures | file_early))
        if early:
            offenders.setdefault(
                key,
                (
                    item.nodeid,
                    f"{early[0]} builds the app before a function-scoped mark "
                    "fires; it must reach isolate_supabase_module through its "
                    "own arguments",
                ),
            )
            continue
        if names & _ISOLATION_FIXTURES:
            continue
        if names & app_fixtures or builds:
            offenders.setdefault(
                key, (item.nodeid, "no isolation fixture in the closure")
            )
    return offenders


def pytest_collection_modifyitems(config, items):
    """Refuse the run when a test reaches the app without Supabase isolation.

    ``app.py`` calls ``load_dotenv()`` at import, so on a machine holding the
    repo ``.env`` a test that builds the app runs with REAL service-role
    credentials. ``_forbid_real_supabase_clients`` above is the runtime
    backstop, but it only fires once a code path actually constructs a client;
    a file missing the mark stays quiet until one does. Four hand sweeps
    (#296, #299, #302, #308) closed that gap by hand and it reopened each
    time.

    Two failure modes are refused: no isolation fixture in the closure at all,
    and an app fixture of a scope wider than function that does not request
    ``isolate_supabase_module`` -- the second is the one that a green suite
    and a module-level mark both look clean under.

    Ceilings: the gate sees only the items collected, so a single-file run
    polices that file alone and the full run is what binds; and it polices
    only files under this directory, because the fixtures it prescribes are
    not visible outside it.
    """
    global _COLLECTION_GATE_SAW
    scoped = [item for item in items if _under_tests(getattr(item, "path", None))]
    _COLLECTION_GATE_SAW = len(scoped)
    offenders = _unisolated(
        scoped, _APP_BUILDING_FIXTURES, early_fixtures=_EARLY_APP_FIXTURES
    )
    if offenders:
        listing = "\n".join(
            f"  {path}\n      {reason}\n      e.g. {nodeid}"
            for path, (nodeid, reason) in sorted(offenders.items())
        )
        raise pytest.UsageError(
            f"{len(offenders)} test file(s) reach the Flask app without Supabase "
            f"isolation:\n{listing}\n\n"
            "Apply whichever remedy the line above names: either "
            '`pytestmark = pytest.mark.usefixtures("isolate_supabase")` at module '
            "level, or -- for a MODULE- or CLASS-scoped fixture that builds the "
            "app -- that fixture reaching `isolate_supabase_module` through "
        "its own arguments, "
            "because a function-scoped mark is built too late for it."
        )
