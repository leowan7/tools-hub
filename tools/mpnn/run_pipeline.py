"""Modal entrypoint for D1 — ProteinMPNN standalone.

Reads job configuration from the ``JOB_PAYLOAD`` env var (same RunPod-parity
shape the Kendrew pipelines use), runs ProteinMPNN, writes the result to
``/tmp/smoke_results.json``. For smoke / standalone tiers the wrapper
returns this file inline via the Modal function return value — see
``tools/mpnn/modal_app.py``.

Contract (per docs/ATOMIC-TOOLS.md):

- ``preflight()`` is called first and must complete in <= 60 s. On any
  failure it writes ``{"status":"FAILED","error":{...}}`` to
  ``/tmp/smoke_results.json`` and ``sys.exit(1)`` so the build-time
  Layer-1 checks are not duplicated at runtime.
- ``run()`` executes ``protein_mpnn_run.py`` on the target PDB, then
  parses the FASTA output. Stub rejection: fails if every returned
  sequence is identical (MPNN's silent-stub failure mode).

Environment variables (set by ``tools/mpnn/modal_app.py`` from the
payload):

    JOB_PAYLOAD     JSON string with job_spec + input_presigned_url + tier
    WEBHOOK_URL     URL to POST results to (ignored on smoke tier)
    JOB_ID          tool_jobs row id (used for log prefixing)
    JOB_TOKEN       Job-specific auth token for the webhook
    JOB_TIER        ``smoke`` | ``standalone``

Output shape (``/tmp/smoke_results.json``)::

    {
      "status": "COMPLETED",
      "tier": "smoke",
      "sequences": [
        {"seq": "MDPLR...", "score": 1.23, "recovery": 0.52, "chain": "A"},
        ...
      ],
      "runtime_seconds": 47,
      "provider_job_id": "<job_id>"
    }

Raw capture: immediately before the work dir is torn down, the COMPLETE
tree is tarred to ``/tmp/raw_archive.tgz``; ``tools/mpnn/modal_app.py``
parks that file on the ``ranomics-mpnn-raw`` Volume. Nothing in here
decides which fields are worth keeping — ``parse_mpnn_output`` reads a
score and a recovery off each FASTA header and drops the rest of the
MPNN tree with the container. Filtering and ranking happen locally,
afterwards, where re-parsing is free and re-running is not.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mpnn_pipeline")


PROTEINMPNN_DIR = os.environ.get("PROTEINMPNN_DIR", "/opt/ProteinMPNN")
PROTEINMPNN_WEIGHTS = os.environ.get(
    "PROTEINMPNN_WEIGHTS", f"{PROTEINMPNN_DIR}/vanilla_model_weights"
)
PROTEINMPNN_SCRIPT = f"{PROTEINMPNN_DIR}/protein_mpnn_run.py"
SMOKE_TARGET_PDB = "/opt/smoke_target.pdb"
SMOKE_RESULTS_PATH = "/tmp/smoke_results.json"
# Fixed path the Modal wrapper collects the raw tree from. MUST sit outside the
# work dir it archives, or the tar ends up inside its own source — see
# _archive_raw, which re-checks rather than trusting this constant.
RAW_ARCHIVE_PATH = "/tmp/raw_archive.tgz"

# Bounds enforced on the two numeric job_spec params. Mirrored from the
# tools-hub adapter validate() but re-checked here because the pipeline
# may be invoked directly (e.g. ``modal run`` for staging validation).
# MUST stay in sync with tools/mpnn/__init__.py NUM_SEQ_MAX: this clamp runs
# on every prod job, so a value below the adapter cap silently truncates the
# user's requested sequence count (the pre-2026-07 bug: form promised 200,
# prod delivered 20).
NUM_SEQ_MIN = 1
NUM_SEQ_MAX = 1000
TEMP_MIN = 0.01
TEMP_MAX = 1.0

# Name of the fixed-positions file handed to ProteinMPNN. One JSON object on one
# line: {"<pdb_stem>": {"<chain>": [1-indexed positions to KEEP FIXED]}}, with an
# entry for EVERY designed chain because upstream subscripts it bare.
FIXED_POSITIONS_JSONL = "fixed_positions.jsonl"

# Free POSITIONS (per chain, not per sequence-comparison) needed before low output
# diversity counts as evidence that MPNN misbehaved. Below this, identical or
# all-native samples are the expected result of a heavily constrained run rather
# than a stub, and failing would bill a point-mutation scan and then reject its
# correct answer. Used by both reject_stub and verify_fixed_positions, which face
# the same question from opposite ends.
MIN_FREE_TO_JUDGE_DIVERSITY = 10


def _chain_ca_residues(pdb: Path) -> dict[str, dict[int, set[str]]]:
    """Observed CA residues per chain: ``{chain: {resSeq: {iCode, ...}}}``.

    Text parse rather than Bio.PDB: a fixed-column read has no version surface.
    Encoding mirrors upstream's ``line.decode("utf-8", "ignore")`` so a stray byte
    is skipped rather than killing the run with an uncaught UnicodeDecodeError.
    """
    obs: dict[str, dict[int, set[str]]] = {}
    for line in pdb.read_text(encoding="utf-8", errors="ignore").splitlines():
        # Short line: a truncated record would otherwise IndexError on line[26].
        # Atom name compared the way upstream does it (line[12:16].strip()), so a
        # left-aligned "CA  " is counted here exactly as it is there.
        if len(line) < 27 or line[12:16].strip() != "CA":
            continue
        # " CA " in a HETATM record is usually a CALCIUM ION, not an alpha carbon.
        # The one exception is MSE, which upstream rewrites to ATOM/MET and counts
        # (protein_mpnn_utils.parse_PDB_biounits).
        if not (line.startswith("ATOM") or line[:6] == "HETATM" and line[17:20] == "MSE"):
            continue
        # Upstream reads columns 22-27 as ONE token and only calls the last
        # character an insertion code when it is alphabetic
        # (``if resn[-1].isalpha(): resa, resn = resn[-1], int(resn[:-1])-1``).
        # Splitting at a fixed 26 instead would read the 5-wide residue number
        # "   31" as residue 3 with iCode "1" and refuse a perfectly ordinary
        # chain. (Upstream also subtracts 1 from EVERY residue number, iCoded or
        # not. Not reproduced here because a uniform offset cancels out of the
        # min/max/range arithmetic these numbers are used for.)
        token = line[22:27].strip()
        icode = " "
        if token and token[-1].isalpha():
            token, icode = token[:-1], token[-1]
        try:
            resn = int(token)
        except ValueError:
            continue
        # Key on residue identity (resSeq + iCode), never on atom count: a residue
        # with alternate locations emits one " CA " per conformer, and upstream
        # keeps only the first (``if atom not in xyz[resn][resa]``).
        obs.setdefault(line[21], {}).setdefault(resn, set()).add(icode)
    return obs


def _chain_residue_counts(pdb: Path) -> dict[str, int]:
    """Chain lengths AS PROTEINMPNN COUNTS THEM, which is not "residues present".

    ``parse_PDB_biounits`` walks ``range(min_resn, max_resn+1)`` and appends a gap
    token for every residue number absent from the file, so an unresolved loop
    still occupies positions. Counting only observed residues disagrees with
    upstream on any gapped chain -- measured on this repo's own 1jff_raw.pdb:
    412 observed vs 438 counted -- and every 1-indexed position past the gap then
    refers to a different residue than the caller meant.
    """
    counts: dict[str, int] = {}
    for chain, res in _chain_ca_residues(pdb).items():
        counts[chain] = sum(
            len(res[r]) if r in res else 1 for r in range(min(res), max(res) + 1)
        )
    return counts


def _designed_chains(chains_to_design: str) -> set[str]:
    """Chain ids MPNN will actually design.

    Mirrors upstream EXACTLY: protein_mpnn_run.py does
    ``args.pdb_path_chains.split()`` -- whitespace only. Accepting commas here
    would be worse than rejecting them: "A,B" would validate happily against
    chains A and B, and upstream would then die on ``b['seq_chain_A,B']``
    (KeyError) after the job was submitted, blaming its own parser rather than
    the request. Refusing at pre-flight names the real problem for free.
    """
    return set((chains_to_design or "").split())


def _header_chain_list(header: str, key: str) -> list[str] | None:
    """Parse ``designed_chains=['A', 'C']`` off an MPNN native FASTA header.

    Three outcomes, kept distinct because they mean different things:
    None when the field is absent or its contents are not parseable (unknown MPNN
    build -- no sound mapping), and [] only when the field is genuinely empty
    (MPNN designed nothing, which is a hard failure the caller reports as such).
    """
    match = re.search(rf"{re.escape(key)}\s*=\s*\[([^\]]*)\]", header)
    if match is None:
        return None
    inner = match.group(1).strip()
    names = re.findall(r"['\"]([^'\"]+)['\"]", inner)
    if inner and not names:
        return None  # non-empty but unquoted/unknown shape: do not read as "empty"
    return names


def normalise_fixed_positions(
    job_spec: dict[str, Any],
    pdb: Path,
    chains_to_design: str,
) -> dict[str, list[int]]:
    """Validate and normalise ``parameters.fixed_positions``.

    SEMANTICS ARE PROTEINMPNN'S, DELIBERATELY UNCHANGED: the listed positions are
    the ones held FIXED, 1-indexed within their chain. Callers usually hold the
    complement ("the positions I want redesigned") and must invert before calling.
    Mirroring upstream is worth the caller-side inversion — a wrapper that flipped
    the sense would put two opposite conventions in one system, and the failure is
    silent in both directions (freeze everything, or freeze nothing).

    "1-indexed within their chain" is upstream's index into a GAP-FILLED span, not
    a count of residues present, so chains with unresolved residues are refused
    below rather than silently mis-indexed. On a contiguous chain the two coincide
    and position i is simply the i-th residue.

    Returns {} when the caller asked for nothing, which is the pre-existing
    whole-chain-redesign behaviour.
    """
    # Guarded HERE, not in the caller: every route into this function passes a
    # raw job_spec, and a truthy non-dict `parameters` (a string, a list, an int)
    # raises AttributeError — which is neither TypeError nor ValueError, so it
    # escapes every catch, kills the run, and writes no FAILED result at all.
    raw_params = job_spec.get("parameters")
    raw = raw_params.get("fixed_positions") if isinstance(raw_params, dict) else None
    if raw in (None, {}, []):
        return {}
    if not isinstance(raw, dict):
        _fail(
            "preflight",
            "fixed_positions",
            "fixed_positions must be an object mapping chain -> list of "
            f"1-indexed positions, got {type(raw).__name__}",
        )

    residues = _chain_ca_residues(pdb)
    counts = _chain_residue_counts(pdb)
    designed = _designed_chains(chains_to_design)
    out: dict[str, list[int]] = {}
    for chain, positions in raw.items():
        chain = str(chain).strip()
        # Two keys that differ only by whitespace collapse to one entry here, and
        # the second would silently drop the first's positions.
        if chain in out:
            _fail(
                "preflight",
                "fixed_positions",
                f"chain {chain!r} appears more than once in fixed_positions "
                "(keys differing only by surrounding whitespace); merge them",
            )
        if chain not in counts:
            _fail(
                "preflight",
                "fixed_positions",
                f"chain {chain!r} is not in the input PDB (chains: "
                f"{sorted(counts)})",
            )
        # Fixing positions on a chain MPNN was never asked to design is a no-op
        # that reads as a successful freeze. Refuse it rather than honour it.
        if chain not in designed:
            _fail(
                "preflight",
                "fixed_positions",
                f"chain {chain!r} is not among the designed chains "
                f"({sorted(designed)}); fixing positions there does nothing"
                + (
                    " — chains_to_design must be SPACE-separated: ProteinMPNN "
                    "splits it on whitespace only, so a comma yields one token "
                    "that matches no chain at all"
                    if any("," in d for d in designed)
                    else ""
                ),
            )
        if not isinstance(positions, (list, tuple)):
            _fail(
                "preflight",
                "fixed_positions",
                f"fixed_positions[{chain!r}] must be a list, got "
                f"{type(positions).__name__}",
            )
        # Plain ints only, no coercion. int() would turn 3.9 into 3, True into 1
        # and "2" into 2 -- each of which then freezes a residue the caller never
        # named and verifies perfectly, because whatever got frozen IS frozen.
        # (It also keeps float('inf') from raising OverflowError, which is not a
        # ValueError and would escape as an uncaught crash with no FAILED result.)
        bad_type = [p for p in positions if isinstance(p, bool) or not isinstance(p, int)]
        if bad_type:
            _fail(
                "preflight",
                "fixed_positions",
                f"fixed_positions[{chain!r}] must contain plain ints, got "
                f"{[type(p).__name__ for p in bad_type[:4]]}",
            )
        pos = sorted(set(positions))
        n = counts[chain]
        # A gap or an insertion code makes "position i" mean the i-th slot of
        # upstream's gap-filled span rather than the i-th residue of the chain,
        # and a caller counting residues off a sequence would freeze the wrong
        # ones from there onward. Refuse instead of guessing which convention was
        # meant. Whole-chain redesign is unaffected.
        n_present = sum(len(v) for v in residues[chain].values())
        lo, hi = min(residues[chain]), max(residues[chain])
        # Insertion codes shift positions without leaving a gap, so the count
        # check below cannot see them: 1, 2, 2B, 3 is 4 contiguous positions in
        # which position 4 is author residue 3.
        icoded = sorted(r for r, codes in residues[chain].items() if codes != {" "})
        if icoded:
            _fail(
                "preflight",
                "fixed_positions",
                f"chain {chain!r} has insertion codes at residue(s) "
                f"{icoded[:8]}, which occupy ProteinMPNN positions of their own, "
                "so a 1-indexed position stops matching the residue you named "
                "from the first one onward. Renumber the chain sequentially "
                "before fixing positions",
            )
        if n != n_present:
            _fail(
                "preflight",
                "fixed_positions",
                f"chain {chain!r} is not contiguous: residues {lo}-{hi} span {n} "
                f"ProteinMPNN positions but only {n_present} are present. Upstream "
                "counts unresolved residues as positions, so a 1-indexed position "
                "would not land on the residue you named. Renumber the chain "
                "contiguously before fixing positions",
            )
        # An offset chain makes the two plausible conventions differ by exactly
        # lo-1, and nothing in the request says which was meant. The bounds check
        # only catches author numbers LARGER than the chain, so a chain numbered
        # from 20 accepts author number 100 and silently freezes residue 119.
        if lo != 1:
            _fail(
                "preflight",
                "fixed_positions",
                f"chain {chain!r} is numbered {lo}-{hi}, not from 1, so a "
                "ProteinMPNN position and an author residue number differ by "
                f"{lo - 1} and the request does not say which it holds. Author "
                f"residue r is position r-{lo - 1} here (so {lo + 7} -> 8). "
                "Renumber the chain from 1, or subtract the offset yourself",
            )
        bad = [p for p in pos if p < 1 or p > n]
        if bad:
            # An off-by-one caller (0-indexed) lands here instead of silently
            # freezing the wrong residues.
            _fail(
                "preflight",
                "fixed_positions",
                f"fixed_positions[{chain!r}] out of range for a {n}-residue "
                f"chain (1-indexed): {bad[:8]}",
            )
        if len(pos) == n:
            _fail(
                "preflight",
                "fixed_positions",
                f"fixed_positions[{chain!r}] fixes all {n} residues — there is "
                "nothing left to design",
            )
        # Symmetric with the all-fixed case above. An explicit empty list is a
        # whole-chain redesign wearing the shape of a freeze, which is precisely
        # the confusion this whole check exists to prevent.
        if not pos:
            _fail(
                "preflight",
                "fixed_positions",
                f"fixed_positions[{chain!r}] is empty — that requests a full "
                "redesign of the chain. Omit the chain if that is the intent",
            )
        out[chain] = pos
    return out


# ===========================================================================
# Result file writer
# ===========================================================================


def _write_result(payload: dict[str, Any]) -> None:
    """Write the canonical smoke-result JSON. Overwrites any prior file."""
    try:
        with open(SMOKE_RESULTS_PATH, "w") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        # Last-ditch: log to stderr so Modal logs capture the reason. The
        # wrapper's ``read_smoke_results`` will return None and the run
        # will be reported as FAILED via exit_code.
        logger.error("Could not write %s: %s", SMOKE_RESULTS_PATH, exc)


def _fail(bucket: str, check: str, detail: str) -> None:
    """Write a FAILED result and exit 1. Matches the Kendrew shape."""
    logger.error("pipeline FAILED at %s/%s: %s", bucket, check, detail)
    _write_result(
        {
            "status": "FAILED",
            "error": {"bucket": bucket, "check": check, "detail": detail},
            "tier": os.environ.get("JOB_TIER", ""),
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )
    sys.exit(1)


# ===========================================================================
# Preflight
# ===========================================================================


def preflight(payload: dict[str, Any]) -> None:
    """Cheap runtime sanity check. Runs in well under 60 s.

    Asserts the things Layer-1 already checked, plus GPU availability and
    tmp-writable, which only exist at runtime. Failures write FAILED to
    ``/tmp/smoke_results.json`` and sys.exit(1) so the Modal wrapper
    surfaces them inline.
    """
    # 1. payload shape
    for key in ("target_chain",):
        if key not in payload.get("job_spec", {}):
            _fail(
                "preflight",
                "payload",
                f"missing required key in job_spec: {key}",
            )

    # 2. MPNN binary on disk + executable via python
    if not os.path.isfile(PROTEINMPNN_SCRIPT):
        _fail(
            "preflight",
            "binary",
            f"protein_mpnn_run.py not found at {PROTEINMPNN_SCRIPT}",
        )

    # 3. Weights present
    weight_file = f"{PROTEINMPNN_WEIGHTS}/v_48_020.pt"
    if not os.path.isfile(weight_file):
        _fail("preflight", "weights", f"vanilla weights missing: {weight_file}")

    # 4. Torch + CUDA available (the A10G-24GB SKU must report a device)
    try:
        import torch  # noqa: PLC0415
    except Exception as exc:
        _fail("preflight", "torch", f"torch import failed: {exc}")
    if not torch.cuda.is_available():
        _fail(
            "preflight",
            "cuda",
            "torch.cuda.is_available() is False — no GPU visible",
        )

    # 5. MPNN module imports (uses torch — must succeed after step 4)
    # ProteinMPNN ships a protein_mpnn_utils.py next to protein_mpnn_run.py;
    # we import the former because the latter has side-effects at import.
    mpnn_utils = Path(PROTEINMPNN_DIR) / "protein_mpnn_utils.py"
    if not mpnn_utils.is_file():
        _fail(
            "preflight",
            "module",
            f"protein_mpnn_utils.py not found at {mpnn_utils}",
        )

    # 6. /tmp writable
    try:
        probe = Path("/tmp") / ".mpnn_preflight_probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        _fail("preflight", "tmp", f"/tmp is not writable: {exc}")

    logger.info("preflight ok — GPU=%s", torch.cuda.get_device_name(0))


# ===========================================================================
# Payload parsing / fetch
# ===========================================================================


def parse_payload() -> dict[str, Any]:
    """Read and parse the JOB_PAYLOAD env var."""
    raw = os.environ.get("JOB_PAYLOAD", "").strip()
    if not raw:
        _fail("preflight", "env", "JOB_PAYLOAD env var is empty")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail("preflight", "env", f"JOB_PAYLOAD is not valid JSON: {exc}")
    return {}  # unreachable; _fail exits


def resolve_input_pdb(payload: dict[str, Any], workdir: Path) -> Path:
    """Either download the caller PDB or copy the baked smoke target.

    Smoke tier uses the baked target to avoid a network hop on the smoke
    path; standalone tier downloads the user's upload from the presigned
    URL in the payload.
    """
    tier = str(payload.get("tier") or "").lower()
    if tier == "smoke":
        if not os.path.isfile(SMOKE_TARGET_PDB):
            _fail(
                "input",
                "smoke_fixture",
                f"baked smoke target missing at {SMOKE_TARGET_PDB}",
            )
        dest = workdir / "target.pdb"
        shutil.copy(SMOKE_TARGET_PDB, dest)
        logger.info("smoke tier: using baked target %s", SMOKE_TARGET_PDB)
        return dest

    url = str(payload.get("input_presigned_url") or "").strip()
    if not url:
        _fail("input", "url", "input_presigned_url missing on non-smoke tier")

    import requests  # noqa: PLC0415

    dest = workdir / "target.pdb"
    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=32768):
                    if chunk:
                        fh.write(chunk)
    except Exception as exc:
        _fail("input", "download", f"PDB download failed: {exc}")
    if not dest.is_file() or dest.stat().st_size < 100:
        _fail("input", "download", "downloaded PDB is empty or tiny")
    return dest


# ===========================================================================
# MPNN invocation
# ===========================================================================


def run_mpnn(
    target_pdb: Path,
    chains_to_design: str,
    num_seq_per_target: int,
    sampling_temp: float,
    workdir: Path,
    fixed_positions: dict[str, list[int]] | None = None,
) -> Path:
    """Invoke ``protein_mpnn_run.py`` and return the output directory.

    Command matches the vanilla MPNN README usage. Output goes under
    ``workdir/mpnn_out/`` with ``seqs/<pdb_stem>.fa`` as the FASTA.
    """
    out_dir = workdir / "mpnn_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # MPNN's path input is a directory of PDBs. Stage our single PDB.
    pdb_stage = workdir / "pdb_in"
    pdb_stage.mkdir(parents=True, exist_ok=True)
    staged = pdb_stage / target_pdb.name
    if staged.resolve() != target_pdb.resolve():
        shutil.copy(target_pdb, staged)

    designed = _designed_chains(chains_to_design)
    if fixed_positions and len(staged.suffix) != 4:
        # Upstream derives the dict key by stripping exactly four characters
        # (`biounit[(fi+1):-4]`), which equals Path.stem only for a 4-char
        # extension. A ".pdb1" would key as "x.p" there and "x" here: KeyError.
        # Any 4-char suffix (.pdb, .PDB, .ent, .cif) is fine.
        _fail(
            "preflight",
            "fixed_positions",
            f"fixed positions need a 4-character file extension; got "
            f"{staged.name!r}, whose MPNN dict key would not match the one "
            "written here",
        )

    cmd = [
        "python3",
        PROTEINMPNN_SCRIPT,
        "--pdb_path", str(staged),
        "--pdb_path_chains", chains_to_design,
        "--out_folder", str(out_dir),
        "--num_seq_per_target", str(num_seq_per_target),
        "--sampling_temp", str(sampling_temp),
        "--seed", "37",
        "--batch_size", "1",
    ]

    if fixed_positions:
        # EVERY designed chain needs an entry, empty list included. Upstream reads
        # it as `fixed_position_dict[b['name']][letter]` -- a BARE subscript, run
        # once per designed chain -- so a chain absent from this dict is a KeyError
        # that kills the run after the GPU is paid for, not a default of "nothing
        # fixed". Upstream's own make_fixed_positions_dict.py emits every chain the
        # same way, and the `if fixed_pos_list:` guard beside that lookup is what
        # makes the empty list a safe no-op.
        payload = {c: fixed_positions.get(c, []) for c in sorted(designed)}
        # ProteinMPNN keys this dict by the parsed PDB's name, which is the file
        # stem of what we pass to --pdb_path. Derive it from `staged` rather than
        # from the caller's id.
        fp_path = workdir / FIXED_POSITIONS_JSONL
        fp_path.write_text(json.dumps({staged.stem: payload}) + "\n")
        cmd += ["--fixed_positions_jsonl", str(fp_path)]
        logger.info(
            "mpnn fixing %d position(s) across %d of %d designed chain(s): %s",
            sum(len(v) for v in fixed_positions.values()),
            len(fixed_positions),
            len(designed),
            payload,
        )
    logger.info("mpnn cmd: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=540,  # 9 min; ATOMIC spec caps runtime at 10 min
        )
    except subprocess.TimeoutExpired as exc:
        _fail("tool-invocation", "timeout", f"MPNN exceeded 9 min: {exc}")
        return out_dir  # unreachable

    if result.returncode != 0:
        tail = (result.stderr or "")[-1500:]
        _fail(
            "tool-invocation",
            "exit",
            f"MPNN exited {result.returncode}: ...{tail}",
        )

    logger.info("mpnn exit 0")
    return out_dir


# ===========================================================================
# Output parser + stub rejection
# ===========================================================================


def parse_mpnn_output(out_dir: Path, pdb_stem: str) -> list[dict[str, Any]]:
    """Parse MPNN's FASTA output into the atomic-tool sequence schema.

    ProteinMPNN writes ``<out_dir>/seqs/<pdb_stem>.fa`` where every other
    line is a FASTA header carrying score + sample metadata, followed by
    the sequence. The first record is the original (native) sequence.
    Return the non-native samples.
    """
    fa_path = out_dir / "seqs" / f"{pdb_stem}.fa"
    if not fa_path.is_file():
        _fail(
            "parser",
            "fasta_missing",
            f"expected MPNN FASTA at {fa_path}",
        )

    sequences: list[dict[str, Any]] = []
    header: str | None = None
    for line in fa_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            header = line[1:]
            continue
        if header is None:
            continue
        seq = line
        # Skip the first record (original native) which MPNN always emits.
        if header.startswith(f"{pdb_stem}"):
            # Native record: no "sample=" metadata.
            if "sample=" not in header:
                header = None
                continue

        score = _extract_metadata(header, "global_score")
        if score is None:
            score = _extract_metadata(header, "score")
        recovery = _extract_metadata(header, "seq_recovery")
        sample = _extract_metadata(header, "sample")

        sequences.append(
            {
                "seq": seq,
                "score": float(score) if score is not None else None,
                "recovery": float(recovery) if recovery is not None else None,
                "sample": int(sample) if sample is not None else None,
                "chain": "",  # MPNN's FASTA does not break chains out
            }
        )
        header = None

    if not sequences:
        _fail(
            "parser",
            "empty",
            f"parsed zero sample sequences from {fa_path.name}",
        )

    return sequences


def _native_record(out_dir: Path, pdb_stem: str) -> tuple[str, str] | None:
    """The native (input) record MPNN echoes first, as ``(header, sequence)``.

    The header is not decoration: it carries ``designed_chains=[...]``, which is
    the only authoritative statement of what MPNN actually designed, and the only
    sound way to map "/"-separated segments onto chain ids. The sequence carries
    the same segmentation as every sampled record, which makes it the reference
    for the fixed-position comparison.
    """
    fa_path = out_dir / "seqs" / f"{pdb_stem}.fa"
    if not fa_path.is_file():
        return None
    header = None
    for line in fa_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            header = line[1:]
            continue
        if header is not None and "sample=" not in header:
            return header, line
        header = None
    return None


def verify_fixed_positions(
    sequences: list[dict[str, Any]],
    native: tuple[str, str] | None,
    fixed_positions: dict[str, list[int]],
    chain_counts: dict[str, int],
) -> dict[str, Any]:
    """Prove the freeze happened AND that a design happened. _fail if not.

    The failure modes this guards are the ones that are SILENT upstream. Verified
    against dauparas/ProteinMPNN@main rather than assumed — most ways of getting
    this wrong actually crash, and only these do not:

      1. A --fixed_positions_jsonl path that does not exist. protein_mpnn_run.py
         does `if os.path.isfile(...): ... else: fixed_positions_dict = None`.
         Upstream does print a notice, but run_mpnn captures stdout and never
         reads it (only stderr, and only on a non-zero exit), so it is silent
         HERE. Result: a full redesign that looks like a success, and a caller
         splicing a "conserved" interface that was in fact rewritten.
      2. A 0-indexed position. `fixed_position_mask[np.array(fixed_pos_list)-1]`
         turns 0 into index -1, silently freezing the LAST residue of the chain.
         (normalise_fixed_positions rejects 0 before it can get here; this is why.)

    For contrast, the modes that do NOT need catching here because upstream dies
    loudly: a dict key that does not match the parsed PDB name, and a designed
    chain missing from the dict, are both bare subscripts (KeyError); a
    --pdb_path_chains value MPNN cannot parse fails on `seq_chain_A,B` (KeyError).

    What is measured:

      * the native header's designed_chains=[...] must contain every chain we
        asked to fix
      * MPNN's own segment lengths must agree with the residue counts the bounds
        check used, so a parser disagreement is loud instead of mis-indexed
      * every fixed position must equal the native residue, and at least one
        such comparison must actually have been made
      * where enough positions were free to judge it, at least one must DIFFER
        from native — otherwise nothing was designed at all and every assertion
        above passes vacuously

    Segments carry ONLY the designed chains, in alphabetical chain order
    (protein_mpnn_run.py masks with chain_M and sorts via np.argsort before
    joining), so the header list IS the segment mapping. Residue counts are a
    cross-check and never the mapping: inferring chains from lengths makes any
    two equal-length chains ambiguous and would refuse homodimers — after the
    GPU had already been billed.
    """
    if not fixed_positions:
        return {"checked": False, "reason": "no fixed positions requested"}
    if not native:
        _fail(
            "verify",
            "fixed_positions",
            "fixed positions were requested but MPNN emitted no native record "
            "to verify them against",
        )

    header, nat = native
    designed = _header_chain_list(header, "designed_chains")
    if designed is None:
        _fail(
            "verify",
            "fixed_positions",
            "MPNN's native FASTA header carries no designed_chains=[...] field, "
            f"so there is no sound way to map segments onto chains: {header!r}",
        )

    missing = sorted(set(fixed_positions) - set(designed))
    if missing:
        _fail(
            "verify",
            "fixed_positions",
            f"MPNN reports designed_chains={designed}, but positions were fixed "
            f"on {missing}, which it did not design. Freezing residues in a chain "
            "that was never designed proves nothing — check chains_to_design is "
            "space-separated, since MPNN splits it on whitespace only",
        )

    nat_segs = nat.split("/")
    if len(nat_segs) != len(designed):
        _fail(
            "verify",
            "fixed_positions",
            f"native record has {len(nat_segs)} segment(s) but its header lists "
            f"designed_chains={designed}",
        )
    lengths = [len(s) for s in nat_segs]

    # Our count vs MPNN's. A disagreement means the pre-flight bounds check ran
    # against the wrong chain lengths, so positions may be off the end or simply
    # the wrong residues. Name both numbers rather than trusting either.
    disagree = [
        (chain, chain_counts[chain], len(seg))
        for chain, seg in zip(designed, nat_segs)
        if chain in chain_counts and chain_counts[chain] != len(seg)
    ]
    if disagree:
        _fail(
            "verify",
            "fixed_positions",
            "residue counts disagree with MPNN's parser for "
            + ", ".join(f"chain {c}: ours={o}, mpnn={m}" for c, o, m in disagree)
            + " — the 1-indexed positions were bounds-checked against the wrong "
            "lengths, so the freeze cannot be trusted",
        )

    # Free POSITIONS per chain, counted once and independently of how many
    # sequences came back. Summing comparisons across sequences instead would
    # make the threshold depend on num_seq_per_target: at the production n=8 it
    # would trip at two free positions, the exact case it exists to protect.
    free_positions = {
        chain: lengths[idx]
        - len({p for p in fixed_positions.get(chain, ()) if 1 <= p <= lengths[idx]})
        for idx, chain in enumerate(designed)
    }
    free_changes = {chain: 0 for chain in designed}

    n_checked = 0
    for rec in sequences:
        segs = (rec.get("seq") or "").split("/")
        if [len(s) for s in segs] != lengths:
            _fail(
                "verify",
                "fixed_positions",
                f"designed record has segmentation {[len(s) for s in segs]} but "
                f"the native record has {lengths}",
            )
        for idx, chain in enumerate(designed):
            frozen = set(fixed_positions.get(chain, ()))
            for p in range(1, lengths[idx] + 1):
                same = segs[idx][p - 1] == nat_segs[idx][p - 1]
                if p in frozen:
                    if not same:
                        _fail(
                            "verify",
                            "fixed_positions",
                            f"position {p} of chain {chain} was requested FIXED "
                            f"but MPNN returned {segs[idx][p - 1]!r} where the "
                            f"input has {nat_segs[idx][p - 1]!r} — the "
                            "fixed-positions file did not take effect",
                        )
                    n_checked += 1
                elif not same:
                    free_changes[chain] += 1

    # A "pass" that asserted nothing is not a pass. Every requested position
    # landing outside the emitted segment would otherwise return checked=True on
    # zero comparisons — the same vacuous-success shape this whole function exists
    # to refuse, one level up.
    if n_checked == 0:
        _fail(
            "verify",
            "fixed_positions",
            f"no fixed position was actually compared: requested "
            f"{ {k: len(v) for k, v in fixed_positions.items()} } against segment "
            f"lengths {lengths} for chains {designed}",
        )

    # Without this the rest is satisfiable by doing nothing: a run that designs
    # zero positions conserves every fixed position perfectly, and reject_stub is
    # skipped precisely when few positions are free.
    #
    # Judged PER CHAIN. A global count lets one busy chain mask another that was
    # never touched, which is the same vacuous pass at chain granularity.
    judged = [c for c in designed if free_positions[c] >= MIN_FREE_TO_JUDGE_DIVERSITY]
    dead = [c for c in judged if free_changes[c] == 0]
    if dead:
        _fail(
            "verify",
            "fixed_positions",
            "not one free position changed in "
            + ", ".join(f"chain {c} ({free_positions[c]} free)" for c in dead)
            + f" across {len(sequences)} sequence(s) — MPNN returned the input "
            "unchanged there, not a design, and the fixed-position check would "
            "otherwise pass vacuously",
        )

    logger.info(
        "fixed-position check ok — %d position-assertions across %d sequence(s); "
        "free positions %s, changes %s; echo-judged chains: %s",
        n_checked,
        len(sequences),
        free_positions,
        free_changes,
        judged or "none (too few free positions anywhere)",
    )
    return {
        "checked": True,
        "n_sequences": len(sequences),
        "n_assertions": n_checked,
        "free_positions": free_positions,
        "free_changes": free_changes,
        # True only when EVERY designed chain had enough free positions to judge.
        # bool(judged) would report True while a second chain came back a verbatim
        # native echo, and reject_stub's docstring leans on this flag as the
        # compensating control for its own blind spot — so the two blind spots
        # would line up instead of covering each other. False means a no-op could
        # not be ruled out somewhere, NOT that the run failed verification.
        "echo_judged": len(judged) == len(designed),
        "echo_unjudged_chains": [c for c in designed if c not in judged],
        "designed_chains": designed,
        "fixed_positions": {k: len(v) for k, v in fixed_positions.items()},
    }


def _extract_metadata(header: str, key: str) -> str | None:
    """Pull a key=value metadatum from an MPNN FASTA header."""
    match = re.search(rf"{re.escape(key)}\s*=\s*([^,\s]+)", header)
    if not match:
        return None
    return match.group(1)


def reject_stub(
    sequences: list[dict[str, Any]],
    n_free_positions: int | None = None,
) -> None:
    """Stub-rejection guard. Per ATOMIC-TOOLS.md D1 section.

    MPNN's silent-stub failure modes seen in practice:

    1. Every returned sequence is identical (model never ran / wrong
       weights loaded). Hard fail.
    2. Every sequence shares identical score + recovery floats to the
       bit (same random seed, no sampling). Hard fail.
    3. "Degenerate mode" — sequences differ by only 1-2 residues and
       score/recovery spreads are tiny (< 0.01). The model technically
       ran but collapsed; results are not usable. Hard fail so we don't
       bill a user for useless output.

    EVERY one of those reads low diversity as failure, which is right for a
    whole-chain redesign and wrong for a constrained fixed-position run: freeze
    105 of 110 residues and near-argmax sampling makes identical samples the
    EXPECTED output, not a stub. The near-clone guard is worse still — it trips
    at a Hamming distance of 2, which a 5-position redesign cannot exceed often.

    ``n_free_positions`` is how much freedom MPNN had in the FREEST designed
    chain — these guards compare whole concatenated sequences, so that is what
    sets the diversity they should expect (None = whole-chain redesign, the
    pre-existing behaviour). Below MIN_FREE_TO_JUDGE_DIVERSITY they genuinely
    cannot separate a stub from a correct answer, so they are skipped rather than
    guessed. The blind spot is then reported instead of papered over: the result
    carries ``stub_check_skipped``, verify_fixed_positions still proves the freeze
    held, and its ``echo_judged`` flag records whether a no-op could be ruled out.
    """
    if (
        n_free_positions is not None
        and n_free_positions < MIN_FREE_TO_JUDGE_DIVERSITY
    ):
        logger.info(
            "stub rejection skipped — only %d position(s) were free to change, "
            "so identical samples are expected rather than diagnostic",
            n_free_positions,
        )
        return

    seqs = [s.get("seq") or "" for s in sequences]
    if len(seqs) >= 2 and len(set(seqs)) == 1:
        _fail(
            "parser",
            "stub",
            (
                "all returned sequences are identical — "
                "this is the ProteinMPNN silent-stub failure mode. "
                f"n={len(seqs)}, length={len(seqs[0])}"
            ),
        )

    # Defence-in-depth: exact equality of score + recovery across the
    # set. Catches the naive replay-the-same-tensor stub.
    recoveries = [s.get("recovery") for s in sequences if s.get("recovery") is not None]
    scores = [s.get("score") for s in sequences if s.get("score") is not None]
    if (
        len(seqs) >= 3
        and len(set(recoveries)) == 1
        and len(set(scores)) == 1
        and recoveries
    ):
        _fail(
            "parser",
            "stub",
            (
                "all returned sequences share identical score + recovery "
                f"(score={scores[0]}, recovery={recoveries[0]}) — stub suspect."
            ),
        )

    # Near-clone detection: pairwise Hamming distance. If every pair of
    # sequences differs by <= 2 residues, the model has collapsed. Codex
    # P2 — the previous guards only tripped on bit-exact matches, which
    # missed this real ProteinMPNN degenerate mode.
    if len(seqs) >= 3 and all(len(s) == len(seqs[0]) and s for s in seqs):
        max_pairwise_hamming = 0
        for i, s1 in enumerate(seqs):
            for s2 in seqs[i + 1:]:
                d = sum(1 for a, b in zip(s1, s2) if a != b)
                if d > max_pairwise_hamming:
                    max_pairwise_hamming = d
        if max_pairwise_hamming <= 2:
            _fail(
                "parser",
                "stub",
                (
                    "returned sequences are near-clones (max pairwise "
                    f"Hamming={max_pairwise_hamming} over n={len(seqs)} "
                    f"samples of length {len(seqs[0])}) — ProteinMPNN "
                    "degenerate mode."
                ),
            )

    # Near-clone detection on score/recovery: tight cluster (spread <
    # 0.01) on both score AND recovery across >=3 samples. Covers the
    # failure mode where sampling injects residue diversity but the
    # probability landscape is collapsed.
    if len(seqs) >= 3 and len(scores) >= 3 and len(recoveries) >= 3:
        score_spread = max(scores) - min(scores)
        recovery_spread = max(recoveries) - min(recoveries)
        if score_spread < 0.01 and recovery_spread < 0.01:
            _fail(
                "parser",
                "stub",
                (
                    "score+recovery cluster is suspiciously tight "
                    f"(score spread={score_spread:.4f}, "
                    f"recovery spread={recovery_spread:.4f} over n={len(seqs)}) — "
                    "ProteinMPNN degenerate mode."
                ),
            )


# ===========================================================================
# Raw capture
# ===========================================================================


def _archive_raw(work_dir: Path) -> None:
    """Tar the COMPLETE work dir to RAW_ARCHIVE_PATH. Best-effort; never raises.

    A container must not decide which fields are worth keeping. The parser above
    keeps ``seq``/``score``/``recovery``/``sample`` off each FASTA header and lets
    the rest of the MPNN tree — the per-residue probability npz, the scores dir,
    the unparsed header fields, the staged input PDB — die with the tempdir.
    Anything not shipped here is recoverable only by paying for the GPU again.
    That is exactly how ``design_iptm`` was lost on 460 BoltzGen designs: three
    numbers were kept out of ~190 columns, and the one that was kept was the
    wrong one. Decide LOCALLY, where re-parsing is free.

    Not gated on success, on sequences, or on filenames_to_upload: a run that
    crashed or parsed to zero is precisely the run whose tree you need. Callers
    invoke this from a ``finally``, so failure to archive must never break the
    run — problems are logged, never raised.
    """
    # Pre-binding for the handler at the bottom, which removes ``dest``. The try
    # only assigns it once abspath+isdir have both succeeded, so a failure
    # before that would have the cleanup raise UnboundLocalError on top of the
    # error it was invoked to clean up after — and UnboundLocalError is a
    # NameError, which the inner ``except OSError`` does not catch, so it would
    # escape a never-raises function called from a ``finally``. The single call
    # site passes a live Path, so this is the contract being made unconditional
    # rather than a defect being repaired.
    dest: str | None = None
    try:
        src = os.path.abspath(str(work_dir))
        if not os.path.isdir(src):
            logger.warning("raw capture: no work dir at %s — nothing to archive", src)
            return
        dest = os.path.abspath(RAW_ARCHIVE_PATH)
        # The tar must never be written inside the tree it archives, or it tars
        # itself. /tmp/raw_archive.tgz is outside a /tmp/mpnn_*/ work dir, but
        # check rather than trust the layout — a future dir= change would make
        # this silently recursive.
        if os.path.commonpath([src, dest]) == src:
            logger.warning(
                "raw capture: archive path %s is inside work dir %s — skipping "
                "so the tar does not archive itself",
                dest,
                src,
            )
            return
        # Stream to a file, never io.BytesIO: ~1x peak RSS instead of ~3-4x.
        with tarfile.open(dest, "w:gz") as tf:
            tf.add(src, arcname=os.path.basename(src.rstrip(os.sep)) or "work")
        logger.info(
            "raw capture: archived %s -> %s (%.1f MB)",
            src,
            dest,
            os.path.getsize(dest) / 1e6,
        )
    except Exception as exc:  # noqa: BLE001 — capture is best-effort by design
        logger.warning("raw capture failed (non-fatal): %s: %s", type(exc).__name__, exc)
        # A crash mid-write (e.g. ENOSPC) can leave a truncated but still-openable .tgz at
        # the destination; the wrapper parks whatever exists. Remove the partial so a failed
        # capture parks NOTHING rather than a tar that reports success but cannot be read.
        try:
            if dest is not None and os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass


# ===========================================================================
# Main
# ===========================================================================


def main() -> None:
    start = time.time()
    payload = parse_payload()
    preflight(payload)

    job_spec = payload.get("job_spec") or {}
    tier = str(payload.get("tier") or "").lower() or "standalone"

    chains_to_design = str(job_spec.get("target_chain") or "A").strip()
    # A non-dict `parameters` (null, a string, a list) used to raise AttributeError
    # here — outside the TypeError/ValueError catch, so the run died with no FAILED
    # result written at all.
    raw_params = job_spec.get("parameters")
    params = raw_params if isinstance(raw_params, dict) else {}
    try:
        num_seq_per_target = int(params.get("num_seq_per_target", 5))
    except (TypeError, ValueError):
        num_seq_per_target = 5
    try:
        sampling_temp = float(params.get("sampling_temp", 0.1))
    except (TypeError, ValueError):
        sampling_temp = 0.1

    # Defensive clamping (adapter already validates, but belts-and-braces
    # because the pipeline may be invoked directly via ``modal run``).
    num_seq_per_target = max(NUM_SEQ_MIN, min(NUM_SEQ_MAX, num_seq_per_target))
    sampling_temp = max(TEMP_MIN, min(TEMP_MAX, sampling_temp))

    # Smoke tier forces the fastest possible preset regardless of caller.
    if tier == "smoke":
        num_seq_per_target = 2
        sampling_temp = 0.1

    with tempfile.TemporaryDirectory(prefix="mpnn_", dir="/tmp") as _td:
        workdir = Path(_td)
        try:
            target_pdb = resolve_input_pdb(payload, workdir)
            # Validated against the REAL pdb, so chain names and bounds are checked
            # against what MPNN will actually parse rather than what the caller
            # believed it uploaded. Smoke tier ignores caller params entirely.
            fixed_positions = (
                {}
                if tier == "smoke"
                else normalise_fixed_positions(job_spec, target_pdb, chains_to_design)
            )
            chain_counts = _chain_residue_counts(target_pdb)
            out_dir = run_mpnn(
                target_pdb=target_pdb,
                chains_to_design=chains_to_design,
                num_seq_per_target=num_seq_per_target,
                sampling_temp=sampling_temp,
                workdir=workdir,
                fixed_positions=fixed_positions,
            )
            sequences = parse_mpnn_output(out_dir, pdb_stem=target_pdb.stem)
            # How much freedom MPNN actually had. Without this, freezing most of
            # a binder makes identical samples — the correct answer — look like
            # the silent-stub failure mode, and the run is rejected after it is
            # paid for. None keeps the pre-existing behaviour for redesigns.
            #
            # The MAXIMUM across designed chains. No single scalar is right here
            # — reject_stub compares whole concatenated sequences, so strictly the
            # expected diversity follows the TOTAL free positions — and max is
            # chosen because the two errors are not symmetric, not because it is
            # exact:
            #
            #   * A false reject bills a rescue run and then hard-FAILs the user's
            #     correct answer with a message blaming the model. Unrecoverable.
            #     Summing does this to any multi-chain run frozen the way this
            #     feature intends (two chains at 105-of-110 clears 10 and fails).
            #   * A false accept is visible in the emitted result: free_changes
            #     all-zero with echo_judged false and stub_check_skipped true is
            #     the complete signature, which is what those fields are for.
            #
            # Not the minimum: one tight chain would switch the guard off for a
            # co-designed chain that was entirely free — the normal shape here
            # (freeze an interface, redesign a partner) — and would also lose the
            # near-clone and tight-score detectors, which catch degenerate modes
            # verify_fixed_positions cannot see because free_changes > 0 there.
            #
            # KNOWN HOLE: several chains each just under the threshold sum to
            # plenty of freedom, and a genuine stub across all of them is skipped
            # (2 chains x 9 free). Declared in the result rather than hidden. The
            # exact fix is to run the stub guard PER CHAIN on segs[idx], gated on
            # free_positions[chain], the way verify_fixed_positions computes
            # `dead` — worth doing once something actually calls this.
            n_free_max = None
            if fixed_positions:
                n_free_max = max(
                    chain_counts.get(c, 0) - len(fixed_positions.get(c, ()))
                    for c in _designed_chains(chains_to_design)
                )
            reject_stub(sequences, n_free_positions=n_free_max)
            fixed_check = verify_fixed_positions(
                sequences,
                _native_record(out_dir, target_pdb.stem),
                fixed_positions,
                chain_counts,
            )
            # Whether the stub guard actually ran. Skipping it is correct when
            # MPNN had too little freedom for diversity to mean anything, but it
            # is a real blind spot and a consumer cannot otherwise tell a guarded
            # run from an unguarded one — the log line is not machine-readable.
            if n_free_max is not None:
                fixed_check = {
                    **fixed_check,
                    "max_free_positions": n_free_max,
                    "stub_check_skipped": n_free_max < MIN_FREE_TO_JUDGE_DIVERSITY,
                }
            # Smoke tier discarded the request above. Say that, rather than
            # reporting the "nothing was asked for" reason and leaving a consumer
            # unable to tell the two apart.
            if tier == "smoke" and params.get("fixed_positions"):
                fixed_check = {
                    "checked": False,
                    "reason": "smoke tier ignores caller fixed_positions",
                }
        finally:
            # Archive the tree before TemporaryDirectory.__exit__ rmtrees it, on
            # EVERY exit path. Every _fail() above raises SystemExit from inside
            # this block (download failure, MPNN timeout, non-zero exit, missing
            # FASTA, stub rejection) and would otherwise destroy the evidence for
            # the failure it is reporting.
            _archive_raw(workdir)

    runtime_seconds = int(time.time() - start)
    _write_result(
        {
            "status": "COMPLETED",
            "tier": tier,
            "sequences": sequences,
            "num_sequences": len(sequences),
            "sampling_temp": sampling_temp,
            "chains_designed": chains_to_design,
            # Carried into the result so a consumer can tell a genuine
            # fixed-position run from a whole-chain redesign without re-reading
            # the request it was answering.
            "fixed_positions_check": fixed_check,
            "runtime_seconds": runtime_seconds,
            "provider_job_id": os.environ.get("JOB_ID", ""),
        }
    )
    logger.info(
        "pipeline ok — %d sequences, runtime=%ds",
        len(sequences),
        runtime_seconds,
    )


if __name__ == "__main__":
    main()
