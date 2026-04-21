"""Impossibility and risk detection for a proposed library plan.

Given a partially assembled plan dictionary (as produced by planner._build),
this module returns a list of warning records. Each record has a
``severity`` field: ``error`` means the plan cannot proceed as designed;
``warning`` means the plan is risky but can still be attempted.

Scientific references behind the thresholds:

- Yeast transformation ceiling ~1e8 transformants per standard high-
  efficiency lithium acetate protocol (Chao et al. 2006 Nat Protoc;
  Benatuil et al. 2010 Protein Eng Des Sel extends this to ~1e9 with
  electroporation). We default to 1e8 and tolerate a 100x miss before
  flagging as impossible.
- Picomolar KD is near the selection floor for yeast display because the
  avidity of two-dye FACS staining and the wash-off kinetics no longer
  discriminate monomer KD differences below ~10 pM (Boder et al. 2000 PNAS;
  Hanes and Pluckthun 1997 for ribosome display comparison).
- Trimer synthesis cost scales with positions because each position requires
  custom oligos. 12 positions is the conventional cost ceiling.
"""

from __future__ import annotations

import math
from typing import List


def check_feasibility(plan: dict) -> List[dict]:
    """Inspect a proposed plan and return a list of flags.

    Args:
        plan: Partial plan dict with ``inputs`` and ``library`` subkeys.

    Returns:
        List of dicts with ``severity``, ``code``, and ``message``.
    """
    if not isinstance(plan, dict):
        raise ValueError("plan must be a dict")
    if "inputs" not in plan or "library" not in plan:
        raise ValueError("plan must contain 'inputs' and 'library' keys")

    inputs = plan["inputs"]
    library = plan["library"]

    flags: List[dict] = []

    theoretical = library.get("theoretical_size", 0)
    ceiling = inputs.get("yeast_transformation_ceiling", 10 ** 8)
    scheme = inputs["diversification_scheme"]
    positions = inputs["diversification_positions"]
    starting_material = inputs["starting_material"]
    target_kd_nm = inputs["target_kd_nm"]

    # --- Hard errors -------------------------------------------------------

    if theoretical > ceiling * 100:
        orders = math.log10(max(theoretical / ceiling, 1.0))
        flags.append({
            "severity": "error",
            "code": "library_exceeds_transformation_ceiling",
            "message": (
                f"Library of 1e{math.log10(theoretical):.1f} variants "
                f"exceeds yeast transformation ceiling 1e{math.log10(ceiling):.1f} "
                f"by {orders:.1f} orders of magnitude. Reduce diversified "
                f"positions, switch to a hierarchical library strategy, or "
                f"move to a host with higher transformation efficiency."
            ),
        })

    if target_kd_nm < 0.01:
        flags.append({
            "severity": "error",
            "code": "kd_below_yeast_display_floor",
            "message": (
                f"Picomolar KD target ({target_kd_nm} nM) is at the limits "
                f"of yeast display selection. Avidity and wash-off kinetics "
                f"no longer discriminate below ~10 pM. Affinity maturation "
                f"by mammalian display or ribosome display is recommended "
                f"for final polishing."
            ),
        })

    # --- Soft warnings -----------------------------------------------------

    if scheme == "trimer" and positions > 12:
        flags.append({
            "severity": "warning",
            "code": "trimer_cost_at_high_positions",
            "message": (
                f"Trimer synthesis at {positions} positions is typically "
                f"cost prohibitive. Consider NNK for screening and reserve "
                f"trimer for a reduced-diversity affinity maturation step."
            ),
        })

    if (
        scheme == "NNK"
        and positions > 8
        and starting_material == "naive"
        and target_kd_nm < 10.0
    ):
        flags.append({
            "severity": "warning",
            "code": "naive_nnk_affinity_gap",
            "message": (
                f"Naive NNK libraries at {positions} positions typically "
                f"yield 100-500 nM binders on the first pass. Plan on a "
                f"dedicated affinity maturation round (e.g. targeted NNK or "
                f"trimer on the CDR3 region) to reach {target_kd_nm} nM."
            ),
        })

    if scheme == "NNN" and positions >= 4:
        flags.append({
            "severity": "warning",
            "code": "nnn_stop_dilution",
            "message": (
                f"NNN at {positions} positions loses ~{(1 - (61/64)**positions) * 100:.1f}% "
                f"of the library to stop codons. NNK or NNS give equivalent "
                f"amino acid coverage with one stop per position instead of "
                f"three."
            ),
        })

    # Bottleneck warning: functional library larger than ceiling but not
    # catastrophic.
    functional = library.get("functional_size", theoretical)
    if (
        functional > ceiling
        and theoretical <= ceiling * 100
    ):
        flags.append({
            "severity": "warning",
            "code": "functional_library_above_transformation_ceiling",
            "message": (
                f"Functional library ({functional:.2e}) exceeds single-batch "
                f"yeast transformation ceiling ({ceiling:.0e}). Either "
                f"pool multiple transformations or accept that the library "
                f"will sample {(ceiling/functional)*100:.1f}% of theoretical "
                f"functional diversity."
            ),
        })

    return flags
