"""Per-tool preflight rules — size envelope, gap thresholds, hotspot policy.

Extracted from pdb_preflight.py so the magic numbers live in one reviewable
place. Each binder design tool has a single ToolRules entry; the preflight
evaluator reads from TOOL_RULES rather than hardcoding constants.

Initial values come from research synthesis (theoretical caps + published
distribution); Week 2 deliberate-fail calibration tunes them against
observed OOM/timeout boundaries on tools.ranomics.com.

Adding a 5th tool:
    Append to TOOL_RULES with a complete ToolRules entry. Tests under
    tests/test_pdb_preflight_rules.py iterate over TOOL_RULES so coverage
    extends automatically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GapThresholds:
    """When does an internal chain gap warn vs hard-fail.

    ``warn_length``
        Length (in missing residues) at which we surface a soft warning
        in the panel (verdict kind stays READY-ish, AF fallback offered).
    ``needs_fix_length``
        Length at which we HARD-fail, but only when the gap is within
        ``needs_fix_hotspot_distance`` residues of a hotspot. Set to
        ``None`` to disable length-based hard fail (e.g. boltzgen
        without near-hotspot rule).
    ``needs_fix_hotspot_distance``
        Sequence-distance window (residues) for the near-hotspot rule.
        Ignored when ``needs_fix_length`` is None.
    ``needs_fix_on_any_gap``
        rfdiffusion-style: any internal gap is an unconditional hard fail
        because the contig builder asserts every residue in the declared
        range exists. When True, the length + distance rules are bypassed
        for hard-fail (they still drive WARN messaging).
    """
    warn_length: int
    needs_fix_length: Optional[int]
    needs_fix_hotspot_distance: int
    needs_fix_on_any_gap: bool = False


@dataclass(frozen=True)
class SizeEnvelope:
    """Per-tool residue ceiling + runtime heuristic.

    ``hard_cap_target_aa``
        Reject submit if the (cleaned) target chain residue count exceeds.
    ``soft_warn_target_aa``
        Show amber "this is large" notice in the panel but allow submit.
    ``hard_cap_combined_aa``
        Reject if (target_aa + binder_length_max) exceeds. Catches the
        case where the target alone is within budget but the user picked
        an oversized binder length that pushes total complex past GPU VRAM.
    ``runtime_base_min``
        Baseline wall-clock minutes for a 120-aa target at the tool's
        ``runtime_baseline_designs`` count. Curve anchor for the runtime
        estimator.
    ``runtime_alpha``
        Exponent for target-size scaling; runtime ~ (target_aa/120)^alpha.
        1.0 for ~linear (boltzgen, rfantibody), 1.2 for diffusion+AF2
        validation (rfdiffusion), 1.5 for AF2-backprop loops (bindcraft).
    ``runtime_baseline_designs``
        Number of designs at the ``runtime_base_min`` anchor. Used so the
        estimator linearly scales when the user requests more/fewer designs.
        100 for diffusion tools, 10 for bindcraft (default trajectories).
    """
    hard_cap_target_aa: int
    soft_warn_target_aa: int
    hard_cap_combined_aa: int
    runtime_base_min: float
    runtime_alpha: float
    runtime_baseline_designs: int = 100


@dataclass(frozen=True)
class ToolRules:
    """All preflight rules for a single binder design tool."""
    slug: str
    gpu: str                       # human-readable, surfaced in the panel
    multi_chain_supported: bool    # False for rfantibody (upstream limit)
    hotspots_required: bool        # True for rfantibody / rfdiffusion / bindcraft
    min_target_aa: int             # below this the model has nothing to design
    size: SizeEnvelope
    gap: GapThresholds


# ---------------------------------------------------------------------------
# Initial constants
# (pre-calibration; Week 2 deliberate-fail loop tunes them against real
# OOM/timeout boundaries observed on tools.ranomics.com)
# ---------------------------------------------------------------------------

_RFANTIBODY = ToolRules(
    slug="rfantibody",
    gpu="A100-40GB",
    multi_chain_supported=False,
    hotspots_required=True,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=400,
        soft_warn_target_aa=250,
        hard_cap_combined_aa=520,    # +120 VHH framework (binder fixed)
        runtime_base_min=20.0,
        runtime_alpha=1.0,
    ),
    gap=GapThresholds(
        warn_length=5,
        needs_fix_length=3,
        needs_fix_hotspot_distance=10,
    ),
)

_RFDIFFUSION = ToolRules(
    slug="rfdiffusion",
    gpu="A100-40GB",
    multi_chain_supported=True,
    hotspots_required=True,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=400,
        soft_warn_target_aa=250,
        hard_cap_combined_aa=500,
        runtime_base_min=15.0,
        runtime_alpha=1.2,
    ),
    gap=GapThresholds(
        warn_length=5,
        needs_fix_length=None,        # length rule unused when on_any_gap
        needs_fix_hotspot_distance=0,
        needs_fix_on_any_gap=True,    # contig builder asserts every res
    ),
)

_BINDCRAFT = ToolRules(
    slug="bindcraft",
    gpu="A100-80GB",
    multi_chain_supported=True,
    hotspots_required=True,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=350,
        soft_warn_target_aa=200,
        hard_cap_combined_aa=450,
        runtime_base_min=45.0,
        runtime_alpha=1.5,
        runtime_baseline_designs=10,  # bindcraft default trajectories
    ),
    gap=GapThresholds(
        warn_length=10,
        needs_fix_length=20,
        needs_fix_hotspot_distance=5,
    ),
)

_BOLTZGEN = ToolRules(
    slug="boltzgen",
    gpu="A100-40GB",
    multi_chain_supported=True,
    hotspots_required=False,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=400,
        soft_warn_target_aa=250,
        hard_cap_combined_aa=500,
        runtime_base_min=25.0,
        runtime_alpha=1.0,
    ),
    gap=GapThresholds(
        warn_length=20,
        needs_fix_length=50,
        needs_fix_hotspot_distance=10,
    ),
)


TOOL_RULES: dict[str, ToolRules] = {
    _RFANTIBODY.slug: _RFANTIBODY,
    _RFDIFFUSION.slug: _RFDIFFUSION,
    _BINDCRAFT.slug: _BINDCRAFT,
    _BOLTZGEN.slug: _BOLTZGEN,
}


# Convenience views maintained for the existing pdb_preflight contract.
# Derived from TOOL_RULES so adding a 5th tool needs only one dict entry.

BINDER_DESIGN_TOOLS: frozenset[str] = frozenset(TOOL_RULES.keys())
HOTSPOTS_REQUIRED: frozenset[str] = frozenset(
    slug for slug, rules in TOOL_RULES.items() if rules.hotspots_required
)


def runtime_estimate_min(
    rules: ToolRules,
    target_aa: int,
    num_designs: int,
) -> float:
    """Rough wall-clock estimate (minutes) for the preflight panel.

    Form: ``base × (aa/120)^alpha × (num_designs / baseline_designs)``.

    Not precise — surface this to users so they don't accidentally
    submit a 6-hour job thinking it's a 30-minute one. Real runtime
    depends on Modal cold-start, MSA build (where applicable), and GPU
    contention.

    Args:
        rules: ToolRules for the tool the user is about to submit to.
        target_aa: residue count on the (cleaned) target chain.
        num_designs: how many designs the user has requested.

    Returns:
        Estimated wall-clock minutes. Floor of 5 to avoid showing
        "1 min" for tiny targets where Modal cold-start dominates.
    """
    if target_aa <= 0 or num_designs <= 0:
        return float(rules.size.runtime_base_min)
    size_factor = (target_aa / 120.0) ** rules.size.runtime_alpha
    design_factor = num_designs / rules.size.runtime_baseline_designs
    est = rules.size.runtime_base_min * size_factor * design_factor
    return max(5.0, est)
