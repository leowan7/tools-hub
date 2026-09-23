"""The real-client guard in ``conftest`` must actually fire.

A guard that never fires is indistinguishable from an inert one, so this file
builds a client on purpose and asserts the refusal.
"""

from __future__ import annotations

import pytest

supabase = pytest.importorskip("supabase")

_FAKE_URL = "https://example.supabase.co"


def test_building_a_real_client_is_refused():
    with pytest.raises(BaseException, match="REAL Supabase client") as excinfo:
        supabase.create_client(_FAKE_URL, "not-a-real-key")
    assert not isinstance(excinfo.value, Exception), (
        "the alarm must not be an Exception subclass -- every in-app build "
        "site catches Exception broadly and would swallow it"
    )


def test_the_refusal_names_the_running_test():
    with pytest.raises(BaseException, match="test_the_refusal_names_the_running_test"):
        supabase.create_client(_FAKE_URL, "not-a-real-key")


def test_the_rls_tripwire_is_exempt(monkeypatch):
    """``tests/test_rls.py`` must still reach the real constructor.

    Empty credentials make the real ``create_client`` fail on its own -- it
    refuses a blank URL before it ever reads the key, so nothing that could
    reach the production project is constructed. The ``pytest.raises`` below
    is the enforcement: a release that stopped refusing would fail here rather
    than quietly start building clients.
    """
    # not-a-citation: a fabricated pytest nodeid, not a reference to a symbol.
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_rls.py::test_x (call)")
    with pytest.raises(Exception) as excinfo:  # noqa: PT011
        supabase.create_client("", "")
    assert "REAL Supabase client" not in str(excinfo.value)


def test_the_alarm_is_not_swallowed_by_app_code(monkeypatch):
    """The alarm must survive the broad ``except Exception`` at every sink.

    ``shared.credits.get_service_client``,
    ``shared.supabase_client.get_supabase_client``,
    ``scout.handoff._get_service_client`` and
    ``scout.quota._get_service_client`` each wrap the constructor in
    ``except Exception:`` and return None, so an ``Exception`` alarm is caught
    by the very code it indicts and the forgetful test goes green holding a
    ``None`` client.

    Reaching the guard THROUGH a sink is the only way to see that. The direct
    ``create_client`` tests above cannot -- nothing catches anything in
    between, so they pass either way.
    """
    monkeypatch.setenv("SUPABASE_URL", _FAKE_URL)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "not-a-real-service-key")

    from shared.credits import get_service_client

    with pytest.raises(BaseException, match="REAL Supabase client"):
        get_service_client()
