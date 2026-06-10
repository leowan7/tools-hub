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

    Note: the prior ``runtime_hard_cap_min`` field was retired by the
    tier-collapse PR. Wall-clock is no longer a preflight block; long
    campaigns are a legitimate user choice on the single-tier model,
    and Modal's own per-subprocess timeout is the residual safety net.
    The runtime estimate is still surfaced as advisory copy in the
    preflight panel.
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
# Constants
#
# Size caps reflect Week 2 calibration (2026-06-05) + published binder
# design literature:
#
#   - Watson et al. 2023 (RFdiffusion, Nature): training distribution
#     50-400 aa, designs against >700 aa targets (TfR, hemagglutinin).
#   - Pacesa et al. 2024 (BindCraft): default-settings examples up to
#     ~500 aa target on A100-80GB.
#   - Adaptyv 2024 community designs: HER2 ECD ~620 aa via BindCraft +
#     RFdiffusion.
#
# Week 2 empirical: rfantibody at 412 aa (1JFF chain A) × 4 designs ran
# clean in 2489s wall (RFdiffusion 1115s + ProteinMPNN 42s + RF2 ~1330s)
# on A100-40GB. No OOM. The fixture sat at the prior hard_cap of 400 and
# completed comfortably — caps were too tight.
#
# Runtime estimator anchors are calibrated from this data point:
#   rfantibody 41 min @ 412 aa × 4 designs  →  base=200 min @ 120 aa × 100
#   designs assuming alpha=1.2. Other tools scaled proportionally from
#   published per-design rates (RFdiffusion ~5-10 min/design at small
#   targets, BindCraft ~30 min/trajectory, Boltz-1 ~5-10 min/design).
#
# The estimate is surfaced in the preflight panel as advisory copy. It
# no longer blocks submit — the tier-collapse PR retired the wall-clock
# hard cap in favour of letting users run multi-day campaigns when they
# explicitly choose a large design count.
# ---------------------------------------------------------------------------

_RFANTIBODY = ToolRules(
    slug="rfantibody",
    gpu="A100-40GB",                 # matches llm-pd infrastructure/modal/rfantibody_app.py:_GPU
    multi_chain_supported=False,
    hotspots_required=True,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=600,      # Week 2: 400 → 600 (lit + empirical)
        soft_warn_target_aa=360,     # 60% of hard cap
        hard_cap_combined_aa=720,    # +120 VHH framework (binder fixed)
        runtime_base_min=200.0,      # Week 2 calibrated from 412/4 = 41 min
        runtime_alpha=1.2,           # RF2 triangle attention dominates
    ),
    gap=GapThresholds(
        warn_length=5,
        needs_fix_length=3,
        needs_fix_hotspot_distance=10,
    ),
)

_RFDIFFUSION = ToolRules(
    slug="rfdiffusion",
    gpu="A100-40GB",                 # matches llm-pd infrastructure/modal/rfdiffusion_app.py:_GPU
    multi_chain_supported=True,
    hotspots_required=True,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=500,      # Week 2: 400 → 500 (Watson 2023 distribution)
        soft_warn_target_aa=300,
        hard_cap_combined_aa=600,
        runtime_base_min=150.0,      # Faster than rfantibody (no RF2 stage)
        runtime_alpha=1.2,
    ),
    gap=GapThresholds(
        warn_length=5,
        needs_fix_length=None,        # length rule unused when on_any_gap
        needs_fix_hotspot_distance=0,
        needs_fix_on_any_gap=True,    # Week 2: VERIFIED — contig builder
                                      # asserts at run_inference.py:84 →
                                      # contigs.py:396 with
                                      # "AssertionError: ('A', N) is not
                                      # in pdb file!" for any missing res
                                      # in the declared range.
    ),
)

_BINDCRAFT = ToolRules(
    slug="bindcraft",
    gpu="A100-80GB",
    multi_chain_supported=True,
    hotspots_required=True,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=500,      # Week 2: 350 → 500 (Pacesa 2024)
        soft_warn_target_aa=300,
        hard_cap_combined_aa=600,
        runtime_base_min=300.0,      # 10 trajectories × ~30 min at small target
        runtime_alpha=1.5,           # AF2 multimer + ColabDesign backprop
        runtime_baseline_designs=10, # bindcraft default trajectories
    ),
    gap=GapThresholds(
        warn_length=10,
        needs_fix_length=20,
        needs_fix_hotspot_distance=5,
    ),
)

_BOLTZGEN = ToolRules(
    slug="boltzgen",
    gpu="A100-40GB",                 # Week 2: verified A100-SXM4-40GB via Modal log
    multi_chain_supported=True,
    hotspots_required=False,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=600,      # Week 2: 400 → 600 (AF3-class headroom)
        soft_warn_target_aa=360,
        hard_cap_combined_aa=700,
        runtime_base_min=600.0,      # Boltz-1 diffusion ~5-10 min/design × 100
        runtime_alpha=1.0,
    ),
    gap=GapThresholds(
        warn_length=20,
        needs_fix_length=50,
        needs_fix_hotspot_distance=10,
    ),
)


_PXDESIGN = ToolRules(
    slug="pxdesign",
    gpu="A100-80GB",                 # matches tools/pxdesign/__init__.py docstring
    multi_chain_supported=True,
    hotspots_required=True,          # pxdesign requires >=1 hotspot
    min_target_aa=30,
    size=SizeEnvelope(
        # pxdesign shares BindCraft's AF2 memory regime (AF2 Initial Guess
        # validation). BindCraft (Pacesa 2025): target practically limited
        # to ~600 aa; an 80GB card fits ~950 aa of target + binder.
        hard_cap_target_aa=600,      # BindCraft ~600 aa practical target ceiling
        soft_warn_target_aa=360,
        hard_cap_combined_aa=950,    # BindCraft: ~950 aa (target + binder) on 80GB
        runtime_base_min=300.0,      # AF2-IG validation per design
        runtime_alpha=1.3,
        runtime_baseline_designs=8,  # pxdesign default num_designs
    ),
    gap=GapThresholds(
        # pxdesign renumbers the target chain to 1..N, so a numbering gap
        # closes but the physical backbone break remains near the epitope.
        # Warn early; hard-fail a sizeable gap within reach of a hotspot.
        warn_length=5,
        needs_fix_length=10,
        needs_fix_hotspot_distance=8,
    ),
)


TOOL_RULES: dict[str, ToolRules] = {
    _RFANTIBODY.slug: _RFANTIBODY,
    _RFDIFFUSION.slug: _RFDIFFUSION,
    _BINDCRAFT.slug: _BINDCRAFT,
    _BOLTZGEN.slug: _BOLTZGEN,
    _PXDESIGN.slug: _PXDESIGN,
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
