"""Smoke tests for the Platform API blueprint.

Cover the happy path on the three endpoints that don't touch the
database (targets list, cost-estimate, openapi.json). The deeper
campaigns flow (create → poll → results) gets its own test once the
0023 migration has been applied to a real Supabase project.

    pytest tests/test_platform_api.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from flask import Flask

from shared.api_keys import APIKeyContext
from tools.platform_api import platform_api_bp


def _build_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(platform_api_bp)
    return app


def _valid_ctx() -> APIKeyContext:
    return APIKeyContext(
        key_id="k1",
        user_id="u1",
        role="member",
        prefix="rk_live_abcd",
        label="test",
        created_at=None,
        last_used_at=None,
        revoked_at=None,
    )


def test_targets_returns_calibrated_catalogue():
    """The catalogue is populated; every entry carries the documented
    on-wire shape so an agent can plan without an extra round-trip."""
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ):
        resp = client.get(
            "/api/v1/targets",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body["targets"], list)
    assert body["total"] == len(body["targets"])
    assert body["total"] >= 5  # alpha catalogue floor
    sample = body["targets"][0]
    for required in ("target_id", "name", "supported_experiment_types", "typical_campaign_range_usd"):
        assert required in sample, f"missing required field: {required}"
    assert resp.headers["X-Robots-Tag"] == "noindex"
    assert resp.headers["Cache-Control"] == "no-store"


def test_targets_requires_auth():
    app = _build_app()
    client = app.test_client()
    resp = client.get("/api/v1/targets")
    assert resp.status_code == 401


def test_cost_estimate_yeast_display_requires_human_quote():
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ):
        resp = client.post(
            "/api/v1/experiments/cost-estimate",
            json={
                "experiment_type": "yeast_display",
                "candidate_count": 5000,
                "target_kind": "custom",
            },
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["requires_human_quote"] is True
    assert body["experiment_type"] == "yeast_display"
    assert isinstance(body["estimated_range_usd"], list)
    assert len(body["estimated_range_usd"]) == 2
    assert body["scoping_url"].startswith("http")


def test_cost_estimate_rejects_unknown_experiment_type():
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ):
        resp = client.post(
            "/api/v1/experiments/cost-estimate",
            json={"experiment_type": "totally_made_up", "target_kind": "custom"},
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "invalid_experiment_type"


def test_openapi_served_unauthenticated():
    app = _build_app()
    client = app.test_client()
    resp = client.get("/api/v1/openapi.json")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["openapi"].startswith("3.")
    assert "/experiments" in body["paths"]
    assert "/experiments/cost-estimate" in body["paths"]
    assert "/quotes/{quote_id}/confirm" in body["paths"]
    assert "Results" in body["components"]["schemas"]
    # Spec endpoint is cacheable (vs no-store on the dynamic endpoints).
    assert "max-age=300" in resp.headers["Cache-Control"]


def test_options_preflight_emits_cors_headers():
    """OPTIONS preflight must include the CORS allow-headers so a
    browser-based agent can call the API cross-origin. The status code
    can be either 200 (Flask auto-OPTIONS) or 204 (our explicit handler)
    — both are spec-compliant for preflight."""
    app = _build_app()
    client = app.test_client()
    resp = client.open("/api/v1/experiments", method="OPTIONS")
    assert resp.status_code in (200, 204)
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "Authorization" in resp.headers["Access-Control-Allow-Headers"]
    assert "Idempotency-Key" in resp.headers["Access-Control-Allow-Headers"]


def test_create_experiment_rejects_empty_sequences():
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ):
        resp = client.post(
            "/api/v1/experiments",
            json={
                "experiment_spec": {
                    "experiment_type": "yeast_display",
                    "target": {"custom": {"name": "HER2"}},
                    "sequences": {},
                }
            },
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "invalid_sequences"


def test_create_experiment_rejects_non_canonical_residues():
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ):
        resp = client.post(
            "/api/v1/experiments",
            json={
                "experiment_spec": {
                    "experiment_type": "yeast_display",
                    "target": {"custom": {"name": "HER2"}},
                    "sequences": {"d1": "MASR123XYZ"},
                }
            },
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "invalid_sequences"


def test_create_experiment_rejects_unknown_target_id():
    """A target_id not in the catalogue returns 404 unknown_target.

    Distinct from the old behaviour: before the catalogue shipped, any
    target_id returned 400 calibrated_targets_unavailable. Now unknown
    ids are properly distinguished from known ids."""
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ):
        resp = client.post(
            "/api/v1/experiments",
            json={
                "experiment_spec": {
                    "experiment_type": "yeast_display",
                    "target": {"target_id": "tgt_does_not_exist_v1"},
                    "sequences": {"d1": "MASR"},
                }
            },
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["error"]["code"] == "unknown_target"


def test_webhook_signature_roundtrip():
    """End-to-end check that sign_payload + verify_signature agree."""
    import time
    from shared.webhooks import (
        format_signature_header,
        sign_payload,
        verify_signature,
    )

    secret = "test-secret-for-roundtrip"
    body = b'{"event":"test"}'
    ts = int(time.time())
    v1 = sign_payload(ts, body, secret)
    header = format_signature_header(ts, v1)

    assert verify_signature(header, body, secret) is True
    # Mutated body fails
    assert verify_signature(header, body + b"x", secret) is False
    # Wrong secret fails
    assert verify_signature(header, body, "wrong-secret") is False


# ---------------------------------------------------------------------------
# DELETE /experiments/{id} — withdraw
# ---------------------------------------------------------------------------


def _viewer_ctx() -> APIKeyContext:
    return APIKeyContext(
        key_id="k1",
        user_id="u1",
        role="viewer",
        prefix="rk_live_view",
        label="test-viewer",
        created_at=None,
        last_used_at=None,
        revoked_at=None,
    )


def _api_campaign(status: str):
    """Minimal API-source Campaign for withdraw-route tests."""
    from shared.campaigns import Campaign

    return Campaign(
        id="exp-smoke-1",
        user_id="u1",
        source_job_id=None,
        candidate_indices=[0],
        target_name="Smoke Antigen",
        target_context="",
        assay_type="yeast_display",
        affinity_goal_kd_nm=None,
        timeline_weeks=None,
        budget_band="custom",
        status=status,
        ranomics_contact=None,
        notes_internal=None,
        created_at=None,
        reviewed_at=None,
        name="smoke",
        submission_source="api",
    )


def test_withdraw_deletes_waiting_experiment():
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch(
        "tools.platform_api.routes._load_owned_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),
    ), patch(
        "tools.platform_api.routes.delete_api_campaign", return_value=True
    ) as delete_mock:
        resp = client.delete(
            "/api/v1/experiments/exp-smoke-1",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["experiment_id"] == "exp-smoke-1"
    assert body["status"] == "Withdrawn"
    assert delete_mock.called
    # Security-critical: the delete must be owner-scoped and status-guarded.
    kw = delete_mock.call_args.kwargs
    assert kw.get("user_id") == "u1"
    allowed = kw.get("allowed_statuses") or frozenset()
    assert {"Draft", "WaitingForConfirmation"} <= set(allowed)


def test_withdraw_rejects_experiment_past_initial_review():
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch(
        "tools.platform_api.routes._load_owned_campaign",
        return_value=_api_campaign("Sorting"),
    ), patch(
        "tools.platform_api.routes.delete_api_campaign", return_value=True
    ) as delete_mock:
        resp = client.delete(
            "/api/v1/experiments/exp-smoke-1",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "not_withdrawable"
    # A row that has moved into lab work must never be deleted.
    assert not delete_mock.called


def test_withdraw_unknown_experiment_returns_404():
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch("tools.platform_api.routes.get_campaign", return_value=None):
        resp = client.delete(
            "/api/v1/experiments/does-not-exist",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "experiment_not_found"


def test_withdraw_requires_member_key():
    app = _build_app()
    client = app.test_client()
    with patch("shared.api_auth.resolve_token", return_value=_viewer_ctx()):
        resp = client.delete(
            "/api/v1/experiments/exp-smoke-1",
            headers={"Authorization": "Bearer rk_live_view"},
        )
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "forbidden_role"


def test_openapi_documents_withdraw():
    app = _build_app()
    client = app.test_client()
    body = client.get("/api/v1/openapi.json").get_json()
    path = body["paths"]["/experiments/{experiment_id}"]
    assert "delete" in path
    assert path["delete"]["responses"].get("409") is not None


def test_get_experiment_unknown_returns_404_not_500():
    """Regression: an unknown/not-owned id must 404, not 500.

    _load_owned_campaign returns an error Response on a miss; the handlers
    previously checked ``isinstance(campaign, tuple)``, which never matches a
    Response, so the 404 fell through into campaign_to_status_view and raised
    AttributeError -> 500. All four read/confirm handlers now share the fixed
    ``not isinstance(campaign, Campaign)`` check."""
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch("tools.platform_api.routes.get_campaign", return_value=None):
        resp = client.get(
            "/api/v1/experiments/does-not-exist",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "experiment_not_found"


# ---------------------------------------------------------------------------
# Withdraw hardening: TOCTOU race handling, query scoping, sibling 404s
# ---------------------------------------------------------------------------


def _web_campaign(status: str = "submitted"):
    """A web-form Campaign (submission_source='web'); invisible to the API."""
    from shared.campaigns import Campaign

    return Campaign(
        id="web-1",
        user_id="u1",
        source_job_id="job-1",
        candidate_indices=[0],
        target_name="HER2",
        target_context="",
        assay_type="yeast_display",
        affinity_goal_kd_nm=None,
        timeline_weeks=None,
        budget_band="pilot",
        status=status,
        ranomics_contact=None,
        notes_internal=None,
        created_at=None,
        reviewed_at=None,
        submission_source="web",
    )


class _FakeDeleteClient:
    """Minimal chainable Supabase stub that records the DELETE filters."""

    def __init__(self, returned_rows):
        self._rows = returned_rows
        self.filters: dict = {}

    def table(self, name):
        self.table_name = name
        return self

    def delete(self):
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def in_(self, col, vals):
        self.filters[col] = list(vals)
        return self

    def execute(self):
        class _R:
            pass

        r = _R()
        r.data = self._rows
        return r


def test_withdraw_race_gone_returns_404():
    """Status-guarded delete matched nothing and a re-check finds the row
    gone (withdrawn concurrently) -> 404, not a misleading 500."""
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch(
        "tools.platform_api.routes._load_owned_campaign",
        side_effect=[
            _api_campaign("WaitingForConfirmation"),
            ({"error": {"code": "experiment_not_found"}}, 404),
        ],
    ), patch(
        "tools.platform_api.routes.delete_api_campaign", return_value=False
    ):
        resp = client.delete(
            "/api/v1/experiments/exp-smoke-1",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 404


def test_withdraw_race_advanced_returns_409():
    """Delete matched nothing because the row advanced (quote issued) between
    load and delete -> 409, not 500."""
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch(
        "tools.platform_api.routes._load_owned_campaign",
        side_effect=[
            _api_campaign("WaitingForConfirmation"),
            _api_campaign("QuoteSent"),
        ],
    ), patch(
        "tools.platform_api.routes.delete_api_campaign", return_value=False
    ):
        resp = client.delete(
            "/api/v1/experiments/exp-smoke-1",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "not_withdrawable"


def test_withdraw_genuine_db_fault_returns_500():
    """Delete matched nothing yet the row is still owned + withdrawable: a
    genuine DB fault, surfaced as 500 (not a false 404/409)."""
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch(
        "tools.platform_api.routes._load_owned_campaign",
        side_effect=[
            _api_campaign("WaitingForConfirmation"),
            _api_campaign("WaitingForConfirmation"),
        ],
    ), patch(
        "tools.platform_api.routes.delete_api_campaign", return_value=False
    ):
        resp = client.delete(
            "/api/v1/experiments/exp-smoke-1",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 500
    assert resp.get_json()["error"]["code"] == "withdraw_failed"


def test_withdraw_rejects_web_form_campaign():
    """A submission_source='web' row is invisible to the API DELETE (404)."""
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch(
        "tools.platform_api.routes.get_campaign", return_value=_web_campaign()
    ):
        resp = client.delete(
            "/api/v1/experiments/web-1",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "experiment_not_found"


def test_cors_allows_delete_method():
    app = _build_app()
    client = app.test_client()
    resp = client.open("/api/v1/experiments", method="OPTIONS")
    assert "DELETE" in resp.headers.get("Access-Control-Allow-Methods", "")


def test_delete_api_campaign_scopes_query_and_returns_true():
    from shared import campaigns

    fake = _FakeDeleteClient([{"id": "exp-1"}])
    with patch.object(campaigns, "get_service_client", return_value=fake):
        ok = campaigns.delete_api_campaign(
            "exp-1",
            user_id="u1",
            allowed_statuses=frozenset({"Draft", "WaitingForConfirmation"}),
        )
    assert ok is True
    assert fake.filters["id"] == "exp-1"
    assert fake.filters["user_id"] == "u1"
    assert fake.filters["submission_source"] == "api"
    assert set(fake.filters["status"]) == {"Draft", "WaitingForConfirmation"}


def test_delete_api_campaign_false_when_no_row_matched():
    from shared import campaigns

    fake = _FakeDeleteClient([])
    with patch.object(campaigns, "get_service_client", return_value=fake):
        ok = campaigns.delete_api_campaign("exp-1", user_id="u1")
    assert ok is False


def test_delete_api_campaign_false_when_no_client():
    from shared import campaigns

    with patch.object(campaigns, "get_service_client", return_value=None):
        ok = campaigns.delete_api_campaign("exp-1", user_id="u1")
    assert ok is False


def test_get_quote_unknown_returns_404():
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch("tools.platform_api.routes.get_campaign", return_value=None):
        resp = client.get(
            "/api/v1/experiments/nope/quote",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 404


def test_get_results_unknown_returns_404():
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch("tools.platform_api.routes.get_campaign", return_value=None):
        resp = client.get(
            "/api/v1/experiments/nope/results",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 404


def test_confirm_quote_unknown_returns_404():
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch("tools.platform_api.routes.get_campaign", return_value=None):
        resp = client.post(
            "/api/v1/quotes/nope/confirm",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 404
