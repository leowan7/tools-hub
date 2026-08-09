"""Multi-chain target support in the binder-generator adapters.

The three binder generators whose pipelines live in llm-proteinDesigner
(bindcraft, pxdesign, rfdiffusion) accept an oligomeric target. The
pipeline-side contract is:

    target_chain:     "A,B"              comma string; "A" behaves as before
    hotspot_residues: ["A296", "B264"]   chain-prefixed; bare ints still
                                         accepted, attributed to the FIRST
                                         target chain

These adapters previously coerced every hotspot token with ``int()``, so a
chain-prefixed hotspot was rejected at the form layer even where the
pipeline supported it — BindCraft's has passed ``target_chain`` straight
through to its ``chains`` setting all along.

Backward compatibility is the load-bearing assertion here: a single-chain
target with bare integer hotspots must still produce the exact payload it
did before, plain ints and all.
"""
from __future__ import annotations

import pytest

from tools import bindcraft as bindcraft_mod
from tools import pxdesign as pxdesign_mod
from tools import rfdiffusion as rfdiffusion_mod
from tools.base import parse_hotspot_residues, parse_target_chains


# ---------------------------------------------------------------------------
# parse_target_chains
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("A", ["A"]),
    ("A,B", ["A", "B"]),
    (" A , B ", ["A", "B"]),
    ("B,A", ["B", "A"]),      # order is preserved, not sorted
    ("A,B,A", ["A", "B"]),    # de-duplicated
    ("", []),
    ("  ", []),
])
def test_parse_target_chains(raw, expected):
    assert parse_target_chains(raw) == expected


# ---------------------------------------------------------------------------
# parse_hotspot_residues
# ---------------------------------------------------------------------------

def test_single_chain_bare_ints_stay_bare_ints():
    """The payload shape submitted before multi-chain existed."""
    assert parse_hotspot_residues("54,56,115", ["A"]) == ([54, 56, 115], None)


def test_single_chain_prefixed_tokens_normalize_to_strings():
    assert parse_hotspot_residues("A54,A56", ["A"]) == (["A54", "A56"], None)


def test_two_chain_prefixed_tokens():
    assert parse_hotspot_residues("A296,B264", ["A", "B"]) == (
        ["A296", "B264"], None
    )


def test_two_chain_bare_ints_attach_to_first_chain():
    assert parse_hotspot_residues("296,264", ["A", "B"]) == (
        ["A296", "A264"], None
    )
    assert parse_hotspot_residues("296", ["B", "A"]) == (["B296"], None)


def test_mixed_bare_and_prefixed():
    assert parse_hotspot_residues("296, B264", ["A", "B"]) == (
        ["A296", "B264"], None
    )


def test_whitespace_tolerated():
    assert parse_hotspot_residues(" 54 , 56 ", ["A"]) == ([54, 56], None)


def test_hotspot_naming_an_untargeted_chain_is_an_error():
    residues, err = parse_hotspot_residues("C25", ["A", "B"])
    assert residues is None
    assert "does not name one of your target chains" in err
    assert "A, B" in err


def test_garbage_token_is_an_error():
    residues, err = parse_hotspot_residues("xyz", ["A", "B"])
    assert residues is None
    assert "integer" in err.lower()


def test_chain_prefix_without_a_number_is_an_error():
    residues, err = parse_hotspot_residues("Axx", ["A", "B"])
    assert residues is None
    assert "integer" in err.lower()


def test_empty_hotspots_is_an_error():
    residues, err = parse_hotspot_residues("", ["A"])
    assert residues is None
    assert "at least one hotspot" in err.lower()


def test_no_target_chains_is_an_error():
    residues, err = parse_hotspot_residues("54", [])
    assert residues is None
    assert "Target chain is required" in err


# ---------------------------------------------------------------------------
# Adapter validate() — all three binder generators
# ---------------------------------------------------------------------------

def _form(mod_name: str, target_chain: str, hotspots: str) -> dict:
    common = {
        "preset": "pilot",
        "target_chain": target_chain,
        "hotspot_residues": hotspots,
        "num_designs": "2",
    }
    if mod_name == "pxdesign":
        common["binder_length"] = "80"
    else:
        common["binder_length_min"] = "55"
        common["binder_length_max"] = "65"
    return common


ADAPTERS = [
    ("bindcraft", bindcraft_mod),
    ("pxdesign", pxdesign_mod),
    ("rfdiffusion", rfdiffusion_mod),
]


@pytest.mark.parametrize("name,mod", ADAPTERS)
def test_single_chain_pilot_unchanged(name, mod):
    """Existing callers submit target_chain "A" with bare ints. That payload
    must not move."""
    inputs, err = mod.validate(_form(name, "A", "54,56,115"), {})
    assert err is None, err
    assert inputs["target_chain"] == "A"
    assert inputs["hotspot_residues"] == [54, 56, 115]


@pytest.mark.parametrize("name,mod", ADAPTERS)
def test_two_chain_target_accepted(name, mod):
    inputs, err = mod.validate(_form(name, "A,B", "A296,B264"), {})
    assert err is None, err
    assert inputs["target_chain"] == "A,B"
    assert inputs["hotspot_residues"] == ["A296", "B264"]


@pytest.mark.parametrize("name,mod", ADAPTERS)
def test_two_chain_bare_ints_go_to_first_chain(name, mod):
    inputs, err = mod.validate(_form(name, "A,B", "296,264"), {})
    assert err is None, err
    assert inputs["hotspot_residues"] == ["A296", "A264"]


@pytest.mark.parametrize("name,mod", ADAPTERS)
def test_hotspot_on_untargeted_chain_rejected(name, mod):
    inputs, err = mod.validate(_form(name, "A,B", "C25"), {})
    assert inputs is None
    assert "target chains" in err


@pytest.mark.parametrize("name,mod", ADAPTERS)
def test_three_chain_target_accepted(name, mod):
    """The 4-character cap used to be applied to the WHOLE field, so it
    admitted "A,B" and rejected "A,B,C" — silently capping every target at
    two chains for no reason but the separator's width. It is now per-token."""
    inputs, err = mod.validate(_form(name, "A,B,C", "A296"), {})
    assert err is None, err
    assert inputs["target_chain"] == "A,B,C"


@pytest.mark.parametrize("name,mod", ADAPTERS)
def test_overlong_chain_id_still_rejected_per_token(name, mod):
    inputs, err = mod.validate(_form(name, "A,BCDEF", "A296"), {})
    assert inputs is None
    assert "too long" in err


@pytest.mark.parametrize("name,mod", ADAPTERS)
def test_whitespace_separated_chains_accepted(name, mod):
    """tools-hub's own convention, predating the comma contract:
    shared/pdb_inspect.validate_target_chain splits on whitespace, five tools
    declare multi_chain_supported=True, and the form copy says "List chains
    separated by spaces". Parsing only commas here made the feature
    unreachable — every gate after this one rejected the comma form."""
    inputs, err = mod.validate(_form(name, "A B", "A296 B264"), {})
    assert err is None, err
    assert inputs["hotspot_residues"] == ["A296", "B264"]


@pytest.mark.parametrize("name,mod", ADAPTERS)
@pytest.mark.parametrize("typed", ["A,B", "A B", "A, B", "A  B"])
def test_target_chain_is_canonicalised_to_the_comma_form(name, mod, typed):
    """Both separators are accepted at this boundary and exactly one is
    emitted, so no container downstream has to guess which it will get."""
    inputs, err = mod.validate(_form(name, typed, "A296"), {})
    assert err is None, err
    assert inputs["target_chain"] == "A,B"
    payload = mod.build_payload(inputs, "https://example.invalid/t.pdb")
    assert payload["target_chain"] == "A,B"


@pytest.mark.parametrize("name,mod", ADAPTERS)
def test_build_payload_forwards_the_multichain_shape(name, mod):
    inputs, err = mod.validate(_form(name, "A,B", "A296,B264"), {})
    assert err is None, err
    payload = mod.build_payload(inputs, "https://example.invalid/target.pdb")
    assert payload["target_chain"] == "A,B"
    assert payload["hotspot_residues"] == ["A296", "B264"]


# ---------------------------------------------------------------------------
# The seam: what validate() EMITS must be what the shared layer can PARSE
# ---------------------------------------------------------------------------
#
# Canonicalising to "A,B" was correct at the adapter boundary and broke
# everything downstream of it, because eight parsers in shared/ split on
# whitespace only. blueprints/tools.py:1204 feeds the POST-validate value
# into preflight_for_tool, and blueprints/tools.py:1223 blocks submit on
# `not verdict.ok` — so every multi-chain submission was refused with
# "Target chain 'A,B' isn't in this PDB. Found chain(s): A, B."
#
# The tests above did not catch it because each one checks a single side of
# the seam: the adapter emits "A,B" (true), and the shared parsers accept
# "A B" (also true). Nothing asserted that the emitted form is an accepted
# form. These do.

from shared.pdb_inspect import (            # noqa: E402
    inspect_pdb_bytes, validate_target_chain,
)
from shared.pdb_preflight import (          # noqa: E402
    VerdictKind, _chain_tokens, preflight_for_tool,
)
from tools import boltzgen as boltzgen_mod                          # noqa: E402
from tests.test_pdb_preflight import _atom_line                     # noqa: E402


def _two_chain_pdb(n_res: int = 40) -> bytes:
    """Two chains of ``n_res`` ALA each — above every tool's 30-residue floor."""
    lines = ["HEADER    SYNTHETIC TWO CHAIN\n"]
    serial = 0
    for ci, cid in enumerate(("A", "B")):
        for i in range(n_res):
            for aname, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 2.0)]:
                serial += 1
                lines.append(_atom_line(
                    serial=serial, name=aname, resname="ALA", chain=cid,
                    resnum=i + 1, x=i * 4.0 + off,
                    y=1.0 if aname != "O" else 2.0, z=1.0 + 50.0 * ci,
                ))
    lines.append("END\n")
    return "".join(lines).encode()


@pytest.mark.parametrize("typed", ["A,B", "A B", "A, B"])
def test_chain_tokens_accepts_whatever_validate_emits(typed):
    _, mod = ADAPTERS[0]
    inputs, err = mod.validate(_form("bindcraft", typed, "A5,B5"), {})
    assert err is None, err
    emitted = inputs["target_chain"]
    assert _chain_tokens(emitted) == ["A", "B"], (
        f"validate() emits {emitted!r} but the shared tokenizer reads it as "
        f"{_chain_tokens(emitted)!r} — the seam is broken"
    )


@pytest.mark.parametrize("typed", ["A,B", "A B"])
def test_validate_target_chain_accepts_whatever_validate_emits(typed):
    _, mod = ADAPTERS[0]
    inputs, _ = mod.validate(_form("bindcraft", typed, "A5,B5"), {})
    report = inspect_pdb_bytes(_two_chain_pdb())
    assert validate_target_chain(report, inputs["target_chain"]) is None


# The container gate (shared/pdb_preflight_rules.multi_chain_container_ready)
# is what decides whether a multi-chain target is actually runnable, and it
# tracks GPU EVIDENCE, not model capability. Splitting the parametrisation by
# evidence level keeps the distinction executable instead of a comment.
GPU_VERIFIED = [("pxdesign", pxdesign_mod), ("rfdiffusion", rfdiffusion_mod)]
GATED = [("bindcraft", bindcraft_mod)]


@pytest.mark.parametrize("name,mod", GPU_VERIFIED)
@pytest.mark.parametrize("typed", ["A,B", "A B"])
def test_preflight_accepts_the_canonical_form_end_to_end(name, mod, typed):
    """The exact path blueprints/tools.py takes: form -> validate() ->
    preflight_for_tool(inputs["target_chain"]). Both chains must survive."""
    inputs, err = mod.validate(_form(name, typed, "A5,B5"), {})
    assert err is None, err

    verdict = preflight_for_tool(
        name, _two_chain_pdb(),
        target_chain=inputs["target_chain"],
        hotspots=[], binder_max_aa=65, num_designs=2,
    )
    assert "isn't in this PDB" not in (verdict.reason or ""), verdict.reason
    # 80 residues across both chains, not 40 from one and not 0 from a
    # whole-string lookup that matched nothing.
    assert verdict.cleanup.residues_kept_on_target_chain == 80, (
        f"{name}: kept {verdict.cleanup.residues_kept_on_target_chain} "
        f"residues for {inputs['target_chain']!r}; expected both chains"
    )
    assert verdict.cleanup.chains_dropped == []


@pytest.mark.parametrize("name,mod", GATED)
@pytest.mark.parametrize("typed", ["A,B", "A B"])
def test_preflight_refuses_multichain_for_the_unverified_image(name, mod, typed):
    """bindcraft parses the multi-chain form correctly and is then refused by
    the container gate, because its image has never been run on a multi-chain
    target — it is the one binder tool with no smoke tier, so clearing it costs
    a full paid pilot.

    The refusal must name the IMAGE, not the structure. Before the shared-layer
    fix, this same input was refused with "Target chain 'A,B' isn't in this
    PDB" — a wrong reason that sends the user off to re-examine a perfectly
    good file. A gate is only useful if it says what is actually blocking.
    """
    inputs, err = mod.validate(_form(name, typed, "A5,B5"), {})
    assert err is None, err

    verdict = preflight_for_tool(
        name, _two_chain_pdb(),
        target_chain=inputs["target_chain"],
        hotspots=[], binder_max_aa=65, num_designs=2,
    )
    assert not verdict.ok
    reason = verdict.reason or ""
    assert "isn't in this PDB" not in reason, (
        f"refused for the wrong reason: {reason!r}"
    )
    assert "GPU image" in reason, reason


# ---------------------------------------------------------------------------
# The OTHER half of the same seam: hotspot_residues
# ---------------------------------------------------------------------------
#
# The two tests above pass ``hotspots=[]``. That is precisely why they went
# green while the hotspot half of the contract was broken: fixing the
# target_chain seam and then asserting it with an empty hotspot list proves
# only that the field you happened to think about works.
#
# validate() emits ["A296", "B264"]; blueprints/tools.py hands that POST-
# validate value to preflight_for_tool; and shared/pdb_inspect.py and
# shared/pdb_preflight.py both coerced every token with a bare int(). So:
#
#   rfdiffusion / pxdesign  -> NEEDS_FIX, "backbone is incomplete in this
#                              PDB" — a false claim about a complete file
#   boltzgen                -> READY, every hotspot silently discarded, and
#                              a paid A100 run with no epitope constraint
#
# Anything below that reaches for a hotspot must use the value validate()
# actually produced, never a hand-written list.

def _asymmetric_pdb(n_res: int = 40) -> bytes:
    """Chain A numbered 1..n, chain B numbered 500..500+n.

    Disjoint ranges, so "is this hotspot on the chain it names" and "is it on
    any chain at all" give different answers — which is the only way to prove
    the check is chain-aware rather than unioning.
    """
    lines = ["HEADER    SYNTHETIC ASYMMETRIC\n"]
    serial = 0
    for ci, (cid, first) in enumerate((("A", 1), ("B", 500))):
        for i in range(n_res):
            for aname, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 2.0)]:
                serial += 1
                lines.append(_atom_line(
                    serial=serial, name=aname, resname="ALA", chain=cid,
                    resnum=first + i, x=i * 4.0 + off,
                    y=1.0 if aname != "O" else 2.0, z=1.0 + 50.0 * ci,
                ))
    lines.append("END\n")
    return "".join(lines).encode()


@pytest.mark.parametrize("name,mod", GPU_VERIFIED)
@pytest.mark.parametrize("typed", ["A,B", "A B"])
def test_preflight_keeps_the_hotspots_validate_emits(name, mod, typed):
    """form -> validate() -> preflight_for_tool, carrying the hotspots through.

    Every emitted hotspot must survive. A dropped one is not cosmetic: for
    these two tools ``hotspots_required=True`` turns it into a NEEDS_FIX that
    blocks submit.
    """
    inputs, err = mod.validate(_form(name, typed, "A5,B7"), {})
    assert err is None, err
    emitted = inputs["hotspot_residues"]
    assert emitted == ["A5", "B7"]

    verdict = preflight_for_tool(
        name, _two_chain_pdb(),
        target_chain=inputs["target_chain"], hotspots=emitted,
        binder_max_aa=65, num_designs=2,
    )
    assert verdict.hotspot_status["dropped"] == [], (
        f"{name}: preflight dropped {verdict.hotspot_status['dropped']!r} of "
        f"the hotspots validate() emitted — the seam is broken"
    )
    assert verdict.hotspot_status["surviving"] == ["A5", "B7"]
    assert verdict.ok, verdict.reason
    assert "backbone" not in (verdict.reason or "").lower(), (
        f"{name}: blamed the file's backbone for a parser failure: "
        f"{verdict.reason!r}"
    )


def test_boltzgen_does_not_silently_discard_hotspots():
    """boltzgen has hotspots_required=False, so a dropped hotspot does NOT
    block submit — it returns READY and launches an A100 run with the epitope
    constraint quietly removed. That is worse than the hard refusal the other
    tools got, and it is why 'the verdict was ok' is not enough to assert.
    """
    from tools import boltzgen as boltzgen_mod

    form = {
        "preset": "pilot", "target_chain": "A,B",
        "hotspot_residues": "A5,B7", "num_designs": "2",
        "binder_length_min": "55", "binder_length_max": "65",
    }
    inputs, err = boltzgen_mod.validate(form, {})
    assert err is None, err
    # Pin the adapter's own output too. boltzgen is not in GPU_VERIFIED, so
    # this is the ONLY place its hotspots reach preflight — and an adapter
    # that quietly dropped the prefixed tokens would otherwise sail through
    # every assertion below, which is exactly the PR #109 failure mode.
    assert inputs["hotspot_residues"] == ["A5", "B7"]

    verdict = preflight_for_tool(
        "boltzgen", _two_chain_pdb(),
        target_chain=inputs["target_chain"],
        hotspots=inputs["hotspot_residues"],
        binder_max_aa=65, num_designs=2,
    )
    assert verdict.ok, verdict.reason
    assert verdict.hotspot_status["dropped"] == [], (
        "boltzgen returned READY while discarding "
        f"{verdict.hotspot_status['dropped']!r} — a paid run with no hotspots"
    )
    # "nothing was dropped" is satisfied by an empty list, so assert the
    # positive as well: these specific hotspots reached the gate intact.
    assert verdict.hotspot_status["surviving"] == ["A5", "B7"]


@pytest.mark.parametrize("name,mod", GPU_VERIFIED)
def test_single_chain_bare_int_hotspots_are_byte_identical(name, mod):
    """The backward-compatibility floor. Every job submitted before the
    multi-chain contract posts bare ints on one chain; those must round-trip
    through preflight as ints, not as newly-prefixed strings.
    """
    inputs, err = mod.validate(_form(name, "A", "5,7"), {})
    assert err is None, err
    assert inputs["hotspot_residues"] == [5, 7]

    verdict = preflight_for_tool(
        name, _two_chain_pdb(),
        target_chain=inputs["target_chain"],
        hotspots=inputs["hotspot_residues"],
        binder_max_aa=65, num_designs=2,
    )
    assert verdict.hotspot_status["surviving"] == [5, 7]
    assert verdict.hotspot_status["dropped"] == []


def test_a_hotspot_is_checked_against_the_chain_it_names():
    """``B5`` must not be accepted because chain A happens to have residue 5.

    On a homodimer both protomers carry the same numbering, so a union check
    passes every prefixed hotspot regardless of which protomer it names — it
    would report a valid epitope on a chain the design never touches.
    """
    pdb = _asymmetric_pdb()          # A: 1..40, B: 500..539
    verdict = preflight_for_tool(
        "rfdiffusion", pdb, target_chain="A,B",
        hotspots=["A5", "B5"], binder_max_aa=65, num_designs=2,
    )
    assert verdict.hotspot_status["surviving"] == ["A5"]
    assert verdict.hotspot_status["dropped"] == ["B5"], (
        "B5 does not exist on chain B (which runs 500..539) and must not be "
        "accepted just because chain A has a residue 5"
    )


def test_suggestions_for_a_dropped_prefixed_hotspot_stay_on_its_chain():
    """The refusal suggests nearby clean residues. For a prefixed hotspot they
    must come from that chain and come back prefixed, so they can be pasted
    straight back into the field."""
    from shared.pdb_preflight import _nearest_clean_residues

    pdb = _asymmetric_pdb()          # A: 1..40, B: 500..539
    nearest = _nearest_clean_residues(pdb, "A,B", ["B498"], [])
    assert nearest, "expected suggestions near B498 on chain B"
    assert all(str(r).startswith("B") for r in nearest), nearest
    assert all(500 <= int(str(r)[1:]) <= 539 for r in nearest), nearest


# ---------------------------------------------------------------------------
# A BARE hotspot on a target whose chains are numbered differently
#
# The half of the seam the tests above miss. They all pass PREFIXED hotspots,
# which is what a user gets when they type one. A bare number is the other
# half, and it has no chain of its own — so something must attribute it, and
# for a long time two components attributed it differently:
#
#   tools/base.py:108     bare 520 on "A,B"  ->  "A520"   (FIRST named chain)
#   _check_hotspots       bare 520 on "A,B"  ->  in range on the UNION
#
# On any target whose chains carry different numbering — a Fab H/L, any
# heterocomplex — those disagree, and the disagreement is not cosmetic. The
# adapter's answer is the one that reaches the GPU, because build_payload
# ships inputs["hotspot_residues"] verbatim; the preflight's answer is the one
# that decides whether the run is allowed to start. Executed end to end on the
# fixture below, trunk produced:
#
#   preflight panel      -> READY            (520 range-checks on chain B)
#   adapter emits        -> ["A520"]
#   boltzgen submit gate -> ok=True, dropped=['A520']
#   payload              -> ships "A520" anyway
#   container            -> docker/boltzgen/run_pipeline.py raises
#
# Two independent faults, and BOTH have to be fixed for the money to be safe:
# the attribution has to match, AND a dropped hotspot has to refuse even on a
# tool whose hotspots are optional.
# ---------------------------------------------------------------------------

_BOLTZGEN_FORM_BASE = {
    "preset": "pilot", "binder_length_min": "55", "binder_length_max": "65",
    "budget": "4", "protocol": "protein-anything",
}


def _boltzgen_form(target_chain: str, hotspots: str) -> dict:
    return dict(_BOLTZGEN_FORM_BASE, target_chain=target_chain,
                hotspot_residues=hotspots)


def test_a_bare_hotspot_is_judged_on_the_chain_it_will_be_sent_as():
    """520 exists on chain B only, and the adapter will ship it as "A520".

    RED on trunk: the union check called it surviving, so the panel said READY
    for a token that can only ever address chain A.
    """
    from shared.pdb_preflight import _check_hotspots

    pdb = _asymmetric_pdb()          # A: 1..40, B: 500..539
    surviving, dropped = _check_hotspots(pdb, "A,B", [520])
    assert surviving == []
    assert dropped == [520], (
        "bare 520 was accepted because chain B happens to carry it, but "
        "tools/base.py will ship it as 'A520' and chain A runs 1..40"
    )
    # ...and the same number typed for the chain it really is on passes.
    assert _check_hotspots(pdb, "A,B", ["B520"]) == (["B520"], [])
    assert _check_hotspots(pdb, "A,B", [20]) == ([20], [])    # 20 IS on A


def test_the_panel_and_the_submit_gate_agree_on_a_bare_hotspot():
    """The AJAX panel passes the bare token; the submit gate passes the
    adapter's rewritten one. Trunk gave READY and READY; the run then died.

    Both must now reach the same verdict, or the panel green-lights a submit
    the gate refuses.
    """
    pdb = _asymmetric_pdb()
    panel = preflight_for_tool(
        "boltzgen", pdb, target_chain="A,B", hotspots=[520],
    )
    inputs, err = boltzgen_mod.validate(_boltzgen_form("A,B", "520"), {})
    assert err is None, err
    assert inputs["hotspot_residues"] == ["A520"]
    gate = preflight_for_tool(
        "boltzgen", pdb, target_chain=inputs["target_chain"],
        hotspots=inputs["hotspot_residues"],
    )
    assert panel.ok is gate.ok is False, (
        f"panel ok={panel.ok} gate ok={gate.ok} — a divergence here is the "
        f"defect itself"
    )


def test_a_dropped_hotspot_refuses_even_when_hotspots_are_optional():
    """THE MONEY GATE. boltzgen and proteina are hotspots_required=False, so
    trunk returned READY and let the wallet hold, the A100 and the container
    failure all happen. The payload still carries the token either way.
    """
    pdb = _asymmetric_pdb()
    for slug in ("boltzgen", "proteina"):
        verdict = preflight_for_tool(
            slug, pdb, target_chain="A,B", hotspots=["A520"],
        )
        assert not verdict.ok, (
            f"{slug}: READY while dropping "
            f"{verdict.hotspot_status['dropped']!r} — build_payload ships that "
            f"token, so this funds a run that cannot succeed"
        )
        assert verdict.hotspot_status["dropped"] == ["A520"]


def test_the_refusal_does_not_blame_a_backbone_that_is_intact():
    """The fixture is synthetic and every residue has a complete N/CA/C/O
    backbone. "A520" is dropped because chain A stops at 40, so copy that
    asserts an incomplete backbone sends the user to PyMOL for nothing."""
    verdict = preflight_for_tool(
        "boltzgen", _asymmetric_pdb(), target_chain="A,B", hotspots=["A520"],
    )
    reason = verdict.reason or ""
    assert "outside that chain's numbering" in reason, reason
    assert "incomplete backbone" not in reason.lower(), reason


def test_the_refusal_states_the_attribution_that_caused_it():
    """A user who typed "520" and is told "A520 can't be used" has no way to
    connect the two. The one sentence that closes that gap has to be there."""
    verdict = preflight_for_tool(
        "boltzgen", _asymmetric_pdb(), target_chain="A,B", hotspots=[520],
    )
    reason = verdict.reason or ""
    assert "without a chain letter is read as chain A" in reason, reason
    # And the fix tells them how to say what they meant.
    assert "Prefix a hotspot with its chain" in (verdict.suggested_fix or "")


def test_suggestions_for_a_bare_hotspot_come_from_the_chain_it_lands_on():
    """A bare suggestion pasted back into the field is itself attributed to
    the first chain, so offering a chain-B residue as a bare number hands the
    user a value that will be re-read as chain A and dropped again.

    THE SECOND CASE IS THE TEST. A bare 35 is within ±10 of chain A residues
    only, so the union and the first chain give the SAME answer for it and it
    discriminates nothing — asserting on that alone left a revert to the union
    fully green. 505 is the number that separates them: chain B carries
    500..539 all around it, and chain A carries nothing within reach.
    """
    from shared.pdb_preflight import _nearest_clean_residues

    pdb = _asymmetric_pdb()          # A: 1..40, B: 500..539
    nearest = _nearest_clean_residues(pdb, "A,B", [35], [])
    assert nearest, "expected suggestions near 35 on chain A"
    assert all(isinstance(r, int) for r in nearest), nearest
    assert all(1 <= r <= 40 for r in nearest), nearest

    # Bare 505 lands on chain A, which stops at 40. Chain B's 500..539 sit
    # right next to the number but are unreachable without a prefix, so
    # offering them bare would be handing back values that drop again.
    assert _nearest_clean_residues(pdb, "A,B", [505], []) == [], (
        "a bare hotspot was offered chain-B neighbours as bare numbers; "
        "pasted back, tools/base.py re-reads each one as chain A"
    )
    # Prefixed, the same neighbourhood IS reachable and comes back prefixed.
    prefixed = _nearest_clean_residues(pdb, "A,B", ["B505"], [])
    assert prefixed and all(str(r).startswith("B") for r in prefixed), prefixed


# --- BACKWARD COMPATIBILITY, single chain. This is the load-bearing one -----

def test_single_chain_bare_hotspots_are_unmoved_by_the_attribution_rule():
    """One named chain has nothing to attribute BETWEEN, so the first chain
    IS the union and every answer must be byte-identical to trunk's.

    Payload shape included: bare ints in, bare ints out, no new prefixes.
    """
    from shared.pdb_preflight import _check_hotspots

    pdb = _asymmetric_pdb()          # A: 1..40, B: 500..539
    # Surviving and dropped, both still bare ints, both still on chain A.
    assert _check_hotspots(pdb, "A", [5, 7, 39]) == ([5, 7, 39], [])
    assert _check_hotspots(pdb, "A", [5, 999]) == ([5], [999])
    # A single-chain target naming chain B alone reads B's numbering, not A's.
    assert _check_hotspots(pdb, "B", [520]) == ([520], [])

    inputs, err = boltzgen_mod.validate(_boltzgen_form("A", "5,7,39"), {})
    assert err is None, err
    assert inputs["hotspot_residues"] == [5, 7, 39], "payload shape moved"
    assert inputs["target_chain"] == "A"
    verdict = preflight_for_tool(
        "boltzgen", pdb, target_chain=inputs["target_chain"],
        hotspots=inputs["hotspot_residues"],
    )
    assert verdict.ok
    assert verdict.kind is VerdictKind.READY
    assert verdict.hotspot_status == {"surviving": [5, 7, 39], "dropped": []}
    payload = boltzgen_mod.build_payload(inputs, "https://example/t.pdb")
    assert payload["hotspot_residues"] == [5, 7, 39]


def test_single_chain_suggestions_are_still_bare_ints():
    """The other half of the single-chain floor: the refusal's suggestion list
    keeps its old shape, so nothing the user pastes back changes form."""
    from shared.pdb_preflight import _nearest_clean_residues

    nearest = _nearest_clean_residues(_asymmetric_pdb(), "A", [35], [])
    assert nearest and all(isinstance(r, int) for r in nearest), nearest


def test_an_empty_hotspot_list_is_still_fine_for_the_optional_tools():
    """The new refusal must fire on a DROPPED hotspot, never on the absence of
    one — boltzgen and proteina run an open search legitimately."""
    for slug in ("boltzgen", "proteina"):
        verdict = preflight_for_tool(
            slug, _asymmetric_pdb(), target_chain="A,B", hotspots=[],
        )
        assert verdict.ok, f"{slug}: {verdict.reason}"
        assert verdict.hotspot_status == {"surviving": [], "dropped": []}


# --- split_hotspot, the one parser all of the above now share ---------------

@pytest.mark.parametrize("token,chains,expected", [
    (296,           ["A", "B"], (None, 296)),   # already an int
    ("296",         ["A", "B"], (None, 296)),   # bare, unattributed
    ("  296  ",     ["A", "B"], (None, 296)),
    ("A296",        ["A", "B"], ("A", 296)),
    ("B264",        ["A", "B"], ("B", 264)),
    ("-5",          ["A"],      (None, -5)),    # author numbering allows it
    ("C25",         ["A", "B"], (None, None)),  # names an untargeted chain
    ("xyz",         ["A", "B"], (None, None)),
    ("Axx",         ["A"],      (None, None)),
    ("",            ["A"],      (None, None)),
    ("A296",        None,       (None, None)),  # no chain list -> bare only
    ("AB12",        ["A", "AB"], ("AB", 12)),   # longest match wins
    (True,          ["A"],      (None, None)),  # bool is an int subclass
    # int() truncated a float; a JSON body sending 296.0 for residue 296 is
    # the shape that reaches this, and it was in range before the contract
    # changed. R1 covers the wire types too, not just the happy one.
    (296.0,         ["A", "B"], (None, 296)),
    (296.7,         ["A"],      (None, 296)),
])
def test_split_hotspot(token, chains, expected):
    from shared.pdb_inspect import split_hotspot

    assert split_hotspot(token, chains) == expected


def test_the_three_hotspot_validators_give_the_same_answer():
    """There are three independent implementations of "is this hotspot on this
    target": shared/targets.py for the campaign and target-launch routes,
    shared/pdb_inspect.py::validate_hotspots for atomic submit and reuse, and
    shared/pdb_preflight.py::_check_hotspots for the hard gate.

    They have disagreed before — the A18 defect was validate_hotspots calling
    every hotspot out of range on a multi-chain target while targets.py
    accepted them, so the same job passed one route and failed another. Naming
    one of them in a test and trusting the others to follow is how that
    survived, so drive all three off the same inputs here.
    """
    import uuid

    from shared.pdb_inspect import inspect_pdb_bytes, validate_hotspots
    from shared.targets import DesignTarget

    pdb = _asymmetric_pdb()          # A: 1..40, B: 500..539
    target = DesignTarget(
        id=str(uuid.uuid4()), user_id="u-1", kind="pdb",
        chain_summary={"chains": [
            {"chain_id": "A", "min_resnum": 1, "max_resnum": 40},
            {"chain_id": "B", "min_resnum": 500, "max_resnum": 539},
        ]},
    )
    report = inspect_pdb_bytes(pdb)

    def _all_three(hotspots):
        """(targets_ok, inspect_ok, preflight_ok) for one hotspot list."""
        in_range, out_of_range = validate_hotspots(report, "A,B", hotspots)
        verdict = preflight_for_tool(
            "rfdiffusion", pdb, target_chain="A,B", hotspots=hotspots,
            binder_max_aa=65, num_designs=2,
        )
        return (
            target.hotspot_error("A,B", hotspots) is None,
            not out_of_range,
            not verdict.hotspot_status["dropped"],
        )

    # Valid on the chains they name.
    assert _all_three(["A5", "B505"]) == (True, True, True)
    # R2: B5 does not exist on chain B, and chain A having a residue 5 must
    # not rescue it in ANY of the three.
    assert _all_three(["B5"]) == (False, False, False)
    # R1: a BARE number is judged against the FIRST named chain, in all three.
    #
    # This line used to read `_all_three([5, 505]) == (True, True, True)` and
    # called the union "the pre-multi-chain shape, unchanged". The first half
    # was true and the second was not: before multi-chain there was only ever
    # one named chain, so "the union" and "the first chain" were the same
    # sentence, and generalising to the union picked a rule no consumer
    # implements. tools/base.py:108 sends a bare 505 as "A505"; proteina's
    # _parse_hotspots sends it as contig_chains[0] + 505. 505 exists on chain B
    # alone, so all three used to green-light a token that addresses chain A —
    # which runs 1..40 — and the run was funded and then died in the container.
    assert _all_three([5]) == (True, True, True)          # 5 IS on chain A
    assert _all_three([505]) == (False, False, False)     # 505 is on B only
    # A bare number on neither chain still fails everywhere.
    assert _all_three([9000]) == (False, False, False)


def test_validate_hotspots_keeps_the_bare_int_contract():
    """The R1 floor for validate_hotspots specifically: bare ints come back as
    ints, exactly as before the contract changed. A wholesale revert of this
    function to its int()-only body passes every other test in this file,
    because everything else reaches it through preflight rather than calling it.

    The SHAPE is the floor being pinned here, and it has not moved. What moved
    is the multi-chain reading: a bare number is range-checked against the
    first named chain rather than the union, because that is the chain it will
    be sent as. Single-chain callers — every caller that predates multi-chain —
    cannot tell the difference.
    """
    from shared.pdb_inspect import inspect_pdb_bytes, validate_hotspots

    report = inspect_pdb_bytes(_asymmetric_pdb())   # A: 1..40, B: 500..539

    in_range, out_of_range = validate_hotspots(report, "A,B", [5, 505, 9000])
    assert in_range == [5]
    assert out_of_range == [505, 9000], (
        "505 lives on chain B alone; a bare token is shipped against chain A"
    )
    assert all(isinstance(h, int) for h in in_range), in_range

    # Single chain, the shape every pre-#109 caller posts.
    in_range, out_of_range = validate_hotspots(report, "A", [5, 505])
    assert in_range == [5]
    assert out_of_range == [505]

    # Prefixed tokens echo back as typed, so the message can name the chain.
    in_range, out_of_range = validate_hotspots(report, "A,B", ["A5", "B5"])
    assert in_range == ["A5"]
    assert out_of_range == ["B5"]


def test_a_gap_is_only_near_a_hotspot_on_its_own_chain():
    """The gap-distance math decides whether an internal gap is a hard fail.

    THE FIXTURE IS THE TEST. An earlier version of this used a heterodimer
    with chain B numbered from 500, so even a chain-BLIND minimum measured
    A5 against B's gap as ~506 and the "far" assertion (> 400) passed anyway:
    the numbering offset, not the chain routing, was carrying it. A
    homodimer is the only fixture that can tell them apart, and a homodimer
    is the case that matters — both Fc protomers carry the same numbering,
    so a chain-blind minimum reports a gap on B as sitting 4 residues from a
    hotspot the user placed on A, and refuses the submit.
    """
    import math

    from shared.pdb_preflight import _check_internal_gaps
    from shared.pdb_preflight_rules import TOOL_RULES

    # Identical numbering on both protomers. A is complete; B is missing
    # 41..100. A45 falls INSIDE that range numerically, so a chain-blind
    # minimum scores it 4 residues from the gap while the truth is that it
    # sits on the other protomer entirely.
    lines = ["HEADER    SYNTHETIC HOMODIMER\n"]
    serial = 0
    for ci, (cid, resnums) in enumerate((
        ("A", list(range(1, 121))),
        ("B", list(range(1, 41)) + list(range(101, 121))),
    )):
        for i, resnum in enumerate(resnums):
            for aname, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 2.0)]:
                serial += 1
                lines.append(_atom_line(
                    serial=serial, name=aname, resname="ALA", chain=cid,
                    resnum=resnum, x=i * 4.0 + off,
                    y=1.0 if aname != "O" else 2.0, z=1.0 + 50.0 * ci,
                ))
    lines.append("END\n")
    pdb = "".join(lines).encode()

    rules = TOOL_RULES["pxdesign"]
    near = _check_internal_gaps(pdb, "A,B", ["B45"], rules)
    far = _check_internal_gaps(pdb, "A,B", ["A45"], rules)

    assert near.gaps and far.gaps, "both runs must see the same gap on B"
    assert near.gaps[0].nearest_hotspot_distance == 4, (
        f"B45 is 4 residues from the gap on its own chain, got "
        f"{near.gaps[0].nearest_hotspot_distance}"
    )
    # inf, not "some large number" — a chain-blind union scores this 4, and
    # any threshold-based assertion would pass for the wrong reason.
    assert far.gaps[0].nearest_hotspot_distance == math.inf, (
        "A45 is on the other protomer; a gap on chain B must not be measured "
        f"against it at all, got {far.gaps[0].nearest_hotspot_distance}"
    )
    # And the verdict, not just the distance: this is a hard submit gate.
    assert near.hard_fail_message and not far.hard_fail_message, (
        f"near={near.hard_fail_message!r} far={far.hard_fail_message!r}"
    )
    # R1: a BARE hotspot keeps measuring against every chain, as it always did.
    bare = _check_internal_gaps(pdb, "A,B", [45], rules)
    assert bare.gaps[0].nearest_hotspot_distance == 4


def test_nearest_clean_residues_keeps_the_bare_int_contract():
    """R1 for the suggestion list: bare input returns bare ints, ordered by
    distance then resnum. Callers render these straight into the refusal, so
    both the type and the order are observable."""
    from shared.pdb_preflight import _nearest_clean_residues

    pdb = _asymmetric_pdb()          # A: 1..40, B: 500..539
    nearest = _nearest_clean_residues(pdb, "A,B", [20], [])
    assert nearest == [19, 21, 18, 22, 17, 23], nearest
    assert all(isinstance(r, int) for r in nearest), nearest


def test_suggestions_do_not_offer_the_same_residue_twice():
    """A dropped list holding both forms reaches a residue by two routes and
    used to render both labels — "19, A22, 18, A19, 22" is three residues
    dressed as five, in two formats, inside a rejection message."""
    from shared.pdb_preflight import _nearest_clean_residues

    pdb = _asymmetric_pdb()          # A: 1..40, B: 500..539
    nearest = _nearest_clean_residues(pdb, "A", [20, "A21"], [])
    resnums = [int(str(r).lstrip("A")) for r in nearest]
    assert len(resnums) == len(set(resnums)), (
        f"same residue offered twice under two labels: {nearest!r}"
    )


def test_every_dropped_chain_gets_at_least_one_suggestion():
    """One global top-N starves a protomer.

    THE FIXTURE IS THE TEST, again. An earlier version gave chain B a clean
    residue at distance 1, which TIES chain A's best — so a global top-N
    still surfaced it at index 2 and the mutation this test exists to kill
    survived. Chain B's nearest must be strictly worse than chain A's sixth,
    or ranking and round-robin produce the same chain set.

    Here A offers six candidates at distances 1,1,2,2,3,3 and B's nearest is
    5 away, so a global top-N fills every slot from A and the user is told
    nothing at all about their chain B hotspot.
    """
    from shared.pdb_preflight import _nearest_clean_residues

    lines = ["HEADER    SYNTHETIC STARVE\n"]
    serial = 0
    for ci, (cid, resnums) in enumerate((
        ("A", list(range(1, 41))),          # dense around the dropped A20
        ("B", list(range(510, 551))),       # nearest to dropped B505 is 5 away
    )):
        for i, resnum in enumerate(resnums):
            for aname, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 2.0)]:
                serial += 1
                lines.append(_atom_line(
                    serial=serial, name=aname, resname="ALA", chain=cid,
                    resnum=resnum, x=i * 4.0 + off,
                    y=1.0 if aname != "O" else 2.0, z=1.0 + 50.0 * ci,
                ))
    lines.append("END\n")
    pdb = "".join(lines).encode()

    nearest = _nearest_clean_residues(pdb, "A,B", ["A20", "B505"], [])
    chains = {str(r)[0] for r in nearest}
    assert chains == {"A", "B"}, (
        f"only chain(s) {chains} got suggestions: {nearest!r} — the other "
        f"dropped hotspot is unaddressed"
    )


def test_a_bare_suggestion_is_not_deleted_by_a_namesake_on_another_chain():
    """The de-dup that stops "19, A19" being offered as two residues must not
    fire across protomers, where 19 and B19 really are different residues.

    Keyed on the number alone, a single dropped "B21" deleted A19, A18 and
    A22 — the nearest clean neighbours of the bare hotspot 20 — leaving a
    residue ten away as the closest thing the user was offered.
    """
    from shared.pdb_preflight import _nearest_clean_residues

    # Homodimer, identical numbering, 20 and 21 broken on BOTH protomers.
    lines = ["HEADER    SYNTHETIC HOMODIMER DEDUP\n"]
    serial = 0
    for ci, cid in enumerate(("A", "B")):
        for i, resnum in enumerate(range(1, 61)):
            atoms = ([("N", 0.0), ("CA", 1.0)] if resnum in (20, 21)
                     else [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 2.0)])
            for aname, off in atoms:
                serial += 1
                lines.append(_atom_line(
                    serial=serial, name=aname, resname="ALA", chain=cid,
                    resnum=resnum, x=i * 4.0 + off,
                    y=1.0 if aname != "O" else 2.0, z=1.0 + 50.0 * ci,
                ))
    lines.append("END\n")
    pdb = "".join(lines).encode()

    nearest = _nearest_clean_residues(pdb, "A,B", [20, "B21"], [])
    bare = [r for r in nearest if isinstance(r, int)]
    assert bare, f"the bare hotspot 20 got no suggestions at all: {nearest!r}"
    assert min(abs(r - 20) for r in bare) <= 2, (
        f"nearest bare suggestion is {bare!r}, but 19/18/22 are clean on "
        f"chain A — deleted because chain B happens to share the numbers"
    )


# --- the R1 type contract, on STRING input ------------------------------
#
# The three "bare hotspots stay ints" assertions above all pass ints IN, so
# `.append(h)` and `.append(n)` are indistinguishable and an echo mutation
# survives in every validator. The shapes that actually reach these functions
# include strings — a reuse token, a JSON body, a form value — and echoing
# them back puts "5" where an int was promised, into a serialised
# hotspot_status the browser and the job row both read.

@pytest.mark.parametrize("raw,expected_in,expected_out", [
    (["5"], [5], []),
    ([" 5 "], [5], []),
    ([5], [5], []),
    (["9000"], [], [9000]),
    ([9000], [], [9000]),
])
def test_bare_hotspots_normalise_to_int_whatever_their_input_type(
    raw, expected_in, expected_out,
):
    from shared.pdb_inspect import inspect_pdb_bytes, validate_hotspots

    report = inspect_pdb_bytes(_asymmetric_pdb())   # A: 1..40, B: 500..539
    in_range, out_of_range = validate_hotspots(report, "A,B", raw)
    assert in_range == expected_in
    assert out_of_range == expected_out
    assert all(isinstance(h, int) for h in in_range + out_of_range), (
        f"{raw!r} echoed its input type back instead of normalising: "
        f"{in_range!r} / {out_of_range!r}"
    )


@pytest.mark.parametrize("raw,surviving,dropped", [
    (["5"], [5], []),
    (["9000"], [], [9000]),
])
def test_preflight_hotspot_status_is_int_typed_for_bare_input(
    raw, surviving, dropped,
):
    """hotspot_status is serialised into the panel JSON and stamped onto the
    job row, so its element type is a wire contract, not an internal detail."""
    verdict = preflight_for_tool(
        "rfdiffusion", _asymmetric_pdb(), target_chain="A,B",
        hotspots=raw, binder_max_aa=65, num_designs=2,
    )
    assert verdict.hotspot_status["surviving"] == surviving
    assert verdict.hotspot_status["dropped"] == dropped
    assert all(
        isinstance(h, int)
        for h in verdict.hotspot_status["surviving"]
        + verdict.hotspot_status["dropped"]
    ), verdict.hotspot_status


def test_targets_hotspot_error_normalises_and_reports_unparseable_tokens():
    """The third validator, including the arm the three-validator test does
    not reach: a token that parses to nothing at all."""
    import uuid

    from shared.targets import DesignTarget

    target = DesignTarget(
        id=str(uuid.uuid4()), user_id="u-1", kind="pdb",
        chain_summary={"chains": [
            {"chain_id": "A", "min_resnum": 1, "max_resnum": 40},
            {"chain_id": "B", "min_resnum": 500, "max_resnum": 539},
        ]},
    )
    assert target.hotspot_error("A,B", ["5"]) is None
    err = target.hotspot_error("A,B", ["xyz"])
    assert err and "xyz" in err, err
    # 9000 is on neither chain and must be echoed as a number, not as "9000".
    err = target.hotspot_error("A,B", ["9000"])
    assert err and "'9000'" not in err and "9000" in err, err


def test_split_hotspot_prefers_the_longest_matching_chain_id():
    """Documented as "longest match wins", and the AB12 row above cannot see
    it: with chains ["A", "AB"] a first-match-wins parser reading "A" first
    returns (None, None) because "B12" is not an int, so BOTH orderings
    happen to agree there. Only a case where the short prefix leaves a valid
    integer behind can tell them apart."""
    from shared.pdb_inspect import split_hotspot

    # "A12" is a valid parse under chain "A"; "AB12" must still win for AB.
    assert split_hotspot("AB12", ["A", "AB"]) == ("AB", 12)
    assert split_hotspot("AB12", ["AB", "A"]) == ("AB", 12)
    # And the short id still resolves when it is the only match.
    assert split_hotspot("A12", ["A", "AB"]) == ("A", 12)


def test_nearest_suggestions_come_from_the_hotspots_own_chain():
    """A prefixed dropped hotspot must not be offered neighbours that exist
    only on the other protomer.

    THE FIXTURE IS THE TEST. On a target whose second chain is numbered far
    away, `pool = union` and the per-chain pool give the same answer, because
    nothing on the other chain falls inside the window — so the assertion
    passes for a parser that ignores the chain entirely. Chain B has to carry
    residues NEAR the dropped number for the two to differ, and then the
    union offers "A46".."A55": labels built from the hotspot's chain over
    residues that exist only on B, i.e. suggestions the user cannot use.
    """
    from shared.pdb_preflight import _nearest_clean_residues

    lines = ["HEADER    SYNTHETIC OVERLAP\n"]
    serial = 0
    for ci, (cid, resnums) in enumerate((
        ("A", list(range(1, 41))),      # stops at 40
        ("B", list(range(1, 121))),     # covers 41..55, right beside A45
    )):
        for i, resnum in enumerate(resnums):
            for aname, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 2.0)]:
                serial += 1
                lines.append(_atom_line(
                    serial=serial, name=aname, resname="ALA", chain=cid,
                    resnum=resnum, x=i * 4.0 + off,
                    y=1.0 if aname != "O" else 2.0, z=1.0 + 50.0 * ci,
                ))
    lines.append("END\n")
    pdb = "".join(lines).encode()

    nearest = _nearest_clean_residues(pdb, "A,B", ["A45"], [])
    assert nearest, "expected suggestions near A45 on chain A"
    assert all(str(r).startswith("A") for r in nearest), nearest
    assert all(int(str(r)[1:]) <= 40 for r in nearest), (
        f"suggestions for A45 must exist on chain A, which stops at 40: "
        f"{nearest!r}"
    )


# ===========================================================================
# THE GATE MUST JUDGE THE TOKEN THAT SHIPS  (P0-1)
# ===========================================================================
#
# proteina's `_parse_hotspots` emits two representations of one input:
# `hotspot_spec` (["B520"], what the container matches on) and
# `hotspot_residues` ([520], bare, kept so the pre-multi-chain range checks
# kept compiling). Every gate read the bare one, and the bare one cannot say
# whether the letter was never typed or was stripped:
#
#     typed "B520"  ->  spec ["B520"]  bare [520]   must RUN
#     typed "520"   ->  spec ["A520"]  bare [520]   must be REFUSED
#
# Same bare list, opposite correct verdicts. Under the first-chain rule the
# gates read [520] as chain A and refused BOTH -- a false refusal of the
# canonical multi-chain case, on a paid path.

def _proteina_form(hotspots: str, contig: str = "A1-40,B500-539") -> dict:
    """What the proteina launch form posts for a custom two-chain target."""
    return {
        "preset": "protein_binder",
        "target_input": contig,
        "hotspot_residues": hotspots,
        "binder_length_min": "60",
        "binder_length_max": "80",
        "_has_custom_target": "1",
    }


_ASYM_SUMMARY = {
    "chains": [
        {"chain_id": "A", "standard_residue_count": 40, "hetatm_resnames": [],
         "water_count": 0, "min_resnum": 1, "max_resnum": 40},
        {"chain_id": "B", "standard_residue_count": 40, "hetatm_resnames": [],
         "water_count": 0, "min_resnum": 500, "max_resnum": 539},
    ],
}


@pytest.mark.parametrize("typed,expected_spec", [
    ("B520", ["B520"]),                    # the canonical single-chain-B pick
    ("A20 B520", ["A20", "B520"]),         # one hotspot per chain
    ("A20,B520", ["A20", "B520"]),         # the separator the form also posts
])
def test_a_chain_prefixed_proteina_hotspot_clears_every_money_gate(
    typed, expected_spec,
):
    """RED on a492b71 at all three gates. The user typed the chain letter, the
    letter is correct, the token that ships carries it -- and the run was
    refused because the field the gates read had already dropped it.
    """
    import uuid as _uuid

    from shared.pdb_preflight import shipped_hotspots
    from shared.targets import DesignTarget
    from tools import proteina as proteina_mod

    inputs, err = proteina_mod.adapter.validate(_proteina_form(typed), {})
    assert err is None, err
    assert inputs["hotspot_spec"] == expected_spec
    # The precondition that makes this test worth having: the bare copy really
    # is lossy, so a gate reading it cannot get this right by accident.
    assert inputs["hotspot_residues"] == [int(t[1:]) for t in expected_spec], (
        "hotspot_residues stopped being the stripped copy; re-read this test"
    )

    gate_tokens = shipped_hotspots(inputs)
    assert gate_tokens == expected_spec, (
        f"the gates would judge {gate_tokens!r}, but build_payload ships "
        f"{inputs['hotspot_spec']!r}"
    )

    # Gate 1 + 2 -- POST /campaigns and POST /targets/<id>/launch both call this.
    target = DesignTarget(
        id=str(_uuid.uuid4()), user_id="u-1", chain_summary=_ASYM_SUMMARY,
    )
    assert target.hotspot_error("A B", gate_tokens) is None, (
        target.hotspot_error("A B", gate_tokens)
    )

    # Gate 3 -- the atomic submit route's hard preflight.
    verdict = preflight_for_tool(
        "proteina", _asymmetric_pdb(), target_chain=inputs["target_chain"],
        hotspots=gate_tokens, binder_max_aa=80, num_designs=2,
    )
    assert verdict.ok, verdict.reason
    assert verdict.hotspot_status["dropped"] == []


def test_the_bare_hotspot_that_lands_off_its_chain_is_still_refused():
    """The A1 defect, which the fix above must not re-open.

    Typed bare on a two-chain contig, 520 is promoted onto chain A by
    proteina's own parser and ships as "A520". Chain A runs 1..40, so the run
    cannot succeed and must not be funded -- by either the range gate or the
    preflight, and for a hotspots-OPTIONAL tool as much as a required one.
    """
    import uuid as _uuid

    from shared.pdb_preflight import shipped_hotspots
    from shared.targets import DesignTarget
    from tools import proteina as proteina_mod

    inputs, err = proteina_mod.adapter.validate(_proteina_form("520"), {})
    assert err is None, err
    assert inputs["hotspot_spec"] == ["A520"], (
        "proteina no longer promotes a bare hotspot onto the first contig "
        "chain; the premise of this test has moved"
    )

    gate_tokens = shipped_hotspots(inputs)
    target = DesignTarget(
        id=str(_uuid.uuid4()), user_id="u-1", chain_summary=_ASYM_SUMMARY,
    )
    range_err = target.hotspot_error("A B", gate_tokens)
    assert range_err and "A520" in range_err, range_err

    for slug in ("proteina", "boltzgen"):
        verdict = preflight_for_tool(
            slug, _asymmetric_pdb(), target_chain="A B",
            hotspots=gate_tokens, binder_max_aa=80, num_designs=2,
        )
        assert not verdict.ok, (
            f"{slug}: funded a run whose only hotspot ships as A520 against a "
            f"chain that stops at 40"
        )


def test_shipped_hotspots_prefers_the_spec_and_is_a_no_op_without_one():
    """The precedence rule, and the half of it that protects every other tool.

    Only proteina emits `hotspot_spec`. Every other adapter's
    `hotspot_residues` is ALREADY the shipped token, so the helper must fall
    through untouched rather than assume the key exists.
    """
    from shared.pdb_preflight import shipped_hotspots

    # proteina: spec wins outright, exactly as the container's own parser does
    # (tools/proteina/run_pipeline.py falls back to hotspot_residues only when
    # the spec yields no tokens).
    assert shipped_hotspots(
        {"hotspot_spec": ["B520"], "hotspot_residues": [520]}
    ) == ["B520"]
    # No spec -> the bare list, unchanged and unwrapped.
    assert shipped_hotspots({"hotspot_residues": [42, 88]}) == [42, 88]
    assert shipped_hotspots({"hotspot_residues": ["A5", "B7"]}) == ["A5", "B7"]
    # An EMPTY spec is not a spec. proteina emits [] for an open search, and
    # falling through to the bare list there is what keeps a campaign replaying
    # its stored params unchanged.
    assert shipped_hotspots({"hotspot_spec": [], "hotspot_residues": [42]}) == [42]
    # iggm's epitope key rides along rather than replacing anything.
    assert shipped_hotspots({"epitope_pdb_resnums": [32, 45]}) == [32, 45]
    assert shipped_hotspots({
        "hotspot_spec": ["B520"], "hotspot_residues": [520],
        "epitope_pdb_resnums": [32],
    }) == ["B520", 32]
    # Nothing at all, and a falsy input, are both empty rather than a crash.
    assert shipped_hotspots({}) == []
    assert shipped_hotspots(None) == []


def test_shipped_hotspots_reads_a_string_field_the_way_the_container_does():
    """A plain string is ONE field, not a list of characters.

    `list("B520")` is `["B", "5", "2", "0"]` -- four tokens, none of which
    parses as a residue, so the range gate would refuse an input the container
    accepts. Unreachable from the sole producer today (proteina's
    `_parse_hotspots` emits a list) and fail-CLOSED if it were reached, so this
    is parity work rather than a live refusal -- but the gate exists to judge
    the token that ships, and it cannot do that while reading the field in a
    shape the container never would.
    """
    from shared.pdb_preflight import shipped_hotspots

    assert shipped_hotspots({"hotspot_spec": "B520"}) == ["B520"]
    # Commas and whitespace both separate, the container's rule exactly.
    assert shipped_hotspots({"hotspot_spec": "B520, A12 A13"}) == [
        "B520", "A12", "A13",
    ]
    # The sibling fields go through the same reader -- the character-explosion
    # was never specific to the spec.
    assert shipped_hotspots({"hotspot_residues": "A5,B7"}) == ["A5", "B7"]
    assert shipped_hotspots({"epitope_pdb_resnums": "32 45"}) == ["32", "45"]
    # An empty / whitespace-only string is still "no spec", so precedence is
    # unchanged and the bare list behind it is still consulted.
    assert shipped_hotspots(
        {"hotspot_spec": "  ", "hotspot_residues": [520]}
    ) == [520]
    # The list shape every producer actually emits is untouched.
    assert shipped_hotspots({"hotspot_spec": ["B520"]}) == ["B520"]
    assert shipped_hotspots({"hotspot_residues": [42, 88]}) == [42, 88]


def test_the_container_tokenises_a_string_hotspot_field_the_same_way():
    """The parity claim above, executed rather than cited.

    If proteina's own `_hotspot_tokens` ever stops splitting a string on
    commas-and-whitespace, this is the test that should go red -- before the
    gate starts judging tokens the container will not produce.
    """
    import importlib.util
    from pathlib import Path

    from shared.pdb_preflight import shipped_hotspots

    spec = importlib.util.spec_from_file_location(
        "_proteina_run_pipeline_tokens",
        str(Path(__file__).resolve().parents[1]
            / "tools" / "proteina" / "run_pipeline.py"),
    )
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)

    for raw in ("B520", "B520, A12 A13", "  ", "", "5,7,39"):
        assert shipped_hotspots({"hotspot_spec": raw}) == rp._hotspot_tokens(
            raw, "hotspot_spec"
        ), raw


@pytest.mark.parametrize("name,mod", [
    ("bindcraft", bindcraft_mod),
    ("pxdesign", pxdesign_mod),
    ("rfdiffusion", rfdiffusion_mod),
])
@pytest.mark.parametrize("typed_chain,typed_hot", [
    ("A", "5,7"),        # the pre-multi-chain shape: bare ints in, bare out
    ("A,B", "A5,B7"),
])
def test_every_other_adapter_is_untouched_by_the_spec_preference(
    name, mod, typed_chain, typed_hot,
):
    """The four gates now route through `shipped_hotspots`. For the five tools
    that emit no spec, what they judge must be identical to the field they
    judged before -- same objects, same types, same order."""
    from shared.pdb_preflight import shipped_hotspots

    inputs, err = mod.validate(_form(name, typed_chain, typed_hot), {})
    assert err is None, err
    assert "hotspot_spec" not in inputs, (
        f"{name} started emitting hotspot_spec; the no-op claim needs "
        f"re-checking"
    )
    judged = shipped_hotspots(inputs)
    assert judged == inputs["hotspot_residues"]
    assert [type(h) for h in judged] == [
        type(h) for h in inputs["hotspot_residues"]
    ]


# ===========================================================================
# AN INCOMPLETE BACKBONE IS NOT AN ABSENT RESIDUE  (P0-2)
# ===========================================================================
#
# Preflight drops any hotspot whose residue lacks a complete N/CA/C/O
# backbone. Missing O atoms are routine -- terminal residues, disordered
# loops -- and whether that is fatal is a per-tool fact about the CONTAINER,
# not about `hotspots_required`. Executed against the fixture below:
#
#   normalize_for_boltzgen / _pxdesign  -> renumber_map has no ("A", 30), so
#       boltzgen's run_pipeline raises "not present after structure cleanup"
#       with the GPU already running. Refusing is right.
#   proteina's pdb_ca_residues -> select_residues -> missing_hotspots -> []
#       for "A30". It runs, correctly constrained. Refusing is a false
#       refusal of work trunk did successfully.

def _missing_o_pdb(n_res: int = 40, drop_o_at: int = 30) -> bytes:
    """One chain, 1..n, where `drop_o_at` carries N/CA/C and no O."""
    lines = ["HEADER    SYNTHETIC MISSING O\n"]
    serial = 0
    for i in range(n_res):
        for aname, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 2.0)]:
            if aname == "O" and (i + 1) == drop_o_at:
                continue
            serial += 1
            lines.append(_atom_line(
                serial=serial, name=aname, resname="ALA", chain="A",
                resnum=i + 1, x=i * 4.0 + off,
                y=1.0 if aname != "O" else 2.0, z=1.0,
            ))
    lines.append("END\n")
    return "".join(lines).encode()


def test_the_fixture_really_is_missing_only_an_oxygen():
    """Precondition. If residue 30 were absent outright, or complete, every
    assertion below would pass for the wrong reason."""
    from shared.pdb_preflight import (
        _clean_resnums_by_chain, _present_resnums_by_chain,
    )

    pdb = _missing_o_pdb()
    assert 30 in _present_resnums_by_chain(pdb, "A")["A"], (
        "residue 30 has no usable CA -- the fixture is not the case under test"
    )
    assert 30 not in _clean_resnums_by_chain(pdb, "A")["A"], (
        "residue 30 still has a complete N/CA/C/O backbone -- the fixture "
        "does not exercise the split at all"
    )


def test_an_incomplete_backbone_hotspot_still_runs_on_proteina():
    """RED on a492b71. Single chain, one hotspot, one missing oxygen -- the
    branch refused it, trunk ran it, and proteina's own container accepts it.
    """
    verdict = preflight_for_tool(
        "proteina", _missing_o_pdb(), target_chain="A", hotspots=[30],
        binder_max_aa=80, num_designs=2,
    )
    assert verdict.ok, verdict.reason
    # Still REPORTED as dropped by the cleanup summary -- this is about the
    # verdict, not about hiding the fact from the panel.
    assert verdict.hotspot_status["dropped"] == [30]


def test_proteinas_container_accepts_the_hotspot_the_gate_now_admits():
    """The evidence the verdict above rests on, executed rather than cited.

    proteina's container never runs pipeline_normalize; it selects residues by
    CA. If that ever changes, this test is the one that should go red first --
    before a user pays for the run the gate waved through.
    """
    import importlib.util
    import tempfile
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "_proteina_run_pipeline",
        str(Path(__file__).resolve().parents[1]
            / "tools" / "proteina" / "run_pipeline.py"),
    )
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "target.pdb"
        path.write_bytes(_missing_o_pdb())
        residues, unparsable = rp.pdb_ca_residues(path)

    assert unparsable == 0
    selected = rp.select_residues(residues, [("A", 1, 40)])
    assert ("A", 30) in selected, (
        "proteina's own selection no longer contains residue 30; the gate "
        "must stop admitting it"
    )
    assert rp.missing_hotspots(selected, ["A30"]) == []


def test_boltzgen_still_refuses_an_incomplete_backbone_hotspot():
    """The counter-case, and the reason this is a per-tool rule rather than a
    revert. boltzgen is hotspots-OPTIONAL like proteina, but it runs
    pipeline_normalize in-container, which drops residue 30 -- so its own
    run_pipeline raises after the wallet hold. Refusing at the gate is right.
    """
    verdict = preflight_for_tool(
        "boltzgen", _missing_o_pdb(), target_chain="A", hotspots=[30],
        binder_max_aa=80, num_designs=2,
    )
    assert not verdict.ok
    assert "backbone is incomplete" in (verdict.reason or ""), verdict.reason


def test_the_container_cleanup_boltzgen_runs_really_does_drop_that_residue():
    """The evidence behind hotspot_needs_full_backbone=True for boltzgen.

    shared/pipeline_normalize is this repo's VENDORED copy of the module the
    boltzgen image mounts. It is NOT byte-identical to the sibling's original
    (llm-proteinDesigner/backend/pdb_utils/pipeline_normalize.py), so this
    asserts on the copy that always ships with the repo and
    `test_the_normalizer_the_image_mounts_agrees_with_the_vendored_copy`
    below checks the original whenever that checkout is present. The
    renumber_map is exactly what docker/boltzgen/run_pipeline.py:1083 consults
    before raising "Hotspot residue(s) ... are not present after structure
    cleanup".
    """
    import tempfile
    from pathlib import Path

    from shared.pipeline_normalize import (
        normalize_for_boltzgen, normalize_for_proteina,
    )

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pdb"
        src.write_bytes(_missing_o_pdb())
        report = normalize_for_boltzgen(
            str(src), str(Path(tmp) / "bz.pdb"), target_chain="A",
        )
        assert report.renumber_map, "expected boltzgen to renumber"
        assert ("A", 30) not in report.renumber_map, (
            "boltzgen's cleanup now keeps residue 30; its gate should stop "
            "refusing it"
        )
        # proteina's PRESET of the same module drops it too -- which is why the
        # preflight dry-run files it as dropped, and exactly why the verdict
        # must not be read off that dry-run for proteina: the real proteina
        # container does not run this module at all.
        out = Path(tmp) / "pr.pdb"
        normalize_for_proteina(str(src), str(out), target_chain="A")
        kept = {
            int(line[22:26]) for line in out.read_text().splitlines()
            if line.startswith("ATOM") and line[12:16].strip() == "CA"
        }
        assert 30 not in kept


@pytest.mark.parametrize("slug", ["proteina", "boltzgen", "rfdiffusion"])
def test_a_hotspot_that_is_not_on_the_chain_at_all_is_refused_for_every_tool(
    slug,
):
    """The other half of the split. An absent residue cannot be resolved by
    anything downstream, so it hard-fails whatever the tool's backbone rule and
    whatever `hotspots_required` says.

    Structure is the CLEAN fixture on purpose: rfdiffusion hard-fails on any
    internal gap before it ever reaches the hotspot branch, so running this
    against `_missing_o_pdb` would assert on the gap message instead.
    """
    verdict = preflight_for_tool(
        slug, _asymmetric_pdb(), target_chain="A", hotspots=[9000],
        binder_max_aa=80, num_designs=2,
    )
    assert not verdict.ok
    assert "outside that chain's numbering" in (verdict.reason or ""), (
        verdict.reason
    )
    assert "backbone" not in (verdict.reason or "").lower(), (
        f"blamed a backbone for a residue that was never in the file: "
        f"{verdict.reason!r}"
    )


def test_a_mixed_refusal_names_each_cause_against_its_own_residue():
    """Both causes at once. The old copy printed one sentence covering both
    with an "either/or", which tells a user neither which residue to re-pick
    nor which structure to repair."""
    verdict = preflight_for_tool(
        "pxdesign", _missing_o_pdb(), target_chain="A", hotspots=[30, 9000],
        binder_max_aa=80, num_designs=2,
    )
    assert not verdict.ok
    reason = verdict.reason or ""
    assert "outside that chain's numbering" in reason, reason
    assert "backbone is incomplete" in reason, reason
    # 9000 is named by the absent clause and 30 by the backbone clause, not
    # the other way round.
    assert reason.index("9000") < reason.index("outside that chain's"), reason
    assert reason.index("outside that chain's") < reason.index("30 is on"), (
        reason
    )
    assert reason.index("30 is on") < reason.index("backbone is incomplete"), (
        reason
    )


def test_the_suggestion_list_names_only_the_chains_it_searched():
    """`_nearest_clean_residues` looks on the chain each dropped hotspot lands
    on. A bare number lands on the first named chain only, so labelling its
    suggestions "chain(s) A, B" claims a search of B that never happened."""
    verdict = preflight_for_tool(
        "boltzgen", _asymmetric_pdb(), target_chain="A,B", hotspots=[9000],
        binder_max_aa=80, num_designs=2,
    )
    fix = verdict.suggested_fix or ""
    assert "on chain(s) A:" in fix, fix
    assert "chain(s) A, B:" not in fix, fix
    # A prefixed hotspot on B labels B, and only B.
    verdict_b = preflight_for_tool(
        "boltzgen", _asymmetric_pdb(), target_chain="A,B", hotspots=["B9000"],
        binder_max_aa=80, num_designs=2,
    )
    assert "on chain(s) B:" in (verdict_b.suggested_fix or ""), (
        verdict_b.suggested_fix
    )


def test_both_submit_side_gates_ask_for_the_shipped_token():
    """The two gates in blueprints/tools.py that no route test can reach here.

    `POST /tools/<slug>/submit` needs a real upload, a wallet hold and a job
    row, so the campaign and launch routes are the ones pinned end to end
    above. This asserts the remaining two call sites read the SAME field those
    do, because the failure mode is silent: `inputs["hotspot_residues"]` is a
    perfectly good expression that simply judges the wrong token, and proteina
    is the only adapter for which the two differ.

    Same shape as test_proteina_smoke's "production asks the predicate instead
    of restating it".
    """
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "blueprints" / "tools.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    def _is_shipped_call(node) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "shipped_hotspots"
        )

    # 1. the hard preflight on the atomic submit route
    assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "preflight_hotspots"
            for t in n.targets
        )
    ]
    assert len(assigns) == 1, (
        f"expected exactly one preflight_hotspots assignment, found "
        f"{len(assigns)}"
    )
    assert _is_shipped_call(assigns[0].value), (
        "the submit gate stopped reading shipped_hotspots(inputs); a "
        "chain-prefixed proteina hotspot is refused again at "
        f"line {assigns[0].lineno}"
    )

    # 2. the reuse-token path, which runs validate_hotspots AND the preflight
    reuse = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_verify_reuse_pdb_bytes"
    ]
    assert len(reuse) == 1, f"expected one reuse gate, found {len(reuse)}"
    hotspot_kw = [k for k in reuse[0].keywords if k.arg == "hotspots"]
    assert hotspot_kw, "the reuse gate stopped passing hotspots at all"
    assert _is_shipped_call(hotspot_kw[0].value), (
        "the reuse gate stopped reading shipped_hotspots(inputs), so a "
        "resampled proteina job is range-checked on the stripped copy"
    )


def test_an_unparseable_hotspot_token_is_refused_by_the_optional_tools_too():
    """A token that parses to no residue at all belongs with the ABSENT half.

    The adapters' own regexes reject garbage at validate(), so this arrives
    only from a stored campaign param, a reuse token or a crafted POST -- and
    those are exactly the paths that skip validate(). Filing it as "backbone
    incomplete" would hand it proteina's benign verdict and fund a run whose
    hotspot addresses nothing.
    """
    for slug in ("proteina", "boltzgen", "pxdesign"):
        verdict = preflight_for_tool(
            slug, _missing_o_pdb(), target_chain="A", hotspots=["xyz"],
            binder_max_aa=80, num_designs=2,
        )
        assert not verdict.ok, f"{slug}: funded a run whose hotspot is 'xyz'"
        assert verdict.hotspot_status["dropped"] == ["xyz"]


def _origin_placeholder_pdb(n_res: int = 40, at: int = 30) -> bytes:
    """One chain, 1..n, where `at` has a COMPLETE N/CA/C/O backbone whose atoms
    all sit at 0,0,0 -- the placeholder convention for an unresolved residue."""
    lines = ["HEADER    SYNTHETIC ORIGIN\n"]
    serial = 0
    for i in range(n_res):
        placeholder = (i + 1) == at
        for aname, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 2.0)]:
            serial += 1
            lines.append(_atom_line(
                serial=serial, name=aname, resname="ALA", chain="A",
                resnum=i + 1,
                x=0.0 if placeholder else i * 4.0 + off,
                y=0.0 if placeholder else (1.0 if aname != "O" else 2.0),
                z=0.0 if placeholder else 1.0,
            ))
    lines.append("END\n")
    return "".join(lines).encode()


def test_a_residue_parked_at_the_origin_is_refused_but_not_called_absent():
    """The two probes PARTITION the dropped list, so every rule except the
    backbone one has to match between them.

    A residue whose atoms all sit at 0,0,0 is a placeholder. Its backbone is
    complete, so calling it "incomplete" would be a false sentence -- and,
    worse, would hand it proteina's benign verdict and fund a design aimed at
    the coordinate origin. `_clean_resnums_by_chain` already rejects it; the
    presence probe must reject it the same way, even though proteina's own
    container (`pdb_ca_residues`) does not filter coordinates at all.

    THE REFUSAL IS UNCHANGED AND THE SENTENCE IS NOT. This used to be reported
    with the ABSENT wording -- "the chain each one names has no residue with
    that number, so it is outside that chain's numbering" -- about a residue
    the file plainly numbers, and the fix line then told the user to "pick a
    hotspot that exists". Both are false here: the number is fine, the
    coordinates are not, and re-picking a number in the same unresolved stretch
    fails identically.
    """
    from shared.pdb_preflight import (
        _clean_resnums_by_chain, _origin_only_resnums_by_chain,
        _present_resnums_by_chain,
    )

    pdb = _origin_placeholder_pdb()
    assert 30 not in _clean_resnums_by_chain(pdb, "A")["A"]
    assert 30 not in _present_resnums_by_chain(pdb, "A")["A"], (
        "the presence probe kept a residue its sibling dropped for a reason "
        "that has nothing to do with backbone completeness"
    )
    # ...and the residue IS numbered on the chain, which is the whole reason
    # the absent wording was wrong. If this is empty the fixture has stopped
    # exercising the case and every assertion below passes for free.
    assert 30 in _origin_only_resnums_by_chain(pdb, "A")["A"]

    verdict = preflight_for_tool(
        "proteina", pdb, target_chain="A", hotspots=[30],
        binder_max_aa=80, num_designs=2,
    )
    assert not verdict.ok, "funded a design aimed at the coordinate origin"
    assert "backbone is incomplete" not in (verdict.reason or ""), (
        f"called a complete backbone incomplete: {verdict.reason!r}"
    )
    assert "outside that chain's numbering" not in (verdict.reason or ""), (
        f"still blaming the numbering for a residue that is numbered: "
        f"{verdict.reason!r}"
    )
    assert "0,0,0" in (verdict.reason or ""), verdict.reason
    assert "never resolved" in (verdict.reason or ""), verdict.reason
    assert "Pick a hotspot that exists" not in (verdict.suggested_fix or ""), (
        f"sent the user to re-pick a number that was never wrong: "
        f"{verdict.suggested_fix!r}"
    )


def test_an_absent_hotspot_and_an_at_origin_one_do_not_read_the_same():
    """ONE FILE, TWO CAUSES, TWO SENTENCES -- the assertion the wording fix
    exists for.

    Residue 999 is not on chain A at all; residue 30 is numbered there and
    parked at 0,0,0. Both are hard-failed and both always were, so this test
    says nothing about the verdict -- it says the user can tell which of two
    different problems they have, and is sent to the right remedy for it.

    Deliberately asserts the DIFFERENCE first: collapsing the two branches back
    into one string is the regression, and a test that only checked each branch
    against its own substrings would survive a merge that made both emit the
    absent text.
    """
    pdb = _origin_placeholder_pdb()

    absent = preflight_for_tool(
        "proteina", pdb, target_chain="A", hotspots=[999],
        binder_max_aa=80, num_designs=2,
    )
    at_origin = preflight_for_tool(
        "proteina", pdb, target_chain="A", hotspots=[30],
        binder_max_aa=80, num_designs=2,
    )

    # Preconditions: same verdict, so the difference below is about text only.
    assert not absent.ok and not at_origin.ok, (
        "a refusal DECISION moved; this test only governs the wording"
    )
    assert absent.hotspot_status["dropped"] == [999]
    assert at_origin.hotspot_status["dropped"] == [30]

    assert absent.reason != at_origin.reason, (
        "an at-origin hotspot is still reported with the absent wording"
    )
    assert absent.suggested_fix != at_origin.suggested_fix, (
        "both causes are still routed to the same remedy"
    )

    # Each names its own cause, rather than merely differing by the residue
    # number interpolated into one shared sentence.
    assert "outside that chain's numbering" in (absent.reason or "")
    assert "outside that chain's numbering" not in (at_origin.reason or "")
    assert "0,0,0" in (at_origin.reason or "")
    assert "0,0,0" not in (absent.reason or "")

    # And each points somewhere useful: re-pick for a number that isn't there,
    # a different structure for one that is there and unresolved.
    assert "Pick a hotspot that exists" in (absent.suggested_fix or "")
    assert "Pick a hotspot that exists" not in (at_origin.suggested_fix or "")
    assert "resolves them" in (at_origin.suggested_fix or "")


def test_an_at_origin_hotspot_beside_an_absent_one_keeps_both_sentences():
    """The two causes in one request. Each clause is built independently, so
    neither may swallow the other -- a user who typed both gets told about
    both, and the fix line carries both remedies.
    """
    verdict = preflight_for_tool(
        "proteina", _origin_placeholder_pdb(), target_chain="A",
        hotspots=[30, 999], binder_max_aa=80, num_designs=2,
    )
    assert not verdict.ok
    reason = verdict.reason or ""
    assert "outside that chain's numbering" in reason, reason
    assert "0,0,0" in reason, reason
    # Attributed to the right residue on each side, not merged into one list.
    assert "residue(s) 999 can't be used" in reason, reason
    assert "residue(s) 30 can't be used" in reason, reason
    fix = verdict.suggested_fix or ""
    assert "Pick a hotspot that exists" in fix, fix
    assert "Residue(s) 30 are numbered in this file" in fix, fix


# --- BACKWARD COMPATIBILITY, across every PDB-target tool -------------------
#
# The pre-multi-chain shape is `target_chain: "A"` with bare integer hotspots.
# It has to survive validate -> build_payload -> preflight unchanged, for all
# six tools, or this branch is a regression for every job submitted before the
# contract existed. Executed against trunk (1853746), against a492b71 and
# against HEAD: the validate() dict, the payload dict and the full preflight
# verdict -- kind, reason, suggested_fix and hotspot_status -- come back
# identical for all three, per tool.

_BACKCOMPAT_FORMS = {
    "bindcraft": {
        "preset": "pilot", "target_chain": "A", "hotspot_residues": "5,7,39",
        "binder_length_min": "55", "binder_length_max": "65", "num_designs": "2",
    },
    "pxdesign": {
        "preset": "pilot", "target_chain": "A", "hotspot_residues": "5,7,39",
        "binder_length": "80", "num_designs": "2",
    },
    "rfdiffusion": {
        "preset": "pilot", "target_chain": "A", "hotspot_residues": "5,7,39",
        "binder_length_min": "55", "binder_length_max": "65", "num_designs": "2",
    },
    "rfantibody": {
        "preset": "pilot", "target_chain": "A", "hotspot_residues": "5,7,39",
        "num_designs": "2",
    },
    "boltzgen": {
        "preset": "pilot", "target_chain": "A", "hotspot_residues": "5,7,39",
        "binder_length_min": "55", "binder_length_max": "65", "num_designs": "2",
    },
    "proteina": {
        "preset": "protein_binder", "target_chain": "A",
        "hotspot_residues": "5,7,39", "binder_length_min": "60",
        "binder_length_max": "80", "_has_custom_target": "1",
    },
}


def _clean_single_chain_pdb(n_res: int = 60) -> bytes:
    lines = ["HEADER    BACKCOMPAT\n"]
    serial = 0
    for i in range(n_res):
        for aname, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 2.0)]:
            serial += 1
            lines.append(_atom_line(
                serial=serial, name=aname, resname="ALA", chain="A",
                resnum=i + 1, x=i * 4.0 + off,
                y=1.0 if aname != "O" else 2.0, z=1.0,
            ))
    lines.append("END\n")
    return "".join(lines).encode()


@pytest.mark.parametrize("slug", sorted(_BACKCOMPAT_FORMS))
def test_the_pre_multichain_shape_survives_every_tool_unchanged(slug):
    """One chain, bare ints, all six tools: same payload shape, same verdict.

    Deliberately asserts the SAME expected values for every tool rather than
    each tool's own output, because the claim is that they agree -- a
    per-tool expectation would let one drift alone and stay green.
    """
    import uuid as _uuid

    from shared.pdb_preflight import shipped_hotspots
    from shared.targets import DesignTarget

    mod = __import__(f"tools.{slug}", fromlist=["*"])
    validate = getattr(mod, "validate", None) or mod.adapter.validate
    build = getattr(mod, "build_payload", None) or mod.adapter.build_payload

    inputs, err = validate(_BACKCOMPAT_FORMS[slug], {})
    assert err is None, err
    assert inputs["target_chain"] == "A"
    assert inputs["hotspot_residues"] == [5, 7, 39], (
        f"{slug}: bare ints did not survive validate(): "
        f"{inputs['hotspot_residues']!r}"
    )
    assert all(isinstance(h, int) for h in inputs["hotspot_residues"])

    payload = build(inputs, "https://example/t.pdb")
    assert payload["hotspot_residues"] == [5, 7, 39], (
        f"{slug}: the payload the container receives changed shape"
    )

    # The gates read this, and for a single chain it must still range-check
    # green -- proteina's spec is ["A5", "A7", "A39"], which names chain A.
    gate_tokens = shipped_hotspots(inputs)
    target = DesignTarget(
        id=str(_uuid.uuid4()), user_id="u-1",
        chain_summary={"chains": [{
            "chain_id": "A", "standard_residue_count": 60,
            "hetatm_resnames": [], "water_count": 0,
            "min_resnum": 1, "max_resnum": 60,
        }]},
    )
    assert target.hotspot_error("A", gate_tokens) is None, (
        target.hotspot_error("A", gate_tokens)
    )

    verdict = preflight_for_tool(
        slug, _clean_single_chain_pdb(), target_chain="A",
        hotspots=inputs["hotspot_residues"], binder_max_aa=80, num_designs=2,
    )
    assert verdict.kind is VerdictKind.READY, verdict.reason
    assert verdict.reason is None and verdict.suggested_fix is None
    assert verdict.hotspot_status == {
        "surviving": [5, 7, 39], "dropped": [],
    }


def test_the_panel_and_the_submit_gate_agree_on_a_prefixed_hotspot():
    """The other half of the panel/gate agreement already pinned for a bare
    number. The AJAX panel has ALWAYS rebuilt the prefixed token
    (`f"{_cid}{_resnum}"` in blueprints/tools.tool_preflight), so before the
    fix it returned READY for B520 while the submit gate -- reading proteina's
    stripped copy -- refused. Panel green plus gate red is the one divergence
    direction that panel's own comment forbids: Run stays enabled and the user
    is refused on click.
    """
    from shared.pdb_preflight import shipped_hotspots
    from tools import proteina as proteina_mod

    pdb = _asymmetric_pdb()
    panel = preflight_for_tool(
        "proteina", pdb, target_chain="A B", hotspots=["B520"],
    )
    inputs, err = proteina_mod.adapter.validate(_proteina_form("B520"), {})
    assert err is None, err
    gate = preflight_for_tool(
        "proteina", pdb, target_chain=inputs["target_chain"],
        hotspots=shipped_hotspots(inputs),
    )
    assert panel.ok is gate.ok is True, (
        f"panel ok={panel.ok} gate ok={gate.ok} -- a divergence here is the "
        f"defect itself"
    )


def test_the_normalizer_the_image_mounts_agrees_with_the_vendored_copy():
    """The claim under hotspot_needs_full_backbone=True is about the module the
    GPU loads, not the one this repo carries -- and they are not byte-identical.

    tools-hub vendors shared/pipeline_normalize.py; the boltzgen and pxdesign
    images are built from llm-proteinDesigner and mount ITS
    backend/pdb_utils/pipeline_normalize.py. Asserting only on the vendored
    copy would be an argument about a file the GPU never loads. Skipped when
    the sibling checkout is not beside this one, so CI without it stays green.
    """
    import importlib.util
    import sys
    import tempfile
    from pathlib import Path

    # Found by search, not by a fixed number of `..`: this repo is worked on
    # both from its main checkout and from git worktrees at an unrelated
    # depth, and a hardcoded parents[N] silently resolves to nothing in one of
    # them -- which reads as "the sibling is absent" and skips forever.
    tail = Path("llm-proteinDesigner") / "backend" / "pdb_utils" / \
        "pipeline_normalize.py"
    root = Path(__file__).resolve().parents[1]
    candidates = [
        anc / tail for anc in [root, *root.parents]
    ] + [
        anc / "Documents" / "Claude_projects" / tail
        for anc in [root, *root.parents]
    ]
    sibling = next((c for c in candidates if c.exists()), None)
    if sibling is None:
        pytest.skip("llm-proteinDesigner checkout not found next to this repo")

    spec = importlib.util.spec_from_file_location("_sibling_pn", str(sibling))
    pn = importlib.util.module_from_spec(spec)
    # Registered before exec: the module defines dataclasses, and the
    # dataclasses machinery looks its own module up in sys.modules.
    sys.modules["_sibling_pn"] = pn
    try:
        spec.loader.exec_module(pn)

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.pdb"
            src.write_bytes(_missing_o_pdb())
            report = pn.normalize_for_boltzgen(
                str(src), str(Path(tmp) / "out.pdb"), target_chain="A",
            )
        assert report.renumber_map, "expected the shipped normalizer to renumber"
        assert ("A", 30) not in report.renumber_map, (
            "the normalizer the boltzgen image actually mounts now KEEPS a "
            "residue missing only its O — hotspot_needs_full_backbone=True is "
            "no longer true for boltzgen and the gate refuses runs that would "
            "succeed"
        )
        assert report.residues_dropped_per_chain.get("A") == 1
    finally:
        sys.modules.pop("_sibling_pn", None)
