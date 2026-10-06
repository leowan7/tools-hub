"""Live charging, option (i') of docs/design/LIVE-CHARGING-2026-10-01.md."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from shared import jobs as jobs_mod
from shared.wallet import (
    REASON_INSUFFICIENT,
    REASON_OK,
    REASON_PER_TOOL_CAP,
    REASON_SELF_SERVE_CEILING,
    REASON_WALLET_FROZEN,
    PreflightResult,
    compute_charge_usd,
)
from shared.wallet_estimates import TOOL_SPECS, compute_hard_cap, gpu_class_for_job
from shared.wallet_guard import REASON_WALLET_EMPTY, live_charging_enabled

pytestmark = pytest.mark.usefixtures("isolate_supabase")

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
LIVE = {"af2", "colabfold", "esmfold", "boltz2"}


# ---------------------------------------------------------------------------
# Enable list: only tiers whose container uploads each design as it finishes
# ---------------------------------------------------------------------------


def _upload_sites(tool: str) -> list[tuple[tuple[str, ...], bool]]:
    """(enclosing function chain, inside a loop) for every upload_pdb call."""
    path = ROOT / "tools" / tool / "run_pipeline.py"
    sites: list = []

    def walk(node, funcs, in_loop):  # noqa: ANN001
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, funcs + (child.name,), False)
                continue
            if isinstance(child, ast.Call) and getattr(child.func, "id", None) == "upload_pdb":
                sites.append((funcs, in_loop))
            walk(child, funcs, in_loop or isinstance(child, (ast.For, ast.While)))

    walk(ast.parse(path.read_text(encoding="utf-8")), (), False)
    return sites


@pytest.mark.parametrize("tool,chain,needs_loop", [
    ("af2", ("_run_batch", "_dispatch"), False),
    ("colabfold", ("_run_batch", "_dispatch"), False),
    ("esmfold", ("_run_batch_folds",), True),
    ("boltz2", ("main",), True),
])
def test_live_tools_upload_each_design_mid_run(tool, chain, needs_loop):
    sites = _upload_sites(tool)
    assert sites
    for funcs, in_loop in sites:
        assert funcs == chain
        assert in_loop or not needs_loop


@pytest.mark.parametrize("tool", ["af2", "colabfold", "esmfold"])
def test_batch_records_reach_the_container_only_on_the_batch_preset(tool):
    import importlib

    build = importlib.import_module(f"tools.{tool}").build_payload
    inputs = {"num_recycles": 3, "use_templates": False, "model_preset": "monomer",
              "fasta_records": [], "fasta_text": ">a\nMK\n",
              "batch_records": [{"name": "a", "sequence": "MK"}]}
    assert "batch_records" in build({**inputs, "preset": "batch"}, "")
    assert "batch_records" not in build({**inputs, "preset": "standalone"}, "")


@pytest.mark.parametrize("slug", sorted(TOOL_SPECS))
def test_live_charging_only_on_the_enabled_tiers(slug):
    for preset in ("", "standalone", "batch", "pilot", "full", "smoke", "validate"):
        expected = slug == "boltz2" or (slug in LIVE and preset == "batch")
        assert live_charging_enabled(slug, {"preset": preset}) is expected


def test_enabled_tools_are_priced_tools():
    assert LIVE <= set(TOOL_SPECS)


# ---------------------------------------------------------------------------
# requires_wallet: hold = min(balance, cap); the empty-wallet refusal at balance <= $0
# ---------------------------------------------------------------------------


def _post(slug, form, *, balance=None, state=None, estimate=Decimal("2.00"),
          reason=None, reserve=None, cushion=Decimal("1.23")):
    """POST through requires_wallet with the wallet layer faked."""
    from flask import Flask, g

    from shared.wallet_guard import requires_wallet

    state = state if state is not None else {"balance": Decimal(str(balance))}
    seen: dict = {}

    @requires_wallet(tool_slug=slug)
    def handler():
        seen["hold_tx_id"] = g.wallet_hold_tx_id
        seen["hold_usd"] = getattr(g, "wallet_hold_usd", None)
        seen["limited"] = getattr(g, "wallet_balance_limited", None)
        g.wallet_hold_consumed = True
        return "RAN"

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "k"
    app.add_url_rule("/x", view_func=handler, methods=["POST"])
    app.add_url_rule("/tools/<tool>", endpoint="tools.tool_form", view_func=lambda tool: "form")

    def preflight(_uid, _slug, amount, _params):
        bal = state["balance"]
        why = reason or (REASON_OK if bal >= amount else REASON_INSUFFICIENT)
        return PreflightResult(
            allow=why == REASON_OK, reason=why, estimated_cost_usd=amount,
            balance_usd=bal, deficit_usd=max(amount - bal, Decimal("0")),
            hard_cap_usd=Decimal("999"),
        )

    reserve_mock = MagicMock(side_effect=reserve or (lambda *a: "tx-1"))
    with app.test_client() as c, patch(
        "shared.wallet_guard.estimated_cost_for_tool", return_value=estimate,
    ), patch(
        "shared.wallet_guard.get_or_create_wallet",
        side_effect=lambda _uid: {"balance_usd": state["balance"]},
    ), patch(
        "shared.wallet_guard.wallet_preflight", side_effect=preflight,
    ), patch(
        "shared.wallet_guard.cushioned_hold_usd", return_value=cushion,
    ), patch(
        "shared.wallet_guard.wallet_reserve_hold", reserve_mock,
    ), patch(
        "shared.wallet_guard.wallet_release_hold",
    ), patch(
        "shared.wallet_guard.render_template", return_value="GATE",
    ) as render:
        with c.session_transaction() as sess:
            sess["user_id"] = "u-1"
        body = c.post("/x", data=form).get_data(as_text=True)
    gate = render.call_args.kwargs.get("gate_reason") if render.called else None
    return SimpleNamespace(body=body, seen=seen, reserve=reserve_mock, gate=gate, state=state)


def _amounts(reserve_mock):
    return [call.args[3] for call in reserve_mock.call_args_list]


@pytest.mark.parametrize("slug,form", [
    ("boltz2", {}),
    ("af2", {"preset": "batch"}),
    ("colabfold", {"preset": "batch"}),
    ("esmfold", {"preset": "batch"}),
])
def test_balance_under_the_cap_holds_the_whole_balance(slug, form):
    cap = compute_hard_cap(slug, {k: v for k, v in form.items()})
    balance = (cap / 3).quantize(Decimal("0.01"))
    r = _post(slug, form, balance=balance)
    assert r.body == "RAN"
    assert _amounts(r.reserve) == [balance]
    assert r.seen == {"hold_tx_id": "tx-1", "hold_usd": balance, "limited": True}


def test_balance_over_the_cap_holds_the_cap():
    cap = compute_hard_cap("boltz2", {})
    r = _post("boltz2", {}, balance=cap + 50)
    assert _amounts(r.reserve) == [cap]
    assert r.seen == {"hold_tx_id": "tx-1", "hold_usd": cap, "limited": False}


@pytest.mark.parametrize("balance", ["0", "0.00"])
def test_empty_wallet_is_refused(balance):
    r = _post("boltz2", {}, balance=balance)
    assert r.body == "GATE"
    assert r.gate == REASON_WALLET_EMPTY
    r.reserve.assert_not_called()


def test_null_hold_retries_once_on_a_fresh_balance():
    state = {"balance": Decimal("0.30")}

    def reserve(*_a):
        if len(r_calls) == 0:
            r_calls.append(1)
            state["balance"] = Decimal("0.10")
            return None
        return "tx-2"

    r_calls: list = []
    r = _post("boltz2", {}, state=state, reserve=reserve)
    assert _amounts(r.reserve) == [Decimal("0.30"), Decimal("0.10")]
    assert r.seen["hold_tx_id"] == "tx-2"
    assert r.seen["hold_usd"] == Decimal("0.10")


def test_two_null_holds_show_the_retry_gate():
    r = _post("boltz2", {}, balance="0.30", reserve=lambda *_a: None)
    assert r.body == "GATE"
    assert r.gate == "hold_failed"
    assert len(_amounts(r.reserve)) == 2


def test_null_hold_then_empty_wallet_is_refused_as_empty():
    state = {"balance": Decimal("0.30")}

    def reserve(*_a):
        state["balance"] = Decimal("0")

    r = _post("boltz2", {}, state=state, reserve=reserve)
    assert r.gate == REASON_WALLET_EMPTY
    assert len(_amounts(r.reserve)) == 1


@pytest.mark.parametrize("why", [REASON_WALLET_FROZEN, REASON_PER_TOOL_CAP, REASON_SELF_SERVE_CEILING])
def test_live_tools_keep_the_other_refusals(why):
    r = _post("boltz2", {}, balance="50", reason=why)
    assert r.gate == why
    r.reserve.assert_not_called()


@pytest.mark.parametrize("slug,form", [("af2", {"preset": "standalone"}), ("bindcraft", {})])
def test_other_tools_keep_the_cushioned_hold(slug, form):
    short = _post(slug, form, balance="1.00")
    assert short.gate == REASON_INSUFFICIENT
    short.reserve.assert_not_called()

    ok = _post(slug, form, balance="50")
    assert ok.body == "RAN"
    assert _amounts(ok.reserve) == [Decimal("1.23")]
    assert ok.seen["hold_usd"] is None


def test_free_run_takes_no_hold():
    r = _post("boltz2", {}, balance="0", estimate=Decimal("0"))
    assert r.body == "RAN"
    r.reserve.assert_not_called()


# ---------------------------------------------------------------------------
# Submit route: the hold size reaches the job row
# ---------------------------------------------------------------------------


def test_submit_stashes_hold_and_balance_limited(monkeypatch):
    monkeypatch.setenv("FLAG_TOOL_AF2", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    ctx = SimpleNamespace(user_id="u-test", tier="free", balance=100, email="u@example.com")
    monkeypatch.setattr("blueprints.tools.load_user_context", lambda: ctx)
    fake_job = SimpleNamespace(id="job-1", user_id="u-test", tool="af2", preset="batch",
                               job_token="t" * 64, inputs={})
    short = PreflightResult(allow=False, reason=REASON_INSUFFICIENT,
                            estimated_cost_usd=Decimal("2.00"), balance_usd=Decimal("0.30"),
                            deficit_usd=Decimal("1.70"), hard_cap_usd=Decimal("999"))
    with patch("blueprints.tools.create_job", return_value=fake_job) as create_job, patch(
        "blueprints.tools.set_modal_call",
    ), patch("gpu.modal_client.ModalClient.submit",
             return_value={"function_call_id": "fc-1", "gpu_seconds_cap": 600}), patch(
        "shared.wallet_guard.estimated_cost_for_tool", return_value=Decimal("2.00"),
    ), patch("shared.wallet_guard.get_or_create_wallet",
             return_value={"balance_usd": Decimal("0.30")}), patch(
        "shared.wallet_guard.wallet_preflight", return_value=short,
    ), patch("shared.wallet_guard.wallet_reserve_hold", return_value="tx-1"), patch(
        "shared.wallet_guard.wallet_release_hold",
    ):
        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess["user_email"] = "u@example.com"
            sess["user_id"] = "u-test"
        client.post("/tools/af2/submit", data={
            "preset": "batch",
            "sequences": ">a\nMKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKRQTL\n",
        }, content_type="multipart/form-data")
    create_job.assert_called_once()
    assert create_job.call_args.kwargs["inputs"]["_wallet"] == {
        "hold_tx_id": "tx-1", "estimate_usd": "2.00", "hold_usd": "0.30",
        "balance_limited": True, "tool_slug": "af2",
    }


# ---------------------------------------------------------------------------
# Estimate endpoint: the form's gate agrees with requires_wallet
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool,params,balance,short", [
    ("boltz2", {}, "0.10", False),
    ("af2", {"preset": "batch"}, "0.10", False),
    ("boltz2", {}, "0", True),
    ("af2", {"preset": "standalone"}, "0.10", True),
])
def test_estimate_deficit_matches_the_guard(monkeypatch, tool, params, balance, short):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["user_email"] = "u@example.com"
        sess["user_id"] = "u-test"
    with patch("blueprints.wallet.get_or_create_wallet", return_value={"balance_usd": balance}), \
         patch("blueprints.wallet.estimated_cost_for_tool", return_value=Decimal("0.20")), \
         patch("blueprints.wallet.cushioned_hold_usd", return_value=Decimal("0.25")):
        body = client.get("/api/wallet/estimate", query_string={
            "tool": tool, "params": json.dumps(params),
        }).get_json()
    assert body["hard_block"] is False
    assert (Decimal(body["deficit_usd"]) > 0) is short


# ---------------------------------------------------------------------------
# Stop check (campaigns:tick) over an in-memory tool_jobs table
# ---------------------------------------------------------------------------


def _col(row, col):
    if "->" not in col:
        return row.get(col)
    head, *path = col.replace("->>", "->").split("->")
    value = row.get(head)
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)


class _Query:
    def __init__(self, store, payload=None):
        self.store, self.payload, self.filters, self.one = store, payload, [], False

    def select(self, *_a):
        return self

    def update(self, payload):
        return _Query(self.store, payload)

    def eq(self, col, val):
        self.filters.append((col, lambda v: v == val))
        return self

    def in_(self, col, vals):
        self.filters.append((col, lambda v: v in vals))
        return self

    def single(self):
        self.one = True
        return self

    def execute(self):
        hits = [r for r in self.store.rows.values()
                if all(test(_col(r, c)) for c, test in self.filters)]
        if self.payload is not None:
            for r in hits:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in hits])
        if self.one:
            return SimpleNamespace(data=dict(hits[0]) if hits else None)
        return SimpleNamespace(data=[dict(r) for r in hits])


class _Jobs:
    def __init__(self, *rows):
        self.rows = {r["id"]: r for r in rows}

    def client(self):
        return SimpleNamespace(table=lambda _name: _Query(self))


HOLD = Decimal("0.30")


def _job_row(job_id="job-1", wallet=None, **over):
    row = {
        "id": job_id, "user_id": "u-1", "tool": "boltz2", "preset": "pilot",
        "status": "running", "result": None, "error": None,
        "modal_function_call_id": "fc-1", "job_token": "t" * 64,
        "gpu_seconds_used": None, "created_at": "2026-10-01T00:00:00+00:00",
        "started_at": NOW.isoformat(), "completed_at": None,
        "campaign_id": None, "failure_class": None,
        "inputs": {"_wallet": wallet if wallet is not None else {
            "hold_tx_id": "hold-1", "hold_usd": str(HOLD),
            "balance_limited": True, "tool_slug": "boltz2",
        }},
    }
    row.update(over)
    return row


def _seconds_to_spend(hold, tool="boltz2"):
    gpu = gpu_class_for_job(tool, None)
    t = 1
    while compute_charge_usd(t, gpu) < hold:
        t += 1
    return t


def _stop(store, elapsed, *, cancel=None, candidates=(), reconstruct_exc=None):
    modal = MagicMock()
    modal.cancel.side_effect = cancel or (lambda _fc: {"ok": True, "error": None})
    rec = MagicMock(side_effect=reconstruct_exc, return_value=list(candidates))
    with patch.object(jobs_mod, "get_service_client", store.client), \
         patch.object(jobs_mod, "_charge_workspace_for_completed_job"), \
         patch.object(jobs_mod, "_send_completion_email") as email, \
         patch("shared.job_recovery.reconstruct", rec), \
         patch("shared.wallet.settle_hold") as settle, \
         patch("shared.wallet.release_hold") as release:
        summary = jobs_mod.stop_wallet_limited_jobs(
            modal_client=modal, now=NOW + timedelta(seconds=elapsed),
        )
    return SimpleNamespace(summary=summary, modal=modal, settle=settle,
                           release=release, email=email, rec=rec)


DESIGNS = [{"name": "d1", "pdb_key": "designs/d1.pdb"},
           {"name": "d2", "pdb_key": "designs/d2.pdb"}]


def test_run_keeps_going_while_its_cost_is_under_the_hold():
    store = _Jobs(_job_row())
    r = _stop(store, _seconds_to_spend(HOLD) - 1, candidates=DESIGNS)
    assert r.summary["stopped"] == 0
    assert store.rows["job-1"]["status"] == "running"
    r.modal.cancel.assert_not_called()


def test_run_stops_when_its_cost_reaches_the_hold():
    store = _Jobs(_job_row())
    t = _seconds_to_spend(HOLD)
    r = _stop(store, t, candidates=DESIGNS)
    row = store.rows["job-1"]
    assert r.summary["stopped"] == 1
    r.modal.cancel.assert_called_once_with("fc-1")
    assert row["status"] == "succeeded"
    assert row["failure_class"] == "succeeded"
    assert row["gpu_seconds_used"] == t
    assert row["result"]["partial"] is True
    assert row["result"]["stop_reason"] == jobs_mod.WALLET_STOP_REASON
    assert [c["name"] for c in row["result"]["candidates"]] == ["d1", "d2"]
    r.settle.assert_called_once()
    assert r.settle.call_args.args == ("hold-1",)
    assert r.settle.call_args.kwargs["gpu_seconds"] == t
    r.release.assert_not_called()
    r.email.assert_called_once()


def test_run_stops_when_its_cost_equals_the_hold_exactly():
    t = 600
    exact = compute_charge_usd(t, gpu_class_for_job("boltz2", None))
    store = _Jobs(_job_row(wallet={
        "hold_tx_id": "hold-1", "hold_usd": str(exact),
        "balance_limited": True, "tool_slug": "boltz2",
    }))
    r = _stop(store, t, candidates=DESIGNS)
    assert r.summary["stopped"] == 1
    assert store.rows["job-1"]["status"] == "succeeded"


def test_stop_before_any_design_finished_is_billed_as_no_yield():
    store = _Jobs(_job_row())
    r = _stop(store, _seconds_to_spend(HOLD))
    assert store.rows["job-1"]["failure_class"] == "completed_no_yield"
    r.settle.assert_called_once()
    r.release.assert_not_called()


def test_reconstruct_error_still_stops_the_run():
    store = _Jobs(_job_row())
    r = _stop(store, _seconds_to_spend(HOLD), reconstruct_exc=RuntimeError("storage"))
    assert r.summary["stopped"] == 1
    assert store.rows["job-1"]["failure_class"] == "completed_no_yield"


@pytest.mark.parametrize("status", ["pending", "running"])
def test_run_with_no_heartbeat_is_metered_from_created_at(status):
    t = _seconds_to_spend(HOLD)
    under = _Jobs(_job_row(status=status, started_at=None, created_at=NOW.isoformat()))
    assert _stop(under, t - 1, candidates=DESIGNS).summary["stopped"] == 0
    assert under.rows["job-1"]["status"] == status

    store = _Jobs(_job_row(status=status, started_at=None, created_at=NOW.isoformat()))
    r = _stop(store, t, candidates=DESIGNS)
    row = store.rows["job-1"]
    assert r.summary["stopped"] == 1
    r.modal.cancel.assert_called_once_with("fc-1")
    assert row["status"] == "succeeded"
    assert row["gpu_seconds_used"] == t


def _raise(_fc):
    raise RuntimeError("modal down")


@pytest.mark.parametrize("cancel", [lambda _fc: {"ok": False, "error": "x"}, _raise])
def test_failed_modal_cancel_leaves_the_run_going(cancel):
    store = _Jobs(_job_row())
    r = _stop(store, _seconds_to_spend(HOLD), cancel=cancel, candidates=DESIGNS)
    assert r.summary == {"stopped": 0, "cancel_failed": 1, "errors": []}
    assert store.rows["job-1"]["status"] == "running"
    r.settle.assert_not_called()


def test_stop_rebuilds_from_the_row_as_it_is_after_the_cancel():
    store = _Jobs(_job_row())
    late = {"name": "d3", "pdb_key": "designs/d3.pdb"}

    def cancel(_fc):
        row = store.rows["job-1"]
        row["inputs"] = {**row["inputs"], "_partial_candidates": [late]}
        return {"ok": True, "error": None}

    r = _stop(store, _seconds_to_spend(HOLD), cancel=cancel, candidates=DESIGNS)
    assert r.rec.call_args.args[0].inputs["_partial_candidates"] == [late]


def test_run_with_no_modal_call_stops_without_a_cancel():
    store = _Jobs(_job_row(modal_function_call_id=None))
    r = _stop(store, _seconds_to_spend(HOLD), candidates=DESIGNS)
    assert r.summary["stopped"] == 1
    r.modal.cancel.assert_not_called()


@pytest.mark.parametrize("over", [
    {"campaign_id": "camp-1"},
    {"wallet": {"hold_tx_id": "hold-1", "hold_usd": "0.30", "balance_limited": "true"}},
    {"wallet": {"hold_tx_id": "hold-1", "balance_limited": True}},
    {"wallet": {"hold_tx_id": "hold-1", "hold_usd": "0.30", "balance_limited": False}},
    {"wallet": {"hold_tx_id": "hold-1", "estimate_usd": "2.00"}},
    {"status": "pending", "started_at": None, "modal_function_call_id": None},
])
def test_rows_the_stop_never_touches(over):
    store = _Jobs(_job_row(**over))
    status = store.rows["job-1"]["status"]
    r = _stop(store, 10 * 86400, candidates=DESIGNS)
    assert r.summary == {"stopped": 0, "cancel_failed": 0, "errors": []}
    assert store.rows["job-1"]["status"] == status
    r.modal.cancel.assert_not_called()


def test_webhook_that_lands_during_the_cancel_wins():
    store = _Jobs(_job_row())

    def webhook_first(_fc):
        store.rows["job-1"].update(status="succeeded", result={"candidates": DESIGNS})
        return {"ok": True, "error": None}

    r = _stop(store, _seconds_to_spend(HOLD), cancel=webhook_first, candidates=DESIGNS[:1])
    assert r.summary["stopped"] == 0
    assert "stop_reason" not in store.rows["job-1"]["result"]
    r.settle.assert_not_called()


# ---------------------------------------------------------------------------
# A2: a failed user cancel leaves the job running
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cancel", [lambda _fc: {"ok": False, "error": "x"}, _raise])
def test_failed_user_cancel_leaves_the_job_running(cancel):
    store = _Jobs(_job_row())
    modal = MagicMock()
    modal.cancel.side_effect = cancel
    with patch.object(jobs_mod, "get_service_client", store.client), \
         patch("shared.wallet.release_hold") as release, \
         patch("shared.wallet.settle_hold") as settle:
        job, err = jobs_mod.cancel_job("job-1", user_id="u-1", modal_client=modal)
    assert (job, err) == (None, "modal_cancel_failed")
    assert store.rows["job-1"]["status"] == "running"
    release.assert_not_called()
    settle.assert_not_called()


@pytest.mark.parametrize("cancel", [lambda _fc: {"ok": False, "error": "x"}, _raise])
def test_campaign_child_is_cancelled_locally_when_modal_cancel_fails(cancel):
    store = _Jobs(_job_row())
    modal = MagicMock()
    modal.cancel.side_effect = cancel
    with patch.object(jobs_mod, "get_service_client", store.client), \
         patch.object(jobs_mod, "_settle_wallet_hold_for_completed_job"):
        job, err = jobs_mod.cancel_job("job-1", user_id="u-1", modal_client=modal,
                                       leave_running_if_cancel_fails=False)
    assert err is None
    assert store.rows["job-1"]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Ledger: the stop settles through the real settle_hold into a mirror of
# supabase/migrations/0020_wallet_corrections.sql::settle_hold
# ---------------------------------------------------------------------------


class _Ledger:
    def __init__(self, balance):
        self.rows = [{"id": "t0", "kind": "topup", "amount_usd": Decimal(balance)}]
        self.lowest = self.balance()

    def balance(self):
        return sum(r["amount_usd"] for r in self.rows)

    def add(self, row):
        self.rows.append(row)
        self.lowest = min(self.lowest, self.balance())

    def hold(self, amount):
        assert self.balance() >= amount
        self.add({"id": "hold-1", "user_id": "u-1", "kind": "hold", "amount_usd": -amount,
                  "estimated_cost_usd": amount, "tool_slug": "boltz2"})

    def settle(self, p):
        hold = next(r for r in self.rows if r["id"] == p["p_hold_tx_id"])
        if any(r.get("parent_tx_id") == hold["id"] for r in self.rows):
            return
        actual = min(Decimal(str(p["p_actual_usd"])), Decimal(str(p["p_hard_cap_usd"])))
        diff = hold["estimated_cost_usd"] - actual
        if diff > 0:
            kind, amount = "hold_release", diff
        elif diff < 0 and self.balance() + diff >= 0:
            kind, amount = "charge", diff
        elif diff < 0:
            kind, amount = "absorbed_variance", Decimal("0")
        else:
            kind, amount = "charge", Decimal("0")
        self.add({"id": f"s{len(self.rows)}", "kind": kind, "amount_usd": amount,
                  "estimated_cost_usd": abs(diff), "parent_tx_id": hold["id"]})

    def client(self):
        ledger = self

        class _Sel:
            def select(self, *_a):
                return self

            def eq(self, _col, val):
                self.id = val
                return self

            def maybe_single(self):
                return self

            def execute(self):
                return SimpleNamespace(
                    data=next((dict(r) for r in ledger.rows if r["id"] == self.id), None))

        class _Rpc:
            def __init__(self, name, params):
                assert name == "settle_hold"
                self.params = params

            def execute(self):
                ledger.settle(self.params)

        return SimpleNamespace(table=lambda _name: _Sel(), rpc=_Rpc)


def test_sql_mirror_matches_the_migrations():
    mig = ROOT / "supabase" / "migrations"
    settle = (mig / "0020_wallet_corrections.sql").read_text(encoding="utf-8")
    for line in ("v_capped_actual := LEAST(p_actual_usd, p_hard_cap_usd);",
                 "v_diff := v_estimate - v_capped_actual;",
                 "IF v_diff > 0 THEN",
                 "IF v_balance + v_diff >= 0 THEN",
                 "SELECT v_user_id, 'absorbed_variance', 0, v_balance,",
                 "WHERE parent_tx_id = p_hold_tx_id"):
        assert line in settle
    hold = (mig / "0035_phase2_remove_daily_cap.sql").read_text(encoding="utf-8")
    assert "IF v_balance < p_amount_usd THEN" in hold
    assert "(p_user_id, 'hold', -p_amount_usd, v_balance - p_amount_usd," in hold
    assert "p_tool_slug, p_job_id, p_amount_usd)" in hold
    for fn, latest in (("settle_hold", "0020_wallet_corrections.sql"),
                       ("try_hold_for_job", "0035_phase2_remove_daily_cap.sql")):
        defs = sorted(p.name for p in mig.glob("*.sql")
                      if f"FUNCTION public.{fn}(" in p.read_text(encoding="utf-8"))
        assert defs[-1] == latest


def _stop_and_settle(ledger, elapsed):
    store = _Jobs(_job_row())
    modal = MagicMock()
    modal.cancel.return_value = {"ok": True, "error": None}
    with patch.object(jobs_mod, "get_service_client", store.client), \
         patch.object(jobs_mod, "_charge_workspace_for_completed_job"), \
         patch.object(jobs_mod, "_send_completion_email"), \
         patch("shared.job_recovery.reconstruct", return_value=DESIGNS), \
         patch("shared.wallet.get_service_client", ledger.client), \
         patch("shared.wallet._wallet", return_value=None), \
         patch("shared.wallet._post_settle_hooks"):
        summary = jobs_mod.stop_wallet_limited_jobs(
            modal_client=modal, now=NOW + timedelta(seconds=elapsed),
        )
    assert summary["stopped"] == 1
    actual = compute_charge_usd(elapsed, gpu_class_for_job("boltz2", None))
    assert actual > HOLD
    return min(actual, compute_hard_cap("boltz2", {})) - HOLD


def test_late_stop_charges_the_hold_and_absorbs_the_rest():
    ledger = _Ledger("0.30")
    ledger.hold(HOLD)
    owed = _stop_and_settle(ledger, _seconds_to_spend(HOLD) + 120)
    assert [r["kind"] for r in ledger.rows] == ["topup", "hold", "absorbed_variance"]
    assert ledger.rows[-1]["amount_usd"] == 0
    assert ledger.rows[-1]["estimated_cost_usd"] == owed
    assert ledger.balance() == 0
    assert ledger.lowest == 0


def test_overshoot_after_a_mid_run_top_up_comes_out_of_the_top_up():
    ledger = _Ledger("0.30")
    ledger.hold(HOLD)
    ledger.add({"id": "t1", "kind": "topup", "amount_usd": Decimal("20")})
    owed = _stop_and_settle(ledger, _seconds_to_spend(HOLD) + 120)
    assert ledger.rows[-1]["kind"] == "charge"
    assert ledger.balance() == Decimal("20") - owed
    assert ledger.lowest >= 0


def test_overshoot_bigger_than_the_top_up_is_absorbed_whole():
    ledger = _Ledger("0.30")
    ledger.hold(HOLD)
    ledger.add({"id": "t1", "kind": "topup", "amount_usd": Decimal("0.01")})
    assert _stop_and_settle(ledger, _seconds_to_spend(HOLD) + 600) > Decimal("0.01")
    assert ledger.rows[-1]["kind"] == "absorbed_variance"
    assert ledger.balance() == Decimal("0.01")


# ---------------------------------------------------------------------------
# campaigns:tick runs the stop check
# ---------------------------------------------------------------------------


def _tick(stop):
    from cron.tick_campaigns import tick_campaigns

    client = MagicMock()
    client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []
    with patch("shared.credits.get_service_client", return_value=client), \
         patch("shared.jobs.stop_wallet_limited_jobs", stop), \
         patch("shared.compute_campaigns.sweep_paused_campaigns", return_value={}):
        return tick_campaigns()


def test_tick_runs_the_stop_check():
    stop = MagicMock(return_value={"stopped": 1, "cancel_failed": 0, "errors": []})
    summary = _tick(stop)
    stop.assert_called_once_with()
    assert summary["wallet_stop"]["stopped"] == 1


def test_tick_counts_the_stop_check_errors():
    stop = MagicMock(return_value={"stopped": 0, "cancel_failed": 0, "errors": ["job-1:boom"]})
    summary = _tick(stop)
    assert "wallet stop: job-1:boom" in summary["errors"]


def test_tick_carries_on_when_the_stop_check_raises():
    summary = _tick(MagicMock(side_effect=RuntimeError("boom")))
    assert "wallet stop check failed" in summary["errors"]
    assert "reconciled" in summary
