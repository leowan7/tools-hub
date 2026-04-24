"""Static reference metadata for the RFantibody tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so Phase 2 "About" panels, citation blocks, and cost
previews can import plain-data constants without touching the adapter
contract. Other tools (BindCraft, BoltzGen, PXDesign) will grow their
own ``meta.py`` alongside this one.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"credits": int, "typical_minutes": str}}.
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

# Credits and typical wall-clock for each preset. Source of truth for the
# credit counts is the ``Preset.credits_cost`` values in ``__init__.py`` —
# keep in sync by eye when either file changes.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "smoke": {"credits": 2, "typical_minutes": "3"},
    "mini_pilot": {"credits": 8, "typical_minutes": "7"},
    "pilot": {"credits": 15, "typical_minutes": "15-60"},
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
