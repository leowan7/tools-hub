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
comparison_one_liner: str = (
    "Pick BindCraft when you have a target PDB plus known hotspot "
    "residues and want de novo 60-150 aa protein binders."
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
        "You want de novo 50&ndash;150 aa protein binders (not antibodies).",
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
                "(50&ndash;150). Shorter binders are easier to validate "
                "in yeast display; longer ones can target larger interfaces."
            ),
        },
        {
            "name": "Number of designs",
            "explanation": (
                "How many final filtered designs to return (1&ndash;5). "
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


# Sample binder-design targets the user can load in one click.
examples: list[dict] = [
    {
        "id": "6m0j_E",
        "label": "SARS-CoV-2 RBD (6m0j chain E)",
        "description": (
            "Classic de novo binder design benchmark; targets the "
            "ACE2 interface on the spike RBD."
        ),
        "filename": "6m0j_E.pdb",
        "params": {
            "target_chain": "E",
            "hotspot_residues": "417,453,486,493,501",
            "binder_length_min": "50",
            "binder_length_max": "100",
            "num_designs": "8",
        },
    },
    {
        "id": "4z18_A",
        "label": "PD-L1 ectodomain (4z18 chain A)",
        "description": (
            "Immuno-oncology target. Hotspots on the PD-1 binding "
            "interface."
        ),
        "filename": "4z18_A.pdb",
        "params": {
            "target_chain": "A",
            "hotspot_residues": "67,113,121",
            "binder_length_min": "50",
            "binder_length_max": "100",
            "num_designs": "8",
        },
    },
]
