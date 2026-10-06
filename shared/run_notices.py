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


def stopped_for_balance(job) -> bool:  # noqa: ANN001
    """True for a succeeded run the live-charging stop finished
    (shared/jobs.py::stop_wallet_limited_jobs)."""
    from shared.jobs import WALLET_STOP_REASON  # noqa: PLC0415

    result = getattr(job, "result", None)
    return (
        getattr(job, "status", None) == "succeeded"
        and isinstance(result, dict)
        and result.get("stop_reason") == WALLET_STOP_REASON
    )


def partial_line(job) -> str:  # noqa: ANN001
    """The "this run stopped ..." line for a partial run, or "".

    Only a succeeded job whose result carries ``partial`` gets a line
    (tools/proteina/run_pipeline.py sets it when the search exits nonzero after
    scoring designs; shared/jobs.py::stop_wallet_limited_jobs sets it with
    ``stop_reason``). A run stopped for balance (``stopped_for_balance``) says
    its wallet balance reached $0, after N designs or before any finished.
    Otherwise "time limit" is said when ``search.status`` is "timeout", and
    "stopped early" for any other partial result. N is the designs the result
    delivered; M is the job's design-count input, left out when unknown or not
    above N.
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
    if stopped_for_balance(job):
        head = "This run stopped when your wallet balance reached $0"
        if n == 0:
            return f"{head}, before any design finished. Add funds to run again."
        return (f"{head}, after {_designs(n)}. The finished designs are in your "
                "results and downloads. Add funds to run again.")
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
