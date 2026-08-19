"""Unit tests for the request idempotency middleware.

Stream G.1 (Wave-0 hardening). These tests use a fake Supabase client so
they run offline — no Railway / Supabase config required.

Usage
-----
    pytest tests/test_idempotency.py -v
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from unittest.mock import patch

import pytest
from flask import Flask, Response, jsonify

from shared.idempotency import (
    _claim_key,
    _compute_key,
    _store_response,
    idempotent,
)

# This file is already hermetic by a different mechanism -- it builds a bare
# `Flask(__name__)` rather than calling create_app(), and `patch_deps` below is
# autouse and replaces `shared.idempotency.get_service_client`. The marker is
# here so it does not LOOK like one of the files that reads production: the
# project's stated contract is that any test touching a route opts in explicitly,
# and a reader grepping for the fixture should find it. It also fails closed if
# someone later adds a create_app() test here.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

USER_ID = "00000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# Fake Supabase table + client
# ---------------------------------------------------------------------------


def _as_dt(value: Any) -> datetime:
    """Compare timestamps as timestamps, the way Postgres does.

    Both sides of the ``expires_at <`` predicate are ISO-8601 strings here, and
    comparing them as strings happens to work while every one carries the same
    offset. It stops working the moment one does not, and would then silently
    pass a stale-row clear that should not have matched.
    """
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class _FakeTable:
    """Minimal in-memory stand-in for a Supabase table client.

    Models three PostgREST behaviours the previous fake ignored, each of which
    silently hid real bugs:

    * ``select("a,b")`` **projects** — the returned rows carry only the named
      columns. Ignoring this made the ``location`` replay assertions vacuous:
      they passed while production returned a 302 with no ``Location``, because
      ``_claim_key``'s explicit column list never asked for it.
    * with ``known_columns`` set, an UPDATE naming a column the table does not
      have raises, the way PostgREST does before a migration is applied. That
      is the path ``_store_response``'s fallback exists to survive.
    * ``insert`` enforces the PRIMARY KEY: a second insert of a key already in
      the store RAISES, as PostgREST does on a unique violation. This is the
      whole basis of the claim, so it is modelled with ``dict.setdefault``, one
      atomic operation, rather than a check-then-set whose own correctness
      would depend on the GIL. Measured, not assumed: check-then-set here
      survives 300 runs of the race on CPython today, so this is not a bug
      being fixed. ``setdefault`` is kept anyway, because a fake standing in
      for a PRIMARY KEY should not rest on an interpreter detail that a
      free-threaded build removes.

    ``before_write`` is called once at the start of any write's ``execute()``.
    The concurrency test uses it to hold every writer at a barrier until all of
    them have finished READING, which is the interleaving that makes a
    read-then-write claim admit two winners.
    """

    def __init__(
        self,
        store: dict[str, dict],
        known_columns: Optional[set[str]] = None,
        before_write: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._store = store
        self._known_columns = known_columns
        self._before_write = before_write
        self._filter_key: Optional[str] = None
        self._update_payload: Optional[dict] = None
        self._pending_insert: Optional[dict] = None
        self._projection: Optional[list[str]] = None
        self._pending_delete = False
        self._is_null: list[str] = []
        self._at_most: list[tuple[str, Any]] = []

    def _project(self, row: dict) -> dict:
        if self._projection is None or "*" in self._projection:
            return dict(row)
        return {k: v for k, v in row.items() if k in self._projection}

    def select(self, *args: Any, **_kwargs: Any) -> "_FakeTable":
        cols = args[0] if args else "*"
        self._projection = [c.strip() for c in str(cols).split(",") if c.strip()]
        return self

    def eq(self, column: str, value: Any) -> "_FakeTable":
        if column == "key":
            self._filter_key = value
        return self

    def is_(self, column: str, value: Any) -> "_FakeTable":
        """Model the NULL predicate, do not accept-and-ignore it.

        ``_release_key`` scopes its DELETE to ``response_status IS NULL`` so it
        can only remove a claim that never completed. A fake that swallowed this
        would delete unconditionally and the scoping -- the thing that stops a
        losing sibling wiping the winner's cached success -- would be untestable
        in exactly the direction that matters.

        postgrest-py renders both ``.is_(col, None)`` and ``.is_(col, "null")``
        as ``is.null``, and this repo issues both. Of its 19 production call
        sites (counted with ``ast``, not grep -- a docstring in
        ``shared/target_results.py`` quotes an ``.is_()`` call, so grep reads
        20 and produced exactly that wrong number here once already), only the
        2 in ``shared/idempotency.py`` pass
        None; the other 17 pass the string, across ``shared/targets.py`` (x5),
        ``shared/api_keys.py`` (x4), ``shared/jobs.py`` (x3),
        ``shared/compute_campaigns.py``, ``shared/handoffs.py``,
        ``shared/target_results.py``, ``cron/purge_old_storage.py`` and
        ``webhooks/modal.py``. Both are
        accepted, because a fake that refused the string would break the moment
        this module was refactored to the majority convention -- and break
        SILENTLY, since the caller swallows it (below).

        The raise is documentation, not a guard. An earlier version of this
        docstring justified refusing on the grounds that a raise here escapes to
        the test. It does not: the builder chain in ``_release_key`` sits inside
        the ``try`` whose bare ``except Exception`` returns False, and in
        ``_store_response`` it is inside the ``_write`` closure called from the
        same kind of ``try``. Either way a refusal is swallowed into the same
        result as a fake that ignored the predicate.
        """
        if value is not None and value != "null":
            raise AssertionError(
                f"is_({column!r}, {value!r}) is not a null predicate"
            )
        self._is_null.append(column)
        return self

    def lte(self, column: str, value: Any) -> "_FakeTable":
        """Model the ``<=`` predicate, do not accept-and-ignore it.

        ``_claim_key`` clears a stale row with ``delete().eq(key).lte(
        "expires_at", now)``. The predicate is the entire safety property: it
        is what stops that delete removing the LIVE claim a concurrent caller
        inserted a moment earlier. A fake that dropped it would delete
        unconditionally and the suite would be blind to a stale-row clear that
        wipes the winner -- see
        ``test_a_stalled_caller_cannot_delete_a_claim_that_went_live``, which
        is the test that actually exercises that ordering.
        """
        self._at_most.append((column, value))
        return self

    def _passes_predicates(self, row: dict) -> bool:
        if not all(row.get(col) is None for col in self._is_null):
            return False
        for column, bound in self._at_most:
            value = row.get(column)
            if value is None:
                return False
            if _as_dt(value) > _as_dt(bound):
                return False
        return True

    def insert(self, payload: dict) -> "_FakeTable":
        self._pending_insert = payload
        return self

    def update(self, payload: dict) -> "_FakeTable":
        self._update_payload = payload
        return self

    def delete(self) -> "_FakeTable":
        self._pending_delete = True
        return self

    def execute(self) -> Any:
        if self._before_write is not None and (
            self._pending_delete
            or self._pending_insert is not None
            or self._update_payload is not None
        ):
            self._before_write()
        if self._pending_insert is not None:
            row = dict(self._pending_insert)
            self._pending_insert = None
            self._projection = None
            self._is_null = []
            self._at_most = []
            # setdefault is ONE atomic operation under the GIL. That is what
            # makes this a PRIMARY KEY rather than a check-then-set carrying
            # the same race as the bug under test.
            if self._store.setdefault(row["key"], row) is not row:
                raise RuntimeError(
                    "duplicate key value violates unique constraint "
                    '"request_idempotency_pkey"'
                )
            return type("R", (), {"data": [row]})()
        if self._pending_delete:
            # Must actually remove the row. A no-op delete would let
            # _release_key report success while leaving a claim whose
            # response_status is NULL, which reads as "in flight" and 409s
            # every retry for the rest of the TTL -- the exact failure the
            # release exists to prevent.
            self._pending_delete = False
            key = self._filter_key
            self._filter_key = None
            self._projection = None
            removed = None
            if key is not None:
                candidate = self._store.get(key)
                # The IS NULL and `<` predicates are part of the WHERE clause,
                # so a row that fails either is not deleted AND not returned.
                if candidate is not None and self._passes_predicates(candidate):
                    removed = self._store.pop(key)
            self._is_null = []
            self._at_most = []
            return type("R", (), {"data": [removed] if removed else []})()
        if self._update_payload is not None and self._filter_key is not None:
            payload = self._update_payload
            self._update_payload = None
            key = self._filter_key
            self._filter_key = None
            self._projection = None
            if self._known_columns is not None:
                unknown = set(payload) - self._known_columns
                if unknown:
                    raise RuntimeError(
                        f"column {sorted(unknown)[0]!r} of relation "
                        "'request_idempotency' does not exist"
                    )
            existing = self._store.get(key)
            # `_store_response` scopes its write to `response_status IS NULL` so
            # it cannot overwrite a claim another request already completed. The
            # predicate is part of the WHERE clause, so a row that fails it is
            # neither written nor returned. Honoured here as well as on the
            # delete path: modelling it for one verb and not the other made the
            # scoping look effective while the clobber still happened.
            if existing is not None and self._passes_predicates(existing):
                existing.update(payload)
            self._is_null = []
            self._at_most = []
            return type("R", (), {"data": []})()
        if self._filter_key is not None:
            row = self._store.get(self._filter_key)
            data = [self._project(row)] if row else []
            self._filter_key = None
            self._projection = None
            return type("R", (), {"data": data})()
        self._projection = None
        return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(
        self,
        store: dict[str, dict],
        known_columns: Optional[set[str]] = None,
        before_write: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._store = store
        self._known_columns = known_columns
        self._before_write = before_write

    def table(self, _name: str) -> _FakeTable:
        return _FakeTable(
            self._store,
            known_columns=self._known_columns,
            before_write=self._before_write,
        )


# The columns request_idempotency had BEFORE migration 0038 added `location`.
_PRE_0038_COLUMNS = {
    "key", "user_id", "route", "response_status", "response_body",
    "content_type", "expires_at", "created_at",
}


@pytest.fixture
def fake_store():
    return {}


@pytest.fixture
def fake_client(fake_store):
    return _FakeClient(fake_store)


@pytest.fixture
def app(fake_client):
    """Flask app with an @idempotent route and stubbed user context."""
    flask_app = Flask(__name__)

    call_counter = {"count": 0}

    @flask_app.route("/echo", methods=["POST"])
    @idempotent(ttl_seconds=60)
    def echo():
        call_counter["count"] += 1
        from flask import request

        return (
            jsonify(
                {
                    "call": call_counter["count"],
                    "body": request.get_data(as_text=True),
                }
            ),
            200,
        )

    redirect_counter = {"count": 0}

    @flask_app.route("/go", methods=["POST"])
    @idempotent(ttl_seconds=60)
    def go():
        from flask import redirect

        redirect_counter["count"] += 1
        return redirect("/jobs/compare?ids=a,b")

    flask_app.call_counter = call_counter  # type: ignore[attr-defined]
    flask_app.redirect_counter = redirect_counter  # type: ignore[attr-defined]
    return flask_app


@pytest.fixture
def user_ctx():
    class _Ctx:
        user_id = USER_ID
        email = "test@example.com"
        tier = "scout_pro"
        balance = 100

    return _Ctx()


@pytest.fixture(autouse=True)
def patch_deps(fake_client, user_ctx):
    with patch(
        "shared.idempotency.get_service_client", return_value=fake_client
    ), patch(
        "shared.idempotency.load_user_context", return_value=user_ctx
    ):
        yield


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def test_key_is_deterministic_for_same_body(app, user_ctx):
    with app.test_request_context("/echo", method="POST", data=b"hello"):
        key_a = _compute_key(user_ctx.user_id, "/echo", b"hello")
    with app.test_request_context("/echo", method="POST", data=b"hello"):
        key_b = _compute_key(user_ctx.user_id, "/echo", b"hello")
    assert key_a == key_b


def test_key_differs_for_different_body(app, user_ctx):
    with app.test_request_context("/echo", method="POST"):
        a = _compute_key(user_ctx.user_id, "/echo", b"one")
        b = _compute_key(user_ctx.user_id, "/echo", b"two")
    assert a != b


def test_key_differs_for_different_user(app):
    with app.test_request_context("/echo", method="POST"):
        a = _compute_key("user-a", "/echo", b"same")
        b = _compute_key("user-b", "/echo", b"same")
    assert a != b


def test_header_overrides_body_hash(app, user_ctx):
    with app.test_request_context(
        "/echo", method="POST", data=b"body-one",
        headers={"Idempotency-Key": "client-supplied"},
    ):
        with_header = _compute_key(user_ctx.user_id, "/echo", b"body-one")
    with app.test_request_context(
        "/echo", method="POST", data=b"body-two",
        headers={"Idempotency-Key": "client-supplied"},
    ):
        same_header_diff_body = _compute_key(
            user_ctx.user_id, "/echo", b"body-two"
        )
    # Same client-supplied header should dedup even across different bodies.
    assert with_header == same_header_diff_body


# ---------------------------------------------------------------------------
# End-to-end via Flask test client
# ---------------------------------------------------------------------------


def test_first_call_runs_handler_and_caches(app):
    client = app.test_client()
    r = client.post("/echo", data=b"hello")
    assert r.status_code == 200
    assert r.json["call"] == 1
    assert app.call_counter["count"] == 1


def test_replay_returns_cached_response_without_rerunning(app):
    client = app.test_client()
    r1 = client.post("/echo", data=b"hello")
    r2 = client.post("/echo", data=b"hello")
    assert r1.json == r2.json
    # Handler invoked only once.
    assert app.call_counter["count"] == 1
    assert r2.headers.get("Idempotent-Replay") == "true"


def test_replayed_redirect_keeps_its_location(app):
    """A cached response used to persist status + body + content-type only, so
    a replayed redirect came back as a bare 302 with no Location and the
    browser rendered a blank page. Every return path in the campaign refold
    route is a redirect, so double-clicking Re-fold hit this."""
    client = app.test_client()
    r1 = client.post("/go", data=b"same")
    r2 = client.post("/go", data=b"same")

    assert r1.status_code == 302
    assert r2.status_code == 302
    assert app.redirect_counter["count"] == 1        # handler ran once
    assert r2.headers.get("Idempotent-Replay") == "true"
    assert r2.headers.get("Location") == r1.headers.get("Location")
    assert "/jobs/compare" in r2.headers["Location"]


def test_redirect_still_dedupes_before_migration_0038(fake_store, user_ctx):
    """Deploy-before-migration must degrade, not break.

    Until 0038 is applied the table has no ``location`` column, so the UPDATE
    that carries it raises and ``_store_response`` retries without it. The
    replay then has no Location (the pre-fix behaviour, and the reason the
    migration is scheduled ahead of the deploy), but the guarantee that
    actually costs money -- the handler runs exactly once -- must still hold.
    """
    from flask import Flask, redirect as flask_redirect

    pre_migration = _FakeClient(fake_store, known_columns=_PRE_0038_COLUMNS)
    flask_app = Flask(__name__)
    counter = {"count": 0}

    @flask_app.route("/go", methods=["POST"])
    @idempotent(ttl_seconds=60)
    def go():
        counter["count"] += 1
        return flask_redirect("/jobs/compare?ids=a,b")

    with patch(
        "shared.idempotency.get_service_client", return_value=pre_migration
    ), patch("shared.idempotency.load_user_context", return_value=user_ctx):
        client = flask_app.test_client()
        r1 = client.post("/go", data=b"same")
        r2 = client.post("/go", data=b"same")

    assert r1.status_code == 302
    assert r1.headers.get("Location") == "/jobs/compare?ids=a,b"
    assert r2.status_code == 302
    assert r2.headers.get("Idempotent-Replay") == "true"
    assert counter["count"] == 1, "handler must not re-run: it places a hold"
    # No Location on the replay, but the cached body still carries the link.
    assert r2.headers.get("Location") is None
    assert b"/jobs/compare" in r2.data


def test_replayed_non_redirect_has_no_location(app):
    """The column is only written for responses that actually redirect."""
    client = app.test_client()
    client.post("/echo", data=b"hello")
    r2 = client.post("/echo", data=b"hello")
    assert r2.headers.get("Idempotent-Replay") == "true"
    assert r2.headers.get("Location") is None


def test_different_body_is_not_deduped(app):
    client = app.test_client()
    r1 = client.post("/echo", data=b"hello")
    r2 = client.post("/echo", data=b"world")
    assert r1.json["call"] == 1
    assert r2.json["call"] == 2
    assert app.call_counter["count"] == 2


def test_in_flight_returns_409(app, fake_store, user_ctx):
    """A second request that finds a claimed but incomplete row must 409."""
    # Pre-seed a claimed-but-incomplete row for the key the next call would
    # compute, then fire the request.
    with app.test_request_context("/echo", method="POST", data=b"hello"):
        key = _compute_key(user_ctx.user_id, "/echo", b"hello")
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    fake_store[key] = {
        "key": key,
        "user_id": user_ctx.user_id,
        "route": "/echo",
        "response_status": None,
        "response_body": None,
        "content_type": None,
        "expires_at": future.isoformat(),
    }

    r = app.test_client().post("/echo", data=b"hello")
    assert r.status_code == 409
    assert r.json["status"] == "in_progress"
    # Handler should not have run.
    assert app.call_counter["count"] == 0


def test_expired_row_does_not_block_new_request(app, fake_store, user_ctx):
    """A stale row past expires_at must not short-circuit a new request."""
    with app.test_request_context("/echo", method="POST", data=b"hello"):
        key = _compute_key(user_ctx.user_id, "/echo", b"hello")
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    fake_store[key] = {
        "key": key,
        "user_id": user_ctx.user_id,
        "route": "/echo",
        "response_status": 200,
        "response_body": '{"stale": true}',
        "content_type": "application/json",
        "expires_at": past.isoformat(),
    }

    r = app.test_client().post("/echo", data=b"hello")
    assert r.status_code == 200
    assert r.json["call"] == 1  # Fresh handler invocation.


def test_fail_open_when_supabase_unavailable(app, user_ctx):
    """No client at all: the handler still runs.

    Deliberately NOT the same decision as a client that is present and
    failing (see the fail-closed test below). With no client, every write that
    moves money short-circuits on the same missing client -- `reserve_hold` and
    `top_up_wallet` return None, `_cas_transition` returns False -- so an
    unguarded handler cannot spend anything, and failing closed here would take
    every guarded route offline in any environment that never had Supabase set
    up.

    NOT because "the wallet gate refuses". It does not: nine of the ten guarded
    routes carry no wallet decorator, and the one that does falls THROUGH on a
    null wallet row (`shared/wallet_guard.py:219-224`). `_claim_key`'s own
    docstring forbids the near-identical "the wallet decorator refuses"; this
    docstring reached for the same false idea in different words and was
    wrong.
    """
    with patch("shared.idempotency.get_service_client", return_value=None):
        r = app.test_client().post("/echo", data=b"hello")
    assert r.status_code == 200
    assert app.call_counter["count"] == 1


class _ExplodingClient:
    """A client that is PRESENT and whose every query fails.

    Not the same as no client, but NOT because the wallet gate still works --
    it does not. `get_or_create_wallet` needs this same client, so
    `wallet_preflight` returns allow=False, `requires_wallet` falls THROUGH,
    and the handler runs (status 200). `reserve_hold` then returns None at
    `shared/wallet.py:575`, so this particular fault does not reach a charge.
    `_claim_key`'s docstring forbids writing "the wallet gate is working in
    that case" -- this docstring said it anyway and was wrong.

    What makes it different from no client is scope: a fault confined to the
    idempotency table alone leaves the money path healthy, and we cannot tell
    the two apart from in here. That is what the refusal is sized for.
    """

    def table(self, _name: str) -> Any:
        raise RuntimeError("connection reset by peer")


def test_a_failing_ledger_refuses_instead_of_running_the_handler(app):
    """A live client whose query fails must fail CLOSED, not open.

    This guard is the only thing standing between a double-click and two wallet
    holds plus two Modal jobs on the five routes that spend. When it cannot read
    the ledger it cannot tell a retry from a first attempt, and on that path
    "run it and hope" is a double charge with nothing downstream to catch it.

    It used to return "open" here and run the handler anyway, so a single
    PostgREST blip turned every double-submit into two paid launches.
    """
    with patch(
        "shared.idempotency.get_service_client", return_value=_ExplodingClient()
    ):
        r = app.test_client().post("/echo", data=b"hello")
    assert r.status_code == 503, "a ledger we cannot read must refuse"
    assert app.call_counter["count"] == 0, (
        "the handler ran without a usable dedup ledger; a retry of this "
        "request would place a second hold and launch a second job"
    )


class _BrokenAtTable(_FakeTable):
    """A ledger where ONE verb fails — the shape a partial outage takes.

    A client that fails on every call only ever exercises the first query
    `_claim_key` makes. The three later refusals need a ledger that answers the
    fast-path SELECT and then breaks, so each is reachable on its own.
    """

    def __init__(self, store, *, fail, budget, **kwargs):
        super().__init__(store, **kwargs)
        self._fail = fail
        self._budget = budget

    def execute(self):  # noqa: ANN201
        if self._pending_delete:
            verb = "delete"
        elif self._pending_insert is not None:
            verb = "insert"
        elif self._update_payload is not None:
            verb = "update"
        else:
            verb = "select"
        if verb in self._fail:
            # `select_survives` spends here, so the fast-path read can succeed
            # and only the re-read fail.
            if verb == "select" and self._budget["select"] > 0:
                self._budget["select"] -= 1
            else:
                raise RuntimeError(f"{verb} failed: connection reset by peer")
        return super().execute()


class _BrokenAtClient:
    def __init__(self, store, *, fail, select_survives=0):
        self._store = store
        self._fail = set(fail)
        self._budget = {"select": select_survives}

    def table(self, _name: str) -> _BrokenAtTable:
        return _BrokenAtTable(self._store, fail=self._fail, budget=self._budget)


def _stale_row(key: str) -> dict:
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    return {
        "key": key, "user_id": USER_ID, "route": "/echo",
        "response_status": 200, "response_body": "{}",
        "content_type": "application/json", "expires_at": past.isoformat(),
    }


@pytest.mark.parametrize(
    "label, store, client",
    [
        (
            "the stale-row DELETE fails",
            {"k": _stale_row("k")},
            lambda store: _BrokenAtClient(store, fail={"delete"}),
        ),
        (
            "the INSERT fails and the re-read fails too",
            {},
            lambda store: _BrokenAtClient(
                store, fail={"insert", "select"}, select_survives=1
            ),
        ),
        (
            "the INSERT fails and no claim is held",
            {},
            lambda store: _BrokenAtClient(store, fail={"insert"}),
        ),
    ],
)
def test_every_way_the_ledger_can_break_refuses(label, store, client):
    """All FOUR refusal exits, not just the one the route test happens to hit.

    `_claim_key` gives up in four places, and until this existed only the
    fast-path SELECT's was covered: the other three could each be flipped back
    to `"open"` with the whole suite still green, which would quietly restore
    fail-open on the exits a half-configured deployment actually takes. The
    fourth is covered by
    `test_a_failing_ledger_refuses_instead_of_running_the_handler`, which also
    pins the 503 the decorator turns this into.
    """
    with patch(
        "shared.idempotency.get_service_client", return_value=client(store)
    ):
        state, row = _claim_key("k", USER_ID, "/echo", 60)
    assert (state, row) == ("unavailable", None), (
        f"{label}: got {state!r}, so the handler runs against a ledger that "
        "cannot deduplicate it — a retry places a second hold and job"
    )


# ---------------------------------------------------------------------------
# Concurrency — the claim has to be a lock, not a suggestion
#
# These drive two REAL threads through `_claim_key` with the same key, held at
# a barrier so that neither writes until both have finished reading. That is
# the interleaving a read-then-write claim cannot survive.
#
# Threads are the test's mechanism, not the production one, and the difference
# matters for how urgently this reads. `gunicorn.conf.py:42` sets
# `workers = max(1, _int_env("WEB_CONCURRENCY", 2))` and sets neither
# `worker_class` nor `threads`, so the shipped default is TWO WORKER PROCESSES.
# Two processes race on one Postgres table exactly as two threads do. This was
# reachable in production under the config as deployed, not contingent on the
# proposed switch to threaded workers -- that would only widen it.
#
# Both tests fail against the `upsert(claim_row, on_conflict="key")` this
# replaced: ON CONFLICT DO UPDATE succeeds for BOTH racing writers, so both are
# told "claimed" and both run the handler.
# ---------------------------------------------------------------------------


def _race_two_claims(store: dict[str, dict], key: str) -> list[tuple]:
    """Run two concurrent `_claim_key` calls for `key`; return both outcomes.

    The barrier is the whole point. Without it the two calls would almost
    always serialise and the loser would simply see the winner's committed row
    on its own SELECT -- the safe ordering, which proves nothing.
    """
    barrier = threading.Barrier(2, timeout=10)
    client = _FakeClient(store, before_write=barrier.wait)
    outcomes: list[tuple] = []
    lock = threading.Lock()

    def claim() -> None:
        result = _claim_key(key, USER_ID, "/echo", 60)
        with lock:
            outcomes.append(result)

    # Patched once, on this thread, around both: `patch` mutates module state
    # and is not safe to enter and exit concurrently from the workers.
    with patch("shared.idempotency.get_service_client", return_value=client):
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not any(t.is_alive() for t in threads), "a claim thread hung"

    assert len(outcomes) == 2, f"expected two outcomes, got {outcomes}"
    return outcomes


def test_two_concurrent_claims_produce_exactly_one_winner():
    """Exactly one of two simultaneous claims may run the handler."""
    store: dict[str, dict] = {}
    outcomes = _race_two_claims(store, "race-fresh-key")

    states = sorted(state for state, _ in outcomes)
    assert states.count("claimed") == 1, (
        f"expected exactly one winner, got {states}. Two callers past this "
        "guard means one click places two wallet holds and launches two "
        "Modal jobs."
    )
    assert states == ["claimed", "in_flight"], (
        f"the loser must be told the claim is held, got {states}"
    )
    assert len(store) == 1, "one claim, one row"


def _claim_parked_after_its_read(store: dict[str, dict], key: str):
    """Start a claim, let its SELECT run, and park it before its first WRITE.

    The barrier used above cannot express this. It releases every caller into
    its write at the same moment, so each one's read saw the same world its
    write acts on. The dangerous ordering is the opposite: a caller that read,
    then LOST THE CPU long enough for someone else to clear the stale row and
    claim the key, and whose write finally lands on a world that moved. That is
    the only ordering in which the stale-row DELETE meets a LIVE claim.

    Returns (thread, client, has_read, resume, outcome). The thread is not
    started; `has_read` fires once its SELECT is done.
    """
    has_read = threading.Event()
    resume = threading.Event()
    outcome: dict = {}

    def _park() -> None:
        has_read.set()
        assert resume.wait(timeout=10), "the parked caller was never resumed"

    client = _FakeClient(store, before_write=_park)

    def _run() -> None:
        outcome["result"] = _claim_key(key, USER_ID, "/echo", 60)

    return threading.Thread(target=_run), client, has_read, resume, outcome


def _run_parked_against_a_winner(store, key, after_winner_claims=None):
    """Drive the read/stall/write ordering; return (winner, parked) outcomes."""
    parked, parked_client, has_read, resume, outcome = _claim_parked_after_its_read(
        store, key
    )
    plain = _FakeClient(store)

    def _pick():
        return parked_client if threading.current_thread() is parked else plain

    with patch("shared.idempotency.get_service_client", _pick):
        parked.start()
        assert has_read.wait(timeout=10), "the parked caller never read"
        winner = _claim_key(key, USER_ID, "/echo", 60)
        if after_winner_claims is not None:
            after_winner_claims()
        resume.set()
        parked.join(timeout=10)
    assert not parked.is_alive(), "the parked claim thread hung"
    return winner, outcome["result"]


def test_a_stalled_caller_cannot_delete_a_claim_that_went_live():
    """The stale-row DELETE must not remove a claim that went live meanwhile.

    A caller reads the expired row, stalls, and by the time its DELETE runs
    another request has cleared that row and claimed the key. Only the
    `lte("expires_at", now)` predicate keeps that DELETE off the live claim.
    Without it the stalled caller wipes the winner, inserts cleanly, and is
    told "claimed" as well -- so one click funds and launches twice, and the
    winner's cached response is gone for the next click too.

    This is the test the barrier ones cannot be: drop the predicate from
    `_claim_key` and only this goes red.
    """
    key = "stalled-caller-key"
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    store: dict[str, dict] = {
        key: {
            "key": key,
            "user_id": USER_ID,
            "route": "/echo",
            "response_status": 200,
            "response_body": '{"stale": true}',
            "content_type": "application/json",
            "expires_at": past.isoformat(),
        }
    }
    winner, parked = _run_parked_against_a_winner(store, key)

    assert winner[0] == "claimed", f"the unblocked caller should win: {winner}"
    assert parked[0] == "in_flight", (
        f"the stalled caller was told {parked[0]!r}. Its DELETE removed a LIVE "
        "claim, so both callers run: one click, two wallet holds, two jobs."
    )
    assert len(store) == 1, "one claim, one row"
    assert store[key]["response_status"] is None, "the live claim was replaced"


def test_a_caller_that_lost_the_race_replays_the_winners_cached_response():
    """Losing to a request that already FINISHED must replay, not 409.

    This is the only reason `_classify_failed_claim` re-reads instead of
    answering every failed insert with a blanket 409, and nothing else reaches
    the branch: the fast-path SELECT catches a completed row long before the
    insert, so it takes a caller that read while the key was free and wrote
    after the winner had cached its response.
    """
    key = "lost-then-replay-key"
    store: dict[str, dict] = {}

    def _cache_the_winners_response() -> None:
        _store_response(key, Response(response='{"launched": 1}', status=200,
                                      content_type="application/json"))

    winner, parked = _run_parked_against_a_winner(
        store, key, after_winner_claims=_cache_the_winners_response
    )

    assert winner[0] == "claimed"
    state, row = parked
    assert state == "replay", (
        f"expected the winner's cached response, got {state!r}: a 409 here "
        "tells the user their launch is 'still running' when it has finished"
    )
    assert row is not None and row["response_status"] == 200
    assert row["response_body"] == '{"launched": 1}'


def test_two_concurrent_claims_over_a_stale_row_produce_exactly_one_winner():
    """Same, on the path where an expired row has to be cleared first.

    A different code path: both callers see the stale row, so both issue the
    conditional DELETE before racing to INSERT.

    This does NOT cover the DELETE's predicate, and it cannot. `before_write`
    releases both callers into their DELETEs together, so both run while the
    only row present is the stale one -- an unscoped DELETE would remove
    exactly the row that was going to be cleared anyway, and the test stays
    green with the predicate deleted (verified). The ordering that catches it
    is a caller that reads and then stalls, which is
    `test_a_stalled_caller_cannot_delete_a_claim_that_went_live` above. Do not
    read this test as guarding `lte`.
    """
    key = "race-stale-key"
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    store: dict[str, dict] = {
        key: {
            "key": key,
            "user_id": USER_ID,
            "route": "/echo",
            "response_status": 200,
            "response_body": '{"stale": true}',
            "content_type": "application/json",
            "expires_at": past.isoformat(),
        }
    }
    outcomes = _race_two_claims(store, key)

    states = sorted(state for state, _ in outcomes)
    assert states == ["claimed", "in_flight"], (
        f"expected exactly one winner over a stale row, got {states}"
    )
    assert len(store) == 1, "one claim, one row"
    surviving = store[key]
    assert surviving["response_status"] is None, (
        "the surviving row is the stale one, not the new claim: a replay would "
        "serve a minute-old response to a request that never ran"
    )
    assert _as_dt(surviving["expires_at"]) > datetime.now(timezone.utc), (
        "the winner's claim must be live, not the expired row it replaced"
    )


# ---------------------------------------------------------------------------
# The stream-consumed body (Phase 2)
#
# In production the raw body is ALWAYS empty by the time the decorator reads
# it: app.py's _enforce_csrf before_request touches request.form, which
# consumes the stream, and Werkzeug's get_data() does not populate its cache
# from a form parse. These tests reproduce that with a before_request of their
# own. Without it they would exercise a code path production never takes.
# ---------------------------------------------------------------------------


@pytest.fixture
def form_app(fake_client):
    """An @idempotent route behind a form-consuming before_request."""
    flask_app = Flask(__name__)
    counter = {"count": 0}

    @flask_app.before_request
    def _consume_form():  # noqa: ANN202
        from flask import request
        request.form.get("_csrf")

    @flask_app.route("/launch", methods=["POST"])
    @idempotent(ttl_seconds=60)
    def launch():
        from flask import request
        counter["count"] += 1
        return jsonify({
            "call": counter["count"],
            "tools": request.form.getlist("tools"),
        }), 200

    flask_app.counter = counter  # type: ignore[attr-defined]
    return flask_app


def test_the_raw_body_really_is_empty_once_the_form_is_parsed(form_app):
    """The premise of every test below. If this ever fails, Werkzeug changed
    and the form fallback is no longer the production path."""
    from flask import request
    with form_app.test_request_context(
        "/launch", method="POST", data={"_csrf": "t", "tools": "rfdiffusion"},
    ):
        request.form.get("_csrf")
        assert request.get_data(cache=True) == b""


def test_a_double_submit_of_the_same_form_runs_the_handler_once(form_app):
    client = form_app.test_client()
    data = {"_csrf": "t", "tools": "rfdiffusion", "designs": "12"}
    first = client.post("/launch", data=data)
    second = client.post("/launch", data=data)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers.get("Idempotent-Replay") == "true"
    assert form_app.counter["count"] == 1


def test_two_different_submissions_both_run(form_app):
    """The live defect this fixes. With the key reduced to (user, route), the
    second submission is treated as a replay of the first: it never runs, and
    the user is shown the earlier response with no indication."""
    client = form_app.test_client()
    client.post("/launch", data={"_csrf": "t", "tools": "rfdiffusion"})
    second = client.post(
        "/launch", data={"_csrf": "t", "tools": ["rfdiffusion", "pxdesign"]},
    )
    assert form_app.counter["count"] == 2
    assert second.headers.get("Idempotent-Replay") is None
    assert second.json["tools"] == ["rfdiffusion", "pxdesign"]


def test_every_value_of_a_multi_valued_field_is_in_the_key(form_app):
    """Built from form.lists(), not to_dict(). to_dict() keeps only the first
    value, which would make "run 1 tool" and "run 7 tools" the same request."""
    client = form_app.test_client()
    client.post("/launch", data={"_csrf": "t", "tools": ["a", "b"]})
    client.post("/launch", data={"_csrf": "t", "tools": ["a", "b", "c"]})
    assert form_app.counter["count"] == 2


def test_a_rotated_csrf_token_is_not_a_different_request(form_app):
    """_csrf is excluded from the fingerprint. It rotates independently of what
    the user typed, so including it would defeat dedup on a real double-click."""
    client = form_app.test_client()
    client.post("/launch", data={"_csrf": "token-one", "tools": "rfdiffusion"})
    second = client.post(
        "/launch", data={"_csrf": "token-two", "tools": "rfdiffusion"},
    )
    assert form_app.counter["count"] == 1
    assert second.headers.get("Idempotent-Replay") == "true"


def test_field_order_does_not_change_the_key(form_app, user_ctx):
    from flask import request

    def _key(fields):
        with form_app.test_request_context("/launch", method="POST", data=fields):
            request.form.get("_csrf")
            return _compute_key(user_ctx.user_id, "/launch", b"")

    # dicts preserve insertion order, so these two post their fields in
    # opposite orders on the wire.
    assert _key({"b": "2", "a": "1"}) == _key({"a": "1", "b": "2"})


def test_an_uploaded_files_name_and_size_are_in_the_key(form_app, user_ctx):
    """Two submissions identical except for the attached structure must not
    collide. tool_submit is multipart, so without this the second upload is
    silently dropped."""
    import io

    from flask import request

    def _key(name, blob):
        with form_app.test_request_context(
            "/launch", method="POST",
            data={"_csrf": "t", "target_pdb": (io.BytesIO(blob), name)},
            content_type="multipart/form-data",
        ):
            request.form.get("_csrf")
            return _compute_key(user_ctx.user_id, "/launch", b"")

    same = _key("a.pdb", b"ATOM" * 10)
    assert same == _key("a.pdb", b"ATOM" * 10)
    assert same != _key("b.pdb", b"ATOM" * 10)     # different filename
    assert same != _key("a.pdb", b"ATOM" * 99)     # different size


# ---------------------------------------------------------------------------
# Failures are not cached (Phase 2)
# ---------------------------------------------------------------------------


@pytest.fixture
def failing_app(fake_client):
    """A route that fails until told otherwise, mirroring "top up and retry"."""
    flask_app = Flask(__name__)
    state = {"fail": True, "calls": 0}

    @flask_app.before_request
    def _consume_form():  # noqa: ANN202
        from flask import request
        request.form.get("_csrf")

    @flask_app.route("/launch", methods=["POST"])
    @idempotent(ttl_seconds=60)
    def launch():
        state["calls"] += 1
        if state["fail"]:
            return jsonify({"error": "insufficient balance"}), 400
        return jsonify({"ok": True}), 200

    flask_app.state = state  # type: ignore[attr-defined]
    return flask_app


def test_a_corrected_retry_is_not_answered_with_the_stale_error(
    failing_app, fake_store,
):
    """A user refused for insufficient balance tops up in another tab and
    resubmits the identical form. Caching the 400 would replay the refusal for
    the rest of the TTL."""
    client = failing_app.test_client()
    data = {"_csrf": "t", "tools": "rfdiffusion"}
    first = client.post("/launch", data=data)
    assert first.status_code == 400
    assert fake_store == {}, "a failed request must leave no claim behind"

    failing_app.state["fail"] = False
    second = client.post("/launch", data=data)
    assert second.status_code == 200
    assert second.headers.get("Idempotent-Replay") is None
    assert failing_app.state["calls"] == 2


def test_a_failed_request_leaves_no_in_flight_claim(failing_app, fake_store):
    """The claim must be DELETED, not merely left unwritten. A row with
    response_status NULL reads as in-flight and 409s every retry until it
    expires, which is worse than replaying the error."""
    client = failing_app.test_client()
    client.post("/launch", data={"_csrf": "t", "tools": "rfdiffusion"})
    assert fake_store == {}
    second = client.post("/launch", data={"_csrf": "t", "tools": "rfdiffusion"})
    assert second.status_code == 400, "a 409 means the claim was orphaned"


def test_a_successful_response_is_still_cached(failing_app, fake_store):
    """The release path must not have broken ordinary dedup."""
    failing_app.state["fail"] = False
    client = failing_app.test_client()
    client.post("/launch", data={"_csrf": "t", "tools": "rfdiffusion"})
    assert len(fake_store) == 1
    second = client.post("/launch", data={"_csrf": "t", "tools": "rfdiffusion"})
    assert second.headers.get("Idempotent-Replay") == "true"
    assert failing_app.state["calls"] == 1


def test_a_repeated_file_field_still_contributes_to_the_key(form_app, user_ctx):
    """Sorting (name, FileStorage) pairs falls through to comparing two
    FileStorage objects and raises TypeError. The surrounding except catches
    it, so nothing 500s -- which is exactly what makes it dangerous: the whole
    file fingerprint is silently dropped and two DIFFERENT uploads collide
    again. Assert the property, not the absence of a crash."""
    import io

    from flask import request

    def _key(names):
        with form_app.test_request_context(
            "/launch", method="POST",
            data={"_csrf": "t", "pdb": [
                (io.BytesIO(b"ATOM  " + n.encode()), n) for n in names
            ]},
            content_type="multipart/form-data",
        ):
            request.form.get("_csrf")
            assert len(request.files.getlist("pdb")) == len(names)
            return _compute_key(user_ctx.user_id, "/launch", b"")

    assert _key(["a.pdb", "b.pdb"]) == _key(["a.pdb", "b.pdb"])
    assert _key(["a.pdb", "b.pdb"]) != _key(["a.pdb", "c.pdb"])
    # And the same two files in the other order are the same request. Sorting
    # by field name alone leaves repeated parts in wire order, which passes
    # the two assertions above while still failing to dedup a re-submit.
    assert _key(["a.pdb", "b.pdb"]) == _key(["b.pdb", "a.pdb"])


def test_releasing_a_claim_that_matched_nothing_reports_failure(fake_store):
    """A delete that matches no row must not report success: the claim would
    still be sitting there with response_status NULL, and the caller would skip
    its cache fallback and hand every retry a 409 until the TTL expired."""
    from shared.idempotency import _release_key
    assert _release_key("no-such-key") is False
    fake_store["real-key"] = {"key": "real-key", "response_status": None}
    assert _release_key("real-key") is True
    assert "real-key" not in fake_store


def test_releasing_never_removes_a_claim_that_already_completed(fake_store):
    """The scoping, not just the delete. This is the leg that makes a duplicate
    launch impossible rather than merely unlikely.

    Delete by key alone would remove a completed row belonging to a DIFFERENT
    request: a launch that really created and funded runs, whose cached 302 is
    gone, so the next click launches the whole set a second time -- real money,
    silently.

    The pairing that first produced it (two racing siblings, both told
    "claimed" by an upsert that could not fail) is gone; `_claim_key` inserts
    now and the concurrency tests above hold it to one winner. The scoping is
    still what stands between that clobber and a claim taken over after its TTL
    expired, which is documented in full on `_store_response`.

    ``response_status IS NULL`` is what confines the release to a claim that
    never finished. Red if that predicate is dropped.
    """
    from shared.idempotency import _release_key

    fake_store["done-key"] = {
        "key": "done-key",
        "response_status": 302,
        "response_body": "",
        "location": "/targets/t-1?launched=g-1",
    }
    assert _release_key("done-key") is False
    assert fake_store["done-key"]["response_status"] == 302, (
        "a completed claim was deleted; the winner's cached success is gone "
        "and the next submit re-launches everything"
    )


def test_an_error_is_cached_when_the_claim_cannot_be_released(failing_app):
    """Degradation path: if the delete fails we fall back to caching, because
    replaying a 400 beats orphaning a claim that 409s every retry."""
    with patch("shared.idempotency._release_key", return_value=False):
        client = failing_app.test_client()
        client.post("/launch", data={"_csrf": "t", "tools": "rfdiffusion"})
        second = client.post(
            "/launch", data={"_csrf": "t", "tools": "rfdiffusion"},
        )
    assert second.status_code == 400
    assert second.headers.get("Idempotent-Replay") == "true"
    assert failing_app.state["calls"] == 1


def test_a_nul_in_a_field_value_cannot_forge_a_part_boundary(form_app, user_ctx):
    """Two genuinely different launches must not hash alike.

    The fingerprint frames every component by LENGTH -- ``len:field len:value``,
    with no delimiter anywhere. This test pins the reason, which is a defect in
    the encoding it replaced, so the paragraphs below describe that older shape
    rather than the current one.

    A delimited encoding -- ``field=value`` parts joined on a separator -- lets a
    field VALUE containing that separator spell out an extra part, and ``%00``
    survives urlencoded decoding into ``request.form``, so NUL is not a
    theoretical separator here. These two forms are different launches:

        A: name="a\\x00tools=iggm", tools=["rfdiffusion"]
        B: name="a",               tools=["iggm", "rfdiffusion"]

    Under ``b"\\0".join(parts)`` both serialise to
    ``name=a\\0tools=iggm\\0tools=rfdiffusion`` -- so B, submitted within the
    TTL, would be answered from A's cache and never run. B launches two tools
    where A launched one, which is real money that silently does not happen.

    A length prefix cannot be spelled from inside a value, which is why every
    component is prefixed rather than delimited.
    """
    from flask import request

    def _key(data):
        with form_app.test_request_context(
            "/launch", method="POST", data=data,
            content_type="application/x-www-form-urlencoded",
        ):
            # Consume the stream exactly as app.py's _enforce_csrf does, so the
            # form fallback is the path under test.
            request.form.get("_csrf")
            return _compute_key(user_ctx.user_id, "/launch", b"")

    forged = _key({
        "_csrf": "t", "name": "a\x00tools=iggm", "tools": ["rfdiffusion"],
    })
    genuine = _key({
        "_csrf": "t", "name": "a", "tools": ["iggm", "rfdiffusion"],
    })
    assert forged != genuine, (
        "a NUL inside a field value forged a part boundary, so two different "
        "launches share one idempotency key and the second is a silent replay"
    )
    # And the fingerprint is still stable for a genuine re-submit.
    assert genuine == _key({
        "_csrf": "t", "name": "a", "tools": ["rfdiffusion", "iggm"],
    })


def test_a_nul_bearing_value_survives_form_decoding(form_app):
    """Precondition for the test above. If NUL could not reach request.form the
    forgery would be unreachable and that test would prove nothing."""
    from flask import request

    with form_app.test_request_context(
        "/launch", method="POST",
        data={"_csrf": "t", "name": "a\x00tools=iggm"},
        content_type="application/x-www-form-urlencoded",
    ):
        assert "\x00" in request.form["name"]


def test_a_losing_siblings_failure_cannot_overwrite_a_cached_success(fake_store):
    """`_store_response` may only write a claim that has not completed.

    This reproduces the shape A42 hit in production. Two submissions of the
    same form were both told "claimed", because `_claim_key` upserted and ON
    CONFLICT DO UPDATE cannot fail. The winner launches and caches its 302. The
    loser then fails -- typically on the velocity cap, because the winner's
    budgets are already in `_campaign_spend_today` -- and the wrapper's 4xx path
    calls `_release_key`, which correctly matches nothing, and then falls
    through to `_store_response`.

    `_claim_key` inserts now, so two siblings can no longer be told "claimed"
    at once and this exact pairing is unreachable. The write is still driven
    directly here, because the scoping it verifies is what stops the same
    clobber arriving by the other route `_store_response` documents: a claim
    whose TTL expired under a slow handler and was taken over.

    Unscoped, that write replaced the winner's cached 302 with the loser's 400.
    The user was shown "Nothing was started and nothing was charged" for a
    launch that was funded and billing, and every click inside the TTL replayed
    that 400. `_release_key` alone does not prevent this: it is the pair of
    scopings that does.
    """
    from flask import Flask

    from shared.idempotency import _store_response

    fake_store["winner"] = {
        "key": "winner",
        "response_status": 302,
        "response_body": "",
        "content_type": "text/html; charset=utf-8",
        "location": "/targets/t-1?launched=g-1",
    }

    app = Flask(__name__)
    with app.test_request_context():
        loser = app.make_response(("Nothing was started.", 400))
        _store_response("winner", loser)

    row = fake_store["winner"]
    assert row["response_status"] == 302, (
        "the loser's 400 overwrote the winner's cached success; the next click "
        "replays a false 'nothing was charged' for a billing launch"
    )
    assert row["location"] == "/targets/t-1?launched=g-1"


def test_store_response_still_caches_an_unfinished_claim(fake_store):
    """The scoping above must not break the orphaned-claim fallback it serves.

    When a release fails for an infra reason the row is still present with a
    NULL status, so this write must still match and still cache -- otherwise a
    failed release would leave a claim that 409s every retry until the TTL.
    """
    from flask import Flask

    from shared.idempotency import _store_response

    fake_store["live"] = {"key": "live", "response_status": None}

    app = Flask(__name__)
    with app.test_request_context():
        _store_response("live", app.make_response(("nope", 400)))

    assert fake_store["live"]["response_status"] == 400


def test_an_equals_in_a_field_name_cannot_forge_a_boundary_either(
    form_app, user_ctx,
):
    """The NUL forgery closed the boundary BETWEEN parts. This closes the one
    INSIDE a part.

    Parts used to be a single `f"{field}={value}"` string with one length
    prefix, so a field NAME containing `=` could still be confused with a
    value: `%3D` decodes into `request.form`, and `{"a": "b=c"}` and
    `{"a=b": "c"}` both encoded to `5:a=b=c`. Framing the name and the value
    separately gives `1:a3:b=c` and `3:a=b1:c`.

    Low impact on the launch route specifically (the confusable fields are
    non-price), but the fingerprint guards seven routes and a collision means a
    silent replay.
    """
    from flask import request

    def _key(data):
        with form_app.test_request_context(
            "/launch", method="POST", data=data,
            content_type="application/x-www-form-urlencoded",
        ):
            request.form.get("_csrf")
            return _compute_key(user_ctx.user_id, "/launch", b"")

    assert _key({"_csrf": "t", "a": "b=c"}) != _key({"_csrf": "t", "a=b": "c"})


def test_an_equals_bearing_field_name_survives_form_decoding(form_app):
    """Precondition: if `=` could not appear in a decoded field name, the test
    above would pass vacuously."""
    from flask import request

    with form_app.test_request_context(
        "/launch", method="POST", data={"_csrf": "t", "a=b": "c"},
        content_type="application/x-www-form-urlencoded",
    ):
        assert "a=b" in request.form


def test_a_form_field_cannot_be_spelled_as_a_file_descriptor(form_app, user_ctx):
    """File parts and form parts share one list, so they must not collide.

    Pins the property, and deliberately does NOT attribute it to the "file" tag
    on file descriptors: removing that tag changes no test, because the outer
    per-part length prefix already frames each part whole and a 3-component file
    part cannot equal a pair of 2-component form parts. The tag is debugging
    aid, not the guard.
    """
    import io as _io

    from flask import request

    def _with_file():
        with form_app.test_request_context(
            "/launch", method="POST",
            data={"_csrf": "t", "pdb": (_io.BytesIO(b"ATOM"), "x.pdb")},
            content_type="multipart/form-data",
        ):
            request.form.get("_csrf")
            assert request.files.getlist("pdb")
            return _compute_key(user_ctx.user_id, "/launch", b"")

    def _with_fields(data):
        with form_app.test_request_context(
            "/launch", method="POST", data=data,
            content_type="application/x-www-form-urlencoded",
        ):
            request.form.get("_csrf")
            return _compute_key(user_ctx.user_id, "/launch", b"")

    # A form field trying to spell the file descriptor's components.
    forged = _with_fields({"_csrf": "t", "file": "pdb", "x.pdb": "4"})
    assert _with_file() != forged
