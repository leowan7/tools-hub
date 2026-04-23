"""RLS regression tests for the Ranomics tools-hub Supabase schema.

Stream G (Wave-0 hardening). These tests hit a live Supabase project with
the anon key and assert that Row-Level Security is in force on every
user-scoped table AND every user-scoped view.

They exist because migrations 0001 and 0002 created views that defaulted
to SECURITY DEFINER — the views bypassed RLS on their underlying tables.
Migration 0003 recreated them with security_invoker = true. This test
suite is the tripwire: if anyone ever recreates a view without the
invoker flag, these assertions fail before the change reaches prod.

Environment
-----------
    SUPABASE_URL          — project URL (same one app code uses)
    SUPABASE_ANON_KEY     — anon/publishable key; RLS-gated

The tests skip (do not fail) when either env var is missing, so they are
safe to run in contributor environments that have no Supabase access.

Usage
-----
    pytest tests/test_rls.py -v
"""

from __future__ import annotations

import os

import pytest

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()

USER_SCOPED_TABLES = ("user_tier", "credits_ledger", "scout_runs")
USER_SCOPED_VIEWS = ("credits_balance", "scout_run_count_30d")
DENY_ALL_TABLES = ("stripe_events",)


@pytest.fixture(scope="module")
def anon_client():
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        pytest.skip("SUPABASE_URL / SUPABASE_ANON_KEY not configured")
    try:
        from supabase import create_client
    except ImportError:
        pytest.skip("supabase package not installed")
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


@pytest.mark.parametrize("table", USER_SCOPED_TABLES)
def test_anon_cannot_read_user_scoped_table(anon_client, table):
    """Unauthenticated anon caller must get zero rows from user-scoped tables.

    The self-read policy (auth.uid() = user_id) yields NULL on the left
    side for the anon role, so the predicate is false and no rows match.
    An anon caller seeing any rows means RLS is disabled or the policy is
    permissive — either way, a leak.
    """
    response = anon_client.table(table).select("*").limit(1).execute()
    rows = getattr(response, "data", []) or []
    assert rows == [], (
        f"anon read {len(rows)} row(s) from public.{table}; "
        "RLS is off or the self-read policy is too permissive"
    )


@pytest.mark.parametrize("view", USER_SCOPED_VIEWS)
def test_anon_cannot_read_user_scoped_view(anon_client, view):
    """Views over RLS-protected tables must honour RLS too.

    Regression test for the SECURITY DEFINER default — without
    security_invoker = true the view would return every row to any
    caller with the anon key. Two shapes of success are acceptable:

      1. Empty list — RLS on the underlying table filtered every row.
      2. PermissionError (42501) — anon lacks SELECT on a referenced
         table (e.g. auth.users under credits_balance). Stronger signal
         since the anon client is blocked at the Postgres permission
         layer, not just RLS-filtered.

    Any other outcome (a row returned, or a different error) means the
    view is leaking.
    """
    try:
        from postgrest.exceptions import APIError  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        APIError = Exception  # type: ignore[assignment,misc]

    try:
        response = anon_client.table(view).select("*").limit(1).execute()
    except APIError as exc:
        code = getattr(exc, "code", None) or (
            exc.args[0].get("code") if exc.args and isinstance(exc.args[0], dict) else None
        )
        assert code == "42501", (
            f"anon hit view public.{view} and got an unexpected error "
            f"({code}): {exc!r}"
        )
        return

    rows = getattr(response, "data", []) or []
    assert rows == [], (
        f"anon read {len(rows)} row(s) from view public.{view}; "
        "view is not security_invoker or underlying RLS is off"
    )


@pytest.mark.parametrize("table", DENY_ALL_TABLES)
def test_anon_denied_on_deny_all_table(anon_client, table):
    """Tables with RLS on and zero policies must return nothing to anon.

    stripe_events is written via the service role only. Anon must not see
    webhook payloads.
    """
    response = anon_client.table(table).select("*").limit(1).execute()
    rows = getattr(response, "data", []) or []
    assert rows == [], (
        f"anon read {len(rows)} row(s) from public.{table}; "
        "table should have RLS on with no policies for anon"
    )
