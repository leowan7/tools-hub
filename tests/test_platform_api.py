"""Smoke tests for the Platform API blueprint.

Cover the happy path on the three endpoints that don't touch the
database (targets list, cost-estimate, openapi.json). The deeper
campaigns flow (create → poll → results) gets its own test once the
0023 migration has been applied to a real Supabase project.

    pytest tests/test_platform_api.py -v
"""

from __future__ import annotations

import json
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


def _api_campaign(
    status: str,
    *,
    quote_total_usd=None,
    quote_line_items=None,
    quote_valid_until=None,
    quote_currency="USD",
    last_transition_at=None,
    results_status="none",
    results=None,
    webhook_url=None,
    notes_customer=None,
    notes_internal=None,
):
    """Minimal API-source Campaign for route tests.

    Quote / results / notes fields default to the empty state; pass them to
    exercise the populated paths.
    """
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
        notes_internal=notes_internal,
        created_at=None,
        reviewed_at=None,
        name="smoke",
        submission_source="api",
        webhook_url=webhook_url,
        results_status=results_status,
        last_transition_at=last_transition_at,
        quote_total_usd=quote_total_usd,
        quote_currency=quote_currency,
        quote_line_items=quote_line_items or [],
        quote_valid_until=quote_valid_until,
        results=results,
        notes_customer=notes_customer,
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


def test_confirm_quote_rejects_unpriced_quote():
    """#1: a QuoteSent row with no posted price (total_usd null) must 409
    quote_not_finalized and NOT advance into WaitingForMaterials. Guards
    against an agent accepting a price-less quote."""
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch(
        "tools.platform_api.routes._load_owned_campaign",
        return_value=_api_campaign("QuoteSent"),  # quote_total_usd defaults None
    ), patch(
        "tools.platform_api.routes.transition_api_status"
    ) as trans_mock:
        resp = client.post(
            "/api/v1/quotes/exp-smoke-1/confirm",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "quote_not_finalized"
    assert not trans_mock.called


def test_confirm_quote_succeeds_when_priced():
    """A QuoteSent row with a posted price confirms and advances to
    WaitingForMaterials."""
    app = _build_app()
    client = app.test_client()
    priced = _api_campaign("QuoteSent", quote_total_usd=48000.0)
    moved = _transition_result(
        moved=True,
        status="WaitingForMaterials",
        webhook_url=None,
        prev_status="QuoteSent",
    )
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch(
        "tools.platform_api.routes._load_owned_campaign", return_value=priced
    ), patch(
        "tools.platform_api.routes.transition_api_status", return_value=moved
    ) as trans_mock, patch(
        "tools.platform_api.routes.dispatch_webhook"
    ) as dispatch_mock:
        resp = client.post(
            "/api/v1/quotes/exp-smoke-1/confirm",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 200
    assert trans_mock.called
    assert resp.get_json()["status"] == "WaitingForMaterials"
    # A real confirm fires experiment.confirmed (prev_status QuoteSent).
    assert dispatch_mock.called
    _, kwargs = dispatch_mock.call_args
    assert kwargs["event_type"] == "experiment.confirmed"
    assert kwargs["payload"]["prev_status"] == "QuoteSent"



# ---------------------------------------------------------------------------
# GET /experiments/{id}/quote — operator quote (Phase 1)
# ---------------------------------------------------------------------------


def test_quote_not_ready_before_quotesent():
    """Before a quote is issued (Draft / WaitingForConfirmation) the
    endpoint 404s with quote_not_ready, regardless of quote columns."""
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch(
        "tools.platform_api.routes._load_owned_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),
    ):
        resp = client.get(
            "/api/v1/experiments/exp-smoke-1/quote",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "quote_not_ready"


def test_quote_returns_persisted_values():
    """Once QuoteSent with a persisted price, the endpoint hands back the
    real total, currency, line items, and validity — no stub fields."""
    app = _build_app()
    client = app.test_client()
    campaign = _api_campaign(
        "QuoteSent",
        quote_total_usd=48000.0,
        quote_line_items=[{"name": "Yeast-display campaign", "amount_usd": 48000.0}],
        quote_valid_until="2099-01-01T23:59:59+00:00",
        last_transition_at="2026-06-10T12:00:00+00:00",
    )
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch(
        "tools.platform_api.routes._load_owned_campaign", return_value=campaign
    ):
        resp = client.get(
            "/api/v1/experiments/exp-smoke-1/quote",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["experiment_id"] == "exp-smoke-1"
    assert body["quote_id"] == "exp-smoke-1"
    assert body["status"] == "QuoteSent"
    assert body["total_usd"] == 48000.0
    assert body["currency"] == "USD"
    assert body["line_items"] == [
        {"name": "Yeast-display campaign", "amount_usd": 48000.0}
    ]
    assert body["valid_until"] == "2099-01-01T23:59:59+00:00"
    assert body["issued_at"] == "2026-06-10T12:00:00+00:00"
    assert body["terms_url"].startswith("http")
    # A finalised quote carries no pending-note.
    assert "note" not in body


def test_quote_pending_note_when_total_null():
    """QuoteSent but the operator has not posted a price: total_usd is null
    and a soft note explains the quote is being finalised (not a $0 quote)."""
    app = _build_app()
    client = app.test_client()
    with patch(
        "shared.api_auth.resolve_token", return_value=_valid_ctx()
    ), patch(
        "tools.platform_api.routes._load_owned_campaign",
        return_value=_api_campaign("QuoteSent"),
    ):
        resp = client.get(
            "/api/v1/experiments/exp-smoke-1/quote",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total_usd"] is None
    assert body["line_items"] == []
    assert body["currency"] == "USD"
    assert "note" in body


def test_set_campaign_quote_writes_full_patch():
    """set_campaign_quote writes every quote column on each save (the form
    is the full source of truth), going through the service client."""
    from shared import campaigns as campaigns_mod

    captured: dict = {}

    class _Resp:
        data = [
            {
                "id": "exp-smoke-1",
                "user_id": "u1",
                "source_job_id": None,
                "candidate_indices": [0],
                "target_name": "Smoke Antigen",
                "assay_type": "yeast_display",
                "budget_band": "custom",
                "status": "QuoteSent",
                "submission_source": "api",
                "quote_total_usd": 1000,
                "quote_currency": "USD",
                "quote_line_items": [{"name": "x", "amount_usd": 1000}],
            }
        ]

    class _Table:
        def update(self, patch):
            captured["patch"] = patch
            return self

        def eq(self, *_a, **_k):
            return self

        def execute(self):
            return _Resp()

    class _Client:
        def table(self, _name):
            return _Table()

    with patch.object(campaigns_mod, "get_service_client", return_value=_Client()):
        result = campaigns_mod.set_campaign_quote(
            "exp-smoke-1",
            total_usd=1000.0,
            currency="USD",
            line_items=[{"name": "x", "amount_usd": 1000.0}],
            valid_until="2099-01-01T23:59:59+00:00",
            notes="scope notes",
        )

    patch_written = captured["patch"]
    assert set(patch_written.keys()) == {
        "quote_total_usd",
        "quote_currency",
        "quote_line_items",
        "quote_valid_until",
        "quote_notes",
    }
    assert patch_written["quote_total_usd"] == 1000.0
    assert patch_written["quote_currency"] == "USD"
    assert patch_written["quote_line_items"] == [{"name": "x", "amount_usd": 1000.0}]
    assert result is not None
    assert result.quote_total_usd == 1000.0


def test_openapi_documents_quote_currency():
    app = _build_app()
    client = app.test_client()
    body = client.get("/api/v1/openapi.json").get_json()
    quote_props = body["components"]["schemas"]["Quote"]["properties"]
    assert "currency" in quote_props
    assert "total_usd" in quote_props


# ---------------------------------------------------------------------------
# GET /experiments/{id}/results — results delivery (Phase 2)
# ---------------------------------------------------------------------------


def test_results_not_ready_when_status_none():
    app = _build_app()
    client = app.test_client()
    with patch("shared.api_auth.resolve_token", return_value=_valid_ctx()), patch(
        "tools.platform_api.routes._load_owned_campaign",
        return_value=_api_campaign("DataAnalysis", results_status="none"),
    ):
        resp = client.get(
            "/api/v1/experiments/exp-smoke-1/results",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "results_not_ready"


def test_results_returns_envelope_with_fresh_signed_urls():
    """download_paths resolve to fresh signed URLs; external downloads merge;
    rounds + sequences pass through; internal paths never leak to the client."""
    app = _build_app()
    client = app.test_client()
    campaign = _api_campaign(
        "Done",
        results_status="all",
        results={
            "rounds": [{"round_id": "r1", "sort_gate": "top1pct"}],
            "sequences": [
                {"user_key": "d1", "log2_enrichment": 3.2, "called_hit": True}
            ],
            "download_paths": {
                "enrichment_table_csv": "exp-smoke-1/results/enrichment_table_csv.csv"
            },
            "downloads": {"raw_reads_fastq": "https://example.com/raw.fastq.gz"},
        },
    )
    with patch("shared.api_auth.resolve_token", return_value=_valid_ctx()), patch(
        "tools.platform_api.routes._load_owned_campaign", return_value=campaign
    ), patch(
        "shared.storage.presigned_campaign_url",
        return_value="https://signed.example/enrichment.csv?token=abc",
    ):
        resp = client.get(
            "/api/v1/experiments/exp-smoke-1/results",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["results_status"] == "all"
    assert body["rounds"][0]["round_id"] == "r1"
    assert body["sequences"][0]["called_hit"] is True
    # Uploaded file -> fresh signed URL.
    assert body["downloads"]["enrichment_table_csv"].startswith("https://signed.example")
    # External link merged through.
    assert body["downloads"]["raw_reads_fastq"] == "https://example.com/raw.fastq.gz"
    # Internal storage paths never leak.
    assert "download_paths" not in body


def test_set_campaign_results_writes_column_and_status():
    from shared import campaigns as campaigns_mod

    captured: dict = {}

    class _Resp:
        data = [
            {
                "id": "exp-smoke-1",
                "user_id": "u1",
                "source_job_id": None,
                "candidate_indices": [0],
                "target_name": "x",
                "assay_type": "yeast_display",
                "budget_band": "custom",
                "status": "Done",
                "submission_source": "api",
                "results_status": "all",
                "results": {"rounds": [], "sequences": []},
            }
        ]

    class _Table:
        def update(self, patch):
            captured["patch"] = patch
            return self

        def eq(self, *_a, **_k):
            return self

        def execute(self):
            return _Resp()

    class _Client:
        def table(self, _name):
            return _Table()

    with patch.object(campaigns_mod, "get_service_client", return_value=_Client()):
        result = campaigns_mod.set_campaign_results(
            "exp-smoke-1",
            results={"rounds": [], "sequences": []},
            results_status="all",
        )
    assert set(captured["patch"].keys()) == {"results", "results_status"}
    assert captured["patch"]["results_status"] == "all"
    assert result is not None
    assert result.results_status == "all"


def test_set_campaign_results_rejects_bad_status():
    from shared import campaigns as campaigns_mod

    with pytest.raises(ValueError):
        campaigns_mod.set_campaign_results("x", results={}, results_status="bogus")


def test_results_save_route_persists_and_redirects():
    client = _full_app_client()
    saved = _api_campaign("DataAnalysis", results_status="all")
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("DataAnalysis", results_status="none"),
    ), patch(
        "shared.campaigns.set_campaign_results", return_value=saved
    ) as set_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/results",
            data={
                "results_status": "all",
                "results_json": '{"rounds": [], "sequences": []}',
            },
        )
    assert resp.status_code in (302, 303)
    assert "results_saved=1" in resp.headers["Location"]
    assert set_mock.called


def test_results_save_route_rejects_bad_json_before_write():
    client = _full_app_client()
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("DataAnalysis", results_status="none"),
    ), patch("shared.campaigns.set_campaign_results") as set_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/results",
            data={"results_status": "partial", "results_json": "{not valid json"},
        )
    assert resp.status_code in (302, 303)
    assert "results_error=1" in resp.headers["Location"]
    assert not set_mock.called


def test_results_save_fires_results_ready_webhook():
    """T2: when results genuinely become available (results_status leaves
    'none') and a webhook_url is set, the route fires one results_ready
    webhook."""
    client = _full_app_client()
    saved = _api_campaign(
        "DataAnalysis",
        results_status="all",
        webhook_url="https://hook.example/x",
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("DataAnalysis", results_status="none"),
    ), patch(
        "shared.campaigns.set_campaign_results", return_value=saved
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/results",
            data={
                "results_status": "all",
                "results_json": '{"rounds": [], "sequences": []}',
            },
        )
    assert resp.status_code in (302, 303)
    assert dispatch_mock.called
    _, kwargs = dispatch_mock.call_args
    assert kwargs["event_type"] == "experiment.results_ready"
    assert kwargs["payload"]["results_status"] == "all"


def test_results_save_no_webhook_without_url():
    """T2 negative: results became available but no webhook_url is set — no
    dispatch."""
    client = _full_app_client()
    saved = _api_campaign("DataAnalysis", results_status="all", webhook_url=None)
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("DataAnalysis", results_status="none"),
    ), patch(
        "shared.campaigns.set_campaign_results", return_value=saved
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/results",
            data={"results_status": "all", "results_json": ""},
        )
    assert resp.status_code in (302, 303)
    assert not dispatch_mock.called


def test_results_save_no_webhook_when_status_unchanged():
    """T2 negative: results_status did not change (already 'all') — no
    results_ready dispatch even with a webhook_url set."""
    client = _full_app_client()
    saved = _api_campaign(
        "DataAnalysis",
        results_status="all",
        webhook_url="https://hook.example/x",
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("DataAnalysis", results_status="all"),
    ), patch(
        "shared.campaigns.set_campaign_results", return_value=saved
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/results",
            data={"results_status": "all", "results_json": ""},
        )
    assert resp.status_code in (302, 303)
    assert not dispatch_mock.called


# ---------------------------------------------------------------------------
# Customer notification (Phase 3): notes_customer + notify-on-transition
# ---------------------------------------------------------------------------


def test_status_view_includes_notes_customer():
    app = _build_app()
    client = app.test_client()
    camp = _api_campaign("QuoteSent", notes_customer="Quote covers one sort round.")
    with patch("shared.api_auth.resolve_token", return_value=_valid_ctx()), patch(
        "tools.platform_api.routes._load_owned_campaign", return_value=camp
    ):
        resp = client.get(
            "/api/v1/experiments/exp-smoke-1",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 200
    assert resp.get_json()["notes_customer"] == "Quote covers one sort round."


def test_notes_internal_never_leaves_internal_surface():
    """Headline product invariant: notes_internal is operator-only and must
    NEVER reach the customer — not via the experiment status view, and not via
    a notification webhook payload. Only notes_customer may surface. This is
    the project's central data-leak guard; it previously had zero coverage."""
    SENTINEL = "INTERNAL-ONLY do-not-leak 9z8y7x"

    # (1) Status view: GET /experiments/{id} must not echo notes_internal even
    # when the row carries it alongside a customer-safe note.
    app = _build_app()
    api_client = app.test_client()
    camp = _api_campaign(
        "QuoteSent",
        notes_internal=SENTINEL,
        notes_customer="Customer-safe summary.",
    )
    with patch("shared.api_auth.resolve_token", return_value=_valid_ctx()), patch(
        "tools.platform_api.routes._load_owned_campaign", return_value=camp
    ):
        resp = api_client.get(
            "/api/v1/experiments/exp-smoke-1",
            headers={"Authorization": "Bearer rk_live_xxx"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "notes_internal" not in body
    assert SENTINEL not in resp.get_data(as_text=True)
    assert body.get("notes_customer") == "Customer-safe summary."

    # (2) Webhook payload: even when the PERSISTED row carries notes_internal,
    # the status-change payload must never include it.
    full_client = _full_app_client()
    tr = _transition_result(
        moved=True, status="QuoteSent", webhook_url="https://hook.example/x"
    )
    persisted = _api_campaign(
        "QuoteSent",
        webhook_url="https://hook.example/x",
        notes_customer="Customer-safe summary.",
        notes_internal=SENTINEL,
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation", quote_total_usd=48000.0),
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ), patch(
        "shared.campaigns.set_campaign_admin_fields", return_value=persisted
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with full_client.session_transaction() as sess:
            sess["user_email"] = STAFF
        full_client.post(
            "/admin/lab-projects/exp-smoke-1/status",
            data={
                "status": "QuoteSent",
                "notify_customer": "1",
                "notes_internal": SENTINEL,
                "notes_customer": "Customer-safe summary.",
            },
        )
    assert dispatch_mock.called
    _, kwargs = dispatch_mock.call_args
    payload = kwargs["payload"]
    assert "notes_internal" not in payload
    assert SENTINEL not in json.dumps(payload)
    assert payload.get("notes_customer") == "Customer-safe summary."

    # (3) Serialization guard: campaign_to_api_view is the POST /experiments
    # 201 creation body and the GET detail body. It must include notes_customer
    # but never notes_internal, even when the row carries both. This catches a
    # future edit that adds notes_internal to the view (which the route-level
    # assertions above would not).
    from shared.campaigns import campaign_to_api_view, campaign_to_status_view

    api_view = campaign_to_api_view(camp)
    assert "notes_internal" not in api_view
    assert SENTINEL not in json.dumps(api_view)
    assert api_view.get("notes_customer") == "Customer-safe summary."

    status_view = campaign_to_status_view(camp)
    assert "notes_internal" not in status_view
    assert SENTINEL not in json.dumps(status_view)


def _transition_result(*, moved, status, webhook_url, prev_status="WaitingForConfirmation"):
    from shared.campaigns import TransitionResult

    camp = _api_campaign(status, webhook_url=webhook_url)
    return TransitionResult(moved=moved, prev_status=prev_status, campaign=camp)


def test_admin_notify_fires_webhook_on_quotesent():
    client = _full_app_client()
    tr = _transition_result(
        moved=True, status="QuoteSent", webhook_url="https://hook.example/x"
    )
    # M1: the webhook note is sourced from the PERSISTED row (the refreshed
    # Campaign returned by set_campaign_admin_fields), not the raw form field,
    # so the payload can never diverge from what GET /experiments returns.
    persisted = _api_campaign(
        "QuoteSent",
        webhook_url="https://hook.example/x",
        notes_customer="Ready to confirm.",
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        # Priced row: a real QuoteSent transition has a posted price (else the
        # #1 status-dropdown guard blocks it).
        return_value=_api_campaign("WaitingForConfirmation", quote_total_usd=48000.0),
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ), patch(
        "shared.campaigns.set_campaign_admin_fields", return_value=persisted
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/status",
            data={
                "status": "QuoteSent",
                "notify_customer": "1",
                "notes_customer": "Ready to confirm.",
            },
        )
    assert resp.status_code in (302, 303)
    assert dispatch_mock.called
    _, kwargs = dispatch_mock.call_args
    assert kwargs["event_type"] == "experiment.status_changed"
    assert kwargs["payload"]["new_status"] == "QuoteSent"
    assert kwargs["payload"]["notes_customer"] == "Ready to confirm."


def test_admin_no_webhook_when_notes_persist_fails():
    """M1: if the operator attaches a customer note but the persist fails
    (set_campaign_admin_fields returns None), the webhook must NOT fire — it
    would otherwise carry a note the stored record never received."""
    client = _full_app_client()
    tr = _transition_result(
        moved=True, status="QuoteSent", webhook_url="https://hook.example/x"
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation", quote_total_usd=48000.0),
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ), patch(
        "shared.campaigns.set_campaign_admin_fields", return_value=None
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/status",
            data={
                "status": "QuoteSent",
                "notify_customer": "1",
                "notes_customer": "Ready to confirm.",
            },
        )
    assert resp.status_code in (302, 303)
    assert not dispatch_mock.called


def test_admin_notify_includes_stored_note_when_form_blank():
    """#2: a notify transition with a blank note field still carries the note
    already stored on the row, so the webhook payload never diverges from what
    GET /experiments returns (set_campaign_admin_fields returns the row with
    its prior note untouched)."""
    client = _full_app_client()
    tr = _transition_result(
        moved=True,
        status="Done",
        webhook_url="https://hook.example/x",
        prev_status="InReview",
    )
    persisted = _api_campaign(
        "Done",
        webhook_url="https://hook.example/x",
        notes_customer="Stored from a prior save.",
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        # Priced: a real InReview row passed QuoteSent, so it has a price (else
        # the #1 price-band guard blocks the move).
        return_value=_api_campaign("InReview", quote_total_usd=48000.0),
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ), patch(
        "shared.campaigns.set_campaign_admin_fields", return_value=persisted
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/status",
            data={"status": "Done", "notify_customer": "1"},
        )
    assert resp.status_code in (302, 303)
    assert dispatch_mock.called
    _, kwargs = dispatch_mock.call_args
    assert kwargs["payload"]["notes_customer"] == "Stored from a prior save."


def test_admin_no_webhook_when_notify_unchecked():
    client = _full_app_client()
    tr = _transition_result(
        moved=True, status="QuoteSent", webhook_url="https://hook.example/x"
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation", quote_total_usd=48000.0),
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ), patch(
        "shared.campaigns.set_campaign_admin_fields", return_value=tr.campaign
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/status",
            data={"status": "QuoteSent"},
        )
    assert resp.status_code in (302, 303)
    assert not dispatch_mock.called


def test_admin_no_webhook_for_non_customer_status():
    client = _full_app_client()
    tr = _transition_result(
        moved=True,
        status="Sorting",
        webhook_url="https://hook.example/x",
        prev_status="LibraryConstruction",
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        # Priced: a real LibraryConstruction row passed QuoteSent, so it carries
        # a price. Without one the #1 band guard would early-return and this test
        # would pass vacuously instead of exercising the _NOTIFY_STATUSES filter.
        return_value=_api_campaign("LibraryConstruction", quote_total_usd=48000.0),
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ) as trans_mock, patch(
        "shared.campaigns.set_campaign_admin_fields", return_value=tr.campaign
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/status",
            data={"status": "Sorting", "notify_customer": "1"},
        )
    assert resp.status_code in (302, 303)
    # The transition fires (Sorting is a real move), but Sorting is not a
    # customer-notify status, so no webhook dispatches.
    assert trans_mock.called
    assert not dispatch_mock.called


def test_results_upload_over_cap_redirects_gracefully():
    """An upload over MAX_CONTENT_LENGTH raises 413 during form parsing,
    before the route body; the 413 handler must degrade it to the friendly
    ?results_error=1 redirect instead of a raw error page."""
    import app as appmod

    flask_app = appmod.app
    client = flask_app.test_client()
    orig_cap = flask_app.config.get("MAX_CONTENT_LENGTH")
    flask_app.config["MAX_CONTENT_LENGTH"] = 64  # bytes — force the 413
    try:
        with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
            "shared.campaigns.get_campaign",
            return_value=_api_campaign("DataAnalysis", results_status="none"),
        ):
            with client.session_transaction() as sess:
                sess["user_email"] = STAFF
            resp = client.post(
                "/admin/lab-projects/exp-smoke-1/results",
                data={"results_status": "all", "results_json": "x" * 2000},
            )
    finally:
        flask_app.config["MAX_CONTENT_LENGTH"] = orig_cap
    assert resp.status_code in (302, 303)
    assert "results_error=1" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# POST /admin/lab-projects/{id}/quote — operator quote save (full-app route)
#
# These exercise the real admin route in app.py (not the API blueprint), so
# they import the full app and drive it with a staff session. They lock in the
# adversarial-review fix: a failed quote write must NOT advance the FSM or
# claim success, and a malformed valid_until must be rejected before any write.
# ---------------------------------------------------------------------------

STAFF = "staff@ranomics.com"


def _full_app_client():
    import app as appmod  # noqa: PLC0415

    return appmod.app.test_client()


def test_quote_save_failure_does_not_transition_or_claim_success():
    client = _full_app_client()
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),
    ), patch(
        "shared.campaigns.set_campaign_quote", return_value=None
    ) as set_mock, patch(
        "shared.campaigns.transition_api_status"
    ) as trans_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/quote",
            data={"quote_total_usd": "48000", "set_quote_sent": "1"},
        )
    assert resp.status_code in (302, 303)
    assert "quote_error=1" in resp.headers["Location"]
    assert set_mock.called
    # The FSM must not advance when the quote write failed.
    assert not trans_mock.called


def test_quote_save_success_transitions_and_redirects_quoted():
    client = _full_app_client()
    saved = _api_campaign("WaitingForConfirmation", quote_total_usd=48000.0)
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),
    ), patch(
        "shared.campaigns.set_campaign_quote", return_value=saved
    ) as set_mock, patch(
        "shared.campaigns.transition_api_status"
    ) as trans_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/quote",
            data={"quote_total_usd": "48000", "set_quote_sent": "1"},
        )
    assert resp.status_code in (302, 303)
    assert "quoted=1" in resp.headers["Location"]
    assert set_mock.called
    assert trans_mock.called


def test_quote_save_rejects_malformed_valid_until_before_any_write():
    client = _full_app_client()
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),
    ), patch(
        "shared.campaigns.set_campaign_quote"
    ) as set_mock, patch(
        "shared.campaigns.transition_api_status"
    ) as trans_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/quote",
            data={"quote_valid_until": "2026-13-45", "set_quote_sent": "1"},
        )
    assert resp.status_code in (302, 303)
    assert "quote_error=1" in resp.headers["Location"]
    # Rejected before touching the DB; no write, no transition.
    assert not set_mock.called
    assert not trans_mock.called


def test_quote_save_rejects_non_api_campaign():
    client = _full_app_client()
    web_campaign = _api_campaign("WaitingForConfirmation")
    object.__setattr__(web_campaign, "submission_source", "web")
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign", return_value=web_campaign
    ), patch(
        "shared.campaigns.set_campaign_quote"
    ) as set_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/quote",
            data={"quote_total_usd": "100"},
        )
    assert resp.status_code == 404
    assert not set_mock.called


def test_quote_save_fires_webhook_when_notify_and_moved():
    """#2: posting a quote with "Notify customer" ticked fires one
    status_changed webhook when the row actually moves to QuoteSent and has a
    webhook_url, so an autonomous agent learns the quote is ready."""
    client = _full_app_client()
    saved = _api_campaign("WaitingForConfirmation", quote_total_usd=48000.0)
    tr = _transition_result(
        moved=True, status="QuoteSent", webhook_url="https://hook.example/x"
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),
    ), patch(
        "shared.campaigns.set_campaign_quote", return_value=saved
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/quote",
            data={
                "quote_total_usd": "48000",
                "set_quote_sent": "1",
                "notify_customer": "1",
            },
        )
    assert resp.status_code in (302, 303)
    assert "quoted=1" in resp.headers["Location"]
    assert dispatch_mock.called
    _, kwargs = dispatch_mock.call_args
    assert kwargs["event_type"] == "experiment.status_changed"
    assert kwargs["payload"]["new_status"] == "QuoteSent"


def test_quote_save_no_webhook_when_notify_unchecked():
    """The quote-save path stays silent by default: no notify checkbox, no
    webhook, even when the row moves to QuoteSent with a webhook_url."""
    client = _full_app_client()
    saved = _api_campaign("WaitingForConfirmation", quote_total_usd=48000.0)
    tr = _transition_result(
        moved=True, status="QuoteSent", webhook_url="https://hook.example/x"
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),
    ), patch(
        "shared.campaigns.set_campaign_quote", return_value=saved
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/quote",
            data={"quote_total_usd": "48000", "set_quote_sent": "1"},
        )
    assert resp.status_code in (302, 303)
    assert not dispatch_mock.called


def test_quote_save_no_webhook_without_url():
    """Notify ticked and the row moved, but no webhook_url is set -> no
    dispatch."""
    client = _full_app_client()
    saved = _api_campaign("WaitingForConfirmation", quote_total_usd=48000.0)
    tr = _transition_result(moved=True, status="QuoteSent", webhook_url=None)
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),
    ), patch(
        "shared.campaigns.set_campaign_quote", return_value=saved
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ), patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/quote",
            data={
                "quote_total_usd": "48000",
                "set_quote_sent": "1",
                "notify_customer": "1",
            },
        )
    assert resp.status_code in (302, 303)
    assert not dispatch_mock.called


def test_quote_save_blocks_quotesent_without_price():
    """M2: 'Move to QuoteSent' with a blank total (and no summable line items)
    is rejected with quote_error=price_required; no write, no advance."""
    client = _full_app_client()
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),
    ), patch(
        "shared.campaigns.set_campaign_quote"
    ) as set_mock, patch(
        "shared.campaigns.transition_api_status"
    ) as trans_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/quote",
            data={"set_quote_sent": "1"},  # blank total
        )
    assert resp.status_code in (302, 303)
    assert "quote_error=price_required" in resp.headers["Location"]
    assert not set_mock.called
    assert not trans_mock.called


def test_quote_save_blocks_price_clobber_on_priced_row():
    """M2: re-saving the quote form with a blank total on a row already in the
    price-required band is rejected, so an existing price cannot be silently
    nulled (which would strand the customer's quote and break confirm_quote)."""
    client = _full_app_client()
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("QuoteSent", quote_total_usd=48000.0),
    ), patch(
        "shared.campaigns.set_campaign_quote"
    ) as set_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/quote",
            data={},  # blank total
        )
    assert resp.status_code in (302, 303)
    assert "quote_error=price_required" in resp.headers["Location"]
    assert not set_mock.called


def test_quote_save_no_webhook_on_noop_transition():
    """L3: re-saving a quote on a row already in QuoteSent does not move the
    FSM, so no quote-ready webhook fires even with notify ticked."""
    client = _full_app_client()
    saved = _api_campaign(
        "QuoteSent", quote_total_usd=48000.0, webhook_url="https://hook.example/x"
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign(
            "QuoteSent", quote_total_usd=48000.0, webhook_url="https://hook.example/x"
        ),
    ), patch(
        "shared.campaigns.set_campaign_quote", return_value=saved
    ), patch(
        "shared.campaigns.transition_api_status"
    ) as trans_mock, patch("shared.webhooks.dispatch_webhook") as dispatch_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/quote",
            data={
                "quote_total_usd": "48000",
                "set_quote_sent": "1",
                "notify_customer": "1",
            },
        )
    assert resp.status_code in (302, 303)
    # Already QuoteSent: the set_quote_sent block is skipped, so no move, no
    # quote-ready webhook.
    assert not trans_mock.called
    assert not dispatch_mock.called


def test_status_dropdown_blocks_quotesent_without_price():
    """#1: the bare admin status control must not move an API row to QuoteSent
    while quote_total_usd is null (it would create a price-less quote that
    confirm_quote now rejects). The transition must not fire."""
    client = _full_app_client()
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),  # total None
    ), patch(
        "shared.campaigns.transition_api_status"
    ) as trans_mock, patch(
        "shared.campaigns.set_campaign_admin_fields"
    ) as fields_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/status",
            data={"status": "QuoteSent"},
        )
    assert resp.status_code in (302, 303)
    assert "status_error=quote_required" in resp.headers["Location"]
    assert not trans_mock.called
    # L1: notes/contact typed in the same submission are still persisted; only
    # the price-less transition is blocked.
    assert fields_mock.called


def test_status_dropdown_blocks_skip_across_quote_line_without_price():
    """M1: the guard blocks ANY jump across the quote line without a price, not
    just the literal 'QuoteSent'. The FSM RPC is forward-only but not
    adjacency-enforced, so a null-price row could otherwise be pushed straight
    to WaitingForMaterials, skipping confirm_quote's guard entirely."""
    client = _full_app_client()
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),  # total None
    ), patch(
        "shared.campaigns.transition_api_status"
    ) as trans_mock, patch(
        "shared.campaigns.set_campaign_admin_fields"
    ):
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/status",
            data={"status": "WaitingForMaterials"},
        )
    assert resp.status_code in (302, 303)
    assert "status_error=quote_required" in resp.headers["Location"]
    assert not trans_mock.called


def test_status_dropdown_allows_cancel_without_price():
    """Cancelled is exempt from the price band: an unpriced API row can still
    be cancelled."""
    client = _full_app_client()
    tr = _transition_result(
        moved=True, status="Cancelled", webhook_url=None, prev_status="WaitingForConfirmation"
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation"),  # total None
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ) as trans_mock, patch(
        "shared.campaigns.set_campaign_admin_fields", return_value=tr.campaign
    ):
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/status",
            data={"status": "Cancelled"},
        )
    assert resp.status_code in (302, 303)
    assert "updated=1" in resp.headers["Location"]
    assert trans_mock.called


def test_status_dropdown_allows_quotesent_with_price():
    """The guard only blocks the price-less case: a QuoteSent move is allowed
    once quote_total_usd is set."""
    client = _full_app_client()
    tr = _transition_result(
        moved=True, status="QuoteSent", webhook_url=None
    )
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation", quote_total_usd=48000.0),
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ) as trans_mock, patch(
        "shared.campaigns.set_campaign_admin_fields", return_value=tr.campaign
    ):
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
        resp = client.post(
            "/admin/lab-projects/exp-smoke-1/status",
            data={"status": "QuoteSent"},
        )
    assert resp.status_code in (302, 303)
    assert "updated=1" in resp.headers["Location"]
    assert trans_mock.called


def test_admin_status_change_is_audit_logged():
    """cso L2: an admin campaign-status change writes a staff_action audit
    row (who + what + which entity) to the user_events trail."""
    client = _full_app_client()
    tr = _transition_result(moved=True, status="QuoteSent", webhook_url=None)
    with patch("shared.auth.STAFF_EMAILS", {STAFF}), patch(
        "shared.campaigns.get_campaign",
        return_value=_api_campaign("WaitingForConfirmation", quote_total_usd=48000.0),
    ), patch(
        "shared.campaigns.transition_api_status", return_value=tr
    ), patch(
        "shared.campaigns.set_campaign_admin_fields",
        return_value=_api_campaign("QuoteSent"),
    ), patch("shared.events.log_event") as log_mock:
        with client.session_transaction() as sess:
            sess["user_email"] = STAFF
            sess["user_id"] = "staff-uid"
        client.post(
            "/admin/lab-projects/exp-smoke-1/status",
            data={"status": "QuoteSent"},
        )

    audit = [
        kw for _, kw in log_mock.call_args_list
        if kw.get("event_type") == "staff_action:campaign_status"
    ]
    assert audit, "no staff_action audit row was logged"
    props = audit[0]["props"]
    assert props["staff_email"] == STAFF
    assert props["target_id"] == "exp-smoke-1"
    assert props["new_status"] == "QuoteSent"
