"""Inter-chain protein-protein interaction (PPI) interface detection.

Analyzes the uploaded structure to find natural binding interfaces between
the target chain and all other chains. This identifies functional interaction
sites (e.g. RBX1 binding site on Cullin) directly from the co-crystal
structure — no external database required.

Usage:
    from scout.interfaces import detect_interfaces
    interfaces = detect_interfaces(pdb_path, "A")

Each returned dict has:
    partner_chain   str       — Chain ID of the interacting partner
    partner_name    str       — Protein name from PDB COMPND records
    contact_residues list[int] — Target chain residue numbers at the interface
    partner_residues list[int] — Partner chain residue numbers at the interface
    contact_count   int       — Number of target residues in contact
    interface_area  str       — Qualitative size: "small", "medium", "large"
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Distance cutoff for defining inter-chain contacts. 4.5 Å captures
# hydrogen bonds and van der Waals contacts at protein-protein interfaces.
_CONTACT_CUTOFF = 4.5

# Minimum number of contact residues to report an interface.
# Filters out crystal contacts and incidental chain proximity.
_MIN_CONTACT_RESIDUES = 5


def _extract_chain_names(pdb_path: str) -> dict:
    """Extract chain ID → protein name mapping from PDB COMPND or mmCIF headers.

    Args:
        pdb_path: Path to the structure file.

    Returns:
        Dict mapping chain ID (str) to protein name (str).
        Returns empty dict on failure.
    """
    pdb_str = str(pdb_path)
    try:
        with open(pdb_str, "r", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}

    chain_names = {}

    if pdb_str.endswith(".cif"):
        # mmCIF: use BioPython's MMCIF2Dict to reliably parse loop data
        # (regex fails on multi-line sequence fields in _entity_poly).
        try:
            from Bio.PDB.MMCIF2Dict import MMCIF2Dict  # noqa: PLC0415
            cif = MMCIF2Dict(pdb_str)

            # Build entity_id → description mapping.
            entity_ids = cif.get("_entity.id", [])
            entity_descs = cif.get("_entity.pdbx_description", [])
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]
            if isinstance(entity_descs, str):
                entity_descs = [entity_descs]
            entity_names = {}
            for eid, desc in zip(entity_ids, entity_descs):
                entity_names[eid] = desc.strip("'\" ").title() if desc else ""

            # Map entity_id → chain IDs via _entity_poly.
            poly_eids = cif.get("_entity_poly.entity_id", [])
            poly_strands = cif.get("_entity_poly.pdbx_strand_id", [])
            if isinstance(poly_eids, str):
                poly_eids = [poly_eids]
            if isinstance(poly_strands, str):
                poly_strands = [poly_strands]
            for eid, strands in zip(poly_eids, poly_strands):
                name = entity_names.get(eid, "")
                for chain_id in strands.split(","):
                    chain_id = chain_id.strip()
                    if chain_id:
                        chain_names[chain_id] = name
        except Exception:
            logger.debug("MMCIF2Dict parsing failed for chain names.", exc_info=True)

        return chain_names

    # PDB format: parse COMPND records for MOL_ID → MOLECULE + CHAIN mapping.
    compnd_lines = [
        line[10:].strip()
        for line in text.splitlines()
        if line.startswith("COMPND")
    ]
    compnd_blob = " ".join(compnd_lines)

    # Split by MOL_ID entries.
    mol_blocks = re.split(r"MOL_ID:\s*\d+;", compnd_blob)

    for block in mol_blocks[1:]:  # skip empty first split
        # Extract MOLECULE name.
        mol_match = re.search(r"MOLECULE:\s*([^;]+)", block)
        molecule_name = mol_match.group(1).strip().title() if mol_match else ""

        # Extract CHAIN IDs.
        chain_match = re.search(r"CHAIN:\s*([^;]+)", block)
        if chain_match:
            chains = [c.strip() for c in chain_match.group(1).split(",")]
            for chain_id in chains:
                if chain_id:
                    chain_names[chain_id] = molecule_name

    # Fallback: extract names from DBREF records (UniProt entry names).
    if not chain_names:
        for line in text.splitlines():
            if line.startswith("DBREF "):
                chain_id = line[12].strip()
                # DBREF columns 42-67 contain the database entry name.
                entry_name = line[42:67].strip()
                if chain_id and entry_name and chain_id not in chain_names:
                    # Convert UniProt entry name like "CUL1_HUMAN" to readable.
                    readable = entry_name.split("_")[0].title() if "_" in entry_name else entry_name
                    chain_names[chain_id] = readable

    return chain_names


def detect_interfaces(pdb_path, target_chain_id: str) -> list:
    """Detect protein-protein interaction interfaces in the uploaded structure.

    For each non-target chain in the structure, computes heavy-atom contacts
    within the distance cutoff. Returns interfaces with at least
    _MIN_CONTACT_RESIDUES residues on the target chain.

    Args:
        pdb_path: Path to the uploaded PDB or mmCIF file.
        target_chain_id: Chain ID selected by the user for epitope analysis.

    Returns:
        List of interface dicts sorted by contact count (largest first).
        Empty list if the structure has only one chain or no significant
        interfaces are found.
    """
    try:
        from Bio.PDB import MMCIFParser, PDBParser  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        logger.debug("BioPython or NumPy not available; skipping interface detection.")
        return []

    pdb_str = str(pdb_path)
    try:
        if pdb_str.endswith(".cif"):
            parser = MMCIFParser(QUIET=True)
        else:
            parser = PDBParser(PERMISSIVE=True, QUIET=True)

        structure = parser.get_structure("query", pdb_str)
        model = next(structure.get_models())
        chain_map = {chain.id: chain for chain in model.get_chains()}
    except Exception:
        logger.debug("Structure parsing failed for interface detection.", exc_info=True)
        return []

    if target_chain_id not in chain_map:
        return []

    # Extract chain names from PDB headers.
    chain_names = _extract_chain_names(pdb_path)

    # Collect target chain residue atoms.
    target_chain = chain_map[target_chain_id]
    target_residues = []
    target_atoms = []
    target_atom_residx = []  # maps atom index → residue index

    for residue in target_chain:
        if residue.id[0] != " ":
            continue  # Skip HETATM and water
        res_idx = len(target_residues)
        target_residues.append(residue)
        for atom in residue.get_atoms():
            target_atoms.append(atom.coord)
            target_atom_residx.append(res_idx)

    if not target_atoms:
        return []

    target_coords = np.array(target_atoms, dtype=np.float32)

    interfaces = []

    for partner_id, partner_chain in chain_map.items():
        if partner_id == target_chain_id:
            continue

        # Collect partner chain atoms.
        partner_residues = []
        partner_atoms = []
        partner_atom_residx = []

        for residue in partner_chain:
            if residue.id[0] != " ":
                continue
            res_idx = len(partner_residues)
            partner_residues.append(residue)
            for atom in residue.get_atoms():
                partner_atoms.append(atom.coord)
                partner_atom_residx.append(res_idx)

        if not partner_atoms:
            continue

        partner_coords = np.array(partner_atoms, dtype=np.float32)

        # Find contacts using KDTree for efficiency.
        try:
            from scipy.spatial import cKDTree  # noqa: PLC0415
            partner_tree = cKDTree(partner_coords)
            # Query each target atom against partner tree.
            distances, _ = partner_tree.query(target_coords, distance_upper_bound=_CONTACT_CUTOFF)
            contact_target_atoms = np.where(distances <= _CONTACT_CUTOFF)[0]

            # Map back to residue numbers.
            contact_target_residx = set(target_atom_residx[i] for i in contact_target_atoms)
            contact_target_resnums = sorted(
                target_residues[i].id[1] for i in contact_target_residx
            )

            # Also find partner residues in contact (reverse query).
            target_tree = cKDTree(target_coords)
            distances_rev, _ = target_tree.query(partner_coords, distance_upper_bound=_CONTACT_CUTOFF)
            contact_partner_atoms = np.where(distances_rev <= _CONTACT_CUTOFF)[0]
            contact_partner_residx = set(partner_atom_residx[i] for i in contact_partner_atoms)
            contact_partner_resnums = sorted(
                partner_residues[i].id[1] for i in contact_partner_residx
            )

        except ImportError:
            # Fallback: brute-force pairwise distances (slower but no scipy needed).
            contact_target_residx = set()
            contact_partner_residx = set()
            for t_idx, t_coord in enumerate(target_coords):
                for p_idx, p_coord in enumerate(partner_coords):
                    dist = np.linalg.norm(t_coord - p_coord)
                    if dist <= _CONTACT_CUTOFF:
                        contact_target_residx.add(target_atom_residx[t_idx])
                        contact_partner_residx.add(partner_atom_residx[p_idx])
            contact_target_resnums = sorted(
                target_residues[i].id[1] for i in contact_target_residx
            )
            contact_partner_resnums = sorted(
                partner_residues[i].id[1] for i in contact_partner_residx
            )

        if len(contact_target_resnums) < _MIN_CONTACT_RESIDUES:
            continue

        # Classify interface size.
        n_contacts = len(contact_target_resnums)
        if n_contacts >= 20:
            interface_area = "large"
        elif n_contacts >= 10:
            interface_area = "medium"
        else:
            interface_area = "small"

        partner_name = chain_names.get(partner_id, "")

        interfaces.append({
            "partner_chain": partner_id,
            "partner_name": partner_name,
            "contact_residues": contact_target_resnums,
            "partner_residues": contact_partner_resnums,
            "contact_count": n_contacts,
            "interface_area": interface_area,
        })

    # Sort by contact count descending.
    interfaces.sort(key=lambda x: x["contact_count"], reverse=True)

    logger.info(
        "Detected %d PPI interface(s) for chain %s in %s.",
        len(interfaces), target_chain_id, pdb_path,
    )
    return interfaces
