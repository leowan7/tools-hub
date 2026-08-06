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
    # WHERE THE CAP CAME FROM, because the refusal copy must not claim more
    # confidence than the number has. "literature" = set from published
    # binder-design work plus (for rfantibody) a clean in-house run near the
    # cap; the panel may say the job would likely exhaust GPU memory.
    # "untested" = no run has ever approached this number here, so the copy
    # says the limit is a precaution rather than a predicted failure point.
    # Default is the cautious one on purpose: a new tool that forgets to
    # declare this under-claims instead of over-claiming.
    cap_basis: str = "untested"


@dataclass(frozen=True)
class ToolRules:
    """All preflight rules for a single binder design tool.

    On the two multi-chain flags
    ----------------------------
    They answer different questions and BOTH must be True before preflight
    lets a multi-chain target through (see
    ``pdb_preflight.preflight_for_tool``):

    ``multi_chain_supported``
        Can the MODEL do it? Upstream/published capability. RFdiffusion,
        BindCraft, BoltzGen, PXDesign and Proteina all genuinely design
        against multi-chain targets; rfantibody does not (it builds a VHH
        against one chain).

    ``multi_chain_container_ready``
        Can the IMAGE WE ACTUALLY RUN do it? This is the one that bills.
        Today it is True for proteina alone, because proteina is the only
        tool whose container lives in THIS repo
        (``tools/proteina/run_pipeline.py``) and was rewritten and proven on
        a live A100. Every other tool is dispatched to an image built from
        the sibling repo ``llm-proteinDesigner``, whose
        ``backend/pdb_utils/pipeline_normalize.py`` still matches the target
        chain by exact string equality (:301) and raises when the whole
        ``target_chain`` string is not a chain id (:383). Verified by
        importing that module and executing it against a clean two-chain
        PDB: ``chain="A"`` normalizes, ``chain="A B"`` raises ValueError,
        for rfdiffusion / boltzgen / pxdesign / rfantibody alike. BindCraft
        ships as a separate prebuilt image (``kendrew-bindcraft:v7``) that
        cannot be inspected from here, so it is UNVERIFIED rather than
        known-good and is gated on the same conservative footing.

    Keeping them separate matters. Collapsing the truth into
    ``multi_chain_supported=False`` for rfdiffusion et al. would encode a
    claim that is simply false about the model, and the next person to read
    it would be right to "fix" it back to True — silently re-opening a paid
    failure. The split records the aspiration AND the reality, and names the
    thing that has to change: port the multi-chain normalizer to
    llm-proteinDesigner, rebuild those images, then flip
    ``multi_chain_container_ready``. Nothing else here needs to move.
    """
    slug: str
    gpu: str                       # human-readable, surfaced in the panel
    multi_chain_supported: bool    # can the MODEL do it (upstream capability)
    multi_chain_container_ready: bool  # can OUR IMAGE do it (what bills)
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
    multi_chain_supported=False,     # VHH against one chain — an upstream limit
    multi_chain_container_ready=False,
    hotspots_required=True,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=600,      # Week 2: 400 → 600 (lit + empirical)
        soft_warn_target_aa=360,     # 60% of hard cap
        hard_cap_combined_aa=720,    # +120 VHH framework (binder fixed)
        runtime_base_min=200.0,      # Week 2 calibrated from 412/4 = 41 min
        runtime_alpha=1.2,           # RF2 triangle attention dominates
        cap_basis="literature",      # + a clean 412 aa in-house run
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
    multi_chain_supported=True,      # Watson 2023 designs against multi-chain targets
    multi_chain_container_ready=False,   # llm-pd normalizer is exact-match; VERIFIED raises
    hotspots_required=True,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=500,      # Week 2: 400 → 500 (Watson 2023 distribution)
        soft_warn_target_aa=300,
        hard_cap_combined_aa=600,
        runtime_base_min=150.0,      # Faster than rfantibody (no RF2 stage)
        runtime_alpha=1.2,
        cap_basis="literature",      # Watson 2023 training distribution
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
    multi_chain_supported=True,      # Pacesa 2024 takes multi-chain target settings
    # UNVERIFIED, not known-good: bindcraft runs from a separate prebuilt
    # image (config.runpod_image_bindcraft = kendrew-bindcraft:v7) rather
    # than llm-pd's normalizer, so its chain handling could not be executed
    # from here the way the other four were. Gated on the conservative
    # footing — this restores exactly the pre-change outcome and costs a
    # user only a message, where guessing wrong costs a funded A100 run.
    multi_chain_container_ready=False,
    hotspots_required=True,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=500,      # Week 2: 350 → 500 (Pacesa 2024)
        soft_warn_target_aa=300,
        hard_cap_combined_aa=600,
        runtime_base_min=300.0,      # 10 trajectories × ~30 min at small target
        runtime_alpha=1.5,           # AF2 multimer + ColabDesign backprop
        runtime_baseline_designs=10, # bindcraft default trajectories
        cap_basis="literature",      # Pacesa 2024 default-settings examples
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
    multi_chain_supported=True,      # Boltz-class models cofold multi-chain
    multi_chain_container_ready=False,   # llm-pd normalizer is exact-match; VERIFIED raises
    hotspots_required=False,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=600,      # Week 2: 400 → 600 (AF3-class headroom)
        soft_warn_target_aa=360,
        hard_cap_combined_aa=700,
        runtime_base_min=600.0,      # Boltz-1 diffusion ~5-10 min/design × 100
        runtime_alpha=1.0,
        cap_basis="literature",      # AF3-class headroom
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
    multi_chain_container_ready=False,   # llm-pd normalizer is exact-match; VERIFIED raises
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
        cap_basis="literature",      # BindCraft/Pacesa 2025 practical ceiling
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


_PROTEINA = ToolRules(
    slug="proteina",
    gpu="A100-80GB",                 # matches tools/proteina/modal_app.py:_GPU
    multi_chain_supported=True,      # a 3-chain target is a validated upstream example
    # The ONLY True in this column. proteina is the one tool whose container
    # lives in this repo (tools/proteina/run_pipeline.py) and it was rewritten
    # for multi-chain and proven end-to-end on a live A100 — custom PDB +
    # multi-chain contig + cross-chain hotspots reaching the model. The other
    # five dispatch to images that still carry the single-chain normalizer.
    multi_chain_container_ready=True,
    hotspots_required=False,         # hotspot-directed, but an open search is valid
    min_target_aa=30,
    size=SizeEnvelope(
        # This entry is a COST GATE before it is a UI panel.
        #
        # THERE IS NO USABLE VRAM MEASUREMENT FOR THIS TOOL. Read that before
        # changing any number here, because the two runs that look like
        # measurements are not.
        #
        # Two paid A100-80GB canary shards (protein_binder, binder_length
        # [60, 120], 8 designs) recorded 67,546 MB at 129 aa / 1 chain and
        # 67,570 MB at 130 aa / 2 chains, device-wide nvidia-smi. Both figures
        # are ~91% a JAX preallocation constant: proteinfoundation.generate
        # imports colabdesign -> JAX, and JAX's default is PREALLOCATE=true at
        # MEM_FRACTION=0.75, i.e. 0.75 x 81,920 = 61,440 MB reserved on the
        # first JAX op no matter how big the target is. Subtract it and the
        # readings are 6,106 MB and 6,130 MB. They agreed to 24 MB because a
        # CONSTANT dominated, not because the workload is flat in target size.
        # tools/proteina/run_pipeline.py now disables that preallocation
        # (_ALLOCATOR_ENV, following tools/af2 and tools/colabfold, which had
        # set the same flags all along). Consequences:
        #     - "130 aa sits at 82.5% of the card" was never true of DEMAND;
        #     - any cap computed by scaling 67,570 scales an allocator policy;
        #     - readings taken BEFORE that change cannot be compared with
        #       readings taken after it. The canary now reports
        #       ``vram_prealloc_disabled`` so the two can never be mixed.
        #
        # WHAT IS ACTUALLY KNOWN: one protein_binder shard, 8 designs, at
        # 130 residues over 2 chains, completed in 359 s. That is a working
        # configuration and a wall-clock. It is not a memory ceiling, and no
        # second target size has ever been run, so there is no slope of any
        # kind — not in VRAM, not in runtime.
        #
        # 140 IS THEREFORE A POLICY NUMBER, NOT A DERIVED ONE. It is "the one
        # configuration we have seen work, plus a little slack", chosen so the
        # tool stays usable for targets of the size we have evidence for while
        # refusing sizes we have never run. No arithmetic supports a specific
        # value; anyone who writes one here is inventing it. Deliberately no
        # percentage-of-card figure appears in this comment.
        #
        # WHAT THIS COSTS: it refuses the campaign the feature was built for.
        # 3S7G is 830 aa across 4 chains and the typical CH2+CH3 two-chain
        # SELECTION is 415 aa. Note the gate sizes the contig selection, not
        # the upload (shared/pdb_preflight.py::_selection_residue_count), so
        # uploading whole 3S7G and designing against A236-300,B236-300 is 130
        # residues and runs — it does not need hand-trimming. Wrong-low costs
        # one error message; wrong-high costs a 4-shard first wave
        # (_LAUNCH_CONCURRENCY_OVERRIDE["proteina"] = 4) that runs to
        # _MAX_SESSION_S = 7200 and bills ~$12.58 per shard for zero designs,
        # which the ~$15/shard hold covers, so nothing downstream stops it.
        #
        # TO RAISE THIS, MEASURE — and note the earlier plan to settle it with
        # a single 415 aa run was WRONG while preallocation was on: that run
        # would have read ~68 GB right up until it OOMed inside JAX's own pool,
        # so it could not have discriminated anything. With _ALLOCATOR_ENV in
        # force, run one protein_binder shard per size at 130 (to re-baseline
        # post-change), 260 and 415 residues, recording peak_proc_vram_mb,
        # baseline_vram_mb and runtime_s. Three points across a 3x span give
        # the first real scaling curve this tool has had; set the cap from it.
        hard_cap_target_aa=140,
        # The one size ever run. Anything above it is untested, so the amber
        # notice starts exactly there rather than at some fraction of the cap.
        soft_warn_target_aa=130,
        # 140 target + 120 binder. 120 is the top of the binder range the
        # canaries actually ran; the form's _BINDER_LEN_MAX of 300 has never
        # been measured against any target.
        hard_cap_combined_aa=260,
        # MEASURED: 359 s = 6.0 min for one 8-design shard at 130 aa. Solving
        # this estimator's own curve at that point — base x (130/120)^1.3 x 8/8
        # — gives base = 5.4. The 75.0 that used to be here was anchored to
        # meta.py's "30 to 120 min" placeholder, i.e. 5-20x the real number.
        runtime_base_min=5.4,
        # ASSUMED, not measured — borrowed from pxdesign's AF2-validation
        # regime. One target size cannot yield a scaling exponent, so this
        # curve is advisory copy only and must not be treated as calibrated.
        runtime_alpha=1.3,
        runtime_baseline_designs=8,  # _SHARD_DESIGNS
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
    _PXDESIGN.slug: _PXDESIGN,
    _PROTEINA.slug: _PROTEINA,
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
