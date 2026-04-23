"""PXDesign tool adapter.

Kendrew Modal app: ``kendrew-pxdesign-prod``. GPU: A100-80GB.
PXDesign generates binders and validates them with JAX AF2 Initial
Guess (AF2-IG) — real ipTM / pLDDT / pAE scores from the AF2 monomer
model run in initial-guess mode against the baked target.

Known-good on commit ``5f22eec`` (the cuDNN 9 upgrade). Historical
smoke runs land around 17 min; mini_pilot (preview tier, post-filter
enabled) lands around 30–40 min.

Smoke / mini_pilot tiers use the baked ``/opt/smoke_target.pdb``
(PD-L1 IgV) and ignore caller-supplied PDBs — the form presents this
as a "reference-target demo" and has no PDB upload. Real-target design
ships in a later wave once the pilot tier UI lands.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the Kendrew PXDesign job_spec shape.

    The Kendrew PXDesign pipeline rejects ``preset="basic"`` — only
    ``preview`` is a valid ``parameters.preset`` value. We translate
    our UI tiers (smoke / mini_pilot) into ``preview`` on the payload
    side and use ``post_filter`` as the real smoke-vs-mini-pilot knob.
    """
    preset = (form.get("preset") or "").strip()
    if preset not in {"smoke", "mini_pilot"}:
        return None, "Pick a preset."

    raw_length = (form.get("binder_length") or "80").strip()
    try:
        binder_length = int(raw_length)
    except (TypeError, ValueError):
        return None, "Binder length must be an integer."
    if binder_length < 40 or binder_length > 150:
        return None, "Binder length must be between 40 and 150 residues."

    return (
        {
            "preset": preset,
            "binder_length": binder_length,
            # Pass-through metadata for the results page.
            "target": "PD-L1 IgV (residues 18-132, chain A)",
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Kendrew job_spec that PXDesign's run_pipeline.py expects.

    ``parameters.preset`` MUST be ``"preview"`` — the pipeline rejects
    ``"basic"``. ``post_filter`` is what actually differentiates smoke
    from mini_pilot at the pipeline level (smoke skips, mini_pilot
    adds the post-design filtering pass).
    """
    return {
        "target_chain": "A",
        "hotspot_residues": [],
        "parameters": {
            "num_designs": 1,
            "preset": "preview",
            "binder_length": inputs["binder_length"],
            "post_filter": inputs["preset"] == "mini_pilot",
        },
    }


adapter = ToolAdapter(
    slug="pxdesign",
    label="PXDesign — JAX AF2-IG binder design",
    blurb=(
        "Binder design with JAX AF2 Initial Guess validation — real "
        "ipTM / pLDDT / pAE from the AF2 monomer model run in "
        "initial-guess mode against the target. GPU: A100-80GB. "
        "Known-good on commit 5f22eec (cuDNN 9 upgrade). Historical "
        "smoke runs ~17 min; mini_pilot ~30–40 min."
    ),
    presets=(
        Preset(
            slug="smoke",
            label="Smoke — 1 design, 8 credits",
            credits_cost=8,
            description=(
                "~17 min, 1 candidate against PD-L1 reference "
                "(baked target), real AF2-IG scores."
            ),
        ),
        Preset(
            slug="mini_pilot",
            label="Preview — 1 design + post-filter, 16 credits",
            credits_cost=16,
            description=(
                "~35 min, 1 candidate with post-filter against PD-L1 "
                "reference, pilot-quality scoring."
            ),
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=False,
    form_template="tools/pxdesign_form.html",
    results_partial="tools/pxdesign_results.html",
)

register(adapter)
