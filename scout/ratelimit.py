"""Two-tier fixed-window rate limiting for the anonymous Epitope Scout flow.

Scout's landing, intake (upload / fetch-pdb / example) and analysis routes
are reachable without an account so a first-time visitor can decide their
hotspot residues before signing up. That makes them the only unauthenticated
*compute* + *upload* surface in the app, so they need their own meter:
signed-in callers are metered by ``scout.quota`` (3 runs / 30 days on the
free tier), anonymous callers are metered here.

Only anonymous requests are limited. A signed-in user is already capped by
the Supabase-backed quota, and keying a shared lab's NAT address into the
same bucket would punish paying users for their neighbours.

The two tiers
-------------

- **Per session** (``session_limit``), keyed on the anonymous id in the
  signed session cookie. TIGHT. This is the only limit an ordinary visitor
  should ever meet, and it catches a runaway tab or a hand-rolled script
  before it spends its whole institution's share.
- **Per IP** (``limit``), keyed on ``shared.metrics._client_ip``. The TRUE
  bound, because a cookie is free to rotate and a session key therefore
  bounds nothing an attacker cares about.

The session tier is charged FIRST and refuses without touching the per-IP
bucket, so one over-eager session cannot burn its neighbours' allowance on
requests it was refused anyway. An attacker rotating cookies simply lands on
the per-IP bucket, which is the point of having it.

One analysis, one charge
------------------------

A single Scout analysis is TWO HTTP requests: ``GET /scout/progress`` (the
SSE stream that actually runs the pipeline) and then ``POST /scout/analyze``
(the finalise step, which finds the pipeline's ``results.csv`` already on
disk and only resolves UniProt, looks up known binders and detects
interfaces). Both share the ``scout_analyze`` bucket, so until 2026-08-18 a
limit of 10 bought FIVE analyses, and QC measured the sixth researcher behind
one university NAT being refused with no concurrency involved at all.

The fix is NOT to stop metering ``/progress`` — it does the expensive half,
and leaving it unmetered would make full-pipeline compute free. Instead the
pair shares ONE charge, via a single-use follow-up credit:

- ``/scout/progress`` is declared ``pair=PAIR_OPENS``. It is charged on
  EVERY call, exactly as before, and a charge that is allowed leaves one
  credit behind.
- ``/scout/analyze`` is declared ``pair=PAIR_CLOSES``. It spends an
  outstanding credit if there is one, and is charged normally if there is
  not.

A credit is bound to the session, the address AND the job, so it can only
ever pay for the second half of the analysis that bought it. See
``_FOLLOWUP`` for why it cannot be replayed, banked, raced, stolen or
diverted — a legitimate analysis costs 1 instead of 2, and every route that
can execute a pipeline on its own is still charged for it.

What the limits ACTUALLY are, fleet-wide
---------------------------------------

The counters live in process memory, so the numbers configured in
``scout.routes`` are per-worker, not per-fleet. Gunicorn runs
``WEB_CONCURRENCY`` workers (default 2, see ``gunicorn.conf.py``) and a
caller's requests land on whichever worker is free, so the real ceiling is
``workers x limit``. With the shipped defaults that is:

- intake:  10/worker  -> **20 per 10 min per IP** across a 2-worker fleet
- analyze: 10/worker  -> **20 per 10 min per IP** across a 2-worker fleet

and it goes up proportionally if ``WEB_CONCURRENCY`` is raised. The counters
also reset on every deploy and on any worker recycle, so a caller who is
limited can be un-limited by a deploy landing mid-window. Quote the doubled
numbers, not the configured ones, when reasoning about abuse cost.

PHASE 3 WILL CHANGE THIS. When the window counters move to shared, durable
state, ``_FOLLOWUP`` MUST MOVE WITH THEM. It is per-worker for exactly the
same reason the counters are, and it degrades exactly the same way: a credit
granted on worker 1 is invisible to worker 2, so a ``/analyze`` that lands on
the other worker is charged. That degradation is FAIL-CLOSED — the caller
pays twice, as they do today — which is why this is safe to ship before
Phase 3. Leaving ``_FOLLOWUP`` in process memory *after* Phase 3 would not
be: the counters would be exact and the credit that offsets them would not.

ponytail: in-process fixed-window counters, deliberately. The worst case
this guards is wasted CPU on one box, which does not justify a Redis
dependency — move the counters into Redis or a Supabase table if Scout ever
needs a limit that holds exactly across the fleet and survives deploys.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from functools import wraps

from flask import jsonify, request, session

# Reuse the app's existing forwarded-header-aware client IP resolution rather
# than re-deriving it. That function counts X-Forwarded-For hops from the
# RIGHT (TRUSTED_PROXY_HOPS, default 1) precisely so this key cannot be chosen
# by the caller — a leftmost read would let one header nullify every bucket
# below. Do not swap it for an inline header parse.
from shared.metrics import _client_ip

# (bucket, key) -> (window_expires_at_monotonic, hits_in_window)
_WINDOWS: dict[tuple[str, str], tuple[float, int]] = {}
_LOCK = threading.Lock()

# Hard bound on the counter table so a spray of unique source addresses
# cannot grow it without limit. Expired entries are dropped first; if the
# table is still full we evict the entries CLOSEST TO EXPIRING, never the
# whole table. Clearing it would hand an attacker the control itself: fill
# the table and every currently-limited caller is re-allowed, which is a
# cheaper attack than the one the limiter exists to stop.
#
# Eviction order is LOWEST HIT COUNT first, ties broken by soonest expiry.
# That ordering is the whole point, so do not "simplify" it to oldest-first:
# the spray keys an attacker uses to apply the pressure have exactly 1 hit
# each and cost nothing to forget, while the entry that must survive is the
# one already at or over its limit. Evicting by age instead would evict the
# limited caller FIRST — it has been in the table longest — which is the
# reset the attacker was fishing for.
_MAX_KEYS = 20_000

# Evict in batches so a sustained spray does not pay a full sort on every
# request once the table is full — one sort per batch instead of per call.
# ponytail: O(n log n) sort per batch. If this ever gets hot, a heap or an
# expiry-bucketed ring would make it O(log n), but at 20k keys the sort is
# sub-millisecond and runs once per 200 requests.
_EVICT_BATCH = 200


def hit(bucket: str, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Record one call against ``(bucket, key)``.

    Returns ``(allowed, retry_after_seconds)``. ``allowed`` is False once the
    number of calls inside the current window exceeds ``limit``.
    """
    now = time.monotonic()
    slot = (bucket, key or "unknown")
    with _LOCK:
        if len(_WINDOWS) >= _MAX_KEYS:
            for stale, (expires, _) in list(_WINDOWS.items()):
                if expires <= now:
                    del _WINDOWS[stale]
            if len(_WINDOWS) >= _MAX_KEYS:
                overflow = len(_WINDOWS) - _MAX_KEYS + _EVICT_BATCH
                # (hits, expires) — cheapest-to-forget first. See _MAX_KEYS.
                cheapest = sorted(
                    _WINDOWS.items(), key=lambda kv: (kv[1][1], kv[1][0])
                )[:overflow]
                for stale, _ in cheapest:
                    del _WINDOWS[stale]

        expires, hits = _WINDOWS.get(slot, (0.0, 0))
        if expires <= now:
            expires, hits = now + window_seconds, 0
        hits += 1
        _WINDOWS[slot] = (expires, hits)
        return hits <= limit, max(1, int(expires - now))


# ---------------------------------------------------------------------------
# The per-session tier
# ---------------------------------------------------------------------------

# Session key holding the anonymous owner id. Lives here rather than in
# ``scout.routes`` because the limiter is the module that has to key on it and
# routes already imports from here; the other direction would be a cycle.
# ``scout.routes`` re-exports it, so ``scout_routes.ANON_SESSION_KEY`` still
# resolves.
ANON_SESSION_KEY = "scout_anon_id"

# One shared bucket for every caller that presents no anonymous session id.
_NO_SESSION_KEY = "anon:no-session"


def _session_key() -> str:
    """Bucket key for the per-session tier.

    Every anonymous caller who has completed an intake carries a random
    ``anon:<uuid4>`` in the SIGNED session cookie, so this key cannot be
    chosen or forged the way a header can — a caller can throw one away and
    get a fresh one, but only by starting a new session.

    Callers presenting no id at all — a direct POST with no cookie, a browser
    with cookies blocked, a sprayer that simply never sends one — ALL SHARE
    ONE BUCKET. That is deliberate and fails closed. Minting a key per
    request instead would hand every cookie-less caller an unlimited supply of
    fresh session buckets, which is the one thing this tier must not allow.
    Nothing legitimate is lost: without the id such a caller cannot own a job
    directory either, so every analysis it attempts 404s regardless.
    """
    key = session.get(ANON_SESSION_KEY)
    return key if isinstance(key, str) and key else _NO_SESSION_KEY


# ---------------------------------------------------------------------------
# One analysis, one charge — the follow-up credit
# ---------------------------------------------------------------------------

# (session key, per-IP key, job id) -> monotonic expiry of ONE outstanding
# credit.
#
# Granted by a request that was CHARGED and can execute a pipeline on its own
# (``/scout/progress``); spent by the finalise request that rides on the work
# that charge already paid for (``/scout/analyze``).
#
# Why an attacker gains nothing from it:
#
# * **It cannot be minted for free.** A credit is written only after the
#   charge was taken AND allowed. A refused request grants nothing.
# * **It cannot be replayed.** ``_spend_followup`` POPS under ``_LOCK``, so it
#   is single-use. Two ``/analyze`` calls after one ``/progress`` cost one
#   free and one charged, and N of them cost N-1.
# * **It cannot be banked.** At most ONE credit is outstanding per key —
#   granting overwrites. Ten ``/progress`` calls in a row leave one credit,
#   not ten, so a burst of cheap grants cannot be cashed in later.
# * **It cannot be raced.** The pop is atomic under the same lock the
#   counters use, so two concurrent ``/analyze`` calls against one credit
#   produce exactly one free ride.
# * **It cannot be stolen.** The key pairs the signed session id with the
#   per-IP key, so a NAT neighbour cannot spend a credit somebody else paid
#   for, and neither can the same session from another address.
# * **It cannot be DIVERTED, which is the subtle one.** The job id is in the
#   key, so a credit bought by ``/progress?job_id=A`` can only ever be spent
#   by ``/analyze`` on job A — and job A's ``results.csv`` was written by the
#   very call that bought the credit, so that ``/analyze`` takes the cheap
#   finalise path. Without the job id a charge on a bogus or abandoned job
#   would fund a free ``/analyze`` on a DIFFERENT job, and that ``/analyze``
#   runs the whole pipeline itself when ``results.csv`` is missing. One charge
#   would then buy ~24 CPU-s instead of ~15.
# * **Replaying a job id buys nothing.** Re-requesting
#   ``/scout/progress?job_id=...`` runs the whole pipeline again and is
#   charged again, every time, because that route only ever GRANTS.
#
# Eviction here is SOONEST-TO-EXPIRE FIRST, which is the OPPOSITE of the
# ordering ``_MAX_KEYS`` mandates for ``_WINDOWS``, and the inversion is
# correct: dropping a counter re-allows a limited caller, which is the reset
# an attacker sprays for, while dropping a credit merely CHARGES a caller who
# would otherwise have ridden free. Losing an entry here fails closed. Do not
# unify the two policies.
_FOLLOWUP: dict[tuple[str, str, str], float] = {}

# How long a credit stays redeemable.
#
# It only has to survive from the start of the SSE stream to the POST the
# browser fires when the stream reports "done". Phase 1 sized the served
# worst case of that stream at ~43 s (15 s queued + ~28 s of adversarial
# compute), so 120 s carries it with ~3x margin while keeping the table small.
# A credit that expires first costs the caller one extra charge — the
# behaviour they had before this existed — and costs the box nothing.
FOLLOWUP_TTL_SECONDS = 120.0

PAIR_OPENS = "opens"    # charged always; grants one credit when allowed
PAIR_CLOSES = "closes"  # spends a credit if one is outstanding, else charged


def _analysis_id() -> str:
    """The job this request is about, so a credit binds to ONE analysis.

    ``/scout/progress`` carries it in the query string, ``/scout/analyze`` in
    the JSON body. Flask caches the parsed body on the request, so reading it
    here costs no second parse in the view. Anything unparseable reads as ""
    — such a request is a 400 before it does any work, so it has nothing to
    divert a credit to.
    """
    job_id = request.args.get("job_id", "")
    if not job_id:
        body = request.get_json(silent=True)
        job_id = body.get("job_id", "") if isinstance(body, dict) else ""
    return job_id.strip() if isinstance(job_id, str) else ""


def _followup_key() -> tuple[str, str, str]:
    return (_session_key(), _client_ip() or "unknown", _analysis_id())


def _grant_followup(key: tuple[str, str, str]) -> None:
    """Leave one credit for the paired follow-up request. Overwrites."""
    now = time.monotonic()
    with _LOCK:
        if len(_FOLLOWUP) >= _MAX_KEYS:
            for stale, expires in list(_FOLLOWUP.items()):
                if expires <= now:
                    del _FOLLOWUP[stale]
            if len(_FOLLOWUP) >= _MAX_KEYS:
                overflow = len(_FOLLOWUP) - _MAX_KEYS + _EVICT_BATCH
                soonest = sorted(_FOLLOWUP.items(), key=lambda kv: kv[1])[:overflow]
                for stale, _ in soonest:
                    del _FOLLOWUP[stale]
        _FOLLOWUP[key] = now + FOLLOWUP_TTL_SECONDS


def _spend_followup(key: tuple[str, str, str]) -> bool:
    """Consume the outstanding credit for ``key``. True if there was one.

    Pops unconditionally, so an EXPIRED credit is both refused and reclaimed
    in the same call.
    """
    now = time.monotonic()
    with _LOCK:
        expires = _FOLLOWUP.pop(key, 0.0)
    return expires > now


def reset() -> None:
    """Drop every counter. Test helper; not used by request handling."""
    global _INFLIGHT, _WAITING
    with _LOCK:
        _WINDOWS.clear()
        # Credits too, or one test's unspent credit gives the next test a
        # free request and the charge assertions there quietly stop meaning
        # anything.
        _FOLLOWUP.clear()
    with _INFLIGHT_LOCK:
        _INFLIGHT = 0
        # _WAITING too, or a test that leaves a waiter parked shrinks the
        # queue for every test after it.
        _WAITING = 0


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
#
# The rate limiter bounds how OFTEN an address may ask; this bounds how many
# anonymous scoring pipelines run at once in ONE process, and how many callers
# may WAIT for one.
#
# STILL INERT AS DEPLOYED, deliberately. ``gunicorn.conf.py`` runs sync
# workers, so a process serves one request at a time, ``_INFLIGHT`` cannot
# exceed 1, and no cap above 1 is reachable. The full evaluation of why the
# worker class was NOT flipped — and exactly what has to land before it is —
# lives in ``gunicorn.conf.py``; read that before changing anything here. What
# is kept here is the correct guard, correctly sized, so the flip is one line
# rather than a redesign.
#
# WHY TWO SLOTS. Under a GIL, N concurrent CPU-bound pipelines do not finish
# one after another — they interleave and all finish at roughly the SAME time,
# near N x cost / 1.07, where 1.07 effective cores is the measured ceiling of
# a gthread process on this workload. That is the fact that sizes this
# number, and it cuts the opposite way from intuition: a larger N does not
# mean "the queue drains sooner", it means NOBODY finishes until N x cost has
# elapsed, so the first slot to free frees LATER the more slots there are.
#
# Adversarial cost is ~15 CPU-s per anonymous analysis at the 8 MB upload cap
# — 9.0 in run_pipeline, ~4.2 in the known-binder lookup, ~1 in interface
# detection, ~0.8 in the second structure parse. (Not 9.0: that covered
# run_pipeline alone. The third parse in the route is gone, see
# scout/routes.py.)
#
#     N=2  ->  first slot frees at ~28 s worst case, ~4 s typical
#     N=4  ->  ~56 s worst case: longer than any wait a browser should hold,
#              so a queued caller could never be served at all under
#              adversarial load. The queue would be decoration.
#
# Two is what makes the queue able to do its job. It also covers the network
# I/O inside the lookup, which releases the GIL, so two slots overlap usefully
# where four only contend. Raising this buys no throughput whatsoever — the
# number to raise if Scout needs more capacity is the worker count.

# How many anonymous callers may WAIT for a slot, per process.
#
# Without a queue this semaphore sheds instantly: the next concurrent caller
# is refused even though a slot frees a second or two later, because the
# typical analysis is ~2 CPU-s, not the ~15 worst case. That turns an ordinary
# burst — a lab meeting, a workshop, everyone trying it after the same
# seminar, which is exactly the audience this tool is for — into a wall of
# errors.
#
# But the queue MUST have a ceiling. An unbounded one is a slower way to fall
# over: callers pile up, each holding a worker thread, until the process stops
# accepting anything at all and the queue simply moves into the kernel's
# socket backlog where nothing can see or bound it.
#
# Two, matched to the slots. A third waiter would sit out the whole wait below
# and shed anyway on any load heavy enough to fill the first two places, so it
# would buy a held thread and a slower error.
ANON_MAX_QUEUED_RUNS = 2

# How long a queued caller waits before being told the truth.
#
# The case the queue exists for is the ordinary burst, where two typical
# analyses (~2 CPU-s each) clear in ~4 s — so 15 s carries it with over 3x
# margin. Under genuinely adversarial load the first slot does not free for
# ~28 s and this expires first, which is the honest outcome: an immediate
# "busy, try again" beats a browser held for a minute and then refused
# anyway. Phase 5 turns that refusal into a signup prompt.
#
# Served worst case is therefore bounded at 15 + ~28 = ~43 s.
ANON_QUEUE_WAIT_SEC = 15.0

_INFLIGHT = 0
_WAITING = 0
# A Condition, not a Lock, so a caller can park until a slot is RELEASED
# rather than poll for one. Every existing ``with _INFLIGHT_LOCK`` site works
# unchanged, but note it is NOT the same object: a bare ``Condition()`` is
# backed by an RLock, so this mutex is reentrant where a ``Lock`` was not.
# Nothing here relies on that, and nothing should — reentrancy turns a
# same-thread re-entry from a loud deadlock into a silent success, which is
# how a lock held across a ``yield`` stays invisible. The acquire/release
# decision is deliberately kept out of the yield path for that reason.
_INFLIGHT_LOCK = threading.Condition()


@contextmanager
def anon_compute_slot(
    limit: int,
    *,
    max_waiting: int | None = None,
    wait_timeout: float | None = None,
):
    """Yield True when an anonymous compute slot was taken, False when full.

    Waits up to ``wait_timeout`` for a slot rather than refusing immediately,
    but only if fewer than ``max_waiting`` callers are already waiting. Past
    that the answer is an immediate False and the caller sheds.

    Signed-in callers always get True without consuming a slot — the paywall
    already bounds them, and a free-tier visitor must never be able to starve
    someone who is paying.

    Released in a ``finally`` so an exception, or a client that hangs up
    mid-stream (which closes a streaming generator), cannot leak the slot and
    wedge the pool at "full" forever.
    """
    global _INFLIGHT, _WAITING
    if session.get("user_email"):
        yield True
        return
    # Resolved here rather than in the signature: a module constant used as a
    # default argument freezes at import, so a test (or a future runtime
    # override) that rebinds the constant would be silently ignored. That exact
    # bug has shipped in this repo before.
    if max_waiting is None:
        max_waiting = ANON_MAX_QUEUED_RUNS
    if wait_timeout is None:
        wait_timeout = ANON_QUEUE_WAIT_SEC

    # Decide under the lock, yield OUTSIDE it. A `yield` inside a
    # `@contextmanager` hands control to the caller's `with` body and does not
    # come back until that body finishes, so a `yield` under this lock would
    # hold the process-wide compute mutex for the entire duration of whatever
    # the caller does next. On /scout/analyze that is a jsonify; on
    # /scout/progress it is an SSE frame written to a client socket, so one
    # slow reader would serialise every slot acquire and release in the
    # process behind its own network write. Keep the acquire/release decision
    # and the yield strictly separate.
    granted = False
    with _INFLIGHT_LOCK:
        if _INFLIGHT < limit:
            granted = True
        elif _WAITING < max_waiting:
            _WAITING += 1
            try:
                granted = _INFLIGHT_LOCK.wait_for(
                    lambda: _INFLIGHT < limit, timeout=wait_timeout
                )
            finally:
                # Decremented in a finally so a timeout, or an exception
                # thrown into a waiting generator, cannot leak a queue place
                # and shrink the queue permanently.
                _WAITING -= 1
        if granted:
            _INFLIGHT += 1

    if not granted:
        yield False
        return
    try:
        yield True
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT = max(0, _INFLIGHT - 1)
            # Wake exactly one waiter: one slot came free, so waking all of
            # them would just have them re-park after a pointless GIL scramble.
            _INFLIGHT_LOCK.notify()


def inflight_anon_runs() -> int:
    """Current anonymous pipelines in flight in this process. For tests/debug."""
    with _INFLIGHT_LOCK:
        return _INFLIGHT


def queued_anon_runs() -> int:
    """Callers currently waiting for a slot in this process. For tests/debug."""
    with _INFLIGHT_LOCK:
        return _WAITING


_OVER_LIMIT_MESSAGE = (
    "Too many Epitope Scout requests from this network. Wait a minute and "
    "try again, or sign in for a free account with a higher allowance."
)

# The per-session refusal says something different on purpose. "This network"
# is wrong and actively confusing when the caller alone is over the limit —
# and it is the wrong call to action, because signing in genuinely does fix
# the session case immediately. Phase 5 turns this string into the funnel.
_SESSION_LIMIT_MESSAGE = (
    "You have used the free Epitope Scout allowance for this session. Sign "
    "in for a free account to keep going, or wait a few minutes."
)

# Machine-readable refusal reason, carried in every refusal body.
#
# There are now three ways Scout can say no to an anonymous caller, and on the
# SSE route they are otherwise indistinguishable: the per-IP limit and the
# compute shed BOTH answer HTTP 200 ``text/event-stream`` with a
# ``{"stage": "error"}`` frame, because EventSource cannot read a non-2xx
# body. So both get counted as successes by status code, and any refusal-rate
# measurement taken from status codes alone conflates them. The reason field
# is what makes them separable without inspecting prose that a copy edit will
# change out from under the metric.
#
# The values are an API surface for Phase 6's counters and for the front end.
# Do not rename them; add new ones.
REASON_RATE_LIMITED = "rate_limited"   # per-IP window, this module
REASON_SESSION_LIMITED = "session_rate_limited"  # per-session window, here
REASON_BUSY = "busy"                   # compute slot/queue full, scout.routes
REASON_AT_CAPACITY = "at_capacity"     # live-job cap, scout.routes

# REASON_SESSION_LIMITED exists because the two tiers mean two very different
# things and Phase 6 has to alert on them differently. A per-session refusal
# is ONE caller over their own allowance: expected, cheap, and the conversion
# moment Phase 5 is built around. A per-IP refusal means a whole institution
# has hit the shared ceiling, which is the failure the plan calls "an outage
# that does not look like one". A counter that merges them cannot see that.

# Not refusals — the two ways /scout/progress fails without us saying no.
# They share the SSE ``{"stage": "error"}`` frame with the three above, so a
# counter that keys on the frame alone cannot tell "we shed you" from "your
# job expired". Phase 6 needs that split: shedding means the box is under
# pressure, an expired job means the reaper is ahead of the user, and a
# missing parameter means the front end sent a bad request. Different
# problems, different responses.
REASON_BAD_REQUEST = "bad_request"     # caller omitted job_id / chain
REASON_JOB_EXPIRED = "job_expired"     # job dir reaped or never existed


def _refuse(*, sse: bool, retry_after: int, reason: str, message: str):
    """Build the refusal both tiers share, in the shape the route can carry."""
    if sse:
        from flask import current_app  # noqa: PLC0415

        def _limited_stream():
            yield "data: " + json.dumps({
                "stage": "error",
                "msg": message,
                "reason": reason,
            }) + "\n\n"

        return current_app.response_class(
            _limited_stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Retry-After": str(retry_after),
            },
        )
    response = jsonify({
        "error": message,
        "retry_after": retry_after,
        "reason": reason,
    })
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


def anon_rate_limit(
    bucket: str,
    *,
    limit: int,
    window_seconds: int,
    session_limit: int | None = None,
    sse: bool = False,
    pair: str | None = None,
):
    """Decorator: meter anonymous calls to this route against both tiers.

    ``limit`` is the per-IP ceiling and ``session_limit``, when given, the
    tighter per-session one. Signed-in requests pass straight through
    (``scout.quota`` meters those). Over-limit anonymous requests get a JSON
    429 with ``Retry-After`` — the Scout page's fetch handlers already render
    a non-2xx ``{"error": ...}`` body, so no front-end change is needed.

    ``sse=True`` is for the EventSource endpoints: ``EventSource`` cannot
    read a 429 body, so those get a 200 ``text/event-stream`` carrying the
    same ``{"stage": "error"}`` event the route already emits for a missing
    job, which the page renders inline.

    ``pair`` makes the two requests of one analysis share a single charge.
    ``PAIR_OPENS`` is charged always and grants a credit; ``PAIR_CLOSES``
    spends one if it can. Read ``_FOLLOWUP`` before changing either — the
    asymmetry is what stops ``/scout/progress`` becoming free compute.
    """
    # At import, not per request. A typo here would fail SILENTLY — no grant,
    # no spend, and the pair quietly billed twice again — which is the kind of
    # regression that only shows up as a support ticket from a lab.
    if pair not in (None, PAIR_OPENS, PAIR_CLOSES):
        raise ValueError(f"anon_rate_limit: unknown pair role {pair!r}")

    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if session.get("user_email"):
                return f(*args, **kwargs)

            # One analysis, one charge. Checked BEFORE either tier so a
            # spent credit covers both of them — otherwise the session tier
            # would still be charged twice per analysis and would bite at
            # half the number it advertises.
            if pair == PAIR_CLOSES and _spend_followup(_followup_key()):
                return f(*args, **kwargs)

            # Session tier first, and it returns WITHOUT touching the per-IP
            # bucket. A session over its own allowance must not go on
            # spending its institution's shared budget on requests it is
            # being refused anyway. Nothing is lost against an attacker:
            # rotating the cookie skips this tier entirely and lands on the
            # per-IP one, which is the tier that is supposed to stop them.
            if session_limit is not None:
                allowed, retry_after = hit(
                    bucket + ":session",
                    _session_key(),
                    limit=session_limit,
                    window_seconds=window_seconds,
                )
                if not allowed:
                    return _refuse(
                        sse=sse,
                        retry_after=retry_after,
                        reason=REASON_SESSION_LIMITED,
                        message=_SESSION_LIMIT_MESSAGE,
                    )

            allowed, retry_after = hit(
                bucket, _client_ip(), limit=limit, window_seconds=window_seconds
            )
            if not allowed:
                return _refuse(
                    sse=sse,
                    retry_after=retry_after,
                    reason=REASON_RATE_LIMITED,
                    message=_OVER_LIMIT_MESSAGE,
                )

            # Only a charge that was actually taken AND allowed buys a
            # credit. Granting before the refusal checks would let a refused
            # caller pay nothing and still hand itself a free follow-up.
            if pair == PAIR_OPENS:
                _grant_followup(_followup_key())
            return f(*args, **kwargs)

        return wrapped

    return decorator
