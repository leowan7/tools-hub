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
import struct
import zipfile

import pytest

from shared.exports import (
    _safe_arcname,
    candidates_to_csv,
    candidates_to_fasta,
    candidates_to_zip,
)
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

    def test_an_oversized_key_does_not_take_the_whole_zip(self):
        """Coercing the TYPE does not close the whole-file blast radius on its
        own. A ZIP header stores the entry-name length in two bytes, and the
        list below -- 4000 design names, which is what a container writing its
        manifest into ``pdb_key`` would produce -- coerces to 106890 legal
        characters. That aborts the archive out of ``struct.error`` where it
        used to abort out of ``AttributeError``, losing the healthy designs
        exactly as before.

        So the bound in ``shared.exports._safe_arcname`` is the one guard of
        its own that the definition-level coercion does NOT make redundant:
        it is a length, and nothing upstream supplies one.

        The row is a real list rather than a long string because the whole
        chain is the claim -- container writes a container, ``export_key``
        coerces it, the bound keeps the archive openable."""
        rows = [
            {"pdb_key": ["designs/design_%d.pdb" % i for i in range(4000)],
             "sequence": "ACDE", "scores": {}, "pdb_content_b64": "QVRPTQo="},
            {"pdb_key": "designs/good.pdb", "sequence": "EFGH",
             "scores": {}, "pdb_content_b64": "QVRPTQo="},
        ]
        data = candidates_to_zip(
            rows, lambda job_id, key: None, default_job_id="job-1"
        )
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
        assert len(infos) == 2, [i.filename[:40] for i in infos]
        assert "designs/good.pdb" in [i.filename for i in infos]
        assert max(len(i.filename.encode("utf-8")) for i in infos) <= 65535
        # Every entry is a FILE carrying its bytes, not an empty directory.
        assert all(not i.is_dir() and i.file_size for i in infos), \
            [(i.filename[:20], i.is_dir(), i.file_size) for i in infos]

    def test_a_long_key_is_a_long_fasta_id_and_not_a_crash(self):
        """The FASTA half needs no bound, and that is a measurement rather
        than an assumption. The key is separator-free ON PURPOSE: ``_basename``
        returns the last ``/`` segment, so a long key full of separators
        reaches the header as a short tail and an assertion on it would hold
        for any input length whatsoever -- which is exactly what an earlier
        version of this test asserted, under a docstring claiming it had been
        measured."""
        body = candidates_to_fasta(
            [{"pdb_key": "b" * 90000, "sequence": "ACDE", "scores": {}}]
        )
        header = body.splitlines()[0]
        assert header.startswith(">rank1_"), header[:40]
        assert len(header) == 90007, len(header)

    def test_a_cut_landing_on_a_separator_is_still_a_file(self):
        """A truncation that lands exactly on a ``/`` leaves the name ending
        in one, and zipfile sets the directory bit on any such name: that
        entry extracts as an empty FOLDER and the design's bytes are gone,
        silently, for that one design. One character of the key decides it,
        so it is pinned rather than reasoned about."""
        key = "x" * 65534 + "/" + "y" * 100
        data = candidates_to_zip(
            [{"pdb_key": key, "pdb_content_b64": "QVRPTQo="}],
            lambda job_id, _key: None, default_job_id="job-1",
        )
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.infolist()[0]
        assert not info.filename.endswith("/"), info.filename[-8:]
        assert not info.is_dir(), info.filename[-8:]
        assert info.file_size == 5, info.file_size

    def test_the_bound_is_the_zip_limit_not_a_shorter_one(self):
        """65535 bytes is the last name a ZIP header can store, so that one
        passes through untouched and only 65536 is cut. A tighter bound would
        silently rename entries that were always legal, and a looser one would
        not fix anything. Bytes, not characters: the header counts the encoded
        name, so one multi-byte character is two of the 65535."""
        assert _safe_arcname("d" * 65535) == "d" * 65535
        assert len(_safe_arcname("d" * 65536).encode("utf-8")) == 65535
        assert len(_safe_arcname("\u00e9" * 40000).encode("utf-8")) <= 65535
        # Where 65535 comes from, rather than someone's preference: zipfile
        # is what refuses, at exactly one byte past it. Pinned here so the
        # bound and its reason cannot drift apart -- a measurement recorded
        # in a commit message is not something a later reader can re-run.
        with zipfile.ZipFile(io.BytesIO(), "w") as zf:
            zf.writestr("d" * 65535, b"ok")
        with pytest.raises(struct.error):
            with zipfile.ZipFile(io.BytesIO(), "w") as zf:
                zf.writestr("d" * 65536, b"too long")
