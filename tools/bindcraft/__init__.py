"""BindCraft tool adapter.

Modal app: ``ranomics-bindcraft-prod``. GPU: A100-80GB.
4-hour max session — pilot tier uses the webhook-only flow so the
caller can close the tab and receive the final results by email.

BindCraft is structure-based de novo binder design built on
JAX + AlphaFold2 multimer + ColabDesign. Every run requires a
caller-supplied target PDB (``requires_pdb=True``). The only preset
shipped is ``pilot`` (caller-controlled ``num_designs`` 1-24,
default 8).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the Kendrew BindCraft job_spec shape."""
    preset = (form.get("preset") or "").strip()
    if preset != "pilot":
        return None, "Pick a preset."

    target_chain = (form.get("target_chain") or "A").strip()
    if not target_chain:
        return None, "Target chain is required."
    if len(target_chain) > 4:
        return None, "Target chain must be at most 4 characters."

    raw_hotspots = (form.get("hotspot_residues") or "").strip()
    if not raw_hotspots:
        return None, "At least one hotspot residue is required."
    try:
        hotspot_residues = [
            int(tok.strip()) for tok in raw_hotspots.split(",") if tok.strip()
        ]
    except ValueError:
        return None, "Hotspot residues must be comma-separated integers (e.g. 54,56,115)."
    if not hotspot_residues:
        return None, "At least one hotspot residue is required."

    try:
        binder_length_min = int(form.get("binder_length_min") or 50)
        binder_length_max = int(form.get("binder_length_max") or 100)
    except ValueError:
        return None, "Binder length must be whole numbers."

    if binder_length_min < 50:
        return None, "binder_length_min must be >= 50."
    if binder_length_max > 150:
        return None, "binder_length_max must be <= 150."
    if binder_length_min > binder_length_max:
        return None, "binder_length_min must be <= binder_length_max."

    raw_num_designs = (form.get("num_designs") or "4").strip()
    try:
        num_designs = int(raw_num_designs)
    except ValueError:
        return None, "num_designs must be a whole number."
    # Tier-collapse PR: raised the per-job cap from 24 to 500. BindCraft
    # trajectories are expensive (~30 min each on A100-80GB), so the
    # wallet $500 hard cap typically constrains real campaigns long
    # before this validator's ceiling. Bigger campaigns top up the
    # wallet.
    if num_designs < 1 or num_designs > 500:
        return None, "num_designs must be between 1 and 500."

    return (
        {
            "preset": preset,
            "target_chain": target_chain,
            "hotspot_residues": hotspot_residues,
            "binder_length_min": binder_length_min,
            "binder_length_max": binder_length_max,
            "num_designs": num_designs,
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Kendrew job_spec BindCraft's run_pipeline.py expects.

    The target PDB presigned URL is forwarded separately by the
    generic submit route, so it is not embedded in this dict.
    """
    return {
        "target_chain": inputs["target_chain"],
        "hotspot_residues": inputs["hotspot_residues"],
        "parameters": {
            "binder_length": {
                "min": inputs["binder_length_min"],
                "max": inputs["binder_length_max"],
            },
            "num_designs": inputs["num_designs"],
        },
    }


adapter = ToolAdapter(
    slug="bindcraft",
    label="BindCraft",
    blurb=(
        "Structure-based de novo binder design on JAX, AlphaFold2 "
        "multimer, and ColabDesign. Four hour max session; results are "
        "emailed on completion."
    ),
    presets=(
        Preset(
            slug="pilot",
            label="Your target, ~30 min start to first results",
            description=(
                "BindCraft against your uploaded PDB on A100-80GB. "
                "Pick 1 to 500 trajectories. Start with a small batch "
                "(4 trajectories, ~30 to 45 min) to confirm your target "
                "and hotspots, then scale to 100+ once the small batch "
                "looks reasonable. Results emailed on completion."
            ),
            requires_pdb=True,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=True,
    form_template="tools/bindcraft_form.html",
    results_partial="tools/bindcraft_results.html",
)

register(adapter)
