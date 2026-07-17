"""IgGM — antibody / nanobody design + structure prediction (diffusion).

Modal app: ``ranomics-iggm-prod``. GPU: A100-40GB. Atomic primitive.

IgGM (Tencent AI4S, ICLR 2025, MIT) is a generative diffusion model for
antibody/nanobody engineering. One model covers antibody-antigen complex
structure prediction, CDR design, framework redesign / humanization,
affinity maturation, and inverse (sequence-from-structure) design.

Presets map to IgGM's ``--run_task`` (argparse choices are
``design`` / ``inverse_design`` / ``fr_design`` / ``affinity_maturation``,
default ``design``). The default ``design`` task serves both structure
prediction and CDR design, so those two are split into separate presets
for legibility (same CLI, different validation + guidance); the other
three each map to their own ``--run_task``:

- ``complex_prediction`` — fold the antibody-antigen complex (run_task
  ``design``, no masking required). Optional epitope hint.
- ``cdr_design``          — redesign CDRs masked with ``X`` (run_task ``design``).
- ``fr_design``           — framework redesign / humanization.
- ``affinity_maturation`` — variants to improve binding; needs a wild-type
  reference (``--fasta_origin``) + ``--num_samples``.
- ``inverse_design``      — sequence from backbone.

Input model (single-source-of-truth for the antigen):
the user pastes only the *antibody* chains as FASTA (``>H`` required,
``>L`` optional = nanobody when absent), with design positions masked
``X``. The antigen is NOT hand-typed — it is derived from the uploaded
antigen PDB (chain selected via ``target_chain``), and the pipeline
appends it as the last FASTA record before invoking ``design.py``
(IgGM reads the antigen as ``ids[-1]`` / ``sequences[-1]``). Epitope
positions come from the structure picker as PDB residue numbers; the
pipeline converts them to IgGM's 1-based sequential positions along the
antigen chain (see ``run_pipeline.py``).

BUILD-TIME VERIFICATION (pinned commit + staging smoke I-1): the exact
FASTA<->PDB chain-id coupling, the checkpoint cache dir, and whether
``--fasta``/``--antigen`` accept a single file vs a directory. The I-1
smoke asserts the predicted antibody actually contacts the requested
epitope, which end-to-end validates the epitope conversion + coupling.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Bounds. Mirror IgGM's argparse where it has one; otherwise conservative
# launch caps re-tuned from the canary runs.
# ---------------------------------------------------------------------------

ANTIBODY_LEN_MIN = 80          # a VH domain is ~110-130 aa; guard fat-finger
ANTIBODY_LEN_MAX = 400         # paired scFv-ish upper bound per chain
NUM_SAMPLES_MAX = 100          # per-field bound on the raw sample count
# Ceiling on the TOTAL inference passes one job may launch. Every preset except
# affinity_maturation runs exactly num_samples passes (one per sample). IgGM's
# affinity_maturation instead runs one pass PER masked position PER sample (a
# per-position deep scan): passes = num_samples * n_masked. Verified on canary
# I-3 — num_samples=100 on a 14-residue CDR-H3 mask launched 1400 passes
# (~9.3 h @ ~24 s/pass), far past the Modal session window. At the measured
# ~24 s/pass, 100 passes ≈ 40 min, comfortably inside the 1 h session cap with
# teardown headroom. Bounding the PRODUCT here is the pre-GPU guard that stops a
# maturation job from running past the cap, failing, and still billing.
MAX_TOTAL_PASSES = 100
MAX_ANTIGEN_SIZE_DEFAULT = 2000  # IgGM -mas default
MAX_ANTIGEN_SIZE_MIN = 64
MAX_ANTIGEN_SIZE_MAX = 2000
# Preflight antigen-length reject threshold (calibrated below the observed
# OOM envelope in canary I-*; conservative until then). Independent of
# ``max_antigen_size`` (IgGM's crop knob) — we reject rather than crop so
# epitope positions never shift underneath the mapping.
ANTIGEN_LEN_HARD_CAP = 1000
EPITOPE_MAX = 128              # generous; a real epitope is ~15-25 residues
CANONICAL_AA = set("ACDEFGHIKLMNPQRSTVWY")
DESIGN_AA = CANONICAL_AA | {"X"}  # X = mask token, legal only in the design FASTA

# preset slug -> IgGM --run_task value
_PRESET_RUN_TASK = {
    "complex_prediction": "design",
    "cdr_design": "design",
    "fr_design": "fr_design",
    "affinity_maturation": "affinity_maturation",
    "inverse_design": "inverse_design",
}
_PRESETS = tuple(_PRESET_RUN_TASK.keys())
# Presets that require at least one masked (X) position in the antibody FASTA.
# affinity_maturation is included: the X positions are the residues it matures
# (against the wild-type reference), and its total compute scales with their
# count, so "no masks" is a no-op we reject up front.
_MASK_REQUIRED = {"cdr_design", "fr_design", "affinity_maturation"}


def _parse_epitope(raw: str) -> tuple[Optional[list[int]], Optional[str]]:
    """Comma/space/semicolon-separated positive 1-indexed PDB residue numbers.

    These are the raw numbers from the structure picker. The pipeline
    converts them to IgGM's 1-based sequential positions after parsing the
    antigen chain from the PDB, so we only sanity-check them here.
    """
    raw = (raw or "").strip()
    if not raw:
        return [], None
    out: list[int] = []
    for tok in raw.replace(";", " ").replace(",", " ").split():
        tok = tok.strip()
        if not tok:
            continue
        try:
            n = int(tok)
        except ValueError:
            return None, f"Epitope residues must be integers; got {tok!r}."
        out.append(n)
    if len(out) > EPITOPE_MAX:
        return None, f"Too many epitope residues (max {EPITOPE_MAX})."
    return out, None


def _parse_antibody_fasta(
    raw: str,
) -> tuple[Optional[list[dict[str, str]]], Optional[str]]:
    """Parse the pasted antibody FASTA into ordered ``{header, sequence}``.

    Requires a heavy chain (``>H``); a light chain (``>L``) is optional
    (its absence = nanobody / VHH). The antigen is NOT accepted here — it
    is derived from the uploaded PDB — so an ``>A`` record is rejected with
    a helpful message. ``X`` is a legal mask token; other non-canonical
    residues are rejected.
    """
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    if not lines:
        return None, "Paste the antibody heavy chain (>H) sequence."
    if not any(ln.startswith(">") for ln in lines):
        return None, "Antibody FASTA needs a >H header (and optional >L)."

    records: list[dict[str, str]] = []
    header: Optional[str] = None
    buf: list[str] = []

    def _flush() -> None:
        if header is not None and buf:
            records.append({"header": header, "sequence": "".join(buf).upper()})

    for ln in lines:
        if ln.startswith(">"):
            _flush()
            header = ln[1:].strip()
            buf = []
        else:
            buf.append(ln)
    _flush()

    if not records:
        return None, "Could not parse any antibody sequences from the FASTA."

    headers = {r["header"].upper() for r in records}
    if "A" in headers:
        return None, (
            "Do not include an >A (antigen) record; the antigen is taken "
            "from the uploaded PDB. Paste only the antibody chains (>H, >L)."
        )
    if "H" not in headers:
        return None, "Antibody FASTA must include a heavy chain (>H)."
    extra = headers - {"H", "L"}
    if extra:
        return None, (
            "Unexpected FASTA header(s): "
            + ", ".join(sorted(extra))
            + ". Use >H (heavy) and optionally >L (light)."
        )

    for r in records:
        seq = r["sequence"]
        if not (ANTIBODY_LEN_MIN <= len(seq) <= ANTIBODY_LEN_MAX):
            return None, (
                f"Chain >{r['header']} is {len(seq)} aa; must be "
                f"{ANTIBODY_LEN_MIN}-{ANTIBODY_LEN_MAX} aa."
            )
        bad = set(seq) - DESIGN_AA
        if bad:
            return None, (
                f"Chain >{r['header']} has non-canonical residues "
                f"{sorted(bad)} (X is allowed as the mask token)."
            )
    return records, None


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce the IgGM form into a job_spec. Antigen PDB flows through the
    shared ``requires_pdb`` staging path on the submit handler."""
    preset = (form.get("preset") or "complex_prediction").strip()
    if preset not in _PRESETS:
        return None, "Pick a preset."

    antibody, ab_err = _parse_antibody_fasta(form.get("fasta") or "")
    if ab_err:
        return None, ab_err

    has_mask = any("X" in r["sequence"] for r in antibody)
    if preset in _MASK_REQUIRED and not has_mask:
        return None, (
            "This mode redesigns masked positions; mark the residues to "
            "design with X in the antibody FASTA (at least one)."
        )

    antigen_chain = (form.get("target_chain") or "A").strip()
    if not antigen_chain or len(antigen_chain) > 4:
        return None, "Antigen chain ID is required (max 4 characters)."

    epitope, ep_err = _parse_epitope(form.get("epitope") or "")
    if ep_err:
        return None, ep_err

    # max_antigen_size (IgGM crop knob). Default matches upstream (2000).
    raw_mas = (form.get("max_antigen_size") or "").strip()
    if raw_mas:
        try:
            max_antigen_size = int(raw_mas)
        except ValueError:
            return None, "Max antigen size must be an integer."
        if not (MAX_ANTIGEN_SIZE_MIN <= max_antigen_size <= MAX_ANTIGEN_SIZE_MAX):
            return None, (
                f"Max antigen size must be {MAX_ANTIGEN_SIZE_MIN}-"
                f"{MAX_ANTIGEN_SIZE_MAX}."
            )
    else:
        max_antigen_size = MAX_ANTIGEN_SIZE_DEFAULT

    # num_samples. Maturation defaults higher (the paper example uses 100);
    # the other presets default to 1.
    raw_ns = (form.get("num_samples") or "").strip()
    default_ns = 8 if preset == "affinity_maturation" else 1
    if raw_ns:
        try:
            num_samples = int(raw_ns)
        except ValueError:
            return None, "Number of samples must be an integer."
    else:
        num_samples = default_ns
    if num_samples < 1 or num_samples > NUM_SAMPLES_MAX:
        return None, f"Number of samples must be 1-{NUM_SAMPLES_MAX}."

    fasta_origin: Optional[str] = None
    if preset == "affinity_maturation":
        raw_origin = (form.get("fasta_origin") or "").strip()
        wt, wt_err = _parse_antibody_fasta(raw_origin)
        if wt_err:
            return None, f"Wild-type reference: {wt_err}"
        # WT reference must be canonical (no X) and align per-chain to the design.
        design_by_chain = {r["header"].upper(): r["sequence"] for r in antibody}
        for r in wt:
            if "X" in r["sequence"]:
                return None, "Wild-type reference must not contain X (no masks)."
            dh = r["header"].upper()
            if dh not in design_by_chain:
                return None, (
                    f"Wild-type chain >{r['header']} has no matching design chain."
                )
            if len(r["sequence"]) != len(design_by_chain[dh]):
                return None, (
                    f"Wild-type chain >{r['header']} length "
                    f"({len(r['sequence'])}) must equal the design chain "
                    f"({len(design_by_chain[dh])}); indels are not supported."
                )
        if num_samples < 2:
            return None, "Affinity maturation needs num_samples >= 2."
        fasta_origin = raw_origin

    # ---- total inference passes: the wallet + session guard ----
    # design.py runs num_samples passes for every preset EXCEPT
    # affinity_maturation, which expands to one pass per masked (X) position
    # per sample. Bound the product so the job fits one Modal session (see
    # MAX_TOTAL_PASSES). n_masked drives both this cap and the wallet estimate
    # (mirrored in shared/wallet_estimates so the hold covers the real work).
    n_masked = sum(r["sequence"].count("X") for r in antibody)
    if preset == "affinity_maturation":
        total_passes = num_samples * n_masked
        if total_passes > MAX_TOTAL_PASSES:
            max_samples = MAX_TOTAL_PASSES // n_masked  # floor; may be < 2
            if max_samples >= 2:
                advice = f"Lower samples to <= {max_samples}, or mask fewer positions."
            else:
                # Even the 2-sample minimum over this many positions is over-cap,
                # so no sample count works: the only fix is fewer masked positions.
                advice = (
                    f"Even 2 samples over {n_masked} positions exceeds the "
                    f"limit; mask at most {MAX_TOTAL_PASSES // 2} positions."
                )
            return None, (
                f"Affinity maturation designs one variant per masked position "
                f"per sample: {num_samples} samples x {n_masked} masked "
                f"positions = {total_passes} designs, over the "
                f"{MAX_TOTAL_PASSES}-per-run limit. {advice}"
            )
    else:
        total_passes = num_samples
        if total_passes > MAX_TOTAL_PASSES:
            return None, (
                f"{total_passes} samples is over the {MAX_TOTAL_PASSES}-per-run "
                f"limit. Lower the sample count."
            )

    ab_desc = "nanobody (VHH)" if len(antibody) == 1 else "antibody (H+L)"

    return (
        {
            "preset": preset,
            "run_task": _PRESET_RUN_TASK[preset],
            "antibody_fasta": antibody,
            "fasta_origin": fasta_origin,
            "antigen_chain": antigen_chain,
            "epitope_pdb_resnums": epitope,
            "max_antigen_size": max_antigen_size,
            "num_samples": num_samples,
            # n_masked / total_passes: raw num_samples is what design.py gets on
            # the CLI; total_passes is the true design count IgGM produces (it
            # equals num_samples for every preset except affinity_maturation,
            # which expands per masked position). Downstream uses total_passes
            # for the delivered-design count + pricing.
            "n_masked": n_masked,
            "total_passes": total_passes,
            # relax forced off at launch (PyRosetta license + py3.10 + slow).
            "relax": False,
            "target": f"{ab_desc} vs antigen chain {antigen_chain}",
            "num_samples_for_scaling": total_passes,
            "parameters": {"n_designs_total": total_passes},
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the IgGM job_spec. The antigen PDB presigned URL is forwarded
    by the generic submit route via ``_input_presigned_url`` — not embedded
    here (matches boltz2).

    Campaign-safe: a compute campaign validates with a placeholder
    ``num_samples=1`` and injects the real per-chunk ``num_samples`` afterward,
    so ``total_passes`` / ``parameters`` (frozen at the placeholder in
    ``validate``) are RECOMPUTED here from the injected count for every preset
    except ``affinity_maturation``. design.py produces exactly ``num_samples``
    designs for those presets, so ``total_passes == num_samples``.
    ``affinity_maturation`` is atomic-only (rejected on the campaign route), so
    its stored ``total_passes`` (= num_samples * n_masked, computed in
    ``validate``) stays authoritative. The atomic path is unchanged for every
    preset (there ``inputs['total_passes']`` already equals the recomputed
    value)."""
    num_samples = int(inputs["num_samples"])
    if inputs["preset"] == "affinity_maturation":
        total_passes = int(inputs.get("total_passes") or num_samples)
        parameters = inputs.get("parameters") or {"n_designs_total": total_passes}
    else:
        total_passes = num_samples
        parameters = {"n_designs_total": total_passes}
    return {
        "preset": inputs["preset"],
        "run_task": inputs["run_task"],
        "antibody_fasta": inputs["antibody_fasta"],
        "fasta_origin": inputs["fasta_origin"],
        "antigen_chain": inputs["antigen_chain"],
        "epitope_pdb_resnums": inputs["epitope_pdb_resnums"],
        "max_antigen_size": inputs["max_antigen_size"],
        "num_samples": num_samples,
        # total_passes / n_masked must reach the container: run_pipeline reads
        # total_passes for the progress heartbeats (design.py gets raw
        # num_samples on the CLI and expands internally for maturation).
        "total_passes": total_passes,
        "n_masked": inputs["n_masked"],
        "relax": inputs["relax"],
        "parameters": parameters,
    }


adapter = ToolAdapter(
    slug="iggm",
    label="IgGM",
    blurb=(
        "Antibody + nanobody design and structure prediction in one "
        "diffusion model. Predict an antibody-antigen complex, redesign "
        "CDRs or framework, mature affinity, or recover sequence from "
        "structure, epitope-guided, from the antigen you upload."
    ),
    presets=(
        Preset(
            slug="complex_prediction",
            label="Antibody-antigen complex prediction",
            description=(
                "Fold the antibody-antigen complex from the full heavy "
                "(and light) chain sequences against your antigen. Add an "
                "optional epitope to guide docking. Fastest mode."
            ),
            requires_pdb=True,
        ),
        Preset(
            slug="cdr_design",
            label="CDR design (H3 or all CDRs)",
            description=(
                "Mask CDR positions with X in the antibody FASTA and IgGM "
                "redesigns them against your antigen. Add an epitope for "
                "epitope-guided design."
            ),
            requires_pdb=True,
            long_running=True,
        ),
        Preset(
            slug="fr_design",
            label="Framework redesign / humanization",
            description=(
                "Mask framework positions with X; IgGM redesigns them. "
                "Use for humanization or framework engineering."
            ),
            requires_pdb=True,
            long_running=True,
        ),
        Preset(
            slug="affinity_maturation",
            label="Affinity maturation",
            description=(
                "Improve binding by exploring variants at the positions you "
                "mask with X, against a wild-type reference (same length, no "
                "masks). It designs one variant per masked position per sample, "
                "so a few samples over a short loop already gives a rich set."
            ),
            requires_pdb=True,
            long_running=True,
        ),
        Preset(
            slug="inverse_design",
            label="Inverse design (sequence from structure)",
            description=(
                "Recover the antibody sequence given the complex backbone."
            ),
            requires_pdb=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=True,
    form_template="tools/iggm_form.html",
    results_partial="tools/iggm_results.html",
)

register(adapter)
