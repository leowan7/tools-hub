"""Static reference metadata for the BindCraft tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so Phase 2 "About" panels, citation blocks, and cost
previews can import plain-data constants without touching the adapter
contract. Parallel to ``tools/rfantibody/meta.py``.

Shapes
------
    PRESET_RUNTIME    — {preset_slug: {"credits": int, "typical_minutes": str}}.
                         ``typical_minutes`` is a human-readable range (e.g.
                         ``"45"``) pulled straight from adapter copy.
    paper_citation    — short inline citation.
    paper_url         — bioRxiv permalink for the BindCraft paper.
    github_url        — upstream repository.
    comparison_one_liner — "pick BindCraft when..." positioning string
                         rendered on the About panel.
    example_output_id — optional job_id of a public demo run to link to
                         from the About panel. Phase 3 will populate this;
                         today it is None.
"""

from __future__ import annotations

from typing import Optional

# Credits and typical wall-clock for each preset. Source of truth for the
# credit counts is the ``Preset.credits_cost`` values in ``__init__.py`` —
# keep in sync by eye when either file changes. BindCraft only ships a
# single ``pilot`` preset today; smoke / mini_pilot are intentionally not
# offered because the pipeline cost floor is ~45 min on A100-80GB.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "pilot": {"credits": 20, "typical_minutes": "45"},
}

paper_citation: str = "Pacesa et al., bioRxiv 2024"
paper_url: str = "https://www.biorxiv.org/content/10.1101/2024.09.30.615802v1"
github_url: str = "https://github.com/martinpacesa/BindCraft"
comparison_one_liner: str = (
    "Pick BindCraft when you have a target PDB plus known hotspot "
    "residues and want de novo 60-150 aa protein binders."
)
example_output_id: Optional[str] = None
