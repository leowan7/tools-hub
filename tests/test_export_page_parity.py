"""The job exports against the job page, tool by tool (QA 2026-09-30).

P0-3: mpnn's CSV was a header with no rows.
P1-2: the CSV numbered the STORED list while af2/colabfold/esmfold/boltz2/
      esmfold2-design/iggm/opendde pages re-sort ``designs``, so page row 1 and
      CSV rank 1 were different designs.
P1-3: the FASTA button always rendered and served a stub on every tool whose
      rows carry a structure but no sequence field.

Driven by each tool's own ``tools/<pkg>/example/result.json`` rendered through
its real results partial, so a tool whose page order changes without the
export following it fails here.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import math
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_worked_examples import _render_partial, tools_app  # noqa: F401

pytestmark = pytest.mark.usefixtures("isolate_supabase")

REPO = Path(__file__).resolve().parent.parent
JOB_ID = "0f6be32f-e971-41f7-b7ec-33fbf795addd"

# Every tool whose page renders the shared candidate table from its example.
# Listed so the audit cannot pass by rendering nothing.
TABLE_TOOLS = (
    "af2", "bindcraft", "boltz2", "boltzgen", "esmfold2-design", "iggm",
    "opendde", "proteina", "pxdesign", "rfantibody", "rfdiffusion",
)


def _example(slug: str) -> dict:
    pkg = slug.replace("-", "_")
    return json.loads((REPO / "tools" / pkg / "example" / "result.json").read_text())


def _page_rows(html: str) -> list[dict]:
    """Per rendered row: its stored index and its metric cells."""
    out = []
    for row in re.findall(r'<tr class="cand-row[^"]*".*?</tr>', html, re.S):
        ref = re.search(r'data-ref-idx="(\d+)"', row)
        cells = dict(re.findall(r'data-col="([^"]+)" data-val="([^"]*)"', row))
        out.append({"ref": int(ref.group(1)) if ref else None, "cells": cells})
    return out


def _stored(result: dict) -> list:
    cands = result.get("candidates")
    return cands if isinstance(cands, list) and cands else result.get("designs") or []


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _reversed_designs(slug: str) -> dict:
    """The example with ``designs`` stored backwards and no ``candidates``,
    so the page's own sort is what orders it -- boltz2's and opendde's
    examples are already stored in page order and prove nothing as shipped."""
    result = copy.deepcopy(_example(slug))
    result.pop("candidates", None)
    result["designs"] = list(reversed(result["designs"]))
    return result


# colabfold/esmfold examples are the single-structure shape, which renders
# no candidate table; this is their multi-design shape, stored in ascending
# pLDDT so the page's sort has to move every row.
_BATCH = {"designs": [
    {"name": f"d{i}", "pdb_key": f"designs/design_{i}.pdb", "mean_plddt": p,
     "iptm": 0.1 * i, "ptm": 0.2, "total_aa": 90 + i, "num_chains": 2}
    for i, p in enumerate((0.61, 0.72, 0.83, 0.94))]}

_PARITY_CASES = [(s, False) for s in TABLE_TOOLS] + [
    (s, True) for s in ("af2", "boltz2", "esmfold2-design", "iggm", "opendde")
] + [("colabfold", "batch"), ("esmfold", "batch")]


@pytest.mark.parametrize("slug,reverse", _PARITY_CASES)
def test_csv_rows_are_the_page_rows_in_page_order_with_every_page_value(
        tools_app, slug, reverse):
    from shared.exports import candidates_to_csv
    from shared.jobs import page_ordered_records

    flask_app, _ = tools_app
    if reverse == "batch":
        result = copy.deepcopy(_BATCH)
    else:
        result = _reversed_designs(slug) if reverse else _example(slug)
    page = _page_rows(_render_partial(
        flask_app, slug, job_id=JOB_ID, example=False, result=result))
    assert page, f"{slug}: the page rendered no candidate rows"

    stored = _stored(result)
    exported = page_ordered_records(slug, result)
    # Identity, not equality: two designs can share every value.
    export_refs = [next(i for i, s in enumerate(stored) if s is r) for r in exported]
    assert export_refs == [p["ref"] for p in page], slug

    csv_rows = list(csv.DictReader(io.StringIO(candidates_to_csv(exported))))
    assert len(csv_rows) == len(page)
    for n, (p, c) in enumerate(zip(page, csv_rows), start=1):
        assert c["rank"] == str(n)
        csv_nums = [x for x in map(_num, c.values()) if x is not None]
        for col, val in p["cells"].items():
            want = _num(val)
            if want is None:
                continue
            # By value, not by name: page columns are display names (ipTM,
            # epitope_contacts) and the CSV keeps the pipeline's own (iptm,
            # n_epitope_contacts).
            assert any(math.isclose(want, x, rel_tol=1e-3, abs_tol=1e-3)
                       for x in csv_nums), f"{slug} row {n}: {col}={val} not in CSV {c}"


def test_the_iggm_case_from_the_report_now_leads_with_the_page_row_one():
    from shared.jobs import page_ordered_records

    result = _example("iggm")
    stored_first = result["designs"][0]["pdb_key"]
    first = page_ordered_records("iggm", result)[0]
    assert first["n_epitope_contacts"] == max(
        d["n_epitope_contacts"] for d in result["designs"])
    assert first["pdb_key"] != stored_first


def test_page_order_never_drops_or_adds_a_row():
    from shared.jobs import page_ordered_records

    designs = [{"mean_plddt": 0.5}, "garbage", {"mean_plddt": None}, {"mean_plddt": 0.9}]
    out = page_ordered_records("af2", {"designs": designs})
    assert len(out) == 4
    assert out[0] == {"mean_plddt": 0.9}
    # Stored candidates are shown as stored, and an empty list defers to designs.
    assert page_ordered_records("af2", {"candidates": [{"a": 2}, {"a": 1}]}) == [{"a": 2}, {"a": 1}]
    assert len(page_ordered_records("af2", {"candidates": [], "designs": designs})) == 4


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
        sess["access_token"] = "fake-token"
    return c


def _wire(monkeypatch, tool, result, fetch=None):
    import blueprints.jobs as jobs_mod

    job = MagicMock()
    job.id = str(uuid.uuid4())
    job.user_id = "u-1"
    job.tool = tool
    job.preset = None
    job.result = result
    monkeypatch.setattr(jobs_mod, "load_user_context", lambda: SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com"))
    monkeypatch.setattr(jobs_mod, "get_job", lambda _id, user_id=None: job)
    if fetch is not None:
        monkeypatch.setattr(jobs_mod, "download_output", fetch)
    return job


def test_mpnn_csv_carries_every_row_and_column_the_page_shows(client, monkeypatch):
    result = _example("mpnn")
    job = _wire(monkeypatch, "mpnn", result)
    body = client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True)
    rows = list(csv.DictReader(io.StringIO(body)))
    assert len(rows) == len(result["sequences"]) == 2
    for n, (row, seq) in enumerate(zip(rows, result["sequences"]), start=1):
        assert row["rank"] == str(n)
        assert float(row["score"]) == seq["score"]
        assert float(row["recovery"]) == seq["recovery"]
        assert row["sequence"] == seq["seq"]


def test_mpnn_malformed_row_is_kept_not_renumbered():
    from shared.exports import sequences_to_csv

    rows = list(csv.DictReader(io.StringIO(sequences_to_csv(
        [{"seq": "AA", "score": 1.0}, None, {"seq": "CC", "score": 2.0}]))))
    assert [r["rank"] for r in rows] == ["1", "2", "3"]
    assert [r["sequence"] for r in rows] == ["AA", "", "CC"]


def test_mpnn_csv_row_keys_named_rank_or_sequence_do_not_duplicate_columns():
    from shared.exports import sequences_to_csv

    text = sequences_to_csv([{"seq": "AAA", "rank": 7, "sequence": "ZZZ"}])
    assert text.splitlines()[0] == "rank,score,recovery,sequence"
    row = next(csv.DictReader(io.StringIO(text)))
    assert (row["rank"], row["sequence"]) == ("1", "AAA")


def test_the_csv_route_exports_in_page_order(client, monkeypatch):
    result = _example("iggm")
    job = _wire(monkeypatch, "iggm", result)
    rows = list(csv.DictReader(io.StringIO(
        client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True))))
    contacts = [int(r["n_epitope_contacts"]) for r in rows]
    assert contacts == sorted(contacts, reverse=True)
    assert len(rows) == len(result["designs"])


def _pdb(chains: dict[str, str]) -> bytes:
    three = {"A": "ALA", "C": "CYS", "G": "GLY", "K": "LYS", "M": "MET", "W": "TRP"}
    lines, n = [], 1
    for cid, seq in chains.items():
        for i, aa in enumerate(seq, start=1):
            lines.append(
                f"ATOM  {n:5d}  CA  {three[aa]} {cid}{i:4d}    "
                f"{float(i):8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 90.00           C")
            n += 1
        lines.append("TER")
    lines.append("END")
    return ("\n".join(lines) + "\n").encode()


# The P1-3 list. colabfold/esmfold examples are the single-structure shape,
# which renders no candidate table; their multi-design shape is used instead.
FASTA_TOOLS = (
    "af2", "colabfold", "esmfold", "boltz2", "boltzgen", "bindcraft",
    "pxdesign", "opendde", "iggm", "rfantibody", "proteina",
)


def _with_structures(slug: str) -> dict:
    result = copy.deepcopy(_example(slug))
    if slug in ("colabfold", "esmfold"):
        result = {"designs": [{"pdb_key": "design_0.pdb", "mean_plddt": 0.8},
                              {"pdb_key": "design_1.pdb", "mean_plddt": 0.9}]}
    rows = _stored(result)
    for i, row in enumerate(rows):
        # Stripped examples drop the key some tools store (boltz2's
        # run_pipeline writes one per design); inline b64 rows keep theirs.
        if not row.get("pdb_content_b64"):
            row.setdefault("pdb_key", f"designs/design_{i}.pdb")
        row.pop("sequence", None)
        row.pop("binder_sequence", None)
    return result


@pytest.mark.parametrize("slug", FASTA_TOOLS)
def test_fasta_reads_sequences_from_structures(client, monkeypatch, slug):
    import base64

    result = _with_structures(slug)
    pdb = _pdb({"A": "MKW", "B": "GC"})
    for row in _stored(result):
        if row.get("pdb_content_b64"):
            row["pdb_content_b64"] = base64.b64encode(pdb).decode()
    job = _wire(monkeypatch, slug, result, fetch=lambda **_kw: pdb)
    body = client.get(f"/jobs/{job.id}/export.fasta").get_data(as_text=True)
    assert "No sequences found" not in body
    headers = [ln for ln in body.splitlines() if ln.startswith(">")]
    assert len(headers) == 2 * len(_stored(result))
    assert headers[0].split()[0].startswith(">rank1_")
    assert headers[0].split()[0].endswith("_chainA")
    assert body.splitlines()[1] == "MKW"
    assert "GC" in body


def test_fasta_rank_labels_follow_the_csv(client, monkeypatch):
    result = _with_structures("iggm")
    job = _wire(monkeypatch, "iggm", result, fetch=lambda **_kw: _pdb({"A": "MK"}))
    csv_rows = list(csv.DictReader(io.StringIO(
        client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True))))
    fasta = client.get(f"/jobs/{job.id}/export.fasta").get_data(as_text=True)
    first = [ln for ln in fasta.splitlines() if ln.startswith(">")][0]
    from shared.exports import _basename
    assert first.startswith(f">rank1_{_basename(csv_rows[0]['pdb_key'], '')}_chainA")


def test_fasta_storage_miss_and_unparseable_bytes_fall_back_to_the_note(client, monkeypatch):
    from shared.storage import StorageError

    def miss(**_kw):
        raise StorageError("gone")

    job = _wire(monkeypatch, "af2", {"designs": [{"pdb_key": "a.pdb"}]}, fetch=miss)
    assert "No sequences found" in client.get(
        f"/jobs/{job.id}/export.fasta").get_data(as_text=True)
    job = _wire(monkeypatch, "af2", {"designs": [{"pdb_key": "a.pdb"}]},
                fetch=lambda **_kw: b"not a structure")
    assert "No sequences found" in client.get(
        f"/jobs/{job.id}/export.fasta").get_data(as_text=True)


def test_structure_chain_sequences_skips_placeholder_chains():
    from shared.exports import structure_chain_sequences

    assert structure_chain_sequences(_pdb({"A": "GGGG", "B": "MK"})) == [("B", "MK")]


def test_structure_chain_sequences_reads_mmcif_and_modified_residues():
    from shared.exports import structure_chain_sequences

    assert structure_chain_sequences(_pdb({"A": "MKW"})) == [("A", "MKW")]
    mse = _pdb({"A": "MK"}).replace(b"MET", b"MSE")
    assert structure_chain_sequences(mse) == [("A", "MK")]
    cif = b"""data_x
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
_atom_site.auth_asym_id
_atom_site.pdbx_PDB_model_num
ATOM 1 C CA . GLY A 1 1 ? 0.0 0.0 0.0 1.0 90.0 1 A 1
ATOM 2 C CA . LYS A 1 2 ? 3.8 0.0 0.0 1.0 90.0 2 A 1
"""
    assert structure_chain_sequences(cif) == [("A", "GK")]
    assert structure_chain_sequences(b"") == []


# ---------------------------------------------------------------------------
# The FASTA button
# ---------------------------------------------------------------------------

def _has_fasta_button(html: str) -> bool:
    return "/export.fasta" in html


def test_fasta_button_shows_for_rows_with_a_structure(tools_app):
    flask_app, _ = tools_app
    html = _render_partial(flask_app, "iggm", job_id=JOB_ID, example=False,
                           result=_example("iggm"))
    assert _has_fasta_button(html)


def test_fasta_button_hidden_when_no_row_has_a_sequence_or_structure(tools_app):
    flask_app, _ = tools_app
    result = _example("proteina")
    assert not any(c.get("pdb_key") or c.get("sequence") for c in result["candidates"])
    html = _render_partial(flask_app, "proteina", job_id=JOB_ID, example=False,
                           result=result)
    assert "/export.csv" in html          # the bar itself rendered
    assert not _has_fasta_button(html)


def test_fasta_button_on_a_campaign_table_needs_a_real_sequence(tools_app):
    flask_app, _ = tools_app
    tmpl = flask_app.jinja_env.from_string(
        "{% from 'components/candidate_table.html' import candidate_table %}"
        "{{ candidate_table(rows, [], 'job-1', 'iggm', campaign_id='c-1') }}")
    with flask_app.test_request_context("/x"):
        structure_only = tmpl.render(rows=[{"pdb_key": "a.pdb", "scores": {}}])
        with_seq = tmpl.render(rows=[{"pdb_key": "a.pdb", "sequence": "MK", "scores": {}}])
    assert "/export.csv" in structure_only
    assert not _has_fasta_button(structure_only)
    assert _has_fasta_button(with_seq)
