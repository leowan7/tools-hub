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

from typing import NotRequired, Optional, TypedDict


class Legend(TypedDict):
    # OPTIONAL, because a legend is allowed to have no bar. Nothing renders
    # these — ``legend_text`` and ``email_caption`` read ``explanation`` and
    # ``caveat`` only — so they are the module's record of what the numbers
    # mean, and a wrong one sits inert until someone wires it up.
    #
    # boltzgen's ipTM omits them. It carried 0.7/0.8, which are the Boltz-2
    # COFOLD bars, against a number a generator confidence head produces from
    # its own output. On the audited 100-design replicate — the one run where
    # this legend's own column was captured — `design_to_target_iptm` spans
    # 0.084-0.583, 0/100 over 0.70.
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
    ("pxdesign", "pAE"): {
        "good": 5.0,
        "excellent": 3.0,
        "direction": "lower_is_better",
        "explanation": (
            "Global predicted aligned error. Below 5 angstroms is good "
            "complex geometry; below 3 is excellent."
        ),
    },

    # ── BoltzGen (Boltz-1 distilled generator + refold check) ────────
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
        # cofold span 0.166-0.806 on `binder_to_target`, the per-chain-pair
        # column feld1/13_boltz_cofold.py exists to read precisely because it
        # cannot pick up target-internal contacts (29 rows, 1 over 0.70). Its
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
# EVERY chain pair" (tools/af2/run_pipeline.py:202,
# tools/colabfold/run_pipeline.py:149, tools/esmfold2_design/run_pipeline.py:440,
# tools/proteina/run_pipeline.py:1787), all of them describing the incident
# where 460 boltzgen designs were scored on it. The conclusion above survives
# either reduction. The "~0.9 for a real crystal dimer" figure that travelled
# with the max does not, so it is gone from here and from the banner. The
# sibling repo states the mechanism in
# llm-proteinDesigner/docs/MULTI-CHAIN-TARGETS.md — there is no such file in
# THIS repo, and citing it unqualified sent readers looking for one.
#
# This matters more than a mis-rendered number because ipTM is also the
# RANKING key (shared/result_columns.py). It no longer labels filter_status:
# llm-proteinDesigner fix/boltzgen-unreachable-gate drops that leg, so the
# label is pLDDT and refolding RMSD only.
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
