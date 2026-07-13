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
    comparison_one_liner — "pick BindCraft when..." positioning string
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
    "Pick BindCraft when you have a target PDB plus known hotspot "
    "residues and want de novo 60 to 150 aa protein binders."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "BindCraft (Pacesa et al., bioRxiv 2024). De novo binder design "
        "via AlphaFold2-Multimer hallucination with hotspot-focused "
        "backpropagation, followed by ProteinMPNN sequence design and "
        "AF2 re-prediction filtering."
    ),
    "when_to_use": [
        "You have a target PDB and at least one hotspot residue you want the binder to contact.",
        "You want de novo 50 to 150 aa protein binders (not antibodies).",
        "You can wait ~45 min per pilot run and want filtered hits with ipTM and pLDDT above the BindCraft default thresholds.",
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
