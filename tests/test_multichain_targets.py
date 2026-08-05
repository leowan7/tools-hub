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
def test_build_payload_forwards_the_multichain_shape(name, mod):
    inputs, err = mod.validate(_form(name, "A,B", "A296,B264"), {})
    assert err is None, err
    payload = mod.build_payload(inputs, "https://example.invalid/target.pdb")
    assert payload["target_chain"] == "A,B"
    assert payload["hotspot_residues"] == ["A296", "B264"]
