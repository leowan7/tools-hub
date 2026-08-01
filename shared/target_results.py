"""Fan one design target's many runs into ONE ranked candidate table.

WHY THIS MODULE EXISTS
----------------------
A target can be launched at up to seven design tools. Each launch becomes a
compute campaign whose sub-jobs land in ``tool_jobs``, and a launch made from
the ``target:`` reuse token lands as a standalone ``tool_jobs`` row with no
campaign at all. Until this module existed the target page could only list one
panel per run, so the product claim "upload a target once, fan N tools at it,
get one ranked table" stopped one screen short.

This is the data layer under that screen. It does the reads and the row
provenance; :mod:`shared.ranking` does the cohort and percentile math and is
not re-implemented here.

WHAT THIS MODULE IS NOT
-----------------------
It is NOT a generalisation of ``aggregate_campaign_candidates``
(shared/compute_campaigns.py:1177). That function has zero diff from this
work, deliberately: the two paths differ in the ownership gate, the row source
(one table versus two), the dedupe scope, the ranking key, the envelope, the
sentinel, and whether standalone jobs exist at all. They share about fifteen
lines, and unifying the rest would put the four tests pinning the legacy
campaign sort permanently at risk to save them.

TWO INHERITED BEHAVIOURS THIS MODULE DOES NOT COPY
--------------------------------------------------
1. ``aggregate_campaign_candidates`` wraps its fetch in a bare
   ``except Exception`` and returns an empty envelope (:1230-1237). That idiom
   turns an unmodelled builder method into an empty table with a green suite.
   Here a read failure logs with ``exc_info`` and sets ``partial``, which
   travels in the envelope so the page can disclose it. What that does and does
   not cover is enumerated below; it is not a blanket claim.
2. Its ``best_by_chunk`` dedupe map is built once over one campaign's rows.
   That is correct there because there is only one campaign. Here the map is
   built PER CAMPAIGN inside :func:`_merge_child_rows`, for the reason given
   at that function.

A LIMIT ON WHAT ``partial`` CAN CLAIM
-------------------------------------
``partial`` covers the reads this module issues DIRECTLY: the ownership probe,
the per-campaign child reads, and the standalone pages. The ownership probe
counts because a failure there is re-asked through this module's own client
rather than trusted (:func:`_owned_target_exists`); ``shared.targets.get_target``
swallows its own exceptions and returns the same None it returns for a target
that does not exist, and serving that as "not found" 404s the owner of a
perfectly good target. Two reads reached THROUGH another module can still come
back SHORT with no channel to say so, and ``partial`` stays False across both.
Neither is closable from this file.

1. :func:`~shared.compute_campaigns.list_campaigns_for_target` catches its own
   paging failure, logs, and returns the runs it managed to read
   (shared/compute_campaigns.py:1077-1081). A SHORT run list is therefore
   invisible here, and because it never raises, the ``except`` around the call
   below is depth against a future change rather than a live path.
2. :func:`~shared.compute_campaigns.iter_succeeded_children` stops at
   ``_MAX_CHILD_PAGES`` with only a ``logger.error`` and then returns NORMALLY
   (shared/compute_campaigns.py:1170-1174), so a campaign truncated at the page
   bound reads here as a complete one. Out of reach with today's constants
   (``MAX_SUBJOBS_PER_CAMPAIGN`` x ``DEFAULT_MAX_ATTEMPTS`` is 100,000 against a
   101,000-row budget) but reachable by a campaign row whose ``max_attempts``
   was raised out of band, since that column is read per campaign.

Both need a completeness out-param on the callee, which is outside this phase.
So ``partial is False`` means "no read THIS module issued failed", not "the run
list and every campaign's children are complete". Do not read the flag as
stronger than it is, and do not add a comment here claiming it is.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Optional

from shared.compute_campaigns import (
    CAMPAIGN_TERMINAL_STATUSES,
    iter_succeeded_children,
    list_campaigns_for_target,
)
from shared.credits import get_service_client
from shared.jobs import candidate_records, count_passed_candidates
from shared.ranking import (
    DEFAULT_LIMIT,
    SORT_MODES,
    SORT_PERCENTILE,
    rank_candidates,
)
from shared.result_columns import columns_for, normalize_candidate
from shared.targets import get_target

logger = logging.getLogger(__name__)


# Columns the standalone read projects.
#
# ``preset`` is in the list and it is not padding. ``tool_jobs.preset`` is a
# real column (supabase/migrations/0005_tool_jobs.sql:29, ``preset text NOT
# NULL``), and shared.ranking keys its cohorts on ``(tool, preset)``. Omit it
# and every standalone row carries preset None, lands in a different cohort
# from the same tool's campaign rows, and splits one population into two that
# each overstate their percentile.
#
# ``campaign_id`` is deliberately ABSENT. It is filtered server-side (see
# _read_standalone_jobs) and a projection that returned it would let a future
# edit filter in Python instead, which is the one thing that filter must not
# become.
#
# ``inputs`` carries the refold discriminator (see _is_refold).
_STANDALONE_COLUMNS = "id,tool,preset,status,inputs,result"

# Columns the campaign child read projects, overriding
# ``iter_succeeded_children``'s default of "id,chunk_index,attempt,result".
#
# ``user_id`` is the addition, and it is not projected to be displayed. The
# child read carries no tenancy predicate of its own: it filters on
# ``campaign_id`` and ``status`` alone with the service-role client, which
# bypasses the ``auth.uid() = user_id`` policy at 0005_tool_jobs.sql:59. Its
# whole safety is inherited from ``list_campaigns_for_target``'s owner filter
# plus the convention that ``_dispatch_chunk`` stamps ``campaign.user_id`` on
# every sub-job it creates. That convention is not a schema constraint:
# 0034_compute_campaigns.sql:98-120 adds ``campaign_id`` with an FK to
# ``compute_campaigns(id)`` and NOTHING relating the two ``user_id`` columns.
# Projecting it lets the aggregator check the invariant instead of assuming it
# (see _drop_foreign_children, which is where the check is applied), so a
# re-parent, an admin clone or a second future writer of
# ``tool_jobs.campaign_id`` cannot put another tenant's design in this table.
_CHILD_COLUMNS = "id,user_id,chunk_index,attempt,result"

# Rows per page on the standalone read. At or below the PostgREST max_rows in
# supabase/config.toml (1000), or a page comes back short and paging stops
# early believing it reached the end.
_STANDALONE_PAGE_SIZE = 500

# Runaway guard only. Unlike a campaign's children there is no schema-level
# bound on how many standalone jobs may point at one target, so this cannot be
# derived the way _MAX_CHILD_PAGES is; it is a large round number. Hitting it
# sets ``partial``, so a truncated read is disclosed rather than served as a
# complete one.
_MAX_STANDALONE_PAGES = 200


def _is_refold(job: Mapping[str, Any]) -> bool:
    """True when a standalone job is a refold re-measurement, not a design run.

    ``blueprints/jobs.py:415`` stamps ``_refold_of_job_id`` into the new job's
    ``inputs`` and nothing else writes that key.

    The ``isinstance`` guard is not defensive decoration. ``tool_jobs.inputs``
    is ``jsonb NOT NULL``, so SQL NULL is impossible, but a jsonb scalar (a
    bare string from a legacy write) is a legal value of that column and
    ``"legacy".get`` is an AttributeError. Raised here it would abort the whole
    standalone page under the log-and-set-partial rule, so every standalone
    design would vanish behind a partial banner because of one malformed row.
    A non-dict ``inputs`` cannot carry the key, so it is not a refold.
    """
    inputs = job.get("inputs")
    if not isinstance(inputs, Mapping):
        return False
    return bool(inputs.get("_refold_of_job_id"))


def _candidate_rows(
    job: Mapping[str, Any],
    *,
    tool: Optional[str],
    preset: Optional[str],
    campaign_id: Optional[str],
) -> list[dict[str, Any]]:
    """One job row's candidates, tagged with the provenance ranking needs.

    ``normalize_candidate`` runs on the row that is RETURNED, so the table and
    the CSV see the tool's declared metric name (iggm persists
    ``n_epitope_contacts`` for the declared ``epitope_contacts``). It is NOT
    what makes ranking resolve that metric: ``shared.ranking.resolve_metric``
    normalizes independently before reading a value, so the ORDER is identical
    with or without this call. What is lost without it is the payload, and
    only the payload: every iggm design would rank perfectly and render an
    empty Score cell and an empty ``epitope_contacts`` CSV column. It is a
    pass-through for every other tool.

    ``_source_index`` is the position of the record in the job's own result,
    counting records this loop skipped. That keeps ``(job_id, index)`` unique,
    which is what makes ``shared.ranking.canonical_sort_key`` a total order.

    ``_source_chunk`` is stamped ONLY when the job carries one, and absent
    rather than None otherwise. It is the campaign sub-job the design came
    from, which ``templates/components/candidate_table.html`` renders as the
    ``{tool} #{chunk}`` chip (gated on the key being not-None, falling through
    to a job-id chip when it is missing) and which ``shared.exports`` exports
    as ``source_chunk`` (a provenance column is omitted from the CSV when NO
    row carries it, so an absent key on standalone rows costs nothing). The
    campaign aggregator stamps the same key from the same column
    (shared/compute_campaigns.py:1262); without it here the target table
    cannot say which of a run's sub-jobs produced a design, and ``#0`` from
    every campaign would be indistinguishable.
    """
    job_id = job.get("id")
    chunk = job.get("chunk_index")
    rows: list[dict[str, Any]] = []
    for index, cand in enumerate(candidate_records(job.get("result"))):
        if not isinstance(cand, Mapping):
            continue
        row = dict(normalize_candidate(cand, tool or ""))
        row["_source_tool"] = tool
        row["_source_preset"] = preset
        row["_source_campaign_id"] = campaign_id
        row["_source_job_id"] = job_id
        row["_source_index"] = index
        if chunk is not None:
            row["_source_chunk"] = chunk
        rows.append(row)
    return rows


def _merge_child_rows(
    rows,
    *,
    tool: str,
    campaign_id: Optional[str] = None,
    preset: Optional[str] = None,
) -> tuple[list[dict[str, Any]], int]:
    """Dedupe ONE campaign's retry siblings and expand them to candidate rows.

    Returns ``(candidate_rows, passed_count)``, where ``passed_count`` is the
    per-RESULT ``count_passed_candidates`` total over the deduped jobs. It is
    accumulated here because the result JSON is already in memory; calling
    ``_campaign_passed_filters`` once per run instead would be N full re-scans
    of every result on a page that has just scanned them.

    ``tool``, ``campaign_id`` and ``preset`` are SCALARS rather than values
    derived per row, so that passing several campaigns' rows through one call
    is not expressible. That is the point, and the reason is the dedupe map
    below: ``chunk_index`` is unique only WITHIN a campaign (the unique index
    is ``(campaign_id, chunk_index, attempt)``,
    supabase/migrations/0034_compute_campaigns.sql:133-134). Merge several
    campaigns through one map and chunk 0 of bindcraft evicts chunk 0 of
    boltzgen, silently deleting a whole sub-job of designs the user paid for.

    Retry dedupe keeps the highest ``attempt`` per ``chunk_index``. A row with
    no ``chunk_index`` keys on its job id instead; that should not happen for a
    campaign child, and keying it on ``None`` would make every such row collide
    with every other.
    """
    best_by_chunk: dict[Any, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        chunk = row.get("chunk_index")
        key = chunk if chunk is not None else row.get("id")
        attempt = row.get("attempt") or 1
        previous = best_by_chunk.get(key)
        if previous is None or attempt > (previous.get("attempt") or 1):
            best_by_chunk[key] = row

    merged: list[dict[str, Any]] = []
    passed = 0
    for job in best_by_chunk.values():
        passed += count_passed_candidates(job.get("result"))
        merged += _candidate_rows(
            job, tool=tool, preset=preset, campaign_id=campaign_id,
        )
    return merged, passed


def _drop_foreign_children(
    rows, *, user_id: str, campaign_id: Any,
) -> list[Mapping[str, Any]]:
    """Drop any campaign child that carries ANOTHER tenant's ``user_id``.

    The child read filters on ``campaign_id`` and ``status`` alone with the
    service-role client, so its whole tenancy safety is inherited from
    ``list_campaigns_for_target``'s owner filter plus the convention that
    ``_dispatch_chunk`` stamps ``campaign.user_id`` on every sub-job it
    creates. That convention is not a schema constraint (see _CHILD_COLUMNS),
    so this checks it rather than assuming it. No violating row is reachable
    through the application today; a re-parent, an admin clone, a direct write
    or a second future writer of ``tool_jobs.campaign_id`` would make one.

    A row that carries NO ``user_id`` at all is KEPT, and that is deliberate.
    The column is ``NOT NULL``, so the only way to get here without it is a
    projection that stopped returning it, and treating "cannot check" as
    "foreign" would then empty a paying user's table in silence. Kept plus a
    logged error degrades to exactly the safety the code had before this
    check existed, which is the honest fallback.
    """
    kept: list[Mapping[str, Any]] = []
    foreign = 0
    unchecked = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        owner = row.get("user_id")
        if owner is None:
            unchecked += 1
        elif str(owner) != str(user_id):
            foreign += 1
            continue
        kept.append(row)
    if foreign:
        logger.error(
            "target_results: dropped %s sub-job(s) of campaign %s belonging to "
            "another user; tool_jobs.user_id disagrees with its campaign's",
            foreign, campaign_id,
        )
    if unchecked:
        logger.error(
            "target_results: %s sub-job(s) of campaign %s came back with no "
            "user_id, so the ownership invariant could not be checked",
            unchecked, campaign_id,
        )
    return kept


def _read_standalone_jobs(
    client, target_id: str, user_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    """A target's succeeded standalone jobs. Returns ``(rows, incomplete)``.

    NEVER raises. A page that fails returns the pages already read alongside
    ``incomplete=True``, mirroring the campaign loop's per-campaign
    containment: one failing page at offset 1000 should cost the rows in that
    page, not the thousand rows already in memory. The caller turns
    ``incomplete`` into ``partial`` either way, so a short read is disclosed
    rather than served as a complete one.

    Three filters, each load-bearing for a different reason:

    ``.eq("user_id", user_id)``
        The tenancy boundary, and the whole of it. ``get_service_client``
        authenticates with the service-role key (shared/credits.py:51-72),
        which bypasses RLS, so the ``FOR SELECT USING (auth.uid() = user_id)``
        policy at supabase/migrations/0005_tool_jobs.sql:59 is not a backstop
        here. ``tool_jobs.target_id`` is a plain nullable column with no
        parentage predicate, so owning the target does not imply owning the
        row: the two gates are independent and both are required.
    ``.is_("campaign_id", "null")``
        ``_dispatch_chunk`` stamps the parent's ``target_id`` on EVERY campaign
        sub-job, so without this filter every campaign child comes back a
        second time here and the table doubles. It is server-side because the
        client-side equivalent would transfer every campaign result JSON twice.
    ``.eq("status", "succeeded")``
        Nothing else carries candidates. Served by
        ``tool_jobs_target_status_idx``
        (supabase/migrations/0039_design_targets.sql:184-186).

    Paged with ``.order("id").range()`` because a plain select is clamped by
    PostgREST at ``max_rows`` and ``.limit()`` is clamped identically, so a
    clamped read is indistinguishable from a complete one at the call site.
    """
    rows: list[dict[str, Any]] = []
    start = 0
    for _ in range(_MAX_STANDALONE_PAGES):
        try:
            response = (
                client.table("tool_jobs")
                .select(_STANDALONE_COLUMNS)
                .eq("target_id", target_id)
                .eq("user_id", user_id)
                .eq("status", "succeeded")
                .is_("campaign_id", "null")
                .order("id")
                .range(start, start + _STANDALONE_PAGE_SIZE - 1)
                .execute()
            )
        except Exception:
            logger.warning(
                "target_results: standalone read failed for target %s at "
                "offset %s; keeping the %s row(s) already read",
                target_id, start, len(rows), exc_info=True,
            )
            return rows, True
        batch = list(getattr(response, "data", None) or [])
        rows += batch
        if len(batch) < _STANDALONE_PAGE_SIZE:
            return rows, False
        start += _STANDALONE_PAGE_SIZE

    logger.error(
        "target_results: standalone page bound hit for target %s; "
        "the standalone run list is incomplete", target_id,
    )
    return rows, True


def _owned_target_exists(client, target_id: str, user_id: str) -> bool:
    """Ask the ownership question again, through THIS module's client. RAISES.

    ``shared.targets.get_target`` answers None to three different facts: the
    target is absent, the target is another tenant's, and the read BLEW UP
    (it has a bare ``except Exception: return None`` and also returns None
    when it could not build a client of its own). Serving all three as the
    not-found sentinel 404s a paying user's own target, and its CSV, FASTA and
    ZIP, on any transient backend failure, while ``partial=False`` asserts
    that nothing failed. This repo has already had exactly such a transient
    (the Supabase HTTP/2 hang on Railway).

    So the None is disambiguated here rather than trusted: this read is issued
    on the 404 path only, it propagates its exception instead of swallowing
    it, and it is owner-scoped, so a True answer is proof of ownership and a
    clean False is proof of absence-or-foreignness. It costs one extra query
    on the path that returns no data anyway, and none at all on the path that
    returns a table.
    """
    response = (
        client.table("design_targets")
        .select("id")
        .eq("id", target_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return bool(getattr(response, "data", None) or [])


def _not_found(sort_mode: str, limit: Optional[int]) -> dict[str, Any]:
    """The envelope for a target that does not exist or is not this user's.

    This is the only value of ``ok`` False the module produces, and it is
    reached only after a SUCCESSFUL read said so. The empty shape is reused by
    :func:`_unreadable`, which flips ``ok`` back to True: owned but unreadable
    is a different answer from not yours, and the route 404s on this sentinel.
    """
    return {
        "ok": False,
        "partial": False,
        "candidates": [],
        "total": 0,
        "shown": 0,
        "unranked": 0,
        "capped": False,
        "limit": limit,
        "columns": [],
        "tools": [],
        "per_tool": {},
        "campaigns": [],
        "standalone_jobs": 0,
        "refold_jobs": 0,
        "passed_total": 0,
        "provisional": False,
        "sort_mode": sort_mode,
        "multi_tool": False,
        "split_tools": [],
    }


def _unreadable(sort_mode: str, limit: Optional[int]) -> dict[str, Any]:
    """The envelope for "we could not look", which is not "you have nothing".

    ``ok`` True keeps the route off the 404 path, and ``partial`` True is the
    disclosure that the emptiness is a failure and not an answer. Built from
    :func:`_not_found` so the two envelopes cannot drift apart on keys: a
    route reads ``agg["provisional"]`` on either without a KeyError.

    Note what this deliberately does NOT do: when the backend is unreachable
    this is also the answer for a target that is not yours and for one that
    does not exist, because ownership could not be established either way.
    That discloses nothing, since every id gets the identical answer while the
    read is failing, and the alternative is 404ing the owner.
    """
    envelope = _not_found(sort_mode, limit)
    envelope["ok"] = True
    envelope["partial"] = True
    return envelope


def aggregate_target_candidates(
    target_id: str,
    *,
    user_id: str,
    limit: Optional[int] = DEFAULT_LIMIT,
    sort_mode: str = SORT_PERCENTILE,
) -> dict[str, Any]:
    """Every design run against one target, pooled into one ranked table.

    ``user_id`` is REQUIRED and keyword-only, unlike
    ``aggregate_campaign_candidates``'s optional one. That function can default
    it to None safely because ``get_campaign`` gates the parent and every child
    is reached through its campaign id. A ``tool_jobs`` row reached by
    ``target_id`` has no such parentage predicate, so a defaulted None here
    would be a cross-tenant read one keyword argument away.

    A falsy ``user_id`` raises rather than being passed down, because the two
    falsy values fail differently and neither fails usefully: ``None`` makes
    ``list_campaigns_for_target`` skip its owner filter entirely and return
    every user's campaigns on that target, while ``""`` reaches PostgREST as
    ``user_id=eq.`` against a uuid column, which errors on the standalone read
    and is swallowed into an empty list on the campaign one.

    ``sort_mode`` changes the ORDER of the returned rows, never the SET: the
    ``limit`` slice is taken in canonical order before the display sort, so
    "top 300" is the same 300 designs whichever way they are shown and an
    export can honour the active sort without changing what it contains. An
    unrecognised value falls back to the default rather than raising, because
    it arrives from a query string.

    Envelope, extending :func:`shared.ranking.rank_candidates` (whose ``rows``
    is renamed ``candidates`` and whose ``tools`` mapping is renamed
    ``per_tool``, leaving ``tools`` free for the slug list the page iterates)::

        ok             False = not found or not yours. THE SENTINEL.
        partial        a read this module issued failed; see the module
                       docstring for what this flag cannot cover.
        candidates     selected rows, in display order
        total          rows before the cap
        shown          len(candidates)
        unranked       rows with no usable primary metric
        capped         total > shown
        limit          the cap that was applied
        columns        [] in pooled mode; columns_for(tool) only when there is
                       exactly ONE COHORT (one tool at one preset), so the page
                       can degrade to today's single-tool table. Gated on the
                       same flag as ``multi_tool``, never on len(tools)
        split_tools    slugs that contributed more than one preset, so the row
                       label can name the cohort the tool alone does not
        tools          sorted slugs contributing >= 1 design
        per_tool       build_tool_stats entry per tool, plus ``campaigns``
        campaigns      the ComputeCampaign objects, so the route does not read
                       the run list again to render the run strip. It still
                       reads it once more on the EMPTY path only, with
                       include_drafts, to count stranded drafts (this envelope
                       excludes them and cannot answer that question)
        standalone_jobs  succeeded non-refold standalone jobs read
        refold_jobs      succeeded standalone refold jobs, counted not ranked
        passed_total     see below
        provisional      see below
        sort_mode      the mode actually applied
        multi_tool     more than one (tool, preset) COHORT, so it is also true
                       for one tool run at two presets; see the note at the
                       return statement

    ``ok`` IS THE SENTINEL, not ``tools == []``. ``_campaign_export`` gates on
    ``agg.get("tool") is None`` (blueprints/campaigns.py:687-688); under that
    idiom an owned but EMPTY target would 404 a paying user's freshly launched
    work. ``ok=True`` with ``tools == []`` means yours and empty: render an
    empty state, export an empty file.

    ``passed_total`` uses ``count_passed_candidates``'s per-RESULT semantics
    summed over the deduped jobs, so a target total equals the sum of the run
    pages beneath it. ``per_tool[t]["passed"]`` answers a different question
    under ``shared.ranking``'s per-cohort regime, where a record carrying no
    filter verdict of its own is not a failure. The two diverge after job
    recovery, which writes ``filter_status`` only when the streamed partial
    carried one. Two questions, two predicates. They are pinned by separate
    tests; do not print them as one number and do not "unify" them.

    ``provisional`` is computed over CAMPAIGNS ONLY, against
    ``CAMPAIGN_TERMINAL_STATUSES`` (shared/compute_campaigns.py:226) and never
    ``CAMPAIGN_STATUSES``. The statuses that DISCRIMINATE the two sets are
    ``funded``, ``running`` and ``completing``: all three are members of
    ``CAMPAIGN_STATUSES``, so under that set a mid-flight run reads as
    terminal and the page presents a moving percentile table as final. That,
    not the A38 gap, is why the terminal set is the right one; probed status
    by status, ``paused_insufficient_funds`` is absent from BOTH sets, so it
    is provisional either way and A38 stays a separate latent problem rather
    than one this comparison would surface. Pinned by
    test_a_non_terminal_campaign_is_provisional, which uses those three.
    Standalone jobs cannot make a target provisional: they are read only at
    status ``succeeded``, and ``succeeded`` is a ``tool_jobs`` status, not a
    campaign one, so testing it against the campaign set would mark every
    finished standalone run provisional forever. A target with only standalone
    runs is therefore never provisional, which understates the case where one
    is still running; reading pending jobs to fix that is a second query for a
    banner and is not in this phase.
    """
    if not user_id:
        raise ValueError(
            "aggregate_target_candidates requires a user_id; it is the whole "
            "tenancy boundary on a target_id-keyed read"
        )
    effective_mode = sort_mode if sort_mode in SORT_MODES else SORT_PERCENTILE

    # The client is resolved BEFORE the ownership gate, and the order is
    # load-bearing. ``shared.targets`` binds the same ``get_service_client``
    # object this module does (shared/targets.py:30), so whenever there is no
    # client ``get_target`` also answers None. Gate first and every no-client
    # request answers "not found", which leaves the owned-but-unreadable
    # branch below unreachable in production while a test can still construct
    # it: a branch that certifies an outcome no user can ever receive.
    client = get_service_client()
    if client is None:
        logger.warning("target_results: no service client for target %s", target_id)
        return _unreadable(effective_mode, limit)

    # The ownership gate, before any data read. A foreign or missing target
    # reads no tool_jobs row at all.
    if get_target(target_id, user_id=user_id) is None:
        # None means absent OR foreign OR the read failed; see
        # _owned_target_exists. Only a successful read may 404 someone.
        try:
            owned = _owned_target_exists(client, target_id, user_id)
        except Exception:
            logger.warning(
                "target_results: ownership read failed for target %s",
                target_id, exc_info=True,
            )
            return _unreadable(effective_mode, limit)
        if not owned:
            return _not_found(effective_mode, limit)

    partial = False
    merged: list[dict[str, Any]] = []
    passed_total = 0
    standalone_jobs = 0
    refold_jobs = 0
    campaigns: list = []

    # -- campaign side: a LOOP, one request sequence per campaign ------------
    #
    # Never one .in_() over every campaign id. Three reasons, all of which a
    # later "optimisation" would break: _MAX_CHILD_PAGES
    # (shared/compute_campaigns.py:1137) is derived PER CAMPAIGN and widened to
    # an IN list silently truncates; one pathological 50k-child campaign would
    # exhaust a shared page budget and truncate every campaign after it; and
    # the per-campaign dedupe map below is only per-campaign because the read
    # is (see _merge_child_rows).
    try:
        campaigns = list(
            list_campaigns_for_target(target_id, user_id=user_id)
        )
    except Exception:
        logger.warning(
            "target_results: campaign list failed for target %s",
            target_id, exc_info=True,
        )
        partial = True

    for campaign in campaigns:
        try:
            child_rows = list(iter_succeeded_children(
                campaign.id, client, columns=_CHILD_COLUMNS,
            ))
        except Exception:
            logger.warning(
                "target_results: children failed for campaign %s",
                getattr(campaign, "id", None), exc_info=True,
            )
            partial = True
            continue
        rows, passed = _merge_child_rows(
            _drop_foreign_children(
                child_rows, user_id=user_id, campaign_id=campaign.id,
            ),
            tool=campaign.tool,
            campaign_id=campaign.id,
            preset=campaign.preset,
        )
        merged += rows
        passed_total += passed

    # -- standalone side ----------------------------------------------------
    #
    # No try/except here: _read_standalone_jobs never raises, because a failed
    # page must not cost the pages already read. It reports the same way a
    # truncated one does, and both set partial.
    standalone_rows, standalone_incomplete = _read_standalone_jobs(
        client, target_id, user_id,
    )
    if standalone_incomplete:
        partial = True

    for job in standalone_rows:
        # A refold is not a design, it is a RE-MEASUREMENT of a design that is
        # already a row in this table. Ranking it double-counts the molecule
        # and files it under the REFOLDER's tool (boltz2 / esmfold /
        # colabfold), so one design becomes two rows attributed to two tools.
        # It merges in SILENTLY without this filter: refolds carry no
        # campaign_id and _spawn_refold_job stamps target_id
        # (blueprints/jobs.py:424-431), and candidate_records reads designs[]
        # (shared/jobs.py:109-112), which is exactly the shape boltz2 and
        # esmfold emit.
        if _is_refold(job):
            refold_jobs += 1
            continue
        standalone_jobs += 1
        passed_total += count_passed_candidates(job.get("result"))
        merged += _candidate_rows(
            job,
            tool=job.get("tool"),
            preset=job.get("preset"),
            campaign_id=None,
        )

    ranked = rank_candidates(merged, limit=limit, sort_mode=effective_mode)

    per_tool = ranked["tools"]
    campaign_counts: dict[str, int] = {}
    for campaign in campaigns:
        slug = str(getattr(campaign, "tool", "") or "")
        campaign_counts[slug] = campaign_counts.get(slug, 0) + 1
    for slug, stats in per_tool.items():
        stats["campaigns"] = campaign_counts.get(slug, 0)

    tools = sorted(per_tool)

    # COHORTS, not tools. The comparable population is (tool, preset), which is
    # what cohort_key_for keys on and why build_tool_stats reports `presets` per
    # tool at all. Deriving the display flag from the TOOL count instead sent a
    # target holding one tool at two presets down the single-tool path: today's
    # table, whose Score column carries `data-col` and is therefore re-sortable
    # in the browser, pooling two populations whose numbers do not mean the same
    # thing. proteina's `total_reward` is `-i_pAE` under protein_binder and an
    # RF3 composite under its other variants (shared/ranking.py::cohort_key_for),
    # and templates/targets/launch.html offers proteina at two presets and iggm
    # at four, so this is a form the launch screen itself can produce.
    #
    # That is the exact error the preset half of the cohort key exists to
    # prevent, reappearing at the display layer where the ranking math cannot
    # see it. One cohort means one comparable population and today's table is
    # correct; more than one, by either route, means the pooled presentation.
    multi_cohort = len(tools) > 1 or any(
        len(stats.get("presets") or ()) > 1 for stats in per_tool.values()
    )
    # The tools whose rows need a preset to identify their cohort. Without it
    # the Tool column shows ONE label over two populations the ranking
    # deliberately kept apart, which is the split the pooled table entered
    # pooled mode to disclose. Empty for every target where the tool alone is
    # the cohort, which is all of them until someone runs proteina or iggm at
    # two presets.
    split_tools = sorted(
        slug for slug, stats in per_tool.items()
        if len(stats.get("presets") or ()) > 1
    )

    return {
        "ok": True,
        "partial": partial,
        "candidates": ranked["rows"],
        "total": ranked["total"],
        "shown": ranked["shown"],
        "unranked": ranked["unranked"],
        "capped": ranked["capped"],
        "limit": ranked["limit"],
        # [] in pooled mode: no single column set describes rows whose metrics
        # are not comparable. Gated on the same flag as `multi_tool` and not on
        # `len(tools)`, because the two disagree for one tool at two presets and
        # a non-empty `columns` there would render that tool's native columns
        # BESIDE the pooled Tool/Score/Pctile ones.
        "columns": columns_for(tools[0]) if len(tools) == 1 and not multi_cohort else [],
        "tools": tools,
        "per_tool": per_tool,
        "campaigns": campaigns,
        "standalone_jobs": standalone_jobs,
        "refold_jobs": refold_jobs,
        "passed_total": passed_total,
        # ``partial or ...``, not the any() alone. When the campaign-list read
        # fails, ``campaigns`` stays [] and any() over an empty list is False,
        # so a target whose runs were mid-flight was certified as settled by a
        # read that never saw them. A read that could not enumerate the runs
        # cannot certify they are all terminal, and provisional is the safe
        # direction: it says "this ranking may still move", which is exactly
        # what is true when part of the input is missing.
        #
        # CAMPAIGN_TERMINAL_STATUSES and never CAMPAIGN_STATUSES. The two
        # disagree on draft, funded, running and completing, so the latter
        # would report an actively running campaign as finished. They agree
        # about paused_insufficient_funds, which is in NEITHER, so a paused run
        # is provisional under both (register item A38, whose stated reason was
        # wrong for years).
        "provisional": partial or any(
            getattr(c, "status", None) not in CAMPAIGN_TERMINAL_STATUSES
            for c in campaigns
        ),
        "sort_mode": ranked["sort_mode"],
        # Named for the display mode it selects, not for its input: it is true
        # for more than one COHORT, which one tool at two presets also
        # satisfies. It therefore does NOT license cross-TOOL copy; the page
        # gates that on len(tools) instead, and labels split cohorts from
        # ``split_tools``.
        "multi_tool": multi_cohort,
        "split_tools": split_tools,
    }
