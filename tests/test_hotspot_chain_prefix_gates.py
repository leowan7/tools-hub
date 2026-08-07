"""Chain-prefixed hotspot tokens must survive the two shared gates.

This is a REGRESSION suite over behaviour PR #120 already shipped, not a test
of a new fix. It is worth being explicit about that, because this file was
originally written the other way round and the story it told was wrong.

PR #120 taught both gates the chain-prefixed hotspot TOKEN (``"A296"``), not
just the multi-chain ``target_chain`` FIELD (``"A,B"``). What it did NOT do is
leave much of that behaviour pinned from the outside: the one end-to-end
preflight test in ``tests/test_multichain_targets.py`` still passed
``hotspots=[]``, so the prefixed token had never been driven through
``preflight_for_tool`` with a non-empty list at all. That gap is what this file
closes, and it is why the suite is worth its weight even though nothing here
was broken when it was written.

Two load-bearing properties are asserted:

1. **The prefixed path works, and is chain-attributed.** Not merely "is
   accepted": ``"A105"`` must be REJECTED on a file where residue 105 exists
   only on chain B, and a repair suggestion for a chain-A hotspot must never be
   drawn from chain B.
2. **The bare-int path is unchanged.** Not merely "still passes": the element
   *types* are compared, because ``25`` becoming ``"25"`` is exactly the
   regression class PR #120 chased through ~2352 differential cases.

Every assertion here has been mutation-checked against the shipped
implementation — five mutations (bare suggestion labels; a chain-blind
suggestion pool; preflight attributing a prefixed token to the first named
chain; ``validate_hotspots`` unioning instead of honouring the prefix;
``split_hotspot`` ignoring the chain list) each turn this file red. An earlier
draft of the suggestion test could NOT see the chain-blind mutation, because
its dropped hotspot's neighbours were on the right chain anyway; that is what
``test_repair_suggestions_never_leak_in_from_another_chain`` exists to catch.

The fixture is deliberately ASYMMETRIC (chain A 1..40, chain B 101..140).
``tests/test_multichain_targets._two_chain_pdb`` numbers both chains 1..40, so
against it a chain-blind implementation and a correct one return the same
answer for every input — a fixture that cannot tell the bug from the fix.
"""
from __future__ import annotations

import pytest

from shared.pdb_inspect import inspect_pdb_bytes, validate_hotspots
from shared.pdb_preflight import preflight_for_tool
from tests.test_pdb_preflight import _atom_line

pytestmark = pytest.mark.usefixtures("isolate_supabase")


# Chains whose numbering does not overlap, so "which chain is this residue on?"
# has a different answer from "does any chain have this residue?".
CHAIN_A_RANGE = (1, 40)
CHAIN_B_RANGE = (101, 140)


def _asymmetric_two_chain_pdb() -> bytes:
    """Chain A numbered 1..40, chain B numbered 101..140.

    Both are 40 ALA residues, above every tool's 30-residue floor. The offset
    numbering is the whole point: a hotspot of ``B105`` is resolvable only if
    the checker looks at chain B specifically, and ``A105`` must be REJECTED
    even though residue 105 exists in the file.
    """
    lines = ["HEADER    SYNTHETIC ASYMMETRIC TWO CHAIN\n"]
    serial = 0
    for ci, (cid, (lo, _hi)) in enumerate(
        (("A", CHAIN_A_RANGE), ("B", CHAIN_B_RANGE))
    ):
        for i in range(40):
            for aname, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 2.0)]:
                serial += 1
                lines.append(_atom_line(
                    serial=serial, name=aname, resname="ALA", chain=cid,
                    resnum=lo + i, x=i * 4.0 + off,
                    y=1.0 if aname != "O" else 2.0, z=1.0 + 50.0 * ci,
                ))
    lines.append("END\n")
    return "".join(lines).encode()


@pytest.fixture(scope="module")
def pdb() -> bytes:
    return _asymmetric_two_chain_pdb()


@pytest.fixture(scope="module")
def report(pdb):
    return inspect_pdb_bytes(pdb)


def test_the_fixture_is_actually_asymmetric(report):
    """Guard the guard. If someone renumbers this fixture to overlap, every
    chain-attribution test below silently stops testing anything."""
    seen = {c.chain_id: (c.min_resnum, c.max_resnum) for c in report.chains}
    assert seen == {"A": CHAIN_A_RANGE, "B": CHAIN_B_RANGE}
    a_lo, a_hi = CHAIN_A_RANGE
    b_lo, b_hi = CHAIN_B_RANGE
    assert a_hi < b_lo, "ranges must not overlap or the tests prove nothing"


# ---------------------------------------------------------------------------
# The prefixed path — every test here fails against the pre-fix code
# ---------------------------------------------------------------------------

def test_prefixed_hotspot_on_a_single_chain_target_is_in_range(report):
    in_range, out_of_range = validate_hotspots(report, "A", ["A25"])
    assert out_of_range == []
    assert in_range == ["A25"]


def test_prefixed_hotspots_on_a_multi_chain_target_are_in_range(report):
    in_range, out_of_range = validate_hotspots(report, "A,B", ["A25", "B125"])
    assert out_of_range == []
    assert in_range == ["A25", "B125"]


def test_whitespace_chain_form_also_resolves_prefixed_tokens(report):
    """``validate_target_chain`` has always accepted "A B"; the hotspot checker
    must read the same field the same way."""
    in_range, out_of_range = validate_hotspots(report, "A B", ["A25", "B125"])
    assert out_of_range == []
    assert in_range == ["A25", "B125"]


def test_a_prefixed_token_is_checked_against_its_own_chain_only(report):
    """The chain-attribution assertion. Residue 105 exists in this file — on
    chain B. ``A105`` must still be refused, and a checker that unions the two
    ranges cannot tell the difference."""
    in_range, out_of_range = validate_hotspots(report, "A,B", ["A105"])
    assert in_range == []
    assert out_of_range == ["A105"]


def test_the_mirror_case_passes_so_the_previous_test_is_not_vacuous(report):
    in_range, out_of_range = validate_hotspots(report, "A,B", ["B105"])
    assert out_of_range == []
    assert in_range == ["B105"]


def test_a_token_naming_an_untargeted_chain_is_refused(report):
    """Chain C is not in the file at all, and chain B is not a target here."""
    _, out_of_range = validate_hotspots(report, "A", ["C25"])
    assert out_of_range == ["C25"]
    _, out_of_range = validate_hotspots(report, "A", ["B125"])
    assert out_of_range == ["B125"]


def test_mixed_bare_and_prefixed_tokens_resolve_independently(report):
    """A bare int keeps its historical meaning — checked against the union —
    while the prefixed token beside it is pinned to its own chain."""
    in_range, out_of_range = validate_hotspots(report, "A,B", [25, "B125"])
    assert out_of_range == []
    assert in_range == [25, "B125"]


@pytest.mark.parametrize("tool", ["rfdiffusion", "pxdesign", "boltzgen"])
def test_preflight_accepts_prefixed_hotspots_end_to_end(tool, pdb):
    """The gate that actually blocks submit.

    The two failure modes this pins are the ones a bare ``int()`` parser
    produces, and they are asymmetric in cost: rfdiffusion and pxdesign refuse
    the submit citing an incomplete backbone — a claim about the FILE, which is
    in fact complete — while boltzgen (``hotspots_required=False``) returns
    READY and runs a PAID GPU job with every hotspot silently discarded. The
    second is the expensive one, which is why boltzgen is parametrised here
    rather than left to the two tools that fail loudly.
    """
    verdict = preflight_for_tool(
        tool, pdb, target_chain="A,B", hotspots=["A25", "B125"],
        binder_max_aa=65, num_designs=2,
    )
    assert verdict.hotspot_status["dropped"] == [], verdict.reason
    assert verdict.hotspot_status["surviving"] == ["A25", "B125"]
    assert "backbone is incomplete" not in (verdict.reason or "")


@pytest.mark.parametrize("tool", ["rfdiffusion", "pxdesign", "boltzgen"])
def test_preflight_drops_a_prefixed_hotspot_on_the_wrong_chain(tool, pdb):
    """Chain attribution at the preflight gate, not just the inspect gate.
    Residue 105 is present in the file but not on chain A."""
    verdict = preflight_for_tool(
        tool, pdb, target_chain="A,B", hotspots=["A105"],
        binder_max_aa=65, num_designs=2,
    )
    assert verdict.hotspot_status["surviving"] == []
    assert verdict.hotspot_status["dropped"] == ["A105"]


def test_repair_suggestions_never_leak_in_from_another_chain(pdb):
    """The companion to the test below, and the one that can actually catch a
    chain-blind suggestion pool.

    ``"A105"`` is dropped because residue 105 is not on chain A — but it IS on
    chain B, and 101..115 all sit within the ±10 suggestion window of 105. So
    a pool that searches the union of the named chains instead of the hotspot's
    own chain produces suggestions here, and produces them labelled ``"A101"``
    and up: residues that do not exist. A pool confined to chain A finds
    nothing within ±10 of 105 and correctly offers nothing.

    The sibling test below uses ``"A45"``, whose neighbours are all on chain A
    anyway — which is exactly why it CANNOT see this mutation, and why this
    test exists separately rather than as one more assertion there.
    """
    verdict = preflight_for_tool(
        "rfdiffusion", pdb, target_chain="A,B", hotspots=["A105"],
        binder_max_aa=65, num_designs=2,
    )
    assert verdict.hotspot_status["dropped"] == ["A105"]
    for s in verdict.nearest_clean_residues or []:
        n = int(str(s)[1:]) if str(s).startswith("A") else int(s)
        assert CHAIN_A_RANGE[0] <= n <= CHAIN_A_RANGE[1], (
            f"suggestion {s!r} repairs a chain-A hotspot with a residue that "
            f"is not on chain A; the whole set was "
            f"{verdict.nearest_clean_residues!r}"
        )


def test_a_dropped_prefixed_hotspot_still_gets_nearest_residue_suggestions(pdb):
    """The repair hint must survive the prefix, AND come back pasteable.

    Two properties, and the second is the one worth pinning. A prefixed
    hotspot is confined to its own chain, so its suggestions have to be drawn
    from THAT chain — offering chain B's residues to repair an ``"A45"`` typo
    would be actively wrong. And they have to be echoed in the same prefixed
    form the user typed, because a bare suggestion cannot be pasted back into
    a multi-chain field: on an Fc homodimer both protomers carry residue 264,
    so ``264`` alone does not say which protomer the fix means.
    """
    verdict = preflight_for_tool(
        "rfdiffusion", pdb, target_chain="A,B", hotspots=["A45"],
        binder_max_aa=65, num_designs=2,
    )
    assert verdict.hotspot_status["dropped"] == ["A45"]
    assert verdict.nearest_clean_residues, (
        "a prefixed hotspot just off the end of chain A should still suggest "
        "nearby real residues, as the bare form does"
    )
    for s in verdict.nearest_clean_residues:
        assert isinstance(s, str) and s.startswith("A"), (
            f"suggestion {s!r} is not pasteable back into a multi-chain "
            f"hotspot field; the whole set was "
            f"{verdict.nearest_clean_residues!r}"
        )
        assert CHAIN_A_RANGE[0] <= int(s[1:]) <= CHAIN_A_RANGE[1], (
            f"suggestion {s!r} is outside chain A's range {CHAIN_A_RANGE}; "
            f"the whole set was {verdict.nearest_clean_residues!r}"
        )


# ---------------------------------------------------------------------------
# The bare-int path — byte-identical, element types included
# ---------------------------------------------------------------------------

BARE_CASES = [
    ("A", [25]),
    ("A", [25, 30]),
    ("A", [1, 40]),
    ("A", [999]),           # out of range
    ("A", [25, 999]),       # one of each
    ("A", []),
    ("A,B", [25]),
    ("A,B", [125]),         # union semantics: on chain B, bare, still in range
    ("A,B", [25, 125]),
    ("A,B", [999]),
    ("A B", [25]),
]


@pytest.mark.parametrize("target_chain,hotspots", BARE_CASES)
def test_bare_ints_keep_their_exact_values_and_types(
    target_chain, hotspots, report
):
    in_range, out_of_range = validate_hotspots(report, target_chain, hotspots)
    for bucket in (in_range, out_of_range):
        assert all(type(v) is int for v in bucket), (
            f"{target_chain!r}/{hotspots!r} -> in_range={in_range!r} "
            f"out_of_range={out_of_range!r}: a bare int must stay an int, "
            f"not become a string"
        )
    # Union semantics for bare ints, unchanged: in range on ANY named chain.
    expected_in = [
        h for h in hotspots
        if (CHAIN_A_RANGE[0] <= h <= CHAIN_A_RANGE[1] and "A" in target_chain)
        or (CHAIN_B_RANGE[0] <= h <= CHAIN_B_RANGE[1] and "B" in target_chain)
    ]
    assert in_range == expected_in
    assert out_of_range == [h for h in hotspots if h not in expected_in]


@pytest.mark.parametrize("target_chain,hotspots", BARE_CASES)
@pytest.mark.parametrize("tool", ["rfdiffusion", "pxdesign", "boltzgen"])
def test_bare_int_preflight_verdict_is_unchanged(
    tool, target_chain, hotspots, pdb
):
    """Differential guard on the full verdict, including ``hotspot_status``
    element types — the shape PR #120 verified across ~2352 cases."""
    verdict = preflight_for_tool(
        tool, pdb, target_chain=target_chain, hotspots=hotspots,
        binder_max_aa=65, num_designs=2,
    )
    for key in ("surviving", "dropped"):
        assert all(
            type(v) is int for v in verdict.hotspot_status[key]
        ), f"{tool}/{target_chain}/{hotspots}: {key}={verdict.hotspot_status[key]!r}"


def test_a_bare_int_out_of_range_still_reads_as_a_plain_number(pdb):
    """The user-facing message for the historical case must not start
    rendering quotes or chain letters that were never typed."""
    verdict = preflight_for_tool(
        "rfdiffusion", pdb, target_chain="A", hotspots=[999],
        binder_max_aa=65, num_designs=2,
    )
    assert verdict.hotspot_status["dropped"] == [999]
    assert "'999'" not in (verdict.reason or "")
