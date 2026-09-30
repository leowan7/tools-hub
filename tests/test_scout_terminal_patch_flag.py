"""The "terminal patch" flag measures distance from the CHAIN's ends.

It used to measure from the patch's own lowest residue number, so the lowest
residue of every patch counted as terminal and any compact patch was flagged
wherever it sat. Reported by the hub lead on prod build dbbb7833, 2026-09-30:
1BTL THR200-GLN206 (chain numbered ~26-290) and all three top 1HEW epitopes
were flagged "terminal patch".
"""

import csv
import io
import shutil
from pathlib import Path

import pytest

from scout import ratelimit
from scout.flags import _CSV_COLUMNS_BASE, compute_quality_flags
from scout.parser import parse_pdb

pytestmark = pytest.mark.usefixtures("isolate_supabase")

TMP = Path("tmp")

# 1BTL-like numbering: the chain starts at 26, not 1.
FIRST, LAST = 26, 290


def _flags(residues: str, first=FIRST, last=LAST) -> str:
    # Every other flag held off, so only "terminal patch" can appear.
    return compute_quality_flags(
        secondary_structure="helix",
        hydrophobicity=0.5,
        burial_raw=50.0,
        bfactor_score=0.9,
        is_functional_site=False,
        residues_str=residues,
        chain_first=first,
        chain_last=last,
    )


def test_mid_chain_compact_patch_is_not_terminal():
    assert _flags("THR200,LEU201,ALA202,GLN205,GLN206") == ""


def test_n_terminal_patch_is_terminal():
    assert _flags("HIS26,PRO27,GLU28,THR29,LEU30") == "terminal patch"


def test_c_terminal_patch_is_terminal():
    assert _flags("ALA286,LYS287,HIS288,TRP289,SER290") == "terminal patch"


def test_proximity_edge_is_inclusive_on_both_ends():
    # first+5 and last-5 are terminal; first+6 and last-6 are not.
    assert _flags("ALA31,ALA285,ALA150") == "terminal patch"
    assert _flags("ALA32,ALA284,ALA150") == ""


def test_unknown_chain_ends_skip_the_flag():
    assert _flags("HIS26,PRO27,GLU28", first=None, last=None) == ""


def _pdb(residues) -> bytes:
    """One CA per (resseq, icode, resname); HOH and MSE written as HETATM."""
    lines = []
    for serial, (resseq, icode, resname) in enumerate(residues, start=1):
        record = "HETATM" if resname in ("HOH", "MSE") else "ATOM  "
        atom = " O  " if resname == "HOH" else " CA "
        lines.append(
            f"{record}{serial:5d} {atom} {resname} A{resseq:4d}{icode}   "
            f"{serial * 3.8:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 20.00           "
            f"{atom.strip()[0]}"
        )
    lines.append("END")
    return ("\n".join(lines) + "\n").encode()


def test_parser_records_chain_ends_across_insertion_codes_and_breaks(tmp_path):
    # Starts at 26, has an insertion code (27A), a break (29-39 missing),
    # and a water numbered past the chain's last residue.
    residues = [(26, " ", "ALA"), (27, " ", "GLY"), (27, "A", "SER"),
                (28, " ", "ALA")]
    residues += [(n, " ", "LEU") for n in range(40, 46)]
    residues += [(500, " ", "HOH")]
    path = tmp_path / "t.pdb"
    path.write_bytes(_pdb(residues))

    (chain,) = parse_pdb(path).chains
    assert (chain.residue_count, chain.first_resseq, chain.last_resseq) == (10, 26, 45)


def test_parser_chain_ends_skip_a_free_mse_but_keep_a_terminal_one(tmp_path):
    # HETATM MSE25 and MSE31 are the chain's termini; MSE401 is a free ligand.
    residues = [(25, " ", "MSE")] + [(n, " ", "ALA") for n in range(26, 31)]
    residues += [(31, " ", "MSE"), (401, " ", "MSE")]
    path = tmp_path / "t.pdb"
    path.write_bytes(_pdb(residues))

    (chain,) = parse_pdb(path).chains
    assert (chain.first_resseq, chain.last_resseq) == (25, 31)


# -- Route: the flag written to results_annotated.csv uses the chain's ends --


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WEBHOOK_SWEEP_ENABLED", "0")
    monkeypatch.setattr(
        "scout.epitope_db.resolve_uniprot_id",
        lambda *a, **k: {"uniprot_id": "", "protein_name": "",
                         "identity_pct": "unknown", "source": ""},
    )
    monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])
    monkeypatch.setattr("scout.interfaces.detect_interfaces", lambda *a, **k: [])
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    before = {p.name for p in TMP.iterdir()} if TMP.exists() else set()
    ratelimit.reset()
    yield flask_app.test_client()
    ratelimit.reset()
    for entry in TMP.iterdir() if TMP.exists() else []:
        if entry.name not in before and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def test_analyze_flags_against_the_chain_ends(client):
    # Chain A numbered 26..90.
    pdb = _pdb([(n, " ", "ALA") for n in range(26, 91)])
    resp = client.post(
        "/scout/upload",
        data={"file": (io.BytesIO(pdb), "target.pdb")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.data
    job_id = resp.get_json()["job_id"]

    patches = {
        "1": "ALA50,ALA51,ALA52,ALA55,ALA56",   # mid-chain, compact
        "2": "ALA26,ALA27,ALA28,ALA29,ALA40",   # N-terminal
        "3": "ALA86,ALA87,ALA88,ALA89,ALA70",   # C-terminal
    }
    with (TMP / job_id / "results.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS_BASE)
        writer.writeheader()
        for eid, residues in patches.items():
            row = dict.fromkeys(_CSV_COLUMNS_BASE, "0")
            row.update({
                "epitope_id": eid, "chain_id": "A", "residues": residues,
                "residue_count": "5", "composite_score": "0.7",
                "secondary_structure": "helix", "hydrophobicity": "0.5",
                "burial_raw": "50", "bfactor_score": "0.9",
            })
            writer.writerow(row)

    resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
    assert resp.status_code == 200, resp.data

    with (TMP / job_id / "results_annotated.csv").open(newline="") as fh:
        flags = {r["epitope_id"]: r["quality_flags"] for r in csv.DictReader(fh)}
    assert flags == {"1": "", "2": "terminal patch", "3": "terminal patch"}
