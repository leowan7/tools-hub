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
