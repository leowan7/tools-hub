"""Regression tests for transport-error handling in scripts/smoke_platform_api.py.

The smoke creates a real public.lab_campaigns row at its create step. If the
connection dies *after* that 201, an unhandled exception kills main() before
_summarise() runs: the row leaks with no experiment_id and no cleanup SQL
anywhere in the CI log, and the operator has to hunt it down in the admin UI.
That mid-run network failure is precisely what this monitor exists to catch, so
_http() must turn every transport error into its status=0 sentinel rather than
let it propagate.

The gap these tests pin down is the family urllib does *not* wrap into
URLError. `AbstractHTTPHandler.do_open` only wraps errors raised by
`h.request(...)`; anything raised by `h.getresponse()` -- or later by
`resp.read()`, which runs inside the `with` block but outside any handler --
reaches the caller raw. None of http.client.RemoteDisconnected,
http.client.IncompleteRead, or ConnectionResetError is a URLError or a
TimeoutError (see test_transport_errors_are_not_covered_by_urllib_error_types).

Nothing here touches the network: BASE_URL is pinned to an unroutable
.invalid host and every test stubs urlopen.

    pytest tests/test_smoke_platform_api_network.py -v
"""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SMOKE_PATH = _REPO_ROOT / "scripts" / "smoke_platform_api.py"

_FAKE_BASE_URL = "https://smoke-test.invalid/api/v1"


def _load_smoke_module():
    """Import the smoke script by path.

    scripts/ is not a package, and the module resolves RK_LIVE_KEY (through
    _env_or_die, which sys.exit(2)s when unset) and BASE_URL at import time, so
    both env vars have to be set before exec_module. BASE_URL is pointed at an
    unroutable .invalid host so this file cannot reach the live API -- which
    would create a real lab_campaigns row -- even if some future test forgets
    to stub urlopen.
    """
    saved = {k: os.environ.get(k) for k in ("RK_LIVE_KEY", "PLATFORM_API_BASE_URL")}
    os.environ["RK_LIVE_KEY"] = "rk_live_not_a_real_key_for_tests"
    os.environ["PLATFORM_API_BASE_URL"] = _FAKE_BASE_URL
    try:
        spec = importlib.util.spec_from_file_location("_smoke_platform_api", _SMOKE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["_smoke_platform_api"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


smoke = _load_smoke_module()


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for the HTTPResponse urlopen hands back, as _http() uses it."""

    def __init__(self, status: int, payload, headers=None, read_exc: BaseException | None = None):
        self.status = status
        self.headers = headers if headers is not None else {"Content-Type": "application/json"}
        self._payload = payload
        self._read_exc = read_exc

    def read(self) -> bytes:
        if self._read_exc is not None:
            raise self._read_exc
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _RawResponse(_FakeResponse):
    """A response whose body is arbitrary bytes rather than encoded JSON."""

    def __init__(self, status: int, payload: bytes, headers=None):
        super().__init__(status, None, headers=headers)
        self._raw = payload

    def read(self) -> bytes:
        return self._raw


def _raising_urlopen(exc: BaseException):
    """urlopen stub that fails the way h.getresponse() does: raw, unwrapped."""

    def _fake(req, timeout=None):  # noqa: ARG001
        raise exc

    return _fake


def _reading_urlopen(exc: BaseException):
    """urlopen stub that connects, then dies inside resp.read()."""

    def _fake(req, timeout=None):  # noqa: ARG001
        return _FakeResponse(200, None, read_exc=exc)

    return _fake


# The transport failures urllib leaves unwrapped. Fresh instances per test, so
# each is built by a factory rather than shared at module scope.
_TRANSPORT_ERRORS = [
    pytest.param(
        lambda: http.client.RemoteDisconnected("Remote end closed connection without response"),
        id="RemoteDisconnected",
    ),
    pytest.param(lambda: http.client.IncompleteRead(b"partial"), id="IncompleteRead"),
    pytest.param(lambda: ConnectionResetError(104, "Connection reset by peer"), id="ConnectionResetError"),
    pytest.param(lambda: http.client.BadStatusLine("\x16\x03\x01"), id="BadStatusLine"),
]


# ---------------------------------------------------------------------------
# _http(): every transport error becomes the status=0 sentinel
# ---------------------------------------------------------------------------


def test_transport_errors_are_not_covered_by_urllib_error_types():
    """Why the widened catch is needed at all.

    If this ever starts failing because CPython moved these under URLError,
    the narrower except-tuple would have been sufficient and this whole file
    is arguing against a hazard that no longer exists.
    """
    narrow = (urllib.error.URLError, TimeoutError)
    for cls in (http.client.RemoteDisconnected, http.client.IncompleteRead, ConnectionResetError):
        assert not issubclass(cls, narrow), f"{cls.__name__} unexpectedly covered by {narrow}"


@pytest.mark.parametrize("make_exc", _TRANSPORT_ERRORS)
def test_http_returns_sentinel_when_getresponse_dies(monkeypatch, make_exc):
    """A raw transport error out of urlopen must not escape _http()."""
    monkeypatch.setattr(urllib.request, "urlopen", _raising_urlopen(make_exc()))

    resp = smoke._http("GET", "/targets")

    assert resp.status == 0
    assert resp.headers == {}
    assert "network error" in str(resp.body)


@pytest.mark.parametrize("make_exc", _TRANSPORT_ERRORS)
def test_http_returns_sentinel_when_body_read_dies(monkeypatch, make_exc):
    """The reset can also land in resp.read(), inside the `with` but outside
    any handler. Same requirement: sentinel, not traceback."""
    monkeypatch.setattr(urllib.request, "urlopen", _reading_urlopen(make_exc()))

    resp = smoke._http("POST", "/experiments", body={"name": "x"})

    assert resp.status == 0
    assert "network error" in str(resp.body)


def test_httperror_still_reports_its_real_status(monkeypatch):
    """Ordering guard: HTTPError is a subclass of URLError (and so of OSError),
    so widening the second handler must not shadow the first. A 404 has to stay
    a 404 -- collapsing it to the status=0 sentinel would turn every clean
    API-level rejection into an indistinguishable 'network error'."""
    err = urllib.error.HTTPError(
        url=f"{_FAKE_BASE_URL}/experiments/nope",
        code=404,
        msg="Not Found",
        hdrs={"Content-Type": "application/json"},
        fp=None,
    )
    monkeypatch.setattr(urllib.request, "urlopen", _raising_urlopen(err))

    resp = smoke._http("GET", "/experiments/nope")

    assert resp.status == 404


# A gzip magic number: 0x8b is an invalid UTF-8 start byte. What a struggling
# edge actually returns when it answers 502 with a compressed error page.
_NOT_UTF8 = b"\x1f\x8b\x08\x00 upstream connect error"


def _httperror_with_body(payload: bytes, code: int = 502):
    return urllib.error.HTTPError(
        url=f"{_FAKE_BASE_URL}/experiments/x",
        code=code,
        msg="Bad Gateway",
        hdrs={"Content-Type": "application/octet-stream"},
        fp=io.BytesIO(payload),
    )


def test_httperror_survives_a_non_utf8_body(monkeypatch):
    """UnicodeDecodeError is a ValueError, so it sits outside the transport
    handler. Decoding the error body must not be able to kill the run: the
    status code is what the step needs, and the body is diagnostics."""
    monkeypatch.setattr(urllib.request, "urlopen", _raising_urlopen(_httperror_with_body(_NOT_UTF8)))

    resp = smoke._http("DELETE", "/experiments/x")

    assert resp.status == 502


def test_success_body_survives_non_utf8(monkeypatch):
    """Same hazard on the 200 path (resp.read() is inside the `with` but
    outside any handler). Must degrade to a failed shape assertion, not a
    traceback."""

    def _fake(req, timeout=None):  # noqa: ARG001
        return _RawResponse(200, _NOT_UTF8)

    monkeypatch.setattr(urllib.request, "urlopen", _fake)

    resp = smoke._http("GET", "/targets")

    assert resp.status == 200  # a real answer, not the transport sentinel


def test_httperror_body_read_dying_keeps_the_status_code(monkeypatch):
    """The error body can also drop mid-read. Keep the real status."""
    err = _httperror_with_body(b"")
    monkeypatch.setattr(err, "read", lambda: (_ for _ in ()).throw(http.client.IncompleteRead(b"")))
    monkeypatch.setattr(urllib.request, "urlopen", _raising_urlopen(err))

    resp = smoke._http("DELETE", "/experiments/x")

    assert resp.status == 502


def test_programming_errors_still_propagate(monkeypatch):
    """The catch is widened to the transport family, not to bare Exception.
    A real bug in the script must still surface as a traceback instead of
    being laundered into a fake 'network error' result."""
    monkeypatch.setattr(urllib.request, "urlopen", _raising_urlopen(ValueError("bug in the smoke, not the network")))

    with pytest.raises(ValueError):
        smoke._http("GET", "/targets")


# ---------------------------------------------------------------------------
# main(): the summary still names the row it leaked
# ---------------------------------------------------------------------------


_LEAKED_ID = "11111111-2222-3333-4444-555555555555"


def _scripted_urlopen(die_after: int, exc_factory):
    """Serve the smoke's opening steps, then fail every later call.

    Models the real hazard: the create succeeds (a lab_campaigns row now
    exists), then the connection drops for the rest of the run -- so replay,
    read-back and withdraw all fail and the row is left behind.
    """
    calls = {"n": 0}

    def _fake(req, timeout=None):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] > die_after:
            raise exc_factory()
        path = req.full_url[len(_FAKE_BASE_URL) :]
        if req.method == "GET" and path == "/targets":
            return _FakeResponse(200, {"targets": [], "total": 0})
        if req.method == "POST" and path == "/experiments/cost-estimate":
            return _FakeResponse(200, {"requires_human_quote": True, "estimated_range_usd": [1, 2]})
        if req.method == "POST" and path == "/experiments":
            return _FakeResponse(
                201,
                {
                    "experiment_id": _LEAKED_ID,
                    "status": "WaitingForConfirmation",
                    "status_log": [{"status": "Draft"}, {"status": "WaitingForConfirmation"}],
                },
            )
        raise AssertionError(f"unstubbed request: {req.method} {path}")

    return _fake


@pytest.fixture
def _no_service_creds(monkeypatch):
    """Force the optional quote round-trip to skip, so main() takes the
    withdraw path. Without this the step would import shared.campaigns and
    reach the real database if the ambient env happens to carry creds."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)


def test_main_reports_the_leaked_row_when_the_connection_drops(monkeypatch, capsys, _no_service_creds):
    """The whole point of the sentinel: a reset after the 201 must still leave
    the operator an experiment_id and runnable cleanup SQL in the CI log."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _scripted_urlopen(3, lambda: http.client.RemoteDisconnected("Remote end closed connection")),
    )

    rc = smoke.main()
    out = capsys.readouterr().out

    assert rc == 1, "a dropped connection mid-run must fail the job"
    assert "RESULTS" in out, "the summary block must print at all"
    assert _LEAKED_ID in out, "the leaked experiment_id must be named"
    assert f"DELETE FROM lab_campaigns WHERE id = '{_LEAKED_ID}';" in out
    assert "OVERALL: FAIL" in out


def test_main_reports_the_leaked_row_when_the_error_body_is_not_utf8(monkeypatch, capsys, _no_service_creds):
    """The end-to-end shape of the same hazard: the row is created, then the
    withdraw draws a 502 whose body is gzipped. Decoding it must not kill the
    run -- otherwise the row leaks with nothing in the log, which is exactly
    the failure the status=0 sentinel exists to prevent."""
    calls = {"n": 0}
    scripted = _scripted_urlopen(10**6, lambda: AssertionError("unreachable"))

    def _fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] > 3:
            raise _httperror_with_body(_NOT_UTF8)
        return scripted(req, timeout)

    monkeypatch.setattr(urllib.request, "urlopen", _fake)

    rc = smoke.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "RESULTS" in out
    assert _LEAKED_ID in out
    assert f"DELETE FROM lab_campaigns WHERE id = '{_LEAKED_ID}';" in out


def test_main_summarises_when_the_create_call_itself_drops(monkeypatch, capsys, _no_service_creds):
    """A transport death on the create call is its own leak case: the server
    may already have committed the row, but no experiment_id ever reached the
    client, so there is nothing to withdraw and no SQL to print. The run must
    still summarise (the operator's only cue to go check the admin UI) and
    must not fabricate a cleanup line for an id it never had."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _scripted_urlopen(2, lambda: http.client.RemoteDisconnected("Remote end closed connection")),
    )

    rc = smoke.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "RESULTS" in out
    assert "no experiment_id captured" in out
    assert "DELETE FROM lab_campaigns" not in out


def test_main_summarises_when_the_very_first_call_drops(monkeypatch, capsys, _no_service_creds):
    """A reset before anything is created still has to reach _summarise() --
    and must not claim a leftover row, because none was ever made."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _scripted_urlopen(0, lambda: http.client.RemoteDisconnected("Remote end closed connection")),
    )

    rc = smoke.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "RESULTS" in out
    assert "no experiment created" in out
    assert "DELETE FROM lab_campaigns" not in out
