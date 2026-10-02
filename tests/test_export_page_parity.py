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
# The scores CSV's sequence columns
#
# Why this section exists, as reported from a live check of the deployed #405
# on 2026-10-01 and NOT reproduced here: the CSV carried a sequence for
# esmfold2-design / rfdiffusion / mpnn (which store the field) and an empty
# one for boltzgen / proteina / rfantibody / iggm, whose FASTA DID carry
# records -- read from the structure file, which the CSV was not handed.
# The tests below pin the fix on fixtures, not on that observation.
# ---------------------------------------------------------------------------

def _chain_rows(per_design: list[dict[str, str]]) -> tuple[dict, dict[str, bytes]]:
    """A ``candidates`` result, plus the bytes each row's ``pdb_key`` fetches.

    Stored under ``candidates`` and on descending ipTM, so page order is
    stored order (``page_ordered_records``) and a row's position here is its
    position in both exports."""
    files = {f"designs/design_{i}.pdb": _pdb(chains)
             for i, chains in enumerate(per_design)}
    return {"candidates": [
        {"name": f"d{i}", "pdb_key": key, "scores": {"ipTM": 0.9 - 0.1 * i}}
        for i, key in enumerate(files)]}, files


def test_the_csv_writes_one_column_per_chain_of_the_complex(client, monkeypatch):
    target = "MKWMKW"
    result, files = _chain_rows([
        {"A": target, "B": "MKC"},
        {"A": target, "B": "WCG"},
        {"A": target, "B": "GKM"},
    ])
    job = _wire(monkeypatch, "boltzgen", result,
                fetch=lambda **kw: files[kw["filename"]])
    body = client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True)
    rows = list(csv.DictReader(io.StringIO(body)))
    assert body.splitlines()[0].endswith("sequence_chainA,sequence_chainB")
    assert [r["sequence_chainB"] for r in rows] == ["MKC", "WCG", "GKM"]
    # The target repeats, which is what a target echo looks like. It is kept:
    # no row says which chain is the design, so dropping the constant one
    # drops a designed chain that a run did not vary (the test below).
    assert [r["sequence_chainA"] for r in rows] == [target] * 3


def test_the_antibody_csv_keeps_a_designed_chain_that_did_not_vary(
        client, monkeypatch):
    """An antibody run whose light chain came out identical in every design."""
    antigen, light = "MKWMKW", "WCG"
    result, files = _chain_rows([
        {"A": antigen, "H": "MKC", "L": light},
        {"A": antigen, "H": "CKM", "L": light},
    ])
    job = _wire(monkeypatch, "iggm", result,
                fetch=lambda **kw: files[kw["filename"]])
    rows = list(csv.DictReader(io.StringIO(
        client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True))))
    assert [k for k in rows[0] if k.startswith("sequence_chain")] == [
        "sequence_chainA", "sequence_chainH", "sequence_chainL"]
    assert [(r["sequence_chainH"], r["sequence_chainL"]) for r in rows] == [
        ("MKC", light), ("CKM", light)]


def test_an_unreadable_structure_says_so_instead_of_leaving_blank_cells(
        client, monkeypatch):
    """A storage miss on one design, which ``_storage_fetcher`` turns into
    ``None``. That row cannot be dropped the way the FASTA drops a record --
    the row IS the design, scores and all -- so blank sequence cells would
    read as a design that has no sequence, not one whose file could not be
    read."""
    from shared.storage import StorageError
    result, files = _chain_rows([{"A": "MKWMKW", "B": "MKC"},
                                 {"A": "MKWMKW", "B": "WCG"}])
    gone = sorted(files)[1]

    def fetch(**kw):
        if kw["filename"] == gone:
            raise StorageError("object not found")
        return files[kw["filename"]]

    job = _wire(monkeypatch, "boltzgen", result, fetch=fetch)
    body = client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True)
    rows = list(csv.DictReader(io.StringIO(body)))
    assert rows[0]["sequence_chainB"] == "MKC"
    assert rows[0]["sequence_note"] == ""
    assert rows[1]["sequence_chainA"] == rows[1]["sequence_chainB"] == ""
    assert rows[1]["sequence_note"] == "structure unavailable"
    # Last, so every column before it keeps its position.
    assert body.splitlines()[0].endswith(
        "sequence_chainA,sequence_chainB,sequence_note")


def test_a_structure_with_no_readable_chain_says_that_instead(
        client, monkeypatch):
    """A poly-GLY backbone downloads and parses without error, and
    ``structure_chain_sequences`` still returns nothing for it -- it keeps a
    chain only at two or more distinct residue letters
    (``test_structure_chain_sequences_skips_placeholder_chains``). Saying
    "structure unavailable" about a file that was read would be false, so
    the two causes get two strings."""
    result, files = _chain_rows([{"A": "GGGGGG"}, {"A": "MKWMKW"}])
    job = _wire(monkeypatch, "proteina", result,
                fetch=lambda **kw: files[kw["filename"]])
    rows = list(csv.DictReader(io.StringIO(
        client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True))))
    assert rows[0]["sequence_chainA"] == ""
    assert rows[0]["sequence_note"] == "no sequence in structure"
    assert rows[1]["sequence_chainA"] == "MKWMKW"
    assert rows[1]["sequence_note"] == ""


def test_a_row_with_no_structure_at_all_says_so_too(client, monkeypatch):
    """A row with no ``pdb_key`` and no ``pdb_content_b64`` has nothing to
    fetch (``_structure_bytes`` needs one of them), so it is settled in its
    own branch before the read budget and never reaches the no-chains branch
    after the read: nothing was missing and nothing was unreadable, there was
    simply nothing to read. In a job where the OTHER rows do carry
    a structure, they create the chain columns this row would otherwise sit
    blank in -- the same unexplained blank cell the note exists to prevent."""
    result, files = _chain_rows([{"A": "MKWMKW"}])
    result["candidates"].append({"name": "d1", "scores": {"ipTM": 0.5}})
    job = _wire(monkeypatch, "boltzgen", result,
                fetch=lambda **kw: files[kw["filename"]])
    rows = list(csv.DictReader(io.StringIO(
        client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True))))
    assert rows[0]["sequence_chainA"] == "MKWMKW"
    assert rows[0]["sequence_note"] == ""
    assert rows[1]["sequence_chainA"] == ""
    assert rows[1]["sequence_note"] == "no structure stored"


def test_a_job_past_the_cap_reads_up_to_it_and_notes_the_rest(client, monkeypatch):
    """One download and one parse per sequence-less row, in series, inside the
    request. Nothing upstream bounds the row count, and gunicorn kills a
    request at its ``timeout`` under sync workers, so an uncapped loop trades
    the CSV for a 502 and takes the worker down with it. Rows past the cap get
    the reason in the note column -- the one note of the four that says nothing
    about the row's own structure, which here is perfectly readable."""
    import shared.exports as exports
    monkeypatch.setattr(exports, "_MAX_STRUCTURE_READS", 2)
    result, files = _chain_rows(
        [{"A": "MKWMKW", "B": c} for c in ("MKC", "WCG", "GKM", "CWA")])
    reads: list[str] = []

    def fetch(**kw):
        reads.append(kw["filename"])
        return files[kw["filename"]]

    job = _wire(monkeypatch, "rfantibody", result, fetch=fetch)
    rows = list(csv.DictReader(io.StringIO(
        client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True))))
    assert len(reads) == 2
    assert [r["sequence_chainB"] for r in rows] == ["MKC", "WCG", "", ""]
    capped = "not read: this export reads at most 2 structures"
    assert [r["sequence_note"] for r in rows] == ["", "", capped, capped]
    # The note is built from the limit, so the two cannot drift apart.
    assert str(exports._MAX_STRUCTURE_READS) in rows[2]["sequence_note"]


def test_the_read_cap_counts_reads_and_not_rows(client, monkeypatch):
    """A row the loop settles without reading anything must not spend the
    budget. Counting rows instead let a job's structureless rows exhaust it
    before the loop reached a row that HAS a readable structure, which then
    carried "not read" with nothing read: probed at cap 3 with three
    structureless rows, zero downloads were attempted and both readable rows
    were reported unread."""
    import shared.exports as exports
    monkeypatch.setattr(exports, "_MAX_STRUCTURE_READS", 2)
    result, files = _chain_rows(
        [{"A": "MKWMKW", "B": c} for c in ("MKC", "WCG")])
    # First, and as many as the cap allows reads, so counting rows spends the
    # whole budget on them and leaves the two readable rows below unread.
    result["candidates"] = [
        {"name": f"n{i}", "scores": {"ipTM": 0.95}} for i in range(3)
    ] + result["candidates"]
    reads: list[str] = []

    def fetch(**kw):
        reads.append(kw["filename"])
        return files[kw["filename"]]

    job = _wire(monkeypatch, "boltzgen", result, fetch=fetch)
    rows = list(csv.DictReader(io.StringIO(
        client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True))))
    assert len(reads) == 2
    assert [r["sequence_chainB"] for r in rows] == ["", "", "", "MKC", "WCG"]
    assert [r["sequence_note"] for r in rows] == (
        ["no structure stored"] * 3 + ["", ""])


def test_the_cap_clears_the_form_caps_its_comment_claims_it_does():
    """``_MAX_STRUCTURE_READS``' comment says it sits above the design counts
    boltzgen (``budget`` 50) and iggm can reach, so for those two the cap is
    unreachable and their CSV is never short a row their FASTA carries. iggm's
    is the larger of the two, so clearing it clears both."""
    from shared.exports import _MAX_STRUCTURE_READS
    from tools.iggm import NUM_SAMPLES_MAX
    assert NUM_SAMPLES_MAX == 100
    assert _MAX_STRUCTURE_READS > NUM_SAMPLES_MAX


def test_the_csv_and_the_fasta_carry_the_same_chains(client, monkeypatch):
    """Neither export is told which chain is the design, so both write all
    of them; a reader diffing the two downloads finds no chain in one and
    missing from the other."""
    result, files = _chain_rows([
        {"A": "MKWMKW", "H": "MKC", "L": "WCG"},
        {"A": "MKWMKW", "H": "CKM", "L": "WCG"},
    ])
    job = _wire(monkeypatch, "iggm", result,
                fetch=lambda **kw: files[kw["filename"]])
    csv_body = client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True)
    fasta = client.get(f"/jobs/{job.id}/export.fasta").get_data(as_text=True)
    in_csv = {k[len("sequence_chain"):] for k in
              csv.DictReader(io.StringIO(csv_body)).fieldnames
              if k.startswith("sequence_chain")}
    in_fasta = {line.rsplit("_chain", 1)[1].split()[0]
                for line in fasta.splitlines() if line.startswith(">")}
    assert in_csv == in_fasta == {"A", "H", "L"}


def test_the_csv_reads_no_more_structures_than_the_fasta(client, monkeypatch):
    result, files = _chain_rows([{"A": "MKWMKW", "B": "MKC"},
                                 {"A": "MKWMKW", "B": "WCG"}])
    result["candidates"][1]["sequence"] = "MKWC"
    reads: list[str] = []

    def fetch(**kw):
        reads.append(kw["filename"])
        return files[kw["filename"]]

    job = _wire(monkeypatch, "boltzgen", result, fetch=fetch)
    body = client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True)
    csv_reads, reads[:] = list(reads), []
    client.get(f"/jobs/{job.id}/export.fasta")
    # The row storing a sequence is read by neither route.
    assert csv_reads == reads == ["designs/design_0.pdb"]
    rows = list(csv.DictReader(io.StringIO(body)))
    assert body.splitlines()[0].endswith(
        "sequence,sequence_chainA,sequence_chainB")
    assert rows[1]["sequence"] == "MKWC"
    assert (rows[1]["sequence_chainA"], rows[1]["sequence_chainB"]) == ("", "")


def test_a_blank_chain_id_does_not_put_a_space_in_the_column_name(
        client, monkeypatch):
    """A PDB may leave the chain column blank; Biopython reads that as " "."""
    result, files = _chain_rows([{" ": "MKWC"}])
    job = _wire(monkeypatch, "boltzgen", result,
                fetch=lambda **kw: files[kw["filename"]])
    body = client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True)
    assert body.splitlines()[0].endswith(",sequence_chain")
    assert next(csv.DictReader(io.StringIO(body)))["sequence_chain"] == "MKWC"


def test_without_a_fetcher_no_structure_is_read_and_no_chain_column_written():
    """The campaign, target and admin exports pass none."""
    from shared.exports import candidate_table, candidates_to_csv

    cands = [{"name": "d0", "pdb_key": "designs/design_0.pdb",
              "scores": {"ipTM": 0.9}}]
    _leading, _metrics, rows = candidate_table(cands)   # the admin caller
    assert not [k for k in rows[0] if k.startswith("sequence_chain")]
    assert "sequence" not in candidates_to_csv(cands).splitlines()[0]


def test_a_scores_key_named_like_a_sequence_column_is_not_also_a_metric():
    """A tool scoring a per-chain quantity could name it ``sequence_chainA``.
    Discovered as a metric it lands in the row under the name
    :func:`sequence_columns` owns, which scans the row by prefix -- so the
    header carried the name twice and ``csv.DictReader`` kept only the last.
    No tool emits such a key today; this pins the same rule the ``"sequence"``
    exclusion already applies -- the reserved name wins -- for the names the
    chain columns take.

    Three cases, because the first version of this guard filtered only the
    header and only when a fetcher was given, and each of the other two was a
    real defect: with a fetcher and the chain extracted; without a fetcher at
    all (the campaign and target exports); and with a fetcher on a row that
    ends up with a note, where nothing overwrites the metric value and the
    CSV printed a number where a sequence belongs."""
    from shared.exports import candidates_to_csv

    cands = [{"name": "d0", "pdb_key": "d0.pdb",
              "scores": {"ipTM": 0.9, "sequence_chainA": 42,
                         "sequence_note": 7}}]
    out = candidates_to_csv(
        cands, fetch_bytes=lambda j, f: _pdb({"A": "MKWMKW"}),
        default_job_id="j1",
    )
    header = out.splitlines()[0].split(",")
    assert [h for h in header if header.count(h) > 1] == []
    assert header.count("sequence_chainA") == 1
    row = next(csv.DictReader(io.StringIO(out)))
    assert row["sequence_chainA"] == "MKWMKW"

    # No fetcher: these columns are never written, but sequence_columns still
    # scans the row, so the name must be gone from the row too.
    plain = candidates_to_csv(cands)
    plain_header = plain.splitlines()[0].split(",")
    assert [h for h in plain_header if plain_header.count(h) > 1] == []
    assert "42" not in plain

    # A row that gets a note: nothing overwrites the metric value, so only
    # removing it keeps a number out of the chain column.
    noted = candidates_to_csv(
        [{"name": "d1", "pdb_key": "designs/d1.pdb", "scores": {"iptm": 0.9}},
         {"name": "d2", "scores": {"sequence_chainA": 0.42}}],
        fetch_bytes=lambda j, f: _pdb({"A": "MKWM"}), default_job_id="j1",
    )
    second = list(csv.DictReader(io.StringIO(noted)))[1]
    assert second["sequence_note"] == "no structure stored"
    assert second["sequence_chainA"] == ""


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
