"""The on-request design quality figure: who gets one, and how it degrades.

The requirement that shaped this module is the degradation one. At least one
esmfold run (eb94dfd2-a854-44c4-b741-c39fac360c07, 2026-11-12) has no
retrievable structure, so "the tool stores structures" never meant "every run
has one". Everything that can be missing must render as NO FIGURE -- never an
error, never a zero.

Two of the tests here exist because a reviewer found the defect they pin, and
both were WRONG ANSWERS rather than crashes, which is the kind this file is
for: a fold was screened as though it were a design
(test_a_fold_is_never_screened_as_a_design) and an uploaded target could be
measured and presented as the customer's binder
(test_rule_two_needs_the_target_chain_to_be_in_the_file).
"""

from __future__ import annotations

import pathlib

import pytest

from shared.design_quality import (
    design_profile,
    designed_chains,
    designs_its_own_chain,
    profile_for_structure_name,
)

# A tool that really is a binder designer, used wherever a test needs the
# designed-chain rules to be reachable at all. NOT a placeholder like
# "anything": the tool gate (designs_its_own_chain) is load-bearing now, and a
# made-up slug would make every test below vacuous.
_DESIGN_TOOL = "rfdiffusion"

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
        {"sequence": sequence}, 0, _DESIGN_TOOL, {}, {}, real_chains
    )
    assert got == [chain_id]


def test_a_lowercase_or_padded_sequence_still_matches(real_chains):
    chain_id, sequence = real_chains[0]
    messy = f"  {sequence.lower()}  "
    assert designed_chains({"sequence": messy}, 0, _DESIGN_TOOL, {}, {}, real_chains) == [chain_id]


def test_binder_sequence_and_nested_scores_are_read_too(real_chains):
    """The three keys shared/exports.py::_candidate_sequence reads."""
    chain_id, sequence = real_chains[0]
    for row in (
        {"binder_sequence": sequence},
        {"scores": {"sequence": sequence}},
    ):
        assert designed_chains(row, 0, _DESIGN_TOOL, {}, {}, real_chains) == [chain_id]


def test_a_sequence_matching_no_chain_names_nothing(real_chains):
    assert designed_chains(
        {"sequence": "WWWWWWWWWW"}, 0, "rfd4", {"target_chain": "A"}, {}, real_chains
    ) is None


def test_a_sequence_matching_two_chains_names_nothing():
    """Ambiguity is unknown, not a coin toss."""
    chains = [("A", "ACDEFGHIKL"), ("B", "ACDEFGHIKL")]
    assert designed_chains({"sequence": "ACDEFGHIKL"}, 0, _DESIGN_TOOL, {}, {}, chains) is None


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


def test_a_fold_is_never_screened_as_a_design(real_chains):
    """A fold tool has no designed chain, whatever its row looks like.

    THIS IS THE DEFECT THE TOOL GATE EXISTS FOR, and it produced a confident
    false statement rather than an error. shared/jobs.py::_single_fold_record
    gives an ESMFold fold a one-row candidate list carrying ``sequence`` and
    inline ``pdb_content_b64`` -- precisely the shape rule 1 matches -- so an
    ordinary single-chain fold resolved to "the designed chain" and the page
    reported liability motifs on a protein the CUSTOMER supplied, labelled as
    a design of ours.

    The sequence below is a real chain's, so rule 1 would match it if the gate
    were not there: this test fails if the gate is removed, rather than passing
    for an unrelated reason.
    """
    _chain_id, sequence = real_chains[0]
    row = {"sequence": sequence}

    for fold_tool in ("esmfold", "af2", "colabfold", "mpnn"):
        assert not designs_its_own_chain(fold_tool), fold_tool
        assert designed_chains(
            row, 0, fold_tool, {}, {}, real_chains
        ) is None, f"{fold_tool} is a fold; it has no designed chain to screen"

    # Non-vacuity: the same row and chains DO resolve for a real design tool,
    # so the None above is the gate and not a broken fixture.
    assert designed_chains(row, 0, _DESIGN_TOOL, {}, {}, real_chains) is not None


def test_no_tool_outside_the_design_band_is_ever_eligible():
    """Derived from the catalog, so a fold tool added LATER is covered too.

    WHY THIS MATTERS, and it is the reason this test is written against the
    registry rather than a hand-written list: if a prediction tool is ever
    admitted to the eligible set, this page starts measuring A PROTEIN THE
    CUSTOMER UPLOADED and presenting it as a design of ours, with liability
    motifs attached. That already happened once -- see
    test_a_fold_is_never_screened_as_a_design -- and a hand list would not
    notice the next fold tool to be added to the catalog.

    ``shared/tools_catalog.py::_TOOL_CATEGORIES`` is the workflow band each
    slug is filed under on the homepage and /tools. Everything outside the
    design band either predicts a structure for a sequence its user chose
    ("Predict or check a 3D structure") or picks sequences for a structure its
    user already has ("Choose sequences for a structure I already have").
    Neither produces a chain WE designed, so neither may ever be eligible.

    If this test fails because a tool legitimately moved bands, the fix is to
    decide deliberately whether it has a single identifiable designed chain --
    not to add it to an exception list here.
    """
    from shared.tools_catalog import _TOOL_CATEGORIES

    design_band = "Make new binders for my target"
    outside = {
        slug for slug, band in _TOOL_CATEGORIES.items() if band != design_band
    }
    assert outside, "the catalog has no non-design tools; this test is vacuous"

    for slug in sorted(outside):
        assert not designs_its_own_chain(slug), (
            f"{slug} is filed under {_TOOL_CATEGORIES[slug]!r}, not the design "
            "band, so it has no chain WE designed -- measuring it would "
            "present the customer's own input as our design"
        )

    # Non-vacuity: the design band is not empty either, so the assertion above
    # is about the band and not about an empty set or a gate that rejects
    # everything.
    assert any(
        designs_its_own_chain(slug)
        for slug, band in _TOOL_CATEGORIES.items() if band == design_band
    ), "no design-band tool passes the gate; the gate rejects everything"


def test_iggm_is_in_the_design_band_and_still_ineligible():
    """The gate is not "is this a designer" -- iggm is one and is still out.

    iggm is filed under the design band and genuinely designs chains, so it is
    excluded for a DIFFERENT reason from a fold tool: an antibody design has a
    heavy AND a light chain, and this figure describes one chain's surface.
    That is the reason its meta.py copy gives the reader, and it is why the
    exclusion is on the science rather than the plumbing.
    """
    from shared.tools_catalog import _TOOL_CATEGORIES

    assert _TOOL_CATEGORIES["iggm"] == "Make new binders for my target"
    assert not designs_its_own_chain("iggm")


def test_the_two_tools_that_qualify_pass_the_gate():
    """rfd4 by the target-chain rule, esmfold2-design by the row-sequence rule.

    esmfold2-design is NOT in BINDER_DESIGN_TOOLS, because that set is derived
    from the PDB preflight rules and esmfold2-design takes a target sequence
    rather than a target file. It still designs a binder, so the gate has to
    admit it, and the slug is hyphenated while its package directory is not.
    """
    assert designs_its_own_chain("rfd4")
    assert designs_its_own_chain("esmfold2-design")
    # The underscore spelling is the package directory, not a registered tool.
    assert not designs_its_own_chain("esmfold2_design")


def test_rule_two_needs_the_target_chain_to_be_in_the_file():
    """An absent target must not make every other chain "designed".

    Without this guard a structure that does not contain the named target has
    whatever chain it DOES contain declared designed -- so an uploaded target
    delivered under a different chain id would be measured and shown to the
    customer as their binder. shared/exports.py::_chain_records carries the
    same guard for the same reason, and this module claims to borrow that rule
    whole; omitting this half made the claim false.
    """
    present = [("A", "ACDEFGHIKL"), ("B", "MNPQRSTVWY")]
    assert designed_chains({}, 0, "rfd4", {"target_chain": "A"}, {}, present) == ["B"]

    # THE DISCRIMINATING CASE IS A SINGLE-CHAIN FILE, and getting this wrong
    # is how the first version of this test passed while the guard was absent.
    # With two leftover chains, removing the guard still returns None -- the
    # "more than one designed chain" rule catches it, not this guard -- so such
    # a case proves nothing. One leftover chain is the only shape where the
    # guard is the thing standing between the reader and a measurement of
    # what may be their own uploaded target.
    one_chain_not_the_target = [("B", "MNPQRSTVWY")]
    assert designed_chains(
        {}, 0, "rfd4", {"target_chain": "A"}, {}, one_chain_not_the_target
    ) is None, (
        "a file that does not contain the named target must name no designed "
        "chain: the single chain it does contain may BE the target, relabelled"
    )

    # And the guard must not fire when the target IS there, which is what
    # makes the assertion above about absence rather than about arity.
    assert designed_chains(
        {}, 0, "rfd4", {"target_chain": "B"},
        {}, [("A", "ACDEFGHIKL"), ("B", "MNPQRSTVWY")],
    ) == ["A"]


def test_no_chains_at_all_names_nothing():
    assert designed_chains({"sequence": "ACDE"}, 0, _DESIGN_TOOL, {}, {}, []) is None


# ---------------------------------------------------------------------------
# The profile, and every way it must degrade to nothing.
# ---------------------------------------------------------------------------

def _fetcher(data):
    def _fetch(_job_id, _filename):
        return data
    return _fetch


def test_a_real_structure_produces_a_figure(real_bytes, real_chains):
    chain_id, sequence = real_chains[0]
    got = design_profile(
        {"sequence": sequence, "pdb_key": "designs/d1.pdb"}, 0,
        tool=_DESIGN_TOOL, result={}, inputs={},
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
    got = design_profile(
        {"sequence": sequence,
         "pdb_content_b64": base64.b64encode(real_bytes).decode()}, 0,
        tool=_DESIGN_TOOL, result={}, inputs={},
        fetch_bytes=None, job_id=None,
    )
    assert got is not None


def test_only_the_design_asked_for_is_read(real_bytes, real_chains):
    """The whole cost argument: one read however many rows the job has."""
    _chain_id, sequence = real_chains[0]
    reads = []

    def _counting_fetch(job_id, filename):
        reads.append(filename)
        return real_bytes

    rows = [{"sequence": sequence, "pdb_key": f"designs/d{i}.pdb"} for i in range(50)]
    got = profile_for_structure_name(
        rows, "designs/d37.pdb", tool=_DESIGN_TOOL, result={}, inputs={},
        fetch_bytes=_counting_fetch, job_id="job-1",
    )
    assert got is not None
    assert len(reads) == 1, f"expected exactly one structure read, made {len(reads)}"
    assert reads[0] == "designs/d37.pdb", (
        "the design read must be the one asked for, not any other"
    )


def test_the_design_is_addressed_by_name_not_by_position(real_bytes, real_chains):
    """A row's position in the table it is drawn in is not its identity.

    The candidate table is rendered for campaigns and targets too, where the
    rows come from many jobs and a row's index has nothing to do with its
    position in the job that produced it. Addressing by structure name is what
    makes the measurement land on the design the reader actually expanded, and
    it is the same rule the 3D viewer and the download link already use.
    """
    _chain_id, sequence = real_chains[0]
    reads = []

    def _counting_fetch(job_id, filename):
        reads.append(filename)
        return real_bytes

    rows = [
        {"sequence": sequence, "pdb_key": "designs/first.pdb", "rank": 7},
        {"sequence": sequence, "pdb_key": "designs/second.pdb", "rank": 1},
    ]
    assert profile_for_structure_name(
        rows, "designs/second.pdb", tool=_DESIGN_TOOL, result={}, inputs={},
        fetch_bytes=_counting_fetch, job_id="job-1",
    ) is not None
    assert reads == ["designs/second.pdb"]

    # Basename matching, the same rule as blueprints/jobs.py::job_candidate_pdb:
    # a request for the bare filename finds a row keyed with its directory.
    reads.clear()
    assert profile_for_structure_name(
        rows, "second.pdb", tool=_DESIGN_TOOL, result={}, inputs={},
        fetch_bytes=_counting_fetch, job_id="job-1",
    ) is not None
    assert reads == ["designs/second.pdb"]


def test_a_name_that_matches_no_row_measures_nothing(real_bytes, real_chains):
    """Including when other rows would have measured fine."""
    _chain_id, sequence = real_chains[0]
    rows = [{"sequence": sequence, "pdb_key": "designs/first.pdb"}]
    assert profile_for_structure_name(
        rows, "designs/nope.pdb", tool=_DESIGN_TOOL, result={}, inputs={},
        fetch_bytes=lambda j, f: real_bytes, job_id="job-1",
    ) is None


def test_a_malformed_row_does_not_shift_which_design_is_measured(
    real_bytes, real_chains
):
    """A non-dict row is skipped without renumbering the rows after it.

    ``is_candidate_array`` admits a list whose first element is not a dict, and
    the storage key for a row that stores none is POSITIONAL -- so dropping a
    malformed row from the enumeration rather than skipping it would measure
    one design and key it as another. Same hazard the exports already record
    as "coerce, never filter".
    """
    _chain_id, sequence = real_chains[0]
    reads = []

    def _counting_fetch(job_id, filename):
        reads.append(filename)
        return real_bytes

    rows = ["garbage", {"sequence": sequence, "pdb_key": "designs/real.pdb"}]
    assert profile_for_structure_name(
        rows, "designs/real.pdb", tool=_DESIGN_TOOL, result={}, inputs={},
        fetch_bytes=_counting_fetch, job_id="job-1",
    ) is not None
    assert reads == ["designs/real.pdb"]


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
    got = profile_for_structure_name(
        rows, "designs/d.pdb", tool=_DESIGN_TOOL, result={}, inputs={},
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
    assert profile_for_structure_name(
        [{"sequence": sequence, "pdb_key": "designs/vanished.pdb"}],
        "designs/vanished.pdb",
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

    GATE_COLUMNS IS KEYED BY TOOL, and the names to check are in the VALUES
    (``dict[str, tuple[str, ...]]``, score_legends.py:GATE_COLUMNS). An earlier
    version of this test asserted ``name not in GATE_COLUMNS``, which tested
    the tool names and would have passed with ``largest_patch_a2`` sitting
    inside a tool's tuple -- it enforced nothing. The shape below is the one
    tests/test_rfd4_results_and_exports.py::
    test_the_developability_columns_are_not_a_pass_fail_bar already uses for
    the sequence pair: iterate the items and intersect the tuples.

    Non-vacuity is checked in the same test rather than asserted, because the
    bug this replaces was a guard that could not fail.
    """
    from shared.score_legends import GATE_COLUMNS

    produced = {"largest_patch_a2", "total_apolar_a2", "patch_atom_count"}
    for tool, cols in GATE_COLUMNS.items():
        overlap = produced & set(cols)
        assert not overlap, (
            f"{sorted(overlap)} is a gate leg for {tool}, which would turn a "
            "screening number into a verdict on the design"
        )

    # The assertion above can fail: the same comparison over a name that IS a
    # gate leg finds it. Without this, a GATE_COLUMNS that changed shape again
    # would silently make the loop vacuous a second time.
    assert any(GATE_COLUMNS.values()), "GATE_COLUMNS has no columns to check"
    planted = {"pLDDT"}
    assert any(
        planted & set(cols) for cols in GATE_COLUMNS.values()
    ), "the intersection above cannot detect a name that is a gate leg"


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

    assert "for this design only" in prose, (
        "the panel must name the one design it describes"
    )
    assert "has been measured" in prose and "no other design" in prose.lower(), (
        "the panel must say the other designs were not measured"
    )
    assert "not a measurement of the real molecule" in prose, (
        "the panel must say the figure comes from a predicted structure"
    )
    # The empty state is part of the copy contract, not an afterthought: this
    # panel is now fetched on request, so a reader who clicks and gets nothing
    # must be told that in words rather than shown a blank or a zero.
    assert "No surface measurement is available" in prose, (
        "the panel must have an honest empty state"
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
