"""RFantibody tool adapter.

Kendrew Modal app: ``kendrew-rfantibody-prod``. GPU: A100-40GB.
Validation: 2x smoke + 2x mini_pilot green on commit 64c4ab0 + code-check
PASS on HEAD (VALIDATION-LOG.md).

Smoke / mini_pilot tiers use the baked ``/opt/smoke_target.pdb`` (PD-L1
IgV, chain A, residues 18-132) and ignore caller-supplied PDBs -- the
form presents this as a "reference-target demo" and the form has no
PDB upload. Pilot tier accepts a caller-uploaded target PDB plus
hotspots and runs on the webhook flow (~15-60 min on A100-40GB).
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
    the form feels like a real form. At pilot tier the caller supplies
    the target PDB, chain, hotspots, framework, and CDR lengths.
    """
    preset = (form.get("preset") or "").strip()
    if preset not in {"smoke", "mini_pilot", "pilot"}:
        return None, "Pick a preset."

    if preset in {"smoke", "mini_pilot"}:
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

    # pilot tier -- real target supplied by caller
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

    framework_raw = (form.get("framework") or "VHH").strip()
    framework_upper = framework_raw.upper()
    if framework_upper not in {"VHH", "SCFV"}:
        return None, "Framework must be VHH or scFv."
    framework = "VHH" if framework_upper == "VHH" else "scFv"

    cdr_lengths = (form.get("cdr_lengths") or "H1:8,H2:7,H3:10-16").strip()

    return (
        {
            "preset": preset,
            "target_chain": target_chain,
            "hotspot_residues": hotspot_residues,
            "framework": framework,
            "cdr_lengths": cdr_lengths,
            "target": f"Your uploaded PDB (chain {target_chain})",
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Kendrew job_spec RFantibody's run_pipeline.py expects.

    At smoke/mini_pilot the hard-coded preset inside run_pipeline.py
    overrides these fields, but we send the full shape anyway for
    forwards-compat. At pilot tier the presigned_url is forwarded by
    the generic submit route via ``_input_pdb_url`` -- this function
    does not embed it in the returned dict.
    """
    preset = inputs.get("preset", "")
    if preset in {"smoke", "mini_pilot"}:
        return {
            "target_chain": "A",
            "parameters": {
                "framework": inputs.get("framework", "VHH"),
                "cdr_lengths": "H1:8,H2:7,H3:10" if preset == "smoke"
                               else "H1:8,H2:7,H3:10-13",
                "num_designs": 1 if preset == "smoke" else 2,
            },
            # Smoke/mini_pilot ignore hotspots but the pipeline validates shape.
            "hotspot_residues": [54, 56, 115],
        }

    # pilot tier
    return {
        "target_chain": inputs["target_chain"],
        "hotspot_residues": inputs["hotspot_residues"],
        "parameters": {
            "framework": inputs["framework"],
            "cdr_lengths": inputs["cdr_lengths"],
            "num_designs": 2,
        },
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
        Preset(
            slug="pilot",
            label="Pilot — your target, ~30 min",
            credits_cost=15,
            description=(
                "Real RFantibody design against your uploaded target PDB. "
                "1-2 final candidates; results emailed when run completes "
                "(~15-60 min on A100-40GB)."
            ),
            requires_pdb=True,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=False,
    form_template="tools/rfantibody_form.html",
    results_partial="tools/rfantibody_results.html",
)

register(adapter)
