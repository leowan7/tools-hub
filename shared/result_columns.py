"""Per-tool result columns + primary sort key for the merged campaign
results table.

The campaign results page (``templates/runs/detail.html``) pools candidates
from every sub-job into ONE table, so it needs the tool's column set and a
single ranking key up front — the per-tool ``*_results.html`` partials each
hard-code their own ``{% set columns %}`` line, which only works when the whole
table is one job. This module is the shared source of truth for the seven
fan-out (campaign-capable) tools; keep it in sync with the matching
``templates/tools/<tool>_results.html`` column lists.

``direction`` is the sense of the PRIMARY metric only: ``"desc"`` = higher is
better (ipTM, reward, contacts), ``"asc"`` = lower is better (interface pAE).
It drives the global merge order; the per-column display formatting still lives
in the ``candidate_table`` macro.
"""

from __future__ import annotations

from typing import Optional

# Column sets mirror templates/tools/<tool>_results.html. Only the seven tools
# in shared.compute_campaigns.SUPPORTED_TOOLS reach the campaign results page.
_TOOL_RESULT_COLUMNS: dict[str, list[str]] = {
    "rfdiffusion": ["ipTM", "pLDDT", "i_pAE", "filter_status"],
    "bindcraft": ["ipTM", "pLDDT", "RMSD", "shape_complementarity", "SAP"],
    "boltzgen": ["ipTM", "pLDDT", "refolding_rmsd"],
    "pxdesign": ["ipTM", "pLDDT", "pAE", "filter_status"],
    "rfantibody": ["ipAE", "pLDDT", "pAE"],
    "proteina": [
        "total_reward", "af2_iptm", "af2_plddt",
        "rf3_score", "binder_scrmsd", "cluster_id",
    ],
    "iggm": ["epitope_contacts"],
}

# Primary metric each tool's designs are globally ranked by, and its direction.
# Interface pAE (rfantibody) is lower-is-better; every other headline metric is
# higher-is-better.
_TOOL_PRIMARY_METRIC: dict[str, tuple[str, str]] = {
    "rfdiffusion": ("ipTM", "desc"),
    "bindcraft": ("ipTM", "desc"),
    "boltzgen": ("ipTM", "desc"),
    "pxdesign": ("ipTM", "desc"),
    "rfantibody": ("ipAE", "asc"),
    "proteina": ("total_reward", "desc"),
    "iggm": ("epitope_contacts", "desc"),
}


def columns_for(tool: str) -> list[str]:
    """Display columns for a tool's merged campaign table ([] if unknown)."""
    return list(_TOOL_RESULT_COLUMNS.get(tool, []))


def primary_metric_for(tool: str) -> tuple[Optional[str], str]:
    """``(metric_key, direction)`` the tool's designs rank by.

    Falls back to ``(None, "desc")`` for a tool with no registered primary —
    the merge then keeps pipeline order within each sub-job.
    """
    return _TOOL_PRIMARY_METRIC.get(tool, (None, "desc"))


def candidate_metric(cand: object, key: Optional[str]) -> Optional[float]:
    """Numeric value of ``key`` for a candidate, checking ``scores`` then the
    record root (mirrors how the table and filter helpers resolve metrics).
    Returns None when absent or non-numeric so it can sort last."""
    if not isinstance(cand, dict) or not key:
        return None
    scores = cand.get("scores")
    if isinstance(scores, dict) and scores.get(key) is not None:
        val = scores.get(key)
    else:
        val = cand.get(key)
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
