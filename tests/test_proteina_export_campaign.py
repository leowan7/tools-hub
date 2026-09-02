"""Tests for the campaign export.

This tool's whole value is that a clean run MEANS something — "I don't want to
lose anything" is not served by a file that merely looks complete. So most of
these assert a REFUSAL, and the happy-path tests check that what landed can be
round-tripped back to the coordinates it came from.
"""
import csv
import json

import pytest

from tools.proteina import export_campaign as ec

# EU/author numbering, so residue ids do not start at 1 — the parser must key
# on the file's own numbering rather than a running counter.
AA1_TO_3 = {v: k for k, v in ec.AA3.items()}


def _pdb(seq: str, chain: str = "C", start: int = 241, icodes=()) -> str:
    lines, serial = [], 1
    for i, aa in enumerate(seq):
        icode = icodes[i] if i < len(icodes) else " "
        lines.append(
            f"ATOM  {serial:>5}  CA  {AA1_TO_3[aa]} {chain}"
            f"{start + i:>4}{icode}"
            f"{i:>12.3f}{0.0:>8.3f}{0.0:>8.3f}  1.00  0.00           C")
        serial += 1
    return "\n".join(lines) + "\n"


@pytest.fixture
def campaign(tmp_path):
    """Two designs in one shard: one passes, one is a refold failure."""
    root = tmp_path / "camp"
    (root / "shard_000").mkdir(parents=True)
    seqs = {"design_001.pdb": "ACDEFGHIKL", "design_002.pdb": "MNPQRSTVWY"}
    for name, s in seqs.items():
        (root / "shard_000" / name).write_text(_pdb(s), encoding="utf-8")
    with (root / "manifest.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["shard_index", "round", "bin_lo", "bin_hi", "job_id",
                    "call_id", "rank", "name", "binder_length", "total_reward",
                    "af2_iptm", "af2_plddt", "binder_scrmsd", "cluster_id",
                    "pdb_file"])
        w.writerow([0, 0, 60, 69, "job-a", "fc-a", 1, "d1", 10, -0.2,
                    0.91, 0.88, 1.2, "", "shard_000/design_001.pdb"])
        w.writerow([0, 0, 60, 69, "job-a", "fc-a", 2, "d2", 10, -0.5,
                    0.95, 0.70, 31.0, "", "shard_000/design_002.pdb"])
    (root / "ledger.jsonl").write_text(
        json.dumps({"index": 0, "bin": [60, 69], "state": "intent",
                    "job_id": "job-a"}) + "\n"
        + json.dumps({"index": 0, "state": "collected", "runtime_seconds": 3600,
                      "designs": 2}) + "\n", encoding="utf-8")
    return root


class TestSequencesComeBackOutOfTheCoordinates:

    def test_the_sequence_matches_what_went_in(self, campaign):
        seq, n = ec.parse_binder(campaign / "shard_000" / "design_001.pdb")
        assert seq == "ACDEFGHIKL"
        assert n == 10

    def test_only_the_binder_chain_is_read(self, tmp_path):
        p = tmp_path / "x.pdb"
        p.write_text(_pdb("AAAA", chain="A") + _pdb("CDEF", chain="C"),
                     encoding="utf-8")
        assert ec.parse_binder(p)[0] == "CDEF"

    def test_an_insertion_code_is_not_collapsed(self, tmp_path):
        """Two residues sharing a residue number differ only by icode. Keyed
        on the number alone they become one, silently shortening a design."""
        p = tmp_path / "x.pdb"
        p.write_text(
            f"ATOM      1  CA  ALA C 100 {0.0:>11.3f}{0.0:>8.3f}{0.0:>8.3f}\n"
            f"ATOM      2  CA  CYS C 100A{0.0:>11.3f}{0.0:>8.3f}{0.0:>8.3f}\n",
            encoding="utf-8")
        assert ec.parse_binder(p)[0] == "AC"

    def test_a_second_altloc_does_not_duplicate_a_residue(self, tmp_path):
        p = tmp_path / "x.pdb"
        p.write_text(
            f"ATOM      1  CA AALA C 100 {0.0:>11.3f}{0.0:>8.3f}{0.0:>8.3f}\n"
            f"ATOM      2  CA BSER C 100 {0.0:>11.3f}{0.0:>8.3f}{0.0:>8.3f}\n",
            encoding="utf-8")
        assert ec.parse_binder(p) == ("A", 1)

    def test_a_residue_modelled_only_as_altloc_B_is_not_dropped(self,
                                                                tmp_path):
        """Filtering to altloc A/blank looks safe and silently shortens the
        sequence. Dedup on (resseq, icode) already collapses altlocs, so the
        filter only ever costs residues."""
        p = tmp_path / "x.pdb"
        p.write_text(
            f"ATOM      1  CA  ALA C 100 {0.0:>11.3f}{0.0:>8.3f}{0.0:>8.3f}\n"
            f"ATOM      2  CA BCYS C 101 {0.0:>11.3f}{0.0:>8.3f}{0.0:>8.3f}\n",
            encoding="utf-8")
        assert ec.parse_binder(p) == ("AC", 2)

    def test_a_non_standard_residue_is_fatal_not_X(self, tmp_path):
        """Writing X into an orderable FASTA is worse than not writing it."""
        p = tmp_path / "x.pdb"
        p.write_text(
            f"ATOM      1  CA  MSE C 100 {0.0:>11.3f}{0.0:>8.3f}{0.0:>8.3f}\n",
            encoding="utf-8")
        with pytest.raises(ec.ExportError, match="standard twenty"):
            ec.parse_binder(p)


class TestTheExportRefusesRatherThanMislead:

    def test_a_missing_pdb_stops_the_whole_export(self, campaign):
        (campaign / "shard_000" / "design_002.pdb").unlink()
        with pytest.raises(ec.ExportError, match="not on disk"):
            ec.collect(campaign)

    def test_a_length_disagreement_stops_the_export(self, campaign):
        text = (campaign / "manifest.csv").read_text(encoding="utf-8")
        (campaign / "manifest.csv").write_text(
            text.replace(",10,-0.2,", ",99,-0.2,"), encoding="utf-8")
        with pytest.raises(ec.ExportError, match="manifest says 99"):
            ec.collect(campaign)

    def test_no_manifest_is_refused(self, tmp_path):
        with pytest.raises(ec.ExportError, match="no manifest"):
            ec.collect(tmp_path)

    def test_main_returns_nonzero_on_refusal(self, campaign, capsys):
        (campaign / "shard_000" / "design_001.pdb").unlink()
        rc = ec.main(["--campaign", str(campaign)])
        assert rc == 1
        assert "REFUSED" in capsys.readouterr().err


class TestWhatLands:

    def test_every_design_reaches_the_csv_and_the_fasta(self, campaign):
        prov = ec.write_export(campaign, campaign / "export")
        rows = list(csv.DictReader(
            (campaign / "export" / "designs.csv").open(encoding="utf-8")))
        assert len(rows) == prov["designs"] == 2
        fasta = (campaign / "export" / "all_designs.fasta").read_text()
        assert fasta.count(">") == 2
        assert "ACDEFGHIKL" in fasta

    def test_only_passing_designs_reach_passing_fasta(self, campaign):
        ec.write_export(campaign, campaign / "export")
        p = (campaign / "export" / "passing.fasta").read_text()
        assert p.count(">") == 1
        assert "ACDEFGHIKL" in p and "MNPQRSTVWY" not in p

    def test_high_iptm_does_not_pass_on_its_own(self, campaign):
        """design_002 has the HIGHER ipTM (0.95) and fails on scRMSD. A filter
        that read only ipTM would pick it."""
        rows, _ = ec.collect(campaign)
        d2 = [r for r in rows if r["rank"] == "2"][0]
        assert d2["af2_iptm"] == "0.95"
        assert d2["refolded"] == 0 and d2["passes"] == 0

    def test_checksums_cover_the_coordinates_not_just_the_exports(
            self, campaign):
        ec.write_export(campaign, campaign / "export")
        text = (campaign / "export" / "CHECKSUMS.sha256").read_text()
        assert "shard_000/design_001.pdb" in text
        assert "export/designs.csv" in text
        real = ec.sha256(campaign / "shard_000" / "design_001.pdb")
        assert real in text

    def test_provenance_carries_the_ledger_and_the_gpu_time(self, campaign):
        prov = ec.write_export(campaign, campaign / "export")
        assert prov["gpu_seconds"] == 3600
        # The ledger is replayed newest-wins, so the shard entry has to carry
        # BOTH the job_id written at `intent` and the runtime written at
        # `collected` — a plain last-record-wins read would lose the job_id.
        shard = prov["shards"][0]
        assert shard["job_id"] == "job-a"
        assert shard["runtime_seconds"] == 3600
        assert shard["state"] == "collected"
        assert shard["bin"] == [60, 69]

    def test_provenance_survives_a_torn_final_ledger_line(self, campaign):
        """A kill mid-write leaves a partial line. It must not take the
        export down with it — that is the point of one JSON object per line."""
        with (campaign / "ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"index": 0, "state": "colle')
        prov = ec.write_export(campaign, campaign / "export")
        assert prov["shards"][0]["state"] == "collected"

    def test_rerunning_is_idempotent(self, campaign):
        a = ec.write_export(campaign, campaign / "export")
        first = (campaign / "export" / "designs.csv").read_text()
        b = ec.write_export(campaign, campaign / "export")
        assert a["designs"] == b["designs"]
        assert (campaign / "export" / "designs.csv").read_text() == first

    def test_duplicate_sequences_are_counted(self, campaign):
        (campaign / "shard_000" / "design_002.pdb").write_text(
            _pdb("ACDEFGHIKL"), encoding="utf-8")
        prov = ec.write_export(campaign, campaign / "export")
        assert prov["distinct_sequences"] == 1
        assert prov["duplicate_sequences"] == 1


class TestSelfCopyDetection:
    """42.8% of tier 1 reproduced the target's own sequence, and the length
    result inverts depending on whether that is accounted for."""

    def test_a_verbatim_copy_of_the_target_is_flagged(self, campaign,
                                                      tmp_path):
        target = tmp_path / "target.pdb"
        target.write_text(_pdb("WWWACDEFGHIKLWWW", chain="A"),
                          encoding="utf-8")
        rows, _ = ec.collect(campaign, ec.target_sequences(target))
        d1 = [r for r in rows if r["rank"] == "1"][0]
        assert d1["target_overlap"] == 1.0
        assert d1["self_copy"] == 1

    def test_an_unrelated_sequence_is_not_flagged(self, campaign, tmp_path):
        target = tmp_path / "target.pdb"
        target.write_text(_pdb("WWWWWWWWWWWWWWWW", chain="A"),
                          encoding="utf-8")
        rows, _ = ec.collect(campaign, ec.target_sequences(target))
        assert all(r["self_copy"] == 0 for r in rows)

    def test_every_target_chain_is_checked_not_just_the_first(
            self, campaign, tmp_path):
        """The Fc is a homodimer staged as A and B; a copy of either counts."""
        target = tmp_path / "target.pdb"
        target.write_text(_pdb("WWWWWWWWWW", chain="A")
                          + _pdb("ACDEFGHIKL", chain="B"), encoding="utf-8")
        rows, _ = ec.collect(campaign, ec.target_sequences(target))
        assert [r for r in rows if r["rank"] == "1"][0]["self_copy"] == 1

    def test_the_columns_are_absent_without_a_target(self, campaign):
        rows, _ = ec.collect(campaign)
        assert "self_copy" not in rows[0]

    def test_longest_common_substring_is_contiguous_not_gapped(self):
        """Verbatim regurgitation is the mode being detected. A subsequence
        LCS would score every case below far higher and flag ordinary designs
        as copies — ABCXYZDEF/ABCDEF is 6 as a subsequence and 3 as a
        substring, which is the whole distinction."""
        assert ec.longest_common_substring("ABCDE", "ABCDE") == 5
        assert ec.longest_common_substring("ABCXYZDEF", "ABCDEF") == 3
        assert ec.longest_common_substring("AXCXE", "ABCDE") == 1
        assert ec.longest_common_substring("QQQ", "ABCDE") == 0
        assert ec.longest_common_substring("", "ABC") == 0

    def test_provenance_records_the_target_and_its_checksum(self, campaign,
                                                            tmp_path):
        target = tmp_path / "target.pdb"
        target.write_text(_pdb("WWWACDEFGHIKLWWW", chain="A"),
                          encoding="utf-8")
        prov = ec.write_export(campaign, campaign / "export", target)
        assert prov["target"]["sha256"] == ec.sha256(target)
        assert prov["target"]["chains"] == {"A": 16}
        assert prov["target"]["self_copies"] == 1


class TestThePlddtScaleIsStated:
    """The export prints a column called ``af2_plddt`` on 0-1. So does
    ``/jobs/<id>/export.csv`` -- on 0-100, since #200. Same design, same
    header, two numbers. The fix is to SAY which, not to rewrite one:
    manifest.csv sits in the parent directory on the same 0-1 scale, and
    a rescaled designs.csv would put two scales under one header inside
    one campaign directory.
    """

    def test_the_fasta_header_carries_both_readings(self, campaign):
        ec.write_export(campaign, campaign / "export")
        fasta = (campaign / "export" / "all_designs.fasta").read_text(
            encoding="utf-8")

        assert "plddt=0.88 (AF2 scale: 88.0/100)" in fasta, (
            "a reader seeing plddt=0.88 beside the field's usual "
            "'>80 is confidently folded' rule reads a catastrophic "
            "design, which is the opposite of what it says"
        )
        assert "plddt=0.7 (AF2 scale: 70.0/100)" in fasta

    def test_the_csv_value_is_NOT_rewritten(self, campaign):
        """The archival guarantee. Annotating is the whole change;
        rescaling would desync designs.csv from the manifest.csv beside
        it, which is the trap this is meant to prevent, not cause."""
        ec.write_export(campaign, campaign / "export")
        with (campaign / "export" / "designs.csv").open(
                encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))

        assert [r["af2_plddt"] for r in rows] == ["0.88", "0.7"]
        # ...and it still agrees with the manifest it was read from.
        with (campaign / "manifest.csv").open(
                encoding="utf-8", newline="") as fh:
            src = list(csv.DictReader(fh))
        assert [r["af2_plddt"] for r in rows] == [
            r["af2_plddt"] for r in src
        ]

    def test_provenance_records_the_scales_machine_readably(self, campaign):
        ec.write_export(campaign, campaign / "export")
        prov = json.loads(
            (campaign / "export" / "provenance.json").read_text(
                encoding="utf-8"))

        assert prov["scales"]["af2_plddt"] == "0-1"
        assert prov["scales"]["pdb_b_factor"] == "0-1"
        assert "0-100" in prov["scales"]["note"]

    def test_the_readme_names_the_scale(self, campaign):
        ec.write_export(campaign, campaign / "export")
        readme = (campaign / "export" / "README.md").read_text(
            encoding="utf-8")

        assert "## Scales" in readme
        assert "0-1 here, not 0-100" in readme

    @pytest.mark.parametrize(
        "raw, expected",
        [("0.88", "0.88 (AF2 scale: 88.0/100)"),
         ("88.0", "88.0"),
         ("", ""),
         ("n/a", "n/a"),
         (None, "None")],
    )
    def test_only_a_fractional_number_is_annotated(self, raw, expected):
        """A value already on 0-100 must NOT be annotated -- some CSVs
        write a plain ``plddt`` column that way, and labelling it would
        manufacture the confusion this exists to remove. Unparseable
        text comes back untouched rather than crashing an export whose
        entire point is that it either completes or refuses loudly."""
        assert ec.plddt_label(raw) == expected
