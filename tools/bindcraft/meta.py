"""Static reference metadata for the BindCraft tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so Phase 2 "About" panels, citation blocks, and cost
previews can import plain-data constants without touching the adapter
contract. Parallel to ``tools/rfantibody/meta.py``.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"typical_minutes": str}}.
                         ``typical_minutes`` is a human-readable range (e.g.
                         ``"45"``) pulled straight from adapter copy.
    paper_citation    — short inline citation.
    paper_url         — bioRxiv permalink for the BindCraft paper.
    github_url        — upstream repository.
    comparison_one_liner — what you have / what you get, plus
                           which sibling tool to use instead.
                         rendered on the About panel.
    example_output_id — optional job_id of a public demo run to link to
                         from the About panel. Phase 3 will populate this;
                         today it is None.
"""

from __future__ import annotations

from typing import Optional

# Typical wall-clock per preset. BindCraft ships only the ``pilot``
# preset; the pipeline cost floor is ~45 min on A100-80GB.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "pilot": {"typical_minutes": "45"},
}

paper_citation: str = "Pacesa et al., bioRxiv 2024"
paper_url: str = "https://www.biorxiv.org/content/10.1101/2024.09.30.615802v1"
github_url: str = "https://github.com/martinpacesa/BindCraft"

seo_faq: list[dict] = [
    {
        "q": "Can I run BindCraft online without installing it?",
        "a": (
            "Yes. Ranomics Tools runs the full BindCraft hallucination "
            "loop on a dedicated GPU through your browser. Upload a target "
            "PDB, pick hotspots and binder length, and candidates come "
            "back with AF2 ipTM and pLDDT on every hit."
        ),
    },
    {
        "q": "BindCraft vs RFdiffusion: which should I pick?",
        "a": (
            "Both design de novo binders against your target. BindCraft "
            "hallucinates sequences and backbones jointly inside AF2; "
            "RFdiffusion samples a backbone first then drops sequences in "
            "with ProteinMPNN. Run both on the same hub and compare ipTM "
            "before committing to wet-lab."
        ),
    },
    {
        "q": "How long does a BindCraft pilot take?",
        "a": (
            "Typical pilot runs finish in roughly 20 to 60 minutes on a "
            "dedicated A100, depending on target size and how many "
            "candidates pass the internal ipTM filter. Billing is by the "
            "second so a faster preset costs less."
        ),
    },
]

comparison_one_liner: str = (
    "You have a target structure and know roughly which patch of "
    "its surface you want gripped, and you want brand-new "
    "mini-proteins of 60 to 150 residues built to grip it. Every "
    "candidate is refolded and filtered before you see it, so what "
    "comes back is already a shortlist."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Designs brand-new mini-proteins that grip a patch of your "
        "target. It runs AlphaFold2 multimer backwards — pushing a "
        "random starting sequence toward one the model believes will "
        "bind the residues you named — then assigns a real sequence "
        "with ProteinMPNN and refolds every candidate to check the "
        "answer survives. What reaches you has already been filtered on "
        "interface confidence and fold quality. BindCraft, Pacesa et "
        "al., bioRxiv 2024."
    ),
    "when_to_use": [
        (
            "You have a target structure and at least one residue on its "
            "surface you want the binder to touch."
        ),
        (
            "You want a small de novo protein of 50 to 150 residues, not an "
            "antibody."
        ),
        (
            "You can wait about 45 minutes for a first run, and you would "
            "rather see a filtered shortlist than every candidate the run "
            "generated."
        ),
    ],
    "prerequisites": [
        "Target structure as <code>.pdb</code>, <code>.cif</code>, or <code>.mmcif</code>.",
        "Chain ID of the target within that structure.",
        "At least one hotspot residue index on the target chain.",
    ],
    "inputs": [
        {
            "name": "Hotspot residues",
            "explanation": (
                "Comma-separated target-chain residue indices the binder "
                "should contact (e.g. <code>54,56,115</code>). These "
                "bias AF2 backpropagation toward the intended epitope. "
                "Click residues in the 3D viewer to toggle them."
            ),
        },
        {
            "name": "Binder length (min/max)",
            "explanation": (
                "Residue-count window for the generated binder chain "
                "(50 to 150). Shorter binders are easier to validate "
                "in yeast display; longer ones can target larger interfaces."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many final filtered designs to return (1 to 5). "
                "Each passes AF2 re-prediction with ipTM and pLDDT above "
                "the BindCraft default thresholds. Pipeline cost floor "
                "is ~45 min regardless of count."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "pilot", "typical": "~45 min"},
    ],
    "output_summary": (
        "Filtered candidate binders with ipTM, pLDDT, shape complementarity, "
        "and downloadable PDBs. Hand off promising designs to the Ranomics "
        "yeast display CRO for in vitro validation."
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
    "label": "Starter pilot: 2 trajectories",
    "goal": (
        "Check that your target parses, your hotspots resolve against "
        "it, and the pipeline returns scored designs &mdash; before "
        "committing to a run that hunts for hits."
    ),
    "you_need": (
        "A structure file for your target (.pdb, .cif or .mmcif), the "
        "chain ID it sits on, and at least one residue on the face you "
        "want the binder to touch."
    ),
    # 2, not the form's default of 4. BindCraft prices in whole
    # containers of two trajectories (wallet_estimates designs_per_run_
    # baseline=2), so 2 is one container — the smallest complete unit of
    # work — and exactly half the price of the default. A pilot that
    # restated the default was a button promising a change it did not
    # make.
    "params": {
        "preset": "pilot",
        "num_designs": "2",
    },
    "next_step": (
        "Two trajectories tell you the setup works, not whether the "
        "target is bindable &mdash; BindCraft burns more GPU per design "
        "than anything else here, which is why the first step is this "
        "small. Once it comes back clean, clone the run and raise the "
        "count."
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
