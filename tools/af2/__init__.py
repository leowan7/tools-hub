"""AF2 standalone (D2) — atomic primitive.

Modal app: ``ranomics-af2-prod``. GPU: A100-80GB.
Clones the D1 ProteinMPNN shape per ``docs/ATOMIC-TOOLS.md`` D2 section.

The user uploads a FASTA (single chain or multimer) and receives a
predicted structure (PDB), per-residue pLDDT, a PAE matrix, and the
scalar pTM / ipTM confidence metrics. 2-credit tool (4 credits above
1500 AA total per PRODUCT-PLAN.md; we cap at 1500 AA in the validate
branch for the atomic launch).

Unlike the composite pipelines, D2 exposes two tiers:

- ``smoke`` — baked ~58-residue BPTI monomer. No FASTA upload. 1 recycle,
  single-sequence mode (no MSA fetch). Demos the output shape before the
  user spends real compute.
- ``standalone`` — caller-supplied FASTA. Default 3 recycles, ColabFold
  MMseqs2 MSA. 2 credits.

Both tiers use the same Modal function (``ranomics-af2-prod::run_tool``)
and the same ``run_pipeline.py``; tier selection only changes which
FASTA is used and the recycle / MSA defaults.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

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

    Branches on preset:
      - ``smoke``: baked ~58 aa BPTI monomer, 1 recycle, no MSA. Credits-
        free demo.
      - ``standalone``: caller-supplied FASTA + ``num_recycles`` +
        ``use_templates`` + implicit ``model_preset`` (monomer vs
        multimer inferred from record count).

    The shape returned is consumed by ``build_payload`` below and is
    also the ``inputs`` blob persisted on the ``tool_jobs`` row.
    """
    preset = (form.get("preset") or "").strip()
    if preset not in {"smoke", "standalone"}:
        return None, "Pick a preset."

    if preset == "smoke":
        return (
            {
                "preset": preset,
                "fasta_records": [
                    {
                        "header": "BPTI_smoke",
                        "sequence": (
                            "RPDFCLEPPYTGPCKARIIRYFYNAKAGLCQTFVYGGCRAKRNNFKS"
                            "AEDCMRTCGGA"
                        ),
                    }
                ],
                "model_preset": "monomer",
                "num_recycles": 1,
                "use_templates": False,
                # Pass-through metadata for the results page.
                "target": "BPTI (58 aa monomer)",
            },
            None,
        )

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

    use_templates = _parse_bool(form.get("use_templates"), True)

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


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the AF2 job_spec shape ``run_pipeline.py`` expects.

    AF2 does not consume a presigned URL — the FASTA ships inline in the
    payload because it is small (< 30 kB even at the 1500-AA cap). This
    keeps the atomic tool self-contained and avoids round-tripping
    through Supabase Storage for a few kB of text.
    """
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
    label="AlphaFold2 — structure prediction from sequence",
    blurb=(
        "Paste a FASTA (monomer or multimer), get a predicted structure "
        "with pLDDT, PAE, and pTM/ipTM. Atomic primitive — A100-80GB, "
        "~5-10 min per run."
    ),
    presets=(
        Preset(
            slug="smoke",
            label="Smoke — BPTI demo, 0 credits",
            credits_cost=0,
            description=(
                "Runs against a baked BPTI (58 aa) monomer with 1 recycle "
                "and no MSA. Same pipeline, smallest preset — verifies "
                "the tool works before you spend compute on a real target."
            ),
        ),
        Preset(
            slug="standalone",
            label="Standalone — your FASTA, 2 credits",
            credits_cost=2,
            description=(
                "Paste or upload FASTA (single chain or multimer). "
                "ColabFold MMseqs2 MSA + AF2. Up to 1500 AA total across "
                "chains. ~5-10 min on A100-80GB."
            ),
            # FASTA ships inline in the payload, not via PDB upload —
            # leave requires_pdb False on both presets.
            requires_pdb=False,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=False,
    form_template="tools/af2_form.html",
    results_partial="tools/af2_results.html",
)

register(adapter)
