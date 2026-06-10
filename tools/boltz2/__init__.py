"""Boltz-2 cofold validation (antibody-trained) — atomic primitive.

Modal app: ``ranomics-boltz2-prod``. GPU: A100-40GB.

The user uploads an antigen PDB plus one or more binder sequences
(scFv, nanobody, peptide, anything that folds as a protein chain) and
receives, per design, the predicted complex PDB, Boltz-2 confidence
metrics (ipTM, pTM, complex_pLDDT, complex_iplddt), and a hotspot
contact analysis against an optional list of antigen residue positions.

Two presets at launch:

- ``standalone`` — single-sequence cofold (YAML ``msa: empty`` per chain).
  Default. ~15 s / design on A100-40GB. The right choice for designed
  binder sequences (MPNN, RFantibody, BindCraft, BoltzGen, RFdiffusion,
  PXDesign outputs) where no informative MSA exists.
- ``msa_server`` — Boltz fetches MSAs from the public ColabFold MMseqs2
  endpoint via ``--use_msa_server``. ~3 min / design. Better for natural
  / near-native sequences; for designed sequences the MSA is usually
  dominated by the closest natural homologues and the result barely
  differs from ``standalone``.

The Modal pipeline lives in ``tools/boltz2/modal_app.py`` and the
subprocess body in ``tools/boltz2/run_pipeline.py``. Per-design PDBs are
streamed back to the hub via presigned PUT URLs (partial-results
contract) and surface live on the job detail page as each fold completes.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Bounds (also enforced on the pipeline side for direct ``modal run`` use).
# ---------------------------------------------------------------------------

BINDER_LEN_MIN = 20
BINDER_LEN_MAX = 400
MAX_BINDERS = 50
ANTIGEN_CHAIN_MAX = 4
CANONICAL_AA = set("ACDEFGHIKLMNPQRSTVWYX")


def _parse_hotspots(raw: str) -> tuple[Optional[list[int]], Optional[str]]:
    """Comma- or semicolon-separated 1-indexed positive integers. Empty -> []."""
    raw = (raw or "").strip()
    if not raw:
        return [], None
    out: list[int] = []
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            n = int(tok)
        except ValueError:
            return None, f"Hotspot residues must be integers; got {tok!r}."
        if n <= 0:
            return None, "Hotspot residues must be positive 1-indexed integers."
        out.append(n)
    return out, None


def _parse_binder_text(raw: str) -> tuple[
    Optional[list[dict[str, str]]], Optional[str]
]:
    """Accept either FASTA (``>name`` records) or one binder sequence per line.

    Returns ``([{name, sequence}, ...], None)`` on success.
    Returns ``(None, error_message)`` on parse failure.

    Auto-names unnamed sequences ``design_0``, ``design_1``, ... so the
    smoke-test path "paste one sequence" still works.
    """
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    if not lines:
        return None, "Paste at least one binder sequence."

    records: list[dict[str, str]] = []
    has_fasta_header = any(ln.startswith(">") for ln in lines)

    if not has_fasta_header:
        # One sequence per line. Auto-name as design_<i>.
        for i, ln in enumerate(lines):
            records.append({"name": f"design_{i}", "sequence": ln.upper()})
        return records, None

    header: Optional[str] = None
    buf: list[str] = []
    for ln in lines:
        if ln.startswith(">"):
            if header is not None:
                records.append({
                    "name": header,
                    "sequence": "".join(buf).upper(),
                })
            header = ln[1:].strip() or f"design_{len(records)}"
            buf = []
        else:
            # Allow inline sequences after the previous header.
            buf.append(ln)
    if header is not None:
        records.append({"name": header, "sequence": "".join(buf).upper()})

    # FASTA without any sequence after a header.
    records = [r for r in records if r["sequence"]]
    return records, None


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the Boltz-2 job_spec shape.

    Antigen PDB upload + chain ID + hotspots flow through the shared
    PDB-staging path on the submit handler (``requires_pdb=True``).
    Binder sequences travel inline in the job_spec (no Storage round-trip
    — they are short).
    """
    preset = (form.get("preset") or "standalone").strip() or "standalone"
    if preset not in {"standalone", "msa_server"}:
        return None, "Pick a preset (standalone or msa_server)."

    # Antigen chain ID. Field name is ``target_chain`` for hotspot-picker
    # JS compat (the picker hardcodes that selector). Semantically the
    # field is "antigen chain" in the Boltz-2 mental model.
    antigen_chain = (form.get("target_chain") or "A").strip()
    if not antigen_chain:
        return None, "Antigen chain ID is required."
    if len(antigen_chain) > ANTIGEN_CHAIN_MAX:
        return None, (
            f"Antigen chain ID too long (max {ANTIGEN_CHAIN_MAX} characters)."
        )

    hotspots, hot_err = _parse_hotspots(form.get("hotspot_residues") or "")
    if hot_err:
        return None, hot_err

    binders, bind_err = _parse_binder_text(form.get("binder_sequences") or "")
    if bind_err:
        return None, bind_err
    if not binders:
        return None, "Could not parse any binder sequences."
    if len(binders) > MAX_BINDERS:
        return None, (
            f"Max {MAX_BINDERS} binder sequences per run "
            f"(received {len(binders)})."
        )

    for b in binders:
        name = b["name"]
        seq = b["sequence"]
        if len(seq) < BINDER_LEN_MIN:
            return None, (
                f"Binder {name!r} is {len(seq)} aa — min {BINDER_LEN_MIN}."
            )
        if len(seq) > BINDER_LEN_MAX:
            return None, (
                f"Binder {name!r} is {len(seq)} aa — max {BINDER_LEN_MAX}."
            )
        non_canonical = set(seq) - CANONICAL_AA
        if non_canonical:
            return None, (
                f"Binder {name!r} contains non-canonical residues: "
                f"{sorted(non_canonical)}"
            )

    return (
        {
            "preset": preset,
            "target_chain": antigen_chain,
            "hotspot_residues": hotspots,
            "binder_sequences": binders,
            "target": (
                f"Antigen + {len(binders)} binder"
                f"{'s' if len(binders) != 1 else ''}"
            ),
            "parameters": {"n_designs_total": len(binders)},
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the Boltz-2 job_spec for ``run_pipeline.py``.

    The antigen PDB presigned URL is forwarded by the generic submit
    route via ``_input_presigned_url`` — this function does not embed it.
    """
    return {
        "preset": inputs["preset"],
        "antigen_chain": inputs["target_chain"],
        "hotspot_residues": inputs["hotspot_residues"],
        "binder_sequences": inputs["binder_sequences"],
        "parameters": inputs["parameters"],
    }


adapter = ToolAdapter(
    slug="boltz2",
    label="Boltz-2",
    blurb=(
        "Cofold validation. Validate designed binders against your "
        "antigen with an antibody-trained cofold model. ~15 s per design "
        "in single-sequence mode."
    ),
    presets=(
        Preset(
            slug="standalone",
            label="Single-sequence (fast)",
            description=(
                "YAML ``msa: empty`` per chain. The right choice for "
                "designed sequences (MPNN, RFantibody, BindCraft, "
                "BoltzGen, RFdiffusion, PXDesign outputs) where no "
                "informative MSA exists. ~15 s/design on A100-40GB."
            ),
            requires_pdb=True,
        ),
        Preset(
            slug="msa_server",
            label="With MSA (slower, natural sequences)",
            description=(
                "Boltz fetches MSAs from the public ColabFold MMseqs2 "
                "endpoint at runtime. Better for natural / near-native "
                "sequences; ~3 min/design including MSA fetch."
            ),
            requires_pdb=True,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=True,
    form_template="tools/boltz2_form.html",
    results_partial="tools/boltz2_results.html",
)

register(adapter)
