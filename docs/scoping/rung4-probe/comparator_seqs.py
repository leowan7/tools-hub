"""Extract the binder sequence and amino-acid composition of every design in the
recorded `proteina` run on our own target (proteina_direct_out/design_00*.pdb)."""
import glob, sys, collections, json
import gemmi

AA3 = gemmi.ResidueInfo
rows = []
for path in sorted(glob.glob(sys.argv[1] + "/design_0*.pdb")):
    st = gemmi.read_structure(path)
    st.remove_ligands_and_waters(); st.remove_hydrogens()
    per_chain = {}
    for ch in st[0]:
        seq = "".join(gemmi.find_tabulated_residue(r.name).one_letter_code.upper()
                      for r in ch if gemmi.find_tabulated_residue(r.name).is_amino_acid())
        if seq:
            per_chain[ch.name] = seq
    # binder = the chain that is neither of the two target protomers (shortest)
    binder_chain = min(per_chain, key=lambda c: len(per_chain[c]))
    seq = per_chain[binder_chain]
    cnt = collections.Counter(seq)
    top = cnt.most_common(3)
    rows.append({
        "design": path.split("\\")[-1].split("/")[-1],
        "chains": {c: len(s) for c, s in per_chain.items()},
        "binder_chain": binder_chain,
        "len": len(seq),
        "seq": seq,
        "top3": [[a, n, round(100 * n / len(seq), 1)] for a, n in top],
        "E_pct": round(100 * cnt["E"] / len(seq), 1),
        "A_pct": round(100 * cnt["A"] / len(seq), 1),
        "counts": dict(sorted(cnt.items())),
    })
print(json.dumps(rows, indent=1))
