"""Guards for the anonymous compute bound: the slot, the capped queue, and the
worker class that is deliberately NOT threaded yet.

The compute cap is inert as deployed — gunicorn runs sync workers, so a process
serves one request at a time and ``_INFLIGHT`` cannot exceed 1. It is kept, and
tested, because it is the correct guard the moment the worker class changes,
and because the reasons it has not changed are specific and expiring (see the
long note in ``gunicorn.conf.py``). Testing it now is what makes that flip a
one-line change instead of a redesign.

The queue is the other half. Shedding the instant the last slot is taken
refuses a caller who would have waited two seconds — the typical analysis is
~2 CPU-s, not the ~15 worst case. But an UNBOUNDED queue is a slower way to
fall over, so it has a ceiling and sheds past it.

The tests below drive real threads through the real Condition rather than
asserting on the shape of the code, because the failure modes here are
ordering, leaks, and locks held across a yield — none of which source
inspection can see.
"""

from __future__ import annotations

import runpy
import threading
import time
from pathlib import Path

import flask
import pytest

from scout import ratelimit
from scout.ratelimit import (
    ANON_MAX_QUEUED_RUNS,
    ANON_QUEUE_WAIT_SEC,
    anon_compute_slot,
    inflight_anon_runs,
    queued_anon_runs,
)
from scout.routes import ANON_MAX_CONCURRENT_RUNS

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GUNICORN_CONF = _REPO_ROOT / "gunicorn.conf.py"

# Short enough to keep the suite quick, long enough that a scheduling hiccup
# does not read as a timeout.
_TEST_WAIT = 2.0
_SETTLE = 0.4


def _load_gunicorn_conf(env: dict | None = None, monkeypatch=None) -> dict:
    """Execute gunicorn.conf.py and return its namespace.

    Executed rather than parsed: `threads` and `workers` are computed from the
    environment through `_int_env`, so the only honest way to learn the value
    that would actually reach gunicorn is to run the file. Reading the source
    would happily pass a config that crashes on boot.
    """
    if env is not None:
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
    return runpy.run_path(str(_GUNICORN_CONF))


@pytest.fixture
def app():
    application = flask.Flask(__name__)
    application.secret_key = "test-secret"
    return application


@pytest.fixture(autouse=True)
def _clean_slots():
    ratelimit.reset()
    yield
    ratelimit.reset()


class _Caller:
    """One anonymous caller, on its own thread with its own request context."""

    def __init__(self, app, *, limit, max_waiting=None, wait_timeout=_TEST_WAIT,
                 signed_in=False):
        self.app = app
        self.limit = limit
        self.max_waiting = max_waiting
        self.wait_timeout = wait_timeout
        self.signed_in = signed_in
        self.granted: bool | None = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self.app.test_request_context():
            if self.signed_in:
                flask.session["user_email"] = "someone@example.com"
            kwargs = {"wait_timeout": self.wait_timeout}
            if self.max_waiting is not None:
                kwargs["max_waiting"] = self.max_waiting
            with anon_compute_slot(self.limit, **kwargs) as slot:
                self.granted = slot
                self.entered.set()
                if slot:
                    self.release.wait(timeout=30)
        self.finished.set()

    def start(self):
        self.thread.start()
        return self

    def let_go(self):
        self.release.set()
        self.finished.wait(timeout=30)


# ---------------------------------------------------------------------------
# The worker model
# ---------------------------------------------------------------------------


class TestWorkerModel:
    def test_worker_class_is_still_sync(self):
        """A threaded worker class must not arrive by accident.

        Flipping this line is a fleet-wide change with two named prerequisites
        — the `shared/idempotency.py` and `shared/wallet.py` read-then-act
        races it widens — and it permanently removes the `timeout` watchdog
        for slow requests, which is today the only automatic recovery from a
        wedged worker. It is a decision, taken deliberately, with the
        reasoning in gunicorn.conf.py. This test is here so that flipping it
        goes red and someone has to read that reasoning first.
        """
        conf = _load_gunicorn_conf()
        assert conf.get("worker_class", "sync") == "sync", (
            "worker_class changed — see the WORKER CLASS note in "
            "gunicorn.conf.py; the idempotency and wallet races must land first"
        )
        assert "threads" not in conf, (
            "`threads` only has meaning under a threaded worker class"
        )

    def test_the_timeout_watchdog_is_still_a_request_watchdog(self):
        """Under sync workers `timeout` bounds a slow REQUEST, because the
        arbiter's heartbeat comes from the request loop. That is the property
        being preserved by not flipping the worker class, and it is what makes
        every remaining unbounded blocking call in the app survivable.

        Floored well above the 30 s Supabase budget so a legitimately slow
        request is not killed mid-flight.
        """
        conf = _load_gunicorn_conf()
        assert conf["timeout"] >= 60

    def test_the_sizing_still_fits_the_thread_budget_it_is_written_for(self):
        """The invariant that makes the numbers a system rather than three
        unrelated constants, checked now so the flip needs no rework.

        Every in-flight anonymous run would hold a thread for its whole SSE
        stream, and every queued caller holds one while parked. If the
        documented thread count only covered those two, a busy Scout would
        consume every thread in the process and page loads, /healthz and
        signed-in routes would get nothing — which is the outage threads would
        be turned on to prevent in the first place.
        """
        documented_threads = 8  # gunicorn.conf.py, "TO FLIP IT"
        reserved = ANON_MAX_CONCURRENT_RUNS + ANON_MAX_QUEUED_RUNS
        assert reserved < documented_threads, (
            f"{reserved} reserved for anonymous compute leaves no headroom "
            f"under the documented threads={documented_threads}"
        )

    def test_more_than_one_worker(self):
        """With one worker, one wedged request is a full outage (incident
        2026-06-10). Under sync workers the worker count IS the concurrency
        bound, so this is the whole of it."""
        assert _load_gunicorn_conf()["workers"] >= 2

    def test_a_zero_worker_count_cannot_crash_the_boot(self, monkeypatch):
        """gunicorn refuses to start on workers=0, which is the worst possible
        failure — a total outage caused by a stray env var."""
        for bad in ("0", "-4", "", "banana"):
            conf = _load_gunicorn_conf({"WEB_CONCURRENCY": bad}, monkeypatch)
            assert conf["workers"] >= 1, f"WEB_CONCURRENCY={bad!r}"

    def test_preload_still_on(self):
        """Import errors must surface in logs, not as silent worker death."""
        assert _load_gunicorn_conf()["preload_app"] is True


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------


class TestQueueingRatherThanShedding:
    def test_a_caller_past_the_cap_waits_instead_of_being_refused(self, app):
        """The behaviour change. Before this, the 5th concurrent caller got an
        immediate 503 even if a slot freed a second later."""
        holders = [_Caller(app, limit=2).start() for _ in range(2)]
        for h in holders:
            assert h.entered.wait(timeout=5)
        assert inflight_anon_runs() == 2

        waiter = _Caller(app, limit=2).start()
        # It must be waiting, not refused.
        assert not waiter.entered.wait(timeout=_SETTLE)
        assert queued_anon_runs() == 1

        holders[0].let_go()
        assert waiter.entered.wait(timeout=5)
        assert waiter.granted is True, "a freed slot must go to the waiter"

        waiter.let_go()
        holders[1].let_go()
        assert inflight_anon_runs() == 0
        assert queued_anon_runs() == 0

    def test_the_queue_is_bounded_and_sheds_immediately_when_full(self, app):
        """The ceiling. An unbounded queue would park this caller too, hold a
        worker thread for it, and turn a burst into a wedged process."""
        holders = [_Caller(app, limit=2).start() for _ in range(2)]
        waiters = [_Caller(app, limit=2, max_waiting=2).start() for _ in range(2)]
        for h in holders:
            assert h.entered.wait(timeout=5)
        time.sleep(_SETTLE)
        assert queued_anon_runs() == 2

        started = time.monotonic()
        shed = _Caller(app, limit=2, max_waiting=2).start()
        assert shed.entered.wait(timeout=5)
        elapsed = time.monotonic() - started

        assert shed.granted is False
        assert elapsed < _TEST_WAIT / 2, (
            f"shed after {elapsed:.2f}s — it waited instead of shedding, so "
            f"the queue bound is not being enforced"
        )

        for h in holders:
            h.let_go()
        for w in waiters:
            w.entered.wait(timeout=5)
            w.let_go()

    def test_a_waiter_that_times_out_sheds_cleanly(self, app):
        holder = _Caller(app, limit=1).start()
        assert holder.entered.wait(timeout=5)

        waiter = _Caller(app, limit=1, wait_timeout=0.3).start()
        assert waiter.entered.wait(timeout=5)
        assert waiter.granted is False
        holder.let_go()

    def test_a_timed_out_waiter_gives_its_queue_place_back(self, app):
        """A leaked queue place shrinks the queue permanently, so the process
        sheds earlier and earlier until it refuses everything."""
        holder = _Caller(app, limit=1).start()
        assert holder.entered.wait(timeout=5)

        for _ in range(3):
            w = _Caller(app, limit=1, wait_timeout=0.2).start()
            assert w.entered.wait(timeout=5)
            assert w.granted is False

        assert queued_anon_runs() == 0
        holder.let_go()

    def test_every_waiter_eventually_runs_when_slots_free(self, app):
        """No lost wakeups: notify() wakes one waiter per released slot, and
        every release must wake somebody."""
        holders = [_Caller(app, limit=2).start() for _ in range(2)]
        for h in holders:
            assert h.entered.wait(timeout=5)
        # max_waiting passed explicitly: this test is about wakeups, not about
        # the shipped queue ceiling, and inheriting the constant would silently
        # turn it into a shed test the next time that constant moves.
        waiters = [
            _Caller(app, limit=2, max_waiting=4, wait_timeout=10).start()
            for _ in range(4)
        ]
        time.sleep(_SETTLE)
        assert queued_anon_runs() == 4

        for h in holders:
            h.let_go()
        for w in waiters:
            assert w.entered.wait(timeout=10)
            assert w.granted is True, "a waiter was never woken"
            w.let_go()

        assert inflight_anon_runs() == 0
        assert queued_anon_runs() == 0


class TestSlotAccounting:
    def test_signed_in_callers_neither_take_a_slot_nor_queue(self, app):
        """A free visitor must never be able to starve a paying user."""
        holders = [_Caller(app, limit=1).start()]
        assert holders[0].entered.wait(timeout=5)

        member = _Caller(app, limit=1, signed_in=True).start()
        assert member.entered.wait(timeout=5)
        assert member.granted is True
        assert inflight_anon_runs() == 1, "a signed-in caller consumed a slot"

        member.let_go()
        holders[0].let_go()

    def test_an_exception_inside_the_slot_still_releases_it(self, app):
        with app.test_request_context():
            with pytest.raises(RuntimeError):
                with anon_compute_slot(2) as slot:
                    assert slot
                    raise RuntimeError("boom")
        assert inflight_anon_runs() == 0

    def test_defaults_are_read_at_call_time_not_frozen_at_import(
        self, app, monkeypatch
    ):
        """Module constants used as default arguments freeze at import, so a
        rebind is silently ignored. That bug has shipped in this repo before,
        so the resolution is checked rather than assumed.

        The assertion is on the ELAPSED TIME, not just the refusal. Asserting
        `granted is False` alone passes against a frozen default too — the
        caller queues, waits out `wait_timeout`, and is refused for a
        completely different reason. Mutation testing caught exactly that, so
        the distinction is load-bearing: shed immediately, do not time out.
        """
        monkeypatch.setattr(ratelimit, "ANON_MAX_QUEUED_RUNS", 0)
        holder = _Caller(app, limit=1).start()
        assert holder.entered.wait(timeout=5)

        # max_waiting is not passed, so it must pick up the patched 0 and shed.
        started = time.monotonic()
        shed = _Caller(app, limit=1, wait_timeout=_TEST_WAIT).start()
        assert shed.entered.wait(timeout=5)
        elapsed = time.monotonic() - started

        assert shed.granted is False
        assert elapsed < _TEST_WAIT / 2, (
            f"refused after {elapsed:.2f}s — that is the wait timeout firing, "
            f"not the patched queue ceiling; the default froze at import"
        )
        holder.let_go()

    def test_the_shipped_wait_is_bounded(self):
        """A queue that parks a browser for minutes is not a queue, it is a
        hang. Two worst-case pipelines interleave for ~28 s before the first
        slot frees, so waiting much past that cannot help."""
        assert 0 < ANON_QUEUE_WAIT_SEC <= 60
        assert 0 < ANON_MAX_QUEUED_RUNS <= 16

    def test_slots_are_sized_for_a_gil_bound_process(self):
        """More slots do not drain the queue sooner — under a GIL, concurrent
        CPU-bound pipelines interleave and all finish LATE together, so the
        first free slot arrives later the more slots there are. At ~15 CPU-s
        adversarial and ~1.07 effective cores, four slots would put the first
        release at ~56 s, past any wait a browser should hold, and the queue
        could never be served under load at all.
        """
        assert ANON_MAX_CONCURRENT_RUNS <= 2, (
            "raising the slot count buys no throughput and pushes the first "
            "free slot past ANON_QUEUE_WAIT_SEC — see scout/ratelimit.py"
        )
        worst_case_cpu_s = 15.0
        effective_cores = 1.07
        first_free = ANON_MAX_CONCURRENT_RUNS * worst_case_cpu_s / effective_cores
        assert first_free > ANON_QUEUE_WAIT_SEC, (
            "the comments claim the wait expires first under adversarial "
            "load; recheck the arithmetic in scout/ratelimit.py"
        )


class TestTheLockIsNotHeldAcrossTheYield:
    """``anon_compute_slot`` is a @contextmanager, so a ``yield`` inside
    ``with _INFLIGHT_LOCK`` hands control to the caller's ``with`` BODY while
    still holding the process-wide compute mutex, and does not get it back
    until that body finishes.

    That is not academic. On /scout/progress the shed body writes an SSE frame
    to a client socket, so one slow reader would serialise every slot acquire
    and release in the process behind its own network write. It is also
    self-concealing: ``threading.Condition()`` is RLock-backed, so a
    same-thread re-entry succeeds silently where a ``Lock`` would have
    deadlocked loudly.

    Probed rather than read, because the source looks identical either way.
    """

    @staticmethod
    def _lock_free_while_body_runs(app, *, expect_granted):
        held_for = 0.35
        probe: dict = {}
        body_running = threading.Event()

        def _run():
            with app.test_request_context():
                with anon_compute_slot(1, max_waiting=0, wait_timeout=0.05) as slot:
                    assert slot is expect_granted, slot
                    body_running.set()
                    # Stand in for whatever the route does next: a jsonify on
                    # /analyze, an SSE frame written to a socket on /progress.
                    time.sleep(held_for)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        assert body_running.wait(timeout=5)
        # Sample mid-body, not at the edges.
        time.sleep(held_for / 3)
        probe["acquired"] = ratelimit._INFLIGHT_LOCK.acquire(blocking=False)
        if probe["acquired"]:
            ratelimit._INFLIGHT_LOCK.release()
        worker.join(timeout=10)
        assert not worker.is_alive()
        return probe["acquired"]

    def test_the_shed_body_does_not_hold_the_compute_mutex(self, app):
        holder = _Caller(app, limit=1).start()
        assert holder.entered.wait(timeout=5)
        try:
            acquired = self._lock_free_while_body_runs(app, expect_granted=False)
        finally:
            holder.let_go()
        assert acquired, (
            "_INFLIGHT_LOCK was held for the whole shed body — every slot "
            "acquire and release in the process serialises behind it, and on "
            "/scout/progress that body writes to a client socket"
        )

    def test_the_granted_body_does_not_hold_the_compute_mutex_either(self, app):
        acquired = self._lock_free_while_body_runs(app, expect_granted=True)
        assert acquired, "_INFLIGHT_LOCK was held across the granted yield"

    def test_reset_clears_waiters_too(self, app):
        """A leaked waiter count shrinks the queue for everything after it."""
        ratelimit._WAITING = 3
        ratelimit.reset()
        assert queued_anon_runs() == 0
