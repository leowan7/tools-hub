"""Static reference metadata for the ESMFold (D4) tool.

Kept separate from ``__init__.py`` (which owns the :class:`ToolAdapter`
registration) so About panels, citation blocks, and cost previews can
import plain-data constants without touching the adapter contract.
Parallel to ``tools/colabfold/meta.py``.

Shapes
------
    PRESET_RUNTIME    - {preset_slug: {"credits": int, "typical_minutes": str}}.
    paper_citation    - short inline citation.
    paper_url         - Science / bioRxiv permalink.
    github_url        - upstream ESM repository.
    comparison_one_liner - "pick ESMFold when..." positioning string.
    example_output_id - optional job_id of a public demo run (None today).
"""

from __future__ import annotations

from typing import Optional

# Credits + typical wall-clock. Source of truth for the credit counts is
# the ``Preset.credits_cost`` values in ``__init__.py`` - keep in sync.
PRESET_RUNTIME: dict[str, dict[str, object]] = {
    "smoke": {"credits": 0, "typical_minutes": "0.5-1"},
    "standalone": {"credits": 1, "typical_minutes": "0.5-1"},
}

paper_citation: str = "Lin et al., Science 2023"
paper_url: str = "https://www.science.org/doi/10.1126/science.ade2574"
github_url: str = "https://github.com/facebookresearch/esm"
comparison_one_liner: str = (
    "Pick ESMFold when you need the fastest possible monomer fold - no "
    "MSA, no multimer, single-sequence ESM-2 language-model prediction. "
    "Pair with ColabFold (D3) for multimers or AF2 standalone (D2) for "
    "full MSA-backed accuracy."
)
example_output_id: Optional[str] = None
