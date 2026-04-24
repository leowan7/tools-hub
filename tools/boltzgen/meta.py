"""Static reference metadata for the BoltzGen tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so Phase 2 "About" panels, citation blocks, and cost
previews can import plain-data constants without touching the adapter
contract.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"credits": int, "typical_minutes": str}}.
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
    "smoke": {"credits": 3, "typical_minutes": "5"},
    "mini_pilot": {"credits": 10, "typical_minutes": "10"},
    "pilot": {"credits": 10, "typical_minutes": "15-60"},
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
