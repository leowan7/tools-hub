"""Extended PXDesign metadata for UI panels.

Phase 2 frontend work exposes richer "About this tool" context on the
form page (and the underlying comparison/citation text referenced from
results). The core ``ToolAdapter`` in ``base.py`` is shared across all
tools and intentionally minimal, so per-tool narrative metadata lives
here and is rendered directly by the PXDesign templates.

Citations reflect what the pipeline code actually does, not what would
be plausible. PXDesign itself is a Ranomics-internal binder-design
pipeline (private repo at ``llm-proteinDesigner/backend/pipelines/``);
its scoring stage runs JAX AF2 in initial-guess mode per Bennett et al.
(2023), which is the citation a protein engineer would expect to see.
"""

from __future__ import annotations


# Underlying model — PXDesign is a Ranomics in-house pipeline that
# wraps an RFdiffusion-style backbone generator with JAX AF2 Initial
# Guess (AF2-IG) validation. The generator is private; the AF2-IG
# scoring stage is published.
paper_citation: str = (
    "Bennett, N. R., Coventry, B., Goreshnik, I., et al. "
    "\"Improving de novo protein binder design with deep learning.\" "
    "Nature Communications 14, 2625 (2023). "
    "Ranomics in-house pipeline; scoring stage uses AF2 Initial Guess."
)

paper_url: str = "https://www.nature.com/articles/s41467-023-38328-5"

# PXDesign source lives in the private Kendrew repo
# (llm-proteinDesigner/backend/pipelines/pxdesign.py). No public repo.
github_url: str = ""

# One-line decision helper shown in the "About" panel.
comparison_one_liner: str = (
    "You have a target and you want every single candidate to "
    "arrive with a real AlphaFold2 confidence score against that "
    "target, not a cheaper stand-in. This is the pipeline Ranomics "
    "runs for its own wet-lab campaigns. For design without that "
    "filtering step use BindCraft; for antibody formats use "
    "RFantibody or IgGM."
)

# Optional reference job id that the form page can link to as an
# example output. Populated when a showcase run exists.
example_output_id: str | None = None


# Runtime + cost reference rendered as a table on the form page.
# Values mirror the ``Preset`` tuples in ``__init__.py`` and the
# ``GPU_TIMEOUT`` map in ``gpu/modal_client.py``.
preset_runtime_rows: tuple[dict[str, str], ...] = (
    {
        "slug": "pilot",
        "label": "Pilot",
        "runtime": "30 to 60 min",
        "target": "Your uploaded target",
    },
)


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "Designs binders against your target and then scores every one "
        "of them with AlphaFold2 run against that same target, so "
        "nothing reaches you on a cheaper stand-in number. The scoring "
        "uses AlphaFold2's initial-guess mode, which is far quicker "
        "than a full multimer prediction and is what makes "
        "per-candidate scoring affordable at all. This is the pipeline "
        "Ranomics runs for its own wet-lab campaigns. PXDesign is "
        "in-house; the initial-guess method is Bennett et al., "
        "<em>Nature Communications</em> 2023."
    ),
    "when_to_use": [
        (
            "You want an AlphaFold2 confidence number against your actual "
            "target on every candidate, not on a filtered subset."
        ),
        (
            "You want that scoring faster than a full AlphaFold2 multimer "
            "run on each design."
        ),
        (
            "You want the same pipeline Ranomics runs for its own wet-lab "
            "campaigns."
        ),
    ],
    "prerequisites": [
        "Target structure (<code>.pdb</code> / <code>.cif</code>).",
        "Chain ID of the target.",
        "At least one hotspot residue.",
    ],
    "inputs": [
        {
            "name": "Hotspot residues",
            "explanation": (
                "Comma-separated target-chain residues defining the "
                "epitope. Click in the 3D viewer to toggle."
            ),
        },
        {
            "name": "Binder length",
            "explanation": (
                "Residue count for the generated binder. ~40 residues "
                "is the validated default; PD-L1 published binders are "
                "in this range."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many candidates to score. Higher counts increase "
                "cost and runtime linearly."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "pilot", "typical": "30 to 60 min"},
    ],
    "output_summary": (
        "Ranked candidates with ipTM, pLDDT, pAE, and downloadable PDBs. "
        "Target ipTM &ge; 0.70 on 1 to 2 of 5 designs for a tractable "
        "epitope."
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
    "label": "Starter pilot: 4 designs",
    "goal": (
        "See whether your target and the face you picked give designs "
        "with usable AF2 confidence, before scaling."
    ),
    "you_need": (
        "A structure file for your target (.pdb or .cif), the chain ID, "
        "and at least one residue on the face you want bound."
    ),
    "params": {
        "preset": "pilot",
        "num_designs": "4",
    },
    "next_step": (
        "If any of the four comes back with good interface confidence, "
        "clone the run and raise the design count."
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
EXAMPLE: dict | None = None
