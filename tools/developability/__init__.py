"""Binder Developability Scout scoring package.

Public API::

    from tools.developability import score_developability

    result = score_developability("EVQLVESG...", chain_type="VH")
    print(result["composite_score"])

The scoring pipeline combines five independent scientific dimensions
(humanness, liabilities, charge, hydrophobicity, aggregation) into a
single composite score plus per-residue flags. See ``score.py`` for
details.
"""

from tools.developability.score import DEFAULT_WEIGHTS, score_developability

__all__ = ["score_developability", "DEFAULT_WEIGHTS"]
__version__ = "0.1.0"
