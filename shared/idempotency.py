"""Request idempotency for the Ranomics tools-hub.

Stream G.1 (Wave-0 hardening). Wraps mutating tool routes so that
accidental or deliberate retries return a cached response instead of
re-running the handler — no double-charges, no duplicate GPU jobs.

Usage
-----
    from shared.idempotency import idempotent

    @flask_app.route("/tools/mpnn/submit", methods=["POST"])
    @login_required
    @idempotent(ttl_seconds=60)
    @requires_wallet(tool_slug="mpnn")
    def mpnn_submit():
        ...

Decorator order matters: ``@idempotent`` is placed ABOVE
``@requires_wallet`` so a replay short-circuits without touching the
wallet ledger. The first request pays; cached replays do not.

Key scheme
----------
If the client sends an ``Idempotency-Key`` header, that value (prefixed
with the route) is used verbatim. Otherwise the key is
``sha256(user_id || route || content)``, where ``content`` is the raw body
when one is readable and a canonical encoding of the parsed form when it is
not. Keys are scoped to a single user — different users posting identical
bodies get different keys.

The form fallback is load-bearing, not a nicety. ``_enforce_csrf`` in app.py
is a ``before_request`` that reads ``request.form`` on every protected POST,
which consumes the stream, so ``request.get_data()`` here returns ``b""`` for
essentially every browser form submission. Hashing that empty body reduces the
key to ``(user, route)`` and makes any two different submissions to the same
route inside the TTL collide.

TTL
---
Default 60 s. Wide enough to absorb double-clicks and network retries,
short enough that a legitimate re-submission a minute later works.

Failures are not cached
-----------------------
A handler response with status >= 400, or an exception out of the handler,
releases the claim instead of storing it. The request did not happen, so there
is nothing to deduplicate, and caching it would block the user's corrected
retry for the rest of the TTL.

There are two triggers and they are not equivalent. An exception is detected
directly, so it releases whatever the route would have rendered. A *returned*
failure is detected only by its STATUS CODE, so that half helps only routes
that signal failure with one. ``tool_submit`` and the other
``blueprints/tools.py`` form handlers re-render their error page with a bare
``render_template``, which Flask serves as HTTP 200, so their failures ARE
still cached for the TTL and a corrected retry inside that window replays the
stale error page. Filed as A41; fixing it means giving those paths real 4xx
codes, which changes what the browser and every existing test see.

Failure modes
-------------
- No Supabase client AT ALL (``get_service_client()`` returns None): fails
  OPEN — the handler runs without dedup. Safe because every write that moves
  money takes the same client and short-circuits without it, so an unguarded
  handler still cannot spend anything.
- A client that IS present and whose query fails: fails CLOSED — HTTP 503,
  nothing runs. Note this INCLUDES the no-service-role-key case, because
  ``get_service_client`` falls back to a live anon client rather than None and
  RLS then refuses the write. A replay of these routes costs real money or
  real work, and a guard that cannot tell a retry from a first attempt must
  not wave one through. See :func:`_claim_key` for the full reasoning, and do
  not summarise "no service-role key" as failing open — it does not.
- Row exists with ``response_status IS NULL``: a prior request for the
  same key is still in flight. Returns HTTP 409 "request in progress".
- Row exists with a non-NULL status: replay the cached status + body.

Environment
-----------
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — same vars the rest of the
    app uses. No new configuration.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable, Optional

from flask import Response, jsonify, request

from shared.credits import get_service_client, load_user_context

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60
IDEMPOTENCY_HEADER = "Idempotency-Key"
_TABLE = "request_idempotency"


# Form fields excluded from the content hash. ``_csrf`` identifies the SESSION,
# not the submission, so it carries nothing the key needs and is dropped to keep
# the key a function of what the user actually typed. Note it is stable per
# session today -- app.py mints one token and reuses it -- so excluding it is
# not what makes a double-click dedup; it is insurance against a future rotating
# token silently breaking dedup, and it keeps the key honest either way.
_KEY_EXCLUDED_FIELDS = frozenset({"_csrf"})


def _form_fingerprint() -> bytes:
    """A canonical, order-independent encoding of the parsed form.

    Used when the raw body has already been consumed. Fully sorted, values and
    file parts alike, so two identical submissions hash identically regardless
    of the order the browser serialised them in. Built from ``lists()`` rather
    than ``to_dict()`` so a multi-valued field (the launch screen's ``tools``
    checkboxes) contributes every value: collapsing to the first would make
    "run rfdiffusion" and "run rfdiffusion + 6 more" the same request.

    File parts contribute field name, filename, and byte length only. The bytes
    themselves are deliberately not hashed: reading them here would either cost
    a full pass over every upload on every request or consume the stream the
    handler needs. The honest consequence is that two submissions identical in
    every form field AND uploading files of the same name and size within the
    TTL still collide. That combination is a double-click far more often than
    it is two distinct jobs.
    """
    def _framed(*fields: str) -> bytes:
        """Length-prefix EVERY field of a part, not just the part itself.

        A single ``f"{field}={value}"`` string prefixed once is still forgeable,
        because a field NAME may contain ``=`` (``%3D`` decodes into
        ``request.form``): ``{"a": "b=c"}`` and ``{"a=b": "c"}`` both encode to
        ``5:a=b=c``. Framing each component separately gives ``1:a3:b=c`` and
        ``3:a=b1:c``, which cannot be confused, because a length is a property of
        the bytes and not something they can spell.
        """
        out = bytearray()
        for text in fields:
            raw = text.encode("utf-8")
            out += b"%d:" % len(raw)
            out += raw
        return bytes(out)

    parts: list[bytes] = []
    try:
        for field, values in sorted(request.form.lists()):
            if field in _KEY_EXCLUDED_FIELDS:
                continue
            for value in sorted(values):
                parts.append(_framed(field, value))
    except Exception:  # pragma: no cover - defensive; form parsing already ran
        logger.warning("Idempotency form fingerprint failed.", exc_info=True)
        return b""
    try:
        # Build the descriptors first, then sort THOSE. Sorting the raw
        # (name, FileStorage) pairs raises TypeError the moment one field name
        # repeats, because Python falls through to comparing two FileStorage
        # objects; sorting by name alone avoids the crash but leaves repeated
        # parts in wire order, so the same two files posted in the other order
        # would hash differently and fail to dedup. Sorting the finished
        # strings gets both.
        file_parts = []
        for field, storage in request.files.items(multi=True):
            size = -1
            try:
                stream = storage.stream
                pos = stream.tell()
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(pos)
            except Exception:
                size = -1
            file_parts.append(
                _framed("file", field, storage.filename or "", str(size))
            )
        parts.extend(sorted(file_parts))
    except Exception:  # pragma: no cover
        logger.warning("Idempotency file fingerprint failed.", exc_info=True)
    # Length-prefixed framing throughout, instead of joining on a separator. A
    # raw b"\0" join is forgeable, because a field VALUE may contain NUL (%00
    # survives urlencoded decoding): {"name": "a\0tools=iggm", "tools":
    # "rfdiffusion"} and {"name": "a", "tools": ["rfdiffusion", "iggm"]}
    # serialise to the same bytes, so those two genuinely different launches
    # collide and the second replays the first. `_framed` also separates each
    # field name from its value, closing the same hole one level down for a
    # name containing "=". A length is a property of the bytes, not something
    # they can spell, so no input can forge a boundary.
    #
    # The "file" tag on file descriptors is belt-and-braces, NOT the thing that
    # separates them from form fields: the outer prefix already frames each part
    # whole, so a 3-component file part cannot equal a pair of 2-component form
    # parts however they are spelled. Verified by removing the tag, which
    # changes no test. It is kept because it makes a part self-describing when
    # this ever needs debugging from a stored key.
    return b"".join(b"%d:" % len(part) + part for part in parts)


def _compute_key(user_id: str, route: str, body: bytes) -> str:
    """Derive an idempotency key from the request.

    Honours a client-supplied ``Idempotency-Key`` header if present so
    well-behaved integrations can retry on their own terms. Otherwise
    falls back to a content hash so replays of the same payload dedup
    automatically.

    When the raw body is empty the content hash falls back to the parsed form.
    This is the normal case for every cookie-authenticated form POST in this
    app, not an edge case: ``app.py``'s ``_enforce_csrf`` ``before_request``
    reads ``request.form``, which consumes the stream, so by the time this runs
    ``request.get_data()`` returns ``b""``. Without the fallback the key
    degenerates to ``sha256(user_id + route)`` and any two DIFFERENT
    submissions to the same route inside the TTL collide: the second is treated
    as a duplicate, never runs, and replays the first one's response. Do not
    "simplify" this back to hashing ``body`` alone.
    """
    header_value = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
    if header_value:
        # Namespace with route + user so a client that reuses the same
        # header across endpoints (or users) doesn't cross-collide.
        raw = f"{user_id}:{route}:{header_value}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    hasher = hashlib.sha256()
    hasher.update(user_id.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(route.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(body if body else _form_fingerprint())
    return hasher.hexdigest()


def _claim_key(
    key: str, user_id: str, route: str, ttl_seconds: int
) -> tuple[str, Optional[dict]]:
    """Try to claim the idempotency key for this request.

    Returns a tuple ``(state, row)``:
      - ``"claimed"``  — we hold the lock; run the handler
      - ``"replay"``   — ``row`` has a cached response to return
      - ``"in_flight"`` — another request is still processing
      - ``"open"``     — no Supabase client at all; proceed without dedup
      - ``"unavailable"`` — the ledger errored; refuse the request

    Failing open and failing closed are not one decision, and these are split.

    ``"open"`` is for one CONFIGURATION state rather than an outage: no client
    at all. (Not "no service-role key" -- that yields a live anon client and
    refuses; see below.) Running unguarded is safe there because every write
    that moves money takes the same client and short-circuits without it --
    ``reserve_hold`` (shared/wallet.py:575) and ``top_up_wallet``
    (shared/wallet.py:410) return None on a null client, and
    ``_cas_transition`` (shared/compute_campaigns.py:1843) returns False -- so
    an unguarded handler cannot place a hold, credit a wallet, or drive a
    campaign. ``reserve_hold``'s own null-client check is at :577, but on this
    path it never runs: ``wallet_preflight`` has already denied, so :575
    returns first. Failing closed here would instead take every guarded route
    down permanently in an environment that never had Supabase configured.

    Do NOT restate that as "the wallet decorator refuses". It does not, twice
    over: only one of the ten guarded routes carries ``requires_wallet`` at all
    (``blueprints/tools.py:1331``), and the decorator it carries is
    ``shared/wallet_guard.py``'s, which on a null wallet row deliberately falls
    THROUGH to the handler (:219-224) rather than blocking. The
    ``requires_wallet`` that does gate on a preflight is ``shared/wallet.py:901``
    and it is wired to no route at all. An earlier version of this paragraph
    claimed that chain and was wrong.

    One configuration is deliberately NOT given the open answer, because it is
    the one where open is most dangerous. With ``SUPABASE_URL`` and an anon key
    set but no service-role key, ``get_service_client`` returns a live ANON
    client (shared/credits.py:59-64) rather than None, and migration 0004
    enables RLS on this table with no policies -- so the SELECT reads empty and
    the INSERT is refused, and this refuses with it. The cost is that
    ``/library-planner/plan`` and, for signed-in callers only,
    ``/developability/score`` -- which spend nothing -- also 503 in a
    half-configured dev environment. Signed-in only because that route is
    deliberately anonymous (blueprints/tools.py:117-119 carries no
    ``@login_required``) and the decorator hands an anonymous request straight
    to the handler, so it never reaches this function without a user. The
    alternative is
    worse: a PRODUCTION deploy that lost its service-role key would fail open
    on the money routes and silently double-charge every double-click, which is
    exactly the hole this function was rewritten to close. A loud 503 naming
    the ledger is the better half of that trade, and `credits.py` already logs
    the missing key on the way past.

    ``"unavailable"`` is a live client whose query FAILED. Two very different
    faults land there and the refusal is sized for the narrower one. A fault
    scoped to THIS TABLE -- an RLS denial, a dropped grant -- leaves the rest
    of the request path healthy, so the handler really would
    spend money while we no longer know whether this exact request already ran.
    A broad fault (timeout, reset connection) breaks the same client
    everywhere, and the handler would bail downstream anyway:
    ``get_or_create_wallet`` swallows it and returns None (shared/wallet.py:277),
    after which ``create_job`` returns None and ``tool_submit`` stops before the
    Modal spawn. We cannot tell the two apart from in here, so we answer for the
    one that can cost money. Do NOT write "the wallet gate is working in that
    case" -- for the broad fault it is not, and an earlier version of this
    paragraph said exactly that and was wrong. Five of the ten guarded
    routes spend --
    ``compute_campaign_create``, ``compute_campaign_refold``, ``job_refold``,
    ``target_launch_submit``, ``tool_submit`` -- and for those, refusing costs
    the user a retry while running costs a second charge and a second GPU job
    that nothing downstream catches. It used to fail open, which made any
    PostgREST blip turn every double-click into two paid launches.

    The other five (``job_cancel``, ``campaigns_submit``, ``target_create``,
    ``developability_score``, ``library_planner_plan``) pay the refusal without
    the benefit, and ``job_cancel`` is the one that stings: a user cannot STOP
    a running job while the ledger is down. They are guarded anyway because a
    replay of any of them costs real work (blueprints/lab_projects.py:1286-1298
    is the enumeration), and splitting the stance per route would mean a guard
    whose safety depends on correctly classifying every future route.
    """
    client = get_service_client()
    if client is None:
        logger.warning(
            "Idempotency service client unavailable — proceeding without dedup."
        )
        return ("open", None)

    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)

    # Fast path: is there a live row for this key already?
    #
    # select("*"), not an explicit column list. PostgREST projects exactly the
    # columns asked for, so an explicit list silently drops any column added
    # later -- which is what made the `location` replay fix a no-op until this
    # changed. Naming `location` explicitly is worse than the wildcard: before
    # migration 0038 is applied PostgREST 400s on the unknown column, which the
    # bare except below now answers by REFUSING -- so naming it would take every
    # guarded route offline for the length of that deploy window rather than,
    # as it did when this failed open, double-charging every double-submit.
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("key", key)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
    except Exception:
        logger.error("Idempotency lookup failed — refusing.", exc_info=True)
        return ("unavailable", None)

    live = [r for r in rows if _row_still_live(r, now)]
    if live:
        row = live[0]
        if row.get("response_status") is None:
            return ("in_flight", row)
        return ("replay", row)

    # Not claimed, or every existing row is stale — claim it.
    claim_row = {
        "key": key,
        "user_id": user_id,
        "route": route,
        "response_status": None,
        "response_body": None,
        "content_type": None,
        "expires_at": expires.isoformat(),
    }
    # Two writes, each atomic on its own, and the INSERT is the sole arbiter
    # of the race:
    #
    #   1. Any row that survived the check above is expired, so clear it with a
    #      DELETE whose predicate IS staleness. It cannot remove a live claim,
    #      including the fresh one a concurrent caller may have inserted a
    #      moment ago, whose expires_at is a minute in the future. `lte`, not
    #      `lt`, so it is the exact complement of `_row_still_live`'s
    #      `expires > now`: under `lt` a row expiring on the captured
    #      microsecond would be neither live nor clearable, and would 503.
    #   2. INSERT. ``key`` is the PRIMARY KEY, so exactly one of any number of
    #      concurrent callers commits and every other raises a unique
    #      violation.
    #
    # This replaced ``upsert(claim_row, on_conflict="key")``, which was never a
    # lock: ON CONFLICT DO UPDATE *succeeds* for both racing writers, so the
    # preceding SELECT was a TOCTOU and BOTH sides of a double-submit were told
    # "claimed" and ran the handler -- two wallet holds and two Modal jobs for
    # one click (audit A42). Do not "simplify" this pair back into an upsert;
    # an upsert cannot fail, and a claim that cannot fail is not a claim.
    if rows:
        try:
            (
                client.table(_TABLE)
                .delete()
                .eq("key", key)
                .lte("expires_at", now.isoformat())
                .execute()
            )
        except Exception:
            logger.error(
                "Idempotency stale-row clear failed — refusing.", exc_info=True
            )
            return ("unavailable", None)

    try:
        client.table(_TABLE).insert(claim_row).execute()
    except Exception as exc:
        # Two very different things land here: we lost a race (unique
        # violation), or the ledger is broken. Only a re-read can tell them
        # apart, and it matters -- the loser of a race must be given the
        # winner's answer, an infra fault must be refused.
        #
        # Nothing is logged HERE, deliberately. Losing is the guard working,
        # and at one claim per worker process a single double-click loses N-1
        # times: logging a traceback at the raise site turns normal operation
        # into a stack-trace storm, burying the real faults in the log that is
        # the only place they surface (ALERTING.md:17 has Sentry "Deferred by
        # decision", so no error-rate monitor exists to be moved).
        # `_classify_failed_claim` knows which case it was, so it logs.
        return _classify_failed_claim(client, key, exc)

    return ("claimed", None)


def _classify_failed_claim(
    client: Any, key: str, exc: BaseException
) -> tuple[str, Optional[dict]]:
    """Decide what an INSERT that did not commit actually meant.

    A live row for the key means the claim is held -- replay it if that request
    has already finished, 409 if it is still running. No live row means we
    cannot establish that anything is holding it, and on a route that spends
    money the honest answer to "I cannot tell whether this already ran" is to
    refuse.

    Note what this deliberately does NOT claim. "Live row" is not the same as
    "somebody else won", because our own insert may have committed with only
    its response lost; and "no live row" is not the same as "the ledger is
    broken", because the winner of a race can fail and release before we look.
    Both readings were written here once and both were wrong. The branch does
    not need either: it needs only "is the key held", which is what it asks.

    The first of those has a cost worth stating. If the committed row was ours,
    nobody is running the handler, and answering 409 leaves the key claimed
    until the TTL expires -- 60 s in which the user cannot retry. The old
    fail-open path would have run the handler instead. That is the fail-closed
    trade taken deliberately: a minute of 409 beats a second charge, and the
    row cannot be distinguished from a genuine in-flight claim without an owner
    token on it.

    Reading our own key back is also what makes the loser's answer correct
    rather than merely safe. Answering every failed insert with 409 would be
    safe too, but it would hand a 409 to the second of two clicks even after the
    first had finished and cached a perfectly good response to replay.

    :func:`shared.campaigns._is_unique_violation` exists and is NOT used here on
    purpose. ``create_api_campaign`` has to classify because it must tell a
    clean replay from a 500; this re-read answers the same question from the
    ledger itself, which needs no exception sniffing and stays right whatever
    shape supabase-py raises next. Same pattern otherwise -- insert, lose,
    re-fetch -- and deliberately so.
    """
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("key", key)
            .execute()
        )
        rows = list(getattr(response, "data", None) or [])
    except Exception:
        logger.error(
            "Idempotency re-read after a failed claim failed — refusing.",
            exc_info=True,
        )
        return ("unavailable", None)

    now = datetime.now(timezone.utc)
    for row in rows:
        if not _row_still_live(row, now):
            continue
        # A live claim exists. Usually someone else's; possibly OUR OWN, if the
        # insert committed and only its response was lost. We cannot tell which
        # from here and do not need to -- both answers are "do not run" -- so
        # the message does not guess. INFO and no traceback either way: this is
        # the guard working, not a fault.
        logger.info("Idempotency key %s is already claimed.", key)
        if row.get("response_status") is None:
            return ("in_flight", row)
        return ("replay", row)
    # No live claim. Most often the ledger is broken, and that is why this
    # refuses. It is NOT proof of that, though: losing the race to a winner
    # who then failed and released its claim lands here too, with nothing
    # wrong. WARNING rather than ERROR for exactly that reason -- a healthy
    # race must not page anyone. The refusal stands regardless, because from
    # here the two are indistinguishable and only one of them is safe to run.
    logger.warning(
        "Idempotency claim for key %s did not commit and no claim is held "
        "— refusing.", key, exc_info=exc,
    )
    return ("unavailable", None)


def _row_still_live(row: dict, now: datetime) -> bool:
    """Return True if the row's expires_at is still in the future."""
    raw = row.get("expires_at")
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return expires > now


def _store_response(key: str, response: Response) -> None:
    """Persist the handler's response for future replays.

    Scoped to ``response_status IS NULL``, for the same reason
    :func:`_release_key` is: it may only ever write a claim that has not already
    completed.

    It was put there for a concurrent SIBLING. :func:`_claim_key` upserted, and
    ON CONFLICT DO UPDATE succeeds for both racing writers, so two submissions
    of the same form were both told ``"claimed"`` (audit A42) and the loser's
    400 overwrote the winner's cached 302 -- the browser was shown "Nothing was
    started and nothing was charged" for a launch that was funded and billing.
    The claim is a real INSERT now, so two callers racing for a free key can no
    longer both be told "claimed", and that collision is closed at its source.
    Not "only one caller ever reaches this function" -- the takeover below puts
    two here at once by a different route.

    The predicate stays, because a second route to the same clobber is open and
    nothing else stands in it: a claim whose TTL expires while its handler is
    still running. The row goes stale, a later request takes it over and
    completes, and only then does the original handler return and write here.
    The predicate is what stops that write burying the response the user was
    actually shown.

    The reverse ordering it does NOT catch: the slow handler writing into a
    takeover claim that has not completed yet, which still looks like its own.
    Telling those apart needs the row to carry an owner token, so it is
    recorded rather than fixed -- it costs a column, and a handler outliving a
    60 s TTL is not the common case. Do not read the predicate as covering it.

    The predicate does not weaken the orphaned-claim fallback it serves. A
    release that failed for an infra reason leaves the row present with a NULL
    status, so this write still matches and still caches.
    """
    client = get_service_client()
    if client is None:
        return
    try:
        body_text = response.get_data(as_text=True)
    except Exception:
        body_text = ""
    fields = {
        "response_status": int(response.status_code),
        "response_body": body_text,
        "content_type": response.headers.get("Content-Type"),
    }
    # Redirects carry their destination in a header, not the body, so a replay
    # that drops it returns a 302 to nowhere. Only set the column when there is
    # one, so non-redirect routes are untouched.
    location = response.headers.get("Location")
    if location:
        fields["location"] = location

    def _write(payload: dict):
        return (
            client.table(_TABLE)
            .update(payload)
            .eq("key", key)
            .is_("response_status", None)
            .execute()
        )

    try:
        _write(fields)
    except Exception:
        # Tolerate a deploy that lands before migration 0038: retry without the
        # new column rather than losing the cache entirely, because an
        # uncached response means the guarded handler re-runs on replay.
        if "location" not in fields:
            logger.warning(
                "Failed to cache idempotent response for key %s", key, exc_info=True
            )
            return
        fields.pop("location", None)
        try:
            _write(fields)
        except Exception:
            logger.warning(
                "Failed to cache idempotent response for key %s", key, exc_info=True
            )


def _release_key(key: str) -> bool:
    """Drop an UNFINISHED claim so a corrected retry can run. True iff it went.

    Called instead of :func:`_store_response` when the handler failed. The row
    MUST go rather than simply be left unwritten: a claim with
    ``response_status IS NULL`` reads as "in flight" to the next request, which
    would answer every retry with a 409 for the rest of the TTL.

    Scoped to ``response_status IS NULL`` so it can only ever remove a claim
    that never completed. Without that predicate the delete is by key alone, and
    would wipe a cached success belonging to a DIFFERENT request: a launch that
    really started, whose 302 is gone, so the next click launches everything a
    second time.

    Its original case was a concurrent sibling, back when :func:`_claim_key`
    upserted and both racing writers were told ``"claimed"``. The claim is a
    real INSERT now and that pairing cannot happen. What is left is the same
    surviving case :func:`_store_response` documents at length -- a handler that
    outlives its own TTL and returns after a later request has taken the claim
    over and completed it. Read that docstring for what the predicate does and
    does not cover.
    """
    client = get_service_client()
    if client is None:
        return False
    try:
        response = (
            client.table(_TABLE)
            .delete()
            .eq("key", key)
            .is_("response_status", None)
            .execute()
        )
    except Exception:
        logger.warning(
            "Failed to release idempotency claim for key %s", key, exc_info=True
        )
        return False
    # The deleted rows, not merely "the call did not raise". A delete that
    # matched nothing leaves the claim in place with response_status NULL, and
    # reporting success for that would skip the caller's cache fallback and
    # hand every retry a 409 until the TTL expired -- the outcome this
    # function exists to prevent.
    return bool(getattr(response, "data", None))


def _replay_response(row: dict) -> Response:
    """Reconstruct a Flask Response from a cached row."""
    status = int(row.get("response_status") or 200)
    body = row.get("response_body") or ""
    content_type = row.get("content_type") or "application/json"
    resp = Response(response=body, status=status, content_type=content_type)
    location = row.get("location")
    if location:
        resp.headers["Location"] = location
    resp.headers["Idempotent-Replay"] = "true"
    return resp


def idempotent(
    *, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> Callable:
    """Flask decorator — dedup replays of a mutating route.

    Place ABOVE ``@requires_wallet`` so cached replays do not place a
    second wallet hold. The first request places the hold; subsequent
    replays return the stored response untouched.
    """

    if ttl_seconds <= 0:
        raise ValueError("idempotent ttl_seconds must be positive.")

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapped(*args: Any, **kwargs: Any):
            ctx = load_user_context()
            if ctx is None:
                # login_required should have intercepted; if not, let the
                # wrapped handler produce its own auth response.
                return f(*args, **kwargs)

            route = request.path
            body = request.get_data(cache=True) or b""
            key = _compute_key(ctx.user_id, route, body)

            state, row = _claim_key(key, ctx.user_id, route, ttl_seconds)
            _observe(state)
            if state == "replay" and row is not None:
                return _replay_response(row)
            if state == "unavailable":
                # Same shape as the 409 below, and the same caveat: on a
                # browser form POST this renders as a raw JSON blob. Left that
                # way deliberately -- matching the in-flight answer keeps one
                # rendering problem instead of two. Nothing covers giving them
                # real pages -- A41 is about `tool_submit`'s failures returning
                # 200, which is a different defect, and no open item or
                # front-end handler exists for either payload.
                return (
                    jsonify(
                        {
                            "status": "unavailable",
                            "detail": (
                                "The request ledger is unavailable, so this "
                                "request cannot be checked against an earlier "
                                "one. Nothing was started and nothing was "
                                "charged. Retry in a moment."
                            ),
                        }
                    ),
                    503,
                )
            if state == "in_flight":
                return (
                    jsonify(
                        {
                            "status": "in_progress",
                            "detail": (
                                "An earlier request with the same key "
                                "is still running. Retry in a moment."
                            ),
                        }
                    ),
                    409,
                )

            try:
                response = f(*args, **kwargs)
            except Exception:
                # An exception never reaches the status check below, so without
                # this the row keeps response_status NULL and every retry for
                # the rest of the TTL is answered 409 "still in progress" --
                # including the retry made after the fault has cleared, and on
                # a browser form POST that 409 is a raw JSON blob. Unhandled
                # exceptions are the commonest source of 5xx, so this is the
                # case the release path most needs to cover.
                #
                # The trade-off, stated because it is real: releasing says
                # "nothing happened, retry freely", which is a lie if the
                # handler already mutated state before raising. That is why a
                # handler that spends money must not raise after its first
                # write.
                #
                # `target_launch_submit`'s fund/drive loop is the one that
                # matters, and it holds by TOTALITY of its callees, not by
                # catching: `fund_campaign` and `get_campaign` each swallow
                # everything and return False/None, and only the
                # `drive_campaign_async` spawn is wrapped in a try. Do not
                # restate this as "the loop catches" -- it does not, and an
                # earlier version of this comment said so and was wrong. Adding
                # a fallible call to that loop without a guard reintroduces the
                # exact hazard this paragraph describes.
                #
                # Left un-released the duplicate happens anyway, 60 s later;
                # this only stops the lockout in front of it.
                if state == "claimed":
                    _release_key(key)
                raise

            # Flask handler may return tuple (body, status) or a Response.
            flask_response = _as_flask_response(response)
            if state == "claimed":
                if flask_response.status_code >= 400:
                    # A failed request did not happen, so there is nothing to
                    # be idempotent about. Caching it means a user refused for
                    # insufficient balance, who then tops up in another tab,
                    # gets the same refusal replayed for the rest of the TTL.
                    # If the release fails, fall back to caching: replaying a
                    # 400 is a worse experience than a fresh attempt, but it is
                    # better than the 409 "still in progress" that an orphaned
                    # claim would produce until it expired.
                    if not _release_key(key):
                        _store_response(key, flask_response)
                else:
                    _store_response(key, flask_response)
            return flask_response

        return wrapped

    return decorator


def _as_flask_response(returned: Any) -> Response:
    """Normalise a Flask handler return value to a Response object."""
    if isinstance(returned, Response):
        return returned
    if isinstance(returned, tuple):
        body = returned[0]
        status = returned[1] if len(returned) > 1 else 200
        if isinstance(body, Response):
            body.status_code = status
            return body
        return Response(response=body, status=status)
    return Response(response=returned)


def _observe(outcome: str) -> None:
    """Lazy-imported metrics hook. Never raises."""
    try:
        from shared.metrics import observe_idempotency_outcome  # noqa: PLC0415
        observe_idempotency_outcome(outcome)
    except Exception:  # pragma: no cover
        pass
