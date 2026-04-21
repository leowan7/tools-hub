"""Top-level yeast display library planner.

This is the public entry point. Given a design intent (scaffold, diversified
positions, codon scheme, target KD, starting material), returns a complete
experimental plan:

    from library_planner.planner import plan_library

    plan = plan_library(
        scaffold="VHH",
        diversification_positions=6,
        diversification_scheme="NNK",
        target_kd_nm=10.0,
        starting_material="naive",
    )

The returned dict is pure data (nested lists, dicts, primitives) and is
safe to JSON-serialize for downstream Flask rendering.
"""

from __future__ import annotations

import math
from typing import Any, Dict

from tools.library_planner import codon_bias
from tools.library_planner import combinatorics
from tools.library_planner import failure_modes
from tools.library_planner import ngs_depth
from tools.library_planner import sort_strategy

VALID_SCAFFOLDS = ("scFv", "VHH", "Fab", "DARPin", "custom")
VALID_SCHEMES = ("NNK", "NNS", "NNN", "trimer")
VALID_STARTING = ("naive", "immunized", "computational_pool")

DEFAULT_YEAST_CEILING = 10 ** 8


def _validate_inputs(
    scaffold: str,
    diversification_positions: int,
    diversification_scheme: str,
    target_coverage: float,
    target_kd_nm: float,
    starting_material: str,
    yeast_transformation_ceiling: int,
) -> None:
    """Validate all inputs at function top. Fail fast with informative errors.

    Raises:
        ValueError: If any input is malformed.
    """
    if scaffold not in VALID_SCAFFOLDS:
        raise ValueError(
            f"unknown scaffold {scaffold!r}; valid: {list(VALID_SCAFFOLDS)}"
        )
    if (
        not isinstance(diversification_positions, int)
        or isinstance(diversification_positions, bool)
    ):
        raise ValueError("diversification_positions must be an int")
    if diversification_positions < 1:
        raise ValueError("diversification_positions must be >= 1")
    if diversification_scheme not in VALID_SCHEMES:
        raise ValueError(
            f"unknown diversification_scheme {diversification_scheme!r}; "
            f"valid: {list(VALID_SCHEMES)}"
        )
    if not isinstance(target_coverage, (int, float)):
        raise ValueError("target_coverage must be numeric")
    if target_coverage <= 0 or target_coverage >= 1:
        raise ValueError("target_coverage must be in (0, 1)")
    if not isinstance(target_kd_nm, (int, float)):
        raise ValueError("target_kd_nm must be numeric")
    if target_kd_nm <= 0:
        raise ValueError("target_kd_nm must be > 0")
    if starting_material not in VALID_STARTING:
        raise ValueError(
            f"unknown starting_material {starting_material!r}; valid: "
            f"{list(VALID_STARTING)}"
        )
    if (
        not isinstance(yeast_transformation_ceiling, int)
        or isinstance(yeast_transformation_ceiling, bool)
    ):
        raise ValueError("yeast_transformation_ceiling must be an int")
    if yeast_transformation_ceiling < 1:
        raise ValueError("yeast_transformation_ceiling must be >= 1")


def _format_scientific(value: float) -> str:
    """Format a large number in scientific notation for readable prose."""
    if value <= 0:
        return "0"
    return f"{value:.2e}"


def _build_summary(plan: dict) -> str:
    """Build a 1-paragraph plain-language summary from the plan dict.

    Args:
        plan: Complete plan dict.

    Returns:
        Human-readable paragraph summarizing the plan.
    """
    inputs = plan["inputs"]
    library = plan["library"]
    ngs = plan["ngs_depth"]
    sort_rounds = plan["sort_strategy"]
    feasibility = plan["feasibility"]

    theoretical = library["theoretical_size"]
    functional = library["functional_size"]
    feasible = library["feasible_on_yeast"]

    has_errors = any(f["severity"] == "error" for f in feasibility)
    n_rounds = len(sort_rounds)
    methods = ", ".join(r["method"] for r in sort_rounds)

    feasibility_phrase = (
        "Plan is feasible on yeast display." if feasible and not has_errors
        else "Plan has one or more hard feasibility failures; see the "
        "feasibility section."
    )

    return (
        f"Library plan for {inputs['scaffold']} with "
        f"{inputs['diversification_positions']} {inputs['diversification_scheme']} "
        f"positions on {inputs['starting_material']} starting material targeting "
        f"{inputs['target_kd_nm']} nM KD. Theoretical diversity is "
        f"{_format_scientific(theoretical)} DNA variants "
        f"({_format_scientific(functional)} stop-free). "
        f"NGS budget for {int(inputs['target_coverage'] * 100)} percent library "
        f"coverage is {_format_scientific(ngs['recommended'])} reads. "
        f"Proposed sort strategy runs {n_rounds} rounds ({methods}) with "
        f"log-linear KD titration toward the {inputs['target_kd_nm']} nM goal. "
        f"{feasibility_phrase}"
    )


def plan_library(
    scaffold: str,
    diversification_positions: int,
    diversification_scheme: str,
    target_kd_nm: float,
    starting_material: str,
    target_coverage: float = 0.90,
    yeast_transformation_ceiling: int = DEFAULT_YEAST_CEILING,
) -> Dict[str, Any]:
    """Plan a yeast display library from a high-level design intent.

    Args:
        scaffold: Scaffold name. One of scFv, VHH, Fab, DARPin, custom.
        diversification_positions: Number of diversified residues.
        diversification_scheme: Codon scheme. One of NNK, NNS, NNN, trimer.
        target_kd_nm: Desired final KD in nanomolar.
        starting_material: One of naive, immunized, computational_pool.
        target_coverage: Fraction of library the user wants to sample
            (default 0.90). Used for NGS read-depth calculations.
        yeast_transformation_ceiling: Practical transformation ceiling for
            the host (default 1e8).

    Returns:
        Dict with nested ``inputs``, ``library``, ``codon_analysis``,
        ``ngs_depth``, ``sort_strategy``, ``feasibility``, and ``summary``
        sections. Pure data; JSON-serializable.

    Raises:
        ValueError: If any input is invalid.
    """
    _validate_inputs(
        scaffold=scaffold,
        diversification_positions=diversification_positions,
        diversification_scheme=diversification_scheme,
        target_coverage=target_coverage,
        target_kd_nm=target_kd_nm,
        starting_material=starting_material,
        yeast_transformation_ceiling=yeast_transformation_ceiling,
    )

    inputs = {
        "scaffold": scaffold,
        "diversification_positions": diversification_positions,
        "diversification_scheme": diversification_scheme,
        "target_coverage": target_coverage,
        "target_kd_nm": target_kd_nm,
        "starting_material": starting_material,
        "yeast_transformation_ceiling": yeast_transformation_ceiling,
    }

    # --- Combinatorics ----------------------------------------------------
    theoretical = combinatorics.theoretical_size(
        diversification_scheme, diversification_positions
    )
    functional = combinatorics.functional_size(
        diversification_scheme, diversification_positions
    )
    aa_space = combinatorics.functional_amino_acid_space(
        diversification_positions
    )
    feasible_on_yeast = theoretical <= yeast_transformation_ceiling * 100

    recommended_scheme = codon_bias.recommend_scheme(
        diversification_positions, scaffold
    )

    library_section = {
        "theoretical_size": theoretical,
        "functional_size": functional,
        "amino_acid_space": aa_space,
        "feasible_on_yeast": feasible_on_yeast,
        "yeast_transformation_ceiling": yeast_transformation_ceiling,
        "recommended_scheme": recommended_scheme,
    }

    # --- Codon analysis ---------------------------------------------------
    codon_warnings = codon_bias.bias_warnings(
        diversification_scheme, scaffold
    )
    codon_section = {
        "warnings": codon_warnings,
        "scheme_recommendation": recommended_scheme,
    }

    # --- Sort strategy ----------------------------------------------------
    # Size the sort by the functional library (stop-codon free). Cap at the
    # yeast transformation ceiling so downstream NGS and MACS triggers
    # reflect what is physically in the flask.
    sortable_library = min(functional, yeast_transformation_ceiling)
    sort_rounds = sort_strategy.recommend_sort_rounds(
        target_kd_nm=target_kd_nm,
        starting_material=starting_material,
        library_size=sortable_library,
    )

    # --- NGS depth --------------------------------------------------------
    ngs_section = ngs_depth.coverage_profile(sortable_library)
    # Map sort rounds to gate fractions for per-round coverage planning.
    gate_fractions = [
        (r["gate_percent"] / 100.0) if r.get("gate_percent") else 0.01
        for r in sort_rounds
    ]
    ngs_section["per_round"] = ngs_depth.per_round_coverage(
        initial_library_size=sortable_library,
        round_gate_fractions=gate_fractions,
        target_coverage=max(target_coverage, 0.90),
    )

    # --- Feasibility ------------------------------------------------------
    partial_plan = {"inputs": inputs, "library": library_section}
    feasibility_flags = failure_modes.check_feasibility(partial_plan)

    # Re-evaluate feasible_on_yeast if any error was raised.
    if any(f["severity"] == "error" for f in feasibility_flags):
        library_section["feasible_on_yeast"] = False

    plan = {
        "inputs": inputs,
        "library": library_section,
        "codon_analysis": codon_section,
        "ngs_depth": ngs_section,
        "sort_strategy": sort_rounds,
        "feasibility": feasibility_flags,
    }
    plan["summary"] = _build_summary(plan)
    return plan
