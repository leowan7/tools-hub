"""MPNN chain-existence gate (item 4).

ProteinMPNN's form field is ``chains_to_design`` (space- OR comma-
separated, e.g. "A B" / "H,L"). The adapter's ``validate()`` normalizes it
to a space-separated string stored under ``inputs["target_chain"]`` -- which
is exactly the key the submit-side upload gate feeds to
``shared.pdb_inspect.validate_target_chain`` (app.py ``tool_submit``). So a
design chain that is absent from the uploaded backbone is rejected upfront,
in parity with the reuse-token path (see ``test_reuse_preflight.py``).

That coverage is *implicit*: it relies on the adapter naming the normalized
chains ``target_chain`` and on ``validate_target_chain`` splitting on
whitespace. These tests pin that seam so a future refactor (renaming the
key, dropping the comma normalization, or narrowing the validator) cannot
silently let a bogus design chain through to a wasted ~30 to 60 s Modal run.

Pure-function tests: no Flask, no Supabase, no Modal.
"""
from __future__ import annotations

from shared.pdb_inspect import inspect_pdb_bytes, validate_target_chain
from tools.mpnn import validate as mpnn_validate


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


def _multi_chain_pdb(chain_residues) -> bytes:
    atoms = []
    for cid, residues in chain_residues.items():
        body = _chain_pdb(cid, residues).decode().splitlines()
        atoms += [ln for ln in body if ln.startswith("ATOM")]
    return ("HEADER    MULTI\n" + "\n".join(atoms) + "\nEND\n").encode()


# ---------------------------------------------------------------------------
# The adapter normalizes chains_to_design (comma or space) into the
# target_chain key the gate reads.
# ---------------------------------------------------------------------------

def test_mpnn_validate_normalizes_comma_chains_to_target_chain():
    inputs, err = mpnn_validate({"chains_to_design": "A,Z"}, {})
    assert err is None
    assert inputs["target_chain"] == "A Z"


def test_mpnn_validate_normalizes_spaced_chains():
    inputs, err = mpnn_validate({"chains_to_design": "H L"}, {})
    assert err is None
    assert inputs["target_chain"] == "H L"


# ---------------------------------------------------------------------------
# The normalized chains are gated against the uploaded backbone: a missing
# design chain is rejected upfront, a present set passes.
# ---------------------------------------------------------------------------

def test_mpnn_missing_design_chain_rejected_at_gate():
    # Design chains A and Z, backbone only has A -> caught upfront, naming Z,
    # instead of a wasted Modal run that fails on the absent chain.
    inputs, err = mpnn_validate({"chains_to_design": "A,Z"}, {})
    assert err is None
    report = inspect_pdb_bytes(
        _chain_pdb("A", list(range(1, 41))), filename="bb.pdb",
    )
    msg = validate_target_chain(report, inputs["target_chain"])
    assert msg is not None
    assert "Z" in msg


def test_mpnn_present_design_chains_pass_gate():
    inputs, err = mpnn_validate({"chains_to_design": "A B"}, {})
    assert err is None
    report = inspect_pdb_bytes(
        _multi_chain_pdb({"A": list(range(1, 41)), "B": list(range(1, 31))}),
        filename="bb.pdb",
    )
    assert validate_target_chain(report, inputs["target_chain"]) is None


def test_mpnn_single_design_chain_present_passes():
    inputs, err = mpnn_validate({"chains_to_design": "A"}, {})
    assert err is None
    report = inspect_pdb_bytes(
        _chain_pdb("A", list(range(1, 41))), filename="bb.pdb",
    )
    assert validate_target_chain(report, inputs["target_chain"]) is None
