"""Tests for the reuse-token verification helper (gap 2).

Reuse tokens (job:/handoff:/example:/resample:) stage PDB bytes that skip
the upload-boundary inspection + hard-gate. ``app._verify_reuse_pdb_bytes``
re-runs that gate on the resolved bytes so a mismatch is flagged upfront
instead of crashing 30-60 min into a Modal run. resample: in particular
pipes a fold model's predicted PDB straight into MPNN.

Pure-function tests: import app (no create_app, no Supabase, no Modal).
"""
from __future__ import annotations

from types import SimpleNamespace


import app as app_mod
import blueprints.tools as tools_mod


def _atom_line(serial, name, chain, resnum, x):
    elem = name[0].rjust(2)
    aname = f" {name:<3s}" if len(name) < 4 else name[:4]
    return (
        f"ATOM  {serial:5d} {aname} ALA "
        f"{chain:1s}{resnum:4d}    "
        f"{x:8.3f}{1.0:8.3f}{1.0:8.3f}{1.0:6.2f}{10.0:6.2f}          {elem}\n"
    )


def _chain_pdb(chain_id, residues) -> bytes:
    lines = ["HEADER    SYNTHETIC\n"]
    serial = 0
    for i, rn in enumerate(residues):
        xb = float(i * 4.0)
        for nm, off in [("N", 0.0), ("CA", 1.0), ("C", 2.0), ("O", 3.0)]:
            serial += 1
            lines.append(_atom_line(serial, nm, chain_id, rn, xb + off))
    lines.append("END\n")
    return "".join(lines).encode()


def _two_chain_pdb(a_res, b_res, b_chain="B") -> bytes:
    a = [ln for ln in _chain_pdb("A", a_res).decode().splitlines() if ln.startswith("ATOM")]
    b = [ln for ln in _chain_pdb(b_chain, b_res).decode().splitlines() if ln.startswith("ATOM")]
    return ("HEADER    TWOCHAIN\n" + "\n".join(a + b) + "\nEND\n").encode()


def _adapter(slug):
    return SimpleNamespace(slug=slug)


CLEAN_100 = _chain_pdb("A", list(range(1, 101)))


# ---------------------------------------------------------------------------
# Non-preflight tools (e.g. MPNN — the resample: target): inspection only
# ---------------------------------------------------------------------------

def test_clean_bytes_no_target_chain_pass():
    err = app_mod._verify_reuse_pdb_bytes(
        _adapter("mpnn"), CLEAN_100,
        target_chain="", hotspots=[], filename="predicted.pdb",
    )
    assert err is None


def test_garbage_bytes_rejected():
    err = app_mod._verify_reuse_pdb_bytes(
        _adapter("mpnn"), b"not a pdb at all\n",
        target_chain="", hotspots=[], filename="predicted.pdb",
    )
    assert err is not None


def test_empty_bytes_rejected():
    err = app_mod._verify_reuse_pdb_bytes(
        _adapter("mpnn"), b"",
        target_chain="", hotspots=[], filename="predicted.pdb",
    )
    assert err is not None


def test_mpnn_design_chain_present_passes():
    err = app_mod._verify_reuse_pdb_bytes(
        _adapter("mpnn"), CLEAN_100,
        target_chain="A", hotspots=[], filename="predicted.pdb",
    )
    assert err is None


def test_mpnn_design_chain_absent_rejected():
    # resample chain: user asked MPNN to design chain B but the predicted
    # PDB only has chain A -> caught upfront.
    err = app_mod._verify_reuse_pdb_bytes(
        _adapter("mpnn"), CLEAN_100,
        target_chain="B", hotspots=[], filename="predicted.pdb",
    )
    assert err is not None
    assert "B" in err


# ---------------------------------------------------------------------------
# Binder tools: full hard-gate runs on the reused bytes
# ---------------------------------------------------------------------------

def test_binder_clean_target_passes():
    err = app_mod._verify_reuse_pdb_bytes(
        _adapter("rfdiffusion"), CLEAN_100,
        target_chain="A", hotspots=[40, 60], filename="clone.pdb",
    )
    assert err is None


def test_binder_oversized_target_rejected():
    big = _chain_pdb("A", list(range(1, 551)))   # 550 aa > rfdiffusion cap 500
    err = app_mod._verify_reuse_pdb_bytes(
        _adapter("rfdiffusion"), big,
        target_chain="A", hotspots=[100, 200], filename="clone.pdb",
    )
    assert err is not None
    assert "GPU envelope" in err or "out of memory" in err.lower()


def test_binder_hotspot_out_of_range_rejected():
    err = app_mod._verify_reuse_pdb_bytes(
        _adapter("rfdiffusion"), CLEAN_100,   # residues 1..100
        target_chain="A", hotspots=[500], filename="clone.pdb",
    )
    assert err is not None
    assert "500" in err


# ---------------------------------------------------------------------------
# boltz2 reuse: sequence-position semantics + multi-chain
# ---------------------------------------------------------------------------

def test_boltz2_reuse_seq_position_hotspot_out_of_range_rejected():
    err = app_mod._verify_reuse_pdb_bytes(
        _adapter("boltz2"), CLEAN_100,
        target_chain="A", hotspots=[200], filename="clone.pdb",
    )
    assert err is not None
    assert "200" in err


def test_boltz2_reuse_multi_chain_rejected():
    data = _two_chain_pdb(list(range(1, 101)), list(range(1, 81)))
    err = app_mod._verify_reuse_pdb_bytes(
        _adapter("boltz2"), data,
        target_chain="A", hotspots=[], filename="clone.pdb",
    )
    assert err is not None
    assert "B" in err


def test_boltz2_reuse_clean_single_chain_passes():
    err = app_mod._verify_reuse_pdb_bytes(
        _adapter("boltz2"), CLEAN_100,
        target_chain="A", hotspots=[5, 50], filename="clone.pdb",
    )
    assert err is None


# ---------------------------------------------------------------------------
# Combined-complex binder size derivation (drives boltz2 / pxdesign caps)
# ---------------------------------------------------------------------------

def test_size_params_derive_binder_from_boltz2_sequences():
    bmax, _ = app_mod._parse_preflight_size_params(
        {"binder_sequences": [
            {"name": "a", "sequence": "A" * 120},
            {"name": "b", "sequence": "A" * 250},
        ]}
    )
    assert bmax == 250  # longest binder


def test_size_params_derive_binder_from_pxdesign_length():
    bmax, _ = app_mod._parse_preflight_size_params({"binder_length": "140"})
    assert bmax == 140


def test_size_params_prefer_binder_length_max_when_present():
    bmax, _ = app_mod._parse_preflight_size_params(
        {"binder_length_max": "90", "binder_length": "140"}
    )
    assert bmax == 90  # explicit max wins over the fallbacks


# ---------------------------------------------------------------------------
# Panel parity (gap 3): pxdesign + boltz2 forms now render the rich preflight
# panel. Membership in _PREFLIGHT_PANEL_FORMS is what flips a submit-side
# hard-gate rejection from the plain ``error`` string to the verdict UI, and
# it is what tells the rest of the form to mount preflight.js for live
# feedback. Lock the membership so a future refactor can't silently drop a
# tool back to the plain-error path.
# ---------------------------------------------------------------------------

def test_pxdesign_and_boltz2_are_panel_forms():
    assert "pxdesign" in tools_mod._PREFLIGHT_PANEL_FORMS
    assert "boltz2" in tools_mod._PREFLIGHT_PANEL_FORMS


def test_original_binder_tools_remain_panel_forms():
    for slug in ("rfantibody", "rfdiffusion", "bindcraft", "boltzgen"):
        assert slug in tools_mod._PREFLIGHT_PANEL_FORMS


def test_every_preflight_tool_has_a_panel():
    # The item-3 goal: every PDB tool with a preflight evaluator also gives
    # live panel feedback, so none silently lands on the plain-error path by
    # omission. The fallback branch in tool_submit stays as a defensive net.
    from shared.pdb_preflight import PREFLIGHT_TOOLS

    assert PREFLIGHT_TOOLS <= tools_mod._PREFLIGHT_PANEL_FORMS
