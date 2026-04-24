"""Static reference metadata for the AF2 standalone (D2) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/mpnn/meta.py``.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"credits": int, "typical_minutes": str}}.
    paper_citation    — short inline citation.
    paper_url         — Nature permalink.
    github_url        — upstream ColabFold repository (which bundles AF2).
    comparison_one_liner — "pick AF2 when..." positioning string.
    example_output_id — optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

# Credits + typical wall-clock. Source of truth for the credit counts is
# the ``Preset.credits_cost`` values in ``__init__.py`` — keep in sync.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    # Smoke: 58-AA monomer, 1 recycle, no MSA. Expected ~1-2 min cold,
    # <30 s warm on A100-80GB.
    "smoke": {"credits": 0, "typical_minutes": "2"},
    # Standalone: user FASTA, MMseqs2 MSA + 3 recycles. MSA fetch
    # dominates for short sequences; fold time scales with length.
    "standalone": {"credits": 2, "typical_minutes": "5-10"},
}

paper_citation: str = "Jumper et al., Nature 2021 (AF2); Mirdita et al., Nature Methods 2022 (ColabFold)"
paper_url: str = "https://www.nature.com/articles/s41586-021-03819-2"
# ColabFold is the packaging we actually ship — AF2 weights + MMseqs2
# MSA + a clean pip install. The upstream AlphaFold2 repo is linked
# from the ColabFold README.
github_url: str = "https://github.com/sokrypton/ColabFold"
comparison_one_liner: str = (
    "Pick AF2 when you need the gold-standard structure prediction with "
    "calibrated pLDDT + PAE. For faster single-sequence folds use "
    "ESMFold (D4); for affinity-aware folds use Boltz-2 (D6)."
)
example_output_id: Optional[str] = None
