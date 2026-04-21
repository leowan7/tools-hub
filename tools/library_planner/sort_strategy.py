"""MACS -> FACS sort round planner for yeast display campaigns.

Round design principles encoded here (drawn from the standard Chao et al.
2006 Nat Protoc yeast display protocol and its derivatives):

1. Start the titration loose: first round labelling concentration is at or
   above 10x the target KD to ensure binders are captured even with weak
   display signal.
2. End tight: the final round is at or below 0.1x the target KD to
   differentiate top from middle of the affinity distribution.
3. MACS first when library size > 1e8 so that the full diversity passes
   through a high-throughput enrichment before the FACS bottleneck.
4. Expect ~100-fold enrichment per FACS round in a well-behaved campaign
   (published ranges span ~50x to ~500x depending on labelling).
5. Gate stringency tightens across rounds: 1-2% in the first FACS round,
   narrowing toward 0.1% in the final round.

Starting material adjustments:
- Naive libraries need a wide MACS pre-enrichment.
- Immunized libraries can skip MACS and start on FACS at moderate
  stringency because binder frequency is already enriched.
- Computationally designed pools typically arrive with enriched hit rates
  and do not need MACS.
"""

from __future__ import annotations

from typing import List

VALID_STARTING = ("naive", "immunized", "computational_pool")


def _validate(
    target_kd_nm: float, starting_material: str, library_size: int
) -> None:
    """Validate sort planner inputs."""
    if not isinstance(target_kd_nm, (int, float)):
        raise ValueError("target_kd_nm must be numeric")
    if target_kd_nm <= 0:
        raise ValueError("target_kd_nm must be > 0")
    if starting_material not in VALID_STARTING:
        raise ValueError(
            f"unknown starting_material {starting_material!r}; valid: "
            f"{list(VALID_STARTING)}"
        )
    if not isinstance(library_size, (int, float)):
        raise ValueError("library_size must be numeric")
    if library_size < 1:
        raise ValueError("library_size must be >= 1")


def recommend_sort_rounds(
    target_kd_nm: float,
    starting_material: str,
    library_size: int,
) -> List[dict]:
    """Return the per-round sort plan for a campaign.

    Args:
        target_kd_nm: Final desired KD of the hit, in nanomolar.
        starting_material: One of naive, immunized, computational_pool.
        library_size: Total library size (unique variants). Used to decide
            whether a MACS round is needed before FACS.

    Returns:
        List of dicts, one per round, with ``round``, ``method``,
        ``label_concentration_nm``, ``gate_percent``, ``expected_enrichment``,
        and ``notes``.
    """
    _validate(target_kd_nm, starting_material, library_size)

    rounds: List[dict] = []

    needs_macs = (starting_material == "naive" and library_size > 1e8) or (
        starting_material == "immunized" and library_size > 5e9
    )

    if needs_macs:
        rounds.append({
            "round": 1,
            "method": "MACS",
            "label_concentration_nm": round(max(target_kd_nm * 10.0, 100.0), 2),
            "gate_percent": None,
            "expected_enrichment": "10-100x",
            "notes": (
                "Magnetic pre-enrichment to compress the library into the "
                "FACS-tractable range. Recover ~1e8 cells for round 2."
            ),
        })

    # Number of FACS rounds after MACS (if any).
    if starting_material == "naive":
        n_facs = 3
        start_conc_multiplier = 10.0
    elif starting_material == "immunized":
        n_facs = 3
        start_conc_multiplier = 5.0
    else:
        # Computational pool typically arrives pre-enriched.
        n_facs = 3
        start_conc_multiplier = 3.0

    # Log-linear titration from start_conc_multiplier x KD down to 0.1 x KD
    # across the FACS rounds.
    end_multiplier = 0.1
    if n_facs > 1:
        ratio = (end_multiplier / start_conc_multiplier) ** (1.0 / (n_facs - 1))
    else:
        ratio = 1.0

    # Gate stringency by round.
    gate_schedule = [2.0, 0.5, 0.1] if n_facs == 3 else [1.0] * n_facs

    current_mult = start_conc_multiplier
    starting_round_index = len(rounds) + 1
    for facs_idx in range(n_facs):
        round_number = starting_round_index + facs_idx
        label_nm = round(current_mult * target_kd_nm, 3)
        gate = gate_schedule[facs_idx] if facs_idx < len(gate_schedule) else 0.1
        rounds.append({
            "round": round_number,
            "method": "FACS",
            "label_concentration_nm": label_nm,
            "gate_percent": gate,
            "expected_enrichment": "~100x",
            "notes": _facs_round_note(
                facs_idx, n_facs, starting_material, target_kd_nm
            ),
        })
        current_mult *= ratio

    return rounds


def _facs_round_note(
    idx: int, n_rounds: int, starting_material: str, target_kd_nm: float
) -> str:
    """Compose a per-round guidance note."""
    if idx == 0:
        if starting_material == "naive":
            return (
                "Wide first FACS gate. Look for a clear shoulder above "
                "background; enrichment may still be modest."
            )
        if starting_material == "immunized":
            return (
                "First FACS on immunized output. Moderate gate is often "
                "sufficient to pick up the dominant binder families."
            )
        return (
            "First FACS on a pre-enriched computational pool. Most sequences "
            "should display; gate to the top binders."
        )
    if idx == n_rounds - 1:
        return (
            f"Final stringent round. Label concentration is at or below "
            f"target KD ({target_kd_nm} nM) so only high-affinity variants "
            f"survive. Pick singles after this round for Sanger / NGS."
        )
    return (
        "Middle round titrating concentration down and gate stringency up. "
        "Verify enrichment by running output library mini-titration flow."
    )
