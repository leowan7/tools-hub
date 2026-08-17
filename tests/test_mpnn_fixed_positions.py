"""MPNN fixed-position redesign: validation, CLI wiring, and the effect check.

Most ways of getting this wrong crash upstream and need no guarding here: a dict
key that does not match the parsed PDB name, and a designed chain missing from
the dict, are bare subscripts (KeyError); a ``--pdb_path_chains`` value MPNN
cannot parse dies on ``seq_chain_A,B``. The genuinely SILENT ones are:

  * a ``--fixed_positions_jsonl`` path that does not exist — upstream sets
    ``fixed_positions_dict = None`` and designs every position, returning
    sequences that look entirely healthy. A caller that then splices in a
    "conserved" interface is splicing something that was silently rewritten.
  * a 0-indexed position — ``np.array([0]) - 1`` indexes -1 and freezes the LAST
    residue of the chain instead.
  * a run that designed NOTHING, where every requested position is trivially
    unchanged, so a check that only asks "did the frozen residues survive?"
    passes with full marks.

The last is why ``test_FAILS_on_a_native_echo`` exists and why the suite tests
``main()`` end to end rather than the checker alone: a safety net that can be
unplugged, or satisfied by doing nothing, is not a safety net.

The opposite error matters just as much. A heavily constrained run SHOULD return
near-identical samples, and the pre-existing ``reject_stub`` reads that as a
stub — so several tests here pin that correct low-diversity output survives.

Ground truth for the output format is upstream ``protein_mpnn_run.py``: the FASTA
sequence carries ONLY the designed chains (``_S_to_seq(S, chain_M)``), joined by
"/" in alphabetical chain order, and the native header carries
``designed_chains=[...]``.

Pure-function tests: no Modal, no GPU, no network.
"""
from __future__ import annotations

import json

import pytest

from tools.mpnn import run_pipeline as rp


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def _pdb(
    chains: dict[str, int],
    het_ca: int = 0,
    altloc_first: str = "",
    skip: tuple[str, int] | None = None,
    mse_first: str = "",
) -> str:
    """Minimal PDB: `chains` maps chain id -> residue count (one CA each).

    `altloc_first` gives residue 1 of that chain a second altloc conformer, which
    emits a second " CA " record for the SAME residue.
    `skip` omits one residue number entirely, leaving a gap in the numbering.
    `mse_first` writes residue 1 of that chain as a HETATM MSE.
    """
    lines, serial = [], 1

    def atom(chain: str, i: int, alt: str = " ", mse: bool = False) -> str:
        rec, resname = ("HETATM", "MSE") if mse else ("ATOM  ", "ALA")
        return (
            f"{rec}{serial:5d}  CA {alt}{resname} {chain}{i:4d}    "
            f"{0.0:8.3f}{0.0:8.3f}{float(i):8.3f}  1.00  0.00           C"
        )

    for chain, n in chains.items():
        for i in range(1, n + 1):
            if skip == (chain, i):
                continue
            if chain == altloc_first and i == 1:
                lines.append(atom(chain, i, "A"))
                serial += 1
                lines.append(atom(chain, i, "B"))
            elif chain == mse_first and i == 1:
                lines.append(atom(chain, i, mse=True))
            else:
                lines.append(atom(chain, i))
            serial += 1
    for i in range(het_ca):  # calcium ions, which also carry the atom name "CA"
        lines.append(
            f"HETATM{serial + i:5d}  CA   CA A{900 + i:4d}    "
            f"{0.0:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00  0.00          CA"
        )
    return "\n".join(lines) + "\n"


@pytest.fixture()
def pdb_abc(tmp_path):
    """Fc-like complex: A=8, B=6 (target context), C=4 (binder, designed)."""
    p = tmp_path / "design_001.pdb"
    p.write_text(_pdb({"A": 8, "B": 6, "C": 4}))
    return p


def _spec(fixed):
    return {"parameters": {"fixed_positions": fixed}}


def _header(designed, fixed=("A", "B")):
    """A realistic MPNN native header."""
    return (
        "design_001, score=1.1141, global_score=1.1141, "
        f"fixed_chains={list(fixed)}, designed_chains={list(designed)}, "
        "model_name=v_48_020, git_hash=abc123, seed=37"
    )


# --------------------------------------------------------------------------
# _chain_residue_counts / _designed_chains / _header_chain_list
# --------------------------------------------------------------------------
def test_chain_counts_ignore_calcium_hetatm(tmp_path):
    """A HETATM calcium is named " CA " too. Counting it inflates the chain."""
    p = tmp_path / "x.pdb"
    p.write_text(_pdb({"A": 5}, het_ca=3))
    assert rp._chain_residue_counts(p) == {"A": 5}


def test_chain_counts_collapse_altloc_conformers(tmp_path):
    """One residue with two altlocs is ONE residue. Counting atoms reports 6 for
    a 5-residue chain, and the bounds check would then accept position 6."""
    p = tmp_path / "x.pdb"
    p.write_text(_pdb({"A": 5}, altloc_first="A"))
    assert rp._chain_residue_counts(p) == {"A": 5}


def test_altloc_inflation_would_let_a_past_the_end_position_through(tmp_path):
    """The consequence, pinned directly: with atom-counting the out-of-range
    guard stops guarding."""
    p = tmp_path / "design_001.pdb"
    p.write_text(_pdb({"A": 5}, altloc_first="A"))
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"A": [6]}), p, "A")


def test_chain_counts_include_gaps_the_way_upstream_does(tmp_path):
    """parse_PDB_biounits walks range(min_resn, max_resn+1) and appends a gap
    token for absent numbers, so a 5-residue chain missing residue 3 is still
    5 positions to MPNN — 4 observed plus the gap."""
    p = tmp_path / "x.pdb"
    p.write_text(_pdb({"A": 5}, skip=("A", 3)))
    assert rp._chain_residue_counts(p) == {"A": 5}


def test_chain_counts_count_each_insertion_code(tmp_path):
    """Upstream nests xyz[resn][iCode] and flattens with sorted(xyz[resn]), so
    residue 2A and 2B occupy two consecutive positions, not one."""
    p = tmp_path / "x.pdb"
    body = _pdb({"A": 3}).splitlines()
    body.insert(2, body[1][:26] + "B" + body[1][27:])   # a second iCode at resSeq 2
    p.write_text("\n".join(body) + "\n")
    assert rp._chain_residue_counts(p) == {"A": 4}


def test_chain_counts_include_mse(tmp_path):
    """Upstream rewrites HETATM MSE to ATOM MET and counts it."""
    p = tmp_path / "x.pdb"
    p.write_text(_pdb({"A": 5}, mse_first="A"))
    assert rp._chain_residue_counts(p) == {"A": 5}


@pytest.mark.parametrize("n", [16, 22, 25, 26])
def test_chain_counts_survive_a_truncated_line(tmp_path, n):
    """A short record must not IndexError on the chain/iCode columns. 26 is the
    boundary — line[26] is the iCode — so the 26-char case must be a WELL-FORMED
    record truncated exactly there.

    Residue 99 on purpose: a truncated line carrying a resSeq that duplicates an
    existing residue would be absorbed into it, and a loosened length guard that
    wrongly PARSED the line would still report the same count."""
    full = "ATOM      9  CA  ALA A  99    "
    p = tmp_path / "x.pdb"
    p.write_text(_pdb({"A": 3}) + full[:n] + "\nEND\n")
    assert rp._chain_residue_counts(p) == {"A": 3}


def test_a_blank_residue_number_is_skipped_not_crashed(tmp_path):
    """A full-length record with an empty resSeq field: the token is "", so
    token[-1] would IndexError without the emptiness guard."""
    p = tmp_path / "x.pdb"
    blank = "ATOM      9  CA  ALA A        0.000   0.000   0.000  1.00  0.00"
    p.write_text(_pdb({"A": 3}) + blank + "\nEND\n")
    assert rp._chain_residue_counts(p) == {"A": 3}


def test_chain_counts_survive_a_non_utf8_byte(tmp_path):
    """Upstream decodes with errors='ignore'; an uncaught UnicodeDecodeError
    here would kill the run with no FAILED result written."""
    p = tmp_path / "x.pdb"
    p.write_bytes(_pdb({"A": 3}).encode() + b"REMARK  \xff\xfe bad bytes\n")
    assert rp._chain_residue_counts(p) == {"A": 3}


def test_gapped_chain_is_refused_for_fixed_positions(tmp_path):
    """Position i is upstream's index into the GAP-FILLED span, so on a gapped
    chain it stops meaning "the i-th residue" and the freeze lands elsewhere."""
    p = tmp_path / "design_001.pdb"
    p.write_text(_pdb({"A": 8}, skip=("A", 3)))
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"A": [5]}), p, "A")


def test_contiguous_chain_is_not_refused(tmp_path):
    """The contiguity guard must not reject ordinary designed backbones."""
    p = tmp_path / "design_001.pdb"
    p.write_text(_pdb({"A": 8}))
    assert rp.normalise_fixed_positions(_spec({"A": [5]}), p, "A") == {"A": [5]}


def _offset_pdb(path, start, n):
    """A contiguous chain A numbered from `start` instead of 1."""
    body = _pdb({"A": n}).splitlines()
    out = []
    for i, line in enumerate(body):
        out.append(line[:22] + f"{start + i:4d}" + line[26:])
    path.write_text("\n".join(out) + "\n")
    return path


def test_author_numbered_chain_is_refused(tmp_path):
    """A chain numbered from 20 accepts author number 100 and silently freezes
    residue 119 — the bounds check only catches numbers LARGER than the chain.
    Two plausible conventions differ by the offset and the request says nothing
    about which it holds, so refuse rather than pick one."""
    p = _offset_pdb(tmp_path / "design_001.pdb", 20, 211)
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"A": [100]}), p, "A")


def test_chain_numbered_from_one_is_accepted(tmp_path):
    """The offset guard must not reject the designed backbones this is for."""
    p = _offset_pdb(tmp_path / "design_001.pdb", 1, 60)
    assert rp.normalise_fixed_positions(_spec({"A": [30]}), p, "A") == {"A": [30]}


@pytest.mark.parametrize(
    "raw,expected",
    [("C", {"C"}), ("A B", {"A", "B"}), ("  A   B ", {"A", "B"}), ("", set())],
)
def test_designed_chains_splits_on_whitespace(raw, expected):
    assert rp._designed_chains(raw) == expected


def test_designed_chains_mirrors_upstream_on_commas():
    """Upstream is args.pdb_path_chains.split() — whitespace ONLY. Accepting
    commas here would validate against chains MPNN never matches."""
    assert rp._designed_chains("A,B") == {"A,B"}


def test_comma_separated_chains_are_rejected_with_the_reason(pdb_abc):
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"C": [1]}), pdb_abc, "B,C")


@pytest.mark.parametrize(
    "header,expected",
    [
        (_header(["C"]), ["C"]),
        (_header(["A", "C"]), ["A", "C"]),
        (_header([]), []),                       # designed nothing
        ("design_001, score=1.0", None),         # field absent entirely
        ("designed_chains=[A]", None),           # unquoted: unknown shape
        ("designed_chains=[ ]", []),             # whitespace only: genuinely empty
    ],
)
def test_header_chain_list_distinguishes_empty_from_absent(header, expected):
    assert rp._header_chain_list(header, "designed_chains") == expected


def test_unparseable_header_contents_do_not_read_as_designed_nothing():
    """Reading an unrecognised shape as [] would blame the wrong thing — the
    caller would be told MPNN designed no chains rather than that we cannot
    read its header."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs("MDEF"), ("design_001, designed_chains=[C]", "CDEF"),
            {"C": [1]}, COUNTS,
        )


# --------------------------------------------------------------------------
# normalise_fixed_positions
# --------------------------------------------------------------------------
@pytest.mark.parametrize("empty", [None, {}, []])
def test_absent_means_whole_chain_redesign(pdb_abc, empty):
    assert rp.normalise_fixed_positions(_spec(empty), pdb_abc, "C") == {}


def test_missing_parameters_block_means_whole_chain_redesign(pdb_abc):
    assert rp.normalise_fixed_positions({}, pdb_abc, "C") == {}


def test_happy_path_dedupes_and_sorts(pdb_abc):
    got = rp.normalise_fixed_positions(_spec({"C": [3, 1, 1]}), pdb_abc, "C")
    assert got == {"C": [1, 3]}


def test_zero_indexed_caller_is_rejected(pdb_abc):
    """The off-by-one that would otherwise freeze the wrong residues silently."""
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"C": [0, 1, 2]}), pdb_abc, "C")


def test_position_past_chain_end_is_rejected(pdb_abc):
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"C": [5]}), pdb_abc, "C")


def test_unknown_chain_is_rejected(pdb_abc):
    """Chain Z is in neither the PDB nor the designed set. Designing Z as well
    isolates this from the undesigned-chain check, which would otherwise fire
    first and let a broken PDB-membership test pass."""
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"Z": [1]}), pdb_abc, "C Z")


def test_fixing_an_undesigned_chain_is_rejected(pdb_abc):
    """Fixing positions on a chain MPNN never designs is a no-op that reads as
    a successful freeze."""
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"A": [1]}), pdb_abc, "C")


def test_fixing_every_residue_is_rejected(pdb_abc):
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"C": [1, 2, 3, 4]}), pdb_abc, "C")


def test_explicit_empty_list_is_rejected(pdb_abc):
    """Symmetric with fixing everything: an empty list is a full redesign in the
    shape of a freeze."""
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"C": []}), pdb_abc, "C")


def test_whitespace_duplicate_chain_keys_are_rejected(pdb_abc, caplog):
    """{"C": [1,2], "C ": [3]} strips to one key and would drop [1,2].

    Asserts the REASON, not just the exit: without the .strip() the second key
    fails as "not in the input PDB" instead, and a bare raises() would pass on
    the wrong error."""
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"C": [1, 2], "C ": [3]}), pdb_abc, "C")
    assert "appears more than once" in caplog.text


@pytest.mark.parametrize("bad", ["hello", ["a"], 7, True])
def test_a_non_dict_parameters_block_fails_cleanly(pdb_abc, bad):
    """A truthy non-dict raises AttributeError, which is neither TypeError nor
    ValueError — it escapes every catch, kills the run, and writes no FAILED
    result, so the job reports as an unexplained infrastructure failure. Guarded
    here rather than in main(), because every route in passes a raw job_spec."""
    assert rp.normalise_fixed_positions({"parameters": bad}, pdb_abc, "C") == {}


def test_five_wide_residue_numbers_are_not_mistaken_for_insertion_codes(tmp_path):
    """Upstream reads columns 22-27 as one token and only treats the last
    character as an iCode when it is ALPHABETIC. Splitting at a fixed 26 reads
    residue "   31" as residue 3 + iCode "1" and refuses an ordinary chain.

    Asserts residue IDENTITY, not just the count: both parses yield 3 residues
    here, so a count-only assertion passes under either and pins nothing."""
    body = _pdb({"A": 3}).splitlines()
    wide = [ln[:22] + f"{30 + i:5d}" + ln[27:] for i, ln in enumerate(body)]
    p = tmp_path / "design_001.pdb"
    p.write_text("\n".join(wide) + "\n")
    assert rp._chain_ca_residues(p) == {"A": {30: {" "}, 31: {" "}, 32: {" "}}}
    assert rp._chain_residue_counts(p) == {"A": 3}


def test_large_residue_numbers_keep_their_identity(tmp_path):
    """resSeq 10000-10004 splits at 26 into residue 1000 with iCodes 0-4 — same
    count, completely different residues, and it would trip the iCode refusal."""
    body = _pdb({"A": 5}).splitlines()
    wide = [ln[:22] + f"{10000 + i:5d}" + ln[27:] for i, ln in enumerate(body)]
    p = tmp_path / "design_001.pdb"
    p.write_text("\n".join(wide) + "\n")
    assert rp._chain_ca_residues(p) == {"A": {n: {" "} for n in range(10000, 10005)}}


def test_insertion_codes_are_refused(tmp_path):
    """An iCode occupies a position of its own without leaving a gap, so the
    contiguity check cannot see it: 1, 2, 2B, 3 is 4 positions in which position
    4 is author residue 3."""
    body = _pdb({"A": 6}).splitlines()
    body.insert(2, body[1][:26] + "B" + body[1][27:])   # residue 2B
    p = tmp_path / "design_001.pdb"
    p.write_text("\n".join(body) + "\n")
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"A": [4]}), p, "A")


@pytest.mark.parametrize("bad", ["C", ["C"], 3])
def test_non_object_payload_is_rejected(pdb_abc, bad):
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec(bad), pdb_abc, "C")


@pytest.mark.parametrize(
    "bad",
    [
        ["x"],          # not a number at all
        [True],         # int(True) == 1 -> would freeze position 1
        [3.9],          # int(3.9) == 3  -> would freeze position 3
        ["2"],          # int("2") == 2  -> would freeze position 2
        [float("inf")], # int(inf) raises OverflowError, not ValueError
    ],
)
def test_non_integer_positions_are_rejected_not_coerced(pdb_abc, bad):
    """Every one of these coerces to a valid position that then verifies
    perfectly — because whatever got frozen IS frozen."""
    with pytest.raises(SystemExit):
        rp.normalise_fixed_positions(_spec({"C": bad}), pdb_abc, "C")


def test_multi_chain_design_accepts_either_chain(pdb_abc):
    got = rp.normalise_fixed_positions(_spec({"B": [2]}), pdb_abc, "B C")
    assert got == {"B": [2]}
    # ...and run_mpnn must widen it to every designed chain before the wire.
    # See test_jsonl_carries_EVERY_designed_chain.


# --------------------------------------------------------------------------
# run_mpnn CLI wiring
# --------------------------------------------------------------------------
class _R:
    returncode = 0
    stdout = ""
    stderr = ""


def test_jsonl_is_keyed_by_the_staged_stem_and_flag_is_passed(pdb_abc, tmp_path, monkeypatch):
    """A key that does not match the parsed PDB name fixes NOTHING, silently."""
    captured = {}
    monkeypatch.setattr(
        rp.subprocess, "run", lambda cmd, **kw: (captured.__setitem__("cmd", cmd), _R())[1]
    )
    wd = tmp_path / "wd"
    wd.mkdir()
    rp.run_mpnn(
        target_pdb=pdb_abc,
        chains_to_design="C",
        num_seq_per_target=2,
        sampling_temp=0.1,
        workdir=wd,
        fixed_positions={"C": [1, 2]},
    )
    cmd = captured["cmd"]
    assert "--fixed_positions_jsonl" in cmd
    fp = cmd[cmd.index("--fixed_positions_jsonl") + 1]
    doc = json.loads(open(fp).read())
    # the key must be the stem of the file handed to --pdb_path
    staged = cmd[cmd.index("--pdb_path") + 1]
    assert list(doc) == [rp.Path(staged).stem] == ["design_001"]
    assert doc["design_001"] == {"C": [1, 2]}


def test_jsonl_carries_EVERY_designed_chain(pdb_abc, tmp_path, monkeypatch):
    """Upstream reads fixed_position_dict[name][letter] with a BARE subscript,
    once per designed chain, so a designed chain missing from the dict is a
    KeyError that kills the run AFTER the GPU is billed. Upstream's own
    make_fixed_positions_dict.py emits every chain, empty list included, and the
    `if fixed_pos_list:` guard beside the lookup makes [] a safe no-op."""
    captured = {}
    monkeypatch.setattr(
        rp.subprocess, "run", lambda cmd, **kw: (captured.__setitem__("cmd", cmd), _R())[1]
    )
    wd = tmp_path / "wd_multi"
    wd.mkdir()
    rp.run_mpnn(
        target_pdb=pdb_abc,
        chains_to_design="B C",
        num_seq_per_target=2,
        sampling_temp=0.1,
        workdir=wd,
        fixed_positions={"B": [2]},          # nothing fixed on C
    )
    cmd = captured["cmd"]
    fp = cmd[cmd.index("--fixed_positions_jsonl") + 1]
    doc = json.loads(open(fp).read())
    assert doc["design_001"] == {"B": [2], "C": []}


@pytest.mark.parametrize(
    "suffix,ok",
    [(".pdb", True), (".PDB", True), (".ent", True), (".pdb1", False), (".p", False)],
)
def test_jsonl_requires_a_four_character_extension(pdb_abc, tmp_path, suffix, ok, monkeypatch):
    """Upstream strips exactly 4 chars to key the dict (`biounit[(fi+1):-4]`),
    which equals Path.stem only for a 4-char extension. ".pdb1" would key as
    "x.p" there and "x" here — a KeyError mid-run."""
    p = tmp_path / f"design_001{suffix}"
    p.write_text(pdb_abc.read_text())
    monkeypatch.setattr(rp.subprocess, "run", lambda cmd, **kw: _R())
    wd = tmp_path / f"wd{suffix.replace('.', '_')}"
    wd.mkdir()
    call = dict(
        target_pdb=p,
        chains_to_design="C",
        num_seq_per_target=2,
        sampling_temp=0.1,
        workdir=wd,
        fixed_positions={"C": [1]},
    )
    if ok:
        rp.run_mpnn(**call)
    else:
        with pytest.raises(SystemExit):
            rp.run_mpnn(**call)


def test_extension_is_only_checked_when_positions_are_fixed(pdb_abc, tmp_path, monkeypatch):
    """The key only has to match when a fixed-positions file is keyed by it, so
    an ordinary redesign must not be refused for its filename."""
    p = tmp_path / "design_001.pdb1"
    p.write_text(pdb_abc.read_text())
    monkeypatch.setattr(rp.subprocess, "run", lambda cmd, **kw: _R())
    wd = tmp_path / "wd_ext_free"
    wd.mkdir()
    rp.run_mpnn(
        target_pdb=p,
        chains_to_design="C",
        num_seq_per_target=2,
        sampling_temp=0.1,
        workdir=wd,
        fixed_positions={},
    )


def test_no_flag_when_nothing_is_fixed(pdb_abc, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        rp.subprocess, "run", lambda cmd, **kw: (captured.__setitem__("cmd", cmd), _R())[1]
    )
    wd = tmp_path / "wd2"
    wd.mkdir()
    rp.run_mpnn(
        target_pdb=pdb_abc,
        chains_to_design="C",
        num_seq_per_target=2,
        sampling_temp=0.1,
        workdir=wd,
        fixed_positions={},
    )
    assert "--fixed_positions_jsonl" not in captured["cmd"]


# --------------------------------------------------------------------------
# _native_record
# --------------------------------------------------------------------------
def _write_fa(out_dir, stem, header, native, samples):
    seqs = out_dir / "seqs"
    seqs.mkdir(parents=True, exist_ok=True)
    body = f">{header}\n{native}\n"
    for i, s in enumerate(samples, 1):
        body += f">T=0.1, sample={i}, score=1.0, global_score=1.0, seq_recovery=0.5\n{s}\n"
    (seqs / f"{stem}.fa").write_text(body)


def test_native_record_returns_header_and_sequence(tmp_path):
    _write_fa(tmp_path, "design_001", _header(["C"]), "CDEF", ["MDEF"])
    got = rp._native_record(tmp_path, "design_001")
    assert got == (_header(["C"]), "CDEF")


def test_native_record_is_none_without_a_fasta(tmp_path):
    assert rp._native_record(tmp_path, "nope") is None


# --------------------------------------------------------------------------
# verify_fixed_positions -- the fail-open AND fail-closed guards
# --------------------------------------------------------------------------
# Only the DESIGNED chain appears in MPNN's output. Chains A and B are context.
COUNTS = {"A": 8, "B": 6, "C": 4}
NATIVE = (_header(["C"]), "CDEF")

# Long enough that "nothing changed" clears MIN_FREE_TO_JUDGE_DIVERSITY and so
# counts as evidence of a no-op run rather than an ordinary design outcome.
NAT20 = "CDEFGHIKLMNPQRSTVWYA"
DESIGNED20 = "CD" + "M" * 18          # positions 1-2 conserved, the rest redesigned
NATIVE20 = (_header(["C"]), NAT20)
COUNTS20 = {"A": 8, "B": 6, "C": 20}


def _seqs(*designed):
    return [{"seq": s} for s in designed]


def test_passes_when_the_fixed_residues_survive():
    out = rp.verify_fixed_positions(
        _seqs("CDMM", "CDWW"), NATIVE, {"C": [1, 2]}, COUNTS
    )
    assert out["checked"] is True
    assert out["n_assertions"] == 4          # 2 positions x 2 sequences
    assert out["free_positions"] == {"C": 2}
    assert out["free_changes"] == {"C": 4}   # 2 free positions x 2 sequences
    assert out["designed_chains"] == ["C"]


def test_FAILS_when_a_fixed_residue_was_rewritten():
    """The fail-open case: MPNN ignoring the file returns a healthy-looking full
    redesign, and only this check can tell."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs("MDEF"),                   # position 1 of C changed C -> M
            NATIVE,
            {"C": [1, 2]},
            COUNTS,
        )


def test_FAILS_on_an_interior_fixed_position():
    """Not just position 1 — an off-by-one in the index base would pass a check
    that only ever looked at the first residue."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs("CDMF"),                   # position 3 changed E -> M
            NATIVE,
            {"C": [3]},
            COUNTS,
        )


def test_FAILS_on_a_native_echo():
    """THE vacuous-pass case. Every fixed position survives perfectly because
    nothing was designed at all. n=1, so reject_stub (which needs n>=2) is blind
    to it and only this guard is left."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(_seqs(NAT20), NATIVE20, {"C": [1, 2]}, COUNTS20)


def test_FAILS_when_every_sequence_is_a_native_echo():
    """Same hole with more samples, in case reject_stub is ever loosened."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs(NAT20, NAT20), NATIVE20, {"C": [1, 2]}, COUNTS20
        )


def test_a_tiny_all_native_result_is_NOT_called_an_echo():
    """The other side of the trade. Two free positions recovering native at
    T=0.1 is an ordinary design outcome, not evidence of a stub — failing here
    would bill a point-mutation scan and then reject its correct answer. Reported
    as unjudged rather than silently treated as verified."""
    out = rp.verify_fixed_positions(_seqs("CDEF"), NATIVE, {"C": [1, 2]}, COUNTS)
    assert out["checked"] is True
    assert out["echo_judged"] is False
    assert out["free_changes"] == {"C": 0}


@pytest.mark.parametrize("n_seq", [1, 4, 8, 16])
def test_echo_threshold_counts_POSITIONS_not_comparisons(n_seq):
    """Summing free comparisons across sequences would make the threshold depend
    on num_seq_per_target: at the production n=8 a 2-free-position run would trip
    it, which is precisely the case the threshold exists to protect."""
    out = rp.verify_fixed_positions(
        _seqs(*["CDEF"] * n_seq), NATIVE, {"C": [1, 2]}, COUNTS
    )
    assert out["echo_judged"] is False


def test_echo_judgement_turns_on_at_the_threshold():
    """Pin the boundary itself: below MIN_FREE_TO_JUDGE_DIVERSITY free positions
    the run is unjudged, at or above it an all-native result fails."""
    n = rp.MIN_FREE_TO_JUDGE_DIVERSITY
    nat = "A" * (n + 1)                      # 1 fixed + n free positions
    hdr = (_header(["C"]), nat)
    assert rp.verify_fixed_positions(
        _seqs(nat[:-1] + "M"), hdr, {"C": [1]}, {"C": n + 1}
    )["echo_judged"] is True
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(_seqs(nat), hdr, {"C": [1]}, {"C": n + 1})
    # One below the threshold: unjudged, not failed.
    nat2 = "A" * n
    assert rp.verify_fixed_positions(
        _seqs(nat2), (_header(["C"]), nat2), {"C": [1]}, {"C": n}
    )["echo_judged"] is False


def test_a_dead_chain_is_not_masked_by_a_busy_one():
    """Judged per chain: a global counter lets one active chain hide another
    MPNN never touched, which is the same vacuous pass at chain granularity."""
    n = rp.MIN_FREE_TO_JUDGE_DIVERSITY
    nat = "A" * n
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            # chain C returned untouched; chain D fully rewritten past its freeze
            _seqs(nat + "/" + "A" + "M" * (n - 1)),
            (_header(["C", "D"], fixed=[]), nat + "/" + nat),
            {"D": [1]},
            {"C": n, "D": n},
        )


def test_FAILS_when_no_fixed_position_was_actually_compared():
    """checked=True on zero assertions is the same vacuous-success shape one
    level up. Reachable when a requested position falls outside the segment MPNN
    emitted — e.g. after a parser disagreement on a chain we could not cross-check."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs("MDEF"), NATIVE, {"C": [9]}, {}      # chain C absent: no cross-check
        )


def test_FAILS_when_the_fixed_chain_was_not_designed():
    """designed_chains=[] is what a --pdb_path_chains MPNN could not parse looks
    like from the output side. Freezing residues in an undesigned chain is
    vacuous, however perfectly they survive."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs("CDEF"), (_header([]), "CDEF"), {"C": [1]}, COUNTS
        )


def test_FAILS_when_a_fixed_chain_is_missing_from_designed_chains():
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs("AAAAAAAA"), (_header(["A"]), "AAAAAAAA"), {"C": [1]}, COUNTS
        )


def test_FAILS_when_the_header_has_no_designed_chains_field():
    """An MPNN build that does not report it leaves no sound segment mapping."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs("MDEF"), ("design_001, score=1.0", "CDEF"), {"C": [1]}, COUNTS
        )


@pytest.mark.parametrize("ours", [7, 2])
def test_FAILS_when_our_residue_count_disagrees_with_mpnn(ours):
    """If the two parsers disagree the bounds check ran against the wrong length,
    so the positions may not be the residues the caller named.

    Both directions matter and only one is realistic: gaps and MSE make MPNN's
    count LARGER than a naive observed-residue count, so testing ours>theirs
    alone would leave the real-world direction unpinned."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs("CDMF"), NATIVE, {"C": [1]}, {"A": 8, "B": 6, "C": ours}
        )


def test_unfixed_positions_are_free_to_change():
    """The check must not accidentally require the whole chain to be conserved."""
    out = rp.verify_fixed_positions(_seqs("CDMM"), NATIVE, {"C": [1, 2]}, COUNTS)
    assert out["checked"] is True


@pytest.mark.parametrize("seq", ["CDEFGH", "CDE"])
def test_FAILS_on_segmentation_mismatch(seq):
    """Both directions: a record LONGER and a record SHORTER than the native.
    Testing only the longer one leaves a `!=` that could be `>` unpinned, and a
    short record would then index past the end of its own segment."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(_seqs(seq), NATIVE, {"C": [1]}, COUNTS)


def test_a_chain_we_could_not_count_skips_the_cross_check():
    """The cross-check is guarded by `chain in chain_counts`. Dropping that guard
    turns an uncountable chain into a hard failure (or a KeyError) rather than an
    unchecked one, so pin that it stays a clean pass."""
    out = rp.verify_fixed_positions(_seqs("CDMM"), NATIVE, {"C": [1]}, {})
    assert out["checked"] is True
    assert out["n_assertions"] == 1


def test_FAILS_when_a_fixed_chain_is_absent_from_the_output_entirely():
    """Isolates the designed_chains membership check: chain C is present and
    verifies fine, so only the missing chain D can fail this."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs("CDMM"), NATIVE, {"C": [1], "D": [1]}, COUNTS
        )


def test_FAILS_when_segment_count_disagrees_with_the_header():
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs("CDEF/CDEF"), (_header(["C"]), "CDEF/CDEF"), {"C": [1]}, COUNTS
        )


def test_FAILS_without_a_native_record():
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(_seqs("CDEF"), None, {"C": [1]}, COUNTS)


def test_HOMODIMER_of_equal_length_chains_verifies():
    """Two designed chains of identical length. Mapping by residue count made
    this ambiguous and refused it — after the GPU was billed — even though the
    header states the order outright. Fc homodimers are the main target here."""
    out = rp.verify_fixed_positions(
        _seqs("MDEF/CDWW"),
        (_header(["A", "B"], fixed=[]), "CDEF/CDEF"),
        {"B": [1, 2]},
        {"A": 4, "B": 4},
    )
    assert out["checked"] is True
    assert out["designed_chains"] == ["A", "B"]
    assert out["n_assertions"] == 2


def test_multi_chain_positions_map_to_the_right_segment():
    """Chain order comes from the header, so a position fixed on the SECOND
    designed chain must be compared against the second segment."""
    with pytest.raises(SystemExit):
        rp.verify_fixed_positions(
            _seqs("CDEF/MDEF"),              # segment 2 position 1 changed
            (_header(["A", "B"], fixed=[]), "CDEF/CDEF"),
            {"B": [1]},
            {"A": 4, "B": 4},
        )


def test_no_request_means_no_check():
    out = rp.verify_fixed_positions(_seqs("AAAA"), (_header(["A"]), "AAAA"), {}, {"A": 4})
    assert out["checked"] is False


# --------------------------------------------------------------------------
# reject_stub vs a constrained run -- low diversity is the CORRECT answer here
# --------------------------------------------------------------------------
def test_stub_guard_still_fires_on_an_unconstrained_redesign():
    """The pre-existing contract must not be loosened for ordinary runs."""
    with pytest.raises(SystemExit):
        rp.reject_stub(_seqs("MKWVAHEDEL", "MKWVAHEDEL"))
    with pytest.raises(SystemExit):
        rp.reject_stub(_seqs("MKWVAHEDEL", "MKWVAHEDEL"), n_free_positions=None)


def test_stub_guard_would_reject_a_CORRECT_constrained_run():
    """Freeze 105 of 110 residues at T=0.1 and all 8 samples collide — that is
    MPNN doing exactly the right thing. Every stub guard reads it as a stub, and
    the near-clone guard trips at Hamming 2, which a 5-position redesign can
    rarely exceed. Left unguarded, a legitimate rescue run is billed and then
    reported FAILED with a message blaming the model."""
    identical = _seqs(*["MKWVAHEDEL"] * 8)
    with pytest.raises(SystemExit):                       # what it used to do
        rp.reject_stub(identical)
    rp.reject_stub(identical, n_free_positions=5)         # ...and no longer does


def test_stub_guard_resumes_once_enough_positions_are_free():
    n = rp.MIN_FREE_TO_JUDGE_DIVERSITY
    identical = _seqs(*["MKWVAHEDEL"] * 8)
    rp.reject_stub(identical, n_free_positions=n - 1)
    with pytest.raises(SystemExit):
        rp.reject_stub(identical, n_free_positions=n)


def _near_clones(n_samples: int = 4) -> list[dict]:
    """Distinct samples within Hamming 2 of each other, with score/recovery
    spread deliberately wide so ONLY the near-clone guard is in play."""
    base = "MKWVAHEDEL" * 4
    out = []
    for i in range(n_samples):
        s = list(base)
        s[i] = "G"                       # one substitution each -> pairwise <= 2
        out.append(
            {"seq": "".join(s), "score": 1.0 + i, "recovery": 0.1 + i}
        )
    return out


def test_near_clone_guard_no_longer_false_fails_a_tight_constrained_run():
    """The band MIN_FREE_TO_JUDGE_DIVERSITY alone left exposed. At 10-39 free
    positions the old code turned the near-clone guard back on with its absolute
    Hamming<=2 threshold, which was calibrated for a ~110-position whole-chain
    redesign. A correct 20-position rescue run at T=0.1 lands inside it and was
    hard-failed AFTER the GPU ran, blaming the model for the caller's own
    constraint. Nothing here is a stub: the samples are all distinct."""
    clones = _near_clones()
    assert len({s["seq"] for s in clones}) == len(clones)
    rp.reject_stub(clones, n_free_positions=20)


def test_near_clone_guard_still_fires_once_the_freedom_makes_it_diagnostic():
    """The loosening must be a band, not a hole — at full freedom the guard is
    unchanged, which is the whole point of keeping the absolute threshold."""
    clones = _near_clones()
    with pytest.raises(SystemExit):
        rp.reject_stub(clones, n_free_positions=rp.MIN_FREE_FOR_WHOLE_SEQUENCE_DIVERSITY)
    with pytest.raises(SystemExit):
        rp.reject_stub(clones, n_free_positions=None)


def test_a_real_stub_is_still_caught_inside_the_loosened_band():
    """The guards that do NOT need freedom stay on throughout. An all-identical
    return is the actual silent-stub mode, and it must still fail at 20 free
    positions even though the near-clone guard is skipped there."""
    with pytest.raises(SystemExit):
        rp.reject_stub(_seqs(*["MKWVAHEDEL"] * 8), n_free_positions=20)


@pytest.mark.parametrize("bad", ["not-a-dict", ["a"], 7, True])
def test_main_survives_a_non_dict_parameters_block(tmp_path, monkeypatch, bad):
    """`params.get(...)` on a truthy non-dict raises AttributeError, which is
    neither TypeError nor ValueError — it escapes main()'s catch, kills the run,
    and writes NO result file, so the job reports as an unexplained
    infrastructure failure. The isinstance guard that prevents this is
    load-bearing and was previously pinned only at the normalise() level, one
    frame below where it fires."""
    pdb = tmp_path / "design_001.pdb"
    pdb.write_text(_pdb({"C": 20}))
    out_dir = tmp_path / "mpnn_out"
    _write_fa(out_dir, "design_001", _header(["C"]), NAT20, [DESIGNED20, "CD" + "W" * 18])

    written = {}
    monkeypatch.setattr(rp, "parse_payload", lambda: {
        "tier": "standalone",
        "job_spec": {"target_chain": "C", "parameters": bad},
    })
    monkeypatch.setattr(rp, "preflight", lambda payload: None)
    monkeypatch.setattr(rp, "resolve_input_pdb", lambda payload, workdir: pdb)
    monkeypatch.setattr(rp, "run_mpnn", lambda **kw: out_dir)
    monkeypatch.setattr(rp, "_archive_raw", lambda workdir: None)
    monkeypatch.setattr(rp, "_write_result", lambda p: written.update(p))
    rp.main()                                  # must not raise AttributeError
    assert written["status"] == "COMPLETED"    # falls back to defaults


def test_stub_guard_stays_on_for_a_short_unfrozen_chain(tmp_path, monkeypatch):
    """`n_free_max` must stay None when nothing was frozen. Computing it
    unconditionally would let any designed chain shorter than the threshold
    disable stub rejection for an ordinary redesign."""
    pdb = tmp_path / "design_001.pdb"
    pdb.write_text(_pdb({"C": 6}))             # shorter than the threshold
    out_dir = tmp_path / "mpnn_out"
    _write_fa(out_dir, "design_001", _header(["C"]), "AAAAAA", ["AAAAAA", "AAAAAA"])

    written = {}
    monkeypatch.setattr(rp, "parse_payload", lambda: {
        "tier": "standalone",
        "job_spec": {"target_chain": "C", "parameters": {"num_seq_per_target": 2}},
    })
    monkeypatch.setattr(rp, "preflight", lambda payload: None)
    monkeypatch.setattr(rp, "resolve_input_pdb", lambda payload, workdir: pdb)
    monkeypatch.setattr(rp, "run_mpnn", lambda **kw: out_dir)
    monkeypatch.setattr(rp, "_archive_raw", lambda workdir: None)
    monkeypatch.setattr(rp, "_write_result", lambda p: written.update(p))
    with pytest.raises(SystemExit):
        rp.main()
    assert written["error"]["check"] == "stub"


def test_free_positions_are_measured_over_DESIGNED_chains_only(tmp_path, monkeypatch):
    """An undesigned context chain is not something MPNN was free to vary, so it
    must not raise n_free_max and switch the stub guard back on for a run that is
    legitimately low-diversity."""
    pdb = tmp_path / "design_001.pdb"
    pdb.write_text(_pdb({"A": 60, "C": 20}))   # A is context, only C is designed
    out_dir = tmp_path / "mpnn_out"
    nat = "A" * 20
    _write_fa(out_dir, "design_001", _header(["C"]), nat, ["MM" + nat[2:]] * 8)

    written = {}
    monkeypatch.setattr(rp, "parse_payload", lambda: {
        "tier": "standalone",
        "job_spec": {
            "target_chain": "C",
            "parameters": {"num_seq_per_target": 8,
                           "fixed_positions": {"C": list(range(3, 21))}},
        },
    })
    monkeypatch.setattr(rp, "preflight", lambda payload: None)
    monkeypatch.setattr(rp, "resolve_input_pdb", lambda payload, workdir: pdb)
    monkeypatch.setattr(rp, "run_mpnn", lambda **kw: out_dir)
    monkeypatch.setattr(rp, "_archive_raw", lambda workdir: None)
    monkeypatch.setattr(rp, "_write_result", lambda p: written.update(p))
    rp.main()
    assert written["status"] == "COMPLETED"
    assert written["fixed_positions_check"]["max_free_positions"] == 2


def test_stub_check_skipped_is_False_exactly_at_the_threshold(tmp_path, monkeypatch):
    """Boundary of the machine-readable flag: at exactly the threshold the guard
    RUNS, so the field must not claim it was skipped."""
    n = rp.MIN_FREE_TO_JUDGE_DIVERSITY
    pdb = tmp_path / "design_001.pdb"
    pdb.write_text(_pdb({"C": n + 2}))
    out_dir = tmp_path / "mpnn_out"
    nat = "A" * (n + 2)
    diverse = ["AA" + "M" * n, "AA" + "W" * n]
    _write_fa(out_dir, "design_001", _header(["C"]), nat, diverse)

    written = {}
    monkeypatch.setattr(rp, "parse_payload", lambda: {
        "tier": "standalone",
        "job_spec": {
            "target_chain": "C",
            "parameters": {"num_seq_per_target": 2, "fixed_positions": {"C": [1, 2]}},
        },
    })
    monkeypatch.setattr(rp, "preflight", lambda payload: None)
    monkeypatch.setattr(rp, "resolve_input_pdb", lambda payload, workdir: pdb)
    monkeypatch.setattr(rp, "run_mpnn", lambda **kw: out_dir)
    monkeypatch.setattr(rp, "_archive_raw", lambda workdir: None)
    monkeypatch.setattr(rp, "_write_result", lambda p: written.update(p))
    rp.main()
    assert written["fixed_positions_check"]["max_free_positions"] == n
    assert written["fixed_positions_check"]["stub_check_skipped"] is False


def test_main_STILL_stub_rejects_an_unconstrained_redesign(tmp_path, monkeypatch):
    """The pre-existing contract, pinned end to end. Passing n_free_positions=0
    instead of None would disable stub rejection for every ordinary redesign,
    and nothing else in this file would notice."""
    written, _ = _run_main(tmp_path, monkeypatch, [NAT20, NAT20], fixed=None)
    with pytest.raises(SystemExit):
        rp.main()
    assert written["status"] == "FAILED"
    assert written["error"]["check"] == "stub"


def _run_two_chain_main(tmp_path, monkeypatch, c_len, d_len, frozen, samples):
    """main() over two designed chains C and D with independent freedom."""
    pdb = tmp_path / "design_001.pdb"
    pdb.write_text(_pdb({"C": c_len, "D": d_len}))
    out_dir = tmp_path / "mpnn_out"
    native = "A" * c_len + "/" + "A" * d_len
    _write_fa(out_dir, "design_001", _header(["C", "D"], fixed=[]), native, samples)

    written = {}
    monkeypatch.setattr(rp, "parse_payload", lambda: {
        "tier": "standalone",
        "job_spec": {
            "target_chain": "C D",
            "parameters": {"num_seq_per_target": len(samples),
                           "fixed_positions": frozen},
        },
    })
    monkeypatch.setattr(rp, "preflight", lambda payload: None)
    monkeypatch.setattr(rp, "resolve_input_pdb", lambda payload, workdir: pdb)
    monkeypatch.setattr(rp, "run_mpnn", lambda **kw: out_dir)
    monkeypatch.setattr(rp, "_archive_raw", lambda workdir: None)
    monkeypatch.setattr(rp, "_write_result", lambda p: written.update(p))
    return written


def test_two_equally_frozen_chains_are_not_stub_rejected(tmp_path, monkeypatch):
    """Both chains frozen to 2 free positions each. Identical samples are the
    expected output, exactly as for one such chain — summing across chains would
    report 4 (or, on a longer pair, clear the threshold) and re-fail it."""
    frozen = list(range(3, 21))
    same = "MM" + "A" * 18
    written = _run_two_chain_main(
        tmp_path, monkeypatch, 20, 20,
        {"C": frozen, "D": frozen}, [same + "/" + same] * 8,
    )
    rp.main()
    assert written["status"] == "COMPLETED"
    check = written["fixed_positions_check"]
    assert check["free_positions"] == {"C": 2, "D": 2}
    assert check["max_free_positions"] == 2
    assert check["stub_check_skipped"] is True


def test_a_free_chain_keeps_the_stub_guard_ON_for_the_whole_run(tmp_path, monkeypatch):
    """The aggregation must be the MAXIMUM. Chain C is frozen to 2 free
    positions; chain D is entirely free at 60. Taking the minimum switches stub
    rejection off for the whole run, so a textbook silent stub on D — 8
    byte-identical samples — would ship as COMPLETED and be billed. Freeze an
    interface on one chain and co-design a partner freely: that is the normal
    shape of this feature, not a corner case."""
    written = _run_two_chain_main(
        tmp_path, monkeypatch, 20, 60,
        {"C": list(range(3, 21))}, ["A" * 20 + "/" + "A" * 60] * 8,
    )
    with pytest.raises(SystemExit):
        rp.main()
    assert written["status"] == "FAILED"
    assert written["error"]["check"] == "stub"


def test_echo_judged_is_False_when_any_chain_went_unjudged():
    """A busy chain must not certify a silent one. Reporting True here would let
    a consumer read a chain that came back a verbatim native echo as verified."""
    n = rp.MIN_FREE_TO_JUDGE_DIVERSITY
    big, small = "A" * (n * 2), "A" * 4
    out = rp.verify_fixed_positions(
        _seqs("M" * (n * 2) + "/" + small),      # chain C rewritten, D untouched
        (_header(["C", "D"], fixed=[]), big + "/" + small),
        {"D": [1]},
        {"C": n * 2, "D": 4},
    )
    assert out["echo_judged"] is False
    assert out["echo_unjudged_chains"] == ["D"]


def test_main_does_not_reject_a_legitimately_low_diversity_run(tmp_path, monkeypatch):
    """End to end: the mitigation has to sit in reject_stub, which runs FIRST.
    Putting it only in verify_fixed_positions leaves it unreachable."""
    # Positions 3-20 frozen, 1-2 free, and both samples come back identical --
    # the correct output of a heavily constrained run, and formerly a hard FAILED.
    written, _ = _run_main(
        tmp_path,
        monkeypatch,
        ["MM" + NAT20[2:]] * 2,
        fixed={"C": list(range(3, 21))},
    )
    rp.main()
    assert written["status"] == "COMPLETED"
    assert written["fixed_positions_check"]["echo_judged"] is False


# --------------------------------------------------------------------------
# main() wiring -- the check must actually be plugged in
# --------------------------------------------------------------------------
def _run_main(
    tmp_path,
    monkeypatch,
    samples,
    native=NAT20,
    designed=("C",),
    tier="standalone",
    fixed={"C": [1, 2]},
):
    """Drive main() with MPNN stubbed out.

    Returns (result_payload, run_mpnn_kwargs), both filled in as main() runs. The
    kwargs matter: stubbing run_mpnn without asserting what it received let
    `fixed_positions=None` and a hardcoded chain string reach it undetected.
    """
    pdb = tmp_path / "design_001.pdb"
    pdb.write_text(_pdb({"A": 8, "B": 6, "C": 20}))
    out_dir = tmp_path / "mpnn_out"
    _write_fa(out_dir, "design_001", _header(list(designed)), native, samples)

    written, kwargs = {}, {}

    def fake_run_mpnn(**kw):
        kwargs.update(kw)
        return out_dir

    monkeypatch.setattr(rp, "parse_payload", lambda: {
        "tier": tier,
        "job_spec": {
            "target_chain": "C",
            "parameters": {"num_seq_per_target": 1, "fixed_positions": fixed},
        },
    })
    monkeypatch.setattr(rp, "preflight", lambda payload: None)
    monkeypatch.setattr(rp, "resolve_input_pdb", lambda payload, workdir: pdb)
    monkeypatch.setattr(rp, "run_mpnn", fake_run_mpnn)
    monkeypatch.setattr(rp, "_archive_raw", lambda workdir: None)
    monkeypatch.setattr(rp, "_write_result", lambda payload: written.update(payload))
    return written, kwargs


def test_main_wiring_completes_on_a_real_design(tmp_path, monkeypatch):
    written, kwargs = _run_main(tmp_path, monkeypatch, [DESIGNED20])
    rp.main()
    assert written["status"] == "COMPLETED"
    check = written["fixed_positions_check"]
    assert check["checked"] is True
    assert check["n_assertions"] == 2


def test_main_forwards_the_normalised_request_to_mpnn(tmp_path, monkeypatch):
    """A stubbed run_mpnn that is never asserted on hides the two ways this can
    be unplugged: dropping fixed_positions, and normalising against a chain
    string other than the caller's."""
    _, kwargs = _run_main(tmp_path, monkeypatch, [DESIGNED20])
    rp.main()
    assert kwargs["fixed_positions"] == {"C": [1, 2]}
    assert kwargs["chains_to_design"] == "C"


def test_main_passes_real_chain_counts_to_the_check(tmp_path, monkeypatch):
    """chain_counts={} would silently disable the parser-disagreement
    cross-check, since it is guarded by `chain in chain_counts`.

    Native and sample are the SAME length here on purpose: the segmentation
    guard then has nothing to catch, so only the cross-check can fail this run
    and the test genuinely pins it."""
    written, _ = _run_main(
        tmp_path, monkeypatch, ["CD" + "M" * 5], native="CD" + "A" * 5
    )
    with pytest.raises(SystemExit):          # 7 emitted vs 20 counted
        rp.main()
    assert written["status"] == "FAILED"


def test_main_ACTUALLY_CALLS_the_check(tmp_path, monkeypatch):
    """Regression guard for the whole safety net: replacing the
    verify_fixed_positions() call in main() with a canned {"checked": True} left
    every other test in this file green. A native echo at n=1 clears preflight,
    clears reject_stub, and must still fail the run."""
    written, _ = _run_main(tmp_path, monkeypatch, [NAT20])
    with pytest.raises(SystemExit):
        rp.main()
    assert written["status"] == "FAILED"


def test_main_fails_when_mpnn_designed_a_different_chain(tmp_path, monkeypatch):
    """The freeze was requested on C; MPNN reports it designed A."""
    written, _ = _run_main(
        tmp_path, monkeypatch, ["M" + "A" * 7], native="A" * 8, designed=("A",)
    )
    with pytest.raises(SystemExit):
        rp.main()
    assert written["status"] == "FAILED"


def test_main_says_smoke_tier_DISCARDED_the_request(tmp_path, monkeypatch):
    """Smoke forces its own preset and drops the caller's fixed_positions. The
    result must not report that as "nothing was requested" — a consumer cannot
    tell those apart, and one of them means the freeze never happened."""
    written, kwargs = _run_main(
        tmp_path, monkeypatch, [DESIGNED20, "CD" + "W" * 18], tier="smoke"
    )
    rp.main()
    assert written["status"] == "COMPLETED"
    assert kwargs["fixed_positions"] == {}
    assert written["fixed_positions_check"] == {
        "checked": False,
        "reason": "smoke tier ignores caller fixed_positions",
    }
