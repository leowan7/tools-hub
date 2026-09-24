"""Boltz-2 cofold validation (antibody-trained) — atomic primitive.

Modal app: ``ranomics-boltz2-prod``. GPU: A100-40GB.

The user uploads an antigen PDB plus one or more binder sequences
(scFv, nanobody, peptide, anything that folds as a protein chain) and
receives, per design, the predicted complex PDB, Boltz-2 confidence
metrics (ipTM, pTM, complex_pLDDT, complex_iplddt), and a hotspot
contact analysis against an optional list of antigen residue positions.

Two presets at launch:

- ``standalone`` — single-sequence cofold (YAML ``msa: empty`` per chain).
  Default. ~69 s / design on A100-40GB, MEASURED — see the runtime note
  below, and the batching note after it for why that figure is now a
  ceiling rather than the rate. The right choice for designed binder
  sequences (MPNN,
  RFantibody, BindCraft, BoltzGen, RFdiffusion, PXDesign outputs) where
  no informative MSA exists.
- ``msa_server`` — Boltz fetches MSAs from the public ColabFold MMseqs2
  endpoint via ``--use_msa_server``. ~214 s / design on A100-40GB,
  MEASURED — see the runtime note below. Intended for natural /
  near-native sequences. NOT the default, and on the one comparison that
  exists it was actively worse: see the discrimination note below.

Runtime, both tiers measured
----------------------------
Job ``gate1-standalone-1789842854`` on prod ``ranomics-boltz2-prod`` v13
(``cab729e``), A100-40GB, 2026-09-19. Three designs: 81.9 s, 68.5 s,
69.5 s. The first runs ~13 s longer than the others, so the marginal
rate is ~69 s / design and a one-binder run is ~82 s of container time
plus ~9 s of container spawn (wall 229 s against 220 s of container
runtime). Per-design times are derived from the interval between
consecutive upload WARNING lines in the container log rather than from
an internal fold timer, so read them at ~1 s precision — far finer than
the 4.6x they correct.

CONDITIONS, which are the whole sample: binders 242-246 aa against a
107 aa antigen, single-sequence mode, ``--no_kernels --output_format
pdb``, n=1 per design. One antigen, one binder-length band, three folds.
The rate outside that band is not measured, so treat ~69 s as an anchor
rather than a curve. Every design in that run was its own ``boltz
predict`` process, so each per-design time above includes process
start-up and a model load. The first design's extra ~13 s is therefore
not a one-time model load, and its cause was not isolated. That is no
longer how ``tools/boltz2/run_pipeline.py::main`` folds — see the
batching note below — so ~69 s is the rate this run was measured at, not
the rate a ``standalone`` run costs now. The figure it replaces,
"~15 s / design", has no timing on
record in this repo; the commit that introduced it, ``d2a1b8c``, also
calls ~15 s "the fold kernel", which whole-process times can neither
confirm nor refute.

``msa_server`` was measured on 2026-09-21 by Gate 1 Rung B: job
``gate1-msa_server-1790046491``, same image and the same three binders as
Rung A. 643 s of pipeline runtime for 3 designs, so **214 s / design
(3.6 min)** — against the "~3 min / design" this file used to advertise,
~19% optimistic, and ~3.1x standalone's marginal rate for ~2.9x the cost.

That 214 s is an AGGREGATE, not a marginal rate: the split between the
MSA-server fetch and GPU compute was not measured, and unlike Rung A no
per-design interval was resolved, so there is no separate first-design
premium to subtract. Do not read it as a per-design marginal cost the way
~69 s can be read.

Batching: folds are chunked, and ~69 s is now a ceiling
--------------------------------------------------------
``tools/boltz2/run_pipeline.py::main`` folds ``FOLD_CHUNK`` designs per
``boltz predict`` process on ``standalone``. Boltz expands a directory of
input YAMLs into one record each and folds them in one process, so the
start-up and checkpoint load are paid once a chunk rather than once a
design. ``msa_server`` is exempt and still folds one design per process;
the constant's own comment carries why ten and not the whole run.

MEASURED by two throwaway Modal probes on A100-SXM4-40GB, 2026-09-23,
$0.20 of GPU in total, on Rung A's inputs and Rung A's flags. One process
folding 3 records took 112.4 s, against ~219.9 s for the same 3 folded
one per process; a second folding 5 records took 161.4 s. Subtracting
each run's folds from its total puts the FIXED cost — imports, model
build, checkpoint load, CUDA init, teardown, exit — at 60.3 s and 69.8 s,
paid once per process, and each record's own fold at 14.1-23.5 s, the
longest being the first fold in each process. The build-and-load term
alone moved 33.3 -> 41.3 s between the two, so read ~60-70 s as a band
rather than a constant.

EXTRAPOLATING that band, 50 standalone designs in chunks of 10 are
~1000-1525 s (5 x 60-70 s, plus 50 x 14-23.5 s) against ~3450 s at the
per-design rate: 58-72% of headroom under ``modal_app.py``'s 3600 s
``_MAX_SESSION_S`` instead of ~4%, and ~$0.71-1.09 instead of ~$2.46.

NOT measured, which is why nothing downstream has been re-anchored:

- No batched fold has run through prod, the web flow, the wrapper, an
  upload or the wallet. Both probes were standalone ``modal run`` apps,
  and the first rebuilt the image on a cache miss instead of pulling
  prod's, so neither ran on a byte-identical image.
- The largest batch ever run is 5 records, not 10 and not 50. Memory
  behaviour at chunk size, OOM skips and longer binders are unobserved.
- ``msa_server`` batching was never tried, which is the other half of why
  that preset is left alone.

So ``MAX_BINDERS``, ``MAX_BINDERS_BY_PRESET``, ``tools/boltz2/meta.py``
and the boltz2 rows in ``shared/wallet_estimates.py`` all still carry the
per-design ~69 s and ~214 s. Quoting a projection there would under-hold
the wallet against a cost nobody has watched, and the error runs the safe
way round: the hold is a cushioned estimate and the charge is billed on
actual GPU seconds, so an over-quote is released rather than kept. They
move when a prod run measures the chunked rate.

Batching does NOT pin the fold. Five byte-identical copies of one input,
folded in one process, spanned ipTM 0.263-0.830 and re-docked the binder
(21 pairwise CA RMSDs, median 16.3 A), and against a criterion fixed
before the run one copy landed 2.05 A from the pose the per-design path
produced. The variation is per fold, not per process — no ``--seed`` is
passed anywhere in this package, so the diffusion sampler is unseeded.
Full write-up in the 2026-09-23 rows of docs/VALIDATION-LOG.md under
"## Boltz-2".

Discrimination, and why ``standalone`` stays the default
--------------------------------------------------------
Rung B is a NEGATIVE result. On the same three binders, the cognate held
(ipTM 0.953 -> 0.948) while the worst decoy by the ESM prior climbed
0.619 -> 0.801, collapsing the cognate-to-best-decoy margin 0.234 ->
0.147 (37%) and inverting the two decoys' rank. Not a thin-MSA artifact:
~17.5-18.5k UniRef and ~2.4-2.8k paired sequences per design, against a
Rung A archive with no MSA files at all. So ``msa_server`` costs ~3x the
runtime and, here, bought worse separation.

WHY is a HYPOTHESIS, not a measurement: all three binders are
trastuzumab-framework scFvs, so a paired MSA may be scoring the shared
framework rather than the designed interface. Testing that needs a
non-antibody binder and was not run. n=1 per condition — a feasibility
gate, not a statistical claim, and it does not license a general claim
about natural or near-native sequences, which remain untested here. Full
write-up: docs/VALIDATION-LOG.md under "## Boltz-2".

The Modal pipeline lives in ``tools/boltz2/modal_app.py`` and the
subprocess body in ``tools/boltz2/run_pipeline.py``. Per-design PDBs are
streamed back to the hub via presigned PUT URLs (partial-results
contract) and surface live on the job detail page as each fold completes.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from shared.storage import _output_object_path
from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Bounds. Enforced HERE only: ``run_pipeline.py::main`` defends the tier value
# and the no-binders case and nothing else, so a direct ``modal run`` is bound
# by none of these.
# ---------------------------------------------------------------------------

BINDER_LEN_MIN = 20
BINDER_LEN_MAX = 400
MAX_BINDERS = 50
# msa_server gets its own ceiling, below MAX_BINDERS, because the two presets
# fold at rates ~3x apart against ONE shared 60 min Modal function timeout
# (``_MAX_SESSION_S`` in ``tools/boltz2/modal_app.py``). Extrapolating the
# per-design rates measured above: 50 standalone binders are 81.9 + 49 * 69 =
# ~3463 s, ~4% under that ceiling, while 50 msa_server binders are 50 * 214 =
# ~10700 s, ~3x over, crossing 3600 s at the 17th. Hence 16, which extrapolates
# to 16 * 214 = ~3424 s, ~5% of headroom.
#
# Chunked folding has since cut the standalone side of that to ~1000-1525 s
# (batching note in this file's docstring), so ~4% is the headroom 50
# standalone binders had BEFORE batching and is no longer what 50 is pressed
# against. The msa_server arithmetic is untouched, because that preset is not
# batched: 16 still rests on 214 s/design. Neither cap moves until a prod run
# measures a chunked rate.
#
# Both totals are EXTRAPOLATIONS from three folds at 242-246 aa, not measured
# 50-binder runs, and a longer binder folds slower. So 16 refuses the batch
# sizes the arithmetic says cannot finish; it does not certify that 16 always
# will. What it removes is the SILENT part: before this cap a user could submit
# 50 on msa_server, pay for an hour of A100 time and receive ~16 designs, with
# nothing in the form, the validator or the estimate having said so.
#
# Capped here rather than by raising ``_MAX_SESSION_S``, on cost as much as on
# evidence: that constant lives in ``modal_app.py``, which the deploy trigger
# in ``.github/workflows/deploy-modal.yml`` does NOT exclude (this file and
# ``meta.py`` it does), and sizing it honestly needs a 50-record measurement
# that still does not exist. An overrun stays survivable either way, which is
# why this is a product cap and not a data-loss fix: each design is PUT to its
# own presigned URL as it is collected, from inside the per-design loop in
# ``run_pipeline.py::main``, so a timeout costs the tail, not the run. On
# standalone that tail now rounds up to a chunk: a design is collected only
# after its chunk's ``boltz predict`` exits, so a timeout mid-chunk loses
# every design in it rather than one.
#
# Enforced in ``validate`` below and pinned by
# ``tests/test_boltz2_smoke.py::TestPresetBinderCap``.
MAX_BINDERS_BY_PRESET = {"msa_server": 16}
ANTIGEN_CHAIN_MAX = 4
CANONICAL_AA = set("ACDEFGHIKLMNPQRSTVWYX")
# A binder's name becomes a file name, and ``validate`` refuses a name that
# cannot safely be one: it contains '/' or NUL, it starts with '.', or it is
# longer than this many bytes.
#
# WHICH file name has changed since these rules were written (#338). They were
# the fix for a crash: ``main`` wrote each design's input to
# ``d_{i:03d}/{name}.yaml`` from the raw name, so a '/' pointed into a folder
# that did not exist and raised FileNotFoundError after earlier designs had
# already folded and uploaded. Chunked folding took the name out of that path
# — ``main`` now writes ``run_pipeline.py::_record_id(i).yaml``, an index — so
# the name no longer reaches boltz, boltz's output names, or
# ``collect_outputs``'s glob, and that crash is gone by construction rather
# than by this check.
#
# What the name still becomes is the storage key ``{name}_complex.pdb``, via
# ``shared/storage.py::_output_object_path``, which keeps only the basename
# and runs Werkzeug's ``secure_filename`` over it. That handles the first two
# rules and not the third: '4D5/trastuzumab' is stored as 'trastuzumab',
# '.hidden' as 'hidden', and a 201-byte name is stored at 201 bytes, because
# ``secure_filename`` does not truncate. So the byte cap is the only thing
# bounding the key, and the other two rules refuse a rename rather than
# prevent a break.
#
# They are worth keeping as a refusal, at submit time, because the rename is
# silent: the user gets back an object named after something they did not
# type. What the cap does NOT do is cover every path — ``blueprints/jobs.py::
# _spawn_refold_job`` builds ``binder_sequences`` from an upstream candidate's
# header and bypasses ``validate`` by design, so a name arriving that way is
# normalised but not bounded. The collision two names can normalise into is
# refused separately, a few lines into ``validate`` (#333).
#
# These rules are narrower than "anything the sanitiser would change", and
# deliberately: 'anti-HER2 scFv' is accepted here and still stored as
# 'anti-HER2_scFv'. 200 is a conservative bound, not a derived ceiling. It was
# once sized against the longest file name boltz derives from a record's id,
# back when that id was the binder name — a derivation #338 got wrong and #339
# corrects. On this branch the id is an index, so no boltz file name carries
# the name at all, and the storage key above is the only thing left to bound.
# Pinned by ``tests/test_boltz2_smoke.py::TestBinderNameIsAFileName``.
BINDER_NAME_MAX_BYTES = 200


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
    max_binders = MAX_BINDERS_BY_PRESET.get(preset, MAX_BINDERS)
    if len(binders) > max_binders:
        msg = (
            f"Max {max_binders} binder sequences per run on this preset "
            f"(received {len(binders)})."
        )
        if max_binders < MAX_BINDERS:
            msg += (
                " This preset folds ~3x slower, so a batch that size would "
                "run past the 60-minute ceiling and the tail would be cut "
                "off. Split it into smaller runs, or use the single-sequence "
                f"preset, which takes up to {MAX_BINDERS}."
            )
        return None, msg

    # run_pipeline.py::main uploads each design under f"{name}_complex.pdb",
    # and the upload-URL endpoint (webhooks/uploads.py) mints each key's URL
    # for the storage path shared/storage.py::_output_object_path gives it.
    # That function normalises the key, so "binder 1" and "binder_1" land on
    # one object as surely as two "VHH-12"s do. Pinned by
    # tests/test_boltz2_smoke.py::TestBinderNamesGetTheirOwnObject.
    saved_as: dict[str, str] = {}
    for b in binders:
        name = b["name"]
        seq = b["sequence"]
        # See BINDER_NAME_MAX_BYTES for why each of these is refused.
        if "/" in name or "\0" in name or name.startswith("."):
            return None, (
                f"Binder {name!r}: a binder's name becomes a file name, so it "
                f"cannot contain '/' or a NUL byte, or start with '.'. Rename it."
            )
        if len(name.encode("utf-8")) > BINDER_NAME_MAX_BYTES:
            return None, (
                f"Binder {name!r}: the name is "
                f"{len(name.encode('utf-8'))} bytes — max "
                f"{BINDER_NAME_MAX_BYTES}. Shorten it."
            )
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
        fname = _output_object_path("", "", f"{name}_complex.pdb").rsplit("/", 1)[-1]
        if fname in saved_as:
            other = saved_as[fname]
            if other == name:
                return None, (
                    f"Two binders are named {name!r}. Each result is saved "
                    f"under its binder's name, so only one of the two could "
                    f"be kept. Rename one."
                )
            return None, (
                f"Binders {other!r} and {name!r} would both be saved as "
                f"{fname!r}, so only one of the two results could be kept. "
                f"Rename one."
            )
        saved_as[fname] = name

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
        "Paste a designed binder, upload the target it should hit, and "
        "get back the predicted complex plus a 0-to-1 confidence score "
        "for the contact between them. Just over a minute per design in "
        "single-sequence mode."
    ),
    presets=(
        Preset(
            slug="standalone",
            label="Single-sequence (fast)",
            description=(
                "YAML ``msa: empty`` per chain. The right choice for "
                "designed sequences (MPNN, RFantibody, BindCraft, "
                "BoltzGen, RFdiffusion, PXDesign outputs) where no "
                "informative MSA exists. ~69 s/design on A100-40GB, "
                "measured on 242-246 aa binders against a 107 aa antigen."
            ),
            requires_pdb=True,
        ),
        Preset(
            slug="msa_server",
            label="With MSA (slower, natural sequences)",
            description=(
                "Boltz fetches MSAs from the public ColabFold MMseqs2 "
                "endpoint at runtime. Intended for natural / near-native "
                "sequences; ~3.6 min/design including MSA fetch, "
                "measured. On the one head-to-head we have run it "
                "separated a designed binder from decoys WORSE than the "
                "single-sequence default, so prefer that unless you know "
                "your sequence has natural relatives."
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
