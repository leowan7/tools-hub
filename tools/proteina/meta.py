"""Static reference metadata for the Proteina-Complexa tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so the "About" panel, citation block, license notices, and
cost previews import plain-data constants without touching the adapter
contract. Mirrors ``tools/boltzgen/meta.py``.
"""

from __future__ import annotations

from typing import Optional

PRESET_RUNTIME: dict[str, dict[str, object]] = {
    # Per-shard wall-clock. The campaign fans many shards out in parallel, so
    # total campaign time depends on the requested design count and the launch
    # concurrency (4), not on this number alone.
    #
    # protein_binder is MEASURED, at three sizes. Paid A100-80GB canary shards
    # returned 8 designs in 576 s (9.6 min) at 130 aa, 645 s (10.8 min) at
    # 260 aa and 874 s (14.6 min) at 415 aa. The band below has to CONTAIN the
    # span it cites, and "~10 to 15" did not — the 130 aa shard, the smallest
    # and fastest of the three, sits at 9.6 and fell outside its own claimed
    # range. "~9 to 15" spans all three; the top of it also covers the 500-aa
    # cap in shared/pdb_preflight_rules.py (_PROTEINA): runtime scales as
    # (aa/120)^0.34, so a target at the cap comes out at ~14.6 min too — the
    # curve is nearly flat in target size, and Modal cold-start and GPU
    # contention matter more than the extra residues.
    #
    # THIS COPY HAS BEEN WRONG TWICE, both times in the direction a user plans
    # against. It shipped as "30 to 120" for all three variants, a placeholder
    # never re-set, which overstated a real shard by 5-20x. It was then
    # corrected to "~6" from a 359 s reading at 130 aa. TWO readings exist at
    # that size and they disagree by ~60%: 359 s and 576 s. Both are recorded
    # as completed 8-design protein_binder shards at 130 aa; what separates
    # them is the JAX ALLOCATOR REGIME. The 359 s wall-clock belongs to the
    # preallocation-ON shard that read 67,570 MB; the three shards this band is
    # drawn from all ran with preallocation disabled.
    # shared/pdb_preflight_rules.py::_PROTEINA documents those two regimes as
    # non-comparable and names allocate-on-demand as a candidate for the gap,
    # not a diagnosis of it. The 576 s reading is the one taken under the
    # allocator settings production runs today, so it is the one that describes
    # what a user's shard will do; the 359 s figure is not used for anything.
    # Both times the number here was also load-bearing:
    # shared/pdb_preflight_rules.py anchors its runtime estimator to this
    # measurement, so an error in a docs constant reaches the preflight panel
    # looking calibrated.
    #
    # ligand_binder and motif_ame have NEVER been timed. Their band is bounded,
    # not measured: the floor is the smallest complete protein_binder
    # measurement (9.6 min) and the ceiling is the physical
    # _MAX_SESSION_S = 7200 s (120 min) session wall in modal_app.py, past
    # which the shard is killed. Re-set each from its own canary.
    #
    # THAT FLOOR ROUNDS DOWN, TO 9, for the same reason protein_binder's does.
    # It read 10 while protein_binder's read 9 — the same 9.6 min rounded two
    # different ways in adjacent lines, which had the never-measured presets
    # claiming a HIGHER floor than the only preset anyone has timed, and
    # overstated the sentence below by 0.4 min. Rounding down keeps a lower
    # bound a lower bound; rounding up turns it into a claim.
    #
    # AND THE FLOOR IS WEAKER THAN IT LOOKS. This used to read "same container,
    # same reward stack". The container is the same; the reward stack is NOT.
    # protein_binder scores on AF2 alone, while RF3 is the SOLE reward for
    # ligand_binder and is what motif_ame needs too — Dockerfile.modal:219-222
    # says so outright ("Only ligand_binder (RF3 is its sole reward) and
    # motif_ame need it; protein_binder scores on AF2 alone"), and
    # ``reward_attributions`` below splits them the same way. So the floor is
    # not evidence transferred from a comparable run; it is a lower bound
    # borrowed from a DIFFERENT scoring path, and there is no reason to think
    # RF3 scoring is as fast as AF2 scoring. Treat 9 as "cannot plausibly be
    # quicker than the fastest thing we timed", not as a measurement.
    "protein_binder": {"typical_minutes": "~9 to 15"},
    "ligand_binder": {"typical_minutes": "9 to 120 (not yet measured)"},
    "motif_ame": {"typical_minutes": "9 to 120 (not yet measured)"},
    "validate": {"typical_minutes": "1 to 3"},
}

paper_citation: str = "Geffner et al., NVIDIA (2025)"
paper_url: str = "https://research.nvidia.com/labs/genair/proteina-complexa/"
# The binder/ligand/AME search code, configs, reward stack, and weights live in
# the Proteina-Complexa repo (branch ``dev``), NOT the base ``proteina`` backbone
# generator — the module name ``proteinfoundation`` is shared between the two,
# which is an easy mix-up. Pinned commit: 916eaaedce5b07c205efb6ef32370c01d366591e.
github_url: str = "https://github.com/NVIDIA-Digital-Bio/proteina-complexa"

# NVIDIA Open Model License notice — surfaced verbatim on the tool page + repo.
model_license_notice: str = (
    "Licensed by NVIDIA Corporation under the NVIDIA Open Model License"
)

# Reward-model attributions surfaced alongside the results.
reward_attributions: list[str] = [
    "AlphaFold2 parameters (CC-BY-4.0, DeepMind) — protein-binder confidence.",
    "RoseTTAFold3 via RosettaCommons foundry (BSD) — ligand + motif reward.",
    "ESM2 (MIT, Meta AI) — sequence likelihood.",
    "Foldseek / MMseqs2 / DSSP — post-hoc diversity clustering.",
]

seo_faq: list[dict] = [
    {
        "q": "Can I run Proteina-Complexa online without a GPU cluster?",
        "a": (
            "Yes. Ranomics Tools runs Proteina-Complexa as a fund-and-drain "
            "campaign of independent search shards on dedicated A100-80GB "
            "GPUs. Pick a protein or small-molecule target, choose how many "
            "designs you want, and shards fan out automatically. You only pay "
            "for compute that runs, and the campaign pauses if your balance "
            "runs low."
        ),
    },
    {
        "q": "Can Proteina-Complexa design binders against a small molecule?",
        "a": (
            "Yes. The ligand-binder variant takes a small-molecule target as "
            "an SDF and designs de novo binders scored by the RoseTTAFold3 "
            "reward. The protein-binder variant targets a protein PDB and is "
            "scored by AlphaFold2 confidence."
        ),
    },
    {
        "q": "How are Proteina-Complexa designs scored and ranked?",
        # THE FIFTH SURFACE. This answer renders twice on /tools/proteina —
        # as visible FAQ copy and inside the FAQPage JSON-LD, so it is
        # rich-result eligible — and it used to read "Each search shard
        # filters candidates through an AF2 / RF3 / force-field reward
        # stack". No shard does. Dockerfile.modal:229-231: "Only
        # ligand_binder (RF3 is its sole reward) and motif_ame need it;
        # protein_binder scores on AF2 alone." That made this answer the
        # direct negation of ``about["output_summary"]`` below ("The ligand
        # and motif variants score on RF3 only"), two paragraphs away on
        # the same page. The which-model-follows-the-target clause is
        # copied verbatim from ``about["what_it_is"]`` so the page states
        # the mapping in one voice rather than three.
        "a": (
            "Every candidate is re-folded and scored against your target as "
            "it is generated, and which model does that scoring follows the "
            "target: a protein target is scored by an AlphaFold2 refold, a "
            "small-molecule or motif target by RoseTTAFold3, with a physics "
            "force field added where it applies. Each shard keeps what "
            "scores well, and the hub then ranks across every shard at once "
            "and clusters the winners, so you get a spread of different "
            "high-scoring designs rather than near-duplicates."
        ),
    },
]

# THE REWARD STACK IS A MENU, NOT A PIPELINE. This one-liner is the
# highest-blast-radius string in the package — it feeds the homepage
# card, /tools, and /help/tools/proteina — and it shipped for review
# reading "every candidate is filtered through three independent
# scoring checks". No variant runs all three.
# Dockerfile.modal:229-231: "Only ligand_binder (RF3 is its sole
# reward) and motif_ame need it; protein_binder scores on AF2 alone,
# so it runs regardless of this switch." The comment ~70 lines above
# says the same, and ``about["output_summary"]`` below — which renders
# one scroll away on the same page — already said the true version.
# Say which model scores which target, or say nothing.
comparison_one_liner: str = (
    "You have a hard target — a recessed pocket, a site spanning "
    "two chains, or a small molecule rather than a protein — and "
    "you want to throw as much search at it as your balance allows. "
    "Every candidate is re-folded and scored against your target as "
    "it is generated, and the run fans out across as many GPUs as "
    "you fund."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Designs binders for the targets the standard tools find hard: "
        "a recessed pocket, a site spanning two chains, a small "
        "molecule instead of a protein, an enzyme active site. Rather "
        "than generating candidates and hoping, it searches — it "
        "generates, re-folds every candidate and scores how well it "
        "grips your target, keeps what scores well and generates "
        "again from there. Which model does that scoring follows the "
        "target: a protein target is scored by an AlphaFold2 refold, "
        "a small-molecule or motif target by RoseTTAFold3, with a "
        "physics force field added where it applies. "
        "The run splits into independent shards "
        "across as many GPUs as your balance funds, then ranks globally "
        "across all of them and clusters the winners, so you get a "
        "spread of different designs rather than many copies of one. "
        "Proteina-Complexa, Geffner et al., NVIDIA 2025."
    ),
    "when_to_use": [
        (
            "You want to aim a binder at one specific patch, including a "
            "recessed or partly shielded one, by naming residues on it."
        ),
        (
            "The site you care about spans more than one chain of your "
            "target."
        ),
        (
            "Your target is a small molecule rather than a protein."
        ),
        (
            "You would rather pay for a search that filters as it goes than "
            "for raw generation you have to filter afterwards."
        ),
        (
            "You want to scale the search across many GPUs, with your "
            "prepaid balance as the only ceiling."
        ),
        (
            "You want a spread of different good designs rather than many "
            "variations on one."
        ),
    ],
    "prerequisites": [
        "A target: your own structure (<code>.pdb</code>/<code>.cif</code>) for "
        "the protein-binder variant, or a curated benchmark task for any variant.",
        "For your own target, the chain ID &mdash; or a chain/residue range such "
        "as <code>A1-150</code>, or <code>A12-157,B12-157,C12-157</code> for a "
        "multi-chain target.",
        "Optionally, hotspot residues to aim the binder at a specific epitope.",
        "A funded wallet that covers at least the first wave of shards.",
    ],
    "inputs": [
        {
            "name": "Design variant",
            "explanation": (
                "<code>protein_binder</code> for a protein target (AF2 reward), "
                "<code>ligand_binder</code> for a small-molecule SDF target "
                "(RF3 reward), <code>motif_ame</code> for motif scaffolding / "
                "enzyme active sites, or <code>validate</code> for a free "
                "config check before spending GPU."
            ),
        },
        {
            "name": "Target",
            "explanation": (
                "Your own structure, or a curated benchmark task whose target "
                "is baked in &mdash; the two are mutually exclusive. Uploading "
                "your own is available on the protein-binder variant; the "
                "ligand and motif variants run curated tasks (their tasks "
                "resolve from separate upstream registries)."
            ),
        },
        {
            "name": "Target region",
            "explanation": (
                "Which chains and residues to design against, e.g. "
                "<code>A1-150</code>, or <code>A12-157,B12-157,C12-157</code> "
                "for a multi-chain target. Blank uses the whole target chain."
            ),
        },
        {
            "name": "Hotspot residues",
            "explanation": (
                "Optional. Residues the binder should contact, in original PDB "
                "numbering &mdash; plain numbers use the target chain, or "
                "prefix the chain (<code>A113 C73</code>) for a multi-chain "
                "region. Every hotspot is checked against your structure "
                "before any GPU runs, so a residue that is not there is "
                "refused rather than quietly ignored."
            ),
        },
        {
            "name": "Binder length",
            "explanation": (
                "The range each design's length is drawn from. Defaults to "
                "60-120 residues."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many designs to search for. This scales the number of "
                "independent search shards; each shard runs on its own GPU and "
                "returns its survivors, and the hub picks the global top set."
            ),
        },
    ],
    # Kept in lockstep with PRESET_RUNTIME above — see the provenance note
    # there for what is measured (protein_binder, at 130 / 260 / 415-residue
    # targets) and what is only bounded by the 7200 s session wall.
    "runtime_table": [
        {"preset": "protein_binder",
         "typical": "~9 to 15 min / shard (measured at 130-415 residues)"},
        {"preset": "ligand_binder", "typical": "not yet measured (under 120 min / shard)"},
        {"preset": "motif_ame", "typical": "not yet measured (under 120 min / shard)"},
        {"preset": "validate", "typical": "1 to 3 min (free)"},
    ],
    "output_summary": (
        "Ranked designs with reward scores (AF2 pLDDT / ipTM for protein, "
        "RF3 score for ligand / motif, force-field energy where applicable), "
        "a structural diversity cluster id, and downloadable structures. The "
        "ligand and motif variants score on RF3 only."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
    "model_license_notice": model_license_notice,
    "reward_attributions": reward_attributions,
}


# ---------------------------------------------------------------------------
# PILOT — the guided starter recipe rendered by
# templates/components/pilot_card.html.
#
# NO PRICE AND NO RUNTIME STRING BELONGS IN THIS DICT. Both are derived
# at render time (blueprints/tools.py::_pilot_context) from
# shared.wallet_estimates.estimated_cost_for_tool over ``params`` and
# from the preset runtime map above. A hand-written second rate card
# drifts off the real one within a month.
#
# ``params`` keys are FORM FIELD NAMES. The same dict pre-fills the
# form via ?pilot=1 and feeds the estimator, and the form posts those
# same names to /api/wallet/estimate — so the card's price and the
# form's live price cannot disagree. Only include keys the form
# actually honours through pre_value()/pre_checked(); a key no field
# reads is a pre-fill that silently does nothing.
# ---------------------------------------------------------------------------
PILOT: dict | None = {
    "label": "Starter pilot: one shard, 8 designs",
    # "see what the reward stack returns" was both jargon on a page aimed
    # at a bench biologist and the loosest of the reward-stack strings —
    # the pilot runs protein_binder, which scores on AF2, not a stack.
    "goal": (
        "Run the smallest complete unit of a Proteina search against "
        "your own target and see how the designs score against it."
    ),
    "you_need": (
        "A structure file for your target and its chain ID &mdash; or a "
        "chain and residue range such as <code>A1-150</code>. Hotspot "
        "residues are optional, and are what aims the binder at one "
        "specific face."
    ),
    # num_designs stays at 8 because 8 IS the floor: proteina is a
    # fixed-container tool (compute_campaigns._FIXED_CONTAINER_TOOLS),
    # one shard is one whole A100-80GB container, and the estimate is
    # flat from 1 design to 8. Asking for fewer costs exactly the same
    # and returns less, so there is no cheaper first run to offer.
    #
    # What the pilot does change is the length window. The form leaves
    # both boxes blank, which silently means 60-120 rather than
    # unconstrained; 60-100 is a narrower, more designable first search
    # and makes the implicit default visible instead of leaving the
    # user to discover it in the help text.
    "params": {
        "preset": "protein_binder",
        "num_designs": "8",
        "binder_length_min": "60",
        "binder_length_max": "100",
    },
    "next_step": (
        "8 designs is one shard on one GPU, and it costs the same as "
        "one design would &mdash; a shard is a whole container. Raise "
        "the count and the run fans out across GPUs as a campaign "
        "bounded by your wallet, with a single ranked list pooled "
        "across every shard. Widen the length window once you know the "
        "target is workable."
    ),
}


# ---------------------------------------------------------------------------
# EXAMPLE - one real past run, rendered by
# templates/components/worked_example.html.
#
# PROVENANCE. One shard of the tier-2 length sweep, 2026-08-10: 64 designs
# from a single ranomics-proteina-prod call (3447 GPU-seconds), re-derived by
# scripts/_build_proteina_example.py. One shard is one job, so this is the
# shape a single submission returns, not an aggregate over the campaign's 46
# shards. The target is the same two-chain protein the PXDesign example uses
# and is described the same way; the hotspot set is different (18 here, 6
# there) because these were different rounds.
#
# THE PREVIOUS NOTE HERE WAS RIGHT TO REFUSE THE OLD PAYLOAD, and this one
# clears both of its objections. It rejected proteina_direct_out/
# smoke_result.json for pre-dating the pLDDT polarity fix (#129, 9fbe547,
# 2026-08-09). This shard ran 2026-08-10, after it, and the polarity is
# checked rather than assumed: af2_plddt correlates +0.63 with af2_iptm and
# +0.62 with total_reward across the 64, and the top-12 median pLDDT (0.840)
# is above the bottom-12 (0.733). Under the inversion every one of those
# signs would flip.
#
# It also called that payload degenerate - "af2_iptm 0.09 to 0.10 across all
# 8, binder_scrmsd 34 to 41 A" - without naming the cause. That signature is
# now identified: it is the generator returning the target's own sequence
# back as the binder. Twelve of this shard's 64 designs do exactly that, at
# af2_iptm 0.086-0.098 and binder_scrmsd 32-44 A. The old smoke run was 8
# for 8 of them.
#
# SELF-COPIES ARE NOT FILTERED BY THE TOOL and are shown here on purpose.
# tools/proteina/run_pipeline.py has no notion of target overlap, so a real
# operator gets these rows too; hiding them would misreport the output. They
# do sort to the bottom on total_reward (ranks 51-64 here), and across all
# 17,024 designs of the campaign not one self-copy ever cleared ipTM 0.80.
#
# COST is the customer-facing charge for 3447 GPU-seconds on this tool's GPU
# class, from shared.wallet.compute_charge_usd - not the campaign's raw Modal
# cost. tests/test_worked_examples.py recomputes it, so a rate-card change
# fails a test instead of leaving a stale price on a public page.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = {
    "target": (
        "A two-chain human secreted protein, about 210 residues per chain, "
        "from a solved crystal structure. Eighteen hotspot residues, nine on "
        "each chain, across the two-fold interface where the chains meet."
    ),
    "why_this_target": (
        "The site sits on a symmetry axis, so a binder has to reach both "
        "chains at once rather than settle on either one. It is also a "
        "target whose own sequence the generator already knows well, which "
        "turns out to be what this run is worth reading for."
    ),
    "inputs_used": [
        (
            "Target structure",
            "the two-chain crystal structure, chains A and B",
            "Both chains staged and both named as target chains. Give one "
            "chain and the model designs against half a site that does not "
            "exist on its own.",
        ),
        (
            "Hotspot residues",
            "18 residues, 9 per chain, each written with its chain letter",
            "The chain letter is required once a run names more than one "
            "target chain, and the form refuses a bare <code>241</code> "
            "there. It has to: the model matches hotspots literally as "
            "chain plus number, and on a homodimer whose two protomers "
            "share one numbering a bare token would resolve to chain A "
            "alone &mdash; a run that looks symmetric and is not. Writing "
            "<code>A241</code> and <code>B241</code> is what makes the ask "
            "symmetric.",
        ),
        (
            "Binder length (residues)",
            "60 to 69",
            "A window, not a number &mdash; the generator draws a length "
            "per design. Leaving the field empty gives you 60 to 120; this "
            "is the bottom of that default, where binders are cheaper to "
            "fold and easier to order later.",
        ),
        (
            "Number of designs",
            "64",
            "One shard: 16 starting samples with 4 replicas each. A shard "
            "is one container, so all 64 came back from a single call.",
        ),
        (
            "Design variant",
            "Protein binder (de novo, vs a protein target)",
            "The only variant that designs against a structure you upload; "
            "the others run curated ligand and motif benchmarks. It also "
            "settles the <code>rf3_score</code> column below, which is empty "
            "on every row: RF3 is a second scoring stack those other "
            "variants need, a protein binder run does not, and it is not "
            "free. That is a consequence of this choice rather than a switch "
            "of its own &mdash; there is no RF3 control on the form.",
        ),
    ],
    "what_came_back": (
        "64 designs, of which <strong>12 passed</strong> &mdash; ipTM at or "
        "above 0.80 with the re-folded complex landing within 5 &Aring;. "
        "They are ranks 1 to 11 and 13, so on this shard the ranking and the "
        "filter agree: the top of the table is the answer. The best scored "
        "ipTM 0.89 at pLDDT 0.89, re-folding 1.32 &Aring; from where the "
        "generator put it. "
        "Of the 52 that did not pass, 30 failed on the re-fold. "
        "<strong>Twelve of those 30 are the model handing the target's own "
        "sequence back as the binder</strong> &mdash; literal copies of a "
        "stretch of the chain it was asked to bind."
    ),
    "how_to_read_it": (
        "<strong>Read <code>binder_scrmsd</code> before you trust "
        "<code>af2_plddt</code>.</strong> The twelve copies score pLDDT "
        "0.71 to 0.76 &mdash; unremarkable, nothing that reads as broken "
        "&mdash; while their <code>binder_scrmsd</code> is 32 to 44 &Aring; "
        "against a median of 2.0 &Aring; for everything else. That column "
        "is the generator's backbone compared against an independent re-fold "
        "of its own sequence, and a target fragment re-folds into the target's "
        "shape rather than the shape it was drawn as. "
        "ipTM agrees (0.086 to 0.098, against 0.67 median) and "
        "<code>total_reward</code> sends all twelve to ranks 51 to 64, so "
        "reading the top of the table is safe. Reading the whole table is "
        "what needs this paragraph. "
        "<strong>The scores shortlist; they do not diagnose.</strong> "
        "Thirteen rows here carry that profile &mdash; re-fold past 30 "
        "&Aring; at ipTM under 0.10 &mdash; and twelve of them are copies. "
        "The thirteenth, at rank 55, is an ordinary de-novo sequence that "
        "shares 4% of itself with the target and simply failed to re-fold. "
        "Its pLDDT is 0.58, below the band the twelve sit in, but that gap "
        "is one shard's worth of evidence and not a rule. We know which "
        "twelve because we compared each sequence against the target's, "
        "which takes a substring search and no GPU. If a row of yours looks "
        "like this, that comparison is the check that settles it."
    ),
    "what_we_did_next": (
        "Scaled the same settings out to 17,024 designs across four rounds. "
        "Target self-copies ran at 29% of everything generated and "
        "<strong>not one of them ever cleared ipTM 0.80</strong>, so the "
        "score gate that was already there kept them out of every shortlist. "
        "That is the reassuring version: on this target the failure mode is "
        "loud and it is caught. It is worth a look at your own output anyway, "
        "because nothing in the tool is checking for it. "
        "One note on price. This run holds against your wallet at the "
        "per-run ceiling and settles on the GPU seconds it actually used, "
        "which is why 57 minutes bills at a small fraction of the hold you "
        "see at submit. The surplus is released, not spent."
    ),
    "cost_usd": "6.02",
    "runtime": "57 minutes",
}
