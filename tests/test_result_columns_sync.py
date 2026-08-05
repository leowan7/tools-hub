"""Fence: every campaign tool's PRIMARY metric must be one of its own columns.

Phase 3's combined target table renders one Score cell per row as
``{metric label} {value}`` — the metric NAME travels beside the number so that
no reader mistakes BindCraft's ipTM for BoltzGen's. That makes a drift between
``_TOOL_PRIMARY_METRIC`` and ``_TOOL_RESULT_COLUMNS`` newly dangerous: before
Phase 3 a stale primary key merely ordered the table oddly, whereas now it
prints a metric name that the tool does not actually report next to a number
that it does.

All seven pass today. This is a fence against future edits, not a fix.

It is NOT the fuller sync test the Phase 0 plan called for (assert
``shared/result_columns.py`` matches each ``templates/tools/<tool>_results.html``
``{% set columns %}`` line, filed as register item 8/10 and still open). That one
needs a stored-result fixture per tool; ``tests/test_export_shapes.py`` carries
only two generic shapes. This covers the internal consistency of the two dicts,
which is the half Phase 3 depends on.
"""

import pytest

from shared import metric_glossary
from shared.compute_campaigns import SUPPORTED_TOOLS
from shared.result_columns import columns_for, primary_metric_for

pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.mark.parametrize("tool", sorted(SUPPORTED_TOOLS))
def test_primary_metric_is_one_of_the_tools_own_columns(tool):
    metric, direction = primary_metric_for(tool)
    columns = columns_for(tool)

    assert metric is not None, (
        f"{tool} is campaign-capable but has no registered primary metric, so "
        f"every one of its designs would be unrankable in the target table"
    )
    assert metric in columns, (
        f"{tool}'s primary metric {metric!r} is not in its column set "
        f"{columns!r}; the target table's Score cell would label the value with "
        f"a metric the tool does not report"
    )
    assert direction in ("asc", "desc"), (
        f"{tool}'s primary metric direction {direction!r} is neither asc nor "
        f"desc, so the percentile could not decide which end is better"
    )


def test_every_campaign_tool_has_columns():
    """A tool with no columns renders a row with nothing in it."""
    missing = sorted(t for t in SUPPORTED_TOOLS if not columns_for(t))
    assert missing == [], f"campaign-capable tools with no column set: {missing}"


@pytest.mark.parametrize("tool", sorted(SUPPORTED_TOOLS))
def test_primary_metric_has_a_glossary_label_and_a_format(tool):
    """The Score cell prints the primary metric's LABEL beside its value.

    A metric absent from the glossary falls back to rendering its raw key, so
    proteina's cell read "total_reward 12.346" rather than "Reward -6.12"; and a
    metric absent from _FORMAT falls to the generic ".3f", printing a precision
    the underlying number may not carry. Both were true of total_reward until
    this test existed.
    """
    metric, _direction = primary_metric_for(tool)

    assert metric in metric_glossary.GLOSSARY, (
        f"{tool}'s primary metric {metric!r} has no glossary entry, so the "
        f"target table's Score cell would label it with its raw key"
    )
    assert metric in metric_glossary._FORMAT, (
        f"{tool}'s primary metric {metric!r} has no format entry, so it would "
        f"render at the generic .3f default"
    )

    # Only that a label exists. NOT that it differs from the key: ipTM's label
    # is legitimately "ipTM", so comparing the two would conflate a correct
    # entry with the glossary's not-found fallback (which returns the key as the
    # label). The membership assertion above is what actually catches a missing
    # entry; this catches an entry present but blank.
    label = metric_glossary.get(metric)["label"]
    assert label, f"{tool}'s primary metric {metric!r} has a blank label"


def test_format_value_renders_each_primary_metric_without_raising():
    """format_value never raises by contract. Prove it on the real metrics,
    including the two shapes that reach it in practice: a float and a None."""
    for tool in sorted(SUPPORTED_TOOLS):
        metric, _ = primary_metric_for(tool)
        assert metric_glossary.format_value(metric, 0.8123) not in ("", None)
        assert metric_glossary.format_value(metric, None) == "—"
