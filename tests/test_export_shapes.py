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
    """af2 / colabfold / esmfold / boltz2 / iggm / esmfold2_design.

    Copied from what the pipelines actually persist, NOT invented: metrics sit
    at the record ROOT under the pipeline's own lowercase names, and there is
    no nested ``scores`` dict (see tools/boltz2/run_pipeline.py:652-666 and
    the equivalents in af2/colabfold/esmfold/iggm). An earlier version of this
    fixture gave designs rows a nested ``scores``, which made the CSV and FASTA
    assertions below pass against a shape no tool emits.
    """
    return {
        "designs": [
            {
                "rank": 0,
                "name": "design_0",
                "pdb_key": "design_0.pdb",
                "sequence": "MKTAYIAKQR",
                "pdb_content_b64": "QVRPTQo=",
                "iptm": 0.66,
                "complex_plddt": 0.794,
                "n_hotspot_contacts": 5,
                "contacted_residues": [12, 15, 19],   # non-scalar: not a column
                "filter_status": "PASS",
            },
            {
                "rank": 1,
                "name": "design_1",
                "pdb_key": "design_1.pdb",
                "sequence": "GGSGGSGGSG",
                "pdb_content_b64": "QVRPTQo=",
                "iptm": 0.60,
                "complex_plddt": 0.771,
                "n_hotspot_contacts": 2,
                "contacted_residues": [12],
                "filter_status": "FAIL",
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


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_csv_export_carries_the_metrics_not_just_the_rows(shape):
    """Row count alone is not the bar. The designs shape keeps its metrics at
    the record root, so a scores-only exporter produced a file with the right
    number of rows and no science in it."""
    csv_text = candidates_to_csv(candidate_records(SHAPES[shape]()))
    header, first = csv_text.splitlines()[0], csv_text.splitlines()[1]
    metric = "ipTM" if shape == "candidates" else "iptm"
    assert metric in header.split(","), header
    value = "0.81" if shape == "candidates" else "0.66"
    assert value in first.split(","), first
    # Bulk and identity fields must never become columns.
    for banned in ("pdb_content_b64", "sequence", "contacted_residues"):
        assert banned not in header.split(","), header


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


class TestNonStringPdbKey:
    """``pdb_key`` is whatever the tool container wrote into ``job.result``,
    and not every container's source lives in this repo, so its type is not
    ours to guarantee. A non-string one raised AttributeError out of
    ``_basename`` (FASTA ids) and ``_safe_arcname`` (ZIP entry names), and
    because each serializer builds ONE document out of every row, that
    aborted the whole download rather than dropping the one bad design.
    Only the CSV survived, because ``csv`` stringifies what it writes.

    NOT position-dependent, which is what separates this from the same
    defect class on the per-row structure route,
    ``blueprints/jobs.py::job_candidate_pdb``: that loop RETURNS on the
    first basename match, so a bad row there reached only the requests
    whose own match sat after it, and its guard is tested with the bad
    row FIRST for that reason
    (``tests/test_candidate_pdb.py::TestPdbKeyNotAString``). Here the
    shape of the loop makes position irrelevant -- the serializer has
    to walk every row to finish the file, so a bad last row aborts it
    exactly as a bad first row does. Hence the parametrized position
    below.

    Coerced at the definition in ``shared.exports.export_key``, which is what
    makes one guard cover all three formats.
    """

    # A container can write any JSON scalar or container here. ``True`` is
    # in the list because ``isinstance(True, int)`` is True, the same trap
    # ``blueprints/jobs.py::_share_headline_metric`` guards its scores
    # against.
    NON_STRINGS = (12345, 3.5, True, ["designs/a.pdb"], {"key": "a.pdb"})

    @pytest.mark.parametrize("bad", NON_STRINGS)
    def test_fasta_export_survives_a_non_string_pdb_key(self, bad):
        body = candidates_to_fasta(
            [{"pdb_key": bad, "sequence": "ACDE", "scores": {}}]
        )
        assert body.count(">") == 1, body

    @pytest.mark.parametrize("bad", NON_STRINGS)
    def test_zip_export_survives_a_non_string_pdb_key(self, bad):
        data = candidates_to_zip(
            [{"pdb_key": bad, "pdb_content_b64": "QVRPTQo="}],
            lambda job_id, key: None,          # force the inline b64 path
            default_job_id="job-1",
        )
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert len(zf.namelist()) == 1, zf.namelist()

    @pytest.mark.parametrize("position", [0, 1, 2])
    def test_one_bad_key_anywhere_takes_the_whole_file(self, position):
        """Before the coercion both serializers raised part-way through
        and the caller received nothing at all, the healthy designs
        included. The asserts below are the FIXED behaviour: all three
        designs present, whichever row carried the bad key.

        The count is asserted at EVERY position because the outcome
        cannot vary with position -- nothing reaches the caller until
        the walk over every row finishes. The early-return loop in
        ``blueprints/jobs.py::job_candidate_pdb`` is the opposite shape,
        where position decides the blast radius."""
        rows = [
            {"pdb_key": f"designs/good_{i}.pdb", "sequence": "ACDE",
             "scores": {}}
            for i in range(3)
        ]
        rows[position] = dict(rows[position], pdb_key=12345)
        assert candidates_to_fasta(rows).count(">") == 3
        data = candidates_to_zip(
            [dict(r, pdb_content_b64="QVRPTQo=") for r in rows],
            lambda job_id, key: None,
            default_job_id="job-1",
        )
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert len(zf.namelist()) == 3, zf.namelist()

    @pytest.mark.parametrize("falsy", ["", None, 0])
    def test_a_falsy_key_still_takes_the_candidate_n_fallback(self, falsy):
        """The coercion must not turn a falsy key truthy. A falsy ``pdb_key``
        means "no structure reference", and each serializer falls back to the
        rank on one -- ``candidate_{i + 1}`` for the FASTA id, and the same
        name with ``.pdb`` for the ZIP entry, as the two asserts below spell
        out. A blanket ``str()`` would make ``0`` the legal filename ``"0"``
        and ``None`` the filename ``"None"``, and both
        would pass the ``or`` and name a design after a key that is not one."""
        body = candidates_to_fasta(
            [{"pdb_key": falsy, "sequence": "ACDE", "scores": {}}]
        )
        assert body.splitlines()[0] == ">rank1_candidate_1", body
        data = candidates_to_zip(
            [{"pdb_key": falsy, "pdb_content_b64": "QVRPTQo="}],
            lambda job_id, key: None,
            default_job_id="job-1",
        )
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.namelist() == ["candidate_1.pdb"], zf.namelist()

    def test_the_csv_column_is_unchanged_by_the_coercion(self):
        """The CSV was never the broken one, so the fix must not move it.
        ``csv`` stringified the raw value already; ``export_key`` now hands it
        the same text itself."""
        csv_text = candidates_to_csv([{"pdb_key": 12345, "scores": {}}])
        assert csv_text.splitlines()[1].split(",")[1] == "12345", csv_text
