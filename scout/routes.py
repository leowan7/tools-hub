"""Epitope Scout Flask Blueprint mounted under ``/scout``.

Ported from ``epitope-scout/app.py`` as part of the Scout-into-tools-hub
consolidation. Auth, signup, password-reset, and the upgrade page are
owned by tools-hub (``shared.auth`` + ``/pricing``). Everything left in
this module is Scout-specific: PDB upload, structural scoring, SSE
progress, feasibility, and the tool handoff.

The free-tier paywall (``scout.quota``) still works because the
tools-hub Supabase project is the same one Scout always used.
"""

from __future__ import annotations

import csv as csv_module
import json
import logging
import re
import shutil
import uuid
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from shared.auth import login_required

from scout.errors import ScoutInputError
from scout.jobs import (
    cleanup_old_jobs,
    count_job_dirs,
    create_job_dir,
    resolve_owned_job_dir,
)
from scout.parser import parse_pdb
from scout.quota import (
    FREE_TIER_RUN_CAP,
    quota_status,
    record_scout_run,
    requires_scout_quota,
)
from scout.ratelimit import (
    REASON_AT_CAPACITY,
    REASON_BAD_REQUEST,
    REASON_BUSY,
    REASON_JOB_EXPIRED,
    anon_compute_slot,
    anon_rate_limit,
)

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdb", ".cif"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# ---------------------------------------------------------------------------
# Anonymous-access policy
# ---------------------------------------------------------------------------
#
# Deciding which residues to aim a binder at is the single thing that blocks a
# bench biologist, and Scout is the tool that answers it — so the landing page,
# the 1HEW example, structure intake and the scoring run are reachable without
# an account. The handoff into a design tool still needs one: it writes a
# user-keyed ``scout_handoffs`` row and stages a PDB under the user's storage
# prefix, neither of which exists for an anonymous visitor.
#
# That makes /scout the only unauthenticated upload + compute path in the app,
# so it carries its own abuse controls. Every number below is a policy choice,
# documented with why it is where it is:

# Upload ceiling for anonymous callers. 1HEW is 40 KB; a typical antibody /
# antigen complex is well under 1 MB; the largest single-assembly entries a
# Scout user would realistically bring are a few MB. 8 MB leaves an order of
# magnitude of headroom while cutting the worst-case anonymous disk write to
# 40% of the signed-in cap. Signed-in users keep MAX_UPLOAD_BYTES (20 MB),
# which is also the app-wide MAX_CONTENT_LENGTH Werkzeug enforces during
# multipart parsing, before any of this code runs.
ANON_MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# Fleet-wide ceiling on anonymous job directories that exist at once. With the
# 1-hour retention window in cleanup_old_jobs, 60 x 8 MB bounds anonymous disk
# use at roughly 480 MB. This is the backstop the per-IP limit cannot provide:
# a distributed source spraying from many addresses defeats an IP bucket but
# still lands in this one counter.
ANON_MAX_LIVE_JOBS = 60

# ...and per session, so one visitor cannot occupy the whole fleet-wide budget
# by itself. Five is enough to re-upload a few times while iterating on which
# structure to score.
ANON_MAX_LIVE_JOBS_PER_SESSION = 5

# Per-IP fixed windows. Ten minutes is long enough that a burst of retries
# after a bad file does not lock a real user out for the rest of the session.
ANON_RATE_WINDOW_SECONDS = 600
# Intake (upload / fetch-pdb / example) — 10 structures per 10 minutes. A real
# first visit uses 1-3.
ANON_INTAKE_LIMIT = 10
# Analysis. The page runs one SSE /progress stream *and* one POST /analyze per
# scoring run, and both share this bucket, so 10 is about 5 real runs per 10
# minutes. /progress is the expensive half — it is what actually executes the
# pipeline — so it cannot be left unmetered while /analyze is capped.
ANON_ANALYZE_LIMIT = 10

# How many anonymous scoring pipelines may run at once in one worker process.
#
# INERT AS DEPLOYED — gunicorn runs sync workers, so a process serves one
# request at a time and in-flight anonymous runs can never exceed 1, let alone
# 2. Real concurrency is bounded by the worker count (WEB_CONCURRENCY,
# default 2). Do not cite this as current protection; see gunicorn.conf.py for
# the evaluation of the worker-class change and what has to land first.
#
# Two is derived rather than inherited, so that the flip is a one-line change
# and not a redesign: see the arithmetic above ANON_MAX_QUEUED_RUNS in
# scout/ratelimit.py. Short version — a threaded worker is GIL-bound at ~1.07
# effective cores, and concurrent CPU-bound pipelines under a GIL all finish
# LATE together rather than one at a time, so more slots push the first free
# slot further away and buy no throughput at all.
#
# Signed-in callers never consume a slot, so a free visitor cannot starve a
# paying user.
ANON_MAX_CONCURRENT_RUNS = 2

_BUSY_MESSAGE = (
    "Epitope Scout is busy with other free runs right now. Try again in a "
    "moment, or sign in to run it on your account."
)

# Session key holding the anonymous owner id. Prefixed so it can never collide
# with a Supabase uid or an email address.
ANON_SESSION_KEY = "scout_anon_id"
ANON_OWNER_PREFIX = "anon:"

scout_bp = Blueprint(
    "scout",
    __name__,
    url_prefix="/scout",
    template_folder="../templates/scout",
)


def _signed_in_owner_key() -> str:
    """Stable per-user key used to stamp and gate scout job directories.

    Prefers the Supabase auth uid (set in the session at login when
    available) and falls back to the email. The same key is written when a
    job dir is created and checked on every read, so the fallback stays
    internally consistent within a session.
    """
    return (session.get("user_id") or session.get("user_email") or "").strip()


def _current_owner_key(*, mint: bool = False) -> str:
    """Owner key to stamp on a NEW job dir, or "" when there is none.

    Signed-in callers get their user key. Anonymous callers get a random
    per-session id held in the signed session cookie, so an anonymous job is
    readable only by the browser session that created it — the uploaded
    structure is someone's unpublished target and must not become readable
    by anyone who can name a job id.

    ``mint=True`` only on the routes that create a job. Read routes must not
    mint, or every crawler hit would allocate a Scout owner id and with it a
    job-directory budget.

    To be precise about what that does and does not prevent: a GET of a Scout
    page DOES still return a Set-Cookie, because base.html renders a CSRF
    token meta tag and that touches the session. What ``mint=False`` buys is
    that the cookie carries no ``scout_anon_id`` — so a crawler consumes no
    ANON_MAX_LIVE_JOBS_PER_SESSION budget and causes no disk allocation. The
    session cookie itself is unavoidable while the CSRF meta is rendered.
    """
    key = _signed_in_owner_key()
    if key:
        return key
    anon = session.get(ANON_SESSION_KEY)
    if isinstance(anon, str) and anon.startswith(ANON_OWNER_PREFIX):
        return anon
    if not mint:
        return ""
    anon = ANON_OWNER_PREFIX + uuid.uuid4().hex
    session[ANON_SESSION_KEY] = anon
    return anon


def _owner_keys() -> list[str]:
    """Every owner key this session may read job dirs under.

    Both the signed-in key and the session's anonymous id, when both exist:
    a visitor who scored a structure anonymously and then signed in to use
    the handoff keeps access to the job they just ran. Both keys are bound
    to this one signed session, so this widens nothing across users.
    """
    keys = [k for k in (_signed_in_owner_key(), session.get(ANON_SESSION_KEY)) if k]
    return [k for k in keys if isinstance(k, str)]


def _resolve_job_dir(job_id) -> "Path | None":
    """Validate + confine + ownership-check ``job_id`` against this session."""
    for owner in _owner_keys():
        job_dir = resolve_owned_job_dir(job_id, owner)
        if job_dir is not None:
            return job_dir
    return None


def _anon_capacity_error() -> "tuple[dict, int] | None":
    """Refuse a new anonymous job when the live-job budgets are full.

    Returns a ``(payload, status)`` pair to hand straight to ``jsonify``, or
    None when there is room. Signed-in callers are never limited here.
    Call AFTER ``cleanup_old_jobs`` so expired dirs do not count.
    """
    if _signed_in_owner_key():
        return None
    if count_job_dirs(ANON_OWNER_PREFIX) >= ANON_MAX_LIVE_JOBS:
        logger.warning("Scout anonymous job capacity reached (%d).", ANON_MAX_LIVE_JOBS)
        return {
            "error": (
                "Epitope Scout is at capacity for anonymous runs right now. "
                "Try again in a few minutes, or sign in to run it on your account."
            ),
            "reason": REASON_AT_CAPACITY,
        }, 503
    anon = session.get(ANON_SESSION_KEY)
    if anon and count_job_dirs(anon) >= ANON_MAX_LIVE_JOBS_PER_SESSION:
        return {
            "error": (
                "You have several Epitope Scout structures still loaded. "
                "Earlier ones are cleared automatically within the hour, or "
                "sign in to keep more at once."
            ),
            "reason": REASON_AT_CAPACITY,
        }, 429
    return None


def _reject_oversized(save_path: Path, job_dir: Path) -> "tuple[dict, int] | None":
    """Delete the job and refuse it when the stored structure is over cap.

    The app-wide ``MAX_CONTENT_LENGTH`` already bounds the request body at
    20 MB during multipart parsing; this is the tighter anonymous ceiling,
    measured on the bytes actually written rather than a client-supplied
    Content-Length.
    """
    cap = MAX_UPLOAD_BYTES if _signed_in_owner_key() else ANON_MAX_UPLOAD_BYTES
    try:
        size = save_path.stat().st_size
    except OSError:
        size = 0
    if size <= cap:
        return None
    shutil.rmtree(job_dir, ignore_errors=True)
    return {
        "error": (
            f"That structure is {size // (1024 * 1024)} MB. The limit is "
            f"{cap // (1024 * 1024)} MB — upload a single chain or complex "
            "rather than a full asymmetric unit."
        )
    }, 413


def _extract_structure_title(path: Path, suffix: str) -> str:
    """Extract a human-readable protein name from a PDB or mmCIF header."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""

    if suffix == ".pdb":
        compnd_lines = [
            line[10:].strip()
            for line in text.splitlines()
            if line.startswith("COMPND")
        ]
        compnd_blob = " ".join(compnd_lines)
        match = re.search(r"MOLECULE:\s*([^;]+)", compnd_blob, re.IGNORECASE)
        if match:
            return match.group(1).strip().title()
        return ""

    if suffix == ".cif":
        match = re.search(r"_struct\.title\s+'([^']+)'", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        match = re.search(r"_struct\.title\s+\"([^\"]+)\"", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        match = re.search(r"_struct\.title\s+(\S[^\n]+)", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return ""

    return ""


def _find_input_file(job_dir: Path) -> "Path | None":
    for ext in (".pdb", ".cif"):
        candidate = job_dir / f"input{ext}"
        if candidate.exists():
            return candidate
    return None


_GENERIC_ERROR = "Analysis failed. Check that the PDB is valid and try again."


def _client_error(exc: BaseException) -> str:
    """Client-safe text for a caught exception.

    Callers log first, except where the text IS the log -- a forwarded
    ``ScoutInputError`` is already recorded in the response.

    Scout signals "your input is wrong" by raising
    :class:`~scout.errors.ScoutInputError` with a message written for the
    person who uploaded the structure -- "Chain 'Z' not found in structure.
    Available chains: A, B" (``scout/pipeline.py``, 6 sites). Those are the
    product's only diagnostics and are forwarded verbatim. Everything else
    is replaced.

    THE ALLOWLIST IS A TYPE SCOUT OWNS, and that is the whole point.
    Allowlisting ``ValueError`` -- as this did until 2026-08-19 -- made the
    safety of this function a claim about every frame in the transitive
    stack: Biopython, numpy, scipy, freesasa, the stdlib, at every version
    anyone will ever install. Any of them may raise a plain ``ValueError``
    whose message quotes a path or echoes the caller's own input, and
    ``int()`` already does the latter. Auditing that set once said nothing
    about the next release. The guarantee is now a property of this repo.

    The rule the old type was really for still stands. ``OSError.__str__``
    interpolates ``filename``, so a ``FileNotFoundError`` raised anywhere
    under the pipeline would hand the browser an absolute server path. It is
    an allowlist rather than an ``OSError`` denylist because the set of types
    reaching these handlers is open -- the SSE workers catch bare
    ``Exception``.

    ``isinstance``, not exact type: subclassing ``ScoutInputError`` is itself
    the declaration that the message was written for a user, and an exact
    check would silently downgrade such a subclass to the generic string.
    The exact check this replaced existed because ``UnicodeDecodeError`` and
    ``json.JSONDecodeError`` subclass ``ValueError`` without inheriting any
    promise about their text; nothing outside ``scout`` subclasses this.

    Server-side faults deliberately do NOT get this type -- see
    ``scout/errors.py``. ``scout/epitope_db.py`` still raises plain
    ``ValueError`` for a malformed SAbDab summary, which is an operator's
    problem and reaches the user as the generic message.

    ONE THING THIS DOES NOT COST US, because of a gate two routes away.
    Biopython answers an empty or non-``data_`` structure file with a plain
    ``ValueError`` carrying real advice ("Empty file.", "The input mmCIF file
    must begin with a 'data_' directive."), and those are exactly the
    messages a stricter allowlist would swallow. They never reach here:
    every intake path runs ``scout.parser.parse_pdb`` first, which catches
    everything and answers with its own curated 422. If that gate is ever
    removed, this function starts eating those messages -- reinstate them as
    ``ScoutInputError`` at the same time.

    An empty message falls back too -- the SSE clients render ``data.msg``
    straight into the banner (``templates/scout/index.html:346``), and a
    blank banner is worse than a generic one.
    """
    if isinstance(exc, ScoutInputError) and str(exc).strip():
        return str(exc)
    return _GENERIC_ERROR

# Per-chain residue counts, written at intake, read by /analyze.
_CHAIN_INDEX_NAME = "chains.json"


def _save_chain_index(job_dir: Path, result) -> None:
    """Persist the per-chain residue counts this parse already produced.

    /analyze needs the target chain's residue count to cap patch size, and
    every intake route has already parsed the whole structure to list its
    chains for the picker. Recomputing it in /analyze meant a second full
    BioPython parse of a file up to the 8 MB anonymous cap — ~0.8 CPU-s — for
    a number that was in hand a moment earlier. A few hundred bytes on disk
    deletes that parse.

    Best effort: if this fails, /analyze parses instead.
    """
    try:
        (job_dir / _CHAIN_INDEX_NAME).write_text(
            json.dumps({chain.id: chain.residue_count for chain in result.chains}),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("Could not write chain index for %s", job_dir, exc_info=True)


def _chain_residue_count(job_dir: Path, pdb_path: Path, chain_id: str) -> "int | None":
    """Residue count for ``chain_id``, from the intake index or by parsing.

    The parse is a fallback for job directories created before the index
    existed — a deploy landing mid-session. It MUST be called from inside the
    caller's compute slot: it is a full parse of a caller-chosen structure,
    and running it outside the bound left ~0.8 CPU-s of every anonymous
    analysis unmetered.
    """
    try:
        index = json.loads(
            (job_dir / _CHAIN_INDEX_NAME).read_text(encoding="utf-8")
        )
        count = index.get(chain_id)
        if isinstance(count, int):
            return count
    except (OSError, ValueError, AttributeError):
        pass

    try:
        for chain in parse_pdb(pdb_path).chains:
            if chain.id == chain_id:
                return chain.residue_count
    except Exception:
        logger.debug(
            "Chain residue count unavailable for %s", pdb_path, exc_info=True
        )
    return None


def _get_binder_overlaps(job_dir: Path, epitope_residues: list[int]) -> list[dict]:
    cache_path = job_dir / "analyze_cache.json"
    if not cache_path.exists():
        return []

    try:
        with cache_path.open() as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    epitope_set = set(epitope_residues)
    overlaps = []
    for binder in cache.get("known_binders", []):
        contacts = set(binder.get("contact_residues", []))
        overlap = epitope_set & contacts
        if overlap:
            overlaps.append({
                "pdb_id": binder.get("pdb_id", ""),
                "binder_type": binder.get("binder_type", ""),
                "species": binder.get("species", ""),
                "resolution": binder.get("resolution"),
                "affinity": binder.get("affinity", ""),
                "overlap_count": len(overlap),
                "overlap_residues": sorted(overlap),
                "total_contacts": len(contacts),
            })
    return overlaps


# ---------------------------------------------------------------------------
# Landing + quota
# ---------------------------------------------------------------------------

@scout_bp.route("/", methods=["GET"])
def index():
    return render_template("scout/index.html"), 200


@scout_bp.route("/quota", methods=["GET"])
def quota_json():
    email = session.get("user_email", "")
    if not email:
        return jsonify({
            "tier": "anon",
            "runs_used": 0,
            "runs_cap": FREE_TIER_RUN_CAP,
            "runs_remaining": FREE_TIER_RUN_CAP,
            "unlimited": False,
        }), 200
    return jsonify(quota_status(email)), 200


# ---------------------------------------------------------------------------
# Upload + fetch + example
# ---------------------------------------------------------------------------

@scout_bp.route("/upload", methods=["POST"])
@anon_rate_limit(
    "scout_intake",
    limit=ANON_INTAKE_LIMIT,
    window_seconds=ANON_RATE_WINDOW_SECONDS,
)
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file submitted."}), 400

    uploaded_file = request.files["file"]
    if not uploaded_file.filename:
        return jsonify({"error": "No file selected."}), 400

    suffix = Path(uploaded_file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return jsonify({
            "error": (
                f'Unsupported file type "{suffix}". '
                "Please upload a .pdb or .cif file."
            )
        }), 400

    cleanup_old_jobs()
    capacity_error = _anon_capacity_error()
    if capacity_error is not None:
        return jsonify(capacity_error[0]), capacity_error[1]

    job_id, job_dir = create_job_dir(_current_owner_key(mint=True))

    save_path = job_dir / f"input{suffix}"
    uploaded_file.save(str(save_path))

    oversized = _reject_oversized(save_path, job_dir)
    if oversized is not None:
        return jsonify(oversized[0]), oversized[1]

    result = parse_pdb(save_path)
    if result.error:
        # Drop the job rather than leaving an unparseable blob on disk for
        # the retention window: the only thing that makes an uploaded file
        # worth keeping is that Scout can read it.
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": result.error}), 422
    _save_chain_index(job_dir, result)

    structure_title = _extract_structure_title(save_path, suffix)

    return jsonify({
        "job_id": job_id,
        "filename": uploaded_file.filename,
        "chains": [
            {"id": chain.id, "residue_count": chain.residue_count, "name": chain.name}
            for chain in result.chains
        ],
        "structure_title": structure_title,
    }), 200


@scout_bp.route("/fetch-pdb", methods=["POST"])
@anon_rate_limit(
    "scout_intake",
    limit=ANON_INTAKE_LIMIT,
    window_seconds=ANON_RATE_WINDOW_SECONDS,
)
def fetch_pdb():
    import requests as http_requests  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    pdb_id = data.get("pdb_id", "").strip().upper()

    if not pdb_id or not re.match(r"^[A-Z0-9]{4}$", pdb_id):
        return jsonify({"error": "Please enter a valid 4-character PDB ID."}), 400

    download_urls = [
        (f"https://files.rcsb.org/download/{pdb_id}.pdb", ".pdb"),
        (f"https://files.rcsb.org/download/{pdb_id}.cif", ".cif"),
    ]

    # Bound the RCSB body the same way an upload is bounded. This route now
    # runs for anonymous callers, and some real PDB entries (whole ribosomes)
    # are hundreds of megabytes — reading one straight into memory would let a
    # 4-character request pull far more than the upload cap allows.
    size_cap = MAX_UPLOAD_BYTES if _signed_in_owner_key() else ANON_MAX_UPLOAD_BYTES
    content = None
    suffix = ".pdb"
    for url, ext in download_urls:
        try:
            with http_requests.get(url, timeout=30, stream=True) as resp:
                if resp.status_code != 200:
                    continue
                chunks: list[bytes] = []
                total = 0
                over_cap = False
                for chunk in resp.iter_content(65536):
                    total += len(chunk)
                    if total > size_cap:
                        over_cap = True
                        break
                    chunks.append(chunk)
            if over_cap:
                return jsonify({
                    "error": (
                        f'PDB entry "{pdb_id}" is larger than the '
                        f"{size_cap // (1024 * 1024)} MB limit. Download it, cut "
                        "it down to the chains you care about, and upload that."
                    )
                }), 413
            if total > 100:
                content = b"".join(chunks)
                suffix = ext
                break
        except Exception:
            continue

    if content is None:
        return jsonify({
            "error": f"PDB ID \"{pdb_id}\" not found on RCSB. Check the ID and try again."
        }), 404

    cleanup_old_jobs()
    capacity_error = _anon_capacity_error()
    if capacity_error is not None:
        return jsonify(capacity_error[0]), capacity_error[1]

    job_id, job_dir = create_job_dir(_current_owner_key(mint=True))
    save_path = job_dir / f"input{suffix}"
    save_path.write_bytes(content)

    oversized = _reject_oversized(save_path, job_dir)
    if oversized is not None:
        return jsonify(oversized[0]), oversized[1]

    result = parse_pdb(save_path)
    if result.error:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": result.error}), 422
    _save_chain_index(job_dir, result)

    structure_title = _extract_structure_title(save_path, suffix)

    return jsonify({
        "job_id": job_id,
        "filename": f"{pdb_id}{suffix}",
        "chains": [
            {"id": chain.id, "residue_count": chain.residue_count, "name": chain.name}
            for chain in result.chains
        ],
        "structure_title": structure_title,
    }), 200


@scout_bp.route("/example", methods=["GET"])
@anon_rate_limit(
    "scout_intake",
    limit=ANON_INTAKE_LIMIT,
    window_seconds=ANON_RATE_WINDOW_SECONDS,
)
def example():
    example_src = Path(current_app.root_path) / "static" / "example" / "1HEW.pdb"
    if not example_src.exists():
        return jsonify({"error": "Example protein file not found on server."}), 500

    cleanup_old_jobs()
    capacity_error = _anon_capacity_error()
    if capacity_error is not None:
        return jsonify(capacity_error[0]), capacity_error[1]

    job_id, job_dir = create_job_dir(_current_owner_key(mint=True))
    dest = job_dir / "input.pdb"
    shutil.copy2(str(example_src), str(dest))

    result = parse_pdb(dest)
    if result.error:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": result.error}), 422
    _save_chain_index(job_dir, result)

    structure_title = _extract_structure_title(dest, ".pdb")

    return jsonify({
        "job_id": job_id,
        "filename": "1HEW.pdb",
        "chains": [
            {"id": chain.id, "residue_count": chain.residue_count, "name": chain.name}
            for chain in result.chains
        ],
        "structure_title": structure_title,
    }), 200


# ---------------------------------------------------------------------------
# Analyze + progress SSE
# ---------------------------------------------------------------------------

@scout_bp.route("/analyze", methods=["POST"])
@anon_rate_limit(
    "scout_analyze",
    limit=ANON_ANALYZE_LIMIT,
    window_seconds=ANON_RATE_WINDOW_SECONDS,
)
@requires_scout_quota
def analyze():
    data = request.get_json(silent=True) or {}
    # str() before strip(): these come straight from user JSON, so a non-string
    # scalar ({"job_id": 123}) raised AttributeError here -- before any handler
    # -- and the route answered an HTML 500 to a JSON caller. Reachable
    # anonymously: this route has no @login_required.
    job_id = str(data.get("job_id", "")).strip()
    chain_id = str(data.get("chain", "")).strip()

    if not job_id or not chain_id:
        return jsonify({"error": "job_id and chain are required."}), 400

    job_dir = _resolve_job_dir(job_id)
    if job_dir is None:
        return jsonify({"error": "Job not found or expired. Please re-upload your file."}), 404
    pdb_path = _find_input_file(job_dir)
    if pdb_path is None:
        return jsonify({"error": "Job not found or expired. Please re-upload your file."}), 404

    known_binders = []
    ppi_interfaces = []
    _chain_total = None
    with anon_compute_slot(ANON_MAX_CONCURRENT_RUNS) as _slot:
        if not _slot:
            return jsonify({"error": _BUSY_MESSAGE, "reason": REASON_BUSY}), 503
        try:
            csv_path_prelim = job_dir / "results.csv"
            if not csv_path_prelim.exists():
                from scout.pipeline import run_pipeline  # noqa: PLC0415
                run_pipeline(pdb_path, chain_id)

            known_binders = []
            uniprot_id = ""
            uniprot_name = ""
            uniprot_identity_pct = "unknown"
            from scout.epitope_db import fetch_known_binders, resolve_uniprot_id  # noqa: PLC0415
            uniprot_result = resolve_uniprot_id(pdb_path, chain_id)
            uniprot_id = uniprot_result["uniprot_id"]
            uniprot_name = uniprot_result["protein_name"]
            uniprot_identity_pct = uniprot_result["identity_pct"]
            logger.warning(
                "UniProt resolution: id=%s name=%s identity=%s",
                uniprot_id or "(empty)", uniprot_name or "(none)", uniprot_identity_pct,
            )
            if uniprot_id:
                try:
                    known_binders = fetch_known_binders(uniprot_id)
                    logger.warning(
                        "Known binder lookup for %s: %d binders found",
                        uniprot_id, len(known_binders),
                    )
                except Exception:
                    logger.exception("Known binder lookup failed for %s", uniprot_id)
                    known_binders = []

            from scout.interfaces import detect_interfaces  # noqa: PLC0415
            ppi_interfaces = detect_interfaces(pdb_path, chain_id)
        except ValueError as exc:
            if isinstance(exc, ScoutInputError):
                return jsonify({"error": _client_error(exc)}), 422
            # Not the uploader's fault, so do not answer 422 "check that the
            # PDB is valid" -- the same reasoning the FileNotFoundError clause
            # in feasibility_analyze already applies. And it is the only
            # surviving record, since the text is withheld.
            logger.exception("Pipeline error for job %s", job_id)
            return jsonify({"error": _GENERIC_ERROR}), 500
        except Exception:
            logger.exception("Pipeline error for job %s", job_id)
            return jsonify({"error": _GENERIC_ERROR}), 500

        # Inside the slot on purpose. This used to be a bare parse_pdb() after
        # the `with` block, so the most expensive fallback path in the route —
        # a full parse of a caller-chosen structure up to the 8 MB cap — ran
        # outside the concurrency bound and was invisible to it. Normally it
        # is now a small JSON read, because intake already knew the answer.
        _chain_total = _chain_residue_count(job_dir, pdb_path, chain_id)

    _MIN_COMPOSITE = 0.40
    _MIN_RESI_COUNT = 5
    _MAX_PATCH_FRACTION = 0.30

    all_rows = []
    all_epitopes = []
    csv_path = job_dir / "results.csv"
    if csv_path.exists():
        with csv_path.open(newline="") as csv_file:
            reader = csv_module.DictReader(csv_file)
            fieldnames = reader.fieldnames or []
            for row in reader:
                resi_nums = sorted(int(n) for n in re.findall(r'\d+', row.get("residues", "")))
                if len(resi_nums) > 1:
                    filtered = []
                    for k, num in enumerate(resi_nums):
                        prev_gap = abs(num - resi_nums[k - 1]) if k > 0 else 999
                        next_gap = abs(resi_nums[k + 1] - num) if k + 1 < len(resi_nums) else 999
                        if min(prev_gap, next_gap) <= 20:
                            filtered.append(num)
                    resi_nums = filtered if filtered else resi_nums
                filled_nums = []
                for k, num in enumerate(resi_nums):
                    filled_nums.append(num)
                    if k + 1 < len(resi_nums):
                        gap = resi_nums[k + 1] - num
                        if 1 < gap <= 4:
                            filled_nums.extend(range(num + 1, resi_nums[k + 1]))
                composite = float(row.get("composite_score", 0))
                rcount = int(row.get("residue_count", 0))
                all_rows.append(dict(row))
                all_epitopes.append({
                    "epitope_id": int(row.get("epitope_id", row.get("patch_id", 0))),
                    "residues": row.get("residues", ""),
                    "residue_numbers": filled_nums,
                    "residue_count": rcount,
                    "composite_score": composite,
                    "mean_rsa": float(row.get("mean_rsa", 0)),
                    "secondary_structure": row.get("secondary_structure", "loop"),
                    "centroid_x": float(row.get("centroid_x", 0)),
                    "centroid_y": float(row.get("centroid_y", 0)),
                    "centroid_z": float(row.get("centroid_z", 0)),
                    "_row": dict(row),
                })

    _max_resi = (
        int(_chain_total * _MAX_PATCH_FRACTION)
        if _chain_total is not None
        else None
    )
    _MIN_CENTROID_DIST = 15.0

    ranked_candidates = sorted(
        [e for e in all_epitopes
         if e["composite_score"] >= _MIN_COMPOSITE
         and e["residue_count"] >= _MIN_RESI_COUNT
         and (_max_resi is None or e["residue_count"] <= _max_resi)],
        key=lambda e: e["composite_score"],
        reverse=True,
    )

    top3 = []
    for candidate in ranked_candidates:
        cx = candidate.get("centroid_x", 0)
        cy = candidate.get("centroid_y", 0)
        cz = candidate.get("centroid_z", 0)
        too_close = False
        for selected in top3:
            sx = selected.get("centroid_x", 0)
            sy = selected.get("centroid_y", 0)
            sz = selected.get("centroid_z", 0)
            dist = ((cx - sx) ** 2 + (cy - sy) ** 2 + (cz - sz) ** 2) ** 0.5
            if dist < _MIN_CENTROID_DIST:
                too_close = True
                break
        if not too_close:
            top3.append(candidate)
        if len(top3) >= 3:
            break

    from scout.flags import compute_quality_flags, CSV_COLUMNS_ANNOTATED  # noqa: PLC0415

    _is_plddt = any(
        row.get("is_plddt", "0") == "1"
        for row in all_rows
    ) if all_rows else False

    _flag_chain_length = _chain_total or 0

    for e in all_epitopes:
        row = e["_row"]
        e["quality_flags"] = compute_quality_flags(
            secondary_structure=row.get("secondary_structure", "loop"),
            hydrophobicity=float(row.get("hydrophobicity", 0)),
            burial_raw=float(row.get("burial_raw", 0)),
            bfactor_score=float(row.get("bfactor_score", 0)),
            is_functional_site=False,
            residues_str=row.get("residues", ""),
            chain_length=_flag_chain_length,
            is_plddt=_is_plddt,
        )

    epitopes_annotated_path = job_dir / "epitopes_annotated.csv"
    if top3:
        with epitopes_annotated_path.open("w", newline="") as csv_file:
            writer = csv_module.DictWriter(csv_file, fieldnames=CSV_COLUMNS_ANNOTATED)
            writer.writeheader()
            for rank, epitope in enumerate(top3, start=1):
                row = epitope["_row"].copy()
                row["epitope_id"] = rank
                row["quality_flags"] = epitope["quality_flags"]
                writer.writerow(row)

    results_annotated_path = job_dir / "results_annotated.csv"
    with results_annotated_path.open("w", newline="") as csv_file:
        writer = csv_module.DictWriter(csv_file, fieldnames=CSV_COLUMNS_ANNOTATED)
        writer.writeheader()
        for e in all_epitopes:
            row = e["_row"].copy()
            row["quality_flags"] = e["quality_flags"]
            writer.writerow(row)

    epitopes_csv_path = job_dir / "epitopes.csv"
    if top3 and fieldnames:
        with epitopes_csv_path.open("w", newline="") as csv_file:
            writer = csv_module.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            for rank, epitope in enumerate(top3, start=1):
                row = epitope["_row"].copy()
                row["epitope_id"] = rank
                writer.writerow(row)

    for e in top3:
        e.pop("_row", None)
        e.pop("centroid_x", None)
        e.pop("centroid_y", None)
        e.pop("centroid_z", None)

    pdb_format = pdb_path.suffix.lstrip(".") if pdb_path else "pdb"

    analyze_cache = {
        "epitopes": top3,
        "known_binders": known_binders,
        "ppi_interfaces": ppi_interfaces,
        "chain": chain_id,
    }
    analyze_cache_path = job_dir / "analyze_cache.json"
    with analyze_cache_path.open("w") as _cf:
        json.dump(analyze_cache, _cf)

    _email = session.get("user_email", "")
    if _email:
        record_scout_run(
            _email,
            metadata={
                "job_id": job_id,
                "chain": chain_id,
                "uniprot_id": uniprot_id or None,
                # Which secondary-structure branch ran. Constant per run,
                # so read it off any row. Lets the fallback-vs-DSSP split
                # be answered from the ledger instead of Railway logs.
                "ss_method": all_rows[0].get("ss_method") if all_rows else None,
            },
        )

    return jsonify({
        "download_url": url_for("scout.download", job_id=job_id),
        "download_url_full": url_for("scout.download", job_id=job_id) + "?full=1",
        "pdb_url": url_for("scout.serve_pdb", job_id=job_id),
        "pdb_format": pdb_format,
        "chain": chain_id,
        "epitopes": top3,
        "known_binders": known_binders,
        "ppi_interfaces": ppi_interfaces,
        "uniprot_id": uniprot_id,
        "uniprot_name": uniprot_name,
        "sequence_identity_pct": uniprot_identity_pct,
    }), 200


@scout_bp.route("/pdb/<job_id>", methods=["GET"])
def serve_pdb(job_id):
    job_dir = _resolve_job_dir(job_id)
    if job_dir is None:
        return jsonify({"error": "Structure file not found. Please re-upload your file."}), 404
    input_path = _find_input_file(job_dir)
    if input_path is None:
        return jsonify({"error": "Structure file not found. Please re-upload your file."}), 404
    return send_file(str(input_path), mimetype="chemical/x-pdb")


@scout_bp.route("/download/<job_id>", methods=["GET"])
def download(job_id):
    job_dir = _resolve_job_dir(job_id)
    if job_dir is None:
        return jsonify({"error": "Results not found. Please run analysis first."}), 404

    full = request.args.get("full", "0") == "1"
    if full:
        csv_path = job_dir / "results_annotated.csv"
        fallback_path = job_dir / "results.csv"
        download_name = "all_patches.csv"
    else:
        csv_path = job_dir / "epitopes_annotated.csv"
        fallback_path = job_dir / "epitopes.csv"
        download_name = "top3_epitopes.csv"

    if not csv_path.exists():
        csv_path = fallback_path
    if not csv_path.exists():
        return jsonify({"error": "Results not found. Please run analysis first."}), 404
    return send_file(str(csv_path), as_attachment=True, download_name=download_name)


@scout_bp.route("/progress", methods=["GET"])
@anon_rate_limit(
    "scout_analyze",
    limit=ANON_ANALYZE_LIMIT,
    window_seconds=ANON_RATE_WINDOW_SECONDS,
    sse=True,
)
@requires_scout_quota
def progress():
    from flask import stream_with_context  # noqa: PLC0415

    job_id = request.args.get("job_id", "").strip()
    chain_id = request.args.get("chain", "").strip()

    job_dir = _resolve_job_dir(job_id) if job_id else None
    pdb_path = _find_input_file(job_dir) if job_dir else None

    if not job_id or not chain_id or pdb_path is None:
        def _error_stream():
            if not job_id or not chain_id:
                msg = "job_id and chain are required."
                reason = REASON_BAD_REQUEST
            else:
                msg = "Job not found or expired. Please re-upload your file."
                reason = REASON_JOB_EXPIRED
            yield "data: " + json.dumps({
                "stage": "error", "msg": msg, "reason": reason,
            }) + "\n\n"

        return current_app.response_class(
            _error_stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _generate():
        try:
            import gevent  # noqa: PLC0415,F401
            import gevent.queue as gqueue  # noqa: PLC0415
            _use_gevent = True
        except ImportError:
            _use_gevent = False

        def _run_worker(q):
            def callback(stage, pct):
                stage_labels = {
                    "parsing": "Parsing structure\u2026",
                    "sasa": "Calculating solvent accessibility\u2026",
                    "patches": "Clustering surface patches\u2026",
                    "scoring": "Scoring epitope geometry\u2026",
                    "ranking": "Finalising results\u2026",
                }
                q.put({"stage": stage, "pct": pct, "msg": stage_labels.get(stage, stage)})

            try:
                from scout.pipeline import run_pipeline  # noqa: PLC0415
                run_pipeline(pdb_path, chain_id, progress_callback=callback)

                q.put({"stage": "done", "pct": 100, "result": {
                    "download_url": url_for("scout.download", job_id=job_id),
                    "download_url_full": url_for("scout.download", job_id=job_id) + "?full=1",
                    "pdb_url": url_for("scout.serve_pdb", job_id=job_id),
                    "pdb_format": pdb_path.suffix.lstrip("."),
                    "chain": chain_id,
                }})
            except Exception as exc:
                logger.exception("SSE pipeline error for job %s", job_id)
                q.put({"stage": "error", "msg": _client_error(exc)})

        if _use_gevent:
            import gevent  # noqa: PLC0415
            import gevent.queue as gqueue  # noqa: PLC0415
            q = gqueue.Queue()
            gevent.spawn(_run_worker, q)
            while True:
                try:
                    event = q.get(timeout=15)
                except gqueue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("stage") in ("done", "error"):
                    break
        else:
            import queue as stdqueue  # noqa: PLC0415
            q = stdqueue.Queue()
            _run_worker(q)
            while not q.empty():
                event = q.get_nowait()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("stage") in ("done", "error"):
                    break

    def _slotted():
        """Hold an anonymous compute slot for as long as the stream runs.

        The slot is taken when the response starts being iterated and given
        back when the generator closes — including when the browser hangs up
        part-way, which raises GeneratorExit through the ``with``. Wrapping
        the generator rather than decorating the view is what makes that true:
        a decorator would release the slot the moment the view returned, well
        before the pipeline it is protecting had started.
        """
        with anon_compute_slot(ANON_MAX_CONCURRENT_RUNS) as slot:
            if not slot:
                # `reason` is what makes this separable from the per-IP 429:
                # EventSource cannot read a non-2xx body, so both refusals
                # leave here as HTTP 200 text/event-stream and are otherwise
                # identical apart from prose. See scout/ratelimit.py.
                busy = {
                    "stage": "error",
                    "msg": _BUSY_MESSAGE,
                    "reason": REASON_BUSY,
                }
                yield f"data: {json.dumps(busy)}\n\n"
                return
            yield from _generate()

    return current_app.response_class(
        stream_with_context(_slotted()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Feasibility
# ---------------------------------------------------------------------------

@scout_bp.route("/feasibility", methods=["GET"])
@login_required
def feasibility_page():
    job_id = request.args.get("job_id", "")
    epitope_id = request.args.get("epitope_id", "")
    return render_template("scout/feasibility.html", job_id=job_id, epitope_id=epitope_id)


@scout_bp.route("/feasibility/analyze", methods=["POST"])
@login_required
def feasibility_analyze():
    from scout.feasibility import generate_recommendations  # noqa: PLC0415
    from scout.pipeline import run_feasibility_pipeline  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    # str() before strip() -- see analyze(); same user-JSON crash, same fix.
    job_id = str(data.get("job_id", "")).strip()
    chain_id = str(data.get("chain", "")).strip()

    if not job_id or not chain_id:
        return jsonify({"error": "job_id and chain are required."}), 400

    job_dir = _resolve_job_dir(job_id)
    if job_dir is None:
        return jsonify({"error": "Job not found or expired. Please re-upload."}), 404
    pdb_path = _find_input_file(job_dir)
    if pdb_path is None:
        return jsonify({"error": "Job not found or expired. Please re-upload."}), 404

    epitope_residues = data.get("epitope_residues", [])
    epitope_id = data.get("epitope_id")

    if not epitope_residues and epitope_id is not None:
        results_csv = job_dir / "results.csv"
        if not results_csv.exists():
            return jsonify({"error": "No Epitope Scout results found for this job. Run epitope analysis first."}), 404

        try:
            epitope_id = int(epitope_id)
            with results_csv.open() as f:
                reader = csv_module.DictReader(f)
                for row in reader:
                    if int(row.get("epitope_id", 0)) == epitope_id:
                        residues_str = row.get("residues", "")
                        for token in residues_str.split(","):
                            token = token.strip()
                            num = re.sub(r"[^0-9\-]", "", token)
                            if num:
                                epitope_residues.append(int(num))
                        break
        except (ValueError, KeyError, TypeError):
            # TypeError: int() on a non-scalar epitope_id straight from user
            # JSON. This block sits OUTSIDE the main try below, so without it
            # the route answers an HTML 500 -- the exact failure this change
            # set out to close. The message is gated for the same reason as
            # every other client-facing string here.
            logger.warning("Epitope parse failed for job %s", job_id, exc_info=True)
            # A flat string rather than _GENERIC_ERROR: it names the actual
            # failure. int() raises a plain ValueError quoting the caller's own
            # input ("invalid literal for int() with base 10: 'QCPROBE'"), which
            # _client_error would now replace anyway -- but with text about the
            # PDB being invalid, which this is not.
            return jsonify({
                "error": "Could not read that epitope from the stored results.",
            }), 400

    if not epitope_residues:
        return jsonify({"error": "epitope_residues or epitope_id is required."}), 400

    try:
        feasibility_csv = run_feasibility_pipeline(
            pdb_path, chain_id, epitope_residues,
        )
    except FileNotFoundError:
        # A staged file is missing server-side. Answering 422 "check that the
        # PDB is valid" blamed the user for the server losing the job; it is
        # gone, and re-uploading is the actionable instruction. The path is
        # the whole diagnostic, so it is logged rather than sent.
        logger.warning(
            "Feasibility staged file missing for job %s", job_id, exc_info=True
        )
        return jsonify({"error": "Job not found or expired. Please re-upload."}), 404
    except ValueError as exc:
        if isinstance(exc, ScoutInputError):
            # Forwarded verbatim, so nothing is hidden that a traceback would
            # help reconstruct -- and a wrong-chain typo is the most common
            # user error on this route, so exc_info would be pure log noise.
            logger.warning("Feasibility rejected input for job %s: %s", job_id, exc)
            return jsonify({"error": _client_error(exc)}), 422
        # Withheld, so the traceback is the only surviving account of it -- and
        # 500, not 422, because a plain ValueError is a server fault. Blaming
        # the upload here is the mistake the FileNotFoundError clause above
        # was written to avoid.
        logger.exception("Feasibility pipeline fault for job %s", job_id)
        return jsonify({"error": _GENERIC_ERROR}), 500
    except Exception:
        # Anything else used to escape as an unhandled 500 with a stack-trace
        # page. The route contracts to return JSON; the UI only reaches it
        # after the SSE stream reports done, but it is directly callable.
        # _GENERIC_ERROR directly, not _client_error: the ValueError clause
        # above catches every ValueError by isinstance, so a forward from here
        # is unreachable and only reads as though it were possible.
        logger.exception("Feasibility pipeline error for job %s", job_id)
        return jsonify({"error": _GENERIC_ERROR}), 500

    result_row = {}
    with feasibility_csv.open() as f:
        reader = csv_module.DictReader(f)
        for row in reader:
            result_row = row
            break

    dimensions = {
        "surface_topology": float(result_row.get("surface_topology", 0)),
        "epitope_rigidity": float(result_row.get("epitope_rigidity", 0)),
        "geometric_access": float(result_row.get("geometric_access", 0)),
        "glycan_risk": float(result_row.get("glycan_risk", 0)),
        "interface_competition": float(result_row.get("interface_competition", 0)),
    }

    composite = float(result_row.get("composite_feasibility", 0))
    tier = result_row.get("tier", "Unknown")
    result = generate_recommendations(dimensions, composite, tier, len(epitope_residues))

    return jsonify({
        "composite_feasibility": composite,
        "tier": result.tier,
        "tier_color": result.tier_color,
        "dimensions": dimensions,
        "dimension_descriptions": result.dimension_descriptions,
        "recommended_approach": result.recommended_approach,
        "recommended_scaffold": result.recommended_scaffold,
        "design_scale_min": result.design_scale_min,
        "design_scale_max": result.design_scale_max,
        "expected_hit_rate": result.expected_hit_rate,
        "hit_rate_citation": result.hit_rate_citation,
        "risk_factors": result.risk_factors,
        "residues": result_row.get("residues", ""),
        "residue_count": int(result_row.get("residue_count", 0)),
        "download_url": url_for("scout.feasibility_download", job_id=job_id),
        "pdb_url": url_for("scout.serve_pdb", job_id=job_id),
        "pdb_format": pdb_path.suffix.lstrip("."),
        "chain": chain_id,
        "known_binder_overlaps": _get_binder_overlaps(job_dir, epitope_residues),
    }), 200


@scout_bp.route("/feasibility/progress", methods=["GET"])
@login_required
def feasibility_progress():
    from flask import stream_with_context  # noqa: PLC0415

    job_id = request.args.get("job_id", "").strip()
    chain_id = request.args.get("chain", "").strip()
    epitope_str = request.args.get("epitope_residues", "").strip()
    epitope_id = request.args.get("epitope_id", "").strip()

    job_dir = _resolve_job_dir(job_id) if job_id else None
    pdb_path = _find_input_file(job_dir) if job_dir else None

    if not job_id or not chain_id or pdb_path is None:
        def _error_stream():
            msg = "job_id and chain are required." if not job_id or not chain_id else "Job not found or expired."
            yield f"data: {json.dumps({'stage': 'error', 'msg': msg})}\n\n"
        return current_app.response_class(
            _error_stream(), mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    epitope_residues: list[int] = []
    if epitope_str:
        epitope_residues = [int(x.strip()) for x in epitope_str.split(",") if x.strip().lstrip("-").isdigit()]
    elif epitope_id:
        results_csv = job_dir / "results.csv"
        if results_csv.exists():
            try:
                eid = int(epitope_id)
                with results_csv.open() as f:
                    reader = csv_module.DictReader(f)
                    for row in reader:
                        if int(row.get("epitope_id", 0)) == eid:
                            for token in row.get("residues", "").split(","):
                                num = re.sub(r"[^0-9\-]", "", token.strip())
                                if num:
                                    epitope_residues.append(int(num))
                            break
            except (ValueError, KeyError):
                pass

    if not epitope_residues:
        def _err():
            yield f"data: {json.dumps({'stage': 'error', 'msg': 'No epitope residues specified.'})}\n\n"
        return current_app.response_class(
            _err(), mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _generate():
        try:
            import gevent  # noqa: PLC0415,F401
            import gevent.queue as gqueue  # noqa: PLC0415
            _use_gevent = True
        except ImportError:
            _use_gevent = False

        def _run_worker(q):
            def callback(stage, pct):
                stage_labels = {
                    "parsing": "Parsing structure\u2026",
                    "sasa": "Calculating solvent accessibility\u2026",
                    "bfactor": "Computing rigidity scores\u2026",
                    "topology": "Analyzing surface topology\u2026",
                    "accessibility": "Evaluating geometric accessibility\u2026",
                    "glycan": "Detecting glycosylation sites\u2026",
                    "interfaces": "Detecting protein interfaces\u2026",
                    "scoring": "Computing feasibility score\u2026",
                }
                q.put({"stage": stage, "pct": pct, "msg": stage_labels.get(stage, stage)})

            try:
                from scout.pipeline import run_feasibility_pipeline  # noqa: PLC0415
                run_feasibility_pipeline(pdb_path, chain_id, epitope_residues, progress_callback=callback)
                q.put({"stage": "done", "pct": 100, "result": {
                    "job_id": job_id,
                    "chain": chain_id,
                }})
            except Exception as exc:
                logger.exception("Feasibility SSE error for job %s", job_id)
                q.put({"stage": "error", "msg": _client_error(exc)})

        if _use_gevent:
            import gevent  # noqa: PLC0415
            import gevent.queue as gqueue  # noqa: PLC0415
            q = gqueue.Queue()
            gevent.spawn(_run_worker, q)
            while True:
                try:
                    event = q.get(timeout=15)
                except gqueue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("stage") in ("done", "error"):
                    break
        else:
            import queue as stdqueue  # noqa: PLC0415
            q = stdqueue.Queue()
            _run_worker(q)
            while not q.empty():
                event = q.get_nowait()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("stage") in ("done", "error"):
                    break

    return current_app.response_class(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@scout_bp.route("/feasibility/download/<job_id>", methods=["GET"])
@login_required
def feasibility_download(job_id):
    job_dir = _resolve_job_dir(job_id)
    if job_dir is None:
        return jsonify({"error": "Feasibility results not found. Run analysis first."}), 404
    csv_path = job_dir / "feasibility_results.csv"
    if not csv_path.exists():
        return jsonify({"error": "Feasibility results not found. Run analysis first."}), 404

    return send_file(
        str(csv_path),
        as_attachment=True,
        download_name=f"feasibility_{job_id[:8]}.csv",
        mimetype="text/csv",
    )


# ---------------------------------------------------------------------------
# Scout -> Tools-hub handoff
# ---------------------------------------------------------------------------

# Re-exported from the leaf module so the tools blueprint can read the
# same set without importing this one. See scout/handoff.py for why.
from scout.handoff import VALID_HANDOFF_TOOLS  # noqa: E402,PLC0415


@scout_bp.route("/handoff/tool", methods=["POST"])
@login_required
def handoff_to_tool():
    from scout.handoff import create_handoff, handoff_redirect_url  # noqa: PLC0415

    tool = (request.form.get("tool") or "").strip().lower()
    scout_job_id = (request.form.get("scout_job_id") or "").strip()
    target_chain = (request.form.get("target_chain") or "A").strip() or "A"
    hotspots_raw = (request.form.get("hotspot_residues") or "").strip()
    scout_epitope_id = (request.form.get("scout_epitope_id") or "").strip() or None

    if tool not in VALID_HANDOFF_TOOLS:
        return jsonify({"error": f"Unknown tool: {tool}"}), 400
    if not scout_job_id:
        return jsonify({"error": "scout_job_id is required"}), 400

    # Validate + confine + ownership-check before touching the filesystem,
    # then hand the confined PDB path to create_handoff so it never builds
    # a path from the raw, user-supplied scout_job_id.
    job_dir = _resolve_job_dir(scout_job_id)
    if job_dir is None:
        return jsonify({"error": "Scout job not found or expired."}), 404
    input_pdb = job_dir / "input.pdb"

    hotspots: list[int] = []
    if hotspots_raw:
        try:
            hotspots = [
                int(tok.strip())
                for tok in hotspots_raw.split(",")
                if tok.strip()
            ]
        except ValueError:
            return jsonify({"error": "hotspot_residues must be integers"}), 400
    if not hotspots:
        return jsonify({"error": "At least one hotspot residue is required"}), 400

    email = session.get("user_email", "")
    handoff_id = create_handoff(
        user_email=email,
        scout_job_id=scout_job_id,
        target_chain=target_chain,
        hotspot_residues=hotspots,
        scout_epitope_id=scout_epitope_id,
        pdb_path=input_pdb,
    )
    if not handoff_id:
        return (
            jsonify({
                "error": (
                    "Could not stage handoff. Make sure the Scout run "
                    "still has its PDB, and try again."
                )
            }),
            500,
        )
    return redirect(handoff_redirect_url(tool, handoff_id))
