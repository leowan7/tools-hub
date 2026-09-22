"""``log_event``'s worker must not outlive the test that spawned it.

A full run at 1cb4d20 emitted a ``PytestUnhandledThreadExceptionWarning`` whose
traceback ran ``shared/events.py`` ``_write`` -> ``get_service_client`` ->
``create_client`` -> the ``tests/conftest.py`` guard, attributed to
``<no active test>``: the fire-and-forget insert thread was still running after
its test's ``isolate_supabase`` teardown had put the real ``.env`` credentials
back, so it resolved the REAL production project and only the guard turned it
around. Timing-dependent -- a 339.70 s run of the same tree emitted it where a
412.79 s run did not.

Distinct from the vector PR #311 (df5b008) closed. There a module-scoped fixture
was built before a function-scoped ``isolate_supabase`` mark could fire. Here the
mark fired correctly and the thread simply outlived it.

The close is in ``log_event``: whether a client is obtainable is decided on the
CALLER's thread, so under isolation no worker is created to outlive anything.
The guard stays exactly as it was -- it is the backstop, not the fix.
"""

from __future__ import annotations

import pytest

from shared import events
from shared.credits import get_service_client, service_client_available

_FAKE_URL = "https://example.supabase.co"

_SUPABASE_ENV = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_KEY",
    "SUPABASE_ANON_KEY",
)


class _ShedEverything:
    """Stand-in for ``_EVENT_INFLIGHT`` that counts and refuses.

    Counting the ``acquire`` is how these tests read the decision without
    starting a thread: ``log_event`` reaches the semaphore only after the
    credential gate lets it through, and a refused ``acquire`` makes it return
    before ``threading.Thread`` is ever constructed. A gate moved BELOW the
    spawn would leave ``attempts`` unchanged and fail the tests here.
    """

    def __init__(self) -> None:
        self.attempts = 0

    def acquire(self, blocking: bool = True) -> bool:  # noqa: FBT001, FBT002
        self.attempts += 1
        return False

    def release(self) -> None:
        """A no-op on purpose.

        A double that RAISED here could reproduce the very defect this file
        pins. _write in shared/events.py resolves _EVENT_INFLIGHT at
        call time in a finally that sits OUTSIDE its except Exception,
        so a worker still in flight from an earlier test would reach this
        object and propagate on a non-pytest thread. After the gate nothing
        should be in flight, but the double must not be what puts it there.
        """


def _count_gate_passes(monkeypatch) -> _ShedEverything:
    counter = _ShedEverything()
    monkeypatch.setattr(events, "_EVENT_INFLIGHT", counter)
    return counter


def test_no_worker_is_spawned_without_credentials(isolate_supabase, monkeypatch):
    """The regression. Blank credentials must stop ``log_event`` BEFORE it
    detaches, so nothing is left running to read the environment back."""
    counter = _count_gate_passes(monkeypatch)

    events.log_event(event_type="page_view", session_id="s-1", path="/p")

    assert counter.attempts == 0, (
        "log_event got past the credential gate under isolate_supabase; a "
        "worker would be spawned that resolves credentials on its own thread, "
        "after this test's teardown restores the real .env values"
    )


def test_a_worker_is_still_spawned_when_configured(isolate_supabase, monkeypatch):
    """The positive control.

    Without it the test above passes for any reason at all, including a
    ``log_event`` broken into a no-op. A configured deployment must still hand
    the insert off the request thread -- that is the whole point of the module.
    """
    monkeypatch.setenv("SUPABASE_URL", _FAKE_URL)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "not-a-real-service-key")
    counter = _count_gate_passes(monkeypatch)

    events.log_event(event_type="page_view", session_id="s-1", path="/p")

    assert counter.attempts == 1, (
        "a configured deployment must still detach the insert; the gate is a "
        "credential check, not a kill switch"
    )


# URL, SERVICE_ROLE, KEY, ANON -> can get_service_client() build a client?
_MATRIX = [
    pytest.param(_FAKE_URL, "svc", "", "", True, id="service-role-key"),
    pytest.param(_FAKE_URL, "", "anon", "", True, id="falls-back-to-SUPABASE_KEY"),
    pytest.param(_FAKE_URL, "", "", "anon", True, id="falls-back-to-ANON_KEY"),
    pytest.param(_FAKE_URL, "", "", "", False, id="url-but-no-key"),
    pytest.param("", "svc", "anon", "anon", False, id="keys-but-no-url"),
    pytest.param("", "", "", "", False, id="nothing-set"),
]


@pytest.mark.parametrize(("url", "service", "key", "anon", "expected"), _MATRIX)
def test_predicate_agrees_with_get_service_client(
    monkeypatch, url, service, key, anon, expected
):
    """``service_client_available`` reads the same env ``get_service_client``
    does, in a second place, so it can drift. This pins the two together.

    The conftest guard supplies the observation: reaching ``create_client`` at
    all raises the refusal, so a raise means a client WOULD have been built and
    a ``None`` means it would not. Nothing here weakens the guard -- the raise
    is what the assertion reads.
    """
    pytest.importorskip("supabase")  # with it absent the guard steps aside
    for name, value in zip(_SUPABASE_ENV, (url, service, key, anon), strict=True):
        monkeypatch.setenv(name, value)

    assert service_client_available() is expected

    if expected:
        with pytest.raises(BaseException, match="REAL Supabase client"):
            get_service_client()
    else:
        assert get_service_client() is None
