"""BoltzGen tool adapter.

Kendrew Modal app: ``kendrew-boltzgen-prod``. GPU: A100-40GB.

BoltzGen uses the Boltz-2 model to generate binder backbones against a
reference target, then scores each candidate for refolding RMSD, ipTM,
and pLDDT. Smoke / mini_pilot tiers use the baked
``/opt/smoke_target.pdb`` (PD-L1 IgV, chain A, residues 18-132) and
ignore caller-supplied PDBs. The pilot tier accepts a caller-supplied
target PDB, optional hotspot residues, and configurable binder-length
window; it runs ~15-60 min on A100-40GB and emails results on
completion.
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

    Branches on preset:
      - ``smoke`` / ``mini_pilot``: baked PD-L1 target, default binder length.
      - ``pilot``: caller-supplied target PDB + hotspots + binder length.
    """
    preset = (form.get("preset") or "").strip()
    if preset not in {"smoke", "mini_pilot", "pilot"}:
        return None, "Pick a preset."

    if preset in {"smoke", "mini_pilot"}:
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

    # Pilot tier — real target.
    target_chain = (form.get("target_chain") or "A").strip()
    if not target_chain:
        return None, "Target chain is required."
    if len(target_chain) > 4:
        return None, "Target chain must be at most 4 characters."

    raw_hotspots = (form.get("hotspot_residues") or "").strip()
    if raw_hotspots:
        try:
            hotspot_residues = [
                int(tok.strip()) for tok in raw_hotspots.split(",") if tok.strip()
            ]
        except ValueError:
            return None, "Hotspot residues must be comma-separated integers (e.g. 54,56,115)."
    else:
        # BoltzGen accepts an empty hotspot list as "no hotspot constraint".
        hotspot_residues = []

    binder_length_min = _parse_int(form.get("binder_length_min"), 50)
    binder_length_max = _parse_int(form.get("binder_length_max"), 100)

    if binder_length_min < 20 or binder_length_min > 200:
        return None, "binder_length_min must be between 20 and 200."
    if binder_length_max < 20 or binder_length_max > 200:
        return None, "binder_length_max must be between 20 and 200."
    if binder_length_min > binder_length_max:
        return None, "binder_length_min must be <= binder_length_max."

    budget = _parse_int(form.get("budget"), 5)
    if budget < 1 or budget > 20:
        return None, "budget must be between 1 and 20."

    return (
        {
            "preset": preset,
            "target_chain": target_chain,
            "hotspot_residues": hotspot_residues,
            "binder_length_min": binder_length_min,
            "binder_length_max": binder_length_max,
            "budget": budget,
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Kendrew job_spec BoltzGen's run_pipeline.py expects.

    Branches on preset:
      - ``smoke`` / ``mini_pilot``: baked target shape (hard-coded preset
        inside run_pipeline.py overrides most fields, but we send the
        full shape anyway for forwards-compat).
      - ``pilot``: caller target; presigned URL is forwarded by the
        generic submit route, not embedded here.
    """
    preset = inputs["preset"]

    if preset in {"smoke", "mini_pilot"}:
        is_smoke = preset == "smoke"
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

    # Pilot tier.
    return {
        "target_chain": inputs["target_chain"],
        "hotspot_residues": inputs["hotspot_residues"],
        "parameters": {
            "binder_length": {
                "min": inputs["binder_length_min"],
                "max": inputs["binder_length_max"],
            },
            "num_designs": 1000,
            "budget": inputs["budget"],
            "protocol": "protein-anything",
        },
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
        Preset(
            slug="pilot",
            label="Pilot — your target, ~30 min",
            credits_cost=10,
            description=(
                "Real BoltzGen run against your uploaded target. 5 "
                "final candidates with refolding RMSD + ipTM scores; "
                "results emailed when complete (~15-60 min on A100-40GB)."
            ),
            requires_pdb=True,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=False,
    form_template="tools/boltzgen_form.html",
    results_partial="tools/boltzgen_results.html",
)

register(adapter)
