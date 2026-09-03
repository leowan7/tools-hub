"""Behaviour tests for shared.ranking.

A mis-ranked table looks completely fine, so every assertion here computes
the real numbers and reads them back. There are deliberately no assertions
on module source text: a substring check passes for a comment and fails for
a rename, which is the opposite of what this file is for.
"""

from __future__ import annotations

import json
import random
import types

import pytest

from shared import ranking

pytestmark = pytest.mark.usefixtures("isolate_supabase")


def _reject_constant(token):
    """json.loads hook: a strict parser rejects Infinity/NaN, so must we."""
    raise AssertionError(f"envelope carries the non-JSON token {token}")


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def _row(
    tool: str,
    *,
    job: str,
    index: int,
    preset=None,
    scores=None,
    campaign: str = "camp-1",
    **extra,
) -> dict:
    """One provenance-tagged candidate row as the aggregation layer builds it."""
    row = {
        "_source_tool": tool,
        "_source_preset": preset,
        "_source_campaign_id": campaign,
        "_source_job_id": job,
        "_source_index": index,
    }
    if scores is not None:
        row["scores"] = dict(scores)
    row.update(extra)
    return row


def _metric_rows(tool, metric, values, *, job, preset=None, extra_scores=None):
    """One row per value, indexed in the order given."""
    out = []
    for idx, value in enumerate(values):
        scores = {metric: value}
        if extra_scores:
            scores.update(extra_scores)
        out.append(_row(tool, job=job, index=idx, preset=preset, scores=scores))
    return out


def _ident(row) -> tuple:
    return (
        row["_source_tool"], row["_source_job_id"], row["_source_index"],
    )


def _find(result, tool, index):
    for row in result["rows"]:
        if row["_source_tool"] == tool and row["_source_index"] == index:
            return row
    raise AssertionError(f"row {tool}/{index} not in result")


# ---------------------------------------------------------------------------
# Pass regime: the cohort-scope decision
# ---------------------------------------------------------------------------

def test_tool_with_no_bar_is_not_demoted_below_a_gated_tool():
    """The single most important behaviour in the module.

    bindcraft declares no quality bar at all; pxdesign does, and here every
    pxdesign row MEETS it while scoring lower. If a tool without a bar were
    treated as failing one, the whole table would be pxdesign first, i.e.
    partitioned on tool identity rather than design quality.

    The regime is now a property of the TOOL (score_legends.GATE_COLUMNS)
    rather than an inference from whether some row happened to carry a stored
    word. That is a stronger version of the same guarantee: it no longer
    depends on which container version produced the rows.
    """
    rows = _metric_rows(
        "bindcraft", "ipTM", [0.99 - 0.01 * i for i in range(10)], job="bc-1",
    ) + _metric_rows(
        "pxdesign", "ipTM", [0.85 - 0.01 * i for i in range(10)], job="px-1",
        extra_scores={"pLDDT": 88.0, "pAE": 3.0},
    )

    out = ranking.rank_candidates(rows, limit=None)

    assert all(r["_passed"] for r in out["rows"])
    assert out["tools"]["bindcraft"]["has_bar"] is False
    assert out["tools"]["pxdesign"]["has_bar"] is True
    # Identical rank structure in both cohorts, so the two tools alternate.
    assert [r["_source_tool"] for r in out["rows"]] == ["bindcraft", "pxdesign"] * 10


def test_a_row_short_of_its_own_bar_sorts_below_a_bar_free_tools_rows():
    """passed leads the sort key even against a much better raw score.

    boltzgen is the gated tool here on purpose: its bar is pLDDT and refolding
    RMSD and NOT the ipTM it ranks on, so a row's rank and its verdict can be
    set independently. That separation is the point of
    GATE_COLUMNS["boltzgen"] -- an ipTM leg is what mislabelled 65 production
    designs.
    """
    rows = _metric_rows(
        "boltzgen", "ipTM", [0.99, 0.98], job="bg-short",
        extra_scores={"pLDDT": 40.0, "refolding_rmsd": 1.0},
    ) + [
        _row("boltzgen", job="bg-meets", index=i,
             scores={"ipTM": 0.70 - 0.01 * i, "pLDDT": 88.0,
                     "refolding_rmsd": 1.0})
        for i in range(3)
    ] + _metric_rows(
        "bindcraft", "ipTM", [0.50 - 0.01 * i for i in range(5)], job="bc-1",
    )

    out = ranking.rank_candidates(rows, limit=None)

    assert [_ident(r) for r in out["rows"][-2:]] == [
        ("boltzgen", "bg-short", 0), ("boltzgen", "bg-short", 1),
    ]
    assert all(r["_passed"] for r in out["rows"][:-2])


def test_an_unmeasured_row_is_not_failed_by_a_fully_measured_sibling():
    """Unjudged is not failed, even inside a cohort whose tool HAS a bar.

    A job rebuilt by shared/job_recovery holds only what was streamed during
    the run, and boltzgen's refold happens at the end, so one cohort holds
    fully measured rows beside rows missing a whole leg. Calling those
    failures would invert the table: ``passed`` leads the sort key, so the
    tool's best designs would sink below every other row and get pushed past
    the cap, while a much worse fully measured design leads.
    """
    # One row is fully measured; four are missing refolding_rmsd entirely.
    minimal = [
        _row("boltzgen", job="bg-1", index=0,
             scores={"ipTM": 0.50, "pLDDT": 88.0, "refolding_rmsd": 1.0}),
    ] + _metric_rows(
        "boltzgen", "ipTM", [0.90 - 0.01 * i for i in range(4)], job="bg-2",
        extra_scores={"pLDDT": 88.0},
    )

    out = ranking.rank_candidates(minimal, limit=None)

    assert out["tools"]["boltzgen"]["has_bar"] is True
    assert all(r["_passed"] for r in out["rows"])
    assert out["tools"]["boltzgen"]["passed"] == 5
    # The four better designs lead; the measured 0.50 row is last on merit.
    assert [r["_metric_value"] for r in out["rows"]] == [
        pytest.approx(v) for v in (0.90, 0.89, 0.88, 0.87, 0.50)
    ]

    # At scale: 240 recovered rows must not be demoted below another tool.
    # This is the probed production shape -- 240 recovered rows at ipTM 0.99
    # sank below 100 bindcraft rows at 0.70, the best design was handed rank
    # 161, and 100 rows fell past the 300 cap out of the table entirely.
    rows = (
        [_row("boltzgen", job="bg-live", index=i,
              scores={"ipTM": 0.55 - 0.001 * i, "pLDDT": 88.0,
                      "refolding_rmsd": 1.0})
         for i in range(60)]
        + [_row("boltzgen", job="bg-recovered", index=i,
                scores={"ipTM": 0.99 - 0.001 * i, "pLDDT": 88.0})
           for i in range(240)]
        + _metric_rows("bindcraft", "ipTM", [0.70] * 100, job="bc-1")
    )

    out = ranking.rank_candidates(rows)          # DEFAULT_LIMIT = 300

    assert out["tools"]["boltzgen"]["passed"] == 300      # not 60
    assert out["tools"]["boltzgen"]["has_bar"] is True
    best = next(
        r for r in out["rows"]
        if r["_source_job_id"] == "bg-recovered" and r["_source_index"] == 0
    )
    assert best["_metric_value"] == pytest.approx(0.99)
    assert best["_rank_position"] == 1
    assert [r["_source_job_id"] for r in out["rows"][:5]] == ["bg-recovered"] * 5


# ---------------------------------------------------------------------------
# Cohort identity
# ---------------------------------------------------------------------------

def test_two_jobs_of_one_tool_at_the_same_preset_share_one_cohort():
    rows = _metric_rows(
        "bindcraft", "ipTM", [0.9, 0.8, 0.7], job="bc-a", preset="default",
    ) + _metric_rows(
        "bindcraft", "ipTM", [0.6, 0.5, 0.4], job="bc-b", preset="default",
    )

    out = ranking.rank_candidates(rows, limit=None)

    assert {r["_cohort_n"] for r in out["rows"]} == {6}
    assert out["tools"]["bindcraft"]["cohort_n"] == 6
    assert _find(out, "bindcraft", 0)["_rank_within_cohort"] == 1


def test_two_presets_of_one_tool_do_not_share_a_cohort():
    """proteina total_reward means a different quantity per preset, so the
    best protein_binder design is top of ITS population even though every
    ligand_binder reward is numerically larger."""
    rows = _metric_rows(
        "proteina", "total_reward", [-3.0 - 0.1 * i for i in range(20)],
        job="pro-prot", preset="protein_binder",
    ) + _metric_rows(
        "proteina", "total_reward", [12.0 - 0.1 * i for i in range(20)],
        job="pro-lig", preset="ligand_binder",
    )

    out = ranking.rank_candidates(rows, limit=None)
    best_protein = next(
        r for r in out["rows"] if r["_source_job_id"] == "pro-prot"
        and r["_source_index"] == 0
    )

    assert best_protein["_cohort_n"] == 20        # not 40
    assert best_protein["_rank_within_cohort"] == 1
    assert best_protein["_rank_percentile"] == 97  # would be 48 pooled
    assert out["tools"]["proteina"]["presets"]["protein_binder"]["cohort_n"] == 20
    assert out["tools"]["proteina"]["presets"]["ligand_binder"]["cohort_n"] == 20
    # The TOOL-level cohort_n sums the two cohorts and is therefore NOT a
    # percentile denominator: no row was ever ranked out of 40. Pinned so the
    # page cannot render it as "rank k of 40" from the tool block alone.
    assert out["tools"]["proteina"]["cohort_n"] == 40
    assert {r["_cohort_n"] for r in out["rows"]} == {20}


def test_a_blank_preset_and_an_absent_preset_are_one_cohort():
    """"" and None both mean "no preset", so they are one population.

    Splitting them would halve the denominator and overstate every percentile
    on both sides: the top design would report rank 1 of 25 at the 98th
    percentile when it is really rank 1 of 50 at the 99th.
    """
    rows = (
        _metric_rows(
            "bindcraft", "ipTM", [0.99 - 0.001 * i for i in range(20)],
            job="bc-none", preset=None,
        )
        + _metric_rows(
            "bindcraft", "ipTM", [0.97 - 0.001 * i for i in range(20)],
            job="bc-blank", preset="",
        )
        + _metric_rows(
            "bindcraft", "ipTM", [0.95 - 0.001 * i for i in range(10)],
            job="bc-space", preset="   ",
        )
    )

    out = ranking.rank_candidates(rows, limit=None)

    assert {r["_cohort_n"] for r in out["rows"]} == {50}
    assert {r["_cohort_preset"] for r in out["rows"]} == {None}
    assert list(out["tools"]["bindcraft"]["presets"]) == [None]
    assert out["rows"][0]["_rank_within_cohort"] == 1
    assert out["rows"][0]["_rank_percentile"] == 99      # would be 98 split


def test_one_tool_splits_on_preset_even_when_one_side_has_none():
    """The cohort key is (tool, preset) EXACTLY, which is a contract on the
    caller, not just a fact about this module.

    A campaign path stamping ``campaign.preset`` beside a standalone path that
    leaves ``_source_preset`` unset gives one tool two cohorts. The module
    cannot tell that apart from a genuine two-preset tool, so it does what it
    is told and the aggregation layer owes every row of a tool the same
    preset value. Pinned with the consequence spelled out, because the number
    it produces looks perfectly reasonable on the page.
    """
    rows = _metric_rows(
        "bindcraft", "ipTM", [0.99 - 0.001 * i for i in range(300)],
        job="camp", preset="default",
    ) + _metric_rows(
        "bindcraft", "ipTM", [0.60 - 0.01 * i for i in range(6)],
        job="solo", preset=None,
    )

    out = ranking.rank_candidates(rows, limit=None)
    solo_best = next(
        r for r in out["rows"]
        if r["_source_job_id"] == "solo" and r["_source_index"] == 0
    )

    assert sorted(
        out["tools"]["bindcraft"]["presets"], key=lambda p: (p is None, str(p)),
    ) == ["default", None]
    assert solo_best["_cohort_n"] == 6                   # not 306
    assert solo_best["_rank_fraction"] == pytest.approx(11 / 12)
    assert solo_best["_rank_percentile"] is None         # cohort under 20
    # 301st of 306 on the tool's own metric, shown at table position 26, with
    # 275 strictly better designs of the SAME tool sitting below it.
    assert solo_best["_rank_position"] == 26
    assert sum(
        1 for r in out["rows"][26:]
        if r["_metric_value"] is not None and r["_metric_value"] > 0.60
    ) == 275


# ---------------------------------------------------------------------------
# The statistic
# ---------------------------------------------------------------------------

def test_tied_integer_metric_gives_every_tied_row_the_same_percentile():
    """Ten iggm designs tied at the cohort maximum share one mid-rank
    percentile. Strict-better ranking would hand each of them 99."""
    rows = _metric_rows(
        "iggm", "epitope_contacts", [12] * 10 + [4] * 10, job="ig-1",
    )

    out = ranking.rank_candidates(rows, limit=None)
    top = [r for r in out["rows"] if r["_metric_value"] == 12]
    bottom = [r for r in out["rows"] if r["_metric_value"] == 4]

    assert len(top) == 10 and len(bottom) == 10
    assert {r["_rank_percentile"] for r in top} == {75}
    assert {r["_rank_within_cohort"] for r in top} == {1}
    assert {r["_rank_percentile"] for r in bottom} == {25}
    assert {r["_rank_within_cohort"] for r in bottom} == {11}


def test_percentile_is_computed_before_the_cap_not_after():
    rows = _metric_rows(
        "bindcraft", "ipTM", [0.99 - 0.001 * i for i in range(40)], job="bc-1",
    )

    capped = ranking.rank_candidates(rows, limit=5)
    uncapped = ranking.rank_candidates(rows, limit=None)

    assert capped["shown"] == 5 and capped["total"] == 40 and capped["capped"]
    assert capped["rows"][0]["_cohort_n"] == 40
    # Over 40 rows the best is the 98th percentile; recomputed over the 5
    # survivors it would be the 90th.
    assert capped["rows"][0]["_rank_percentile"] == 98
    assert capped["rows"][4]["_rank_percentile"] == 88
    assert [r["_rank_percentile"] for r in capped["rows"]] == [
        r["_rank_percentile"] for r in uncapped["rows"][:5]
    ]


def test_no_caller_is_ever_handed_a_hundredth_percentile():
    """Unreachable from a real cohort (a row is tied with itself), so this
    pins the direct-call path the clamp actually guards."""
    assert ranking.rank_statistics([1.0, 2.0], 5.0, "desc") == (1.0, 99, 1)
    big = _metric_rows(
        "bindcraft", "ipTM", [0.99 - 0.001 * i for i in range(200)], job="bc-1",
    )
    out = ranking.rank_candidates(big, limit=None)
    assert max(r["_rank_percentile"] for r in out["rows"]) == 99


def test_a_tie_on_rank_fraction_is_broken_toward_the_larger_cohort():
    """0.975 out of 60 designs is a better evidenced claim than 0.975 out of
    20. The tie-break must not fall through to the alphabetical tool slug,
    which here would put the smaller cohort first."""
    rows = _metric_rows(
        "boltzgen", "ipTM", [0.99 - 0.001 * i for i in range(60)], job="bg-1",
    ) + _metric_rows(
        "bindcraft", "ipTM", [0.80 - 0.001 * i for i in range(20)], job="bc-1",
    )

    out = ranking.rank_candidates(rows, limit=None)
    big = _find(out, "boltzgen", 1)     # rank 2 of 60 -> 117/120
    small = _find(out, "bindcraft", 0)  # rank 1 of 20 ->  39/40

    assert big["_rank_fraction"] == small["_rank_fraction"]
    assert big["_cohort_n"] == 60 and small["_cohort_n"] == 20
    assert big["_rank_position"] == 2
    assert small["_rank_position"] == 3


def test_asc_direction_ranks_lower_values_better():
    """rfantibody ranks on interface pAE, where lower is better."""
    rows = _metric_rows(
        "rfantibody", "ipAE", [3.0 + 0.1 * i for i in range(20)], job="rfab-1",
    )

    out = ranking.rank_candidates(rows, limit=None)

    assert out["tools"]["rfantibody"]["direction"] == "asc"
    assert out["rows"][0]["_metric_value"] == pytest.approx(3.0)
    assert out["rows"][0]["_rank_within_cohort"] == 1
    assert out["rows"][0]["_rank_percentile"] == 97
    assert out["rows"][-1]["_metric_value"] == pytest.approx(4.9)
    assert out["rows"][-1]["_rank_within_cohort"] == 20
    assert out["rows"][-1]["_rank_percentile"] == 2


def test_root_level_metric_alias_resolves_and_ranks():
    """iggm persists n_epitope_contacts at the record root; its declared
    primary metric is epitope_contacts."""
    rows = [
        _row("iggm", job="ig-1", index=i, n_epitope_contacts=20 - i)
        for i in range(20)
    ]

    out = ranking.rank_candidates(rows, limit=None)

    assert out["tools"]["iggm"]["cohort_n"] == 20
    assert out["tools"]["iggm"]["unranked"] == 0
    assert out["rows"][0]["_metric_value"] == pytest.approx(20)
    assert out["rows"][0]["_rank_within_cohort"] == 1


# ---------------------------------------------------------------------------
# Null, missing and non-finite metrics
# ---------------------------------------------------------------------------

def test_unresolvable_metric_is_excluded_counted_and_sorted_last():
    """A recovered rfantibody job carries no ipAE. Those rows must not sit
    in the denominator, must be counted as unranked, and must fall below
    every ranked row of their own tool."""
    rows = _metric_rows(
        "rfantibody", "ipAE", [3.0 + 0.1 * i for i in range(25)], job="rfab-1",
    ) + [
        _row("rfantibody", job="rfab-recovered", index=i,
             scores={"pLDDT": 80.0})
        for i in range(5)
    ]

    out = ranking.rank_candidates(rows, limit=None)

    assert out["tools"]["rfantibody"]["total"] == 30
    assert out["tools"]["rfantibody"]["cohort_n"] == 25
    assert out["tools"]["rfantibody"]["unranked"] == 5
    assert out["unranked"] == 5
    assert {r["_cohort_n"] for r in out["rows"]} == {25}
    tail = out["rows"][25:]
    assert all(r["_source_job_id"] == "rfab-recovered" for r in tail)
    assert all(r["_ranked"] is False for r in tail)
    assert all(r["_rank_percentile"] is None for r in tail)
    # No percentile is being WITHHELD from these rows, there is none to
    # compute. The page owes the reader "no metric", not "small sample", and
    # this flag is what decides which copy it shows.
    assert all(r["_percentile_suppressed"] is False for r in tail)


def test_tool_with_no_registered_primary_metric_is_unranked_not_a_crash():
    rows = _metric_rows(
        "esmfold2_design", "plddt", [90.0, 80.0, 70.0], job="esm-1",
    ) + _metric_rows(
        "bindcraft", "ipTM", [0.9, 0.8], job="bc-1",
    )

    out = ranking.rank_candidates(rows, limit=None)
    esm = [r for r in out["rows"] if r["_source_tool"] == "esmfold2_design"]

    assert out["tools"]["esmfold2_design"]["metric"] is None
    assert out["tools"]["esmfold2_design"]["cohort_n"] == 0
    assert out["tools"]["esmfold2_design"]["unranked"] == 3
    assert out["tools"]["esmfold2_design"]["percentile_suppressed"] is False
    assert all(r["_ranked"] is False for r in esm)
    assert all(r["_rank_fraction"] is None for r in esm)
    # Unrankable rows sit below the ranked ones, in a stable explicable order.
    assert [r["_source_tool"] for r in out["rows"]] == (
        ["bindcraft"] * 2 + ["esmfold2_design"] * 3
    )
    assert [r["_source_index"] for r in esm] == [0, 1, 2]


def test_cohort_with_no_resolvable_values_does_not_divide_by_zero():
    rows = _metric_rows("bindcraft", "pLDDT", [90.0, 85.0, 80.0], job="bc-1")

    out = ranking.rank_candidates(rows, limit=None)

    assert out["tools"]["bindcraft"]["metric"] == "ipTM"
    assert out["tools"]["bindcraft"]["cohort_n"] == 0
    assert out["tools"]["bindcraft"]["unranked"] == 3
    assert all(r["_cohort_n"] == 0 for r in out["rows"])
    assert ranking.rank_statistics([], 1.0, "desc") == (0.0, 0, 0)


def test_nan_metric_is_unranked_and_sorted_last():
    """float() accepts NaN, and a NaN left inside the cohort would leave
    sorted_values unsorted, making every bisect in that cohort meaningless."""
    rows = _metric_rows(
        "bindcraft", "ipTM", [0.5 + 0.01 * i for i in range(20)], job="bc-1",
    ) + [_row("bindcraft", job="bc-nan", index=0,
              scores={"ipTM": float("nan")})]

    out = ranking.rank_candidates(rows, limit=None)
    nan_row = next(r for r in out["rows"] if r["_source_job_id"] == "bc-nan")

    assert nan_row["_ranked"] is False
    assert nan_row["_metric_value"] is None
    assert nan_row["_rank_fraction"] is None
    assert out["rows"][-1] is nan_row
    assert out["tools"]["bindcraft"]["cohort_n"] == 20
    assert out["tools"]["bindcraft"]["unranked"] == 1


def test_infinite_metric_is_unranked_and_keeps_the_annotations_json_safe():
    """An infinite metric is corrupt data, not an extreme design.

    Ranking it costs twice. It takes rank 1 on a value nobody measured and,
    being a cohort member, it demotes every real design by one rank. And
    ``_metric_value`` is written into the returned envelope, where json.dumps
    emits infinity as the bare token ``Infinity``: not JSON, so a browser
    parsing the response fails on the WHOLE body and the page shows no
    designs at all rather than one odd row.

    The jsonb string is the vector that matters, because a row read back from
    jsonb cannot carry a bare Infinity (JSON has no token for one) but CAN
    carry the string "1e999", and candidate_metric coerces with a bare
    float(). Scope note, checked rather than assumed: the module cannot make
    an arbitrary payload safe, since each row is a shallow copy and the
    caller's own ``scores`` dict passes through untouched. What it guarantees
    is that nothing IT writes is non-finite.
    """
    rows = _metric_rows(
        "bindcraft", "ipTM", [0.5 + 0.01 * i for i in range(20)], job="bc-1",
    ) + [
        _row("bindcraft", job="bc-inf", index=0, scores={"ipTM": "1e999"}),
        _row("bindcraft", job="bc-inf", index=1, scores={"ipTM": "inf"}),
        _row("bindcraft", job="bc-inf", index=2, scores={"ipTM": "-inf"}),
        _row("bindcraft", job="bc-inf", index=3,
             scores={"ipTM": float("inf")}),
    ]

    out = ranking.rank_candidates(rows, limit=None)
    infinite = [r for r in out["rows"] if r["_source_job_id"] == "bc-inf"]

    assert len(infinite) == 4
    assert all(r["_ranked"] is False for r in infinite)
    assert all(r["_metric_value"] is None for r in infinite)
    assert all(r["_rank_fraction"] is None for r in infinite)
    # The denominator is the 20 real designs, and the best of them keeps the
    # rank an infinite row would have taken off it.
    assert out["tools"]["bindcraft"]["cohort_n"] == 20
    assert out["tools"]["bindcraft"]["unranked"] == 4
    assert out["rows"][0]["_metric_value"] == pytest.approx(0.69)
    assert out["rows"][0]["_rank_within_cohort"] == 1
    assert out["rows"][0]["_rank_percentile"] == 97
    assert [r["_source_job_id"] for r in out["rows"][-4:]] == ["bc-inf"] * 4

    # Every annotation this module writes survives a strict JSON round trip.
    annotated_only = {
        **{k: v for k, v in out.items() if k != "rows"},
        "rows": [
            {k: v for k, v in r.items()
             if k in ranking.ANNOTATION_KEYS + ranking.SELECTION_KEYS}
            for r in out["rows"]
        ],
    }
    payload = json.dumps(annotated_only)
    assert "Infinity" not in payload and "NaN" not in payload
    json.loads(payload, parse_constant=_reject_constant)

    # And the jsonb-sourced rows, payload included, are clean: a stored "inf"
    # is a STRING and serializes as one. Only the caller-built float row
    # (index 3, unreachable from a jsonb read) carries a bare token.
    from_jsonb = json.dumps({
        "rows": [r for r in out["rows"] if r["_source_index"] != 3],
    })
    assert "Infinity" not in from_jsonb
    json.loads(from_jsonb, parse_constant=_reject_constant)


# ---------------------------------------------------------------------------
# Small cohorts
# ---------------------------------------------------------------------------

def test_small_cohort_suppresses_the_percentile_but_still_orders():
    """A 5-design run gets no percentile and a k-of-n rank instead, but it
    keeps its sorted position: its best design must not be buried under a
    large run's mediocre ones."""
    rows = _metric_rows(
        "bindcraft", "ipTM", [0.90 - 0.01 * i for i in range(5)], job="bc-1",
    ) + _metric_rows(
        "boltzgen", "ipTM", [0.99 - 0.001 * i for i in range(40)], job="bg-1",
    )

    out = ranking.rank_candidates(rows, limit=None)
    best_small = _find(out, "bindcraft", 0)

    assert best_small["_cohort_n"] == 5
    assert best_small["_rank_percentile"] is None
    assert best_small["_percentile_suppressed"] is True
    assert best_small["_rank_within_cohort"] == 1
    assert out["tools"]["bindcraft"]["percentile_suppressed"] is True
    assert out["tools"]["boltzgen"]["percentile_suppressed"] is False
    # Exactly four boltzgen rows carry a higher rank fraction than 0.9.
    assert best_small["_rank_position"] == 5
    assert out["rows"][4] is best_small


def test_a_cohort_of_one_lands_mid_table_on_a_fraction_fixed_by_its_size():
    """n=1 gives 0.5 for ANY value: n_worse=0 and n_equal=1 is 1/2 whatever
    the design scored. So a lone design outranks half the table on the
    strength of being alone, and, being under MIN_PERCENTILE_COHORT, shows no
    percentile that would explain why it sits there. Pinned rather than
    fixed: changing it is an ordering decision, not a bug fix, and the page
    needs to know the position is as unassertable as the percentile.
    """
    assert ranking.rank_statistics([25.0], 25.0, "asc") == (0.5, 50, 1)
    assert ranking.rank_statistics([0.99], 0.99, "desc") == (0.5, 50, 1)

    # iggm, because it declares no quality bar. rfantibody stood here and now
    # HAS one (pLDDT / ipAE / pAE), so a lone row on a very poor ipAE is sunk
    # by its own measurement before the cohort statistic gets to speak -- which
    # is the right outcome, and is pinned separately below. This test is about
    # the statistic, so it needs a tool the bar cannot interfere with.
    rows = [
        _row("iggm", job="iggm-1", index=0, scores={"epitope_contacts": 1.0}),
    ] + _metric_rows(
        "bindcraft", "ipTM", [0.50 + 0.01 * i for i in range(40)], job="bc-1",
    )

    out = ranking.rank_candidates(rows, limit=None)
    lone = _find(out, "iggm", 0)

    assert lone["_cohort_n"] == 1
    assert lone["_rank_fraction"] == 0.5
    assert lone["_rank_percentile"] is None
    assert lone["_percentile_suppressed"] is True
    assert lone["_rank_position"] == 21     # of 41, on 1 epitope contact
    assert sum(1 for r in out["rows"][21:] if r["_ranked"]) == 20


def test_a_lone_row_of_a_GATED_tool_sinks_on_its_own_measurement():
    """The counterpart, and an improvement this change brought with it.

    The cohort-of-one statistic puts any lone design mid-table whatever it
    scored. When the tool declares a bar, a design measured short of it is now
    sunk on that measurement first, because ``passed`` leads the sort key. The
    fraction is still 0.5; it just no longer decides where the row lands.
    """
    rows = [
        _row("rfantibody", job="rfab-1", index=0,
             scores={"ipAE": 25.0, "pLDDT": 88.0, "pAE": 3.0}),
    ] + _metric_rows(
        "bindcraft", "ipTM", [0.50 + 0.01 * i for i in range(40)], job="bc-1",
    )

    out = ranking.rank_candidates(rows, limit=None)
    lone = _find(out, "rfantibody", 0)

    assert lone["_rank_fraction"] == 0.5
    assert lone["_passed"] is False
    assert out["rows"][-1] is lone


# ---------------------------------------------------------------------------
# Selection under a cap
# ---------------------------------------------------------------------------

def _floor_scenario():
    return _metric_rows(
        "bindcraft", "ipTM", [0.99 - 0.001 * i for i in range(300)], job="bc-1",
    ) + _metric_rows(
        "iggm", "epitope_contacts", [9, 7, 5, 3], job="ig-1",
    )


def test_per_tool_floor_keeps_every_contributing_tool_in_a_capped_result():
    rows = _floor_scenario()

    # Premise: the 4-design tool tops out at a 0.875 rank fraction, below
    # every one of the top 20 bindcraft rows, so a plain top-N drops it.
    plain = ranking.sort_canonical(ranking.annotate_rows(rows))
    assert all(r["_source_tool"] == "bindcraft" for r in plain[:20])

    out = ranking.rank_candidates(rows, limit=20)
    iggm_rows = [r for r in out["rows"] if r["_source_tool"] == "iggm"]

    assert out["shown"] == 20
    assert len(iggm_rows) == 4                      # min(PER_TOOL_FLOOR, 4)
    assert out["tools"]["iggm"]["shown"] == 4
    assert out["tools"]["bindcraft"]["shown"] == 16
    assert all(r["_floor_reserved"] is True for r in iggm_rows)


def test_the_floor_does_not_spend_the_cap_on_a_tools_failed_designs():
    """A tool whose every design its own filter rejected reserves nothing.

    Measured before the passing-only rule: 6 failed pxdesign designs against 30
    passing bindcraft ones at a cap of 10 gave pxdesign 5 of the 10 slots, so
    half the visible table was designs that failed quality control and 5 passing
    designs were pushed out of a table whose whole claim is that it ranks the
    good ones.

    The pair below is the other half and both are needed: flip pxdesign's rows
    to passing and its floor must come back, or "reserves nothing" could be
    satisfied by a floor that had simply stopped working.
    """
    # boltzgen, whose bar (pLDDT / refolding RMSD) is independent of the ipTM
    # it ranks on, so the same poor-ranking rows can be put either side of the
    # bar without changing their position in the sort.
    def _scenario(plddt):
        bg = _metric_rows(
            "boltzgen", "ipTM", [0.30 + 0.01 * i for i in range(6)], job="bg-1",
            extra_scores={"pLDDT": plddt, "refolding_rmsd": 1.0},
        )
        bc = _metric_rows(
            "bindcraft", "ipTM", [0.90 - 0.001 * i for i in range(30)], job="bc-1",
        )
        return bg + bc

    failed = ranking.rank_candidates(_scenario(40.0), limit=10)
    shown_tools = {r["_source_tool"] for r in failed["rows"]}

    assert shown_tools == {"bindcraft"}, "a rejected design took a capped slot"
    assert failed["tools"]["boltzgen"]["shown"] == 0
    # The tool is not hidden: its stats still report what it produced.
    assert failed["tools"]["boltzgen"]["total"] == 6
    assert not any(r["_floor_reserved"] for r in failed["rows"])

    passed = ranking.rank_candidates(_scenario(88.0), limit=10)

    assert passed["tools"]["boltzgen"]["shown"] == ranking.PER_TOOL_FLOOR
    assert any(r["_floor_reserved"] for r in passed["rows"])


def test_floor_reserved_rows_keep_their_sorted_position():
    """The floor changes membership, never order: the capped table still
    reads monotonically by rank fraction WITHIN the passed bucket.

    Every row of this fixture passes (no cohort carries a filter signal), so
    here the passed bucket is the whole table. The qualification is not
    decoration: see the failed-row test below, where the fraction column is
    deliberately NOT monotone across the bucket boundary.
    """
    out = ranking.rank_candidates(_floor_scenario(), limit=20)
    fractions = [r["_rank_fraction"] for r in out["rows"]]

    assert all(r["_passed"] for r in out["rows"])
    assert fractions == sorted(fractions, reverse=True)
    assert out["rows"][0]["_source_tool"] == "bindcraft"
    assert [r["_rank_position"] for r in out["rows"]] == list(range(1, 21))


def test_failed_rows_restart_the_fraction_range_below_the_passed_bucket():
    """The percentile column is monotone per bucket, not down the table.

    ``passed`` leads canonical_sort_key, so the failed rows are appended after
    the ranked list carrying their own independent fraction range. That
    discontinuity is intended (a design its own pipeline rejected does not
    belong above one nobody rejected) but it is invisible in the numbers: the
    row above the boundary can show the 2nd percentile and the row below it
    the 98th. Pinned so the template renders the failed rows as a separated
    group rather than as a continuation of the ranked list.
    """
    rows = (
        [_row("boltzgen", job="bg-short", index=i,
              scores={"ipTM": 0.99 - 0.001 * i, "pLDDT": 40.0,
                      "refolding_rmsd": 1.0})
         for i in range(5)]
        + [_row("boltzgen", job="bg-meets", index=i,
                scores={"ipTM": 0.60 - 0.001 * i, "pLDDT": 88.0,
                        "refolding_rmsd": 1.0})
           for i in range(20)]
        + _metric_rows(
            "bindcraft", "ipTM", [0.50 - 0.001 * i for i in range(25)],
            job="bc-1",
        )
    )

    out = ranking.rank_candidates(rows, limit=None)
    fractions = [r["_rank_fraction"] for r in out["rows"]]
    passed = [r for r in out["rows"] if r["_passed"]]
    failed = [r for r in out["rows"] if not r["_passed"]]

    # Every failed row sits below every passed row, on a much better metric.
    assert len(failed) == 5
    assert out["rows"][-5:] == failed
    assert min(r["_metric_value"] for r in failed) > max(
        r["_metric_value"] for r in passed
    )
    # Monotone inside each bucket...
    for bucket in (passed, failed):
        bucket_fractions = [r["_rank_fraction"] for r in bucket]
        assert bucket_fractions == sorted(bucket_fractions, reverse=True)
    # ...and NOT monotone down the table as a whole.
    assert fractions != sorted(fractions, reverse=True)
    assert passed[-1]["_rank_percentile"] < failed[0]["_rank_percentile"]


def _seven_tool_scenario():
    """Seven contributing tools; rfdiffusion (last in slug order) is strongest.

    Its cohort is large enough that its top rows out-rank every other tool's
    best, so canonical positions 0, 1 and 2 are all rfdiffusion. The other six
    hold 6 designs each, whose best possible rank fraction is 11/12.
    """
    rows = _metric_rows(
        "rfdiffusion", "ipTM", [0.99 - 0.001 * i for i in range(40)],
        job="rfd-1",
    )
    for tool, metric, values in (
        ("bindcraft", "ipTM", [0.90 - 0.01 * i for i in range(6)]),
        ("boltzgen", "ipTM", [0.88 - 0.01 * i for i in range(6)]),
        ("pxdesign", "ipTM", [0.86 - 0.01 * i for i in range(6)]),
        ("proteina", "total_reward", [-2.0 - 0.1 * i for i in range(6)]),
        ("iggm", "epitope_contacts", [12 - i for i in range(6)]),
        ("rfantibody", "ipAE", [3.0 + 0.1 * i for i in range(6)]),
    ):
        rows += _metric_rows(tool, metric, values, job=f"{tool}-1")
    return rows


def test_a_cap_below_the_tool_count_still_keeps_the_best_design():
    """The floor must never evict canonical position 0.

    With a cap smaller than the number of contributing tools the round robin
    cannot reach every tool. Visiting them in slug order spent the whole cap
    on the alphabetically first ones, so the target's best design was absent
    from the table and _rank_position 1 went to a strictly worse design, with
    nothing in the output to signal the omission. Best-row order makes the
    head of the canonical order unevictable.
    """
    rows = _seven_tool_scenario()
    canonical = ranking.sort_canonical(ranking.annotate_rows(rows))

    # Premise: the strongest tool is last in slug order and holds the top 3.
    assert [_ident(r) for r in canonical[:3]] == [
        ("rfdiffusion", "rfd-1", 0),
        ("rfdiffusion", "rfd-1", 1),
        ("rfdiffusion", "rfd-1", 2),
    ]

    out = ranking.rank_candidates(rows, limit=3)

    assert _ident(out["rows"][0]) == ("rfdiffusion", "rfd-1", 0)
    assert out["rows"][0]["_rank_position"] == 1
    # Hand-computed, NOT max(r["_rank_fraction"] for r in canonical). Both
    # sides of that comparison came from annotate_rows on the same input, so
    # any error in the statistic moved them together and the line could not
    # fail with the implementation. rfdiffusion's cohort is 40 distinct values,
    # so its best row has n_worse=39 and n_equal=1: (39 + 0.5) / 40 = 79/80.
    assert out["rows"][0]["_rank_fraction"] == pytest.approx(79 / 80)
    # The best design survives every cap, not just this one.
    for cap in range(1, 9):
        capped = ranking.rank_candidates(rows, limit=cap)
        assert _ident(capped["rows"][0]) == ("rfdiffusion", "rfd-1", 0), cap


def test_a_cap_at_the_tool_count_leaves_no_tool_at_zero():
    """The floor's own claim, at the smallest cap that can honour it."""
    rows = _seven_tool_scenario()

    out = ranking.rank_candidates(rows, limit=7)

    assert out["shown"] == 7
    assert sorted(r["_source_tool"] for r in out["rows"]) == sorted(
        out["tools"]
    )
    assert all(stats["shown"] == 1 for stats in out["tools"].values())


def test_limit_none_returns_every_row_uncapped():
    rows = _floor_scenario()

    out = ranking.rank_candidates(rows, limit=None)

    assert out["shown"] == len(rows) == out["total"]
    assert out["capped"] is False
    assert all(r["_floor_reserved"] is False for r in out["rows"])


# ---------------------------------------------------------------------------
# Determinism and display order
# ---------------------------------------------------------------------------

def _mixed_rows():
    return (
        _metric_rows(
            "bindcraft", "ipTM", [0.9 - 0.005 * i for i in range(30)],
            job="bc-1",
        )
        + _metric_rows(
            "pxdesign", "ipTM", [0.8 - 0.005 * i for i in range(25)],
            job="px-1",
            extra_scores={"filter_status": "pass"},
        )
        + [
            _row("pxdesign", job="px-2", index=i,
                 scores={"ipTM": 0.95, "filter_status": "fail"})
            for i in range(4)
        ]
        + _metric_rows(
            "rfantibody", "ipAE", [3.0 + 0.05 * i for i in range(22)],
            job="rfab-1",
        )
        + _metric_rows(
            "iggm", "epitope_contacts", [12] * 7 + [8] * 7 + [4] * 7,
            job="ig-1",
        )
        + _metric_rows("proteina", "total_reward", [-2.0, -3.0, -4.0],
                       job="pro-1", preset="protein_binder")
        + _metric_rows("proteina", "total_reward", [11.0, 10.0],
                       job="pro-2", preset="ligand_binder")
        + [_row("esmfold2_design", job="esm-1", index=i, scores={"plddt": 80})
           for i in range(3)]
        + [_row("boltzgen", job="bg-1", index=i, scores={"pLDDT": 70.0})
           for i in range(2)]
    )


def test_ranking_is_deterministic_across_repeats_and_input_shuffles():
    rows = _mixed_rows()
    shuffled = list(rows)
    random.Random(1234).shuffle(shuffled)

    first = ranking.rank_candidates(rows, limit=40)
    second = ranking.rank_candidates(rows, limit=40)
    from_shuffled = ranking.rank_candidates(shuffled, limit=40)

    order = [_ident(r) for r in first["rows"]]
    assert [_ident(r) for r in second["rows"]] == order
    assert [_ident(r) for r in from_shuffled["rows"]] == order
    assert first["tools"] == from_shuffled["tools"]
    assert first["rows"] == from_shuffled["rows"]


def test_sort_mode_changes_the_order_but_not_the_set():
    rows = _mixed_rows()

    by_percentile = ranking.rank_candidates(
        rows, limit=40, sort_mode=ranking.SORT_PERCENTILE,
    )
    by_tool = ranking.rank_candidates(
        rows, limit=40, sort_mode=ranking.SORT_TOOL,
    )

    assert {_ident(r) for r in by_tool["rows"]} == {
        _ident(r) for r in by_percentile["rows"]
    }
    assert [_ident(r) for r in by_tool["rows"]] != [
        _ident(r) for r in by_percentile["rows"]
    ]
    slugs = [r["_source_tool"] for r in by_tool["rows"]]
    assert slugs == sorted(slugs)
    for slug in set(slugs):
        positions = [
            r["_rank_position"] for r in by_tool["rows"]
            if r["_source_tool"] == slug
        ]
        assert positions == sorted(positions)


def test_unknown_sort_mode_falls_back_to_percentile():
    rows = _mixed_rows()

    out = ranking.rank_candidates(rows, limit=10, sort_mode="native_score")
    canonical = ranking.rank_candidates(rows, limit=10)

    assert out["sort_mode"] == ranking.SORT_PERCENTILE
    assert [_ident(r) for r in out["rows"]] == [
        _ident(r) for r in canonical["rows"]
    ]


# ---------------------------------------------------------------------------
# Contract hygiene
# ---------------------------------------------------------------------------

def test_rank_candidates_does_not_mutate_caller_rows():
    rows = _mixed_rows()
    before = [dict(r) for r in rows]

    ranking.rank_candidates(rows, limit=10)

    assert rows == before


def test_select_under_cap_stamps_floor_reserved_in_place():
    """The one function that does write to the dicts it is handed. Named so
    the module-wide "rows are never mutated" claim stays honest: it is safe
    only because annotate_rows copied first.
    """
    uncapped = ranking.sort_canonical(ranking.annotate_rows(
        _metric_rows("bindcraft", "ipTM", [0.9, 0.8, 0.7], job="bc-1"),
    ))

    ranking.select_under_cap(uncapped, None)

    assert all(r["_floor_reserved"] is False for r in uncapped)

    capped = ranking.sort_canonical(ranking.annotate_rows(
        _metric_rows("bindcraft", "ipTM", [0.9, 0.8, 0.7, 0.6], job="bc-1"),
    ))

    ranking.select_under_cap(capped, 2)

    # Only the SELECTED rows are stamped; the rest are left untouched.
    assert [("_floor_reserved" in r) for r in capped] == [
        True, True, False, False,
    ]


def test_passed_here_is_not_the_same_number_as_the_counting_helper():
    """One result, two legitimate answers. Pinned because a page that shows
    both would otherwise look like it is contradicting itself.

    A design whose gate columns were not all measured is passed HERE (ordering
    sinks a row only on evidence it fell short) and not counted THERE
    (counting claims a design met the bar, which needs evidence for).
    """
    from shared import jobs

    candidates = [
        {"scores": {"ipTM": 0.90, "pLDDT": 88.0, "pAE": 3.0}},   # measured
        {"scores": {"ipTM": 0.99}},                              # unmeasured
    ]
    rows = [
        _row("pxdesign", job="px-1", index=i, preset="default",
             scores=c["scores"])
        for i, c in enumerate(candidates)
    ]

    out = ranking.rank_candidates(rows, limit=None)

    assert jobs.count_candidates_meeting_bar(
        {"candidates": candidates}, "pxdesign",
    ) == 1
    assert out["tools"]["pxdesign"]["passed"] == 2
    assert out["tools"]["pxdesign"]["has_bar"] is True


def test_a_non_coercible_source_index_degrades_one_row_without_raising():
    """_as_sort_int's job is that tuple comparison cannot raise. int(inf)
    raises OverflowError, which is neither TypeError nor ValueError, and it
    escaped the whole ranking call rather than degrading one row."""
    for bad in (float("inf"), float("-inf"), None, "abc", [1], {"a": 1}):
        assert ranking.canonical_sort_key({"_source_index": bad})[-1] == -1

    rows = _metric_rows("bindcraft", "ipTM", [0.9, 0.8], job="bc-1")
    rows[0]["_source_index"] = float("inf")

    out = ranking.rank_candidates(rows, limit=None)

    assert out["shown"] == 2
    assert [r["_metric_value"] for r in out["rows"]] == [
        pytest.approx(0.9), pytest.approx(0.8),
    ]


def test_a_read_only_mapping_row_is_ranked_like_a_dict():
    """The signature says Iterable[Mapping]; a dict-only guard silently
    dropped a valid MappingProxyType row and returned an empty table for a
    target that has designs."""
    rows = [
        types.MappingProxyType(
            _row("bindcraft", job="bc-1", index=i, scores={"ipTM": 0.9 - 0.1 * i}),
        )
        for i in range(3)
    ]

    out = ranking.rank_candidates(rows, limit=None)

    assert out["total"] == 3 and out["shown"] == 3
    assert out["tools"]["bindcraft"]["cohort_n"] == 3
    assert [r["_rank_within_cohort"] for r in out["rows"]] == [1, 2, 3]
    # Genuine non-mappings are still dropped rather than raising.
    assert ranking.rank_candidates(
        [dict(rows[0]), None, "junk", 42, ["x"]], limit=None,
    )["total"] == 1


def test_every_returned_row_carries_the_documented_annotations():
    out = ranking.rank_candidates(_mixed_rows(), limit=10)

    for row in out["rows"]:
        for key in ranking.ANNOTATION_KEYS + ranking.SELECTION_KEYS:
            assert key in row, key


# ---------------------------------------------------------------------------
# ordinal / ordinal_suffix
#
# Presentation, but it lives here because the number being suffixed is the one
# this module computes. The Pctile cell hardcoded "th", so 27 of the 100
# reachable percentiles rendered wrong and "93th" shipped beside "97th".
# ---------------------------------------------------------------------------

def test_the_three_irregular_suffixes():
    assert ranking.ordinal(1) == "1st"
    assert ranking.ordinal(2) == "2nd"
    assert ranking.ordinal(3) == "3rd"
    assert ranking.ordinal(4) == "4th"


def test_the_teens_are_th_not_st_nd_rd():
    """The case a last-digit rule gets wrong, and the reason the modulo 100
    check runs before the modulo 10 one."""
    assert ranking.ordinal(11) == "11th"
    assert ranking.ordinal(12) == "12th"
    assert ranking.ordinal(13) == "13th"


def test_the_twenties_resume_the_irregular_suffixes():
    assert ranking.ordinal(21) == "21st"
    assert ranking.ordinal(22) == "22nd"
    assert ranking.ordinal(23) == "23rd"
    assert ranking.ordinal(93) == "93rd"


def test_every_reachable_percentile_agrees_with_english():
    """rank_statistics clamps to 0..99, so this is the whole reachable domain.

    Written as an independent enumeration rather than as a second copy of the
    implementation: a re-derivation that shares the implementation's rule would
    share its bug.
    """
    st = {1: "st", 21: "st", 31: "st", 41: "st", 51: "st", 61: "st", 71: "st",
          81: "st", 91: "st"}
    nd = {2: "nd", 22: "nd", 32: "nd", 42: "nd", 52: "nd", 62: "nd", 72: "nd",
          82: "nd", 92: "nd"}
    rd = {3: "rd", 23: "rd", 33: "rd", 43: "rd", 53: "rd", 63: "rd", 73: "rd",
          83: "rd", 93: "rd"}
    expected = {**st, **nd, **rd}
    wrong = [n for n in range(100)
             if ranking.ordinal(n) != f"{n}{expected.get(n, 'th')}"]
    assert wrong == [], wrong
    # And the count that makes this worth a function: a bare "th" is wrong for
    # 27 of the 100, which is why the old cell was visibly broken rather than
    # pedantically so.
    assert len(expected) == 27


def test_a_missing_or_unparseable_value_degrades_to_th():
    """The cell only calls this when the percentile is not None, so these are
    defensive. They must not raise: a 500 on the results page would be a far
    worse outcome than a wrong suffix."""
    assert ranking.ordinal_suffix(None) == "th"
    assert ranking.ordinal_suffix("abc") == "th"
    assert ranking.ordinal_suffix("21") == "st"
