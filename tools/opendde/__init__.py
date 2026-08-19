"""OpenDDE — all-atom co-folding (protein + DNA + RNA + ligand).

Modal app: ``ranomics-opendde-prod``. GPU: H100. Atomic primitive (a single
Modal job, boltz2 shape) — NOT a compute campaign.

Ion entities are part of OpenDDE's schema but are BLOCKED by this adapter: the
v1 preview model cannot featurize them (see ION_UNSUPPORTED_MSG). Protein, DNA,
RNA and ligand all fold cleanly (verified at the O-1/O-2/O-3 + isolation canaries).

OpenDDE (Aureka AI Research, Apache-2.0, preview released 2026-07-03) is an
AlphaFold3-class foundation model that co-folds an arbitrary mix of biomolecular
entities specified in one JSON. It is the multi-modal differentiator versus the
protein-only Boltz-2 tool in the catalog.

Two presets (the checkpoint is the compute boundary):

- ``general`` — the general ``opendde.pt`` checkpoint. Any entity mix.
- ``abag``    — the antibody-antigen-specialised ``opendde_abag.pt`` checkpoint,
  selected at run time via ``--load_checkpoint_path``.

Input is inline (``requires_pdb=False``) — no upload, no hotspot picker. The user
either fills five guided textareas (one per entity type) that this adapter
assembles into the OpenDDE JSON, or pastes an exact OpenDDE spec into a single
JSON escape hatch. Both paths pass through the SAME bound checks — the escape
hatch is not a way around the cost / size ceilings.

Verified against the upstream repo (github.com/aurekaresearch/OpenDDE,
docs/infer_json_format.md) and HF card at build:

- Schema is a top-level list of ``{name, modelSeeds, sequences:[...]}``; each
  sequence entity is exactly one of ``proteinChain | dnaSequence | rnaSequence |
  ligand | ion``.
- ``proteinChain``/``dnaSequence``/``rnaSequence`` are objects with
  ``sequence`` + ``count`` + ``id`` (``id`` is an ARRAY of chain letters whose
  length must equal ``count``).
- ``ligand`` / ``ion`` are OBJECTS whose inner key repeats the entity name, plus
  ``count`` (+ optional ``id``): ``{"ligand": {"ligand": "CCD_ATP", "count": 1}}``,
  ``{"ion": {"ion": "MG", "count": 1}}``. The ligand code is ``"CCD_ATP"`` /
  ``"CCD_NAG_BMA_BGC"`` / ``"FILE_x.sdf"`` / a bare SMILES (no ``SMILES:`` prefix);
  the ion code is a bare CCD component name (``"MG"``, not ``"CCD_MG"``). A bare
  string here crashes OpenDDE's ``json_parser.build_ligand`` (verified O-2).

Cost model: this is a single H100 container physically capped at
``_MAX_SESSION_S`` (see modal_app.py), so the wallet holds a fixed
container-budget estimate (``scaling_param=None`` in wallet_estimates.py) that
cannot under-hold. ``n_designs_total = n_seeds * sample`` is stamped for the job
record and drives how many predictions are packed into that fixed budget; it does
not move the hold. The ``MAX_TOKENS`` / ``MAX_CHAINS`` gates are the OOM/cost
backstop (OpenDDE documents no inference size cap), calibrated at the O-1/O-2
benchmark.
"""

from __future__ import annotations

import json
import string
from typing import Any, Mapping, Optional

from tools.base import Preset, ToolAdapter, register


# ---------------------------------------------------------------------------
# Bounds. Self-imposed — OpenDDE documents no inference size / sampler caps, so
# these are our OOM + wallet backstop, refined from the O-1/O-2 benchmark.
# ---------------------------------------------------------------------------

PRESET_SLUGS = ("general", "abag")
SPEC_MODES = ("guided", "json")

# Sampler knobs. Ranges bound the per-design compute so a single container's
# worst case still fits inside _MAX_SESSION_S. Defaults mirror the upstream
# README example (sample 1 / step 200 / cycle 10).
SAMPLE_MIN, SAMPLE_MAX, SAMPLE_DEFAULT = 1, 4, 1
STEP_MIN, STEP_MAX, STEP_DEFAULT = 1, 400, 200
CYCLE_MIN, CYCLE_MAX, CYCLE_DEFAULT = 1, 20, 10
N_SEEDS_MIN, N_SEEDS_MAX, N_SEEDS_DEFAULT = 1, 4, 1
SEED_MIN, SEED_MAX, SEED_DEFAULT = 0, 2**31 - 1, 1

# Size ceilings (the OOM / cost proxy — scaling is size-blind).
MAX_TOKENS = 2000          # sum of polymer residues + per-ligand/ion allowance
MAX_CHAINS = 20            # total entity instances (polymer chains + ligands + ions)
MAX_SEQ_LEN = 1600         # single-chain sanity bound (well under MAX_TOKENS)
LIGAND_TOKEN_COST = 24     # heavy-atom allowance per ligand (conservative)
ION_TOKEN_COST = 1

# Alphabets. X is the unknown/mask token and is legal everywhere.
PROTEIN_AA = set("ACDEFGHIKLMNPQRSTVWYX")
DNA_NT = set("ATGCNX")
RNA_NT = set("AUGCNX")

# The only sequence-entity keys OpenDDE accepts. A pasted spec whose entity uses
# any other key (e.g. an AlphaFold3 ``protein`` / ``rna`` shorthand) is rejected
# — the escape hatch validates against OpenDDE's real schema, not just any JSON.
POLYMER_KEYS = {
    "proteinChain": PROTEIN_AA,
    "dnaSequence": DNA_NT,
    "rnaSequence": RNA_NT,
}
# ligand + ion. Named for what they are NOT (polymers) — each is an OBJECT whose
# inner key repeats the entity name, NOT a bare string (see _validate_sequences).
NONPOLYMER_KEYS = {"ligand", "ion"}
ENTITY_KEYS = set(POLYMER_KEYS) | NONPOLYMER_KEYS

# The OpenDDE v1 preview cannot featurize ion entities: its template featurizer's
# map_to_standard raises on any ion (verified at the O-2 / iso canaries, on both
# checkpoints, even for a lone protein+ion with --use_template false). Ions are
# still a RECOGNISED key (so a paste gets this precise message, not "unknown
# type"), but validate() rejects them PRE-GPU so a user is never charged for a
# guaranteed failure. Protein / DNA / RNA / ligand all fold cleanly. Revisit when
# upstream fixes ion featurization (or if we later enable the template path).
ION_UNSUPPORTED_MSG = (
    "Ion entities are not supported yet — the OpenDDE v1 preview model cannot "
    "featurize ions. Remove the ion(s); protein, DNA, RNA, and ligand entities "
    "are supported."
)

JOB_NAME = "opendde_job"   # fixed, filesystem-safe; the pipeline globs recursively
MODEL_NAME = "opendde_v1"  # -n value (the architecture, not a job id)

# Chain-id pool for auto-assignment in guided mode.
_CHAIN_ID_POOL = list(string.ascii_uppercase) + list(string.ascii_lowercase)


# ---------------------------------------------------------------------------
# Scalar parsers (each returns (value, error) like the esmfold2 adapter)
# ---------------------------------------------------------------------------


def _parse_int(raw: str, name: str, lo: int, hi: int, default: int) -> tuple[Optional[int], Optional[str]]:
    raw = (raw or "").strip()
    if not raw:
        return default, None
    try:
        val = int(raw)
    except ValueError:
        return None, f"{name} must be an integer; got {raw!r}."
    if val < lo or val > hi:
        return None, f"{name} must be between {lo} and {hi}."
    return val, None


# ---------------------------------------------------------------------------
# Guided-mode entity parsing
# ---------------------------------------------------------------------------


def _parse_fasta_records(block: str) -> list[tuple[Optional[str], str]]:
    """Parse a FASTA-ish textarea into (id, sequence) records.

    ``>id`` headers are optional: a block with no ``>`` is one unnamed record
    (id ``None``). Whitespace inside a sequence is stripped.
    """
    block = (block or "").strip()
    if not block:
        return []
    records: list[tuple[Optional[str], str]] = []
    cur_id: Optional[str] = None
    cur_seq: list[str] = []
    started = False
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            if started:
                records.append((cur_id, "".join(cur_seq)))
            cur_id = line[1:].strip() or None
            cur_seq = []
            started = True
        else:
            cur_seq.append(line.replace(" ", ""))
            if not started:
                started = True
    if started:
        records.append((cur_id, "".join(cur_seq)))
    return [(cid, seq) for cid, seq in records if seq]


def _next_chain_id(used: set[str]) -> Optional[str]:
    for cid in _CHAIN_ID_POOL:
        if cid not in used:
            used.add(cid)
            return cid
    return None


def _assemble_guided(form: Mapping[str, Any]) -> tuple[Optional[list], Optional[str]]:
    """Build the OpenDDE ``sequences`` list from the five guided textareas."""
    sequences: list[dict] = []
    used_ids: set[str] = set()

    for field, entity_key in (
        ("proteins", "proteinChain"),
        ("dna", "dnaSequence"),
        ("rna", "rnaSequence"),
    ):
        alphabet = POLYMER_KEYS[entity_key]
        for rec_id, seq in _parse_fasta_records(form.get(field) or ""):
            seq = seq.upper()
            if len(seq) > MAX_SEQ_LEN:
                return None, f"A {field} chain is too long ({len(seq)} > {MAX_SEQ_LEN})."
            bad = set(seq) - alphabet
            if bad:
                return None, (
                    f"A {field} sequence has invalid residues {sorted(bad)}; "
                    f"allowed: {''.join(sorted(alphabet))}."
                )
            # Respect a user-provided single-token id if unique, else auto-assign.
            cid = (rec_id or "").strip()
            if cid and (len(cid) > 4 or cid in used_ids):
                cid = ""
            if not cid:
                cid = _next_chain_id(used_ids)
                if cid is None:
                    return None, f"Too many chains (> {len(_CHAIN_ID_POOL)})."
            else:
                used_ids.add(cid)
            sequences.append(
                {entity_key: {"sequence": seq, "count": 1, "id": [cid]}}
            )

    # Ligands — one per line: CCD_x / CCD_x_y_z / FILE_x.sdf / bare SMILES. OpenDDE
    # wraps a ligand as an OBJECT {"ligand": {"ligand": <code>, "count", "id"}},
    # NOT a bare string — a bare string crashes its json_parser.build_ligand.
    for line in (form.get("ligands") or "").splitlines():
        token = line.strip()
        if not token:
            continue
        if len(token) > 512:
            return None, "A ligand entry is too long (> 512 chars)."
        cid = _next_chain_id(used_ids)
        if cid is None:
            return None, f"Too many chains (> {len(_CHAIN_ID_POOL)})."
        sequences.append({"ligand": {"ligand": token, "count": 1, "id": [cid]}})

    # Ions are blocked pre-GPU (OpenDDE v1 preview cannot featurize them).
    if (form.get("ions") or "").strip():
        return None, ION_UNSUPPORTED_MSG

    if not sequences:
        return None, "Add at least one entity (protein, DNA, RNA, or ligand)."
    return sequences, None


# ---------------------------------------------------------------------------
# JSON escape-hatch validation — re-apply EVERY bound
# ---------------------------------------------------------------------------


def _validate_sequences(sequences: Any) -> tuple[Optional[list], Optional[str]]:
    """Validate a ``sequences`` list against OpenDDE's real schema + our bounds.

    Returns the (normalised) sequences list or an error. Rejects any entity that
    is not exactly one recognised OpenDDE entity type — this is what stops an
    AlphaFold3-shaped paste (or any foreign schema) from slipping through the
    escape hatch.
    """
    if not isinstance(sequences, list) or not sequences:
        return None, "'sequences' must be a non-empty list."
    out: list[dict] = []
    for i, entity in enumerate(sequences):
        if not isinstance(entity, dict) or len(entity) != 1:
            return None, f"Entity {i} must be an object with exactly one key."
        (key, val), = entity.items()
        if key not in ENTITY_KEYS:
            return None, (
                f"Entity {i} uses unknown type {key!r}; allowed: "
                f"{', '.join(sorted(ENTITY_KEYS))}."
            )
        if key == "ion":
            return None, ION_UNSUPPORTED_MSG
        if key in POLYMER_KEYS:
            if not isinstance(val, dict) or "sequence" not in val:
                return None, f"Entity {i} ({key}) needs a 'sequence'."
            seq = str(val.get("sequence") or "").strip().upper()
            if not seq:
                return None, f"Entity {i} ({key}) has an empty sequence."
            if len(seq) > MAX_SEQ_LEN:
                return None, f"Entity {i} ({key}) sequence too long ({len(seq)} > {MAX_SEQ_LEN})."
            bad = set(seq) - POLYMER_KEYS[key]
            if bad:
                return None, f"Entity {i} ({key}) has invalid residues {sorted(bad)}."
            count = val.get("count", 1)
            if not isinstance(count, int) or count < 1 or count > MAX_CHAINS:
                return None, f"Entity {i} ({key}) has an invalid 'count'."
            ids = val.get("id")
            if ids is not None:
                if not isinstance(ids, list) or len(ids) != count or not all(
                    isinstance(x, str) and x for x in ids
                ):
                    return None, (
                        f"Entity {i} ({key}) 'id' must be a list of {count} chain "
                        f"letters (one per count)."
                    )
            out.append({key: {"sequence": seq, "count": count, **({"id": ids} if ids else {})}})
        else:  # ligand / ion — OBJECT {"<key>": {"<key>": <code>, count, id}}.
               # Re-apply the SAME bounds as guided mode so the escape hatch is not
               # a way around the size / format checks (mirrors _assemble_guided).
            if not isinstance(val, dict) or key not in val:
                return None, (
                    f"Entity {i} ({key}) must be an object with a {key!r} field, "
                    f'e.g. {{"{key}": {{"{key}": "…", "count": 1}}}}.'
                )
            code = val.get(key)
            if not isinstance(code, str) or not code.strip():
                return None, f"Entity {i} ({key}) {key!r} must be a non-empty string."
            code = code.strip()
            count = val.get("count", 1)
            if not isinstance(count, int) or count < 1 or count > MAX_CHAINS:
                return None, f"Entity {i} ({key}) has an invalid 'count'."
            ids = val.get("id")
            if ids is not None:
                if not isinstance(ids, list) or len(ids) != count or not all(
                    isinstance(x, str) and x for x in ids
                ):
                    return None, (
                        f"Entity {i} ({key}) 'id' must be a list of {count} chain "
                        f"letters (one per count)."
                    )
            if key == "ligand":
                if len(code) > 512:
                    return None, f"Entity {i} (ligand) is too long (> 512 chars)."
                norm = code
            else:  # ion — CCD element name (optional oxidation-state digit)
                norm = code.upper()
                if not (norm[0].isalpha() and norm.isalnum()) or len(norm) > 4:
                    return None, f"Entity {i} (ion) must be a CCD element name like MG."
            out.append({key: {key: norm, "count": count, **({"id": ids} if ids else {})}})
    return out, None


def _parse_json_spec(raw: str) -> tuple[Optional[list], Optional[str]]:
    """Parse + validate a pasted OpenDDE spec into a normalised ``sequences`` list."""
    raw = (raw or "").strip()
    if not raw:
        return None, "Paste an OpenDDE JSON spec, or switch to guided mode."
    # Bound the raw size before parsing — a huge or deeply-nested paste must fail
    # as a clean validation error, never as an uncaught RecursionError (which is
    # NOT a JSONDecodeError) bubbling up to a 500.
    if len(raw) > 200_000:
        return None, "Spec is too large (max 200 KB)."
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        return None, f"Spec is not valid JSON: {exc}."
    # Accept a bare job object or the top-level list form; require exactly one job
    # so the cost model (n_designs_total = seeds * sample) stays unambiguous.
    if isinstance(parsed, dict):
        job = parsed
    elif isinstance(parsed, list):
        if len(parsed) != 1:
            return None, "Submit exactly one job per run (a single-element list)."
        job = parsed[0]
    else:
        return None, "Spec must be a job object or a one-element list."
    if not isinstance(job, dict):
        return None, "The job must be an object."
    return _validate_sequences(job.get("sequences"))


# ---------------------------------------------------------------------------
# Size accounting (the OOM / cost gate)
# ---------------------------------------------------------------------------


def _count_tokens_and_chains(sequences: list) -> tuple[int, int]:
    tokens = 0
    chains = 0
    for entity in sequences:
        (key, val), = entity.items()
        if key in POLYMER_KEYS:
            count = int(val.get("count", 1))
            tokens += len(str(val.get("sequence", ""))) * count
            chains += count
        elif key == "ligand":
            count = int(val.get("count", 1)) if isinstance(val, dict) else 1
            tokens += LIGAND_TOKEN_COST * count
            chains += count
        else:  # ion
            count = int(val.get("count", 1)) if isinstance(val, dict) else 1
            tokens += ION_TOKEN_COST * count
            chains += count
    return tokens, chains


# ---------------------------------------------------------------------------
# Adapter entrypoints
# ---------------------------------------------------------------------------


def validate(
    form: Mapping[str, Any], files: Mapping[str, Any]
) -> tuple[Optional[dict], Optional[str]]:
    """Coerce form fields into the OpenDDE job_spec shape.

    No PDB upload — every input is inline. Both the guided textareas and the
    JSON escape hatch converge on a validated ``sequences`` list that then passes
    the same MAX_TOKENS / MAX_CHAINS gate.
    """
    preset = (form.get("preset") or "general").strip()
    if preset not in PRESET_SLUGS:
        return None, "Pick a checkpoint (general or abag)."

    spec_mode = (form.get("spec_mode") or "guided").strip()
    if spec_mode not in SPEC_MODES:
        return None, "Pick an input mode (guided or json)."

    # Sampler knobs (apply in both modes).
    sample, err = _parse_int(form.get("sample"), "Samples", SAMPLE_MIN, SAMPLE_MAX, SAMPLE_DEFAULT)
    if err:
        return None, err
    step, err = _parse_int(form.get("step"), "Diffusion steps", STEP_MIN, STEP_MAX, STEP_DEFAULT)
    if err:
        return None, err
    cycle, err = _parse_int(form.get("cycle"), "Recycles", CYCLE_MIN, CYCLE_MAX, CYCLE_DEFAULT)
    if err:
        return None, err
    seed, err = _parse_int(form.get("seed"), "Starting seed", SEED_MIN, SEED_MAX, SEED_DEFAULT)
    if err:
        return None, err
    n_seeds, err = _parse_int(form.get("n_seeds"), "Seeds", N_SEEDS_MIN, N_SEEDS_MAX, N_SEEDS_DEFAULT)
    if err:
        return None, err

    # Build the entity list.
    if spec_mode == "guided":
        sequences, err = _assemble_guided(form)
    else:
        sequences, err = _parse_json_spec(form.get("spec_json") or "")
    if err:
        return None, err

    tokens, chains = _count_tokens_and_chains(sequences)
    if chains > MAX_CHAINS:
        return None, f"Too many chains ({chains} > {MAX_CHAINS}). Reduce the entity count."
    if tokens > MAX_TOKENS:
        return None, (
            f"Complex is too large ({tokens} tokens > {MAX_TOKENS}). Trim sequences "
            f"or split the job."
        )

    # Seeds are controlled by the Starting-seed + Seeds fields in BOTH modes; any
    # modelSeeds in a pasted spec is overridden so the cost count is deterministic.
    model_seeds = list(range(seed, seed + n_seeds))
    n_designs_total = n_seeds * sample

    spec = [{"name": JOB_NAME, "modelSeeds": model_seeds, "sequences": sequences}]

    entity_summary = _summarise(sequences)
    return (
        {
            "preset": preset,
            "spec_mode": spec_mode,
            "spec": spec,
            "sample": sample,
            "step": step,
            "cycle": cycle,
            "seed": seed,
            "n_seeds": n_seeds,
            "n_tokens": tokens,
            "n_chains": chains,
            "target": entity_summary,
            "parameters": {"n_designs_total": n_designs_total},
        },
        None,
    )


def _summarise(sequences: list) -> str:
    counts: dict[str, int] = {}
    for entity in sequences:
        (key, _), = entity.items()
        counts[key] = counts.get(key, 0) + 1
    label = {
        "proteinChain": "protein",
        "dnaSequence": "DNA",
        "rnaSequence": "RNA",
        "ligand": "ligand",
        "ion": "ion",
    }
    bits = [f"{n} {label.get(k, k)}{'s' if n != 1 else ''}" for k, n in counts.items()]
    return " + ".join(bits) if bits else "complex"


def build_payload(inputs: dict, presigned_url: str) -> dict:
    """Build the OpenDDE job_spec for the Modal pipeline.

    ``presigned_url`` is unused — OpenDDE inputs are inline, so the assembled
    JSON spec travels inside the job_spec itself.
    """
    return {
        "preset": inputs["preset"],
        "spec": inputs["spec"],
        "sample": inputs["sample"],
        "step": inputs["step"],
        "cycle": inputs["cycle"],
        "n_designs_total": inputs["parameters"]["n_designs_total"],
        "parameters": inputs["parameters"],
    }


adapter = ToolAdapter(
    slug="opendde",
    label="OpenDDE co-folding",
    blurb=(
        "Describe any mix of protein, DNA, RNA and small molecules in "
        "one spec and get the whole complex folded together, every atom "
        "modelled. The multi-molecule counterpart to the protein-only "
        "Boltz-2 tool."
    ),
    presets=(
        Preset(
            slug="general",
            label="General co-folding",
            description=(
                "The general OpenDDE checkpoint. Co-fold any mix of protein, "
                "DNA, RNA, and ligand entities. Choose this unless your target "
                "is specifically an antibody-antigen pair."
            ),
            requires_pdb=False,
            long_running=True,
        ),
        Preset(
            slug="abag",
            label="Antibody-antigen (ABAG)",
            description=(
                "The antibody-antigen specialised checkpoint. Tuned for "
                "predicting antibody or nanobody complexes with their antigen."
            ),
            requires_pdb=False,
            long_running=True,
        ),
    ),
    validate=validate,
    build_payload=build_payload,
    requires_pdb=False,
    form_template="tools/opendde_form.html",
    results_partial="tools/opendde_results.html",
)

register(adapter)
