"""Static reference metadata for the BoltzGen tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so Phase 2 "About" panels, citation blocks, and cost
previews can import plain-data constants without touching the adapter
contract.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"typical_minutes": str}}.
                         ``typical_minutes`` is a human-readable range
                         (e.g. ``"15-60"``).
    paper_citation    — short inline citation.
    paper_url         — bioRxiv permalink for the BoltzGen preprint.
    github_url        — upstream HannesStark/boltzgen repo.
    comparison_one_liner — what you have / what you get, plus
                           which sibling tool to use instead.
    example_output_id — optional job_id of a public demo run; None until
                         Phase 3 populates it.
"""

from __future__ import annotations

from typing import Optional

PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "pilot": {"typical_minutes": "15 to 60"},
}

paper_citation: str = "Stark et al., bioRxiv 2025"
paper_url: str = "https://www.biorxiv.org/content/10.1101/2025.11.20.689494v1"
github_url: str = "https://github.com/HannesStark/boltzgen"

seo_faq: list[dict] = [
    {
        "q": "Can I run BoltzGen online without setting up the model locally?",
        "a": (
            # Scaffold names are the form's own <option> labels, and the
            # metrics are scoped: designfolding_metrics defaults false on the
            # peptide protocol, so pLDDT and refolding RMSD are not emitted
            # there. templates/tools/boltzgen_form.html says the same.
            "Yes. Ranomics Tools runs BoltzGen on a dedicated GPU through "
            "your browser. Upload a target PDB, pick a scaffold class "
            "(mini-protein, nanobody, antibody or peptide), and candidates "
            "come back with a structure and the generator's interface score "
            "per hit \u2014 plus pLDDT and a refolding RMSD on the "
            "mini-protein, nanobody and antibody protocols."
        ),
    },
    {
        "q": "Which modalities does BoltzGen design against the same target?",
        "a": (
            "BoltzGen can design mini-proteins, nanobodies, antibodies "
            "(scFv-class), or peptides against the same target with "
            "glycan and PTM support. The form switches scaffold class "
            "per run, so you can A/B different modalities cheaply before "
            "committing to wet-lab."
        ),
    },
    {
        "q": "How long does a BoltzGen pilot run take?",
        # 15 to 60, matching PRESET_RUNTIME above and the "runtime_table"
        # entry in ``about`` below. Those two are the derived source: the
        # runtime band on the tool page and the pilot card both read them.
        # This answer said 30 to 90 and ``when_to_use`` said 5 to 60, so
        # the same page quoted three different runtimes for one run.
        "a": (
            "Pilot runs typically finish in 15 to 60 minutes on a "
            "dedicated A100, depending on target size and the binder "
            "format you picked. Billing is by the second of GPU time."
        ),
    },
]

comparison_one_liner: str = (
    "You have a target and have not settled on what shape the "
    "binder should be. One model here aims mini-proteins, "
    "nanobodies, antibodies or peptides at the same site, so you "
    "can compare formats instead of guessing. The only design tool "
    "here that handles sugars and modified residues on the target "
    "natively."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    # "already checked against the site you aimed at" was not true of the
    # refold and is the claim the whole tool was read through. BoltzGen runs
    # one refold and it folds the BINDER ALONE — no target in it, so nothing
    # about the site can be checked there. That fold is a self-consistency
    # check, which is worth having and is what it now says. The interface
    # number is the generator's own confidence, not a second opinion, so the
    # honest instruction is to rank on it and re-fold a shortlist.
    "what_it_is": (
        "Designs a binder against your target, then refolds each candidate "
        "from its own sequence so you can see whether it holds the shape it "
        "was designed as. The interface score is the generator's own read, "
        "not a second opinion, so rank on it and re-fold a shortlist before "
        "you trust it. Four formats share one target — a small "
        "de novo protein, a nanobody, an antibody, or a short peptide — "
        "so you can compare formats on the same epitope rather than "
        "guessing which to commit to. It is the only design tool here "
        "that handles sugars, post-translational modifications and "
        "non-standard residues on the target natively. BoltzGen, "
        "Stark et al., bioRxiv 2025."
    ),
    "when_to_use": [
        (
            "You have not settled on the binder format and want to aim "
            "mini-proteins, nanobodies, antibodies and peptides at the same "
            "site."
        ),
        (
            "Your target carries sugars, modified residues or other "
            "chemistry a protein-only model would silently drop."
        ),
        (
            "You want each design refolded on its own, so you can see "
            "whether it folds back to the shape it was designed as."
        ),
        (
            "You can wait 15 to 60 minutes per run."
        ),
    ],
    "prerequisites": [
        "Target structure (<code>.pdb</code> / <code>.cif</code>).",
        "Chain ID of the target.",
        "At least one hotspot residue.",
    ],
    "inputs": [
        {
            "name": "Protocol",
            "explanation": (
                "BoltzGen design protocol. <code>protein-anything</code> "
                "for general mini-protein binders, "
                "<code>nanobody-anything</code> for VHH scaffolds, "
                "<code>antibody-anything</code> for antibody scaffolds, "
                "<code>peptide-anything</code> for short cyclic or "
                "linear peptides."
            ),
        },
        {
            "name": "Hotspot residues",
            "explanation": (
                "Comma-separated target-chain residue indices the binder "
                "should contact. Click residues in the 3D viewer to toggle."
            ),
        },
        {
            "name": "Binder length (min/max)",
            # The peptide floor read 5 here and in boltzgen_form.html.
            # ``_parse_inputs`` in __init__.py:92-95 refuses anything under
            # 10 and the form inputs carry min="10", so 5 was a number the
            # tool rejects — and this block renders on the SAME page as the
            # form, so the two said different things one scroll apart.
            "explanation": (
                "Residue-count window for the generated binder. Typical "
                "starting ranges: mini-protein 50 to 100, nanobody "
                "110 to 130, antibody 110 to 200, peptide 10 to 30. "
                "10 is the shortest binder this tool accepts."
            ),
        },
        {
            "name": "Budget (designs)",
            "explanation": (
                # NOT "higher budgets cost more" — that shipped, and the
                # pilot card on the same page said the opposite. The
                # estimator returns $8.7380 at budget 1, 4, 10, 50 and
                # 200 because it scales on ``num_designs``, which this
                # form never submits and ``build_payload`` pins at 200.
                # tests/test_pilot_recipes.py::TestBudgetDoesNotChangeThePrice
                # measures that rather than trusting this comment.
                #
                # SCOPE OF THE CLAIM. Two things are measured from this
                # repo and both are asserted by that test class: the
                # payload pins the pool at 200 whatever the budget, and
                # the estimate is flat across the whole range. What is
                # NOT measured here is the GPU time the container
                # actually burns — that code lives in
                # llm-proteinDesigner, and users settle at metered
                # actual, not at the estimate. So the copy says "the
                # same estimate", which is checkable here, rather than
                # "does not change what the run costs", which is a very
                # likely inference about another repo dressed as a
                # measurement. Closing it properly is a gpu_seconds
                # comparison across two budgets on the next real run.
                "How many of the ranked candidates come back to you "
                "(1 to 50). This only chooses how many you receive: "
                "BoltzGen is asked for the same 200 candidates at every "
                "setting, so every budget quotes the same estimate."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "pilot", "typical": "15 to 60 min"},
    ],
    # "signals self-consistent BINDING" was the refold claim again, in a
    # third place. The refold folds the binder alone, so it says the design
    # folds to the shape it was designed as -- nothing about whether it binds.
    #
    # And 2 A was a third bar for one metric on one page: the container's pass
    # bar is 2.0, the results legend calls 1.5 good and 1.0 excellent
    # (shared/score_legends.py). Both are real and they mean different things,
    # so name which is which rather than picking one and contradicting the
    # other four lines down the page.
    "output_summary": (
        "Ranked candidate binders with ipTM, pLDDT, refolding RMSD, "
        "and downloadable PDBs. Refolding RMSD is the design against its own "
        "refold: at or under 2 &Aring; it clears the RMSD leg of the pass "
        "bar, which also needs pLDDT at or above 80. Under 1.5 &Aring; the "
        "results tooltip calls it self-consistent. That says the binder folds "
        "as designed, not that it binds &mdash; re-fold a shortlist against "
        "your target to check that."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
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
    "label": "A guided first run",
    "goal": (
        "Check that your target and the face you picked produce designs "
        "at all, on the one model that also handles glycans, modified "
        "residues and non-canonical chemistry. BoltzGen charges one flat "
        "price per run, so this is a guided first run at the tool&rsquo;s "
        "normal cost &mdash; not a cheaper trial."
    ),
    "you_need": (
        "A structure file for your target (.pdb or .cif), the chain ID, "
        "and at least one residue on the face you want bound."
    ),
    # Identical to the form's defaults, and measured to be unavoidable.
    # BoltzGen has exactly one preset, and ``budget`` selects how many
    # of the 200 generated candidates come back — build_payload pins
    # that 200 regardless — so the estimate is flat at $8.74 for every
    # budget from 1 to 50. There is no knob on this form that buys a
    # smaller or cheaper first run; lowering the budget returns fewer
    # designs for the same money, which is strictly worse. See
    # tests/test_pilot_recipes.py::test_a_no_op_pilot_is_only_allowed_
    # when_nothing_cheaper_exists, which proves that rather than
    # trusting this comment.
    "params": {
        "preset": "pilot",
        "budget": "4",
    },
    "next_step": (
        "Read the interface scores on what comes back. <code>budget</code> "
        "only "
        "chooses how many of the candidates are returned to you &mdash; "
        "the run is asked for the same 200 either way &mdash; so raising "
        "it on a later run quotes the same estimate."
    ),
}


# ---------------------------------------------------------------------------
# EXAMPLE — one real past run, rendered by
# templates/components/worked_example.html. None here, deliberately:
# No real completed-run payload for this tool exists anywhere on disk
# (searched 2026-08-18: every .json in the tree, .deploy-logs/, scratch/,
# runs/, tmp/). The fixtures in tests/ are synthetic and the stage JSONs
# under runs/ are pipeline-stage outputs, not job results. Capture one from
# a real run and this becomes a two-file change: example/result.json plus
# the narration below. scripts/capture_example_result.py pulls a succeeded
# run out of the jobs table, scrubs the customer-identifying fields, and
# prints the figures the narration has to match. The results partial is
# already example-safe — the guard lives in the two shared macros, not
# here — so nothing else needs touching.
# ---------------------------------------------------------------------------
# Captured from job 758c45e5 (2026-05-28) with
# scripts/capture_example_result.py. Every figure below is a recorded
# fact about THAT run, read back off the payload or the wallet ledger,
# not an estimate and not a round number someone liked.
#
# The target is a PUBLISHED structure and is named for that reason:
# 4ZQK is the human PD-1/PD-L1 complex, and chain A is PD-L1 (verified
# against RCSB, not recalled). The publishing rule for these pages is
# scores and published references only, which is why the sibling
# pxdesign example has to describe its target instead of naming it.
EXAMPLE: dict | None = {
    "target": (
        "Human PD-L1, the IgV domain, taken as chain A of "
        "<strong>PDB 4ZQK</strong> &mdash; the solved PD-1/PD-L1 complex. "
        "115 modelled residues, crystal numbering 18-132. Hotspots Ile54, "
        "Tyr56 and Met115."
    ),
    "why_this_target": (
        "It is a target you can mark your own homework on. Chain B of the "
        "same file is PD-1, the natural partner, so the three hotspots are "
        "not a guess &mdash; they are residues a real binding protein is "
        "known to cover. That makes one question answerable here that is "
        "usually not: never mind whether these designs are good, did the "
        "model aim where it was pointed?"
    ),
    "inputs_used": [
        (
            "Target PDB",
            "the 4ZQK file, uploaded whole",
            "Both chains, exactly as it downloads from the PDB. No "
            "trimming and no renumbering, so chain A keeps its crystal "
            "numbering 18-132 &mdash; which is the numbering the hotspot "
            "field expects.",
        ),
        (
            "Target chain",
            "A",
            "This is what restricts the design to PD-L1. Chain B in the "
            "same file is PD-1, and the run ignores it &mdash; otherwise "
            "the model would be designing into an occupied site.",
        ),
        (
            "Hotspot residues",
            "54, 56, 115",
            "Ile54, Tyr56 and Met115 in the file's own numbering. Three is "
            "a normal number: enough to aim the binder at one patch, few "
            "enough that you have not drawn the interface yourself.",
        ),
        (
            "Binder length min",
            "50",
            "A window rather than a fixed number, so the generator chooses "
            "within it.",
        ),
        (
            "Binder length max",
            "70",
            "The top design came back at 57 residues, in the middle of the "
            "window rather than pinned to either end.",
        ),
        (
            "Budget (final candidates)",
            "5",
            "How many designs to keep. BoltzGen generated and scored 200 "
            "to return these 5, ranked by ipTM &mdash; you are seeing the "
            "top of a much larger pile. The guided pilot on this page "
            "uses 4.",
        ),
    ],
    "what_came_back": (
        "<strong>Read the banner above the table first: the platform marks "
        "all five of these below threshold, and it is right.</strong> The "
        "pass bar wants pLDDT at or above 80 and none of these reaches it "
        "&mdash; they run 69.1 to 78.2. Nothing here is a design you would "
        "order. "
        "What the run did do is aim. The delivered complex puts the binder "
        "on <strong>Ile54, Tyr56 and Met115</strong> &mdash; all three "
        "residues we asked for, at 3.8, 2.3 and 3.3 &Aring; &mdash; along "
        "with Glu58, Arg113 and Tyr123, part of the same face PD-1 itself "
        "covers. On a target whose natural partner sits in the next chain "
        "of the same file, that is checkable rather than asserted, and it "
        "is what a pilot is for: the epitope is reachable, so a bigger run "
        "is worth paying for. Four of the five also refold to under "
        "1.5 &Aring; of the pose they were designed in, the tightest at "
        "0.51 &Aring;."
    ),
    "how_to_read_it": (
        "<strong>Refolding RMSD is the column with a real bar on it.</strong> "
        "BoltzGen re-folds each design on its own, with no target present, "
        "and measures the drift from the pose it was generated in. Under "
        "1.5 &Aring; the table calls it self-consistent: the design wants "
        "to be that shape whether or not the target is there. Four of these "
        "five clear it; rank 2, at 1.77 &Aring;, does not. "
        "<strong>pLDDT is the one that fails here.</strong> Above 80 is "
        "confidently folded and 0 of 5 make it. A design can sit on the "
        "right epitope and still not be a protein worth synthesising, and "
        "that is exactly this run. "
        "<strong>Do not judge the ipTM against 0.7.</strong> That bar comes "
        "from cofolding, where a model is handed both partners and asked "
        "whether they dock. BoltzGen's is its generator's own score, not on "
        "that scale, which is why this site shows it with no pass bar. The "
        "0.538 to 0.653 here is not “nearly good” &mdash; it is a "
        "reading you can rank designs by and cannot compare against a "
        "cofold number, including the 0.98 quoted further down this page "
        "from a different measurement."
    ),
    "what_we_did_next": (
        "Treated all five as failed, because they are, and kept the "
        "epitope. What 200 designs and 82 minutes bought is the knowledge "
        "that the site is reachable &mdash; the generator put binders on "
        "the residues we named &mdash; and that a 50-70 residue budget "
        "produced nothing foldable enough to order. The next run is a "
        "larger budget on the same hotspots, then an independent cofold of "
        "whatever clears pLDDT 80, and only then SPR or BLI. Nothing on "
        "this page is evidence of binding."
    ),
    "cost_usd": "8.64",
    "runtime": "82 minutes",
}
