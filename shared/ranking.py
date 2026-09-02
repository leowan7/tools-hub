"""Cross-tool ranking math for the combined target results table.

WHY THIS MODULE EXISTS
----------------------
The target page pools designs from every tool that ever ran against one
target into ONE table. Those designs are scored on incompatible scales:
rfdiffusion/bindcraft/boltzgen/pxdesign report ipTM (0..1, higher better),
rfantibody reports interface pAE (lower better), proteina reports a reward
whose very definition changes with the preset, and iggm reports a small
integer contact count. Sorting those native numbers against each other would
be the single most misleading thing this page could do, and a column label
does not fix a wrong ordering.

So the table is ordered on a RELATIVE statistic instead: where a design sits
inside its own comparable population. That is the only quantity that means
the same thing for an ipTM row and an ipAE row.

This module owns that statistic and nothing else. It performs no I/O itself:
every function here is a pure transform over dicts the caller already loaded.
It does import ``shared.jobs`` and ``shared.result_columns``, and
``shared.jobs`` imports ``get_service_client`` at module level, so this
module is not free of a transitive database import. What it never does is
CALL one.

WHAT THE CALLER PROVIDES
------------------------
Rows tagged with provenance by the aggregation layer, each a dict carrying
``_source_tool``, ``_source_preset``, ``_source_campaign_id``,
``_source_job_id``, ``_source_index`` plus the candidate payload (a ``scores``
dict and/or root level metrics). ``annotate_rows`` and ``rank_candidates``
never mutate the caller's dicts: every annotated row returned here is a
shallow copy. ``select_under_cap`` is the exception and says so at its own
definition, since it stamps ``_floor_reserved`` in place on the annotated
copies it is handed.

THE FOUR DECISIONS THIS MODULE MAKES
------------------------------------
1. Cohort  = (tool, preset), computed over the FULL row set.
2. Pass regime decided per cohort, pass VERDICT per record, and a record
   that carries no verdict of its own is not a failure.
3. Mid-rank percentile inside the cohort, suppressed (but still used for
   ordering) below :data:`MIN_PERCENTILE_COHORT`.
4. Selection under a cap with a per-tool floor, so a small contributor
   cannot vanish from a table whose product claim is "every design from
   every tool".

Each is argued at its own definition below.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Optional

from shared.score_legends import judge, tool_has_bar
from shared.result_columns import (
    candidate_metric,
    normalize_candidate,
    primary_metric_for,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Below this cohort size the percentile is not DISPLAYED. A percentile over
# 8 designs claims a resolution the sample does not carry: one design moves
# the number by 12 points. Suppression stops the page ASSERTING a precision
# it does not have; it does not stop the page ORDERING the row (see
# annotate_rows, where the rank fraction is still computed and still used as
# the sort key).
MIN_PERCENTILE_COHORT = 20

# Minimum rows reserved for every contributing tool when a cap applies. See
# select_under_cap for why membership needs a floor and ordering does not.
PER_TOOL_FLOOR = 5

# Matches the campaign aggregation default so the two tables cap alike.
DEFAULT_LIMIT = 300

# Display orders. There are deliberately only two. A third mode ordering the
# NATIVE scores across tools would rank ipTM 0.91 against ipAE 3.7 against
# reward 12.4, which is meaningless, and a "scores are not comparable" note
# under the table does not undo a wrong row order.
SORT_PERCENTILE = "percentile"
SORT_TOOL = "tool"
SORT_MODES: tuple[str, ...] = (SORT_PERCENTILE, SORT_TOOL)

# Keys annotate_rows writes onto every row copy. Selection adds
# ``_rank_position`` and ``_floor_reserved`` on top of these.
ANNOTATION_KEYS: tuple[str, ...] = (
    "_cohort_preset",
    "_cohort_n",
    "_tool_has_bar",
    "_metric_key",
    "_metric_direction",
    "_metric_value",
    "_passed",
    "_ranked",
    "_rank_fraction",
    "_rank_percentile",
    "_percentile_suppressed",
    "_rank_within_cohort",
)

SELECTION_KEYS: tuple[str, ...] = ("_rank_position", "_floor_reserved")


# ---------------------------------------------------------------------------
# Cohort identity
# ---------------------------------------------------------------------------

def ordinal_suffix(value: Any) -> str:
    """Return the English ordinal suffix for ``value`` ("st", "nd", "rd", "th").

    Presentation, in a module that is otherwise pure statistics, because the
    thing being suffixed is the statistic this module computes and the
    alternative is arithmetic in a Jinja template. It stays a pure function, so
    the reason ranking.py has no I/O is untouched.

    It exists because the percentile cell hardcoded ``th``. Percentiles here run
    0 to 99 (rank_statistics clamps at 99), and a bare "th" is wrong for 27 of
    those 100 values: every x1, x2 and x3 outside the teens. "93th" was on
    screen beside "97th".

    The teens are the case a naive last-digit rule gets wrong, so they are
    handled first: 11, 12 and 13 take "th", which is why 11 does not become
    "11st".
    """
    try:
        n = abs(int(value))
    except (TypeError, ValueError):
        return "th"
    if n % 100 in (11, 12, 13):
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def ordinal(value: Any) -> str:
    """``value`` with its ordinal suffix, e.g. ``93`` -> ``"93rd"``."""
    return f"{value}{ordinal_suffix(value)}"


def cohort_key_for(row: Mapping[str, Any]) -> tuple[str, Optional[str]]:
    """The comparable population a row belongs to: ``(tool, preset)``.

    NOT the tool alone. proteina's ``total_reward`` is ``-i_pAE`` under the
    protein_binder preset and an RF3 composite under ligand_binder
    (tools/proteina/run_pipeline.py:116-117), so percentile ranking two
    proteina runs at different presets against each other would compare two
    different quantities that happen to share a column name.

    A blank preset normalizes to absent, so ``""`` and ``None`` are ONE
    population and not two half sized ones. Both mean "this row carries no
    preset", and splitting them would halve the denominator and overstate
    every percentile on both sides of the split.

    CONTRACT FOR THE AGGREGATION LAYER. For a tool whose preset is forced
    server side, (tool, preset) is meant to collapse to (tool), but it only
    collapses if every row of that tool arrives carrying the SAME preset
    value. So one value per tool has to be stamped across ALL of that tool's
    sources. A campaign path that stamps ``campaign.preset`` (a non optional
    str) beside a standalone path that leaves ``_source_preset`` unset splits
    one tool's designs into two cohorts: probed, a 6 design standalone
    bindcraft run beside 300 bindcraft campaign rows put a design that is
    301st of 306 by the tool's own metric at table position 26, on a rank
    fraction of 0.917 out of a cohort of 6. This module cannot detect that,
    because a genuine two preset tool looks identical to it. What it does is
    surface it: ``build_tool_stats`` reports more than one key under that
    tool's ``presets``.
    """
    tool = str(row.get("_source_tool") or "")
    preset = row.get("_source_preset")
    text = "" if preset is None else str(preset).strip()
    return (tool, text or None)


# ---------------------------------------------------------------------------
# Metric resolution
# ---------------------------------------------------------------------------

def resolve_metric(
    row: Mapping[str, Any], tool: str, metric_key: Optional[str],
) -> Optional[float]:
    """The row's primary metric as a rankable float, or None.

    ``normalize_candidate`` runs first so a tool that persists its headline
    metric at the record root under a different name (iggm writes
    ``n_epitope_contacts`` for the declared ``epitope_contacts``) resolves
    here whether or not the caller already normalized. It is a pass through
    for every other tool.

    NON-FINITE VALUES ARE UNRESOLVABLE, NaN and both infinities alike, even
    though ``float()`` accepts all three. ``candidate_metric`` coerces with a
    bare ``float()``, so the jsonb strings "nan", "inf" and "1e999" all arrive
    here as real non-finite floats.

    NaN, because a NaN inside ``sorted_values`` leaves that list UNSORTED and
    every ``bisect`` result in the cohort is then meaningless. The damage is
    not confined to the corrupt row: probed on a 5 value cohort, the NaN row
    came back at the 50th percentile and rank 1 of 5, and a real row that
    should have been the 90th came back at the 70th.

    Infinity, for two reasons. It sorts first (or last) on a value nobody
    measured, and being a cohort member it adds 1 to n and 1 to every other
    row's ``n_better``, so one corrupt row demotes every real design by a
    rank: probed, 20 real bindcraft rows plus one ``{"ipTM": "inf"}`` row put
    the corrupt row at rank 1 and dropped the best real design from the 97th
    percentile to the 92nd. And ``_metric_value`` goes into the returned
    envelope, where ``json.dumps`` writes infinity as the bare token
    ``Infinity``: not JSON, so a browser parsing that response raises
    SyntaxError for the WHOLE body and one bad row costs the page every
    design rather than one row.

    That last guarantee is about what this module WRITES, and no further.
    Rows are shallow copies, so the caller's own ``scores`` payload passes
    through untouched and this function cannot sanitize it. It does not have
    to: a row read back from jsonb cannot carry a bare ``Infinity``, because
    JSON has no token for one. The reachable vector is the string form, and a
    stored "1e999" serializes back out as the string it is.
    """
    if not metric_key:
        return None
    value = candidate_metric(normalize_candidate(row, tool), metric_key)
    if value is None or not math.isfinite(value):
        return None
    return value


# ---------------------------------------------------------------------------
# The statistic
# ---------------------------------------------------------------------------

def rank_statistics(
    sorted_values: Sequence[float], value: float, direction: str,
) -> tuple[float, int, int]:
    """``(rank_fraction, percentile, rank_within_cohort)`` for one value.

    ``sorted_values`` is the ascending list of every RESOLVABLE primary
    metric in the cohort, including ``value`` itself. ``direction`` comes
    from ``primary_metric_for`` and is never inferred from the data;
    anything other than ``"asc"`` is treated as descending, matching that
    function's own ``"desc"`` fallback.

        n_better = strictly better than value, per the direction
        n_equal  = equal to value, INCLUDING the row itself
        n_worse  = n - n_better - n_equal
        rank_fraction = (n_worse + n_equal / 2) / n
        percentile    = min(99, floor(100 * rank_fraction))

    MID-RANK, not strict-better. iggm's epitope_contacts is a small integer
    with few distinct values, so a dozen designs tied at the cohort maximum
    must share one percentile rather than each claiming the top of a
    distribution none of them is alone at. A tie here is BIT-EXACT float
    equality, so the rule bites for that integer metric and essentially never
    for the ipTM and ipAE tools. Do not read it as a promise about those
    columns: two ipTM rows the table renders identically at three decimals
    (0.8504 and 0.8501) are not tied and do come back five percentile points
    apart.

    The percentile is computed in exact integer arithmetic rather than
    ``floor(100 * float_fraction)`` because the float form floors a whole tie
    block one point low wherever the exact quotient is not representable:
    n=25, n_worse=14, n_equal=1 is exactly 58 and floors to 57 as a float.
    Tied rows share n, n_worse and n_equal, so they can never disagree with
    each other under either form; what the exact form buys is the right
    number, not internal consistency.

    The 99 clamp is NOT reachable from annotate_rows: a row is always tied
    with itself, so n_equal >= 1 and the fraction is at most 1 - 1/(2n), i.e.
    a percentile of AT MOST 99, and lower than that in a small cohort (97 at
    n=20, 98 at n=25, 99 only from n=50 up). What the clamp bounds is a DIRECT
    call with a value outside ``sorted_values``, where n_equal is 0 and the
    fraction can be exactly 1.0. So no caller of this function is ever handed
    a "100th percentile", which is not a thing.

    A cohort of ONE is always 0.5: n_worse = 0 and n_equal = 1 give 1/2
    whatever the value is. That number describes the sample, not the design,
    which is the same reason :data:`MIN_PERCENTILE_COHORT` withholds it. The
    row is still ORDERED on it, so a lone design lands mid table on the
    strength of being alone.

    ``rank_within_cohort`` is a competition rank (1 + n_better), so tied
    rows share a rank the same way they share a percentile.

    An empty cohort returns ``(0.0, 0, 0)`` rather than dividing by zero.
    Callers never rank a row that has no cohort population, since a row with
    a resolvable value is itself in that population.
    """
    n = len(sorted_values)
    if n == 0:
        return (0.0, 0, 0)
    lo = bisect_left(sorted_values, value)
    hi = bisect_right(sorted_values, value)
    n_equal = hi - lo
    if direction == "asc":
        n_better = lo
        n_worse = n - hi
    else:
        n_better = n - hi
        n_worse = lo
    # 2 * n_worse + n_equal over 2 * n is (n_worse + n_equal / 2) / n with no
    # floating point in the numerator.
    numerator = 2 * n_worse + n_equal
    rank_fraction = numerator / (2.0 * n)
    percentile = min(99, (100 * numerator) // (2 * n))
    return (rank_fraction, int(percentile), n_better + 1)


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------

def annotate_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Annotate every row with its cohort statistics. Input order preserved.

    Returns shallow copies; the caller's rows are never mutated. Entries that
    are not a Mapping are dropped, and dropping them shrinks ``total``, so
    junk in is silently fewer designs out. The guard tests ``Mapping`` rather
    than ``dict`` so it matches the declared parameter type: tested against
    ``dict``, a perfectly valid ``MappingProxyType`` row was discarded and one
    design in came back as an empty table.

    Cohorts are formed over the FULL row set, before any cap, so the
    percentile answers "out of everything this target has" and does not
    change meaning when the table is truncated for display.

    PASS REGIME, two levels::

        has_bar   = tool_has_bar(tool)                  # per TOOL
        passed(c) = judge(tool, c).verdict != "below"    # per RECORD

    The TOOL decides whether a bar applies at all; the RECORD is then sunk
    only on evidence that it fell short. Both halves are load bearing.

    Tool scope for the REGIME, and this is the half that used to be guessed.
    It was ``any(record_has_filter_signal(c) for c in cohort_rows)`` -- read
    off whether some row in the cohort happened to carry a stored
    ``filter_status``, which made the regime depend on which container version
    ran and on whether job recovery had rebuilt the row. It is now a
    declaration, ``shared.score_legends.GATE_COLUMNS``.

    THE TEXT THAT STOOD HERE WAS RIGHT AND AN EARLIER VERSION OF THIS
    PARAGRAPH SAID IT WAS WRONG. It claimed only rfdiffusion and pxdesign
    "register a ``filter_status`` COLUMN at all", cited
    shared/result_columns.py for it, and went on to note in as many words that
    "shared/jobs.py names a different and wider set, and the two have not been
    reconciled". Both halves were true: only those two tools carried the
    column, and the wider set was exactly the gap that mattered. Rewriting it
    as a claim about which tools EMIT a filter, and then calling that false
    because bindcraft and rfantibody do stamp one, attacked a sentence nobody
    wrote. (The shared/jobs.py docstring, separately, really did say those two
    tools "omit the field", and really was false.) Deciding the regime per
    record would still be wrong for the original reason: it partitions the
    table on tool identity rather than on design quality.

    Record scope for the VERDICT, because the rows of one cohort are not all
    measured alike. A recovered chunk arrives without the metrics the run
    produces at the end (shared/job_recovery.py rebuilds from records streamed
    DURING the run, and boltzgen's refold happens after), so it sits
    ``unjudged`` beside fully measured siblings. UNJUDGED IS NOT FAILED, and
    since ``passed`` LEADS canonical_sort_key that is not a small distinction:
    probed under the old rule, 240 recovered pxdesign rows at ipTM 0.99 sank
    below 100 bindcraft rows at 0.70, the target's best design was handed rank
    161, and 100 of those rows fell past the 300 cap and out of the table
    entirely. A design nobody rejected keeps its place.

    Per RESULT scope (``shared.jobs.count_candidates_meeting_bar``) answers a
    different question and answers it the other way round: it counts only
    ``meets``, because "N designs meet the bar" is a claim that needs evidence
    FOR, while sinking a row needs evidence AGAINST. It is aggregating one
    job's delivered count, not ordering one design against another target
    wide.

    UNRANKED ROWS, two distinct causes, both marked ``_ranked = False``:

    (a) The tool has a registered primary metric but this row cannot resolve
        a value. A recovered rfantibody job carries no ipAE, so its designs
        have nothing to rank on. Such a row is excluded from the cohort
        DENOMINATOR, which is correct (it would otherwise inflate n with a
        value nobody measured) but silently shrinks the sample, so it is
        counted per tool as ``unranked`` for the caller to disclose.
    (b) The tool has no registered primary metric at all. Every row of that
        tool is unranked and the cohort n is 0.

    Both sort after the ranked rows of their own passed bucket (see
    canonical_sort_key).
    """
    annotated = [dict(row) for row in rows if isinstance(row, Mapping)]

    cohorts: dict[tuple[str, Optional[str]], list[int]] = {}
    for idx, row in enumerate(annotated):
        cohorts.setdefault(cohort_key_for(row), []).append(idx)

    for (tool, preset), members in cohorts.items():
        metric_key, direction = primary_metric_for(tool)
        has_bar = tool_has_bar(tool)
        values: dict[int, float] = {}
        for i in members:
            value = resolve_metric(annotated[i], tool, metric_key)
            if value is not None:
                values[i] = value
        sorted_values = sorted(values.values())
        cohort_n = len(sorted_values)
        # Suppression is a property of the COHORT SIZE, so it is decided once
        # here and every ranked row of the cohort agrees about it.
        suppressed = cohort_n < MIN_PERCENTILE_COHORT

        for i in members:
            row = annotated[i]
            row["_cohort_preset"] = preset
            row["_cohort_n"] = cohort_n
            row["_tool_has_bar"] = has_bar
            row["_metric_key"] = metric_key
            row["_metric_direction"] = direction
            row["_passed"] = judge(tool, row).verdict != "below"
            value = values.get(i)
            row["_metric_value"] = value
            if value is None:
                row["_ranked"] = False
                row["_rank_fraction"] = None
                row["_rank_percentile"] = None
                row["_rank_within_cohort"] = None
                # Not "suppressed": there is no percentile being withheld,
                # the row has nothing to compute one from. The two states
                # need different copy on the page.
                row["_percentile_suppressed"] = False
                continue
            fraction, percentile, within = rank_statistics(
                sorted_values, value, direction,
            )
            row["_ranked"] = True
            row["_rank_fraction"] = fraction
            row["_rank_within_cohort"] = within
            row["_percentile_suppressed"] = suppressed
            # The fraction survives suppression; only the CLAIM is withheld.
            row["_rank_percentile"] = None if suppressed else percentile

    return annotated


# ---------------------------------------------------------------------------
# Canonical order
# ---------------------------------------------------------------------------

def _as_sort_int(value: Any) -> int:
    """Coerce a provenance index to an int so tuple comparison cannot raise.

    Catches ``Exception`` rather than ``(TypeError, ValueError)`` because
    ``int(float("inf"))`` raises OverflowError, which is neither, and a
    coercion whose whole job is "cannot raise" must not have a third case that
    aborts the entire ranking call to degrade one row.
    """
    try:
        return int(value)
    except Exception:
        return -1


def canonical_sort_key(row: Mapping[str, Any]) -> tuple:
    """Total order over annotated rows.

    ``(passed, unranked, -rank_fraction, -cohort_n, tool, job_id, index)``

    * ``passed`` LEADS. A row its own tool marked fail belongs below a row
      from a tool that has no filter: that is the tool's own verdict on its
      own design, not a cross tool comparison, and a design its pipeline
      rejected should not be presented above one nobody rejected.

      A CONSEQUENCE THE TEMPLATE HAS TO HANDLE: because passed leads, the
      failed bucket is appended after the ranked list carrying its own
      independent fraction range, so the rank fraction (and the percentile
      column that renders it) is monotone only WITHIN a bucket, never down
      the whole table. Probed: position 45 shows the 2nd percentile and
      position 46, the first failed row, shows the 98th. Render the failed
      rows as a visually separated group. Rendered as a continuation of the
      ranked list, that jump reads as a broken sort.
    * ``unranked`` sinks the rows with no usable metric below the ranked
      rows of the same bucket. It is redundant today, since a ranked row's
      fraction is at least 0.5/n and therefore always above the 0.0 an
      unranked row falls back to. It is written out anyway so that invariant
      is not load-bearing: nothing about this ordering should quietly break
      if the statistic is ever allowed to reach zero.
    * ``-rank_fraction`` is the ranking proper. Note that a SUPPRESSED
      percentile still carries its fraction here, so a small cohort's best
      design keeps its position instead of being buried under a few hundred
      mediocre rows from a large run. Do not "tidy" suppressed rows to the
      bottom: that would hide exactly the designs a small careful run
      exists to produce.
    * ``-cohort_n`` breaks a genuine tie toward the better evidenced number:
      0.95 out of 400 designs is a stronger claim than 0.95 out of 21.
    * tool slug, job id and source index make the key deterministic. The
      target page has no live refresh and the percentile moves as sub jobs
      land, so a non deterministic tie break would reshuffle equal rows on
      every reload for no reason. This is a TOTAL order as long as
      (job_id, index) is unique per row, which is how the aggregation layer
      builds it; rows that do collide fall back to Python's stable sort and
      therefore to input order.
    """
    fraction = row.get("_rank_fraction")
    return (
        0 if row.get("_passed") else 1,
        0 if row.get("_ranked") else 1,
        -float(fraction if fraction is not None else 0.0),
        -int(row.get("_cohort_n") or 0),
        str(row.get("_source_tool") or ""),
        str(row.get("_source_job_id") or ""),
        _as_sort_int(row.get("_source_index")),
    )


def sort_canonical(
    annotated: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Annotated rows in canonical order (new list, rows not copied again)."""
    return sorted(annotated, key=canonical_sort_key)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Selection under a cap
# ---------------------------------------------------------------------------

def select_under_cap(
    ordered: Sequence[dict[str, Any]], limit: Optional[int],
) -> list[dict[str, Any]]:
    """Take at most ``limit`` rows, reserving a floor for every tool.

    ``ordered`` must already be in canonical order. The returned rows stay
    in canonical order, and each is stamped with ``_floor_reserved``: True
    when the row is present ONLY because of the floor, i.e. it sits beyond
    the plain top ``limit`` prefix. That stamp is written IN PLACE, into rows
    that are already annotate_rows' copies rather than the caller's dicts.
    Under a cap only the SELECTED rows are stamped, so a row that did not
    make the cut carries no ``_floor_reserved`` key at all.

    WHY A FLOOR. Percentile ranking is relative within a cohort, so no tool
    is "uniformly weak" in fraction terms. What kills a small contributor is
    RESOLUTION: a tool with 4 designs has a best fraction of 0.875, while a
    tool with 300 has 30 rows above that. Cap at 300 rows over several
    thousand and every row of the small run falls below the cut and the tool
    disappears from a table whose whole product claim is "every design from
    every tool".

    WHY ONLY MEMBERSHIP. Reserved rows are emitted at their correct sorted
    position, never promoted to the top of the table or grouped into a
    consolation block. The floor changes WHICH rows are shown; it must not
    change what a position in the table means, or the table stops reading
    monotonically inside its bucket and the ordering claim becomes false.
    (The one discontinuity that IS by design is the passed/failed boundary;
    canonical_sort_key documents it.)

    The floor is allocated round robin: every tool's best row, then every
    tool's second, and so on, so a cap too small to hold every tool's full
    floor still spreads what it has rather than giving one tool five while
    another holds none.

    Tools are visited in order of their BEST canonical position, not slug
    order. When the cap is smaller than the number of contributing tools the
    round robin cannot reach every tool, and slug order then spent the entire
    cap on the alphabetically first tools INCLUDING the slot canonical
    position 0 needed: probed, a cap of 3 over 7 tools returned bindcraft,
    boltzgen and iggm, handed ``_rank_position`` 1 to a design at rank
    fraction 0.917, and left the target's best design (0.988, rfdiffusion)
    out of the table with nothing in the output to signal the omission.
    Visiting best row first makes the head of the canonical order unevictable
    and leaves slug order with no say; the strongest contributors are served
    first when the cap cannot serve everyone. Best positions are unique, so
    the order is still total and deterministic.

    A cap at or above the number of contributing tools leaves no tool at
    zero. Below it nothing can, and this spends what there is on the tools
    holding the best designs.

    ONLY PASSING ROWS ARE RESERVED. A tool contributes to the floor through
    the rows in the passed bucket; a tool whose every design its own filter
    marked fail reserves nothing. Measured before this rule existed: 6 failed
    pxdesign designs against 30 passing bindcraft ones at a cap of 10 gave
    pxdesign 5 of the 10 slots, so half the table was designs that failed
    quality control while 5 passing designs were pushed out. The floor exists
    so a tool's good work is not hidden by a cap; a tool with no good work is
    not hidden by anything, and its per-tool stats still report what it
    produced. Spending a scarce display budget on another tool's rejects
    inverts the purpose. When nothing anywhere passes, the floor reserves
    nothing and the cap fills straight down the canonical order.

    ``limit=None`` (or a limit at or above the row count) selects everything
    and stamps every row False.
    """
    total = len(ordered)
    if limit is None or limit >= total:
        for row in ordered:
            row["_floor_reserved"] = False
        return list(ordered)

    cap = max(0, int(limit))
    if cap == 0:
        return []

    positions_by_tool: dict[str, list[int]] = {}
    for pos, row in enumerate(ordered):
        if not row.get("_passed"):
            continue
        tool = str(row.get("_source_tool") or "")
        positions_by_tool.setdefault(tool, []).append(pos)

    # Best canonical position first. positions_by_tool[t] is built in
    # ascending order, so [0] is that tool's best row.
    tool_order = sorted(positions_by_tool, key=lambda t: positions_by_tool[t][0])

    keep: set[int] = set()
    for slot in range(PER_TOOL_FLOOR):
        if len(keep) >= cap:
            break
        for tool in tool_order:
            positions = positions_by_tool[tool]
            if slot >= len(positions):
                continue
            if len(keep) >= cap:
                break
            keep.add(positions[slot])

    # Fill the remainder straight down the canonical order. Positions already
    # reserved are simply re-added as a no-op, so the fill neither double
    # counts them nor skips the row that should take the freed slot.
    for pos in range(total):
        if len(keep) >= cap:
            break
        keep.add(pos)

    chosen = sorted(keep)
    selected = [ordered[pos] for pos in chosen]
    for pos, row in zip(chosen, selected):
        # Positions 0..cap-1 are what an unfloored top-N would have taken.
        row["_floor_reserved"] = pos >= cap
    return selected


# ---------------------------------------------------------------------------
# Display order
# ---------------------------------------------------------------------------

def apply_sort_mode(
    selected: Sequence[dict[str, Any]], sort_mode: str,
) -> list[dict[str, Any]]:
    """Reorder the SELECTED rows for display. Never changes the set.

    ``SORT_PERCENTILE`` returns the canonical order unchanged.
    ``SORT_TOOL`` groups by tool slug ALPHABETICALLY, canonical order within
    each group. Alphabetical because every other grouping order (best of,
    cohort size, delivery time) is a third statistic making a fourth claim
    about which tool did better, which is the claim this page refuses to
    make.

    Because the limit slice is taken in canonical order before this runs,
    "top 300" is the same 300 designs whichever way they are displayed, so
    an export can honour the active sort without changing what it contains.
    """
    if sort_mode == SORT_TOOL:
        return sorted(
            selected,
            key=lambda r: (
                str(r.get("_source_tool") or ""), canonical_sort_key(r),
            ),
        )
    return list(selected)


# ---------------------------------------------------------------------------
# Per-tool disclosure
# ---------------------------------------------------------------------------

def build_tool_stats(
    annotated: Sequence[Mapping[str, Any]],
    selected: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Per-tool numbers the page needs in order to disclose what it did.

    Keyed by tool slug, in slug order. Each entry carries:

    ``total``
        rows this tool contributed, before any cap.
    ``cohort_n``
        rows that carry a resolvable primary metric, summed over the tool's
        cohorts. For a single cohort tool that IS the denominator its
        percentiles were computed over. For a tool with more than one cohort
        it is NOT: proteina at two presets reports 40 here while every row it
        produced carries ``_cohort_n`` 20 and no percentile was ever computed
        over 40. Do not render this figure as a denominator without checking
        ``presets``; the real per cohort n lives there and on each row.
    ``unranked``
        ``total - cohort_n``. Excluding these rows is right, but it shrinks
        the sample invisibly, so the number has to be surfaceable.
    ``shown``
        rows of this tool in the selected (capped) set.
    ``passed``
        rows NOT shown to fall short of the tool's bar, before any cap.
        NOT interchangeable with ``count_candidates_meeting_bar``, which
        counts only rows shown to MEET it. An unjudged row — one whose gate
        columns were not all measured — is counted here and excluded there,
        on purpose: ordering needs evidence a design failed, counting needs
        evidence a design passed. Probed on a 2 candidate result, one meeting
        the bar and one unjudged: 1 there, 2 here. Two questions, two answers.
        Do not print them as one number.
    ``metric`` / ``direction``
        the registered primary metric, ``None`` for a tool with none.
    ``has_bar``
        True when the tool declares a quality bar (score_legends.GATE_COLUMNS).
    ``percentile_suppressed``
        True when the tool has ranked rows but NONE of them carries a
        percentile, i.e. every cohort of the tool is under
        :data:`MIN_PERCENTILE_COHORT`. A tool with no registered metric is
        False here: its percentiles are absent for a different reason
        (``metric`` is None), and the page owes the reader that distinction.
    ``presets``
        per cohort detail, keyed by preset, for a tool that ran at more than
        one (proteina today).
    """
    shown_counts: dict[str, int] = {}
    for row in selected:
        tool = str(row.get("_source_tool") or "")
        shown_counts[tool] = shown_counts.get(tool, 0) + 1

    by_tool: dict[str, list[Mapping[str, Any]]] = {}
    for row in annotated:
        by_tool.setdefault(str(row.get("_source_tool") or ""), []).append(row)

    stats: dict[str, dict[str, Any]] = {}
    for tool in sorted(by_tool):
        rows = by_tool[tool]
        metric_key, direction = primary_metric_for(tool)
        ranked = [r for r in rows if r.get("_ranked")]

        presets: dict[Any, dict[str, Any]] = {}
        # Sorted with a None-safe key: preset is None for every tool whose
        # preset is forced server side, and None does not compare with str.
        for preset in sorted(
            {r.get("_cohort_preset") for r in rows},
            key=lambda p: (p is None, str(p)),
        ):
            members = [r for r in rows if r.get("_cohort_preset") == preset]
            cohort_n = int(members[0].get("_cohort_n") or 0) if members else 0
            presets[preset] = {
                "total": len(members),
                "cohort_n": cohort_n,
                "has_bar": bool(members[0].get("_tool_has_bar")),
                "percentile_suppressed": bool(
                    cohort_n and cohort_n < MIN_PERCENTILE_COHORT
                ),
            }

        stats[tool] = {
            "tool": tool,
            "total": len(rows),
            "cohort_n": len(ranked),
            "unranked": len(rows) - len(ranked),
            "shown": shown_counts.get(tool, 0),
            "passed": sum(1 for r in rows if r.get("_passed")),
            "metric": metric_key,
            "direction": direction,
            "has_bar": any(r.get("_tool_has_bar") for r in rows),
            "percentile_suppressed": bool(ranked) and not any(
                r.get("_rank_percentile") is not None for r in ranked
            ),
            "presets": presets,
        }
    return stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def rank_candidates(
    rows: Iterable[Mapping[str, Any]],
    *,
    limit: Optional[int] = DEFAULT_LIMIT,
    sort_mode: str = SORT_PERCENTILE,
) -> dict[str, Any]:
    """Rank, cap and order a target's pooled designs.

    Returns::

        {
          "rows": [...],      # selected rows, in display order
          "tools": {...},     # build_tool_stats mapping, slug keyed
          "total": int,       # rows in, before the cap
          "shown": int,       # len(rows)
          "unranked": int,    # rows with no usable primary metric
          "capped": bool,
          "limit": int | None,
          "sort_mode": str,   # the mode actually applied
        }

    Each returned row is a shallow copy of the caller's dict plus the
    :data:`ANNOTATION_KEYS` and :data:`SELECTION_KEYS`. ``_rank_position`` is
    the 1-based position in CANONICAL order within the selected set, so it
    still reads as the design's rank when the table is grouped by tool.

    An unrecognised ``sort_mode`` falls back to :data:`SORT_PERCENTILE`
    rather than raising, because it arrives from a query string; the mode
    actually applied comes back in the envelope.
    """
    effective_mode = sort_mode if sort_mode in SORT_MODES else SORT_PERCENTILE

    annotated = annotate_rows(rows)
    ordered = sort_canonical(annotated)
    selected = select_under_cap(ordered, limit)
    for position, row in enumerate(selected, start=1):
        row["_rank_position"] = position

    return {
        "rows": apply_sort_mode(selected, effective_mode),
        "tools": build_tool_stats(annotated, selected),
        "total": len(annotated),
        "shown": len(selected),
        "unranked": sum(1 for r in annotated if not r.get("_ranked")),
        "capped": len(selected) < len(annotated),
        "limit": limit,
        "sort_mode": effective_mode,
    }
