"""Liability severity comes from measured exposure, not a guessed region.

The hub lead's ruling of 2026-11-16 replaced a CDR-span escalation -- where
the spans were guessed from sequence LENGTH -- with escalation on measured
solvent accessibility. These tests pin the three states that rule has to keep
apart (exposed / buried / unknown) and the absence of the old one.
"""

from __future__ import annotations

import inspect

import pytest

from shared.developability.dimensions import liabilities as lia
from shared.developability.dimensions.liabilities import (
    find_liabilities,
    score_liabilities,
)
from shared.surface_patch import EXPOSED_RESIDUE_RSA

# One oxidation site (the M at position 1) and one NG deamidation site at 2.
SEQ = "MNGSTKACDE"
_M_POSITION = 1


def _only(records, kind, position=None):
    hits = [
        r for r in records
        if r["type"] == kind and (position is None or r["position"] == position)
    ]
    assert len(hits) == 1, f"expected exactly one {kind} record, got {hits}"
    return hits[0]


def _rsa(exposed_positions, length=len(SEQ), value=None):
    """RSA list where the named 1-based positions are exposed, others buried."""
    hi = value if value is not None else EXPOSED_RESIDUE_RSA + 0.2
    lo = EXPOSED_RESIDUE_RSA - 0.2
    return [hi if (i + 1) in exposed_positions else lo for i in range(length)]


# ---------------------------------------------------------------------------
# The old rule is gone.
# ---------------------------------------------------------------------------

def test_the_cdr_heuristic_is_gone_entirely():
    """Not just unused -- removed, along with the argument that drove it."""
    assert not hasattr(lia, "_approximate_cdr_spans")
    assert not hasattr(lia, "_is_in_cdr")
    assert "chain_type" not in inspect.signature(find_liabilities).parameters
    assert "chain_type" not in inspect.signature(score_liabilities).parameters


def test_no_record_carries_an_in_cdr_key():
    for record in find_liabilities(SEQ):
        assert "in_cdr" not in record


# ---------------------------------------------------------------------------
# Exposed / buried / unknown.
# ---------------------------------------------------------------------------

def test_an_exposed_motif_outranks_a_buried_one():
    """The ruling, stated as the one comparison that matters."""
    exposed = _only(find_liabilities(SEQ, _rsa({_M_POSITION})), "oxidation", _M_POSITION)
    buried = _only(find_liabilities(SEQ, _rsa(set())), "oxidation", _M_POSITION)

    assert exposed["base_severity"] == buried["base_severity"] == "low"
    assert exposed["severity"] == "medium"
    assert buried["severity"] == "low"
    assert exposed["exposed"] is True
    assert buried["exposed"] is False
    assert lia.SEVERITY_WEIGHT[exposed["severity"]] > lia.SEVERITY_WEIGHT[buried["severity"]]


def test_unknown_exposure_does_not_escalate_but_is_not_recorded_as_buried():
    """The distinction a bare boolean would lose.

    With no structure we must not claim a motif is buried -- that is a
    measurement we did not make. So the severity behaves as for buried (no
    escalation, the conservative direction for a SCORE) while ``exposed``
    stays None so nothing downstream can render it as "buried".
    """
    unknown = _only(find_liabilities(SEQ, None), "oxidation", _M_POSITION)
    buried = _only(find_liabilities(SEQ, _rsa(set())), "oxidation", _M_POSITION)

    assert unknown["severity"] == buried["severity"] == "low"
    assert unknown["exposed"] is None
    assert buried["exposed"] is False


def test_a_residue_of_unknown_type_is_unknown_not_buried():
    """A None inside the RSA list is unknown for that residue."""
    rsa = [None] * len(SEQ)
    assert _only(find_liabilities(SEQ, rsa), "oxidation", _M_POSITION)["exposed"] is None


def test_the_threshold_is_the_one_in_surface_patch():
    """Non-vacuity for EXPOSED_RESIDUE_RSA: just over escalates, just under does not."""
    just_over = _rsa({_M_POSITION}, value=EXPOSED_RESIDUE_RSA + 0.01)
    just_under = _rsa({_M_POSITION}, value=EXPOSED_RESIDUE_RSA - 0.01)
    assert _only(find_liabilities(SEQ, just_over), "oxidation", _M_POSITION)["exposed"] is True
    assert _only(find_liabilities(SEQ, just_under), "oxidation", _M_POSITION)["exposed"] is False


def test_high_severity_cannot_escalate_past_high():
    """N-glycosylation starts at the top of the scale."""
    record = _only(find_liabilities(SEQ, _rsa({1, 2, 3, 4, 5})), "n_glycosylation")
    assert record["base_severity"] == "high"
    assert record["severity"] == "high"


# ---------------------------------------------------------------------------
# Multi-residue motifs, and the whole-chain finding.
# ---------------------------------------------------------------------------

def test_a_multi_residue_motif_is_exposed_if_any_residue_is():
    """NG spans positions 2-3; exposing either one is enough.

    A dipeptide half-buried is still reachable, and taking the maximum is the
    conservative direction for a liability.
    """
    ng_at = 2
    first_only = _only(find_liabilities(SEQ, _rsa({ng_at})), "deamidation", ng_at)
    second_only = _only(find_liabilities(SEQ, _rsa({ng_at + 1})), "deamidation", ng_at)
    neither = _only(find_liabilities(SEQ, _rsa(set())), "deamidation", ng_at)

    assert first_only["exposed"] is True
    assert second_only["exposed"] is True
    assert neither["exposed"] is False


def test_a_misaligned_rsa_list_is_discarded_entirely():
    """A length mismatch means the positional mapping is untrustworthy.

    An earlier version of this test filtered to ``position > 3`` and asserted
    the tail was unknown. For this fixture the only such record is the
    free-cysteine at 8, whose ``exposed`` is hard-coded None, so the test
    passed whatever the truncation logic did -- including the wrong answer it
    actually gave, which was to call a straddling motif measured-buried. It
    now asserts the whole behaviour, including position 1, which the old
    truncation logic reported as exposed=True off a short list.
    """
    short = _rsa({_M_POSITION}, length=len(SEQ) - 1)
    long = _rsa({_M_POSITION}, length=len(SEQ) + 1)

    for bad in (short, long):
        records = find_liabilities(SEQ, bad)
        assert records, "fixture produced no records at all"
        assert all(r["exposed"] is None for r in records), (
            "a misaligned residue_rsa must be discarded, so every motif "
            f"reports unknown exposure: {[(r['position'], r['exposed']) for r in records]}"
        )
    # Non-vacuity: the SAME positions do get measured off a correct-length list.
    good = _only(find_liabilities(SEQ, _rsa({_M_POSITION})), "oxidation", _M_POSITION)
    assert good["exposed"] is True


def test_a_misaligned_rsa_list_does_not_change_the_score():
    """Discarded exposure must land on the no-structure score, not a third one."""
    assert (
        score_liabilities(SEQ, _rsa({_M_POSITION}, length=len(SEQ) - 1))["score"]
        == score_liabilities(SEQ, None)["score"]
    )


def test_a_motif_straddling_the_end_of_the_list_is_unknown():
    """Unit-level, because find_liabilities' length check makes it unreachable.

    ``_motif_exposed`` is the one place that indexes the list, and its contract
    is that an index it cannot read makes the motif unknown. The length guard
    in :func:`find_liabilities` means no motif reaches it out of range today,
    so this pins the helper's own contract rather than a path through the
    scanner. Dropping the out-of-range indices and answering from the rest --
    the earlier behaviour -- reported a measured False here.
    """
    two_buried = [0.0, 0.0]
    assert lia._motif_exposed(two_buried, 1, 1) is False  # fully in range
    assert lia._motif_exposed(two_buried, 2, 2) is None   # second residue past end
    assert lia._motif_exposed(two_buried, 3, 1) is None   # starts past the end


def test_one_unknown_residue_makes_the_whole_motif_unknown():
    """Reachable: an RSA entry is None for a residue of unknown type."""
    rsa = _rsa({2})          # NG at 2-3: position 2 exposed, 3 buried
    assert _only(find_liabilities(SEQ, rsa), "deamidation", 2)["exposed"] is True
    rsa[2] = None            # now residue 3's accessibility is unknown
    assert _only(find_liabilities(SEQ, rsa), "deamidation", 2)["exposed"] is None


def test_free_cysteine_is_not_escalated_by_exposure():
    """It is a count over the whole chain, so one residue's exposure is mute.

    SEQ has a single cysteine, so the count is odd.
    """
    record = _only(find_liabilities(SEQ, _rsa(set(range(1, len(SEQ) + 1)))), "free_cysteine")
    assert record["severity"] == "high"
    assert record["exposed"] is None


# ---------------------------------------------------------------------------
# The score moves in the right direction.
# ---------------------------------------------------------------------------

def test_measuring_exposure_can_only_lower_or_hold_the_score():
    all_exposed = score_liabilities(SEQ, _rsa(set(range(1, len(SEQ) + 1))))
    none_exposed = score_liabilities(SEQ, _rsa(set()))
    unknown = score_liabilities(SEQ, None)

    assert all_exposed["score"] < none_exposed["score"]
    assert unknown["score"] == none_exposed["score"]
    assert all_exposed["weighted_count"] > none_exposed["weighted_count"]
    # Same findings either way -- exposure changes severity, not detection.
    assert all_exposed["count"] == none_exposed["count"] == unknown["count"]


def test_score_developability_passes_exposure_through():
    """The composite path, not just the dimension."""
    from shared.developability import score_developability

    exposed = score_developability(SEQ, residue_rsa=_rsa(set(range(1, len(SEQ) + 1))))
    buried = score_developability(SEQ, residue_rsa=_rsa(set()))
    assert exposed["dimensions"]["liabilities"]["score"] < buried["dimensions"]["liabilities"]["score"]
    assert exposed["composite_score"] < buried["composite_score"]


@pytest.mark.parametrize("chain_type", ["VH", "VL", "VHH", "SCFV", "OTHER"])
def test_chain_type_changes_no_number(chain_type):
    """It is a label now. Its two former consumers are both gone."""
    from shared.developability import score_developability

    baseline = score_developability(SEQ, chain_type="VH")
    got = score_developability(SEQ, chain_type=chain_type)
    assert got["composite_score"] == baseline["composite_score"]
    assert got["dimensions"] == baseline["dimensions"]
    assert got["chain_type"] == chain_type.upper()


# ---------------------------------------------------------------------------
# The dimension list, wherever it is written out in prose.
# ---------------------------------------------------------------------------

def test_every_written_out_dimension_list_matches_the_code():
    """Prose that names the dimensions must name the ones that exist.

    This defect landed three times in one branch: the package docstring and
    the dimensions docstring both still listed ``hydrophobicity`` after it was
    deleted, and the results template claimed "five scientific dimensions"
    over three cards -- a figure that had been wrong before either deletion.
    Two deletions went past all three because nothing tied the prose to
    ``_DIMENSIONS``. This does.

    The template is not checked here: it now renders
    ``result.dimensions | length`` instead of a written-out number
    (templates/developability_results.html), so its count cannot drift.
    """
    import io

    from shared.developability import score as score_mod

    live = set(score_mod._DIMENSIONS)
    gone = {"hydrophobicity", "humanness", "human-likeness"}

    for path in (
        "shared/developability/__init__.py",
        "shared/developability/dimensions/__init__.py",
    ):
        text = io.open(path, encoding="utf-8").read()
        doc = text.split('"""')[1]
        named = {d for d in live if d in doc}
        assert named == live, (
            f"{path} writes out a dimension list that is missing "
            f"{sorted(live - named)}. _DIMENSIONS is {sorted(live)}."
        )
        # A deleted dimension may be NAMED as deleted ("why there is no
        # hydrophobicity dimension"); what it must not do is sit in the list
        # of what the pipeline combines. The lists above are comma-separated,
        # so test the comma-joined run rather than the whole docstring.
        for stale in gone:
            assert f"{stale}," not in doc and f", {stale}" not in doc, (
                f"{path} still lists the deleted dimension {stale!r} as one "
                f"of the scored dimensions."
            )
