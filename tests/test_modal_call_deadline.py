"""Every Modal gRPC round trip made from a request handler has a deadline.

The Modal SDK applies none of its own. In modal 1.4.2 the default retry policy
is ``Retry()`` with ``attempt_timeout=None`` and ``total_timeout=None``, the
timeout finally handed to the RPC is built only from those two, and grpclib's
keepalive is off, so a half-open channel is never detected. Only the handshake
is bounded. A stale channel therefore means the handshake succeeds, the request
is written, and the response never arrives — the call blocks forever.

``ModalClient.submit`` and ``.poll`` are reachable from
``blueprints/jobs.py:529``, ``blueprints/tools.py:1912`` and
``blueprints/jobs.py:310``. This repo has already taken that outage once, from
the same cause on the same host: see the note in ``shared/supabase_client.py``
for 2026-06-10.

Today gunicorn's sync worker is killed at ``timeout``, which contains the
damage at the cost of every other request on that worker. That backstop
disappears the moment the worker class becomes threaded, and it was never a fix
— an unbounded blocking call on a request path is a bug under any worker model.

Why the fake is shaped the way it is
------------------------------------
The round-2 version of this file used a fake whose ``Function.from_name`` and
``FunctionCall.from_id`` both blocked. Those are the FIRST call in every
method, so ``spawn``, ``get`` and ``cancel`` were never reached and an
independent mutation set unwrapped each of them in turn without a single test
going red. A fake that can only wedge at the front door cannot test the rooms
behind it. ``_SelectiveModal`` takes the wedge point by name and returns
working objects for every call before it, so each call site is reachable and
each is asserted reached.

    pytest tests/test_modal_call_deadline.py -v
"""

from __future__ import annotations

import threading
import time

import pytest

from gpu import modal_client
from gpu.modal_client import ModalCallTimeout, ModalClient, _bounded_modal_call

# Every gRPC hop the client makes, in the order each method makes them.
WEDGE_POINTS = ("from_name", "spawn", "from_id", "get", "cancel")

# How long a wedged call blocks before giving up. Not "forever": under a
# mutation that unwraps a call site it is the REQUEST thread that parks here,
# so forever would hang the suite instead of failing it. Comfortably above the
# bounded deadline the tests run with (0.3 s) and comfortably below any
# patience a human has.
WEDGE_CEILING_SEC = 5.0

# What a healthy ``fc.get()`` hands back — enough for
# ``_interpret_pipeline_return`` to call it a success.
HEALTHY_RESULT = {
    "exit_code": 0,
    "smoke_result": {"status": "COMPLETED", "sequences": ["AAAA"]},
}


class _Handle:
    """Stands in for both a ``Function`` and a ``FunctionCall``.

    Only ``object_id`` is ever read off the real objects, so one class covers
    both and keeps the fake small.
    """

    object_id = "fc-fake-0001"

    def __init__(self, modal: "_SelectiveModal"):
        self._modal = modal

    def spawn(self, _payload):
        return self._modal._call("spawn", self)

    def get(self, timeout):
        return self._modal._call("get", HEALTHY_RESULT)

    def cancel(self):
        return self._modal._call("cancel", None)


class _SelectiveModal:
    """A fake ``modal`` module that wedges at exactly ONE named call.

    Deliberately blocks rather than raises: a call that raises was never the
    failure mode. The failure mode is a call that succeeds at the handshake
    and then never returns.

    ``wedge_at=None`` wedges nowhere, which is the control that proves the
    pre-wedge calls really do return usable objects — without it a fake that
    silently broke ``from_name`` would make every wedge test pass for the
    wrong reason.
    """

    def __init__(self, wedge_at: str | None):
        assert wedge_at is None or wedge_at in WEDGE_POINTS, wedge_at
        self.wedge_at = wedge_at
        self.reached: list[str] = []  # list.append is atomic under the GIL
        self.released = threading.Event()

    def _call(self, name: str, value):
        self.reached.append(name)
        if name == self.wedge_at:
            self.released.wait(timeout=WEDGE_CEILING_SEC)
            raise AssertionError(f"{name} was released only at teardown")
        return value

    @property
    def Function(self):  # noqa: N802 — mirrors the modal API
        outer = self

        class _Function:
            @staticmethod
            def from_name(*_args, **_kwargs):
                return outer._call("from_name", _Handle(outer))

        return _Function

    @property
    def FunctionCall(self):  # noqa: N802 — mirrors the modal API
        outer = self

        class _FunctionCall:
            @staticmethod
            def from_id(_function_call_id):
                return outer._call("from_id", _Handle(outer))

        return _FunctionCall


@pytest.fixture
def short_deadline(monkeypatch):
    """Shrink the cap so a wedge is provable in a fraction of a second."""
    monkeypatch.setattr(modal_client, "_MODAL_CALL_TIMEOUT_SEC", 0.3)
    return 0.3


@pytest.fixture
def wedge(monkeypatch):
    """Install a fake modal wedged at a named call site.

    Usage: ``fake = wedge("spawn")``.
    """
    installed: list[_SelectiveModal] = []

    def _install(wedge_at: str | None) -> _SelectiveModal:
        fake = _SelectiveModal(wedge_at)
        monkeypatch.setattr(modal_client, "_import_modal", lambda: fake)
        installed.append(fake)
        return fake

    yield _install
    for fake in installed:
        fake.released.set()


def _submit(client: ModalClient):
    return client.submit(
        "mpnn", "smoke", {},
        job_id="j-1", job_token="t-1", webhook_url="",
    )


class TestTheHelper:
    def test_a_wedged_call_raises_instead_of_blocking(self, short_deadline):
        started = time.monotonic()
        with pytest.raises(ModalCallTimeout):
            _bounded_modal_call("wedge", lambda: time.sleep(30))
        elapsed = time.monotonic() - started
        assert elapsed < short_deadline * 6, (
            f"took {elapsed:.2f}s — the deadline is not being applied"
        )

    def test_the_return_value_is_passed_through(self):
        assert _bounded_modal_call("ok", lambda: {"a": 1}) == {"a": 1}

    def test_an_exception_is_re_raised_on_the_caller(self):
        """Not swallowed, and not reshaped: callers already have error paths
        for whatever Modal raises, and they must keep working."""
        class Boom(RuntimeError):
            pass

        with pytest.raises(Boom):
            _bounded_modal_call("boom", lambda: (_ for _ in ()).throw(Boom("x")))


class TestTheShippedDeadline:
    """The number is bracketed on both sides, and neither side is arbitrary."""

    def test_it_is_above_modals_own_channel_connect_budget(self):
        """Below Modal's budget, this deadline preempts the SDK's own retry.

        ``modal/_utils/grpc_utils.py`` decorates its channel-connect helper
        with ``@retry(n_attempts=18, ..., total_timeout=63.0)`` — verified
        identical in 1.4.2 (``connect_channel``) and 1.5.4
        (``create_channel_with_fallbacks``); establishing the
        channel is allowed 63 s, by design, for exactly the transient blips
        this deadline exists to survive. Any of our calls can be the one that
        triggers a connect. Cap below that and a blip Modal would have ridden
        out becomes a hard submit failure and a released wallet hold.

        The 63 is read out of the INSTALLED SDK rather than hardcoded, so an
        upstream change to the budget fails here instead of rotting.
        """
        budget = _modal_connect_budget_sec()
        assert budget == 63.0, (
            f"Modal's connect budget moved to {budget}s — re-derive "
            "_MODAL_CALL_TIMEOUT_SEC against the new number"
        )
        assert modal_client._MODAL_CALL_TIMEOUT_SEC > budget, (
            f"{modal_client._MODAL_CALL_TIMEOUT_SEC}s is at or below Modal's "
            f"{budget}s connect retry budget — the SDK is designed to still "
            "be retrying at that point"
        )

    def test_it_is_below_the_gunicorn_worker_watchdog(self):
        """Above the watchdog, the worker dies before the call gives up —
        which takes out every other in-flight request on that worker, the
        exact outcome bounding the call exists to avoid.

        ``gunicorn.conf.py:164`` defaults ``timeout`` to 120.
        """
        assert modal_client._MODAL_CALL_TIMEOUT_SEC < 120

    def test_each_method_makes_one_bounded_call_not_two(self):
        """The bracket only holds per CALL. A method that bounds two hops
        separately has twice the budget as its worst case — at 90 s that is
        180 s, straight through the 120 s watchdog — and a ``spawn`` cut on
        its own deadline can still land, billing a GPU job with no job row
        tracking it.
        """
        import ast
        import inspect
        import textwrap

        for name in ("submit", "poll", "cancel"):
            src = textwrap.dedent(inspect.getsource(getattr(ModalClient, name)))
            tree = ast.parse(src)
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_bounded_modal_call"
            ]
            assert len(calls) == 1, (
                f"ModalClient.{name} makes {len(calls)} bounded calls; the "
                "deadline bracket is per call, so it must make exactly 1"
            )


def _modal_connect_budget_sec() -> float:
    """Read the channel-connect ``total_timeout`` out of the installed SDK.

    ``@retry`` closes over its arguments rather than storing them, so this
    reads the closure. Documented CPython introspection, and the alternative
    is a hardcoded 63 that goes stale in silence.

    The helper is looked up under SEVERAL names because it has been renamed
    upstream already and requirements.txt pins only ``modal>=1.4,<2.0``, so
    the whole range has to work: 1.4.2 has ``connect_channel`` and no
    ``create_channel_with_fallbacks``, 1.5.4 has exactly the reverse. Both
    carry ``total_timeout=63.0``, so the shipped deadline is unaffected by
    the rename -- which is the point of reading it rather than trusting it.

    ``create_channel`` is deliberately NOT a candidate even though it exists
    in both and is what an AttributeError on the old name suggests. It closes
    over ``total_timeout=None``, so accepting it would turn a rename into a
    silent "no budget" instead of a failure.

    A name this does not know about FAILS rather than skipping: skipping
    would let an upstream budget change through as a green suite, which is
    the exact rot this function exists to prevent.
    """
    grpc_utils = pytest.importorskip("modal._utils.grpc_utils")
    candidates = ("create_channel_with_fallbacks", "connect_channel")
    for name in candidates:
        fn = getattr(grpc_utils, name, None)
        if fn is not None:
            break
    else:
        pytest.fail(
            f"modal._utils.grpc_utils has none of {candidates} -- the "
            "channel-connect helper was renamed again. Re-read the SDK and "
            "re-check that gpu/modal_client.py's deadline still clears its "
            "total_timeout."
        )
    closure = dict(zip(
        fn.__code__.co_freevars,
        (cell.cell_contents for cell in fn.__closure__ or ()),
    ))
    assert "total_timeout" in closure, (
        "modal's @retry decorator no longer closes over total_timeout — "
        f"re-read grpc_utils.{name}"
    )
    return closure["total_timeout"]


class TestEveryCallSiteIsBounded:
    """The helper existing is not the guard. Each hop reaching it is.

    One test per hop, each asserting the fake actually got that far. Round 2
    had two of these five and believed it had all of them.
    """

    def test_submit_is_bounded_at_from_name(self, wedge, short_deadline):
        fake = wedge("from_name")
        started = time.monotonic()
        with pytest.raises(RuntimeError):
            _submit(ModalClient())
        elapsed = time.monotonic() - started
        assert fake.reached == ["from_name"]
        assert elapsed < short_deadline * 6, (
            f"submit blocked {elapsed:.2f}s — Function.from_name is unbounded"
        )

    def test_submit_is_bounded_at_spawn(self, wedge, short_deadline):
        """The hop that actually costs money. A spawn that lands after we gave
        up is a billed GPU job with no job row, so it must be inside the same
        budget as the lookup, not outside it."""
        fake = wedge("spawn")
        started = time.monotonic()
        with pytest.raises(RuntimeError):
            _submit(ModalClient())
        elapsed = time.monotonic() - started
        assert fake.reached == ["from_name", "spawn"], (
            f"the wedge never reached spawn: {fake.reached}"
        )
        assert elapsed < short_deadline * 6, (
            f"submit blocked {elapsed:.2f}s — fn.spawn is unbounded"
        )

    def test_poll_is_bounded_at_from_id(self, wedge, short_deadline):
        fake = wedge("from_id")
        started = time.monotonic()
        result = ModalClient().poll("fc-real-looking-id")
        elapsed = time.monotonic() - started
        assert fake.reached == ["from_id"]
        assert elapsed < short_deadline * 6, (
            f"poll blocked {elapsed:.2f}s — FunctionCall.from_id is unbounded"
        )
        assert result["status"] == "error"

    def test_poll_is_bounded_at_get(self, wedge, short_deadline):
        fake = wedge("get")
        started = time.monotonic()
        result = ModalClient().poll("fc-real-looking-id")
        elapsed = time.monotonic() - started
        assert fake.reached == ["from_id", "get"], (
            f"the wedge never reached get: {fake.reached}"
        )
        assert elapsed < short_deadline * 6, (
            f"poll blocked {elapsed:.2f}s — fc.get is unbounded"
        )
        assert result["status"] == "error"

    def test_cancel_is_bounded_at_from_id(self, wedge, short_deadline):
        fake = wedge("from_id")
        started = time.monotonic()
        result = ModalClient().cancel("fc-real-looking-id")
        elapsed = time.monotonic() - started
        assert fake.reached == ["from_id"]
        assert elapsed < short_deadline * 6
        assert result["ok"] is False

    def test_cancel_is_bounded_at_cancel(self, wedge, short_deadline):
        fake = wedge("cancel")
        started = time.monotonic()
        result = ModalClient().cancel("fc-real-looking-id")
        elapsed = time.monotonic() - started
        assert fake.reached == ["from_id", "cancel"], (
            f"the wedge never reached cancel: {fake.reached}"
        )
        assert elapsed < short_deadline * 6, (
            f"cancel blocked {elapsed:.2f}s — fc.cancel is unbounded"
        )
        assert result["ok"] is False


class TestAWedgedChannelIsNotAHealthyJob:
    """``ModalCallTimeout`` must be caught BEFORE Modal's own TimeoutError.

    This is an ordering property in ``poll``, and ordering properties fail
    silently: swap the clauses and the code still parses, still runs, still
    returns a well-formed dict — one that says the job is fine.
    """

    def test_our_timeout_is_a_kind_of_timeout_error(self):
        """The reason ordering matters at all. If this stopped being true the
        two clauses would be independent and the ordering free."""
        assert issubclass(ModalCallTimeout, TimeoutError)

    def test_a_wedged_get_reports_error_not_running(self, wedge, short_deadline):
        """With the clause: 'error'. Without it, ``ModalCallTimeout`` falls
        into ``except TimeoutError`` and a permanently dead channel is
        reported as a healthy running job, forever, on every poll."""
        fake = wedge("get")
        result = ModalClient().poll("fc-real-looking-id")
        assert fake.reached == ["from_id", "get"]
        assert result["status"] == "error", (
            "a wedged channel was reported as %r — the ModalCallTimeout "
            "clause is not being reached before `except TimeoutError`"
            % result["status"]
        )
        assert result["error"], "an error status with no error string"

    def test_modals_own_not_finished_yet_is_still_running(self, monkeypatch):
        """The other half of the pair. ``fc.get(timeout=0)`` raises the
        BUILTIN TimeoutError to mean 'still computing' (modal 1.4.2
        ``_functions.py:327``; ``modal.exception.TimeoutError`` is a different
        class and is not what is raised here). That one must stay 'running'.
        """
        class _FC:
            @staticmethod
            def from_id(_id):
                class _Call:
                    @staticmethod
                    def get(timeout):
                        raise TimeoutError("not finished yet")
                return _Call()

        monkeypatch.setattr(
            modal_client, "_import_modal",
            lambda: type("M", (), {"FunctionCall": _FC}),
        )
        assert ModalClient().poll("fc-real")["status"] == "running"


class TestNothingElseChanged:
    """The bound must be invisible on every path that already worked."""

    def test_the_offline_stub_still_short_circuits(self, monkeypatch):
        monkeypatch.setattr(modal_client, "_import_modal", lambda: None)
        out = ModalClient().submit(
            "mpnn", "smoke", {},
            job_id="j-2", job_token="t-2", webhook_url="",
        )
        assert out["function_call_id"].startswith("fc-stub-")

    def test_a_stub_id_still_polls_as_running(self):
        assert ModalClient().poll("fc-stub-x")["status"] == "running"

    def test_an_unwedged_client_submits_polls_and_cancels(self, wedge):
        """The control. Proves the pre-wedge hops return usable objects, so a
        wedge test that passes is passing because of the wedge."""
        fake = wedge(None)
        assert _submit(ModalClient())["function_call_id"] == "fc-fake-0001"
        assert ModalClient().poll("fc-real")["status"] == "succeeded"
        assert ModalClient().cancel("fc-real")["ok"] is True
        assert fake.reached == [
            "from_name", "spawn", "from_id", "get", "from_id", "cancel",
        ]
