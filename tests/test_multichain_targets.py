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
from shared.pdb_preflight import _chain_tokens, preflight_for_tool  # noqa: E402
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
    # R1: the pre-multi-chain shape, unioned across both chains, unchanged.
    assert _all_three([5, 505]) == (True, True, True)
    # A bare number on neither chain still fails everywhere.
    assert _all_three([9000]) == (False, False, False)


def test_validate_hotspots_keeps_the_bare_int_contract():
    """The R1 floor for validate_hotspots specifically: bare ints come back as
    ints, in range against the union, exactly as before the contract changed.
    A wholesale revert of this function to its int()-only body passes every
    other test in this file, because everything else reaches it through
    preflight rather than calling it.
    """
    from shared.pdb_inspect import inspect_pdb_bytes, validate_hotspots

    report = inspect_pdb_bytes(_asymmetric_pdb())   # A: 1..40, B: 500..539

    in_range, out_of_range = validate_hotspots(report, "A,B", [5, 505, 9000])
    assert in_range == [5, 505]
    assert out_of_range == [9000]
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
