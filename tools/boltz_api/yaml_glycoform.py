"""Boltz S&B / Protein-Design input body emitter for glycoform-bearing IgG Fc.

The Boltz API represents an N-linked glycan as one `ligand_ccd` entity per sugar
plus explicit bond constraints — not the chained-CCD YAML pattern from
github.com/jwohlwend/boltz/issues/622. The model is the same; the wire format differs.

Glycan topology encoded here for the pilot:

  S2G2F (biantennary, core-fucosylated, fully sialylated):

         SIA — GAL — NAG \\
                          BMA — NAG — NAG(+FUC) — Asn-ND2
         SIA — GAL — NAG /

  G2F (biantennary, core-fucosylated, no sialic acid — strip both SIA):

               GAL — NAG \\
                          BMA — NAG — NAG(+FUC) — Asn-ND2
               GAL — NAG /

Inter-sugar linkages (matched to canonical complex N-glycan):

  Asn ND2 → NAG1 C1    (β1-N glycosidic, the protein-glycan bond)
  NAG1 O4 → NAG2 C1    (β1-4)
  NAG1 O6 → FUC  C1    (α1-6 core fucose)
  NAG2 O4 → BMA  C1    (β1-4)
  BMA  O3 → MAN3 C1    (α1-3 arm)
  BMA  O6 → MAN6 C1    (α1-6 arm)
  MAN3 O2 → NAG3 C1    (β1-2 antenna)
  MAN6 O2 → NAG4 C1    (β1-2 antenna)
  NAG3 O4 → GAL3 C1    (β1-4)
  NAG4 O4 → GAL4 C1    (β1-4)
  GAL3 O6 → SIA3 C2    (α2-6 sialic acid, S2G2F only)
  GAL4 O6 → SIA4 C2    (α2-6 sialic acid, S2G2F only)
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import Literal, Sequence

GlycoformName = Literal["S2G2F", "G2F"]
ASN_GLYCAN_CONNECT_ATOM = "ND2"
SUGAR_ANOMERIC_C = "C1"
SIA_ANOMERIC_C = "C2"  # sialic acid links via C2 not C1


@dataclass(frozen=True)
class SugarNode:
    """One monosaccharide in the glycan tree.

    chain_label is filled in by the emitter; it's the API chain_id assigned to this
    entity. parent_link_atom is the atom on the *parent* sugar that bonds to this
    sugar's anomeric carbon.
    """

    ccd: str  # CCD code: NAG, BMA, MAN, GAL, FUC, SIA
    parent_idx: int | None  # index into the glycan list; None for root (bonds to Asn)
    parent_link_atom: str  # parent atom: O4, O6, O3, O2, ND2 (for root)
    self_anomeric_atom: str = SUGAR_ANOMERIC_C  # C1 for most, C2 for SIA


def s2g2f_tree() -> list[SugarNode]:
    """Biantennary, core-fucosylated, fully α2-6 sialylated. 12 sugars."""
    return [
        SugarNode("NAG", parent_idx=None, parent_link_atom=ASN_GLYCAN_CONNECT_ATOM),  # 0 NAG1 core
        SugarNode("FUC", parent_idx=0, parent_link_atom="O6"),  # 1 core fucose
        SugarNode("NAG", parent_idx=0, parent_link_atom="O4"),  # 2 NAG2
        SugarNode("BMA", parent_idx=2, parent_link_atom="O4"),  # 3 β-mannose core
        SugarNode("MAN", parent_idx=3, parent_link_atom="O3"),  # 4 α1-3 arm
        SugarNode("MAN", parent_idx=3, parent_link_atom="O6"),  # 5 α1-6 arm
        SugarNode("NAG", parent_idx=4, parent_link_atom="O2"),  # 6 antenna 3 GlcNAc
        SugarNode("NAG", parent_idx=5, parent_link_atom="O2"),  # 7 antenna 6 GlcNAc
        SugarNode("GAL", parent_idx=6, parent_link_atom="O4"),  # 8
        SugarNode("GAL", parent_idx=7, parent_link_atom="O4"),  # 9
        SugarNode("SIA", parent_idx=8, parent_link_atom="O6", self_anomeric_atom=SIA_ANOMERIC_C),  # 10
        SugarNode("SIA", parent_idx=9, parent_link_atom="O6", self_anomeric_atom=SIA_ANOMERIC_C),  # 11
    ]


def g2f_tree() -> list[SugarNode]:
    """Biantennary, core-fucosylated, terminal galactose (no sialic acid). 10 sugars."""
    return s2g2f_tree()[:10]


GLYCOFORM_TREES: dict[GlycoformName, list[SugarNode]] = {
    "S2G2F": s2g2f_tree(),
    "G2F": g2f_tree(),
}


@dataclass
class GlycoformTargetSpec:
    """Per-Asn glycan installation site on an Fc dimer."""

    fc_chain_a: str = "A"
    fc_chain_b: str = "B"
    asn_residue_index_a: int = 297
    asn_residue_index_b: int = 297
    glycoform: GlycoformName = "S2G2F"
    fc_sequence_a: str = ""  # filled at emit time from PDB
    fc_sequence_b: str = ""

    def trees(self) -> dict[str, list[SugarNode]]:
        """Returns the same glycan tree for each chain (A and B Asn297)."""
        tree = list(GLYCOFORM_TREES[self.glycoform])  # copy
        return {self.fc_chain_a: tree, self.fc_chain_b: list(tree)}


def _allocate_ligand_chain_ids(start: int, n: int) -> list[str]:
    """Allocate single-char ligand chain ids C, D, E, ... after protein chains A, B."""
    pool = string.ascii_uppercase + string.ascii_lowercase
    if start + n > len(pool):
        raise ValueError(f"Out of chain id pool: need {n} from offset {start}")
    return list(pool[start : start + n])


def build_structure_binding_input(
    spec: GlycoformTargetSpec,
    *,
    include_binder: dict[str, str] | None = None,
    pocket_residue_indices: Sequence[int] | None = None,
    num_samples: int = 1,
    model: str = "boltz-2.1",
) -> dict:
    """Build the Boltz S&B request body for the glycoform-bearing Fc.

    Args:
        spec: which glycoform on which chains.
        include_binder: optional {chain_id: sequence} for a binder; if present, the
            request will set up protein_protein_binding with this chain id as binder.
        pocket_residue_indices: optional Fc-chain residue indices to use as pocket
            constraint — only meaningful when include_binder is set.
        num_samples: how many samples Boltz produces; 1 keeps cost minimal.
        model: API model version string.

    Returns the dict that POSTs to /compute/v1/predictions/structure-and-binding.
    """
    if not (spec.fc_sequence_a and spec.fc_sequence_b):
        raise ValueError("GlycoformTargetSpec needs both Fc sequences populated")

    entities: list[dict] = []
    bonds: list[dict] = []

    # Protein entities: Fc chain A + chain B.
    entities.append(
        {
            "chain_ids": [spec.fc_chain_a],
            "type": "protein",
            "value": spec.fc_sequence_a,
        }
    )
    entities.append(
        {
            "chain_ids": [spec.fc_chain_b],
            "type": "protein",
            "value": spec.fc_sequence_b,
        }
    )

    # Binder protein entity (optional).
    next_chain_offset = 2  # A, B used
    binder_chain_ids: list[str] = []
    if include_binder:
        for binder_chain, binder_seq in include_binder.items():
            entities.append(
                {
                    "chain_ids": [binder_chain],
                    "type": "protein",
                    "value": binder_seq,
                }
            )
            binder_chain_ids.append(binder_chain)
            next_chain_offset += 1

    # Glycan ligand entities + bonds for each chain.
    trees = spec.trees()
    for fc_chain, asn_idx in (
        (spec.fc_chain_a, spec.asn_residue_index_a),
        (spec.fc_chain_b, spec.asn_residue_index_b),
    ):
        tree = trees[fc_chain]
        glycan_chain_ids = _allocate_ligand_chain_ids(next_chain_offset, len(tree))
        next_chain_offset += len(tree)
        for sugar_chain, sugar in zip(glycan_chain_ids, tree, strict=True):
            entities.append(
                {
                    "chain_ids": [sugar_chain],
                    "type": "ligand_ccd",
                    "value": sugar.ccd,
                }
            )
        # Asn ND2 → root NAG C1 (the protein-glycan glycosidic bond).
        # Boltz API uses 0-indexed residue positions; spec carries the 1-indexed
        # human position so we subtract 1 here.
        root_sugar = tree[0]
        bonds.append(
            {
                "atom1": {
                    "type": "polymer_atom",
                    "chain_id": fc_chain,
                    "residue_index": asn_idx - 1,
                    "atom_name": ASN_GLYCAN_CONNECT_ATOM,
                },
                "atom2": {
                    "type": "ligand_atom",
                    "chain_id": glycan_chain_ids[0],
                    "atom_name": root_sugar.self_anomeric_atom,
                },
            }
        )
        # Inter-sugar bonds.
        for i, sugar in enumerate(tree):
            if sugar.parent_idx is None:
                continue
            bonds.append(
                {
                    "atom1": {
                        "type": "ligand_atom",
                        "chain_id": glycan_chain_ids[sugar.parent_idx],
                        "atom_name": sugar.parent_link_atom,
                    },
                    "atom2": {
                        "type": "ligand_atom",
                        "chain_id": glycan_chain_ids[i],
                        "atom_name": sugar.self_anomeric_atom,
                    },
                }
            )

    body: dict = {
        "model": model,
        "input": {
            "entities": entities,
            "bonds": bonds,
            "num_samples": num_samples,
        },
    }

    if include_binder:
        body["input"]["binding"] = {
            "type": "protein_protein_binding",
            "binder_chain_ids": binder_chain_ids,
        }
        if pocket_residue_indices:
            body["input"]["constraints"] = [
                {
                    "type": "pocket",
                    "binder_chain_ids": binder_chain_ids,
                    "pocket_chain_id": spec.fc_chain_a,
                    "pocket_residue_indices": list(pocket_residue_indices),
                    "max_distance_angstrom": 6.0,
                }
            ]

    return body


def build_library_screen_input(
    spec: GlycoformTargetSpec,
    *,
    binder_sequences: Sequence[tuple[str, str]],  # (design_id, sequence) pairs
    binder_chain_id: str = "X",
) -> dict:
    """Build a Protein Library Screen request body for a single glycoform target.

    Each binder sequence is rendered as one `proteins` entry with a single protein entity
    on `binder_chain_id`. The target carries the full glycoform Fc + glycan ligands.
    """
    if not (spec.fc_sequence_a and spec.fc_sequence_b):
        raise ValueError("GlycoformTargetSpec needs both Fc sequences populated")
    sb_body = build_structure_binding_input(spec)
    target_entities = sb_body["input"]["entities"]
    target_bonds = sb_body["input"]["bonds"]

    proteins = []
    for design_id, seq in binder_sequences:
        proteins.append(
            {
                "id": design_id,
                "entities": [
                    {"chain_ids": [binder_chain_id], "type": "protein", "value": seq}
                ],
            }
        )

    return {
        "target": {
            "type": "no_template",
            "entities": target_entities,
            "bonds": target_bonds,
        },
        "proteins": proteins,
    }


def build_protein_design_input(
    spec: GlycoformTargetSpec,
    *,
    hotspot_residue_indices: Sequence[int],
    num_proteins: int = 100,
    binder_curated: str = "boltz_nanobody",
    binder_chain_id_for_constraint: str = "X",
) -> dict:
    """Build the Boltz Protein Design (BoltzGen) request body.

    Uses the curated `boltz_nanobody` binder scaffold — the canonical Kao-style nanobody
    form — rather than a free-length protein. Constraints live inside `target.no_template`
    (not at the request top level) as a pocket constraint with chain-id-keyed
    `contact_residues`.
    """
    if not (spec.fc_sequence_a and spec.fc_sequence_b):
        raise ValueError("GlycoformTargetSpec needs both Fc sequences populated")

    sb_body = build_structure_binding_input(spec)
    target_entities = sb_body["input"]["entities"]
    target_bonds = sb_body["input"]["bonds"]

    body: dict = {
        "binder_specification": {
            "type": "boltz_curated",
            "binder": binder_curated,
        },
        "num_proteins": num_proteins,
        "target": {
            "type": "no_template",
            "entities": target_entities,
            "bonds": target_bonds,
            "constraints": [
                {
                    "type": "pocket",
                    "binder_chain_id": binder_chain_id_for_constraint,
                    "contact_residues": {spec.fc_chain_a: list(hotspot_residue_indices)},
                    "max_distance_angstrom": 6.0,
                }
            ],
        },
    }
    return body
