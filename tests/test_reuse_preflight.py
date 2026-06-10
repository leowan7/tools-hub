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

import pytest

import app as app_mod


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
