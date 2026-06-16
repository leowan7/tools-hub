"""Finalization regression — composite-tool jobs must reach 'succeeded'.

Guards the bug where a BoltzGen COMPLETED webhook returned HTTP 200 but the
job stayed 'running': the terminal payload inlined a base64 PDB per candidate
(redundant with the Storage upload), the multi-MB ``result`` jsonb write threw
inside ``_cas_update``, and the handler still answered 200 ``already_terminal``.

Two halves:
  * ``_slim_result_for_persist`` drops the redundant inline copies (and only
    those) so the write stays small.
  * The webhook now answers 500 when the write did NOT land, instead of a
    silent 200.

Fakes mirror the local pattern in ``tests/test_cancel_race.py``.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from shared import jobs as jobs_mod
from shared.jobs import _slim_result_for_persist
from webhooks import modal as modal_webhook


# ---------------------------------------------------------------------------
# Unit — _slim_result_for_persist
# ---------------------------------------------------------------------------


def _cand(rank: int, *, with_b64: bool = True, pdb_key: str | None = None) -> dict:
    out = {
        "rank": rank,
        "pdb_key": pdb_key or f"designs/design_{rank:03d}.cif",
        "scores": {"ipTM": 0.8, "pLDDT": 90.0, "refolding_rmsd": 1.1},
    }
    if with_b64:
        out["pdb_content_b64"] = "QUJD" * 64
    return out


class TestSlimResultForPersist:
    def test_strips_inline_for_storage_backed_candidates(self):
        result = {"candidates": [_cand(1), _cand(2), _cand(3)]}
        out = _slim_result_for_persist(result)
        assert len(out["candidates"]) == 3
        for cand in out["candidates"]:
            assert "pdb_content_b64" not in cand
            assert cand["pdb_key"].startswith("designs/")
            assert cand["scores"]["refolding_rmsd"] == 1.1

    def test_keeps_inline_for_failed_uploads(self):
        result = {
            "candidates": [_cand(1), _cand(2)],
            "failed_uploads": ["design_002.cif"],
        }
        out = _slim_result_for_persist(result)
        by_rank = {c["rank"]: c for c in out["candidates"]}
        assert "pdb_content_b64" not in by_rank[1]
        # Upload failed → inline is the only copy and must survive.
        assert "pdb_content_b64" in by_rank[2]

    def test_keeps_inline_for_non_storage_pdb_key(self):
        # Smoke/mini_pilot tiers use a bare-filename pdb_key with NO Storage
        # upload, so the inline copy is the only one and must survive.
        result = {"candidates": [_cand(1, pdb_key="design_001.cif")]}
        out = _slim_result_for_persist(result)
        assert "pdb_content_b64" in out["candidates"][0]

    def test_does_not_touch_top_level_no_fallback_fields(self):
        # AF2-shaped result: top-level base64 with no Storage copy.
        result = {
            "pdb_b64": "QUJD" * 32,
            "pae_matrix_b64": "QUJD" * 128,
            "candidates": [_cand(1)],
        }
        out = _slim_result_for_persist(result)
        assert out["pdb_b64"] == result["pdb_b64"]
        assert out["pae_matrix_b64"] == result["pae_matrix_b64"]
        assert "pdb_content_b64" not in out["candidates"][0]

    def test_passthrough_for_non_candidate_results(self):
        assert _slim_result_for_persist(None) is None
        assert _slim_result_for_persist({"pdb_b64": "x"}) == {"pdb_b64": "x"}
        assert _slim_result_for_persist({"candidates": []}) == {"candidates": []}

    def test_does_not_mutate_input(self):
        result = {"candidates": [_cand(1)]}
        _slim_result_for_persist(result)
        assert "pdb_content_b64" in result["candidates"][0]


# ---------------------------------------------------------------------------
# Integration — webhook route through complete_job + fake Supabase
# ---------------------------------------------------------------------------


def _row(**over) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "tool": "boltzgen",
        "preset": "pilot",
        "status": "running",
        "inputs": {},
        "result": None,
        "error": None,
        "modal_function_call_id": "fc-stub-boltzgen-pilot-abc",
        "job_token": "t" * 64,
        "gpu_seconds_used": None,
        "created_at": "2026-06-15T00:00:00Z",
        "started_at": "2026-06-15T00:00:00Z",
        "completed_at": None,
    }
    base.update(over)
    return base


class _FakeJobsStore:
    def __init__(self, rows: list[dict]):
        self.rows = {r["id"]: dict(r) for r in rows}

    def update(self, job_id: str, payload: dict) -> None:
        self.rows[job_id].update(payload)


def _fake_client_factory(store: _FakeJobsStore):
    def _fake_client():
        client = MagicMock()
        table = MagicMock()

        class _SelectQuery:
            def __init__(self):
                self._filters: dict = {}

            def eq(self, col, val):
                self._filters[col] = val
                return self

            def single(self):
                return self

            def _matches(self, row):
                return all(row.get(k) == v for k, v in self._filters.items())

            def execute(self):
                rows = [r for r in store.rows.values() if self._matches(r)]
                return MagicMock(
                    data=(dict(rows[0]) if rows else None), count=len(rows)
                )

        class _UpdateQuery:
            def __init__(self, payload):
                self._payload = payload
                self._job_id = None
                self._allowed: list | None = None

            def eq(self, col, val):
                if col == "id":
                    self._job_id = val
                return self

            def in_(self, col, values):
                if col == "status":
                    self._allowed = list(values)
                return self

            def execute(self):
                if self._job_id is None or self._job_id not in store.rows:
                    return MagicMock(data=[])
                current = store.rows[self._job_id].get("status")
                if self._allowed is not None and current not in self._allowed:
                    return MagicMock(data=[])
                store.update(self._job_id, self._payload)
                return MagicMock(data=[dict(store.rows[self._job_id])])

        table.select = lambda *_, **__: _SelectQuery()
        table.update = lambda payload: _UpdateQuery(payload)
        client.table.return_value = table
        return client

    return _fake_client


@pytest.fixture
def store():
    return _FakeJobsStore([])


@pytest.fixture
def patched_service_client(store):
    with patch.object(jobs_mod, "get_service_client", _fake_client_factory(store)):
        yield


def _webhook_client():
    from flask import Flask

    app = Flask(__name__)
    modal_webhook.register_modal_webhooks(app)
    return app.test_client()


class TestCompletedWebhookFinalizes:
    """A COMPLETED webhook with a large candidates payload must succeed and
    persist a slimmed ``result.candidates`` (no inline base64)."""

    def test_finalizes_and_slims(self, patched_service_client, store):
        row = _row(status="running")
        store.rows[row["id"]] = row
        candidates = [_cand(i) for i in range(1, 51)]

        with patch("shared.wallet.settle_hold"), patch(
            "shared.wallet.release_hold"
        ), patch.object(
            jobs_mod, "_send_completion_email", lambda _j: None
        ), patch.object(
            modal_webhook, "_emit_job_completed", lambda _j: None
        ):
            resp = _webhook_client().post(
                f"/webhooks/modal/{row['id']}/{row['job_token']}",
                json={"status": "COMPLETED", "output": {"candidates": candidates}},
            )

        assert resp.status_code == 200
        assert resp.get_json() == {"status": "recorded", "terminal": "succeeded"}
        stored = store.rows[row["id"]]
        assert stored["status"] == "succeeded"
        persisted = stored["result"]["candidates"]
        assert len(persisted) == 50
        assert all("pdb_content_b64" not in c for c in persisted)
        assert all(c["pdb_key"].startswith("designs/") for c in persisted)


class TestFailedFinalizeIsLoud:
    """When the terminal write does not land (CAS no-op), the webhook must
    answer 500 — not a false 200 success."""

    def test_returns_500_when_write_does_not_land(
        self, patched_service_client, store
    ):
        row = _row(status="running")
        store.rows[row["id"]] = row

        # Force the result write to no-op so the row stays 'running'.
        with patch.object(jobs_mod, "_cas_update", return_value=False), patch(
            "shared.wallet.settle_hold"
        ), patch("shared.wallet.release_hold"), patch.object(
            jobs_mod, "_send_completion_email", lambda _j: None
        ):
            resp = _webhook_client().post(
                f"/webhooks/modal/{row['id']}/{row['job_token']}",
                json={"status": "COMPLETED", "output": {"candidates": [_cand(1)]}},
            )

        assert resp.status_code == 500
        assert resp.get_json()["reason"] == "finalize_failed"
        assert store.rows[row["id"]]["status"] == "running"
        assert store.rows[row["id"]]["result"] is None
