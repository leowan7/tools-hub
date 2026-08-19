"""Caught exception text must not reach the browser verbatim.

Two defects, both found by the independent QC round on PR #158 and left
out of scope then (``docs/qc/scout-interface-competition-round1.md``, D6
and D7).

**D6 — raw exception text is forwarded to the client.** Four sites in
``scout/routes.py`` put a caught exception's ``str()`` straight into a
response body: the two SSE workers (``progress`` and
``feasibility/progress``), and two ``jsonify({"error": str(exc)})``
returns (``analyze`` and ``feasibility_analyze``). ``OSError.__str__``
interpolates ``filename``, so a ``FileNotFoundError`` anywhere under the
pipeline hands the client an absolute server path. QC observed this
live::

    data: {"stage":"error","msg":"detector exploded: C:/secret/path/input.pdb"}

**D7 — ``/feasibility/analyze`` 500s on anything unexpected.** It caught
only ``(ValueError, FileNotFoundError)``; every other exception escaped
as an unhandled 500 with a stack-trace page instead of the JSON body the
route contracts to return.

The rule under test is a type allowlist, not a blanket redaction: a
``ScoutInputError`` ("Chain 'Z' not found in structure. Available chains:
A, B") is written for the user and must survive verbatim, while every
other type is replaced. Both directions are asserted below, because a
blanket "Internal error" would fix the leak by breaking the product.

The allowlisted type is ``scout.errors.ScoutInputError`` and not
``ValueError`` because the second version of this rule allowlisted
``ValueError`` and thereby promised something about every frame in the
transitive stack -- Biopython, numpy, scipy, the stdlib -- at every
version anyone would ever install. ``test_analyze_redacts_a_plain_
valueerror_from_a_dependency`` injects what that promise was betting
against.

Exposure differs per route and is recorded here so it is not re-litigated:
``/scout/progress`` and ``/scout/analyze`` are anonymous (``@anon_rate_limit``
+ ``@requires_scout_quota``, which passes through when not signed in); the
three ``/scout/feasibility*`` routes are ``@login_required``.

    pytest tests/test_scout_error_disclosure.py -v
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scout import ratelimit
from scout.errors import ScoutInputError

TMP = Path("tmp")

# A path shaped like something a real deployment would rather not publish.
# The token is what the assertions look for: it appears nowhere else in the
# repo, so a hit can only have come from the exception we injected.
_LEAKY_PATH = "C:/srv/tools-hub/instance/private-8f3a2b/input.pdb"
_LEAK_TOKEN = "private-8f3a2b"

# An app-authored message, copied in shape from scout/pipeline.py:310.
_USEFUL_MESSAGE = "Chain 'Z' not found in structure. Available chains: A, B"


# Minimal results.csv: the epitope_id branch of feasibility_analyze is only
# reached once one exists, so the TypeError below it is otherwise unreachable.
_RESULTS_CSV = '''epitope_id,residues
1,"10,11,12"
'''


def _leaky_oserror() -> FileNotFoundError:
    """The exception whose ``str()`` carries an absolute server path."""
    return FileNotFoundError(2, "No such file or directory", _LEAKY_PATH)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    ratelimit.reset()
    yield app.test_client()
    ratelimit.reset()


@pytest.fixture
def reap_jobs():
    """Delete every job dir this test created, whatever the outcome.

    ``tmp/`` is shared with every other worktree's run, so this only ever
    removes names that were absent when the test started.
    """
    before = {p.name for p in TMP.iterdir()} if TMP.exists() else set()
    yield
    if not TMP.exists():
        return
    for entry in TMP.iterdir():
        if entry.name not in before and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def _login(client, *, user_id="u-err-disclosure", email="someone@example.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email
        sess["user_id"] = user_id


def _new_job(client) -> str:
    """A real job dir owned by this session, via the 1HEW example."""
    resp = client.get("/scout/example")
    assert resp.status_code == 200, resp.data
    return resp.get_json()["job_id"]


def _raise(exc):
    def _boom(*args, **kwargs):
        raise exc
    return _boom


def _sse_events(resp) -> list[dict]:
    """Parse the ``data:`` frames out of an SSE response body."""
    events = []
    for line in resp.get_data(as_text=True).splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def _sse_error(resp) -> dict:
    events = _sse_events(resp)
    errors = [e for e in events if e.get("stage") == "error"]
    assert errors, f"no error event in stream: {events}"
    return errors[0]


# ---------------------------------------------------------------------------
# D6 — the leak, on all four sites
# ---------------------------------------------------------------------------

class TestExceptionTextDoesNotReachTheClient:

    def test_analyze_sse_redacts_an_oserror(self, client, monkeypatch, reap_jobs):
        """scout/routes.py:898 — the anonymous progress stream."""
        job_id = _new_job(client)
        monkeypatch.setattr("scout.pipeline.run_pipeline", _raise(_leaky_oserror()))

        resp = client.get(f"/scout/progress?job_id={job_id}&chain=A")

        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        event = _sse_error(resp)
        assert _LEAK_TOKEN not in event["msg"], event
        assert _LEAKY_PATH not in resp.get_data(as_text=True)

    def test_feasibility_sse_redacts_an_oserror(self, client, monkeypatch, reap_jobs):
        """scout/routes.py:1137 — the feasibility progress stream."""
        _login(client)
        job_id = _new_job(client)
        monkeypatch.setattr(
            "scout.pipeline.run_feasibility_pipeline", _raise(_leaky_oserror())
        )

        resp = client.get(
            f"/scout/feasibility/progress?job_id={job_id}&chain=A&epitope_residues=10,11,12"
        )

        assert resp.status_code == 200
        assert resp.mimetype == "text/event-stream"
        event = _sse_error(resp)
        assert _LEAK_TOKEN not in event["msg"], event
        assert _LEAKY_PATH not in resp.get_data(as_text=True)

    def test_feasibility_analyze_json_redacts_an_oserror(
        self, client, monkeypatch, reap_jobs
    ):
        """scout/routes.py:1012 — reached from except (ValueError, FileNotFoundError)."""
        _login(client)
        job_id = _new_job(client)
        monkeypatch.setattr(
            "scout.pipeline.run_feasibility_pipeline", _raise(_leaky_oserror())
        )

        resp = client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "A", "epitope_residues": [10, 11, 12]},
        )

        # 404, not 422: a missing staged file is the server losing the job,
        # not the user's PDB being invalid. Changed after round-1 QC.
        assert resp.status_code == 404, resp.data
        assert _LEAK_TOKEN not in resp.get_data(as_text=True), resp.data
        assert "re-upload" in resp.get_json()["error"].lower()

    def test_analyze_json_redacts_a_non_valueerror(
        self, client, monkeypatch, reap_jobs
    ):
        """scout/routes.py:609/610 — analyze()'s own JSON error return.

        The ``except ValueError`` at :608 forwards its message (asserted
        below in TestUsefulMessagesSurvive); this pins that the *broad*
        handler underneath it stays generic.
        """
        job_id = _new_job(client)
        monkeypatch.setattr("scout.pipeline.run_pipeline", _raise(_leaky_oserror()))

        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        assert resp.status_code in (422, 500), resp.data
        assert _LEAK_TOKEN not in resp.get_data(as_text=True), resp.data


    def test_analyze_redacts_a_plain_valueerror_from_a_dependency(
        self, client, monkeypatch, reap_jobs
    ):
        """Why the allowlist is ``ScoutInputError`` and not ``ValueError``.

        Nothing reachable under the pipeline raises a path-carrying plain
        ``ValueError`` today. The audit that established that covered one
        snapshot of Biopython, numpy, scipy and the stdlib, and said nothing
        about the next release -- so this injects the exception it was
        betting against. The message is shaped after the stdlib's own
        ``float()``, which quotes its argument back verbatim.
        """
        job_id = _new_job(client)
        monkeypatch.setattr(
            "scout.pipeline.run_pipeline",
            _raise(ValueError(f"could not convert string to float: '{_LEAKY_PATH}'")),
        )

        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        assert resp.status_code in (422, 500), resp.data
        assert _LEAK_TOKEN not in resp.get_data(as_text=True), resp.data

    def test_analyze_sse_redacts_a_plain_valueerror_from_a_dependency(
        self, client, monkeypatch, reap_jobs
    ):
        """Same injection on the anonymous SSE route, which is the exposed one."""
        job_id = _new_job(client)
        monkeypatch.setattr(
            "scout.pipeline.run_pipeline",
            _raise(ValueError(f"could not convert string to float: '{_LEAKY_PATH}'")),
        )

        resp = client.get(f"/scout/progress?job_id={job_id}&chain=A")

        assert _LEAK_TOKEN not in resp.get_data(as_text=True), resp.data


# ---------------------------------------------------------------------------
# The other direction: a blanket redaction would be a usability regression
# ---------------------------------------------------------------------------

class TestUsefulMessagesSurvive:

    def test_analyze_forwards_an_app_authored_valueerror(
        self, client, monkeypatch, reap_jobs
    ):
        job_id = _new_job(client)
        monkeypatch.setattr(
            "scout.pipeline.run_pipeline", _raise(ScoutInputError(_USEFUL_MESSAGE))
        )

        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        assert resp.status_code == 422, resp.data
        assert resp.get_json()["error"] == _USEFUL_MESSAGE

    def test_feasibility_analyze_forwards_an_app_authored_valueerror(
        self, client, monkeypatch, reap_jobs
    ):
        _login(client)
        job_id = _new_job(client)
        monkeypatch.setattr(
            "scout.pipeline.run_feasibility_pipeline",
            _raise(ScoutInputError("No valid residues found for epitope selection")),
        )

        resp = client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "A", "epitope_residues": [1, 2, 3]},
        )

        assert resp.status_code == 422, resp.data
        assert resp.get_json()["error"] == "No valid residues found for epitope selection"

    def test_feasibility_sse_forwards_an_app_authored_valueerror(
        self, client, monkeypatch, reap_jobs
    ):
        _login(client)
        job_id = _new_job(client)
        monkeypatch.setattr(
            "scout.pipeline.run_feasibility_pipeline", _raise(ScoutInputError(_USEFUL_MESSAGE))
        )

        resp = client.get(
            f"/scout/feasibility/progress?job_id={job_id}&chain=A&epitope_residues=10,11,12"
        )

        assert _sse_error(resp)["msg"] == _USEFUL_MESSAGE


# ---------------------------------------------------------------------------
# D7 — /feasibility/analyze must answer, not 500 with a stack trace
# ---------------------------------------------------------------------------

class TestFeasibilityAnalyzeHandlesTheUnexpected:

    def test_unexpected_exception_returns_json_not_a_stack_trace(
        self, app, client, monkeypatch, reap_jobs
    ):
        """A RuntimeError escaped the narrow except and became a 500 page.

        ``PROPAGATE_EXCEPTIONS`` is switched off so the app's real error
        response is exercised rather than the test client re-raising.
        """
        app.config["PROPAGATE_EXCEPTIONS"] = False
        _login(client)
        job_id = _new_job(client)
        monkeypatch.setattr(
            "scout.pipeline.run_feasibility_pipeline",
            _raise(RuntimeError(f"detector exploded: {_LEAKY_PATH}")),
        )

        resp = client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "A", "epitope_residues": [10, 11, 12]},
        )

        assert resp.status_code == 500, resp.status_code
        body = resp.get_json()
        assert body is not None, resp.get_data(as_text=True)[:400]
        assert body["error"]
        assert _LEAK_TOKEN not in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# The detail is redacted, not destroyed
# ---------------------------------------------------------------------------

class TestOperatorStillGetsTheDetail:
    """Redaction moves the detail to the log; it must not delete it.

    The 422 branch of ``feasibility_analyze`` is the one that previously
    logged *nothing* and relied on the client to carry the message, so it
    is the branch most likely to lose the diagnostic in a later edit.
    """

    def test_feasibility_analyze_logs_the_path_it_hid(
        self, client, monkeypatch, reap_jobs, caplog
    ):
        _login(client)
        job_id = _new_job(client)
        monkeypatch.setattr(
            "scout.pipeline.run_feasibility_pipeline", _raise(_leaky_oserror())
        )

        with caplog.at_level("WARNING", logger="scout.routes"):
            resp = client.post(
                "/scout/feasibility/analyze",
                json={"job_id": job_id, "chain": "A", "epitope_residues": [10, 11, 12]},
            )

        assert _LEAK_TOKEN not in resp.get_data(as_text=True)
        assert _LEAK_TOKEN in caplog.text, caplog.text

    def test_analyze_sse_logs_the_path_it_hid(
        self, client, monkeypatch, reap_jobs, caplog
    ):
        job_id = _new_job(client)
        monkeypatch.setattr("scout.pipeline.run_pipeline", _raise(_leaky_oserror()))

        with caplog.at_level("ERROR", logger="scout.routes"):
            resp = client.get(f"/scout/progress?job_id={job_id}&chain=A")
            body = resp.get_data(as_text=True)

        assert _LEAK_TOKEN not in body
        assert _LEAK_TOKEN in caplog.text, caplog.text


# ---------------------------------------------------------------------------
# Contract guard: the browser terminates on stage in ("done", "error")
# ---------------------------------------------------------------------------

class TestSseContractIsIntact:

    @pytest.mark.parametrize("exc", [
        FileNotFoundError(2, "No such file or directory", _LEAKY_PATH),
        RuntimeError("boom"),
        ScoutInputError(_USEFUL_MESSAGE),
        ScoutInputError(""),
        ValueError(_USEFUL_MESSAGE),
        ValueError(""),
    ])
    def test_error_event_keeps_stage_and_a_nonempty_msg(
        self, client, monkeypatch, reap_jobs, exc
    ):
        """templates/scout/index.html:342 reads data.stage then data.msg.

        An empty or missing ``msg`` would leave the banner blank, and a
        missing ``stage`` would hang the stream open.
        """
        job_id = _new_job(client)
        monkeypatch.setattr("scout.pipeline.run_pipeline", _raise(exc))

        event = _sse_error(client.get(f"/scout/progress?job_id={job_id}&chain=A"))

        assert event["stage"] == "error"
        assert isinstance(event["msg"], str) and event["msg"].strip()


class TestJsonRoutesAnswerJsonForMalformedInput:
    """Round-1 QC: the unhandled-500 class was only half closed.

    The original change caught unexpected exceptions from the pipeline call
    but left three crashes that happen BEFORE it -- two of them on the
    anonymous route. A JSON-contracted endpoint answering an HTML error page
    is the defect; the redaction work is what made it visible.
    """

    def test_analyze_survives_a_non_string_job_id(self, client, reap_jobs):
        """ANONYMOUS route. {"job_id": 123} used to raise AttributeError.

        The status is 404 once coerced (no such job), not 400 -- the point of
        the assertion is that a JSON body comes back at all instead of the
        HTML 500 page an uncaught AttributeError produced.
        """
        resp = client.post("/scout/analyze", json={"job_id": 123, "chain": "A"})

        assert resp.status_code != 500, resp.data
        assert resp.get_json() is not None, "answered HTML, not JSON"

    def test_analyze_survives_a_non_string_chain(self, client, reap_jobs):
        resp = client.post("/scout/analyze", json={"job_id": "x", "chain": ["A"]})

        assert resp.status_code != 500, resp.data
        assert resp.get_json() is not None, "answered HTML, not JSON"

    def test_feasibility_analyze_survives_a_non_string_job_id(
        self, client, reap_jobs
    ):
        _login(client)
        resp = client.post(
            "/scout/feasibility/analyze", json={"job_id": 123, "chain": "A"}
        )

        assert resp.status_code in (400, 404), resp.data
        assert resp.get_json() is not None, "answered HTML, not JSON"

    def test_feasibility_analyze_survives_a_non_scalar_epitope_id(
        self, client, reap_jobs
    ):
        """int({"x": 1}) raises TypeError, which the epitope block never caught.

        That block sits outside the route's main try/except, so the TypeError
        escaped as an HTML 500 even after the broad handler was added.
        """
        _login(client)
        job_id = _new_job(client)
        # The epitope branch is only reached once results.csv exists.
        (TMP / job_id / "results.csv").write_text(_RESULTS_CSV, encoding="utf-8")

        resp = client.post(
            "/scout/feasibility/analyze",
            json={
                "job_id": job_id,
                "chain": "A",
                "epitope_residues": [],
                "epitope_id": {"x": 1},
            },
        )

        assert resp.status_code == 400, resp.data
        assert resp.get_json() is not None, "answered HTML, not JSON"
        assert _LEAK_TOKEN not in resp.get_data(as_text=True)

    def test_the_epitope_parse_error_no_longer_echoes_caller_input(
        self, client, reap_jobs
    ):
        """The fifth un-gated site: it reflected the caller's own string back."""
        _login(client)
        job_id = _new_job(client)
        (TMP / job_id / "results.csv").write_text(_RESULTS_CSV, encoding="utf-8")

        resp = client.post(
            "/scout/feasibility/analyze",
            json={
                "job_id": job_id,
                "chain": "A",
                "epitope_residues": [],
                "epitope_id": "QCPROBE-NOTANINT",
            },
        )

        assert resp.status_code == 400, resp.data
        assert "QCPROBE" not in resp.get_data(as_text=True), resp.data
