"""PDB -> mmCIF conversion for RFD4-Proteina, with an explicit author->label map.

Why this file exists
====================

Upstream RFD4 resolves every residue id in a condition spec against the mmCIF
``label_seq_id``, not against author numbering
(``docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md`` §4.1, §10 item 5). Our whole
stack speaks author numbering: the hotspot picker emits author chain + author
resSeq (``static/js/hotspot_picker.js:239-244``), and
``shared/pdb_inspect.split_hotspot``
parses exactly that form. The two numbering systems coincide only when a chain's
author numbering is 1-based and gapless. How often that holds across real
structures is unmeasured; of the three fixtures in
``tests/test_rfd4_numbering.py``, one coincides and two do not.

A wrong number here does not fail. It names a different residue, and the run
completes with output identical in shape to a correct one -- the same silent
failure mode ``tools/proteina/run_pipeline.py:706-715`` documents for upstream's
own hotspot matcher, and the same class as the recorded proteina defect where a
bare residue id addressed the wrong protomer of a homodimer. So the mapping is
produced as a VALUE (:class:`ResidueMapping`) that the caller carries beside the
CIF and asserts on, rather than as an invisible side effect of writing a file.

Scope of what this module is proven to do
=========================================

``tests/test_rfd4_numbering.py`` exercises the conversion and the mapping
against real structures whose author numbering and ``label_seq_id`` diverge
(non-1-based chains, author gaps, insertion codes, and two protomers sharing one
author numbering).

It does NOT prove that upstream's reader agrees with the CIF this module writes.
§10 item 5 stays UNKNOWN until a container run checks it: that needs the image
from §8 step 1 and is step 2's gate, not this module's. Nothing here has been
fed to ``rfd4-generate``.

Constraints
===========

Stdlib only, pure functions, no Biopython. The RFD4 container will get exactly
one tools-side file the way ``tools/proteina/modal_app.py`` ships exactly its own
``run_pipeline.py`` (``.github/workflows/deploy-modal.yml:29-30``: *"the only
tools-side file any image gets is that app's own run_pipeline.py"*), so anything
this module is to be used by in-container must be importable with no third-party
dependency and no ``shared/`` package. Nothing copies this file into an image
today -- ``tools/rfd4/Dockerfile.modal`` has no COPY for it and there is no rfd4
``modal_app.py`` -- so whether step 2 inlines it into ``run_pipeline.py`` or adds
a COPY is step 2's choice; stdlib-only keeps both open. That is also what makes
it testable offline, with no GPU and no Modal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

__all__ = [
    "RFD4_HOTSPOT_ATOM_SET",
    "AuthorResidue",
    "NumberingError",
    "ResidueEntry",
    "ResidueMapping",
    "format_hotspot_selection",
    "hotspot_atoms_for_residue",
    "label_range_for_chain",
    "pdb_to_cif",
]

# The twenty standard amino acids, plus the modified residues biotite/atomworks
# treat as protein when building a structure. Copied deliberately from
# `tools/proteina/run_pipeline.py:_MODRES_EQUIV` (:722-725) rather than imported:
# that module is a standalone container file and importing across tools would
# break the one-file mount this module's docstring cites.
_STANDARD_AA = frozenset({
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
})
_MODRES_EQUIV = frozenset({
    "MSE", "CME", "CSO", "SEP", "TPO", "PTR", "KCX", "HYP", "LLP",
    "CSD", "OCS", "MLY", "M3L", "CAS", "CSS", "CSX", "PCA", "SAC",
})

# Water spellings. `HOH` is the PDB's own; the rest appear in tool output.
_WATER_RESNAMES = frozenset({"HOH", "DOD", "WAT", "H2O"})


def _is_polymer(record: str, resname: str) -> bool:
    """Whether a residue occupies a position in its chain's sequence.

    The record column decides, not the residue name alone, because
    ``tools/proteina/run_pipeline.py:775-780`` -- the in-repo statement of what
    counts as a chain residue -- keys on it: an ``ATOM`` record is a chain
    residue whatever its name, and a ``HETATM`` record is one only when
    ``_MODRES_EQUIV`` lists it. Classifying on the name alone makes the two
    parsers disagree about the same chain: an ``ATOM``-record ``UNK`` in a
    modelled or cryo-EM target is skipped here and counted there, so every later
    ``label_seq_id`` in that chain is one too low and a hotspot addresses the
    wrong residue with nothing failing -- the module docstring's failure mode
    (``test_an_atom_record_residue_outside_the_name_set_keeps_its_position``,
    ``test_a_hetatm_record_standard_residue_is_not_a_chain_residue``).

    Water is the one departure from that rule: an ``ATOM``-record water would
    otherwise be given a sequence position inside the polymer entity, which is
    the merge the non-polymer asym split exists to remove.
    """
    if record == "HETATM":
        return resname in _MODRES_EQUIV
    return resname not in _WATER_RESNAMES


class NumberingError(ValueError):
    """A structure this module refuses to convert, or a residue it cannot place.

    Raised rather than worked around. Every path that raises is a path where
    guessing would produce a CIF that names a residue the customer did not pick.
    """


@dataclass(frozen=True)
class AuthorResidue:
    """One residue as our stack addresses it: author chain, author resSeq, icode.

    ``icode`` is ``""`` when absent, never ``None`` or ``" "``, so that it is a
    usable dict key. ``shared/pdb_inspect.split_hotspot`` cannot express an
    insertion code at all: it parses the residue part with ``int()``
    (``shared/pdb_inspect.py:367`` and ``:375``), so the token ``B52A`` is
    unparseable and comes back as ``(None, None)``. Every hotspot reaching this
    module from that path therefore carries ``icode=""`` and can only address
    the un-inserted residue --
    ``test_insertion_coded_residues_are_separately_addressable`` shows the two
    are different residues with different label_seq_ids, so on a Kabat-numbered
    chain that is a real loss of reach, not a formatting detail.
    """

    chain: str
    seq: int
    icode: str = ""

    def token(self) -> str:
        """Render as the author-numbering token our hotspot fields carry."""
        return f"{self.chain}{self.seq}{self.icode}"


@dataclass(frozen=True)
class ResidueEntry:
    """One row of the mapping: an author residue and where it landed in the CIF.

    ``label_seq_id`` is ``None`` for a non-polymer residue (ligand, water), which
    mmCIF writes as ``.`` and which therefore has no sequence position to address.
    """

    author: AuthorResidue
    resname: str
    label_asym_id: str
    label_entity_id: int
    label_seq_id: Optional[int]

    @property
    def is_polymer(self) -> bool:
        return self.label_seq_id is not None


class ResidueMapping:
    """An inspectable author-numbering <-> ``label_seq_id`` map for one structure.

    Carry this beside the CIF. Every id sent to upstream should be produced by
    :meth:`label_for`, and every id read back from upstream output should be
    turned into author numbering by :meth:`author_for`, so that neither direction
    can be an unchecked assumption that the two numberings coincide.

    Ordering of :attr:`entries` is the order residues appear in the source file,
    which is also the order ``label_seq_id`` is assigned in -- asserted by
    ``test_label_seq_id_is_the_within_chain_file_order_index``.
    """

    __slots__ = ("entries", "_by_author", "_by_label")

    FORMAT_VERSION = 1

    def __init__(self, entries: Sequence[ResidueEntry]):
        self.entries = tuple(entries)
        by_author: dict[tuple[str, int, str], ResidueEntry] = {}
        by_label: dict[tuple[str, int], ResidueEntry] = {}
        for entry in self.entries:
            key = (entry.author.chain, entry.author.seq, entry.author.icode)
            if key in by_author:
                raise NumberingError(
                    f"duplicate author residue {entry.author.token()!r} in one "
                    "structure; this parser cannot tell the two apart and will "
                    "not guess which one a hotspot means"
                )
            by_author[key] = entry
            if entry.label_seq_id is not None:
                label_key = (entry.label_asym_id, entry.label_seq_id)
                if label_key in by_label:
                    # `author_for` is the direction that turns upstream's output
                    # back into an id a customer reads, so a silent overwrite
                    # here renames a residue. `pdb_to_cif` assigns labels
                    # monotonically and cannot produce this; `from_json_obj` on a
                    # payload written elsewhere can
                    # (`test_a_duplicate_label_seq_id_is_refused`).
                    raise NumberingError(
                        f"duplicate label_seq_id {label_key[0]}/{label_key[1]} "
                        f"claimed by {by_label[label_key].author.token()!r} and "
                        f"{entry.author.token()!r}"
                    )
                by_label[label_key] = entry
        self._by_author = by_author
        self._by_label = by_label

    # -- lookups ---------------------------------------------------------

    def label_for(self, chain: str, seq: int, icode: str = "") -> tuple[str, int]:
        """Author numbering -> ``(label_asym_id, label_seq_id)``.

        Raises :class:`NumberingError` when the residue is absent from the
        structure or is a non-polymer. Both are refusals rather than fallbacks.
        Whether RFD4 reports an id it cannot resolve is UNVERIFIED (scope §10A);
        the precedent for the bad case is proteina's upstream, which drops an
        unmatched hotspot token and runs anyway
        (``tools/proteina/run_pipeline.py:706-715``). Refusing here makes the
        question moot, before the GPU is touched.
        """
        entry = self._by_author.get((chain, int(seq), icode or ""))
        if entry is None:
            raise NumberingError(
                f"residue {chain}{seq}{icode} is not in this structure; "
                f"chains present: {sorted(self.chains())}"
            )
        if entry.label_seq_id is None:
            raise NumberingError(
                f"residue {chain}{seq}{icode} ({entry.resname}) is not a polymer "
                "residue and has no label_seq_id to address"
            )
        return entry.label_asym_id, entry.label_seq_id

    def author_for(self, label_asym_id: str, label_seq_id: int) -> AuthorResidue:
        """``(label_asym_id, label_seq_id)`` -> author numbering."""
        entry = self._by_label.get((label_asym_id, int(label_seq_id)))
        if entry is None:
            raise NumberingError(
                f"no polymer residue at {label_asym_id}/{label_seq_id} in this "
                "structure"
            )
        return entry.author

    def chains(self) -> frozenset[str]:
        return frozenset(e.author.chain for e in self.entries)

    def polymer_entries(self, chain: Optional[str] = None) -> tuple[ResidueEntry, ...]:
        return tuple(
            e for e in self.entries
            if e.is_polymer and (chain is None or e.author.chain == chain)
        )

    def diverges(self) -> bool:
        """True when any polymer residue's ``label_seq_id`` != its author resSeq.

        The reason to compute this rather than assume it: on a 1-based gapless
        single chain the two numberings coincide, so a test written only against
        such a structure cannot distinguish this mapping from the identity
        function. ``test_identity_mapping_would_fail_the_diverging_fixtures``
        is the check that at least one fixture is not of that kind.
        """
        return any(
            e.label_seq_id != e.author.seq or e.author.icode
            for e in self.polymer_entries()
        )

    # -- serialisation ---------------------------------------------------

    def to_json_obj(self) -> dict:
        """A plain-JSON form for carrying in a job payload or an artefact.

        Round-trip equality with :meth:`from_json_obj` is asserted by
        ``test_mapping_survives_a_json_round_trip``.
        """
        return {
            "format_version": self.FORMAT_VERSION,
            "residues": [
                {
                    "auth_asym_id": e.author.chain,
                    "auth_seq_id": e.author.seq,
                    "pdbx_PDB_ins_code": e.author.icode,
                    "comp_id": e.resname,
                    "label_asym_id": e.label_asym_id,
                    "label_entity_id": e.label_entity_id,
                    "label_seq_id": e.label_seq_id,
                }
                for e in self.entries
            ],
        }

    @classmethod
    def from_json_obj(cls, obj: Mapping) -> "ResidueMapping":
        version = obj.get("format_version")
        if version != cls.FORMAT_VERSION:
            raise NumberingError(
                f"unsupported mapping format_version {version!r}; "
                f"this code reads {cls.FORMAT_VERSION}"
            )
        return cls([
            ResidueEntry(
                author=AuthorResidue(
                    chain=row["auth_asym_id"],
                    seq=int(row["auth_seq_id"]),
                    icode=row.get("pdbx_PDB_ins_code") or "",
                ),
                resname=row["comp_id"],
                label_asym_id=row["label_asym_id"],
                label_entity_id=int(row["label_entity_id"]),
                label_seq_id=(
                    None if row.get("label_seq_id") is None
                    else int(row["label_seq_id"])
                ),
            )
            for row in obj["residues"]
        ])

    def to_json(self) -> str:
        return json.dumps(self.to_json_obj(), separators=(",", ":"), sort_keys=True)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ResidueMapping) and self.entries == other.entries

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ResidueMapping({len(self.entries)} residues, {sorted(self.chains())})"


# ---------------------------------------------------------------------------
# PDB parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Atom:
    record: str
    serial: int
    name: str
    altloc: str
    resname: str
    chain: str
    seq: int
    icode: str
    x: float
    y: float
    z: float
    occupancy: float
    bfactor: float
    element: str


def _element_of(name: str, raw_element: str) -> str:
    if raw_element:
        return raw_element.capitalize()
    # Columns 77-78 are optional in older files. The atom name's first
    # non-digit character is the conventional fallback.
    for ch in name:
        if ch.isalpha():
            return ch.upper()
    return "X"


def _parse_atoms(pdb_text: str) -> list[_Atom]:
    """Parse ATOM/HETATM records of the FIRST model only.

    First model only matches ``shared/pdb_inspect``'s single-model rule for NMR
    ensembles, which ``tools/proteina/run_pipeline.py:736-737`` also follows;
    writing every model into one CIF would give each residue several coordinate
    sets and no way to say which one a hotspot refers to.
    """
    atoms: list[_Atom] = []
    for lineno, line in enumerate(pdb_text.splitlines(), start=1):
        if line.startswith("ENDMDL"):
            break
        record = line[:6]
        if record not in ("ATOM  ", "HETATM"):
            continue
        if len(line) < 54:
            raise NumberingError(
                f"line {lineno}: {record.strip()} record is {len(line)} columns, "
                "too short to hold coordinates"
            )
        # The resSeq field is columns 23-26 and the insertion code is column 27.
        # A residue numbered >= 10000 overflows into the insertion-code column,
        # where `line[22:26]` reads only its first four digits and the fifth
        # becomes an "insertion code" -- a SILENT misparse onto a wrong residue
        # number, which `tools/proteina/run_pipeline.py:747-765` records as a
        # known, unfixed hazard in that parser. Refused here instead: PDB has no
        # legal 5-column resSeq, and a refusal cannot address a wrong residue.
        # Enforced by `test_five_digit_resseq_is_refused_not_misparsed`.
        if line[22:27].strip().lstrip("-").isdigit() and len(line[22:27].strip()) == 5:
            raise NumberingError(
                f"line {lineno}: residue sequence number {line[22:27].strip()!r} "
                "overflows the PDB resSeq field into the insertion-code column; "
                "supply an mmCIF instead"
            )
        try:
            seq = int(line[22:26])
        except ValueError:
            raise NumberingError(
                f"line {lineno}: residue sequence number {line[22:26]!r} is not "
                "an integer"
            ) from None
        try:
            x, y, z = float(line[30:38]), float(line[38:46]), float(line[46:54])
        except ValueError:
            raise NumberingError(f"line {lineno}: unparsable coordinates") from None
        try:
            serial = int(line[6:11])
        except ValueError:
            serial = len(atoms) + 1
        name = line[12:16].strip()
        atoms.append(_Atom(
            record=record.strip(),
            serial=serial,
            name=name,
            altloc=line[16:17].strip(),
            resname=line[17:20].strip().upper(),
            # Column 22 only (index 21), the same single column
            # `tools/proteina/run_pipeline.py:782` reads. A two-column read
            # would fold column 23 into the chain id and produce ids that no
            # picker token matches. (`shared/pdb_inspect.split_hotspot` reads no
            # columns at all -- it prefix-matches a token against the known
            # chain ids, `shared/pdb_inspect.py:370-377` -- so it is not
            # evidence for a width either way.)
            chain=line[21:22].strip(),
            seq=seq,
            icode=line[26:27].strip(),
            x=x, y=y, z=z,
            occupancy=_float_or(line[54:60], 1.0),
            bfactor=_float_or(line[60:66], 0.0),
            element=_element_of(name, line[76:78].strip()),
        ))
    if not atoms:
        raise NumberingError("no ATOM or HETATM records in the first model")
    return atoms


def _float_or(raw: str, default: float) -> float:
    try:
        return float(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def _cif_value(value: object) -> str:
    """Quote one mmCIF value. ``None`` becomes ``.``, ``""`` becomes ``?``."""
    if value is None:
        return "."
    text = str(value)
    if text == "":
        return "?"
    if any(c.isspace() for c in text) or text[0] in "_#$'\"[];":
        return "'" + text.replace("'", "\\'") + "'"
    return text


_ATOM_SITE_FIELDS = (
    "group_PDB", "id", "type_symbol", "label_atom_id", "label_alt_id",
    "label_comp_id", "label_asym_id", "label_entity_id", "label_seq_id",
    "pdbx_PDB_ins_code", "Cartn_x", "Cartn_y", "Cartn_z", "occupancy",
    "B_iso_or_equiv", "auth_seq_id", "auth_comp_id", "auth_asym_id",
    "auth_atom_id", "pdbx_PDB_model_num",
)


def pdb_to_cif(
    pdb_text: str, *, entry_id: str = "RFD4",
) -> tuple[str, ResidueMapping]:
    """Convert PDB text to mmCIF text, returning the CIF and its residue mapping.

    For a POLYMER residue, ``label_asym_id`` is kept equal to the author chain
    id and one ``polymer`` entity is emitted per chain. Both are deliberate: it
    means the only numbering that differs between the two systems is the
    sequence id, which is the one this module exists to track, and it keeps the
    mapping readable to a human debugging a run. Whether the PDB's own
    deposition pipeline assigns ``label_asym_id`` the same way is UNVERIFIED
    here, so a ``label_asym_id`` from this writer must not be compared against
    one from an RCSB file.

    Non-polymer residues do NOT stay in that asym. In PDB format a water,
    glycan or metal normally carries a protein chain's own id, so leaving them
    there would declare a ``polymer`` entity holding both a protein and its
    non-polymer residues: ``static/example/1HEW.pdb`` carries 106 of them, 103
    waters on chain A and 3 NAG on chain B, so chain A's own entity would absorb
    the 103 (counted by calling this function on it, 2026-11-02). Each
    ``(chain, resname)`` group therefore gets its own ``non-polymer`` entity and
    its own asym id, written ``<chain>_<resname>``
    (``test_waters_get_their_own_asym_and_entity``). An author chain id is one
    column wide, so a generated id can never collide with one.

    ``label_seq_id`` is the 1-based index of a polymer residue within its chain,
    in file order (``test_label_seq_id_is_the_within_chain_file_order_index``).
    Unobserved residues get no index: this writer sees only what the coordinate
    records contain and deliberately ignores ``SEQRES``, so an author-numbering
    gap does NOT produce a ``label_seq_id`` gap
    (``test_author_gap_does_not_gap_the_label_numbering``). A deposited CIF,
    whose entity sequence comes from the full construct, would number those
    residues differently. That divergence is why the mapping must travel with
    the CIF this function wrote, and not be re-derived from any other file.

    Which residues those are is decided by :func:`_is_polymer`, on the record
    column and not the residue name alone. Non-polymer residues are written with
    ``label_seq_id`` ``.`` and are not part of any entity sequence, so a hotspot
    on one is refused by :meth:`ResidueMapping.label_for` rather than given a
    made-up position
    (``test_a_hetatm_record_standard_residue_is_not_a_chain_residue``).
    """
    atoms = _parse_atoms(pdb_text)
    # mmCIF has no empty asym id, and a hotspot built on one renders with the
    # chain field missing, which addresses nothing. How often tool-generated
    # PDBs leave column 22 blank is uncited here; the refusal is cheap either
    # way and the caller must handle it (`test_a_blank_chain_id_is_refused`).
    # Assigning a chain id during staging instead is step 2's option; nothing
    # does that today.
    for atom in atoms:
        if not atom.chain:
            raise NumberingError(
                f"atom {atom.serial} (residue {atom.resname}{atom.seq}) has a "
                "blank chain id in column 22; every residue needs a chain id to "
                "be addressable"
            )

    # Group atoms into residues in file order, keyed on the author identity.
    # A key that comes back AFTER a different residue intervened is a second
    # residue claiming an identity already taken -- what a concatenated file, a
    # relax output that reuses chain ids, or a multi-model file with a missing
    # ENDMDL produces. Collapsing the two would give one author id two sets of
    # coordinates, so a hotspot naming it would address both residues and the
    # run would complete -- this module's own failure mode
    # (`test_a_repeated_author_residue_is_refused_not_merged`). For a polymer
    # residue that also writes two rows sharing one label_asym_id +
    # label_seq_id + label_atom_id; for a non-polymer, where label_seq_id is
    # `.`, such rows are normal and only auth_seq_id tells them apart.
    residue_keys: list[tuple[str, int, str]] = []
    residue_resname: dict[tuple[str, int, str], str] = {}
    residue_record: dict[tuple[str, int, str], str] = {}
    seen: set[tuple[str, int, str]] = set()
    previous_key: Optional[tuple[str, int, str]] = None
    for atom in atoms:
        key = (atom.chain, atom.seq, atom.icode)
        if key != previous_key:
            if key in seen:
                raise NumberingError(
                    f"author residue {atom.chain}{atom.seq}{atom.icode} appears "
                    "more than once in the first model; this parser will not "
                    "guess which copy a hotspot means"
                )
            seen.add(key)
            residue_keys.append(key)
            residue_resname[key] = atom.resname
            residue_record[key] = atom.record
            previous_key = key
        elif atom.resname != residue_resname[key]:
            # Same author key, different residue name: two residues, not one.
            # Taking the first name would classify the pair by that name alone
            # -- a water numbered to continue the chain would be handed the
            # chain's asym and a `label_seq_id`, and deposited
            # microheterogeneity (two residue types at one position) would write
            # two rows identical in label_asym_id + label_seq_id +
            # label_atom_id. The repeated-key guard above cannot see either,
            # because the key never changes
            # (`test_a_resname_change_at_one_author_key_is_refused`,
            # `test_a_nonpolymer_sharing_a_polymer_author_number_is_refused`).
            raise NumberingError(
                f"author residue {atom.chain}{atom.seq}{atom.icode} carries two "
                f"residue names ({residue_resname[key]} and {atom.resname}); "
                "this parser will not guess which one a hotspot means"
            )
        elif atom.record != residue_record[key]:
            # Same author key and the same residue name, but one atom is an
            # ATOM record and the other a HETATM. The record column is a
            # classification input (`_is_polymer`), so absorbing the difference
            # would let the first atom's record decide the whole residue's
            # entity and `label_seq_id` -- the same shape of bug as the resname
            # check above, for the other field. Checked after that one, so a
            # pair differing in BOTH fields reports the resname, which is the
            # more specific fact
            # (`test_a_record_type_change_at_one_author_key_is_refused`).
            raise NumberingError(
                f"author residue {atom.chain}{atom.seq}{atom.icode} mixes "
                f"{residue_record[key]} and {atom.record} records; this parser "
                "will not guess whether it is a chain residue"
            )

    # One asym per author chain for the polymer, plus one asym per
    # (chain, resname) for everything else. In PDB format waters, glycans and
    # metals normally carry the protein's own chain id, so leaving them inside
    # the polymer asym would declare an entity of type `polymer` that holds
    # water, and a reader re-deriving numbering over that asym would count the
    # waters (`test_waters_get_their_own_asym_and_entity`). A non-polymer asym
    # id is written `<chain>_<resname>`; an author chain id is one column wide,
    # so it can never collide with one.
    asym_order: list[str] = []
    asym_type: dict[str, str] = {}
    asym_of: dict[tuple[str, int, str], str] = {}
    for key in residue_keys:
        chain, _seq, _icode = key
        polymer = _is_polymer(residue_record[key], residue_resname[key])
        asym = chain if polymer else chain + "_" + residue_resname[key]
        asym_of[key] = asym
        if asym not in asym_type:
            asym_order.append(asym)
            asym_type[asym] = "polymer" if polymer else "non-polymer"
    entity_of = {asym: i + 1 for i, asym in enumerate(asym_order)}

    entries: list[ResidueEntry] = []
    next_label_seq: dict[str, int] = {asym: 1 for asym in asym_order}
    label_of: dict[tuple[str, int, str], Optional[int]] = {}
    for key in residue_keys:
        chain, seq, icode = key
        resname = residue_resname[key]
        asym = asym_of[key]
        if _is_polymer(residue_record[key], resname):
            label_seq: Optional[int] = next_label_seq[asym]
            next_label_seq[asym] += 1
        else:
            label_seq = None
        label_of[key] = label_seq
        entries.append(ResidueEntry(
            author=AuthorResidue(chain=chain, seq=seq, icode=icode),
            resname=resname,
            label_asym_id=asym,
            label_entity_id=entity_of[asym],
            label_seq_id=label_seq,
        ))
    mapping = ResidueMapping(entries)

    lines: list[str] = [f"data_{entry_id}", "#", f"_entry.id {_cif_value(entry_id)}", "#"]

    # _entity: one per asym, so a non-polymer group such as `A_HOH` gets its
    # own. `pdbx_description` carries the author chain id so
    # that a human reading the CIF can tie an entity back to the upload.
    lines += ["loop_", "_entity.id", "_entity.type", "_entity.pdbx_description"]
    for asym in asym_order:
        lines.append(" ".join([
            str(entity_of[asym]),
            asym_type[asym],
            _cif_value("author chain " + asym.split("_")[0]),
        ]))
    lines.append("#")

    # _entity_poly_seq: the observed polymer residues only, see the docstring.
    lines += [
        "loop_", "_entity_poly_seq.entity_id", "_entity_poly_seq.num",
        "_entity_poly_seq.mon_id",
    ]
    for entry in mapping.entries:
        if entry.is_polymer:
            lines.append(
                f"{entry.label_entity_id} {entry.label_seq_id} {entry.resname}"
            )
    lines.append("#")

    lines += ["loop_", "_struct_asym.id", "_struct_asym.entity_id"]
    for asym in asym_order:
        lines.append(f"{_cif_value(asym)} {entity_of[asym]}")
    lines.append("#")

    lines += ["loop_"] + [f"_atom_site.{f}" for f in _ATOM_SITE_FIELDS]
    for i, atom in enumerate(atoms, start=1):
        key = (atom.chain, atom.seq, atom.icode)
        lines.append(" ".join(_cif_value(v) for v in (
            atom.record,
            i,
            atom.element,
            atom.name,
            atom.altloc or None,
            atom.resname,
            asym_of[key],
            entity_of[asym_of[key]],
            label_of[key],
            atom.icode or None,
            f"{atom.x:.3f}",
            f"{atom.y:.3f}",
            f"{atom.z:.3f}",
            f"{atom.occupancy:.2f}",
            f"{atom.bfactor:.2f}",
            atom.seq,
            atom.resname,
            atom.chain,
            atom.name,
            1,
        )))
    lines.append("#")
    return "\n".join(lines) + "\n", mapping


def label_range_for_chain(mapping: ResidueMapping, chain: str) -> tuple[int, int]:
    """First and last ``label_seq_id`` of a chain, for a contig segment.

    A contig segment such as ``A17-131`` names residue ids in the same numbering
    as the condition spec, i.e. ``label_seq_id`` (scope §4.1), so building one
    from author numbers is the same bug as a mis-numbered hotspot. Proteina
    additionally requires a contig to cover its whole chain
    (scope §4.1); whether RFD4 does is not known here and is step 2's to
    settle.
    """
    labels = [e.label_seq_id for e in mapping.polymer_entries(chain)]
    if not labels:
        raise NumberingError(f"chain {chain!r} has no polymer residues")
    return min(labels), max(labels)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Hotspots -- §10 item 6, DECISION NOT MADE
# ---------------------------------------------------------------------------

# Upstream hotspots are atom-level: `sel('A/*/56/[CG,OH]')` names atoms, while
# our picker emits a residue (scope §4.1, §10 item 6). Which atoms a picked
# residue expands to is a scientific decision nobody has made, so it is made in
# exactly one place -- this constant and `hotspot_atoms_for_residue` below -- and
# nothing else in the pipeline may hard-code an atom name. Changing the decision
# means editing BOTH: `hotspot_atoms_for_residue` refuses any value of this
# constant it does not implement, so the constant alone is a label, not a switch.
#
# Options, with consequences, are written up in
# `docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md` §10A.
RFD4_HOTSPOT_ATOM_SET = "sidechain_heavy"

# Backbone atom names, excluded from the "sidechain_heavy" set. `OT1`/`OT2` are
# terminal-oxygen names used in place of `OXT`; which writers still emit them is
# uncited here. Without them a C-terminal glycine returns ('OT1', 'OT2') instead
# of taking the CA fallback (`test_old_style_terminal_oxygens_are_backbone`).
_BACKBONE = frozenset({"N", "CA", "C", "O", "OXT", "OT1", "OT2"})


def hotspot_atoms_for_residue(
    resname: str, observed_atom_names: Iterable[str],
) -> tuple[str, ...]:
    """Expand ONE picked residue to the atom names a hotspot selection names.

    THE SINGLE POINT OF CHANGE for §10 item 6. ``observed_atom_names`` is what
    the structure actually contains for that residue, so a side chain truncated
    by the crystallographer cannot produce a selection naming an absent atom.

    Under ``RFD4_HOTSPOT_ATOM_SET == "sidechain_heavy"`` this returns the
    residue's non-hydrogen side-chain atoms, falling back to ``CA`` when there
    are none -- glycine, and any residue modelled to backbone only
    (``test_glycine_hotspot_falls_back_to_CA``).

    No claim is made that this atom set is the right scientific choice; it has
    not been compared against any alternative on a GPU run. It is the option
    §10A recommends, and §10 item 6 stays open.
    """
    if RFD4_HOTSPOT_ATOM_SET != "sidechain_heavy":  # pragma: no cover
        raise NumberingError(
            f"unimplemented hotspot atom set {RFD4_HOTSPOT_ATOM_SET!r}"
        )
    # Alternate conformers present each heavy atom once per altloc, so the
    # observed names arrive with duplicates; a selection must name each atom
    # once (`test_alternate_conformers_do_not_duplicate_an_atom_name`).
    names: list[str] = []
    for raw in observed_atom_names:
        if raw and raw not in names:
            names.append(raw)
    sidechain = tuple(
        n for n in names
        if n not in _BACKBONE and not n.startswith("H") and not n[0].isdigit()
    )
    if sidechain:
        return sidechain
    if "CA" in names:
        return ("CA",)
    raise NumberingError(
        f"residue {resname} has no side-chain atom and no CA to fall back to"
    )


def format_hotspot_selection(
    label_asym_id: str, label_seq_id: int, atom_names: Sequence[str],
) -> str:
    """Render one atom-level hotspot selection in upstream's documented shape.

    The shape is taken from scope §4.1's single documented example,
    ``sel('A/*/56/[CG,OH]')``: chain, a ``*`` field, residue id, atom list. What
    the second field selects is NOT established -- no upstream source read in
    this repo states it, and ``*`` is the only value we have seen -- so it is
    emitted literally. Step 2 will confirm the whole expression against the
    container; until then nothing here has been accepted by ``rfd4-generate``.
    """
    if not atom_names:
        raise NumberingError("a hotspot selection needs at least one atom name")
    atoms = ",".join(atom_names)
    return f"sel('{label_asym_id}/*/{label_seq_id}/[{atoms}]')"
