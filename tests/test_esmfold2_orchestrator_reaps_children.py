"""The ESMFold2-design orchestrator must not leave children billing.

``run_tool`` fans out up to ``N_SEEDS_MAX`` H100 workers, each bounded by its
own ``_MAX_SESSION_S``. Only the ORCHESTRATOR's FunctionCall id is persisted
(``shared.jobs.set_modal_call``), so nothing downstream can reach a child. Two
reachable triggers ended in a full refund with the GPUs still running:

  1. User cancel. ``ModalClient.cancel`` cancels the orchestrator only; the job
     classifies ``user_cancelled`` with ``gpu_seconds_used == 0`` and
     ``shared.jobs`` routes that to ``release_hold``.
  2. Orchestrator timeout while children still run. Swept later as
     ``no_progress_timeout``, which is in ``_REFUNDED_FAILURE_CLASSES``.

So every unreaped child is H100 time absorbed for zero revenue, bounded only by
the worker ceiling: at 64 seeds x 5400 s that is ~$835 of raw H100.

Most of these drive :func:`_gather_children` -- the wait/reap loop ``run_tool``
delegates to -- with fake FunctionCalls; a few call :func:`_reap_children`
directly to land an interrupt inside the reap itself. ``run_tool`` is a
``modal.Function`` and is not locally callable, which is why the loop is a
module-level helper, and why the two properties that live in ``run_tool``'s own
body (guarded spawn loops, no ``.remote()``) are checked over its AST instead.

Runs fully offline - no Modal, no GPU.
"""

from __future__ import annotations

import ast
import inspect

import modal.exception
import pytest

from tools.esmfold2_design import modal_app
from tools.esmfold2_design.modal_app import (
    _CHILD_WAIT_BUDGET_S,
    _MAX_SESSION_S,
    _ORCHESTRATOR_TIMEOUT_S,
    _REAP_ATTEMPTS,
    _gather_children,
)


class FakeCall:
    """A stand-in for ``modal.FunctionCall``.

    ``outcome`` is a dict (the child returned it), an exception instance (the
    child raised it), or ``None`` meaning "still running" -- which makes
    ``.get()`` raise ``TimeoutError`` however long it is given, exactly as
    Modal's does for a call that has not produced an output.
    """

    def __init__(self, outcome: object = None, *, object_id: str = "fc-fake"):
        self.outcome = outcome
        self.object_id = object_id
        self.cancelled = 0
        self.cancel_kwargs: list[dict] = []
        self.get_timeouts: list[float | None] = []

    def get(self, timeout: float | None = None):
        self.get_timeouts.append(timeout)
        if self.outcome is None:
            raise TimeoutError("Timeout exceeded")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def cancel(self, **kwargs):
        self.cancelled += 1
        self.cancel_kwargs.append(kwargs)


def _clock(ticks: list[float]):
    """A fake ``time.monotonic`` that walks ``ticks``, then holds the last."""

    seq = list(ticks)

    def _now() -> float:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _now


# -- the deadline path -------------------------------------------------------


def test_deadline_reaps_children_that_are_still_running() -> None:
    """A child that has not returned by the deadline is cancelled, not waited on.

    This is the orchestrator-timeout trigger. Without the cancel the child runs
    on to its own ``_MAX_SESSION_S`` on an H100 the user is refunded for.
    """
    done = FakeCall({"exit_code": 0}, object_id="fc-done")
    running = FakeCall(None, object_id="fc-running")

    successes, failures = _gather_children(
        [(0, done), (1, running)], budget_s=600.0, monotonic=_clock([0.0])
    )

    assert successes == [(0, {"exit_code": 0})]
    assert [s for s, _ in failures] == [1]
    assert running.cancelled == 1, (
        "the child still running at the deadline was never cancelled; it keeps "
        "billing an H100 for a job that is about to be refunded"
    )
    assert done.cancelled == 0, "a child that already returned needs no cancel"


def test_budget_is_shared_not_per_child() -> None:
    """A child behind the slow one is still harvested, not failed unread.

    The wait is sequential but the runs are not, so by the time the budget is
    gone the tail of the list is typically sitting on finished results. Failing
    them unread would throw away GPU time that was paid for and completed.
    """
    slow = FakeCall(None, object_id="fc-slow")
    finished = FakeCall({"exit_code": 0, "seed": 1}, object_id="fc-finished")

    # Clock jumps past the 600 s budget after the first child is waited on.
    successes, failures = _gather_children(
        [(0, slow), (1, finished)],
        budget_s=600.0,
        monotonic=_clock([0.0, 0.0, 9_999.0]),
    )

    assert successes == [(1, {"exit_code": 0, "seed": 1})], (
        "a child that had already finished was failed unread once the budget "
        "was spent"
    )
    assert [s for s, _ in failures] == [0]
    assert finished.get_timeouts == [0.0], (
        "an exhausted budget must still poll (timeout=0), not skip the child"
    )


def test_wait_is_bounded_by_the_remaining_budget() -> None:
    """``.get()`` is handed a timeout, never allowed to block indefinitely.

    An unbounded ``.get()`` is what let Modal reach the orchestrator's own
    timeout and kill the container before any ``finally`` could run.
    """
    running = FakeCall(None)

    _gather_children([(0, running)], budget_s=600.0, monotonic=_clock([0.0]))

    assert running.get_timeouts == [600.0]


# -- the cancel path ---------------------------------------------------------


def test_input_cancellation_still_reaps_every_child() -> None:
    """A user cancel unwinds this frame; the children must not survive it.

    Modal cancels an input by raising ``InputCancellation``, a *BaseException*
    (modal 1.4.2 ``exception.py``), so ``except Exception`` cannot swallow it
    and only a ``finally`` runs. Simulated with ``KeyboardInterrupt``, which is
    the same BaseException-that-is-not-Exception shape.
    """

    class Exploding(FakeCall):
        def get(self, timeout: float | None = None):
            raise KeyboardInterrupt("input cancelled by user")

    boom = Exploding(None, object_id="fc-boom")
    sibling = FakeCall(None, object_id="fc-sibling")

    with pytest.raises(KeyboardInterrupt):
        _gather_children(
            [(0, boom), (1, sibling)], budget_s=600.0, monotonic=_clock([0.0])
        )

    assert boom.cancelled == 1 and sibling.cancelled == 1, (
        "a cancel that unwound the wait loop left children running; this is "
        "the user-cancel trigger, which refunds the hold in full"
    )


def test_a_refusing_cancel_does_not_strand_the_rest() -> None:
    """One child that refuses to cancel must not abandon the others."""

    class Stubborn(FakeCall):
        def cancel(self, **kwargs):
            super().cancel(**kwargs)
            raise RuntimeError("modal said no")

    stubborn = Stubborn(None, object_id="fc-stubborn")
    tail = FakeCall(None, object_id="fc-tail")

    successes, _failures = _gather_children(
        [(0, stubborn), (1, tail)], budget_s=600.0, monotonic=_clock([0.0])
    )

    assert successes == []
    assert stubborn.cancelled == _REAP_ATTEMPTS, (
        "a cancel that kept raising was not retried the full _REAP_ATTEMPTS"
    )
    assert tail.cancelled == 1, (
        "a raising cancel on an earlier child stopped the loop and stranded "
        "every H100 behind it, or the retry pass re-cancelled a child that "
        "had already been reaped"
    )


def test_a_cancel_during_the_reap_does_not_strand_the_tail() -> None:
    """InputCancellation landing mid-reap must not end the loop.

    The reap's per-child handler has to catch BaseException, not Exception:
    ``InputCancellation`` is not an ``Exception`` (modal 1.4.2
    ``exception.py``), so an ``except Exception`` would let it escape the loop
    and leave every child behind it billing. The cancellation is deferred, not
    dropped -- the container must still see it.
    """

    class CancelledMidReap(FakeCall):
        def cancel(self, **kwargs):
            super().cancel(**kwargs)
            raise KeyboardInterrupt("input cancelled by user")

    first = CancelledMidReap(None, object_id="fc-first")
    tail = FakeCall(None, object_id="fc-tail")

    with pytest.raises(KeyboardInterrupt):
        modal_app._reap_children([(0, first), (1, tail)], set())

    assert tail.cancelled == 1, (
        "a cancel arriving during the reap escaped the loop and stranded "
        "every H100 behind it"
    )


def test_a_cancel_landing_outside_call_cancel_does_not_strand_the_tail() -> None:
    """The guard must cover the whole loop body, not just ``call.cancel()``.

    A signal lands on whichever bytecode happens to be executing, which
    includes the ``object_id`` lookup and the log write either side of the
    cancel. Guarding only the cancel shrinks the window instead of closing it,
    and every child after the interrupted one is stranded just the same.
    """

    class CancelledOnAttrAccess:
        """A cancel that arrives during the ``object_id`` lookup."""

        def __init__(self) -> None:
            self.cancelled = 0

        @property
        def object_id(self) -> str:
            raise KeyboardInterrupt("input cancelled by user")

        def cancel(self, **kwargs) -> None:
            self.cancelled += 1

    first = CancelledOnAttrAccess()
    tail = FakeCall(None, object_id="fc-tail")

    with pytest.raises(KeyboardInterrupt):
        modal_app._reap_children([(0, first), (1, tail)], set())

    assert first.cancelled == 0, "the interrupt landed before the cancel"
    assert tail.cancelled == 1, (
        "an interrupt outside call.cancel() escaped the reap loop and left "
        "every H100 behind it billing"
    )


def test_every_spawn_and_harvest_in_run_tool_is_inside_a_reaping_guard() -> None:
    """No statement between the first spawn and the last harvest is unguarded.

    Two windows, one property. ``_gather_children``'s own ``finally`` is not on
    the stack during the spawn loop, and it is not on the stack yet during the
    log write and the call setup that follow the loop -- and by then
    ``children`` holds every handle, so a cancel landing there is the most
    expensive one possible. Covering the ``_gather_children`` CALL as well as
    the spawns is what closes the second window.

    ``run_tool`` is a ``modal.Function`` and not locally callable, so this is
    checked structurally: every ``_run_one_seed.spawn`` AND every
    ``_gather_children`` call must sit inside a ``try`` whose handler is bare or
    ``BaseException`` (an ``except Exception`` would not catch
    ``InputCancellation``) and calls ``_reap_children``.
    """
    tree = ast.parse(inspect.getsource(modal_app))
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "run_tool"
    )

    def _reaping_try(node: ast.Try) -> bool:
        for handler in node.handlers:
            catches_base = handler.type is None or (
                isinstance(handler.type, ast.Name)
                and handler.type.id == "BaseException"
            )
            reaps = any(
                isinstance(c, ast.Call)
                and ast.unparse(c.func) == "_reap_children"
                for c in ast.walk(handler)
            )
            if catches_base and reaps:
                return True
        return False

    must_guard = ("_run_one_seed.spawn", "_gather_children")

    guarded: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and _reaping_try(node):
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and ast.unparse(inner.func) in must_guard
                ):
                    guarded.add(id(inner))

    for name in must_guard:
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == name
        ]
        assert calls, f"no {name} call found in run_tool; the AST scan is vacuous"
        unguarded = [n for n in calls if id(n) not in guarded]
        assert not unguarded, (
            f"{len(unguarded)} of {len(calls)} {name} call(s) in run_tool sit "
            f"outside a try that reaps on BaseException; a cancel landing "
            f"there strands every child spawned so far"
        )


def test_reap_is_idempotent_over_a_shared_settled_set() -> None:
    """Two reaps sharing one set cancel each child once, not twice.

    ``run_tool`` reaps in ``_gather_children`` and again in the guard wrapped
    around it. Both take the same ``settled`` set, and ``_reap_children``
    records each successful cancel there, so the outer pass is a no-op over
    what the inner one already handled.
    """
    a = FakeCall(None, object_id="fc-a")
    b = FakeCall(None, object_id="fc-b")
    shared: set[int] = set()

    modal_app._reap_children([(0, a), (1, b)], shared)
    modal_app._reap_children([(0, a), (1, b)], shared)

    assert (a.cancelled, b.cancelled) == (1, 1)
    assert shared == {0, 1}


def test_a_flaky_cancel_is_retried_inside_one_reap() -> None:
    """A cancel that raises is retried, not written off.

    The reap swallows errors so one child cannot stop the loop. On the
    normal-return path nothing reaps again afterwards, so without the retry a
    single transient gRPC failure would hand that one child its whole
    ``_MAX_SESSION_S`` -- the original leak, reintroduced one seed at a time.
    """

    class FailsOnce(FakeCall):
        def cancel(self, **kwargs):
            super().cancel(**kwargs)
            if self.cancelled == 1:
                raise RuntimeError("modal said no")

    flaky = FailsOnce(None, object_id="fc-flaky")
    shared: set[int] = set()

    modal_app._reap_children([(0, flaky)], shared)

    assert flaky.cancelled == 2, "the failed cancel was never retried"
    assert shared == {0}, "the eventual success was not recorded"


def test_reap_leaves_terminate_containers_at_its_default() -> None:
    """Cancel the INPUT, not the container: the worker's ``finally`` must run.

    ``_run_one_seed`` parks its raw archive in a ``finally``, and that archive
    is the only route back to the work a killed run already did.
    ``terminate_containers=True`` (``modal.FunctionCall.cancel``'s non-default)
    kills the container mid-flight and takes it with it.
    """
    running = FakeCall(None)

    _gather_children([(0, running)], budget_s=600.0, monotonic=_clock([0.0]))

    assert running.cancel_kwargs == [{}], (
        "reap passed terminate_containers explicitly; True would skip "
        "_run_one_seed's raw-archive finally"
    )


# -- only a returned .get() proves a child is terminal -----------------------


def test_a_child_that_raised_is_reported_failed_and_still_reaped() -> None:
    """A raised ``.get()`` is reported as a failure but does not prove terminality.

    Only a ``.get()`` that RETURNED proves the child is done. An exception does
    not, so the seed stays unsettled and is cancelled anyway. The redundant
    cancel of a genuinely dead call costs one gRPC round trip; skipping it on a
    child that is in fact alive costs a whole H100 session.
    """
    failed = FakeCall(RuntimeError("seed blew up"))

    successes, failures = _gather_children(
        [(0, failed)], budget_s=600.0, monotonic=_clock([0.0])
    )

    assert successes == []
    assert failures == [(0, "seed blew up")]
    assert failed.cancelled == 1


def test_a_transport_error_on_get_still_reaps_the_child() -> None:
    """A failed POLL leaves the child running, so it must be cancelled.

    ``FunctionCall.get()`` raises ``modal.exception.ConnectionError`` /
    ``ClientClosed`` once the ``FunctionGetOutputs`` retries are exhausted
    (modal 1.4.2 ``_utils/grpc_utils.py``) -- the child is untouched and still
    burning its H100. Both derive from ``modal.exception.Error(Exception)``,
    the same base as a child's own error surfaced through Modal, so no type
    test can separate them; the loop has to reap on both.
    """
    stranded = FakeCall(modal.exception.ConnectionError("channel closed"))

    successes, failures = _gather_children(
        [(0, stranded)], budget_s=600.0, monotonic=_clock([0.0])
    )

    assert successes == []
    assert failures == [(0, "channel closed")]
    assert stranded.cancelled == 1, (
        "a transport failure was read as the child terminating; the seed runs "
        "on uncancelled to its own _MAX_SESSION_S"
    )


def test_a_transport_error_is_not_a_builtin_timeout() -> None:
    """Pins the type split the two handlers in _gather_children rely on.

    The ``except TimeoutError`` branch must catch only "no output yet"
    (``modal/_functions.py`` raises the BUILTIN ``TimeoutError``). If Modal's
    transport errors were ever to derive from it, they would be reported as
    unfinished-at-deadline rather than as failures -- both still reap, but the
    failure text would lie about what happened.
    """
    assert not issubclass(modal.exception.ConnectionError, TimeoutError)
    assert not issubclass(modal.exception.ClientClosed, TimeoutError)
    # Same split, other direction: FunctionTimeoutError means the CHILD hit its
    # own ceiling and is genuinely dead, but it is NOT a builtin TimeoutError
    # either, so it lands in the same `except Exception` branch.
    assert not issubclass(modal.exception.FunctionTimeoutError, TimeoutError)


# -- the budget leaves room to do the reaping --------------------------------


def test_child_budget_leaves_the_orchestrator_room_to_reap() -> None:
    """The wait must end while this container is still alive to cancel.

    Modal enforces ``timeout=`` by killing the task; ``FunctionTimeoutError`` is
    raised client-side (modal 1.4.2 ``_utils/function_utils.py``), never inside
    the container, so a ``finally`` cannot be trusted on that path. The budget
    therefore has to expire strictly before ``_ORCHESTRATOR_TIMEOUT_S``.
    """
    assert _CHILD_WAIT_BUDGET_S < _ORCHESTRATOR_TIMEOUT_S, (
        "the child wait budget reaches or exceeds the orchestrator's own "
        "timeout, so Modal kills the container before the reap can run"
    )
    assert _ORCHESTRATOR_TIMEOUT_S - _CHILD_WAIT_BUDGET_S >= 60, (
        "under a minute of reap margin for up to N_SEEDS_MAX cancels"
    )


def test_child_budget_still_covers_a_full_worker_session() -> None:
    """The reap margin must not be paid for out of a legitimate run's time.

    A child that starts promptly and runs its whole ``_MAX_SESSION_S`` has to
    be waited out, or the fix turns successful long runs into reaped ones.
    """
    assert _CHILD_WAIT_BUDGET_S > _MAX_SESSION_S, (
        f"child budget {_CHILD_WAIT_BUDGET_S}s <= worker ceiling "
        f"{_MAX_SESSION_S}s: a worker running to its own limit would be "
        f"cancelled by its parent as a straggler"
    )


def test_run_tool_holds_a_handle_on_every_child() -> None:
    """No spawn path may use ``.remote()``, which returns no FunctionCall.

    ``.remote()`` is ``.spawn().get()`` with the handle thrown away. The
    single-seed path used it, so a cancelled ``n_seeds=1`` run -- the most
    common shape -- had nothing to cancel either.
    """
    tree = ast.parse(inspect.getsource(modal_app))
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "run_tool"
    )
    calls = {
        ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)
    }
    assert "_run_one_seed.remote" not in calls, (
        "run_tool spawns a child via .remote(), which hands back no "
        "FunctionCall; that child cannot be reaped"
    )
    assert "_run_one_seed.spawn" in calls
