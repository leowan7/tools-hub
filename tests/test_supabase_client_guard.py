"""The real-client guard in ``conftest`` must actually fire.

A guard that never fires is indistinguishable from an inert one, so this file
builds a client on purpose and asserts the refusal.
"""

from __future__ import annotations

import pytest

supabase = pytest.importorskip("supabase")

_FAKE_URL = "https://example.supabase.co"


def test_building_a_real_client_is_refused():
    with pytest.raises(AssertionError, match="REAL Supabase client"):
        supabase.create_client(_FAKE_URL, "not-a-real-key")


def test_the_refusal_names_the_running_test():
    with pytest.raises(AssertionError, match="test_the_refusal_names_the_running_test"):
        supabase.create_client(_FAKE_URL, "not-a-real-key")


def test_the_rls_tripwire_is_exempt(monkeypatch):
    """``tests/test_rls.py`` must still reach the real constructor.

    Empty credentials make the real ``create_client`` fail on its own, so the
    fall-through is proven without constructing anything that could talk to
    the production project.
    """
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_rls.py::test_x (call)")
    with pytest.raises(Exception) as excinfo:  # noqa: PT011
        supabase.create_client("", "")
    assert "REAL Supabase client" not in str(excinfo.value)
