"""Live charging, option (ii) of docs/design/LIVE-CHARGING-2026-10-01.md."""

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
    live_due_usd,
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
# requires_wallet: a $0 anchor on a live tier; the empty-wallet refusal at balance <= $0
# ---------------------------------------------------------------------------


def _post(slug, form, *, balance=None, state=None, estimate=Decimal("2.00"),
          reason=None, open_run=None, consume=True, cushion=Decimal("1.23")):
    """POST through requires_wallet with the wallet layer faked."""
    from flask import Flask, g

    from shared.wallet_guard import requires_wallet

    state = state if state is not None else {"balance": Decimal(str(balance))}
    seen: dict = {}

    @requires_wallet(tool_slug=slug)
    def handler():
        seen["hold_tx_id"] = g.wallet_hold_tx_id
        seen["live"] = getattr(g, "wallet_live", False)
        g.wallet_hold_consumed = consume
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

    open_mock = MagicMock(side_effect=open_run or (lambda *a: "anchor-1"))
    reserve_mock = MagicMock(return_value="tx-1")
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
        "shared.wallet_guard.open_live_run", open_mock,
    ), patch(
        "shared.wallet_guard.wallet_reserve_hold", reserve_mock,
    ), patch(
        "shared.wallet_guard.wallet_release_hold",
    ) as release, patch(
        "shared.wallet_guard.render_template", return_value="GATE",
    ) as render:
        with c.session_transaction() as sess:
            sess["user_id"] = "u-1"
        body = c.post("/x", data=form).get_data(as_text=True)
    gate = render.call_args.kwargs.get("gate_reason") if render.called else None
    return SimpleNamespace(body=body, seen=seen, open=open_mock, reserve=reserve_mock,
                           release=release, gate=gate)


LIVE_FORMS = [("boltz2", {}), ("af2", {"preset": "batch"}),
              ("colabfold", {"preset": "batch"}), ("esmfold", {"preset": "batch"})]


@pytest.mark.parametrize("slug,form", LIVE_FORMS)
def test_live_run_opens_an_anchor_on_any_balance_above_zero(slug, form):
    r = _post(slug, form, balance="0.01")
    assert r.body == "RAN"
    r.open.assert_called_once_with("u-1", slug)
    r.reserve.assert_not_called()
    assert r.seen == {"hold_tx_id": "anchor-1", "live": True}


@pytest.mark.parametrize("balance", ["0", "0.00"])
def test_empty_wallet_is_refused(balance):
    r = _post("boltz2", {}, balance=balance)
    assert r.body == "GATE"
    assert r.gate == REASON_WALLET_EMPTY
    r.open.assert_not_called()


def test_failed_open_shows_the_retry_gate():
    r = _post("boltz2", {}, balance="0.30", open_run=lambda *_a: None)
    assert r.gate == "hold_failed"


def test_failed_open_on_an_emptied_wallet_is_refused_as_empty():
    state = {"balance": Decimal("0.30")}

    def open_run(*_a):
        state["balance"] = Decimal("0")

    assert _post("boltz2", {}, state=state, open_run=open_run).gate == REASON_WALLET_EMPTY


def test_unused_anchor_is_released_on_an_early_return():
    r = _post("boltz2", {}, balance="5", consume=False)
    assert r.body == "RAN"
    assert r.release.call_args.args[0] == "anchor-1"


@pytest.mark.parametrize("why", [REASON_WALLET_FROZEN, REASON_PER_TOOL_CAP, REASON_SELF_SERVE_CEILING])
def test_live_tools_keep_the_other_refusals(why):
    r = _post("boltz2", {}, balance="50", reason=why)
    assert r.gate == why
    r.open.assert_not_called()


@pytest.mark.parametrize("slug,form", [("af2", {"preset": "standalone"}), ("bindcraft", {})])
def test_other_tools_keep_the_cushioned_hold(slug, form):
    short = _post(slug, form, balance="1.00")
    assert short.gate == REASON_INSUFFICIENT
    short.reserve.assert_not_called()

    ok = _post(slug, form, balance="50")
    assert ok.body == "RAN"
    assert ok.reserve.call_args.args[3] == Decimal("1.23")
    ok.open.assert_not_called()
    assert ok.seen == {"hold_tx_id": "tx-1", "live": False}


def test_free_run_takes_no_hold():
    r = _post("boltz2", {}, balance="0", estimate=Decimal("0"))
    assert r.body == "RAN"
    r.open.assert_not_called()
    r.reserve.assert_not_called()


# ---------------------------------------------------------------------------
# Submit route: the live mark reaches the job row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset,allow,wallet", [
    ("batch", False, {"hold_tx_id": "anchor-1", "estimate_usd": "2.00", "live": True,
                      "tool_slug": "af2"}),
    ("standalone", True, {"hold_tx_id": "tx-1", "estimate_usd": "2.00", "tool_slug": "af2"}),
])
def test_submit_stashes_the_live_mark(monkeypatch, preset, allow, wallet):
    monkeypatch.setenv("FLAG_TOOL_AF2", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    ctx = SimpleNamespace(user_id="u-test", tier="free", balance=100, email="u@example.com")
    monkeypatch.setattr("blueprints.tools.load_user_context", lambda: ctx)
    fake_job = SimpleNamespace(id="job-1", user_id="u-test", tool="af2", preset=preset,
                               job_token="t" * 64, inputs={})
    pre = PreflightResult(allow=allow, reason=REASON_OK if allow else REASON_INSUFFICIENT,
                          estimated_cost_usd=Decimal("2.00"), balance_usd=Decimal("0.30"),
                          deficit_usd=Decimal("0") if allow else Decimal("1.70"),
                          hard_cap_usd=Decimal("999"))
    with patch("blueprints.tools.create_job", return_value=fake_job) as create_job, patch(
        "blueprints.tools.set_modal_call",
    ), patch("gpu.modal_client.ModalClient.submit",
             return_value={"function_call_id": "fc-1", "gpu_seconds_cap": 600}), patch(
        "shared.wallet_guard.estimated_cost_for_tool", return_value=Decimal("2.00"),
    ), patch("shared.wallet_guard.get_or_create_wallet",
             return_value={"balance_usd": Decimal("0.30")}), patch(
        "shared.wallet_guard.wallet_preflight", return_value=pre,
    ), patch("shared.wallet_guard.cushioned_hold_usd", return_value=Decimal("0.25")), patch(
        "shared.wallet_guard.open_live_run", return_value="anchor-1",
    ), patch("shared.wallet_guard.wallet_reserve_hold", return_value="tx-1"), patch(
        "shared.wallet_guard.wallet_release_hold",
    ), patch("shared.idempotency.get_service_client", return_value=None):
        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess["user_email"] = "u@example.com"
            sess["user_id"] = "u-test"
        fasta = ">a\nMKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVVHSLAKWKRQTL\n"
        client.post("/tools/af2/submit", data={"preset": preset, "sequences": fasta, "fasta": fasta},
                    content_type="multipart/form-data")
    create_job.assert_called_once()
    assert create_job.call_args.kwargs["inputs"]["_wallet"] == wallet


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
# The meter (campaigns:tick) over an in-memory tool_jobs table
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
        self.order_by = None

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

    def order(self, col):
        self.order_by = col
        return self

    def single(self):
        self.one = True
        return self

    def execute(self):
        hits = [r for r in self.store.rows.values()
                if all(test(_col(r, c)) for c, test in self.filters)]
        if self.order_by:
            hits.sort(key=lambda r: r[self.order_by])
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


class _Wallet:
    """debit_live_run as supabase/migrations/0045_live_run_debits.sql writes it:
    take due less taken, as far as the balance goes (the SQL itself is proven
    by scripts/check_live_charging_local_pg.py)."""

    def __init__(self, balance):
        self.balance, self.taken, self.calls = Decimal(balance), {}, []

    def debit(self, hold_tx_id, user_id, due, gpu_seconds, gpu_class):
        self.calls.append((hold_tx_id, user_id, due, gpu_seconds, gpu_class))
        target = due - self.taken.get(hold_tx_id, Decimal("0"))
        take = min(max(target, Decimal("0")), self.balance)
        self.balance -= take
        self.taken[hold_tx_id] = self.taken.get(hold_tx_id, Decimal("0")) + take
        return {"settled": False, "debited": take, "taken": self.taken[hold_tx_id],
                "short": max(target - take, Decimal("0")), "balance_after": self.balance}


GPU = gpu_class_for_job("boltz2", None)


def _job_row(job_id="job-1", wallet=None, **over):
    row = {
        "id": job_id, "user_id": "u-1", "tool": "boltz2", "preset": "pilot",
        "status": "running", "result": None, "error": None,
        "modal_function_call_id": "fc-1", "job_token": "t" * 64,
        "gpu_seconds_used": None, "created_at": "2026-10-01T00:00:00+00:00",
        "started_at": NOW.isoformat(), "completed_at": None,
        "campaign_id": None, "failure_class": None,
        "inputs": {"_wallet": wallet if wallet is not None else {
            "hold_tx_id": "hold-1", "estimate_usd": "2.00", "live": True,
            "tool_slug": "boltz2",
        }, "_progress": {"stage": "folding"}},
    }
    row.update(over)
    return row


def _due(seconds, tool="boltz2"):
    return live_due_usd(tool, seconds, gpu_class_for_job(tool, None), {})


def _meter(store, elapsed, *, wallet=None, debit=None, cancel=None, candidates=(),
           reconstruct_exc=None):
    wallet = wallet if wallet is not None else _Wallet("0")
    modal = MagicMock()
    modal.cancel.side_effect = cancel or (lambda _fc: {"ok": True, "error": None})
    rec = MagicMock(side_effect=reconstruct_exc, return_value=list(candidates))
    with patch.object(jobs_mod, "get_service_client", store.client), \
         patch.object(jobs_mod, "_charge_workspace_for_completed_job"), \
         patch.object(jobs_mod, "_send_completion_email") as email, \
         patch("shared.job_recovery.reconstruct", rec), \
         patch("shared.wallet.debit_live_run", debit or wallet.debit), \
         patch("shared.wallet.settle_live_run") as settle, \
         patch("shared.wallet.settle_hold") as settle_hold, \
         patch("shared.wallet.release_hold") as release:
        summary = jobs_mod.meter_live_runs(
            modal_client=modal, now=NOW + timedelta(seconds=elapsed),
        )
    return SimpleNamespace(summary=summary, modal=modal, settle=settle, wallet=wallet,
                           settle_hold=settle_hold, release=release, email=email, rec=rec)


DESIGNS = [{"name": "d1", "pdb_key": "designs/d1.pdb"},
           {"name": "d2", "pdb_key": "designs/d2.pdb"}]


def test_meter_takes_what_the_run_owes_so_far():
    store, wallet = _Jobs(_job_row()), _Wallet("50")
    first = _meter(store, 60, wallet=wallet)
    assert first.summary == {"debited": 1, "stopped": 0, "cancel_failed": 0, "errors": []}
    assert wallet.calls == [("hold-1", "u-1", _due(60), 60, GPU)]
    assert wallet.taken["hold-1"] == _due(60) > 0

    _meter(store, 120, wallet=wallet)
    assert wallet.taken["hold-1"] == _due(120)
    assert wallet.balance == Decimal("50") - _due(120)
    assert store.rows["job-1"]["status"] == "running"
    first.modal.cancel.assert_not_called()


def test_tick_with_nothing_new_due_debits_nothing():
    store, wallet = _Jobs(_job_row()), _Wallet("50")
    _meter(store, 60, wallet=wallet)
    assert _meter(store, 60, wallet=wallet).summary["debited"] == 0


def test_meter_prices_the_run_on_its_own_params():
    inputs = {"num_samples": 7, "_partial_candidates": [], **_job_row()["inputs"]}
    store = _Jobs(_job_row(inputs=inputs))
    with patch("shared.wallet.live_due_usd", return_value=Decimal("0.01")) as due:
        _meter(store, 60, wallet=_Wallet("50"))
    due.assert_called_once_with("boltz2", 60, GPU, {"num_samples": 7})


def test_run_stops_when_the_balance_runs_short():
    store, wallet = _Jobs(_job_row()), _Wallet("0.05")
    t = 300
    assert _due(t) > Decimal("0.05")
    r = _meter(store, t, wallet=wallet, candidates=DESIGNS)
    row = store.rows["job-1"]
    assert r.summary == {"debited": 1, "stopped": 1, "cancel_failed": 0, "errors": []}
    assert wallet.balance == 0
    r.modal.cancel.assert_called_once_with("fc-1")
    assert row["status"] == "succeeded"
    assert row["failure_class"] == "succeeded"
    assert row["gpu_seconds_used"] == t
    assert row["result"]["partial"] is True
    assert row["result"]["stop_reason"] == jobs_mod.WALLET_STOP_REASON
    assert [c["name"] for c in row["result"]["candidates"]] == ["d1", "d2"]
    r.settle.assert_called_once()
    assert r.settle.call_args.args[:3] == ("hold-1", t, GPU)
    assert not r.settle.call_args.kwargs.get("refund")
    r.settle_hold.assert_not_called()
    r.release.assert_not_called()
    r.email.assert_called_once()


def test_debit_that_exactly_empties_the_wallet_keeps_the_run_going():
    store, wallet = _Jobs(_job_row()), _Wallet(str(_due(60)))
    r = _meter(store, 60, wallet=wallet)
    assert wallet.balance == 0
    assert r.summary["stopped"] == 0
    assert store.rows["job-1"]["status"] == "running"
    assert _meter(store, 120, wallet=wallet).summary["stopped"] == 1


def test_stop_before_any_design_finished_is_billed_as_no_yield():
    store = _Jobs(_job_row())
    r = _meter(store, 60)
    assert store.rows["job-1"]["failure_class"] == "completed_no_yield"
    r.settle.assert_called_once()
    assert not r.settle.call_args.kwargs.get("refund")


def test_reconstruct_error_still_stops_the_run():
    store = _Jobs(_job_row())
    r = _meter(store, 60, reconstruct_exc=RuntimeError("storage"))
    assert r.summary["stopped"] == 1
    assert store.rows["job-1"]["failure_class"] == "completed_no_yield"


@pytest.mark.parametrize("over", [
    {"status": "pending", "started_at": None},
    {"started_at": (NOW - timedelta(hours=1)).isoformat()},
])
def test_run_before_its_first_heartbeat_is_not_metered(over):
    inputs = {"_wallet": _job_row()["inputs"]["_wallet"]}
    store = _Jobs(_job_row(inputs=inputs, **over))
    r = _meter(store, 60, candidates=DESIGNS)
    assert r.summary == {"debited": 0, "stopped": 0, "cancel_failed": 0, "errors": []}
    assert r.wallet.calls == []
    assert store.rows["job-1"]["status"] == over.get("status", "running")
    r.modal.cancel.assert_not_called()


def _heartbeat(store, at):
    from flask import Flask

    from webhooks import modal as modal_webhook

    app = Flask(__name__)
    modal_webhook.register_modal_webhooks(app)

    def progress(**kw):
        store.rows[kw["job_id"]]["inputs"]["_progress"] = {"stage": kw["stage"]}

    with patch.object(jobs_mod, "get_service_client", store.client), \
         patch.object(jobs_mod, "_now_iso", return_value=at.isoformat()), \
         patch.object(modal_webhook, "_append_heartbeat_state", side_effect=progress), \
         patch.object(modal_webhook, "_run_mid_run_monitor"):
        resp = app.test_client().post(
            "/webhooks/heartbeat", json={"job_id": "job-1", "stage": "folding"},
        )
    assert resp.status_code == 200


@pytest.mark.parametrize("page_open", [False, True])
def test_debits_start_at_the_first_heartbeat_whether_or_not_the_page_is_open(page_open):
    inputs = {"_wallet": _job_row()["inputs"]["_wallet"]}
    store = _Jobs(_job_row(status="pending", started_at=None, inputs=inputs,
                           created_at=(NOW - timedelta(seconds=600)).isoformat()))
    if page_open:
        with patch.object(jobs_mod, "get_service_client", store.client), \
             patch.object(jobs_mod, "_now_iso",
                          return_value=(NOW - timedelta(seconds=590)).isoformat()):
            assert jobs_mod.mark_running("job-1")
    wallet = _Wallet("50")
    _meter(store, -300, wallet=wallet)
    assert wallet.calls == []
    _heartbeat(store, NOW)
    _heartbeat(store, NOW + timedelta(seconds=30))
    _meter(store, 60, wallet=wallet)
    assert [(c[2], c[3]) for c in wallet.calls] == [(_due(60), 60)]
    assert store.rows["job-1"]["status"] == "running"


def _raise(_fc):
    raise RuntimeError("modal down")


@pytest.mark.parametrize("cancel", [lambda _fc: {"ok": False, "error": "x"}, _raise])
def test_failed_modal_cancel_leaves_the_run_going(cancel):
    store = _Jobs(_job_row())
    r = _meter(store, 60, cancel=cancel, candidates=DESIGNS)
    assert r.summary == {"debited": 0, "stopped": 0, "cancel_failed": 1, "errors": []}
    assert store.rows["job-1"]["status"] == "running"
    r.settle.assert_not_called()


def test_stop_rebuilds_from_the_row_as_it_is_after_the_cancel():
    store = _Jobs(_job_row())
    late = {"name": "d3", "pdb_key": "designs/d3.pdb"}

    def cancel(_fc):
        row = store.rows["job-1"]
        row["inputs"] = {**row["inputs"], "_partial_candidates": [late]}
        return {"ok": True, "error": None}

    r = _meter(store, 60, cancel=cancel, candidates=DESIGNS)
    assert r.rec.call_args.args[0].inputs["_partial_candidates"] == [late]


def test_run_with_no_modal_call_stops_without_a_cancel():
    store = _Jobs(_job_row(modal_function_call_id=None))
    r = _meter(store, 60, candidates=DESIGNS)
    assert r.summary["stopped"] == 1
    r.modal.cancel.assert_not_called()


def test_meter_pays_the_oldest_run_first():
    newer = _job_row("job-new", created_at="2026-10-02T00:00:00+00:00",
                     modal_function_call_id="fc-new",
                     wallet={"hold_tx_id": "hold-new", "live": True})
    older = _job_row("job-old", created_at="2026-10-01T00:00:00+00:00",
                     modal_function_call_id="fc-old",
                     wallet={"hold_tx_id": "hold-old", "live": True})
    store, wallet = _Jobs(newer, older), _Wallet(str(_due(60)))
    r = _meter(store, 60, wallet=wallet, candidates=DESIGNS)
    assert [c[0] for c in wallet.calls] == ["hold-old", "hold-new"]
    assert store.rows["job-old"]["status"] == "running"
    assert store.rows["job-new"]["status"] == "succeeded"
    r.modal.cancel.assert_called_once_with("fc-new")


@pytest.mark.parametrize("debit,errors", [
    ({"settled": True, "debited": Decimal("0"), "taken": Decimal("0.30"),
      "short": Decimal("1"), "balance_after": Decimal("0")}, []),
    (None, ["job-1:debit failed"]),
])
def test_settled_or_failed_debit_leaves_the_run_alone(debit, errors):
    store = _Jobs(_job_row())
    r = _meter(store, 60, debit=MagicMock(return_value=debit), candidates=DESIGNS)
    assert r.summary == {"debited": 0, "stopped": 0, "cancel_failed": 0, "errors": errors}
    assert store.rows["job-1"]["status"] == "running"
    r.modal.cancel.assert_not_called()


@pytest.mark.parametrize("over", [
    {"campaign_id": "camp-1"},
    {"wallet": {"hold_tx_id": "hold-1", "live": "true"}},
    {"wallet": {"hold_tx_id": "hold-1", "live": False}},
    {"wallet": {"hold_tx_id": "hold-1", "estimate_usd": "2.00"}},
    {"wallet": {"live": True}},
    {"status": "pending", "started_at": None, "modal_function_call_id": None},
])
def test_rows_the_meter_never_touches(over):
    store = _Jobs(_job_row(**over))
    status = store.rows["job-1"]["status"]
    r = _meter(store, 10 * 86400, candidates=DESIGNS)
    assert r.summary == {"debited": 0, "stopped": 0, "cancel_failed": 0, "errors": []}
    assert r.wallet.calls == []
    assert store.rows["job-1"]["status"] == status
    r.modal.cancel.assert_not_called()


def test_webhook_that_lands_during_the_cancel_wins():
    store = _Jobs(_job_row())

    def webhook_first(_fc):
        store.rows["job-1"].update(status="succeeded", result={"candidates": DESIGNS})
        return {"ok": True, "error": None}

    r = _meter(store, 60, cancel=webhook_first, candidates=DESIGNS[:1])
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
         patch("shared.wallet.settle_live_run") as settle:
        job, err = jobs_mod.cancel_job("job-1", user_id="u-1", modal_client=modal)
    assert (job, err) == (None, "modal_cancel_failed")
    assert store.rows["job-1"]["status"] == "running"
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
# A finished live run settles through settle_live_run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_class,seconds,refund", [
    ("succeeded", 120, False),
    ("completed_no_yield", 120, False),
    ("user_cancelled", 120, False),
    ("user_cancelled", 0, True),
    ("infra_crash", 120, True),
    ("no_progress_timeout", 120, True),
])
def test_live_run_settles_through_settle_live_run(failure_class, seconds, refund):
    from shared.jobs import ToolJob, _settle_wallet_hold_for_completed_job

    status = {"succeeded": "succeeded", "completed_no_yield": "succeeded",
              "user_cancelled": "cancelled"}.get(failure_class, "failed")
    job = ToolJob.from_row(_job_row(status=status, failure_class=failure_class,
                                    gpu_seconds_used=seconds))
    with patch("shared.wallet.settle_live_run") as settle, \
         patch("shared.wallet.settle_hold") as settle_hold, \
         patch("shared.wallet.release_hold") as release:
        _settle_wallet_hold_for_completed_job(job)
    settle.assert_called_once()
    assert settle.call_args.args[:3] == ("hold-1", seconds, GPU)
    assert bool(settle.call_args.kwargs.get("refund")) is refund
    settle_hold.assert_not_called()
    release.assert_not_called()


def test_held_run_still_settles_through_settle_hold():
    from shared.jobs import ToolJob, _settle_wallet_hold_for_completed_job

    job = ToolJob.from_row(_job_row(
        status="succeeded", failure_class="succeeded", gpu_seconds_used=120,
        wallet={"hold_tx_id": "hold-1", "estimate_usd": "2.00", "tool_slug": "boltz2"},
    ))
    with patch("shared.wallet.settle_live_run") as settle, \
         patch("shared.wallet.settle_hold") as settle_hold:
        _settle_wallet_hold_for_completed_job(job)
    settle.assert_not_called()
    settle_hold.assert_called_once()


# ---------------------------------------------------------------------------
# shared.wallet's live calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("data,expected", [
    ("a-1", "a-1"), (["a-1"], "a-1"), ([], None), (None, None),
])
def test_open_live_run_returns_the_anchor_id(data, expected):
    from shared import wallet as wallet_mod

    with patch.object(wallet_mod, "_rpc_data", return_value=data) as rpc:
        assert wallet_mod.open_live_run("u-1", "boltz2") == expected
    rpc.assert_called_once_with("open_live_run", {"p_user_id": "u-1", "p_tool_slug": "boltz2"})


@pytest.mark.parametrize("debited,hooked", [("0.25", True), ("0", False)])
def test_debit_runs_the_settle_hooks_only_when_it_took_money(debited, hooked):
    from shared import wallet as wallet_mod

    data = {"settled": False, "debited": debited, "taken": "0.25", "short": "0",
            "balance_after": "4.75"}
    with patch.object(wallet_mod, "_rpc_data", return_value=data) as rpc, \
         patch.object(wallet_mod, "_post_settle_hooks") as hooks:
        out = wallet_mod.debit_live_run("hold-1", "u-1", Decimal("0.25"), 60, GPU)
    assert rpc.call_args.args[1] == {"p_hold_tx_id": "hold-1", "p_due_usd": "0.25",
                                     "p_gpu_seconds": 60.0, "p_gpu_class": GPU}
    assert out == {"settled": False, "debited": Decimal(debited), "taken": Decimal("0.25"),
                   "short": Decimal("0"), "balance_after": Decimal("4.75")}
    if hooked:
        hooks.assert_called_once_with("u-1", {"balance_usd": Decimal("4.75")}, Decimal("0.25"))
    else:
        hooks.assert_not_called()


def test_debit_that_failed_returns_none():
    from shared import wallet as wallet_mod

    with patch.object(wallet_mod, "_rpc_data", return_value=None):
        assert wallet_mod.debit_live_run("hold-1", "u-1", Decimal("1"), 60, GPU) is None


@pytest.mark.parametrize("refund", [False, True])
def test_settle_live_run_closes_at_the_metered_cost_or_zero(refund):
    from shared import wallet as wallet_mod

    client = MagicMock()
    (client.table.return_value.select.return_value.eq.return_value
     .maybe_single.return_value.execute.return_value) = SimpleNamespace(
        data={"user_id": "u-1", "tool_slug": "boltz2"})
    data = {"settled_before": False, "final": "0", "taken": "0.40", "charged": "0.10",
            "released": "0", "absorbed": "0", "balance_after": "3"}
    with patch.object(wallet_mod, "get_service_client", return_value=client), \
         patch.object(wallet_mod, "_rpc_data", return_value=data) as rpc, \
         patch.object(wallet_mod, "_post_settle_hooks") as hooks:
        wallet_mod.settle_live_run("hold-1", 120, GPU, {}, "failed", refund=refund)
    args = rpc.call_args.args[1]
    assert args["p_final_due_usd"] == ("0" if refund else str(_due(120)))
    assert args["p_failure_reason"] == "failed"
    hooks.assert_called_once_with("u-1", {"balance_usd": Decimal("3")}, Decimal("0.10"))


def test_live_due_is_clamped_at_the_tool_cap():
    cap = compute_hard_cap("boltz2", {})
    assert _due(60) < cap
    assert _due(10 * 86400) == cap


# ---------------------------------------------------------------------------
# campaigns:tick runs the meter
# ---------------------------------------------------------------------------


def _tick(meter):
    from cron.tick_campaigns import tick_campaigns

    client = MagicMock()
    client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = []
    with patch("shared.credits.get_service_client", return_value=client), \
         patch("shared.jobs.meter_live_runs", meter), \
         patch("shared.compute_campaigns.sweep_paused_campaigns", return_value={}):
        return tick_campaigns()


def test_tick_runs_the_meter():
    out = {"debited": 2, "stopped": 1, "cancel_failed": 0, "errors": []}
    meter = MagicMock(return_value=out)
    summary = _tick(meter)
    meter.assert_called_once_with()
    assert summary["live_meter"] == out


def test_tick_counts_the_meter_errors():
    meter = MagicMock(return_value={"debited": 0, "stopped": 0, "cancel_failed": 0,
                                    "errors": ["job-1:boom"]})
    assert "live meter: job-1:boom" in _tick(meter)["errors"]


def test_tick_carries_on_when_the_meter_raises():
    summary = _tick(MagicMock(side_effect=RuntimeError("boom")))
    assert "live meter failed" in summary["errors"]
    assert "reconciled" in summary


# ---------------------------------------------------------------------------
# Wallet history: one line per run, its debits folded under it
# ---------------------------------------------------------------------------


def _tx(tx_id, kind, amount, *, parent=None, at="2026-10-05T12:00:00+00:00"):
    return {"id": tx_id, "user_id": "u-1", "kind": kind, "amount_usd": Decimal(amount),
            "balance_after_usd": Decimal("1"), "created_at": at, "tool_slug": "boltz2",
            "job_id": None, "parent_tx_id": parent, "notes": None, "stripe_event_id": None}


def _run_rows(*closing):
    return [
        _tx("h-1", "hold", "0"),
        _tx("d-2", "run_debit", "-0.10", parent="h-1", at="2026-10-05T12:10:00+00:00"),
        _tx("d-1", "run_debit", "-0.10", parent="h-1", at="2026-10-05T12:05:00+00:00"),
        *closing,
    ]


def _annotate(rows):
    from blueprints.wallet import _build_tx_lineage_annotations
    from tests.test_wallet_templates import _FakeLedgerClient

    return _build_tx_lineage_annotations(_FakeLedgerClient(rows), "u-1", rows)


def test_running_run_is_one_line_with_its_debits_oldest_first():
    ann = _annotate(_run_rows())
    run = ann["h-1"]
    assert (run["role"], run["settled"], run["taken"], run["net"]) == (
        "run", False, Decimal("0.20"), Decimal("-0.20"))
    assert [d["id"] for d in run["debits"]] == ["d-1", "d-2"]
    assert ann["d-1"] == ann["d-2"] == {"role": "debit"}


@pytest.mark.parametrize("closing,net", [
    (_tx("r-1", "hold_release", "0.05", parent="h-1"), Decimal("-0.15")),
    (_tx("c-1", "charge", "-0.03", parent="h-1"), Decimal("-0.23")),
    (_tx("c-1", "charge", "0", parent="h-1"), Decimal("-0.20")),
])
def test_closed_run_nets_its_debits_and_closing_row(closing, net):
    run = _annotate(_run_rows(closing))["h-1"]
    assert (run["role"], run["settled"], run["net"]) == ("run", True, net)


def test_unused_anchor_reads_as_a_run():
    ann = _annotate([_tx("h-1", "hold", "0"), _tx("r-1", "hold_release", "0", parent="h-1")])
    assert ann["h-1"]["role"] == "run"
    assert ann["h-1"]["debits"] == []


def _history(rows, annotations):
    from flask import render_template

    from app import create_app

    app = create_app()
    with app.test_request_context("/account/wallet/transactions"):
        return render_template("wallet/transactions.html", wallet={"balance_usd": 0},
                               transactions=rows, filter_kind=None, page=1,
                               page_size=50, has_next=False, has_prev=False,
                               total_count=len(rows), tx_annotations=annotations)


@pytest.mark.parametrize("closing,hint", [
    ((), "charged as it runs; $0.20 so far"),
    ((_tx("r-1", "hold_release", "0.05", parent="h-1"),), "charged as it ran; it closed at $0.15"),
    ((_tx("r-1", "hold_release", "0.20", parent="h-1"),),
     "charged as it ran; all of it was returned when it closed"),
])
def test_history_says_what_a_run_cost(monkeypatch, closing, hint):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    rows = _run_rows(*closing)
    page = [r for r in rows if r["kind"] != "run_debit"]
    html = _history(page, _annotate(rows))
    assert hint in html
    assert "2 charges while it ran" in html
    assert html.index("12:05") < html.index("12:10")


def test_history_explains_a_debit_row_when_charges_are_filtered(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    rows = _run_rows()
    assert "taken from your balance while the run went" in _history(rows[1:], _annotate(rows))


class _Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args))
            if name == "execute":
                return SimpleNamespace(data=[], count=0)
            return self
        return call


@pytest.mark.parametrize("kind,expected", [
    (None, ("neq", ("kind", "run_debit"))),
    ("charge", ("in_", ("kind", ["charge", "run_debit"]))),
    ("topup", ("eq", ("kind", "topup"))),
])
def test_history_filter_hides_debits_unless_charges_are_asked_for(monkeypatch, kind, expected):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    client, rec = app.test_client(), _Recorder()
    with client.session_transaction() as sess:
        sess["user_email"] = "u@example.com"
    with patch("blueprints.wallet.load_user_context",
               return_value=SimpleNamespace(user_id="u-1")), \
         patch("blueprints.wallet.get_or_create_wallet", return_value={"balance_usd": 0}), \
         patch("shared.credits.get_service_client", return_value=rec):
        resp = client.get("/account/wallet/transactions",
                          query_string={"kind": kind} if kind else {})
    assert resp.status_code == 200
    kind_calls = [c for c in rec.calls if c[0] in ("eq", "neq", "in_") and c[1][0] == "kind"]
    assert kind_calls == [expected]


# ---------------------------------------------------------------------------
# Jobs table and failed-run page: a live run's money reads as taken so far
# ---------------------------------------------------------------------------


def _spend(usd, settled, *, held="0", taken="0"):
    return {"hold-1": {"usd": Decimal(usd), "settled": settled,
                       "held": Decimal(held), "taken": Decimal(taken)}}


def _job(live, status="running", **over):
    wallet = {"hold_tx_id": "hold-1", **({"live": True} if live else {})}
    return SimpleNamespace(id="job-1", status=status, started_at=None, completed_at=None,
                           failure_class=None, inputs={"_wallet": wallet}, **over)


@pytest.mark.parametrize("live,text,note", [(True, "$0.20", "so far"), (False, "$0.21", "reserved")])
def test_jobs_table_names_a_live_run_s_spend_so_far(live, text, note):
    from blueprints.jobs import _jobs_table_cells

    with patch("shared.wallet.job_spend_by_hold", return_value=_spend("0.203", False)):
        cell = _jobs_table_cells([_job(live)], "u-1", NOW)["job-1"]
    assert (cell["spend"], cell["spend_note"]) == (text, note)


@pytest.mark.parametrize("live,spend,line", [
    (True, _spend("0.20", False, taken="0.20"),
     "$0.20 has been taken for this run so far; it has not been settled yet."),
    (True, _spend("0", True, taken="0.20"),
     "The $0.20 taken while this run went was returned to your wallet in full. "
     "You were not charged for this run."),
    (True, _spend("0.15", True, taken="0.20"),
     "You were charged $0.15 for the GPU time this run used. "
     "The rest of the $0.20 taken while this run went was returned to your wallet."),
    (False, _spend("0", True, held="2.00"),
     "The $2.00 hold was returned to your wallet in full. You were not charged for this run."),
])
def test_failed_live_run_says_what_was_taken(live, spend, line):
    from blueprints.jobs import _failure_money

    with patch("shared.wallet.job_spend_by_hold", return_value=spend):
        assert _failure_money("u-1", _job(live, status="failed")) == line


# ---------------------------------------------------------------------------
# What a stopped run tells the user
# ---------------------------------------------------------------------------

STOP_HEAD = "This run stopped when your wallet balance reached $0"


def _finished(n, *, stop_reason=jobs_mod.WALLET_STOP_REASON, status="succeeded"):
    from tests.test_run_notices import _job

    result = {
        "candidates": [{"rank": i + 1, "sequence": "ACDE", "scores": {}} for i in range(n)],
        "candidate_count": n,
        "partial": True,
    }
    if stop_reason:
        result["stop_reason"] = stop_reason
    return _job(result=result, status=status, tool="boltz2")


def test_stop_line_counts_the_finished_designs():
    from shared.run_notices import run_notices

    assert run_notices(_finished(2)) == [
        f"{STOP_HEAD}, after 2 designs. The finished designs are in your "
        "results and downloads. Add funds to run again."
    ]


def test_stop_line_with_no_design_says_none_finished():
    from shared.run_notices import partial_line

    assert partial_line(_finished(0)) == (
        f"{STOP_HEAD}, before any design finished. Add funds to run again."
    )


def test_other_partial_runs_keep_their_line():
    from shared.run_notices import partial_line, stopped_for_balance

    job = _finished(2, stop_reason=None)
    assert not stopped_for_balance(job)
    assert partial_line(job) == "This run stopped early after 2 designs."


def test_unfinished_run_with_a_stop_reason_gets_no_stop_copy():
    from shared.run_notices import partial_line, stopped_for_balance

    job = _finished(2, status="running")
    assert not stopped_for_balance(job)
    assert partial_line(job) == ""


@pytest.fixture
def job_client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _page_text(client, job):
    from tests.test_failed_run_refund_copy import _page

    with patch("shared.jobs.resolve_user_email_and_meta",
               return_value=("u@example.com", {})):
        return _page(client, job)


def test_job_page_says_a_stopped_run_stopped(job_client):
    text = _page_text(job_client, _finished(2))
    assert "Stopped: your wallet balance reached $0. Add funds" in text
    assert "Completed in" not in text
    assert f"{STOP_HEAD}, after 2 designs." in text


def test_job_page_keeps_the_completed_line_for_other_runs(job_client):
    text = _page_text(job_client, _finished(2, stop_reason=None))
    assert "Completed in 600 GPU-seconds." in text
    assert "wallet balance reached $0" not in text


def _mail(monkeypatch, job):
    from tests.test_job_complete_email_headline import _bodies, _sent

    monkeypatch.setattr("shared.wallet.job_spend_by_hold", lambda user_id, holds: {})
    payload = _sent(job)
    return payload, _bodies(payload)


def test_email_for_a_stopped_run_says_so_and_links_to_top_up(monkeypatch):
    payload, bodies = _mail(monkeypatch, _finished(2))
    assert payload["subject"].endswith("run stopped: your wallet balance reached $0")
    assert "run stopped: your wallet balance reached $0" in bodies["html"]
    assert f"{STOP_HEAD}, after 2 designs." in bodies["text"]
    assert "Add funds: " in bodies["text"]
    assert "/account/wallet/topup" in payload["text"]
    assert '/account/wallet/topup"' in payload["html"]
    assert "Add funds" in bodies["html"]


def test_email_for_a_stopped_run_with_no_design(monkeypatch):
    _payload, bodies = _mail(monkeypatch, _finished(0))
    assert "The run stopped before any design finished." in bodies["text"]
    assert "The run stopped before any design finished." in bodies["html"]


def test_email_for_other_runs_has_no_stop_copy(monkeypatch):
    payload, bodies = _mail(monkeypatch, _finished(2, stop_reason=None))
    assert payload["subject"].endswith("run is done")
    assert "wallet balance reached $0" not in bodies["text"] + bodies["html"]
    assert "/account/wallet/topup" not in payload["text"] + payload["html"]


def test_wallet_history_explains_an_absorbed_row(monkeypatch):
    from flask import render_template

    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    hint = "Ranomics covered the rest, and you were not charged for it."
    rows = [{"id": f"tx-{kind}", "kind": kind, "amount_usd": Decimal(amount),
             "balance_after_usd": Decimal("0"), "created_at": "2026-10-05T12:00:00Z",
             "tool_slug": "boltz2", "job_id": "job-1", "notes": None,
             "stripe_event_id": None}
            for kind, amount in (("absorbed_variance", "0"), ("charge", "-0.30"))]
    with app.test_request_context("/account/wallet/transactions"):
        both = render_template("wallet/transactions.html", wallet={"balance_usd": 0},
                               transactions=rows, filter_kind=None, page=1,
                               page_size=50, has_next=False, has_prev=False,
                               total_count=2, tx_annotations={})
        charge_only = render_template("wallet/transactions.html", wallet={"balance_usd": 0},
                                      transactions=rows[1:], filter_kind=None, page=1,
                                      page_size=50, has_next=False, has_prev=False,
                                      total_count=1, tx_annotations={})
    assert both.count(hint) == 1
    assert hint not in charge_only
