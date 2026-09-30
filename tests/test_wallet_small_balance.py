"""A $5 wallet can launch a default rfantibody or proteina run, and the
money-safety invariant (total spend never exceeds funded money) still holds
once the holds are that small.

The QA pass of 2026-09-30 (docs/qa/QA-2026-09-30-functional.md, uncommitted
QA note) saw rfantibody hold $6.55 and proteina hold $15.00 for runs that
settled at $0.63 and $0.80, so neither could start on a $5 signup credit.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from shared import wallet
from shared import wallet_estimates as we
from shared.wallet import REASON_OK, reserve_hold, wallet_preflight
from tests.test_wallet import _FakeClient, _seed_wallet, _Store

_MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
_USER = "00000000-0000-0000-0000-0000000000f5"
_FIVE = Decimal("5.00")

# The params each form sends by default: rfantibody's num_designs falls back
# to "4" (tools/rfantibody/__init__.py, raw_num_designs); proteina prices one
# 8-design shard (TOOL_SPECS["proteina"], _CHUNK_SIZE_OVERRIDE).
_DEFAULT_RUNS = [
    ("rfantibody", {"num_designs": 4}, Decimal("2.9131"), Decimal("4.3697")),
    ("rfantibody", {"num_designs": 2}, Decimal("1.4566"), Decimal("2.1849")),
    ("proteina", {"num_designs": 8}, Decimal("2.4466"), Decimal("3.6699")),
]


@pytest.fixture
def five_dollar_wallet(monkeypatch):
    monkeypatch.setattr(we, "_historical_p90_seconds", lambda slug: None)
    store = _Store()
    _seed_wallet(store, _USER, balance=_FIVE)
    client = _FakeClient(store)
    with patch("shared.wallet.get_service_client", return_value=client), \
            patch.object(wallet, "_send_email_safe", lambda *a, **kw: None):
        yield store


@pytest.mark.parametrize("slug,params,quote,hold", _DEFAULT_RUNS)
def test_default_run_passes_preflight_on_five_dollars(
    five_dollar_wallet, slug, params, quote, hold
):
    assert we.estimated_cost_for_tool(None, slug, params) == quote
    assert we.cushioned_hold_usd(None, slug, params) == hold
    # wallet_guard preflights the estimate, then the hold; both must pass.
    for amount in (quote, hold):
        pre = wallet_preflight(_USER, slug, amount, params)
        assert pre.allow and pre.reason == REASON_OK, (slug, amount, pre.reason)
    assert reserve_hold(_USER, slug, 1, hold, params) is not None


def test_second_hold_past_the_balance_is_refused(five_dollar_wallet):
    """Two proteina shards need $7.34; a $5 wallet funds only the first."""
    hold = we.cushioned_hold_usd(None, "proteina", {"num_designs": 8})
    assert reserve_hold(_USER, "proteina", 1, hold, {"num_designs": 8}) is not None
    assert reserve_hold(_USER, "proteina", 2, hold, {"num_designs": 8}) is None
    balance = wallet._wallet(_USER)["balance_usd"]
    assert Decimal(str(balance)) == _FIVE - hold


def _latest_definition(function: str) -> str:
    """Body of the newest migration's CREATE OR REPLACE for ``function``."""
    pattern = re.compile(
        rf"CREATE OR REPLACE FUNCTION public\.{function}\(.*?\$\$;", re.S
    )
    for path in sorted(_MIGRATIONS.glob("*.sql"), reverse=True):
        match = pattern.search(path.read_text(encoding="utf-8"))
        if match:
            return match.group(0)
    raise AssertionError(f"no migration defines {function}")


def test_hold_refuses_past_balance_under_lock_in_sql():
    """The race-safe half of the invariant lives in SQL, not in the fake."""
    body = _latest_definition("try_hold_for_job")
    lock = body.index("FOR UPDATE")
    check = body.index("IF v_balance < p_amount_usd THEN")
    assert lock < check
    assert body[check:].lstrip().split("\n")[1].strip() == "RETURN NULL;"


def test_settle_overrun_never_takes_balance_below_zero_in_sql():
    """With holds now well under a full container session, an overrun is
    expected; settle must debit it only while the balance covers it."""
    body = _latest_definition("settle_hold")
    assert "LEAST(p_actual_usd, p_hard_cap_usd)" in body
    debit = body.index("IF v_balance + v_diff >= 0 THEN")
    absorbed = body.index("'absorbed_variance', 0, v_balance", debit)
    assert "ELSE" in body[debit:absorbed]


@pytest.mark.parametrize("num_designs", [2, 4, 16])
def test_rfantibody_hold_covers_the_largest_admitted_target(
    monkeypatch, num_designs,
):
    """The price is blind to target size, so the hold must cover the biggest
    target preflight lets through, on the preflight runtime curve."""
    from shared.pdb_preflight_rules import _RFANTIBODY, runtime_estimate_min
    from shared.wallet import compute_charge_usd

    monkeypatch.setattr(we, "_historical_p90_seconds", lambda slug: None)
    minutes = runtime_estimate_min(
        _RFANTIBODY, _RFANTIBODY.size.hard_cap_target_aa, num_designs,
    )
    worst = compute_charge_usd(minutes * 60, "A100-40GB")
    hold = we.cushioned_hold_usd(
        None, "rfantibody", {"num_designs": num_designs},
    )
    assert hold >= worst
