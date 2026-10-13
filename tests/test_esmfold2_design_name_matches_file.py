"""An esmfold2_design candidate's name, file, sequence and scores are ONE design.

Production job dffc8f2b (2026-10-13): the BEST DESIGN panel read "design_0 ·
iPTM 0.967", but the sequence and every score on that line were design_1's.
``_shape_designs`` named each design after its generation index, saved its PDB
under that name, then sorted by iPTM and renamed each one after its new
position. The file stayed with the design and the name moved, so
``design_0`` pointed at ``design_1_complex.pdb``. The multi-seed
``_aggregate`` did the same thing again with ``seed{s}_rank{r}``.

Rows already stored carry the wrong names, so ``ToolJob.from_row`` re-derives
each name from the file it points at.

Runs fully offline: PDB_OUTPUT_DIR is redirected into tmp_path, and no
webhook or upload endpoint is passed, so nothing leaves the process.
"""

from __future__ import annotations

import copy

import pytest

from shared.jobs import ToolJob
from tools.esmfold2_design import run_pipeline
from tools.esmfold2_design.modal_app import _aggregate
from tools.esmfold2_design.run_pipeline import CRITIC_REAL_IPTM, _shape_designs


class _Complex:
    def __init__(self, tag: str) -> None:
        self.tag = tag

    def to_pdb_string(self) -> str:
        return f"ATOM  {self.tag}\n"


def _rows() -> list[dict]:
    """Three designs whose iPTM RISES with generation index, so the sort
    reverses them and every design but the middle one changes position."""
    return [
        {
            "critic_name": CRITIC_REAL_IPTM,
            "designed_sequence": f"TARGET|QVQLVQSGG{'A' * i}",
            "iptm": 0.80 + i / 100,
            "distogram_iptm_proxy": 0.60,
            "cdr_distogram_iptm_proxy": 0.70,
            "final_loss": 0.30,
            "complex": _Complex(f"c{i}"),
        }
        for i in range(3)
    ]


@pytest.fixture
def pdb_dir(monkeypatch, tmp_path):
    out = tmp_path / "results"
    monkeypatch.setattr(run_pipeline, "PDB_OUTPUT_DIR", out)
    return out


@pytest.mark.parametrize("prefix", ["", "seed2_"])
def test_each_shaped_design_is_named_after_its_own_file(pdb_dir, prefix):
    designs = _shape_designs(_rows(), True, pdb_prefix=prefix)

    # Not vacuous: the sort did move designs, so a rename would show.
    assert [round(d["iptm"], 2) for d in designs] == [0.82, 0.81, 0.80]
    assert designs[0]["pdb_key"] == f"{prefix}design_2_complex.pdb"

    for d in designs:
        assert d["pdb_key"] == f"{d['name']}_complex.pdb"
        # The file under that name holds THIS design's structure: row i's
        # complex is tagged c{i} and its binder carries i trailing A's.
        i = len(d["sequence"]) - len("QVQLVQSGG")
        assert (pdb_dir / d["pdb_key"]).read_text() == f"ATOM  c{i}\n"


def _child(seed: int, iptm: float) -> dict:
    name = f"seed{seed}_design_0"
    return {
        "exit_code": 0,
        "smoke_result": {
            "status": "COMPLETED",
            "designs_total": 1,
            "designs_completed": 1,
            "designs": [{"rank": 0, "name": name,
                         "pdb_key": f"{name}_complex.pdb", "iptm": iptm}],
            "candidates": [{"rank": 0, "name": name,
                            "pdb_key": f"{name}_complex.pdb",
                            "sequence": f"SEQ{seed}",
                            "scores": {"ipTM": iptm}}],
            "runtime_seconds": 60,
        },
    }


def test_the_multi_seed_merge_keeps_each_name_with_its_file():
    # seed 1 scores higher, so the merge puts it first.
    out = _aggregate([(0, _child(0, 0.70)), (1, _child(1, 0.90))], [],
                     {"tier": "design", "job_id": "umb"})
    smoke = out["smoke_result"]
    assert [c["seed"] for c in smoke["candidates"]] == [1, 0]
    for row in smoke["candidates"] + smoke["designs"]:
        assert row["pdb_key"] == f"{row['name']}_complex.pdb"


def _row(tool: str, result: dict) -> dict:
    return {"id": "dffc8f2b-4c9f-4b37-b661-7f76e06d8f7e", "user_id": "u",
            "tool": tool, "preset": "scfv", "status": "succeeded",
            "job_token": "t", "result": result}


def _stored_dffc8f2b() -> dict:
    """The production record's shape: names follow position, files follow
    identity. design_0 is listed first because its iPTM is the top one, and
    its file is generation 1's."""
    rows = [
        {"rank": 0, "name": "design_0", "pdb_key": "design_1_complex.pdb",
         "sequence": "DEEAVQQILDLIAQLQAL", "scores": {"ipTM": 0.967}},
        {"rank": 1, "name": "design_1", "pdb_key": "design_0_complex.pdb",
         "sequence": "MAALLARIAELRRKLASF", "scores": {"ipTM": 0.961}},
    ]
    return {"candidates": rows, "designs": copy.deepcopy(rows)}


def test_a_stored_row_is_renamed_after_its_file_on_read():
    stored = _stored_dffc8f2b()
    before = copy.deepcopy(stored)
    job = ToolJob.from_row(_row("esmfold2-design", stored))

    for key in ("candidates", "designs"):
        names = [(r["name"], r["sequence"]) for r in job.result[key]]
        assert names == [("design_1", "DEEAVQQILDLIAQLQAL"),
                         ("design_0", "MAALLARIAELRRKLASF")]
    assert stored == before, "the row dict handed in must not be mutated"


def test_a_stored_multi_seed_row_is_renamed_after_its_file_on_read():
    stored = {"candidates": [
        {"rank": 0, "name": "seed1_rank0",
         "pdb_key": "seed1_design_0_complex.pdb", "scores": {"ipTM": 0.9}},
        {"rank": 1, "name": "seed0_rank1",
         "pdb_key": "seed0_design_3_complex.pdb", "scores": {"ipTM": 0.8}},
    ]}
    job = ToolJob.from_row(_row("esmfold2-design", stored))
    assert [c["name"] for c in job.result["candidates"]] == [
        "seed1_design_0", "seed0_design_3"]


def test_a_fixed_row_is_kept_and_a_recovered_row_gains_its_files_name():
    fixed = {"rank": 0, "name": "design_2", "pdb_key": "design_2_complex.pdb"}
    recovered = {"rank": 3, "pdb_key": "designs/design_3_complex.pdb"}
    job = ToolJob.from_row(
        _row("esmfold2-design", {"candidates": [fixed, recovered]}))
    assert job.result["candidates"][0] is fixed
    assert job.result["candidates"][1]["name"] == "design_3"


def test_a_row_with_no_file_keeps_its_stored_name():
    stored = {"candidates": [
        {"rank": 0, "name": "design_0", "pdb_key": None,
         "scores": {"ipTM": 0.9}},
    ]}
    job = ToolJob.from_row(_row("esmfold2-design", stored))
    assert job.result["candidates"][0]["name"] == "design_0"


def test_another_tools_row_is_left_alone():
    stored = _stored_dffc8f2b()
    job = ToolJob.from_row(_row("boltz2", stored))
    assert job.result == _stored_dffc8f2b()
