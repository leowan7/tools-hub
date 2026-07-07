"""Single-job submit chain-existence gate for the PDB-target design tools.

The ``/tools/<tool>/submit`` route (app.py ``tool_submit``) inspects a
freshly-uploaded structure with ``shared.pdb_inspect.inspect_pdb_bytes``
and then rejects a ``target_chain`` that is absent from that structure via
``shared.pdb_inspect.validate_target_chain`` — the same two helpers the
compute-campaign path uses. That gate reads the chain from the *adapter's
validated* ``inputs["target_chain"]``, so a design tool that (a) surfaces
its target chain under that key and (b) accepts a syntactically-valid but
absent chain in ``validate()`` relies on this submit-side seam to catch the
mismatch upfront — instead of a wasted ~30-60 min Modal run that 500s deep
in the pipeline.

``tests/test_mpnn_chain_gate.py`` pins that seam for ProteinMPNN. This file
pins it for the three PDB-target design tools that carry a ``target_chain``
form field: boltz2, pxdesign, rfantibody. Each tool's ``validate()`` names
the field ``target_chain`` (verified per adapter) and defaults it to "A",
so the field is always specified; the gate fires whenever the named chain
is not in the uploaded structure.

Pure-function tests: they exercise the exact submit-side seam
(adapter.validate -> validate_target_chain against inspect_pdb_bytes) with
no Flask app, Supabase, or Modal. This is the same style used by
``test_mpnn_chain_gate.py`` and ``test_reuse_preflight.py``.
"""

from __future__ import annotations

import pytest

from shared.pdb_inspect import inspect_pdb_bytes, validate_target_chain
from tools.boltz2 import validate as boltz2_validate
from tools.pxdesign import validate as pxdesign_validate
from tools.rfantibody import validate as rfantibody_validate


# ---------------------------------------------------------------------------
# Synthetic PDB builder (mirrors test_mpnn_chain_gate / test_reuse_preflight)
# ---------------------------------------------------------------------------

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


# A structure that has ONLY chain A (residues 1..100).
ONLY_A = _chain_pdb("A", list(range(1, 101)))
# A structure that has chains A and B.
A_AND_B = _multi_chain_pdb({"A": list(range(1, 101)), "B": list(range(1, 81))})


# ---------------------------------------------------------------------------
# Per-tool valid form builders. Each surfaces its target chain under the key
# passed in ``target_chain`` — the SAME field name the adapter reads, which
# is what makes the submit-side gate work. Verified against each adapter:
#   boltz2.validate     -> form.get("target_chain"), emits inputs["target_chain"]
#   pxdesign.validate   -> form.get("target_chain"), emits inputs["target_chain"]
#   rfantibody.validate -> form.get("target_chain"), emits inputs["target_chain"]
# ---------------------------------------------------------------------------

def _boltz2_form(target_chain):
    return {
        "preset": "standalone",
        "target_chain": target_chain,
        "binder_sequences": "MKVLAAAAAAAAAAAAAAAAAA",
    }


def _pxdesign_form(target_chain):
    return {
        "preset": "pilot",
        "target_chain": target_chain,
        "hotspot_residues": "10,20",
        "binder_length": "80",
        "num_designs": "8",
    }


def _rfantibody_form(target_chain):
    return {
        "preset": "pilot",
        "target_chain": target_chain,
        "hotspot_residues": "10,20",
        "num_designs": "4",
        "cdr_lengths": "H1:8,H2:7,H3:10-16",
    }


TOOLS = [
    ("boltz2", boltz2_validate, _boltz2_form),
    ("pxdesign", pxdesign_validate, _pxdesign_form),
    ("rfantibody", rfantibody_validate, _rfantibody_form),
]


# ---------------------------------------------------------------------------
# The adapter surfaces the user chain under target_chain (the gate's key).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug,validate,form_for", TOOLS,
                         ids=[t[0] for t in TOOLS])
def test_adapter_emits_target_chain_key(slug, validate, form_for):
    inputs, err = validate(form_for("A"), {})
    assert err is None, f"{slug}: {err}"
    assert inputs is not None
    assert inputs.get("target_chain") == "A"


# ---------------------------------------------------------------------------
# Absent chain -> gate rejects upfront (clean 4xx message), naming the chain.
# This is the case that used to slip through to a Modal 500.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug,validate,form_for", TOOLS,
                         ids=[t[0] for t in TOOLS])
def test_absent_target_chain_rejected_at_gate(slug, validate, form_for):
    # User typed chain Z; the uploaded structure only has chain A.
    inputs, err = validate(form_for("Z"), {})
    assert err is None, f"{slug} adapter unexpectedly rejected syntax: {err}"
    report = inspect_pdb_bytes(ONLY_A, filename="target.pdb")
    assert report.ok
    msg = validate_target_chain(report, inputs["target_chain"])
    assert msg is not None, f"{slug}: absent chain Z was NOT caught"
    assert "Z" in msg
    # The message names the chain(s) actually present so the user can fix it.
    assert "A" in msg


# ---------------------------------------------------------------------------
# Present chain -> gate passes (no false reject of a valid submission).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug,validate,form_for", TOOLS,
                         ids=[t[0] for t in TOOLS])
def test_present_target_chain_passes_gate(slug, validate, form_for):
    inputs, err = validate(form_for("A"), {})
    assert err is None, f"{slug}: {err}"
    report = inspect_pdb_bytes(ONLY_A, filename="target.pdb")
    assert validate_target_chain(report, inputs["target_chain"]) is None


@pytest.mark.parametrize("slug,validate,form_for", TOOLS,
                         ids=[t[0] for t in TOOLS])
def test_present_chain_in_multichain_structure_passes(slug, validate, form_for):
    # Chain B is present in a 2-chain structure -> no false reject.
    inputs, err = validate(form_for("B"), {})
    assert err is None, f"{slug}: {err}"
    report = inspect_pdb_bytes(A_AND_B, filename="target.pdb")
    assert validate_target_chain(report, inputs["target_chain"]) is None
