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


# Sample VHH / nanobody design targets the user can load in one click.
examples: list[dict] = [
    {
        "id": "6m0j_E",
        "label": "SARS-CoV-2 RBD (6m0j chain E)",
        "description": (
            "Nanobody design against the spike RBD. Same target as "
            "the canonical RFantibody paper benchmark."
        ),
        "filename": "6m0j_E.pdb",
        "params": {
            "target_chain": "E",
            "hotspot_residues": "417,453,486,493,501",
            "cdr_lengths": "H1:8,H2:7,H3:10-16",
            "num_designs": "8",
        },
    },
]
