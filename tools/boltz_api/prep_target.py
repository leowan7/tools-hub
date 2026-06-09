"""Extract Fc sequences and verify Asn297 position from a PDB.

Originally scoped as a ReGlyco-based glycan grafter. The Boltz API generates the
co-folded structure from a sequence + ligand bond list, so pre-grafted structures are
unnecessary for the API path. ReGlyco is deferred to the scale-up phase, where Modal
pipelines may want a complete CIF input.

What this module does for the pilot:
  - Parse a PDB file, keep only the Fc chains we care about (drop FcγR, free glycans).
  - Return the one-letter amino acid sequence per chain.
  - Return the residue index of Asn297 (Eu numbering) within each chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

AA3_TO_AA1 = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    "MSE": "M",
}


@dataclass
class ChainExtract:
    chain_id: str
    sequence: str
    residue_index_offset: int  # 1-based index in `sequence` corresponding to PDB residue X
    asn297_index_in_sequence: int  # 1-based position of Asn297 within the extracted seq
    pdb_resnum_of_first_residue: int
    pdb_resnum_of_asn297: int


def extract_fc_chains(pdb_path: str | Path, chains_to_keep: tuple[str, ...] = ("A", "B")) -> list[ChainExtract]:
    """Parse a PDB file and return per-chain protein sequence + Asn297 position.

    Drops HETATM rows entirely (handles existing glycans and any non-protein ligand).
    Drops chains not in `chains_to_keep`. Asn297 found by PDB residue number 297; if
    not present, raises.
    """
    pdb_path = Path(pdb_path)
    by_chain_residues: dict[str, list[tuple[int, str]]] = {}
    seen_residue_keys: set[tuple[str, int, str]] = set()

    with pdb_path.open() as fh:
        for line in fh:
            if not line.startswith("ATOM"):
                continue
            chain_id = line[21].strip()
            if chain_id not in chains_to_keep:
                continue
            try:
                resnum = int(line[22:26].strip())
            except ValueError:
                continue
            resname = line[17:20].strip()
            icode = line[26].strip()
            key = (chain_id, resnum, icode)
            if key in seen_residue_keys:
                continue
            seen_residue_keys.add(key)
            aa = AA3_TO_AA1.get(resname)
            if aa is None:
                continue
            by_chain_residues.setdefault(chain_id, []).append((resnum, aa))

    out: list[ChainExtract] = []
    for chain_id in chains_to_keep:
        residues = by_chain_residues.get(chain_id, [])
        if not residues:
            raise ValueError(f"No protein residues found for chain {chain_id} in {pdb_path}")
        residues.sort(key=lambda x: x[0])
        sequence = "".join(aa for _, aa in residues)
        first_resnum = residues[0][0]
        asn297_seq_idx = None
        for i, (resnum, aa) in enumerate(residues, start=1):
            if resnum == 297:
                if aa != "N":
                    raise ValueError(
                        f"PDB residue 297 in chain {chain_id} is {aa}, not Asn — wrong PDB?"
                    )
                asn297_seq_idx = i
                break
        if asn297_seq_idx is None:
            raise ValueError(
                f"PDB {pdb_path} chain {chain_id} has no residue 297; "
                f"first={first_resnum}, last={residues[-1][0]}"
            )
        out.append(
            ChainExtract(
                chain_id=chain_id,
                sequence=sequence,
                residue_index_offset=first_resnum,
                asn297_index_in_sequence=asn297_seq_idx,
                pdb_resnum_of_first_residue=first_resnum,
                pdb_resnum_of_asn297=297,
            )
        )
    return out
