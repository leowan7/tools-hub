"""Static reference metadata for the ProteinMPNN (D1) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/bindcraft/meta.py`` etc.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"credits": int, "typical_minutes": str}}.
    paper_citation    — short inline citation.
    paper_url         — bioRxiv / Science permalink.
    github_url        — upstream ProteinMPNN repository.
    comparison_one_liner — "pick MPNN when..." positioning string.
    example_output_id — optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

# Credits + typical wall-clock. Source of truth for the credit counts is
# the ``Preset.credits_cost`` values in ``__init__.py`` — keep in sync.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "smoke": {"credits": 0, "typical_minutes": "1"},
    "standalone": {"credits": 1, "typical_minutes": "1"},
}

paper_citation: str = "Dauparas et al., Science 2022"
paper_url: str = "https://www.science.org/doi/10.1126/science.add2187"
github_url: str = "https://github.com/dauparas/ProteinMPNN"
comparison_one_liner: str = (
    "Pick ProteinMPNN when you already have a backbone and need candidate "
    "sequences. For de novo backbone generation, use RFantibody, BindCraft, "
    "or BoltzGen first and feed the output PDB here."
)
example_output_id: Optional[str] = None
