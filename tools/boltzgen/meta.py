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
    "mini_pilot": {"typical_minutes": "10"},
    "pilot": {"typical_minutes": "15-60"},
}

paper_citation: str = "Wohlwend et al., MIT (2024)"
paper_url: str = "https://github.com/jwohlwend/boltz"
github_url: str = "https://github.com/jwohlwend/boltz"
comparison_one_liner: str = (
    "Pick BoltzGen when your target involves glycans, post-translational "
    "modifications, or non-canonical residues. For standard protein-only "
    "targets, BindCraft or RFantibody are faster and cheaper."
)
example_output_id: Optional[str] = None


# Structured about-panel content. Consumed by the shared
# components/about_panel.html macro on the form page.
about: dict = {
    "what_it_is": (
        "BoltzGen (Wohlwend et al., MIT 2024). Boltz-2 binder design "
        "&mdash; jointly generates a binder backbone against a target, "
        "refolds each candidate end-to-end, and scores affinity via "
        "ipTM and pLDDT. Handles glycans, post-translational "
        "modifications, and non-canonical residues natively."
    ),
    "when_to_use": [
        "Your target has glycans, PTMs, modified residues, or non-canonical chemistry.",
        "You want refolding RMSD as a self-consistency signal alongside ipTM and pLDDT.",
        "You need ~5 to 60 min per run and a budget-tunable number of candidates.",
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
                "Comma-separated target-chain residue indices the binder "
                "should contact. Click residues in the 3D viewer to toggle."
            ),
        },
        {
            "name": "Binder length (min/max)",
            "explanation": (
                "Residue-count window for the generated binder. Default "
                "55&ndash;65 is a good starting range for compact binders."
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
        {"preset": "mini_pilot", "typical": "~10 min"},
        {"preset": "pilot", "typical": "15&ndash;60 min"},
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
