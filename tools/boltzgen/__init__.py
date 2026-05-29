"""BoltzGen tool adapter.

Modal app: ``ranomics-boltzgen-prod``. GPU: A100-40GB.

BoltzGen uses the Boltz-2 model to generate binder backbones against a
reference target, then scores each candidate for refolding RMSD, ipTM,
and pLDDT. The mini_pilot tier uses the baked
``/opt/smoke_target.pdb`` (PD-L1 IgV, chain A, residues 18-132) and
ignores caller-supplied PDBs. The pilot tier accepts a caller-supplied
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
      - ``mini_pilot``: baked PD-L1 target, default binder length.
      - ``pilot``: caller-supplied target PDB + hotspots + binder length.
    """
    preset = (form.get("preset") or "").strip()
    if preset not in {"mini_pilot", "pilot"}:
        return None, "Pick a preset."

    if preset == "mini_pilot":
        binder_length_min = _parse_int(form.get("binder_length_min"), 50)
        binder_length_max = _parse_int(form.get("binder_length_max"), 70)

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

    budget = _parse_int(form.get("budget"), 8)
    if budget < 1 or budget > 24:
        return None, "budget must be between 1 and 24."

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
      - ``mini_pilot``: baked target shape (hard-coded preset inside
        run_pipeline.py overrides most fields, but we send the full
        shape anyway for forwards-compat).
      - ``pilot``: caller target; presigned URL is forwarded by the
        generic submit route, not embedded here.
    """
    preset = inputs["preset"]

    # job_tier is also set at the wrapper level by gpu/modal_client.py, but we
    # echo it inside job_spec so older run_pipeline.py builds (which read
    # job_spec.get("job_tier")) still resolve the tier correctly. This is what
    # gates the pilot fallback that emits top-N designs when none pass the
    # strict ipTM/pLDDT/RMSD thresholds.
    if preset == "mini_pilot":
        return {
            "job_tier": preset,
            "target_chain": "A",
            "parameters": {
                "binder_length": {
                    "min": inputs["binder_length_min"],
                    "max": inputs["binder_length_max"],
                },
                "num_designs": 2,
                "budget": 2,
                "protocol": inputs["protocol"],
            },
            # Mini_pilot ignores hotspots but the pipeline validates shape.
            "hotspot_residues": [],
        }

    # Pilot tier. num_designs is the candidate population BoltzGen generates
    # and refolds (budget then selects the top-N to return). 1000 was the
    # original wave-2 default but ran past the 6600s subprocess timeout in
    # docker/boltzgen/run_pipeline.py:1407 on A100-40GB. 200 fits comfortably
    # within the "~15-60 min" pilot description and still gives the filter
    # enough population to find passing designs.
    return {
        "job_tier": "pilot",
        "target_chain": inputs["target_chain"],
        "hotspot_residues": inputs["hotspot_residues"],
        "parameters": {
            "binder_length": {
                "min": inputs["binder_length_min"],
                "max": inputs["binder_length_max"],
            },
            "num_designs": 200,
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
        "and pLDDT."
    ),
    presets=(
        Preset(
            slug="mini_pilot",
            label="Preview — 2 designs",
            description=(
                "~10 min, 2 candidates against PD-L1 reference, full "
                "scoring pipeline."
            ),
        ),
        Preset(
            slug="pilot",
            label="Pilot — your target, ~30 min",
            description=(
                "Real BoltzGen run against your uploaded target. Up to 24 "
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
