"""Unit tests for the request idempotency middleware.

Stream G.1 (Wave-0 hardening). These tests use a fake Supabase client so
they run offline — no Railway / Supabase config required.

Usage
-----
    pytest tests/test_idempotency.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import patch

import pytest
from flask import Flask, jsonify

from shared.idempotency import _compute_key, idempotent

# This file is already hermetic by a different mechanism -- it builds a bare
# `Flask(__name__)` rather than calling create_app(), and `patch_deps` below is
# autouse and replaces `shared.idempotency.get_service_client`. The marker is
# here so it does not LOOK like one of the files that reads production: the
# project's stated contract is that any test touching a route opts in explicitly,
# and a reader grepping for the fixture should find it. It also fails closed if
# someone later adds a create_app() test here.
pytestmark = pytest.mark.usefixtures("isolate_supabase")


# ---------------------------------------------------------------------------
# Fake Supabase table + client
# ---------------------------------------------------------------------------


class _FakeTable:
    """Minimal in-memory stand-in for a Supabase table client.

    Models two PostgREST behaviours the previous fake ignored, both of which
    silently hid real bugs:

    * ``select("a,b")`` **projects** — the returned rows carry only the named
      columns. Ignoring this made the ``location`` replay assertions vacuous:
      they passed while production returned a 302 with no ``Location``, because
      ``_claim_key``'s explicit column list never asked for it.
    * with ``known_columns`` set, an UPDATE naming a column the table does not
      have raises, the way PostgREST does before a migration is applied. That
      is the path ``_store_response``'s fallback exists to survive.
    """

    def __init__(
        self, store: dict[str, dict], known_columns: Optional[set[str]] = None,
    ) -> None:
        self._store = store
        self._known_columns = known_columns
        self._filter_key: Optional[str] = None
        self._update_payload: Optional[dict] = None
        self._pending_upsert: Optional[dict] = None
        self._projection: Optional[list[str]] = None
        self._pending_delete = False
        self._is_null: list[str] = []

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
        as ``is.null``, and this repo issues both. Of its 17 production call
        sites, only the 2 in ``shared/idempotency.py`` pass None; the other 15
        pass the string, across ``shared/api_keys.py`` (x4),
        ``shared/targets.py`` (x4), ``shared/jobs.py`` (x3),
        ``shared/handoffs.py``, ``shared/compute_campaigns.py``,
        ``cron/purge_old_storage.py`` and ``webhooks/modal.py``. Both are
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

    def upsert(self, payload: dict, on_conflict: str = "key") -> "_FakeTable":
        self._pending_upsert = payload
        return self

    def update(self, payload: dict) -> "_FakeTable":
        self._update_payload = payload
        return self

    def delete(self) -> "_FakeTable":
        self._pending_delete = True
        return self

    def execute(self) -> Any:
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
            is_null, self._is_null = self._is_null, []
            removed = None
            if key is not None:
                candidate = self._store.get(key)
                # The IS NULL predicate is part of the WHERE clause, so a row
                # that fails it is not deleted AND not returned.
                if candidate is not None and all(
                    candidate.get(col) is None for col in is_null
                ):
                    removed = self._store.pop(key)
            return type("R", (), {"data": [removed] if removed else []})()
        if self._pending_upsert is not None:
            row = dict(self._pending_upsert)
            self._store[row["key"]] = row
            self._pending_upsert = None
            self._projection = None
            return type("R", (), {"data": [row]})()
        if self._update_payload is not None and self._filter_key is not None:
            payload = self._update_payload
            self._update_payload = None
            key = self._filter_key
            self._filter_key = None
            self._projection = None
            is_null, self._is_null = self._is_null, []
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
            if existing is not None and all(
                existing.get(col) is None for col in is_null
            ):
                existing.update(payload)
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
        self, store: dict[str, dict], known_columns: Optional[set[str]] = None,
    ) -> None:
        self._store = store
        self._known_columns = known_columns

    def table(self, _name: str) -> _FakeTable:
        return _FakeTable(self._store, known_columns=self._known_columns)


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
        user_id = "00000000-0000-0000-0000-000000000001"
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
    """If get_service_client returns None, the handler still runs."""
    with patch("shared.idempotency.get_service_client", return_value=None):
        r = app.test_client().post("/echo", data=b"hello")
    assert r.status_code == 200
    assert app.call_counter["count"] == 1


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

    ``_claim_key`` upserts, and ``ON CONFLICT DO UPDATE`` succeeds for BOTH
    racing writers, so the SELECT before it is a TOCTOU and not a lock: two
    concurrent submissions of the same form can each run the handler. Suppose
    the winner stores a 302 for a launch that really created and funded runs,
    and the loser then fails validation and releases. Delete by key alone would
    wipe the winner's cached success, and a third click would launch the whole
    set a second time -- real money, silently.

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

    `_claim_key` upserts, and `ON CONFLICT DO UPDATE` succeeds for BOTH racing
    writers, so two concurrent submissions of the same form can each be told
    "claimed" (audit A42). The winner launches and caches its 302. The loser
    then fails -- typically on the velocity cap, because the winner's budgets
    are already in `_campaign_spend_today` -- and the wrapper's 4xx path calls
    `_release_key`, which correctly matches nothing, and then falls through to
    `_store_response`.

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
