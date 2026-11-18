"""Tests for /api/jobs/<job_id>/surface/<filename> (browser-facing).

The on-request measured surface screen. Drives the real app through
``create_app()`` so @login_required and the route body both execute; the
harness here is the one tests/test_candidate_pdb.py established for the
sibling PDB route, including the ``isolate_supabase`` mark that blanks the
repo-root service-role credentials.

WHAT THIS FILE IS CHECKING is the contract that the panel degrades rather than
errors. The measurement itself is tested in tests/test_design_quality.py and
tests/test_surface_patch.py; what matters at the route is that a design with
nothing to show returns 200 and an honest sentence, that another user's job is
not readable, and that a page nobody expands costs nothing.
"""

from __future__ import annotations

import base64
import pathlib
import uuid
from unittest.mock import MagicMock

import pytest

import app as app_mod
import blueprints.jobs as jobs_mod

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_REPO = pathlib.Path(__file__).resolve().parent.parent
_REAL_PDB = _REPO / "static" / "example" / "1HEW.pdb"


@pytest.fixture
def flask_app():
    application = app_mod.create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


def _login(client, user_id: str):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = "test@example.com"
        sess["access_token"] = "fake-token"


def _patch_user_ctx(monkeypatch, user_id: str):
    ctx = MagicMock()
    ctx.user_id = user_id
    ctx.email = "test@example.com"
    monkeypatch.setattr(jobs_mod, "load_user_context", lambda: ctx)


def _job(*, candidates, tool="rfdiffusion"):
    job = MagicMock()
    job.id = str(uuid.uuid4())
    job.user_id = str(uuid.uuid4())
    job.status = "succeeded"
    job.tool = tool
    job.result = {"candidates": candidates}
    job.inputs = {}
    return job


def _designed_row(filename="designs/d0.pdb"):
    """A row whose own sequence names its chain, with the structure inline.

    Inline bytes keep Storage out of the route entirely, so these tests
    exercise the measurement and the rendering without a storage double.
    """
    from shared.exports import structure_chain_sequences

    data = _REAL_PDB.read_bytes()
    chains = structure_chain_sequences(data)
    assert chains, "fixture structure parsed to no chains"
    return {
        "pdb_key": filename,
        "sequence": chains[0][1],
        "pdb_content_b64": base64.b64encode(data).decode(),
    }


class TestAuth:
    def test_no_session_is_refused(self, client, monkeypatch):
        monkeypatch.setattr(jobs_mod, "load_user_context", lambda: None)
        resp = client.get(
            "/api/jobs/abc/surface/design_1.pdb", follow_redirects=False
        )
        assert resp.status_code in (302, 303, 401)

    def test_another_users_job_is_not_found(self, client, monkeypatch):
        """get_job is owner-scoped, so a foreign job id reads as absent."""
        user = str(uuid.uuid4())
        _login(client, user)
        _patch_user_ctx(monkeypatch, user)
        monkeypatch.setattr(jobs_mod, "get_job", lambda _id, user_id=None: None)
        resp = client.get("/api/jobs/someone-elses/surface/d0.pdb")
        assert resp.status_code == 404


class TestDegradation:
    """Nothing to show is an ANSWER, not an error and not a zero."""

    def _get(self, client, monkeypatch, job, filename):
        user = str(uuid.uuid4())
        _login(client, user)
        _patch_user_ctx(monkeypatch, user)
        monkeypatch.setattr(jobs_mod, "get_job", lambda _id, user_id=None: job)
        return client.get(f"/api/jobs/{job.id}/surface/{filename}")

    @staticmethod
    def _prose(resp) -> str:
        """Rendered body with whitespace collapsed.

        The template's prose is hard-wrapped, so a phrase to match straddles a
        newline in the output and a raw substring test would pass or fail on
        where the line happened to break.
        """
        import re

        return re.sub(r"\s+", " ", resp.get_data(as_text=True))

    def test_a_measurable_design_renders_the_figure(self, client, monkeypatch):
        job = _job(candidates=[_designed_row()])
        resp = self._get(client, monkeypatch, job, "designs/d0.pdb")
        assert resp.status_code == 200
        body = self._prose(resp)
        assert "Largest exposed hydrophobic patch" in body
        assert "&#8491;" in body
        # The copy condition travels with the figure, not just with the file.
        assert "for this design only" in body
        assert "not a measurement of the real molecule" in body

    def test_a_design_with_no_structure_says_so_and_does_not_error(
        self, client, monkeypatch
    ):
        job = _job(candidates=[{"pdb_key": "designs/d0.pdb", "sequence": "ACDEFGHIKL"}])
        resp = self._get(client, monkeypatch, job, "designs/d0.pdb")
        assert resp.status_code == 200
        body = self._prose(resp)
        assert "No surface measurement is available" in body
        assert "0 &#8491;" not in body, "a zero would read as 'measured, and flat'"

    def test_a_fold_gets_the_empty_state_not_a_screen(self, client, monkeypatch):
        """The reviewer's finding, at the route.

        An ESMFold row carries a sequence and inline bytes, so without the tool
        gate this returned a full screen describing the customer's OWN input as
        a design of ours.
        """
        job = _job(candidates=[_designed_row()], tool="esmfold")
        resp = self._get(client, monkeypatch, job, "designs/d0.pdb")
        assert resp.status_code == 200
        body = self._prose(resp)
        assert "No surface measurement is available" in body
        assert "Largest exposed hydrophobic patch" not in body

    def test_an_unknown_filename_gets_the_empty_state(self, client, monkeypatch):
        job = _job(candidates=[_designed_row()])
        resp = self._get(client, monkeypatch, job, "designs/not-a-design.pdb")
        assert resp.status_code == 200
        assert "No surface measurement is available" in self._prose(resp)

    def test_a_raising_measurement_still_returns_the_empty_state(
        self, client, monkeypatch
    ):
        """The route's catch-all: a failure here must not 500 the fragment.

        The reader's actual results are already on the page; a broken screen
        must not make them look doubtful.
        """
        def _boom(*_a, **_k):
            raise RuntimeError("measurement exploded")

        monkeypatch.setattr(
            "shared.design_quality.profile_for_structure_name", _boom
        )
        job = _job(candidates=[_designed_row()])
        resp = self._get(client, monkeypatch, job, "designs/d0.pdb")
        assert resp.status_code == 200
        assert "No surface measurement is available" in self._prose(resp)


def test_the_results_page_itself_measures_nothing():
    """The cost argument, pinned where it can regress.

    The whole reason this route exists is that neither the table nor the row's
    detail block may compute the figure at page load -- both are inside the
    per-row loop, so either would pay a Storage fetch and a SASA pass for every
    row of every page view. If someone re-adds a page-load call, this fails.
    """
    page = (_REPO / "templates" / "job_detail.html").read_text(encoding="utf-8")
    assert "{% include \"components/design_quality_panel.html\" %}" not in page, (
        "the panel must not be included at page load; it is fetched on request"
    )

    route = (_REPO / "blueprints" / "jobs.py").read_text(encoding="utf-8")
    body = route.split("def job_detail(")[1].split("\ndef ")[0]
    for banned in ("design_profile(", "profile_for_structure_name(",
                   "surface_profile("):
        assert banned not in body, (
            f"job_detail calls {banned} -- the measurement is on-request only"
        )
