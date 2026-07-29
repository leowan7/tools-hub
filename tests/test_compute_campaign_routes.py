"""Route tests for the /campaigns/* compute-campaign endpoints.

Verifies the templates render and the endpoints wire to the module without
live Supabase/Modal (auth + wallet + persistence are mocked).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

# These exercise real routes through a real create_app(), and app.py calls
# load_dotenv() at import, so without this fixture every render and every
# get_service_client() in here reaches the PRODUCTION Supabase project.
pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _ctx(user_id="u-1"):
    return SimpleNamespace(user_id=user_id, tier="free", balance=100, email="u@example.com")


def _login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"


def test_runs_new_renders(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.get("/campaigns/new")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "New campaign" in body
    assert "rfdiffusion" in body  # supported tool option
    assert 'id="rp-submit"' in body  # cost-confirm submit


def test_runs_list_renders(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), patch(
        "shared.compute_campaigns.list_campaigns_for_user", return_value=[]
    ):
        resp = client.get("/campaigns")
    assert resp.status_code == 200
    assert "Campaigns" in resp.get_data(as_text=True)


def test_estimate_ok(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), patch(
        "shared.wallet.get_or_create_wallet",
        return_value={"balance_usd": "1000", "wallet_frozen": False},
    ), patch("shared.compute_campaigns.get_service_client", return_value=None):
        resp = client.get("/api/campaigns/estimate?tool=rfdiffusion&requested_designs=24")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["total_subjobs"] == 2
    assert float(data["budget_usd"]) > 0
    # Fund-and-drain: the start gate is the first wave, surfaced to the UI.
    assert float(data["first_wave_usd"]) > 0
    assert data["affordable"] is True


def test_estimate_over_cap(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.get("/api/campaigns/estimate?tool=rfdiffusion&requested_designs=999999")
    data = resp.get_json()
    assert data["ok"] is False
    assert "sub-jobs" in data["error"]


def test_estimate_unsupported_tool(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.get("/api/campaigns/estimate?tool=mpnn&requested_designs=10")
    data = resp.get_json()
    assert data["ok"] is False


def test_post_missing_pdb_rerenders_with_error(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.post("/campaigns", data={
            "tool": "rfdiffusion",
            "requested_designs": "24",
            "target_chain": "A",
            "hotspot_residues": "417,453",
            "binder_length_min": "55",
            "binder_length_max": "65",
        })
    assert resp.status_code == 400
    assert "Upload a target PDB" in resp.get_data(as_text=True)


def test_post_over_cap_rerenders_with_error(client):
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.post("/campaigns", data={
            "tool": "rfdiffusion",
            "requested_designs": "999999",
        })
    assert resp.status_code == 400
    assert "sub-jobs" in resp.get_data(as_text=True)


def test_a_bindcraft_campaign_gets_past_preset_validation(client):
    """Regression: every bindcraft campaign used to 400 "Pick a preset."

    The create form's two `name="preset"` selects are both disabled unless the
    tool is proteina or iggm, so nothing posts the field for the five pilot
    tools, and the route built its validation dict straight from request.form.
    Four of the five adapters default the preset internally; bindcraft is the
    one that does not, so it alone rejected. This posts exactly what the real
    form posts for bindcraft -- note the deliberate absence of `preset` -- and
    asserts the run gets far enough to need a PDB, which is the NEXT check
    after validation.

    Do not "improve" this by adding preset to the payload: sending it is what
    the browser cannot do, and the test would then pass against the bug.
    """
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.post("/campaigns", data={
            "tool": "bindcraft",
            "requested_designs": "16",
            "target_chain": "A",
            "hotspot_residues": "417,453",
            "binder_length_min": "50",
            "binder_length_max": "100",
        })
    body = resp.get_data(as_text=True)
    assert "Pick a preset" not in body
    assert "Upload a target PDB" in body
