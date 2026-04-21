"""Yeast display library planner package.

Public API::

    from tools.library_planner import plan_library

    plan = plan_library(
        scaffold="VHH",
        diversification_positions=6,
        diversification_scheme="NNK",
        target_kd_nm=10.0,
        starting_material="naive",
    )
    print(plan["summary"])
"""

from tools.library_planner.planner import plan_library

__all__ = ["plan_library"]
__version__ = "0.1.0"
