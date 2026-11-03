"""Re-derive the Fc hotspot readback claimed in RFD4-RUNG4-RESUME-2026-11-02.md.

Reads only committed artefacts: the free CPU probe's result file. Run from the
repo root with the repo venv:

    python -X utf8 -I docs/scoping/rung4-probe/check_hotspot_readback.py
"""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

# What condition_spec_fc.yaml conditions on, as label_seq_id in the input CIF.
SPEC_LABELS = [17, 19, 20, 22, 36, 38, 40, 77]
# The same residues in author/EU numbering; the offset is the input files' own.
AUTH = [241, 243, 244, 246, 260, 262, 264, 301]
OFFSET = 224
# Upstream renumbers the target chain 1-based from its first present residue,
# which is label_seq_id 10, so output = input - 9.
FIRST_PRESENT_LABEL = 10
EXPECTED_COMPS = ["PHE", "PHE", "PRO", "LYS", "THR", "VAL", "VAL", "ARG"]

res = json.loads((HERE / "cpu_result_20261103T103209.json").read_text(encoding="utf-8"))
col = res["condition_columns"]["fc_binder.cif"]["_atom_site.condition_hotspot_1_atom"]

# Entries read "<chain>/<label>/<auth>/<comp>"; sort by the renumbered position.
flagged = sorted((int(e.split("/")[1]), e.split("/")[3]) for e in col["True"])
got_nums = [n for n, _ in flagged]
got_comps = [c for _, c in flagged]

assert [a - OFFSET for a in AUTH] == SPEC_LABELS, "auth-to-label offset does not hold"
remapped = [L - (FIRST_PRESENT_LABEL - 1) for L in SPEC_LABELS]
assert remapped == [8, 10, 11, 13, 27, 29, 31, 68], f"remap drifted: {remapped}"
assert remapped == got_nums, f"readback {got_nums} != intended {remapped}"
assert got_comps == EXPECTED_COMPS, f"residue identities {got_comps} != {EXPECTED_COMPS}"
assert len(col["False"]) and col["False"][0] == "<204 residues>", "target size changed"

print(f"intended (renumbered): {remapped}")
print(f"read back from CIF:    {got_nums} {got_comps}")
print("OK - hotspots landed where intended, by number AND by residue identity")
