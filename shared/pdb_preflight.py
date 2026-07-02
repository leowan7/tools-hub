"""Per-tool PDB preflight: runs the GPU-side normalizer in dry-run mode
and decides whether the upload is fit to ship to Modal.

The four binder-design tools (rfantibody, rfdiffusion, bindcraft, boltzgen)
all expect an antigen target plus (for 3 of 4) hotspot residues for the
model to build a CDR around. The vendored ``pipeline_normalize`` already
strips waters, hetatm, hydrogens, alt conformations, NMR ensembles, and
modified residues — silently, so by itself it's invisible to the user.
This module surfaces the cleanup as a user-facing verdict, plus runs
additional structural checks not done by the normalizer:

  - VerdictKind.READY:            ✓ Ready to run.
  - VerdictKind.READY_WITH_FALLBACK:
                                  ✓ Ready, but a cleaner AlphaFold target
                                  exists for the same UniProt — or we
                                  noticed a softness (size warn, internal
                                  gap) the user might want to know about.
  - VerdictKind.NEEDS_FIX:        ✗ Can't run. Specific reason + suggested
                                  next action, optionally including an
                                  AlphaFold swap.

Per-tool rules (size caps, gap thresholds, hotspot policy) live in
``pdb_preflight_rules.TOOL_RULES`` — one dataclass per tool. The
evaluator reads from there rather than hardcoding magic numbers.

A hard gate fires only on NEEDS_FIX. tools-hub's ``tool_submit`` releases
the wallet hold and re-renders the form with this verdict above the Run
button. See templates/components/preflight_panel.html for the UI shape.
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from shared.pipeline_normalize import (
    PipelineNormalizationReport,
    normalize_for_bindcraft,
    normalize_for_boltzgen,
    normalize_for_pxdesign,
    normalize_for_rfantibody,
    normalize_for_rfdiffusion,
)
from shared.pdb_preflight_rules import (
    BINDER_DESIGN_TOOLS,
    HOTSPOTS_REQUIRED,
    TOOL_RULES,
    ToolRules,
    runtime_estimate_min,
)
from shared.uniprot_lookup import extract_uniprot_map

logger = logging.getLogger(__name__)


# Back-compat re-export. Old call sites + tests import MIN_TARGET_RESIDUES
# from this module; it's now derived from TOOL_RULES (lowest min across
# binder tools). Per-tool checks use rules.min_target_aa directly.
MIN_TARGET_RESIDUES: int = min(r.min_target_aa for r in TOOL_RULES.values())


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
class GapInfo:
    """One contiguous run of missing residues inside the target chain.

    ``start`` and ``end`` are inclusive endpoints of the MISSING range in
    original PDB numbering (e.g. surviving 49, 50, 60, 61 → start=51,
    end=59, length=9). ``nearest_hotspot_distance`` is the minimum
    sequence distance from any gap endpoint to any user-picked hotspot
    residue, or ``math.inf`` when no hotspots are picked.
    """
    start: int
    end: int
    length: int
    nearest_hotspot_distance: float


@dataclass
class GapAnalysis:
    """Summary of internal-gap detection on the target chain."""
    gaps: list = field(default_factory=list)        # list[GapInfo]
    longest_gap: int = 0
    causes_hard_fail: bool = False                  # True ↔ verdict went NEEDS_FIX
    warn_message: Optional[str] = None              # human prose for the panel
    hard_fail_message: Optional[str] = None         # human prose for NEEDS_FIX


@dataclass
class SizeEnvelopeStatus:
    """Per-tool size check result for the target chain (+ combined budget).

    ``residue_count`` is the post-cleanup residue count on the target
    chain. ``runtime_estimate_min`` is None when the caller didn't pass
    ``num_designs`` (so we don't surface a misleading estimate).
    """
    residue_count: int
    hard_cap_target_aa: int
    soft_warn_target_aa: int
    hard_cap_combined_aa: int
    binder_max_aa: Optional[int] = None
    combined_aa: Optional[int] = None
    over_soft_warn: bool = False
    over_hard_cap: bool = False
    over_combined_cap: bool = False
    runtime_estimate_min: Optional[float] = None
    runtime_basis: Optional[str] = None             # e.g. "100 designs"
    # NOTE: runtime_hard_cap_min + over_runtime_cap were retired by the
    # tier-collapse PR. Wall-clock is no longer a preflight block; long
    # campaigns are a legitimate user choice. The estimate above stays
    # as advisory copy in the panel.
    gpu: Optional[str] = None
    warn_message: Optional[str] = None              # human prose for the panel
    hard_fail_message: Optional[str] = None         # human prose for NEEDS_FIX


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
    gap_analysis: Optional[GapAnalysis] = None
    size_envelope: Optional[SizeEnvelopeStatus] = None

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
    "pxdesign":     normalize_for_pxdesign,
}


# Tools that get a server-side hard-gate preflight at submit. The binder
# design tools (incl. pxdesign) run the normalizer dry-run + structural
# gate via preflight_for_tool. boltz2 is included too but takes a
# dedicated evaluator (sequence-position hotspots, single antigen chain),
# so it is NOT a member of BINDER_DESIGN_TOOLS.
PREFLIGHT_TOOLS: frozenset[str] = BINDER_DESIGN_TOOLS | frozenset({"boltz2"})


# ---------------------------------------------------------------------------
# Boltz-2 structural preflight
#
# Boltz-2 cofolds an antigen chain + binder sequences on A100-40GB. Its
# semantics differ from the binder-design tools in three ways that make
# the shared gate wrong for it:
#   - hotspots are 1-indexed SEQUENCE positions into the named antigen
#     chain (run_pipeline.hotspot_contacts does ag_res[p-1]), not original
#     PDB author numbering;
#   - hotspots are optional;
#   - only the named antigen chain is folded (run_pipeline.chain_seq reads
#     a single chain), so there is no contig builder / gap assertion.
# So boltz2 runs its own checks below rather than preflight_for_tool's.
# ---------------------------------------------------------------------------

# Boltz-2 cofold size envelope, applied to the TOTAL complex (antigen +
# the longest binder), since pair-representation memory scales with the
# whole token count, not the antigen alone. Anchors: standard open-source
# Boltz handles ~1600 tokens on 24GB VRAM (LMI4Boltz, bioRxiv 2025); on
# our A100-40GB that headroom covers ~1800 residues with margin. AF3-class
# models reach ~4352 tokens on 40GB only with host-memory spill, which the
# Modal boltz CLI does not enable, so 1800 stays conservative. The biggest
# realistic complex is well under this: the largest binder-design targets
# in the literature run ~600 to 700 aa (RFdiffusion/BindCraft: transferrin
# receptor, HER2 ECD, hemagglutinin) plus a binder up to 400 aa.
BOLTZ2_COMPLEX_HARD_CAP_AA = 1800
BOLTZ2_GPU = "A100-40GB"


def _ca_residue_counts(pdb_bytes: bytes) -> dict:
    """Per-chain count of residues bearing a CA atom (ATOM records only).

    Mirrors run_pipeline.chain_seq exactly (ATOM ... CA lines, unique
    resnum per chain), so each count equals the antigen length boltz2
    folds and indexes its 1-based hotspot positions into.
    """
    seen: dict = {}
    for raw in pdb_bytes.split(b"\n"):
        if not raw.startswith(b"ATOM"):
            continue
        if len(raw) < 26:
            continue
        try:
            line = raw.decode("ascii", errors="replace")
        except Exception:  # noqa: BLE001 - defensive
            continue
        if line[12:16].strip() != "CA":
            continue
        chain = line[21]
        try:
            resnum = int(line[22:26])
        except ValueError:
            continue
        seen.setdefault(chain, set()).add(resnum)
    return {c: len(s) for c, s in seen.items()}


def _preflight_boltz2(
    pdb_bytes: bytes, *, target_chain: str, hotspots: list,
    binder_max_aa: Optional[int] = None,
) -> PreflightVerdict:
    """Structural preflight for the Boltz-2 cofold tool. Never raises.

    ``binder_max_aa`` is the longest binder sequence to be folded against
    the antigen; the size cap is on the combined complex. When it is None
    (e.g. an AJAX preflight fired before binders are entered) the cap falls
    back to the antigen alone.
    """
    antigen_chain = (target_chain or "A").strip() or "A"
    counts = _ca_residue_counts(pdb_bytes)

    def _verdict(kind: VerdictKind, **kw) -> PreflightVerdict:
        base = dict(
            tool_slug="boltz2",
            target_chain=antigen_chain,
            cleanup=CleanupSummary(),
            hotspot_status={"surviving": [], "dropped": []},
        )
        base.update(kw)
        return PreflightVerdict(kind=kind, **base)

    n_antigen = counts.get(antigen_chain, 0)
    if n_antigen == 0:
        present = sorted(counts.keys())
        if present:
            fix = (
                f"This PDB has protein chain(s) {', '.join(present)}. "
                f"Type one of those as the antigen chain."
            )
        else:
            fix = (
                "Confirm the upload is a protein structure (ATOM records "
                "with CA atoms), not a ligand-only file."
            )
        return _verdict(
            VerdictKind.NEEDS_FIX,
            reason=(
                f"Antigen chain {antigen_chain!r} has no protein residues "
                f"in this PDB."
            ),
            suggested_fix=fix,
        )

    binder_aa = binder_max_aa if (binder_max_aa and binder_max_aa > 0) else 0
    total_aa = n_antigen + binder_aa
    if total_aa > BOLTZ2_COMPLEX_HARD_CAP_AA:
        if binder_aa:
            reason = (
                f"The antigen (chain {antigen_chain}, {n_antigen} residues) "
                f"plus the largest binder ({binder_aa} residues) is "
                f"{total_aa} residues, above the {BOLTZ2_COMPLEX_HARD_CAP_AA}"
                f"-residue Boltz-2 cofold envelope on {BOLTZ2_GPU}. The "
                f"complex would likely run out of GPU memory."
            )
        else:
            reason = (
                f"Antigen chain {antigen_chain} has {n_antigen} residues, "
                f"above the {BOLTZ2_COMPLEX_HARD_CAP_AA}-residue Boltz-2 "
                f"cofold envelope on {BOLTZ2_GPU}. The complex would likely "
                f"run out of GPU memory."
            )
        return _verdict(
            VerdictKind.NEEDS_FIX,
            reason=reason,
            suggested_fix=(
                f"Trim chain {antigen_chain} to the epitope domain you want "
                f"to fold against, or fold a smaller antigen."
            ),
        )

    # Multi-chain antigen. run_pipeline.chain_seq folds ONLY the named
    # chain, so any other protein chain in the upload is silently dropped.
    # Flag it upfront (the boltz2 form has no preflight panel and a
    # successful submit redirects, so a hard block is the only point we
    # can surface this before the run). Tiny incidental chains (< 2 res)
    # are ignored so a stray crystallographic fragment does not block.
    other_chains = sorted(
        c for c, n in counts.items() if c != antigen_chain and n >= 2
    )
    if other_chains:
        listed = sorted({antigen_chain, *other_chains})
        return _verdict(
            VerdictKind.NEEDS_FIX,
            reason=(
                f"This antigen PDB has protein chains {', '.join(listed)}. "
                f"Boltz-2 folds only the single antigen chain you name "
                f"(chain {antigen_chain}); chain(s) "
                f"{', '.join(other_chains)} would be dropped."
            ),
            suggested_fix=(
                f"Upload a PDB containing just chain {antigen_chain}, or set "
                f"the antigen chain to the one you want. Binder sequences go "
                f"in the Binder sequences field, not the antigen PDB."
            ),
        )

    surviving: list = []
    out_of_range: list = []
    for h in hotspots or []:
        try:
            n = int(h)
        except (TypeError, ValueError):
            out_of_range.append(h)
            continue
        if 1 <= n <= n_antigen:
            surviving.append(n)
        else:
            out_of_range.append(n)
    if out_of_range:
        return _verdict(
            VerdictKind.NEEDS_FIX,
            hotspot_status={"surviving": surviving, "dropped": out_of_range},
            reason=(
                f"Hotspot position(s) {out_of_range} are outside antigen "
                f"chain {antigen_chain}, which has {n_antigen} residues."
            ),
            suggested_fix=(
                f"Boltz-2 hotspots are sequence positions counted from 1, "
                f"so use values between 1 and {n_antigen}."
            ),
        )

    return _verdict(
        VerdictKind.READY,
        hotspot_status={"surviving": surviving, "dropped": []},
    )


def preflight_for_tool(
    tool_slug: str,
    pdb_bytes: bytes,
    *,
    target_chain: str,
    hotspots: list,
    binder_max_aa: Optional[int] = None,
    num_designs: Optional[int] = None,
) -> PreflightVerdict:
    """Top-level entry. Returns a PreflightVerdict for the named binder tool.

    ``pdb_bytes`` is the raw upload (post-CIF-conversion if applicable —
    tools-hub does the CIF→PDB conversion at upload time before calling
    this). ``hotspots`` is the user-typed list of integers; empty list is
    valid for boltzgen, required non-empty for the other three.

    ``binder_max_aa`` is the maximum binder length the user picked on the
    form; when provided, the combined-budget cap fires if
    (target_aa + binder_max_aa) exceeds the tool's combined ceiling.
    ``num_designs`` is the requested design count; when provided, the
    panel surfaces a runtime estimate. Both default to None so existing
    callers keep working without payload-shape changes.

    On any unexpected error (parser blow-up, etc.) returns a NEEDS_FIX
    verdict with the underlying message — never raises.
    """
    if tool_slug == "boltz2":
        # Dedicated evaluator: sequence-position hotspots, optional
        # hotspots, single antigen chain. binder_max_aa caps the combined
        # complex; num_designs does not apply.
        return _preflight_boltz2(
            pdb_bytes, target_chain=target_chain, hotspots=hotspots,
            binder_max_aa=binder_max_aa,
        )
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

    rules = TOOL_RULES[tool_slug]
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
    kept = report.residues_kept_per_chain.get(target_chain, 0)

    # ---- min residues (per-tool floor) -------------------------------------
    if kept < rules.min_target_aa:
        return PreflightVerdict(
            kind=VerdictKind.NEEDS_FIX,
            tool_slug=tool_slug,
            target_chain=target_chain,
            cleanup=cleanup,
            hotspot_status={"surviving": [], "dropped": list(hotspots)},
            reason=(
                f"After cleanup, chain {target_chain} has only {kept} "
                f"protein residue(s) — the model needs at least "
                f"{rules.min_target_aa} to design against."
            ),
            suggested_fix=(
                f"Confirm chain {target_chain} is the antigen, not a peptide "
                f"or ligand fragment. If your target really is small, the "
                f"peptide / mini-binder presets on other tools may fit better."
            ),
            alphafold=af_suggestion,
        )

    # ---- size envelope (hard cap + combined budget + runtime estimate) -----
    size_envelope = _check_size_envelope(
        rules, kept, binder_max_aa=binder_max_aa, num_designs=num_designs,
    )
    if size_envelope.over_hard_cap or size_envelope.over_combined_cap:
        return PreflightVerdict(
            kind=VerdictKind.NEEDS_FIX,
            tool_slug=tool_slug,
            target_chain=target_chain,
            cleanup=cleanup,
            hotspot_status={"surviving": [], "dropped": list(hotspots)},
            reason=size_envelope.hard_fail_message,
            suggested_fix=(
                "Try the AlphaFold model trimmed to the epitope domain, "
                "or split your target into a sub-domain that fits."
            ),
            alphafold=af_suggestion,
            size_envelope=size_envelope,
        )

    # ---- internal gap analysis --------------------------------------------
    gap_analysis = _check_internal_gaps(
        pdb_bytes, target_chain, hotspots, rules,
    )
    if gap_analysis.causes_hard_fail:
        return PreflightVerdict(
            kind=VerdictKind.NEEDS_FIX,
            tool_slug=tool_slug,
            target_chain=target_chain,
            cleanup=cleanup,
            hotspot_status={"surviving": [], "dropped": list(hotspots)},
            reason=gap_analysis.hard_fail_message,
            suggested_fix=(
                f"Use the AlphaFold model below — it's a single-conformation "
                f"structure with no missing residues."
                if af_suggestion
                else (
                    "Find an alternative PDB entry without internal disorder, "
                    "or use the same UniProt's AlphaFold model."
                )
            ),
            alphafold=af_suggestion,
            gap_analysis=gap_analysis,
            size_envelope=size_envelope,
        )

    # ---- hotspot validation ------------------------------------------------
    # Walks raw bytes with the same backbone-completeness rules
    # pipeline_normalize applies, so we don't need a second normalizer
    # invocation. Quick and avoids a second tmp file.
    surviving, dropped_hotspots = _check_hotspots(
        pdb_bytes, target_chain, hotspots,
    )

    if rules.hotspots_required and not hotspots:
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
            gap_analysis=gap_analysis,
            size_envelope=size_envelope,
        )

    if dropped_hotspots and rules.hotspots_required:
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
            gap_analysis=gap_analysis,
            size_envelope=size_envelope,
        )

    # ---- decide READY vs READY_WITH_FALLBACK ------------------------------
    # All hard checks passed. Surface AF as a soft suggestion when cleanup
    # actually had to fix something material (altloc collapsed, >5 residues
    # dropped, OR a non-hard-fail gap was detected, OR target is over the
    # soft warn threshold). Pristine inputs get plain READY.
    surfaced_af = bool(
        af_suggestion
        and (
            cleanup.altloc_records_collapsed > 0
            or cleanup.residues_dropped > 5
            or (gap_analysis.warn_message is not None)
            or size_envelope.over_soft_warn
        )
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
        gap_analysis=gap_analysis,
        size_envelope=size_envelope,
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


def _clean_resnums_on_chain(pdb_bytes: bytes, target_chain: str) -> list[int]:
    """Sorted list of resnums on ``target_chain`` with a complete N/CA/C/O
    backbone and no all-zero coordinates.

    Used by both ``_check_hotspots`` and ``_check_internal_gaps`` so the
    notion of "surviving residue" stays consistent across checks. Integer
    resnum only — insertion codes (icodes, e.g. antibody 100A/100B/100C)
    are folded into the same resnum, which is correct for gap detection
    (icodes are insertions WITHIN a resnum, not numbering gaps).
    """
    required = {"N", "CA", "C", "O"}
    bb_present: dict = {}  # resnum -> set of backbone atoms seen
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
    return sorted(r for r, atoms in bb_present.items() if required.issubset(atoms))


def _check_hotspots(
    pdb_bytes: bytes, target_chain: str, hotspots: list,
) -> tuple[list, list]:
    """Return ``(surviving_hotspots, dropped_hotspots)`` after cleanup."""
    if not hotspots:
        return [], []
    clean = set(_clean_resnums_on_chain(pdb_bytes, target_chain))
    surviving: list = []
    dropped: list = []
    for h in hotspots:
        try:
            n = int(h)
        except (TypeError, ValueError):
            dropped.append(h)
            continue
        if n in clean:
            surviving.append(n)
        else:
            dropped.append(n)
    return surviving, dropped


def _check_internal_gaps(
    pdb_bytes: bytes,
    target_chain: str,
    hotspots: list,
    rules: ToolRules,
) -> GapAnalysis:
    """Find internal (not end-of-chain) numbering gaps and apply tool rules.

    End-of-chain truncation (residues before the first surviving resnum or
    after the last) is NOT counted as an internal gap — crystal structures
    routinely lack disordered N/C-terminal tails and that's not what we
    want to warn about. Only gaps BETWEEN two surviving resnums count.
    """
    clean = _clean_resnums_on_chain(pdb_bytes, target_chain)
    if len(clean) < 2:
        return GapAnalysis()

    # Normalize hotspots to integers for distance math; non-integer entries
    # are silently skipped (handled elsewhere by hotspot validator).
    hs_ints: list = []
    for h in hotspots or []:
        try:
            hs_ints.append(int(h))
        except (TypeError, ValueError):
            continue

    gaps: list = []
    for prev, curr in zip(clean, clean[1:]):
        if curr <= prev + 1:
            continue
        start = prev + 1
        end = curr - 1
        length = end - start + 1
        if hs_ints:
            dist = min(
                min(abs(h - start), abs(h - end)) for h in hs_ints
            )
            nearest = float(dist)
        else:
            nearest = math.inf
        gaps.append(GapInfo(start, end, length, nearest))

    if not gaps:
        return GapAnalysis()

    longest = max(g.length for g in gaps)

    # Hard fail decision.
    hard_fail = False
    hard_msg: Optional[str] = None
    if rules.gap.needs_fix_on_any_gap:
        # rfdiffusion-style: any internal gap is a hard fail because the
        # contig builder asserts every residue in the declared range exists.
        g0 = gaps[0]
        if len(gaps) == 1:
            ranges = f"residues {g0.start}-{g0.end} unresolved"
        else:
            extra = len(gaps) - 1
            ranges = (
                f"residues {g0.start}-{g0.end} unresolved "
                f"(plus {extra} more internal gap{'s' if extra != 1 else ''})"
            )
        hard_msg = (
            f"Chain {target_chain} has internal disorder ({ranges}). "
            f"{rules.slug.title()}'s contig builder requires every residue "
            f"in the declared range to exist — it will fail with an assertion "
            f"error mid-run."
        )
        hard_fail = True
    elif rules.gap.needs_fix_length is not None and hs_ints:
        # Length + near-hotspot rule. Find any gap that triggers BOTH
        # conditions.
        for g in gaps:
            if (
                g.length >= rules.gap.needs_fix_length
                and g.nearest_hotspot_distance <= rules.gap.needs_fix_hotspot_distance
            ):
                hard_msg = (
                    f"Chain {target_chain} has a {g.length}-residue gap "
                    f"(residues {g.start}-{g.end} unresolved) "
                    f"{_dist_phrase(g.nearest_hotspot_distance)} a hotspot. "
                    f"{rules.slug.title()} is known to fail near gaps like "
                    f"this — typically a degenerate rotation frame mid-run."
                )
                hard_fail = True
                break

    # Warn decision (separate path; can co-exist with hard_fail, but the
    # NEEDS_FIX path takes precedence in the dispatch).
    warn_msg: Optional[str] = None
    if longest >= rules.gap.warn_length and not hard_fail:
        # Find the worst gap (longest). Mention only the longest in
        # the message to keep the panel readable; the full list is in
        # gap_analysis.gaps for callers that want it.
        worst = max(gaps, key=lambda g: g.length)
        nearest_phrase = ""
        if hs_ints and worst.nearest_hotspot_distance != math.inf:
            nearest_phrase = (
                f" — {int(worst.nearest_hotspot_distance)} residues from "
                f"your nearest hotspot"
            )
        warn_msg = (
            f"Chain {target_chain} has a {worst.length}-residue internal "
            f"gap (residues {worst.start}-{worst.end} unresolved)"
            f"{nearest_phrase}. {rules.slug.title()} may run but design "
            f"quality drops near the seam."
        )

    return GapAnalysis(
        gaps=gaps,
        longest_gap=longest,
        causes_hard_fail=hard_fail,
        warn_message=warn_msg,
        hard_fail_message=hard_msg,
    )


def _dist_phrase(d: float) -> str:
    """Render a sequence-distance number for the gap-near-hotspot message."""
    if d == math.inf:
        return "(no hotspots picked)"
    n = int(d)
    if n == 0:
        return "directly adjacent to"
    if n == 1:
        return "1 residue away from"
    return f"{n} residues away from"


def _check_size_envelope(
    rules: ToolRules,
    target_aa: int,
    *,
    binder_max_aa: Optional[int],
    num_designs: Optional[int],
) -> SizeEnvelopeStatus:
    """Evaluate the target residue count against the tool's size envelope."""
    env = rules.size
    combined = (target_aa + binder_max_aa) if binder_max_aa is not None else None

    over_hard = target_aa > env.hard_cap_target_aa
    over_combined = (
        combined is not None and combined > env.hard_cap_combined_aa
    )
    over_warn = target_aa > env.soft_warn_target_aa

    runtime_min: Optional[float] = None
    runtime_basis: Optional[str] = None
    if num_designs is not None and num_designs > 0:
        runtime_min = runtime_estimate_min(rules, target_aa, num_designs)
        runtime_basis = (
            f"{num_designs} design{'s' if num_designs != 1 else ''}"
        )

    warn_msg: Optional[str] = None
    hard_msg: Optional[str] = None
    if over_hard:
        hard_msg = (
            f"Target chain has {target_aa} residues — {rules.slug.title()}'s "
            f"GPU envelope tops out around {env.hard_cap_target_aa} on "
            f"{rules.gpu}. The job would likely run out of memory partway "
            f"through."
        )
    elif over_combined:
        hard_msg = (
            f"Target ({target_aa} aa) + max binder ({binder_max_aa} aa) "
            f"= {combined} aa total complex, which exceeds the "
            f"{env.hard_cap_combined_aa}-aa combined budget for "
            f"{rules.slug.title()}. Either pick a smaller target or "
            f"shorten the max binder length."
        )
    elif over_warn:
        warn_msg = (
            f"Target chain has {target_aa} residues — that's above the "
            f"{env.soft_warn_target_aa}-aa comfort zone for "
            f"{rules.slug.title()}. The job should still run, but expect "
            f"longer wall-clock and a higher chance of out-of-memory."
        )

    return SizeEnvelopeStatus(
        residue_count=target_aa,
        hard_cap_target_aa=env.hard_cap_target_aa,
        soft_warn_target_aa=env.soft_warn_target_aa,
        hard_cap_combined_aa=env.hard_cap_combined_aa,
        binder_max_aa=binder_max_aa,
        combined_aa=combined,
        over_soft_warn=over_warn and not over_hard and not over_combined,
        over_hard_cap=over_hard,
        over_combined_cap=over_combined and not over_hard,
        runtime_estimate_min=runtime_min,
        runtime_basis=runtime_basis,
        gpu=rules.gpu,
        warn_message=warn_msg,
        hard_fail_message=hard_msg,
    )


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
    clean_set = set(_clean_resnums_on_chain(pdb_bytes, target_chain))
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
