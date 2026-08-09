"""Tests for the multi-shard length-sweep driver.

Every test here drives the REAL functions. Nothing contacts Modal: the driver
splits spawn from collect precisely so the parts that touch money are one call
each and everything around them is offline.
"""

import base64
import csv
import json

import pytest

from tools.proteina import shard_driver as sd
from tools.proteina.direct_call_fc import build_job_spec, build_payload


class TestThePlanStaysBalanced:
    """Round-robin is the property that makes an interrupted campaign usable."""

    def test_every_prefix_is_balanced_to_within_one_shard(self):
        plan = sd.build_plan()
        seen = {tuple(b): 0 for b in sd.BINS}
        for item in plan:
            seen[tuple(item["bin"])] += 1
            counts = list(seen.values())
            assert max(counts) - min(counts) <= 1, (
                f"after {item['index'] + 1} shards the bins are {counts}; "
                "a bin-by-bin plan makes an interrupted run unanalysable")

    def test_the_plan_covers_every_bin_equally(self):
        plan = sd.build_plan()
        assert len(plan) == len(sd.BINS) * sd.SHARDS_PER_BIN
        for b in sd.BINS:
            assert sum(1 for i in plan if i["bin"] == list(b)) == sd.SHARDS_PER_BIN

    def test_index_is_stable_across_calls(self):
        """The ledger keys on index, so it must be reproducible on resume."""
        assert sd.build_plan() == sd.build_plan()


class TestPlanValidationRefusesBeforeSpending:

    def test_a_bin_outside_the_validated_range_is_refused(self):
        with pytest.raises(SystemExit, match="20-300"):
            sd._validate_plan([{"index": 0, "round": 0, "bin": [10, 40]}])
        with pytest.raises(SystemExit, match="20-300"):
            sd._validate_plan([{"index": 0, "round": 0, "bin": [50, 400]}])

    def test_an_inverted_bin_is_refused(self):
        with pytest.raises(SystemExit, match="lo > hi"):
            sd._validate_plan([{"index": 0, "round": 0, "bin": [90, 60]}])

    def test_a_shard_that_would_outrun_the_deadline_is_refused(self, monkeypatch):
        """113 minutes in is the wrong time to discover this."""
        monkeypatch.setattr(sd, "DESIGNS_PER_SHARD", 200)
        with pytest.raises(SystemExit, match="subprocess deadline"):
            sd._validate_plan(sd.build_plan())

    def test_the_shipped_configuration_validates(self):
        sd._validate_plan(sd.build_plan())


class TestTheLedgerSurvivesACrash:

    def test_later_records_merge_onto_earlier_ones(self, tmp_path):
        led = tmp_path / "ledger.jsonl"
        sd.ledger_append(led, {"index": 0, "state": "submitted",
                               "call_id": "fc-1", "job_id": "j0"})
        sd.ledger_append(led, {"index": 0, "state": "collected", "designs": 64})
        rec = sd.ledger_replay(led)[0]
        assert rec["state"] == "collected"
        assert rec["call_id"] == "fc-1", (
            "the merge dropped the call id; a resume could not reconnect")
        assert rec["designs"] == 64

    def test_a_truncated_final_line_is_skipped_not_fatal(self, tmp_path):
        led = tmp_path / "ledger.jsonl"
        sd.ledger_append(led, {"index": 0, "state": "submitted",
                               "call_id": "fc-1"})
        with led.open("a", encoding="utf-8") as fh:
            fh.write('{"index": 1, "state": "sub')      # killed mid-write
        state = sd.ledger_replay(led)
        assert set(state) == {0}
        assert state[0]["call_id"] == "fc-1"

    def test_a_missing_ledger_is_an_empty_campaign(self, tmp_path):
        assert sd.ledger_replay(tmp_path / "nope.jsonl") == {}


class TestAChangedPlanCannotMislabelPaidWork:
    """Bins changing mid-campaign would attach old call ids to new bins and
    silently mislabel every design that came back."""

    def test_a_changed_bin_is_refused(self):
        plan = sd.build_plan()
        state = {0: {"index": 0, "bin": [30, 40], "state": "submitted"}}
        with pytest.raises(SystemExit, match="BINS changed"):
            sd._verify_plan_matches_ledger(plan, state)

    def test_a_ledger_longer_than_the_plan_is_refused(self):
        plan = sd.build_plan()
        state = {len(plan) + 5: {"index": len(plan) + 5, "state": "collected"}}
        with pytest.raises(SystemExit, match="only"):
            sd._verify_plan_matches_ledger(plan, state)

    def test_a_matching_ledger_passes(self):
        plan = sd.build_plan()
        state = {0: {"index": 0, "bin": plan[0]["bin"], "state": "collected"}}
        sd._verify_plan_matches_ledger(plan, state)


class TestTheBudgetCountsEverythingThatWasBilled:

    def test_failed_and_empty_shards_still_count(self):
        """A shard that reached `submitted` was billed whether or not it
        produced designs. Counting only successes would let a run of failures
        walk straight through the ceiling."""
        state = {
            0: {"state": "collected"}, 1: {"state": "failed"},
            2: {"state": "empty"}, 3: {"state": "submitted"},
        }
        assert sd._spent_usd(state) == pytest.approx(4 * sd.shard_usd())

    def test_an_empty_campaign_has_spent_nothing(self):
        assert sd._spent_usd({}) == 0.0

    def test_the_cost_model_reproduces_the_measured_run(self):
        """8 designs were metered at $0.5528. The model must land on that or
        every projection built from it is wrong."""
        assert sd.shard_usd(8) == pytest.approx(0.5528, abs=0.005)


class TestHarvestKeepsWhatWasPaidFor:

    @staticmethod
    def _pdb(n_ca: int) -> bytes:
        lines = ["ATOM      1  CA  GLY A 100      0.000   0.000   0.000"]
        lines += [f"ATOM  {i + 2:>5}  CA  ALA C {i + 1:>3}"
                  f"      0.000   0.000   0.000" for i in range(n_ca)]
        return ("\n".join(lines) + "\nEND\n").encode()

    def _out(self, cands):
        return {"exit_code": 0, "smoke_result": {
            "status": "COMPLETED", "runtime_seconds": 5000,
            "candidates": cands}}

    def test_binder_length_counts_chain_C_only(self):
        assert sd._binder_length(self._pdb(66)) == 66

    def test_designs_are_written_and_rows_carry_the_real_length(self, tmp_path):
        item = {"index": 3, "round": 0, "bin": [50, 60]}
        out = self._out([
            {"rank": 1, "name": "bon_orig0_r0", "pdb_key": "design_001.pdb",
             "scores": {"total_reward": -0.3, "af2_iptm": 0.76,
                        "af2_plddt": 0.77, "binder_scrmsd": 1.2},
             "pdb_content_b64": base64.b64encode(self._pdb(54)).decode()},
        ])
        rows, with_atoms = sd._harvest(out, item, "job-x", "fc-x", tmp_path)
        assert with_atoms == 1
        assert (tmp_path / "shard_003" / "design_001.pdb").is_file()
        assert rows[0]["binder_length"] == 54, (
            "the manifest recorded the bin, not the realised length")
        assert rows[0]["bin_lo"] == 50 and rows[0]["bin_hi"] == 60
        assert rows[0]["shard_index"] == 3

    def test_a_cap_dropped_candidate_keeps_its_scores(self, tmp_path):
        """Its scores cost an A100 to compute and exist nowhere else."""
        out = self._out([
            {"rank": 1, "name": "capped", "pdb_key": "design_001.pdb",
             "scores": {"total_reward": -0.9, "af2_iptm": 0.1,
                        "af2_plddt": 0.5, "binder_scrmsd": 30.0}},
        ])
        rows, with_atoms = sd._harvest(
            out, {"index": 0, "round": 0, "bin": [50, 60]}, "j", "c", tmp_path)
        assert with_atoms == 0
        assert len(rows) == 1
        assert rows[0]["total_reward"] == -0.9
        assert rows[0]["pdb_file"] == ""

    def test_a_zero_design_shard_writes_its_result_and_no_rows(self, tmp_path):
        out = {"exit_code": 1, "smoke_result": {"status": "FAILED",
                                                "candidates": []}}
        rows, with_atoms = sd._harvest(
            out, {"index": 7, "round": 1, "bin": [90, 100]}, "j", "c", tmp_path)
        assert rows == [] and with_atoms == 0
        saved = json.loads(
            (tmp_path / "shard_007" / "smoke_result.json").read_text())
        assert saved["status"] == "FAILED", (
            "the diagnosis for a paid failure was not persisted")

    def test_manifest_appends_and_keeps_one_header(self, tmp_path):
        man = tmp_path / "manifest.csv"
        row = {c: "" for c in sd.MANIFEST_COLUMNS}
        sd._write_manifest_rows(man, [{**row, "rank": 1}])
        sd._write_manifest_rows(man, [{**row, "rank": 2}])
        parsed = list(csv.DictReader(man.open(encoding="utf-8")))
        assert [r["rank"] for r in parsed] == ["1", "2"]


class TestBinderLengthReachesTheJobSpec:

    def test_the_default_is_unchanged_for_every_existing_caller(self):
        assert build_job_spec(
            preset="protein_binder", nsamples=4, replicas=2
        )["binder_length"] == [60, 120]

    def test_a_bin_is_passed_through(self):
        spec = build_job_spec(preset="protein_binder", nsamples=16, replicas=4,
                              binder_length=(50, 60))
        assert spec["binder_length"] == [50, 60]

    def test_it_survives_into_the_payload(self):
        payload = build_payload(
            "https://x", preset="protein_binder", nsamples=16, replicas=4,
            job_id="j", binder_length=(90, 100))
        assert payload["job_spec"]["binder_length"] == [90, 100]
        assert "upload_urls_endpoint" not in payload, (
            "inline delivery depends on this field being absent")

    def test_the_pair_is_a_list_of_ints_not_a_tuple(self):
        """job_spec is JSON-serialised into the container; a tuple would round
        trip as a list anyway, but run_pipeline indexes [0]/[1] and a scalar
        or short sequence raises out of its list comprehension and burns a
        fully billed A100 with no diagnosis."""
        spec = build_job_spec(preset="protein_binder", nsamples=1, replicas=1,
                              binder_length=(50, 60))
        assert isinstance(spec["binder_length"], list)
        assert all(isinstance(v, int) for v in spec["binder_length"])
        assert len(spec["binder_length"]) == 2
