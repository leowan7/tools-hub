"""Aggressive PDB cleanup for Kendrew GPU pipelines.

Standalone module — no imports from sibling pdb_utils modules — so it can
be mounted directly into each Modal docker image alongside run_pipeline.py
without dragging the rest of the backend.

Used by:
- docker/pxdesign/run_pipeline.py
- docker/boltzgen/run_pipeline.py
- docker/rfdiffusion/run_pipeline.py
- docker/rfantibody/run_pipeline.py

Why this exists:
gemmi's ``Structure.remove_ligands_and_waters()`` raises ``missing
entity_type`` on user-uploaded PDBs that don't carry ``_entity.type``
metadata (anything beyond clean RCSB PDBs). Doing the cleanup in
Biopython up front means gemmi only ever sees a sanitized polymer-only
structure.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from Bio.PDB import PDBParser, MMCIFParser, PDBIO, Select


# -- Constants ----------------------------------------------------------------

STANDARD_AA = frozenset([
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
])

WATER_RESNAMES = frozenset(["HOH", "WAT", "H2O", "DOD", "TIP", "TIP3", "TIP4"])

# Modified-residue -> standard parent. Atom dict maps old atom name to
# (new atom name, new element). Same dict pxdesign and boltzgen had inline.
MODRES_MAP: dict = {
    "MSE": ("MET", {"SE": ("SD", "S")}),
    "CME": ("CYS", {}),
    "CSO": ("CYS", {}),
    "SEP": ("SER", {}),
    "TPO": ("THR", {}),
    "PTR": ("TYR", {}),
    "KCX": ("LYS", {}),
    "HYP": ("PRO", {}),
    "LLP": ("LYS", {}),
}

BACKBONE_ATOMS = frozenset(["N", "CA", "C", "O"])


# -- Result type --------------------------------------------------------------

@dataclass
class PipelineNormalizationReport:
    """Result of normalize_for_pipeline.

    output_path
        The cleaned PDB written to disk.
    changes
        Human-readable list of operations applied.
    chains_kept
        Chain IDs surviving cleanup (in order).
    chains_dropped
        Chain IDs removed (with reason, e.g. "B (NAG ligand)").
    residues_kept_per_chain
        {chain_id: count_after} for surviving chains.
    residues_dropped_per_chain
        {chain_id: count_dropped} (waters, HETATM, non-standard residues,
        residues with bad backbones).
    renumber_map
        {(orig_chain, orig_resnum): new_resnum}. Empty unless
        renumber_residues=True. Callers consult this to rewrite hotspot
        indices into the cleaned coordinate space.
    models_collapsed
        True if input was multi-model and we kept only one model.
    altloc_records_collapsed
        Count of alternate-conformation atom records dropped (e.g. atoms
        with altloc 'B' or 'C' that lost to a higher-occupancy 'A').
    """
    output_path: str
    changes: list = field(default_factory=list)
    chains_kept: list = field(default_factory=list)
    chains_dropped: list = field(default_factory=list)
    residues_kept_per_chain: dict = field(default_factory=dict)
    residues_dropped_per_chain: dict = field(default_factory=dict)
    renumber_map: dict = field(default_factory=dict)
    models_collapsed: bool = False
    altloc_records_collapsed: int = 0


class _PipelineSelect(Select):
    """Biopython PDBIO selector implementing the per-tool cleanup contract.

    ``keep_atoms`` is the per-atom altloc allowlist computed by
    ``_pick_altloc_per_residue``: ``(chain_id, resnum, icode, atom_name) ->
    chosen_altloc``. When an atom's altloc differs from the chosen one for
    that name, ``accept_atom`` drops it. This is what collapses multi-altloc
    crystal structures (e.g. 3IUT, 3KKU) down to a single-conformation PDB
    before RFdiffusion's frame builder sees them — without it, downstream
    tools encounter two CA records per residue and produce degenerate
    rotation frames.
    """

    def __init__(
        self,
        *,
        keep_chains: set,
        keep_residues: dict,
        keep_atoms: dict,
        keep_hydrogens: bool,
        first_model_id,
    ):
        self.keep_chains = keep_chains
        self.keep_residues = keep_residues
        self.keep_atoms = keep_atoms
        self.keep_hydrogens = keep_hydrogens
        self.first_model_id = first_model_id

    def accept_model(self, model):
        return model.get_id() == self.first_model_id

    def accept_chain(self, chain):
        return chain.get_id() in self.keep_chains

    def accept_residue(self, residue):
        chain_id = residue.get_parent().get_id()
        _, resnum, _ = residue.get_id()
        return (chain_id, resnum) in self.keep_residues

    def accept_atom(self, atom):
        if not self.keep_hydrogens and atom.element == "H":
            return False
        residue = atom.get_parent()
        chain_id = residue.get_parent().get_id()
        _, resnum, icode = residue.get_id()
        atom_name = atom.get_name().strip()
        chosen = self.keep_atoms.get((chain_id, resnum, icode, atom_name))
        if chosen is None:
            return True
        atom_altloc = atom.get_altloc() or " "
        return atom_altloc == chosen


def _pick_altloc_per_residue(residue) -> tuple[dict, int]:
    """For each atom name in this residue, pick which altloc to keep.

    Returns ``(chosen, collapsed_count)`` where ``chosen`` maps
    ``atom_name -> altloc_str`` and ``collapsed_count`` is the number of
    alternate-conformation atom records that lose to a higher-occupancy
    sibling (i.e. the records ``accept_atom`` will drop).

    Tie-breaker: an atom-record with no altloc (``' '``) always wins;
    otherwise the highest occupancy wins; otherwise alphabetical altloc.
    This matches the convention "no altloc" = single conformation, which
    is more trustworthy than any specific altloc letter.
    """
    by_name: dict = {}  # atom_name -> list[(altloc, occupancy)]
    for atom in residue.get_unpacked_list():
        name = atom.get_name().strip()
        alt = atom.get_altloc() or " "
        try:
            occ = float(atom.get_occupancy())
        except (TypeError, ValueError):
            occ = 0.0
        by_name.setdefault(name, []).append((alt, occ))

    chosen: dict = {}
    collapsed = 0
    for name, entries in by_name.items():
        # Prefer altloc ' ' (single conformation) over any letter.
        blanks = [e for e in entries if e[0] == " "]
        if blanks:
            chosen[name] = " "
        else:
            # Highest occupancy wins; ties broken alphabetically.
            entries_sorted = sorted(entries, key=lambda e: (-e[1], e[0]))
            chosen[name] = entries_sorted[0][0]
        collapsed += len(entries) - 1
    return chosen, collapsed


# -- Public entry points ------------------------------------------------------

def normalize_for_pipeline(
    input_path: str,
    output_path: Optional[str] = None,
    *,
    target_chain: Optional[str] = None,
    keep_hetatm: bool = False,
    hetatm_whitelist: Optional[set] = None,
    keep_waters: bool = False,
    keep_hydrogens: bool = False,
    drop_zero_backbone: bool = True,
    convert_modres: bool = True,
    renumber_residues: bool = False,
    nmr_model: int = 1,
) -> PipelineNormalizationReport:
    """Aggressive cleanup for Kendrew GPU pipelines.

    Reads a user-uploaded PDB or mmCIF, applies normalization, writes a
    cleaned PDB. Designed to produce input that gemmi.read_structure() will
    not crash on when followed by remove_ligands_and_waters().

    When ``output_path is None`` the function runs in **preview mode**: every
    in-memory check still happens (so the returned report's ``changes``,
    ``altloc_records_collapsed``, ``residues_kept_per_chain`` etc. are
    populated), but no file is written. Callers use this to drive the
    tools-hub preflight panel — surface "we'd clean X, Y, Z" before the
    user clicks Run, without leaving cleaned-PDB temp files around.

    Args:
        input_path: User upload (.pdb, .ent, .cif, .mmcif).
        output_path: Where to write the cleaned PDB. Pass ``None`` for a
            dry-run preview that only fills in the report.
        target_chain: If given, drop all other chains. Case-sensitive.
        keep_hetatm: If False (default), drop all HETATM residues except
            those in hetatm_whitelist.
        hetatm_whitelist: 3-letter resnames to retain even with
            keep_hetatm=False. Ignored when keep_hetatm=True.
        keep_waters: If False (default), drop HOH/WAT/H2O/DOD even if
            keep_hetatm is True.
        keep_hydrogens: If False (default), drop H atoms.
        drop_zero_backbone: If True (default), drop residues missing any
            of N/CA/C/O backbone atoms or where any backbone atom has
            all-zero coordinates.
        convert_modres: If True (default), MSE->MET, SEP->SER, etc.
        renumber_residues: If True, residues are reindexed 1..N per chain
            and the report contains the renumber_map.
        nmr_model: 1-based model index to keep (default 1).

    Returns:
        PipelineNormalizationReport.

    Raises:
        ValueError: Unsupported extension; zero standard polymer residues
            survived; user-named target_chain not present after cleanup.
    """
    hetatm_whitelist = hetatm_whitelist or set()

    ext = os.path.splitext(input_path)[1].lower()
    if ext in (".cif", ".mmcif"):
        parser = MMCIFParser(QUIET=True)
    elif ext in (".pdb", ".ent"):
        parser = PDBParser(QUIET=True)
    else:
        raise ValueError(
            f"Unsupported file format: {ext}. Expected .pdb, .cif, or .mmcif"
        )

    structure = parser.get_structure("target", input_path)
    report = PipelineNormalizationReport(output_path=output_path)

    # --- Pick model -----------------------------------------------------------
    models = list(structure.get_models())
    if len(models) > 1:
        report.models_collapsed = True
        report.changes.append(
            f"Selected model {nmr_model} of {len(models)} (multi-model input)"
        )
    model_index = nmr_model - 1
    if model_index < 0 or model_index >= len(models):
        model_index = 0
    target_model = models[model_index]
    target_model_id = target_model.get_id()

    # --- MSE -> MET (and other modres) in place ------------------------------
    # Renames the residue and (where the modres dict specifies) repairs atom
    # names + elements. Also resets the residue id's hetflag from e.g. "H_MSE"
    # to " " (standard) so the downstream HETATM filter does not strip the
    # newly-standardized residue.
    modres_renames = 0
    if convert_modres:
        for residue in target_model.get_residues():
            name = residue.get_resname().strip()
            if name in MODRES_MAP:
                new_name, atom_fixes = MODRES_MAP[name]
                residue.resname = new_name
                # Clear hetflag (standard ATOM going forward)
                rid = residue.get_id()
                residue.id = (" ", rid[1], rid[2])
                for atom in list(residue):
                    aname = atom.get_name().strip()
                    if aname in atom_fixes:
                        new_aname, new_elem = atom_fixes[aname]
                        atom.name = f" {new_aname} " if len(new_aname) <= 2 else new_aname
                        atom.element = new_elem
                modres_renames += 1
    if modres_renames:
        report.changes.append(
            f"Converted {modres_renames} modified residue(s) to standard"
        )

    # --- Decide which chains/residues survive --------------------------------
    keep_chains: set = set()
    drop_chain_reasons: dict = {}
    keep_residues: dict = {}  # (chain_id, resnum) -> True
    keep_atoms: dict = {}     # (chain_id, resnum, icode, atom_name) -> chosen_altloc
    dropped_per_chain: dict = {}
    kept_per_chain: dict = {}
    renumber_map: dict = {}
    total_altloc_collapsed = 0

    # ``target_chain`` may name SEVERAL chains, separated by whitespace
    # ("A B C") or commas ("A,B,C" — the form the tool adapters canonicalise to).
    # The comparison here was an exact string match, so a multi-token value
    # matched no chain, dropped every one of them, and raised below. Splitting is
    # behaviour-preserving for a single id (["A"]) and only affects inputs that
    # previously raised.
    wanted_chains = (
        set((target_chain or "").replace(",", " ").split())
        if target_chain else None
    )

    for chain in target_model:
        chain_id = chain.get_id()
        if wanted_chains is not None and chain_id not in wanted_chains:
            drop_chain_reasons[chain_id] = "non-target chain"
            continue

        kept_count = 0
        dropped_count = 0
        first_resname_seen = None

        for residue in chain:
            hetflag, resnum, icode = residue.get_id()
            resname = residue.get_resname().strip()
            if first_resname_seen is None:
                first_resname_seen = resname

            # Water filter (always before HETATM check; works whether the file
            # tags HOH as HETATM or not).
            if resname in WATER_RESNAMES and not keep_waters:
                dropped_count += 1
                continue

            # HETATM filter
            is_hetatm = bool(hetflag and hetflag.strip())
            if is_hetatm and not keep_hetatm and resname not in hetatm_whitelist:
                dropped_count += 1
                continue

            # Standard-AA filter (after MODRES_MAP applied)
            if resname not in STANDARD_AA:
                dropped_count += 1
                continue

            # Per-atom-name altloc choice. Built unconditionally so multi-
            # altloc crystal structures get collapsed in the output even
            # when the caller passes drop_zero_backbone=False.
            per_atom_altloc, collapsed_here = _pick_altloc_per_residue(residue)

            # Backbone integrity. Use the per-atom-name chosen altloc so
            # the check sees exactly the records that will survive the write
            # (otherwise a residue whose only complete backbone is split
            # across altloc A and altloc B atoms would falsely pass the
            # "4 backbone atoms present" gate while the writer kept only
            # one altloc per name).
            if drop_zero_backbone:
                bb_atoms = {}
                for atom in residue.get_unpacked_list():
                    aname = atom.get_name().strip()
                    if aname not in BACKBONE_ATOMS:
                        continue
                    if (atom.get_altloc() or " ") != per_atom_altloc.get(aname, " "):
                        continue
                    bb_atoms[aname] = atom
                if len(bb_atoms) < 4:
                    dropped_count += 1
                    continue
                bad_coords = False
                for aname in BACKBONE_ATOMS:
                    coord = bb_atoms[aname].get_coord()
                    if all(abs(c) < 1e-6 for c in coord):
                        bad_coords = True
                        break
                if bad_coords:
                    dropped_count += 1
                    continue

            keep_residues[(chain_id, resnum)] = True
            for aname, alt in per_atom_altloc.items():
                keep_atoms[(chain_id, resnum, icode, aname)] = alt
            total_altloc_collapsed += collapsed_here
            kept_count += 1

        if kept_count == 0:
            drop_chain_reasons[chain_id] = (
                f"non-protein chain ({first_resname_seen or '?'})"
            )
            continue

        keep_chains.add(chain_id)
        kept_per_chain[chain_id] = kept_count
        if dropped_count:
            dropped_per_chain[chain_id] = dropped_count

    # Validation. Every named chain must have survived — a multi-chain target
    # missing one of its chains is not a partially-valid run, it is a different
    # structure from the one the user described.
    if wanted_chains is not None and not wanted_chains <= keep_chains:
        missing = sorted(wanted_chains - keep_chains)
        raise ValueError(
            f"Target chain {(missing[0] if len(missing) == 1 else ' '.join(missing))!r} "
            f"is not present (or has no protein residues) in the input "
            f"structure. Found chains: "
            f"{sorted(c.get_id() for c in target_model)}"
        )
    if not keep_chains:
        raise ValueError(
            "Structure contains no standard polymer residues after cleanup"
        )

    report.chains_kept = sorted(keep_chains)
    report.chains_dropped = [
        f"{cid} ({reason})" for cid, reason in sorted(drop_chain_reasons.items())
    ]
    report.residues_kept_per_chain = kept_per_chain
    report.residues_dropped_per_chain = dropped_per_chain
    report.altloc_records_collapsed = total_altloc_collapsed
    if drop_chain_reasons:
        dropped_summary = ", ".join(report.chains_dropped)
        report.changes.append(f"Dropped chain(s): {dropped_summary}")
    total_dropped = sum(dropped_per_chain.values())
    if total_dropped:
        report.changes.append(
            f"Dropped {total_dropped} residue(s) (waters, HETATM, "
            f"non-standard, or bad-backbone)"
        )
    if total_altloc_collapsed:
        report.changes.append(
            f"Collapsed {total_altloc_collapsed} alternate-conformation "
            f"atom record(s) (kept highest-occupancy altloc per atom)"
        )

    # Dry-run preview: caller only wants the report (chains kept, altloc
    # records collapsed, MSE remapped, etc.) — skip the file write and
    # any renumber pass, which both produce side effects only useful to
    # callers that will actually hand the cleaned PDB to a downstream
    # tool. The renumber map is still computed below for symmetry; it's
    # cheap and the report's contract is to expose it.
    if output_path is None:
        report.renumber_map = renumber_map
        return report

    # --- Write a first pass with original numbering --------------------------
    selector = _PipelineSelect(
        keep_chains=keep_chains,
        keep_residues=keep_residues,
        keep_atoms=keep_atoms,
        keep_hydrogens=keep_hydrogens,
        first_model_id=target_model_id,
    )
    io = PDBIO()
    io.set_structure(structure)
    io.save(output_path, selector)

    # --- Renumber pass (if requested) ----------------------------------------
    # PDBIO doesn't expose residue-id rewriting. We re-parse our own output,
    # mutate residue ids, write again. Cheap (already-clean file).
    if renumber_residues:
        cleaned = PDBParser(QUIET=True).get_structure("target", output_path)
        for model in cleaned:
            for chain in model:
                cid = chain.get_id()
                originals = []
                for residue in chain:
                    _, orig_resnum, icode = residue.get_id()
                    originals.append((residue, orig_resnum, icode))
                # Stash to a non-colliding range first (avoid mid-mutation
                # id collisions in chains where original numbering overlaps
                # with the eventual 1..N targets).
                for residue, _, _ in originals:
                    rid = residue.get_id()
                    residue.id = (rid[0], rid[1] + 100000, rid[2])
                # Then assign 1..N.
                for new_idx, (residue, orig_resnum, _) in enumerate(
                    originals, start=1
                ):
                    residue.id = (residue.id[0], new_idx, " ")
                    renumber_map[(cid, orig_resnum)] = new_idx
        io = PDBIO()
        io.set_structure(cleaned)
        io.save(output_path)
        report.changes.append(
            f"Renumbered residues to 1..N per chain "
            f"(across {len(report.chains_kept)} chain(s))"
        )

    # Blank altloc column on the output. _PipelineSelect already filtered
    # multi-altloc records down to one per atom name, so each surviving
    # atom is unambiguous and the altloc letter is superfluous. Some
    # downstream parsers (rfantibody's RFdiffusion fork in particular) can
    # build degenerate rotation frames from any leftover altloc disagreement,
    # so we strip the letter entirely.
    if total_altloc_collapsed:
        _blank_altloc_column(output_path)

    report.renumber_map = renumber_map
    return report


def _blank_altloc_column(path: str) -> None:
    """Rewrite ``path`` clearing the altloc column (index 16) of every
    ATOM/HETATM record. Safe on already-blank files (no-op for those lines)
    and on files with mixed line endings.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    out_chunks: list = []
    start = 0
    n = len(data)
    while start < n:
        end = data.find(b"\n", start)
        if end == -1:
            end = n - 1
        line = data[start:end + 1]
        if line.startswith((b"ATOM", b"HETATM")) and len(line) > 17:
            line = line[:16] + b" " + line[17:]
        out_chunks.append(line)
        start = end + 1
    with open(path, "wb") as fh:
        fh.write(b"".join(out_chunks))


# -- Per-tool presets ---------------------------------------------------------

def normalize_for_pxdesign(
    input_path: str, output_path: Optional[str], *, target_chain: str
) -> PipelineNormalizationReport:
    """pxdesign preset: single chain, strip everything non-polymer, renumber."""
    return normalize_for_pipeline(
        input_path, output_path,
        target_chain=target_chain,
        keep_hetatm=False, keep_waters=False, keep_hydrogens=False,
        drop_zero_backbone=True, convert_modres=True,
        renumber_residues=True,
    )


def normalize_for_boltzgen(
    input_path: str, output_path: Optional[str], *, target_chain: str
) -> PipelineNormalizationReport:
    """boltzgen preset: same as pxdesign — single chain, renumbered."""
    return normalize_for_pipeline(
        input_path, output_path,
        target_chain=target_chain,
        keep_hetatm=False, keep_waters=False, keep_hydrogens=False,
        drop_zero_backbone=True, convert_modres=True,
        renumber_residues=True,
    )


def normalize_for_rfdiffusion(
    input_path: str, output_path: Optional[str], *, target_chain: str
) -> PipelineNormalizationReport:
    """rfdiffusion preset: keep target chain only, preserve original numbering.

    RFdiffusion's frame builder consumes the PDB directly and refers to
    residues by original PDB numbering throughout (hotspots, ppi.hotspot_res
    in the Hydra config). Renumbering would silently invalidate those refs.
    """
    return normalize_for_pipeline(
        input_path, output_path,
        target_chain=target_chain,
        keep_hetatm=False, keep_waters=False, keep_hydrogens=False,
        drop_zero_backbone=True, convert_modres=True,
        renumber_residues=False,
    )


def normalize_for_proteina(
    input_path: str, output_path: Optional[str], *, target_chain: str
) -> PipelineNormalizationReport:
    """proteina preset: keep the named chain(s), preserve original numbering.

    Numbering is load-bearing and must NOT be renumbered. Proteina-Complexa
    matches hotspots as ``f"{chain_id}{res_id}"`` against the ORIGINAL author
    numbering carried in the registered target record, and it does so silently —
    a renumbered structure would make every hotspot miss, and upstream would
    then run an unconstrained search that looks exactly like a successful one.
    Same reasoning as rfdiffusion, with one difference: proteina takes
    multi-chain targets (a three-chain example ships upstream), so several
    whitespace-separated chain ids are expected here.
    """
    return normalize_for_pipeline(
        input_path, output_path,
        target_chain=target_chain,
        keep_hetatm=False, keep_waters=False, keep_hydrogens=False,
        drop_zero_backbone=True, convert_modres=True,
        renumber_residues=False,
    )


def normalize_for_rfantibody(
    input_path: str, output_path: Optional[str], *, target_chain: str
) -> PipelineNormalizationReport:
    """rfantibody preset: keep antigen chain only, preserve numbering.

    rfantibody's epitope hotspots are written as ``A50,A51,A80`` strings
    referencing original chain+resnum. Renumbering would silently break
    them. Same logic as rfdiffusion preset.
    """
    return normalize_for_pipeline(
        input_path, output_path,
        target_chain=target_chain,
        keep_hetatm=False, keep_waters=False, keep_hydrogens=False,
        drop_zero_backbone=True, convert_modres=True,
        renumber_residues=False,
    )


def normalize_for_bindcraft(
    input_path: str, output_path: Optional[str], *, target_chain: str
) -> PipelineNormalizationReport:
    """bindcraft preset: keep target chain only, preserve numbering.

    BindCraft (FreeBindCraft fork) references hotspot residues by original
    PDB author numbering throughout the AF2 multimer + ColabDesign loop.
    Renumbering would silently invalidate them. Same logic as rfdiffusion
    and rfantibody presets.
    """
    return normalize_for_pipeline(
        input_path, output_path,
        target_chain=target_chain,
        keep_hetatm=False, keep_waters=False, keep_hydrogens=False,
        drop_zero_backbone=True, convert_modres=True,
        renumber_residues=False,
    )


# -- Preview entry point ------------------------------------------------------

def preview_for_tool(
    tool_slug: str, input_path: str, *, target_chain: str,
) -> PipelineNormalizationReport:
    """Dry-run the per-tool normalizer for the preflight panel.

    Routes to the correct ``normalize_for_<tool>`` and passes
    ``output_path=None`` so the function fills the report (chains kept,
    altloc records collapsed, MSE remapped, etc.) without writing a
    cleaned PDB to disk. Callers in tools-hub use the returned report to
    render the user-visible "We cleaned X, Y, Z. Ready to run." panel.

    Raises ``ValueError`` for unknown ``tool_slug`` so registration drift
    fails loudly rather than silently dispatching to a wrong preset.
    """
    preset_fn = {
        "rfantibody":   normalize_for_rfantibody,
        "rfdiffusion":  normalize_for_rfdiffusion,
        "bindcraft":    normalize_for_bindcraft,
        "boltzgen":     normalize_for_boltzgen,
        "pxdesign":     normalize_for_pxdesign,
    }.get(tool_slug)
    if preset_fn is None:
        raise ValueError(
            f"No pipeline_normalize preset registered for tool {tool_slug!r}. "
            f"Available: rfantibody, rfdiffusion, bindcraft, boltzgen, pxdesign."
        )
    return preset_fn(input_path, None, target_chain=target_chain)
