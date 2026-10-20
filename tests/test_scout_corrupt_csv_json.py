"""A corrupt stored CSV must not turn a JSON route into an HTML page.

Found by the independent QC round on PR #168 and left out of scope there
because it is pre-existing. Same defect class as D7 in
``docs/qc/scout-interface-competition-round1.md`` -- an exception escaping a
route that contracts to return JSON -- at the sites #168 did not reach.

Before the fix these tests pin, ``analyze`` and ``feasibility_analyze`` finished by reading
back a CSV Scout itself wrote and coercing its cells with bare ``float()`` /
``int()``, *outside* every ``try`` in the function. The ``.get(col, 0)`` default only
covers an absent key, and ``csv.DictReader`` never omits a key that is in the
header. What it yields instead for a partial write is:

* ``""`` for a blank cell -- a trailing comma, or a value that never made it.
  ``float("")`` raises ``ValueError``.
* ``None`` for a row with fewer fields than the header, which is what a write
  cut off mid-row leaves behind. That is ``DictReader.restval``, and
  ``float(None)`` raises ``TypeError``, *not* ``ValueError``.

The ``TypeError`` half is why these tests do not settle for asserting on
``ValueError``: a guard catching only ``ValueError`` still 500s on the
truncated write, which is the likelier of the two corruptions.

Escaping uncaught, either one reaches ``app.py``'s ``errorhandler(500)``,
which renders ``templates/500.html``. A ``fetch()`` caller asking for JSON
gets a branded HTML page instead, and ``resp.json()`` throws in the browser
with no usable message. ``PROPAGATE_EXCEPTIONS = False`` below is what makes
the test client take that production path rather than re-raising.

A corrupt stored CSV is the server's fault, not the uploader's, so the answer
is 500 with ``_GENERIC_ERROR`` -- the convention PR #168 set for this file.
It is emphatically NOT "default the bad cell to 0": that would ship a
fabricated feasibility score under a real tier label, which is the same
failure Scout's constant ``interface_competition`` already was.

The healthy-path tests are not padding. Every corrupt-input assertion here is
also satisfied by a route that returns 500 unconditionally, so each class
pins both directions.

    pytest tests/test_scout_corrupt_csv_json.py -v
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scout import ratelimit
from scout.pipeline import CSV_COLUMNS, FEASIBILITY_CSV_COLUMNS

pytestmark = pytest.mark.usefixtures("isolate_supabase")

TMP = Path("tmp")

# Healthy cell values, keyed off the real column lists so these fixtures
# cannot drift from what the pipeline actually writes. No value contains a
# comma, so the rows below need no quoting and a truncated write is expressible
# as a plain cell slice.
_FEAS_VALUES = {
    "epitope_id": "1", "chain_id": "A", "residues": "LYS23", "residue_count": "6",
    "composite_feasibility": "0.72", "tier": "Promising",
    "surface_topology": "0.8", "epitope_rigidity": "0.7",
    "geometric_access": "0.6", "glycan_risk": "0.9",
    "interface_competition": "1.0",
    "recommended_approach": "de novo binder", "recommended_scaffold": "minibinder",
    "design_scale_min": "1000", "design_scale_max": "5000",
    "expected_hit_rate": "0.02", "risk_factors": "None identified",
}

_MAIN_VALUES = {
    "epitope_id": "1", "chain_id": "A", "residues": "LYS23", "residue_count": "6",
    "mean_rsa": "0.55", "composite_score": "0.81", "hydrophobic_exposure": "0.3",
    "hydrophobicity": "0.4", "hot_spot_fraction": "0.2", "geometry_score": "0.6",
    "burial_raw": "0.5", "mean_bfactor": "30.0", "bfactor_score": "0.7",
    "secondary_structure": "loop", "centroid_x": "1.0", "centroid_y": "2.0",
    "centroid_z": "3.0", "is_plddt": "0", "ss_method": "dssp",
}


def _csv_text(columns, values, *, blank=None, truncate_to=None) -> str:
    """One header row and one data row, optionally corrupted.

    ``blank`` empties a named cell (the ``ValueError`` case). ``truncate_to``
    keeps only the first N cells (the ``restval is None`` case). Passing
    neither yields the healthy row.
    """
    cells = [values[c] for c in columns]
    if blank is not None:
        cells[columns.index(blank)] = ""
    if truncate_to is not None:
        cells = cells[:truncate_to]
    return ",".join(columns) + "\n" + ",".join(cells) + "\n"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    # TESTING on its own re-raises out of the test client, which would hide
    # the very thing under test: what a real caller receives. False routes an
    # unhandled exception through errorhandler(500), exactly as in production.
    flask_app.config["PROPAGATE_EXCEPTIONS"] = False
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


def _login(client, *, user_id="u-corrupt-csv", email="someone@example.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email
        sess["user_id"] = user_id


def _new_job(client) -> str:
    """A real job dir owned by this session, via the 1HEW example."""
    resp = client.get("/scout/example")
    assert resp.status_code == 200, resp.data
    return resp.get_json()["job_id"]


def _job_dir(job_id: str) -> Path:
    return TMP / job_id


def _stage_feasibility_csv(monkeypatch, job_id: str, text: str) -> None:
    """Make run_feasibility_pipeline leave ``text`` on disk and return it.

    The route reads the file back through its own DictReader, so the coercion
    block runs on exactly these bytes. Stubbing the pipeline rather than the
    reader keeps the seam where a real partial write would land.
    """
    path = _job_dir(job_id) / "feasibility_results.csv"

    def _fake(pdb_path, chain_id, epitope_residues, progress_callback=None):
        path.write_text(text)
        return path

    monkeypatch.setattr("scout.pipeline.run_feasibility_pipeline", _fake)


def _stub_uniprot(monkeypatch) -> None:
    """Keep analyze() off the network; it is not what these tests measure."""
    monkeypatch.setattr(
        "scout.epitope_db.resolve_uniprot_id",
        lambda *a, **k: {
            "uniprot_id": "", "protein_name": "", "identity_pct": "unknown",
            "source": "",
        },
    )
    monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])


def _assert_json_not_html(resp) -> dict:
    """The contract: a JSON body, never the branded HTML error page."""
    body = resp.get_data(as_text=True)
    assert resp.mimetype == "application/json", (
        f"route answered {resp.mimetype} with status {resp.status_code}: {body[:400]}"
    )
    assert "<html" not in body.lower(), body[:400]
    payload = resp.get_json()
    assert isinstance(payload, dict) and payload.get("error"), payload
    return payload


# ---------------------------------------------------------------------------
# feasibility_analyze -- the site named in the report
# ---------------------------------------------------------------------------

class TestFeasibilityAnalyzeSurvivesACorruptCsv:

    def test_blank_numeric_cell_answers_json(self, client, monkeypatch, reap_jobs):
        """float("") -> ValueError, from a trailing comma or a lost value."""
        _login(client)
        job_id = _new_job(client)
        _stage_feasibility_csv(
            monkeypatch, job_id,
            _csv_text(FEASIBILITY_CSV_COLUMNS, _FEAS_VALUES,
                      blank="composite_feasibility"),
        )

        resp = client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "A", "epitope_residues": [10, 11, 12]},
        )

        assert resp.status_code == 500, resp.data
        _assert_json_not_html(resp)

    def test_truncated_row_answers_json(self, client, monkeypatch, reap_jobs):
        """float(None) -> TypeError, from a write cut off mid-row."""
        _login(client)
        job_id = _new_job(client)
        _stage_feasibility_csv(
            monkeypatch, job_id,
            _csv_text(FEASIBILITY_CSV_COLUMNS, _FEAS_VALUES, truncate_to=3),
        )

        resp = client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "A", "epitope_residues": [10, 11, 12]},
        )

        assert resp.status_code == 500, resp.data
        _assert_json_not_html(resp)

    def test_blank_residue_count_answers_json(self, client, monkeypatch, reap_jobs):
        """The int() on residue_count inside ``_feasibility_payload``."""
        _login(client)
        job_id = _new_job(client)
        _stage_feasibility_csv(
            monkeypatch, job_id,
            _csv_text(FEASIBILITY_CSV_COLUMNS, _FEAS_VALUES, blank="residue_count"),
        )

        resp = client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "A", "epitope_residues": [10, 11, 12]},
        )

        assert resp.status_code == 500, resp.data
        _assert_json_not_html(resp)

    def test_a_healthy_csv_still_scores(self, client, monkeypatch, reap_jobs):
        """The other direction: an unconditional 500 must not pass."""
        _login(client)
        job_id = _new_job(client)
        _stage_feasibility_csv(
            monkeypatch, job_id,
            _csv_text(FEASIBILITY_CSV_COLUMNS, _FEAS_VALUES),
        )

        resp = client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "A", "epitope_residues": [10, 11, 12]},
        )

        assert resp.status_code == 200, resp.data
        payload = resp.get_json()
        assert payload["composite_feasibility"] == 0.72
        assert payload["residue_count"] == 6
        assert payload["dimensions"]["interface_competition"] == 1.0


# ---------------------------------------------------------------------------
# analyze -- the same shape, on the anonymous route
# ---------------------------------------------------------------------------

class TestAnalyzeSurvivesACorruptResultsCsv:
    """analyze() re-reads results.csv with the same unguarded coercions.

    It skips run_pipeline entirely when results.csv already exists, so
    dropping a corrupt file into the job dir is the whole setup -- and is
    literally the scenario, a stored CSV left by an earlier partial write.
    This route is anonymous, which makes it the wider of the two surfaces.
    """

    def test_blank_numeric_cell_answers_json(self, client, monkeypatch, reap_jobs):
        job_id = _new_job(client)
        _stub_uniprot(monkeypatch)
        (_job_dir(job_id) / "results.csv").write_text(
            _csv_text(CSV_COLUMNS, _MAIN_VALUES, blank="composite_score")
        )

        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        assert resp.status_code == 500, resp.data
        _assert_json_not_html(resp)

    def test_truncated_row_answers_json(self, client, monkeypatch, reap_jobs):
        job_id = _new_job(client)
        _stub_uniprot(monkeypatch)
        (_job_dir(job_id) / "results.csv").write_text(
            _csv_text(CSV_COLUMNS, _MAIN_VALUES, truncate_to=4)
        )

        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        assert resp.status_code == 500, resp.data
        _assert_json_not_html(resp)

    def test_blank_quality_flag_input_answers_json(self, client, monkeypatch, reap_jobs):
        """The second cluster, the one feeding compute_quality_flags."""
        job_id = _new_job(client)
        _stub_uniprot(monkeypatch)
        (_job_dir(job_id) / "results.csv").write_text(
            _csv_text(CSV_COLUMNS, _MAIN_VALUES, blank="hydrophobicity")
        )

        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        assert resp.status_code == 500, resp.data
        _assert_json_not_html(resp)

    def test_a_healthy_csv_still_ranks(self, client, monkeypatch, reap_jobs):
        job_id = _new_job(client)
        _stub_uniprot(monkeypatch)
        (_job_dir(job_id) / "results.csv").write_text(
            _csv_text(CSV_COLUMNS, _MAIN_VALUES)
        )

        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        assert resp.status_code == 200, resp.data
        payload = resp.get_json()
        assert payload["chain"] == "A"
        assert payload["epitopes"][0]["composite_score"] == 0.81


# ---------------------------------------------------------------------------
# feasibility_progress -- the same class, on user data rather than a CSV
# ---------------------------------------------------------------------------

class TestFeasibilityProgressFiltersWhatIntAccepts:
    """The epitope_residues query param was filtered by a wider test than int().

    ``x.strip().lstrip("-").isdigit()`` admitted two token shapes ``int()``
    then refused, and the coercion has no ``try`` around it, so either escaped
    an ``text/event-stream`` route as an HTML 500:

    * ``lstrip("-")`` strips EVERY leading sign, so ``"--5"`` passed the
      filter and ``int("--5")`` raised;
    * ``str.isdigit()`` is true for non-decimal digits such as ``"\\u00b2"``,
      which ``int()`` does not accept.

    Reachable by anyone who can hit the URL. These assert the stream survives;
    the dropped-token outcome is the route's existing "no residues" SSE error,
    which is what an unparseable epitope list already produced.
    """

    @pytest.mark.parametrize("token", ["--5", "²", "-", "5-", "1.5"])
    def test_a_token_int_rejects_does_not_break_the_stream(
        self, client, token, reap_jobs
    ):
        _login(client)
        job_id = _new_job(client)

        resp = client.get(
            "/scout/feasibility/progress",
            query_string={"job_id": job_id, "chain": "A", "epitope_residues": token},
        )

        assert resp.status_code == 200, resp.data
        assert resp.mimetype == "text/event-stream", resp.get_data(as_text=True)[:400]

    def test_a_negative_residue_is_still_accepted(self, client, monkeypatch, reap_jobs):
        """The half the old filter got right: one leading sign must survive."""
        seen = {}

        def _fake(pdb_path, chain_id, epitope_residues, progress_callback=None):
            seen["residues"] = list(epitope_residues)
            raise RuntimeError("stop here; the parse is what is under test")

        _login(client)
        job_id = _new_job(client)
        monkeypatch.setattr("scout.pipeline.run_feasibility_pipeline", _fake)

        client.get(
            "/scout/feasibility/progress",
            query_string={
                "job_id": job_id, "chain": "A", "epitope_residues": "-5,10,--7,abc,12",
            },
        )

        assert seen["residues"] == [-5, 10, 12]
