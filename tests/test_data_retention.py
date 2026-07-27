"""Data-retention sweeper + per-user erasure tests.

Covers, with a fully stubbed Storage client (no network):
  * the pure SELECTION logic — which objects are expired vs retained for a
    given cutoff, including the created_at -> updated_at fallback, the
    "unknown age is retained" rule, and the exact-cutoff boundary;
  * DRY-RUN behaviour — the sweeper and the per-user erasure delete NOTHING
    unless explicitly run with dry_run=False;
  * the bucket split — the AGE sweeper NEVER touches lab-campaigns (CRO
    deliverables), while per-user erasure DOES cover all three buckets;
  * live deletion touches only expired / only the target user's objects;
  * recursive + paginated enumeration (>100 objects under one prefix);
  * prefix-boundary safety (u1 vs u12 must not collide);
  * user_id validation (empty / None / whitespace / non-uuid / '/');
  * a surfaced (not silent) failure when the lab_campaigns lookup errors;
  * env-var parsing robustness and graceful no-client degradation.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cron import purge_old_storage as mod
from shared.storage import (
    AGE_SWEEP_BUCKETS,
    BUCKET,
    CAMPAIGN_BUCKET,
    DATA_BUCKETS,
    OUTPUT_BUCKET,
    list_objects_recursive,
)

# Real user ids are auth.users UUIDs; purge_user_objects now validates shape.
U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"
CAMP_U1 = "aaaaaaaa-0000-0000-0000-000000000001"
CAMP_U2 = "bbbbbbbb-0000-0000-0000-000000000002"


def _iso(days_ago: float, *, zulu: bool = False) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    if zulu:
        return dt.isoformat().replace("+00:00", "Z")
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Stub Supabase client — models each bucket as a flat {path: created_at} map
# and reproduces the non-recursive, paginated list() plus remove().
# ---------------------------------------------------------------------------

class _FakeBucket:
    def __init__(self, objects: dict, removed: list):
        self.objects = objects
        self.removed = removed

    def list(self, path=None, options=None):
        prefix = path or ""
        opts = options or {}
        limit = opts.get("limit", 100)
        offset = opts.get("offset", 0)
        # Folder-scoped, NOT string-prefix: "u1" lists children of "u1/" only,
        # never "u12/..." — the trailing slash is the collision guard.
        base = (prefix + "/") if prefix else ""
        folders: list[str] = []
        files: list[str] = []
        seen: set = set()
        for full in sorted(self.objects):
            if base:
                if not full.startswith(base):
                    continue
                rest = full[len(base):]
            else:
                rest = full
            if not rest:
                continue
            if "/" in rest:
                folder = rest.split("/", 1)[0]
                if folder not in seen:
                    seen.add(folder)
                    folders.append(folder)
            else:
                files.append(rest)
        entries = [
            {"name": f, "id": None, "created_at": None,
             "updated_at": None, "metadata": None}
            for f in folders
        ]
        for name in files:
            ts = self.objects[base + name]
            entries.append({
                "name": name,
                "id": "id-" + base + name,
                "created_at": ts,
                "updated_at": ts,
                "metadata": {"size": 1},
            })
        return entries[offset:offset + limit]

    def remove(self, paths):
        for p in paths:
            self.objects.pop(p, None)
            self.removed.append(p)
        return [{"name": p} for p in paths]


class _FakeStorage:
    def __init__(self, buckets: dict, removed: list):
        self.buckets = buckets
        self.removed = removed

    def from_(self, name: str) -> _FakeBucket:
        return _FakeBucket(self.buckets.setdefault(name, {}), self.removed)


class _FakeTable:
    def __init__(self, name: str, campaigns: list, raise_on_execute: bool,
                 compute_campaigns: list = None,
                 compute_raise: bool = False):
        self.name = name
        self.campaigns = campaigns
        self.raise_on_execute = raise_on_execute
        self.compute_campaigns = compute_campaigns or []
        self.compute_raise = compute_raise
        self._uid = None

    def select(self, *_cols):
        return self

    def eq(self, col, val):
        if col == "user_id":
            self._uid = val
        return self

    def execute(self):
        if self.name == "lab_campaigns" and self.raise_on_execute:
            raise RuntimeError("transient DB failure")
        if self.name == "compute_campaigns":
            if self.compute_raise:
                raise RuntimeError("transient DB failure")
            return SimpleNamespace(data=list(self.compute_campaigns))
        rows = [
            {"id": c["id"]} for c in self.campaigns
            if self.name == "lab_campaigns" and c["user_id"] == self._uid
        ]
        return SimpleNamespace(data=rows)


class _FakeClient:
    def __init__(self, buckets: dict, campaigns=None, campaigns_raise=False,
                 compute_campaigns=None, compute_raise=False):
        self.removed: list = []
        self.storage = _FakeStorage(buckets, self.removed)
        self._campaigns = campaigns or []
        self._campaigns_raise = campaigns_raise
        self._compute_campaigns = compute_campaigns or []
        self._compute_raise = compute_raise

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(
            name, self._campaigns, self._campaigns_raise,
            self._compute_campaigns, self._compute_raise,
        )


# ---------------------------------------------------------------------------
# Pure selection logic
# ---------------------------------------------------------------------------

def test_select_expired_classifies_by_age():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    entries = [
        {"path": "a", "created_at": _iso(60), "updated_at": _iso(60)},   # expired
        {"path": "b", "created_at": _iso(31, zulu=True),                 # expired (Z fmt)
         "updated_at": _iso(31, zulu=True)},
        {"path": "c", "created_at": _iso(5), "updated_at": _iso(5)},     # retained
        {"path": "d", "created_at": _iso(29), "updated_at": _iso(29)},   # retained (edge)
    ]
    expired, retained = mod.select_expired(entries, cutoff)
    assert {e["path"] for e in expired} == {"a", "b"}
    assert {e["path"] for e in retained} == {"c", "d"}


def test_created_at_falls_back_to_updated_at():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    # created_at missing -> must fall back to updated_at (old) => expired.
    assert mod._object_expired(
        {"created_at": None, "updated_at": _iso(90)}, cutoff
    ) is True


def test_unknown_age_is_retained():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    # Neither timestamp parses -> never delete on unknown age.
    assert mod._object_expired(
        {"created_at": None, "updated_at": "not-a-date"}, cutoff
    ) is False


def test_object_at_exact_cutoff_is_retained():
    # IN-03: ts == cutoff must be RETAINED under strict '<' semantics.
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    entry = {"created_at": cutoff.isoformat(), "updated_at": None}
    assert mod._object_expired(entry, cutoff) is False


# ---------------------------------------------------------------------------
# Retention-window resolution / env parsing (WR-02)
# ---------------------------------------------------------------------------

def test_retention_window_floored():
    # A stray 0/negative window must not collapse the cutoff onto "now".
    assert mod._resolve_retention_days(0) == mod._MIN_RETENTION_DAYS
    assert mod._resolve_retention_days(-5) == mod._MIN_RETENTION_DAYS
    assert mod._resolve_retention_days(45) == 45


def test_resolve_retention_days_non_numeric_env_falls_back():
    with patch.dict(os.environ, {"DATA_RETENTION_DAYS": "abc"}):
        assert mod._resolve_retention_days(None) == mod.RETENTION_DAYS


def test_resolve_retention_days_empty_env_falls_back():
    with patch.dict(os.environ, {"DATA_RETENTION_DAYS": "   "}):
        assert mod._resolve_retention_days(None) == mod.RETENTION_DAYS


def test_resolve_retention_days_valid_env_used():
    with patch.dict(os.environ, {"DATA_RETENTION_DAYS": "45"}):
        assert mod._resolve_retention_days(None) == 45


# ---------------------------------------------------------------------------
# Sweeper: dry-run vs apply, and the lab-campaigns exclusion (WR-01)
# ---------------------------------------------------------------------------

def _seed_buckets():
    return {
        BUCKET: {
            "u1/j1/target.pdb": _iso(60),      # expired
            "u1/j2/target.pdb": _iso(5),       # fresh
            "u2/j9/orphan.pdb": _iso(120),     # expired (also an orphan-style old file)
        },
        OUTPUT_BUCKET: {
            "u1/j1/designs/design_0.pdb": _iso(45),   # expired
            "u1/j1/designs/design_1.pdb": _iso(2),    # fresh
        },
        CAMPAIGN_BUCKET: {
            "camp-1/design_0.pdb": _iso(90),          # OLD but MUST be excluded
            "camp-1/results/summary.csv": _iso(1),    # fresh
        },
    }


def test_age_sweep_buckets_exclude_lab_campaigns():
    # WR-01: the age-sweep set must never include the CRO-deliverable bucket.
    assert CAMPAIGN_BUCKET not in AGE_SWEEP_BUCKETS
    assert set(AGE_SWEEP_BUCKETS) == {BUCKET, OUTPUT_BUCKET}
    assert CAMPAIGN_BUCKET in DATA_BUCKETS   # erasure still covers it


def test_sweeper_dry_run_deletes_nothing():
    client = _FakeClient(_seed_buckets())
    summary = mod.purge_old_storage(dry_run=True, client=client)  # default is dry-run
    assert summary["dry_run"] is True
    # Only tool-inputs (2) + tool-outputs (1) expired; lab-campaigns not swept.
    assert summary["total_expired"] == 3
    assert summary["total_deleted"] == 0
    assert client.removed == []              # nothing removed
    assert CAMPAIGN_BUCKET not in summary["buckets"]
    assert summary["buckets"][BUCKET]["expired"] == 2
    assert summary["buckets"][OUTPUT_BUCKET]["expired"] == 1


def test_sweeper_default_is_dry_run():
    # Calling with no dry_run argument must NOT delete.
    client = _FakeClient(_seed_buckets())
    summary = mod.purge_old_storage(client=client)
    assert summary["dry_run"] is True
    assert client.removed == []


def test_sweeper_never_deletes_lab_campaigns_even_old():
    # WR-01: an ancient lab-campaigns object survives a LIVE run untouched.
    buckets = _seed_buckets()
    client = _FakeClient(buckets)
    summary = mod.purge_old_storage(dry_run=False, client=client)
    assert "camp-1/design_0.pdb" in buckets[CAMPAIGN_BUCKET]     # survives
    assert "camp-1/results/summary.csv" in buckets[CAMPAIGN_BUCKET]
    assert all("camp-1/" not in p for p in client.removed)
    assert CAMPAIGN_BUCKET not in summary["buckets"]


def test_input_of_live_campaign_survives_sweep_despite_age():
    """A campaign re-mints a signed URL from its stored target path on EVERY
    wave, so age alone must not decide. An old input belonging to a campaign
    that can still dispatch has to survive, or every remaining chunk becomes
    unrunnable mid-flight."""
    buckets = _seed_buckets()
    client = _FakeClient(
        buckets,
        compute_campaigns=[
            {"target_storage_path": "u1/j1/target.pdb", "status": "running"},
        ],
    )
    summary = mod.purge_old_storage(dry_run=False, client=client)

    assert "u1/j1/target.pdb" in buckets[BUCKET]          # survives
    assert "u1/j1/target.pdb" not in client.removed
    assert summary["buckets"][BUCKET]["protected"] == 1
    # The other expired input (no live campaign) is still swept.
    assert "u2/j9/orphan.pdb" in client.removed


@pytest.mark.parametrize(
    "status", ["completed", "completed_with_failures", "failed", "cancelled"]
)
def test_input_of_terminal_campaign_is_swept(status):
    """A terminal campaign will never dispatch again, so its input is ordinary
    expired data and the guard must not pin it forever."""
    buckets = _seed_buckets()
    client = _FakeClient(
        buckets,
        compute_campaigns=[
            {"target_storage_path": "u1/j1/target.pdb", "status": status},
        ],
    )
    summary = mod.purge_old_storage(dry_run=False, client=client)

    assert "u1/j1/target.pdb" in client.removed
    assert summary["buckets"][BUCKET]["protected"] == 0


def test_unknown_live_campaign_set_blocks_input_deletion():
    """If the live-campaign set cannot be read we cannot prove an input is
    unreferenced, so the sweep must fail CLOSED for tool-inputs — matching the
    module's existing 'never delete on unknown age' posture. tool-outputs is
    unaffected."""
    buckets = _seed_buckets()
    client = _FakeClient(buckets, compute_raise=True)
    summary = mod.purge_old_storage(dry_run=False, client=client)

    assert "u1/j1/target.pdb" in buckets[BUCKET]          # nothing deleted
    assert "u2/j9/orphan.pdb" in buckets[BUCKET]
    assert summary["buckets"][BUCKET]["deleted"] == 0
    assert summary["buckets"][BUCKET]["protected"] is None
    assert any("active-campaign-lookup-failed" in e for e in summary["errors"])
    # The outputs bucket is not campaign-referenced and still sweeps.
    assert "u1/j1/designs/design_0.pdb" in client.removed


def test_active_campaign_paths_ignores_rows_without_a_target():
    """proteina curated-task campaigns carry target_storage_path=None; those
    rows must not poison the protected set with a None entry."""
    client = _FakeClient(
        {},
        compute_campaigns=[
            {"target_storage_path": None, "status": "running"},
            {"target_storage_path": "", "status": "running"},
            {"target_storage_path": "u1/j1/target.pdb", "status": "running"},
        ],
    )
    assert mod.active_campaign_input_paths(client=client) == {"u1/j1/target.pdb"}


def test_sweeper_apply_deletes_only_expired():
    buckets = _seed_buckets()
    client = _FakeClient(buckets)
    summary = mod.purge_old_storage(dry_run=False, client=client)
    assert summary["total_deleted"] == 3
    assert set(client.removed) == {
        "u1/j1/target.pdb",
        "u2/j9/orphan.pdb",
        "u1/j1/designs/design_0.pdb",
    }
    # Fresh objects survive in the underlying store.
    assert "u1/j2/target.pdb" in buckets[BUCKET]
    assert "u1/j1/designs/design_1.pdb" in buckets[OUTPUT_BUCKET]


def test_sweeper_idempotent_second_run_finds_nothing():
    buckets = _seed_buckets()
    client = _FakeClient(buckets)
    mod.purge_old_storage(dry_run=False, client=client)
    client.removed.clear()
    summary = mod.purge_old_storage(dry_run=False, client=client)
    assert summary["total_expired"] == 0
    assert client.removed == []


def test_sweeper_recursive_and_paginated():
    # 150 expired objects under one prefix exercises the >100 pagination path.
    big = {f"u1/j1/designs/design_{i}.pdb": _iso(90) for i in range(150)}
    client = _FakeClient({OUTPUT_BUCKET: big})
    summary = mod.purge_old_storage(
        dry_run=False, buckets=(OUTPUT_BUCKET,), client=client
    )
    assert summary["buckets"][OUTPUT_BUCKET]["scanned"] == 150
    assert summary["total_deleted"] == 150
    assert len(client.removed) == 150


def test_sweeper_no_client_degrades():
    with patch("shared.credits.get_service_client", lambda: None):
        summary = mod.purge_old_storage(dry_run=False)
    assert summary["total_deleted"] == 0
    # Each age-sweep bucket's list attempt recorded a no-client failure.
    assert len(summary["errors"]) == len(AGE_SWEEP_BUCKETS)


# ---------------------------------------------------------------------------
# Prefix-boundary safety (WR-05)
# ---------------------------------------------------------------------------

def test_prefix_boundary_no_collision_u1_vs_u12():
    # WR-05: enumerating prefix "u1" must NOT pull in "u12/..." objects.
    # This is the exact code path per-user erasure uses to locate a user's
    # objects, so a prefix collision here would over- or under-erase.
    client = _FakeClient(
        {BUCKET: {"u1/j/a.pdb": _iso(1), "u12/j/b.pdb": _iso(1)}}
    )
    got = list_objects_recursive(BUCKET, "u1", client=client)
    assert {e["path"] for e in got} == {"u1/j/a.pdb"}


# ---------------------------------------------------------------------------
# Per-user erasure — covers all three buckets, scoped to the one user
# ---------------------------------------------------------------------------

def _seed_multiuser():
    buckets = {
        BUCKET: {
            f"{U1}/j1/a.pdb": _iso(1),
            f"{U1}/j2/b.pdb": _iso(1),
            f"{U2}/j3/c.pdb": _iso(1),      # different user — must be untouched
        },
        OUTPUT_BUCKET: {
            f"{U1}/j1/designs/d0.pdb": _iso(1),
            f"{U2}/j3/designs/d0.pdb": _iso(1),
        },
        CAMPAIGN_BUCKET: {
            f"{CAMP_U1}/design_0.pdb": _iso(1),
            f"{CAMP_U1}/results/r.csv": _iso(1),
            f"{CAMP_U2}/design_0.pdb": _iso(1),   # different user's campaign
        },
    }
    campaigns = [
        {"id": CAMP_U1, "user_id": U1},
        {"id": CAMP_U2, "user_id": U2},
    ]
    return buckets, campaigns


def test_purge_user_dry_run_finds_but_deletes_nothing():
    buckets, campaigns = _seed_multiuser()
    client = _FakeClient(buckets, campaigns)
    summary = mod.purge_user_objects(U1, client=client)  # default dry-run
    assert summary["dry_run"] is True
    assert summary["campaign_ids"] == [CAMP_U1]
    assert summary["buckets"][BUCKET]["found"] == 2
    assert summary["buckets"][OUTPUT_BUCKET]["found"] == 1
    assert summary["buckets"][CAMPAIGN_BUCKET]["found"] == 2  # covers all three
    assert client.removed == []


def test_purge_user_apply_deletes_only_that_user():
    buckets, campaigns = _seed_multiuser()
    client = _FakeClient(buckets, campaigns)
    summary = mod.purge_user_objects(U1, dry_run=False, client=client)
    assert set(client.removed) == {
        f"{U1}/j1/a.pdb", f"{U1}/j2/b.pdb",
        f"{U1}/j1/designs/d0.pdb",
        f"{CAMP_U1}/design_0.pdb", f"{CAMP_U1}/results/r.csv",
    }
    # u2's objects survive across all three buckets (cross-user scoping).
    assert f"{U2}/j3/c.pdb" in buckets[BUCKET]
    assert f"{U2}/j3/designs/d0.pdb" in buckets[OUTPUT_BUCKET]
    assert f"{CAMP_U2}/design_0.pdb" in buckets[CAMPAIGN_BUCKET]
    assert summary["buckets"][CAMPAIGN_BUCKET]["deleted"] == 2


def test_purge_user_campaign_lookup_failure_is_surfaced():
    # WR-04: a DB failure resolving campaign ids must NOT be reported as a
    # clean run — it appears in errors, and lab-campaigns is skipped while the
    # other two buckets are still erased.
    buckets, campaigns = _seed_multiuser()
    client = _FakeClient(buckets, campaigns, campaigns_raise=True)
    summary = mod.purge_user_objects(U1, dry_run=False, client=client)
    assert any("campaign-lookup" in e for e in summary["errors"])
    assert summary["buckets"][CAMPAIGN_BUCKET]["found"] == 0
    # lab-campaigns objects untouched because we could not enumerate them.
    assert f"{CAMP_U1}/design_0.pdb" in buckets[CAMPAIGN_BUCKET]
    # inputs/outputs still erased.
    assert f"{U1}/j1/a.pdb" not in buckets[BUCKET]
    assert f"{U1}/j1/designs/d0.pdb" not in buckets[OUTPUT_BUCKET]


def test_purge_user_invalid_id_refused():
    # WR-03 + IN-04: empty, whitespace, None, non-uuid, and '/'-bearing ids
    # are all refused before ANY listing/deletion.
    buckets, campaigns = _seed_multiuser()
    for bad in ["", "   ", None, "not-a-uuid", f"{U1}/evil"]:
        client = _FakeClient(buckets, campaigns)
        summary = mod.purge_user_objects(bad, dry_run=False, client=client)
        assert summary["errors"], f"expected refusal for {bad!r}"
        assert client.removed == [], f"deleted something for {bad!r}"
        # Underlying store fully intact.
        assert len(buckets[BUCKET]) == 3
