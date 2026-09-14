"""Scout results belong to one chain, and a cached run must prove which.

``results.csv`` is written per job directory, not per chain, and ``/scout/analyze``
used to treat "the file exists" as "this chain is already scored". Analysing
chain A and then chain B of the same structure therefore returned HTTP 200
carrying chain A's epitopes labelled as chain B's, with no pipeline run and no
visible signal — the worst failure mode a tool whose entire output is the
scientific result can have.

The fix stamps the scored chain into the CSV and makes every reader ask for a
chain, so a mismatch is a cache miss rather than a wrong answer. These tests
pin that: the routes here run against a stubbed pipeline because what is under
test is which chain's numbers come back, not the biophysics.

    pytest tests/test_scout_chain_scoped_results.py -v
"""

from __future__ import annotations

import csv
import json
import io
import shutil
import textwrap
from pathlib import Path

import pytest

from scout import epitope_db
from scout.flags import _CSV_COLUMNS_BASE

TMP = Path("tmp")

# Distinct residue numbering per chain. The response echoes ``chain`` straight
# back from the request, so the only honest evidence of WHICH chain was scored
# is the residue numbers that came out of the CSV.
CHAIN_RESIDUES = {"A": list(range(10, 17)), "B": list(range(60, 67))}


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
    """Delete every job dir this test created, whatever the outcome."""
    before = {p.name for p in TMP.iterdir()} if TMP.exists() else set()
    yield
    if not TMP.exists():
        return
    for entry in TMP.iterdir():
        if entry.name not in before and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def _pdb_two_chains(n_residues: int = 40, chains: tuple = ("A", "B")) -> bytes:
    """Two chains, each long enough to clear the 30%-of-chain patch cap.

    ``chains`` is parameterised because a chain id is a single arbitrary byte in
    PDB column 22 — the boundary tests need to build structures whose chains are
    named ``=`` or ``-`` to show those reach the pipeline rather than a 400.
    """
    lines = []
    serial = 1
    for offset, chain in enumerate(chains):
        for i in range(1, n_residues + 1):
            x = i * 3.8 + offset * 200.0
            lines.append(
                f"ATOM  {serial:5d}  CA  ALA {chain}{i:4d}    "
                f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 20.00           C"
            )
            serial += 1
    lines.append("END")
    return ("\n".join(lines) + "\n").encode()


_CIF_HEADER = """data_test
#
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_alt_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.label_entity_id
_atom_site.label_seq_id
_atom_site.pdbx_PDB_ins_code
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
_atom_site.occupancy
_atom_site.B_iso_or_equiv
_atom_site.auth_seq_id
_atom_site.auth_comp_id
_atom_site.auth_asym_id
_atom_site.auth_atom_id
_atom_site.pdbx_PDB_model_num
"""


def _cif_two_chains(chains: tuple, n_residues: int = 40) -> bytes:
    """The same fixture as _pdb_two_chains, in mmCIF.

    PDB cannot express a multi-character chain id at all - column 22 is one
    byte, and widening it corrupts the record so the file will not parse. mmCIF
    auth_asym_id has no such limit and Biopython passes it through verbatim, so
    this is the only way to build a structure whose chain id is longer than one
    character.
    """
    rows = []
    serial = 1
    for offset, chain in enumerate(chains):
        for i in range(1, n_residues + 1):
            x = i * 3.8 + offset * 200.0
            rows.append(
                f"ATOM {serial} C CA . ALA {chain} 1 {i} ? "
                f"{x:.3f} 0.000 0.000 1.00 20.00 {i} ALA {chain} CA 1"
            )
            serial += 1
    return (_CIF_HEADER + "\n".join(rows) + "\n#\n").encode()


def _write_results_csv(job_dir: Path, chain: str) -> None:
    """The ``results.csv`` a real run of ``chain`` would have left behind."""
    residues = CHAIN_RESIDUES.get(chain)
    if residues is None:
        # Chains outside the A/B fixture still need residue numbers unique to
        # them: residue numbers are the only honest evidence of which chain was
        # scored, since the response echoes the requested chain back verbatim.
        base = 100 + (sum(ord(c) for c in chain) % 50) * 10
        residues = list(range(base, base + 7))
    row = dict.fromkeys(_CSV_COLUMNS_BASE, "0")
    row.update({
        "epitope_id": "1",
        "residues": ",".join(f"ALA{n}" for n in residues),
        "residue_count": str(len(residues)),
        "mean_rsa": "0.55",
        "composite_score": "0.72",
        "secondary_structure": "loop",
        "centroid_x": "1.0",
        "centroid_y": "2.0",
        "centroid_z": "3.0",
    })
    if "chain_id" in _CSV_COLUMNS_BASE:
        row["chain_id"] = chain
    with (job_dir / "results.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS_BASE)
        writer.writeheader()
        writer.writerow(row)



@pytest.fixture
def stub_pipeline(monkeypatch):
    """Replace the scorer with one that records its chain and writes that chain.

    The returned list is the chain ids ``run_pipeline`` was actually asked for,
    which is what makes a silent cache hit visible.
    """
    calls: list[str] = []

    def _run(pdb_path, chain_id, progress_callback=None):
        calls.append(chain_id)
        _write_results_csv(Path(pdb_path).parent, chain_id)
        return Path(pdb_path).parent / "results.csv"

    monkeypatch.setattr("scout.pipeline.run_pipeline", _run)
    monkeypatch.setattr(
        "scout.epitope_db.resolve_uniprot_id",
        lambda *a, **k: {"uniprot_id": "", "protein_name": "", "identity_pct": "unknown",
         "source": ""},
    )
    monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])
    monkeypatch.setattr("scout.interfaces.detect_interfaces", lambda *a, **k: [])
    return calls


def _write_feasibility_csv(
    job_dir: Path, chain: str, residues=(10, 11)
) -> Path:
    """One feasibility row, stamped, with the whole declared column set.

    ``/scout/feasibility/analyze`` reads the numeric columns straight back out,
    so a partial row is not a usable fixture. Sits beside ``_write_results_csv``
    rather than in a class, because ``_stub_feasibility_pipeline`` needs it too
    and that is used from three classes.
    """
    from scout.pipeline import FEASIBILITY_CSV_COLUMNS

    row = dict.fromkeys(FEASIBILITY_CSV_COLUMNS, "0")
    row.update({
        "epitope_id": "1",
        "chain_id": chain,
        # Residue numbers are the only honest evidence of which chain was
        # scored; the route echoes `chain` back from the request either way.
        "residues": ",".join(f"ALA{r}" for r in residues),
        "residue_count": str(len(residues)),
        "tier": "Moderate",
    })
    out = job_dir / "feasibility_results.csv"
    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FEASIBILITY_CSV_COLUMNS)
        writer.writeheader()
        writer.writerow(row)
    return out


def _stub_feasibility_pipeline(monkeypatch):
    """Patch run_feasibility_pipeline to write what the real one would.

    It cannot execute here (freesasa is absent from this venv) and the route
    reads its numeric columns straight back out, so the stub has to produce the
    whole declared column set. Module level, because tests in three classes
    need it — one of them used to reach into another class's staticmethod.
    """
    def _run(pdb_path, chain_id, epitope_residues, progress_callback=None):
        return _write_feasibility_csv(
            Path(pdb_path).parent, chain_id, epitope_residues
        )

    monkeypatch.setattr("scout.pipeline.run_feasibility_pipeline", _run)


def _upload_two_chain_job(client) -> str:
    resp = client.post(
        "/scout/upload",
        data={"file": (io.BytesIO(_pdb_two_chains()), "target.pdb")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert {c["id"] for c in body["chains"]} == {"A", "B"}, body
    return body["job_id"]


def _login(client) -> None:
    """Both /scout/feasibility/* routes are @login_required."""
    with client.session_transaction() as sess:
        sess["user_email"] = "someone@example.com"
        sess["user_id"] = "u-chain-scope-test"


def _residue_numbers(resp) -> list[int]:
    body = resp.get_json()
    assert body.get("epitopes"), f"no epitopes came back: {body}"
    return body["epitopes"][0]["residue_numbers"]


class TestAnalyzeIsChainScoped:
    def test_chain_b_after_chain_a_returns_chain_b(
        self, client, stub_pipeline, reap_jobs
    ):
        """The live bug: chain B's request was served chain A's scored surface."""
        job_id = _upload_two_chain_job(client)

        resp_a = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        assert resp_a.status_code == 200, resp_a.data
        assert _residue_numbers(resp_a) == CHAIN_RESIDUES["A"]

        resp_b = client.post("/scout/analyze", json={"job_id": job_id, "chain": "B"})
        assert resp_b.status_code == 200, resp_b.data
        assert _residue_numbers(resp_b) == CHAIN_RESIDUES["B"], (
            "chain B was served chain A's epitopes"
        )

    def test_chain_b_actually_runs_the_pipeline(
        self, client, stub_pipeline, reap_jobs
    ):
        """A different chain is a cache MISS — it must be scored, not reused."""
        job_id = _upload_two_chain_job(client)
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "B"})
        assert stub_pipeline == ["A", "B"], (
            f"pipeline ran for {stub_pipeline}, so chain B reused a cached run"
        )

    def test_same_chain_twice_still_reuses_the_cached_run(
        self, client, stub_pipeline, reap_jobs
    ):
        """The cache must still work — this is a correctness fix, not its removal."""
        job_id = _upload_two_chain_job(client)
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        assert stub_pipeline == ["A"], (
            f"pipeline ran {len(stub_pipeline)}x for one chain; the cache stopped working"
        )

    def test_a_pre_fix_results_csv_is_a_miss_not_a_wrong_answer(
        self, client, stub_pipeline, reap_jobs
    ):
        """Job dirs written before this fix carry no chain — rescore, never guess."""
        job_id = _upload_two_chain_job(client)
        job_dir = TMP / job_id
        _write_results_csv(job_dir, "A")
        # Strip the stamp back off, reproducing what is on disk in production.
        with (job_dir / "results.csv").open(newline="") as fh:
            rows = list(csv.DictReader(fh))
        legacy_cols = [c for c in _CSV_COLUMNS_BASE if c != "chain_id"]
        with (job_dir / "results.csv").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=legacy_cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "B"})
        assert resp.status_code == 200, resp.data
        assert stub_pipeline == ["B"], "an unstamped CSV must be rescored, not trusted"
        assert _residue_numbers(resp) == CHAIN_RESIDUES["B"]


class TestDerivedFilesAreChainScopedToo:
    """The files ``/scout/download`` serves are derived from results.csv.

    Stamping results.csv is not enough on its own: the top-3 CSVs are only
    rewritten when the new run produced a qualifying epitope, so a chain that
    scores nothing leaves the previous chain's file in place for a download
    button that used to be shown unconditionally.
    """

    def _write_unqualifying_csv(self, job_dir: Path, chain: str) -> None:
        """A run that scored the chain but found nothing worth designing at."""
        row = dict.fromkeys(_CSV_COLUMNS_BASE, "0")
        row.update({
            "epitope_id": "1",
            "chain_id": chain,
            "residues": "ALA90,ALA91,ALA92,ALA93,ALA94,ALA95,ALA96",
            "residue_count": "7",
            "composite_score": "0.05",  # below _MIN_COMPOSITE
            "secondary_structure": "loop",
        })
        with (job_dir / "results.csv").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS_BASE)
            writer.writeheader()
            writer.writerow(row)

    def test_top3_download_never_serves_the_previous_chain(
        self, client, monkeypatch, reap_jobs
    ):
        job_id = _upload_two_chain_job(client)
        job_dir = TMP / job_id

        # Chain A scores a real epitope; chain B scores nothing that qualifies.
        def _run(pdb_path, chain_id, progress_callback=None):
            if chain_id == "A":
                _write_results_csv(Path(pdb_path).parent, "A")
            else:
                self._write_unqualifying_csv(Path(pdb_path).parent, chain_id)
            return Path(pdb_path).parent / "results.csv"

        monkeypatch.setattr("scout.pipeline.run_pipeline", _run)
        monkeypatch.setattr(
            "scout.epitope_db.resolve_uniprot_id",
            lambda *a, **k: {"uniprot_id": "", "protein_name": "", "identity_pct": "unknown",
         "source": ""},
        )
        monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])
        monkeypatch.setattr("scout.interfaces.detect_interfaces", lambda *a, **k: [])

        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        resp_b = client.post("/scout/analyze", json={"job_id": job_id, "chain": "B"})
        assert resp_b.status_code == 200, resp_b.data
        assert resp_b.get_json()["epitopes"] == [], "fixture must produce no top-3 for B"

        dl = client.get(f"/scout/download/{job_id}")
        body = dl.get_data(as_text=True) if dl.status_code == 200 else ""
        assert "ALA10" not in body, (
            "the top-3 download served chain A's epitopes after chain B was analysed: "
            f"{body[:200]}"
        )
        # Whatever it does serve must not claim to be chain A.
        for row in csv.DictReader(body.splitlines()):
            assert row.get("chain_id") != "A", f"chain A row leaked into the download: {row}"
        assert not (job_dir / "epitopes.csv").exists() or "ALA10" not in (
            job_dir / "epitopes.csv"
        ).read_text(), "stale epitopes.csv left on disk for the download fallback"


class TestKnownBinderOverlapsAreChainScoped:
    """``analyze_cache.json`` already stamps its chain — nothing read it.

    Feasibility called with explicit ``epitope_residues`` skips the results.csv
    gate entirely, so this is the one surviving path where chain A's data can
    reach a chain B answer.
    """

    def _login(self, client):
        _login(client)

    def test_explicit_residues_do_not_inherit_another_chains_binders(
        self, client, stub_pipeline, monkeypatch, reap_jobs
    ):
        self._login(client)
        job_id = _upload_two_chain_job(client)

        monkeypatch.setattr(
            "scout.epitope_db.fetch_known_binders",
            lambda *a, **k: [{
                "pdb_id": "1ABC", "binder_type": "Fab", "species": "human",
                "resolution": 2.0, "affinity": "1 nM",
                "contact_residues": CHAIN_RESIDUES["A"],
            }],
        )
        monkeypatch.setattr(
            "scout.epitope_db.resolve_uniprot_id",
            lambda *a, **k: {"uniprot_id": "P1", "protein_name": "x", "identity_pct": "100",
             "source": "dbref"},
        )
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        def _feas(pdb_path, chain_id, epitope_residues, progress_callback=None):
            return Path(pdb_path).parent / "feasibility_results.csv"

        monkeypatch.setattr("scout.pipeline.run_feasibility_pipeline", _feas)
        (TMP / job_id / "feasibility_results.csv").write_text(
            "epitope_id,residues,residue_count,composite_feasibility,tier\n"
            "1,\"ALA10\",7,0.5,Moderate\n"
        )

        resp = client.post(
            "/scout/feasibility/analyze",
            json={
                "job_id": job_id,
                "chain": "B",
                "epitope_residues": CHAIN_RESIDUES["A"],
            },
        )
        assert resp.status_code == 200, resp.data
        assert resp.get_json()["known_binder_overlaps"] == [], (
            "chain A's known binders were reported as overlapping a chain B epitope"
        )


class TestAnOutageIsNotFrozenIntoTheJobOnDisk:
    """``analyze_cache.json`` is the durable copy, so it can outlive the bug.

    ``/scout/analyze`` writes the binder list to the job directory, and
    ``_get_binder_overlaps`` reads it back much later, on a different request
    and possibly a different worker. So a coordinate download that failed
    during /analyze used to persist "this antibody contacts nothing" to DISK:
    it survived the outage, survived a worker restart, survived epitope_db's
    in-process cache healing, and only a re-analyze cleared it.

    Two halves. ``scout.epitope_db`` no longer writes a placeholder ``[]`` for
    an interface it could not compute -- the key is simply absent -- so the
    file records "not established" rather than a fact. And the accession is
    stored alongside, so the reader can ask epitope_db for the interface it has
    since managed to compute.
    """

    # Read off the module, not hard-coded: the guard below reasons about
    # "more binders than ever get an interface", which is this number.
    _CAP = epitope_db._MAX_CONTACT_STRUCTURES

    _BINDER = {
        "pdb_id": "1ABC",
        "binder_type": "antibody",
        "species": "human",
        "resolution": 2.0,
        "affinity": "1 nM",
    }

    @staticmethod
    def _resolves_to(monkeypatch, accession="P00001"):
        monkeypatch.setattr(
            "scout.epitope_db.resolve_uniprot_id",
            lambda *a, **k: {
                "uniprot_id": accession,
                "protein_name": "Test",
                "identity_pct": "100",
                "source": "dbref",
            },
        )

    def _analyze_during_outage(self, client, monkeypatch) -> Path:
        """Analyse chain A while the coordinate host is down, return the job."""
        self._resolves_to(monkeypatch)
        # No "contact_residues" key: epitope_db could not read the structure.
        monkeypatch.setattr(
            "scout.epitope_db.fetch_known_binders",
            lambda *a, **k: [dict(self._BINDER)],
        )
        job_id = _upload_two_chain_job(client)
        assert (
            client.post(
                "/scout/analyze", json={"job_id": job_id, "chain": "A"}
            ).status_code
            == 200
        )
        return TMP / job_id

    def test_the_file_records_an_absence_not_a_zero(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        job_dir = self._analyze_during_outage(client, monkeypatch)
        cache = json.loads((job_dir / "analyze_cache.json").read_text())

        assert "contact_residues" not in cache["known_binders"][0], (
            "an interface that was never computed was written to disk as an "
            "empty one; nothing downstream can tell it from a real answer"
        )
        assert cache["uniprot_id"] == "P00001", (
            "without the accession, a later reader has no way to ask for the "
            "interface that has since been computed"
        )

    def test_the_overlap_heals_once_the_download_recovers(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """The end, not the means: the binder comes back into the report.

        No re-analyze. Recording an absence is only worth doing if something
        later reads it as one.
        """
        job_dir = self._analyze_during_outage(client, monkeypatch)

        from scout.routes import _get_binder_overlaps

        assert _get_binder_overlaps(job_dir, CHAIN_RESIDUES["A"], "A") == []

        recovered = {**self._BINDER, "contact_residues": CHAIN_RESIDUES["A"][:3]}
        monkeypatch.setattr(
            "scout.epitope_db.cached_binders", lambda *a, **k: [recovered]
        )

        overlaps = _get_binder_overlaps(job_dir, CHAIN_RESIDUES["A"], "A")
        assert overlaps, (
            "the antibody stayed missing from the feasibility overlap after "
            "the coordinate host recovered; the outage is frozen into the job"
        )
        assert overlaps[0]["pdb_id"] == "1ABC"
        assert overlaps[0]["overlap_count"] == 3

    def test_a_disk_copy_with_nothing_repairable_asks_epitope_db_nothing(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """The normal path must stay a pure file read.

        SIX binders, deliberately more than _MAX_CONTACT_STRUCTURES (5). Only the
        top five are ever given an interface, so the sixth has no
        ``contact_residues`` key permanently -- and an earlier version of this
        guard asked "does EVERY binder have the key", which for any target with
        more than five is unsatisfiable forever. It passed only because its
        fixture had a single binder, so it could not fail for the reason it
        named, while the real route asked epitope_db on every render.
        """
        self._resolves_to(monkeypatch)
        resolved = [
            {**self._BINDER, "pdb_id": f"1AB{i}",
             "contact_residues": CHAIN_RESIDUES["A"][:3]}
            for i in range(self._CAP)
        ]
        # Past the cap: no key, and no lookup will ever give it one.
        beyond = {**self._BINDER, "pdb_id": "9ZZZ"}
        monkeypatch.setattr(
            "scout.epitope_db.fetch_known_binders",
            lambda *a, **k: [*resolved, beyond],
        )
        job_id = _upload_two_chain_job(client)
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        asked = []
        monkeypatch.setattr(
            "scout.epitope_db.cached_binders",
            lambda *a, **k: asked.append(a) or None,
        )

        from scout.routes import _get_binder_overlaps

        overlaps = _get_binder_overlaps(TMP / job_id, CHAIN_RESIDUES["A"], "A")
        assert len(overlaps) == self._CAP
        assert asked == [], (
            "a disk copy whose only missing interfaces are past the cap still "
            "went back to epitope_db; that is every render, forever"
        )

    def test_feasibility_never_reaches_for_the_network_lookup(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """The repair must not put an uncapped network call on this route.

        /feasibility/analyze holds no anon_compute_slot, unlike /analyze. On a
        cold worker ``fetch_known_binders`` is a 12 s RCSB search plus a 60 s
        SAbDab summary fetch plus a round of coordinate downloads, against a
        120 s gunicorn timeout and two sync workers -- so reaching for it here
        trades a stale overlap for a route that can eat a worker. Only the
        in-process cache read is allowed.
        """
        job_dir = self._analyze_during_outage(client, monkeypatch)

        def _tripwire(*a, **k):
            raise AssertionError(
                "the feasibility path called fetch_known_binders; on a cold "
                "worker that is a multi-upstream network call outside the "
                "compute slot"
            )

        monkeypatch.setattr("scout.epitope_db.fetch_known_binders", _tripwire)

        from scout.routes import _get_binder_overlaps

        # Degrades to the disk copy rather than raising or fetching.
        assert _get_binder_overlaps(job_dir, CHAIN_RESIDUES["A"], "A") == []

    def test_a_cold_worker_degrades_to_the_disk_copy(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """``cached_binders`` returns None when this worker never looked up the
        accession -- a restart, a deploy, or simply the other of the two worker
        processes. That is not an error and not a zero; it means "no better
        answer here", and the incomplete disk copy is what gets served.

        Asserting the complete binder SURVIVES is the point. Returning [] for
        everything would also "not raise", and an earlier version of this guard
        asserted exactly that and would have passed against a repair pass that
        threw every binder away.
        """
        self._resolves_to(monkeypatch)
        good = {**self._BINDER, "pdb_id": "2GOOD",
                "contact_residues": CHAIN_RESIDUES["A"][:3]}
        monkeypatch.setattr(
            "scout.epitope_db.fetch_known_binders",
            lambda *a, **k: [good, dict(self._BINDER)],
        )
        job_id = _upload_two_chain_job(client)
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        monkeypatch.setattr(
            "scout.epitope_db.cached_binders", lambda *a, **k: None
        )

        from scout.routes import _get_binder_overlaps

        overlaps = _get_binder_overlaps(TMP / job_id, CHAIN_RESIDUES["A"], "A")
        assert [o["pdb_id"] for o in overlaps] == ["2GOOD"], (
            "a cold worker dropped the binder that DID have its interface; "
            "the fallback must be the incomplete answer, not no answer"
        )


class TestChainIdIsValidatedAtTheBoundary:
    """The boundary rejects what is unsafe to carry, not what looks unusual.

    A chain id is whatever byte sits in PDB column 22 and ``parse_pdb`` hands it
    to the dropdown untouched, so ``_``, ``-``, ``.``, ``=`` and ``@`` are all
    ids this app itself offers — an alphanumeric guard 400s five of them and
    blames the user for the app's own output. Every character in
    ``PARSER_REACHABLE`` below was executed against the parser and round-trips
    upload into the chain list.

    CSV formula injection is what motivated the alphanumeric rule, and dropping
    it is a KNOWN, ACCEPTED gap, not a solved problem. Ownership narrows it (a
    job dir is stamped with its session's owner key and every read goes through
    ``resolve_owned_job_dir``) but does not close it: a user who passes their own
    results file on carries the crafted cell with it. Closing it means escaping
    on write AND before the cache comparison, so both sides still match.
    Tracked separately; do not read these tests as saying the risk is gone.
    """

    # Every one of these is a single legal PDB column-22 byte that parse_pdb
    # puts in the dropdown. Verified by execution, not assumed.
    PARSER_REACHABLE = ["_", "-", ".", "=", "@", "+", "|", "*", "1", "a"]
    # Control characters must be INTERNAL to be a real case: the routes call
    # .strip() first, and Python counts \x1c-\x1f as whitespace, so a trailing
    # one is removed before validation and "A\x1f" is simply chain "A".
    UNSAFE = ["", "A\nB", "A\rB", "A\tB", "A\x00B", "A\x1fB", "A" * 65]

    @pytest.mark.parametrize("chain", PARSER_REACHABLE)
    def test_parser_reachable_ids_are_not_refused(self, client, reap_jobs, chain):
        """A 400 here means the app refused a chain it would itself offer.

        The two-chain fixture has chains A and B, so these get 422 "not found"
        from the pipeline — which is the honest answer, names the available
        chains, and is the layer that should own that decision.
        """
        job_id = _upload_two_chain_job(client)
        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": chain})
        assert resp.status_code != 400, (chain, resp.status_code, resp.data)

    def test_a_parser_reachable_id_analyses_end_to_end(
        self, client, stub_pipeline, reap_jobs
    ):
        """Not merely un-refused: a structure whose chain is ``=`` must score."""
        resp = client.post(
            "/scout/upload",
            data={"file": (io.BytesIO(_pdb_two_chains(chains=("=", "-"))), "t.pdb")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200, resp.data
        job_id = resp.get_json()["job_id"]
        assert "=" in [c["id"] for c in resp.get_json()["chains"]]

        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "="})
        assert resp.status_code == 200, resp.data
        assert stub_pipeline == ["="]

    @pytest.mark.parametrize("bad", UNSAFE)
    def test_json_routes_reject_unsafe(self, client, reap_jobs, bad):
        """Assert the REASON, not just the status.

        /scout/feasibility/analyze answers 400 for a second, unrelated reason
        (no epitope supplied), so a status-only assertion passed with the chain
        guard deleted — QC round 3 proved it by deleting the guard and watching
        the whole suite stay green. The message is what distinguishes them.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        for route in ("/scout/analyze", "/scout/feasibility/analyze"):
            resp = client.post(route, json={"job_id": job_id, "chain": bad})
            assert resp.status_code == 400, (route, bad, resp.status_code, resp.data)
            assert "valid chain id" in resp.get_json()["error"], (
                route, bad, resp.get_json()
            )

    @pytest.mark.parametrize("bad", UNSAFE)
    def test_the_feasibility_download_rejects_unsafe(self, client, reap_jobs, bad):
        """A third kind of chain-taking route: a query parameter, not a body.

        This class enumerated JSON and SSE routes only, so when
        /scout/feasibility/download gained ?chain= it landed outside the guard
        and took an unvalidated chain for several commits.

        A value that strips to empty is NOT a rejection here: the route reads
        it as "no chain named" and falls through to the results.csv heuristic.
        Branching on ``.strip()`` rather than on ``""`` so that a
        whitespace-only entry added to UNSAFE lands on the fallthrough, which
        is what this route does with it. The job is given a servable CSV so
        that case reaches the heuristic instead of 404ing at a missing file.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        _write_feasibility_csv(TMP / job_id, "A")
        resp = client.get(
            f"/scout/feasibility/download/{job_id}", query_string={"chain": bad}
        )
        if not bad.strip():
            assert resp.status_code == 200, (
                "a blank ?chain= must mean 'no chain named' and fall through "
                f"to the heuristic, not be validated: {resp.status_code} "
                f"{resp.get_data(as_text=True)[:200]}"
            )
            return
        assert resp.status_code == 400, (bad, resp.status_code, resp.data)
        assert "valid chain id" in resp.get_json()["error"], (bad, resp.get_json())

    @pytest.mark.parametrize("bad", UNSAFE)
    def test_sse_routes_reject_unsafe(self, client, reap_jobs, bad):
        """Assert on the MESSAGE, not just ``stage == error``.

        The version of this test QC round 2 killed checked only the stage, and
        the stub raises "Chain not found" as its own first event — so it passed
        with the guard removed entirely, and passed on pre-fix main too. The
        message is the only thing that distinguishes the guard from the noise.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        for route in ("/scout/progress", "/scout/feasibility/progress"):
            resp = client.get(route, query_string={"job_id": job_id, "chain": bad})
            payload = json.loads(resp.get_data(as_text=True).split("data: ", 1)[1])
            assert payload["stage"] == "error", (route, bad, payload)
            assert "valid chain id" in payload["msg"], (route, bad, payload)

    def test_a_newline_chain_cannot_forge_an_sse_frame(self, client, reap_jobs):
        """Defence in depth, NOT the thing holding this closed.

        json.dumps in every SSE emitter is what makes frame forging impossible,
        and this passes with _valid_chain deleted entirely. It is here so the
        property stays pinned if an emitter is ever rewritten to interpolate
        the chain by hand — not as evidence that the boundary check earns it.
        """
        job_id = _upload_two_chain_job(client)
        resp = client.get(
            "/scout/progress",
            query_string={"job_id": job_id, "chain": 'A\n\ndata: {"stage": "done"}'},
        )
        body = resp.get_data(as_text=True)
        assert body.count("data: ") == 1, body
        assert '"stage": "done"' not in body, body

    def test_real_chain_ids_still_work(self, client, stub_pipeline, reap_jobs):
        job_id = _upload_two_chain_job(client)
        for chain in ("A", "B"):
            resp = client.post(
                "/scout/analyze", json={"job_id": job_id, "chain": chain}
            )
            assert resp.status_code == 200, (chain, resp.data)


def test_flags_column_list_matches_the_pipeline():
    """``scout.flags`` hand-copies the column list and asks humans to keep it so.

    Nothing checked that until now. It matters more since ``chain_id`` joined the
    list: the annotated CSV writers build their rows from the columns flags.py
    declares, so a drift there silently drops the stamp back out of the file.
    """
    from scout.pipeline import CSV_COLUMNS

    assert _CSV_COLUMNS_BASE == CSV_COLUMNS, (
        "scout/flags.py _CSV_COLUMNS_BASE has drifted from scout/pipeline.py CSV_COLUMNS"
    )


class TestFeasibilityIsChainScoped:
    """``/scout/feasibility`` resolves epitope_id against results.csv too."""

    def _login(self, client):
        _login(client)

    @pytest.fixture
    def spy_feasibility(self, monkeypatch):
        """Record what the feasibility pipeline is asked to score, and stop there."""
        calls: list[dict] = []

        def _run(pdb_path, chain_id, epitope_residues, progress_callback=None):
            calls.append({"chain": chain_id, "residues": list(epitope_residues)})
            raise ValueError("stubbed feasibility pipeline")

        monkeypatch.setattr("scout.pipeline.run_feasibility_pipeline", _run)
        return calls

    def test_epitope_id_is_not_resolved_against_another_chain(
        self, client, stub_pipeline, spy_feasibility, reap_jobs
    ):
        self._login(client)
        job_id = _upload_two_chain_job(client)
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        resp = client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "B", "epitope_id": 1},
        )
        assert not spy_feasibility, (
            "feasibility resolved epitope_id 1 against chain A's results while "
            f"analysing chain B: {spy_feasibility}"
        )
        assert resp.status_code == 404, resp.data

    def test_epitope_id_still_resolves_for_the_matching_chain(
        self, client, stub_pipeline, spy_feasibility, reap_jobs
    ):
        """The lookup must keep working for the chain that was actually scored."""
        self._login(client)
        job_id = _upload_two_chain_job(client)
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "A", "epitope_id": 1},
        )
        assert spy_feasibility, "the matching chain's epitope_id stopped resolving"
        assert spy_feasibility[0]["residues"] == CHAIN_RESIDUES["A"]
class TestEveryGuardFailsWhenItIsRemoved:
    """One test per guard that a mutation pass could delete unnoticed.

    A fix nothing fails on is indistinguishable from a fix that was never made,
    so each test here goes red when its guard is deleted.
    """

    def test_a_stolen_results_file_is_a_409_not_an_empty_200(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """Losing the race must not destroy the winner's derived files.

        analyze re-reads results.csv after the pipeline step. If another chain
        overwrote it in between, everything downstream read zero epitopes,
        concluded "nothing qualifying", DELETED epitopes*.csv and truncated
        results_annotated.csv to a header - then served that as a 200.
        """
        import scout.routes as routes

        job_id = _upload_two_chain_job(client)
        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        assert resp.status_code == 200, resp.data
        job_dir = TMP / job_id
        assert (job_dir / "epitopes_annotated.csv").exists()
        before = (job_dir / "results_annotated.csv").read_bytes()

        # Simulate the interleaving: results.csv becomes chain B's between the
        # cache gate and the read below it.
        real = routes._results_csv_for_chain
        seen = []

        def _steal(jd, cid):
            seen.append(cid)
            if len(seen) > 1:
                _write_results_csv(jd, "B")
            return real(jd, cid)

        monkeypatch.setattr(routes, "_results_csv_for_chain", _steal)
        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        assert resp.status_code == 409, (resp.status_code, resp.data)
        assert (job_dir / "epitopes_annotated.csv").exists(), "deleted the winner's file"
        assert (job_dir / "results_annotated.csv").read_bytes() == before

    def test_binder_overlaps_are_actually_returned_for_the_right_chain(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """Gutting _get_binder_overlaps to ``return []`` passed every test.

        Round 1 added its chain gate and round 1's test covered the gate. That
        it still returns real overlaps for the MATCHING chain was covered by
        nothing, so the quietest possible regression - every known binder
        silently dropped from the report - had nothing standing in its way.
        """
        from scout.routes import _get_binder_overlaps

        binder = {
            "pdb_id": "1ABC",
            "binder_type": "antibody",
            "species": "human",
            "affinity": "1 nM",
            "contact_residues": CHAIN_RESIDUES["A"][:3],
        }
        monkeypatch.setattr(
            "scout.epitope_db.fetch_known_binders", lambda *a, **k: [binder]
        )
        monkeypatch.setattr(
            "scout.epitope_db.resolve_uniprot_id",
            lambda *a, **k: {
                "uniprot_id": "P00001",
                "protein_name": "Test",
                "identity_pct": "100",
                "source": "dbref",
            },
        )
        job_id = _upload_two_chain_job(client)
        assert (
            client.post(
                "/scout/analyze", json={"job_id": job_id, "chain": "A"}
            ).status_code
            == 200
        )
        job_dir = TMP / job_id

        overlaps = _get_binder_overlaps(job_dir, CHAIN_RESIDUES["A"], "A")
        assert overlaps, "the matching chain's binder overlaps came back empty"
        assert overlaps[0]["pdb_id"] == "1ABC", overlaps
        assert overlaps[0]["overlap_count"] == 3, overlaps

        # ...and the round-1 gate still holds for the wrong chain.
        assert _get_binder_overlaps(job_dir, CHAIN_RESIDUES["A"], "B") == []

    def test_a_header_only_results_csv_is_a_miss(self, client, stub_pipeline, reap_jobs):
        """A file with a header and no data rows cannot name its chain.

        A mutation making this a HIT passed 21/21: the route answered 200 with
        zero epitopes and never ran the pipeline again for that job, ever.
        """
        job_id = _upload_two_chain_job(client)
        job_dir = TMP / job_id
        with (job_dir / "results.csv").open("w", newline="") as fh:
            csv.DictWriter(fh, fieldnames=_CSV_COLUMNS_BASE).writeheader()

        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        assert resp.status_code == 200, resp.data
        assert stub_pipeline == ["A"], "the pipeline never re-ran"
        assert _residue_numbers(resp) == CHAIN_RESIDUES["A"]

    def test_sse_separates_no_results_from_unknown_epitope(
        self, client, stub_pipeline, reap_jobs
    ):
        """Round 1's message fix over-claimed and round 2 caught it.

        A bad epitope_id on a chain that HAS been analysed was told to go and
        analyse that chain - advice pointing at work already done.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        assert (
            client.post(
                "/scout/analyze", json={"job_id": job_id, "chain": "A"}
            ).status_code
            == 200
        )

        # Chain A analysed; epitope 99 is not in it.
        resp = client.get(
            "/scout/feasibility/progress",
            query_string={"job_id": job_id, "chain": "A", "epitope_id": "99"},
        )
        msg = json.loads(resp.get_data(as_text=True).split("data: ", 1)[1])["msg"]
        assert "not in chain A" in msg, msg
        assert "Run epitope analysis" not in msg, msg

        # Chain B never analysed: the original message is the correct one.
        resp = client.get(
            "/scout/feasibility/progress",
            query_string={"job_id": job_id, "chain": "B", "epitope_id": "1"},
        )
        msg = json.loads(resp.get_data(as_text=True).split("data: ", 1)[1])["msg"]
        assert "No Epitope Scout results found for chain B" in msg, msg

    def test_feasibility_progress_will_not_resolve_another_chains_epitope(
        self, client, stub_pipeline, reap_jobs
    ):
        """The SSE feasibility results gate, which no test covered."""
        _login(client)
        job_id = _upload_two_chain_job(client)
        assert (
            client.post(
                "/scout/analyze", json={"job_id": job_id, "chain": "A"}
            ).status_code
            == 200
        )

        resp = client.get(
            "/scout/feasibility/progress",
            query_string={"job_id": job_id, "chain": "B", "epitope_id": "1"},
        )
        body = resp.get_data(as_text=True)
        payload = json.loads(body.split("data: ", 1)[1])
        assert payload["stage"] == "error", payload
        for residue in CHAIN_RESIDUES["A"]:
            assert str(residue) not in body, (residue, body)


class TestTheTemplateMatchesTheBackend:
    """The template change had no test at all: reverting it kept the suite green."""

    def _page(self, client):
        resp = client.get("/scout/")
        assert resp.status_code == 200, resp.status_code
        return resp.get_data(as_text=True)

    def test_feasibility_link_uses_the_scored_chain_not_the_dropdown(self, client):
        page = self._page(client)
        assert "function renderEpitopeTable(epitopes, chain)" in page
        assert (
            "encodeURIComponent(chain || document.getElementById('chain-select').value)"
            in page
        ), "the feasibility link stopped preferring the analysed chain"

    def test_an_error_re_enables_the_analyze_button(self, client):
        """Otherwise any SSE error strands the page on a disabled button."""
        page = self._page(client)
        body = page.split("function showAnalyzeError(", 1)[1].split("\n    }", 1)[0]
        assert "btn.disabled = false" in body, body

    def test_the_dead_top_3_download_link_stays_hidden(self, client):
        """Clearing itself is covered by TestNothingFromThePreviousChainSurvives,
        which checks the shared _clearChainScopedResults, not this call site."""
        page = self._page(client)
        handler = page.split("function _handleAnalysisResult(", 1)[1].split(
            "\n    }", 1
        )[0]
        assert (
            "epitopes.length > 0 ? 'inline-flex' : 'none'" in handler
        ), "the dead top-3 download link came back"
class TestEmptyRunsAndChainIdBounds:
    """A chain that scores nothing is not a collision, and long ids are real."""

    def test_a_chain_that_scores_nothing_is_not_reported_as_a_collision(
        self, client, reap_jobs, monkeypatch
    ):
        """A run that produces no rows is not "another analysis replaced this".

        The first version of the stolen-results guard answered both states with
        the same 409, so a chain with no scoreable surface got a conflict
        message blaming a concurrent request that never happened - and got it
        again on every retry, re-running the pipeline each time.
        """
        def _run_nothing(pdb_path, chain_id, progress_callback=None):
            out = Path(pdb_path).parent / "results.csv"
            with out.open("w", newline="") as fh:
                csv.DictWriter(fh, fieldnames=_CSV_COLUMNS_BASE).writeheader()
            return out

        monkeypatch.setattr("scout.pipeline.run_pipeline", _run_nothing)
        monkeypatch.setattr(
            "scout.epitope_db.resolve_uniprot_id",
            lambda *a, **k: {"uniprot_id": "", "protein_name": "", "identity_pct": "unknown",
         "source": ""},
        )
        monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])
        monkeypatch.setattr("scout.interfaces.detect_interfaces", lambda *a, **k: [])

        job_id = _upload_two_chain_job(client)
        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        assert resp.status_code == 422, (resp.status_code, resp.data)
        error = resp.get_json()["error"]
        assert "No surface patches could be scored for chain A" in error, error
        assert "Another analysis" not in error, error

    def test_a_long_chain_id_the_dropdown_offers_is_not_refused(
        self, client, stub_pipeline, reap_jobs
    ):
        """/scout/upload must not offer a chain /scout/analyze then 400s.

        The cap was 16. A 20-character mmCIF auth_asym_id reached the dropdown
        and came back "job_id and a valid chain id are required" - the app
        refusing its own offer. Verified by execution that the parser passes a
        20-character auth_asym_id straight through.
        """
        resp = client.post(
            "/scout/upload",
            data={"file": (io.BytesIO(_cif_two_chains(("A" * 20, "B"))), "t.cif")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        offered = [c["id"] for c in body["chains"]]
        assert "A" * 20 in offered, offered

        for chain in offered:
            got = client.post(
                "/scout/analyze", json={"job_id": body["job_id"], "chain": chain}
            )
            assert got.status_code != 400, (
                f"upload offered chain {chain!r} that analyze refuses: {got.data}"
            )

    def test_an_absurd_chain_id_is_still_refused(self, client, reap_jobs):
        """The residual, stated rather than hidden.

        The cap is 64, so a crafted structure CAN still carry a chain id the
        dropdown offers and the boundary refuses - it just has to be longer than
        any real assembly's. That is a clean 400 on an absurd file, not a wrong
        answer, and it is the deliberate trade for not carrying unbounded
        free text into CSV cells and log lines.
        """
        resp = client.post(
            "/scout/upload",
            data={"file": (io.BytesIO(_cif_two_chains(("A" * 65, "B"))), "t.cif")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200, resp.data
        body = resp.get_json()
        got = client.post(
            "/scout/analyze", json={"job_id": body["job_id"], "chain": "A" * 65}
        )
        assert got.status_code == 400, got.data
        assert "valid chain id" in got.get_json()["error"]

class TestNothingFromThePreviousChainSurvives:
    """No element, file or error message may still describe the last chain."""

    # Every element _clearChainScopedResults is responsible for. Round 4 found
    # two missing (flag cards, PPI) after three earlier rounds had each found
    # one; naming them here means a deletion from that function fails a test
    # instead of quietly stranding one more chain's data on the page.
    CHAIN_SCOPED_ELEMENTS = [
        "viewer-container",
        "epitope-legend",
        "epitope-table-body",
        "known-binders-body",
        "known-binders-section",
        "uniprot-info",
        "uniprot-bar",
        "flag-ref-grid",
        "flag-reference",
    ]

    def _clear_fn(self, client):
        page = client.get("/scout/").get_data(as_text=True)
        assert "function _clearChainScopedResults()" in page, "the clear fn is gone"
        return page.split("function _clearChainScopedResults()", 1)[1].split(
            "\n    }", 1
        )[0]

    @pytest.mark.parametrize("element_id", CHAIN_SCOPED_ELEMENTS)
    def test_every_chain_scoped_element_is_cleared(self, client, element_id):
        assert element_id in self._clear_fn(client), (
            f"{element_id} is no longer cleared between chains"
        )

    def test_the_clear_runs_before_anything_renders(self, client):
        """Unconditional and first, or it does not work.

        renderViewer is async and unawaited. Clearing inside it, or inside a
        branch only some responses take, leaves the previous chain's table and
        flag cards under the new chain's UniProt bar for the whole load - and
        forever if renderViewer returns early on a failed /scout/pdb fetch.
        """
        page = client.get("/scout/").get_data(as_text=True)
        handler = page.split("function _handleAnalysisResult(", 1)[1].split(
            "\n    }", 1
        )[0]
        assert "_clearChainScopedResults();" in handler, handler

        before = handler.split("_clearChainScopedResults();", 1)[0]
        for rendered in ("renderViewer(", "renderKnownBinders(", "uniprot-info"):
            assert rendered not in before, (
                f"{rendered} runs before the clear: {before}"
            )
        # Unconditional: nothing may gate it.
        assert "if" not in before.split("results-section", 1)[-1], before

    def test_a_chain_that_scores_nothing_leaves_no_downloadable_file(
        self, client, reap_jobs, monkeypatch
    ):
        """The 422 early return must not strand the previous chain's CSVs.

        /scout/download takes no chain parameter and falls back through four
        files, so anything left in the job dir is served as the current result.
        """
        job_id = _upload_two_chain_job(client)

        # Chain A scores normally and leaves derived files behind.
        def _run_ok(pdb_path, chain_id, progress_callback=None):
            _write_results_csv(Path(pdb_path).parent, chain_id)
            return Path(pdb_path).parent / "results.csv"

        for target in ("scout.pipeline.run_pipeline",):
            monkeypatch.setattr(target, _run_ok)
        monkeypatch.setattr(
            "scout.epitope_db.resolve_uniprot_id",
            lambda *a, **k: {"uniprot_id": "", "protein_name": "", "identity_pct": "unknown",
         "source": ""},
        )
        monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])
        monkeypatch.setattr("scout.interfaces.detect_interfaces", lambda *a, **k: [])

        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 200
        assert client.get(f"/scout/download/{job_id}").status_code == 200

        # Chain B scores nothing.
        def _run_nothing(pdb_path, chain_id, progress_callback=None):
            out = Path(pdb_path).parent / "results.csv"
            with out.open("w", newline="") as fh:
                csv.DictWriter(fh, fieldnames=_CSV_COLUMNS_BASE).writeheader()
            return out

        monkeypatch.setattr("scout.pipeline.run_pipeline", _run_nothing)
        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "B"})
        assert resp.status_code == 422, resp.data

        # The top-3 files are chain A's and nothing replaced them, so they must
        # be gone entirely.
        got = client.get(f"/scout/download/{job_id}")
        assert got.status_code == 404, (
            f"top-3 download still serves chain A's file: {got.data[:200]}"
        )

        # "All patches" falls back to results.csv, which chain B's own run
        # rewrote header-only. Serving that empty file is truthful; serving
        # chain A's numbers under it is the bug. Assert the property, not 404.
        got = client.get(f"/scout/download/{job_id}?full=1")
        body = got.get_data(as_text=True)
        for residue in CHAIN_RESIDUES["A"]:
            assert f"ALA{residue}" not in body, (
                f"all-patches download still carries chain A's residue {residue}: "
                f"{body[:300]}"
            )

    def test_a_conflicting_run_keeps_the_winners_files(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """The 409 must NOT clear: those files belong to the concurrent run."""
        import scout.routes as routes

        job_id = _upload_two_chain_job(client)
        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 200
        job_dir = TMP / job_id
        before = (job_dir / "epitopes_annotated.csv").read_bytes()

        real = routes._results_csv_for_chain
        seen = []

        def _steal(jd, cid):
            seen.append(cid)
            if len(seen) > 1:
                _write_results_csv(jd, "B")
            return real(jd, cid)

        monkeypatch.setattr(routes, "_results_csv_for_chain", _steal)
        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
        assert resp.status_code == 409, resp.data
        assert (job_dir / "epitopes_annotated.csv").read_bytes() == before

    def test_a_blank_chain_id_cell_is_a_miss_not_a_collision(self, client, reap_jobs):
        """A blank cell names no chain, so it is a can't-say, not another chain."""
        from scout.routes import _results_csv_chain_id

        job_id = _upload_two_chain_job(client)
        job_dir = TMP / job_id
        _write_results_csv(job_dir, "A")
        rows = list(csv.DictReader((job_dir / "results.csv").open(newline="")))
        rows[0]["chain_id"] = ""
        with (job_dir / "results.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS_BASE)
            w.writeheader()
            w.writerows(rows)

        assert _results_csv_chain_id(job_dir) is None

    def test_the_feasibility_404_names_the_chain(
        self, client, stub_pipeline, reap_jobs
    ):
        """Round 4: the chain name could be dropped with the suite still green."""
        _login(client)
        job_id = _upload_two_chain_job(client)
        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 200

        resp = client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "B", "epitope_id": 1},
        )
        assert resp.status_code == 404, resp.data
        error = resp.get_json()["error"]
        assert "chain B" in error, error
class TestCleanupIsBoundToThePipelineNotTheRoute:
    """run_pipeline has two callers, and /scout/progress is the one that runs."""

    def test_progress_alone_invalidates_the_previous_chains_downloads(
        self, client, stub_pipeline, reap_jobs
    ):
        """run_pipeline has TWO callers and only one used to clean up.

        /scout/progress is the caller that actually executes the pipeline, and
        its done event hands the browser a download_url immediately. Scoring
        chain B through progress while chain A's epitopes_annotated.csv sat
        beside it meant /scout/download served chain A's top-3 as chain B's
        result - on every ordinary chain switch, and permanently whenever
        /scout/analyze never landed.
        """
        job_id = _upload_two_chain_job(client)
        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 200

        # Read chain A's top-3 off disk rather than through /scout/download.
        # send_file keeps the handle open, and on Windows that makes the very
        # unlink under test fail with WinError 32 — a test artefact, not the
        # behaviour. (The route handles that: the failure is logged and the run
        # still completes. See _remove_derived_result_files.)
        top3_path = TMP / job_id / "epitopes_annotated.csv"
        assert "ALA10" in top3_path.read_text(), "chain A's top-3 is not there"

        # Score chain B through the SSE route only - no /scout/analyze.
        resp = client.get(
            "/scout/progress", query_string={"job_id": job_id, "chain": "B"}
        )
        body = resp.get_data(as_text=True)
        assert "done" in body, body
        assert stub_pipeline == ["A", "B"], stub_pipeline

        after = client.get(f"/scout/download/{job_id}")
        text = after.get_data(as_text=True) if after.status_code == 200 else ""
        for residue in CHAIN_RESIDUES["A"]:
            assert f"ALA{residue}" not in text, (
                f"download still serves chain A's residue {residue} after chain B "
                f"was scored through /scout/progress: {after.status_code} {text[:200]}"
            )

    def test_reset_all_still_clears_every_chain_scoped_element(self, client):
        """resetAll delegates now; it must not silently stop.

        Round 5 measured that resetAll could drop the call with the whole suite
        green - a gap the shared-helper refactor created.
        """
        page = client.get("/scout/").get_data(as_text=True)
        reset = page.split("function resetAll()", 1)[1].split("\n    }", 1)[0]
        assert "_clearChainScopedResults();" in reset, reset


    def test_a_failed_cleanup_does_not_lose_the_run(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """Invalidating a stale file is best-effort; scoring is not.

        The cleanup runs immediately after every run_pipeline, so an unguarded
        unlink puts a file-system error directly in the path of a completed
        analysis. Windows hits this for real when a preceding /scout/download
        still holds the handle (WinError 32), and it turned the SSE stream into
        an error event instead of delivering results.
        """
        import scout.routes as routes

        real_unlink = Path.unlink
        # Read off the module, not restated here: a hard-coded set silently
        # stops covering the next file added to the cleanup, and the WinError 32
        # this guards against applies to every one of them.
        derived = set(routes._DERIVED_RESULT_FILES)

        fired = []

        def _boom(self, missing_ok=False):
            if self.name in derived:
                fired.append(self.name)
                raise PermissionError(32, "The process cannot access the file")
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _boom)

        job_id = _upload_two_chain_job(client)
        resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        assert resp.status_code == 200, (
            f"a failed cleanup destroyed a successful run: {resp.data}"
        )
        assert _residue_numbers(resp) == CHAIN_RESIDUES["A"]
        # Without this the guard passes vacuously whenever the cleanup stops
        # running or _DERIVED_RESULT_FILES empties: _boom never fires, nothing
        # raises, and "the run survived" proves nothing about surviving a
        # failed unlink.
        assert fired, (
            "no unlink was attempted, so this never exercised a failed cleanup"
        )

    def test_an_unknown_epitope_id_says_so_on_the_json_route_too(
        self, client, stub_pipeline, reap_jobs
    ):
        """The JSON feasibility route got the same 3-way split as the SSE one.

        It used to answer "epitope_residues or epitope_id is required" when
        epitope_id HAD been supplied and simply was not in this chain's
        results - telling the user to send a field they already sent.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 200

        # Chain A IS analysed, so the results gate passes; epitope 99 is simply
        # not in it, which is the branch under test.
        resp = client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "A", "epitope_id": 99},
        )
        assert resp.status_code == 404, (resp.status_code, resp.data)
        error = resp.get_json()["error"]
        assert "Epitope 99 is not in chain A" in error, error
        assert "is required" not in error, error


class TestFeasibilityCsvNamesItsChain:
    """The writer must stamp ``chain_id``; the download gate reads it.

    Strip the stamp and the gate cannot tell which chain the file describes,
    so it refuses every request that names one.
    """

    def test_the_column_list_carries_chain_id(self):
        from scout.pipeline import FEASIBILITY_CSV_COLUMNS

        assert "chain_id" in FEASIBILITY_CSV_COLUMNS, FEASIBILITY_CSV_COLUMNS

    def test_the_writer_row_matches_the_declared_columns(self):
        """Read the writer's row literal out of the source, not from a stub.

        run_feasibility_pipeline cannot execute here (freesasa is absent from
        this venv), and a mock deep enough to reach the writer would prove only
        that the mock works. Parsing the actual dict literal catches both real
        failures: a chain_id column with nothing writing it, and a stamped row
        whose keys no longer match FEASIBILITY_CSV_COLUMNS - which is a
        csv.DictWriter ValueError on the live path in CI.
        """
        import ast
        import inspect

        import scout.pipeline as pipeline

        source = inspect.getsource(pipeline.run_feasibility_pipeline)
        tree = ast.parse(textwrap.dedent(source))

        literals = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            and any(
                isinstance(k, ast.Constant) and k.value == "composite_feasibility"
                for k in node.keys
            )
        ]
        assert len(literals) == 1, f"expected one feasibility row literal, got {len(literals)}"

        keys = {
            k.value for k in literals[0].keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        assert keys == set(pipeline.FEASIBILITY_CSV_COLUMNS), (
            "the feasibility row and its column list have drifted; "
            f"row-only={keys - set(pipeline.FEASIBILITY_CSV_COLUMNS)} "
            f"columns-only={set(pipeline.FEASIBILITY_CSV_COLUMNS) - keys}"
        )
        assert "chain_id" in keys, "the feasibility CSV stopped stamping its chain"

        # The VALUE, not just the key: the route-level tests stub this
        # pipeline out with a stub that stamps its own chain_id argument, so a
        # real writer stamping anything else would not show up there.
        chain_value = next(
            v for k, v in zip(literals[0].keys, literals[0].values)
            if isinstance(k, ast.Constant) and k.value == "chain_id"
        )
        assert isinstance(chain_value, ast.Name) and chain_value.id == "chain_id", (
            "the feasibility row no longer stamps its chain_id argument "
            "verbatim; feasibility_download compares that value against the "
            f"chain in the request: {ast.dump(chain_value)}"
        )


class TestTheFeasibilityDownloadRefusesAfterAChainSwitch:
    """The reported bug, and the regression fixing it first caused.

    ``feasibility_results.csv`` was written by its own pipeline and deleted by
    nobody, and ``/scout/feasibility/download`` gated on ``exists()`` alone —
    the same shape as the ``/scout/analyze`` bug this file is named for.
    Scoring chain A's feasibility, then analysing chain B, left the download
    handing back chain A's row at HTTP 200.

    The first fix added the file to ``_remove_derived_result_files``. That was
    reverted: it also fired on a SAME-chain rescore, which through the browser
    is every Analyze click (the page reaches /scout/analyze only from
    /scout/progress's done handler, and /scout/progress rescores
    unconditionally), and it threw away a result that was still correct. Both
    directions are pinned below.
    """

    def _score_feasibility(self, client, job_id, chain):
        resp = client.post(
            "/scout/feasibility/analyze",
            json={
                "job_id": job_id,
                "chain": chain,
                "epitope_residues": CHAIN_RESIDUES[chain],
            },
        )
        assert resp.status_code == 200, resp.data
        # Read the fixture's own output off DISK, never through the download
        # route: send_file keeps the handle open, and on Windows that makes the
        # very unlink under test fail with WinError 32 — a test artefact, not
        # the behaviour. Same reason as the top-3 test above.
        csv_path = TMP / job_id / "feasibility_results.csv"
        assert f"ALA{CHAIN_RESIDUES[chain][0]}" in csv_path.read_text(), (
            f"the fixture never wrote chain {chain}'s feasibility row"
        )
        return csv_path

    def test_it_is_refused_after_another_chain_is_analysed(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """The live bug: chain A's feasibility row served after chain B ran."""
        _stub_feasibility_pipeline(monkeypatch)
        _login(client)
        job_id = _upload_two_chain_job(client)

        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 200
        self._score_feasibility(client, job_id, "A")

        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "B"}
        ).status_code == 200
        assert stub_pipeline == ["A", "B"], (
            f"pipeline ran for {stub_pipeline}; chain B never rescored, so the "
            "state this test is about was never reached"
        )

        resp = client.get(f"/scout/feasibility/download/{job_id}")
        # Assert the code, not just the absence of chain A in the body: folding
        # every non-200 to an empty string would let a 500, a redirect or a
        # permanently broken route read as success.
        assert resp.status_code == 404, (
            f"expected the download to refuse; got {resp.status_code} "
            f"{resp.get_data(as_text=True)[:200]}"
        )
        # No residue-loop here: a 404 body is the JSON error, which can never
        # contain a residue, so such a loop would assert nothing. What proves
        # chain A's rows are not served is the refusal itself.

    def test_rescoring_the_same_chain_keeps_the_feasibility_result(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """The regression the first fix caused, pinned so it cannot come back.

        /scout/progress rescores UNCONDITIONALLY and is the only way the
        browser reaches an analysis, so deleting the feasibility CSV on rewrite
        threw one away on every Analyze click. Nothing about a same-chain
        rescore invalidates the row: it names the residues it scored, and
        ``epitope_id`` is the constant 1 whatever epitope was picked. Put
        "feasibility_results.csv" back in _DERIVED_RESULT_FILES and this dies.
        """
        _stub_feasibility_pipeline(monkeypatch)
        _login(client)
        job_id = _upload_two_chain_job(client)

        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 200
        csv_path = self._score_feasibility(client, job_id, "A")
        before = csv_path.read_bytes()

        resp = client.get(
            "/scout/progress", query_string={"job_id": job_id, "chain": "A"}
        )
        assert "done" in resp.get_data(as_text=True), resp.data
        assert stub_pipeline == ["A", "A"], (
            f"progress ran for {stub_pipeline}; it must rescore unconditionally, "
            "or this is not measuring the case that caused the regression"
        )
        assert csv_path.read_bytes() == before, (
            "a same-chain rescore destroyed a feasibility result that was still "
            "correct — the regression the cleanup-tuple entry caused"
        )
        got = client.get(f"/scout/feasibility/download/{job_id}?chain=A")
        assert got.status_code == 200, (
            "the download refused a chain-A result after a chain-A rescore: "
            f"{got.status_code} {got.get_data(as_text=True)[:200]}"
        )


class TestTheChainStampParserItself:
    """``_csv_chain_id`` is the whole basis of the download's chain check.

    Every other test in this file feeds it CSVs written by ``csv.DictWriter``,
    which always terminates rows with CRLF — so the row shapes that only a
    direct call can produce are unreached, and the ``newline=""`` this parser
    depends on is otherwise pinned by nothing.
    """

    @staticmethod
    def _chain_id(payload: bytes):
        from scout.routes import _csv_chain_id

        return _csv_chain_id(payload)

    def test_a_lone_cr_terminates_a_row(self):
        """Without ``newline=""`` csv raises here and the parser returns None.

        A false None is not a safe failure: it makes the file unable to name
        its chain, which the download then refuses for every request that
        names one.
        """
        assert self._chain_id(b"epitope_id,chain_id\r1,B\r") == "B"

    def test_crlf_and_lf_both_parse(self):
        assert self._chain_id(b"epitope_id,chain_id\r\n1,B\r\n") == "B"
        assert self._chain_id(b"epitope_id,chain_id\n1,B\n") == "B"

    def test_cannot_say_is_none_not_a_guess(self):
        assert self._chain_id(b"") is None
        assert self._chain_id(b"epitope_id,chain_id\r\n") is None, "header only"
        assert self._chain_id(b"epitope_id\r\n1\r\n") is None, "no chain_id column"
        assert self._chain_id(b"epitope_id,chain_id\r\n1,\r\n") is None, "blank cell"

    def test_undecodable_bytes_do_not_break_the_parse(self):
        """errors="replace" can garble a VALUE but never invent a delimiter.

        UTF-8 maps no byte >= 0x80 to an ASCII character, so a bad decode
        cannot create or destroy a comma, quote or newline.
        """
        assert self._chain_id(b"epitope_id,chain_id\r\n1,B\r\n\xff\xfe") == "B"


class TestTheDownloadAsksTheRequestWhichChain:
    """The download asks the REQUEST which chain it means, not results.csv.

    ``?chain=`` travels on every link the app mints, so the route compares the
    chain asked for against the file's own stamp instead of inferring it.
    Covered here: that precedence, the link carrying the chain, case
    sensitivity, the refusal when the file cannot name itself, and the
    chainless fallback that must NOT refuse a job with no results.csv.
    """

    def test_a_job_with_no_results_csv_can_still_download(
        self, client, reap_jobs
    ):
        """The None cases mean "cannot say" and must not read as a mismatch.

        A job created on the feasibility page never gets a results.csv, so a
        gate that refused whenever it could not compare would break it.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        job_dir = TMP / job_id
        assert not (job_dir / "results.csv").exists(), "fixture must not analyse"

        _write_feasibility_csv(job_dir, "A", [10, 11])

        resp = client.get(f"/scout/feasibility/download/{job_id}")
        assert resp.status_code == 200, (
            "the chain gate refused a standalone feasibility job that has no "
            f"results.csv to compare against: {resp.get_data(as_text=True)[:200]}"
        )

    def test_a_file_that_cannot_name_its_chain_is_refused_to_a_chain_request(
        self, client, stub_pipeline, reap_jobs
    ):
        """"It did not contradict me" is not "it matches".

        An unstamped, header-only or unreadable CSV cannot be shown to describe
        the chain asked for, so it must be refused rather than served on the
        strength of the comparison being unable to run. This is also the
        concurrent-truncation window: `run_feasibility_pipeline` writes with
        open("w"), so a reader can catch the file empty mid-rewrite.

        results.csv is deliberately present and holds the SAME chain being
        asked for. Without that, a gate written as
        ``_csv_chain_id(payload) or _results_csv_chain_id(job_dir)`` — letting
        an unnameable file inherit results.csv's chain, which is the original
        bug — would refuse here anyway, for the wrong reason, and pass.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        job_dir = TMP / job_id

        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 200
        from scout.routes import _results_csv_chain_id

        assert _results_csv_chain_id(job_dir) == "A", (
            "this test only bites while results.csv can supply the chain the "
            "file cannot"
        )

        # Header only: exactly what a reader sees mid-truncation.
        (job_dir / "feasibility_results.csv").write_text(
            "epitope_id,chain_id,residues\n"
        )

        resp = client.get(f"/scout/feasibility/download/{job_id}?chain=A")
        assert resp.status_code == 404, (
            "a CSV that cannot name its chain was served to an explicit "
            f"request for chain A: {resp.status_code} "
            f"{resp.get_data(as_text=True)[:200]}"
        )
        # The `or "unknown"` in the message is otherwise unpinned: every other
        # refusal test uses a file that CAN name its chain, so dropping it
        # would ship "for chain None." to a user with the suite green.
        assert "unknown" in resp.get_json()["error"], resp.get_json()

    def test_a_chainless_request_also_refuses_a_file_that_cannot_name_itself(
        self, client, stub_pipeline, reap_jobs
    ):
        """The other half of "cannot say" — and a behaviour change, so pinned.

        When the REQUEST names no chain but results.csv does, an unstamped file
        still cannot be shown to match, so it is refused. Only when nothing on
        either side names a chain is it served
        (test_a_job_with_no_results_csv_can_still_download).

        This is the case the two refusals differed on before they were merged
        into one comparison: the earlier form served it.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        job_dir = TMP / job_id

        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 200
        (job_dir / "feasibility_results.csv").write_text(
            "epitope_id,chain_id,residues\n"
        )

        resp = client.get(f"/scout/feasibility/download/{job_id}")
        assert resp.status_code == 404, (
            "a CSV that cannot name its chain was served to a chainless "
            f"request while results.csv named one: {resp.status_code} "
            f"{resp.get_data(as_text=True)[:200]}"
        )

    def test_the_downloaded_filename_names_the_chain(self, client, reap_jobs):
        """Two chains must not land in Downloads under one name.

        The whole branch is about not confusing one chain's scores for
        another's; a user who downloads both and cannot tell the files apart
        without opening them has the same problem one layer out.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        _write_feasibility_csv(TMP / job_id, "B", [60, 61])

        resp = client.get(f"/scout/feasibility/download/{job_id}?chain=B")
        assert resp.status_code == 200, resp.data
        disposition = resp.headers["Content-Disposition"]
        assert "chainB" in disposition, (
            f"the download does not name the chain it carries: {disposition}"
        )
    def test_a_corrupt_chain_stamp_does_not_500(self, client, reap_jobs):
        """The suffix is where a file own bytes reach a response header.

        ``_csv_chain_id`` reads the stamp out of the CSV, and on a chainless
        request it is compared against nothing before being spliced into
        ``download_name``. A CR or LF there raises ValueError inside werkzeug
        (executed against werkzeug 3.1.8), so the ``_valid_chain`` gate on the
        suffix is load-bearing rather than belt-and-braces.

        Reachable because csv quotes the field on write and reads it back
        intact: the stamp round-trips as "A\rB".
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        _write_feasibility_csv(TMP / job_id, "A\rB")

        resp = client.get(f"/scout/feasibility/download/{job_id}")
        assert resp.status_code == 200, resp.data
        disposition = resp.headers["Content-Disposition"]
        assert f"feasibility_{job_id[:8]}.csv" in disposition, (
            "a chain the boundary refuses was spliced into the filename: "
            f"{disposition}"
        )

    def test_the_refusal_names_the_files_chain_not_the_callers_string(self, client, reap_jobs):
        """Same reason as test_the_feasibility_404_names_the_chain above.

        Two things a status-only assertion cannot see. The message must name
        the chain the FILE holds, so a user can tell "wrong chain" from "no
        results yet" — and it must NOT echo the caller's own string, which on
        the chainless path would be a chain they never asked for, and which is
        request input reaching a response body.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        job_dir = TMP / job_id
        _write_feasibility_csv(job_dir, "B", [60, 61])

        resp = client.get(f"/scout/feasibility/download/{job_id}?chain=A")
        assert resp.status_code == 404, resp.data
        error = resp.get_json()["error"]
        assert "chain B" in error, error
        assert "chain A" not in error, (
            f"the refusal echoed the caller's chain back at them: {error}"
        )

    def test_the_chain_comparison_is_case_sensitive(self, client, reap_jobs):
        """PDB chain ids are case-sensitive: `a` and `A` are different chains.

        Every other chain in this file is "A" or "B", so a .upper() or
        .casefold() on either side of the comparison would pass the whole suite
        while merging two real chains.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        job_dir = TMP / job_id

        _write_feasibility_csv(job_dir, "A", [10])

        assert client.get(
            f"/scout/feasibility/download/{job_id}?chain=a"
        ).status_code == 404, "chain 'a' was served chain 'A''s feasibility file"
        assert client.get(
            f"/scout/feasibility/download/{job_id}?chain=A"
        ).status_code == 200

    def test_the_chain_asked_for_beats_the_chain_results_csv_holds(
        self, client, stub_pipeline, reap_jobs
    ):
        """``?chain=`` is the request saying which chain it means.

        Comparing against results.csv is only a fallback, and on its own it is
        wrong in BOTH directions: it refuses a feasibility result deliberately
        scored on another chain (explicit epitope_residues skip the results
        gate, so that is a supported flow), and it cannot see a request for a
        chain results.csv does not hold. An explicit chain settles it.
        """
        _login(client)
        job_id = _upload_two_chain_job(client)
        job_dir = TMP / job_id

        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 200

        _write_feasibility_csv(job_dir, "B", [60, 61])

        # results.csv holds A, the file holds B. Asking for B must succeed:
        # the caller said which chain they meant and the file is that chain.
        ok = client.get(f"/scout/feasibility/download/{job_id}?chain=B")
        assert ok.status_code == 200, (
            "the gate refused chain B's own feasibility result because "
            f"results.csv still holds chain A: {ok.get_data(as_text=True)[:200]}"
        )
        assert "ALA60" in ok.get_data(as_text=True)

        # And asking for A must NOT hand back B's file.
        no = client.get(f"/scout/feasibility/download/{job_id}?chain=A")
        assert no.status_code == 404, (
            f"chain B's file was served to a request for chain A: "
            f"{no.status_code} {no.get_data(as_text=True)[:200]}"
        )

    def test_the_advertised_download_url_carries_the_scored_chain(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """The link the page hands out must be one the gate accepts.

        Drop the ``chain=`` from that url_for and the button falls back to the
        results.csv heuristic — exactly the case above that it gets wrong.
        """
        _stub_feasibility_pipeline(monkeypatch)
        _login(client)
        job_id = _upload_two_chain_job(client)
        assert client.post(
            "/scout/analyze", json={"job_id": job_id, "chain": "A"}
        ).status_code == 200

        resp = client.post(
            "/scout/feasibility/analyze",
            json={"job_id": job_id, "chain": "B",
                  "epitope_residues": CHAIN_RESIDUES["B"]},
        )
        assert resp.status_code == 200, resp.data
        url = resp.get_json()["download_url"]
        assert "chain=B" in url, (
            f"the feasibility download link does not name the scored chain: {url}"
        )
        got = client.get(url)
        assert got.status_code == 200, (
            "the page advertised a download link its own gate refuses: "
            f"{got.status_code} {got.get_data(as_text=True)[:200]}"
        )


class TestTheChainIsThreadedThroughEveryCallSite:
    """The functions were tested; the WIRING between them was not.

    Each of these dies to a one-token change at a call site that every other
    test in this file survives.
    """

    def test_feasibility_passes_the_requested_chain_to_binder_overlaps(
        self, client, stub_pipeline, reap_jobs, monkeypatch
    ):
        """M33: the call site can drop `chain_id` with the suite green.

        _get_binder_overlaps' own chain gate is covered; that its ONE caller
        hands it this request's chain was not, so the gate could have been fed
        a constant and still looked like it worked. This is the path round 1's
        D2 was about — explicit epitope_residues skip the results.csv gate, so
        the chain check inside _get_binder_overlaps is the only thing left
        keeping one chain's binders off another chain's epitope.
        """
        import scout.routes as routes

        seen = []
        real = routes._get_binder_overlaps

        def _spy(job_dir, residues, chain_id):
            seen.append(chain_id)
            return real(job_dir, residues, chain_id)

        monkeypatch.setattr(routes, "_get_binder_overlaps", _spy)
        _stub_feasibility_pipeline(monkeypatch)

        _login(client)
        job_id = _upload_two_chain_job(client)
        client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})

        client.post(
            "/scout/feasibility/analyze",
            json={
                "job_id": job_id,
                "chain": "B",
                "epitope_residues": CHAIN_RESIDUES["B"],
            },
        )
        assert seen == ["B"], (
            f"feasibility asked for binder overlaps on {seen}, not chain B"
        )

    def test_the_table_is_rendered_with_the_scored_chain(self, client):
        """M55: renderViewer can stop threading `chain` into the table.

        renderEpitopeTable(epitopes, chain) takes the chain so the feasibility
        link uses the chain that was SCORED rather than the live dropdown. If
        the call site drops the argument the signature still matches, `chain`
        is undefined, and the link silently falls back to the dropdown.
        """
        page = client.get("/scout/").get_data(as_text=True)
        assert "renderEpitopeTable(epitopes, chain);" in page, (
            "renderViewer no longer passes the scored chain to the table"
        )

    def test_the_results_csv_stamp_is_the_chain_that_was_scored(
        self, client, stub_pipeline, reap_jobs
    ):
        """M36/M61: everything asserted the COLUMN existed, nothing its value.

        A stamp hard-coded to "A", or to the first chain in the file, would
        have passed every other test here while breaking the cache gate for
        every other chain.
        """
        job_id = _upload_two_chain_job(client)
        for chain in ("B", "A"):
            assert client.post(
                "/scout/analyze", json={"job_id": job_id, "chain": chain}
            ).status_code == 200
            rows = list(
                csv.DictReader((TMP / job_id / "results.csv").open(newline=""))
            )
            assert rows, f"no rows written for chain {chain}"
            stamps = {r["chain_id"] for r in rows}
            assert stamps == {chain}, (
                f"results.csv for chain {chain} carries stamps {stamps}"
            )

    def test_the_pipeline_stamps_the_chain_it_was_asked_for(self):
        """The same property on the real writer, which cannot run here.

        Parses run_pipeline's row literal: the chain_id cell must be the
        chain_id PARAMETER, not a constant and not something re-derived from
        the structure.
        """
        import ast
        import inspect

        import scout.pipeline as pipeline

        tree = ast.parse(textwrap.dedent(inspect.getsource(pipeline.run_pipeline)))
        stamps = [
            node.values[i]
            for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            for i, k in enumerate(node.keys)
            if isinstance(k, ast.Constant) and k.value == "chain_id"
        ]
        assert stamps, "run_pipeline no longer stamps chain_id at all"
        for value in stamps:
            assert isinstance(value, ast.Name) and value.id == "chain_id", (
                "the results.csv chain stamp is no longer the chain_id argument: "
                f"{ast.dump(value)}"
            )
