"""A resolved accession must say where it came from, and not overclaim.

``resolve_uniprot_id`` answers from one of two places and they are not equally
trustworthy:

  * ``dbref`` -- the uploaded file's own reference record, written by the
    depositor. The identity figure beside it is a real comparison between this
    chain and the named entry.
  * ``sequence_search`` -- INFERRED, by matching this chain's CRC64 against
    UniProtKB. UniProt cannot tell apart organisms carrying an identical
    sequence, so this is wrong about the organism in roughly one answer in six.

Both used to render identically, because ``routes.py`` read ``uniprot_id`` /
``protein_name`` / ``identity_pct`` out of the result and dropped ``source`` on
the floor. The user saw a bare accession and "(identity: 100.0%)" either way.

The identity number is the sharp end. On the sequence-search path it is 1.0 BY
CONSTRUCTION -- the accession was found by matching this chain, and validation
re-fetches that same canonical sequence, so it compares a string against
itself and cannot return anything else. Rendering it reads as corroboration
that the number is structurally incapable of supplying.

    pytest tests/test_scout_uniprot_provenance.py -v
"""

from __future__ import annotations

import csv
import io
import shutil
from pathlib import Path

import pytest

from scout.flags import _CSV_COLUMNS_BASE

TMP = Path("tmp")


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
    from scout import ratelimit

    ratelimit.reset()
    yield app.test_client()
    ratelimit.reset()


@pytest.fixture
def reap_jobs():
    before = {p.name for p in TMP.iterdir()} if TMP.exists() else set()
    yield
    if not TMP.exists():
        return
    for entry in TMP.iterdir():
        if entry.name not in before and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def _pdb(n_residues: int = 40) -> bytes:
    lines = []
    for i in range(1, n_residues + 1):
        lines.append(
            f"ATOM  {i:5d}  CA  ALA A{i:4d}    "
            f"{i * 3.8:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 20.00           C"
        )
    lines.append("END")
    return ("\n".join(lines) + "\n").encode()


def _write_results_csv(job_dir: Path) -> None:
    """One scored epitope, enough for /analyze to return 200.

    Every column is stubbed to "0" first, so a column added later cannot make
    this a KeyError -- but the fields the response actually reads are set
    explicitly, because "0" for residues yields an epitope with none.
    """
    residues = list(range(10, 17))
    row = dict.fromkeys(_CSV_COLUMNS_BASE, "0")
    row.update({
        "epitope_id": "1",
        "chain_id": "A",
        "residues": ",".join(f"ALA{n}" for n in residues),
        "residue_count": str(len(residues)),
        "mean_rsa": "0.55",
        "composite_score": "0.72",
        "secondary_structure": "loop",
        "centroid_x": "1.0",
        "centroid_y": "2.0",
        "centroid_z": "3.0",
    })
    with (job_dir / "results.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS_BASE)
        writer.writeheader()
        writer.writerow(row)


@pytest.fixture
def stub(monkeypatch):
    """Everything except the UniProt result, which each test supplies."""

    def _run(pdb_path, chain_id, progress_callback=None):
        _write_results_csv(Path(pdb_path).parent)
        return Path(pdb_path).parent / "results.csv"

    monkeypatch.setattr("scout.pipeline.run_pipeline", _run)
    monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])
    monkeypatch.setattr("scout.interfaces.detect_interfaces", lambda *a, **k: [])
    return monkeypatch


def _resolve(monkeypatch, **fields):
    result = {
        "uniprot_id": "",
        "protein_name": "",
        "identity": None,
        "identity_pct": "unknown",
        "source": "",
    }
    result.update(fields)
    monkeypatch.setattr(
        "scout.epitope_db.resolve_uniprot_id", lambda *a, **k: result
    )


def _analyze(client):
    resp = client.post(
        "/scout/upload",
        data={"file": (io.BytesIO(_pdb()), "target.pdb")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.data
    job_id = resp.get_json()["job_id"]
    resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
    assert resp.status_code == 200, resp.data
    return resp.get_json()


class TestProvenanceReachesTheClient:
    def test_a_sequence_search_result_is_labelled(self, client, stub, reap_jobs):
        """The bug: this field never left ``resolve_uniprot_id``."""
        _resolve(
            stub, uniprot_id="A0A2K5QDT7", protein_name="Somatotropin",
            identity_pct="100.0%", source="sequence_search",
        )
        body = _analyze(client)
        assert body["uniprot_source"] == "sequence_search"

    def test_a_dbref_result_is_labelled(self, client, stub, reap_jobs):
        _resolve(
            stub, uniprot_id="P00698", protein_name="Lysozyme C",
            identity_pct="93.2%", source="dbref",
        )
        body = _analyze(client)
        assert body["uniprot_source"] == "dbref"

    def test_the_field_is_present_even_with_no_accession(self, client, stub, reap_jobs):
        """A client that reads the key unconditionally must not see it missing."""
        body = _analyze(client)
        assert body["uniprot_source"] == ""


class TestTheTautologicalIdentityIsSuppressed:
    def test_a_sequence_search_identity_is_not_reported(self, client, stub, reap_jobs):
        """100.0% here is an artefact of comparing a string with itself.

        This is the assertion that matters: the server was handing the client a
        number that reads as corroboration and carries none.
        """
        _resolve(
            stub, uniprot_id="A0A2K5QDT7", protein_name="Somatotropin",
            identity_pct="100.0%", source="sequence_search",
        )
        body = _analyze(client)
        assert body["sequence_identity_pct"] == "unknown"
        assert "100.0%" not in str(body["sequence_identity_pct"])

    def test_a_dbref_identity_survives(self, client, stub, reap_jobs):
        """The suppression must be scoped to the path where the number is
        meaningless. On a DBREF the comparison is real: the chain against an
        entry named by someone else, which CAN disagree."""
        _resolve(
            stub, uniprot_id="P00698", protein_name="Lysozyme C",
            identity_pct="93.2%", source="dbref",
        )
        body = _analyze(client)
        assert body["sequence_identity_pct"] == "93.2%"

    def test_a_dbref_identity_of_100_also_survives(self, client, stub, reap_jobs):
        """Kills a suppression keyed on the VALUE rather than the source.

        A DBREF chain that genuinely matches its entry byte for byte scores
        100.0% and has earned it. Filtering on the string would delete a real
        result to hide a fake one.
        """
        _resolve(
            stub, uniprot_id="P00698", protein_name="Lysozyme C",
            identity_pct="100.0%", source="dbref",
        )
        body = _analyze(client)
        assert body["sequence_identity_pct"] == "100.0%"


class TestTheClientActuallyRendersIt:
    """A field the server sends and no template reads is not a fix.

    Asserts the wire contract has a consumer. Deliberately narrow: it pins the
    field NAME and that the two branches are distinguishable, not the copy.
    """

    def test_the_template_reads_uniprot_source(self):
        page = Path("templates/scout/index.html").read_text(encoding="utf-8")
        assert "uniprot_source" in page, (
            "routes.py sends uniprot_source and nothing renders it"
        )
        assert "sequence_search" in page, (
            "the template does not distinguish the inferred case"
        )

    def test_the_identity_line_is_not_shown_for_a_sequence_match(self):
        """The two branches must be mutually exclusive in the template, so an
        inferred result cannot also print an identity figure."""
        page = Path("templates/scout/index.html").read_text(encoding="utf-8")
        idx_source = page.find("data.uniprot_source === 'sequence_search'")
        idx_identity = page.find("' (identity: '")
        assert idx_source != -1 and idx_identity != -1
        between = page[idx_source:idx_identity]
        assert "else if" in between, (
            "the identity branch must be an ELSE of the sequence-search branch; "
            "as a separate if, an inferred result prints both"
        )
