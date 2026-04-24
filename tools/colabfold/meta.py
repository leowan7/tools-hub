"""Static reference metadata for the ColabFold (D3) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/mpnn/meta.py``.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"credits": int, "typical_minutes": str}}.
    paper_citation    — short inline citation.
    paper_url         — Nature Methods / bioRxiv permalink.
    github_url        — upstream ColabFold repository.
    comparison_one_liner — "pick ColabFold when..." positioning string.
    example_output_id — optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

# Credits + typical wall-clock. Source of truth for the credit counts is
# the ``Preset.credits_cost`` values in ``__init__.py`` — keep in sync.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "smoke": {"credits": 0, "typical_minutes": "1-2"},
    "standalone": {"credits": 2, "typical_minutes": "1-2"},
}

paper_citation: str = "Mirdita et al., Nature Methods 2022"
paper_url: str = "https://www.nature.com/articles/s41592-022-01488-1"
github_url: str = "https://github.com/sokrypton/ColabFold"
comparison_one_liner: str = (
    "Pick ColabFold when you need a fast no-MSA fold — 1-2 min per run, "
    "no MMseqs2 round-trip. Pair with AF2 standalone (D2) when you want "
    "full MSA + templates, or with ESMFold (D4) for single-sequence "
    "monomers on an even smaller GPU."
)
example_output_id: Optional[str] = None
