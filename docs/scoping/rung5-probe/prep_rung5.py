"""Rung 5 input prep, $0: pull the 8 designed sequences out of rung 4's result JSON.

The designed chain is identified by SEQUENCE IDENTITY, not by length: rung 4's
pdl1_binder_4 is 116 aa against a 115 aa target, so a length rule mis-picks it
(hand-over note, rfd4-scoring-7C-handover.md). The target chain is the one whose
sequence is identical across all eight designs; the binder is the other one.
"""
import json
import sys
from collections import Counter
from pathlib import Path

AA3TO1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q",
    "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K",
    "MET": "M", "PHE": "F", "PRO": "P", "SER": "S", "THR": "T", "TRP": "W",
    "TYR": "Y", "VAL": "V",
}


def chain_seqs(cif_text: str) -> dict[str, str]:
    """One-letter sequence per auth chain, from the atom_site loop's CA rows."""
    lines = cif_text.splitlines()
    i = next(n for n, l in enumerate(lines) if l.startswith("_atom_site."))
    cols = []
    while lines[i].startswith("_atom_site."):
        cols.append(lines[i].strip().split(".", 1)[1])
        i += 1
    idx = {c: n for n, c in enumerate(cols)}
    need = ["label_atom_id", "label_comp_id", "auth_asym_id", "auth_seq_id"]
    for c in need:
        assert c in idx, f"missing _atom_site.{c}"
    out: dict[str, list[tuple[int, str]]] = {}
    for line in lines[i:]:
        if not line.startswith(("ATOM", "HETATM")):
            break
        f = line.split()
        if f[idx["label_atom_id"]] != "CA":
            continue
        ch = f[idx["auth_asym_id"]]
        res = f[idx["label_comp_id"]]
        out.setdefault(ch, []).append((int(f[idx["auth_seq_id"]]), AA3TO1.get(res, "X")))
    return {ch: "".join(a for _, a in sorted(v)) for ch, v in out.items()}


def main(result_json: Path, out_json: Path) -> None:
    files = json.loads(result_json.read_text())["files"]
    cifs = {k: v for k, v in files.items() if k.endswith("_generated.cif")}
    assert len(cifs) == 8, f"expected 8 design CIFs, got {len(cifs)}"

    per_design = {}
    for path, text in sorted(cifs.items()):
        name = Path(path).stem.replace("_generated", "")
        per_design[name] = chain_seqs(text)

    # The target chain: the sequence that appears once in every design.
    counts = Counter(s for seqs in per_design.values() for s in seqs.values())
    shared = [s for s, n in counts.items() if n == len(per_design)]
    assert len(shared) == 1, f"expected exactly one chain shared by all 8, got {len(shared)}"
    target = shared[0]

    designs = []
    for name, seqs in sorted(per_design.items()):
        binders = {ch: s for ch, s in seqs.items() if s != target}
        assert len(binders) == 1, f"{name}: expected 1 non-target chain, got {list(binders)}"
        ch, seq = next(iter(binders.items()))
        assert "X" not in seq, f"{name}: non-standard residue in designed chain"
        designs.append({"name": name, "binder_chain": ch, "binder_seq": seq,
                        "binder_len": len(seq)})

    assert "X" not in target, "non-standard residue in target chain"
    payload = {"target_seq": target, "target_len": len(target), "designs": designs}
    out_json.write_text(json.dumps(payload, indent=2))
    print(f"target {len(target)} aa; binders " +
          ", ".join(f"{d['name'].split('_')[-1]}={d['binder_len']}" for d in designs))
    print(f"wrote {out_json}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
