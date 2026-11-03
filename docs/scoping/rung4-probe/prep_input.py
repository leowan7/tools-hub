"""Build the rung-4 RFD4-Proteina input: 3AVE IgG1 Fc dimer (chains A,B) -> CIF.

Writes label_asym_id == auth_asym_id == A/B so a parser that reads either field
sees the same chain ids, and reports the label_seq_id of each author-numbered
hotspot used by the recorded `proteina` run.
"""
import sys, gemmi

src, out_cif = sys.argv[1], sys.argv[2]
AUTH_HOTSPOTS = [241, 243, 244, 246, 260, 262, 264, 301]

st = gemmi.read_structure(src)
st.setup_entities()
for model in st:
    for ch in [c.name for c in model]:
        if ch not in ("A", "B"):
            model.remove_chain(ch)
st.remove_ligands_and_waters()
st.remove_hydrogens()
st.remove_alternative_conformations()
st.setup_entities()
st.assign_label_seq_id()

# gemmi names subchains "Axp"/"Bxp" and reuses the chain name as the entity id.
# Force label_asym_id to the chain name and the polymer entity id to "1".
for model in st:
    for ch in model:
        for res in ch:
            res.subchain = ch.name
# One entity PER chain: the Fc protomers have no covalent path between them in
# this fragment, which starts past the hinge cysteines.
# UNVERIFIED rationale: the intent was to avoid upstream's
# index_group_molecule_must_be_connected rejection of two unbonded chains sharing
# a molecule id. It does NOT achieve that -- V1 and V5 still fail with the same
# error (cpu_result.json, variants_result_v5v7.json), so separate entities are
# not what the invariant keys on. Kept as an honest description of the structure,
# not as a fix.
ents = gemmi.EntityList()
for idx, ch in enumerate(st[0], start=1):
    e = gemmi.Entity(str(idx))
    e.entity_type = gemmi.EntityType.Polymer
    e.polymer_type = gemmi.PolymerType.PeptideL
    e.subchains = [ch.name]
    ents.append(e)
st.entities = ents
st.assign_label_seq_id()

doc = st.make_mmcif_document()
doc.write_file(out_cif)

back = gemmi.read_structure(out_cif)
back.setup_entities()
for ch in back[0]:
    seq = [(r.label_seq, r.seqid.num, r.name, r.subchain) for r in ch]
    labs = [s[0] for s in seq]
    auths = [s[1] for s in seq]
    print(f"chain {ch.name}: n_res={len(seq)} label_seq {labs[0]}..{labs[-1]} auth {auths[0]}..{auths[-1]} subchain={seq[0][3]}")
    print(f"  auth_contiguous={set(auths[i+1]-auths[i] for i in range(len(auths)-1))=={1}} "
          f"label_contiguous={labs == list(range(labs[0], labs[0]+len(labs)))}")
    a2l = {}
    for lab, auth, name, _ in seq:
        a2l.setdefault(auth, (lab, name))
    print("  hotspots: " + ", ".join(f"{ch.name}{a}->{a2l[a][0]}({a2l[a][1]})" for a in AUTH_HOTSPOTS))
