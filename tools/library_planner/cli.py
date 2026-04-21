"""Command-line interface for the yeast display library planner.

Usage::

    python -m library_planner.cli --scaffold VHH --positions 6 --scheme NNK \
        --kd 10 --starting-material naive
    python -m library_planner.cli --scaffold scFv --positions 20 --scheme NNK \
        --kd 1 --starting-material naive --summary

Outputs the full plan as indented JSON by default. ``--summary`` prints only
the plain-language summary paragraph.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Support running as `python cli.py` from inside the library_planner directory
# as well as `python -m library_planner.cli` from the parent directory.
if __package__ in (None, ""):
    _here = Path(__file__).resolve().parent
    _parent = _here.parent
    if str(_parent) not in sys.path:
        sys.path.insert(0, str(_parent))

from tools.library_planner.planner import plan_library


def main(argv: list = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector (used for testing).

    Returns:
        Exit status code.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Yeast display library planner. Returns library size, codon "
            "scheme analysis, NGS depth, sort strategy, and feasibility."
        )
    )
    parser.add_argument(
        "--scaffold",
        required=True,
        help="Scaffold name: scFv, VHH, Fab, DARPin, or custom.",
    )
    parser.add_argument(
        "--positions",
        type=int,
        required=True,
        help="Number of diversified positions.",
    )
    parser.add_argument(
        "--scheme",
        required=True,
        help="Codon scheme: NNK, NNS, NNN, or trimer.",
    )
    parser.add_argument(
        "--kd",
        type=float,
        required=True,
        help="Target KD in nanomolar.",
    )
    parser.add_argument(
        "--starting-material",
        required=True,
        help="Starting material: naive, immunized, or computational_pool.",
    )
    parser.add_argument(
        "--coverage",
        type=float,
        default=0.90,
        help="Target coverage fraction for NGS (default 0.90).",
    )
    parser.add_argument(
        "--yeast-ceiling",
        type=int,
        default=10 ** 8,
        help="Yeast transformation ceiling (default 1e8).",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print only the plain-language summary paragraph.",
    )

    args = parser.parse_args(argv)

    try:
        plan = plan_library(
            scaffold=args.scaffold,
            diversification_positions=args.positions,
            diversification_scheme=args.scheme,
            target_kd_nm=args.kd,
            starting_material=args.starting_material,
            target_coverage=args.coverage,
            yeast_transformation_ceiling=args.yeast_ceiling,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.summary:
        print(plan["summary"])
    else:
        print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
