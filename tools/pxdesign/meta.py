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
    "Pick PXDesign when AF2 confidence against a defined target "
    "matters and you want real ipTM / pLDDT / pAE on every candidate. "
    "For hallucination-driven binder design without AF2 filtering use "
    "BindCraft, for antibody and nanobody CDRs use RFantibody, and for "
    "target structure generation without binder design use BoltzGen."
)

# Optional reference job id that the form page can link to as an
# example output. Populated when a showcase run exists.
example_output_id: str | None = None


# Runtime + cost reference rendered as a table on the form page.
# Values mirror the ``Preset`` tuples in ``__init__.py`` and the
# ``GPU_TIMEOUT`` map in ``gpu/modal_client.py``.
preset_runtime_rows: tuple[dict[str, str], ...] = (
    {
        "slug": "smoke",
        "label": "Smoke",
        "credits": "8",
        "runtime": "~17 min",
        "target": "PD-L1 IgV reference (baked)",
    },
    {
        "slug": "mini_pilot",
        "label": "Preview",
        "credits": "16",
        "runtime": "~35 min",
        "target": "PD-L1 IgV reference (baked)",
    },
    {
        "slug": "pilot",
        "label": "Pilot",
        "credits": "15",
        "runtime": "30–60 min",
        "target": "Your uploaded target",
    },
)
