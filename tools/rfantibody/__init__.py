"""RFantibody tool adapter.

Kendrew Modal app: ``kendrew-rfantibody-prod``. GPU: A100-40GB.
Validation: 2× smoke + 2× mini_pilot green on commit 64c4ab0 + code-check
PASS on HEAD (VALIDATION-LOG.md).

Smoke / mini_pilot tiers use the baked ``/opt/smoke_target.pdb`` (PD-L1
IgV, chain A, residues 18–132) and ignore caller-supplied PDBs — the
form presents this as a "reference-target demo" and the form has no
PDB upload. Real-target design ships in a later wave via the webhook
path once the pilot tier UI lands.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the Kendrew RFantibody job_spec shape.

    At smoke / mini_pilot tier the pipeline ignores the caller's
    job_spec and runs the baked target; we still accept a few fields so
    the form feels like a real form and so the same adapter works when
    pilot tier lands.
    """
    preset = (form.get("preset") or "").strip()
    if preset not in {"smoke", "mini_pilot"}:
        return None, "Pick a preset."
    framework = (form.get("framework") or "VHH").strip().upper()
    if framework not in {"VHH", "SCFV"}:
        return None, "Framework must be VHH or scFv."
    return (
        {
            "preset": preset,
            "framework": framework,
            # Pass-through metadata the results page can display.
            "target": "PD-L1 IgV (residues 18-132, chain A)",
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Kendrew job_spec RFantibody's run_pipeline.py expects.

    At smoke/mini_pilot the hard-coded preset inside run_pipeline.py
    overrides these fields, but we send the full shape anyway for
    forwards-compat with the pilot tier.
    """
    return {
        "target_chain": "A",
        "parameters": {
            "framework": inputs.get("framework", "VHH"),
            "cdr_lengths": "H1:8,H2:7,H3:10" if inputs["preset"] == "smoke"
                           else "H1:8,H2:7,H3:10-13",
            "num_designs": 1 if inputs["preset"] == "smoke" else 2,
        },
        # Smoke/mini_pilot ignore hotspots but the pipeline validates shape.
        "hotspot_residues": [54, 56, 115],
    }


adapter = ToolAdapter(
    slug="rfantibody",
    label="RFantibody — VHH / scFv design",
    blurb=(
        "Structure-based antibody binder design. Generates nanobody (VHH) "
        "or scFv candidates against a target epitope, then validates the "
        "fold with RoseTTAFold-2. A100-40GB."
    ),
    presets=(
        Preset(
            slug="smoke",
            label="Smoke — 1 design, 2 credits",
            credits_cost=2,
            description=(
                "One candidate against PD-L1 reference target (~3 min). "
                "Same pipeline, smallest preset. Good for verifying "
                "the tool works for your workflow."
            ),
        ),
        Preset(
            slug="mini_pilot",
            label="Preview — 2 designs, 8 credits",
            credits_cost=8,
            description=(
                "Two ranked candidates against PD-L1 reference target "
                "(~7 min). Real diffusion steps + RF2 validation."
            ),
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=False,
    form_template="tools/rfantibody_form.html",
    results_partial="tools/rfantibody_results.html",
)

register(adapter)
