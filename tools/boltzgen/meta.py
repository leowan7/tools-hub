"""Static reference metadata for the BoltzGen tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so Phase 2 "About" panels, citation blocks, and cost
previews can import plain-data constants without touching the adapter
contract.

Shapes
------
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

paper_citation: str = "Stark et al., bioRxiv 2025"
paper_url: str = "https://www.biorxiv.org/content/10.1101/2025.11.20.689494v1"
github_url: str = "https://github.com/HannesStark/boltzgen"

seo_faq: list[dict] = [
    {
        "q": "Can I run BoltzGen online without setting up the model locally?",
        "a": (
            # Scaffold names are the form's own <option> labels. The
            # metric scoping that stood here, "on the mini-protein and
            # nanobody protocols", existed to exclude peptide-anything,
            # removed on 2026-11-13. It now names exactly the two protocols
            # that remain, so it excludes nothing and was dropped.
            "Yes. Ranomics Tools runs BoltzGen on a dedicated GPU through "
            "your browser. Upload a target PDB, pick a scaffold class "
            "(mini-protein or nanobody), and candidates "
            "come back with a structure and the generator's interface score "
            "per hit \u2014 plus pLDDT and a refolding RMSD."
        ),
    },
    {
        "q": "Which modalities does BoltzGen design against the same target?",
        "a": (
            "BoltzGen can design mini-proteins or nanobodies "
            "against the same target with "
            "glycan and PTM support. The form switches scaffold class "
            "per run, so you can A/B different modalities cheaply before "
            "committing to wet-lab."
        ),
    },
    {
        "q": "How long does a BoltzGen trial run take?",
        "a": (
            "Runtime depends on target size and the binder format you "
            "picked. You pay for the GPU time your run uses."
        ),
    },
]

comparison_one_liner: str = (
    "You have a target and have not settled on what shape the "
    "binder should be. One model here aims mini-proteins "
    "or nanobodies at the same site, so you "
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
        "you trust it. Two formats share one target — a small "
        "de novo protein or a nanobody — "
        "so you can compare formats on the same epitope rather than "
        "guessing which to commit to. It is the only design tool here "
        "that handles sugars, post-translational modifications and "
        "non-standard residues on the target natively. BoltzGen, "
        "Stark et al., bioRxiv 2025."
    ),
    "when_to_use": [
        (
            "You have not settled on the binder format and want to aim "
            "mini-proteins and nanobodies at the same "
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
            "You can leave the run to finish rather than needing an "
            "answer immediately."
        ),
    ],
    "prerequisites": [
        "Target structure (<code>.pdb</code> / <code>.cif</code>).",
        "Chain ID of the target.",
        # Optional, and said so here because nothing enforces it:
        # ``shared/pdb_preflight_rules.py::_BOLTZGEN`` sets
        # hotspots_required=False, so preflight does not refuse a submit
        # naming none, and ``tools/boltzgen/__init__.py::validate`` accepts
        # an empty field and sends ``hotspot_residues: []`` on. This bullet
        # used to state it as a requirement.
        "Optionally, hotspot residues &mdash; target-chain residues the "
        "binder should contact. Leave the field empty and the run starts "
        "with no hotspot constraint.",
    ],
    "inputs": [
        {
            "name": "Protocol",
            "explanation": (
                # The values named here are exactly
                # ``tools/boltzgen/__init__.py::ALLOWED_PROTOCOLS``, the set
                # ``validate`` refuses anything outside of. The two sets
                # are compared in tests/test_boltzgen_retired_protocols.py,
                # so this list cannot drift from the server's.
                "BoltzGen design protocol. <code>protein-anything</code> "
                "for general mini-protein binders, "
                "<code>nanobody-anything</code> for a VHH-shaped binder. "
                "Neither takes a framework or a starting sequence: this "
                "form has no such field."
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
            # ``_parse_inputs`` in __init__.py::validate refuses anything under
            # 10 and the form inputs carry min="10", so 5 was a number the
            # tool rejects — and this block renders on the SAME page as the
            # form, so the two said different things one scroll apart.
            "explanation": (
                "Residue-count window for the generated binder. Typical "
                "starting ranges: mini-protein 50 to 100, nanobody "
                "110 to 130. "
                "10 is the shortest binder this tool accepts."
            ),
        },
        {
            "name": "Budget (designs)",
            "explanation": (
                # NOT "higher budgets cost more" — that shipped, and the
                # pilot card on the same page said the opposite. The
                # estimator returns $6.0690 at budget 1, 4, 10, 50 and
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
                # actual. So the copy says the search size does not
                # change, which is checkable here, rather than "does not
                # change what the run costs", which is a very likely
                # inference about another repo dressed as a measurement.
                # Closing it properly is a gpu_seconds comparison across
                # two budgets on the next real run.
                "How many of the ranked candidates come back to you "
                "(1 to 50). This only chooses how many you receive: "
                "BoltzGen is asked for the same 200 candidates at every "
                "setting, so the budget does not change the size of the "
                "search."
            ),
        },
    ],
    # "signals self-consistent BINDING" was the refold claim again, in a
    # third place. The refold folds the binder alone, so it says the design
    # folds to the shape it was designed as -- nothing about whether it binds.
    #
    # ONE RMSD NUMBER, AND IT IS THE ONE THE VERDICTS ARE COMPUTED FROM.
    # This sentence read "at or under 2 &Aring; it clears the RMSD leg of the
    # pass bar" -- the CONTAINER's RMSD_THRESHOLD (llm-proteinDesigner/docker/
    # boltzgen/run_pipeline.py, no line number, that repo moves on its own;
    # ``CONTAINER_GATES`` in tests/test_derived_verdicts.py names that constant
    # and reads its value out of the sibling checkout, skipping when that repo
    # is absent, so the 2.0 above is a cross-repo reading and not a number this
    # repo pins) -- while every verdict beside it comes from the legend's
    # ``good`` of 1.5: shared/score_legends.py ("boltzgen", "refolding_rmsd"),
    # named in ``GATE_COLUMNS["boltzgen"]``. So a design at 1.8 &Aring; read
    # "clears the RMSD leg" on this page and fell short in the cell.
    #
    # An earlier version of this comment called that a choice between two real
    # bars to be labelled rather than a contradiction. It is not: the
    # container's 2.0 labels ``filter_status`` and nothing else, and that word
    # reaches NO surface a reader can check this sentence against. Nothing in
    # shared/, blueprints/ or templates/ may read it for a verdict (the block
    # above ``GATE_COLUMNS`` in shared/score_legends.py, greped by
    # tests/test_derived_verdicts.py) and shared/exports.py drops it from every
    # export (``_STALE_VERDICT_KEYS``). So it is not named here at all.
    #
    # THE COMPARATOR SITS IMMEDIATELY AFTER THE METRIC ON PURPOSE.
    # tests/test_guide_bar_matches_legend.py reads a stated threshold only
    # when it does, and that adjacency is what stops ordinary prose being
    # read as a bar. Putting a clause between the two -- which is how the
    # old sentence was written -- takes this line back out of that guard.
    "output_summary": (
        "Ranked candidate binders with ipTM, pLDDT, refolding RMSD, and "
        "structures downloadable from a run of your own &mdash; mmCIF or "
        "PDB, whichever the container wrote. Refolding RMSD at or under "
        "1.5 &Aring; clears the RMSD leg of the pass bar, which also needs "
        "pLDDT at or above 80; the number is the design measured against "
        "its own refold. That says the binder folds as designed, not that "
        "it binds &mdash; re-fold a shortlist against your target to check "
        "that. "
        # This milestone began as "BoltzGen shows only four numbers, add the
        # hydrophobic-patch figure", and the answer turned out to be that
        # BoltzGen is one of the tools that cannot have it. Said here rather
        # than left as a silent absence. The patch is measured on the designed
        # chain ALONE (shared/surface_patch.py detaches every other chain), so
        # naming that chain is a precondition; the rules that name it are
        # shared/design_quality.py::designed_chains.
        "No hydrophobic-patch screen is shown for these designs: that "
        "figure is measured on the designed chain by itself, and a BoltzGen "
        "result does not say which of its chains that is."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}


# ---------------------------------------------------------------------------
# PILOT — the guided starter recipe rendered by
# templates/components/pilot_card.html.
#
# NO PRICE AND NO RUNTIME STRING BELONGS IN THIS DICT.
#
# ``params`` keys are FORM FIELD NAMES. The same dict pre-fills the
# form via ?pilot=1 and feeds the estimator, and the form posts those
# same names to /api/wallet/estimate. Only include keys the form
# actually honours through pre_value()/pre_checked(); a key no field
# reads is a pre-fill that silently does nothing.
# ---------------------------------------------------------------------------
PILOT: dict | None = {
    "label": "A guided first run",
    "goal": (
        "Check that your target and the face you picked produce designs "
        "at all, on the one model that also handles glycans, modified "
        "residues and non-canonical chemistry. This is a guided first "
        "run with the settings filled in &mdash; the same run the form "
        "produces on its defaults, not a reduced-scope trial."
    ),
    "you_need": (
        "A structure file for your target (.pdb or .cif), the chain ID, "
        "and at least one residue on the face you want bound."
    ),
    # Identical to the form's defaults, and measured to be unavoidable.
    # BoltzGen has exactly one preset, and ``budget`` selects how many
    # of the 200 generated candidates come back — build_payload pins
    # that 200 regardless — so the estimate is flat at $6.07 for every
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
        "it on a later run buys no bigger search."
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
        "The target is human PD-L1, the IgV domain, chain A of <strong>PDB "
        "4ZQK</strong> (UniProt Q9NZQ7), which is 115 modelled residues in "
        "crystal numbering 18-132, with hotspots Ile54, Tyr56 and Met115."
    ),
    "inputs_used": [
        (
            "Target PDB",
            "the 4ZQK file, uploaded whole",
            "We uploaded the 4ZQK file whole, with both chains, exactly as "
            "it downloads from the PDB. There was no trimming and no "
            "renumbering, so chain A keeps its crystal numbering 18-132, "
            "which is the numbering the hotspot field expects.",
        ),
        (
            "Target chain",
            "A",
            "We set the target chain to A, and that is what restricts the "
            "design to PD-L1. Chain B in the same file is PD-1, and the run "
            "ignores it, because otherwise the model would be designing into "
            "an occupied site.",
        ),
        (
            "Hotspot residues",
            "54, 56, 115",
            "We named residues 54, 56 and 115, which are Ile54, Tyr56 and "
            "Met115 in the file's own numbering. Three is a normal number, "
            "enough to aim the binder at one patch and few enough that you "
            "have not drawn the interface yourself.",
        ),
        (
            "Binder length min",
            "50",
            "We set the minimum length to 50, so the generator gets a window "
            "rather than a fixed number and chooses within it.",
        ),
        (
            "Binder length max",
            "70",
            "We set the maximum length to 70. The top design came back at 57 "
            "residues, in the middle of the window rather than pinned to "
            "either end.",
        ),
        (
            "Budget (final candidates)",
            "5",
            "We set the budget to 5 final candidates, which is how many "
            "designs to keep. BoltzGen generated and scored 200 to return "
            "these 5, ranked by ipTM, so you are seeing the top of a much "
            "larger pile. The guided trial run on this page uses 4.",
        ),
    ],
    "what_came_back": (
        "<strong>Read the banner above the table first: every one of these "
        "five falls short of the bar, and the banner is right.</strong> The "
        "pass bar wants pLDDT at or above 80 and none of these reaches it, "
        "because they run 69.1 to 78.2. Nothing here is a design you would "
        "order. "
        "What the run did do is aim. The delivered complex puts the binder "
        "on <strong>Ile54, Tyr56 and Met115</strong>, all three "
        "residues we asked for, at 3.8, 2.3 and 3.3 &Aring;, along "
        "with Glu58, Arg113 and Tyr123, part of the same face PD-1 itself "
        "covers. On a target whose natural partner sits in the next chain "
        "of the same file, that is checkable rather than asserted, and it "
        "is what a trial run is for: the epitope is reachable, so a bigger run "
        "is worth paying for. Four of the five also refold to under "
        "1.5 &Aring; of the pose they were designed in, the tightest at "
        "0.51 &Aring;. "
        "Running 200 designs against a "
        "115-residue target is the heavy end of a trial run, and you pay "
        "for the GPU time your run uses."
    ),
    "how_to_read_it": (
        "<strong>pLDDT is the column that fails here.</strong> Above 80 is "
        "confidently folded and none of these five reaches it, though a "
        "refolding RMSD under 1.5 &Aring; says each design wants its own "
        "shape with no target present. The ipTM of 0.538 to 0.653 is the "
        "generator's own score, not on a cofold scale."
    ),
    "cost_usd": "6.00",
    "runtime": "82 minutes",
}
