"""The hotspot repair hint, at the seam trunk never checks: the VERDICT.

This file started as a full regression suite over the chain-prefixed hotspot
behaviour PR #120 shipped. Most of that was already pinned in
``tests/test_multichain_targets.py``, and duplicated coverage is not free: it
is one more place to update, and it dilutes the signal when something does go
red. What is left is what no trunk test reaches.

WHAT IS ACTUALLY UNPINNED IN TRUNK. Not the chain-attribution logic — trunk
covers that thoroughly, including the chain-blind suggestion pool, which
``test_multichain_targets::test_nearest_suggestions_come_from_the_hotspots_own_chain``
catches with a purpose-built overlapping fixture of its own. What no trunk
test asserts is ``PreflightVerdict.nearest_clean_residues``. Every trunk
suggestion test calls ``shared.pdb_preflight._nearest_clean_residues``
DIRECTLY; grep the suite for the attribute and this file is the only hit. So
the helper could be correct while ``preflight_for_tool`` passed it the wrong
arguments, dropped the result, or returned it in a form nobody can paste back
into the field, and nothing would go red. Both suggestion tests below go
through ``preflight_for_tool``.

THE FIXTURE. Chain A is numbered 1..40 and chain B 101..140, and the offset is
NEAR on purpose. Trunk's two shared two-chain fixtures are not substitutes:
``_two_chain_pdb`` numbers both chains 1..40, so a chain-blind implementation
and a correct one agree on every input; ``_asymmetric_pdb`` numbers chain B
from 500, far enough that a chain-blind ±10 search around a chain-A hotspot
finds nothing on chain B either — it returns the same empty answer for the
right and the wrong reason. Here ``B101..B115`` sit inside the ±10 window of a
dropped ``"A105"``, so a union pool produces suggestions labelled ``"A101"``
and up: residues that do not exist.

WHAT WAS REMOVED, AND WHAT COVERS IT NOW (verified test by test):

  prefixed tokens in range on a multi-chain target
      -> test_multichain_targets::test_validate_hotspots_keeps_the_bare_int_contract
  whitespace chain field with prefixed tokens
      -> test_targets::test_validate_hotspots_accepts_a_multi_chain_target
         (the chain field is split once, before any per-token work, so the
         separator is not a per-token-kind property)
  a prefixed token checked against its own chain only
      -> test_multichain_targets::test_validate_hotspots_keeps_the_bare_int_contract
         and ::test_a_hotspot_is_checked_against_the_chain_it_names
  suggestions drawn from the hotspot's own chain, prefixed
      -> test_multichain_targets::test_suggestions_for_a_dropped_prefixed_hotspot_stay_on_its_chain
         and ::test_nearest_suggestions_come_from_the_hotspots_own_chain
         (both at the ``_nearest_clean_residues`` seam, not the verdict)
  a token naming an untargeted chain
      -> test_multichain_targets::test_split_hotspot ("C25", ["A","B"])
  mixed bare and prefixed in one list
      -> test_multichain_targets::test_mixed_bare_and_prefixed (adapter parser)
         plus ::test_validate_hotspots_keeps_the_bare_int_contract (both token
         kinds through this function)
  preflight accepts prefixed hotspots end to end
      -> test_multichain_targets::test_preflight_keeps_the_hotspots_validate_emits
         (rfdiffusion, pxdesign) and ::test_boltzgen_does_not_silently_discard_hotspots
  preflight drops a prefixed hotspot on the wrong chain
      -> test_multichain_targets::test_a_hotspot_is_checked_against_the_chain_it_names
  bare ints keep their values and types
      -> test_multichain_targets::test_validate_hotspots_keeps_the_bare_int_contract,
         test_pdb_inspect::test_hotspots_in_range /
         ::test_hotspots_out_of_range_caught /
         ::test_hotspots_against_missing_chain_all_rejected
  bare-int preflight verdict unchanged
      -> test_multichain_targets::test_single_chain_bare_int_hotspots_are_byte_identical

Every assertion below has been mutation-checked against the shipped
implementation: a chain-blind suggestion pool, bare suggestion labels, a
prefix matched only when more than one chain is named, and ``repr`` in the
dropped-hotspot message each turn this file red.
"""
from __future__ import annotations

import pytest

from shared.pdb_inspect import inspect_pdb_bytes, validate_hotspots
from shared.pdb_preflight import preflight_for_tool
from tests.test_pdb_preflight import _atom_line

pytestmark = pytest.mark.usefixtures("isolate_supabase")


# Chains whose numbering does not overlap, so "which chain is this residue on?"
# has a different answer from "does any chain have this residue?" — and whose
# offset is small enough that the two chains fall inside each other's
# suggestion window.
CHAIN_A_RANGE = (1, 40)
CHAIN_B_RANGE = (101, 140)

# The residue the leak test drops as "A105": present on chain B, absent from
# chain A, and with chain-B neighbours inside the ±10 suggestion window.
LEAK_PROBE = 105


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
    # The probe has to be OFF chain A (so "A105" is dropped) and ON chain B
    # (so a union pool has something to leak). Widen the gap and the leak test
    # starts passing against a chain-blind pool, exactly as trunk's
    # _asymmetric_pdb does.
    assert not (a_lo <= LEAK_PROBE <= a_hi), "A105 must be droppable"
    assert b_lo <= LEAK_PROBE <= b_hi, "B105 must exist"


def test_prefixed_hotspot_on_a_single_chain_target_is_in_range(report):
    """A prefixed token against a ONE-element chain list.

    Kept because nothing in trunk exercises it. ``split_hotspot`` matches a
    prefix against the list of chains the target names, and every trunk case
    with a valid prefixed token passes a TWO-element list. tools/base.py has
    its own separate parser loop (``parse_hotspot_residues``), so
    test_multichain_targets::test_single_chain_prefixed_tokens_normalize_to_strings
    covers that implementation and not this one — an "only prefix-match when
    more than one chain is named" regression would pass the whole suite.
    """
    in_range, out_of_range = validate_hotspots(report, "A", ["A25"])
    assert out_of_range == []
    assert in_range == ["A25"]


def test_repair_suggestions_never_leak_in_from_another_chain(pdb):
    """A chain-blind suggestion pool, seen through the VERDICT.

    ``"A105"`` is dropped because residue 105 is not on chain A — but it IS on
    chain B, and 101..115 all sit within the ±10 suggestion window of 105. So
    a pool that searches the union of the named chains instead of the hotspot's
    own chain produces suggestions here, and produces them labelled ``"A101"``
    and up: residues that do not exist. A pool confined to chain A finds
    nothing within ±10 of 105 and correctly offers nothing.

    Be precise about what this adds. trunk's test_multichain_targets::
    test_nearest_suggestions_come_from_the_hotspots_own_chain already catches
    the chain-blind pool, with its own overlapping fixture — a mutation to
    ``pool = union`` turns BOTH red, which is how that was established rather
    than assumed. What it does not do, and no trunk test does, is read the
    suggestions off the VERDICT: it calls ``_nearest_clean_residues`` directly.
    The sibling test below covers the same seam for the happy path; this one
    covers it for the case where the correct answer is "offer nothing", which
    a verdict that silently substituted an unfiltered pool would get wrong.

    The sibling's ``"A45"`` cannot stand in for this: its neighbours are all on
    chain A anyway, so the union and the per-chain pool agree.
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

    Reached through ``preflight_for_tool`` rather than ``_nearest_clean_residues``
    directly, which is the seam trunk's sibling test uses — the verdict has to
    carry the suggestions out, not just compute them.
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


@pytest.mark.parametrize("chain,hotspot,shown", [
    ("A", 999, "999"),
    ("A,B", "A105", "A105"),
])
def test_a_dropped_hotspot_is_named_in_the_message_exactly_as_typed(
    pdb, chain, hotspot, shown
):
    """The refusal has to echo the token the user can find in their own field.

    Kept because no trunk test asserts on the dropped-hotspot message at all.
    ``shared/pdb_preflight.py`` builds it with ``str(h)``; the prefixed row is
    what makes that checkable, because ``repr`` of an int is unquoted and only
    the string token would start rendering as ``'A105'`` — a value the user
    never typed, in a message they are reading to recover from a rejection.
    The bare row is here so the historical shape is pinned in the same place.
    """
    verdict = preflight_for_tool(
        "rfdiffusion", pdb, target_chain=chain, hotspots=[hotspot],
        binder_max_aa=65, num_designs=2,
    )
    assert verdict.hotspot_status["dropped"] == [hotspot]
    reason = verdict.reason or ""
    assert shown in reason, reason
    assert f"'{shown}'" not in reason, (
        f"the refusal quotes the hotspot where the user typed {shown}: "
        f"{reason!r}"
    )
