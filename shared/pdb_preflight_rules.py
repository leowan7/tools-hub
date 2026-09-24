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
        ``runtime_baseline_designs`` count, covering the part of the job
        that scales with the design count. Curve anchor for the runtime
        estimator. It is the whole estimate at the pivot for any tool whose
        ``runtime_fixed_min`` is 0.0.
    ``runtime_fixed_min``
        Wall-clock minutes the job costs ONCE however many designs are
        requested -- scaled by target size, NOT by the design count.
        Defaults to 0.0, which leaves the estimator purely multiplicative:
        the historical shape, and what an envelope gets unless it says
        otherwise. Set it only where a tool's own runtime notes decompose
        the job into a fixed stage plus a per-design stage, because a
        purely multiplicative curve cannot fit both ends of such a job at
        once, and no value of ``runtime_base_min`` alone can rescue it.
    ``runtime_alpha``
        Exponent for target-size scaling; runtime ~ (target_aa/120)^alpha.
        Above 1 where the tool re-folds the complex inside an optimisation
        loop, near 1 where cost rises roughly linearly with target size, and
        below 1 where a fixed batch amortises the per-target work.
    ``runtime_baseline_designs``
        Number of designs at the ``runtime_base_min`` anchor. Used so the
        estimator linearly scales when the user requests more/fewer designs.
        It is meaningful only paired with the base it anchors: moving one
        without the other rescales every estimate that tool makes. WHAT it
        anchors differs per tool -- for some the count the form defaults to,
        for others a fixed batch width -- and nothing makes the two agree, so
        do not read an envelope's value as that tool's form default. All
        six are pinned by a test -- move any one of them and something goes
        red -- but they are not pinned to comparable things, and that is
        the part worth knowing. bindcraft's and pxdesign's are each tied to
        ONE measured run, by tests/test_pdb_preflight.py::
        test_bindcraft_runtime_curve_reproduces_its_one_measured_run and
        tests/test_pdb_preflight.py::
        test_pxdesign_runtime_curve_anchors_on_one_run_and_discloses_the_rest.
        rfantibody's is tied to TWO runs at different target sizes, by
        tests/test_pdb_preflight.py::test_rfantibody_runtime_from_two_runs.
        proteina's is tied to its shard width, by
        tests/test_proteina_shard_size.py::
        test_preflight_runtime_baseline_is_the_shard_width. rfdiffusion's
        and boltzgen's are pinned as VALUES only -- both at 100, which is
        this dataclass's own default -- by tests/test_pdb_preflight.py::
        test_rfdiffusion_baseline_is_not_the_form_default. boltzgen is in
        that last group because it is the only envelope still inheriting
        the default rather than writing it out.
        rfdiffusion's is additionally load-bearing in
        tests/test_pdb_preflight.py::
        test_rfdiffusion_runtime_curve_reproduces_both_recorded_runs, which
        reproduces two recorded runs through its base, its fixed term and
        this count together -- so moving this one alone reds that test too,
        which is more than "pinned as a value" and less than an anchor of
        its own.

    The per-tool values of ``runtime_alpha`` and ``runtime_baseline_designs``
    are deliberately NOT restated here. A list in this docstring cannot fail
    when one of the envelopes below moves, and this one had drifted twice
    over: it read "1.0 for ... rfantibody" against an envelope of 1.2, and
    "100 for diffusion tools" against proteina and pxdesign at 8. Read the
    envelopes; they are the values.
    tests/test_pdb_preflight.py::
    test_bindcraft_runtime_curve_reproduces_its_one_measured_run is what a
    checkable version looks like -- it reads the validator's own fallback out
    of its source rather than repeating the number.

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
    runtime_fixed_min: float = 0.0
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

    On ``hotspot_needs_full_backbone``
    ---------------------------------
    Does a hotspot on a residue with an INCOMPLETE backbone (present, with a
    CA, but short of N/CA/C/O) survive into this tool's run?

    It is a third distinct question, and it is NOT ``hotspots_required``.
    That flag says whether the tool needs a hotspot at all; this one says
    whether the one it was given will resolve. Missing O atoms are routine —
    terminal residues, disordered loops — so answering it with the wrong flag
    either refuses ordinary paid work or funds a run that dies mid-flight.

    True for the five tools dispatched to llm-proteinDesigner images: they run
    ``pipeline_normalize`` in-container, which DROPS such a residue, so the
    hotspot cannot be mapped afterwards. Executed against a synthetic chain
    whose residue 30 carries N/CA/C and no O: ``normalize_for_boltzgen`` and
    ``normalize_for_pxdesign`` both return a ``renumber_map`` with no
    ``("A", 30)``, and llm-proteinDesigner's
    ``docker/boltzgen/run_pipeline.py:1083`` raises ``"Hotspot residue(s) ...
    are not present after structure cleanup"`` on exactly that condition —
    after the wallet hold, with the GPU running.

    Run against BOTH copies of the normalizer, because they are NOT
    byte-identical: this repo's vendored ``shared/pipeline_normalize.py`` and
    the sibling's ``backend/pdb_utils/pipeline_normalize.py`` that the image
    actually mounts. Each drops one residue from chain A and neither maps
    ``("A", 30)``. Checking only the vendored copy would have been an argument
    about a file the GPU never loads.

    False for proteina alone. Its container is the one in THIS repo and it
    never calls ``pipeline_normalize``; it selects residues by CA
    (``pdb_ca_residues`` -> ``select_residues``) and matches
    ``missing_hotspots`` against that set. Executed on the same structure:
    ``missing_hotspots(selected, ["A30"])`` returns ``[]`` — the run is
    accepted and correctly constrained. Refusing it at the gate would be a
    false refusal of a run that succeeds today.

    Defaults True, the cautious direction: a new tool that forgets to declare
    this refuses a user rather than spending their money.
    """
    slug: str
    gpu: str                       # human-readable, surfaced in the panel
    multi_chain_supported: bool    # can the MODEL do it (upstream capability)
    multi_chain_container_ready: bool  # can OUR IMAGE do it (what bills)
    hotspots_required: bool        # True for rfantibody / rfdiffusion / bindcraft
    min_target_aa: int             # below this the model has nothing to design
    size: SizeEnvelope
    gap: GapThresholds
    # Does an incomplete-backbone hotspot survive into this tool's run?
    hotspot_needs_full_backbone: bool = True


# ---------------------------------------------------------------------------
# Constants
#
# Size caps reflect Week 2 calibration (2026-06-05) + published binder
# design literature:
#
#   - Watson et al. 2023 (RFdiffusion, Nature): training distribution
#     50-400 aa, designs against >700 aa targets (TfR, hemagglutinin).
#   - Pacesa et al. 2025 (BindCraft, Nature): default-settings examples
#     up to ~500 aa target on A100-80GB.
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
#   targets, BoltzGen ~5-10 min/design).
#
# THE RFANTIBODY ANCHOR THAT PARAGRAPH RECORDS HAS SINCE MOVED -- base,
# design count and exponent alike -- and so has every tool it says was
# "scaled proportionally", EXCEPT boltzgen, which is still on the
# published rate quoted here. Two things it names have not moved: the
# 120 aa anchor, still the divisor in runtime_estimate_min, and that
# 2489 s run, which is one of the two rfantibody now rests on. The
# paragraph is kept as the starting point the paragraphs below record
# departures from, one per re-anchor; those are the live record.
#
# BindCraft is the exception and is NOT in that list any more: its anchor
# is measured rather than published. The ~30 min/trajectory figure that
# used to sit here over-read the one real bindcraft run by ~3x. See the
# _BINDCRAFT envelope below for the run and the arithmetic. Every OTHER
# envelope in this file is deliberately left on its published rate — this
# change re-baselines bindcraft and nothing else.
#
# One of them is known to be wrong in the same direction: _PXDESIGN’s
# runtime base carries the comment "AF2-IG validation per design", so it
# rests on the same kind of published per-design figure this change just
# corrected for bindcraft. It implies 37.5 min for ONE design at the
# 120 aa anchor, while the two pxdesign pilot rows in
# docs/VALIDATION-LOG.md are whole multi-design jobs on ~115-130 aa
# targets that finished in 8.4 and 7.7 min.
# Re-anchoring it was deferred as a separate calibration on its own
# evidence rather than a side effect of that one. THE PARAGRAPH BELOW
# IS THAT CALIBRATION. This note is kept because it records why the
# two were split, not because pxdesign is still unanchored.
#
# PXDESIGN IS NO LONGER ONE OF THOSE "other tools". Its base was a
# published BindCraft per-trajectory rate applied per design, and it
# over-quoted all three pxdesign runs on record -- 8.4x on the smallest,
# 27x on the middle one, 208x on the largest. It is now anchored on ONE
# of those runs, and the other two are not evidence for it. See the
# _PXDESIGN envelope below for the arithmetic, and for the two residuals
# the re-anchor leaves in place rather than fixing. No other envelope in
# this file is touched. _BINDCRAFT carried the same 300.0 on the same
# published rate until #314 re-anchored it on its own measured run --
# that was a separate change and this one does not revisit it.
#
# RFDIFFUSION IS NO LONGER ONE OF THOSE "other tools" EITHER, and it
# leaves that list for a different reason than bindcraft and pxdesign did.
# Its 150.0 never matched the published "~5-10 min/design" quoted above in
# the first place -- 150/100 is 1.5 min/design -- and by the time it was
# re-examined the container had changed underneath it
# (llm-proteinDesigner#23 put a real MSA in the AF2 re-score, 2.76x), so
# the panel quoted ~11 min for a job that takes 37. The repair is not a
# new constant on the same curve: tools/rfdiffusion/meta.py records the
# job as a fixed stage plus a per-design stage, which nothing purely
# multiplicative can express, so SizeEnvelope grew an optional
# runtime_fixed_min and rfdiffusion is the only envelope that sets it.
# See the _RFDIFFUSION envelope below for the arithmetic. No other
# envelope moves: runtime_fixed_min defaults to 0.0, every other tool
# leaves it there, and the estimator adds the term in a position where
# 0.0 is an exact identity.
#
# RFANTIBODY IS THE LAST OF THOSE "other tools" TO LEAVE, and it is the
# one the list started from -- the 41 min @ 412 aa x 4 designs quoted
# above is its run. A second run has since been recorded at a DIFFERENT
# target size (115 aa), and two runs at the same design count fix the
# size exponent outright, so runtime_alpha moves here. That makes it the
# second measured exponent in this file rather than the first: proteina's
# _SHARD moved 1.3 -> 0.34 the same way, on three sizes rather than two,
# and its comment is the precedent for this one. The pair lands at base
# 14.4 per 4 designs with alpha 0.86, where the shipped 200 per 100 at
# 1.2 under-quoted both runs, by 0.85x and 0.55x. That
# does NOT re-open rfdiffusion: #323's runtime_fixed_min is a
# design-count term, so the two repairs are orthogonal. See the
# _RFANTIBODY envelope below for the solve and for what two points
# cannot establish.
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
        # Runtime rests on TWO recorded runs, both at 4 designs:
        #
        #   - docs/CALIBRATION-WEEK2.md job #1: 1JFF chain A, 412 aa,
        #     4 designs, 2489 s wall = 41.4833 min on A100-40GB
        #     (RFdiffusion 1115s + ProteinMPNN 42s + RF2 ~1330s).
        #   - the 2026-09-08 worked example: 4ZQK chain A, 115 aa,
        #     4 designs, 13.9 min. Attestation per input below.
        #
        # Same design count, different target size, so the pair is two
        # equations in (base, alpha) and the exponent is fitted rather
        # than assumed. Note "412/4 = 41 min" in the Week 2 doc is not a
        # division: it names the SHAPE of job #1. That doc's own scaling
        # to 100 designs ("~17 hours") is LINEAR, 41 min x 25.
        #
        # docs/VALIDATION-LOG.md job e29a462d (4ZQK chain A, 115 aa, 474
        # GPU-s) is deliberately NOT a third point: its DESIGN COUNT is
        # recorded nowhere. The row says "5 ranked candidates", which is a
        # candidate count, and stage 2 runs ProteinMPNN at
        # seqs_per_backbone=5 (tools/rfantibody/meta.py, the "4 -> 20"
        # note), so five candidates reads as ONE backbone -- while the May
        # campaign plan in the same file says rfantibody was hardcoded
        # to 2. num_designs is the divisor here, so those readings put the
        # estimate anywhere from 5.0 (floored, exercising no curve at all)
        # to 9.50. Do not re-add it without a recorded count.
        #
        # Every input the September run turns on is RECORDED rather than
        # inferred, which is exactly what e29a462d lacked:
        #   - designs: tools/rfantibody/example/result.json
        #     "total_designs": 4. The same file's "candidate_count": 20
        #     confirms the 4 -> 20 fanout, so that 4 is a BACKBONE count
        #     and not a candidate count -- the very ambiguity that made
        #     e29a462d unusable.
        #   - runtime: the same file's "runtime_minutes": 13.9. meta.py's
        #     "runtime": "14 minutes" is that same figure, rounded for
        #     display.
        #   - target size: 115 aa for 4ZQK chain A, attested by three real
        #     Modal runs recorded in THIS file -- the "VERIFIED on GPU
        #     2026-08-05" notes on _RFDIFFUSION, _BOLTZGEN and _PXDESIGN,
        #     each of which returned {A:115, ...}. meta.py's "crystal
        #     numbering 18-132" is consistent with 115 but is a numbering
        #     range rather than a count, so it is not what is relied on.
        #
        # That 13.9 has a second and INDEPENDENT record: the same file's
        # "cost_usd": "1.01" is the CUSTOMER-facing charge, raw GPU cost
        # times WALLET_MARKUP. A charge converts back to seconds only
        # through the tool's GPU class, and for this slug that class is
        # pinned rather than asserted -- gpu= above says A100-40GB and
        # tests/test_gpu_class_drift.py
        # ::test_wallet_gpu_class_matches_container asserts the wallet
        # bills rfantibody at exactly that field, while
        # tests/test_gpu_class_drift.py
        # ::test_wallet_gpu_class_is_on_the_rate_card keeps it on the card
        # so the lookup cannot fall through to the 80GB default. wallet.py
        # prices A100-40GB at $0.000714/s and WALLET_MARKUP is 1.70, so
        # $1.01 buys 832 s = 13.87 min, 0.2% from the recorded 13.9. At
        # the 80GB rate instead it would imply 9.63 min, and that apparent
        # 1.44x conflict is nothing but the ratio between the two classes.
        #
        # THE SOLVE. Two runs at the SAME design count are two equations
        # in (base, alpha):
        #
        #   size ratio     412/115                  = 3.5826
        #   runtime ratio  (2489/60) / 13.9         = 2.9844
        #   alpha          ln(2.9844) / ln(3.5826)  = 0.856838
        #   base           13.9 / (115/120)**alpha  = 14.4162
        #
        # Shipped rounded to 14.4 / 0.86, which reproduces the September
        # run at 13.88 (-0.13%) and job #1 at 41.60 (+0.28%). Those
        # residuals are rounding and NOT validation: two points fit a
        # two-parameter power law exactly, so nothing is held out. A third
        # run at a third target size is what would test the exponent.
        #
        # AND THE TWO RUNS ARE 95 DAYS APART (2026-06-05, 2026-09-08), so
        # the fit reads any pipeline change between them as target-size
        # scaling -- the failure mode _RFDIFFUSION below records, where an
        # upstream change moved runtime 2.76x. git log over tools/rfantibody
        # across that window shows form, copy and worked-example commits,
        # plus one canonicalizing cdr_lengths before they reach the GPU
        # (2026-06-10); the container image lives in another repo and was
        # not inspected. That is no evidence of a workload change, not
        # proof there was none.
        #
        # ALPHA MOVES HERE, which one other envelope in this file has done
        # before -- proteina's _SHARD, 1.3 -> 0.34, and on three target
        # sizes against this one's two, so the precedent is the better
        # evidenced of the pair. 1.2 is not merely unfitted but REFUTED by
        # these two runs: a target 3.58x the size took 2.98x the time,
        # while 1.2 predicts 3.5826**1.2 = 4.62x. Re-anchored on the
        # September run alone and left at 1.2 it quotes job #1 at 64.2 min
        # against a measured 41.48, 55% over. runtime_fixed_min cannot
        # rescue it either: runtime_estimate_min multiplies that term by
        # the same size_factor as the base, making it a DESIGN-COUNT term
        # that cancels out of any ratio taken at one design count, and both
        # runs sit at 4 designs. Every (base, fixed) pair whatever puts
        # these two runs 4.6242x apart at alpha=1.2 against a measured
        # 2.9844, so the 0.0 default is left here having been tried.
        runtime_base_min=14.4,       # per 4 designs at the 120 aa anchor
        runtime_alpha=0.86,          # MEASURED, two runs: see the solve above
        # Now EQUAL to the form default (tools/rfantibody/__init__.py
        # ::validate), and it moved from 100 TOGETHER WITH
        # runtime_base_min, 200 -> 14.4. The pairing is the invariant, not
        # the convention -- bindcraft's pair moved 300 -> 40 with 10 -> 4
        # for the same reason. Moving either member alone re-scales this
        # tool's panel by 25x.
        runtime_baseline_designs=4,
        # All three guarded by tests/test_pdb_preflight.py::
        # test_rfantibody_runtime_from_two_runs, which reproduces both runs
        # from whatever the envelope holds rather than asserting any of the
        # three by literal -- move one member of the base/baseline pair
        # alone and the residuals blow out by 25x. Its 5% band is loose
        # against the live 0.13% and 0.28% on purpose: it is sized to catch
        # drift, not to certify the curve. It also mutates alpha back to
        # 1.2, over a swept grid of runtime_fixed_min, to show neither
        # closes job #1.
        #
        # rfdiffusion's baseline assertion is NOT here any more. It moved
        # to tests/test_pdb_preflight.py::
        # test_rfdiffusion_baseline_is_not_the_form_default, now that only
        # one tool makes that claim.
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
    # VERIFIED on GPU 2026-08-05: real two-chain 4ZQK run via Modal returned
    # {A:115, B:106, C:56} in 153 s — both protomers intact, one binder, no
    # target/binder swap. The llm-pd normalizer is no longer exact-match
    # (leowan7/llm-proteinDesigner#11 ports parse_target_chains) and the image
    # was rebuilt against it.
    multi_chain_container_ready=True,
    hotspots_required=True,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=500,      # Week 2: 400 → 500 (Watson 2023 distribution)
        soft_warn_target_aa=300,
        hard_cap_combined_aa=600,
        # THE ONLY TWO-TERM ENVELOPE IN THIS FILE, and it has to be.
        # rfdiffusion's cost is a fixed diffusion+MPNN stage plus a
        # per-design AF2 re-score, so a purely multiplicative curve cannot
        # sit on both ends of it at once: meta.py advertises "25 to 40 min
        # (4 to 8 designs)", a 1.6x spread over a 2x design ratio, and no
        # value of runtime_base_min alone produces a spread under 2x.
        #
        # tools/rfdiffusion/meta.py states the split outright -- a fixed
        # ~700 s plus ~190 s per design -- and that split reproduces the one
        # job it records exactly: 700 + 190*8 = 2220 GPU-s for job 25471e07
        # (4ZQK chain A, 115 aa, 8 designs, ~37 min). Both constants below
        # are that split divided by this curve's own size factor at 115 aa,
        # (115/120)^1.2 = 0.95021, which normalises them to the 120-aa pivot
        # the estimator anchors on, with the per-design one carried up to
        # the 100-design baseline:
        #     fixed = (700/60) / 0.95021       = 12.278  ->  12.28
        #     base  = (190/60) / 0.95021 * 100 = 333.259 ->  333.3
        # At those rounded values the curve returns 24.34 min at 4 designs
        # and 37.01 at 8, against the 24.33 and 37.00 meta.py records --
        # 0.014% out at both ends, pinned at +/-5% by
        # tests/test_pdb_preflight.py::
        # test_rfdiffusion_runtime_curve_reproduces_both_recorded_runs.
        # Both ends come from one decomposition, so this is ONE anchor
        # expressed twice, not two independent confirmations.
        #
        # WHAT THIS REPLACED, because the old 150.0 was not merely low.
        # llm-proteinDesigner#23 made the AF2 re-score fetch a real MSA for
        # the target instead of folding it single-sequence, multiplying the
        # run by 2.76x: job 25471e07 went 804 -> 2220 GPU-s on the same job
        # shape (tools/rfdiffusion/meta.py, runtime header above
        # preset_runtime_rows). #240 (b692593) carried that 277.5 s/design
        # measurement into the chunking and cost path but did not touch this
        # file -- `git show b692593 -- <this file>` is empty -- so the panel
        # quoted ~11 min for the 8-design run that actually takes 37, a
        # factor of ~3.2. Nor was 150.0 ever a published-rate anchor despite
        # the module header crediting it to "RFdiffusion ~5-10 min/design":
        # 150.0/100 is 1.5 min/design, 3.3-6.7x under that rate. It tracked
        # the MEASURED pre-update run (1.76 min/design at the pivot) and
        # went stale with it.
        #
        # The pre-update runs are gone from this comment deliberately. Job
        # 25471e07's 804 GPU-s describes a container that no longer exists.
        # The other, job 5e5109ee (115 aa, 2 designs, 222 GPU-s), never
        # exercised the old curve at all -- at 2.85 min it fell under
        # runtime_estimate_min's max(5.0, est) floor, so the function
        # returned 5.0. Under the two-term curve it estimates 18.0 min,
        # clear of the floor, but its own measurement is pre-MSA and is
        # therefore evidence neither for nor against what ships here.
        runtime_fixed_min=12.28,
        runtime_base_min=333.3,
        runtime_alpha=1.2,
        # As rfantibody: equal to the dataclass default, written out
        # because it is the anchor, and NOT the form default of 4
        # (tools/rfdiffusion/__init__.py::validate).
        runtime_baseline_designs=100,
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
    multi_chain_supported=True,      # Pacesa 2025 takes multi-chain target settings
    # Still UNVERIFIED as of the 2026-08-05 GPU session that cleared
    # rfdiffusion / boltzgen / pxdesign. It could not be cleared the same way:
    # bindcraft is the one binder tool with no smoke tier, so the only way to
    # exercise its chain handling is a full paid pilot run. Its wrapper does
    # pass target_chain straight through to BindCraft's native `chains`
    # setting and needed no code change — but that is a docs read plus a code
    # path, not evidence.
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
        hard_cap_target_aa=500,      # Week 2: 350 → 500 (Pacesa 2025)
        soft_warn_target_aa=300,
        hard_cap_combined_aa=600,
        # RE-ANCHORED (2026-09-18) on the only bindcraft run this repo has
        # measured: job 1c4d5803, 1170 GPU-s billed for 2 designs (19.5 min)
        # against 4ZQK chain A (~115 aa by its 18-132 crystal numbering, so
        # within ~5% of this curve's 120 aa anchor and taken straight with
        # no size correction). Reconciled against the wallet ledger in the
        # worked-example header of tools/bindcraft/meta.py, recorded in
        # docs/VALIDATION-LOG.md, and cited again by
        # tools/bindcraft/__init__.py::validate. ~10 min/design.
        # It is ONE POINT AND NOT A FIT, and it is the only point there is:
        # job 1c4d5803 is the only bindcraft run in the tree with a recorded
        # completion time. The 2026-04-22 4Z18 pilot in
        # docs/VALIDATION-LOG.md passed but logs its GPU seconds as
        # "(not captured)", and the Week 2 calibration run
        # (docs/CALIBRATION-WEEK2.md, "Observed results") was CANCELLED at
        # 2717 s on a 412 aa target, so it timed a cancellation, not a run.
        #
        # One point, but not the only constraint, and that is what makes
        # this a correction rather than a guess: the tool already SHIPS the
        # same rate on another surface. ``PRESET_RUNTIME["pilot"]
        # ["typical_minutes"]`` in tools/bindcraft/meta.py reads "30 to 45",
        # tied there to the ``num_designs`` default of 4 -- 7.5 to 11.25
        # min/design, which brackets the measured ~10 -- and
        # shared/tools_catalog.py and blueprints/tools.py render it to the
        # user. The OLD constants put those same 4 designs at 120 min
        # (300 * (120/120)**1.5 * 4/10), 2.7x the top of the band this
        # tool's own page promises, because a published ~30
        # min/TRAJECTORY rate was being applied per DESIGN.
        #
        # 40.0 is rounded DOWN from the 41.6 that run solves for. The
        # residual, the band containment and the baseline-vs-validator match
        # are all pinned by tests/test_pdb_preflight.py::
        # test_bindcraft_runtime_curve_reproduces_its_one_measured_run.
        runtime_base_min=40.0,       # 4 designs × ~10 min at the 120 aa anchor
        runtime_alpha=1.5,           # UNCHANGED, and still unmeasured: one run
                                     # is one target size, so it carries no
                                     # size-scaling evidence either way. The
                                     # AF2-multimer + ColabDesign-backprop
                                     # reasoning behind 1.5 is the same guess
                                     # it was before this re-anchor. The test
                                     # below does reject alpha>=2.0, but only
                                     # through the LEVEL at 115 aa with base
                                     # held at 40 -- that is not evidence of
                                     # bending, which needs a second size.
        runtime_baseline_designs=4,  # the form default: ``num_designs`` falls
                                     # back to "4" in
                                     # tools/bindcraft/__init__.py::validate
        cap_basis="literature",      # Pacesa 2025 default-settings examples
    ),
    gap=GapThresholds(
        warn_length=10,
        needs_fix_length=20,
        needs_fix_hotspot_distance=5,
    ),
)

_BOLTZGEN = ToolRules(
    slug="boltzgen",
    gpu="A100-40GB",                 # matches llm-pd infrastructure/modal/boltzgen_app.py:_GPU
    multi_chain_supported=True,      # Boltz-class models cofold multi-chain
    # VERIFIED on GPU 2026-08-05: real two-chain 4ZQK run via Modal returned
    # {A:115, B:106, C:55} in 430 s. include:/binding_types: are per-chain
    # LISTS upstream and the wrapper now builds them per chain.
    multi_chain_container_ready=True,
    hotspots_required=False,
    min_target_aa=30,
    size=SizeEnvelope(
        hard_cap_target_aa=600,      # Week 2: 400 → 600 (AF3-class headroom)
        soft_warn_target_aa=360,
        hard_cap_combined_aa=700,
        runtime_base_min=600.0,      # BoltzGen sampling ~5-10 min/design × 100
        # Not checked against a run. tools/boltzgen/__init__.py::build_payload
        # pins num_designs=200, and runtime_estimate_min(_BOLTZGEN, 115, 200)
        # returns 1150 min. The one recorded pilot at that pool
        # (docs/VALIDATION-LOG.md, boltzgen 2026-05-28, 4ZQK chain A = 115 aa)
        # ran ~82 min. It does not render today:
        # shared/pdb_preflight.py::_check_size_envelope estimates only when
        # num_designs is passed, and the boltzgen form sends none.
        # Recalibrate from a recorded run before passing one.
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
    # VERIFIED on GPU 2026-08-05: real two-chain 4ZQK run via Modal returned
    # {A:115, B:106, C:80} in 445 s. target.chains is a per-chain MAP upstream;
    # ensure_cif no longer filters to one chain and each chain gets its own
    # crop and hotspot list.
    multi_chain_container_ready=True,
    hotspots_required=True,          # pxdesign requires >=1 hotspot
    min_target_aa=30,
    size=SizeEnvelope(
        # pxdesign shares BindCraft's AF2 memory regime (AF2 Initial Guess
        # validation). BindCraft (Pacesa 2025): target practically limited
        # to ~600 aa; an 80GB card fits ~950 aa of target + binder.
        hard_cap_target_aa=600,      # BindCraft ~600 aa practical target ceiling
        soft_warn_target_aa=360,
        hard_cap_combined_aa=950,    # BindCraft: ~950 aa (target + binder) on 80GB
        # RE-ANCHORED (2026-09-18). The 300.0 this replaces carried the
        # comment "AF2-IG validation per design" and was the same published
        # BindCraft figure that _BINDCRAFT above carried until #314 --
        # a per-TRAJECTORY rate applied per DESIGN. It implied 37.5 min for ONE design at the
        # 120 aa anchor and quoted 206 min for the run measured below, which
        # took 7.7. Every pxdesign run in docs/VALIDATION-LOG.md that
        # SUCCEEDED finished inside 25 min. That is a property of the
        # successes and not of the tool: two FAIL rows are also on record,
        # and the one of them carrying a runtime, a mini_pilot at 4517
        # GPU-s, ran 75.3 min before dying at 78.5%. Nothing below models a
        # failure, so a curve fitted to the successes cannot bound one.
        #
        # THE ANCHOR is the 2026-05-26 pilot, job 79228f03 in
        # docs/VALIDATION-LOG.md: 462 GPU-s (7.7 min) for num_designs=5
        # against 1HEW chain A. That chain is 129 residues -- counted off
        # static/example/1HEW.pdb, not asserted -- so the size correction is
        # 1.10 and the anchor is nearly independent of the exponent below.
        # It is the closest of the three runs to this curve's own default
        # operating point (5 designs against a baseline of 8). Solved
        # exactly it gives 11.21.
        #
        # 11.2 IS THAT SOLVED VALUE and it is the whole basis. The other
        # two runs do NOT corroborate it and are not used to set it. Backing
        # a base out of either means dividing by its size factor, which
        # raises the target ratio to the exponent below -- and that exponent
        # is unmeasured. Doing it anyway gives 35.51 from job 816fc4a9 (504
        # GPU-s, 2 designs, 4ZQK chain A, ~115 aa) and 1.44 from the
        # 25-design worked example in tools/pxdesign/example/result.json
        # (1380 GPU-s, runtime_minutes 23.0, ~420 aa over two chains). A
        # 25-fold spread is not a corroboration, and averaging into it would
        # be false precision resting on an exponent this file cannot measure.
        #
        # THE THREE RUNS DO NOT FIT THIS CURVE and no value of base makes
        # them. Their per-design rates are 4.20, 1.54 and 0.92 min at
        # n=2, 5 and 25 -- a fixed overhead this estimator has no term for.
        # Choosing base is therefore choosing WHERE to be right, and this
        # one is right at the anchor. TWO RESIDUALS FOLLOW, disclosed rather
        # than fixed, both pinned by tests/test_pdb_preflight.py::
        # test_pxdesign_runtime_curve_anchors_on_one_run_and_discloses_the_rest:
        #   - at n=2 the estimate falls to the max(5.0) floor in
        #     runtime_estimate_min against a measured 8.4 min, ~40% LOW.
        #   - on the 420 aa worked example the estimate is ~178 min against
        #     a measured 23.0, ~8x HIGH. That one is the exponent's doing
        #     and no base fixes it; see runtime_alpha below.
        # Both are far smaller than what they replace: at 300.0 the same two
        # runs were quoted 745% and 20675% high.
        #
        # RECONCILED SEPARATELY, and still not this curve's business: the
        # catalog used to advertise a "30 to 60 min" pilot run, which all
        # three runs above finished below. It now reads "8 to 25 min" on
        # every surface, moved on its own evidence in commit 8a20c46 and
        # pinned by tests/test_pxdesign_runtime_band.py. That band is
        # advertised copy for one preset; this is a per-request cost gate
        # over the whole input domain, so the two are allowed to diverge
        # and the residuals above are where they do.
        runtime_base_min=11.2,       # job 79228f03 solved at the 120 aa anchor
        runtime_alpha=1.3,           # UNCHANGED and still unmeasured. The
                                     # three runs cannot calibrate it: their
                                     # target sizes are confounded with their
                                     # design counts (the largest target is
                                     # also the 25-design run), and a
                                     # log-linear fit through all three
                                     # returns alpha = -0.87 -- runtime
                                     # FALLING as the target grows. That is
                                     # the confounding, not a measurement,
                                     # and it is why this stays on the
                                     # AF2-IG reasoning it arrived with. The
                                     # test named above recomputes that fit.
                                     # This exponent, not the base, is what
                                     # quotes the worked example ~8x high:
                                     # it stretches a 3.5x target ratio to
                                     # 5.1x. Pinning it needs two target
                                     # sizes at one design count and no two
                                     # pxdesign runs on record share one.
        runtime_baseline_designs=8,  # the form default: ``num_designs``
                                     # falls back to "8" in
                                     # tools/pxdesign/__init__.py::validate
                                     # and in the number input in
                                     # templates/tools/pxdesign_form.html.
                                     # Both are checked by the test named
                                     # above; bindcraft's had drifted off
                                     # its form silently, pxdesign's had not.
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
        # measured 415 at 38.9% of the card, the worst VRAM extrapolation error
        # this tool has shown us was 11% LOW at a 1.6x step, and even a 100%
        # model error at 500 aa still fits. (That 11% is the power-law miss,
        # which is the form this comment argues in two paragraphs above:
        # fitted to 130 and 260 aa it predicts 22,564 MB at 415 against a
        # measured 25,457. A straight line through the same two points is the
        # friendlier miss at 8% low, and quoting that one instead would be
        # picking the flattering number. Both under-read, which is the
        # direction that bills. And VRAM is load-bearing on that sentence
        # rather than decoration: 11% is NOT this tool's worst extrapolation
        # error of any kind. Refit RUNTIME over the same two points across the
        # same 1.6x step and it misses the 415 aa shard by 20.35% low — block
        # (4) of tests/test_pdb_preflight.py::test_the_proteina_cap_is_
        # traceable_to_three_post_prealloc_shards runs exactly that refit. The
        # cap's headroom is a VRAM argument, so the VRAM figure is the one that
        # belongs here; it just may not be read as a bound on the tool at
        # large.)
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
        # 576 s. Both are recorded here as completed 8-design protein_binder
        # shards — the 359 s one by the immediately preceding revision of this
        # same comment (at ce609c3: "one protein_binder shard, 8 designs, at
        # 130 residues over 2 chains, completed in 359 s"), the 576 s one by
        # the table above. So "what the 359 s run did is recorded nowhere in
        # this repo", which stood here, was false about the file it was
        # written in. What separates them is the ALLOCATOR REGIME: the 359 s
        # wall-clock belongs to the PREALLOCATION-ON shard that read 67,570 MB,
        # and all three points above were taken with preallocation DISABLED.
        # Forty lines up, this comment says those two regimes must never be
        # mixed; that applies to their wall-clocks as much as to their VRAM.
        # The regime is a CANDIDATE for the gap and not a diagnosis of it:
        # preallocation-off makes JAX allocate on demand instead of reserving
        # up front, which is work moved into the run, but nobody has measured
        # that it accounts for ~60%. It is the difference in run conditions
        # this repo does record; do not upgrade it to the cause, and do not
        # replace it with a fresh claim that nothing anywhere explains the gap
        # — that assertion is what stood here, and it was wrong.
        #
        # WHICH FIGURE THE ESTIMATOR USES DOES NOT DEPEND ON CLOSING THAT.
        # 576 s is the reading taken under the allocator settings production
        # runs today (_ALLOCATOR_ENV in tools/proteina/run_pipeline.py), so it
        # is the one of the two that describes what a user's shard will do.
        # The 359 s figure is not used for anything.
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
    # The only False in this column, and the only one there is evidence for.
    # proteina's container does not run pipeline_normalize at all — grep
    # tools/proteina/run_pipeline.py — it selects by CA and matches
    # missing_hotspots against that, so a hotspot whose residue is merely
    # missing an O still resolves. Verified by executing that module:
    # pdb_ca_residues -> select_residues(A1-40) contains ("A", 30) and
    # missing_hotspots(selected, ["A30"]) == [] for a residue with N/CA/C and
    # no O. Trunk let those runs through and they succeeded.
    hotspot_needs_full_backbone=False,
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

    Form: ``(base × num_designs/baseline_designs + fixed) ×
    (aa/120)^alpha``, where ``fixed`` is ``runtime_fixed_min``. That term
    is 0.0 on every envelope that does not set it, collapsing the form back
    to the purely multiplicative ``base × (aa/120)^alpha ×
    (num_designs / baseline_designs)`` those tools have always used.

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
        # Degenerate input, not an estimate. The two halves of this guard
        # are NOT equally reachable, and the difference matters enough to
        # write down. ``num_designs <= 0`` is dead for anything the app
        # serves: the sole caller,
        # shared/pdb_preflight.py::_check_size_envelope, only calls this
        # under ``if num_designs is not None and num_designs > 0``.
        # ``target_aa <= 0`` is NOT dead. _selection_residue_count returns
        # ``total = 0`` -- a count, not None -- when a declared contig
        # selects no residues, _check_size_envelope passes that straight
        # through as target_aa, and the min-residue floor that would
        # otherwise catch it deliberately tests the whole-chain ``kept``
        # instead (see the comment above that call). So a contig naming a
        # range that matches nothing lands here.
        #
        # It returns the per-design anchor bare and deliberately does NOT
        # add runtime_fixed_min. That is a choice, not a derivation: an
        # input describing no job is not a shorter job, so neither term is
        # meaningful and a fixed stage attributed to zero designs would
        # only look more authoritative. For rfdiffusion, the one two-term
        # envelope, the bare anchor is the wrong shape whatever it returns.
        # Advisory copy on an input that is already nonsense; a second
        # branch would not make it less so.
        return float(rules.size.runtime_base_min)
    size_factor = (target_aa / 120.0) ** rules.size.runtime_alpha
    design_factor = num_designs / rules.size.runtime_baseline_designs
    est = rules.size.runtime_base_min * size_factor * design_factor
    # Added as a separate term, in this order, on purpose. Folding it into
    # the product as ``(fixed + base * design_factor) * size_factor`` is the
    # same arithmetic but RE-ASSOCIATES the multiplication, and that is not
    # free: executed, it moves the last bit of 36 of the 405 (tool, target
    # size, design count) points the guard below checks, on tools whose
    # fixed term is 0.0 and which this change is not supposed to touch at
    # all. Written this way ``+ 0.0`` is an exact IEEE-754 identity, so
    # those envelopes return what they returned before by construction
    # rather than by sampling. tests/test_pdb_preflight.py::
    # test_zero_fixed_term_leaves_every_other_envelope_bit_identical is the
    # check.
    est += rules.size.runtime_fixed_min * size_factor
    return max(5.0, est)
