"""The rfd4 Modal app and pipeline: invocation, input refusals, streaming.

Runs fully offline. ``rfd4-generate`` is never invoked, no Modal function is
deployed and no GPU second is spent by this file: the subprocess driver is
exercised with a stub, ``requests.post`` is patched on the pipeline module, and
the design watcher runs against real files under ``tmp_path``. The structure
fixtures are the two already in the repo for ``tools/rfd4/numbering.py``.

What this file does NOT establish: that ``rfd4-generate`` accepts the condition
spec ``write_condition_spec`` emits, or the atom-level hotspot expression in it.
Scope §5.8 (line 517) records that the one paid run was redirected onto
upstream's own example spec
(``/opt/rfd4/docs/how-to/input_prep/examples/condition_spec_ppi.yaml``), so no
GPU run has consumed a spec this repo generates. That is §8 step 2's gate
(rung 6) and it is unrun.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from tools.rfd4 import numbering, run_pipeline
from tools.rfd4.run_pipeline import (
    RFD4_METRICS_TAGS,
    RFD4_ORACLE_TAG_SUBSTRINGS,
    RFD4_OVERRIDES,
    build_contig,
    build_hotspot_selections,
    collect_design_cifs,
    load_target,
    mapping_from_atom_site,
    new_design_stream,
    read_atom_site,
    resolve_target_chain,
    rfd4_command,
    run_rfd4,
    sweep_designs,
    parse_num_designs,
    validate_sizes,
    write_condition_spec,
)

REPO = Path(__file__).resolve().parent.parent
LYSOZYME = REPO / "static" / "example" / "1HEW.pdb"
FC_DIMER = REPO / "static" / "example" / "3ave_igg1_fc_dimer.pdb"
RUNG4_RESULT = REPO / "docs" / "scoping" / "rung4-probe" / "gpu_result_20261103T114437.json"

_WEBHOOK = "https://hub.example.com/webhooks/modal"


@pytest.fixture
def beats(monkeypatch):
    """Every heartbeat body ``send_heartbeat`` would POST."""
    sent: list[dict] = []

    class _Resp:
        status_code = 200

    def _post(url, json=None, **kw):
        sent.append(json)
        return _Resp()

    monkeypatch.setattr(run_pipeline.requests, "post", _post)
    return sent


@pytest.fixture
def results(monkeypatch, tmp_path):
    """Redirect the result file, and return a reader for it.

    ``_fail`` writes it and exits, so a refusal test reads its bucket here
    rather than only seeing SystemExit.
    """
    path = tmp_path / "smoke_results.json"
    monkeypatch.setattr(run_pipeline, "SMOKE_RESULTS_PATH", str(path))
    return lambda: json.loads(path.read_text())


def _mapping(pdb: Path):
    cif, mapping = numbering.pdb_to_cif(pdb.read_text())
    return cif, mapping, run_pipeline.atom_names_by_residue(read_atom_site(cif))


# ===========================================================================
# The invocation of record
# ===========================================================================


def test_the_command_is_rung_4s_command():
    """Every override and tag matches the run scope §5.8 calls production.

    Read out of the artefact rather than retyped, so a drifted constant fails
    here instead of at the next paid run.
    """
    rung4 = json.loads(RUNG4_RESULT.read_text())["cmd"]
    assert rung4[0] == "rfd4-generate"
    ours = rfd4_command(Path("/tmp/spec.yaml"), Path("/tmp/out"), 8)
    assert ours[0] == "rfd4-generate"
    # The per-request three differ by design: ours name our spec and our out dir.
    fixed_theirs = [a for a in rung4[1:] if not a.startswith(("inputs=", "out_dir="))]
    fixed_ours = [a for a in ours[1:] if not a.startswith(("inputs=", "out_dir="))]
    assert sorted(fixed_ours) == sorted(fixed_theirs)
    assert f"num_replicates={8}" in ours
    assert f"metrics_tags=[{','.join(RFD4_METRICS_TAGS)}]" in ours


def test_no_oracle_metric_group_is_selected():
    """Scope §5.2 line 346's asked-for guard, asserted on the constant (§5.3 line 364).

    The oracle weights are not in the image
    (tools/rfd4/Dockerfile.modal:14-19), so a tag that composed one would fail
    the run at weight load. This is a DENY-list: it catches a tag whose name
    contains one of the enumerated oracle substrings, and says nothing about a
    tag that contains none of them.
    """
    for tag in RFD4_METRICS_TAGS:
        for bad in RFD4_ORACLE_TAG_SUBSTRINGS:
            assert bad not in tag.lower(), (tag, bad)
    # The one override naming an oracle switches upstream's pre-oracle filter
    # OFF, which is the opposite of selecting a group.
    assert [o for o in RFD4_OVERRIDES if "oracle" in o] == [
        "delete_failing_pre_oracle=False"
    ]


def test_num_replicates_is_the_only_count_passed():
    """``batch_size`` is unset, because rung 4 did not set it."""
    ours = rfd4_command(Path("/tmp/s.yaml"), Path("/tmp/o"), 3)
    assert not [a for a in ours if a.startswith("batch_size")]
    assert len([a for a in ours if a.startswith("num_replicates=")]) == 1


# rfd4's _start_beat is covered by the generic pair in
# tests/test_live_charging.py (::test_start_beat_is_the_same_in_every_app and
# ::test_gpu_function_sends_the_start_beat_before_its_work), which parametrize
# over START_BEAT_APPS; that set now includes rfd4 because step 4 added the
# slug to _LIVE_ALL_PRESETS. The two copies that used to sit here were deleted
# rather than kept, as the generic ones assert the same two properties.


# ===========================================================================
# The _atom_site reader
# ===========================================================================

_EOL = """
"""
_MODEL_NUM_FIELD = "_atom_site.pdbx_PDB_model_num" + _EOL
_MINIMAL_HEAD = """data_X
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.label_atom_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.label_entity_id
_atom_site.label_seq_id
_atom_site.auth_asym_id
_atom_site.auth_seq_id
"""


def test_a_quoted_atom_name_stays_one_token():
    """``O5'`` is one atom name, not a name plus a stray quote.

    A reader that split on whitespace alone would shift every later column of
    the row, so the residue's atom list would be wrong rather than missing.
    """
    text = _MINIMAL_HEAD + "ATOM 1 \"O5'\" G A 1 1 A 1\nATOM 2 CA GLY A 1 2 A 2\n"
    rows = read_atom_site(text)
    assert [r["label_atom_id"] for r in rows] == ["O5'", "CA"]
    assert [r["auth_seq_id"] for r in rows] == ["1", "2"]


def test_a_row_wrapped_across_lines_is_one_row():
    text = _MINIMAL_HEAD + "ATOM 1 CA GLY\nA 1 1 A 1\n"
    rows = read_atom_site(text)
    assert len(rows) == 1
    assert rows[0]["label_seq_id"] == "1"


def test_a_semicolon_text_field_is_refused():
    text = _MINIMAL_HEAD + "ATOM 1 CA GLY A 1 1 A 1\n;something\n;\n"
    with pytest.raises(ValueError, match="semicolon"):
        read_atom_site(text)


def test_a_missing_required_column_is_refused():
    head = _MINIMAL_HEAD.replace("_atom_site.label_seq_id\n", "")
    with pytest.raises(ValueError, match="label_seq_id"):
        read_atom_site(head + "ATOM 1 CA GLY A 1 A 1\n")


def test_a_structure_with_no_atom_site_is_refused():
    with pytest.raises(ValueError, match="no _atom_site"):
        read_atom_site("data_X\n_cell.length_a 1.0\n")


def test_a_multi_model_structure_is_refused():
    """Taking model 1 silently would condition on a structure we do not send.

    ``load_target`` hands upstream the uploaded CIF unmodified
    (tools/rfd4/run_pipeline.py:595), so a mapping built from model 1 of a
    20-model ensemble would describe one structure while ``rfd4-generate``
    received another.
    """
    head = _MINIMAL_HEAD + _MODEL_NUM_FIELD
    text = head + """ATOM 1 CA GLY A 1 1 A 1 1
ATOM 2 CA GLY A 1 1 A 1 2
"""
    with pytest.raises(ValueError, match="2 models"):
        read_atom_site(text)
    assert len(read_atom_site(head + """ATOM 1 CA GLY A 1 1 A 1 1
""")) == 1


def test_the_reader_round_trips_the_writers_own_cif():
    """The two halves agree: every polymer residue ``pdb_to_cif`` wrote is read back."""
    cif, mapping, atoms = _mapping(LYSOZYME)
    rebuilt = mapping_from_atom_site(read_atom_site(cif))
    assert len(rebuilt.polymer_entries("A")) == len(mapping.polymer_entries("A"))
    assert rebuilt.label_for("A", 35) == mapping.label_for("A", 35) == ("A", 35)
    # And the atom list is the residue's real atoms, backbone included.
    assert atoms[("A", 35)][:2] == ("N", "CA")


def test_alternate_conformations_collapse_to_one_atom_list():
    text = _MINIMAL_HEAD + "ATOM 1 CB SER A 1 1 A 1\nATOM 2 CB SER A 1 1 A 1\n"
    atoms = run_pipeline.atom_names_by_residue(read_atom_site(text))
    assert atoms[("A", 1)] == ("CB",)


def test_a_pdb_upload_and_a_cif_upload_reach_the_same_numbering(tmp_path, results):
    """One downstream code path: the CIF route must not renumber the PDB route."""
    pdb_path = tmp_path / "t.pdb"
    pdb_path.write_text(LYSOZYME.read_text())
    _, from_pdb, _ = load_target(pdb_path, tmp_path)
    cif_text, _ = numbering.pdb_to_cif(LYSOZYME.read_text())
    cif_path = tmp_path / "t.cif"
    cif_path.write_text(cif_text)
    used, from_cif, _ = load_target(cif_path, tmp_path)
    assert used == cif_path  # a CIF upload is handed to upstream unmodified
    assert from_cif.label_for("A", 35) == from_pdb.label_for("A", 35)
    assert len(from_cif.polymer_entries("A")) == len(from_pdb.polymer_entries("A"))


# ===========================================================================
# Target chain, sizes, contig
# ===========================================================================


def test_a_single_polymer_chain_is_picked_even_with_non_polymer_chains(results):
    """1HEW carries 3 NAG on author chain B; that is not a second target chain."""
    _, mapping, _ = _mapping(LYSOZYME)
    assert sorted(mapping.chains()) == ["A", "B"]
    assert resolve_target_chain(mapping, "") == "A"


def test_a_two_chain_target_is_refused(results):
    """Scope §4.2 proposes multi-chain; no artefact shows upstream accepting one.

    The Fc dimer is the committed two-polymer-chain fixture. A refusal before
    the GPU starts is what keeps a dimer from being designed against one
    protomer while the run looks successful.
    """
    _, mapping, _ = _mapping(FC_DIMER)
    with pytest.raises(SystemExit):
        resolve_target_chain(mapping, "")
    assert results()["error"]["check"] == "chain"


def test_naming_one_chain_of_a_dimer_is_still_refused(results):
    _, mapping, _ = _mapping(FC_DIMER)
    with pytest.raises(SystemExit):
        resolve_target_chain(mapping, "A")
    assert "more than one polymer chain" in results()["error"]["detail"]


def test_an_unknown_chain_is_refused(results):
    _, mapping, _ = _mapping(LYSOZYME)
    with pytest.raises(SystemExit):
        resolve_target_chain(mapping, "Z")
    assert results()["error"]["check"] == "chain"


def test_a_negative_author_residue_keeps_its_sign():
    """The separator class ate the minus, so ``A-5`` read as residue 5.

    The hub's own parser reads these as negative
    (shared/pdb_inspect.py:367, :375); a sign dropped here conditions the GPU
    on a different residue while the run reports success.
    """
    assert run_pipeline._HOTSPOT_TOKEN.match("A-5").groups() == ("A", "-5")
    assert run_pipeline._HOTSPOT_TOKEN.match("-5").groups() == (None, "-5")
    assert run_pipeline._HOTSPOT_TOKEN.match("A:-12").groups() == ("A", "-12")
    assert run_pipeline._HOTSPOT_TOKEN.match("A 35").groups() == ("A", "35")


def test_zero_designs_is_refused_not_defaulted(results):
    """``int(raw or DEFAULT)`` would run eight paid designs for a request of 0."""
    assert parse_num_designs(None) == run_pipeline.DEFAULT_NUM_DESIGNS
    assert parse_num_designs("3") == 3
    for zero in (0, "0"):
        with pytest.raises(SystemExit):
            parse_num_designs(zero)
        assert results()["error"]["check"] == "nsamples"


def test_the_contig_covers_the_whole_chain_in_label_numbering():
    """Shape from ``docs/scoping/rung4-probe/spec_v4_chainA.yaml``."""
    _, mapping, _ = _mapping(LYSOZYME)
    assert build_contig(mapping, "A", (60, 120)) == "60-120, /0, A1-129"


def test_the_size_guards_refuse_outside_section_4_3(results, monkeypatch):
    _, mapping, _ = _mapping(LYSOZYME)
    assert validate_sizes(mapping, "A", (60, 120)) == 129
    monkeypatch.setattr(run_pipeline, "HARD_CAP_TARGET_AA", 100)
    with pytest.raises(SystemExit):
        validate_sizes(mapping, "A", (60, 120))
    assert results()["error"]["check"] == "target_size"


def test_the_caps_bound_the_label_span_a_gapped_cif_hands_upstream(results):
    """A gapped mmCIF upload defeats a cap counted on observed residues.

    ``build_contig`` spans the chain's whole LABEL range
    (tools/rfd4/run_pipeline.py:898), so unobserved residues make the segment
    upstream receives larger than the count -- without limit. 76 observed
    residues over a 440-residue span is 560 aa combined, over §4.3's 520.
    """
    head = _MINIMAL_HEAD + _MODEL_NUM_FIELD
    rows = [
        f"ATOM {n} CA GLY A 1 {seq} A {seq} 1"
        for n, seq in enumerate(list(range(1, 36)) + list(range(400, 441)), 1)
    ]
    mapping = mapping_from_atom_site(read_atom_site(head + _EOL.join(rows) + _EOL))
    assert len(mapping.polymer_entries("A")) == 76
    assert build_contig(mapping, "A", (60, 120)) == "60-120, /0, A1-440"
    with pytest.raises(SystemExit):
        validate_sizes(mapping, "A", (60, 120))
    assert results()["error"]["check"] == "target_size"


def test_the_combined_cap_counts_the_longest_binder(results):
    _, mapping, _ = _mapping(LYSOZYME)
    # 129 + 300 = 429 <= 520 passes; a 400-long binder would be 529.
    assert validate_sizes(mapping, "A", (20, 300)) == 129
    with pytest.raises(SystemExit):
        validate_sizes(mapping, "A", (20, 400))
    assert "520" in results()["error"]["detail"]


@pytest.mark.parametrize(
    "raw,expected",
    [(None, (60, 120)), ([], (60, 120)), ([80, 40], (40, 80)), (["30", "90"], (30, 90))],
)
def test_binder_length_defaults_and_normalises(raw, expected):
    assert run_pipeline.parse_binder_length(raw) == expected


@pytest.mark.parametrize("raw", [[10, 50], [60, 400], [60], [1, 2, 3]])
def test_an_out_of_range_binder_length_is_refused(raw, results):
    with pytest.raises(SystemExit):
        run_pipeline.parse_binder_length(raw)


# ===========================================================================
# Hotspots
# ===========================================================================


def test_a_hotspot_becomes_an_atom_level_selection():
    """Four fields with a CONCRETE residue id -- §10A's option B, unrun.

    A35 of 1HEW is GLU, whose side-chain heavy atoms are CB CG CD OE1 OE2; the
    backbone N CA C O the residue also carries are excluded, which is the
    difference between option B and the three-field residue form that is all
    any committed artefact has had accepted.
    """
    _, mapping, atoms = _mapping(LYSOZYME)
    sels = build_hotspot_selections(mapping, atoms, ["A35"], "A")
    assert sels == ["sel('A/*/35/[CB,CG,CD,OE1,OE2]')"]


def test_a_bare_number_takes_the_target_chain():
    _, mapping, atoms = _mapping(LYSOZYME)
    assert build_hotspot_selections(mapping, atoms, ["35"], "A") == build_hotspot_selections(
        mapping, atoms, ["A35"], "A"
    )


def test_duplicate_hotspots_collapse():
    _, mapping, atoms = _mapping(LYSOZYME)
    assert len(build_hotspot_selections(mapping, atoms, ["A35", "35", "A35"], "A")) == 1


def test_a_hotspot_not_in_the_structure_is_refused(results):
    _, mapping, atoms = _mapping(LYSOZYME)
    with pytest.raises(SystemExit):
        build_hotspot_selections(mapping, atoms, ["A999"], "A")
    assert results()["error"]["check"] == "hotspots"


def test_an_insertion_coded_hotspot_is_refused(results):
    """``shared/pdb_inspect.split_hotspot`` parses with ``int()``
    (shared/pdb_inspect.py:367, :375), so no picker produces one either."""
    _, mapping, atoms = _mapping(LYSOZYME)
    with pytest.raises(SystemExit):
        build_hotspot_selections(mapping, atoms, ["A52A"], "A")
    assert "insertion codes" in results()["error"]["detail"]


def test_no_hotspot_at_all_is_refused_before_the_gpu(results):
    """The configuration is hotspot-conditioned; there is nothing to condition on."""
    _, mapping, atoms = _mapping(LYSOZYME)
    with pytest.raises(SystemExit):
        build_hotspot_selections(mapping, atoms, [], "A")
    assert results()["error"]["check"] == "missing_hotspots"


def test_more_than_the_ceiling_of_hotspots_is_refused(results):
    _, mapping, atoms = _mapping(LYSOZYME)
    with pytest.raises(SystemExit):
        build_hotspot_selections(mapping, atoms, [f"A{i}" for i in range(1, 70)], "A")
    assert str(run_pipeline.MAX_HOTSPOTS) in results()["error"]["detail"]


def test_the_atom_set_has_exactly_one_decision_point():
    """§10 item 6 is open and lives in ``numbering``, not here.

    If this file grew its own atom list, the choice would have two homes and a
    later change could move one and not the other -- the shape of the proteina
    hotspot defect on record.
    """
    source = (REPO / "tools" / "rfd4" / "run_pipeline.py").read_text()
    assert "hotspot_atoms_for_residue" in source
    assert "sidechain_heavy" not in source
    assert numbering.RFD4_HOTSPOT_ATOM_SET == "sidechain_heavy"


# ===========================================================================
# The condition spec
# ===========================================================================


def test_the_spec_matches_the_prevalidated_shape(tmp_path):
    """Key for key against ``docs/scoping/rung4-probe/spec_v4_chainA.yaml``.

    That spec is the only one of ours upstream's CPU prevalidator has accepted
    (``docs/scoping/rung4-probe/cpu_result_20261103T103209.json``, exit 0). Its
    hotspot line is the three-field form; ours is the atom-level form, which is
    the one difference and is unrun.
    """
    out = write_condition_spec(
        tmp_path / "spec.yaml",
        cif_path=PurePosixPath("/tmp/in/target.cif"),
        contig="60-120, /0, A1-129",
        asym="A",
        hotspot_selections=["sel('A/*/35/[CB,CG]')"],
    )
    text = out.read_text()
    assert text.startswith("rfd4_binder:\n")
    for line in (
        '  input: "/tmp/in/target.cif"',
        '  contig: "60-120, /0, A1-129"',
        "  conditions:",
        "    C_CRDGRP:",
        "      1:",
        "    C_SEQ:",
        "      true:",
        "    C_HOT:",
    ):
        assert line in text, line
    assert "has_nan_coord()" in text
    assert text.count("- select:") == 3


def test_a_value_that_would_break_the_document_is_refused(tmp_path):
    """The spec is hand-written, so a quote or backslash is refused, not escaped."""
    with pytest.raises(ValueError):
        write_condition_spec(
            tmp_path / "s.yaml",
            cif_path=PurePosixPath('/tmp/a"b.cif'),
            contig="60-120, /0, A1-9",
            asym="A",
            hotspot_selections=[],
        )


def test_several_hotspots_become_several_entries(tmp_path):
    """Whether upstream unions them is UNVERIFIED; a run will be its first test."""
    out = write_condition_spec(
        tmp_path / "s.yaml",
        cif_path=PurePosixPath("/tmp/t.cif"),
        contig="60-120, /0, A1-9",
        asym="A",
        hotspot_selections=["sel('A/*/1/[CB]')", "sel('A/*/2/[CB]')"],
    )
    hot = out.read_text().split("    C_HOT:")[1]
    assert hot.count("- select:") == 2


# ===========================================================================
# The design watcher and streaming
# ===========================================================================


def _design(out_dir: Path, n: int, *, hidden: bool = False) -> Path:
    sub = out_dir / "inference_samples" / f"rfd4_binder_{n}"
    if hidden:
        sub = sub / f".generated-cif-{n}"
    sub.mkdir(parents=True, exist_ok=True)
    path = sub / f"rfd4_binder_{n}_generated.cif"
    path.write_text(f"data_D{n}\n")
    return path


def test_the_hidden_temp_directory_is_skipped(tmp_path):
    """Scope §10 item 3: each design is written through ``.generated-cif-*``.

    Without the skip the same design is taken twice -- two uploads, two
    ``new_candidate`` beats, two rows on the page for one structure.
    """
    _design(tmp_path, 1)
    _design(tmp_path, 2, hidden=True)
    found = collect_design_cifs(tmp_path)
    assert len(found) == 1
    assert ".generated-cif-" not in str(found[0])


def test_a_non_design_cif_is_not_taken(tmp_path):
    (tmp_path / "inference_samples").mkdir()
    (tmp_path / "inference_samples" / "target_input.cif").write_text("data_T\n")
    _design(tmp_path, 1)
    assert len(collect_design_cifs(tmp_path)) == 1


def test_each_design_is_streamed_once_as_it_lands(tmp_path, beats):
    """Two designs in one sweep is the measured case (scope §5.8 timings)."""
    stream = new_design_stream()
    uploaded: list[int] = []

    def upload_one(path, rank):
        uploaded.append(rank)
        return {"rank": rank, "name": path.stem, "pdb_key": f"{rank:03d}.cif"}

    _design(tmp_path, 1)
    _design(tmp_path, 2)
    sweep_designs(
        tmp_path, stream, upload_one,
        mid_run=True, webhook_url=_WEBHOOK, job_id="j1", planned_total=8,
    )
    assert uploaded == [0, 1]
    cands = [b for b in beats if b.get("new_candidate")]
    assert [b["new_candidate"]["rank"] for b in cands] == [0, 1]
    assert [b["designs_completed"] for b in cands] == [1, 2]
    assert all(b["designs_total"] == 8 for b in cands)
    # A second sweep over the same files takes nothing.
    sweep_designs(
        tmp_path, stream, upload_one,
        mid_run=True, webhook_url=_WEBHOOK, job_id="j1", planned_total=8,
    )
    assert uploaded == [0, 1]


def test_a_streamed_candidate_carries_no_score(tmp_path, beats):
    """At stream time there is none: both metrics CSVs land with the LAST design.

    Every key here is in ``_sanitize_candidate``'s allow-list
    (webhooks/modal.py:508-511).
    """
    stream = new_design_stream()
    _design(tmp_path, 1)
    sweep_designs(
        tmp_path, stream, lambda p, r: {"rank": r, "name": p.stem, "pdb_key": "000.cif"},
        mid_run=True, webhook_url=_WEBHOOK, job_id="j1", planned_total=8,
    )
    cand = [b for b in beats if b.get("new_candidate")][0]["new_candidate"]
    assert set(cand) == {"rank", "name", "pdb_key"}


def test_a_candidate_beat_carries_the_job_token(tmp_path, beats, monkeypatch):
    monkeypatch.setenv("JOB_TOKEN", "tok-1")
    stream = new_design_stream()
    _design(tmp_path, 1)
    sweep_designs(
        tmp_path, stream, lambda p, r: {"rank": r, "name": p.stem, "pdb_key": "000.cif"},
        mid_run=True, webhook_url=_WEBHOOK, job_id="j1", planned_total=8,
    )
    assert [b for b in beats if b.get("new_candidate")][0]["job_token"] == "tok-1"


def test_the_final_sweep_beats_the_measured_total(tmp_path, beats):
    """A short run reads complete only once it has been measured.

    ``shared/job_recovery.py::_completion_signal`` reads this snapshot, so a
    final beat still carrying the PLAN would make a finished 3-design run look
    like a killed 8-design one.
    """
    stream = new_design_stream()
    for n in (1, 2, 3):
        _design(tmp_path, n)
    sweep_designs(
        tmp_path, stream, lambda p, r: {"rank": r, "name": p.stem, "pdb_key": f"{r}.cif"},
        mid_run=True, webhook_url=_WEBHOOK, job_id="j1", planned_total=8,
    )
    assert beats[-1]["designs_total"] == 8
    sweep_designs(
        tmp_path, stream, lambda p, r: {"rank": r, "name": p.stem, "pdb_key": f"{r}.cif"},
        mid_run=False, webhook_url=_WEBHOOK, job_id="j1", planned_total=8,
    )
    assert beats[-1] == {
        "job_id": "j1", "stage": "designing", "designs_completed": 3, "designs_total": 3,
    }


def test_the_final_beat_is_sent_even_when_the_sweep_takes_nothing(tmp_path, beats):
    stream = new_design_stream()
    _design(tmp_path, 1)
    sweep_designs(
        tmp_path, stream, lambda p, r: {"rank": r, "name": p.stem, "pdb_key": "0.cif"},
        mid_run=True, webhook_url=_WEBHOOK, job_id="j1", planned_total=8,
    )
    before = len(beats)
    sweep_designs(
        tmp_path, stream, lambda p, r: {"rank": r, "name": p.stem, "pdb_key": "0.cif"},
        mid_run=False, webhook_url=_WEBHOOK, job_id="j1", planned_total=8,
    )
    assert len(beats) == before + 1
    assert "new_candidate" not in beats[-1]


def test_a_failed_upload_spends_the_rank_and_keeps_the_run(tmp_path, beats):
    """A design that cannot be uploaded is not delivered and not re-offered."""
    stream = new_design_stream()
    _design(tmp_path, 1)
    _design(tmp_path, 2)
    sweep_designs(
        tmp_path, stream,
        lambda p, r: None if r == 0 else {"rank": r, "name": p.stem, "pdb_key": "1.cif"},
        mid_run=False, webhook_url=_WEBHOOK, job_id="j1", planned_total=2,
    )
    assert [d["rank"] for d in stream["designs"]] == [1]
    assert beats[-1]["designs_completed"] == 1
    assert beats[-1]["designs_total"] == 2


def test_a_zero_byte_design_is_left_for_the_next_sweep(tmp_path, beats):
    stream = new_design_stream()
    path = _design(tmp_path, 1)
    path.write_text("")
    sweep_designs(
        tmp_path, stream, lambda p, r: {"rank": r, "name": p.stem, "pdb_key": "0.cif"},
        mid_run=True, webhook_url=_WEBHOOK, job_id="j1", planned_total=8,
    )
    assert stream["designs"] == []
    path.write_text("data_D1\n")
    sweep_designs(
        tmp_path, stream, lambda p, r: {"rank": r, "name": p.stem, "pdb_key": "0.cif"},
        mid_run=True, webhook_url=_WEBHOOK, job_id="j1", planned_total=8,
    )
    assert len(stream["designs"]) == 1


def test_the_keepalive_beats_while_nothing_has_landed(tmp_path, beats, monkeypatch):
    """Scope §5.8 measured 128.2 s before the first design appeared.

    ``gpu_seconds_used``, which is what a cancel bills, is refreshed only when
    a beat arrives.
    """
    stream = new_design_stream()
    stream["beat_at"] -= run_pipeline.KEEPALIVE_INTERVAL_S + 1
    sweep_designs(
        tmp_path, stream, lambda p, r: None,
        mid_run=True, webhook_url=_WEBHOOK, job_id="j1", planned_total=8,
    )
    assert beats[-1]["designs_completed"] == 0
    assert beats[-1]["designs_total"] == 8
    assert "new_candidate" not in beats[-1]


# ===========================================================================
# The subprocess driver
# ===========================================================================


def test_the_sweep_runs_between_waits_and_a_raise_does_not_kill_the_run(tmp_path, monkeypatch):
    calls: list[int] = []

    class _Proc:
        def __init__(self):
            self.n = 0

        def wait(self, timeout=None):
            self.n += 1
            if self.n < 3:
                raise subprocess.TimeoutExpired("rfd4-generate", timeout)
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _Proc())

    def on_poll():
        calls.append(1)
        raise RuntimeError("a sweep blew up")

    assert run_rfd4(tmp_path / "s.yaml", tmp_path / "out", 8, on_poll=on_poll) == 0
    assert len(calls) == 2


# ===========================================================================
# The wallet spec
# ===========================================================================


def test_the_wallet_scales_with_the_design_count(monkeypatch):
    """The estimate and the hard cap MOVE when the design count rises.

    ``_effective_scaling_value`` in shared/wallet_estimates.py looks the
    scaling key up in the caller's params and falls back to
    ``designs_per_run_baseline`` when it is absent -- silently. So a spec with
    no scaling param, or a key nothing supplies, does not raise: the estimate
    and the hard cap simply stop moving with the design count and every size
    prices as the default eight, and no assertion anywhere fires.
    """
    from shared import wallet_estimates as we

    # ``estimated_cost_for_tool`` reads historical p90 from tool_jobs, which
    # builds a Supabase client (shared/credits.py::get_service_client) -- a real
    # one, against production, unless it is stubbed. tests/test_min_charge.py
    # and tests/test_worst_case_hold_floor.py stub the same function.
    monkeypatch.setattr(we, "_historical_p90_seconds", lambda slug: None)

    spec = we.TOOL_SPECS["rfd4"]
    assert spec.scaling_param, "rfd4 must scale on something"
    small = {spec.scaling_param: spec.designs_per_run_baseline}
    large = {spec.scaling_param: spec.designs_per_run_baseline * 10}
    assert we.compute_hard_cap("rfd4", large) > we.compute_hard_cap("rfd4", small)
    assert we.estimated_cost_for_tool(None, "rfd4", large) > (
        we.estimated_cost_for_tool(None, "rfd4", small)
    )


def test_the_adapter_stamps_the_key_the_wallet_scales_on():
    """Whoever writes the rfd4 adapter must stamp ``scaling_param`` by that name.

    The test above proves the SPEC scales; it cannot prove anything reaches it,
    because the two shapes that feed it are built elsewhere -- the raw submit
    form (``_wallet_params_from_form`` in shared/wallet_guard.py) and the
    stored job_spec an adapter writes. tools/rfd4/__init__.py landed in step
    3, so the body below runs and reads the stamp out of that file's source;
    the skip is kept for the case of the file being removed, not because it is
    expected to fire. A missing stamp would price every size as the default
    eight in silence.

    The six adapters that already stamp this key are listed beside
    ``scaling_param`` in the rfd4 spec.
    """
    from shared import wallet_estimates as we

    adapter = REPO / "tools" / "rfd4" / "__init__.py"
    if not adapter.exists():
        pytest.skip("no rfd4 adapter yet (step 3); this guard arms when it lands")
    key = we.TOOL_SPECS["rfd4"].scaling_param
    assert f'"{key}"' in adapter.read_text(encoding="utf-8"), (
        f"the wallet scales rfd4 on {key!r} but the adapter never stamps that "
        f"key, so _effective_scaling_value falls back to the baseline and "
        f"every run prices as {we.TOOL_SPECS['rfd4'].designs_per_run_baseline} designs"
    )


# ===========================================================================
# The designed chain's sequence (the chained Boltz-2 fold's only input)
# ===========================================================================


def _rung4_design(n: int = 0) -> str:
    """One real design CIF from the committed rung-4 artefact."""
    files = json.loads(RUNG4_RESULT.read_text())["files"]
    key = f"/tmp/rfd4_run/inference_samples/pdl1_binder_{n}/pdl1_binder_{n}_generated.cif"
    return files[key]


def test_a_real_design_file_yields_one_sequence_per_chain():
    """Cross-check against the committed $0 rehearsal on the same bytes.

    ``docs/scoping/rung5-probe/prep_rung5.py::chain_seqs`` read these two
    chains as 115 (the PD-L1 target) and 108 (the binder) residues; the
    binder being the LONGER of a 115/116 pair in design 4 is why
    ``_designed_sequence`` discriminates on identity and not on length.
    """
    seqs = run_pipeline.chain_sequences(read_atom_site(_rung4_design(0)))
    assert {ch: len(s) for ch, s in seqs.items()} == {"A": 115, "B": 108}
    assert set("".join(seqs.values())) <= set("ACDEFGHIKLMNPQRSTVWY")


def test_the_binder_chain_is_picked_by_identity_not_by_length():
    text = _rung4_design(0)
    seqs = run_pipeline.chain_sequences(read_atom_site(text))
    picked = run_pipeline._designed_sequence(text, seqs["A"])
    assert picked == seqs["B"]
    # Design 4 is the case a length rule gets wrong: 116 aa binder, 115 aa
    # target. Identity still picks the binder.
    text4 = _rung4_design(4)
    seqs4 = run_pipeline.chain_sequences(read_atom_site(text4))
    assert len(seqs4["B"]) > len(seqs4["A"])
    assert run_pipeline._designed_sequence(text4, seqs4["A"]) == seqs4["B"]


def test_a_design_that_copies_the_target_still_yields_a_sequence():
    """Only ONE occurrence of the target is dropped (memory note
    ``proteina-target-self-copy``: a design can be the target verbatim)."""
    text = _MINIMAL_HEAD + (
        "ATOM 1 CA GLY A 1 1 A 1\n"
        "ATOM 2 CA GLY B 1 1 B 1\n"
    )
    assert run_pipeline._designed_sequence(text, "G") == "G"


def test_a_non_standard_residue_blocks_the_sequence():
    """``X`` is not foldable, and a wrong sequence is worse than no key."""
    text = _MINIMAL_HEAD + (
        "ATOM 1 CA GLY A 1 1 A 1\n"
        "ATOM 2 CA MSE B 1 1 B 1\n"
    )
    assert run_pipeline._designed_sequence(text, "G") == ""


def test_an_unknown_target_sequence_carries_nothing():
    text = _MINIMAL_HEAD + "ATOM 1 CA GLY A 1 1 A 1\n"
    assert run_pipeline._designed_sequence(text, "") == ""
    # Target not present at all: two chains remain, so no single binder.
    text2 = _MINIMAL_HEAD + (
        "ATOM 1 CA GLY A 1 1 A 1\n"
        "ATOM 2 CA ALA B 1 1 B 1\n"
    )
    assert run_pipeline._designed_sequence(text2, "W") == ""


def test_the_uploaded_entry_carries_the_sequence_only_when_it_is_certain(
    tmp_path, monkeypatch
):
    """The fourth key is what ``shared/refold.py`` folds, so it must be absent
    rather than wrong."""
    monkeypatch.setattr(
        run_pipeline, "request_upload_urls",
        lambda endpoint, token, keys: {k: f"https://up/{k}" for k in keys},
    )
    monkeypatch.setattr(run_pipeline, "upload_file", lambda *a, **k: None)
    path = tmp_path / "pdl1_binder_0_generated.cif"
    path.write_text(_rung4_design(0))
    seqs = run_pipeline.chain_sequences(read_atom_site(_rung4_design(0)))

    entry = run_pipeline._upload_design(
        path, 0, upload_endpoint="https://hub/u", job_token="t",
        target_sequence=seqs["A"],
    )
    assert entry == {
        "rank": 0, "name": "000_pdl1_binder_0_generated",
        "pdb_key": "000_pdl1_binder_0_generated.cif", "sequence": seqs["B"],
    }

    bare = run_pipeline._upload_design(
        path, 0, upload_endpoint="https://hub/u", job_token="t",
    )
    assert set(bare) == {"rank", "name", "pdb_key"}
