"""ProteinMPNN standalone (D1) — atomic primitive.

Modal app: ``ranomics-mpnn-prod``. GPU: A10G-24GB.
Pattern setter per ``docs/ATOMIC-TOOLS.md`` D1 section.

The user uploads a backbone PDB + picks chain(s) to design, and receives
``num_seq_per_target`` candidate sequences with MPNN scores and per-
sequence recovery. 1-credit loss leader on the pilot tier.

Unlike the composite pipelines (BindCraft, RFantibody, BoltzGen,
PXDesign), D1 exposes two tiers:

- ``smoke`` — baked ~130-residue target (1HEW). No PDB upload. 2
  sequences. Demos the output shape before the user spends a real PDB.
- ``standalone`` — caller-supplied PDB. Up to 20 sequences. 1 credit
  (capped at 2 credits above 20 per ATOMIC-TOOLS.md D1 pricing).

Both tiers use the same Modal function (``ranomics-mpnn-prod::run_tool``)
and the same ``run_pipeline.py``; tier selection only changes which PDB
is used and the default sample count.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Bounds (also enforced on the pipeline side for direct ``modal run`` use).
# ---------------------------------------------------------------------------

NUM_SEQ_MIN = 1
NUM_SEQ_MAX = 20
TEMP_MIN = 0.01
TEMP_MAX = 1.0


def _parse_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, falling back to ``default`` on failure."""
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_float(value: Any, default: float) -> float:
    """Coerce ``value`` to float, falling back to ``default`` on failure."""
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the MPNN job_spec shape.

    Branches on preset:
      - ``smoke``: baked ~130 aa target, fixed chain A, 2 sequences.
      - ``standalone``: caller-supplied PDB, user-picked chain(s) +
        ``num_seq_per_target`` + ``sampling_temp``.

    The shape returned is consumed by ``build_payload`` below and is
    also the ``inputs`` blob persisted on the ``tool_jobs`` row.
    """
    preset = (form.get("preset") or "").strip()
    if preset not in {"smoke", "standalone"}:
        return None, "Pick a preset."

    if preset == "smoke":
        return (
            {
                "preset": preset,
                "target_chain": "A",
                "num_seq_per_target": 2,
                "sampling_temp": 0.1,
                # Pass-through metadata for the results page.
                "target": "1HEW (lysozyme, ~130 aa, chain A)",
            },
            None,
        )

    # standalone tier — caller target.
    chains_to_design = (form.get("chains_to_design") or "A").strip()
    if not chains_to_design:
        return None, "chains_to_design is required."
    # Accept "A", "AB", "A B", "A,B" — normalize to space-separated.
    normalized_chains = " ".join(
        tok.strip()
        for tok in chains_to_design.replace(",", " ").split()
        if tok.strip()
    )
    if not normalized_chains:
        return None, "chains_to_design must contain at least one chain ID."
    if len(normalized_chains) > 24:
        return None, "chains_to_design too long (max 24 characters)."
    for chain in normalized_chains.split():
        if len(chain) > 4:
            return None, f"chain ID {chain!r} is too long (max 4 characters)."

    num_seq_per_target = _parse_int(form.get("num_seq_per_target"), 5)
    if num_seq_per_target < NUM_SEQ_MIN or num_seq_per_target > NUM_SEQ_MAX:
        return (
            None,
            f"num_seq_per_target must be between {NUM_SEQ_MIN} and {NUM_SEQ_MAX}.",
        )

    sampling_temp = _parse_float(form.get("sampling_temp"), 0.1)
    if sampling_temp < TEMP_MIN or sampling_temp > TEMP_MAX:
        return (
            None,
            f"sampling_temp must be between {TEMP_MIN} and {TEMP_MAX}.",
        )

    return (
        {
            "preset": preset,
            "target_chain": normalized_chains,
            "num_seq_per_target": num_seq_per_target,
            "sampling_temp": sampling_temp,
            "target": f"Your uploaded PDB (chain(s) {normalized_chains})",
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the MPNN job_spec shape ``run_pipeline.py`` expects.

    The presigned URL is forwarded by the generic submit route via
    ``_input_presigned_url`` — this function does not embed it in the
    dict. Smoke tier ignores the URL entirely (baked target).
    """
    return {
        "target_chain": inputs["target_chain"],
        "parameters": {
            "num_seq_per_target": inputs["num_seq_per_target"],
            "sampling_temp": inputs["sampling_temp"],
        },
    }


adapter = ToolAdapter(
    slug="mpnn",
    label="ProteinMPNN — sequence design from backbone",
    blurb=(
        "Upload a backbone PDB, get N candidate sequences with MPNN "
        "scores and per-sequence recovery. Atomic primitive — A10G-24GB, "
        "~30 s per run."
    ),
    presets=(
        Preset(
            slug="smoke",
            label="Smoke — 2 sequences, 0 credits",
            credits_cost=0,
            description=(
                "Runs against a baked 1HEW lysozyme target (chain A, "
                "~130 aa). Same pipeline, smallest preset — verifies the "
                "tool works before you spend a PDB."
            ),
        ),
        Preset(
            slug="standalone",
            label="Standalone — your backbone, 1 credit",
            credits_cost=1,
            description=(
                "Upload a backbone PDB, pick chain(s) to redesign, get "
                "up to 20 candidate sequences. ~30-60 s on A10G-24GB."
            ),
            requires_pdb=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=False,  # smoke tier does not need a PDB; per-preset flag owns this
    form_template="tools/mpnn_form.html",
    results_partial="tools/mpnn_results.html",
)

register(adapter)
