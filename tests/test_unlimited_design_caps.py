"""Workstream D (PR-1): unlimited design caps.

Covers:
* D1 — ``single_container_ceiling`` per tool, and the ``tool_submit`` backstop
  that turns an over-ceiling single-job submit into a campaign pointer instead
  of a doomed single container (without touching the money path).
* D3 — the MPNN sequence cap is reconciled to a single value across the
  adapter, the pipeline clamp, and the form (the pre-2026-07 bug: form promised
  200, ``run_pipeline`` silently clamped prod to 20).

Pure where possible; the backstop test uses the real ``tool_submit`` route via
the zero-estimate path so no wallet hold machinery is involved.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import shared.compute_campaigns as cc

_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# D1 — single-container ceiling (the campaign-reroute threshold)
# ---------------------------------------------------------------------------

def test_single_container_ceiling_matches_chunk_size():
    # The ceiling IS the campaign chunk size: above it, a single job needs
    # more than one container and must fan out.
    assert cc.single_container_ceiling("rfdiffusion") == 12
    assert cc.single_container_ceiling("bindcraft") == 16
    assert cc.single_container_ceiling("boltzgen") == cc.BOLTZGEN_DESIGNS_PER_JOB
    for tool in ("rfdiffusion", "bindcraft", "boltzgen"):
        assert cc.single_container_ceiling(tool) == cc._chunk_size_for(tool)


# ---------------------------------------------------------------------------
# D3 — MPNN cap reconciled to a single source of truth AND raised
# ---------------------------------------------------------------------------

def _int_const(path: str, name: str) -> int:
    text = (_ROOT / path).read_text(encoding="utf-8")
    m = re.search(rf"^{name}\s*=\s*(\d+)", text, re.MULTILINE)
    assert m, f"{name} not found in {path}"
    return int(m.group(1))


def test_mpnn_cap_reconciled_and_raised():
    from tools.mpnn import NUM_SEQ_MAX as ADAPTER_MAX

    # The adapter cap is the raised ceiling.
    assert ADAPTER_MAX == 1000
    # The pipeline clamp (runs on every prod job) must equal the adapter cap,
    # or it silently truncates the user's requested count.
    pipeline_max = _int_const("tools/mpnn/run_pipeline.py", "NUM_SEQ_MAX")
    assert pipeline_max == ADAPTER_MAX, (
        "run_pipeline.NUM_SEQ_MAX must match the adapter cap or prod silently "
        "caps below the promised count"
    )


def test_mpnn_form_max_matches_cap():
    from tools.mpnn import NUM_SEQ_MAX as ADAPTER_MAX

    form = (_ROOT / "templates/tools/mpnn_form.html").read_text(encoding="utf-8")
    assert f'max="{ADAPTER_MAX}"' in form
    # The user-facing hint must not still advertise the old 200.
    assert "1 to 200" not in form


# ---------------------------------------------------------------------------
# D1 — the tool_submit backstop reroutes an over-ceiling single job
# ---------------------------------------------------------------------------

@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _ctx(user_id="u-1"):
    return SimpleNamespace(
        user_id=user_id, tier="free", balance=100, email="u@example.com"
    )


def _over_ceiling_form():
    # rfdiffusion ceiling is 12; 100 must reroute. Valid otherwise so validate
    # passes and the backstop (not a validation error) is what returns.
    return {
        "preset": "pilot",
        "target_chain": "A",
        "hotspot_residues": "54,56",
        "binder_length_min": "55",
        "binder_length_max": "65",
        "num_designs": "100",
    }


def test_over_ceiling_single_job_reroutes_to_campaign(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"

    create_job = MagicMock()
    with patch("blueprints.tools.load_user_context", return_value=_ctx()), patch(
        "shared.idempotency.load_user_context", return_value=None
    ), patch("blueprints.tools.tool_enabled", return_value=True), patch(
        # Zero estimate: requires_wallet skips the gate, so no hold is placed
        # and no wallet machinery is exercised — the request lands in
        # tool_submit and the backstop fires.
        "app.estimated_cost_for_tool",
        return_value=Decimal("0"),
    ), patch("blueprints.tools.create_job", create_job):
        resp = client.post(
            "/tools/rfdiffusion/submit", data=_over_ceiling_form()
        )

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # The backstop returns the campaign pointer, not a launched job.
    assert "campaign" in body.lower()
    assert "/campaigns/new" in body
    # No job row is written for the doomed single container.
    create_job.assert_not_called()


def test_form_render_wires_client_reroute(app):
    # The logged-in tool form must carry the ceiling data-attr, the notice
    # element, and the reroute script so the client can fan out over-ceiling
    # submits. Locks the wiring the browser JS depends on.
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"

    with patch("blueprints.tools.load_user_context", return_value=_ctx()), patch(
        "blueprints.tools.tool_enabled", return_value=True
    ), patch(
        "blueprints.tools.get_or_create_wallet",
        return_value={"balance_usd": "100", "wallet_frozen": False},
    ):
        resp = client.get("/tools/rfdiffusion")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'data-campaign-ceiling="12"' in body
    assert 'id="campaign-reroute-notice"' in body
    # Dedicated campaign CTA: type="button" so the single-job wallet gate never
    # disables it (over-ceiling requests are bounded by the campaign preauth).
    assert 'id="campaign-submit-btn"' in body
    # The reroute script re-points the form to the campaign create endpoint.
    assert "requested_designs" in body and "/campaigns" in body


def test_at_ceiling_single_job_not_rerouted(app):
    # 12 == ceiling: NOT over, so the backstop must not fire. It should fall
    # through to the normal single-job path (which then needs a PDB upload).
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"

    form = _over_ceiling_form()
    form["num_designs"] = "12"
    with patch("blueprints.tools.load_user_context", return_value=_ctx()), patch(
        "shared.idempotency.load_user_context", return_value=None
    ), patch("blueprints.tools.tool_enabled", return_value=True), patch(
        "app.estimated_cost_for_tool", return_value=Decimal("0")
    ), patch("blueprints.tools.create_job", MagicMock()):
        resp = client.post("/tools/rfdiffusion/submit", data=form)

    body = resp.get_data(as_text=True)
    # Not the campaign backstop message — a normal single-job path (here it
    # re-renders asking for the required PDB upload).
    assert "more than one GPU container" not in body


# ---------------------------------------------------------------------------
# D2 — pxDesign + rfantibody join the campaign path (PR-2)
# ---------------------------------------------------------------------------

def test_pxdesign_rfantibody_ceilings():
    # pxdesign's single-job pilot does exactly 24; rfantibody mirrors bindcraft.
    assert cc.single_container_ceiling("pxdesign") == 24
    assert cc.single_container_ceiling("rfantibody") == 16


def test_pxdesign_validator_allows_over_ceiling():
    # The 24-cap was raised so an over-ceiling count passes validation and can
    # reach the reroute/backstop instead of being rejected outright.
    from tools.pxdesign import validate as px_validate

    inputs, err = px_validate(
        {
            "preset": "pilot",
            "target_chain": "A",
            "hotspot_residues": "54",
            "binder_length": "80",
            "num_designs": "100",
        },
        {},
    )
    assert err is None, err
    assert inputs["num_designs"] == 100


def test_pxdesign_over_ceiling_reroutes_to_campaign(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"

    create_job = MagicMock()
    form = {
        "preset": "pilot",
        "target_chain": "A",
        "hotspot_residues": "54",
        "binder_length": "80",
        "num_designs": "100",  # pxdesign ceiling is 24
    }
    with patch("blueprints.tools.load_user_context", return_value=_ctx()), patch(
        "shared.idempotency.load_user_context", return_value=None
    ), patch("blueprints.tools.tool_enabled", return_value=True), patch(
        "app.estimated_cost_for_tool", return_value=Decimal("0")
    ), patch("blueprints.tools.create_job", create_job):
        resp = client.post("/tools/pxdesign/submit", data=form)

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "campaign" in body.lower()
    assert "/campaigns/new" in body
    assert "max 24 per single job" in body
    create_job.assert_not_called()


def test_pxdesign_form_render_wires_reroute(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"

    with patch("blueprints.tools.load_user_context", return_value=_ctx()), patch(
        "blueprints.tools.tool_enabled", return_value=True
    ), patch(
        "blueprints.tools.get_or_create_wallet",
        return_value={"balance_usd": "100", "wallet_frozen": False},
    ):
        resp = client.get("/tools/pxdesign")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'data-campaign-ceiling="24"' in body
    assert 'id="campaign-submit-btn"' in body
