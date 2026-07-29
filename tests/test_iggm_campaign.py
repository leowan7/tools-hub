"""IgGM compute-campaign onboarding tests (Phase A).

Covers the campaign registration + pricing wiring, the campaign-safe
``build_payload`` recompute, the ``affinity_maturation`` exclusion, and the
``FLAG_TOOL_IGGM`` gate. Offline: no live Modal/Supabase. The atomic IgGM tier
is exercised separately by test_iggm_smoke.py; here the focus is the fan-out.
"""

from __future__ import annotations

import pytest as _pytest

# This file boots create_app(), which triggers app.py's load_dotenv() and pulls
# the repo-root PRODUCTION service-role credentials into os.environ for the
# rest of the pytest process. Without this fixture the estimate tests issue a
# real tool_jobs_p90 SELECT against production (they patch
# shared.compute_campaigns.get_service_client, but _historical_p90_seconds
# resolves shared.credits.get_service_client) and every unmarked test file that
# runs afterwards inherits the poisoned environment.
pytestmark = _pytest.mark.usefixtures("isolate_supabase")

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import shared.compute_campaigns as cc
import tools.iggm as ig
from shared.feature_flags import tool_enabled

# A valid masked heavy chain (canonical + a 5-X CDR-H3 mask) for route tests.
_MASKED_HEAVY = (
    "QVQLVESGGGLVQPGGSLRLSCAASGFTFSSYAMSWVRQAPGKGLEWVSAISGSGGSTYYADSVKG"
    "RFTISRDNSKNTLYLQMNSLRAEDTAVYYCAKXXXXXWGQGTLVTVSS"
)


# ---------------------------------------------------------------------------
# Registration + pricing wiring (pure)
# ---------------------------------------------------------------------------


def test_iggm_registered_for_campaigns():
    assert "iggm" in cc.SUPPORTED_TOOLS
    assert cc._DESIGN_PARAM_KEY["iggm"] == "num_samples"
    # Pinned chunk so the estimate (preset='pilot', no iggm PRESET_CAPS row)
    # sizes correctly instead of collapsing to the baseline.
    assert cc._CHUNK_SIZE_OVERRIDE["iggm"] == 40
    # LINEAR, not fixed-container: the per-chunk hold must scale with the count.
    assert "iggm" not in cc._FIXED_CONTAINER_TOOLS
    # The Phase-0 scaling key resolves to iggm's real wallet scaling_param.
    assert cc._scaling_key_for("iggm") == "num_samples"


def test_iggm_plan_chunks_sizes_to_40():
    for preset in ("cdr_design", "complex_prediction", "fr_design",
                   "inverse_design", "pilot"):  # 'pilot' = the estimate default
        plan = cc.plan_chunks("iggm", 120, preset)
        assert plan.chunk_size == 40, preset
        assert plan.total_subjobs == 3, preset
        assert plan.design_param_key == "num_samples", preset
        assert plan.est_cost_per_chunk > 0


def test_iggm_hold_scales_with_count():
    # Not fixed-container, so a 40-design shard holds more than a 1-design one.
    big = cc.child_hold_usd("iggm", 40, "cdr_design")
    small = cc.child_hold_usd("iggm", 1, "cdr_design")
    assert big > small, (small, big)


def test_iggm_adapter_imported_on_driver_path():
    # _ensure_adapters must register the iggm adapter or _dispatch_chunk would
    # skip every chunk forever.
    cc._ensure_adapters()
    from tools.base import get as tool_get  # noqa: PLC0415
    assert tool_get("iggm") is not None


# ---------------------------------------------------------------------------
# build_payload: campaign-safe total_passes recompute
# ---------------------------------------------------------------------------


def _campaign_inputs(preset="cdr_design", num_samples=40):
    # Mirrors what _dispatch_chunk hands build_payload: the sanitized shared
    # params (validated at the placeholder num_samples=1, so total_passes /
    # parameters are STALE at 1) plus the injected per-chunk num_samples.
    return {
        "preset": preset, "run_task": "design",
        "antibody_fasta": [{"header": "H", "sequence": "X" * 100}],
        "fasta_origin": None, "antigen_chain": "A", "epitope_pdb_resnums": [],
        "max_antigen_size": 2000, "num_samples": num_samples,
        "total_passes": 1, "n_masked": 0, "relax": False,
        "parameters": {"n_designs_total": 1},
    }


def test_build_payload_recomputes_stale_total_passes():
    bp = ig.build_payload(_campaign_inputs(num_samples=40), "")
    assert bp["total_passes"] == 40
    assert bp["parameters"] == {"n_designs_total": 40}
    assert bp["num_samples"] == 40


def test_build_payload_atomic_non_affinity_unchanged():
    # Atomic path: total_passes already equals num_samples, so the recompute is
    # a no-op (byte-identical result).
    inp = _campaign_inputs(num_samples=8)
    inp.update(total_passes=8, parameters={"n_designs_total": 8})
    bp = ig.build_payload(inp, "")
    assert bp["total_passes"] == 8
    assert bp["parameters"] == {"n_designs_total": 8}


def test_build_payload_affinity_keeps_stored_total():
    # affinity_maturation is atomic-only; its stored total_passes
    # (= num_samples * n_masked) is authoritative and must NOT be recomputed.
    inp = _campaign_inputs(preset="affinity_maturation", num_samples=4)
    inp.update(total_passes=20, n_masked=5, parameters={"n_designs_total": 20})
    bp = ig.build_payload(inp, "")
    assert bp["total_passes"] == 20
    assert bp["parameters"] == {"n_designs_total": 20}


# ---------------------------------------------------------------------------
# Flag gate (FLAG_TOOL_IGGM)
# ---------------------------------------------------------------------------


def test_iggm_gated_off_by_default(monkeypatch):
    monkeypatch.delenv("FLAG_TOOL_IGGM", raising=False)
    assert tool_enabled("iggm") is False
    from shared.compute_campaigns import (
        campaign_tool_gated_off,
        visible_campaign_tools,
    )
    assert "iggm" not in visible_campaign_tools()
    assert campaign_tool_gated_off("iggm") is True


def test_iggm_visible_when_flag_on(monkeypatch):
    monkeypatch.setenv("FLAG_TOOL_IGGM", "on")
    assert tool_enabled("iggm") is True
    from shared.compute_campaigns import (
        campaign_tool_gated_off,
        visible_campaign_tools,
    )
    assert "iggm" in visible_campaign_tools()
    assert campaign_tool_gated_off("iggm") is False


# ---------------------------------------------------------------------------
# Route behaviour (Flask test client; auth + wallet mocked)
# ---------------------------------------------------------------------------


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


def test_iggm_estimate_hidden_off(client, monkeypatch):
    monkeypatch.delenv("FLAG_TOOL_IGGM", raising=False)
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.get("/api/campaigns/estimate?tool=iggm&requested_designs=120")
    data = resp.get_json()
    assert data["ok"] is False  # gated off -> "not available yet"


def test_iggm_estimate_ok_when_on(client, monkeypatch):
    monkeypatch.setenv("FLAG_TOOL_IGGM", "on")
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()), patch(
        "shared.wallet.get_or_create_wallet",
        return_value={"balance_usd": "1000", "wallet_frozen": False},
    ), patch("shared.compute_campaigns.get_service_client", return_value=None):
        resp = client.get("/api/campaigns/estimate?tool=iggm&requested_designs=120")
    data = resp.get_json()
    assert data["ok"] is True
    assert data["total_subjobs"] == 3
    assert float(data["budget_usd"]) > 0


def test_iggm_campaign_rejects_affinity_maturation(client, monkeypatch):
    monkeypatch.setenv("FLAG_TOOL_IGGM", "on")
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.post("/campaigns", data={
            "tool": "iggm",
            "preset": "affinity_maturation",
            "requested_designs": "120",
        })
    assert resp.status_code == 400
    assert "Affinity maturation is not available as a campaign" in resp.get_data(as_text=True)


def test_iggm_campaign_rejects_missing_epitope(client, monkeypatch):
    # IgGM needs an explicit epitope (our antigen-only input can't auto-derive
    # one, and design.py crashes on epitope=None). The campaign route validates
    # the params via the adapter BEFORE any PDB staging or GPU spend, so an
    # epitope-less cdr_design campaign is rejected pre-flight. Regression guard
    # for the live crash on canary 160454cb (all 3 shards, 2026-07-17).
    monkeypatch.setenv("FLAG_TOOL_IGGM", "on")
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.post("/campaigns", data={
            "tool": "iggm",
            "preset": "cdr_design",
            "requested_designs": "120",
            "fasta": f">H\n{_MASKED_HEAVY}",
            "target_chain": "A",
        })
    assert resp.status_code == 400
    assert "epitope" in resp.get_data(as_text=True).lower()


def test_iggm_option_hidden_in_form_when_off(client, monkeypatch):
    monkeypatch.delenv("FLAG_TOOL_IGGM", raising=False)
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.get("/campaigns/new")
    assert resp.status_code == 200
    assert '<option value="iggm"' not in resp.get_data(as_text=True)


def test_iggm_option_shown_in_form_when_on(client, monkeypatch):
    monkeypatch.setenv("FLAG_TOOL_IGGM", "on")
    _login(client)
    with patch("blueprints.campaigns.load_user_context", return_value=_ctx()):
        resp = client.get("/campaigns/new")
    assert resp.status_code == 200
    assert '<option value="iggm"' in resp.get_data(as_text=True)
