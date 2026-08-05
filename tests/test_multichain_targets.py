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


@pytest.mark.parametrize("name,mod", ADAPTERS)
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
