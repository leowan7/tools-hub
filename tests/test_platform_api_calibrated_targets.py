"""Tests for the calibrated targets catalogue.

Covers:
- GET /api/v1/targets returns the populated catalogue.
- POST /api/v1/experiments resolves a known target_id and skips human
  scoping.
- POST /api/v1/experiments rejects unknown ids (404), unsupported
  experiment types (400), and the target_id + custom combination (400).
- POST /api/v1/experiments/cost-estimate returns a calibrated band when
  given a known target_id; falls back to the placeholder for custom.
- The catalogue module's helpers behave (get_target, list_catalog,
  supports_experiment_type, cost_band).

    pytest tests/test_platform_api_calibrated_targets.py -v
"""

from __future__ import annotations

from unittest.mock import patch

from flask import Flask

from shared.api_keys import APIKeyContext
from tools.platform_api import platform_api_bp
from tools.platform_api import calibrated_targets as ct


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


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


def _auth_headers() -> dict:
    return {"Authorization": "Bearer rk_live_xxx"}


def _patch_auth():
    return patch("shared.api_auth.resolve_token", return_value=_valid_ctx())


# ---------------------------------------------------------------------------
# calibrated_targets module
# ---------------------------------------------------------------------------


def test_catalogue_has_at_least_five_entries():
    assert len(ct.CATALOG) >= 5


def test_each_entry_has_required_fields():
    required = {
        "target_id",
        "name",
        "supported_experiment_types",
        "typical_campaign_range_usd",
    }
    for entry in ct.CATALOG:
        missing = required - set(entry.keys())
        assert not missing, f"{entry.get('target_id')} missing {missing}"


def test_target_ids_are_unique():
    ids = [e["target_id"] for e in ct.CATALOG]
    assert len(ids) == len(set(ids))


def test_target_ids_use_tgt_prefix():
    for entry in ct.CATALOG:
        assert entry["target_id"].startswith("tgt_"), entry["target_id"]


def test_supported_experiment_types_have_a_cost_band():
    """If an entry advertises support for an assay, it must have a
    cost band for it. Catches malformed entries during catalogue
    additions."""
    for entry in ct.CATALOG:
        for etype in entry.get("supported_experiment_types") or []:
            band = (entry.get("typical_campaign_range_usd") or {}).get(etype)
            assert band, f"{entry['target_id']} missing band for {etype}"
            assert (
                isinstance(band, list)
                and len(band) == 2
                and band[0] < band[1]
            ), f"{entry['target_id']} bad band for {etype}: {band}"


def test_get_target_returns_known_entry():
    her2 = ct.get_target("tgt_her2_ecd_v1")
    assert her2 is not None
    assert her2["official_symbol"] == "ERBB2"


def test_get_target_returns_none_for_unknown_id():
    assert ct.get_target("tgt_does_not_exist") is None


def test_get_target_handles_empty_input():
    assert ct.get_target("") is None
    assert ct.get_target(None) is None  # type: ignore[arg-type]


def test_supports_experiment_type_yeast_display_default():
    entry = ct.get_target("tgt_her2_ecd_v1")
    assert ct.supports_experiment_type(entry, "yeast_display") is True


def test_supports_experiment_type_rejects_unsupported():
    her2 = ct.get_target("tgt_her2_ecd_v1")
    # HER2 catalogue entry has yeast + mammalian, not DMS.
    assert ct.supports_experiment_type(her2, "dms") is False


def test_cost_band_returns_two_ints():
    her2 = ct.get_target("tgt_her2_ecd_v1")
    band = ct.cost_band(her2, "yeast_display")
    assert band is not None
    assert len(band) == 2
    assert all(isinstance(v, int) for v in band)
    assert band[0] < band[1]


def test_cost_band_returns_none_on_malformed_band():
    # A corrupted / non-numeric band bound must yield None (the
    # Optional[list[int]] contract), not raise a TypeError that would
    # 500 the cost-estimate endpoint. Audit REVIEW.md #10.
    assert ct.cost_band(
        {"typical_campaign_range_usd": {"yeast_display": [None, 500]}},
        "yeast_display",
    ) is None
    assert ct.cost_band(
        {"typical_campaign_range_usd": {"yeast_display": ["low", "high"]}},
        "yeast_display",
    ) is None


# ---------------------------------------------------------------------------
# GET /api/v1/targets
# ---------------------------------------------------------------------------


def test_get_targets_returns_populated_catalogue():
    app = _build_app()
    client = app.test_client()
    with _patch_auth():
        resp = client.get("/api/v1/targets", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == len(body["targets"])
    assert body["total"] >= 5
    # First entry has the right shape.
    sample = body["targets"][0]
    assert sample["target_id"].startswith("tgt_")
    assert isinstance(sample["typical_campaign_range_usd"], dict)


def test_get_targets_includes_her2():
    app = _build_app()
    client = app.test_client()
    with _patch_auth():
        resp = client.get("/api/v1/targets", headers=_auth_headers())
    body = resp.get_json()
    ids = [t["target_id"] for t in body["targets"]]
    assert "tgt_her2_ecd_v1" in ids


# ---------------------------------------------------------------------------
# POST /api/v1/experiments — target_id resolution
# ---------------------------------------------------------------------------


def _make_create_payload(target: dict, experiment_type: str = "yeast_display") -> dict:
    return {
        "experiment_spec": {
            "experiment_type": experiment_type,
            "target": target,
            "sequences": {
                "d1": "MASRYLLNPHWGVQPRQQGS",
                "d2": "QVQLVQSGAEVKKPGASVKVSCKASGYTFTSYAMHWVRQAPGQRLEW",
            },
        }
    }


def test_create_experiment_rejects_target_id_with_custom():
    """target_id and custom are mutually exclusive."""
    app = _build_app()
    client = app.test_client()
    payload = _make_create_payload(
        target={
            "target_id": "tgt_her2_ecd_v1",
            "custom": {"name": "Custom on the side"},
        }
    )
    with _patch_auth():
        resp = client.post(
            "/api/v1/experiments", json=payload, headers=_auth_headers()
        )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "invalid_target"
    assert "mutually exclusive" in body["error"]["message"]


def test_create_experiment_rejects_non_string_target_id():
    app = _build_app()
    client = app.test_client()
    payload = _make_create_payload(target={"target_id": 12345})
    with _patch_auth():
        resp = client.post(
            "/api/v1/experiments", json=payload, headers=_auth_headers()
        )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "invalid_target"


def test_create_experiment_rejects_unknown_target_id_with_404():
    app = _build_app()
    client = app.test_client()
    payload = _make_create_payload(
        target={"target_id": "tgt_completely_made_up_v1"}
    )
    with _patch_auth():
        resp = client.post(
            "/api/v1/experiments", json=payload, headers=_auth_headers()
        )
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["error"]["code"] == "unknown_target"


def test_create_experiment_rejects_unsupported_experiment_type_for_target():
    """HER2 catalogue entry has no DMS calibration; submitting one fails."""
    app = _build_app()
    client = app.test_client()
    payload = _make_create_payload(
        target={"target_id": "tgt_her2_ecd_v1"},
        experiment_type="dms",
    )
    with _patch_auth():
        resp = client.post(
            "/api/v1/experiments", json=payload, headers=_auth_headers()
        )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["code"] == "unsupported_experiment_type"


def test_create_experiment_resolves_calibrated_target():
    """Happy path: known target_id + supported experiment_type creates
    the experiment with the catalogue name + context grafted in."""
    from unittest.mock import MagicMock
    from shared.campaigns import TransitionResult

    captured: dict = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        m = MagicMock()
        m.id = "exp-uuid-1"
        m.status = "Draft"
        return m

    app = _build_app()
    client = app.test_client()
    payload = _make_create_payload(target={"target_id": "tgt_her2_ecd_v1"})

    with _patch_auth(), patch(
        "tools.platform_api.routes.create_api_campaign", side_effect=_fake_create
    ), patch(
        "tools.platform_api.routes.transition_api_status",
        return_value=TransitionResult(
            moved=False, prev_status=None, campaign=None
        ),
    ), patch(
        "tools.platform_api.routes.campaign_to_api_view",
        return_value={"experiment_id": "exp-uuid-1", "status": "Draft"},
    ), patch(
        "tools.platform_api.routes._fire_webhook"
    ):
        resp = client.post(
            "/api/v1/experiments", json=payload, headers=_auth_headers()
        )

    assert resp.status_code == 201, resp.get_json()
    # Captured target_name comes from the catalogue, not custom input.
    assert captured["target_name"] == "HER2 ECD (subdomain IV)"
    # Context grafts all 5 catalogue fields the routes.py path advertises:
    # catalogue_target_id, uniprot_id, antigen_form, antigen_sequence_stub,
    # calibration_notes. Pin all of them so a future refactor that drops
    # any field in routes.py fails this test loudly.
    ctx = captured["target_context"]
    assert "catalogue_target_id: tgt_her2_ecd_v1" in ctx
    assert "uniprot_id: P04626" in ctx
    assert "antigen_form: Recombinant soluble ECD, biotinylated" in ctx
    assert "antigen_sequence (catalogue stub):" in ctx
    assert "calibration_notes:" in ctx


# ---------------------------------------------------------------------------
# POST /api/v1/experiments/cost-estimate
# ---------------------------------------------------------------------------


def test_cost_estimate_catalog_returns_calibrated_band():
    app = _build_app()
    client = app.test_client()
    with _patch_auth():
        resp = client.post(
            "/api/v1/experiments/cost-estimate",
            json={
                "experiment_type": "yeast_display",
                "target_kind": "catalog",
                "target_id": "tgt_her2_ecd_v1",
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["requires_human_quote"] is False
    assert body["target_id"] == "tgt_her2_ecd_v1"
    assert body["target_name"] == "HER2 ECD (subdomain IV)"
    band = body["estimated_range_usd"]
    assert len(band) == 2 and band[0] < band[1]


def test_cost_estimate_catalog_requires_target_id():
    app = _build_app()
    client = app.test_client()
    with _patch_auth():
        resp = client.post(
            "/api/v1/experiments/cost-estimate",
            json={
                "experiment_type": "yeast_display",
                "target_kind": "catalog",
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_target_id"


def test_cost_estimate_catalog_rejects_unknown_target_id():
    app = _build_app()
    client = app.test_client()
    with _patch_auth():
        resp = client.post(
            "/api/v1/experiments/cost-estimate",
            json={
                "experiment_type": "yeast_display",
                "target_kind": "catalog",
                "target_id": "tgt_nope_v1",
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "unknown_target"


def test_cost_estimate_catalog_rejects_unsupported_experiment_type():
    """HER2 has no DMS band → cost-estimate refuses too."""
    app = _build_app()
    client = app.test_client()
    with _patch_auth():
        resp = client.post(
            "/api/v1/experiments/cost-estimate",
            json={
                "experiment_type": "dms",
                "target_kind": "catalog",
                "target_id": "tgt_her2_ecd_v1",
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "unsupported_experiment_type"


def test_cost_estimate_custom_path_still_requires_human_quote():
    """Regression guard: the custom branch keeps its old shape."""
    app = _build_app()
    client = app.test_client()
    with _patch_auth():
        resp = client.post(
            "/api/v1/experiments/cost-estimate",
            json={
                "experiment_type": "yeast_display",
                "target_kind": "custom",
            },
            headers=_auth_headers(),
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["requires_human_quote"] is True
    assert "scoping_url" in body
