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
    def __init__(
        self,
        rows: list[dict],
        *,
        user_profile_rows: Optional[list[dict]] = None,
    ) -> None:
        # api_keys rows live here. user_profile_rows is a separate
        # in-memory table for CR-01 webhook-secret tests so an UPDATE
        # on user_profiles can't accidentally match an api_keys row.
        self._rows = rows
        self._user_profile_rows = user_profile_rows if user_profile_rows is not None else []

    def table(self, name: str) -> _FakeTable:
        if name == "user_profiles":
            return _FakeTable(self._user_profile_rows)
        return _FakeTable(self._rows)


@pytest.fixture()
def fake_rows() -> list[dict]:
    return []


@pytest.fixture()
def fake_user_profile_rows() -> list[dict]:
    # CR-01: every user in these tests has a user_profiles row so
    # ensure_webhook_secret can find them. The webhook_signing_secret
    # column starts NULL; mint_token / rotate_webhook_secret populates it.
    return [
        {
            "user_id": uid,
            "webhook_signing_secret": None,
            "webhook_secret_prefix": None,
            "webhook_secret_rotated_at": None,
        }
        for uid in ("user-1", "user-7", "user-9", "user-10", "u", "u1")
    ]


@pytest.fixture(autouse=True)
def patched_service_client(fake_rows, fake_user_profile_rows):
    client = _FakeClient(fake_rows, user_profile_rows=fake_user_profile_rows)
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
    # CR-01: mint_token now returns a triple — the webhook secret is the
    # third element. It is non-None on the first mint per user.
    plaintext, prefix, webhook_secret = result
    assert plaintext.startswith("rk_live_")
    # FIX HI-04 (fresh-review): prefix is the literal scheme tag only —
    # no plaintext randomness leaks into the persisted `prefix` column.
    # Pin both length and the no-randomness invariant so a future bump of
    # _PREFIX_DISPLAY_LEN can't silently re-introduce the leak.
    assert prefix == "rk_live_"
    assert len(prefix) == 8
    assert len(plaintext) > 20
    # CR-01: first mint for this user yields a brand-new whsec_… secret.
    assert webhook_secret is not None
    assert webhook_secret.startswith("whsec_")
    assert len(webhook_secret) > 20

    row = fake_rows[-1]
    assert row["user_id"] == "user-1"
    assert row["role"] == "member"
    assert row["label"] == "laptop"
    # Plaintext is never persisted on api_keys.
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
    plaintext, _, _ = api_keys_mod.mint_token(user_id="user-7", role="member")
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
    plaintext, _, _ = api_keys_mod.mint_token(user_id="user-9", role="viewer")
    ctx = api_keys_mod.resolve_token(plaintext)
    assert ctx is not None
    assert api_keys_mod.revoke_key(key_id=ctx.key_id, user_id="user-9") is True
    assert api_keys_mod.resolve_token(plaintext) is None


def test_revoke_scoped_to_owner(fake_rows):
    plaintext, _, _ = api_keys_mod.mint_token(user_id="user-10")
    ctx = api_keys_mod.resolve_token(plaintext)
    assert ctx is not None
    # Different user cannot revoke
    assert (
        api_keys_mod.revoke_key(key_id=ctx.key_id, user_id="attacker") is False
    )
    # Owner can
    assert api_keys_mod.revoke_key(key_id=ctx.key_id, user_id="user-10") is True


def test_viewer_role_cannot_write(fake_rows):
    plaintext, _, _ = api_keys_mod.mint_token(user_id="u", role="viewer")
    ctx = api_keys_mod.resolve_token(plaintext)
    assert ctx is not None
    assert ctx.role == "viewer"
    assert ctx.can_write is False


# ---------------------------------------------------------------------------
# CR-01 tests: per-tenant webhook signing secret
# ---------------------------------------------------------------------------


def test_mint_token_persists_webhook_secret_on_first_mint(
    fake_rows, fake_user_profile_rows
):
    """First mint per user pins a webhook secret onto user_profiles.

    The plaintext is returned once via the third tuple element. The
    persisted column matches the returned plaintext, the prefix column
    captures literally ``whsec_``, and a rotated_at timestamp is set.
    """
    plaintext, _, webhook_secret = api_keys_mod.mint_token(user_id="user-1")
    assert webhook_secret is not None
    assert webhook_secret.startswith("whsec_")

    profile = next(r for r in fake_user_profile_rows if r["user_id"] == "user-1")
    assert profile["webhook_signing_secret"] == webhook_secret
    assert profile["webhook_secret_prefix"] == "whsec_"
    assert profile["webhook_secret_rotated_at"] is not None


def test_second_mint_does_not_rotate_webhook_secret(
    fake_rows, fake_user_profile_rows
):
    """A second mint for the same user does NOT silently issue a fresh
    secret (would invalidate every receiver). Returns None in slot 3."""
    _, _, first_secret = api_keys_mod.mint_token(user_id="user-7")
    assert first_secret is not None

    _, _, second_secret = api_keys_mod.mint_token(user_id="user-7")
    assert second_secret is None

    profile = next(r for r in fake_user_profile_rows if r["user_id"] == "user-7")
    assert profile["webhook_signing_secret"] == first_secret


def test_rotate_webhook_secret_replaces_existing(fake_user_profile_rows):
    """``rotate_webhook_secret`` forces a new secret and returns it."""
    _, _, first_secret = api_keys_mod.mint_token(user_id="u1")
    assert first_secret is not None

    rotated = api_keys_mod.rotate_webhook_secret(user_id="u1")
    assert rotated is not None
    assert rotated != first_secret
    assert rotated.startswith("whsec_")

    profile = next(r for r in fake_user_profile_rows if r["user_id"] == "u1")
    assert profile["webhook_signing_secret"] == rotated


def test_resolve_webhook_secret_returns_plaintext(fake_user_profile_rows):
    """``resolve_webhook_secret`` is the dispatch-time lookup. It MUST
    return the plaintext (HMAC can't sign with a hash)."""
    _, _, expected = api_keys_mod.mint_token(user_id="user-9")
    assert expected is not None
    assert api_keys_mod.resolve_webhook_secret(user_id="user-9") == expected


def test_resolve_webhook_secret_returns_none_for_unknown_user(
    fake_user_profile_rows,
):
    """Unknown / unrotated users return None so the caller can decide
    whether to fall back to the env-var secret."""
    assert api_keys_mod.resolve_webhook_secret(user_id="never-minted") is None


def test_resolve_webhook_secret_rejects_empty_user_id(fake_user_profile_rows):
    """Defense-in-depth: a None / empty string short-circuits before the
    DB lookup so a caller bug can't accidentally fetch the first row."""
    assert api_keys_mod.resolve_webhook_secret(user_id="") is None
    assert api_keys_mod.resolve_webhook_secret(user_id=None) is None  # type: ignore[arg-type]


def test_get_webhook_secret_display_returns_prefix_only(
    fake_user_profile_rows,
):
    """Dashboard display path returns prefix + rotated_at only; the
    plaintext column is column-revoked at the DB layer and must not
    appear in the display dict."""
    _, _, expected = api_keys_mod.mint_token(user_id="user-10")
    assert expected is not None
    display = api_keys_mod.get_webhook_secret_display(user_id="user-10")
    assert display is not None
    assert display["prefix"] == "whsec_"
    assert display["rotated_at"] is not None
    assert "webhook_signing_secret" not in display
    # Sanity: the prefix dict must not contain the plaintext at all.
    assert expected not in display.values()


def test_get_webhook_secret_display_returns_none_before_first_mint(
    fake_user_profile_rows,
):
    display = api_keys_mod.get_webhook_secret_display(user_id="user-1")
    # No mint has happened yet for user-1 in this isolated test, so
    # there's no prefix to display.
    assert display is None


def test_webhook_secret_format_pin():
    """The minted secret obeys the migration-0028 CHECK shape so a
    receiver who validates against the documented regex won't reject
    a freshly issued secret."""
    import re

    secret = api_keys_mod._new_webhook_secret()
    assert re.match(r"^whsec_[A-Za-z0-9_-]{22,128}$", secret)
