"""The "Run more candidates" offer on a finished job (shared/scale_up.py)."""

from __future__ import annotations

import math
import re
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from shared import compute_campaigns as cc
from shared.scale_up import ScaleUp, quote, topup_usd
from shared.wallet_estimates import estimated_cost_for_tool

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_JID = str(uuid.uuid4())


def _job(tool="bindcraft", preset="pilot", status="succeeded", inputs=None,
         candidates=({"rank": 1},)):
    return SimpleNamespace(
        id=_JID, tool=tool, preset=preset, status=status,
        created_at="2026-01-01T00:00:00+00:00",
        inputs=inputs if inputs is not None else {"num_designs": 4},
        result={"candidates": list(candidates)} if candidates is not None else None,
        error=None, gpu_seconds_used=None,
    )


def _quote(job, balance=1000):
    with patch("shared.wallet.get_or_create_wallet",
               return_value={"balance_usd": balance}), \
            patch("shared.feature_flags.tool_enabled", return_value=True):
        return quote("u-1", job)


# ---- quote() ---------------------------------------------------------------

def test_split_tool_is_priced_as_the_split_run():
    q = _quote(_job())
    assert q.route == "split" and q.count == 100 and not q.clamped
    assert q.price_usd == cc.plan_chunks("bindcraft", 100, "pilot").budget_usd
    assert q.price_display == cc.display_cost_usd(q.price_usd)


def test_boltzgen_clamps_to_its_per_job_cap_and_prices_one_job():
    inputs = {"budget": 4}
    q = _quote(_job(tool="boltzgen", inputs=inputs))
    assert q.route == "single" and q.count == 50 and q.clamped
    assert q.price_usd == estimated_cost_for_tool(
        "u-1", "boltzgen", {"budget": 50, "preset": "pilot"})


@pytest.mark.parametrize("job", [
    _job(status="failed"),
    _job(candidates=()),
    _job(candidates=None),
    _job(tool="opendde"),
    _job(tool="proteina", preset="validate"),
    _job(tool="iggm", preset="affinity_maturation", inputs={"num_samples": 4}),
    _job(inputs={"num_designs": 100}),
])
def test_no_offer(job):
    assert _quote(job) is None


@pytest.mark.parametrize("tool,inputs,result", [
    ("iggm", {"num_samples": 8}, {"designs": [{"rank": 1}]}),
    ("esmfold2-design", {"n_seeds": 4}, {"designs": [{"rank": 1}]}),
    ("bindcraft", {"num_designs": 4}, {"status": "ok", "output": {"candidates": [{"rank": 1}]}}),
])
def test_offer_reads_every_result_shape(tool, inputs, result):
    job = _job(tool=tool, preset="cdr_design" if tool == "iggm" else "pilot", inputs=inputs)
    job.result = result
    assert _quote(job) is not None


# ---- next step for the tools with no candidate count to raise --------------

@pytest.mark.parametrize("tool", ["af2", "colabfold", "esmfold"])
def test_fold_with_a_structure_offers_mpnn_on_it(tool):
    from shared.resample import RESAMPLE_MPNN_DEFAULTS
    job = _job(tool=tool, preset="standalone", inputs={})
    job.result = {"pdb_b64": "QVRPTQ=="}
    q = _quote(job)
    assert q.tool == "mpnn" and q.route == "resample" and q.count == 16
    assert q.price_usd == estimated_cost_for_tool(
        "u-1", "mpnn", dict(RESAMPLE_MPNN_DEFAULTS))
    assert "ProteinMPNN" in q.offer_text and q.cta_text == "Design 16 sequences"


# The shape the adapters actually store: the count is one level down, under
# ``parameters`` (tools/af2/__init__.py:351, tools/colabfold/__init__.py:337,
# tools/esmfold/__init__.py:231, tools/boltz2/__init__.py:418). A flat
# ``{"n_designs_total": 7}`` is a shape production never writes, and a quote
# built from it prices one design no matter how large the batch was.
def _batch_inputs(n, **extra):
    return {"parameters": {"n_designs_total": n}, **extra}


@pytest.mark.parametrize("tool", ["af2", "colabfold", "esmfold"])
def test_batch_fold_without_a_structure_offers_another_batch(tool):
    inputs = _batch_inputs(7, batch_records=[{"name": f"d{i}"} for i in range(7)])
    job = _job(tool=tool, preset="batch", inputs=inputs)
    job.result = {"candidates": [{"rank": 1}]}
    q = _quote(job)
    assert q.tool == tool and q.route == "clone" and q.count == 7
    assert q.price_usd == estimated_cost_for_tool(
        "u-1", tool, {**inputs, "n_designs_total": 7, "preset": "batch"})
    assert "Fold another 7" in q.offer_text


@pytest.mark.parametrize("tool", ["af2", "boltz2"])
def test_clone_offer_prices_the_stored_batch_size(tool):
    """The card must quote the run the button opens, not one design.

    ``?clone_from=`` copies the whole stored input into the form
    (blueprints/tools.py:1227), so a 200-record batch re-submits 200 records.
    Reading the count flat returned 0, collapsing the quote to a single
    design -- a ~200x understatement on the price the user is shown.
    """
    big = _batch_inputs(200)
    small = _batch_inputs(1)
    job_big = _job(tool=tool, preset="standalone", inputs=big)
    job_big.result = {"designs": [{"rank": 1}]}
    job_small = _job(tool=tool, preset="standalone", inputs=small)
    job_small.result = {"designs": [{"rank": 1}]}
    q_big, q_small = _quote(job_big), _quote(job_small)
    assert q_big.count == 200 and q_small.count == 1
    assert q_big.price_usd > q_small.price_usd


def test_boltz2_offers_another_screen_priced_at_the_pasted_list():
    # The clone carries ``binder_sequences`` through to the textarea
    # (templates/tools/boltz2_form.html:164), so the count comes from the list.
    inputs = {"binder_sequences": ["AAA", "CCC", "DDD"]}
    job = _job(tool="boltz2", preset="standalone", inputs=inputs)
    job.result = {"designs": [{"rank": 1}]}
    q = _quote(job)
    assert q.tool == "boltz2" and q.route == "clone" and q.count == 3
    assert q.price_usd == estimated_cost_for_tool(
        "u-1", "boltz2",
        {**inputs, "n_designs_total": 3, "preset": "standalone"})
    assert q.cta_text == "Screen 3 more"


@pytest.mark.parametrize("job", [
    _job(tool="af2", status="failed"),
    _job(tool="boltz2", status="failed"),
])
def test_no_next_step_on_a_failed_job(job):
    assert _quote(job) is None


def test_next_step_tops_up_against_the_hold():
    from shared.wallet_estimates import cushioned_hold_usd
    inputs = _batch_inputs(10)
    job = _job(tool="boltz2", preset="msa_server", inputs=inputs)
    job.result = {"designs": [{"rank": 1}]}
    hold = cushioned_hold_usd(
        "u-1", "boltz2", {**inputs, "n_designs_total": 10, "preset": "msa_server"})
    assert _quote(job, balance=0).topup_usd == math.ceil(hold)
    assert _quote(job, balance=hold).topup_usd == 0


def test_single_run_topup_covers_the_hold_the_submit_reserves():
    from shared.wallet_estimates import cushioned_hold_usd
    hold = cushioned_hold_usd("u-1", "boltzgen", {"budget": 50, "preset": "pilot"})
    q = _quote(_job(tool="boltzgen", inputs={"budget": 4}), balance=0)
    assert hold > q.price_usd
    assert q.topup_usd == math.ceil(hold)
    assert _quote(_job(tool="boltzgen", inputs={"budget": 4}),
                  balance=hold).topup_usd == 0


def test_topup_is_the_shortfall_rounded_up_to_a_dollar():
    assert topup_usd(Decimal("10.01"), Decimal("10")) == 1
    assert topup_usd(Decimal("5"), Decimal("10")) == 0


def test_topup_covers_the_first_batch_when_it_exceeds_the_estimate():
    plan = cc.plan_chunks("bindcraft", 100, "pilot")
    first = cc.first_wave_hold_usd(plan, cc.launch_concurrency_for("bindcraft"))
    q = _quote(_job(), balance=0)
    assert q.topup_usd == math.ceil(max(plan.budget_usd, first))


# ---- the card on /jobs/<id> ------------------------------------------------

@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    return c


def _ctx():
    return SimpleNamespace(user_id="u-1", tier="free", balance=100,
                           email="u@example.com")


def _offer(**kw):
    base = dict(tool="bindcraft", count=100, clamped=False, route="split",
                price_usd=Decimal("405.16"), price_display="405.16", topup_usd=0)
    base.update(kw)
    return ScaleUp(**base)


def _page(client, offer):
    # A running job keeps the results partial out of the page; the card is
    # gated on the quote alone.
    with patch("blueprints.jobs.load_user_context", return_value=_ctx()), \
            patch("blueprints.jobs.get_job", return_value=_job(status="running")), \
            patch("blueprints.jobs.scale_up_quote", return_value=offer):
        resp = client.get(f"/jobs/{_JID}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_card_renders_price_and_button(client):
    html = _page(client, _offer())
    assert "Run 100 candidates on the same target" in html
    assert "est. $405.16" in html
    assert f'action="/jobs/{_JID}/scale-up"' in html
    assert "Top up $" not in html
    assert "/GPU" not in html and "per GPU" not in html


def test_card_renders_the_next_step_wording(client):
    html = _page(client, _offer(
        tool="mpnn", count=16, route="resample",
        offer_text="Design 16 sequences on the structure you just predicted, "
                   "with ProteinMPNN",
        cta_text="Design 16 sequences"))
    assert "with ProteinMPNN" in html
    assert ">Design 16 sequences</button>" in html
    assert "Run 16 candidates" not in html


def test_card_says_top_up_when_short(client):
    html = _page(client, _offer(topup_usd=7))
    assert "Top up $7 to run this" in html


def test_card_names_the_clamp(client):
    html = _page(client, _offer(tool="boltzgen", count=50, clamped=True,
                                route="single"))
    assert "runs up to 50 per job" in html


def test_no_card_without_an_offer(client):
    assert "scale-up-card" not in _page(client, None)


# ---- POST /jobs/<id>/scale-up ---------------------------------------------

def _post(client, job, offer, **patches):
    with patch("blueprints.jobs.load_user_context", return_value=_ctx()), \
            patch("blueprints.jobs.get_job", return_value=job), \
            patch("blueprints.jobs.scale_up_quote", return_value=offer), \
            patch("shared.events.emit") as emit, \
            patch("shared.events.log_event") as log_event:
        extra = [patch(k, **v) for k, v in patches.items()]
        mocks = [p.start() for p in extra]
        try:
            resp = client.post(f"/jobs/{_JID}/scale-up")
        finally:
            for p in extra:
                p.stop()
    return resp, emit, log_event, mocks


def test_non_owner_gets_404(client):
    resp, emit, log_event, _ = _post(client, None, _offer())
    assert resp.status_code == 404
    emit.assert_not_called()
    log_event.assert_not_called()


def test_single_route_clones_the_form_at_the_new_count(client):
    offer = _offer(tool="boltzgen", count=50, route="single")
    resp, emit, log_event, _ = _post(client, _job(tool="boltzgen"), offer)
    assert resp.status_code == 302
    loc = urlparse(resp.headers["Location"])
    assert loc.path == "/tools/boltzgen"
    assert parse_qs(loc.query) == {"clone_from": [_JID], "scale_to": ["50"]}
    assert emit.call_args.args[0] == "scale_up_click"
    assert emit.call_args.kwargs["properties"]["route"] == "single"
    assert log_event.call_args.kwargs["event_type"] == "scale_up_click"
    assert log_event.call_args.kwargs["props"]["count"] == 50


def test_resample_route_opens_mpnn_on_this_job(client):
    offer = _offer(tool="mpnn", count=16, route="resample",
                   cta_text="Design 16 sequences")
    resp, emit, _, _ = _post(client, _job(tool="af2"), offer)
    assert resp.status_code == 302
    loc = urlparse(resp.headers["Location"])
    assert loc.path == "/tools/mpnn"
    assert parse_qs(loc.query) == {"resample_from": [_JID]}
    assert emit.call_args.args[0] == "scale_up_click"
    assert emit.call_args.kwargs["properties"]["route"] == "resample"


def test_clone_route_opens_the_same_form_unscaled(client):
    offer = _offer(tool="boltz2", count=10, route="clone")
    resp, emit, _, _ = _post(client, _job(tool="boltz2"), offer)
    loc = urlparse(resp.headers["Location"])
    assert loc.path == "/tools/boltz2"
    assert parse_qs(loc.query) == {"clone_from": [_JID]}
    assert emit.call_args.kwargs["properties"]["route"] == "clone"


def test_split_route_makes_a_target_then_opens_the_run_form(client):
    job = _job(inputs={"num_designs": 4, "_pdb_storage_path": "u-1/x.pdb",
                       "_pdb_filename": "x.cif", "target_chain": "A"})
    resp, _, _, (dl, resolve, find, create) = _post(
        client, job, _offer(),
        **{
            "shared.storage.download_input": {"return_value": b"ATOM"},
            "shared.pdb_intake.resolve_target_upload": {
                "return_value": (SimpleNamespace(sha256="abc"), None)},
            "shared.targets.find_target_by_sha256": {"return_value": None},
            "shared.targets.create_target": {
                "return_value": SimpleNamespace(id="t-new")},
        },
    )
    assert resp.status_code == 302
    loc = urlparse(resp.headers["Location"])
    assert loc.path == "/campaigns/new"
    assert parse_qs(loc.query) == {"target_id": ["t-new"], "source_job": [_JID]}
    assert resolve.call_args.args[0].filename == "x.pdb"
    create.assert_called_once()
    assert create.call_args.kwargs["source"] == "scale_up"


def test_split_route_reuses_a_target_with_the_same_structure(client):
    job = _job(inputs={"_pdb_storage_path": "u-1/x.pdb", "_pdb_filename": "x.pdb"})
    resp, _, _, (_, _, _, create) = _post(
        client, job, _offer(),
        **{
            "shared.storage.download_input": {"return_value": b"ATOM"},
            "shared.pdb_intake.resolve_target_upload": {
                "return_value": (SimpleNamespace(sha256="abc"), None)},
            "shared.targets.find_target_by_sha256": {
                "return_value": SimpleNamespace(id="t-old")},
            "shared.targets.create_target": {},
        },
    )
    assert parse_qs(urlparse(resp.headers["Location"]).query)["target_id"] == ["t-old"]
    create.assert_not_called()


def test_split_route_without_a_staged_structure_opens_the_empty_form(client):
    job = _job(tool="proteina", preset="protein_binder",
               inputs={"task_name": "02_PDL1"})
    resp, _, _, _ = _post(client, job, _offer(tool="proteina"))
    assert parse_qs(urlparse(resp.headers["Location"]).query) == {"source_job": [_JID]}


# ---- the forms it lands on -------------------------------------------------

def test_tool_form_prefills_the_scaled_count(client, monkeypatch):
    from shared.feature_flags import flag_name
    monkeypatch.setenv(flag_name("boltzgen"), "on")
    prior = _job(tool="boltzgen", inputs={"budget": 4, "target_chain": "A"})
    with patch("blueprints.tools.load_user_context", return_value=_ctx()), \
            patch("blueprints.tools.get_job", return_value=prior), \
            patch("blueprints.tools.get_or_create_wallet",
                  return_value={"balance_usd": 50, "wallet_frozen": False}):
        resp = client.get(f"/tools/boltzgen?clone_from={_JID}&scale_to=500")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    tag = re.search(r'<input\b[^>]*\bname="budget"[^>]*>', html).group(0)
    assert 'value="50"' in tag


def test_run_form_prefills_tool_settings_and_100(client):
    src = _job(tool="bindcraft", inputs={
        "num_designs": 4, "target_chain": "B", "hotspot_residues": [12, 15],
        "binder_length_min": 60, "binder_length_max": 90,
    })
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.jobs.get_job", return_value=src), \
            patch.object(cc, "visible_campaign_tools", return_value=("rfdiffusion", "bindcraft")):
        resp = client.get(f"/campaigns/new?source_job={_JID}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert '<option value="bindcraft" selected>' in html
    assert 'value="100"' in html
    assert 'id="target_chain" name="target_chain" maxlength="4" value="B"' in html
    assert 'value="12,15"' in html


def test_run_form_estimate_says_how_much_to_top_up(client):
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), \
            patch("shared.wallet.get_or_create_wallet",
                  return_value={"balance_usd": "0", "wallet_frozen": False}), \
            patch("shared.compute_campaigns.get_service_client", return_value=None):
        data = client.get(
            "/api/campaigns/estimate?tool=bindcraft&requested_designs=100").get_json()
    need = max(Decimal(data["budget_usd"]), Decimal(data["first_wave_usd"]))
    assert data["topup_usd_display"] == str(math.ceil(need))


def _finished_page(client, offer, status="succeeded"):
    with patch("blueprints.jobs.load_user_context", return_value=_ctx()), \
            patch("blueprints.jobs.get_job", return_value=_job(status=status)), \
            patch("blueprints.jobs.scale_up_quote", return_value=offer):
        resp = client.get(f"/jobs/{_JID}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _contact_tags(html):
    return re.findall(r"<a\b[^>]*data-contact-link[^>]*>.*?</a>", html, re.S)


def test_scale_up_is_the_primary_next_step_and_contact_is_a_text_link(client):
    html = _finished_page(client, _offer())
    # Download CSV (candidate_table.html) and the shortlist modal's submit
    # keep their own btn-primary; the offer is the only primary next step.
    buttons = re.findall(r'<(?:a|button)\b[^>]*class="btn-primary"[^>]*>(.*?)</(?:a|button)>',
                         html, re.S)
    assert "Run 100 candidates" in buttons
    assert not any("team" in b.lower() or "scale" in b.lower() for b in buttons)
    [contact] = _contact_tags(html)
    assert "btn-" not in contact
    assert "Talk to the team" in contact
    assert "Run this at scale" not in html
    assert "at scale" not in html


def test_without_an_offer_the_contact_button_promises_no_run(client):
    html = _finished_page(client, None)
    [contact] = _contact_tags(html)
    assert 'class="btn-secondary"' in contact
    assert ">Talk to the team</a>" in contact
    assert "Run this at scale" not in html
    assert "at scale" not in html


def test_failed_job_retry_outranks_ask_the_team(client):
    html = _finished_page(client, None, status="failed")
    [contact] = _contact_tags(html)
    assert "btn-primary" not in contact and "Ask the team" in contact
    assert html.index("data-retry-link") < html.index(contact)
