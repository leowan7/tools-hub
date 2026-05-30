"""PXDesign tool adapter.

Modal app: ``ranomics-pxdesign-prod``. GPU: A100-80GB.
PXDesign generates binders and validates them with JAX AF2 Initial
Guess (AF2-IG) — real ipTM / pLDDT / pAE scores from the AF2 monomer
model run in initial-guess mode against the target.

Known-good on commit ``5f22eec`` (the cuDNN 9 upgrade). The pilot tier
accepts a caller-supplied target PDB with hotspot residues and runs
real-target binder design (~30–60 min on A100-80GB).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register
from tools.pxdesign import meta as _meta  # noqa: F401 — re-export for templates

# Re-export so callers can do ``from tools.pxdesign import paper_citation``.
paper_citation = _meta.paper_citation
paper_url = _meta.paper_url
github_url = _meta.github_url
comparison_one_liner = _meta.comparison_one_liner
example_output_id = _meta.example_output_id
preset_runtime_rows = _meta.preset_runtime_rows


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the Kendrew PXDesign job_spec shape.

    The Kendrew PXDesign pipeline rejects ``preset="basic"`` — only
    ``preview`` is a valid ``parameters.preset`` value, so the pilot
    tier translates to ``preview`` on the payload side.
    """
    preset = (form.get("preset") or "pilot").strip() or "pilot"
    if preset != "pilot":
        return None, "Pick a preset."

    # pilot tier — real user target + hotspots.
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

    raw_length = (form.get("binder_length") or "80").strip()
    try:
        binder_length = int(raw_length)
    except (TypeError, ValueError):
        return None, "Binder length must be an integer."
    if binder_length < 40 or binder_length > 150:
        return None, "Binder length must be between 40 and 150 residues."

    raw_num_designs = (form.get("num_designs") or "8").strip()
    try:
        num_designs = int(raw_num_designs)
    except (TypeError, ValueError):
        return None, "Number of designs must be an integer."
    if num_designs < 1 or num_designs > 24:
        return None, "Number of designs must be between 1 and 24."

    return (
        {
            "preset": preset,
            "target_chain": target_chain,
            "hotspot_residues": hotspot_residues,
            "binder_length": binder_length,
            "num_designs": num_designs,
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Kendrew job_spec that PXDesign's run_pipeline.py expects.

    ``parameters.preset`` MUST be ``"preview"`` — the pipeline rejects
    ``"basic"``. The pilot tier runs against a caller-supplied target —
    the presigned URL is forwarded separately by the generic submit
    route.
    """
    # pilot
    return {
        "target_chain": inputs["target_chain"],
        "hotspot_residues": inputs["hotspot_residues"],
        "parameters": {
            "num_designs": inputs["num_designs"],
            "preset": "preview",
            "binder_length": inputs["binder_length"],
            "post_filter": True,
        },
    }


adapter = ToolAdapter(
    slug="pxdesign",
    label="PXDesign — JAX AF2-IG binder design",
    blurb=(
        "Binder design with JAX AF2 Initial Guess validation — real "
        "ipTM / pLDDT / pAE from the AF2 monomer model run in "
        "initial-guess mode against the target. Pilot ~30–60 min."
    ),
    presets=(
        Preset(
            slug="pilot",
            label="Pilot — your target, ~45 min",
            description=(
                "Real PXDesign run against your uploaded target with "
                "AF2-IG validation. Up to 24 candidates with real ipTM/pLDDT/pAE "
                "scores; results emailed when complete (~30-60 min on A100-80GB)."
            ),
            requires_pdb=True,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=True,
    form_template="tools/pxdesign_form.html",
    results_partial="tools/pxdesign_results.html",
)

register(adapter)
