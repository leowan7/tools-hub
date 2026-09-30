"""P0-1 (QA 2026-09-30): every Scout -> tool handoff redirected with
``handoff_error=stage_failed``.

Cause: ``scout.handoff.resolve_user_id`` called ``admin.list_users()`` with
no page argument, which returns only GoTrue's first page of 50, so any
account past it resolved to None. ``scout.quota._resolve_user_id`` had the
same call. Both now walk every page via ``shared.credits.list_all_auth_users``.

    pytest tests/test_scout_handoff_failures.py -v
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from scout import handoff, quota

TARGET_EMAIL = "late-signup@example.com"
TARGET_ID = "u-page-2"


class _Paged:
    """GoTrue-like admin API: 50 per page when no page is given."""

    def __init__(self, users):
        self._users = users

    def list_users(self, page=None, per_page=None):
        page = page or 1
        per_page = per_page or 50
        start = (page - 1) * per_page
        return self._users[start:start + per_page]


def _client_with_users(n_before: int):
    users = [
        SimpleNamespace(email=f"u{i}@example.com", id=f"u-{i}")
        for i in range(n_before)
    ]
    users.append(SimpleNamespace(email=TARGET_EMAIL, id=TARGET_ID))
    return SimpleNamespace(auth=SimpleNamespace(admin=_Paged(users)))


@pytest.mark.parametrize("module, fn", [
    (handoff, "resolve_user_id"),
    (quota, "_resolve_user_id"),
])
def test_user_past_first_page_resolves(monkeypatch, module, fn):
    client = _client_with_users(60)
    monkeypatch.setattr(module, "_get_service_client", lambda: client)
    assert getattr(module, fn)(TARGET_EMAIL) == TARGET_ID


def _run(monkeypatch, tmp_path, *, user_id="u-1", staged="u-1/x/input.pdb",
         client=object()):
    pdb = tmp_path / "input.pdb"
    pdb.write_text("ATOM\n")
    monkeypatch.setattr(handoff, "resolve_user_id", lambda _e: user_id)
    monkeypatch.setattr(handoff, "stage_pdb", lambda **_k: staged)
    monkeypatch.setattr(handoff, "_get_service_client", lambda: client)
    return handoff.create_handoff(
        user_email="a@example.com", scout_job_id="j", target_chain="A",
        hotspot_residues=[1], pdb_path=pdb,
    )


def _table_client(result):
    def execute():
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(data=result)
    chain = SimpleNamespace(execute=execute)
    table = SimpleNamespace(insert=lambda _row: chain)
    return SimpleNamespace(table=lambda _name: table)


@pytest.mark.parametrize("kwargs, reason", [
    ({"user_id": None}, "user_unresolved"),
    ({"staged": None}, "stage_pdb"),
    ({"client": None}, "no_service_client"),
    ({"client": _table_client([])}, "insert_returned_no_rows"),
    ({"client": _table_client(RuntimeError("rls"))}, "insert_raised"),
])
def test_each_failure_logs_its_reason(monkeypatch, tmp_path, caplog, kwargs, reason):
    with caplog.at_level(logging.WARNING, logger="scout.handoff"):
        assert _run(monkeypatch, tmp_path, **kwargs) is None
    assert f"scout handoff failed: {reason}" in caplog.text


def test_success_returns_id(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, client=_table_client([{"id": "x"}]))
