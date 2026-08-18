"""Static reference metadata for the RFantibody tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so Phase 2 "About" panels, citation blocks, and cost
previews can import plain-data constants without touching the adapter
contract. Other tools (BindCraft, BoltzGen, PXDesign) will grow their
own ``meta.py`` alongside this one.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"typical_minutes": str}}.
                         ``typical_minutes`` is a human-readable range (e.g.
                         ``"15-60"``) pulled straight from adapter copy.
    paper_citation    — short inline citation.
    paper_url         — bioRxiv permalink for the RFantibody paper.
    github_url        — upstream RosettaCommons repo.
    comparison_one_liner — "pick RFantibody when..." positioning string
                         rendered on the About panel.
    example_output_id — optional job_id of a public demo run to link to
                         from the About panel. Phase 3 will populate this;
                         today it is None.
"""

from __future__ import annotations

from typing import Optional

# Typical wall-clock per preset. Used by the About panel runtime table.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "pilot": {"typical_minutes": "15 to 60"},
}

paper_citation: str = "Bennett et al., bioRxiv 2024"
paper_url: str = "https://www.biorxiv.org/content/10.1101/2024.03.14.585103v2"
github_url: str = "https://github.com/RosettaCommons/RFantibody"
comparison_one_liner: str = (
    "Pick RFantibody when you need a VHH (nanobody) scaffold against a "
    "target PDB. For de novo non-antibody binders, use BindCraft. For "
    "designs involving modified residues or glycans, use BoltzGen."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "RFantibody (Bennett et al., bioRxiv 2024). RoseTTAFold-derived "
        "diffusion model that generates VHH (single-domain heavy-chain "
        "antibody) scaffolds against a target. Outputs are scored with "
        "AF2 re-prediction (pAE, pLDDT, ipAE)."
    ),
    "when_to_use": [
        "You want a VHH (nanobody) scaffold rather than a de novo mini-protein.",
        "Your downstream validation uses yeast display, mammalian display, or hybridoma workflows.",
        "Your target is a standard protein epitope without heavy glycosylation.",
    ],
    "prerequisites": [
        "Target structure (<code>.pdb</code> / <code>.cif</code>).",
        "Chain ID of the target.",
        "At least one hotspot residue defining the epitope face.",
    ],
    "inputs": [
        {
            "name": "Hotspot residues",
            "explanation": (
                "Comma-separated target-chain residues defining the "
                "epitope the CDRs should target."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many candidates to generate. Each passes AF2 "
                "re-prediction filtering on pAE and pLDDT."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "pilot", "typical": "15 to 60 min"},
    ],
    "output_summary": (
        "Ranked VHH candidates with pAE, pLDDT, ipAE, and "
        "downloadable PDBs. Filter at pAE &le; 5 / ipAE &le; 6 for "
        "downstream wet-lab work."
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
    "label": "Starter pilot: 4 nanobodies",
    "goal": (
        "Check that a VHH scaffold can be placed on the face you "
        "picked, before scaling up."
    ),
    "you_need": (
        "A structure file for your target (.pdb or .cif), the chain ID, "
        "and at least one residue defining the face you want bound."
    ),
    "params": {
        "preset": "pilot",
        "num_designs": "4",
    },
    "next_step": (
        "If any of the four scores well, clone the run and raise the "
        "design count."
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
