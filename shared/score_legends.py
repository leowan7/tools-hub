"""Per-tool score legends for the candidate-table column headers.

Where ``shared.metric_glossary`` answers "what is this metric called and
where did it come from?", this module answers a narrower question with
sharper thresholds: "given THIS tool's outputs, what counts as a good
column value, and what counts as excellent?".

Lookup is keyed on ``(tool_slug, column_key)`` so the same metric label
can carry different thresholds across tools (e.g. ipTM means slightly
different things between a BindCraft AF2-IG re-score and a Boltz-2
calibrated cofold). When no tool-specific entry exists, the helper
returns ``None`` and the candidate_table macro falls back to the generic
``metric_glossary`` tooltip.

The macro reads ``score_legends_for(tool_slug)`` via a Jinja global
registered in ``app.py``; templates do not import this module directly.
"""

from __future__ import annotations

from typing import NamedTuple, NotRequired, Optional, TypedDict

from shared import metric_glossary as _metric_glossary


class Legend(TypedDict):
    # OPTIONAL, because a legend is allowed to have no bar. Nothing renders
    # these — ``legend_text`` and ``email_caption`` read ``explanation`` and
    # ``caveat`` only — so they are the module's record of what the numbers
    # mean, and a wrong one sits inert until someone wires it up.
    #
    # boltzgen's ipTM omits them. It carried 0.7/0.8, which are the Boltz-2
    # COFOLD bars, against a number a generator confidence head produces from
    # its own output. On the audited 100-design replicate
    # `design_to_target_iptm` spans 0.084-0.583, 0/100 over 0.70. That is the
    # largest capture of THIS column on a multi-chain target; the peptide
    # trees below carry it too, on a single-chain one.
    #
    # CITE THAT, NOT THE FAMILIAR "460 designs, max 0.650". The 460 figure is
    # BoltzGen's bare `iptm`, averaged over EVERY chain pair, so on a
    # homodimer target it carries the target's own crystal interface;
    # boltzgen-workspace/aglyco-fc-vhh/modal_design.py records it as reading
    # "~2x high". Quoting it here would repeat, in the justification, the
    # exact wrong-quantity error this entry exists to correct.
    #
    # AND SCOPE THE CLAIM, do not universalise it. The same self-hosted
    # pipeline on peptide-anything reaches 0.777, with 16 of 36 designs over
    # 0.70 (boltzgen-workspace/mdm2-peptide). The HOSTED Boltz API clears 0.70
    # routinely, up to 0.983 — a different service, and the workspace does not
    # claim to know whether its `iptm` is the same quantity, so treat it as a
    # separate population rather than as a refutation.
    #
    # The bar comes off on the SCALE argument, which holds regardless of
    # reach: 0.70 is calibrated on a cofold, and the audited run's refold has
    # no target in it. A field named ``good`` holding a bar from a different
    # measurement is not a harmless copy, it is the same claim the
    # explanation was making, kept in data. Omit rather than invent a
    # replacement — no run here pairs this number against a cofold on the
    # same designs, so there is nothing to calibrate a bar from.
    good: NotRequired[float]
    excellent: NotRequired[float]
    direction: str  # "higher_is_better" or "lower_is_better"

    # ONE LINE. It is not only the column tooltip: shared/email.py puts it
    # verbatim into the job-completion email as ``top_score_caption``, which
    # templates/email/job_complete.html renders as a 13px line under a single
    # number. That template used to describe the whole slot as a "1-line
    # interpretation of the top score" and this comment repeated it; the slot
    # is one line about the metric PLUS, on a multi-chain job, the ``caveat``
    # below. It is ``explanation`` that is the one line, which is why the
    # tight ceiling is on this field. See ``caveat`` for what belongs
    # elsewhere, and tests/test_job_complete_email_caption.py, which bounds
    # this field AND the rendered caption, separately.
    explanation: str

    # Optional, and NOT part of the one line. A note that is true of a stored
    # result rather than of the metric — "an older run recorded this
    # differently". Every view that renders whatever a job SAVED needs it:
    # components/candidate_table.html via ``legend_text``, and the
    # job-completion email via ``email_caption``.
    #
    # THE EMAIL IS NOT EXEMPT, AND THE COMMENT THAT SAID IT WAS COST A ROUND.
    # It read "the completion email does not, and cannot: it is sent by
    # shared/jobs.complete_job at the terminal transition, so its number always
    # comes from the container running now". complete_job is not only the
    # webhook's caller. THREE paths finalize a job — and send this mail — about
    # a result the app read back out of storage long after the run:
    # shared/jobs.timeout_stuck_job (via shared/job_recovery, which rebuilds
    # result.candidates from a Storage listing), the inline poll in
    # blueprints/jobs.job_status (fires whenever the user next opens the page),
    # and scripts/finalize_stuck_job.py (an operator, days later). All three
    # were driven with the transport captured; each mailed
    # "the binder-to-target interface", uncaveated, for a job submitted before
    # the deploy that made that true.
    #
    # What is real is the LENGTH: ``explanation`` is a one-line slot for 32
    # legends, and 380 characters of era note in it would be wrong on the 31
    # that do not need one. So the split stays and the email opts in per job —
    # see ``email_caption``.
    caveat: NotRequired[str]


# (tool_slug, column_key) -> Legend.
# The column_key matches the score key in candidate.scores[...] (the same
# keys the candidate_table macro iterates over). Numeric thresholds are
# chosen to be defensible across the major papers, not aspirational.
SCORE_LEGENDS: dict[tuple[str, str], Legend] = {
    # ── ProteinMPNN (sequence design) ─────────────────────────────────
    ("mpnn", "recovery"): {
        "good": 0.4,
        "excellent": 0.6,
        "direction": "higher_is_better",
        "explanation": (
            "Recovery is the fraction of native residues recovered. "
            "Above 0.4 is a usable design; above 0.6 is excellent."
        ),
    },
    ("mpnn", "score"): {
        "good": 1.2,
        "excellent": 1.0,
        "direction": "lower_is_better",
        "explanation": (
            "MPNN negative log likelihood per residue. Lower means the "
            "designed sequence is more confident on the input backbone. "
            "Below 1.2 is usable; below 1.0 is excellent."
        ),
    },

    # ── AlphaFold2 (single-prediction fold) ───────────────────────────
    ("af2", "plddt"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is AF2 confidence. Above 80 is confidently folded; "
            "above 90 is high confidence."
        ),
    },
    ("af2", "ptm"): {
        "good": 0.7,
        "excellent": 0.8,
        "direction": "higher_is_better",
        "explanation": (
            "pTM is global fold confidence. Above 0.7 is a credible "
            "model; above 0.8 is strong."
        ),
    },
    ("af2", "iptm"): {
        "good": 0.6,
        "excellent": 0.75,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM predicts complex confidence. Above 0.6 is a "
            "plausible interface; above 0.75 is strong."
        ),
    },

    # ── ColabFold and ESMFold reuse AF2-style pLDDT scale ────────────
    ("colabfold", "plddt"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is AF2 confidence. Above 80 is confidently folded; "
            "above 90 is high confidence."
        ),
    },
    ("colabfold", "ptm"): {
        "good": 0.7,
        "excellent": 0.8,
        "direction": "higher_is_better",
        "explanation": (
            "pTM is global fold confidence. Above 0.7 is a credible "
            "model; above 0.8 is strong."
        ),
    },
    ("colabfold", "iptm"): {
        "good": 0.6,
        "excellent": 0.75,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM predicts complex confidence. Above 0.6 is a "
            "plausible interface; above 0.75 is strong."
        ),
    },
    ("esmfold", "plddt"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is ESMFold confidence (AF2 scale). Above 80 is "
            "confidently folded; above 90 is high confidence."
        ),
    },

    # ── RFdiffusion (backbone diffusion + MPNN + AF2 multimer rescore)
    ("rfdiffusion", "ipTM"): {
        "good": 0.65,
        "excellent": 0.75,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM from the AF2 multimer re-score. Above 0.65 "
            "is a credible binder; above 0.75 is strong."
        ),
    },
    ("rfdiffusion", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is AF2 confidence on the designed binder. Above 80 "
            "is confidently folded; above 90 is high confidence."
        ),
    },
    ("rfdiffusion", "i_pAE"): {
        "good": 10.0,
        "excellent": 6.0,
        "direction": "lower_is_better",
        "explanation": (
            "Interface pAE is AF2's expected positional error across "
            "the binder-target interface. Below 10 angstroms passes; "
            "below 6 is strong."
        ),
    },
    ("rfdiffusion", "RMSD"): {
        "good": 1.5,
        "excellent": 1.0,
        "direction": "lower_is_better",
        "explanation": (
            "RMSD against the design target. Below 1.5 angstroms is "
            "good agreement; below 1.0 is excellent."
        ),
    },

    # ── BindCraft (free hallucination + AF2-IG filters) ──────────────
    ("bindcraft", "ipTM"): {
        "good": 0.75,
        "excellent": 0.85,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM predicts binding likelihood. Above 0.75 is "
            "a credible binder; above 0.85 is a strong candidate."
        ),
    },
    ("bindcraft", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is AF2 confidence on the designed binder. Above 80 "
            "is confidently folded; above 90 is high confidence."
        ),
    },
    ("bindcraft", "RMSD"): {
        "good": 1.5,
        "excellent": 1.0,
        "direction": "lower_is_better",
        "explanation": (
            "Refolding RMSD against the designed backbone. Below 1.5 "
            "angstroms is good agreement; below 1.0 is excellent."
        ),
    },
    ("bindcraft", "shape_complementarity"): {
        "good": 0.65,
        "excellent": 0.75,
        "direction": "higher_is_better",
        "explanation": (
            "Shape complementarity at the interface. Above 0.65 is "
            "antibody-grade fit; above 0.75 is excellent."
        ),
    },
    ("bindcraft", "SAP"): {
        "good": 10,
        "excellent": 5,
        "direction": "lower_is_better",
        "explanation": (
            "Spatial Aggregation Propensity. Below 10 is acceptable; "
            "below 5 is favourable for biomanufacturing."
        ),
    },

    # ── RFantibody (antibody binder design) ──────────────────────────
    ("rfantibody", "ipAE"): {
        "good": 10.0,
        "excellent": 6.0,
        "direction": "lower_is_better",
        "explanation": (
            "Interaction pAE (binder-target). Below 10 angstroms is "
            "the RFantibody pass bar; below 6 is strong."
        ),
    },
    ("rfantibody", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "Predicted local confidence on the designed antibody. "
            "Above 80 is confidently folded; above 90 is high confidence."
        ),
    },
    ("rfantibody", "pAE"): {
        "good": 5.0,
        "excellent": 3.0,
        "direction": "lower_is_better",
        "explanation": (
            "Global predicted aligned error. Below 5 angstroms is good "
            "complex geometry; below 3 is excellent."
        ),
    },

    # ── PXDesign (backbone hallucination + AF2-IG re-score) ──────────
    ("pxdesign", "ipTM"): {
        "good": 0.75,
        "excellent": 0.85,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM from AF2-IG re-scoring. Above 0.75 is a "
            "credible binder; above 0.85 is strong."
        ),
    },
    ("pxdesign", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT is AF2 confidence on the designed binder. Above 80 "
            "is confidently folded; above 90 is high confidence."
        ),
    },
    # NOT a gate leg, and the reason is worth the space. What pxdesign stores
    # under "pAE" is the INTERFACE PAE, not a global one: the container takes
    # the first key present from unscaled_i_pae, unscaled_ipae, unscaled_pae,
    # af2_unscaled_ipae, af2_unscaled_i_pae, af2_ipae, af2_pae, ipae, pae,
    # i_pae, mean_pae (docker/pxdesign/run_pipeline.py, parse_summary_csv), and
    # its own comment there says the af2_* forms are the [0,1] NORMALISED
    # shape. So one column arrives on two scales, and a 0-1 reading clears an
    # Angstrom bar unconditionally -- 0.42 would read as better than excellent.
    # A column that can be either scale cannot answer a single bar, and unlike
    # pLDDT no value-range test separates them: a 0.42 A interface PAE and a
    # 0.42 normalised one are both plausible readings.
    ("pxdesign", "pAE"): {
        "good": 5.0,
        "excellent": 3.0,
        "direction": "lower_is_better",
        "explanation": (
            "Global predicted aligned error. Below 5 angstroms is good "
            "complex geometry; below 3 is excellent."
        ),
    },

    # ── BoltzGen (all-atom binder generator + refold check) ──────────
    ("boltzgen", "ipTM"): {
        # NO ``good``/``excellent`` — see the Legend TypedDict. They were 0.7
        # and 0.8, copied from the boltz2 cofold legend directly below, and
        # this number is not on that scale: 0.70 is a COFOLD bar and the
        # audited run's refold folds the binder alone, so it has no interface
        # to score. That is the argument, and it does not depend on reach.
        #
        # On reach, scoped and on THIS column: the audited n100 gives
        # `design_to_target_iptm` 0.084-0.583, 0/100 over 0.70. NOT a
        # property of the metric — peptide-anything reaches 0.777 on the same
        # pipeline, and the hosted Boltz API reaches 0.983. Do not pool them,
        # and do not reach for the older "460 designs, max 0.650": that is
        # the bare all-chain-pair `iptm`, contaminated by the target's own
        # interface (see the Legend TypedDict).
        #
        # For scale, the same campaign's designs re-scored on a real Boltz-2
        # cofold span 0.166-0.806 on `binder_to_target` (29 rows, 1 over
        # 0.70) — the per-chain-pair column feld1/13_boltz_cofold.py exists to
        # read, since X:A and X:B are binder-to-target pairs with, in its
        # words, "no target-internal contamination possible". It is the better
        # of those two pairs, so it sits ~0.03 above their mean; that is well
        # inside the gap to 0.70 and does not change the conclusion. Its
        # complex-wide `cofold_iptm` sibling reads 0.263-0.852 and is the
        # wrong column for the same reason the 460 figure is.
        # llm-proteinDesigner fix/boltzgen-unreachable-gate removes the
        # matching container-side gate leg for the same reason.
        "direction": "higher_is_better",
        # llm-proteinDesigner#18 (squash-merged as 311c29f; the Modal deploy
        # for that SHA is green) puts `design_to_target_iptm` first in
        # IPTM_KEYS, so the deployed container reports the binder-to-target
        # interface and this may finally say so.
        #
        # It carries the pre-deploy caveat because a results page renders
        # whatever the JOB STORED, and multi-chain runs from before the deploy
        # still hold the complex-wide value. THIS LEGEND IS WHERE THAT CAVEAT
        # LIVES NOW — see MULTICHAIN_IPTM_UNRELIABLE_TOOLS below for why it is
        # not the banner. The two move together: if boltzgen ever goes back
        # into that set, this text must stop calling the number
        # binder-to-target in the same commit, or the tooltip and the banner
        # directly above the same column contradict each other on one screen.
        #
        # BOTH HALVES OF THE BANNER'S CAVEAT HAVE TO BE HERE, and the first
        # attempt moved only one. The banner said, in bold, "these designs are
        # ALSO RANKED BY IT, so both the values and the ORDER of this table
        # should be treated as indicative only". Only the value half arrived,
        # and the words "rank" and "order" then appeared nowhere on a
        # pre-deploy boltzgen results page. boltzgen ranks on ipTM
        # (shared/result_columns.py), and the order is load-bearing rather than
        # cosmetic: aggregate_campaign_candidates / aggregate_target_candidates
        # sort and then truncate at limit=300, so past 300 candidates the ipTM
        # order decides which designs are visible at all. Saying the number may
        # be wrong while saying nothing about the order it produced is the
        # half-measure the banner existed to avoid.
        #
        # IT GOES IN ``caveat``, NOT IN ``explanation``, AND THE REASON IS
        # LENGTH AND SCOPE — NOT THAT THE EMAIL IS SAFE FROM IT. Written into
        # ``explanation`` it took the string from 161 characters (the longest
        # of the other 31 legends) to 496, shared/email.py hands ``explanation``
        # to the job-completion email, and every BoltzGen completion mail then
        # said "treat the order of the table as indicative" — in a message that
        # shows ONE number for ONE design and contains no table — including on
        # single-chain runs, because a legend keyed on (tool, column) cannot
        # see the chains. Those are two real defects: furniture the message
        # does not have, and a multi-chain note on a single-chain run.
        #
        # THE FIX FOR THEM WAS NOT "the email never shows a caveat", AND THAT
        # WRONG COMMENT IS WHAT ROUND 4 SHIPPED. It claimed the mail can only
        # describe a run that just finished, so the caveat's antecedent could
        # never hold there. False: shared/jobs.timeout_stuck_job,
        # blueprints/jobs.job_status's inline poll and
        # scripts/finalize_stuck_job.py all call complete_job with a result the
        # app read back out of Storage, and complete_job sends this mail. I
        # drove all three with the transport captured; each one mailed
        # "the binder-to-target interface" with no caveat about a job submitted
        # 2026-08-01. The email needs the caveat for exactly the same reason
        # the results page does.
        #
        # So the caveat is opt-in per job rather than absent: ``email_caption``
        # appends it when THAT JOB's target names more than one chain, which is
        # the condition the caveat's own first clause states and which the
        # email — unlike the legend — can evaluate, because it has the job.
        #
        # Truncating in the email instead was considered and rejected: every
        # other legend is "definition. threshold.", so "first sentence only"
        # would drop the actionable half from all 31 of them to fix one. So was
        # paraphrasing the caveat into a shorter email-only clause — that is a
        # second string saying the same thing, and every regression in this
        # area so far has been two copies of one claim drifting apart.
        "explanation": (
            "Interface pTM, the binder-to-target interface as BoltzGen's "
            "generator scores it. Not on the Boltz-2 cofold scale, so 0.7 "
            "does not apply. Rank on it, then re-fold a shortlist to confirm."
        ),
        # Deixis-free for the same reason the banner is
        # (components/multichain_iptm_notice.html): this renders in a column
        # header tooltip AND in a pooled per-row tooltip, and in the pooled
        # table the visible order is by percentile, not by this number.
        #
        # AND IT IS NOT CHAIN-GATED IN THE TABLE, deliberately, although the
        # EMAIL gates it (``email_caption``). The tooltip therefore shows on
        # single-chain BoltzGen tables too, where the antecedent is false —
        # over-warning, which this repo's own rule ("a caveat shown to everyone
        # is a caveat nobody reads") argues against. It stays because the gate
        # would have to come from a chain the table does not have. A job page
        # and a campaign page know the chain THEIR run used
        # (job.inputs / campaign.params), but the pooled target page — the one
        # view where a pre-deploy row sits beside a post-deploy one, and so the
        # one that most needs the caveat — has only ``target.target_chain``,
        # which is a default the launch form overrides per run
        # ("Overrides the target default for these runs only",
        # templates/targets/launch.html) and which shared/target_results.py
        # does not record per row. Gating on it would HIDE the caveat from rows
        # that need it, to remove hover-only noise from rows that do not. The
        # email has no such problem: it is about exactly one job and reads that
        # job's own inputs.
        "caveat": (
            "On a multi-chain target the binder-to-target reading holds "
            "for runs after the August 2026 container update; an older run "
            "stored a complex-wide value instead, inflated by the target's "
            "own chain-chain interface, and the stored result does not "
            "record which it is. Designs are ranked on this number, so for "
            "an older multi-chain run treat any ordering derived from it "
            "as indicative too."
        ),
    },
    # These two DO keep their bars, and the reason is the point of the split:
    # both are measured on BoltzGen's refold, which folds the binder on its
    # own. So each describes the binder and nothing else, which is the
    # quantity 80 and 1.5 A were calibrated on. Only ipTM lacked a reading of
    # its own kind — a fold with no target in it has no interface to score.
    ("boltzgen", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "pLDDT of the binder, refolded on its own from its designed "
            "sequence. Above 80 is confidently folded; above 90 is high "
            "confidence."
        ),
    },
    ("boltzgen", "refolding_rmsd"): {
        "good": 1.5,
        "excellent": 1.0,
        "direction": "lower_is_better",
        "explanation": (
            "Backbone RMSD between the designed binder and BoltzGen's "
            "refold of its sequence. Below 1.5 angstroms is "
            "self-consistent; below 1.0 is excellent."
        ),
    },

    # ── Boltz-2 (calibrated cofold validator) ────────────────────────
    ("boltz2", "ipTM"): {
        "good": 0.7,
        "excellent": 0.8,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM from Boltz-2's calibrated confidence head. "
            "Above 0.7 is a credible binder; above 0.8 is strong."
        ),
    },
    ("boltz2", "pTM"): {
        "good": 0.7,
        "excellent": 0.8,
        "direction": "higher_is_better",
        "explanation": (
            "pTM is global complex confidence. Above 0.7 is a credible "
            "model; above 0.8 is strong."
        ),
    },
    ("boltz2", "pLDDT"): {
        "good": 80,
        "excellent": 90,
        "direction": "higher_is_better",
        "explanation": (
            "Complex pLDDT (rescaled to the AF2 0-100 range). Above 80 "
            "is confidently folded; above 90 is high confidence."
        ),
    },
    ("boltz2", "n_hotspot_contacts"): {
        "good": 4,
        "excellent": 5,
        "direction": "higher_is_better",
        "explanation": (
            "Number of user-requested hotspots contacted by the binder. "
            "Above 4 of 7 is the strict-pass bar; 5 or more is strong."
        ),
    },
    # ── ESMFold2-design (gradient design + critic re-score) ──────────
    # HYPHEN, NOT UNDERSCORE. The registered slug is "esmfold2-design"
    # (tools/esmfold2_design/__init__.py:240) even though the package
    # directory is esmfold2_design, and this entry shipped keyed on the
    # directory name. Nothing raised: an unknown tool simply has no legend and
    # no bar, so the feature was inert for this tool while its own test passed
    # over the dead key. shared/tool_meta.py:4 records the same trap costing a
    # PILOT card that silently did not render. tests/test_derived_verdicts.py
    # now asserts every tool key here is in tools.base._REGISTRY.
    #
    # This is a TOOLTIP ONLY. esmfold2-design declares no gate columns; see the
    # note in GATE_COLUMNS for why its bar cannot be a uniform conjunction.
    # The number is the pipeline's own STRICT_IPTM, raised there from 0.55 on
    # 2026-06-03 after three runs returned 0.83 / 0.83 / 0.95.
    ("esmfold2-design", "ipTM"): {
        "good": 0.75,
        "excellent": 0.85,
        "direction": "higher_is_better",
        "explanation": (
            "Interface pTM from the ESMFold2 critic re-score. Above 0.75 "
            "is a credible designed interface; above 0.85 is strong."
        ),
    },

    ("iggm", "epitope_contacts"): {
        "good": 3,
        "excellent": 5,
        "direction": "higher_is_better",
        "explanation": (
            "Number of your requested epitope residues the designed "
            "antibody contacts. More engagement means the antibody is "
            "docking where you asked."
        ),
    },
}


def get_legend(tool_slug: str, column_key: str) -> Optional[Legend]:
    """Return the legend for ``(tool_slug, column_key)`` or None."""
    if not tool_slug or not column_key:
        return None
    return SCORE_LEGENDS.get((tool_slug, column_key))


def legend_text(legend: Optional[Legend]) -> str:
    """The whole legend, as a reader of a STORED results table needs it.

    ``explanation`` plus ``caveat``. Registered as a Jinja global in app.py
    and called from components/candidate_table.html in all three places a
    legend is shown — the column header's ``data-tooltip`` and ``title``, and
    the per-row Score cell in multi-tool mode — so a caveat cannot arrive on
    two surfaces out of three.

    The other consumer of a stored result is the job-completion email, and it
    calls ``email_caption`` rather than this, because it knows which job it is
    about and a table does not.
    """
    if not isinstance(legend, dict):
        return ""
    parts = [str(legend.get("explanation") or ""), str(legend.get("caveat") or "")]
    return " ".join(p for p in (part.strip() for part in parts) if p)


def names_multiple_chains(target_chain) -> bool:  # noqa: ANN001
    """Does this ``target_chain`` field name more than one distinct chain?

    Both separators are accepted and the value is de-duplicated, matching
    ``tools.base.parse_target_chains`` and every other consumer of the field:
    ``"A,B"`` and ``"A B"`` are two chains, ``"A,A"`` is one. Lives here rather
    than in ``tools.base`` so ``shared.email`` — which is sent from workers
    that have not imported the tool adapters — can ask the question without
    importing the registry.
    """
    if isinstance(target_chain, (list, tuple, set)):
        target_chain = ",".join(str(c) for c in target_chain)
    chains = [c for c in str(target_chain or "").replace(",", " ").split() if c]
    return len(set(chains)) > 1


def email_caption(legend: Optional[Legend], target_chain) -> str:  # noqa: ANN001
    """The legend as the JOB-COMPLETION EMAIL needs it, for one job.

    ``explanation`` always; plus ``caveat`` when this job's target names more
    than one chain.

    WHY THE EMAIL GETS THE CAVEAT AT ALL. The mail is not only sent about a run
    that just finished. ``shared/jobs.complete_job`` — which sends it — is
    called by ``timeout_stuck_job`` (whose ``shared/job_recovery`` rebuilds
    ``result.candidates`` from a Storage listing), by the inline poll in
    ``blueprints/jobs.job_status`` (whenever the user next opens the page), and
    by ``scripts/finalize_stuck_job.py`` (by hand, days later). All three were
    driven with the transport captured before this function existed, and all
    three mailed a pre-deploy BoltzGen result described as
    "the binder-to-target interface" with no caveat. A caveat is about what a
    STORED result may hold, and these mails are about stored results.

    WHY THE CHAIN GATE. The caveat's own first clause is "On a multi-chain
    target …", so on a single-chain run it is a conditional with a false
    antecedent: true, but noise — and a caveat shown to everyone is a caveat
    nobody reads (the rule this repo pins at
    tests/test_multichain_iptm_notice.py). The LEGEND cannot apply that gate,
    because it is keyed on ``(tool, column)`` and never sees a job; the email
    can, because ``job.inputs`` carries the chain the run was submitted with.
    That is the whole difference between the two consumers, and it is why this
    is a second function rather than an argument to ``legend_text``.

    The caveat is appended VERBATIM rather than paraphrased into something
    shorter. A shorter email-only wording is a second copy of one claim, and
    two copies drifting apart is what produced the last three defects here.
    """
    if not isinstance(legend, dict):
        return ""
    caption = str(legend.get("explanation") or "").strip()
    caveat = str(legend.get("caveat") or "").strip()
    if caveat and names_multiple_chains(target_chain):
        caption = f"{caption} {caveat}".strip()
    return caption


def score_legends_for(tool_slug: str) -> dict[str, Legend]:
    """Return ``{column_key: Legend}`` for one tool. Empty dict on miss.

    Templates call this via the Jinja global registered in ``app.py``;
    the dict shape lets a template do ``legends.get(col)`` without ever
    importing the module.
    """
    if not tool_slug:
        return {}
    return {
        col: legend
        for (ts, col), legend in SCORE_LEGENDS.items()
        if ts == tool_slug
    }


# ---------------------------------------------------------------------------
# Multi-chain ipTM is not comparable
# ---------------------------------------------------------------------------
#
# ipTM is computed over the interfaces of the WHOLE COMPLEX, not the
# binder-to-target pair alone. On a single-chain target that is harmless — the
# only interface in the complex is the one you designed. On a multi-chain
# target it is not: the target's own chain-chain interface is large and well
# formed whatever the binder does, so it holds the number up almost
# independently of binder quality, and a mediocre binder can rank first
# carrying a plausible-looking score.
#
# DO NOT state the reduction more precisely than this repo can support. This
# comment used to open "a MAX over residues, not a mean", and four pipeline
# files here say the opposite in as many words — interface-pTM "averaged over
# EVERY chain pair" (tools/af2, tools/colabfold, tools/esmfold2_design and
# tools/proteina run_pipeline.py — named without line numbers on purpose; the
# proteina one was cited as :1787 and is now at :3688), all of them describing
# the incident where 460 boltzgen designs were scored on it. The conclusion above survives
# either reduction. The "~0.9 for a real crystal dimer" figure that travelled
# with the max does not, so it is gone from here and from the banner. The
# sibling repo states the mechanism in
# llm-proteinDesigner/docs/MULTI-CHAIN-TARGETS.md — there is no such file in
# THIS repo, and citing it unqualified sent readers looking for one.
#
# This matters more than a mis-rendered number because ipTM is still the
# RANKING key (shared/result_columns.py). It no longer decides pass anywhere:
# llm-proteinDesigner#22 dropped that leg in the container (master 5f60456),
# and GATE_COLUMNS["boltzgen"] leaves it out here, so the bar is pLDDT and
# refolding RMSD on both sides. Order is what is left to get wrong.
#
# BOLTZGEN IS OUT, AND THE RUNS THAT PREDATE THE FIX ARE WHY IT TOOK AN
# ARGUMENT. llm-proteinDesigner#18 (squash-merged as 311c29f, Modal deploy
# green) moves `design_to_target_iptm` to the front of IPTM_KEYS, so the
# deployed container reports the binder-to-target interface. For every
# boltzgen run from here on, the banner's central claim — that the number is
# computed over interfaces including the target's own chain-chain contact —
# is FALSE.
#
# The wrinkle is that a results page renders whatever the job STORED, and
# designs that ran before the deploy still carry the old complex-wide value.
# At least one pre-deploy multi-chain boltzgen run exists (the 4ZQK GPU
# verification). A blanket removal takes their caveat away.
#
# Neither way of telling the two apart exists here, and both were checked
# rather than assumed:
#
#   * NO PER-RECORD MARKER. tools-hub stores the container's result verbatim
#     and reads the number out of `scores["ipTM"]`; nothing in this repo
#     reads, writes or requires a key saying WHICH IPTM_KEYS entry produced
#     it. Grep `design_iptm` here and every hit is a comment. Gating on a key
#     we merely hope the new container emits is a guess about another repo's
#     output shape, which is exactly how design_iptm was lost in the first
#     place.
#   * NO TIMESTAMP AT THE SEAM THAT NEEDS IT — but say this precisely, because
#     an earlier version of this comment said "no usable timestamp" and that
#     is stronger than what was checked. Jobs and campaigns DO carry
#     created_at/completed_at. The pooled target page is one of the six call
#     sites and its candidates are tagged only with _source_tool /
#     _source_preset / _source_campaign_id / _source_job_id / _source_index /
#     _source_chunk (shared/target_results.py), so the row a legend renders
#     against has no date on it. That is a PROJECTION, not an absence: neither
#     _STANDALONE_COLUMNS ("id,tool,preset,status,inputs,result") nor
#     _CHILD_COLUMNS ("id,user_id,chunk_index,attempt,result") selects
#     created_at, and both would have to, plus a new _source_created_at tag,
#     plus a deploy boundary to compare it against. The cost is real and the
#     boundary is fuzzy — the container SHA a job ran under is not recorded
#     either, so a date is a proxy for the thing that actually changed — which
#     is why the answer below is still the right one. It is not right because
#     the data does not exist.
#
# So the choice is which error to make uniformly, and they are not symmetric.
# KEEPING boltzgen shows every future user a mechanism sentence that is untrue
# of their run — not an over-cautious warning but a wrong explanation of a
# correct number, on the one tool where the fix actually landed. DROPPING it
# leaves a shrinking set of historical runs without a BANNER.
#
# It is dropped, and the caveat moves to the boltzgen ipTM LEGEND above, which
# is the only surface that can state it per tool and per era without lying to
# anyone. That legend renders on the ipTM column of every boltzgen table, old
# and new: the header tooltip in single-tool mode, and per row resolved from
# that row's own tool in pooled mode (components/candidate_table.html) — which
# is also the one view where a pre-deploy design sits beside a post-deploy
# one. The banner cannot do that; it is handed a tool slug, not a run.
#
# This does not weaken the rule bindcraft is kept under below. That rule — a
# false positive costs a sentence — holds only while the sentence would be
# TRUE if the run happened. For post-deploy boltzgen it is not true, so the
# cost is not a sentence.
#
# rfdiffusion and pxdesign have no equivalent fix available: the per-pair
# value does not exist anywhere in their output and deriving it from the chain
# layout is a separate piece of work. For them this notice is the remedy, not
# a stopgap.
#
# bindcraft is included even though multi_chain_container_ready=False blocks
# the tool-form path, because the campaign and target-launch routes may not
# call preflight_for_tool at all (an open item in
# docs/HANDOFF-2026-08-07-multichain-finish.md). The notice is non-blocking,
# so a false positive costs a sentence and a false negative costs trust in a
# number.
#
# PROTEINA IS DELIBERATELY ABSENT, and it is the exclusion worth arguing,
# because proteina is the only tool that has actually run a multi-chain target
# on a GPU here. It does surface an interface score — ``af2_iptm``, resolved
# from ``rf3folding_ipTM`` first (tools/proteina/run_pipeline.py) — and that
# number is open to the same inflation as everyone else's. But it is NOT what
# proteina ranks on: its primary metric is ``total_reward``
# (shared/result_columns.py), which proteina defines as ``-i_pae``, an
# interface PAE. So the second half of this notice — "these designs are also
# ranked by it", the half that makes it worth interrupting the user for —
# would be FALSE on a proteina table. A caveat that overstates its own scope
# is precisely the failure this notice exists to avoid.
#
# Open, and deliberately NOT resolved here: proteina's ``af2_iptm`` COLUMN may
# still read high on a multi-chain target for the reasons above, and whether
# i_pae is itself computed over the right chain pair has not been traced. That
# needs its own copy and its own verification, not membership in this set.
# Filed, not fixed.
MULTICHAIN_IPTM_UNRELIABLE_TOOLS = frozenset(
    {"rfdiffusion", "pxdesign", "bindcraft"}
)


def multichain_iptm_unreliable(tools, target_chain: str) -> bool:
    """Should the results view warn that ipTM cannot be read at face value?

    True only when the target names more than one chain AND at least one tool
    in play ranks on a complex-wide ipTM. Registered as a Jinja global in
    ``app.py`` so the decision stays here, in Python, where it is testable —
    rather than as a string-splitting expression repeated across six templates.

    ``tools`` is a single slug on a job or campaign page, or an iterable of
    slugs on the pooled target page, where one table mixes several tools and
    the caveat applies if ANY of them is affected.

    Both chain separators are accepted, matching every other consumer of this
    field (``"A,B"`` and ``"A B"``); see ``tools.base.parse_target_chains``.
    """
    if not names_multiple_chains(target_chain):
        return False
    if not tools:
        return False
    if isinstance(tools, str):
        tools = [tools]
    return any(t in MULTICHAIN_IPTM_UNRELIABLE_TOOLS for t in tools)


# ---------------------------------------------------------------------------
# Derived quality — the bar is applied when a page RENDERS, never stored
# ---------------------------------------------------------------------------
#
# A measurement is true forever. A verdict is true only against a threshold,
# so every frozen copy of one silently becomes a lie the moment the threshold
# moves. Four production defects came out of the verdict layer and none out of
# the measurements:
#
#   1. BoltzGen labelled 65/65 candidates "below threshold" against an ipTM
#      bar of 0.70 its refold cannot reach. The container was fixed
#      (llm-proteinDesigner#22, master 5f60456); the 65 stored labels were not,
#      and could not be.
#   2. shared/job_recovery rebuilds a lost result from records streamed DURING
#      the run. Those carry filter_status but no refolding_rmsd, because the
#      refold happens at the end — a verdict written before one of the two
#      measurements it depends on exists, then frozen.
#   3. On BoltzGen's peptide protocol the refold metrics are never produced at
#      all, so the gate quietly fell through to other columns.
#   4. Two stored candidates carry refolding_rmsd exactly 0.00. That is a
#      placeholder, and it clears a "<= 1.5" bar.
#
# Deriving here makes all four impossible rather than fixed. 1 dies because no
# label is stored to go stale. 2 and 3 die because a gate column with NO
# measurement leaves the record UNJUDGED — "not enough measured to say" is
# itself a fact, and it is never a silent fall-through to whatever else
# happened to be present. 4 dies because a placeholder is declared as one and
# is read as absent.
#
# Nothing in shared/, blueprints/ or templates/ may read ``filter_status``
# for a VERDICT again; tests/test_derived_verdicts.py greps all three. The one
# carve-out is ``is_fabricated`` below, which reads the "stub (smoke)"
# PROVENANCE half of that same field.
#
# WHERE THE COUNTS COME FROM. 65, 50 and 2 are the figures in the change
# request that commissioned this work, not queries run from here; no code or
# test depends on them, and the mechanism argument stands without any of
# them. Treat them as reported, and re-derive before quoting them anywhere
# a reader would take them as measured.


# Columns whose conjunction decides whether one design meets this tool's bar.
#
# THIS IS AN EXPLICIT WHITELIST AND NOT "every column that has a legend",
# which was the first design and would have reproduced defect 1 above:
# boltzgen HAS an ipTM legend, and an all-legend conjunction silently
# reinstates the very leg llm-proteinDesigner#22 removed.
#
# A tool absent from this map has NO bar and every one of its designs is
# ``unjudged`` — that is the correct reading for bindcraft (ships only its own
# accepted designs), proteina and iggm (no gate), and opendde (co-folding has
# no pass/fail concept at all). Absent is not the same as failing.
#
# Every column named here must have a legend for the same tool, because the
# legend carries the bar. tests/test_derived_verdicts.py asserts that.
GATE_COLUMNS: dict[str, tuple[str, ...]] = {
    # ipTM is deliberately NOT a leg. BoltzGen refolds the design ALONE, so
    # its ipTM is not the cofold quantity 0.70 describes. See the IPTM_THRESHOLD
    # note in llm-proteinDesigner/docker/boltzgen/run_pipeline.py.
    "boltzgen": ("pLDDT", "refolding_rmsd"),
    "rfdiffusion": ("ipTM", "pLDDT", "i_pAE"),
    # pAE is deliberately absent, even though the container gates on it. The
    # column arrives on two scales (see the pxdesign pAE legend above) and a
    # 0-1 reading clears an Angstrom bar unconditionally. A leg that can
    # silently pass everything is worse than no leg.
    "pxdesign": ("ipTM", "pLDDT"),
    "rfantibody": ("pLDDT", "ipAE", "pAE"),
    "boltz2": ("ipTM", "pLDDT", "n_hotspot_contacts"),
    # esmfold2-design is ABSENT, and that is a decision rather than the
    # oversight it looks like. Its bar is genuinely mode-dependent: the
    # pipeline's own classifier judges an scFv on the CDR distogram proxy
    # alone, and a minibinder on ipTM AND pI < 6, since an undisplayable
    # scaffold is a drop however well it folds. Neither can join a uniform
    # conjunction. pI is null by construction in scFv mode, so a pI leg leaves
    # every antibody design permanently unjudged; the proxy column holds a
    # DIFFERENT quantity in each mode and has a defensible bar in only one; and
    # picking between them from whichever columns happen to be populated is
    # defect 3 wearing a new name.
    #
    # An ipTM-only bar was tried and is worse than nothing here. This tool's
    # worked example exists to teach that its HIGHEST-ipTM design (0.956) was
    # rejected on pI 11.95, so an ipTM-only bar prints "meets" on the one
    # design the copy beside it tells you not to order. Saying nothing is the
    # honest answer until a record carries its own mode.
    #
    # THE UPGRADE PATH: stamp the mode onto each record in
    # tools/esmfold2_design/run_pipeline.py -- a mode is a FACT about the run,
    # so storing it is what this change endorses, not what it forbids -- then
    # declare a gate set per mode. That edit rebuilds the GPU images, which is
    # why it is not bundled here.
}


# Values that are a placeholder rather than a measurement, per (tool, column).
# Defect 4: two stored BoltzGen candidates carry refolding_rmsd 0.00, which is
# a perfect self-consistency no refold produces and which clears the bar. Read
# as absent, so the record goes unjudged instead of passing on a stand-in.
IMPLAUSIBLE_VALUES: dict[tuple[str, str], frozenset[float]] = {
    # Two stored BoltzGen candidates carry exactly 0.00 -- a perfect
    # self-consistency no refold produces, and it clears a "<= 1.5" bar.
    ("boltzgen", "refolding_rmsd"): frozenset({0.0}),

    # The container parsers' own fallbacks, read off their metric key lists.
    # pxdesign's parse_summary_csv calls _safe_float(..., 0.0) for every metric
    # but pAE, and 99.0 for pAE; rfantibody's declares (metric, keys, default)
    # triples with 0.0 for pLDDT/ipTM/pTM and 99.0 for pAE/ipAE. Those numbers
    # mean "the column was there and would not parse", and every one of them
    # falls on the FAILING side of its bar, so without this the page reports a
    # parse failure as a confident measured shortfall. Unmeasured is the true
    # answer. A real reading never lands on them: an ipTM or pLDDT of exactly
    # 0.0, or a PAE of exactly 99.0 Angstrom, is not a thing a model emits.
    ("pxdesign", "ipTM"): frozenset({0.0}),
    ("pxdesign", "pLDDT"): frozenset({0.0}),
    ("rfantibody", "pLDDT"): frozenset({0.0}),
    ("rfantibody", "ipAE"): frozenset({99.0}),
    ("rfantibody", "pAE"): frozenset({99.0}),
}


# Storage spellings for a legend column key. Pipelines are not consistent:
# boltz2 persists a flat ``designs[]`` with lowercase root keys (``iptm``,
# ``complex_plddt``) while every container writes a capitalised ``scores``
# dict. Resolution tries these in order, under ``scores`` then at the record
# root, which is how the table and the ranking helpers already resolve metrics.
#
# ``complex_iplddt`` / ``iplddt`` are absent on purpose. Interface pLDDT is the
# same quantity measured over a different REGION, so it cannot stand in for a
# whole-structure pLDDT when that column is missing — substituting it would
# answer the bar with a reading of something else, which is defect 3 wearing a
# different column name. shared.metric_glossary.PLDDT_COLUMNS does list both,
# correctly: that set says which values need rescaling to 0-100, not which are
# interchangeable.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "ipTM": ("ipTM", "iptm"),
    "pLDDT": ("pLDDT", "plddt", "complex_pLDDT", "complex_plddt"),
    "pAE": ("pAE", "pae"),
    "i_pAE": ("i_pAE", "i_pae"),
    # "i_pae" included: the webhook's streamed-partial schema
    # (webhooks/modal.py::_sanitize_candidate) carries ONE interface-PAE field
    # and every container maps its own onto it, so a live rfantibody row holds
    # its ipAE under that name. Interface PAE is one quantity under three
    # spellings. "pAE" below is NOT aliased to it: a global PAE is a different
    # measurement and must not answer the interface bar.
    "ipAE": ("ipAE", "ipae", "i_pae"),
}


class Judgement(NamedTuple):
    """What the measurements on ONE record say about ONE tool's bar.

    ``verdict`` is one of:

    ``meets``
        Every gate column is measured and at or better than its ``good``.
    ``below``
        At least one gate column is measured and worse than its ``good``.
        A shortfall settles it even when another leg is unmeasured: one leg
        definitively failing is a fact about the design, not about the gaps.
    ``unjudged``
        The tool declares no bar, or no leg fell short and at least one was
        not measured. NOT a failure. ``shared.ranking`` depends on that
        reading: judging an unmeasured record as failed once sank 240
        recovered pxdesign rows at ipTM 0.99 below 100 bindcraft rows at 0.70.

    ``shortfalls`` and ``unmeasured`` are rendered text, already carrying the
    number and the bar ("pLDDT 72.4, below 80"), because the reader needs the
    fact and not the word.
    """

    verdict: str
    shortfalls: tuple[str, ...]
    unmeasured: tuple[str, ...]

    # The gate columns behind ``shortfalls``, as column KEYS rather than
    # rendered text. A results banner speaks about a whole table, and
    # "no design here reaches pLDDT 80 and Refolding RMSD 1.5" is FALSE when
    # every row fell short on pLDDT and not one of them ever measured the
    # RMSD: half that sentence is then a claim about a number the page does
    # not have. With the keys, a banner names only the legs it actually saw
    # fall short. See :func:`shortfall_bar_text`.
    shortfall_columns: tuple[str, ...] = ()


def _fmt(value: float) -> str:
    """Trim a bar or a reading to what it actually carries: 80.0 -> 80."""
    return f"{value:g}"


def _label_and_unit(column: str) -> tuple[str, str]:
    """Split a glossary label into its name and its trailing unit.

    The glossary labels a column for a TABLE HEADER, where the unit belongs in
    a parenthetical: "Refolding RMSD (A)". Dropped into a sentence that reading
    comes out as "Refolding RMSD (A) 1.5", with the unit stranded before the
    number it belongs to. Split here and the sentence reads
    "Refolding RMSD 1.5 A", which is how anyone would say it out loud.

    A label with no parenthetical returns an empty unit and is unchanged.
    """
    label = str(_metric_glossary.get(column).get("label") or column)
    if label.endswith(")") and " (" in label:
        name, _, unit = label.rpartition(" (")
        return name, " " + unit[:-1]
    return label, ""


def _reading(column: str, value: float) -> str:
    """A MEASUREMENT as a sentence fragment: "Refolding RMSD 1.80 A".

    Formatted at the metric's own declared precision (the same
    ``shared.metric_glossary`` format the table cell uses), not at %g. %g
    gives six significant figures and then trims, so an ipTM of 0.7499999
    printed "0.75" and the cell read "ipTM 0.75, below 0.75" -- a sentence
    that refutes itself and leaves a reader nothing to check.
    """
    label, unit = _label_and_unit(column)
    return f"{label} {_metric_glossary.format_value(column, value)}{unit}"


def _bar_reading(column: str, good: float) -> str:
    """A THRESHOLD as a sentence fragment: "Refolding RMSD 1.5 A".

    %g here, deliberately, where :func:`_reading` uses display precision: a
    bar is an exact chosen number and 1.5 is how it was chosen. Rendering it
    "1.50" would dress a decision up as a measurement.
    """
    label, unit = _label_and_unit(column)
    return f"{label} {_fmt(good)}{unit}"


def _join_bar(tool: str, columns) -> str:
    """``columns`` of ``tool``'s bar as an English list, in bar order."""
    parts = [
        _bar_reading(col, float(get_legend(tool, col)["good"]))
        for col in columns
        if get_legend(tool, col) is not None
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def gate_columns(tool: str) -> tuple[str, ...]:
    """Columns whose conjunction is ``tool``'s bar; empty when it has none."""
    return GATE_COLUMNS.get(tool or "", ())


def tool_has_bar(tool: str) -> bool:
    """Does this tool declare a quality bar at all?

    A PROPERTY OF THE TOOL, not of what one result happened to store, and that
    is the whole point. The regime used to be inferred per cohort from whether
    any row carried a ``filter_status``, which made it depend on which
    container version ran and on whether job recovery had rebuilt the row.
    """
    return bool(gate_columns(tool))


def gate_bar_text(tool: str) -> str:
    """The tool's WHOLE bar as a sentence fragment.

    Every leg, which is right for a tooltip explaining what the bar is. It is
    NOT right for a sentence asserting something about designs -- use
    :func:`shortfall_bar_text` there.
    """
    return _join_bar(tool, gate_columns(tool))


def shortfall_bar_text(tool: str, columns) -> str:
    """The bar restricted to ``columns``: what a banner may safely assert.

    A leg nobody measured cannot be a leg anything failed to reach. Pass the
    union of the rows' ``Judgement.shortfall_columns`` and the sentence names
    only legs the page has evidence about. Bar order is preserved, so the
    banner reads the same way the tooltip does.
    """
    wanted = set(columns or ())
    return _join_bar(tool, [c for c in gate_columns(tool) if c in wanted])


def _resolve(record: object, tool: str, column: str):
    """This record's value for a legend column, on the scale the bar is in.

    ``scores`` first then the record root, over every storage spelling in
    ``_COLUMN_ALIASES`` — the same resolution order the table and
    ``shared.result_columns.candidate_metric`` use. pLDDT is put on 0-100
    EXACTLY ONCE (``plddt_on_100`` is not idempotent below 0.01), because
    boltz2 stores it 0-1 and the legends are all written on 0-100.

    Returns None for absent, unparseable, or declared-placeholder values. Every
    one of those means unmeasured, never failed.
    """
    if not isinstance(record, dict):
        return None
    scores = record.get("scores")
    scores = scores if isinstance(scores, dict) else {}
    raw = None
    for key in _COLUMN_ALIASES.get(column, (column,)):
        if scores.get(key) is not None:
            raw = scores[key]
            break
        if record.get(key) is not None:
            raw = record[key]
            break
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None

    # ORDER MATTERS, AND GETTING IT WRONG MADE THE PLACEHOLDER GUARD INERT.
    # This block used to return the rescaled pLDDT before either check below
    # ran, so IMPLAUSIBLE_VALUES could not be declared on any pLDDT column at
    # all -- the one family most likely to carry a stand-in, since rfantibody
    # and pxdesign both default pLDDT to 0.0 on a parse failure. Declared
    # values are compared in the STORED scale, which is why the check sits
    # ahead of the rescale; 0.0 is 0.0 on either scale, so nothing is lost.
    if value in IMPLAUSIBLE_VALUES.get((tool, column), frozenset()):
        return None

    if column in _metric_glossary.PLDDT_COLUMNS:
        # A negative pLDDT is a broken payload, not a confidence. plddt_on_100
        # passes it through on purpose so a reader SEES it in the table; a bar
        # must not turn it into a confident "pLDDT -5, below 80", which reads
        # as a measured shortfall. Unmeasured is the true answer.
        if value < 0:
            return None
        # Applied exactly once (plddt_on_100 is not idempotent below 0.01),
        # because every legend on this site is written for the 0-100 scale
        # while boltz2 and others store 0-1.
        return _metric_glossary.plddt_on_100(value)

    return value


# The one string a pipeline stamps that this module still reads, and it is
# not a verdict. The smoke tier fabricates deterministic scores when no real
# model output exists (``_stub_scores`` / ``_stub_af2_scores`` in the container
# repo) and marks them "stub (smoke)". That is PROVENANCE -- a fact about where
# a number came from, which is exactly the kind of thing this design says to
# store. Applying a bar to invented numbers would print "Meets pLDDT 80" over
# values no model produced: at rank 20 the rfdiffusion stub is ipTM 0.65 /
# pLDDT 90.0 / i_pAE 10.0, which clears its own tool's bar outright.
_FABRICATED_MARKERS = ("stub",)


def is_fabricated(record: object) -> bool:
    """True when a pipeline marked this record's scores as fabricated.

    Reads the provenance marker only. Nothing here consults, or may consult,
    the pass/fail half of that field -- see the block comment at the top of
    this section for the difference and why it matters.
    """
    if not isinstance(record, dict):
        return False
    scores = record.get("scores")
    scores = scores if isinstance(scores, dict) else {}
    marker = scores.get("filter_status")
    if marker is None:
        marker = record.get("filter_status")
    text = str(marker or "").lower()
    return any(m in text for m in _FABRICATED_MARKERS)


def judge(tool: str, record: object) -> Judgement:
    """Compare ONE record's measurements against ``tool``'s bar, right now.

    The only place in this repo that decides whether a design meets a bar.
    Call it at render time and throw the answer away; never persist it.
    """
    columns = gate_columns(tool)
    if not columns:
        return Judgement("unjudged", (), ())

    if is_fabricated(record):
        return Judgement("unjudged", (), ("smoke-test stub, scores fabricated",))

    shortfalls: list[str] = []
    shortfall_cols: list[str] = []
    unmeasured: list[str] = []
    for col in columns:
        # The bare name. A unit on a column nobody measured is noise: "Not
        # measured: Refolding RMSD" is the fact, and "(A)" adds nothing when
        # there is no number for it to qualify.
        label, _unit = _label_and_unit(col)
        legend = get_legend(tool, col)
        if legend is None:
            # A gate column with no legend has no bar to be compared against.
            # tests/test_derived_verdicts.py makes this unreachable; if it ever
            # happens anyway, "we cannot say" is the safe answer, not "failed".
            unmeasured.append(label)
            continue
        value = _resolve(record, tool, col)
        if value is None:
            unmeasured.append(label)
            continue
        good = float(legend["good"])
        lower_is_better = legend["direction"] == "lower_is_better"
        meets = value <= good if lower_is_better else value >= good
        if not meets:
            side = "above" if lower_is_better else "below"
            _, unit = _label_and_unit(col)
            shortfalls.append(
                f"{_reading(col, value)}, {side} {_fmt(good)}{unit}"
            )
            shortfall_cols.append(col)

    if shortfalls:
        return Judgement(
            "below",
            tuple(shortfalls),
            tuple(unmeasured),
            tuple(shortfall_cols),
        )
    if unmeasured:
        return Judgement("unjudged", (), tuple(unmeasured))
    return Judgement("meets", (), ())
