"""Unit tests for shared.api_keys — minting, resolving, revoking.

Runs offline via a fake Supabase client (same pattern as
test_idempotency.py). No Railway / Supabase config required.

    pytest tests/test_api_keys.py -v
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional
from unittest.mock import patch

import pytest

from shared import api_keys as api_keys_mod


# ---------------------------------------------------------------------------
# Fake Supabase client (mirror of tests/test_idempotency.py shape).
# ---------------------------------------------------------------------------


class _FakeTable:
    """In-memory api_keys table emulator with the chainable methods used
    by shared.api_keys."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        # Filter state, reset on each execute().
        self._eqs: list[tuple[str, Any]] = []
        self._not_null: list[str] = []
        self._is_null: list[str] = []
        self._order_col: Optional[str] = None
        self._order_desc = False
        self._limit: Optional[int] = None
        # Pending mutations.
        self._pending_insert: Optional[dict] = None
        self._pending_update: Optional[dict] = None

    def select(self, *_args, **_kwargs) -> "_FakeTable":
        return self

    def eq(self, column: str, value: Any) -> "_FakeTable":
        self._eqs.append((column, value))
        return self

    def is_(self, column: str, sentinel: str) -> "_FakeTable":
        if sentinel == "null":
            self._is_null.append(column)
        return self

    def order(self, column: str, desc: bool = False) -> "_FakeTable":
        self._order_col = column
        self._order_desc = desc
        return self

    def limit(self, n: int) -> "_FakeTable":
        self._limit = n
        return self

    def insert(self, payload: dict) -> "_FakeTable":
        self._pending_insert = payload
        return self

    def update(self, payload: dict) -> "_FakeTable":
        self._pending_update = payload
        return self

    def _matches(self, row: dict) -> bool:
        for col, val in self._eqs:
            if row.get(col) != val:
                return False
        for col in self._is_null:
            if row.get(col) is not None:
                return False
        return True

    def execute(self) -> Any:
        if self._pending_insert is not None:
            row = dict(self._pending_insert)
            row.setdefault("id", f"key_{len(self._rows) + 1}")
            row.setdefault("created_at", "2026-06-04T00:00:00+00:00")
            row.setdefault("last_used_at", None)
            row.setdefault("revoked_at", None)
            self._rows.append(row)
            return type("R", (), {"data": [dict(row)]})()

        if self._pending_update is not None:
            patched = []
            for row in self._rows:
                if self._matches(row):
                    row.update(self._pending_update)
                    patched.append(dict(row))
            self._reset()
            return type("R", (), {"data": patched})()

        # SELECT
        matched = [dict(r) for r in self._rows if self._matches(r)]
        if self._order_col is not None:
            matched.sort(
                key=lambda r: r.get(self._order_col) or "",
                reverse=self._order_desc,
            )
        if self._limit is not None:
            matched = matched[: self._limit]
        self._reset()
        return type("R", (), {"data": matched})()

    def _reset(self) -> None:
        self._eqs.clear()
        self._not_null.clear()
        self._is_null.clear()
        self._order_col = None
        self._order_desc = False
        self._limit = None
        self._pending_insert = None
        self._pending_update = None


class _FakeClient:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def table(self, _name: str) -> _FakeTable:
        return _FakeTable(self._rows)


@pytest.fixture()
def fake_rows() -> list[dict]:
    return []


@pytest.fixture(autouse=True)
def patched_service_client(fake_rows):
    client = _FakeClient(fake_rows)
    with patch.object(api_keys_mod, "get_service_client", return_value=client):
        yield client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mint_token_returns_plaintext_and_prefix(fake_rows):
    result = api_keys_mod.mint_token(
        user_id="user-1", role="member", label="laptop"
    )
    assert result is not None
    plaintext, prefix = result
    assert plaintext.startswith("rk_live_")
    # FIX HI-04 (fresh-review): prefix is the literal scheme tag only —
    # no plaintext randomness leaks into the persisted `prefix` column.
    # Pin both length and the no-randomness invariant so a future bump of
    # _PREFIX_DISPLAY_LEN can't silently re-introduce the leak.
    assert prefix == "rk_live_"
    assert len(prefix) == 8
    assert len(plaintext) > 20

    row = fake_rows[-1]
    assert row["user_id"] == "user-1"
    assert row["role"] == "member"
    assert row["label"] == "laptop"
    # Plaintext is never persisted.
    assert plaintext not in row.values()
    # The persisted prefix matches the returned prefix and is JUST the
    # literal "rk_live_" — no random bits.
    assert row["prefix"] == "rk_live_"
    # Hashed_token is sha256 of plaintext.
    assert row["hashed_token"] == hashlib.sha256(
        plaintext.encode("utf-8")
    ).hexdigest()


def test_mint_rejects_invalid_role(fake_rows):
    with pytest.raises(ValueError):
        api_keys_mod.mint_token(user_id="user-1", role="admin")


def test_mint_caps_active_keys_per_user(fake_rows, monkeypatch):
    monkeypatch.setattr(api_keys_mod, "_MAX_KEYS_PER_USER", 2)
    a = api_keys_mod.mint_token(user_id="u1")
    b = api_keys_mod.mint_token(user_id="u1")
    c = api_keys_mod.mint_token(user_id="u1")
    assert a is not None
    assert b is not None
    # Cap reached.
    assert c is None


def test_resolve_token_roundtrips(fake_rows):
    plaintext, _ = api_keys_mod.mint_token(user_id="user-7", role="member")
    ctx = api_keys_mod.resolve_token(plaintext)
    assert ctx is not None
    assert ctx.user_id == "user-7"
    assert ctx.role == "member"
    assert ctx.is_active is True
    assert ctx.can_write is True


def test_resolve_token_rejects_unknown(fake_rows):
    assert api_keys_mod.resolve_token("rk_live_does_not_exist_xxxxxxxx") is None


def test_resolve_token_rejects_malformed():
    assert api_keys_mod.resolve_token("") is None
    assert api_keys_mod.resolve_token("hunter2") is None
    assert api_keys_mod.resolve_token("Bearer rk_live_xxxx") is None


def test_resolve_token_rejects_revoked(fake_rows):
    plaintext, _ = api_keys_mod.mint_token(user_id="user-9", role="viewer")
    ctx = api_keys_mod.resolve_token(plaintext)
    assert ctx is not None
    assert api_keys_mod.revoke_key(key_id=ctx.key_id, user_id="user-9") is True
    assert api_keys_mod.resolve_token(plaintext) is None


def test_revoke_scoped_to_owner(fake_rows):
    plaintext, _ = api_keys_mod.mint_token(user_id="user-10")
    ctx = api_keys_mod.resolve_token(plaintext)
    assert ctx is not None
    # Different user cannot revoke
    assert (
        api_keys_mod.revoke_key(key_id=ctx.key_id, user_id="attacker") is False
    )
    # Owner can
    assert api_keys_mod.revoke_key(key_id=ctx.key_id, user_id="user-10") is True


def test_viewer_role_cannot_write(fake_rows):
    plaintext, _ = api_keys_mod.mint_token(user_id="u", role="viewer")
    ctx = api_keys_mod.resolve_token(plaintext)
    assert ctx is not None
    assert ctx.role == "viewer"
    assert ctx.can_write is False
