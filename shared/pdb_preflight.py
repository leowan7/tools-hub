"""Per-tool PDB preflight: runs the GPU-side normalizer in dry-run mode
and decides whether the upload is fit to ship to Modal.

The four binder-design tools (rfantibody, rfdiffusion, bindcraft, boltzgen)
all expect a single-chain antigen target and (for 3 of 4) a list of hotspot
residues that the model must build a CDR around. The vendored
``pipeline_normalize`` already strips waters, hetatm, hydrogens, alt
conformations, NMR ensembles, and modified residues — silently, so by
itself it's invisible to the user. This module surfaces the cleanup as a
user-facing verdict:

  - VerdictKind.READY:            ✓ Ready (plus a list of things we cleaned).
  - VerdictKind.READY_WITH_FALLBACK:
                                  ✓ Ready, but a cleaner AlphaFold target
                                  exists for the same UniProt — offer the swap.
  - VerdictKind.NEEDS_FIX:        ✗ Can't run. Specific reason + suggested
                                  next action, optionally including an
                                  AlphaFold swap.

A hard gate fires only on NEEDS_FIX. tools-hub's ``tool_submit`` releases
the wallet hold and re-renders the form with this verdict above the Run
button. See templates/components/preflight_panel.html for the UI shape.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from shared.pipeline_normalize import (
    PipelineNormalizationReport,
    normalize_for_bindcraft,
    normalize_for_boltzgen,
    normalize_for_rfantibody,
    normalize_for_rfdiffusion,
)
from shared.uniprot_lookup import extract_uniprot_map

logger = logging.getLogger(__name__)


# Tools subject to the hard gate. esmfold2-design is sequence-only — no PDB.
BINDER_DESIGN_TOOLS: frozenset[str] = frozenset({
    "rfantibody",
    "rfdiffusion",
    "bindcraft",
    "boltzgen",
})

# Tools where hotspot residues are required (boltzgen accepts an empty list).
HOTSPOTS_REQUIRED: frozenset[str] = frozenset({
    "rfantibody",
    "rfdiffusion",
    "bindcraft",
})

# Minimum protein residue count on the target chain after cleanup. Below
# this the model has nothing to design against. 30 is the lower bound for
# any meaningful target fold; smaller fragments should go through pxdesign
# or be re-thought as peptide binders.
MIN_TARGET_RESIDUES = 30


class VerdictKind(str, Enum):
    READY = "ready"
    READY_WITH_FALLBACK = "ready_with_fallback"
    NEEDS_FIX = "needs_fix"


@dataclass
class CleanupSummary:
    """What pipeline_normalize did to the upload, in user-readable form.

    ``items`` is the bulleted list rendered under "Cleanup applied:" in
    the preflight panel. Empty when the input was already clean.
    """
    items: list = field(default_factory=list)
    altloc_records_collapsed: int = 0
    residues_dropped: int = 0
    chains_dropped: list = field(default_factory=list)
    residues_kept_on_target_chain: int = 0


@dataclass
class AlphaFoldSuggestion:
    """One-click "use the AlphaFold model instead" offer."""
    uniprot_accession: str
    # We only ship the accession; the actual model fetch happens later
    # in the /tools/<slug>/fetch-alphafold endpoint so that an offer with
    # no follow-through doesn't cost an AlphaFold-DB round trip.

    @property
    def display_id(self) -> str:
        return f"AF-{self.uniprot_accession}"


@dataclass
class PreflightVerdict:
    """The full verdict shown in the preflight panel + used as the gate."""
    kind: VerdictKind
    tool_slug: str
    target_chain: str
    cleanup: CleanupSummary
    hotspot_status: dict           # {"surviving": [...], "dropped": [...]}
    reason: Optional[str] = None
    suggested_fix: Optional[str] = None
    alphafold: Optional[AlphaFoldSuggestion] = None
    nearest_clean_residues: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kind is not VerdictKind.NEEDS_FIX


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_PREVIEW_FN = {
    "rfantibody":   normalize_for_rfantibody,
    "rfdiffusion":  normalize_for_rfdiffusion,
    "bindcraft":    normalize_for_bindcraft,
    "boltzgen":     normalize_for_boltzgen,
}


def preflight_for_tool(
    tool_slug: str,
    pdb_bytes: bytes,
    *,
    target_chain: str,
    hotspots: list,
) -> PreflightVerdict:
    """Top-level entry. Returns a PreflightVerdict for the named binder tool.

    ``pdb_bytes`` is the raw upload (post-CIF-conversion if applicable —
    tools-hub does the CIF→PDB conversion at upload time before calling
    this). ``hotspots`` is the user-typed list of integers; empty list is
    valid for boltzgen, required non-empty for the other three.

    On any unexpected error (parser blow-up, etc.) returns a NEEDS_FIX
    verdict with the underlying message — never raises.
    """
    if tool_slug not in BINDER_DESIGN_TOOLS:
        # Defensive: if a caller asks us to gate a tool not in the list,
        # fall back to a no-op READY. Cheaper than enforcing per-call and
        # keeps the gate composable.
        return PreflightVerdict(
            kind=VerdictKind.READY,
            tool_slug=tool_slug,
            target_chain=target_chain,
            cleanup=CleanupSummary(),
            hotspot_status={"surviving": list(hotspots), "dropped": []},
        )

    preview = _PREVIEW_FN[tool_slug]
    af_suggestion = _maybe_alphafold(pdb_bytes, target_chain)
    target_chain = (target_chain or "").strip()

    # Write to a tmp file so pipeline_normalize.normalize_for_*'s
    # BiopythonParser can stream it. Cleanup is best-effort; on Windows
    # we tolerate a tmp leak in the rare case the parser kept the fd.
    tmp_in = tempfile.NamedTemporaryFile(
        prefix="preflight_", suffix=".pdb", delete=False,
    )
    try:
        tmp_in.write(pdb_bytes)
        tmp_in.close()
        try:
            report = preview(tmp_in.name, None, target_chain=target_chain)
        except ValueError as exc:
            # Normalizer raised because target chain absent / no protein.
            # Map to NEEDS_FIX with the underlying message.
            return _verdict_from_normalizer_value_error(
                tool_slug, target_chain, exc, pdb_bytes, af_suggestion,
            )
        except Exception as exc:
            logger.exception("preflight unexpected error tool=%s", tool_slug)
            return PreflightVerdict(
                kind=VerdictKind.NEEDS_FIX,
                tool_slug=tool_slug,
                target_chain=target_chain,
                cleanup=CleanupSummary(),
                hotspot_status={"surviving": [], "dropped": list(hotspots)},
                reason=(
                    f"Couldn't pre-flight your upload "
                    f"({type(exc).__name__}). The file may be malformed."
                ),
                suggested_fix=(
                    "Open the PDB in PyMOL or ChimeraX, save a clean copy, "
                    "and re-upload."
                ),
                alphafold=af_suggestion,
            )
    finally:
        try:
            os.unlink(tmp_in.name)
        except OSError:
            pass

    cleanup = _summarize_cleanup(report)

    # Now check tool-specific requirements against the cleaned-up structure.
    kept = report.residues_kept_per_chain.get(target_chain, 0)
    if kept < MIN_TARGET_RESIDUES:
        return PreflightVerdict(
            kind=VerdictKind.NEEDS_FIX,
            tool_slug=tool_slug,
            target_chain=target_chain,
            cleanup=cleanup,
            hotspot_status={"surviving": [], "dropped": list(hotspots)},
            reason=(
                f"After cleanup, chain {target_chain} has only {kept} "
                f"protein residue(s) — the model needs at least "
                f"{MIN_TARGET_RESIDUES} to design against."
            ),
            suggested_fix=(
                f"Confirm chain {target_chain} is the antigen, not a peptide "
                f"or ligand fragment. If your target really is small, the "
                f"peptide / mini-binder presets on other tools may fit better."
            ),
            alphafold=af_suggestion,
        )

    # Hotspot validation. We need the per-residue resnum survival map; the
    # current normalizer report exposes counts per chain, not residue ids,
    # so re-derive survival from the cleaned PDB. A re-run with output to
    # /tmp would expose this directly; cheaper to walk the residue ids out
    # of a second dry-run inside our own residue walker.
    surviving, dropped_hotspots = _check_hotspots(
        pdb_bytes, target_chain, hotspots,
    )

    hotspot_required = tool_slug in HOTSPOTS_REQUIRED
    if hotspot_required and not hotspots:
        return PreflightVerdict(
            kind=VerdictKind.NEEDS_FIX,
            tool_slug=tool_slug,
            target_chain=target_chain,
            cleanup=cleanup,
            hotspot_status={"surviving": [], "dropped": []},
            reason="This tool needs at least one hotspot residue.",
            suggested_fix=(
                "Pick 1-5 residues on the epitope face you want the binder "
                "to contact, then type them comma-separated in the Hotspots "
                "field."
            ),
            alphafold=af_suggestion,
        )

    if dropped_hotspots and hotspot_required:
        # One or more user-picked hotspots didn't survive cleanup. Suggest
        # nearby clean residues on the same chain.
        nearest = _nearest_clean_residues(
            pdb_bytes, target_chain, dropped_hotspots, surviving,
        )
        dropped_str = ", ".join(str(h) for h in dropped_hotspots)
        nearest_str = (
            ", ".join(str(r) for r in nearest)
            if nearest
            else "(no clean neighbours found within ±10 of the dropped residues)"
        )
        return PreflightVerdict(
            kind=VerdictKind.NEEDS_FIX,
            tool_slug=tool_slug,
            target_chain=target_chain,
            cleanup=cleanup,
            hotspot_status={
                "surviving": surviving,
                "dropped": dropped_hotspots,
            },
            reason=(
                f"Hotspot residue(s) {dropped_str} were dropped during "
                f"cleanup because their backbone is incomplete in this "
                f"PDB. The model needs a complete N/CA/C/O backbone at "
                f"every hotspot."
            ),
            suggested_fix=(
                f"Pick a hotspot with a clean backbone. Nearest clean "
                f"residues on chain {target_chain}: {nearest_str}. "
                f"Or use the AlphaFold model below — it's single-conformation "
                f"and won't have this gap."
            ),
            alphafold=af_suggestion,
            nearest_clean_residues=nearest,
        )

    # All checks passed. Decide whether to surface the AlphaFold fallback
    # as a soft "you might prefer this" suggestion. We do this only when
    # the cleanup actually had to fix something material on a crystal
    # structure (altloc collapsed, or > 5 residues dropped for bad
    # backbones). Pristine crystal targets / AF inputs don't get the
    # suggestion — it'd be noise.
    surfaced_af = (
        af_suggestion
        and (cleanup.altloc_records_collapsed > 0
             or cleanup.residues_dropped > 5)
    )
    kind = (
        VerdictKind.READY_WITH_FALLBACK
        if surfaced_af else VerdictKind.READY
    )
    return PreflightVerdict(
        kind=kind,
        tool_slug=tool_slug,
        target_chain=target_chain,
        cleanup=cleanup,
        hotspot_status={"surviving": surviving, "dropped": dropped_hotspots},
        alphafold=af_suggestion if surfaced_af else None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _maybe_alphafold(pdb_bytes: bytes, target_chain: str) -> Optional[AlphaFoldSuggestion]:
    """Return an AlphaFold suggestion if we can map the target chain to a UniProt."""
    try:
        m = extract_uniprot_map(pdb_bytes)
    except Exception:  # noqa: BLE001 - defensive
        return None
    rec = m.get(target_chain) if target_chain else None
    if not rec:
        # Fall back to the first mapped chain. The user-typed target_chain
        # may not match the DBREF chain if they typo'd, but we still want
        # the suggestion to surface so the panel can offer the swap.
        if not m:
            return None
        rec = next(iter(m.values()))
    return AlphaFoldSuggestion(uniprot_accession=rec.uniprot_accession)


def _summarize_cleanup(report: PipelineNormalizationReport) -> CleanupSummary:
    """Project the normalizer's report into the user-facing bullet list."""
    items: list = []
    if report.altloc_records_collapsed:
        n = report.altloc_records_collapsed
        items.append(
            f"{n} alternate conformation"
            f"{'s' if n != 1 else ''} collapsed"
        )
    total_dropped = sum(report.residues_dropped_per_chain.values())
    if total_dropped:
        items.append(
            f"{total_dropped} residue"
            f"{'s' if total_dropped != 1 else ''} dropped "
            f"(waters / HETATM / incomplete backbone)"
        )
    if report.chains_dropped:
        items.append(
            f"Other chain(s) dropped: {', '.join(report.chains_dropped)}"
        )
    # Inspect changes for the MSE-remap message (the normalizer adds it
    # only when the remap fires).
    for c in report.changes:
        if "modified residue" in c:
            items.append(c)
    return CleanupSummary(
        items=items,
        altloc_records_collapsed=report.altloc_records_collapsed,
        residues_dropped=total_dropped,
        chains_dropped=list(report.chains_dropped),
        residues_kept_on_target_chain=sum(report.residues_kept_per_chain.values()),
    )


def _check_hotspots(
    pdb_bytes: bytes, target_chain: str, hotspots: list,
) -> tuple[list, list]:
    """Return (surviving_hotspots, dropped_hotspots) after cleanup.

    Walks the raw bytes with the same backbone-completeness rules
    pipeline_normalize applies, so we don't need a second normalizer
    invocation. Quick and avoids a second tmp file.
    """
    if not hotspots:
        return [], []
    # Build per-residue backbone presence on the target chain. We track
    # by (resnum, icode); icode is rarely used in user uploads.
    bb_present: dict = {}  # resnum -> set of backbone atoms seen
    required = {"N", "CA", "C", "O"}
    for raw in pdb_bytes.split(b"\n"):
        if not (raw.startswith(b"ATOM") or raw.startswith(b"HETATM")):
            continue
        try:
            line = raw.decode("ascii", errors="replace")
        except Exception:
            continue
        if len(line) < 54:
            continue
        if (line[21] if len(line) > 21 else " ") != target_chain:
            continue
        atom_name = line[12:16].strip()
        if atom_name not in required:
            continue
        try:
            resnum = int(line[22:26])
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
        except ValueError:
            continue
        if all(abs(c) < 1e-6 for c in (x, y, z)):
            continue
        bb_present.setdefault(resnum, set()).add(atom_name)

    surviving: list = []
    dropped: list = []
    for h in hotspots:
        try:
            n = int(h)
        except (TypeError, ValueError):
            dropped.append(h)
            continue
        if required.issubset(bb_present.get(n, set())):
            surviving.append(n)
        else:
            dropped.append(n)
    return surviving, dropped


def _nearest_clean_residues(
    pdb_bytes: bytes,
    target_chain: str,
    dropped_hotspots: list,
    surviving_resnums: list,
    *,
    window: int = 10,
    max_suggestions: int = 6,
) -> list:
    """For each dropped hotspot, return up to ``max_suggestions`` resnums
    on the same chain within ±``window`` of any dropped hotspot, ordered
    by sequence distance ascending then resnum ascending.
    """
    # Use the same backbone walk to find ALL clean residues on the chain.
    clean_set: set = set()
    required = {"N", "CA", "C", "O"}
    bb: dict = {}
    for raw in pdb_bytes.split(b"\n"):
        if not raw.startswith(b"ATOM"):
            continue
        try:
            line = raw.decode("ascii", errors="replace")
        except Exception:
            continue
        if len(line) < 54:
            continue
        if (line[21] if len(line) > 21 else " ") != target_chain:
            continue
        atom_name = line[12:16].strip()
        if atom_name not in required:
            continue
        try:
            resnum = int(line[22:26])
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
        except ValueError:
            continue
        if all(abs(c) < 1e-6 for c in (x, y, z)):
            continue
        bb.setdefault(resnum, set()).add(atom_name)
    clean_set = {r for r, atoms in bb.items() if required.issubset(atoms)}
    if not clean_set:
        return []

    dropped_ints: list = []
    for h in dropped_hotspots:
        try:
            dropped_ints.append(int(h))
        except (TypeError, ValueError):
            continue
    if not dropped_ints:
        return []

    candidates: dict = {}  # resnum -> min sequence distance to any dropped hotspot
    for r in clean_set:
        if r in dropped_ints:
            continue
        for d in dropped_ints:
            if abs(r - d) <= window:
                prev = candidates.get(r)
                dist = abs(r - d)
                if prev is None or dist < prev:
                    candidates[r] = dist
                break
    ranked = sorted(candidates.items(), key=lambda kv: (kv[1], kv[0]))
    return [r for r, _ in ranked[:max_suggestions]]


def _verdict_from_normalizer_value_error(
    tool_slug: str, target_chain: str, exc: ValueError,
    pdb_bytes: bytes, af_suggestion: Optional[AlphaFoldSuggestion],
) -> PreflightVerdict:
    """Translate a normalize_for_*'s ValueError into a user-facing NEEDS_FIX."""
    msg = str(exc)
    # Two known cases. Match on substring so we don't re-implement raising.
    if "Target chain" in msg and "not present" in msg:
        # Try to extract the actual chain list from the message tail.
        present: list = []
        try:
            # The message ends "Found chains: ['A', 'B']" — parse loosely.
            tail = msg.split("Found chains:", 1)[1]
            for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
                if f"'{ch}'" in tail:
                    present.append(ch)
        except IndexError:
            pass
        if not present:
            # Fall back: walk the raw bytes for chain ids.
            present = sorted({
                chr(raw[21]) for raw in pdb_bytes.split(b"\n")
                if (raw.startswith(b"ATOM") or raw.startswith(b"HETATM"))
                and len(raw) > 21
            })
        if len(present) == 1:
            fix = f"Did you mean chain {present[0]}? Click below to use it."
        elif present:
            fix = (
                f"This PDB has chain(s) {', '.join(present)} — pick one of "
                f"those in the Target chain field."
            )
        else:
            fix = (
                "Inspect your PDB in PyMOL or ChimeraX to identify the "
                "antigen chain, then type that chain ID in the Target chain field."
            )
        return PreflightVerdict(
            kind=VerdictKind.NEEDS_FIX,
            tool_slug=tool_slug,
            target_chain=target_chain,
            cleanup=CleanupSummary(),
            hotspot_status={"surviving": [], "dropped": []},
            reason=(
                f"Target chain {target_chain!r} isn't in this PDB. "
                f"Found chain(s): {', '.join(present) if present else '(none)'}."
            ),
            suggested_fix=fix,
            alphafold=af_suggestion,
        )
    if "no standard polymer residues" in msg:
        return PreflightVerdict(
            kind=VerdictKind.NEEDS_FIX,
            tool_slug=tool_slug,
            target_chain=target_chain,
            cleanup=CleanupSummary(),
            hotspot_status={"surviving": [], "dropped": []},
            reason=(
                "This PDB has no standard protein residues after cleanup — "
                "the file may be all ligands, waters, or non-standard residues."
            ),
            suggested_fix=(
                "Confirm the upload is a protein structure, not a small "
                "molecule or carbohydrate-only file."
            ),
            alphafold=af_suggestion,
        )
    # Unknown ValueError shape — surface its message verbatim.
    return PreflightVerdict(
        kind=VerdictKind.NEEDS_FIX,
        tool_slug=tool_slug,
        target_chain=target_chain,
        cleanup=CleanupSummary(),
        hotspot_status={"surviving": [], "dropped": []},
        reason=msg,
        suggested_fix=(
            "Open the PDB in PyMOL or ChimeraX, save a clean copy with the "
            "antigen chain only, and re-upload."
        ),
        alphafold=af_suggestion,
    )
