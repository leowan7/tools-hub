"""Pure geometry, scoring and verdict logic for the Proteina hotspot canary.

WHY THIS MODULE IS SEPARATE FROM ``_hotspot_canary.py``.

``_hotspot_canary.py`` imports ``modal`` at module scope and builds a
``modal.App`` / ``modal.Image`` when the module body runs, so a test suite that
imported it would (a) be unrunnable anywhere ``modal`` is absent and (b) drag
Modal client construction into every offline run. The consequence was that NOT
ONE of the harness's lines was covered — the thing whose entire job is to be the
last gate before ~$16 of GPU spend had never itself been tested.

Everything here is stdlib-only, imports no third-party package at all, and
touches neither the network nor the filesystem, so
``tests/test_proteina_canary.py`` can execute all of it offline and
``_hotspot_canary.py`` never has to be imported to test the logic that decides
anything.

The split is not cosmetic. Every historical defect in the canary lived in this
logic, not in the Modal plumbing:

* a *perfect* ``centroid_distance_median`` of ``0.0`` is falsy, so
  ``(x or 999) <= 10.0`` turned the best possible result into a FAIL;
* ``(neg_cross or 0) <= 0.2`` turned an UNMEASURABLE negative control into a
  PASS, which is the one direction that must never happen;
* a hard ``len(good) >= 6`` condemned the feature when the per-design outputs
  turned out not to be complexes at all — i.e. it reported FAIL on an
  *unmeasurable*, which reads to the operator exactly like "the feature is
  broken".

So the outcome type here is deliberately THREE-valued — PASS / FAIL /
INCONCLUSIVE — and every numeric comparison uses an explicit ``is None`` check
rather than ``or <default>``. ``Verdict.__bool__`` raises on purpose: the bugs
above all came from treating a result as a truthy scalar, and the type now
refuses to be used that way.

Geometry conventions, all chosen to match what upstream actually matches on:

* upstream keys hotspots as the literal concatenation ``f"{chain_id}{res_id}"``
  with no separator and no insertion code, so residues here are keyed
  ``(chain, resseq)`` and an insertion-coded twin collapses onto its parent;
* a "residue" is a polymer residue with a CA — ATOM records, plus HETATM CA for
  the modified residues biotite treats as protein. Two independent filters run
  here and they cover different things, so neither is redundant: solvent,
  buffer and ion RESNAMES are dropped by ``heavy_atoms`` (that is what keeps a
  calcium ion — ``HETATM`` with BOTH atom name and residue name ``CA`` — out of
  the residue set), while ``ca_positions`` additionally drops every HETATM CA
  whose resname is not in ``MODRES_EQUIV``. The second filter exists to stay in
  LOCKSTEP with ``run_pipeline.pdb_ca_residues``, which applies exactly the same
  rule: a non-standard amino acid such as NLE or ORN is a HETATM that no solvent
  list contains, and counting it as a residue here while upstream does not would
  let the canary compute a patch upstream then refuses.
  THE TWO FILTERS ARE NOT IDENTICAL OVERALL, and the one place they diverge is
  written down here rather than left to be rediscovered: ``heavy_atoms`` drops
  ``SOLVENT_RESNAMES`` on ``ATOM`` records too, which ``pdb_ca_residues`` does
  not. A calcium written as an ``ATOM`` record — legal, and what some
  minimisation pipelines emit — is a residue upstream and is not one here. The
  direction is the safe one (the canary is strictly stricter, so it can only
  decline to select a token upstream would have accepted, never propose one
  upstream would reject) and it is pinned by a test so it cannot silently
  invert.
* contact detection uses ALL heavy atoms of a polymer residue (sidechains make
  interfaces), but the centroid uses CA only — it is compared against a 10 A
  threshold and averaging whole residues lets a long sidechain move the answer
  by a meaningful fraction of that.
* WHICH CHAIN IS THE TARGET IS NEVER ASSUMED. The design output's chain labels
  are upstream's to choose, and nothing in the run guarantees they match the
  input PDB's. If Proteina emits the binder as chain ``A`` and the target as
  chain ``B`` while the input target was chain ``A``, then scoring "every chain
  in ``target_chains``" measures the BINDER's self-contacts and reports
  ``hotspot_recall = 1.0`` — a fabricated PASS on $12 of GPU time. So every
  design is checked against the input target's residues (``verify_target_identity``)
  before any geometry is scored, and a design that fails the check is marked
  UNSCORABLE, which flows to INCONCLUSIVE and never to PASS or FAIL.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, NamedTuple, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# "The binder touches this target residue". 5.0 A between heavy atoms is the
# usual interface cutoff (BioPython / PDBe conventions) and is forgiving enough
# that a real interface is never missed.
CONTACT_A = 5.0

# A patch this far from the requested one is unambiguously a different site.
# With a 5 A contact cutoff, a binder centred on one cannot touch the other.
#
# Measured to the NEAREST POSITIVE HOTSPOT, never to the positive patch's
# centroid. A 4-residue patch spans 10-15 A, so a residue a nominal 25 A from
# the centroid can sit ~17 A from the closest hotspot — and a 60-120 residue
# binder has a maximum dimension well over 40 A, so it would routinely brush
# the positive site. That is a spurious phase-2 FAIL, i.e. a $12 CONDEMNATION
# of a feature that works.
NEGATIVE_MIN_SEPARATION_A = 25.0

# Registry keys the canary writes. Every one starts with this so the container
# hygiene step can prune exactly its own records and nothing else.
CANARY_KEY_PREFIX = "hub_canary"

# CA neighbours within 10 A — the standard cheap burial proxy (a "coordination
# number"). In globular proteins exposed residues typically sit around 10-15 and
# core residues around 20-25, so 18 is a conservative ceiling for "this is on
# the surface".
#
# IT IS A PROXY, NOT SASA, AND ITS BIAS COSTS MONEY IN A SPECIFIC DIRECTION.
# A CA-neighbour count undercounts on small, extended, elongated or
# low-resolution structures — there are simply fewer CAs within 10 A of
# anything — so a genuinely buried site can clear this ceiling and be ACCEPTED
# as the negative control. Spell out what that then costs, because the
# consequence is not obvious and was previously left unstated:
#
#     accepted-but-actually-buried negative patch
#       -> no binder can dock there, for reasons that have nothing to do with
#          hotspots
#       -> the negative shard's own ``centroid_distance_median`` comes back
#          large
#       -> ``negative_verdict`` returns FAIL
#       -> phase 2 spends ~$12 to CONDEMN a feature that works.
#
# The bias is therefore toward a false condemnation, not toward a false pass.
# Two cheap mitigations, both scale-free where the absolute ceiling is not:
# ``pick_far_patch`` also requires the seed to be no more buried than the
# structure's OWN median residue, and it reports ``burial_proxy_reliable``
# false on structures too small for the absolute ceiling to mean anything
# (``BURIAL_PROXY_MIN_RESIDUES``). The raw counts are reported either way so
# the operator can judge, and ``--negative`` overrides the whole selection.
MAX_SURFACE_NEIGHBOURS = 18

# Below this many polymer residues the absolute neighbour ceiling above is not
# meaningful — a 40-residue toy or a short peptide simply cannot accumulate 18
# CA neighbours anywhere, so "under the ceiling" carries no information.
BURIAL_PROXY_MIN_RESIDUES = 50

# Minimum fraction of a design's putative-TARGET residues that must be
# accounted for by the input target's selected residue set, and the minimum
# fraction of those that must carry the SAME residue name. Both are needed:
# a de-novo 100-residue binder numbered 1..100 is a perfect key-subset of a
# 115-residue target, so key overlap alone certifies exactly the inversion it
# is supposed to catch. Sequence identity is what actually distinguishes "this
# is the protein we uploaded" from "this is something else with the same
# numbers". 0.9 leaves room for a handful of modified residues.
TARGET_MIN_KEY_COVERAGE = 0.9
TARGET_MIN_SEQUENCE_IDENTITY = 0.9
# ...and an absolute floor, so a two-residue coincidence cannot certify a
# target. Capped at the reference size so a genuinely tiny target still works.
TARGET_MIN_MATCHED_RESIDUES = 10

# HOW MUCH OF THAT IDENTITY HAS TO BE EVIDENCE. An UNK/UNX/XAA residue name
# means "I do not know what this is", so comparing one against anything answers
# nothing — and the gate used to count that non-answer as a MATCH. A design
# chain whose residues are ALL unknown therefore scored sequence_identity = 1.0
# against ANY reference and was certified as the target, which is precisely the
# output shape a backbone generative model is most likely to emit: Proteina
# generates backbones, and a backbone has no sequence. The relabelled binder
# then reported a fabricated hotspot_recall off its own self-contacts, and
# because every design "verified", the UNSCORABLE note and the chain map that
# would have shown the inversion were suppressed by the gate meant to catch it.
#
# So identity is now computed over INFORMATIVE pairs only — both names known —
# and a chain has to carry enough of them before identity may certify anything:
# an absolute count (capped at what the reference actually offers, so a small
# target still works) and a fraction of the matched keys. Both are applied
# POOLED and PER CHAIN; see verify_target_identity for why per-chain is not
# redundant. A handful of unknowns inside an otherwise-identified chain is
# still fine, which is the case the wildcard was written for.
TARGET_MIN_INFORMATIVE_RESIDUES = 10
TARGET_MIN_INFORMATIVE_FRACTION = 0.5

# Kept deliberately in lockstep with run_pipeline._MODRES_EQUIV — a residue that
# upstream counts as protein must be selectable as a hotspot here too, or the
# canary would compute a patch upstream cannot accept. tests assert the two sets
# are identical so they cannot drift apart silently.
MODRES_EQUIV = frozenset({
    "MSE", "CME", "CSO", "SEP", "TPO", "PTR", "KCX", "HYP", "LLP",
    "CSD", "OCS", "MLY", "M3L", "CAS", "CSS", "CSX", "PCA", "SAC",
})

# The parent amino acid of each of the above. Used ONLY when comparing a design
# output's residue names against the input target's: refold/relax steps
# routinely write MSE back as MET, and counting that as a mismatch would push a
# perfectly good target below the identity floor and make a real run
# unscorable. Never used for selection.
MODRES_PARENT = {
    "MSE": "MET", "CME": "CYS", "CSO": "CYS", "SEP": "SER", "TPO": "THR",
    "PTR": "TYR", "KCX": "LYS", "HYP": "PRO", "LLP": "LYS", "CSD": "CYS",
    "OCS": "CYS", "MLY": "LYS", "M3L": "LYS", "CAS": "CYS", "CSS": "CYS",
    "CSX": "CYS", "PCA": "GLU", "SAC": "SER",
}

# "I do not know what this residue is". Matches anything, in either direction.
UNKNOWN_RESNAMES = frozenset({"UNK", "UNX", "XAA", "X"})

# Solvent, buffer and ion residue names. Never polymer, never a hotspot, and
# never a legitimate contact "residue".
SOLVENT_RESNAMES = frozenset({
    "HOH", "WAT", "DOD", "D2O", "TIP", "TIP3", "SOL",
    "SO4", "PO4", "EDO", "GOL", "PEG", "PG4", "MPD", "DMS", "ACT", "ACY",
    "FMT", "TRS", "IMD", "EPE", "MES", "BME", "NO3", "AZI",
    "NA", "K", "CL", "BR", "IOD", "F", "LI", "CS", "RB",
    "MG", "CA", "ZN", "MN", "FE", "FE2", "CU", "CU1", "CO", "NI", "CD",
    "SR", "BA", "HG", "AU", "PT", "AG",
})

# Outcomes. Three, not two: "we could not measure it" is a distinct answer from
# "we measured it and it is wrong", and conflating them either condemns a
# working feature or blesses a broken one.
PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"
OUTCOMES = (PASS, FAIL, INCONCLUSIVE)

# Process exit codes. INCONCLUSIVE is non-zero (it is not a green light) but
# distinct from FAIL (it is not a condemnation either).
EXIT_CODES = {PASS: 0, FAIL: 1, INCONCLUSIVE: 3}

# Phase 0's controls, named explicitly. Aggregating with `all(... for v in
# results.values() if isinstance(v, dict))` is vacuously True over an empty or
# renamed set of controls, which is a silent pass.
PHASE0_CONTROLS = ("typo_control", "warm_container_control")

# The only phases that exist. ``main`` used to branch ``if phase == 0: ... if
# phase == 1: ...`` and FALL THROUGH to phase 2, so ``--phase 3`` or ``--phase
# -1`` silently spawned the three-shard ~$12 run.
KNOWN_PHASES = (0, 1, 2)

# "A45" / "A-5" / "AB45". Greedy on letters so a multi-character chain id keeps
# its full label, exactly as upstream's f"{chain_id}{res_id}" would render it.
_TOKEN_RE = re.compile(r"^([A-Za-z]+)(-?\d+)$")


# ---------------------------------------------------------------------------
# The console
#
# A PRINT MUST NEVER BE ABLE TO KILL THE RUN, BECAUSE THE PRINT HAPPENS AFTER
# THE MONEY IS COMMITTED. This is a live defect, not a hypothetical: upstream's
# ``complexa target add`` prints
#
#     "  ✓ Updated target '<key>'"      and
#     "  \U0001f4cd Saved to: configs/targets/targets_dict.yaml"
#
# and ``run_shard`` runs that command INSIDE the container, i.e. after three
# A100s are already burning in phase 2. Modal streams container output to the
# local console, and writing either of those characters to a Windows cp1252
# console raises ``UnicodeEncodeError: 'charmap' codec can't encode character
# '✓'``. On 2026-08-04 that killed ``--phase 0`` outright. In phase 2 the
# same raise lands between ``spawn`` and ``get``, where the three shards bill on
# to completion or to the 7200 s cap: ~$12-$38 for nothing.
#
# ``PYTHONIOENCODING=utf-8`` makes it go away and is NOT the fix; an operator
# forgets it exactly once, and the once is the expensive one.
#
# WHY THE STREAM IS RECONFIGURED IN PLACE RATHER THAN WRAPPED. The text that
# kills the run is printed by code this repo does not own — modal's log pump,
# rich's renderer, the interpreter's own traceback printer — all of which hold
# their own reference to whatever ``sys.stdout`` was when they started.
# Replacing ``sys.stdout`` with a safe wrapper does nothing for a holder of the
# original; mutating the original's error handler fixes every holder at once,
# including ones that do not exist yet. Wrapping is therefore only the fallback
# for a stream that cannot be reconfigured at all.
#
# Sanitising at our own print sites (``_hotspot_canary._say``) is the second
# layer and does not replace this one: it can only protect strings this repo
# formats, and the string that actually did the killing was upstream's.
# ---------------------------------------------------------------------------

# ``backslashreplace`` rather than ``replace``: the operator needs to be able to
# tell WHICH character could not be rendered. "✓" says "upstream printed a
# tick"; "?" says nothing, and a screen of "?" is how a cosmetic-looking
# encoding problem gets ignored until it costs $12.
CONSOLE_ERRORS = "backslashreplace"


def safe_text(value: Any, encoding: str | None = None,
              errors: str = CONSOLE_ERRORS) -> str:
    """``value`` rendered so that encoding it to ``encoding`` cannot raise.

    Lossless when the console can carry the text — encoding to UTF-8 and back
    is the identity — so nothing is mangled on a console that was never going
    to fail. ``None``/unknown encodings degrade to ASCII, which every console
    can take.
    """
    text = value if isinstance(value, str) else str(value)
    for candidate in (encoding, "ascii"):
        if not candidate:
            continue
        try:
            return text.encode(candidate, errors).decode(candidate, "replace")
        except (LookupError, UnicodeError, TypeError, ValueError):
            continue
    # Last resort: the error handler itself was unusable.
    return text.encode("ascii", "backslashreplace").decode("ascii")


class SafeStream:
    """Delegating proxy whose ``write`` cannot raise ``UnicodeEncodeError``.

    The FALLBACK, used only for a stream with no working ``reconfigure`` — a
    reconfigured stream is strictly better because it keeps its identity, so
    every existing holder of ``sys.stdout`` is fixed too (see the section
    comment above). Everything except ``write`` is delegated, so ``fileno``,
    ``encoding``, ``isatty`` and ``buffer`` keep working: ``run_shard`` hands
    ``sys.stdout`` straight to ``subprocess.run``, which needs a real fd.
    """

    def __init__(self, stream: Any, errors: str = CONSOLE_ERRORS) -> None:
        object.__setattr__(self, "_stream", stream)
        object.__setattr__(self, "_errors", errors)

    def write(self, text: Any) -> int:
        stream = object.__getattribute__(self, "_stream")
        try:
            return stream.write(text)
        except UnicodeEncodeError:
            return stream.write(safe_text(
                text, getattr(stream, "encoding", None),
                object.__getattribute__(self, "_errors")))

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_stream"), name)


def harden_stream(stream: Any, errors: str = CONSOLE_ERRORS) -> Any:
    """The stream to use in place of ``stream``, unable to raise on an
    unencodable character.

    Returns the SAME object whenever it could be reconfigured, which is the
    whole point — see the section comment. Returns ``None`` for ``None``
    (``pythonw`` has no stdout) and the original object when it has no encoding
    to fail at (``io.StringIO``, pytest's capture), because wrapping something
    that cannot raise buys nothing and only adds a layer between the caller and
    a real file descriptor.
    """
    if stream is None:
        return None
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(errors=errors)
            return stream
        except (ValueError, OSError, TypeError, AttributeError, LookupError):
            pass
    if not getattr(stream, "encoding", None):
        return stream
    if isinstance(stream, SafeStream):
        return stream
    return SafeStream(stream, errors)


# ---------------------------------------------------------------------------
# PDB parsing
# ---------------------------------------------------------------------------


class Atom(NamedTuple):
    chain: str
    resseq: int
    icode: str
    resname: str
    name: str
    element: str
    hetatm: bool
    x: float
    y: float
    z: float


Residue = tuple[str, int]
Point = tuple[float, float, float]


def heavy_atoms(pdb_text: str, *, drop_solvent: bool = True) -> list[Atom]:
    """Every non-hydrogen atom of model 1.

    Stops at the first ``ENDMDL`` (matching shared/pdb_inspect.py's single-model
    rule for NMR ensembles) and drops hydrogens and deuteriums. When the element
    column is blank the symbol is taken from the atom name with any leading
    digits stripped first — PDB v2 writes some hydrogens as ``1HB``, and taking
    ``name[:1]`` there yields ``"1"`` and lets the hydrogen through.
    """
    out: list[Atom] = []
    for line in pdb_text.splitlines():
        record = line[:6]
        if record.startswith("ENDMDL"):
            break
        if record not in ("ATOM  ", "HETATM"):
            continue
        name = line[12:16].strip()
        element = line[76:78].strip().upper()
        if not element:
            element = name.lstrip("0123456789")[:1].upper()
        if element in ("H", "D"):
            continue
        resname = line[17:20].strip().upper()
        if drop_solvent and resname in SOLVENT_RESNAMES:
            continue
        try:
            x, y, z = float(line[30:38]), float(line[38:46]), float(line[46:54])
            resseq = int(line[22:26])
        except ValueError:
            continue
        out.append(Atom(
            chain=line[21:22].strip(), resseq=resseq, icode=line[26:27].strip(),
            resname=resname, name=name, element=element,
            hetatm=(record == "HETATM"), x=x, y=y, z=z,
        ))
    return out


def _polymer_ca_atoms(atoms: Iterable[Atom]) -> Iterator[tuple[Residue, Atom]]:
    """``((chain, resseq), CA atom)`` for every POLYMER residue, first record wins.

    THE HETATM FILTER IS NOT THE CALCIUM FILTER. It used to be documented as
    load-bearing because "a calcium ion is a HETATM whose atom name AND residue
    name are both CA" — but on every path production uses, ``heavy_atoms``
    already dropped it by resname via ``SOLVENT_RESNAMES`` before this function
    saw it, so that justification described work done elsewhere and left the
    guard looking dead. It is not dead. It exists to stay in LOCKSTEP with
    ``run_pipeline.pdb_ca_residues``, which applies the identical rule
    (``if record == "HETATM" and resname not in _MODRES_EQUIV: continue``).
    Non-standard amino acids outside ``MODRES_EQUIV`` — NLE, ORN, ABA, D-amino
    acids — are HETATM records carrying a real CA and appear in no solvent
    list, so without this filter the canary would see residues upstream does
    not: it would compute a negative patch on one and ``missing_hotspots``
    would then reject the token, aborting the shard mid-flight. (It does also
    stop the calcium when a caller passes ``drop_solvent=False``, which is the
    one path where the old claim holds; both are covered by tests that fail if
    the filter is removed.)

    Altloc duplicates and insertion-coded twins collapse onto the first record
    seen, because upstream's match key carries neither.
    """
    seen: set[Residue] = set()
    for a in atoms:
        if a.name != "CA":
            continue
        if a.hetatm and a.resname not in MODRES_EQUIV:
            continue
        key = (a.chain, a.resseq)
        if key in seen:
            continue
        seen.add(key)
        yield key, a


def ca_positions(atoms: Iterable[Atom]) -> dict[Residue, Point]:
    """``(chain, resseq) -> CA coordinate`` for every POLYMER residue.

    Genuinely CA, not "CA-ish": the centroid built from this is compared against
    a 10 A threshold, and averaging every heavy atom of a residue lets a long
    sidechain (Arg, Lys, Trp) shift it by several angstrom.
    """
    return {k: (a.x, a.y, a.z) for k, a in _polymer_ca_atoms(atoms)}


def ca_resnames(atoms: Iterable[Atom]) -> dict[Residue, str]:
    """``(chain, resseq) -> residue name`` over the same residue set.

    The sequence half of ``verify_target_identity``: coordinates say where a
    residue is, names say WHICH protein it belongs to, and only the second can
    tell a relabelled binder from the target it was designed against.
    """
    return {k: a.resname for k, a in _polymer_ca_atoms(atoms)}


def dist(a: Point, b: Point) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def centroid(points: Sequence[Point]) -> Point | None:
    if not points:
        return None
    n = len(points)
    return (sum(p[0] for p in points) / n,
            sum(p[1] for p in points) / n,
            sum(p[2] for p in points) / n)


def ca_centroid_with_coverage(
    atoms: Iterable[Atom], residues: Iterable[Residue]
) -> tuple[Point | None, int, int]:
    """``(centroid, n_found, n_requested)`` over the CA atoms of ``residues``."""
    want = list(dict.fromkeys(residues))
    pos = ca_positions(atoms)
    picked = [pos[k] for k in want if k in pos]
    return centroid(picked), len(picked), len(want)


def ca_centroid(atoms: Iterable[Atom], residues: Iterable[Residue]) -> Point | None:
    return ca_centroid_with_coverage(atoms, residues)[0]


def parse_spec(spec: Iterable[Any]) -> tuple[set[Residue], list[str]]:
    """``["A45", "C73"] -> ({("A",45),("C",73)}, [])``; bad tokens are RETURNED.

    The original silently ``continue``d past anything it could not read, which
    shrank the recall denominator and made a run look BETTER the more hotspot
    tokens were malformed. That is the same silent-drop failure mode the whole
    canary exists to catch, so unreadable tokens are surfaced instead.
    """
    ok: set[Residue] = set()
    bad: list[str] = []
    for raw in spec or ():
        token = str(raw).strip()
        if not token:
            continue
        m = _TOKEN_RE.match(token)
        if not m:
            bad.append(token)
            continue
        ok.add((m.group(1), int(m.group(2))))
    return ok, bad


def chains_present(atoms: Iterable[Atom]) -> list[str]:
    return sorted({a.chain for a in atoms})


def contacts_from_atoms(
    atoms: Sequence[Atom], target_chains: Iterable[str], cutoff: float = CONTACT_A,
    positions: dict[Residue, Point] | None = None,
) -> set[Residue]:
    """Target POLYMER residues with a heavy atom within ``cutoff`` of a binder atom.

    Any chain not in ``target_chains`` is treated as binder. Restricting the
    target side to residues that have a CA keeps the contact set comparable with
    the hotspot token set — a ligand or ion "contact" can never be a hotspot, so
    counting one inflates the denominator and drags the contact centroid.

    Brute force is fine: a shard emits a handful of designs of a few thousand
    atoms each.
    """
    wanted = set(target_chains)
    polymer = ca_positions(atoms) if positions is None else positions
    tgt = [a for a in atoms if a.chain in wanted and (a.chain, a.resseq) in polymer]
    binder = [a for a in atoms if a.chain not in wanted]
    if not tgt or not binder:
        return set()
    cutoff2 = cutoff * cutoff
    hits: set[Residue] = set()
    for t in tgt:
        key = (t.chain, t.resseq)
        if key in hits:
            continue
        for b in binder:
            dx, dy, dz = t.x - b.x, t.y - b.y, t.z - b.z
            if dx * dx + dy * dy + dz * dz <= cutoff2:
                hits.add(key)
                break
    return hits


def contacts(pdb_text: str, target_chains: Iterable[str],
             cutoff: float = CONTACT_A) -> set[Residue]:
    return contacts_from_atoms(heavy_atoms(pdb_text), target_chains, cutoff)


# ---------------------------------------------------------------------------
# Is the thing we are about to call "the target" actually the target?
# ---------------------------------------------------------------------------


def is_unknown_resname(name: Any) -> bool:
    """``UNK`` / ``UNX`` / ``XAA`` / ``X`` — "I do not know what this is"."""
    return str(name).strip().upper() in UNKNOWN_RESNAMES


def is_informative_pair(a: Any, b: Any) -> bool:
    """Does comparing these two residue names carry ANY evidence?

    It does not when either side is unknown. ``same_residue`` returns True for
    that pair by design — a stray UNK inside an otherwise-identified chain must
    not drag a real target below the identity floor — but a True that means "I
    could not tell" cannot be counted alongside a True that means "these agree",
    because a chain of nothing but unknowns then scores a perfect identity
    against any reference at all. Every caller that turns ``same_residue`` into
    a FRACTION must therefore restrict the denominator with this.
    """
    return not is_unknown_resname(a) and not is_unknown_resname(b)


def same_residue(a: str, b: str) -> bool:
    """Do two residue names denote the same residue for identity purposes?

    Equal, or equal after mapping a modified residue to its parent, or either
    side unknown. Deliberately permissive: this comparison exists to catch a
    WHOLE CHAIN being a different protein, not to audit point mutations, and a
    false mismatch here makes a correct run unscorable.

    THE WILDCARD IS NOT A LICENCE TO CERTIFY. "Either side unknown" answers
    "should this pair count against the chain?", not "is this pair evidence
    FOR the chain". Pair it with ``is_informative_pair`` whenever the result is
    aggregated into an identity fraction, or an all-UNK chain reads as a perfect
    match — see ``TARGET_MIN_INFORMATIVE_RESIDUES``.
    """
    a, b = a.strip().upper(), b.strip().upper()
    if a == b:
        return True
    if a in UNKNOWN_RESNAMES or b in UNKNOWN_RESNAMES:
        return True
    return MODRES_PARENT.get(a, a) == MODRES_PARENT.get(b, b)


class IdentityStats(NamedTuple):
    """Matched / informative / identical counts over one set of residue keys."""

    n_observed: int
    n_matched: int
    n_informative: int
    n_identical: int

    @property
    def key_coverage(self) -> float | None:
        return (self.n_matched / self.n_observed) if self.n_observed else None

    @property
    def sequence_identity(self) -> float | None:
        """Identity over INFORMATIVE pairs, or None when there are none.

        ``None`` means "no sequence evidence", which is NOT the same as 0.0 and
        must never be compared against the floor as though it cleared it — the
        all-unknown chain reaches here.
        """
        if not self.n_informative:
            return None
        return self.n_identical / self.n_informative

    @property
    def informative_fraction(self) -> float | None:
        return (self.n_informative / self.n_matched) if self.n_matched else None


def identity_stats(observed: dict[Residue, str], reference: dict[Residue, str],
                   keys: Iterable[Residue] | None = None) -> IdentityStats:
    """Count matched / informative / identical residues over ``keys``."""
    subset = list(observed) if keys is None else [k for k in keys if k in observed]
    matched = [k for k in subset if k in reference]
    informative = [k for k in matched
                   if is_informative_pair(observed[k], reference[k])]
    identical = [k for k in informative if same_residue(observed[k], reference[k])]
    return IdentityStats(len(subset), len(matched), len(informative),
                         len(identical))


def informative_floor(n_reference_informative: int,
                      minimum: int = TARGET_MIN_INFORMATIVE_RESIDUES) -> int:
    """How many informative residues a chain must carry before it may certify.

    Capped at what the reference can actually supply, so a genuinely tiny
    target still works — but never below 1, because zero informative residues is
    exactly the all-unknown case this floor exists to refuse. A reference that
    itself offers nothing informative yields 1, which no design can satisfy,
    and that is the correct answer: a target with no known residue names cannot
    be told apart from a binder by sequence, so nothing may be certified
    against it.
    """
    return max(1, min(int(minimum), int(n_reference_informative)))


def chain_identity_hints(atoms: Iterable[Atom],
                         reference: dict[Residue, str]) -> dict[str, dict]:
    """For each chain in a design, which reference chain does it look like?

    Label-agnostic on purpose: it matches on residue NUMBER against every
    reference chain in turn, so when upstream swaps the labels this says so out
    loud ("design chain B looks like input chain A at 100% identity") instead of
    leaving the operator to infer it from a failed check. Phase 1 exists to
    discover the output chain convention; this is the readout.

    Identity here is INFORMATIVE-ONLY for the same reason the gate's is: an
    all-UNK design chain otherwise "looks like" every reference chain at 100%,
    and the chain map is the diagnostic the operator reads to work out what
    upstream actually emitted. ``sequence_identity`` is ``None`` — never 1.0,
    never 0.0 — when the two chains share no informative pair, and
    ``n_informative`` says how much of the number is real.
    """
    atoms = list(atoms)
    observed = ca_resnames(atoms)
    ref_by_chain: dict[str, dict[int, str]] = {}
    for (chain, resseq), name in reference.items():
        ref_by_chain.setdefault(chain, {})[resseq] = name

    hints: dict[str, dict] = {}
    for chain in sorted({c for c, _ in observed}):
        mine = {r: n for (c, r), n in observed.items() if c == chain}
        best: dict | None = None
        for ref_chain, ref in sorted(ref_by_chain.items()):
            shared = [r for r in mine if r in ref]
            if not shared:
                continue
            informative = [r for r in shared
                           if is_informative_pair(mine[r], ref[r])]
            identical = sum(1 for r in informative if same_residue(mine[r], ref[r]))
            identity = (identical / len(informative)) if informative else None
            cand = {"reference_chain": ref_chain, "n_shared": len(shared),
                    "n_informative": len(informative),
                    "sequence_identity": (
                        None if identity is None else round(identity, 3))}
            rank = (-1.0 if identity is None else identity,
                    cand["n_informative"], cand["n_shared"])
            if best is None or rank > (
                -1.0 if best["sequence_identity"] is None
                else best["sequence_identity"],
                best["n_informative"], best["n_shared"],
            ):
                best = cand
        hints[chain] = {"n_residues": len(mine), "best_match": best}
    return hints


def verify_target_identity(
    atoms: Iterable[Atom],
    target_chains: Iterable[str],
    reference: dict[Residue, str],
    *,
    min_key_coverage: float = TARGET_MIN_KEY_COVERAGE,
    min_identity: float = TARGET_MIN_SEQUENCE_IDENTITY,
    min_matched: int = TARGET_MIN_MATCHED_RESIDUES,
    min_informative: int = TARGET_MIN_INFORMATIVE_RESIDUES,
    min_informative_fraction: float = TARGET_MIN_INFORMATIVE_FRACTION,
) -> dict:
    """Do the chains we are about to score as TARGET really carry the target?

    THE DEFECT THIS EXISTS FOR. ``run_shard`` knows the chain ids of the INPUT
    PDB and calls every other chain in the design output "binder". Nothing in
    the run guarantees the output preserved that labelling — the module
    docstring of ``_hotspot_canary.py`` says outright that phase 1 exists
    because the output chain convention is UNKNOWN. If Proteina emits the
    binder as chain ``A`` and the target as chain ``B`` while the input target
    was chain ``A``, the roles invert silently: ``is_complex`` stays True,
    contacts are computed over BINDER residues keyed ``("A", n)``, and hotspot
    tokens ``A37 A39 A49 A98`` resolve against BINDER residues 37/39/49/98.
    A 100-residue binder on chain A then reports ``hotspot_recall = 1.0`` and
    ``requested_found_in_structure = 4`` — a PASS on binder self-contacts,
    fabricated out of $12 of A100 time.

    THE CRITERION, and why it is not the obvious one. A pure ``(chain, resseq)``
    subset test — "every residue we score as target must be one of the input
    target's" — is the natural first idea and it does NOT catch this: a de-novo
    100-residue binder written as A1..A100 is a perfect subset of a 115-residue
    target's A1..A115. What separates them is the SEQUENCE. So two conditions
    must both hold:

      * key coverage  — at least ``min_key_coverage`` of the residues we would
        score as target must exist in the reference at the SAME (chain, resseq)
        key. This is what fails when upstream renumbers, and renumbering must
        fail: hotspot tokens are matched by number, so a renumbered target
        makes ``A37`` a different residue and every score meaningless.
      * sequence identity — at least ``min_identity`` of those matched keys
        must carry the same residue name (modified residues mapped to their
        parent). This is what fails on a role inversion.

    Plus an absolute floor of ``min_matched`` residues, capped at the reference
    size, so a two-residue coincidence cannot certify anything.

    IDENTITY IS COUNTED OVER INFORMATIVE PAIRS ONLY, AND THERE MUST BE ENOUGH
    OF THEM. ``same_residue`` treats an UNK/UNX/XAA on either side as a match,
    which is right for a stray unknown inside a real chain and catastrophic as
    a blanket rule: a design chain whose residues are ALL unknown scored 1.0
    against any reference and was CERTIFIED, and since every design then
    "verified", the UNSCORABLE note and the chain map — the diagnostics that
    would have shown the inversion — were suppressed by the gate meant to catch
    it. A backbone generative model emitting sequence-free output is the most
    likely way to hit that, not an exotic one. So the denominator excludes
    unknown pairs, and a chain must carry ``min_informative`` of them (capped
    at what the reference offers) and have at least
    ``min_informative_fraction`` of its matched keys be informative.

    EVERY CHECK IS APPLIED PER CHAIN AS WELL AS POOLED, and the per-chain half
    is not decoration. Pooling lets a minority chain be an entirely different
    molecule and still clear a 0.9 floor: a 600-residue chain A carrying the
    real target plus a foreign 60-residue chain B pools to 0.91 and verifies,
    as does a 4 x 300 homotetramer with the binder relabelled onto one chain
    (0.90). Both are role inversions on a real fraction of the interface, and
    both are refused per chain.

    The failure mode is REFUSE TO SCORE, never score wrongly: callers must
    treat ``verified: False`` as UNSCORABLE and let it flow to INCONCLUSIVE.
    """
    atoms = list(atoms)
    wanted = set(target_chains)
    observed = {k: v for k, v in ca_resnames(atoms).items() if k[0] in wanted}
    floor = min(int(min_matched), len(reference)) if reference else 0

    pooled = identity_stats(observed, reference)
    matched = pooled.n_matched
    coverage = pooled.key_coverage
    identity = pooled.sequence_identity
    ref_informative = sum(1 for v in reference.values()
                          if not is_unknown_resname(v))
    inf_floor = informative_floor(ref_informative, min_informative)

    # Computed ONCE per chain and reused by both the report and the checks
    # below, so the numbers the operator reads are literally the numbers that
    # were compared against the floors — recomputing them separately is how a
    # report drifts away from the decision it claims to explain.
    chain_stats: dict[str, IdentityStats] = {}
    per_chain: dict[str, dict] = {}
    ref_informative_by_chain: dict[str, int] = {}
    for (chain, _resseq), name in reference.items():
        ref_informative_by_chain[chain] = (
            ref_informative_by_chain.get(chain, 0)
            + (0 if is_unknown_resname(name) else 1))
    for chain in sorted({c for c, _ in observed}):
        keys = [k for k in observed if k[0] == chain]
        stats = identity_stats(observed, reference, keys)
        chain_stats[chain] = stats
        per_chain[chain] = {
            "n_observed": stats.n_observed,
            "n_matched_keys": stats.n_matched,
            "n_informative": stats.n_informative,
            "n_identical": stats.n_identical,
            "key_coverage": (None if stats.key_coverage is None
                             else round(stats.key_coverage, 4)),
            "sequence_identity": (None if stats.sequence_identity is None
                                  else round(stats.sequence_identity, 4)),
            "min_informative_residues": informative_floor(
                ref_informative_by_chain.get(chain, 0), min_informative),
        }

    out: dict[str, Any] = {
        "verified": False,
        "reason": None,
        "target_chains": sorted(wanted),
        "n_reference_residues": len(reference),
        "n_reference_informative": ref_informative,
        "n_observed": pooled.n_observed,
        "n_matched_keys": matched,
        "n_informative": pooled.n_informative,
        "n_identical": pooled.n_identical,
        "key_coverage": None if coverage is None else round(coverage, 4),
        "sequence_identity": None if identity is None else round(identity, 4),
        "informative_fraction": (
            None if pooled.informative_fraction is None
            else round(pooled.informative_fraction, 4)),
        "min_key_coverage": min_key_coverage,
        "min_sequence_identity": min_identity,
        "min_matched_residues": floor,
        "min_informative_residues": inf_floor,
        "min_informative_fraction": min_informative_fraction,
        "per_chain": per_chain,
        "chain_hints": chain_identity_hints(atoms, reference),
    }
    if not reference:
        out["reason"] = ("no reference residues were supplied, so nothing can "
                         "be verified as the target")
        return out
    if not observed:
        out["reason"] = (f"the chains treated as target {sorted(wanted)} carry no "
                         "polymer residue in this design")
        return out
    if matched < max(1, floor):
        out["reason"] = (
            f"only {matched} of the {pooled.n_observed} residues on "
            f"{sorted(wanted)} exist in the input target at the same "
            f"(chain, residue number) key — need at least {max(1, floor)}. The "
            "design output does not preserve the input numbering, so a hotspot "
            "token cannot be resolved against it")
        return out
    if coverage is not None and coverage < min_key_coverage:
        out["reason"] = (
            f"only {coverage:.0%} of the residues on {sorted(wanted)} exist in "
            f"the input target at the same key (need {min_key_coverage:.0%}) — "
            "these are not the residues we uploaded")
        return out
    reason = _informative_refusal(
        f"the chains {sorted(wanted)}", pooled, inf_floor,
        min_informative_fraction, ref_informative)
    if reason is not None:
        out["reason"] = reason
        return out
    if identity is not None and identity < min_identity:
        out["reason"] = (
            f"the residues on {sorted(wanted)} match the input target's "
            f"numbering but only {identity:.0%} of their {pooled.n_informative} "
            f"informative residue NAMES agree (need {min_identity:.0%}): this "
            "chain is a different protein. The design output almost certainly "
            "labelled the binder with the input target's chain id — see "
            "chain_hints")
        return out

    # Per chain, because a pooled fraction hides a minority chain that is an
    # entirely different molecule — 10% of the target may not be a foreign one.
    for chain in sorted(per_chain):
        entry = per_chain[chain]
        stats = chain_stats[chain]
        chain_coverage = stats.key_coverage
        if chain_coverage is not None and chain_coverage < min_key_coverage:
            out["reason"] = (
                f"chain {chain} has only {chain_coverage:.0%} of its "
                f"{stats.n_observed} residues in the input target at the same "
                f"key (need {min_key_coverage:.0%}) — these are not the "
                "residues we uploaded. A pooled coverage hides a single chain "
                "that is something else entirely; see per_chain")
            return out
        reason = _informative_refusal(
            f"chain {chain}", stats, entry["min_informative_residues"],
            min_informative_fraction, ref_informative_by_chain.get(chain, 0))
        if reason is not None:
            out["reason"] = reason
            return out
        chain_identity = stats.sequence_identity
        if chain_identity is not None and chain_identity < min_identity:
            out["reason"] = (
                f"chain {chain} matches the input target's numbering but only "
                f"{chain_identity:.0%} of its {stats.n_informative} informative "
                f"residue NAMES agree (need {min_identity:.0%}): this chain is "
                "a different protein, even though the target as a whole pooled "
                f"to {identity:.0%}. Scoring it would let a foreign molecule "
                "supply part of the interface — see per_chain and chain_hints")
            return out
    out["verified"] = True
    return out


def _informative_refusal(subject: str, stats: IdentityStats, floor: int,
                         min_fraction: float, n_reference_informative: int
                         ) -> str | None:
    """Why ``subject`` carries too little sequence evidence to certify, or None.

    Shared by the pooled and the per-chain half of ``verify_target_identity`` so
    the two cannot drift, and separate from the identity comparison because "the
    names disagree" and "there are no names" are different findings with
    different remedies.
    """
    if not n_reference_informative:
        return (
            f"{subject} cannot be verified: the INPUT target itself carries no "
            "known residue name (every reference residue is UNK/UNX/XAA), so "
            "no sequence can tell the target apart from a binder. Upload a "
            "target with a sequence, or accept that occupancy is not "
            "measurable for this input")
    if stats.n_informative < floor:
        return (
            f"{subject} carry only {stats.n_informative} residues whose name is "
            f"known on BOTH sides (need {floor} of the {stats.n_matched} matched "
            "keys); the rest are UNK/UNX/XAA, which match anything and are "
            "therefore evidence of nothing. A sequence-free chain scores a "
            "perfect identity against any reference at all, so it cannot "
            "certify that this is the protein we uploaded — most likely the "
            "design output is a bare backbone, or the binder was written with "
            "the input target's chain id. See chain_hints")
    fraction = stats.informative_fraction
    if fraction is not None and fraction < min_fraction:
        return (
            f"{subject} are {1 - fraction:.0%} unknown residues "
            f"({stats.n_informative} of {stats.n_matched} matched keys carry a "
            f"name on both sides, need {min_fraction:.0%}): predominantly "
            "sequence-free, so the identity that would certify them is mostly "
            "wildcard matches rather than evidence. See chain_hints")
    return None


# ---------------------------------------------------------------------------
# Per-design scoring
# ---------------------------------------------------------------------------


def score_from_contacts(hits: Iterable[Residue], requested: Sequence[Any],
                        positions: dict[Residue, Point]) -> dict:
    """``hotspot_recall`` + ``centroid_distance`` against an ALREADY-computed
    contact set.

    Split out because the contact set depends only on the geometry, never on
    which hotspots were requested — so phase 2, which scores every design twice
    (against its own patch and against the positive one), can parse the PDB and
    run the O(target x binder) contact search ONCE instead of twice. That search
    is the dominant cost of interpreting a shard, and it runs on billed GPU
    container time.

    ``None`` for a metric means UNMEASURABLE, never zero and never bad. Callers
    must branch on ``is None``; the verdict functions below do.
    """
    hits = set(hits)
    want, bad = parse_spec(requested)
    found = [k for k in want if k in positions]
    out: dict[str, Any] = {
        "contacts": len(hits),
        "hotspot_recall": None,
        "centroid_distance": None,
        "unparsable_hotspots": bad,
        "requested_found_in_structure": len(found),
        "contact_residues": sorted(f"{c}{r}" for c, r in hits)[:40],
    }
    if not hits:
        # Nothing landed on the target at all: not a score of zero, no score.
        return out
    if bad:
        # A token we could not read would silently shrink the denominator.
        return out
    if not want or not found:
        # No reference patch (the null shard), or a reference patch that is not
        # in this structure. Recall 0.0 here would be a false FAIL.
        return out
    out["hotspot_recall"] = len(want & hits) / len(want)
    c_hits = centroid([positions[k] for k in hits if k in positions])
    c_want = centroid([positions[k] for k in found])
    if c_hits is not None and c_want is not None:
        out["centroid_distance"] = dist(c_hits, c_want)
    return out


def score_design(pdb_text: str, target_chains: Iterable[str],
                 requested: Sequence[Any]) -> dict:
    """One-shot convenience wrapper around ``score_from_contacts``."""
    atoms = heavy_atoms(pdb_text)
    positions = ca_positions(atoms)
    hits = contacts_from_atoms(atoms, target_chains, positions=positions)
    return score_from_contacts(hits, requested, positions)


def score_design_file(pdb_text: str, target_chains: Iterable[str],
                      requested: Sequence[Any], cross_spec: Sequence[Any],
                      reference: dict[Residue, str]) -> dict:
    """THE per-design record a shard emits — every decision in it, pure.

    Lives here rather than inline in ``run_shard`` for the reason the whole
    module split exists: ``_hotspot_canary.py`` imports ``modal`` at top level,
    so anything written inside it is unreachable by pytest and untested. The
    chain-identity gate below is the single most expensive decision in the
    harness to get wrong — it is what stands between a relabelled binder and a
    fabricated ``hotspot_recall = 1.0`` — so it must be here, where the offline
    suite can execute it.

    ORDER MATTERS. Identity is verified BEFORE any geometry is scored, and a
    design that fails verification gets NO metric keys at all. An absent metric
    reads as UNMEASURABLE everywhere downstream (``scorable_designs`` skips it,
    ``median`` ignores it, the verdicts return INCONCLUSIVE), which is exactly
    the intended flow: refuse to score, never score wrongly.
    """
    atoms = heavy_atoms(pdb_text)
    present = set(chains_present(atoms))
    wanted = set(target_chains)
    entry: dict[str, Any] = {
        "chains": sorted(present),
        "is_complex": bool(present & wanted) and bool(present - wanted),
    }
    if not entry["is_complex"]:
        return entry

    check = verify_target_identity(atoms, wanted, reference)
    entry["target_verified"] = check["verified"]
    entry["target_identity"] = check
    if not check["verified"]:
        entry["unscorable_reason"] = check["reason"]
        return entry

    positions = ca_positions(atoms)
    hits = contacts_from_atoms(atoms, wanted, positions=positions)
    entry.update(score_from_contacts(hits, requested, positions))
    if cross_spec:
        cross = score_from_contacts(hits, cross_spec, positions)
        entry["cross_hotspot_recall"] = cross.get("hotspot_recall")
        entry["cross_centroid_distance"] = cross.get("centroid_distance")
        # HOW MUCH OF THE POSITIVE PATCH THIS DESIGN EVEN CONTAINS. Recorded
        # for the cross patch as well as for the design's own, because the
        # negative verdict is the one place the crop points toward a false
        # PASS: a design carrying 1 of the 4 positive hotspots scores a diluted
        # 0.25 and reads as a clean negative control. ``score_from_contacts``
        # has always computed this number; it was thrown away here, so no
        # verdict could consult it.
        entry["cross_requested_found_in_structure"] = cross.get(
            "requested_found_in_structure")
    return entry


def median(values: Iterable[Any]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


# ---------------------------------------------------------------------------
# Registry keys and hygiene
# ---------------------------------------------------------------------------


def canary_task_key(label: str, seed: int, prefix: str = CANARY_KEY_PREFIX) -> str:
    """A STABLE registry key for one canary shard.

    ``hash()`` of a str is salted per process (PYTHONHASHSEED), so the original
    ``abs(hash((label, seed)))`` produced a different key on every invocation:
    two runs of the same phase were not comparable, and a re-run could not be
    correlated with the record it had written. A digest is stable across
    processes, machines and interpreter versions.

    The result must satisfy the adapter's ``_TASK_RE``
    (``^[A-Za-z0-9_\\-]{1,64}$``) because it becomes ``++generation.task_name``;
    ``hub_canary`` + 12 hex characters is 22 characters of ``[a-z0-9_]``.
    """
    blob = f"{label}\x1f{int(seed)}".encode("utf-8")
    return f"{prefix}{hashlib.sha256(blob).hexdigest()[:12]}"


def registry_records(data: Any) -> dict:
    """The TARGET RECORDS of a parsed ``targets_dict.yaml``.

    Upstream nests every record one level down under a top-level
    ``target_dict_cfg:`` key and its own ``target_manager`` compensates with
    ``data.get("target_dict_cfg", data)``. The returned mapping is a REFERENCE
    into ``data``, so mutating it mutates the structure that gets dumped back.
    """
    if not isinstance(data, dict):
        return {}
    inner = data.get("target_dict_cfg")
    return inner if isinstance(inner, dict) else data


def prune_canary_records(data: Any, prefix: str = CANARY_KEY_PREFIX) -> list[str]:
    """Drop this harness's own records from a parsed registry; returns the keys.

    Phase 0 and every shard write into the image's REAL
    ``configs/targets/targets_dict.yaml``. Modal reuses warm containers, Hydra
    composes that whole file on every run, and the canary registers a fresh key
    per (label, seed) — so without a prune the file grows on every invocation
    and a stale absolute ``target_path`` stays readable to the next tenant.
    ``tools/proteina/modal_app.py::_clear_hub_targets`` does the same thing for
    prod; this is the canary's copy, narrowed to keys the canary itself wrote so
    it can never delete a curated benchmark target.
    """
    records = registry_records(data)
    stale = [k for k in list(records) if str(k).startswith(prefix)]
    for k in stale:
        records.pop(k, None)
    return stale


def canary_staged_pdbs(names: Iterable[Any],
                       prefix: str = CANARY_KEY_PREFIX) -> list[str]:
    """Out of a directory listing, the staged PDBs THIS harness wrote.

    The canary stages into the same directory production does
    (``run_pipeline._HUB_TARGET_DIR``) under the same naming rule (the file's
    stem IS the registry key), because a harness that stages somewhere
    production never touches is not exercising the path it exists to test. That
    makes the old cleanup — ``shutil.rmtree`` of a directory the canary owned
    outright — unsafe: the directory is now shared, and a blanket delete there
    could remove a target the canary did not write.

    So the file prune is prefix-based exactly like ``prune_canary_records``,
    and it is safe for exactly the same reason: ``canary_task_key`` emits
    ``hub_canary`` + hex while ``run_pipeline.custom_target_key`` emits
    ``hub_`` + HEX, and "canary" is not hex, so the two namespaces cannot
    collide. Curated benchmark targets are not in this directory at all and do
    not carry the prefix either way.
    """
    return sorted(
        n for n in names
        if isinstance(n, str) and n.startswith(prefix) and n.endswith(".pdb")
    )


# ---------------------------------------------------------------------------
# The Hydra composition assertion
# ---------------------------------------------------------------------------


def _iter_mappings(node: Any) -> Iterator[dict]:
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def _find_registry_record(cfg: Any, key: str) -> dict | None:
    for mapping in _iter_mappings(cfg):
        value = mapping.get(key)
        if isinstance(value, dict) and (
            "target_input" in value or "hotspot_residues" in value
        ):
            return value
    return None


def _task_name_values(cfg: Any) -> list[str]:
    seen: list[str] = []
    for mapping in _iter_mappings(cfg):
        if "task_name" in mapping:
            value = mapping.get("task_name")
            if isinstance(value, str) and value not in seen:
                seen.append(value)
    return seen


def hydra_assertion(cfg: Any, key: str, hotspot_spec: Sequence[Any],
                    contig: str | None = None) -> dict:
    """Did the resolved config SELECT our key and carry OUR hotspots?

    The original did ``if key in json.dumps(cfg)`` — a substring search of the
    whole blob — and that CANNOT distinguish selection from mere presence.
    ``binder_generate.yaml`` composes the entire ``targets_dict.yaml`` into
    ``target_dict_cfg``, so a key appears in the resolved config the instant it
    is REGISTERED, whether or not ``++generation.task_name`` then selected it.
    The substring test therefore passes for a run that designed against a
    completely different target — the exact false positive that makes phase 1
    worthless as evidence.

    So the load-bearing check here is ``task_name_selected``: the value of the
    ``task_name`` key that Hydra actually resolved. ``record_present`` and
    ``hotspots_match`` are the second half — the selected record must carry the
    hotspots we asked for.

    ``hotspots_match`` compares as a SET, not as an ordered list. The observed
    value is whatever OmegaConf round-tripped through a YAML write and a Hydra
    compose, and neither guarantees list order; upstream is free to normalise
    or sort ``hotspot_residues`` and would break nothing by doing so. Demanding
    ordered equality therefore made phase 1 FAIL a perfectly correct run, at
    $4, for a difference that changes no behaviour — hotspots are matched by
    upstream as a membership test against a set. Order is still reported, as
    ``hotspots_order_matches``, because a reordering is worth SEEING; it is
    simply not a failure.
    """
    want_hot = [str(h) for h in (hotspot_spec or [])]
    names = _task_name_values(cfg)
    record = _find_registry_record(cfg, key)
    got_hot = (
        [str(h) for h in (record.get("hotspot_residues") or [])]
        if isinstance(record, dict) else []
    )
    result = {
        "task_name_values": names,
        "task_name_selected": key in names,
        "record_present": isinstance(record, dict),
        "observed_hotspots": got_hot,
        "hotspots_match": isinstance(record, dict) and set(got_hot) == set(want_hot),
        "hotspots_order_matches": (
            isinstance(record, dict) and got_hot == want_hot),
        "hotspots_missing": sorted(set(want_hot) - set(got_hot)),
        "hotspots_unexpected": sorted(set(got_hot) - set(want_hot)),
        "contig_matches": None,
    }
    if contig is not None:
        result["observed_target_input"] = (
            record.get("target_input") if isinstance(record, dict) else None
        )
        result["contig_matches"] = str(result["observed_target_input"]) == str(contig)
    result["ok"] = bool(
        result["task_name_selected"]
        and result["record_present"]
        and result["hotspots_match"]
        and (result["contig_matches"] is not False)
    )
    return result


# ---------------------------------------------------------------------------
# Negative-control patch selection
# ---------------------------------------------------------------------------


def neighbour_counts(ca: dict[Residue, Point], radius: float = 10.0) -> dict[Residue, int]:
    """CA neighbours within ``radius`` — a burial proxy. Low count == exposed.

    There is no SASA here (the container has no BioPython and the canary must
    stay stdlib-only), and CA-neighbour density is the standard cheap stand-in.
    It only has to be good enough to keep the negative control on the SURFACE:
    a patch buried in the core is not designable, so a binder failing to reach
    it would prove nothing about hotspots.
    """
    keys = list(ca)
    counts = {k: 0 for k in keys}
    r2 = radius * radius
    for i, ki in enumerate(keys):
        xi, yi, zi = ca[ki]
        for kj in keys[i + 1:]:
            xj, yj, zj = ca[kj]
            dx, dy, dz = xi - xj, yi - yj, zi - zj
            if dx * dx + dy * dy + dz * dz <= r2:
                counts[ki] += 1
                counts[kj] += 1
    return counts


def pick_far_patch(
    pdb_text: str,
    positive: Sequence[Any],
    *,
    min_separation: float = NEGATIVE_MIN_SEPARATION_A,
    patch_size: int = 4,
    max_seed_neighbours: int = MAX_SURFACE_NEIGHBOURS,
    neighbour_radius: float = 10.0,
) -> tuple[list[str], dict]:
    """A contiguous, SURFACE-exposed patch at least ``min_separation`` from the
    positive one. Returns ``(tokens, diagnostics)``.

    Four things the original got wrong:

    1. it walked every heavy atom and kept the FIRST per residue via
       ``setdefault``, which is normally the backbone N, not the CA — so both
       the distance filter and the patch geometry were computed off the wrong
       atom, and waters/ions became selectable "residues" whose tokens
       ``missing_hotspots`` then rejects, aborting the negative shard;
    2. it chose the patch members from ALL residues by proximity to the far
       seed, so a member could sit well INSIDE ``min_separation`` of the
       positive patch — a "negative" control partly on the POSITIVE site, which
       measures nothing;
    3. it ignored burial entirely, so the far patch could be in the core, where
       no binder can dock for reasons that have nothing to do with hotspots —
       and a negative control that a binder cannot reach for structural reasons
       "passes" without testing the hypothesis;
    4. it measured separation from the positive patch's CENTROID. A 4-residue
       patch spans 10-15 A, so a residue a nominal 25 A from the centroid can
       be ~17 A from the nearest positive hotspot, and a 60-120 residue binder
       is well over 40 A across — it would brush the positive site routinely.
       Separation is now measured to the NEAREST POSITIVE HOTSPOT, which is the
       only reference point the 5 A contact cutoff is comparable with.

    A REFUSAL HERE IS FREE; the alternative is a $12 verdict on a control that
    was never valid. ``missing`` positive hotspots raise even when others
    resolve: ``ca_centroid_with_coverage`` happily returns a centroid from the
    3 tokens of 4 that exist, and the shard would then have gone to the GPU
    with a spec ``missing_hotspots`` rejects in-container.

    The seed is the most exposed residue beyond the floor and the patch is its
    nearest neighbours WITHIN that far set, so the result is contiguous and
    every member clears the floor by construction. If even the most exposed far
    residue is buried, this raises rather than handing back a negative control
    that cannot be designed against. "Buried" is judged both absolutely
    (``max_seed_neighbours``) and RELATIVE to this structure's own median
    residue — see ``MAX_SURFACE_NEIGHBOURS`` for why the absolute ceiling alone
    is biased toward accepting a buried site, and what that costs.

    Raises ValueError rather than SystemExit so the logic is testable without a
    process boundary.
    """
    if patch_size < 1:
        raise ValueError("patch_size must be >= 1")
    atoms = heavy_atoms(pdb_text)
    ca = ca_positions(atoms)
    if not ca:
        raise ValueError("the target PDB contains no polymer CA atoms")
    want, bad = parse_spec(positive)
    if bad:
        raise ValueError(f"unreadable positive hotspot token(s): {bad}")
    if not want:
        raise ValueError("no positive hotspots were given, so there is nothing "
                         "for a negative control to be far FROM")
    absent = sorted(f"{c}{r}" for c, r in want if (c, r) not in ca)
    if absent:
        # EVERY token, not just "all of them". The original only refused when
        # the centroid could not be built at all, so ['A1','A2','A3','A99999']
        # on a 60-residue target returned a patch, reported "3/4" and let three
        # A100 shards spawn — each of which then refused itself in-container,
        # after the money.
        raise ValueError(
            f"positive hotspot(s) not in the target PDB: {absent} "
            f"({len(want) - len(absent)}/{len(want)} resolved). Upstream matches "
            "hotspots literally, so these would be silently dropped and the "
            "search would run unconstrained")
    hotspot_xyz = [ca[k] for k in sorted(want)]
    c_pos, n_found, n_want = ca_centroid_with_coverage(atoms, want)

    def nearest_hotspot(p: Point) -> float:
        return min(dist(p, h) for h in hotspot_xyz)

    far = {k: v for k, v in ca.items()
           if k not in want and nearest_hotspot(v) >= min_separation}
    if len(far) < patch_size:
        raise ValueError(
            f"fewer than {patch_size} residues are >= {min_separation} A from the "
            "nearest positive hotspot — pick a larger target for the negative "
            "control"
        )

    counts = neighbour_counts(ca, neighbour_radius)
    all_counts = sorted(counts.values())
    structure_median = median(all_counts)
    # Most exposed first; ties go to the residue furthest from the positive
    # patch, then to the residue id so the choice is fully deterministic.
    seed = min(far, key=lambda k: (counts[k], -nearest_hotspot(far[k]), k))
    reliable = len(ca) >= BURIAL_PROXY_MIN_RESIDUES
    if counts[seed] > max_seed_neighbours:
        raise ValueError(
            f"the most exposed residue >= {min_separation} A from the positive "
            f"patch still has {counts[seed]} CA neighbours within "
            f"{neighbour_radius} A (surface ceiling {max_seed_neighbours}): every "
            "far site on this target is buried, and a binder failing to reach a "
            "buried site would prove nothing about hotspots. Choose a different "
            "target, or pass the negative patch explicitly."
        )
    if structure_median is not None and counts[seed] > structure_median:
        # Scale-free companion to the absolute ceiling above. The CA-neighbour
        # proxy undercounts on small / extended structures, so the absolute
        # ceiling is easiest to clear exactly where it is least meaningful —
        # and an accepted-but-buried negative patch produces a $12 FAIL on a
        # working feature. A site more buried than this structure's OWN median
        # residue is not a surface site whatever the absolute count says.
        raise ValueError(
            f"the most exposed residue >= {min_separation} A from the positive "
            f"patch has {counts[seed]} CA neighbours within {neighbour_radius} A, "
            f"more than this structure's median residue ({structure_median}): "
            "relative to its own fold, every far site here is buried. The "
            "absolute surface ceiling does not catch this on small or extended "
            "structures, where the neighbour count undercounts everywhere. "
            "Choose a different target, or pass the negative patch explicitly."
        )
    seed_xyz = far[seed]
    patch = sorted(far, key=lambda k: (dist(far[k], seed_xyz), k))[:patch_size]
    tokens = [f"{c}{r}" for c, r in patch]

    separations = [nearest_hotspot(ca[k]) for k in patch]
    centroid_separations = (
        [dist(ca[k], c_pos) for k in patch] if c_pos is not None else [])
    patch_counts = [counts[k] for k in patch]
    return tokens, {
        "positive_centroid_residues_found": f"{n_found}/{n_want}",
        "separation_measured_from": "nearest positive hotspot",
        "min_separation_requested_a": min_separation,
        "min_separation_achieved_a": round(min(separations), 2),
        "max_separation_achieved_a": round(max(separations), 2),
        "min_separation_from_positive_centroid_a": (
            round(min(centroid_separations), 2) if centroid_separations else None),
        "patch_span_a": round(
            max(dist(ca[a], ca[b]) for a in patch for b in patch), 2),
        "seed": f"{seed[0]}{seed[1]}",
        "seed_neighbours": counts[seed],
        "max_seed_neighbours": max_seed_neighbours,
        "neighbour_counts": {f"{c}{r}": counts[(c, r)] for c, r in patch},
        "patch_median_neighbours": median(patch_counts),
        "structure_median_neighbours": structure_median,
        "structure_n_residues": len(ca),
        "burial_proxy_reliable": reliable,
        "burial_proxy_caveat": (
            None if reliable else
            f"this structure has {len(ca)} polymer residues (< "
            f"{BURIAL_PROXY_MIN_RESIDUES}); a CA-neighbour count undercounts at "
            "that size, so the absolute surface ceiling carries little "
            "information here and only the relative check is doing work. An "
            "accepted-but-buried negative patch reads as a FAIL, not as an "
            "INCONCLUSIVE — check the patch by eye before spending"),
        "patch_more_buried_than_median": (
            median(patch_counts) > structure_median),
        "n_far_candidates": len(far),
    }


# ---------------------------------------------------------------------------
# Pre-spend refusals
#
# WHY THEY LIVE HERE AND NOT IN ``_hotspot_canary.py``. Every one of these
# guards money, and an independent mutation pass showed that every refusal
# written inline in the Modal module survived deletion: the suite could only
# assert that a CALL was present in the AST, so "keep the call, delete its
# effect" left 136/136 green while ``--hotspots A99999`` still spawned three
# A100s. The refusals that already lived in this module were all detected,
# because a pure function can be executed offline and its raising asserted. So
# the predicate AND the raise belong here; the Modal module only supplies the
# inputs and lets the exception out.
#
# They raise ``CanaryRefusal`` — a ``SystemExit`` subclass — so the process
# still exits the way every other refusal in the harness does, while the
# offline suite can assert the specific type rather than a stray SystemExit
# from somewhere else.
# ---------------------------------------------------------------------------


class CanaryRefusal(SystemExit):
    """A refusal issued BEFORE anything is spent. Exits non-zero, like any
    other refusal, and is a distinct type so a test can assert that THIS check
    fired rather than that some call appears in the source."""


def refuse_unknown_phase(phase: Any, known: Sequence[int] = KNOWN_PHASES) -> int:
    """Refuse a phase the harness does not implement. Returns the phase.

    ``main`` branched on ``phase == 0`` and ``phase == 1`` and then FELL THROUGH
    to phase 2 with no ``else``, so a typo — ``--phase 3``, ``--phase 5``,
    ``--phase -1`` — silently ran the three-shard ~$12 phase instead of the free
    one the operator asked for. A number the harness does not recognise is never
    an instruction to spend the most money available.
    """
    try:
        value = int(phase)
    except (TypeError, ValueError):
        raise CanaryRefusal(
            f"[canary] --phase {phase!r} is not a number; the phases are "
            f"{list(known)}. NO GPU TIME WAS USED.") from None
    if value not in tuple(known):
        raise CanaryRefusal(
            f"[canary] --phase {value} does not exist; the phases are "
            f"{list(known)} (0 free, 1 ~$4, 2 ~$12). Refusing rather than "
            "falling through to the most expensive one. NO GPU TIME WAS USED.")
    return value


def refuse_empty_hotspot_spec(positive: Sequence[Any], phase: Any) -> list[str]:
    """Refuse a run whose POSITIVE hotspot spec is empty. Returns the tokens.

    ``--hotspots ""`` is not a cheap no-op, it is the most expensive way to
    assert nothing:

    * phase 1 registers no hotspots, so ``hydra_assertion`` compares ``set()``
      against ``set()``, ``hotspots_match`` is vacuously True and the verdict is
      PASS — "the resolved config ... carries our hotspots" — for $4 that tested
      the one thing phase 1 exists to test not at all;
    * phase 2 with ``--negative <spec>`` skips ``pick_far_patch`` (the only
      other code that would have rejected an empty positive spec), then
      ``missing_hotspots(selected, [])`` is vacuously ``[]``, three shards
      spawn, every metric comes back ``None`` and all three verdicts are
      INCONCLUSIVE. ~$12 for a guaranteed non-answer.

    The empty spec IS deliberate for the null shard, which is constructed
    internally; it is never a legitimate value for ``--hotspots``.
    """
    tokens = [str(t) for t in (positive or []) if str(t).strip()]
    if not tokens:
        raise CanaryRefusal(
            f"[canary] phase {phase} needs a non-empty --hotspots spec: the "
            "whole question is whether the hotspots steered the interface, and "
            "with none there is nothing to steer to. An empty spec does not "
            "fail loudly, it passes vacuously — phase 1 would compare no "
            "hotspots against no hotspots and report PASS, and phase 2 would "
            "spend ~$12 to return INCONCLUSIVE three times. (The null shard's "
            "empty spec is built internally and is not this.) NO GPU TIME WAS "
            "USED.")
    return tokens


def refuse_structureless_target(target_pdb: Any, n_residues: int,
                                n_unparsable: int = 0) -> None:
    """Refuse a target PDB from which no CA residue could be read."""
    if n_residues:
        return
    raise CanaryRefusal(
        f"[canary] {target_pdb} contains no CA residues (NO GPU TIME WAS "
        f"USED); {n_unparsable} CA line(s) had unparsable residue numbers")


def refuse_unresolvable_hotspots(target_pdb: Any, contig: Any, n_selected: int,
                                 missing_by_label: Iterable[tuple[str, Sequence[str]]]
                                 ) -> None:
    """Refuse hotspot tokens that match no residue of the selected contig.

    Upstream matches hotspots with a literal membership test against a
    zero-initialised mask, so a token matching nothing is dropped SILENTLY and
    the search then runs unconstrained — the run's exit code, design count and
    reward CSV are all indistinguishable from one that honoured it. "These
    tokens resolve to nothing" is therefore never a warning; it is a refusal,
    and it has to be issued where it is free rather than in-container, three
    A100 startups later.
    """
    bad = [f"{label}: {list(missing)}"
           for label, missing in missing_by_label if missing]
    if not bad:
        return
    raise CanaryRefusal(
        f"[canary] hotspot token(s) match no residue in {target_pdb} "
        f"(contig {contig}, {n_selected} residues selected): "
        f"{'; '.join(bad)}. Upstream drops an unmatched hotspot silently "
        "and then designs unconstrained, so this run would have measured "
        "nothing. NO GPU TIME WAS USED.")


def refuse_unparsable_contig(target_pdb: Any, contig: Any, detail: Any) -> None:
    """Turn ``parse_target_input``'s ``ValueError`` into a refusal. Always raises.

    NO MONEY IS AT STAKE HERE AND IT IS STILL A DEFECT. ``--contig Zz9`` came
    out of ``_refuse_unresolvable_hotspots`` as a bare
    ``ValueError: unparsable target_input segment 'Zz9'`` with a traceback,
    which is the one refusal in the harness that did not tell the operator the
    thing every other refusal tells them: that nothing was spent. Production
    converts the identical failure — same function, same exception — into a
    ``_fail``, so this is the same mirroring rule as the guards around it,
    applied to the cheapest case rather than the most expensive one.

    The parse is production's; only the wrapping is here. It always raises
    because its one call site is an ``except`` branch: there is no "no error"
    input to return on.
    """
    raise CanaryRefusal(
        f"[canary] --contig {contig} cannot be parsed against {target_pdb}: "
        f"{detail}. A contig segment is a chain letter and a range, e.g. "
        "A1-150, or a bare chain id, e.g. A; several are comma-separated. "
        "run_pipeline refuses the same text with the same parser, so this "
        "would never have reached a GPU — but it escaped as a traceback "
        "instead of a refusal. NO GPU TIME WAS USED.")


def refuse_unrenderable_contig(target_pdb: Any, contig: Any,
                               bad: Sequence[Any]) -> None:
    """Refuse a contig upstream's own parser cannot read back.

    THE SECOND GUARD PRODUCTION HAD AND THE CANARY DID NOT, found while
    auditing the first. ``run_pipeline`` refuses a negative author residue
    number pre-GPU (``unrenderable_segments``): atomworks'
    ``CONTIG_REGEX = r"([A-Za-z]+)(\\d+)-(\\d+)"`` carries no sign, so a
    construct that keeps its expression tag derives the contig ``A-5-240`` and
    raises inside ``complexa design``. Nothing before that catches it — the
    selection is non-empty, the registration succeeds and reads back — so the
    shard boots, loads checkpoints and only then dies.

    The canary had no equivalent, which means ``--target-pdb <tagged
    construct>`` would spawn one A100 in phase 1 (~$4) or three in phase 2
    (~$12) to discover what a regex knows for free. Same class as the staging
    drift: production grew a pre-GPU refusal and the harness did not follow.

    The predicate stays in ``run_pipeline`` (``unrenderable_segments``) and is
    CALLED, never restated; this only turns its answer into the refusal.
    """
    if not bad:
        return
    shown = ",".join(
        f"{seg[0]}{seg[1]}-{seg[2]}" if len(seg) >= 3 else str(seg)
        for seg in bad)
    raise CanaryRefusal(
        f"[canary] the contig {contig} for {target_pdb} uses negative residue "
        f"numbers ({shown}), which upstream's CONTIG_REGEX cannot express — it "
        "accepts digits only. Structures carrying an expression tag are usually "
        "numbered this way. Pass --contig with a range starting at 0 or above. "
        "Every other pre-GPU check passes on such a target, so without this the "
        "shard would boot, load checkpoints and die. NO GPU TIME WAS USED.")


def refuse_empty_segments(target_pdb: Any, contig: Any, dead: Sequence[Any],
                          spans: Any) -> None:
    """Refuse a contig segment that selects no residue of the upload.

    PER SEGMENT, WHICH IS THE ENTIRE DEFECT. ``prepare_custom_target`` refuses
    each segment that picks nothing; the canary checked only that the AGGREGATE
    selection was non-empty, so one dead segment hid behind a healthy one.
    Measured: ``--contig A1-300,Z1-50`` against a file of chains A and B selects
    300 residues, clears the size floor, resolves its hotspots in chain A, and
    spawns — one A100 in phase 1 (~$4), three in phase 2 (~$12) — for a request
    production settles for free. PR #109 made multi-segment contigs the ordinary
    input shape, which is what turned this from latent into reachable.

    THE MESSAGE NAMES THE SEGMENT AND THE FILE'S ACTUAL CONTENTS, and that also
    repairs a misdirection the size refusal was giving on its own. ``--contig
    Z1-50`` alone used to come back as "selects 0 residue(s) ... fewer than the
    20 production requires ... Widen --contig", which sends the operator to
    widen a range on a chain the upload does not contain. Widening cannot help;
    naming chain Z and listing the chains that ARE there can.

    ``dead`` IS PRODUCTION'S ANSWER (``run_pipeline.empty_segments``), not one
    computed here. A segment may arrive with ``None`` bounds — an unresolvable
    bare chain id, which ``expand_bare_chains`` deliberately leaves alone — and
    is rendered as the bare chain rather than as ``Z None-None``.
    """
    if not dead:
        return
    shown = ", ".join(
        f"{seg[0]}{seg[1]}-{seg[2]}" if len(seg) >= 3 and seg[1] is not None
        else f"chain {seg[0]}" if len(seg) >= 1 else str(seg)
        for seg in dead)
    raise CanaryRefusal(
        f"[canary] the contig {contig} names {shown}, which selects no residue "
        f"of {target_pdb}. The file contains: {spans}. prepare_custom_target "
        "refuses a segment that picks nothing, one segment at a time; the "
        "canary checked only that the whole selection was non-empty, so a dead "
        "segment beside a healthy one would have spent ~$4 in phase 1 or ~$12 "
        "in phase 2 to fail in the container. Fix the chain id or the range — "
        "if the chain is not in the list above, widening will not help. "
        "NO GPU TIME WAS USED.")


def refuse_target_too_small(target_pdb: Any, contig: Any, too_small: bool,
                            n_selected: int, minimum: Any) -> None:
    """Refuse a contig that selects too little target to design against.

    THE THIRD GUARD PRODUCTION HAD AND THE CANARY DID NOT, and the class is now
    established rather than suspected: ``prepare_custom_target`` refuses a
    selection below ``run_pipeline.MIN_SELECTED_RESIDUES`` before any GPU is
    touched, and the harness had nothing equivalent — only the non-EMPTY checks
    above, which a ten-residue contig passes. ``--contig A10-20`` would spawn
    one A100 in phase 1 (~$4) or three in phase 2 (~$12) to discover what a
    length knows for free.

    WHAT UPSTREAM DOES WITH A SLIVER IS UNVERIFIED, IN BOTH DIRECTIONS, and
    that is the reason to refuse rather than an argument against it. Nothing in
    this repo evidences whether ``complexa design`` refuses a sub-20-residue
    selection or designs happily against it, and no GPU run has ever tested it.
    Both branches are bad and only one of them is loud: if it refuses, the
    money is spent and the verdict is at least honest; if it designs, the
    metrics come back, the harness can report PASS, and the number measured is
    hotspot recall over a target production would have refused to accept at
    all. The pre-GPU answer costs nothing either way, which is why the refusal
    sits here and not in the container. Note the contrast with the two guards
    above, whose failure modes ARE evidenced — atomworks' ``CONTIG_REGEX`` was
    read, and the uncropped-target crash was reproduced on a paid A100.

    ``too_small`` IS PRODUCTION'S ANSWER, NOT ONE COMPUTED HERE, which is why it
    is a parameter rather than ``n_selected < minimum``. The comparison and the
    threshold both live in ``run_pipeline.target_too_small``; this turns its
    verdict into the refusal, and ``minimum`` is carried only so the message can
    quote the number the operator has to clear. Recomputing either here would
    reintroduce exactly the drift this round exists to remove. ``n_selected`` is
    likewise production's count — ``n_selected_residues``, the DISTINCT one —
    and not ``len(select_residues(...))``, which double-counts a residue two
    segments both name and let ``A10-20,A10-20`` clear a floor of 20.
    """
    if not too_small:
        return
    raise CanaryRefusal(
        f"[canary] the contig {contig} selects {n_selected} residue(s) of "
        f"{target_pdb}, fewer than the {minimum} production requires before it "
        "will accept a target at all. prepare_custom_target refuses this "
        "upload for free; the canary would have spent ~$4 in phase 1 or ~$12 "
        "in phase 2 to design against a sliver of surface, and could have come "
        "back GREEN having measured a run production would never have run. "
        "Widen --contig. NO GPU TIME WAS USED.")


def shard_spec_refusal(label: str, missing: Sequence[str],
                       missing_cross: Sequence[str]) -> dict | None:
    """The in-container twin of the hotspot refusal above, as a shard result.

    Deliberately count-free: it used to say "the two refusals above" and there
    are now five ``refuse_*`` functions between it and the top of this section.
    It mirrors ``refuse_unresolvable_hotspots`` and nothing else.

    ``run_shard`` cannot raise: its contract is to RETURN a dict, and the
    entrypoint attributes a returned ``{"error": ...}`` to its label instead of
    losing the other two shards. The decision is here anyway so the offline
    suite executes it — the local pre-spawn check and this one must agree, and
    the second is the only one that sees the contig the container actually
    resolved.

    THE OTHER FOUR HAVE NO TWIN HERE, AND THE SIZE ONE IS AN OPEN GAP. The
    reason cannot be "an in-container check would save nothing": by this line
    the container is running, but a shard that returns an error immediately
    stops billing, while one that designs runs to completion (up to
    ``_MAX_SESSION_S``, ~$12.58 on the cap). The paragraph above is the real
    argument — this is the only place that sees the contig the container
    resolved, and for the size floor that is exactly the case a pre-spawn check
    cannot cover: a contig derived in-container from a target the operator did
    not crop. It is not written yet. What holds today is that every path into a
    shard passes through ``_refuse_unresolvable_hotspots`` first, so the
    remaining exposure is drift between the two resolutions, not a missing
    check on the resolutions we have.

    A cross-reference patch that is not in the structure is as fatal as an own
    one: scoring every shard against a reference that resolves to nothing gives
    recall 0.0 everywhere and a null verdict of PASS on nothing.
    """
    if missing:
        return {"label": label,
                "error": f"hotspots not in structure: {list(missing)}"}
    if missing_cross:
        return {"label": label,
                "error": ("cross-reference hotspots not in structure: "
                          f"{list(missing_cross)}")}
    return None


def target_chains_from_selection(selected: Iterable[Sequence[Any]]) -> list[str]:
    """The chains a shard may treat as TARGET — from the CONTIG's selection.

    These used to be read from every chain of the input PDB while the identity
    ``reference`` was built from the contig's selection, so the two disagreed
    the moment ``--contig`` named a subset of the input's chains. The
    consequences were both bad and silent: a design chain carrying an EXCLUDED
    input chain id landed in the observed set with no reference entry, dropping
    key coverage below the floor and making every design unscorable (~$12 for
    an INCONCLUSIVE); and for a 2-chain input with a contig on chain A only, a
    design of exactly chains A+B had ``present - wanted == set()`` and was not
    even counted as a complex. Deriving both from ``selected`` makes the
    disagreement unrepresentable.
    """
    return sorted({str(r[0]) for r in selected})


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    """Every number the phase-2 verdicts turn on, in one place.

    ``min_hit_fraction`` replaces the original hardcoded ``len(good) >= 6``. The
    shard emits ``nsamples * replicas`` designs (4 * 2 = 8 today), so ``6`` was
    a fraction dressed up as a count and would silently become "all of them" if
    the shard size ever changed. It is now expressed against the number of
    designs that could ACTUALLY be scored.

    ``min_scorable_fraction`` is the OTHER half of that denominator, and it is
    the one whose absence made the quorum unconditional. ``min_hit_fraction``
    used to be normalised onto the SURVIVORS — the designs that passed target
    verification — so a shard that emitted 8 designs, had 7 refused as
    unverifiable and landed the 1 remaining on the patch computed
    ``required = max(1, ceil(0.75 * 1)) = 1`` against ``on_patch = 1`` and
    returned PASS on 1-of-8. That is the exact failure this harness exists to
    detect (a number computed off a denominator nobody chose) relocated into
    the harness, and its verdict flips a production flag.
    ``ceil`` makes the two fractions coincide exactly when they are equal:
    ``ceil(f * N) <= scorable`` iff ``scorable / N >= f``, so a shard that falls
    under this floor is precisely one where the quorum has become UNREACHABLE
    for want of measurable designs. That is an unmeasured run — INCONCLUSIVE —
    and emphatically not a FAIL: "we could not look at most of the designs" is
    not evidence the feature is broken, and condemning it costs $12 and a
    working feature.

    ``max_cross_hotspots`` is a COUNT, not a fraction, and that is deliberate.
    The old ``max_cross_recall = 0.2`` was compared against a median recall, and
    a median recall is not a continuum: with 4 hotspots and 8 designs the only
    reachable medians are 0, 0.125, 0.25, 0.375 ... so 0.2 sat BETWEEN lattice
    points and its real meaning was "FAIL once 5 of 8 designs touch any one
    positive hotspot" — a boundary nobody chose and nobody could read off the
    number. Expressed as a count the question is the one an operator can
    actually answer: how many of the positive hotspots may the median negative
    design touch? One. A binder pointed at a patch >= 25 A away should touch
    zero, but a 60-120 residue binder is well over 40 A across, so brushing a
    single edge residue of the positive patch is expected noise rather than
    evidence the interface did not move — and treating it as evidence costs $12
    and CONDEMNS a working feature. Two or more is a real overlap.
    """

    min_recall_per_design: float = 0.5
    min_hit_fraction: float = 0.75
    min_scorable_fraction: float = 0.75
    max_centroid_a: float = 10.0
    max_cross_hotspots: int = 1
    min_null_margin: float = 0.25

    def __post_init__(self) -> None:
        """The two fractions are not independent, and the class let them look it.

        The whole "an unreachable quorum is INCONCLUSIVE, never FAIL" design
        rests on ``ceil(min_hit_fraction * N) <= ceil(min_scorable_fraction *
        N)`` — i.e. on ``min_hit_fraction <= min_scorable_fraction``. Both
        default to 0.75 so it holds today, but this is a PUBLIC frozen
        dataclass with two independently-settable fields, and setting
        ``min_hit_fraction=1.0`` alone produced:

            N=8 scorable=6 on_patch=6 -> FAIL (required 8, scorable 6)

        Every design that could be measured landed on the patch and the verdict
        CONDEMNED the feature over designs nobody could look at. That is a $12
        wrong FAIL out of a knob, and it is the one combination in here that can
        produce a wrong answer silently — ``Verdict.__bool__`` and
        ``Verdict.__eq__`` both raise rather than allow that, so this does too.
        """
        if self.min_hit_fraction > self.min_scorable_fraction:
            raise ValueError(
                f"min_hit_fraction={self.min_hit_fraction} exceeds "
                f"min_scorable_fraction={self.min_scorable_fraction}: the PASS "
                "quorum would be unreachable from the smallest scorable set the "
                "floor admits, so a shard whose every measurable design landed "
                "on the patch would report FAIL. Raise min_scorable_fraction to "
                "at least min_hit_fraction."
            )


DEFAULT_THRESHOLDS = Thresholds()


@dataclass(frozen=True, eq=False)
class Verdict:
    name: str
    outcome: str
    reason: str
    metrics: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {self.outcome!r}")

    def __bool__(self) -> bool:
        # Deliberate. Every defect this module was rebuilt to fix came from
        # treating a result as a truthy scalar (`x or 999`, `x or 0`,
        # `all(verdicts.values())`). Branch on `.outcome` explicitly.
        raise TypeError(
            f"Verdict({self.name}) has three outcomes and is not a boolean — "
            "compare .outcome against PASS / FAIL / INCONCLUSIVE"
        )

    def __eq__(self, other: Any) -> Any:
        # ``eq=False`` above, then this, because the generated ``__eq__`` left a
        # hole exactly the size of the bug ``__bool__`` closes: ``verdict ==
        # cs.PASS`` returned False SILENTLY rather than raising, so the natural
        # typo for "did this pass" reads as "no" on a verdict that passed. A
        # three-valued result must not be comparable with one of its outcomes.
        if isinstance(other, str):
            raise TypeError(
                f"Verdict({self.name}) is not its outcome string — write "
                "`verdict.outcome == PASS`, not `verdict == PASS`"
            )
        if not isinstance(other, Verdict):
            return NotImplemented
        return (self.name, self.outcome, self.reason, self.metrics) == (
            other.name, other.outcome, other.reason, other.metrics)

    def __hash__(self) -> int:
        # Defining ``__eq__`` sets ``__hash__ = None``, which made every Verdict
        # unhashable: ``{v}``, ``dict[v]`` and ``hash(v)`` all raised TypeError
        # on a frozen value type, and the first caller to group verdicts by
        # identity would have hit it. ``metrics`` is deliberately NOT hashed —
        # it holds nested dicts and lists — and that stays consistent, because
        # equal verdicts necessarily agree on the three fields that are.
        return hash((self.name, self.outcome, self.reason))

    @property
    def passed(self) -> bool:
        return self.outcome == PASS

    def as_dict(self) -> dict:
        return {"name": self.name, "outcome": self.outcome,
                "reason": self.reason, "metrics": dict(self.metrics)}


# ---------------------------------------------------------------------------
# Delivery: would PRODUCTION have shipped this shard?
#
# A SEPARATE AXIS FROM THE OUTCOME, and keeping them separate is the fix. The
# outcome answers "may we turn the flag on", which is a question about geometry:
# did the binders land on the patch. Delivery answers "would run_pipeline have
# handed these designs to a paying customer", which is a question about the exit
# code and the reward table. They are independent — a shard can measure
# perfectly and still exit non-zero, and a clean shard can still miss the patch
# — and this harness used to collapse them into one, always in the direction
# that condemns.
#
# THE COST OF NOT SEPARATING THEM, measured. A run that produced 8 designs, 8
# files, 8 reward rows and 8 complexes, then crashed in `evaluate`, was reported
# FAILED. Production's rule (run_pipeline.py, immediately after `complexa
# design` returns) is:
#
#     n_scored = sum(1 for d in designs if d.get("total_reward") is not None)
#     if rc != 0:
#         if n_scored == 0:
#             _fail("search", "complexa", ...)
#         logger.warning("... but %d/%d designs are fully scored — delivering")
#
# and the reward CSV it reads is written by the GENERATE stage (generate.py:524
# writes rewards_{config}_{job}.csv with total_reward and the af2folding_*
# components), not by evaluate. So that run WOULD HAVE SHIPPED 8 SCORED DESIGNS
# while the canary printed FAILED. A measurement campaign was nearly cancelled
# on that reading.
#
# THREE STATES, NAMED, because two cannot hold the middle one:
#
#   CLEAN     exit 0. Nothing to report.
#   DEGRADED  non-zero exit, and designs came back fully scored. Production
#             delivers. NOT a failure of the feature — and NOT nothing either:
#             the non-zero exit is a real defect with its own diagnosis, so it
#             is stamped onto every verdict it touches and printed in full.
#   FAILED    errored, no exit code, a non-numeric one, a non-zero exit with
#             nothing scored, or a non-zero exit where the shard did not say how
#             many were scored. Production would have failed it too.
#
# "did not say" is FAILED on purpose. A payload with no scored-design count is
# one we cannot prove delivered anything, and guessing the other way is how a
# broken run gets blessed. It also means every hand-built payload in the offline
# suite keeps its old verdict unless it opts in by reporting the count.
# ---------------------------------------------------------------------------

CLEAN = "clean"
DEGRADED = "degraded"
FAILED = "failed"
DELIVERIES = (CLEAN, DEGRADED, FAILED)

# What ``run_shard`` calls the count, computed there by production's OWN
# ``run_pipeline.parse_designs`` so the canary cannot drift from the rule it is
# supposed to be mirroring.
SCORED_KEY = "n_scored_designs"


def scored_design_count(shard: Any) -> int | None:
    """Designs the shard reported as FULLY SCORED, or None if it did not say.

    None is not zero. Zero is "we looked and the reward table scored nothing";
    None is "this payload carries no such number", and the two lead to the same
    verdict for opposite reasons — see the module note above.
    """
    if not isinstance(shard, dict):
        return None
    raw = shard.get(SCORED_KEY)
    if isinstance(raw, bool):
        return None
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def shard_delivery(shard: Any) -> tuple[str, str]:
    """``(state, detail)`` — would production have delivered this shard?

    ``detail`` is "" for CLEAN and a full sentence otherwise. See the module
    note above for why this is not the same question as the verdict.
    """
    if not isinstance(shard, dict):
        return FAILED, "no result was returned"
    error = shard.get("error")
    if error:
        return FAILED, str(error)
    rc = shard.get("exit_code")
    if rc is None:
        return FAILED, "the shard reported no exit code"
    try:
        rc_int = int(rc)
    except (TypeError, ValueError):
        return FAILED, f"the shard reported a non-numeric exit code {rc!r}"
    if rc_int == 0:
        return CLEAN, ""
    n_scored = scored_design_count(shard)
    if n_scored is None:
        return FAILED, (
            f"the design command exited {rc_int} and the shard did not report "
            "how many designs came back fully scored, so there is no evidence "
            "production would have delivered anything"
        )
    if n_scored == 0:
        return FAILED, (
            f"the design command exited {rc_int} with no scored designs — "
            "production fails a run on exactly this reading"
        )
    return DEGRADED, (
        f"the design command exited {rc_int}, but {n_scored} design(s) came "
        "back fully scored, which is the reading on which production DELIVERS "
        "(run_pipeline only fails when a non-zero exit left nothing scored). "
        "This is not a verdict on the feature. The non-zero exit is still a "
        "real defect and needs its own diagnosis — read the stage-log tails."
    )


def shard_failure(shard: Any) -> str | None:
    """Why this shard cannot be interpreted at all, or None.

    A shard that errored or exited non-zero USED TO BE a FAIL unconditionally.
    That was stricter than production and cost a real judgement: see the module
    note above. It is now a FAIL exactly when production would also have failed
    it, which is what ``shard_delivery`` decides. A shard that exited non-zero
    and still delivered scored designs returns None here and is reported
    separately, loudly, by ``shard_degradation``.

    Only a shard that DELIVERED but produced nothing measurable is inconclusive;
    that is still decided downstream, not here.
    """
    state, detail = shard_delivery(shard)
    return detail if state == FAILED else None


def shard_degradation(shard: Any) -> str | None:
    """The DEGRADED sentence for this shard, or None.

    The counterpart to ``shard_failure``: the two are mutually exclusive and
    together cover every non-CLEAN shard, so nothing that used to be reported
    can fall through the gap between them.
    """
    state, detail = shard_delivery(shard)
    return detail if state == DEGRADED else None


def annotate_delivery(verdict: Verdict, *shards: Any) -> Verdict:
    """Stamp a verdict with the delivery state of the shards behind it.

    Always sets ``metrics["delivery"]`` (the worst of the shards', so a pair is
    described by its weakest member) and, for anything but CLEAN, the per-shard
    sentences. A DEGRADED verdict additionally gets a ``[DELIVERED-DEGRADED]``
    prefix on its reason, because the reason is the one line that always reaches
    the console and "PASS" on its own would hide a crashed shard.

    FAILED is not prefixed: its reason already opens with "the shard did not
    complete: ...", which is the same information said once.
    """
    states: list[str] = []
    details: list[str] = []
    for shard in shards:
        state, detail = shard_delivery(shard)
        states.append(state)
        if state == CLEAN:
            continue
        label = (shard.get("label") if isinstance(shard, dict) else None) or "shard"
        details.append(f"{label}: {detail}")
    worst = FAILED if FAILED in states else (DEGRADED if DEGRADED in states else CLEAN)
    metrics = dict(verdict.metrics)
    metrics["delivery"] = worst
    if details:
        metrics["delivery_detail"] = details
    reason = verdict.reason
    if worst == DEGRADED:
        reason = f"[DELIVERED-DEGRADED] {reason} ({' | '.join(details)})"
    return Verdict(verdict.name, verdict.outcome, reason, metrics)


def design_identity(design: Any, index: int = 0) -> str:
    """What makes two design records THE SAME DESIGN.

    ``run_shard`` writes ``name`` — the file's basename — for every record it
    emits, and upstream names one file per sample, so the basename IS the
    sample. A record with no usable name is given an identity of its own rather
    than being merged with its neighbours: deduplication is a refusal to count
    one name twice, never an assertion about anonymous records, and collapsing
    every nameless dict into one would turn a hand-built payload into "1
    design" and refuse a run for the wrong reason.
    """
    if not isinstance(design, dict):
        return f"\x00index:{index}"
    for key in ("name", "container_path"):
        value = design.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"\x00index:{index}"


def unique_designs(shard: Any) -> list[dict]:
    """The shard's design records, ONE PER DESIGN NAME, first record winning.

    THE THIRD FALSE PASS IN THIS SPOT, and it is the same shape as the first
    two. The quorum was moved off ``len(scorable)`` onto ``designs_produced``
    and then onto a floor taken against ``designs_expected``, but every one of
    those counted the FILES ``run_shard`` globbed, and nothing said a file is a
    design. QC measured the consequence: one design written to six paths
    returned ``6/6 designs (8 requested, 6 scorable)`` and exit 0, a green light
    for FLAG_TOOL_PROTEINA off ONE design. Uniform duplication cancels out of
    every fraction — it is invisible in ``on_patch / produced`` — and shows up
    only in the one absolute comparison, ``produced >= hit_quorum(expected)``,
    which is precisely the gate that exists to be un-shrinkable.

    Production already refuses this assumption: ``run_pipeline`` uses the
    byte-identical glob and guards its index pairing on
    ``len(all_pdbs) == total_rows`` rather than trusting the file count. The
    canary assumed what production declines to assume.

    Dropping the LATER record of a repeated name is the conservative direction
    in the one ambiguous case. Two genuinely different designs that happen to
    share a basename (two run directories under ``inference/``) collapse to one,
    which lowers the produced count, which makes the absolute floor HARDER to
    clear — an INCONCLUSIVE, never a PASS. ``design_count_disagreement`` then
    names the collision out loud rather than leaving it to be inferred.
    """
    if not isinstance(shard, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for index, design in enumerate(shard.get("designs") or []):
        if not isinstance(design, dict):
            continue
        key = design_identity(design, index)
        if key in seen:
            continue
        seen.add(key)
        out.append(design)
    return out


def design_files(shard: Any) -> int:
    """How many per-design FILES the shard globbed, duplicates included.

    Reported next to ``designs_produced`` and never in place of it. The pair is
    what makes duplication visible to the operator: QC's scenario printed
    ``6/6 designs (8 requested, 6 scorable)`` while one name accounted for all
    six, and no number on the console said so.
    """
    if not isinstance(shard, dict):
        return 0
    return len([d for d in (shard.get("designs") or []) if isinstance(d, dict)])


def duplicate_design_names(shard: Any) -> list[str]:
    """Design names that more than one file claimed, most repeated first."""
    if not isinstance(shard, dict):
        return []
    counts: dict[str, int] = {}
    for index, design in enumerate(shard.get("designs") or []):
        if not isinstance(design, dict):
            continue
        key = design_identity(design, index)
        counts[key] = counts.get(key, 0) + 1
    repeated = [(n, k) for k, n in counts.items() if n > 1]
    return [f"{k} x{n}" for n, k in sorted(repeated, key=lambda p: (-p[0], p[1]))]


def scorable_designs(shard: Any, field_name: str = "hotspot_recall") -> list[dict]:
    """Designs that are complexes, VERIFIED as carrying the input target, and
    carrying a measured value for ``field_name``.

    Taken over ``unique_designs``, not over the raw list, so the numerator and
    the denominator of every fraction below are drawn from the same set. A
    scorable count taken over the files while the produced count was taken over
    the names would let ``len(scorable)`` EXCEED ``produced`` — six copies of
    one design against a produced count of one — and a numerator larger than
    its denominator walks straight through ``thin_scorable_reason`` and the
    quorum alike.

    ``target_verified`` must be explicitly ``True``. It used to be enough for it
    not to be ``False``, which meant a design with the key simply ABSENT was
    scorable — and eight such designs made the positive, negative and null
    verdicts all PASS. The docstring below claims this is a second independent
    gate against "a future caller, a merge, a hand-built dict"; against that
    threat model an omitted key is the likeliest shape of all, and
    ``is not False`` did not hold against it. ``run_shard`` sets the key on
    every complex it emits, so nothing production produces changes meaning.

    Two gates for one condition is not belt-and-braces for its own sake: a
    design whose "target" chain is really the binder scores ``hotspot_recall =
    1.0`` off binder self-contacts, and that number reaching a verdict is a $12
    PASS on nothing. Anything that reintroduces the metric — a future caller, a
    merge, a hand-built dict — must still not be able to reach a verdict with
    it.
    """
    return [
        d for d in unique_designs(shard)
        if d.get("is_complex")
        and d.get("target_verified") is True
        and d.get(field_name) is not None
    ]


def unverified_designs(shard: Any) -> list[dict]:
    """Complexes whose putative-target chain turned out not to be the target."""
    return [d for d in unique_designs(shard)
            if d.get("is_complex") and d.get("target_verified") is False]


def designs_produced(shard: Any) -> int:
    """How many DISTINCT per-design outputs the shard actually emitted.

    ``scorable_designs`` answers "what survived scoring", which is a different
    and much smaller question, and a fraction normalised onto the survivors says
    nothing about the run: 1 design out of 8 landing on the patch is 100% of the
    scorable set and 12.5% of the shard. The verdicts below take their quorum
    from THIS number.

    DISTINCT, because ``len(shard["designs"])`` counted FILES. ``run_shard``
    globs ``inference/**/*.pdb`` with no de-duplication, so one design written
    to six paths counted six — see ``unique_designs`` for the measurement and
    what it cost.

    IT IS ITSELF A SURVIVOR COUNT, one level up, which is why
    ``designs_expected`` exists. ``run_shard`` builds this list by globbing
    ``inference/**/*.pdb`` while SKIPPING ``filtered_out_samples`` — upstream's
    own filter bucket — and dropping any file it cannot read. So this is
    "post-filter files that were still there", not "the designs we ordered", and
    on its own it re-creates the defect it was meant to close: a shard asked for
    8, upstream filtered 7, and ``1/1 designs on the patch (needed 1)`` is a
    PASS on one design with nothing in the report saying eight were ordered.
    """
    return len(unique_designs(shard))


def designs_expected(shard: Any) -> int | None:
    """How many per-design outputs the shard was ASKED for, or None.

    THE ONLY DENOMINATOR IN PHASE 2 THAT NOBODY DOWNSTREAM CAN SHRINK.
    ``run_shard`` invokes ``build_design_cmd(nsamples=..., replicas=...)`` and
    upstream yields ``nsamples * replicas`` samples for it; the shard records
    that product so a verdict can ask "how many of the designs we PAID for
    survived to be counted" instead of dividing the survivors by themselves.

    None when the shard does not report it — a hand-built dict, or a payload
    from before the shard carried it. The number is NOT guessed. Assuming 8
    because 8 is what the code passes today puts an invented denominator under a
    $12 verdict, which is the entire defect class this module exists to refuse;
    the callers below turn an unknown expectation into INCONCLUSIVE, so a
    refactor that drops the key is loud rather than silently permissive.

    A non-positive or non-numeric value is also None: "0 designs were ordered"
    would make every floor vacuous, which is the same permissive answer wearing
    a number.
    """
    if not isinstance(shard, dict):
        return None
    raw = shard.get("n_designs_expected")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# WHICH CSV IS THE DESIGN TABLE. Upstream writes several under ``inference/``
# and they are not interchangeable, so the choice is made by NAME, here, once —
# "the first CSV we globbed" is a denominator nobody picked, which is the whole
# defect class this module exists to refuse. On the live phase-1 run QC
# measured, four kinds appeared:
#
#   all_rewards_*.csv   nrows 8   ONE ROW PER GENERATED SAMPLE — this is it.
#   rewards_*_0.csv     nrows 8   per-chunk rewards; the ``_0`` is an index, so
#                                 there may be several and a total would have to
#                                 be summed over a set of files whose size is
#                                 upstream's to change. Summing an unknown
#                                 number of files IS the shrinkable denominator.
#   top_samples_*.csv   nrows 8   a RANKED SUBSET by name. It agreed only
#                                 because 8 was all there was; against a real
#                                 top-k it would under-report and manufacture a
#                                 false disagreement, i.e. a $12 INCONCLUSIVE on
#                                 a healthy run.
#   timing_0.csv        nrows 1   not a design table at all.
_DESIGN_TABLE_CSV_RE = re.compile(r"^all_rewards[^/\\]*\.csv$", re.IGNORECASE)


def design_table_rows(shard: Any) -> int | None:
    """How many samples the shard's REWARD TABLE says upstream scored, or None.

    THE INDEPENDENT WITNESS to the produced count. Every other number in phase 2
    that describes "how many designs are there" is derived from the same glob:
    de-duplicating it by name closes the case QC measured but cannot see a
    duplicate wearing two names. The reward table is written by upstream from
    its own sample list, so it is the one count in the payload that the file
    layout cannot move.

    ``None`` — never a guess — when no such CSV was collected, when several
    matched (which of them is THE table is then not a decision this code may
    make silently), or when the row count is not a usable positive integer.
    """
    if not isinstance(shard, dict):
        return None
    csvs = shard.get("csv_files")
    if not isinstance(csvs, dict):
        return None
    hits: list[int] = []
    for path, info in csvs.items():
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        if not _DESIGN_TABLE_CSV_RE.match(name) or not isinstance(info, dict):
            continue
        try:
            rows = int(info.get("nrows"))
        except (TypeError, ValueError):
            continue
        hits.append(rows)
    if len(hits) != 1 or hits[0] <= 0:
        return None
    return hits[0]


def design_count_disagreement(shard: Any, subject: str = "shard") -> str | None:
    """Why the produced count cannot be trusted as a denominator, or None.

    ONE-DIRECTIONAL ON PURPOSE, and the direction is the whole point. Fewer
    designs than the reward table has rows is the ordinary thin run — upstream
    filtered some samples into ``filtered_out_samples``, which the glob skips by
    design — and ``missing_designs_reason`` already holds that against the count
    the shard ORDERED. Refusing it here as well would turn every filtered run
    into an INCONCLUSIVE and cost $12 for a case that is already handled.

    MORE designs than were ordered, or more than upstream scored, is the other
    thing entirely: the produced count then contains something that is not a
    sample we paid for, and that count is the denominator of the quorum and the
    numerator of the one absolute floor phase 2 has. We do not know what the
    extra records are, so the honest answer is that this run was not measured —
    INCONCLUSIVE, the same posture this module takes toward every other
    denominator it cannot account for.

    Both comparisons are made against numbers the FILE LAYOUT CANNOT MOVE:
    ``n_designs_expected`` is ``nsamples * replicas`` recorded by the code that
    built the design command, and the reward table is written by upstream.
    """
    if not isinstance(shard, dict):
        return None
    produced = designs_produced(shard)
    files = design_files(shard)
    repeated = duplicate_design_names(shard)
    duplicate_note = (
        f" ({files} design files carried {produced} distinct name(s); repeated: "
        f"{', '.join(repeated[:8])})" if repeated else ""
    )
    expected = designs_expected(shard)
    if expected is not None and produced > expected:
        return (
            f"the {subject} was asked for {expected} designs and came back with "
            f"{produced} distinct ones{duplicate_note}. More designs than were "
            "ordered means the produced count holds records that are not "
            "samples we paid for — and that count is the denominator of the "
            "PASS quorum and the numerator of the only absolute floor phase 2 "
            "has. What the extra records are is not guessable, so this is an "
            "UNMEASURED run, not a verdict. Re-run phase 1, which dumps the run "
            "tree, and find out what upstream wrote under inference/."
        )
    rows = design_table_rows(shard)
    if rows is not None and produced > rows:
        return (
            f"the {subject} returned {produced} distinct designs but upstream's "
            f"reward table lists only {rows} sample(s){duplicate_note}. The "
            "design files and the table upstream scored disagree, so how many "
            "designs this shard actually produced is unknown — and every "
            "phase-2 gate divides by that number. This is an UNMEASURED run, "
            "not a verdict."
        )
    return None


def hit_quorum(n_produced: int, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> int:
    """How many designs must reach the patch, out of the designs PRODUCED.

    The ``max(1, ...)`` floor is retained but no longer does any work that
    matters: against the produced count it only bites at 0 or 1 designs, where
    "1 of 1" is an honest statement. Against the SCORABLE count — where it used
    to sit — the same floor made ``required`` equal to 1 for any shard with a
    single survivor, which is what turned 1-of-8 into an unconditional PASS.
    """
    try:
        n = max(0, int(n_produced))
    except (TypeError, ValueError):
        n = 0
    return max(1, math.ceil(thresholds.min_hit_fraction * n))


def thin_scorable_reason(n_produced: int, n_scorable: int,
                         thresholds: Thresholds = DEFAULT_THRESHOLDS,
                         subject: str = "shard") -> str | None:
    """Why too little of this shard is measurable to conclude anything, or None.

    Returned as a REASON rather than a bool so the operator reads the two
    numbers that decided it. See ``Thresholds.min_scorable_fraction``: below
    this floor the PASS quorum is arithmetically unreachable, so the only
    outcomes left would be FAIL and INCONCLUSIVE — and a run most of which
    could not be looked at is the second, never the first.
    """
    try:
        produced = max(0, int(n_produced))
        scorable = max(0, int(n_scorable))
    except (TypeError, ValueError):
        return None
    if produced <= 0 or scorable <= 0:
        # "nothing at all was scorable" is a different message, already written
        # by ``_unmeasurable_reason``; this predicate must not steal it.
        return None
    if scorable >= math.ceil(thresholds.min_scorable_fraction * produced):
        return None
    return (
        f"only {scorable} of the {produced} designs the {subject} produced "
        f"could be scored ({produced - scorable} were discarded), which is "
        f"under the {thresholds.min_scorable_fraction:.0%} floor. The "
        f"{hit_quorum(produced, thresholds)} designs a PASS needs cannot be "
        "reached from that few, so this is an UNMEASURED run — not evidence "
        "the feature works and not evidence it is broken. Re-run phase 1, "
        "which dumps the run tree and the per-design chain map, and find out "
        "why the designs were discarded before spending on phase 2 again."
    )


def missing_designs_reason(shard: Any,
                           thresholds: Thresholds = DEFAULT_THRESHOLDS,
                           subject: str = "shard") -> str | None:
    """Why this shard emitted too few designs to conclude anything, or None.

    THE ABSOLUTE FLOOR, and the one gate in phase 2 whose denominator no later
    step can shrink. ``thin_scorable_reason`` asks "of the files that were
    there, how many could we measure"; this asks the question BEFORE it — "were
    the files we paid for there at all". They are not interchangeable and the
    diagnostics keep them apart, because the operator's next move differs:

        requested 8, produced 1, scorable 1  -> upstream FILTERED them; go and
                                                read why (phase 1 dumps the run
                                                tree and the filter log)
        requested 8, produced 8, scorable 1  -> WE could not verify them; go and
                                                read the per-design chain map

    The bar is ``hit_quorum(expected)`` for the same reason the thin-scorable
    bar is: below it the PASS quorum for the run that was ORDERED is
    arithmetically unreachable — fewer design files exist than would have to
    land on the patch — so the only outcomes left are FAIL and INCONCLUSIVE, and
    "upstream discarded most of the samples" is not evidence the hotspots were
    ignored. It is INCONCLUSIVE, exactly as an unmeasurable run is.
    """
    expected = designs_expected(shard)
    produced = designs_produced(shard)
    if expected is None:
        return (
            f"the {subject} did not report how many designs it was asked for, "
            f"so the {produced} it returned cannot be told apart from a run "
            "upstream silently filtered down to a handful. The count is not "
            "assumed: a PASS quorum taken against a guessed denominator is the "
            "defect this harness exists to detect, and it must not be "
            "reproduced inside it. Re-run against a build of the shard that "
            "records n_designs_expected."
        )
    floor = hit_quorum(expected, thresholds)
    if produced >= floor:
        return None
    return (
        f"the {subject} was asked for {expected} designs and only {produced} "
        f"came back to be scored ({expected - produced} were filtered out by "
        "upstream or never written), which is under the "
        f"{floor} a PASS for a run of {expected} needs. The quorum cannot be "
        "reached from that few files, so this is an UNMEASURED run — not "
        "evidence the feature works and not evidence it is broken. Re-run "
        "phase 1, which dumps the run tree and the filter log, and find out "
        "why upstream discarded the samples before spending on phase 2 again."
    )


def _base_metrics(shard: Any) -> dict:
    if not isinstance(shard, dict):
        return {}
    unverified = unverified_designs(shard)
    return {
        "label": shard.get("label"),
        "exit_code": shard.get("exit_code"),
        # Beside the exit code, never behind it. The pair is the delivery
        # question in two numbers: `exit_code 1 | scored 8` and `exit_code 1 |
        # scored 0` are the same row to every other field in this dict and are
        # a shipped campaign and a dead one respectively.
        SCORED_KEY: scored_design_count(shard),
        # BOTH counts, always, and never collapsed into one. "requested 8,
        # produced 1" and "requested 8, produced 8" prescribe different next
        # moves, and only the pair distinguishes them.
        "n_designs_expected": designs_expected(shard),
        "n_designs": designs_produced(shard),
        # THE FILE COUNT AND THE DESIGN COUNT, ALWAYS BOTH. They differ exactly
        # when one design was written to several paths, and that difference is
        # invisible in every ratio the report prints — QC's shard rendered
        # "6/6 designs (8 requested, 6 scorable)" off ONE name. The reward-table
        # row count sits beside them because it is the only one of the three
        # the file layout cannot move.
        "n_design_files": design_files(shard),
        "n_design_rows": design_table_rows(shard),
        "n_complexes": shard.get("n_complexes"),
        "n_target_unverified": len(unverified),
        "hotspot_recall_median": shard.get("hotspot_recall_median"),
        "centroid_distance_median": shard.get("centroid_distance_median"),
        "cross_hotspot_recall_median": shard.get("cross_hotspot_recall_median"),
    }


_UNMEASURABLE = (
    "the shard ran cleanly but no per-design output could be scored "
    "(the designs are not binder+target complexes, so where the binder landed "
    "is not observable from them). This is NOT evidence the feature is broken "
    "and NOT evidence it works — re-run phase 1, which dumps the run tree, and "
    "find the AF2/RF3 refold artifact that does contain both chains, before "
    "spending on phase 2 again."
)

_RELABELLED = (
    "the shard ran cleanly and its outputs DO contain two chains, but the "
    "chains scored as target do not carry the input target's residues, so "
    "nothing could be scored. The design output uses a different chain "
    "labelling from the input PDB — most likely the binder was written with "
    "the target's chain id, in which case scoring it would have measured the "
    "BINDER's own contacts and reported a perfect hotspot recall off nothing. "
    "This is a MEASUREMENT defect, not a verdict on the feature: read "
    "designs[].target_identity.chain_hints, which names the chain that does "
    "look like the input target, and re-run with that convention."
)


def _unmeasurable_reason(shard: Any, tail: str = "") -> str:
    """Which flavour of 'unmeasurable' this shard is — they are not the same.

    "no complexes at all" sends the operator hunting for the refold artifact;
    "complexes whose chains are relabelled" sends them to the chain map. Giving
    the second the first's message costs a re-run of phase 1 to rediscover
    something the shard already reported.
    """
    body = _RELABELLED if unverified_designs(shard) else _UNMEASURABLE
    return (tail + body) if tail else body


def positive_verdict(pos: Any, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> Verdict:
    """Did the binders land on the patch we asked for?

    Every return of the body is stamped with the shard's DELIVERY state — see
    ``annotate_delivery``. Wrapping the whole body rather than each of its eight
    returns is deliberate: a stamp that has to be remembered at a new return
    point is a stamp that will be missing from it.
    """
    return annotate_delivery(_positive_verdict(pos, thresholds), pos)


def _positive_verdict(pos: Any, thresholds: Thresholds) -> Verdict:
    name = "positive"
    failure = shard_failure(pos)
    if failure is not None:
        return Verdict(name, FAIL, f"the positive shard did not complete: {failure}",
                       _base_metrics(pos))
    metrics = _base_metrics(pos)
    scorable = scorable_designs(pos, "hotspot_recall")
    produced = designs_produced(pos)
    metrics["n_scorable"] = len(scorable)

    # BEFORE THE FLOOR THAT USES THE COUNT: whether the produced count means
    # anything at all is upstream of whether it is big enough. A count inflated
    # by duplicate files clears the floor on records nobody ordered.
    inflated = design_count_disagreement(pos, "positive shard")
    if inflated is not None:
        return Verdict(name, INCONCLUSIVE, inflated, metrics)

    # BEFORE anything about measurability, because it is upstream of it: a
    # shard that emitted one file has one file to verify, and "1/1 designs
    # (1 scorable) recall >= 0.5 (needed 1)" would otherwise read as a clean
    # PASS. ``designs_produced`` is a post-filter survivor count — see its
    # docstring — so the quorum still had a denominator nobody chose until this
    # gate compared it with the count the shard ASKED for.
    missing = missing_designs_reason(pos, thresholds, "positive shard")
    if missing is not None:
        return Verdict(name, INCONCLUSIVE, missing, metrics)

    if not scorable:
        return Verdict(name, INCONCLUSIVE, _unmeasurable_reason(pos), metrics)

    # AGAINST THE DESIGNS THE SHARD PRODUCED, NOT THE ONES THAT SURVIVED. The
    # denominator used to be ``len(scorable)``, so 7 of 8 designs being refused
    # as unverifiable SHRANK the bar instead of raising the alarm: required
    # collapsed to 1, the single survivor met it, and phase 2 exited 0 on
    # 1-of-8 — a green light for the production flag off one design.
    #
    # STILL THE PRODUCED COUNT AND NOT ``designs_expected``, DELIBERATELY. The
    # absolute floor above has already established ``produced >=
    # hit_quorum(expected)``, so the two only differ inside that band — and
    # taking the quorum against ``expected`` there would make it UNREACHABLE
    # from the smallest scorable set ``min_scorable_fraction`` admits, which is
    # precisely the wrong-FAIL that ``Thresholds.__post_init__`` now refuses to
    # let the knobs create. The residual is bounded and measured: the weakest
    # PASS a run of 8 can now reach is 5 designs demonstrably on the patch out
    # of 8 ordered (6 produced, 5 scorable, 5 on patch), against 1 before.
    required = hit_quorum(produced, thresholds)
    on_patch = sum(
        1 for d in scorable
        if d["hotspot_recall"] >= thresholds.min_recall_per_design
    )
    # ``n_designs`` is already the produced count (``_base_metrics``); a second
    # key for the same number is how two names for one thing start to disagree.
    #
    # ``requested_found_median`` is HOW MUCH OF THE PATCH THE DESIGNS COULD SEE.
    # The identity gate deliberately admits a design that CROPS the target, and
    # a cropped design's recall is diluted by hotspots that are not in its
    # output at all — ``score_from_contacts`` divides by every requested hotspot
    # while only those present can ever be counted. On this side the dilution
    # pushes recall DOWN, so it can only cost a PASS, never fabricate one; it is
    # surfaced rather than gated for exactly that reason. The negative control,
    # where the same dilution points the other way, does gate on it.
    metrics.update(n_on_patch=on_patch, n_required=required,
                   min_scorable_fraction=thresholds.min_scorable_fraction,
                   requested_found_median=median(
                       d.get("requested_found_in_structure") for d in scorable))

    thin = thin_scorable_reason(produced, len(scorable), thresholds,
                                "positive shard")
    if thin is not None:
        return Verdict(name, INCONCLUSIVE, thin, metrics)

    centroid_median = pos.get("centroid_distance_median")
    if centroid_median is None:
        return Verdict(
            name, INCONCLUSIVE,
            f"{on_patch}/{produced} designs reached the requested patch but "
            "no centroid distance could be computed, so how far off they sit is "
            "unknown", metrics)

    # Named in the verdict text, not only in the metrics: "6/8 designs" is a
    # different claim depending on whether 8 was ordered or 8 is what survived a
    # filter, and the operator reading the console must not have to guess which.
    #
    # ...and the same is true of "8 designs" when 8 FILES carried fewer names.
    # A PASS prints no diagnostic line, so the only place that fact can reach
    # the operator is this sentence. Appended only when the two counts differ,
    # so a healthy run's console is byte-for-byte what it was.
    ordered = designs_expected(pos)
    n_files = design_files(pos)
    dupes = (f", from {n_files} design files" if n_files != produced else "")
    if on_patch >= required and centroid_median <= thresholds.max_centroid_a:
        return Verdict(
            name, PASS,
            f"{on_patch}/{produced} designs ({ordered} requested, "
            f"{len(scorable)} scorable{dupes}) recall "
            f">= {thresholds.min_recall_per_design} of the requested hotspots "
            f"(needed {required}), median centroid offset "
            f"{centroid_median:.2f} A <= {thresholds.max_centroid_a} A",
            metrics)
    return Verdict(
        name, FAIL,
        f"only {on_patch}/{produced} designs ({ordered} requested, "
        f"{len(scorable)} scorable{dupes}) reached "
        f"the requested patch (needed {required}) and/or the median centroid "
        f"offset {centroid_median:.2f} A exceeds {thresholds.max_centroid_a} A",
        metrics)


def cross_reference_size(shard: Any) -> int | None:
    """How many positive hotspots this shard was cross-scored against.

    A median RECALL cannot be turned into "how many hotspots did the median
    design touch" without this, and the ceiling is expressed as a count on
    purpose (see ``Thresholds``). ``run_shard`` always reports it; a shard that
    does not is not comparable against a count, and guessing a denominator
    would put a made-up number under a verdict.
    """
    if not isinstance(shard, dict):
        return None
    spec = shard.get("cross_reference_hotspots")
    if spec is None:
        return None
    want, _bad = parse_spec(spec)
    return len(want)


def negative_verdict(neg: Any, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> Verdict:
    """Same PDB, same seed, a patch >= 25 A away — the interface must MOVE.

    Wrapped for the delivery stamp; see ``positive_verdict``.
    """
    return annotate_delivery(_negative_verdict(neg, thresholds), neg)


def _negative_verdict(neg: Any, thresholds: Thresholds) -> Verdict:
    name = "negative"
    failure = shard_failure(neg)
    if failure is not None:
        return Verdict(name, FAIL, f"the negative shard did not complete: {failure}",
                       _base_metrics(neg))
    metrics = _base_metrics(neg)
    scorable = scorable_designs(neg, "cross_hotspot_recall")
    produced = designs_produced(neg)
    metrics["n_scorable_cross"] = len(scorable)
    metrics["min_scorable_fraction"] = thresholds.min_scorable_fraction

    inflated = design_count_disagreement(neg, "negative shard")
    if inflated is not None:
        return Verdict(name, INCONCLUSIVE, inflated, metrics)

    # The absolute floor, ahead of every measurability question, for the reason
    # given in ``positive_verdict``: a negative control built out of one
    # surviving file blesses the whole comparison off one design, and its median
    # cross-recall would read 0.00 — a textbook clean negative — either way.
    missing = missing_designs_reason(neg, thresholds, "negative shard")
    if missing is not None:
        return Verdict(name, INCONCLUSIVE, missing, metrics)

    cross = neg.get("cross_hotspot_recall_median")
    if not scorable or cross is None:
        # THE false-pass regression. `(cross or 0) <= 0.2` made an unmeasurable
        # negative control PASS, which blesses a feature nobody measured.
        return Verdict(name, INCONCLUSIVE,
                       _unmeasurable_reason(
                           neg, "the negative control could not be measured "
                                "against the positive patch: "), metrics)

    # THE SAME DENOMINATOR HOLE, IN ITS MEDIAN-SHAPED FORM. This verdict never
    # counted anything, so it looked immune — but ``cross`` is a median taken
    # over the SCORABLE designs alone, and a median of one survivor out of
    # eight is a number about one design being reported as a property of the
    # shard. 1-of-8 must not be able to bless the negative control either.
    thin = thin_scorable_reason(produced, len(scorable), thresholds,
                                "negative shard")
    if thin is not None:
        return Verdict(name, INCONCLUSIVE,
                       "the negative control could not be measured against the "
                       "positive patch: " + thin, metrics)

    n_hotspots = cross_reference_size(neg)
    metrics["n_cross_reference_hotspots"] = n_hotspots
    metrics["max_cross_hotspots"] = thresholds.max_cross_hotspots
    if not n_hotspots:
        return Verdict(
            name, INCONCLUSIVE,
            f"the negative shard reports a cross-recall of {cross:.2f} but not "
            "which positive hotspots it was scored against, so 'how many of "
            "them did the median design touch' cannot be computed. The ceiling "
            "is a count, not a fraction, precisely so it does not land between "
            "achievable medians — comparing against a guessed denominator would "
            "put an invented number under a $12 verdict.",
            metrics)
    touched = cross * n_hotspots
    metrics["cross_hotspots_touched_median"] = round(touched, 3)

    # HOW MANY OF THOSE HOTSPOTS THE DESIGNS COULD SEE AT ALL.
    #
    # ``score_from_contacts`` divides by every REQUESTED hotspot while its
    # numerator can only count residues that exist in the design's output, and
    # the identity gate deliberately admits a design that CROPS the target. So
    # a negative design carrying 1 of the 4 positive hotspots and sitting
    # squarely on it scores recall 0.25, which this verdict multiplies back into
    # "touches 1.00 of the 4" and blesses — when the truth is that it touched
    # 100% of everything that was measurable. QC measured exactly that: PASS.
    #
    # The count itself is right; what was wrong is comparing it against a
    # ceiling defined over the FULL patch using a view of part of it. The
    # denominator is therefore made to agree with the numerator: the hotspots
    # the median design could not see are counted, and a PASS has to survive the
    # worst case in which every one of them was touched too. With the full patch
    # present (``unmeasurable == 0``) that is arithmetically identical to the
    # old comparison, so a healthy run's verdict and console are unchanged.
    #
    # Fixing it the other way — dividing recall by the hotspots actually present
    # — was rejected: ``hotspot_recall`` also gates the POSITIVE shard, where
    # the same dilution pushes recall DOWN, and raising it there would loosen a
    # $12 PASS to buy a fix for a different verdict.
    found_median = median(d.get("cross_requested_found_in_structure")
                          for d in scorable)
    metrics["cross_hotspots_visible_median"] = found_median
    if found_median is None:
        return Verdict(
            name, INCONCLUSIVE,
            f"the negative shard reports a cross-recall of {cross:.2f} against "
            f"{n_hotspots} positive hotspots but does not say how many of them "
            "its designs actually CONTAIN. A design that crops the target "
            "scores a diluted recall against hotspots that are not in its "
            "output at all, and that dilution reads here as a clean negative "
            "control — the one direction that must never happen. Re-run "
            "against a build of the shard that records "
            "cross_requested_found_in_structure.",
            metrics)
    unmeasurable = max(0.0, n_hotspots - found_median)
    metrics["cross_hotspots_unmeasurable"] = round(unmeasurable, 3)

    own_centroid = neg.get("centroid_distance_median")
    if own_centroid is None:
        return Verdict(
            name, INCONCLUSIVE,
            f"the median negative design touches {touched:.2f} of the "
            f"{n_hotspots} positive hotspots, but the negative shard's own "
            "centroid offset could not be computed, so we cannot say the "
            "interface moved TO the far patch rather than nowhere",
            metrics)
    # 1e-9 because touched is a float product of a median and a count, and an
    # exact lattice value (2 * 0.5) must not fail on binary representation.
    ceiling = thresholds.max_cross_hotspots + 1e-9
    if touched <= ceiling and touched + unmeasurable > ceiling:
        # Demonstrated overlap is under the ceiling, but only because part of
        # the patch was not in the output to be looked at. Not a FAIL — nothing
        # was shown to overlap — and emphatically not a PASS.
        return Verdict(
            name, INCONCLUSIVE,
            f"the median negative design touches {touched:.2f} of the "
            f"{n_hotspots} positive hotspots (max "
            f"{thresholds.max_cross_hotspots}), but only {found_median:.2f} of "
            f"those {n_hotspots} are present in the design outputs at all — "
            f"{unmeasurable:.2f} were cropped out and cannot be scored. Under "
            "the ceiling here means 'we did not see an overlap', not 'there was "
            "none': the design's recall is diluted by hotspots it does not "
            "contain, and reading that as a clean negative control is the one "
            "direction that must never happen. Re-run phase 1 and read the "
            "per-design coverage before spending on phase 2 again.",
            metrics)
    if touched <= ceiling and own_centroid <= thresholds.max_centroid_a:
        return Verdict(
            name, PASS,
            f"the median negative design touches {touched:.2f} of the "
            f"{n_hotspots} positive hotspots (max "
            f"{thresholds.max_cross_hotspots}) and the binders sit "
            f"{own_centroid:.2f} A from their own requested patch",
            metrics)
    return Verdict(
        name, FAIL,
        f"the median negative design touches {touched:.2f} of the "
        f"{n_hotspots} positive hotspots (max {thresholds.max_cross_hotspots}) "
        f"and/or the binders sit {own_centroid:.2f} A from the far patch they "
        f"were pointed at (max {thresholds.max_centroid_a} A)",
        metrics)


def null_verdict(pos: Any, null: Any,
                 thresholds: Thresholds = DEFAULT_THRESHOLDS) -> Verdict:
    """THE feature-is-a-lie detector.

    A shard given NO hotspots at all, scored against the positive patch, must do
    materially worse than the shard that asked for it. If it does not, the
    hotspot argument changed nothing: it was passed, upstream dropped it, and
    every other signal in the run — exit code, design count, reward CSV — looks
    identical to a run that honoured it.

    Wrapped for the delivery stamp; see ``positive_verdict``. BOTH shards are
    stamped, and the worst of the two is what the verdict carries: a comparison
    is only as sound as its weaker half.
    """
    return annotate_delivery(_null_verdict(pos, null, thresholds), pos, null)


def _null_verdict(pos: Any, null: Any, thresholds: Thresholds) -> Verdict:
    name = "null"
    for shard, which in ((pos, "positive"), (null, "null")):
        failure = shard_failure(shard)
        if failure is not None:
            return Verdict(name, FAIL,
                           f"the {which} shard did not complete: {failure}",
                           {"positive": _base_metrics(pos), "null": _base_metrics(null)})

    # THE FIELD IS CHOSEN ONCE, then the median AND the count are both read off
    # it. They used to be selected by two different tests — the median fell back
    # when ``cross_hotspot_recall_median`` was None, the count fell back when
    # ``n_pos_cross`` was FALSY — which agree only because ``run_shard`` cannot
    # currently emit a non-None cross median with zero cross-scorable designs.
    # A shard that ever did would have had the thin gate applied to a count
    # belonging to the other metric: the number under the verdict and the number
    # gating it would be about different measurements.
    pos_field = "cross_hotspot_recall"
    pos_recall = pos.get("cross_hotspot_recall_median")
    if pos_recall is None:
        pos_field = "hotspot_recall"
        pos_recall = pos.get("hotspot_recall_median")
    null_recall = null.get("cross_hotspot_recall_median")
    n_scorable_positive = len(scorable_designs(pos, pos_field))
    n_scorable_null = len(scorable_designs(null, "cross_hotspot_recall"))
    metrics = {
        "positive_recall_median": pos_recall,
        # Which measurement the positive side of the margin is, so the count
        # beside it can be read as belonging to the same thing.
        "positive_recall_field": pos_field,
        "null_recall_median": null_recall,
        "min_margin": thresholds.min_null_margin,
        "n_scorable_positive": n_scorable_positive,
        "n_scorable_null": n_scorable_null,
        "n_expected_positive": designs_expected(pos),
        "n_expected_null": designs_expected(null),
        "n_produced_positive": designs_produced(pos),
        "n_produced_null": designs_produced(null),
        "min_scorable_fraction": thresholds.min_scorable_fraction,
    }

    # The absolute floor on BOTH shards, before either median is looked at. The
    # margin is the number that says the hotspots were not silently dropped, and
    # a margin computed between two shards upstream filtered down to one file
    # each is a $12 verdict resting on two designs that were never ordered as a
    # sample of anything.
    for shard, which in ((pos, "positive shard"), (null, "null shard")):
        inflated = design_count_disagreement(shard, which)
        if inflated is not None:
            return Verdict(name, INCONCLUSIVE,
                           "the null control could not be compared with the "
                           "positive run: " + inflated, metrics)
        missing = missing_designs_reason(shard, thresholds, which)
        if missing is not None:
            return Verdict(name, INCONCLUSIVE,
                           "the null control could not be compared with the "
                           "positive run: " + missing, metrics)

    if pos_recall is None or null_recall is None:
        blind = pos if pos_recall is None else null
        return Verdict(name, INCONCLUSIVE,
                       _unmeasurable_reason(
                           blind, "the null control could not be compared with "
                                  "the positive run: "), metrics)

    # A MEDIAN WITH NOTHING UNDER IT. ``median`` returns None for an empty set,
    # so ``run_shard`` cannot emit this shape — but the count and the median now
    # come from ONE field precisely so they can be checked against each other,
    # and "0.90 over 0 scorable designs" is a number with no measurement behind
    # it. ``thin_scorable_reason`` deliberately declines to speak for a zero
    # count (that message belongs to ``_unmeasurable_reason``) and this verdict
    # has no ``if not scorable`` guard of its own, so without this the
    # incoherent shard would walk straight into the margin comparison.
    for count, recall, which in ((n_scorable_positive, pos_recall, "positive"),
                                 (n_scorable_null, null_recall, "null")):
        if count <= 0:
            return Verdict(
                name, INCONCLUSIVE,
                "the null control could not be compared with the positive run: "
                f"the {which} shard reports a recall median of {recall:.2f} "
                "taken over 0 scorable designs, so the number the margin rests "
                "on was measured from nothing", metrics)
    # BOTH SIDES OF THE COMPARISON, for the reason spelled out in
    # ``negative_verdict``: each recall is a median over that shard's scorable
    # designs, so a margin computed from one survivor per side is a $12 verdict
    # resting on two designs. The margin is the single most consequential
    # number in phase 2 — it is what says the hotspots were not silently
    # dropped — and it must not be read off a shard nobody could measure.
    for shard, count, which in ((pos, n_scorable_positive, "positive shard"),
                                (null, n_scorable_null, "null shard")):
        thin = thin_scorable_reason(designs_produced(shard), count, thresholds,
                                    which)
        if thin is not None:
            return Verdict(name, INCONCLUSIVE,
                           "the null control could not be compared with the "
                           "positive run: " + thin, metrics)
    # THE SAME CROP AS IN ``negative_verdict``, WEARING THE MARGIN'S SHAPE, and
    # it points at a false PASS here too. ``score_from_contacts`` divides by
    # every requested hotspot while only those present in the output can be
    # counted, so a NULL shard whose designs crop the target reports a recall
    # far below what it actually achieved: 1 of the 4 positive hotspots present
    # and touched reads as 0.25, the margin against a full-view positive run
    # comes out 0.75, and the verdict blesses the run — when the no-hotspot
    # control had landed on the patch just as well as the hotspot run did. That
    # is precisely the "it was passed and ignored" case this verdict exists to
    # catch, reported as its opposite.
    #
    # Only the NULL side needs the worst case. A crop on the positive side
    # lowers ``pos_recall``, which shrinks the margin — it can cost a PASS,
    # never manufacture one — and a crop that hits both sides equally scales the
    # margin down. The dangerous asymmetry is a null that could see less.
    n_cross_null = cross_reference_size(null)
    null_found = median(d.get("cross_requested_found_in_structure")
                        for d in scorable_designs(null, "cross_hotspot_recall"))
    metrics["null_hotspots_visible_median"] = null_found
    metrics["n_cross_reference_hotspots_null"] = n_cross_null
    if not n_cross_null or null_found is None:
        return Verdict(
            name, INCONCLUSIVE,
            "the null control could not be compared with the positive run: it "
            f"recalls {null_recall:.2f} of the positive patch, but the shard "
            "does not say how many of those hotspots its designs actually "
            "CONTAIN. A design that crops the target scores a diluted recall "
            "against hotspots that are not in its output at all, and here that "
            "dilution reads as a wide margin — i.e. as proof the hotspots "
            "steered the search, which is the one conclusion that must never be "
            "reached by accident. Re-run against a build of the shard that "
            "records cross_requested_found_in_structure.",
            metrics)
    # The most the null run could have recalled if every hotspot missing from
    # its outputs had been touched too.
    null_worst = min(1.0, null_recall + max(0.0, n_cross_null - null_found)
                     / n_cross_null)
    metrics["null_recall_worst_case"] = round(null_worst, 4)
    metrics["margin"] = pos_recall - null_recall
    metrics["margin_worst_case"] = pos_recall - null_worst
    cropped = (f" (worst case {null_worst:.2f}, since only {null_found:.2f} of "
               f"the {n_cross_null} positive hotspots are present in the null "
               "designs at all)" if null_worst > null_recall else "")
    if null_worst < pos_recall - thresholds.min_null_margin:
        return Verdict(
            name, PASS,
            f"a no-hotspot run recalls {null_recall:.2f} of the positive patch "
            f"vs {pos_recall:.2f} with hotspots (margin "
            f"{pos_recall - null_recall:.2f} > {thresholds.min_null_margin})"
            + cropped,
            metrics)
    if cropped and null_recall < pos_recall - thresholds.min_null_margin:
        # The measured margin clears the bar and the worst case does not, so the
        # margin is made of hotspots nobody could look at. Not a FAIL — nothing
        # showed the null run reaching the patch — and not a PASS either.
        return Verdict(
            name, INCONCLUSIVE,
            "the null control could not be compared with the positive run: it "
            f"recalls {null_recall:.2f} of the positive patch against "
            f"{pos_recall:.2f} with hotspots, but only {null_found:.2f} of the "
            f"{n_cross_null} positive hotspots are present in the null designs "
            "at all. The rest were cropped out and count as misses, so the "
            f"margin of {pos_recall - null_recall:.2f} is partly made of "
            "hotspots nobody could look at — at worst the null run recalled "
            f"{null_worst:.2f} and the margin is "
            f"{pos_recall - null_worst:.2f}. Re-run phase 1 and read the "
            "per-design coverage before spending on phase 2 again.",
            metrics)
    return Verdict(
        name, FAIL,
        f"a run with NO hotspots recalls {null_recall:.2f} of the positive patch "
        f"against {pos_recall:.2f} with them (margin "
        f"{pos_recall - null_recall:.2f}, need > {thresholds.min_null_margin}). "
        "The hotspot argument changed nothing — it was passed and ignored.",
        metrics)


# The counts an operator needs to tell a near-miss from a catastrophe, in the
# order they are read: how many designs were ORDERED, how many came back, how
# many could be looked at, how many landed, and what the bar was. Rendered from
# ``Verdict.metrics``, which every verdict above already fills — the gap was
# never the measurement, it was that nothing printed it.
#
# "requested" sits immediately before "produced" on purpose. They are the two
# halves of the same question and the pair is what separates the two ways a run
# goes thin: `requested 8 | produced 1 | scorable 1` is upstream FILTERING the
# samples, `requested 8 | produced 8 | scorable 1` is US failing to verify them.
# Either alone reads as "1 design", and they send the operator to different
# files.
_VERDICT_DIAGNOSTIC_FIELDS: tuple[tuple[str, str], ...] = (
    # FIRST, because it says whether the numbers that follow describe a run
    # production would have shipped. A DEGRADED verdict also carries the reason
    # prefix, which reaches the console on a PASS as well; this line is the same
    # fact in the diagnostics block, where a FAIL or an INCONCLUSIVE is read.
    ("delivery", "delivery"),
    ("n_scored_designs", "designs fully scored (production would deliver)"),
    ("n_designs_expected", "designs requested"),
    ("n_designs", "designs produced"),
    # Immediately after the design count, because their whole job is to be read
    # against it: "produced 6 | design files 6" and "produced 1 | design files
    # 6" are the same run to every ratio in the report and completely different
    # runs to the operator.
    ("n_design_files", "design files"),
    ("n_design_rows", "reward-table rows"),
    ("n_expected_positive", "positive designs requested"),
    ("n_produced_positive", "positive designs produced"),
    ("n_expected_null", "null designs requested"),
    ("n_produced_null", "null designs produced"),
    ("n_complexes", "binder+target complexes"),
    ("n_target_unverified", "discarded (target unverified)"),
    ("n_scorable", "scorable"),
    ("n_scorable_cross", "scorable vs the positive patch"),
    ("n_scorable_positive", "scorable (positive)"),
    ("n_scorable_null", "scorable (null)"),
    ("n_on_patch", "on the requested patch"),
    ("n_required", "needed for a PASS"),
    ("requested_found_median", "requested hotspots present in the design (median)"),
    ("cross_hotspots_touched_median", "positive hotspots touched (median)"),
    ("cross_hotspots_visible_median", "positive hotspots present (median)"),
    ("cross_hotspots_unmeasurable", "positive hotspots cropped out (median)"),
    ("max_cross_hotspots", "max hotspots the negative may touch"),
    ("null_hotspots_visible_median", "positive hotspots present in the null designs (median)"),
    ("margin", "margin"),
    ("margin_worst_case", "margin if every cropped hotspot was touched"),
    ("min_margin", "margin needed"),
)


def verdict_diagnostics(verdict: Any) -> list[str]:
    """The counts behind a NON-PASS verdict, as console lines.

    Without these a near-miss and a catastrophe read identically: "only 1/8
    designs reached the requested patch" and "only 7/8 did" are one prose line
    each, and the operator cannot tell whether to re-run, re-measure or stop.
    The numbers were already in ``Verdict.metrics`` and were never printed —
    ``_print_verdict`` emitted name, outcome and reason and dropped the dict.

    Empty for a PASS, so a green run's console is byte-for-byte what it was,
    and empty when there is nothing to say, so this can be called
    unconditionally. Nested metrics (the null verdict's per-shard blocks) are
    skipped rather than flattened into an unreadable line.
    """
    outcome = getattr(verdict, "outcome", None)
    if outcome is None or outcome == PASS:
        return []
    metrics = getattr(verdict, "metrics", None)
    if not isinstance(metrics, dict):
        return []
    parts = []
    for key, label in _VERDICT_DIAGNOSTIC_FIELDS:
        if key not in metrics:
            continue
        value = metrics[key]
        if isinstance(value, (dict, list, tuple)):
            continue
        if isinstance(value, float):
            value = f"{value:.3g}"
        parts.append(f"{label} {'n/a' if value is None else value}")
    if not parts:
        return []
    return [f"            {' | '.join(parts)}"]


def delivery_note(shard: Any) -> list[str]:
    """"This shard crashed AND delivered" — as console lines, or empty.

    THE POINT OF THE WHOLE DELIVERY SPLIT, said where an operator will read it.
    ``shard_failure`` no longer condemns a shard that exited non-zero with
    designs scored, and the danger in that change is the opposite of the one it
    fixes: a crash quietly becoming invisible because it no longer moves the
    verdict. It has to move the CONSOLE instead.

    Empty for a CLEAN shard, so a healthy run's console is unchanged, and empty
    for a FAILED one, whose reason already says the same thing once.

    A RENDERER, not a measurement — the state is decided by ``shard_delivery``
    and formatted here so the offline suite can assert on the lines rather than
    on the presence of a print.
    """
    detail = shard_degradation(shard)
    if detail is None:
        return []
    label = (shard.get("label") if isinstance(shard, dict) else None) or "shard"
    return [
        f"\n[canary] DELIVERED-DEGRADED [{label}]: {detail}",
        f"[canary]   reward-table rows {shard.get('n_reward_rows')}, "
        f"fully scored {scored_design_count(shard)}, "
        f"design files {design_files(shard)}. Production would have shipped "
        "this run; the verdict below judges the MEASUREMENTS, not the exit code.",
    ]


def designs_yield_note(shard: Any,
                       thresholds: Thresholds = DEFAULT_THRESHOLDS) -> list[str]:
    """"Upstream kept N of the M we ordered" — as console lines, or empty.

    FOR PHASE 1, WHICH COSTS $4 AND EXISTS TO STOP PHASE 2 COSTING $12 FOR
    NOTHING. Phase 1 asserts WIRING, so a thin yield is not a failure there and
    ``phase1_verdict`` rightly does not gate on it — but it is the single fact
    that decides whether the next command is worth running, because phase 2's
    controls return INCONCLUSIVE below this floor. The number was in the shard's
    JSON and nothing pointed at it.

    A RENDERER, not a measurement: it reads two integers the container already
    reported and formats them. Empty when the shard delivered what it ordered,
    so a healthy run's console is unchanged, and empty when the counts are not
    both known, so it can be called unconditionally.
    """
    expected = designs_expected(shard)
    if expected is None:
        return []
    produced = designs_produced(shard)
    if produced >= expected:
        return []
    floor = hit_quorum(expected, thresholds)
    verdict = ("would return INCONCLUSIVE at this yield"
               if produced < floor else
               "can still reach a verdict at this yield")
    return [
        f"\n[canary] NOTE: {produced} of the {expected} designs this shard "
        f"ordered came back ({expected - produced} missing). Upstream's filter "
        "puts discarded samples in filtered_out_samples, which the scorer "
        "skips by design, so this is normally the filter rather than a crash. "
        f"Phase 2 needs at least {floor} of {expected} per control and "
        f"{verdict} — read the filter log in the run tree above before "
        "spending ~$12."
    ]


def overall_outcome(verdicts: Sequence[Verdict]) -> str:
    """FAIL beats INCONCLUSIVE beats PASS; an EMPTY set is never a PASS."""
    outcomes = [v.outcome for v in verdicts]
    if not outcomes:
        return INCONCLUSIVE
    if FAIL in outcomes:
        return FAIL
    if INCONCLUSIVE in outcomes:
        return INCONCLUSIVE
    return PASS


def phase2_report(pos: Any, neg: Any, null: Any,
                  thresholds: Thresholds = DEFAULT_THRESHOLDS) -> dict:
    verdicts = [
        positive_verdict(pos, thresholds),
        negative_verdict(neg, thresholds),
        null_verdict(pos, null, thresholds),
    ]
    outcome = overall_outcome(verdicts)
    return {
        "verdicts": verdicts,
        "overall": outcome,
        "exit_code": EXIT_CODES[outcome],
    }


# ---------------------------------------------------------------------------
# Phase 0 / phase 1 aggregation
# ---------------------------------------------------------------------------


def phase0_pass(results: Any,
                required: Sequence[str] = PHASE0_CONTROLS) -> bool:
    """Every named control must be present AND explicitly ``pass is True``.

    ``all(v.get("pass") for v in results.values() if isinstance(v, dict))`` is
    vacuously True when no control ran, when a control was renamed, and when a
    control recorded a non-dict — three different ways to report a green phase 0
    that never executed. Naming the controls makes an absent one a failure.
    """
    if not isinstance(results, dict) or not required:
        return False
    for name in required:
        entry = results.get(name)
        if not isinstance(entry, dict) or entry.get("pass") is not True:
            return False
    return True


def phase1_verdict(shard: Any) -> Verdict:
    """Phase 1 asserts WIRING, not geometry.

    Whether the per-design outputs are complexes is what phase 1 is there to
    DISCOVER, so finding none is a reportable observation, never a failure.

    Wrapped for the delivery stamp; see ``positive_verdict``. This is the phase
    where it matters most: phase 1 costs $4, its whole job is to say whether the
    ~$12 run is worth starting, and a shard that crashed late while delivering 8
    scored designs was reported here as a flat FAIL.
    """
    return annotate_delivery(_phase1_verdict(shard), shard)


def _phase1_verdict(shard: Any) -> Verdict:
    name = "phase1"
    failure = shard_failure(shard)
    if failure is not None:
        return Verdict(name, FAIL, f"the phase 1 shard did not complete: {failure}",
                       _base_metrics(shard))
    metrics = _base_metrics(shard)
    hydra = shard.get("hydra") if isinstance(shard, dict) else None
    metrics["hydra"] = hydra
    if not isinstance(hydra, dict):
        return Verdict(name, INCONCLUSIVE,
                       "no resolved Hydra config was found in the run tree, so "
                       "we cannot tell which target the search actually used",
                       metrics)
    if not hydra.get("task_name_selected"):
        return Verdict(
            name, FAIL,
            "the resolved config's task_name is "
            f"{hydra.get('task_name_values')}, not our registered key — the "
            "search ran against a different target",
            metrics)
    if not hydra.get("hotspots_match"):
        return Verdict(
            name, FAIL,
            f"the composed record carries hotspots {hydra.get('observed_hotspots')}, "
            f"not the ones we registered (missing {hydra.get('hotspots_missing')}, "
            f"unexpected {hydra.get('hotspots_unexpected')})",
            metrics)
    if hydra.get("hotspots_order_matches") is False:
        # Reported, not failed. Upstream matches hotspots as a membership test,
        # so order changes nothing about the run — but OmegaConf round-tripping
        # a reordered list is worth SEEING, and the previous code spent $4 to
        # FAIL a correct run over exactly this.
        return Verdict(
            name, PASS,
            "the resolved config selected our registered key and carries our "
            f"hotspots, reordered to {hydra.get('observed_hotspots')} (order is "
            "informational: upstream matches hotspots by membership, not by "
            "position)", metrics)
    return Verdict(name, PASS,
                   "the resolved config selected our registered key and carries "
                   "our hotspots", metrics)


# ---------------------------------------------------------------------------
# Failure diagnostics — the text upstream wrote and the operator never saw
#
# WHY THIS SECTION EXISTS. Two live phase-1 runs failed on 2026-08-05 and taught
# us nothing at all. Both returned:
#
#     exit_code 1, runtime 49 s, peak_vram 4 MB, n_complexes 0, tree [],
#     csv_files {}, hydra null, every median null
#
# and the only text that reached the console was upstream's own summary —
# "✗ generate failed with exit code 1", the argv, and a CalledProcessError.
# ``complexa design`` runs ``generate`` as a SUB-subprocess whose stdout and
# stderr are REDIRECTED INTO A FILE, so the actual traceback never crosses into
# ``run_shard``'s stream, which is the only stream Modal forwards. The exit code
# is all that survives, and an exit code is not a diagnosis.
#
# THE COLLECTORS WERE ALL POINTED AT THE WRONG DIRECTORY. Designs, tree, CSVs
# and the Hydra config were every one of them globbed under ``inference/``,
# while upstream writes its diagnostics under ``logs/design_pipeline_*``. So
# ``tree: []`` was literally true and completely uninformative: the harness
# looked only where a SUCCESSFUL run puts its outputs and nowhere a failing one
# puts its reasons.
#
# WHY THE LOGIC IS HERE AND NOT INLINE IN ``run_shard``. Same reason as every
# other decision in this file: ``_hotspot_canary.py`` imports ``modal`` at
# module scope, so nothing written inside it is reachable by pytest. Truncation
# boundaries, newest-directory selection, the total-byte budget and the "is this
# run blind?" predicate are all decisions, they are all cheap to get subtly
# wrong, and every one of them is executed by the offline suite from here. The
# Modal module supplies bytes and paths; it decides nothing.
# ---------------------------------------------------------------------------

# Per-file tail. Big enough for a Python traceback plus the CUDA/Hydra preamble
# that usually precedes it, small enough that it cannot dominate the shard's
# return payload or scroll a real failure off a console.
LOG_TAIL_BYTES = 6144

# ...and a budget across ALL files, because the per-file cap alone multiplies by
# however many logs happen to exist. A shard that returns a megabyte of log is a
# shard whose verdict nobody reads.
LOG_TOTAL_BYTES = 24576

# Where upstream actually writes, relative to the work directory. The first
# pattern matches BOTH the per-run directory and the sibling file next to it
# (``design_pipeline_<key>_<run>_Y..M..D..H..M..S..log``); the second is kept
# explicit because that sibling is one of the two files upstream's own error
# message points at, and a pattern that silently stopped matching it should be
# visible in the report rather than inferable from an absence.
LOG_GLOBS = ("logs/design_pipeline_*", "logs/design_pipeline_*.log")

# The pipeline's four stages, GENERATE FIRST — it is the one that failed twice,
# and the total-byte budget is spent in this order, so the most likely cause is
# never the thing the cap drops.
STAGE_LOG_NAMES = ("generate.log", "filter.log", "evaluate.log", "analyze.log")

# How many matched paths to echo back. Enough to see a warm container's history
# of runs and pick the wrong-directory case out of it; not so many that a
# hundred stale runs bury the tail underneath them.
LOG_MATCHED_LIMIT = 40

# The file listing. ``inference/**/*`` ALONE is what made ``tree: []`` the
# entire evidence of two failed runs: a run that dies in `generate` never
# creates that directory's contents, so the listing scoped to it can only ever
# say "nothing", whatever upstream produced elsewhere.
TREE_GLOBS = ("*", "*/*", "inference/**/*", "logs/**/*")

# ``ckpts`` and ``rewards`` are the mounted Volumes: seeded INPUTS, enormous,
# and identical on every run. Listing them tells the operator nothing they did
# not already know and would consume the whole cap doing it.
TREE_EXCLUDE_TOP = ("ckpts", "rewards")

# Which prefixes survive the cap. Sorting alphabetically and then truncating
# puts ``assets/`` and ``configs/`` ahead of ``inference/`` and ``logs/``, so a
# work directory with a few hundred config files would evict exactly the two
# subtrees the listing exists to show. Rank first, sort within rank.
TREE_PRIORITY = ("logs", "inference")

TREE_LIMIT = 400


def should_collect_logs(exit_code: Any, n_complexes: Any) -> bool:
    """Is this run one where the operator would otherwise learn NOTHING?

    Two cases, not one. The obvious one is a non-zero exit. The second is the
    one that actually costs a re-run: the design command exits 0, the harness
    reports ``n_complexes: 0``, every median comes back ``None``, and the phase
    verdict is a polite INCONCLUSIVE that tells the operator to "look for the
    AF2/RF3 refold artifact in the run tree" — a tree that was globbed under
    ``inference/`` and is therefore empty. Both cases are blind, so both collect.

    A run that produced complexes needs no log tail and does not pay for one:
    the return payload and the console stay exactly as they were.

    An exit code or a complex count that is not an integer means we do not know
    the run succeeded, and "not known to have succeeded" collects. Guessing the
    other way costs the diagnostics precisely when the shard is most broken.
    """
    try:
        rc = int(exit_code)
    except (TypeError, ValueError):
        return True
    if rc != 0:
        return True
    try:
        return int(n_complexes) == 0
    except (TypeError, ValueError):
        return True


def newest_path(candidates: Iterable[Sequence[Any]]) -> Any | None:
    """The most recently modified of ``(path, mtime)`` pairs, or None.

    A warm Modal container accumulates one ``design_pipeline_*`` directory per
    run, and ``glob`` returns them in arbitrary order. Reading whichever came
    back first hands the operator a PREVIOUS run's log while presenting it as
    this one's — a diagnostic that is worse than none, because it is confidently
    wrong and there is nothing in it that says so.

    Ties break on the path string, DESCENDING, which is not arbitrary: upstream
    stamps the directory name ``..._Y2026_M08_D05_H00_M58_S46``, so the
    lexicographically greater name is the later run whenever two share an mtime
    second. Pairs whose mtime is not a number are skipped rather than sorted
    against, because comparing a string against a float raises and this runs
    after the money is spent.
    """
    best_key: tuple[float, str] | None = None
    best_path: Any = None
    for pair in candidates:
        try:
            path, mtime = pair[0], pair[1]
            key = (float(mtime), str(path))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if best_key is None or key > best_key:
            best_key, best_path = key, path
    return best_path


def truncate_log_tail(raw: Any, max_bytes: int = LOG_TAIL_BYTES) -> dict:
    """The LAST ``max_bytes`` bytes of ``raw``, decoded so it cannot raise.

    The tail, not the head: a Python traceback and upstream's "what actually
    went wrong" line are the last thing in the file, and a head-truncated log is
    6 KB of CUDA banner.

    Capped in BYTES rather than characters or lines, because the cap exists to
    bound a return payload and a console write, and both are measured in bytes.
    Decoding with ``errors="replace"`` is what makes a cut through the middle of
    a multi-byte character survivable — the slice is taken on bytes, so a cut
    there is not merely possible but likely.

    ``kept_bytes`` is reported so the caller's total budget is spent against
    what was actually kept. It is the pre-decode count on purpose: a replacement
    character is 3 UTF-8 bytes and re-encoding the result would let a stream of
    broken bytes inflate past the budget it was just trimmed to.
    """
    if isinstance(raw, (bytes, bytearray, memoryview)):
        data = bytes(raw)
    else:
        data = str(raw).encode("utf-8", "replace")
    try:
        limit = max(0, int(max_bytes))
    except (TypeError, ValueError):
        limit = LOG_TAIL_BYTES
    truncated = len(data) > limit
    kept = data[len(data) - limit:] if truncated else data
    return {
        "tail": kept.decode("utf-8", "replace"),
        "truncated": truncated,
        "bytes": len(data),
        "kept_bytes": len(kept),
    }


def collect_log_entries(files: Iterable[Sequence[Any]],
                        per_file_bytes: int = LOG_TAIL_BYTES,
                        total_bytes: int = LOG_TOTAL_BYTES) -> list[dict]:
    """One record per ``(path, payload)``, trimmed to fit BOTH caps.

    ``payload`` is the file's bytes, or the exception that stopped us reading
    it. A read that failed is recorded, never dropped: "we looked at
    ``logs/.../generate.log`` and it was not there" is itself the finding when
    upstream changes its layout, and an absent entry is indistinguishable from
    an absent collector.

    The order of ``files`` is the order the budget is spent in, so the caller
    puts the most probable cause first. Once the budget is gone the remaining
    files still get a record — path, real size, and an explicit note — because
    the operator has to be able to tell "this log was empty" from "this log was
    dropped to stay inside the cap", and those need different next actions.
    """
    entries: list[dict] = []
    try:
        remaining = max(0, int(total_bytes))
    except (TypeError, ValueError):
        remaining = LOG_TOTAL_BYTES
    try:
        per_file = max(0, int(per_file_bytes))
    except (TypeError, ValueError):
        per_file = LOG_TAIL_BYTES

    for pair in files:
        try:
            path, payload = pair[0], pair[1]
        except (TypeError, IndexError, KeyError):
            continue
        entry: dict[str, Any] = {"path": str(path)}
        if isinstance(payload, BaseException):
            entry["error"] = f"{type(payload).__name__}: {payload}"
            entries.append(entry)
            continue
        budget = min(per_file, remaining)
        entry.update(truncate_log_tail(payload, budget))
        remaining -= entry["kept_bytes"]
        if entry["truncated"] and budget < per_file:
            entry["budget_exhausted"] = True
        if entry["bytes"] == 0:
            entry["empty"] = True
        entries.append(entry)
    return entries


def build_log_report(*, globs: Iterable[Any], matched: Iterable[Any],
                     selected: Any, files: Iterable[Sequence[Any]],
                     per_file_bytes: int = LOG_TAIL_BYTES,
                     total_bytes: int = LOG_TOTAL_BYTES) -> dict:
    """The whole diagnostics block a shard returns.

    THE PATHS WE LOOKED AT ARE PART OF THE ANSWER. If upstream renames its log
    directory, the next failure has to be diagnosable from this report alone —
    "we globbed X, matched nothing, read nothing" points straight at the
    pattern, whereas an empty ``files`` list with no context reproduces exactly
    the ``tree: []`` non-information this section exists to end.
    """
    matched_list = sorted({str(m) for m in matched})
    entries = collect_log_entries(files, per_file_bytes, total_bytes)
    report: dict[str, Any] = {
        "globs": [str(g) for g in globs],
        "n_matched": len(matched_list),
        "matched": matched_list[:LOG_MATCHED_LIMIT],
        "selected": None if selected is None else str(selected),
        "files": entries,
        "per_file_cap_bytes": per_file_bytes,
        "total_cap_bytes": total_bytes,
        "total_tail_bytes": sum(int(e.get("kept_bytes") or 0) for e in entries),
    }
    if not matched_list:
        report["note"] = (
            "no path matched. Upstream's own failure message names "
            "logs/design_pipeline_<key>_<run>_Y..M..D..H..M..S../generate.log "
            "and the sibling ...log next to it; if it still does, the glob "
            "above is wrong. If it does not, upstream moved its logs and this "
            "collector has to follow.")
    elif not entries:
        report["note"] = (
            "paths matched but no log file was read — the run directory exists "
            "and holds none of the stage logs.")
    return report


def format_log_diagnostics(shard: Any, key: str = "log_diagnostics") -> list[str]:
    """The report as console lines. EMPTY when there is nothing to say.

    A pure renderer rather than a block of prints inside ``main``, for the usual
    reason: ``_hotspot_canary.py`` cannot be imported by the suite, and the
    thing that must be proved is that the collected text REACHES THE OPERATOR.
    Returning lines lets that be asserted directly instead of inferred from the
    presence of a print statement.

    Returns ``[]`` for a shard that carries no diagnostics, so a successful run
    prints exactly what it printed before.
    """
    report = shard.get(key) if isinstance(shard, dict) else None
    if not isinstance(report, dict):
        return []
    label = shard.get("label") or "?"
    lines = [
        "",
        f"--- upstream log tail [{label}] ---",
        "The design command runs `generate` as a sub-subprocess with its output "
        "redirected into a file, so its stderr never reaches this stream. This "
        "is that file.",
    ]
    error = report.get("error")
    if error:
        lines.append(f"  the diagnostics could not be collected: {error}")
        return lines
    for pattern in report.get("globs") or []:
        lines.append(f"  globbed: {pattern}")
    matched = list(report.get("matched") or [])
    n_matched = report.get("n_matched", len(matched))
    lines.append(f"  matched {n_matched} path(s):")
    for path in matched:
        lines.append(f"    {path}")
    if n_matched > len(matched):
        lines.append(f"    ... and {n_matched - len(matched)} more")
    lines.append(f"  newest run directory: {report.get('selected')}")
    note = report.get("note")
    if note:
        lines.append(f"  NOTE: {note}")

    for entry in report.get("files") or []:
        path = entry.get("path")
        if entry.get("error"):
            lines.append(f"  --- {path}: NOT READ ({entry['error']}) ---")
            continue
        size, kept = entry.get("bytes"), entry.get("kept_bytes")
        suffix = f", last {kept} shown" if entry.get("truncated") else ""
        if entry.get("budget_exhausted"):
            suffix += " (the total byte budget ran out here)"
        lines.append(f"  --- {path} ({size} bytes{suffix}) ---")
        tail = str(entry.get("tail") or "")
        if not tail.strip():
            lines.append("      (empty)")
        else:
            lines.extend("      " + line for line in tail.splitlines())
    lines.append("--- end of upstream log tail ---")
    return lines


def select_tree_entries(relative_paths: Iterable[Any],
                        limit: int = TREE_LIMIT,
                        exclude: Sequence[str] = TREE_EXCLUDE_TOP,
                        priority: Sequence[str] = TREE_PRIORITY) -> list[str]:
    """What upstream produced, deduplicated, ranked, capped and separator-normalised.

    Ranked before it is capped. Plain ``sorted(...)[:limit]`` is alphabetical,
    and ``logs`` and ``inference`` — the only two subtrees anyone globs this
    listing to see — sort after ``assets``, ``configs`` and ``hub_targets``, so
    a work directory with a few hundred config files returns a cap's worth of
    files nobody asked about and none of the ones they did.

    Separators are normalised to ``/`` so the listing reads the same wherever it
    is rendered; the container is Linux, but the tests are not, and a listing
    whose shape depends on the host is a listing that gets asserted loosely.
    """
    try:
        cap = max(0, int(limit))
    except (TypeError, ValueError):
        cap = TREE_LIMIT
    blocked = tuple(exclude or ())
    ranks = list(priority or ())
    keep = set()
    for path in relative_paths:
        text = str(path).replace("\\", "/").strip()
        # A leading "./" PREFIX, never ``lstrip("./")`` — that strips any
        # leading dot, and the most interesting entry in the whole listing is
        # ``.hydra/config.yaml``, which it would rename to ``hydra/...``.
        while text.startswith("./"):
            text = text[2:]
        if not text or text == ".":
            continue
        top = text.split("/", 1)[0]
        if top in blocked:
            continue
        keep.add(text)

    def _rank(text: str) -> tuple[int, str]:
        top = text.split("/", 1)[0]
        return (ranks.index(top) if top in ranks else len(ranks), text)

    return sorted(keep, key=_rank)[:cap]
