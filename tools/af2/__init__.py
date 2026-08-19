"""AF2 standalone (D2) — atomic primitive.

Modal app: ``ranomics-af2-prod``. GPU: A100-80GB.
Clones the D1 ProteinMPNN shape per ``docs/ATOMIC-TOOLS.md`` D2 section.

The user uploads a FASTA (single chain or multimer) and receives a
predicted structure (PDB), per-residue pLDDT, a PAE matrix, and the
scalar pTM / ipTM confidence metrics. 2-credit tool (4 credits above
1500 AA total per PRODUCT-PLAN.md; we cap at 1500 AA in the validate
branch for the atomic launch).

D2 exposes a single ``standalone`` tier: caller-supplied FASTA, default
3 recycles, ColabFold MMseqs2 MSA. It runs on the Modal function
(``ranomics-af2-prod::run_tool``) and ``run_pipeline.py``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from shared.sequence_parsing import (
    CANONICAL_AA as SHARED_CANONICAL_AA,
    find_non_canonical_residues,
    parse_fasta_or_lines,
)
from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Bounds (also enforced on the pipeline side for direct ``modal run`` use).
# ---------------------------------------------------------------------------

RECYCLES_MIN = 1
RECYCLES_MAX = 5
# Aggregate-sequence-length cap for the atomic tier. Above this, users
# drop to the composite pipelines (BindCraft / PXDesign) where the
# pricing accounts for the longer A100-80GB seat. ATOMIC-TOOLS.md D2
# notes multimer folds above 1500 AA charge 4 credits; we hard-cap at
# 1500 AA on the standalone tier for the Wave-3 launch so the 2-credit
# price is correct for every accepted payload.
MAX_TOTAL_AA = 1500
# Per-chain sanity cap — well above any real monomer but protects
# against pathological uploads and keeps pae matrix memory bounded.
MAX_CHAIN_AA = 1400
# Batch preset ceiling. AF2 + MSA + templates is the slowest of the
# three structure-prediction tools (~3-5 min per fold cold, ~1-2 min
# warm). Sequential within a single container: 50 records × ~5 min ≈
# 4 h, the Modal session ceiling we accept for V1. Larger batches
# require Modal-side fan-out (`inner_fold.map`) which is a follow-up.
MAX_BATCH = 50
CANONICAL_AA_AF2 = set(SHARED_CANONICAL_AA)


def _parse_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, falling back to ``default`` on failure."""
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_bool(value: Any, default: bool) -> bool:
    """Coerce a form checkbox value to bool.

    HTML checkboxes send ``on`` / ``true`` / ``1`` when ticked and are
    simply absent when unticked, so callers must distinguish "key
    missing" (return ``default``) from "key present with value".
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s == "":
        return default
    return s in {"on", "true", "1", "yes", "y"}


def _parse_fasta(text: str) -> tuple[list[dict[str, str]], Optional[str]]:
    """Parse a FASTA blob into a list of ``{header, sequence}`` records.

    Returns ``(records, error)``. On ``error`` non-None, ``records`` is
    undefined.

    ColabFold accepts both single-chain (one ``>header``) and multimer
    (multiple headers joined with ``:`` in a single sequence) FASTA
    inputs. We normalise on the multi-record shape and let run_pipeline
    concatenate with ``:`` before handing to colabfold_batch.
    """
    text = (text or "").strip()
    if not text:
        return [], "FASTA is empty."
    if not text.startswith(">"):
        return [], "FASTA must start with a '>' header line."

    records: list[dict[str, str]] = []
    header: Optional[str] = None
    buf: list[str] = []

    def flush() -> Optional[str]:
        if header is None:
            return None
        seq = "".join(buf).replace(" ", "").replace("\t", "")
        if not seq:
            return f"Header {header!r} has no sequence."
        # Reject obvious garbage — FASTA should be plain 20 amino acids.
        # ColabFold will tolerate unknowns ('X') but not special chars.
        bad = set(seq.upper()) - set("ACDEFGHIKLMNPQRSTVWYX")
        if bad:
            return (
                f"Sequence for {header!r} contains illegal characters "
                f"(not in the 20 standard AA + X): {sorted(bad)}"
            )
        records.append({"header": header, "sequence": seq.upper()})
        return None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            err = flush()
            if err:
                return [], err
            header = line[1:].strip() or f"chain{len(records) + 1}"
            buf = []
        else:
            buf.append(line)

    err = flush()
    if err:
        return [], err

    if not records:
        return [], "FASTA parsed zero sequences."
    return records, None


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the AF2 job_spec shape.

    Two presets:

    - ``standalone`` (default): caller-supplied FASTA. Multi-record FASTA
      is treated as ONE multimer fold (chains joined with ``:``) — the
      historic single-fold contract.
    - ``batch``: caller-supplied list of fold targets. Each record is one
      independent fold; ``:`` inside a record marks chain breaks for a
      per-record multimer. Up to ``MAX_BATCH`` records.

    A missing or blank preset is treated as ``standalone`` so the form's
    hidden preset field is robust.

    The shape returned is consumed by ``build_payload`` below and is
    also the ``inputs`` blob persisted on the ``tool_jobs`` row.
    """
    preset = (form.get("preset") or "standalone").strip() or "standalone"
    if preset not in {"standalone", "batch"}:
        return None, "Pick a preset (standalone or batch)."

    if preset == "batch":
        return _validate_batch(form)

    # standalone tier — caller target.
    # FASTA arrives either via textarea (``fasta`` form field) or
    # file upload (``fasta_file``). Textarea wins if both present.
    fasta_text = (form.get("fasta") or "").strip()
    if not fasta_text:
        uploaded = files.get("fasta_file") if files else None
        if uploaded is not None and getattr(uploaded, "filename", ""):
            try:
                raw = uploaded.read()
            except Exception as exc:
                return None, f"Could not read uploaded FASTA: {exc}"
            if isinstance(raw, bytes):
                try:
                    fasta_text = raw.decode("utf-8", errors="replace").strip()
                except Exception as exc:
                    return None, f"Uploaded FASTA is not valid UTF-8: {exc}"
            else:
                fasta_text = str(raw).strip()

    if not fasta_text:
        return None, "Paste a FASTA or upload a FASTA file."

    records, err = _parse_fasta(fasta_text)
    if err:
        return None, err

    total_aa = sum(len(r["sequence"]) for r in records)
    if total_aa > MAX_TOTAL_AA:
        return (
            None,
            f"Total sequence length is {total_aa} AA, which exceeds the "
            f"{MAX_TOTAL_AA} AA atomic-tier cap. Split or trim your input.",
        )
    for rec in records:
        if len(rec["sequence"]) > MAX_CHAIN_AA:
            return (
                None,
                f"Chain {rec['header']!r} is {len(rec['sequence'])} AA, "
                f"above the {MAX_CHAIN_AA} AA per-chain cap.",
            )

    num_recycles = _parse_int(form.get("num_recycles"), 3)
    if num_recycles < RECYCLES_MIN or num_recycles > RECYCLES_MAX:
        return (
            None,
            f"num_recycles must be between {RECYCLES_MIN} and {RECYCLES_MAX}.",
        )

    # Default False to match the form's visible unchecked checkbox.
    # Browsers omit unchecked checkboxes from the POST body, so a missing
    # field MUST mean off, not on. Templates also depend on a pdb70
    # database being present in the image; today's AF2 image ships only
    # hhsearch, so end-to-end template runs require explicit opt-in.
    use_templates = _parse_bool(form.get("use_templates"), False)

    # Multimer detection: ColabFold + AlphaFold-multimer kicks in when
    # the record list has > 1 entry. Single-record FASTAs run monomer
    # regardless of length.
    model_preset = "multimer" if len(records) > 1 else "monomer"

    target_desc = (
        f"Your FASTA — {len(records)} chain"
        f"{'s' if len(records) != 1 else ''}, {total_aa} AA"
    )

    return (
        {
            "preset": preset,
            "fasta_records": records,
            "model_preset": model_preset,
            "num_recycles": num_recycles,
            "use_templates": use_templates,
            "target": target_desc,
        },
        None,
    )


def _validate_batch(form: Mapping[str, Any]) -> tuple[Optional[dict], Optional[str]]:
    """Parse the ``sequences`` textarea into a batch_records list.

    Each record's ``sequence`` may contain ``:`` chain breaks for an
    intra-record multimer fold. Per-record total AA ≤ MAX_TOTAL_AA;
    per-chain AA ≤ MAX_CHAIN_AA.
    """
    raw = form.get("sequences") or form.get("batch_sequences") or ""
    records, err = parse_fasta_or_lines(raw, default_name_prefix="fold")
    if err:
        return None, err
    if not records:
        return None, "Paste at least one fold target."
    if len(records) > MAX_BATCH:
        return None, (
            f"Max {MAX_BATCH} records per batch run "
            f"(received {len(records)}). AF2 + MSA is the slowest tool; "
            "use the ESMFold batch for monomer screening and AF2 single-"
            "fold for final validation."
        )

    for r in records:
        name = r["name"]
        seq = r["sequence"]
        chains = seq.split(":") if ":" in seq else [seq]
        chains = [c for c in chains if c]
        if not chains:
            return None, f"Record {name!r} parsed to zero chains."
        total = sum(len(c) for c in chains)
        if total > MAX_TOTAL_AA:
            return None, (
                f"Record {name!r} is {total} AA across {len(chains)} "
                f"chain(s) — max {MAX_TOTAL_AA} per record."
            )
        for ci, chain in enumerate(chains):
            if len(chain) > MAX_CHAIN_AA:
                return None, (
                    f"Record {name!r} chain {ci + 1} is {len(chain)} AA "
                    f"— max {MAX_CHAIN_AA} per chain."
                )
            bad = find_non_canonical_residues(chain, frozenset(CANONICAL_AA_AF2))
            if bad:
                return None, (
                    f"Record {name!r} chain {ci + 1} contains non-canonical "
                    f"residues: {bad}"
                )

    num_recycles = _parse_int(form.get("num_recycles"), 3)
    if num_recycles < RECYCLES_MIN or num_recycles > RECYCLES_MAX:
        return None, (
            f"num_recycles must be between {RECYCLES_MIN} and {RECYCLES_MAX}."
        )
    # Default False to match the form's visible unchecked checkbox.
    # Browsers omit unchecked checkboxes from the POST body, so a missing
    # field MUST mean off, not on. Templates also depend on a pdb70
    # database being present in the image; today's AF2 image ships only
    # hhsearch, so end-to-end template runs require explicit opt-in.
    use_templates = _parse_bool(form.get("use_templates"), False)

    # Normalise per-record shape: keep raw ``sequence`` (with ``:``
    # preserved) plus a precomputed ``chains`` array for the pipeline so
    # downstream code does not re-split. Mirrors the standalone-tier
    # ``fasta_records`` shape (header → name; sequence stays as-is).
    batch_records = [
        {
            "name": r["name"],
            "sequence": r["sequence"],
            "chains": (r["sequence"].split(":") if ":" in r["sequence"] else [r["sequence"]]),
        }
        for r in records
    ]

    return (
        {
            "preset": "batch",
            "batch_records": batch_records,
            "num_recycles": num_recycles,
            "use_templates": use_templates,
            "target": f"AF2 batch ({len(batch_records)} records)",
            "parameters": {"n_designs_total": len(batch_records)},
        },
        None,
    )


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the AF2 job_spec shape ``run_pipeline.py`` expects.

    AF2 does not consume a presigned URL — the FASTA ships inline in the
    payload because it is small (< 30 kB even at the 1500-AA cap). This
    keeps the atomic tool self-contained and avoids round-tripping
    through Supabase Storage for a few kB of text.

    Batch preset forwards ``batch_records`` plus the shared
    ``num_recycles`` / ``use_templates`` parameters; the standalone
    preset keeps the existing single-fold contract.
    """
    if inputs.get("preset") == "batch":
        return {
            "batch_records": inputs.get("batch_records", []),
            "parameters": {
                "num_recycles": inputs["num_recycles"],
                "use_templates": bool(inputs["use_templates"]),
                "n_designs_total": len(inputs.get("batch_records", [])),
            },
        }
    return {
        "fasta_records": inputs["fasta_records"],
        "parameters": {
            "model_preset": inputs["model_preset"],
            "num_recycles": inputs["num_recycles"],
            "use_templates": bool(inputs["use_templates"]),
        },
    }


adapter = ToolAdapter(
    slug="af2",
    label="AlphaFold2",
    blurb=(
        "Paste a sequence — one chain or several — and get a predicted "
        "3D structure back with per-residue and per-residue-pair "
        "confidence scores. About 5 to 10 min per run."
    ),
    presets=(
        Preset(
            slug="standalone",
            label="Standalone with your FASTA",
            description=(
                "Paste or upload FASTA (single chain or multimer). "
                "ColabFold MMseqs2 MSA plus AF2. Up to 1500 AA total "
                "across chains. ~5 to 10 min on A100-80GB."
            ),
            # FASTA ships inline in the payload, not via PDB upload —
            # leave requires_pdb False.
            requires_pdb=False,
        ),
        Preset(
            slug="batch",
            label="Batch for many fold targets",
            description=(
                "Fold many independent targets in one job (up to "
                "50 records). Each record can be a monomer or a "
                "multimer (use ``:`` to separate chains inside a record). "
                "Per-design results stream into the job page as folds "
                "complete. Slowest of the structure-prediction tools. "
                "Expect ~5 to 10 min per fold."
            ),
            requires_pdb=False,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=False,
    form_template="tools/af2_form.html",
    results_partial="tools/af2_results.html",
)

register(adapter)
