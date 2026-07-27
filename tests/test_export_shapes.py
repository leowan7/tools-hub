"""Every tool's stored result shape must survive the export / staging paths.

Job results come in two shapes depending on the pipeline:

  * ``result["candidates"]`` — the canonical binder-design shape
    (rfdiffusion, bindcraft, boltzgen, pxdesign, rfantibody), scores nested
    under ``candidate["scores"]``;
  * ``result["designs"]`` — the cofold/design shape (af2, colabfold, esmfold,
    boltz2, iggm, esmfold2_design), metrics inline at the candidate root.

Seven call sites used to read ``result["candidates"]`` raw, so for every
designs-only tool the CSV export was header-only, the ZIP was empty, the
refold slate was empty, and the lab handoff staged ZERO PDBs while still
creating the row and sending a success email. Nothing errored; it was silent.

These tests pin the shape-tolerance itself (``candidate_records``) and the
pure serializers that consume it, so a future tool that persists under a third
key fails here rather than in production.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from shared.exports import candidates_to_csv, candidates_to_fasta, candidates_to_zip
from shared.jobs import candidate_records
from shared.refold import extract_top_n_sequences


def _candidates_shape() -> dict:
    """rfdiffusion / bindcraft / boltzgen / pxdesign / rfantibody."""
    return {
        "candidates": [
            {
                "pdb_key": "designs/design_0.pdb",
                "sequence": "MKTAYIAKQR",
                "pdb_content_b64": "QVRPTQo=",  # "ATOM\n"
                "scores": {"ipTM": 0.81, "pLDDT": 88.2},
            },
            {
                "pdb_key": "designs/design_1.pdb",
                "sequence": "GGSGGSGGSG",
                "pdb_content_b64": "QVRPTQo=",
                "scores": {"ipTM": 0.74, "pLDDT": 85.0},
            },
        ]
    }


def _designs_shape() -> dict:
    """af2 / colabfold / esmfold / boltz2 / iggm / esmfold2_design."""
    return {
        "designs": [
            {
                "pdb_key": "design_0.pdb",
                "sequence": "MKTAYIAKQR",
                "pdb_content_b64": "QVRPTQo=",
                "scores": {"ipTM": 0.66, "pLDDT": 79.4},
            },
            {
                "pdb_key": "design_1.pdb",
                "sequence": "GGSGGSGGSG",
                "pdb_content_b64": "QVRPTQo=",
                "scores": {"ipTM": 0.60, "pLDDT": 77.1},
            },
        ]
    }


def _wrapped_designs_shape() -> dict:
    """Legacy wrapped rows still exist in prod (see _normalize_result_shape)."""
    return {"output": _designs_shape()}


SHAPES = {
    "candidates": _candidates_shape,
    "designs": _designs_shape,
    "wrapped_designs": _wrapped_designs_shape,
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_candidate_records_finds_rows_for_every_shape(shape):
    assert len(candidate_records(SHAPES[shape]())) == 2


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_csv_export_is_not_header_only(shape):
    csv_text = candidates_to_csv(candidate_records(SHAPES[shape]()))
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    # Header plus one row per design — the regression was header alone.
    assert len(lines) == 3, csv_text
    assert "ipTM" in lines[0]


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_fasta_export_emits_sequences(shape):
    body = candidates_to_fasta(candidate_records(SHAPES[shape]()))
    assert body.count(">") == 2, body


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_zip_export_contains_every_design(shape):
    data = candidates_to_zip(
        candidate_records(SHAPES[shape]()),
        lambda job_id, key: None,          # force the inline b64 path
        default_job_id="job-1",
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert len(zf.namelist()) == 2, zf.namelist()


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_refold_slate_is_not_empty(shape):
    seqs = extract_top_n_sequences(SHAPES[shape](), 5)
    assert len(seqs) == 2


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_lab_staging_resolves_indices_for_every_shape(shape):
    """stage_campaign_candidates skips out-of-range indices silently, so the
    thing that matters is that the list it receives is non-empty and indexes
    the same rows the shortlist UI enumerated."""
    records = candidate_records(SHAPES[shape]())
    assert [r["pdb_key"] for r in records] == [
        records[0]["pdb_key"], records[1]["pdb_key"]
    ]
    # Index 1 must resolve — the raw read made this an IndexError-free no-op.
    assert records[1].get("pdb_key")


def test_candidates_preferred_when_both_keys_present():
    """esmfold2_design emits both; candidates wins so ranking stays stable."""
    both = {
        "candidates": [{"pdb_key": "a.pdb", "scores": {}}],
        "designs": [{"pdb_key": "b.pdb", "scores": {}}, {"pdb_key": "c.pdb"}],
    }
    recs = candidate_records(both)
    assert len(recs) == 1
    assert recs[0]["pdb_key"] == "a.pdb"


def test_unknown_shape_returns_empty_not_error():
    assert candidate_records({"something_else": [1, 2]}) == []
    assert candidate_records(None) == []
    assert candidate_records({}) == []
