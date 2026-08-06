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
    # confidence than the number has.
    #   "literature" — published binder-design work plus (for rfantibody) a
    #       clean in-house run near the cap. ONLY this basis lets the panel
    #       say the job would likely exhaust GPU memory.
    #   "measured"  — derived from this tool's own VRAM/runtime scaling curve,
    #       but set with headroom ABOVE the largest size actually run. A
    #       curve is not a failure point, so the copy stays the cautious one.
    #   "untested"  — no run has ever approached this number here.
    # The copy branch keys on "is this literature-backed", not on an
    # equality with "untested", so an unrecognised value fails toward the
    # cautious wording rather than toward an invented OOM prediction. The
    # default is the most cautious of the three on purpose: a new tool that
    # forgets to declare this under-claims instead of over-claiming.
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
        # THE MEASUREMENT. Three paid A100-80GB canary shards (protein_binder,
        # seed 1234, 8 designs, binder_length [60, 120]), all COMPLETED exit 0:
        #
        #     target aa   peak device VRAM   % of 81,920 MB card   runtime
        #        130           8,943 MB             10.9%           576 s
        #        260          15,541 MB             19.0%           645 s
        #        415          25,457 MB             31.1%           874 s
        #
        # They are comparable to each other for exactly one reason: JAX
        # preallocation was DISABLED for all three (_ALLOCATOR_ENV in
        # tools/proteina/run_pipeline.py). Keep that condition attached to the
        # numbers — the trap it closes can recur. The two shards before these
        # read 67,546 MB at 129 aa and 67,570 MB at 130 aa and agreed to 24 MB,
        # because ~91% of each was a JAX allocator constant (PREALLOCATE=true
        # at MEM_FRACTION=0.75 reserves 61,440 MB on the first JAX op whatever
        # the target size). They measure a policy, not this workload, and must
        # never be mixed with the table above. The canary reports
        # ``vram_prealloc_disabled`` so the two regimes stay distinguishable.
        #
        # THE FIT. Exact quadratic through the three points:
        # MB = 3913 + 32.66*n + 0.04639*n^2. Growth ACCELERATES — the power-law
        # exponent is 0.80 over 130->260 and 1.06 over 260->415 — so a straight
        # line through the low end UNDER-reads at the top, the direction that
        # bills money.
        #
        # MEASUREMENT ENDS AT 415. Above it only the fit is talking: 31,841 MB
        # (38.9% of the card) at 500 aa, 40,209 MB (49%) at 600, OOM near 992.
        #
        # 500 IS STILL A POLICY NUMBER; what changed is its anchor — a scaling
        # curve instead of an allocator constant. It is a 1.2x step past the
        # measured 415 at 38.9% of the card, the worst extrapolation error this
        # tool has shown us was 11% LOW at a 1.6x step, and even a 100% model
        # error at 500 aa still fits. (That 11% is the power-law miss, which is
        # the form this comment argues in two paragraphs above: fitted to 130
        # and 260 aa it predicts 22,564 MB at 415 against a measured 25,457.
        # A straight line through the same two points is the friendlier miss at
        # 8% low, and quoting that one instead would be picking the flattering
        # number. Both under-read, which is the direction that bills.)
        #
        # Being wrong-high costs what it always
        # did: a 4-shard first wave (_LAUNCH_CONCURRENCY_OVERRIDE["proteina"]
        # = 4) running to _MAX_SESSION_S = 7200 at ~$12.58 a shard for zero
        # designs, inside a ~$15/shard hold that covers all of it.
        #
        # WHAT WOULD MOVE IT AGAIN: one completed shard above 415 aa. Not an
        # argument, and not a longer extrapolation from these same points.
        hard_cap_target_aa=500,
        # Exactly where measurement ends and extrapolation begins — not a
        # fraction of the cap. The amber notice means "past here, the number
        # on screen is a model rather than a run".
        soft_warn_target_aa=415,
        # 500 target + 120 binder. 120 is the top of the binder range the
        # canaries actually ran; the form's _BINDER_LEN_MAX of 300 has never
        # been measured against any target.
        hard_cap_combined_aa=620,
        # MEASURED. Least squares on the three runtimes above, in this
        # estimator's own form (minutes = base x (n/120)^alpha at 8 designs),
        # gives base=9.0 with residuals inside +/-10% across 130-415 aa. The
        # 5.4 that used to be here was solved from a 359 s reading at 130 aa.
        # TWO READINGS EXIST AT THAT SIZE AND THEY DISAGREE BY ~60%: 359 s and
        # 576 s. Only the 576 s one has a verified completion attached (exit 0,
        # 8 scored designs), and it is one of the three points above. What the
        # 359 s run did, and why the two differ, is recorded nowhere in this
        # repo — the discrepancy is unexplained rather than diagnosed, and the
        # older figure is not used for anything.
        runtime_base_min=9.0,
        # MEASURED, and it was previously labelled ASSUMED: 1.3, borrowed from
        # pxdesign's AF2-validation regime because one target size cannot
        # yield an exponent. Three sizes can, and the real curve is far
        # flatter than the borrowed one — the old 5.4/1.3 pair put a 415 aa
        # shard at ~27 min against a measured 14.6.
        runtime_alpha=0.34,
        runtime_baseline_designs=8,  # _SHARD_DESIGNS
        # Neither "literature" nor "untested" any more: the cap is derived
        # from this tool's own scaling curve. It still must not predict an OOM
        # — see pdb_preflight._check_size_envelope, where only a
        # literature-backed cap earns that copy — because nothing has ever
        # been run at 500 and the fit says 500 is nowhere near the ceiling.
        cap_basis="measured",
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
