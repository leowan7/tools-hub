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
    comparison_one_liner — "pick BoltzGen when..." positioning string.
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
        "a": (
            "Pilot runs typically finish in 30 to 90 minutes on a "
            "dedicated A100, depending on target size and modality. "
            "Billing is by the second of GPU time."
        ),
    },
]

comparison_one_liner: str = (
    "Pick BoltzGen when you want one model that can design "
    "mini-proteins, nanobodies, antibodies, or peptides against the "
    "same target, or when your target involves glycans, "
    "post-translational modifications, or non-canonical residues."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "BoltzGen (Wohlwend et al., MIT 2024). Boltz-2 binder design. "
        "Jointly generates a binder backbone against a target, "
        "refolds each candidate end-to-end, and scores affinity via "
        "ipTM and pLDDT. Ships four design protocols (mini-protein, "
        "nanobody, antibody, peptide) and handles glycans, "
        "post-translational modifications, and non-canonical residues "
        "natively."
    ),
    "when_to_use": [
        "You want one model that can target the same epitope with mini-proteins, nanobodies, antibodies, or peptides.",
        "Your target has glycans, PTMs, modified residues, or non-canonical chemistry.",
        "You want refolding RMSD as a self-consistency signal alongside ipTM and pLDDT.",
        "You need roughly 5 to 60 min per run and a budget-tunable number of candidates.",
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
                "Number of designs Boltz-2 generates and ranks. Higher "
                "budgets cost more and run longer."
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


# Sample binder-design targets the user can load in one click.
examples: list[dict] = [
    {
        "id": "6m0j_E",
        "label": "SARS-CoV-2 RBD (6m0j chain E)",
        "description": (
            "Boltz-2 binder design against the spike RBD. Same hotspots "
            "as the BindCraft / RFdiffusion examples for easy comparison."
        ),
        "filename": "6m0j_E.pdb",
        "params": {
            "target_chain": "E",
            "hotspot_residues": "417,453,486,493,501",
            "binder_length_min": "50",
            "binder_length_max": "70",
            "budget": "8",
        },
    },
]
