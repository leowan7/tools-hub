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
    "Nature 620, 1089-1100 (2023). "
    "Composite pipeline: RFdiffusion backbones + ProteinMPNN sequences + AF2 multimer validation."
)

paper_url: str = "https://www.nature.com/articles/s41586-023-06415-8"

github_url: str = "https://github.com/RosettaCommons/RFdiffusion"

# One-line decision helper shown in the "About" panel.
comparison_one_liner: str = (
    "Pick RFdiffusion when you want general de novo binder design "
    "scored by AF2 multimer (ipTM / pLDDT / i_pAE). For antibody and "
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
        "slug": "smoke",
        "label": "Smoke",
        "runtime": "~2 min",
        "target": "PD-L1 IgV reference (baked)",
    },
    {
        "slug": "mini_pilot",
        "label": "Preview",
        "runtime": "~7 min",
        "target": "PD-L1 IgV reference (baked)",
    },
    {
        "slug": "pilot",
        "label": "Pilot",
        "runtime": "15-30 min",
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
        "multimer scoring, so every candidate carries real ipTM / "
        "pLDDT / i_pAE statistics from the AF2 re-prediction stage."
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
                "Residue-count window. 55&ndash;65 is a sane default for "
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
        {"preset": "smoke", "typical": "~2 min"},
        {"preset": "mini_pilot", "typical": "~7 min"},
        {"preset": "pilot", "typical": "15&ndash;30 min"},
    ],
    "output_summary": (
        "Ranked candidates with ipTM, pLDDT, i_pAE, and downloadable "
        "PDBs. Aim for at least 1/5 ipTM &ge; 0.65 on a tractable "
        "target before committing to a full pilot."
    ),
    "paper_citation": paper_citation,
    "paper_url": paper_url,
    "github_url": github_url,
}
