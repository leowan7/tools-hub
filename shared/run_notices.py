"""Customer notices for a finished run that stopped early.

Derived when the page or email renders, from values already on the job.
Nothing here is stored.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

_CENT = Decimal("0.01")


def to_cents(value) -> Optional[Decimal]:  # noqa: ANN001
    try:
        amount = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    if not amount.is_finite():
        return None
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def _designs(n: int) -> str:
    return f"{n} design" if n == 1 else f"{n} designs"


def partial_line(job) -> str:  # noqa: ANN001
    """The "stopped at its time limit after N of M designs" line, or "".

    Only a succeeded job whose result carries ``partial`` gets a line
    (tools/proteina/run_pipeline.py sets it when the search exits nonzero after
    scoring designs). "time limit" is said only when ``search.status`` is
    "timeout"; any other nonzero exit says "stopped early". N is the designs the
    result delivered; M is the job's design-count input, left out when unknown
    or not above N.
    """
    if getattr(job, "status", None) != "succeeded":
        return ""
    result = getattr(job, "result", None)
    if not isinstance(result, dict) or not result.get("partial"):
        return ""
    from shared.compute_campaigns import design_param_key  # noqa: PLC0415
    from shared.jobs import candidate_records  # noqa: PLC0415

    n = len(candidate_records(result))
    key = design_param_key(getattr(job, "tool", "") or "")
    try:
        m = int((getattr(job, "inputs", None) or {}).get(key)) if key else 0
    except (TypeError, ValueError):
        m = 0
    search = result.get("search")
    timed_out = isinstance(search, dict) and search.get("status") == "timeout"
    head = "This run stopped at its time limit" if timed_out else "This run stopped early"
    if m > n:
        return f"{head} after {n} of {_designs(m)}."
    return f"{head} after {_designs(n)}."


def run_notices(job) -> list[str]:  # noqa: ANN001
    """Every notice that applies to ``job``, in display order."""
    return [line for line in (partial_line(job),) if line]


def campaign_partial_line(campaign, delivered: int, partial_chunks: int, timeout_chunks: int) -> str:  # noqa: ANN001
    """The campaign-page form of ``partial_line``, or "" when no sub-job was partial.

    Counts come from ``shared.compute_campaigns.aggregate_campaign_candidates``.
    "time limit" is said only when every partial sub-job timed out. M is
    ``campaign.requested_designs``, left out when not above ``delivered``.
    """
    if partial_chunks <= 0:
        return ""
    head = (
        "Part of this run stopped at its time limit"
        if timeout_chunks == partial_chunks
        else "Part of this run stopped early"
    )
    m = int(getattr(campaign, "requested_designs", 0) or 0)
    if m > delivered:
        return f"{head}: {delivered} of {_designs(m)} came back."
    return f"{head}: {_designs(delivered)} came back."
