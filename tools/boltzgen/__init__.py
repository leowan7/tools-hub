"""BoltzGen tool adapter.

Kendrew Modal app: ``kendrew-boltzgen-prod``. GPU: A100-40GB.

BoltzGen uses the Boltz-2 model to generate binder backbones against a
reference target, then scores each candidate for refolding RMSD, ipTM,
and pLDDT. Smoke / mini_pilot tiers use the baked
``/opt/smoke_target.pdb`` (PD-L1 IgV, chain A, residues 18-132) and
ignore caller-supplied PDBs — the form presents this as a
"reference-target demo" and the form has no PDB upload. Real-target
design ships in a later wave via the webhook path once the pilot tier
UI lands.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


def _parse_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, falling back to ``default`` on failure."""
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the Kendrew BoltzGen job_spec shape.

    At smoke / mini_pilot tier the pipeline ignores the caller's
    job_spec and runs against the baked target; we still accept a few
    fields so the form feels real and the same adapter works when the
    pilot tier lands.
    """
    preset = (form.get("preset") or "").strip()
    if preset not in {"smoke", "mini_pilot"}:
        return None, "Pick a preset."

    default_min = 30 if preset == "smoke" else 50
    default_max = 40 if preset == "smoke" else 70
    binder_length_min = _parse_int(form.get("binder_length_min"), default_min)
    binder_length_max = _parse_int(form.get("binder_length_max"), default_max)

    if binder_length_min < 1 or binder_length_max < 1:
        return None, "Binder length must be a positive integer."
    if binder_length_min > binder_length_max:
        return None, "Binder length min must be less than or equal to max."

    protocol = (form.get("protocol") or "protein-anything").strip()
    if protocol not in {"protein-anything"}:
        return None, "Protocol must be protein-anything."

    return (
        {
            "preset": preset,
            "binder_length_min": binder_length_min,
            "binder_length_max": binder_length_max,
            "protocol": protocol,
            # Pass-through metadata the results page can display.
            "target": "PD-L1 IgV (residues 18-132, chain A)",
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Kendrew job_spec BoltzGen's run_pipeline.py expects.

    At smoke / mini_pilot the hard-coded preset inside run_pipeline.py
    overrides these fields, but we send the full shape anyway for
    forwards-compat with the pilot tier.
    """
    is_smoke = inputs["preset"] == "smoke"
    return {
        "target_chain": "A",
        "parameters": {
            "binder_length": {
                "min": inputs["binder_length_min"],
                "max": inputs["binder_length_max"],
            },
            "num_designs": 1 if is_smoke else 2,
            "budget": 1 if is_smoke else 2,
            "protocol": inputs["protocol"],
        },
        # Smoke / mini_pilot ignore hotspots but the pipeline validates shape.
        "hotspot_residues": [],
    }


adapter = ToolAdapter(
    slug="boltzgen",
    label="BoltzGen — structure + affinity design",
    blurb=(
        "Boltz-2 binder design. Generates a binder backbone against a "
        "target, refolds each candidate, and scores affinity via ipTM "
        "and pLDDT. A100-40GB."
    ),
    presets=(
        Preset(
            slug="smoke",
            label="Smoke — 1 design, 3 credits",
            credits_cost=3,
            description=(
                "~5 min, 1 candidate against PD-L1 reference (baked "
                "target), real refolding + ipTM scores."
            ),
        ),
        Preset(
            slug="mini_pilot",
            label="Preview — 2 designs, 10 credits",
            credits_cost=10,
            description=(
                "~10 min, 2 candidates against PD-L1 reference, full "
                "scoring pipeline."
            ),
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=False,
    form_template="tools/boltzgen_form.html",
    results_partial="tools/boltzgen_results.html",
)

register(adapter)
