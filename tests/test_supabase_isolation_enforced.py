"""The collection gate in tests/conftest.py, and proof it can fail.

The gate refuses a run in which a test reaches the Flask app without Supabase
isolation. A green suite is no evidence for it on its own: the suite is
currently clean, so the gate returns "no offenders" whether it works or is
inert. Every assertion here therefore either feeds the gate an offender it
must flag, or reads state only a gate pytest actually invoked could have set.
"""

from __future__ import annotations

import ast
import pathlib
import types

import pytest


def _live_conftest(config):
    """The conftest module object pytest loaded, not a fresh import of it.

    A second import would carry its own ``_COLLECTION_GATE_SAW = 0`` and make
    the wiring assertion below unfalsifiable.
    """
    for plugin in config.pluginmanager.get_plugins():
        path = (getattr(plugin, "__file__", "") or "").replace("\\", "/")
        if path.endswith("tests/conftest.py"):
            return plugin
    raise AssertionError("tests/conftest.py is not registered as a pytest plugin")


@pytest.fixture
def ct(request):
    return _live_conftest(request.config)


def _item(path, fixturenames=(), nodeid="tests/fake.py::test_x"):
    return types.SimpleNamespace(
        path=path, fixturenames=tuple(fixturenames), nodeid=nodeid
    )


def _caller(tmp_path, name="calls_app.py"):
    f = tmp_path / name
    f.write_text("def create_app():\n    pass\n\n\napp = create_app()\n", encoding="utf-8")
    return f


# --- the predicate -------------------------------------------------------


def test_a_mention_is_not_a_call(ct):
    mention = ast.parse("'create_app() in a docstring'\n# create_app()\n")
    assert ct._builds_app(mention) is False
    assert ct._builds_app(ast.parse("create_app()")) is True


def test_an_attribute_call_counts(ct):
    """all_tools_app reaches the app as ``app_module.create_app()``."""
    assert ct._builds_app(ast.parse("app_module.create_app()")) is True


def test_app_building_fixtures_finds_the_real_one(ct):
    names = ct._app_building_fixtures(
        pathlib.Path(ct.__file__).read_text(encoding="utf-8")
    )
    assert "all_tools_app" in names
    assert "isolate_supabase" not in names


# --- the gate, fed an offender it must flag ------------------------------


def test_gate_flags_an_unisolated_caller(tmp_path, ct):
    """The positive control: without this the suite's 0 offenders prove nothing."""
    offenders = ct._unisolated([_item(_caller(tmp_path))], frozenset())
    assert len(offenders) == 1


@pytest.mark.parametrize("fixture", ["isolate_supabase", "isolate_supabase_module"])
def test_gate_clears_either_isolation_fixture(tmp_path, ct, fixture):
    offenders = ct._unisolated([_item(_caller(tmp_path), [fixture])], frozenset())
    assert offenders == {}


def test_a_misspelled_mark_still_reads_as_missing(tmp_path, ct):
    """The closure carries whatever the mark named, so a typo resolves to nothing."""
    offenders = ct._unisolated(
        [_item(_caller(tmp_path), ["isolate_supabse"])], frozenset()
    )
    assert len(offenders) == 1


def test_gate_flags_a_fixture_mediated_reacher(tmp_path, ct):
    """A file that never writes create_app still reaches one via all_tools_app."""
    plain = tmp_path / "no_call.py"
    plain.write_text("def test_x():\n    pass\n", encoding="utf-8")
    app_fixtures = frozenset({"all_tools_app"})

    assert ct._unisolated([_item(plain, ["all_tools_app"])], app_fixtures) != {}
    assert ct._unisolated(
        [_item(plain, ["all_tools_app", "isolate_supabase"])], app_fixtures
    ) == {}


def test_gate_ignores_a_file_that_never_reaches_the_app(tmp_path, ct):
    plain = tmp_path / "plain.py"
    plain.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    assert ct._unisolated([_item(plain)], frozenset()) == {}


# --- the gate is wired ---------------------------------------------------


def test_the_gate_actually_ran(ct):
    """Proves pytest invoked the hook in THIS session.

    Every assertion above calls ``_unisolated`` directly and would pass just
    the same if ``pytest_collection_modifyitems`` were misspelled and never
    called. This one reads the counter the hook itself sets.
    """
    assert ct._COLLECTION_GATE_SAW > 0
