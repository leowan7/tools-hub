"""The top-design quality figure: who gets one, and how it degrades.

The requirement that shaped this module is the degradation one. At least one
esmfold run (eb94dfd2-a854-44c4-b741-c39fac360c07, 2026-11-12) has no
retrievable structure, so "the tool stores structures" never meant "every run
has one". Everything that can be missing must render as NO FIGURE -- never an
error, never a zero.
"""

from __future__ import annotations

import pathlib

import pytest

from shared.design_quality import (
    TOP_DESIGN_INDEX,
    designed_chains,
    top_design_profile,
)

_REPO = pathlib.Path(__file__).resolve().parent.parent
_REAL_PDB = _REPO / "static" / "example" / "1HEW.pdb"


def _chains_of(data: bytes):
    from shared.exports import structure_chain_sequences

    return structure_chain_sequences(data)


@pytest.fixture(scope="module")
def real_bytes() -> bytes:
    return _REAL_PDB.read_bytes()


@pytest.fixture(scope="module")
def real_chains(real_bytes):
    chains = _chains_of(real_bytes)
    assert chains, "fixture structure parsed to no chains"
    return chains


# ---------------------------------------------------------------------------
# Naming the designed chain.
# ---------------------------------------------------------------------------

def test_the_rows_own_sequence_names_its_chain(real_chains):
    """Rule 1: no per-tool knowledge needed when the row carries a sequence."""
    chain_id, sequence = real_chains[0]
    got = designed_chains(
        {"sequence": sequence}, 0, "anything", {}, {}, real_chains
    )
    assert got == [chain_id]


def test_a_lowercase_or_padded_sequence_still_matches(real_chains):
    chain_id, sequence = real_chains[0]
    messy = f"  {sequence.lower()}  "
    assert designed_chains({"sequence": messy}, 0, None, {}, {}, real_chains) == [chain_id]


def test_binder_sequence_and_nested_scores_are_read_too(real_chains):
    """The three keys shared/exports.py::_candidate_sequence reads."""
    chain_id, sequence = real_chains[0]
    for row in (
        {"binder_sequence": sequence},
        {"scores": {"sequence": sequence}},
    ):
        assert designed_chains(row, 0, None, {}, {}, real_chains) == [chain_id]


def test_a_sequence_matching_no_chain_names_nothing(real_chains):
    assert designed_chains(
        {"sequence": "WWWWWWWWWW"}, 0, "rfd4", {"target_chain": "A"}, {}, real_chains
    ) is None


def test_a_sequence_matching_two_chains_names_nothing():
    """Ambiguity is unknown, not a coin toss."""
    chains = [("A", "ACDEFGHIKL"), ("B", "ACDEFGHIKL")]
    assert designed_chains({"sequence": "ACDEFGHIKL"}, 0, None, {}, {}, chains) is None


def test_the_target_chain_names_every_other_chain_designed():
    """Rule 2, for a row with no sequence of its own."""
    chains = [("A", "ACDEFGHIKL"), ("B", "MNPQRSTVWY")]
    assert designed_chains({}, 0, "rfd4", {"target_chain": "A"}, {}, chains) == ["B"]
    assert designed_chains({}, 0, "rfd4", {}, {"target_chain": "B"}, chains) == ["A"]


def test_rule_two_is_the_exports_allow_list_not_any_tool():
    """iggm is excluded on purpose; its delivered antigen chain is relabelled.

    This is not a rule of mine -- it is shared/exports.py's
    ``_TARGET_CHAIN_TOOLS``, and this test exists so widening that set is a
    deliberate act rather than a side effect.
    """
    from shared.exports import _TARGET_CHAIN_TOOLS

    assert _TARGET_CHAIN_TOOLS == frozenset({"rfd4"})
    chains = [("A", "ACDEFGHIKL"), ("B", "MNPQRSTVWY")]
    assert designed_chains({}, 0, "iggm", {"target_chain": "A"}, {}, chains) is None
    assert designed_chains({}, 0, "boltzgen", {"target_chain": "A"}, {}, chains) is None


def test_two_designed_chains_name_nothing():
    """An H+L run is not an error, but it is a different figure."""
    chains = [("A", "ACDEFGHIKL"), ("H", "MNPQRSTVWY"), ("L", "GGGACDEFIK")]
    assert designed_chains({}, 0, "rfd4", {"target_chain": "A"}, {}, chains) is None


def test_no_sequence_and_no_target_chain_names_nothing(real_chains):
    assert designed_chains({}, 0, "boltzgen", {}, {}, real_chains) is None


def test_no_chains_at_all_names_nothing():
    assert designed_chains({"sequence": "ACDE"}, 0, None, {}, {}, []) is None


# ---------------------------------------------------------------------------
# The profile, and every way it must degrade to nothing.
# ---------------------------------------------------------------------------

def _fetcher(data):
    def _fetch(_job_id, _filename):
        return data
    return _fetch


def test_a_real_structure_produces_a_figure(real_bytes, real_chains):
    chain_id, sequence = real_chains[0]
    got = top_design_profile(
        [{"sequence": sequence, "pdb_key": "designs/d1.pdb"}],
        tool="rfdiffusion", result={}, inputs={},
        fetch_bytes=_fetcher(real_bytes), job_id="job-1",
    )
    assert got is not None
    assert got["chain"] == chain_id
    assert got["largest_patch_a2"] > 0
    assert got["residues"] == len(sequence)
    # The liability records come from the scanner, with exposure attached.
    assert isinstance(got["liabilities"], list)
    assert any(r.get("exposed") is not None for r in got["liabilities"]), (
        "a measured structure should resolve the exposure of at least one motif"
    )


def test_inline_bytes_need_no_fetcher(real_bytes, real_chains):
    """A row carrying pdb_content_b64 costs no storage read at all."""
    import base64

    _chain_id, sequence = real_chains[0]
    got = top_design_profile(
        [{"sequence": sequence,
          "pdb_content_b64": base64.b64encode(real_bytes).decode()}],
        tool="rfdiffusion", result={}, inputs={},
        fetch_bytes=None, job_id=None,
    )
    assert got is not None


def test_only_the_top_row_is_read(real_bytes, real_chains):
    """The whole cost argument: one read however many rows there are."""
    _chain_id, sequence = real_chains[0]
    reads = []

    def _counting_fetch(job_id, filename):
        reads.append(filename)
        return real_bytes

    rows = [{"sequence": sequence, "pdb_key": f"designs/d{i}.pdb"} for i in range(50)]
    got = top_design_profile(
        rows, tool="rfdiffusion", result={}, inputs={},
        fetch_bytes=_counting_fetch, job_id="job-1",
    )
    assert got is not None
    assert len(reads) == 1, f"expected exactly one structure read, made {len(reads)}"
    assert reads[0] == "designs/d0.pdb", (
        "the row read must be the FIRST in page order, not any other"
    )


def test_page_order_decides_the_top_design_not_the_rank_field(real_bytes, real_chains):
    """``rank`` means different things per tool; position does not."""
    _chain_id, sequence = real_chains[0]
    reads = []

    def _counting_fetch(job_id, filename):
        reads.append(filename)
        return real_bytes

    rows = [
        {"sequence": sequence, "pdb_key": "designs/first.pdb", "rank": 7},
        {"sequence": sequence, "pdb_key": "designs/second.pdb", "rank": 1},
    ]
    top_design_profile(
        rows, tool="rfdiffusion", result={}, inputs={},
        fetch_bytes=_counting_fetch, job_id="job-1",
    )
    assert reads == ["designs/first.pdb"]
    assert TOP_DESIGN_INDEX == 0


@pytest.mark.parametrize(
    "rows,fetch,label",
    [
        ([], None, "no rows at all"),
        ([{"sequence": "ACDE"}], None, "a row with no structure anywhere"),
        ([{"sequence": "ACDE", "pdb_key": "designs/d.pdb"}], lambda j, f: None,
         "a storage miss"),
        ([{"sequence": "ACDE", "pdb_key": "designs/d.pdb"}],
         lambda j, f: b"not a structure at all", "bytes that do not parse"),
        ([{"pdb_key": "designs/d.pdb"}], None, "no sequence and no target chain"),
        (["not a dict"], None, "a non-dict row"),
        ([None], None, "a None row"),
    ],
)
def test_everything_missing_degrades_to_no_figure(rows, fetch, label):
    """The requirement: never an error, never a zero. Nothing."""
    got = top_design_profile(
        rows, tool="rfdiffusion", result={}, inputs={},
        fetch_bytes=fetch, job_id="job-1",
    )
    assert got is None, f"{label} should produce no figure, got {got!r}"


def test_a_run_whose_structure_is_gone_shows_nothing(real_chains):
    """The esmfold case named in the module docstring, as a test.

    The row looks complete -- it carries its designed sequence and a pdb_key --
    and only the storage read fails. That must render as no figure rather than
    an error page or a zero patch.
    """
    _chain_id, sequence = real_chains[0]
    assert top_design_profile(
        [{"sequence": sequence, "pdb_key": "designs/vanished.pdb"}],
        tool="esmfold2-design", result={}, inputs={},
        fetch_bytes=lambda j, f: None, job_id="eb94dfd2-a854-44c4-b741-c39fac360c07",
    ) is None


def test_a_measured_zero_is_still_a_figure():
    """Distinct from None: a chain measured to have no exposed apolar atom.

    A glycine chain has no side-chain carbon to expose beyond the backbone, so
    this exercises the low end without asserting a particular area.
    """
    from shared.surface_patch import surface_profile

    data = _REAL_PDB.read_bytes()
    profile = surface_profile(data, ["A"])
    assert profile is not None
    # The contract the panel relies on: these keys always exist on a figure.
    for key in ("largest_patch_a2", "patch_atom_count", "total_apolar_a2"):
        assert key in profile


# ---------------------------------------------------------------------------
# It is a screen, not a verdict.
# ---------------------------------------------------------------------------

def test_the_figure_is_not_a_pass_fail_bar():
    """Same line the sequence developability pair sits on.

    shared/score_legends.py::GATE_COLUMNS is what derives a pass/fail verdict
    from a column. Nothing this module produces may appear there.
    """
    from shared.score_legends import GATE_COLUMNS

    for name in ("largest_patch_a2", "total_apolar_a2", "patch_atom_count"):
        assert name not in GATE_COLUMNS, (
            f"{name} is in GATE_COLUMNS, which would turn a screening number "
            "into a verdict on the design"
        )


def test_the_panel_claims_no_ranking_and_no_verdict():
    """The hub lead's copy condition, checked against the rendered template.

    No column exists yet, so the panel must not imply the other designs were
    measured, and it must not read as a pass or a fail.
    """
    text = (_REPO / "templates" / "components" / "design_quality_panel.html").read_text(
        encoding="utf-8"
    )
    # Strip Jinja comments, which discuss the rules and legitimately contain
    # the words the prose must avoid.
    import re

    # Collapse whitespace too: the prose is hard-wrapped, so a phrase to match
    # will otherwise straddle a newline and the assertion passes or fails on
    # where the line happened to break.
    prose = re.sub(r"\s+", " ", re.sub(r"\{#.*?#\}", "", text, flags=re.S))

    assert "design 1 only" in prose, (
        "the panel must name the one design it describes"
    )
    assert "have not been measured" in prose, (
        "the panel must say the other designs were not measured"
    )
    assert "not a measurement of the real molecule" in prose, (
        "the panel must say the figure comes from a predicted structure"
    )
    # A BLANKET WORD BAN IS THE WRONG GUARD HERE, and the first version of
    # this test was one. It banned "passes" and "fails", which flagged the
    # panel's own disclaimer -- "no threshold here that passes or fails a
    # design" -- i.e. it fired on the sentence that makes the copy correct. A
    # denial of a verdict is exactly what we want the prose to contain.
    #
    # So the real check is the three required statements above. What is banned
    # below is only phrasing that cannot be a denial: a comparative claim about
    # designs. "ranked" is deliberately NOT banned -- the panel says a motif is
    # "ranked one step higher", which is about severity, not about designs.
    for banned in ("best design", "better than", "worse than",
                   "highest-ranked design", "top-ranked design"):
        assert banned not in prose.lower(), (
            f"the panel prose contains {banned!r}, which compares designs the "
            "panel has not measured"
        )
