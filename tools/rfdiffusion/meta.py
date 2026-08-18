"""Extended RFdiffusion metadata for UI panels.

The core ``ToolAdapter`` in ``tools/base.py`` is intentionally minimal,
so per-tool narrative metadata lives here and is rendered directly by
the RFdiffusion templates.

Citations reflect what the pipeline code actually does. RFdiffusion is
the public Watson et al. (2023) diffusion-based backbone generator;
the wrapper run on Kendrew composes it with ProteinMPNN sequence
design and JAX AF2 multimer validation, so candidates carry real
ipTM / pLDDT / i_pAE statistics from the AF2 model.
"""

from __future__ import annotations


# Underlying model — RFdiffusion is the public diffusion-based backbone
# generator from the Baker lab. Composite Kendrew pipeline pairs it
# with ProteinMPNN sequence design and AF2 multimer scoring.
paper_citation: str = (
    "Watson, J. L., Juergens, D., Bennett, N. R., et al. "
    "\"De novo design of protein structure and function with RFdiffusion.\" "
    "Nature 620, 1089 to 1100 (2023). "
    "Composite pipeline: RFdiffusion backbones, ProteinMPNN sequences, "
    "and AF2 multimer validation."
)

paper_url: str = "https://www.nature.com/articles/s41586-023-06415-8"

github_url: str = "https://github.com/RosettaCommons/RFdiffusion"


seo_faq: list[dict] = [
    {
        "q": "Can I run RFdiffusion online without installing it locally?",
        "a": (
            "Yes. Ranomics Tools runs the full RFdiffusion + ProteinMPNN + "
            "AF2 multimer pipeline on a dedicated GPU through your browser. "
            "You upload a target PDB, set hotspots and binder length, and "
            "candidates come back with real ipTM, pLDDT, and i_pAE scores."
        ),
    },
    {
        "q": "How much does one RFdiffusion run cost?",
        "a": (
            "Billing is by the second of dedicated GPU time. A pilot run "
            "(~15 to 30 minutes on an A100) typically clears for under a "
            "few dollars from your wallet. New accounts start with a $5 "
            "balance, which covers a first small-target run."
        ),
    },
    {
        "q": "Do I need to choose hotspots before running RFdiffusion?",
        "a": (
            "Hotspots are strongly recommended. They tell RFdiffusion "
            "which residues on the target the binder should contact. If you "
            "do not have a hotspot guess, run Epitope Scout first to score "
            "the target surface, then hand off the picked residues into "
            "the RFdiffusion form."
        ),
    },
]


# One-line decision helper shown in the "About" panel.
comparison_one_liner: str = (
    "Pick RFdiffusion when you want general de novo binder design "
    "scored by AF2 multimer (ipTM, pLDDT, i_pAE). For antibody and "
    "nanobody scaffolds use RFantibody, for AF2-IG initial-guess "
    "scoring use PXDesign, and for hallucination-driven binders "
    "without AF2 filtering use BindCraft."
)

# Optional reference job id linked from the form page as an example.
example_output_id: str | None = None


# Runtime + cost reference rendered as a table on the form page.
# Values mirror the ``Preset`` tuples in ``__init__.py`` and the
# ``PRESET_CAPS`` map in ``gpu/modal_client.py``.
preset_runtime_rows: tuple[dict[str, str], ...] = (
    {
        "slug": "pilot",
        "label": "Pilot",
        "runtime": "15 to 30 min",
        "target": "Your uploaded target",
    },
)


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "RFdiffusion (Watson et al., <em>Nature</em> 2023). "
        "Diffusion-based backbone generator. The Ranomics composite "
        "pipeline pairs it with ProteinMPNN sequence design and AF2 "
        "multimer scoring, so every candidate carries real ipTM, "
        "pLDDT, and i_pAE statistics from the AF2 re-prediction stage."
    ),
    "when_to_use": [
        "You want general de novo binder design with AF2-grounded scoring.",
        "Your target is a standard protein epitope (no glycans, no PTMs).",
        "You want flexible binder length and topology rather than an antibody scaffold.",
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
                "Comma-separated target-chain residues the binder "
                "should contact during diffusion."
            ),
        },
        {
            "name": "Binder length (min/max)",
            "explanation": (
                "Residue-count window. 55 to 65 is a sane default for "
                "compact PD-L1-style targets; longer binders work for "
                "larger interfaces."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many candidates to generate. Each passes "
                "ProteinMPNN sequence design and AF2 multimer scoring."
            ),
        },
    ],
    "runtime_table": [
        {"preset": "pilot", "typical": "15 to 30 min"},
    ],
    "output_summary": (
        "Ranked candidates with ipTM, pLDDT, i_pAE, and downloadable "
        "PDBs. Aim for at least 1 in 5 with ipTM &ge; 0.65 on a "
        "tractable target before committing to a full pilot."
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
    "label": "Starter pilot: 8 binders",
    "goal": (
        "Find out whether your target and the face you picked are "
        "workable, before paying for a large run."
    ),
    "you_need": (
        "A structure file for your target (.pdb or .cif), the chain ID "
        "it sits on, and at least one residue on the face you want the "
        "binder to touch."
    ),
    "params": {
        "preset": "pilot",
        "num_designs": "8",
    },
    "next_step": (
        "Roughly 1 in 5 designs clears the confidence bar on a "
        "tractable target, so 8 is enough to tell. If one or two score "
        "well, clone the run and raise the design count. If none do, "
        "move the hotspots rather than scaling."
    ),
}
