"""ESMFold2 design — scFv CDR design + de novo minibinders via inversion.

Modal app: ``ranomics-esmfold2-design-prod``. GPU: H100. Atomic primitive.

The user picks a target (one of five paper-validated presets, or a pasted
sequence) and a binder mode:

- ``minibinder`` — free 60-200 aa scaffold, sequence-only output, no
  framework. Mirrors RFdiffusion + ProteinMPNN end-state but in one
  gradient pass.
- ``scfv`` — all six CDRs designed jointly on a locked humanized
  framework (trastuzumab / atezolizumab / ocankitug). Heavy + light
  variable domains on a GS-linker. The only catalog tool that designs
  paired heavy + light scFv CDRs end-to-end.

Output is a per-design table with the designed sequence, iPTM, distogram
iPTM proxy (or CDR distogram iPTM proxy for scFvs), final loss,
isoelectric point, and the predicted complex PDB.

The Modal pipeline lives in ``tools/esmfold2_design/modal_app.py`` and
the gradient-descent loop in upstream ``binder_design.py`` (vendored at
``tools/esmfold2_design/run_pipeline.py`` — see the llm-proteinDesigner
side of the repo separation for the deploy).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Bounds. Mirror the upstream binder_design.py constraints.
# ---------------------------------------------------------------------------

TARGET_PRESET_NAMES = ("cd45", "ctla4", "egfr", "pd-l1", "pdgfr")
FRAMEWORK_NAMES = (
    "trastuzumab_framework_vhvl",
    "atezolizumab_framework_vhvl",
    "ocankitug_framework_vhvl",
)
TARGET_SEQ_MIN = 30
TARGET_SEQ_MAX = 800
BATCH_SIZE_MIN = 1
BATCH_SIZE_MAX = 6
# Multi-seed fan-out range. The Modal orchestrator (modal_app.run_tool)
# spawns one H100 child per seed in parallel, so wall-clock stays equal
# to the slowest child regardless of n_seeds. Capped at 64 to stay well
# under Modal's default per-app concurrency ceiling (~100) and to bound
# wallet exposure on a single submission.
N_SEEDS_MIN = 1
N_SEEDS_MAX = 64
SEED_MAX = 2**31 - 1
CANONICAL_AA = set("ACDEFGHIKLMNPQRSTVWYX")


def _parse_seed(raw: str) -> tuple[Optional[int], Optional[str]]:
    raw = (raw or "").strip()
    if not raw:
        return 0, None
    try:
        seed = int(raw)
    except ValueError:
        return None, f"Seed must be an integer; got {raw!r}."
    if seed < 0 or seed > SEED_MAX:
        return None, f"Seed must be between 0 and {SEED_MAX}."
    return seed, None


def _parse_batch_size(raw: str) -> tuple[Optional[int], Optional[str]]:
    raw = (raw or "").strip()
    if not raw:
        # Default 3, not 1: a single-design run usually returns drop
        # because the pI / iPTM gates kill the only candidate. Three
        # designs share the same gradient pass (same wall-clock) and
        # multiply the odds of at least one strict_pass roughly 3x.
        return 3, None
    try:
        bs = int(raw)
    except ValueError:
        return None, f"Batch size must be an integer; got {raw!r}."
    if bs < BATCH_SIZE_MIN or bs > BATCH_SIZE_MAX:
        return None, (
            f"Batch size must be between {BATCH_SIZE_MIN} and "
            f"{BATCH_SIZE_MAX}."
        )
    return bs, None


def _parse_n_seeds(raw: str) -> tuple[Optional[int], Optional[str]]:
    raw = (raw or "").strip()
    if not raw:
        return 1, None
    try:
        n = int(raw)
    except ValueError:
        return None, f"Number of seeds must be an integer; got {raw!r}."
    if n < N_SEEDS_MIN or n > N_SEEDS_MAX:
        return None, (
            f"Number of seeds must be between {N_SEEDS_MIN} and "
            f"{N_SEEDS_MAX}."
        )
    return n, None


def _check_protein_sequence(seq: str, label: str) -> Optional[str]:
    seq = seq.upper()
    if len(seq) < TARGET_SEQ_MIN:
        return f"{label} sequence too short ({len(seq)} aa, min {TARGET_SEQ_MIN})."
    if len(seq) > TARGET_SEQ_MAX:
        return f"{label} sequence too long ({len(seq)} aa, max {TARGET_SEQ_MAX})."
    non_canonical = set(seq) - CANONICAL_AA
    if non_canonical:
        return (
            f"{label} sequence contains non-canonical residues: "
            f"{sorted(non_canonical)}"
        )
    return None


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the ESMFold2-design job_spec shape.

    No PDB upload required — the cookbook's design loop is sequence-only.
    """
    preset = (form.get("preset") or "minibinder").strip()
    if preset not in {"minibinder", "scfv"}:
        return None, "Pick a preset (minibinder or scfv)."

    target_mode = (form.get("target_mode") or "preset").strip()
    if target_mode not in {"preset", "paste"}:
        return None, "Pick a target mode (preset or paste)."

    target_name: Optional[str] = None
    target_sequence: Optional[str] = None
    if target_mode == "preset":
        target_name = (form.get("target_name") or "").strip().lower()
        if target_name not in TARGET_PRESET_NAMES:
            return None, (
                "Pick a target preset: "
                + ", ".join(TARGET_PRESET_NAMES)
                + "."
            )
    else:
        raw_seq = (form.get("target_sequence") or "").strip().replace(" ", "").replace("\n", "")
        if not raw_seq:
            return None, "Paste a target protein sequence."
        seq_err = _check_protein_sequence(raw_seq, "Target")
        if seq_err:
            return None, seq_err
        target_sequence = raw_seq.upper()

    binder_name: Optional[str] = None
    if preset == "minibinder":
        binder_name = "minibinder"
    else:  # preset == "scfv"
        framework = (form.get("binder_framework") or "").strip()
        if framework not in FRAMEWORK_NAMES:
            return None, (
                "Pick a scFv framework: trastuzumab, atezolizumab, or "
                "ocankitug."
            )
        binder_name = framework

    seed, seed_err = _parse_seed(form.get("seed") or "")
    if seed_err:
        return None, seed_err

    batch_size, bs_err = _parse_batch_size(form.get("batch_size") or "")
    if bs_err:
        return None, bs_err

    n_seeds, n_seeds_err = _parse_n_seeds(form.get("n_seeds") or "")
    if n_seeds_err:
        return None, n_seeds_err

    use_scaling_critics = (
        (form.get("use_scaling_critics") or "").strip().lower() in {"on", "1", "true", "yes"}
    )

    label_bits = []
    if target_name:
        label_bits.append(target_name.upper())
    else:
        label_bits.append(f"pasted target ({len(target_sequence or '')} aa)")
    if preset == "scfv":
        label_bits.append(binder_name.replace("_framework_vhvl", "").title() + " scFv")
    else:
        label_bits.append("minibinder")

    return (
        {
            "preset": preset,
            "target_mode": target_mode,
            "target_name": target_name,
            "target_sequence": target_sequence,
            "binder_name": binder_name,
            "is_antibody": preset == "scfv",
            "seed": seed,
            "n_seeds": n_seeds,
            "batch_size": batch_size,
            "use_scaling_critics": use_scaling_critics,
            "target": " + ".join(label_bits),
            # n_seeds * batch_size = total designs returned. n_seeds fans
            # out to parallel Modal children (same wall-clock as one
            # seed), batch_size runs N designs inside one gradient pass.
            # The wallet estimator treats each design as one billable
            # unit so cost scales linearly with both axes.
            "parameters": {"n_designs_total": n_seeds * batch_size},
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the ESMFold2-design job_spec for the Modal pipeline.

    ``presigned_url`` is unused — this tool does not accept a target PDB.
    ``seed`` here is the *start* of the sweep range when ``n_seeds > 1``;
    the Modal orchestrator runs seeds [seed, seed + n_seeds).
    """
    return {
        "preset": inputs["preset"],
        "target_name": inputs["target_name"],
        "target_sequence": inputs["target_sequence"],
        "binder_name": inputs["binder_name"],
        "is_antibody": inputs["is_antibody"],
        "seed": inputs["seed"],
        "n_seeds": inputs.get("n_seeds", 1),
        "batch_size": inputs["batch_size"],
        "use_scaling_critics": inputs["use_scaling_critics"],
        "parameters": inputs["parameters"],
    }


adapter = ToolAdapter(
    slug="esmfold2-design",
    label="ESMFold2 design",
    blurb=(
        "Gradient-based binder design via ESMFold2 inversion. Pick a "
        "paper-validated target preset, choose minibinder or scFv mode, "
        "and get ranked designs with iPTM scores in one model pass."
    ),
    presets=(
        Preset(
            slug="minibinder",
            label="De novo minibinder (60 to 200 aa)",
            description=(
                "Free 60 to 200 aa scaffold generated with an isoelectric "
                "point filter (pI &lt; 6) baked in. No framework "
                "constraints. Equivalent goal to RFdiffusion plus "
                "ProteinMPNN but in one gradient pass through ESMFold2."
            ),
            requires_pdb=False,
            long_running=True,
        ),
        Preset(
            slug="scfv",
            label="scFv with framework-locked CDRs",
            description=(
                "All six CDRs designed jointly on a locked humanized "
                "framework. Three frameworks available: trastuzumab, "
                "atezolizumab, ocankitug. The only catalog tool that "
                "designs paired heavy + light scFv CDRs end-to-end."
            ),
            requires_pdb=False,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=False,
    form_template="tools/esmfold2_design_form.html",
    results_partial="tools/esmfold2_design_results.html",
)

register(adapter)
