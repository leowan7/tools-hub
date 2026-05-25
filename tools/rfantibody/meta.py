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
    "smoke": {"typical_minutes": "3"},
    "mini_pilot": {"typical_minutes": "7"},
    "pilot": {"typical_minutes": "15-60"},
}

paper_citation: str = "Bennett et al., bioRxiv 2024"
paper_url: str = "https://www.biorxiv.org/content/10.1101/2024.03.14.585103v2"
github_url: str = "https://github.com/RosettaCommons/RFantibody"
comparison_one_liner: str = (
    "Pick RFantibody when you need an antibody scaffold (VHH or scFv) "
    "against a target PDB. For de novo non-antibody binders, use "
    "BindCraft. For designs involving modified residues or glycans, use "
    "BoltzGen."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "RFantibody (Bennett et al., bioRxiv 2024). RoseTTAFold-derived "
        "diffusion model that generates VHH (single-domain) and scFv "
        "antibody scaffolds against a target. Outputs are scored with "
        "AF2 re-prediction (pAE, pLDDT, ipAE)."
    ),
    "when_to_use": [
        "You want an antibody scaffold (VHH or scFv) rather than a de novo mini-protein.",
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
            "name": "Scaffold",
            "explanation": (
                "VHH (single-domain heavy-chain antibody) or scFv "
                "(single-chain Fv with linked VH and VL). VHH is "
                "smaller, easier to express; scFv has higher avidity "
                "potential."
            ),
        },
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
        {"preset": "smoke", "typical": "~3 min"},
        {"preset": "mini_pilot", "typical": "~7 min"},
        {"preset": "pilot", "typical": "15&ndash;60 min"},
    ],
    "output_summary": (
        "Ranked antibody candidates with pAE, pLDDT, ipAE, and "
        "downloadable PDBs. Filter at pAE &le; 5 / ipAE &le; 6 for "
        "downstream wet-lab work."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}
