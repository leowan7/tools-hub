"""Binder Developability Scout scoring package.

Public API::

    from shared.developability import score_developability

    result = score_developability("EVQLVESG...", chain_type="VH")
    print(result["composite_score"])

The scoring pipeline combines four independent scientific dimensions
(liabilities, charge, hydrophobicity, aggregation) into a single
composite score plus per-residue flags. See ``score.py`` for details,
including why there is no human-likeness dimension.
"""

from shared.developability.score import DEFAULT_WEIGHTS, score_developability

__all__ = ["score_developability", "DEFAULT_WEIGHTS"]
__version__ = "0.1.0"
