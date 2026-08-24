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
from shared.metrics import observe_scout_refusal

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
    ANON_SESSION_KEY,
    PAIR_CLOSES,
    PAIR_OPENS,
    REASON_AT_CAPACITY,
    REASON_BAD_REQUEST,
    REASON_BUSY,
    REASON_JOB_EXPIRED,
    anon_compute_slot,
    anon_rate_limit,
    job_id_in_body,
    job_id_in_query,
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

# Fixed windows. Ten minutes is long enough that a burst of retries after a
# bad file does not lock a real user out for the rest of the session.
ANON_RATE_WINDOW_SECONDS = 600

# Intake (upload / fetch-pdb / example) — 10 structures per 10 minutes. A real
# first visit uses 1-3.
#
# No per-session tier here, deliberately: intake already has one, and a better
# one. ANON_MAX_LIVE_JOBS_PER_SESSION caps a single session at 5 live jobs,
# which bounds the resource intake actually spends (disk) rather than the
# request count.
#
# THIS IS NOW THE FIRST WALL A REAL LAB MEETS, which is the opposite of what
# this comment said until 2026-08-19. Once one analysis cost one charge instead
# of two, the analyze ceiling below stopped being the binding one: QC measured
# ten researchers behind one NAT getting through, and the ELEVENTH refused
# here, on GET /scout/example, before it ever reached an analysis. Phase 5
# instruments the refusal a visitor actually sees, so it has to instrument this
# one too, not only the analyze bucket.
#
# The 10 == ANON_ANALYZE_LIMIT balance is ACCIDENTAL and nothing asserts it.
# Neither number moved; what moved was the cost of an analysis, which lifted
# the effective analyze ceiling from 5 to 10 and landed on this 10 by
# coincidence. Change either one and the wall moves to the other, silently.
ANON_INTAKE_LIMIT = 10

# ---------------------------------------------------------------------------
# The analysis ceiling — PER IP. This is the true bound; read before changing.
# ---------------------------------------------------------------------------
#
# One analysis is now ONE hit, not two. The page runs a GET /scout/progress
# (the SSE stream that executes the pipeline) and then a POST /scout/analyze
# (finalise), and they share a single charge through the follow-up credit in
# scout/ratelimit.py. Until 2026-08-18 they were billed separately, so this
# number bought HALF as many analyses as it reads.
#
# THE CPU ARITHMETIC, from Phase 0's measurements. A 10-minute window on this
# box is 2 sync workers x 600 s ~= 1,200 CPU-s.
#
#   adversarial /progress  ~9 CPU-s   (run_pipeline at the 8 MB upload cap)
#   adversarial /analyze   ~6 CPU-s   (binder lookup ~4.2 + interfaces ~1)
#   adversarial analysis  ~15 CPU-s   (the pair)
#
#   before: 20 hits/IP fleet-wide, all aimed at /progress = ~180 CPU-s/IP,
#           so ~7 addresses saturate the fleet, and 5 analyses per worker.
#   now:    20 hits/IP fleet-wide, each buying a whole analysis = ~300
#           CPU-s/IP, so ~4 addresses saturate it, and 10 per worker.
#
# THAT INCREASE IS REAL AND IS THE PRICE OF THE FIX. Charging once per
# analysis necessarily lets one charge buy a whole analysis instead of half of
# one; there is no version of "bill it once" that does not. It is stated here
# rather than buried because the lever is one line: dropping this to 6 puts
# per-IP worst-case CPU back at ~180 exactly, at the cost of 6 analyses per
# worker instead of 10.
#
# WHY IT IS NOT BEING RAISED, which the plan's Phase 4 asks for. ONE
# precondition, unmet — and a second consideration that used to be written
# here as a precondition and is not one. See
# docs/DECISION-2026-08-22-per-ip-ceiling.md for the full argument.
#
#  1. PHASE 2 -- ANSWERED 2026-08-24, this is no longer the gate. Railway's
#     edge OVERWRITES the whole X-Forwarded-* family and then appends its own
#     internal hop, and that hop ROTATES across a pool. So the per-IP key was
#     edge-internal noise and this ceiling never bound anyone; it now keys on
#     X-Real-Ip and binds for the first time. Re-read the ceiling decision in
#     that light. See docs/MEASUREMENT-2026-08-24-per-ip-key-is-not-stable.md.
#     The original text follows, kept because its reasoning is still the
#     reason the answer mattered.
#
#     Nobody has verified whether
#     Railway's edge appends, overwrites or forwards X-Forwarded-For
#     verbatim. Under the third case the per-IP key is caller-chosen and this
#     ceiling is decorative. Measured at d3c60c8, 50 requests to
#     GET /scout/example from ONE socket peer, one worker, each carrying a
#     different single-value X-Forwarded-For: 50 admitted, 0 refused. With a
#     FIXED header, or none at all: 10 admitted, 40 refused.
#
#     BUT THAT PROBE ONLY SIMULATES THE THIRD CASE, and saying otherwise is
#     circular. _client_ip counts hops from the RIGHT, so a forged value wins
#     only when the app RECEIVES a single-value header. If the edge appends,
#     the app sees "<forged>, <real client>" and takes the real one; if it
#     overwrites, the forged value never arrives. Both were run: 10 admitted,
#     40 refused, same as an honest caller.
#
#     So the gate is NOT "the ceiling is provably bypassed" — it is that
#     nobody knows which of the three holds, so nobody can say whether this
#     number bounds an attacker at all. A control whose effectiveness is
#     unknown cannot be sized. That is the precondition.
#
#  NOT a precondition, though this comment used to say it was: PHASE 1'S
#  FAIRNESS. A semaphore bounds how many requests run AT ONCE. This ceiling
#  bounds how much CPU one address can DEMAND IN A WINDOW. If N addresses
#  each demand D CPU-s, the fleet must supply N x D however many are in
#  flight — a concurrency cap changes the queueing discipline, not the
#  arithmetic. So Phase 1 cannot make a raised ceiling safe and its absence
#  cannot make one unsafe; the plan's own "What Phase 4 must NOT do" says so
#  and this comment contradicted it.
#
#  What Phase 1 governs is the CONSEQUENCE of saturation, not its threshold:
#  without it a saturated sync fleet does not shed, it queues invisibly with
#  /healthz behind it, so going over budget presents as an outage rather than
#  as some anonymous 503s. Size this number against the CPU budget; require
#  Phase 1 before accepting any ceiling whose WORST case exceeds it.
#
# AND RAISING THIS ALONE WOULD NOT UNBLOCK A LAB. ANON_INTAKE_LIMIT above is
# also 10, and which one binds depends on the shape of the lab, not its size:
# many researchers with one structure each hit INTAKE at the 11th structure,
# before an analysis is ever reached; few researchers with many chains each
# hit this one at the 11th analysis. Both were measured. Move one without the
# other and half the users this is meant to serve are refused at exactly the
# same point as today.
#
# UNITS, because this block mixes them and ratelimit.py's house rule is to
# quote the doubled numbers. The two "11th" counts above are PER WORKER, like
# the limits themselves. Doubling them is the CAPACITY, not the wall index:
# measured on a 2-worker fleet, one IP gets 20 admitted and is refused at
# request 21 — NOT 22, which is what doubling the index would give and what
# this comment said until it was measured. That matches ratelimit.py's "20 per
# 10 min per IP across a 2-worker fleet". The same per-worker/fleet-wide split
# applies to ANON_INTAKE_LIMIT's "ELEVENTH refused here" note above.
#
# The CPU rows further up are FLEET-WIDE. The saturation figure is unaffected
# either way — addresses = (W x 600) / (W x C x 15) = 40 / C, in which the
# worker count cancels, so raising WEB_CONCURRENCY does NOT lower the number of
# addresses it takes to saturate the box.
#
# It does, however, lift BOTH walls together — intake and analyze multiply by W
# alike, measured 10/20/30 admitted at W = 1/2/3 — which is the one thing
# changing either constant alone cannot do (see the paragraph above). It is not
# free: the attacker's quota multiplies by W too, so it is attacker-NEUTRAL
# only while vCPU scales with W, and Railway's allocation is an explicit
# unknown. Stated in full in docs/DECISION-2026-08-22-per-ip-ceiling.md §4.
ANON_ANALYZE_LIMIT = 10

# ...and PER SESSION, keyed on the anonymous id in the signed session cookie.
# TIGHT, and the only limit an ordinary visitor should ever meet.
#
# Phase 0's baseline PROJECTS a thorough first-time visitor at 6 analyses in
# a session — two uploads, several chains, since trying chains is the whole
# point of the tool (docs/qc/anon-load-baseline.md §2.3, "Thorough: 2 uploads,
# 6 analyses", whose stated design input is to "size for ~6 runs per user
# session, not 1-3"). Eight leaves that user 33% of headroom while stopping a
# runaway tab or a hand-rolled loop at 8 rather than letting it spend all 10 of
# the shared per-IP allowance its whole institution draws from.
#
# THAT 6 IS MODELLED, NOT MEASURED, and this comment said "QC measured" until
# 2026-08-18. Two things were wrong with that. anon-load-baseline.md is the
# BUILDER's own Phase 0 measurement document, not a QC one — the Phase 0 QC
# report adopts its §2.3 as a concern without re-deriving it. And §2.3 itself
# is a behavioural projection: §2.1 and §2.2 measured the COST PER ACTION
# against a real server (one analyse click = 2 metered hits), never how many
# actions a visitor takes. Nobody has observed a real session. The cap of 8 is
# a faithful application of Phase 0's own stated design input and does not need
# re-deriving; what needed fixing is a provenance claim that would let a later
# phase treat a model as data.
#
# It bounds nothing an attacker cares about — a cookie is free to discard —
# and it is not supposed to. Its jobs are to make ordinary over-use cheap to
# refuse and to give Phase 5 a refusal it can honestly answer with "sign in",
# which is true here and is NOT true of the per-IP refusal.
#
# HONEST LIMITATION: with the per-IP ceiling stuck at 10/worker there is very
# little room between the tiers, so this one is thin. It becomes the real
# workhorse when Phase 2 lets the per-IP number rise; the structure is what
# makes that a number change rather than a redesign.
ANON_ANALYZE_SESSION_LIMIT = 8

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

# Session key holding the anonymous owner id — imported from scout.ratelimit,
# which needs it to key the per-session tier and cannot import this module
# back. Re-exported here so scout_routes.ANON_SESSION_KEY still resolves.
# Prefixed so it can never collide with a Supabase uid or an email address.
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
        observe_scout_refusal(REASON_AT_CAPACITY)
        return {
            "error": (
                "Epitope Scout is at capacity for anonymous runs right now. "
                "Try again in a few minutes, or sign in to run it on your account."
            ),
            "reason": REASON_AT_CAPACITY,
        }, 503
    anon = session.get(ANON_SESSION_KEY)
    if anon and count_job_dirs(anon) >= ANON_MAX_LIVE_JOBS_PER_SESSION:
        # Same reason as the branch above, DIFFERENT status code (429 vs 503).
        # One refusal, counted once, which a status-code metric could not do.
        observe_scout_refusal(REASON_AT_CAPACITY)
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


# A chain id is whatever byte PDB column 22 (or an mmCIF auth_asym_id) holds,
# and parse_pdb hands it to the dropdown untouched, so ``_``, ``-``, ``.``,
# ``=`` and ``@`` are all ids this app itself offers. Anything stricter refuses
# a structure the user just uploaded. Reject only what is unsafe to carry into
# a CSV cell or a log line; the cap sits far above any real auth_asym_id.
#
# Control characters are not what stops SSE frame forging — every emitter here
# builds its payload with json.dumps, which escapes newlines on its own.
_CHAIN_ID_MAX_LEN = 64


def _valid_chain(chain_id: str) -> bool:
    return (
        bool(chain_id)
        and len(chain_id) <= _CHAIN_ID_MAX_LEN
        and all(ch >= " " and ch != "\x7f" for ch in chain_id)
    )


def _results_csv_for_chain(job_dir: Path, chain_id: str) -> "Path | None":
    """``results.csv`` for this job, but only if it holds *chain_id*'s scores.

    The file is written per job directory, not per chain, so its existence says
    nothing about which chain was scored. Treating it as a cache key is what
    made ``/scout/analyze`` answer a request for chain B with chain A's
    epitopes: HTTP 200, no pipeline run, no visible signal. Returning None on a
    mismatch turns that into a cache miss, which costs a rescore and nothing
    else.

    Route every chain-resolving reader through here. ``download()`` is the
    deliberate exception — it takes no chain at all, and is kept honest from the
    other end by ``_remove_derived_result_files``.

    A CSV with no ``chain_id`` column (written before this stamp existed) and
    one with no data rows are both misses: neither can name its chain.
    """
    if _results_csv_chain_id(job_dir) != chain_id:
        return None
    return job_dir / "results.csv"


def _remove_derived_result_files(job_dir: Path) -> None:
    """Invalidate the three DOWNLOADABLE files derived from ``results.csv``.

    Once results.csv is rewritten for another chain these three describe a chain
    that is no longer there, and ``/scout/download`` takes no chain parameter,
    so it hands back whatever it finds. ``analyze_cache.json`` is excluded on
    purpose: it stamps its own chain and ``_get_binder_overlaps`` checks it.

    **Call this immediately after every ``run_pipeline``** — at the rewrite, not
    at the readers, because run_pipeline has TWO callers and ``/scout/progress``
    is the one that actually executes the pipeline and hands the browser a
    download_url.

    Best-effort: a file that will not delete must not take a successful scoring
    run down with it. Windows raises WinError 32 whenever a preceding
    /scout/download still holds the handle open.
    """
    for name in ("epitopes.csv", "epitopes_annotated.csv", "results_annotated.csv"):
        _unlink_quietly(job_dir / name)


def _unlink_quietly(path: Path) -> None:
    """Delete a derived result file, or log why it could not be deleted.

    Every delete of a derived file goes through here, so the PermissionError
    Windows raises on a still-open handle cannot 500 the view.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "Could not invalidate derived result file %s; /scout/download may "
            "serve a stale copy of it until the next successful run.",
            path,
        )


def _results_csv_chain_id(job_dir: Path) -> "str | None":
    """Which chain ``results.csv`` holds, or None if it cannot say.

    None covers every way the file fails to name a chain — absent, unreadable,
    header-only, or written before the ``chain_id`` stamp existed. All of them
    mean "no cached result for anyone", never "some other chain's result".

    Kept separate from ``_results_csv_for_chain`` so ``/scout/analyze`` can tell
    a cross-chain collision (a stamp naming a DIFFERENT chain) from a run that
    simply scored nothing; those need different answers.
    """
    csv_path = job_dir / "results.csv"
    if not csv_path.exists():
        return None
    try:
        with csv_path.open(newline="") as csv_file:
            first_row = next(csv_module.DictReader(csv_file), None)
    except (OSError, csv_module.Error, UnicodeDecodeError):
        return None
    if first_row is None:
        return None
    # A blank chain_id names no chain: fold it in with the can't-say cases
    # rather than letting it compare unequal to every real chain.
    return first_row.get("chain_id") or None


def _get_binder_overlaps(
    job_dir: Path, epitope_residues: list[int], chain_id: str
) -> list[dict]:
    """Known binders that contact *epitope_residues*, for *chain_id* only.

    The cache has always recorded which chain it was built for and nothing read
    it. Feasibility called with explicit ``epitope_residues`` skips the
    results.csv gate, so this was the last path by which one chain's binders
    could be reported against another chain's epitope — residue numbers collide
    across chains routinely.
    """
    cache_path = job_dir / "analyze_cache.json"
    if not cache_path.exists():
        return []

    try:
        with cache_path.open() as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []

    if cache.get("chain") != chain_id:
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
    session_limit=ANON_ANALYZE_SESSION_LIMIT,
    window_seconds=ANON_RATE_WINDOW_SECONDS,
    # Finalise. In the normal flow /progress has already run the pipeline and
    # paid for the whole analysis, so this spends that credit. When there is
    # no credit — called on its own, or called a second time on the same job —
    # it is charged like anything else, which matters because THIS route can
    # run the pipeline itself when results.csv is missing (a dropped SSE
    # stream), and does the binder lookup and interface detection every time.
    pair=PAIR_CLOSES,
    # The meter must key its credit on the job THIS VIEW runs, so it calls the
    # very function the first line of the body calls. Reading the job id from
    # anywhere else here — or letting the meter fall back to another source —
    # is the diversion described in scout/ratelimit.py: a query string on this
    # POST once keyed the credit on one job while the pipeline ran on another.
    job_id=job_id_in_body,
)
@requires_scout_quota
def analyze():
    job_id = job_id_in_body()
    data = request.get_json(silent=True) or {}
    # str() before strip(): these come straight from user JSON, so a non-string
    # scalar ({"chain": 123}) raised AttributeError here -- before any handler
    # -- and the route answered an HTML 500 to a JSON caller. Reachable
    # anonymously: this route has no @login_required.
    #
    # ``job_id`` is NOT read here any more and does not need the same cast:
    # ``job_id_in_body`` above returns "" for a non-string instead of coercing
    # it, so the crash is closed there. Casting it here as well would be worse
    # than redundant -- the METER calls that same function, so a coerced
    # ``str(123)`` in the view and a "" in the meter would key the credit on
    # one job while the view ran another, which is the diversion above.
    chain_id = str(data.get("chain", "")).strip()

    if not job_id or not _valid_chain(chain_id):
        return jsonify({"error": "job_id and a valid chain id are required."}), 400

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
            observe_scout_refusal(REASON_BUSY)
            return jsonify({"error": _BUSY_MESSAGE, "reason": REASON_BUSY}), 503
        try:
            if _results_csv_for_chain(job_dir, chain_id) is None:
                from scout.pipeline import run_pipeline  # noqa: PLC0415
                run_pipeline(pdb_path, chain_id)
                _remove_derived_result_files(job_dir)

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
    csv_path = _results_csv_for_chain(job_dir, chain_id)
    if csv_path is None:
        # The pipeline either just ran for this chain or its results were
        # already on disk, so a miss here is not normal. Falling through would
        # read zero epitopes, conclude "nothing qualifying", DELETE the derived
        # epitopes*.csv, truncate results_annotated.csv to a header and serve
        # that as a 200.
        #
        # A stamp naming a different chain means a concurrent request overwrote
        # the file mid-run, and retrying resolves it. No stamp means this run
        # produced no scoreable rows — a different problem with a different fix.
        stamped = _results_csv_chain_id(job_dir)
        if stamped is not None:
            # A concurrent run on chain `stamped` owns results.csv. Touch
            # nothing beside it — deleting another live run's output is
            # destructive, and what it has rebuilt is not knowable from here.
            return jsonify({
                "error": "Another analysis on this job replaced the results "
                         "while this one was running. Please run it again."
            }), 409

        # Nothing owns results.csv, so this run scored nothing. No cleanup here:
        # the run_pipeline above already invalidated the derived files, which is
        # the only place that rule lives.
        return jsonify({
            "error": f"No surface patches could be scored for chain {chain_id}. "
                     "Try a different chain, or check the structure has resolved "
                     "side chains for that chain."
        }), 422

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

    # Both top-3 files are rewritten or removed on every run that reaches here;
    # earlier returns are covered beside run_pipeline instead. A chain that
    # scores nothing qualifying used to skip the write and leave the PREVIOUS
    # chain's file for /scout/download — the same job-scoped assumption as the
    # results.csv cache, one file further on.
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
    else:
        _unlink_quietly(epitopes_annotated_path)

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
    else:
        _unlink_quietly(epitopes_csv_path)

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
    session_limit=ANON_ANALYZE_SESSION_LIMIT,
    window_seconds=ANON_RATE_WINDOW_SECONDS,
    sse=True,
    # NOT a status poll. _run_worker below calls run_pipeline UNCONDITIONALLY
    # — there is no results.csv check, unlike /analyze — so every hit on this
    # route is a full scoring run on a caller-chosen structure, ~9 CPU-s at
    # the 8 MB cap. It is charged on EVERY call and PAIR_OPENS does not change
    # that; all it adds is one credit for the POST /scout/analyze that follows.
    # Removing this decorator, or letting this route spend a credit instead of
    # granting one, would make full-pipeline compute free. Do neither.
    pair=PAIR_OPENS,
    # Same rule as /scout/analyze, other source: this route reads the query
    # string, so its meter reads the query string, via the same function the
    # view below calls. No fallback to the body — a GET may carry one.
    job_id=job_id_in_query,
)
@requires_scout_quota
def progress():
    from flask import stream_with_context  # noqa: PLC0415

    job_id = job_id_in_query()
    chain_id = request.args.get("chain", "").strip()

    job_dir = _resolve_job_dir(job_id) if job_id else None
    pdb_path = _find_input_file(job_dir) if job_dir else None

    if not job_id or not _valid_chain(chain_id) or pdb_path is None:
        # Reason picked HERE rather than inside the generator, so the count and
        # the frame cannot pick different ones — and so the count happens when
        # we decide to refuse. A generator body does not run until the response
        # is iterated, and this one is not wrapped in stream_with_context, so
        # in-generator counting would fire late, off the request context, and
        # not at all for a client that hangs up first.
        if not job_id or not _valid_chain(chain_id):
            msg = "job_id and a valid chain id are required."
            reason = REASON_BAD_REQUEST
        else:
            msg = "Job not found or expired. Please re-upload your file."
            reason = REASON_JOB_EXPIRED
        observe_scout_refusal(reason)

        def _error_stream():
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
                _remove_derived_result_files(pdb_path.parent)

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
                # INSIDE the generator, unavoidably: the slot is taken when the
                # response starts being iterated, so the shed does not even
                # exist as a decision until this frame runs. Hoisting it would
                # count refusals that never happened. This body IS inside
                # stream_with_context, so request.endpoint still resolves.
                observe_scout_refusal(REASON_BUSY)
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

    if not job_id or not _valid_chain(chain_id):
        return jsonify({"error": "job_id and a valid chain id are required."}), 400

    job_dir = _resolve_job_dir(job_id)
    if job_dir is None:
        return jsonify({"error": "Job not found or expired. Please re-upload."}), 404
    pdb_path = _find_input_file(job_dir)
    if pdb_path is None:
        return jsonify({"error": "Job not found or expired. Please re-upload."}), 404

    epitope_residues = data.get("epitope_residues", [])
    epitope_id = data.get("epitope_id")

    if not epitope_residues and epitope_id is not None:
        results_csv = _results_csv_for_chain(job_dir, chain_id)
        if results_csv is None:
            return jsonify({"error": f"No Epitope Scout results found for chain {chain_id} on this job. Run epitope analysis on that chain first."}), 404

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
        # "Neither was supplied" and "that epitope_id is not in this chain"
        # are different problems; answering the second with the first tells the
        # user to send a field they already sent.
        if epitope_id is not None:
            return jsonify({
                "error": f"Epitope {epitope_id} is not in chain {chain_id}'s "
                         "results. Pick an epitope from that chain's results table."
            }), 404
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
        "known_binder_overlaps": _get_binder_overlaps(job_dir, epitope_residues, chain_id),
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

    if not job_id or not _valid_chain(chain_id) or pdb_path is None:
        def _error_stream():
            msg = "job_id and a valid chain id are required." if not job_id or not _valid_chain(chain_id) else "Job not found or expired."
            yield f"data: {json.dumps({'stage': 'error', 'msg': msg})}\n\n"
        return current_app.response_class(
            _error_stream(), mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    epitope_residues: list[int] = []
    results_csv = None
    if epitope_str:
        epitope_residues = [int(x.strip()) for x in epitope_str.split(",") if x.strip().lstrip("-").isdigit()]
    elif epitope_id:
        results_csv = _results_csv_for_chain(job_dir, chain_id)
        if results_csv is not None:
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
        # Both UI paths open this stream before the JSON route, so this is the
        # message the user actually reads. "No results for this chain" is only
        # true when the chain gate missed; when it passed, the chain HAS been
        # analysed and the epitope_id simply is not in it, and telling the user
        # to analyse that chain sends them where they have already been.
        if epitope_str or not epitope_id:
            _msg = "No epitope residues specified."
        elif results_csv is None:
            _msg = (
                f"No Epitope Scout results found for chain {chain_id} on this "
                "job. Run epitope analysis on that chain first."
            )
        else:
            _msg = (
                f"Epitope {epitope_id} is not in chain {chain_id}'s results. "
                "Pick an epitope from that chain's results table."
            )

        def _err():
            yield f"data: {json.dumps({'stage': 'error', 'msg': _msg})}\n\n"
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
