"""Route-level tests for the Workspace gate on `/tools/<tool>/submit`.

Uses ColabFold as the test vehicle because:
  * Smoke preset has credits_cost=0 (no balance check trips).
  * No PDB requirement (no upload paths to mock).
  * The standard tool_form / tool_submit flow applies.

Covers:
  1. GET form: workspace_id+target_pdb_id query params surface as hidden
     inputs in the rendered HTML.
  2. POST submit with workspace context + active workspace: preflight
     is consulted, IDs propagate to create_job.
  3. POST submit with workspace context but no active workspace:
     redirects to /workspaces/new (purchase flow).
  4. POST submit with workspace context but cap exhausted: redirects
     to /workspaces/<id> so the upgrade CTA explains why.
  5. POST submit WITHOUT workspace context: legacy fallback path
     (no preflight call, no IDs passed to create_job).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

from shared.workspaces import PreflightResult, Workspace


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_colabfold_smoke.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_colabfold(monkeypatch):
    monkeypatch.setenv("FLAG_TOOL_COLABFOLD", "on")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")

    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    yield flask_app


def _login(client, email="user@example.com"):
    with client.session_transaction() as sess:
        sess["user_email"] = email


def _patched_user(monkeypatch, *, balance=10):
    monkeypatch.setattr(
        "app.load_user_context",
        lambda: SimpleNamespace(
            user_id="u-test-1", tier="free", balance=balance,
            email="user@example.com",
        ),
    )


def _ws_active(*, target="path/to/4Z18.pdb", spent=0.0, ws_id="ws-active-1"):
    now = datetime.now(timezone.utc)
    return Workspace(
        id=ws_id,
        user_id="u-test-1",
        target_pdb_id=target,
        target_label="PD-L1",
        sku="workspace_standard",
        modal_cap_usd=100.0,
        modal_spent_usd=spent,
        activated_at=now,
        expires_at=now + timedelta(days=30),
        refund_eligible_until=None,
        refunded_at=None,
        status="active",
        stripe_payment_intent_id=None,
        stripe_refund_id=None,
    )


# ---------------------------------------------------------------------------
# 1. GET form with workspace query params renders hidden inputs
# ---------------------------------------------------------------------------


class TestGetFormWithWorkspaceContext:
    def test_workspace_query_params_emit_hidden_inputs(
        self, app_with_colabfold, monkeypatch,
    ):
        _patched_user(monkeypatch)
        client = app_with_colabfold.test_client()
        _login(client)
        resp = client.get(
            "/tools/colabfold?workspace_id=ws-abc&target_pdb_id=path/to/x.pdb"
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'name="workspace_id" value="ws-abc"' in body
        assert 'name="target_pdb_id" value="path/to/x.pdb"' in body

    def test_no_query_params_no_hidden_inputs(
        self, app_with_colabfold, monkeypatch,
    ):
        _patched_user(monkeypatch)
        client = app_with_colabfold.test_client()
        _login(client)
        resp = client.get("/tools/colabfold")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'name="workspace_id"' not in body
        assert 'name="target_pdb_id"' not in body

    def test_partial_query_no_hidden_inputs(
        self, app_with_colabfold, monkeypatch,
    ):
        """workspace_id alone (no target_pdb_id) is not enough — both required."""
        _patched_user(monkeypatch)
        client = app_with_colabfold.test_client()
        _login(client)
        resp = client.get("/tools/colabfold?workspace_id=ws-abc")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'name="workspace_id"' not in body
        assert 'name="target_pdb_id"' not in body


# ---------------------------------------------------------------------------
# 2. POST submit with active workspace — preflight allows + IDs propagate
# ---------------------------------------------------------------------------


class TestSubmitWithActiveWorkspace:
    def test_preflight_called_and_create_job_receives_ids(
        self, app_with_colabfold, monkeypatch,
    ):
        _patched_user(monkeypatch)
        ws = _ws_active(target="path/to/4Z18.pdb")
        preflight_mock = MagicMock(
            return_value=PreflightResult(allow=True, workspace=ws, reason="ok")
        )
        create_job_mock = MagicMock(
            return_value=SimpleNamespace(
                id="job-x", job_token="t" * 64, status="pending",
            )
        )
        modal_submit_mock = MagicMock(return_value={"function_call_id": "fc-x"})

        with patch(
            "shared.workspaces.workspace_preflight", preflight_mock
        ), patch("app.create_job", create_job_mock), patch(
            "gpu.modal_client.ModalClient.submit", modal_submit_mock
        ), patch("app.set_modal_call"):
            client = app_with_colabfold.test_client()
            _login(client)
            client.post(
                "/tools/colabfold/submit",
                data={
                    "preset": "smoke",
                    "workspace_id": "ws-active-1",
                    "target_pdb_id": "path/to/4Z18.pdb",
                },
            )

        # Preflight was consulted with the right user+target.
        preflight_mock.assert_called_once()
        args = preflight_mock.call_args
        assert args.args[0] == "u-test-1"
        assert args.args[1] == "path/to/4Z18.pdb"

        # create_job got both workspace IDs forwarded.
        create_job_mock.assert_called_once()
        kwargs = create_job_mock.call_args.kwargs
        assert kwargs["target_pdb_id"] == "path/to/4Z18.pdb"
        assert kwargs["workspace_id"] == "ws-active-1"


# ---------------------------------------------------------------------------
# 3. POST submit with no active workspace -> redirect to purchase flow
# ---------------------------------------------------------------------------


class TestSubmitNoWorkspace:
    def test_no_workspace_redirects_to_workspaces_new(
        self, app_with_colabfold, monkeypatch,
    ):
        _patched_user(monkeypatch)
        preflight_mock = MagicMock(
            return_value=PreflightResult(
                allow=False, workspace=None, reason="no_workspace",
                upgrade_message="Activate a Target Workspace…",
            )
        )
        with patch(
            "shared.workspaces.workspace_preflight", preflight_mock
        ), patch("app.create_job") as create_job_mock:
            client = app_with_colabfold.test_client()
            _login(client)
            resp = client.post(
                "/tools/colabfold/submit",
                data={
                    "preset": "smoke",
                    "workspace_id": "ws-stale",
                    "target_pdb_id": "path/to/missing.pdb",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert "/workspaces/new" in resp.headers.get("Location", "")
        # No job written when blocked.
        create_job_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 4. POST submit with cap exhausted -> redirect to workspace detail
# ---------------------------------------------------------------------------


class TestSubmitCapExhausted:
    def test_cap_exhausted_redirects_to_workspace_detail(
        self, app_with_colabfold, monkeypatch,
    ):
        _patched_user(monkeypatch)
        ws = _ws_active(spent=100.0, ws_id="ws-full-1")
        preflight_mock = MagicMock(
            return_value=PreflightResult(
                allow=False, workspace=ws, reason="cap_exceeded",
                upgrade_message="Workspace cap hit…",
            )
        )
        with patch(
            "shared.workspaces.workspace_preflight", preflight_mock
        ), patch("app.create_job") as create_job_mock:
            client = app_with_colabfold.test_client()
            _login(client)
            resp = client.post(
                "/tools/colabfold/submit",
                data={
                    "preset": "smoke",
                    "workspace_id": "ws-full-1",
                    "target_pdb_id": "path/to/4Z18.pdb",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "/workspaces/ws-full-1" in location
        create_job_mock.assert_not_called()

    def test_expired_workspace_also_redirects_to_detail(
        self, app_with_colabfold, monkeypatch,
    ):
        _patched_user(monkeypatch)
        ws = _ws_active(ws_id="ws-expired-1")
        preflight_mock = MagicMock(
            return_value=PreflightResult(
                allow=False, workspace=ws, reason="expired",
                upgrade_message="Workspace expired",
            )
        )
        with patch(
            "shared.workspaces.workspace_preflight", preflight_mock
        ), patch("app.create_job") as create_job_mock:
            client = app_with_colabfold.test_client()
            _login(client)
            resp = client.post(
                "/tools/colabfold/submit",
                data={
                    "preset": "smoke",
                    "workspace_id": "ws-expired-1",
                    "target_pdb_id": "path/to/4Z18.pdb",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert "/workspaces/ws-expired-1" in resp.headers.get("Location", "")
        create_job_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 5. POST submit WITHOUT workspace context -> legacy path (no preflight)
# ---------------------------------------------------------------------------


class TestSubmitLegacyPath:
    def test_no_workspace_context_skips_preflight(
        self, app_with_colabfold, monkeypatch,
    ):
        """Backwards compat: a submit without workspace_id+target_pdb_id
        proceeds via the legacy credits gate. Preflight is NOT called.
        ``create_job`` receives ``target_pdb_id=None, workspace_id=None``
        so the completion-side charge wiring (item #6) skips this job."""
        _patched_user(monkeypatch, balance=100)
        preflight_mock = MagicMock(
            return_value=PreflightResult(
                allow=False, workspace=None, reason="no_workspace",
            )
        )
        create_job_mock = MagicMock(
            return_value=SimpleNamespace(
                id="job-legacy", job_token="t" * 64, status="pending",
            )
        )
        modal_submit_mock = MagicMock(return_value={"function_call_id": "fc-y"})

        with patch(
            "shared.workspaces.workspace_preflight", preflight_mock
        ), patch("app.create_job", create_job_mock), patch(
            "gpu.modal_client.ModalClient.submit", modal_submit_mock
        ), patch("app.set_modal_call"):
            client = app_with_colabfold.test_client()
            _login(client)
            client.post(
                "/tools/colabfold/submit",
                data={"preset": "smoke"},  # no workspace_* fields
            )

        preflight_mock.assert_not_called()
        create_job_mock.assert_called_once()
        kwargs = create_job_mock.call_args.kwargs
        assert kwargs.get("target_pdb_id") is None
        assert kwargs.get("workspace_id") is None
