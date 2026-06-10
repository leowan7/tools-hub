"""ProteinMPNN standalone (D1) — atomic primitive.

Modal app: ``ranomics-mpnn-prod``. GPU: A10G-24GB.
Pattern setter per ``docs/ATOMIC-TOOLS.md`` D1 section.

The user uploads a backbone PDB + picks chain(s) to design, and receives
``num_seq_per_target`` candidate sequences with MPNN scores and per-
sequence recovery. 1-credit loss leader on the pilot tier.

D1 exposes a single ``standalone`` tier: caller-supplied PDB, up to 200
candidate sequences. It runs on the Modal function
(``ranomics-mpnn-prod::run_tool``) and ``run_pipeline.py``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Bounds (also enforced on the pipeline side for direct ``modal run`` use).
# ---------------------------------------------------------------------------

NUM_SEQ_MIN = 1
NUM_SEQ_MAX = 200
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

    The single ``standalone`` tier takes a caller-supplied PDB, the
    user-picked chain(s), ``num_seq_per_target``, and ``sampling_temp``.
    A missing or blank preset is treated as ``standalone`` so the form's
    hidden preset field is robust.

    The shape returned is consumed by ``build_payload`` below and is
    also the ``inputs`` blob persisted on the ``tool_jobs`` row.
    """
    preset = (form.get("preset") or "standalone").strip() or "standalone"
    if preset != "standalone":
        return None, "Pick a preset."

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

    num_seq_per_target = _parse_int(form.get("num_seq_per_target"), 50)
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
    dict.
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
    label="ProteinMPNN",
    blurb=(
        "Sequence design from a backbone. Upload a backbone PDB, get N "
        "candidate sequences with MPNN scores and per-sequence recovery. "
        "~30 s per run."
    ),
    presets=(
        Preset(
            slug="standalone",
            label="Standalone with your backbone",
            description=(
                "Upload a backbone PDB, pick chain(s) to redesign, get "
                "up to 200 candidate sequences. ~30 to 60 s on A10G-24GB."
            ),
            requires_pdb=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=True,
    form_template="tools/mpnn_form.html",
    results_partial="tools/mpnn_results.html",
)

register(adapter)
