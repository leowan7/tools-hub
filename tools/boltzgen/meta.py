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
    paper_url         — link to the Boltz preprint / repo.
    github_url        — upstream jwohlwend/boltz repo.
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

paper_citation: str = "Wohlwend et al., MIT (2024)"
paper_url: str = "https://github.com/jwohlwend/boltz"
github_url: str = "https://github.com/jwohlwend/boltz"

seo_faq: list[dict] = [
    {
        "q": "Can I run BoltzGen online without setting up the model locally?",
        "a": (
            "Yes. Ranomics Tools runs BoltzGen on a dedicated GPU through "
            "your browser. Upload a target PDB, pick a scaffold class "
            "(mini-binder, VHH, scFv, or peptide), and candidates come "
            "back with structure + affinity-like scores per hit."
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
    "what_it_is": (
        "Designs a binder against your target and refolds it inside the "
        "same model, so every candidate arrives already checked against "
        "the site you aimed at. Four formats share one target — a small "
        "de novo protein, a nanobody, an antibody, or a short peptide — "
        "so you can compare formats on the same epitope rather than "
        "guessing which to commit to. It is the only design tool here "
        "that handles sugars, post-translational modifications and "
        "non-standard residues on the target natively. BoltzGen, "
        "Wohlwend et al., MIT 2024."
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
                "Boltz-2 design protocol. <code>protein-anything</code> "
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
            "explanation": (
                "Residue-count window for the generated binder. Typical "
                "starting ranges: mini-protein 50 to 100, nanobody "
                "110 to 130, antibody 110 to 200, peptide 5 to 30."
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
    "output_summary": (
        "Ranked candidate binders with ipTM, pLDDT, refolding RMSD, "
        "and downloadable PDBs. Refolding RMSD &lt; 2 &Aring; on the "
        "top design typically signals self-consistent binding."
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
# the narration below.
# ---------------------------------------------------------------------------
EXAMPLE: dict | None = None
