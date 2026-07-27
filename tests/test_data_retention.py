"""Data-retention sweeper + per-user erasure tests.

Covers, with a fully stubbed Storage client (no network):
  * the pure SELECTION logic — which objects are expired vs retained for a
    given cutoff, including the created_at -> updated_at fallback and the
    "unknown age is retained" rule;
  * DRY-RUN behaviour — the sweeper and the per-user erasure delete NOTHING
    unless explicitly run with dry_run=False;
  * live deletion touches only expired / only the target user's objects;
  * recursive + paginated enumeration (>100 objects under one prefix);
  * graceful degradation when there is no service client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from cron import purge_old_storage as mod
from shared.storage import BUCKET, CAMPAIGN_BUCKET, OUTPUT_BUCKET


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
    def __init__(self, name: str, campaigns: list):
        self.name = name
        self.campaigns = campaigns
        self._uid = None

    def select(self, *_cols):
        return self

    def eq(self, col, val):
        if col == "user_id":
            self._uid = val
        return self

    def execute(self):
        rows = [
            {"id": c["id"]} for c in self.campaigns
            if self.name == "lab_campaigns" and c["user_id"] == self._uid
        ]
        return SimpleNamespace(data=rows)


class _FakeClient:
    def __init__(self, buckets: dict, campaigns=None):
        self.removed: list = []
        self.storage = _FakeStorage(buckets, self.removed)
        self._campaigns = campaigns or []

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(name, self._campaigns)


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


def test_retention_window_floored():
    # A stray 0/negative window must not collapse the cutoff onto "now".
    assert mod._resolve_retention_days(0) == mod._MIN_RETENTION_DAYS
    assert mod._resolve_retention_days(-5) == mod._MIN_RETENTION_DAYS
    assert mod._resolve_retention_days(45) == 45


# ---------------------------------------------------------------------------
# Sweeper: dry-run vs apply
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
            "camp-1/design_0.pdb": _iso(90),          # expired
            "camp-1/results/summary.csv": _iso(1),    # fresh
        },
    }


def test_sweeper_dry_run_deletes_nothing():
    client = _FakeClient(_seed_buckets())
    summary = mod.purge_old_storage(dry_run=True, client=client)  # default is dry-run
    assert summary["dry_run"] is True
    assert summary["total_expired"] == 4     # 2 in inputs, 1 output, 1 campaign
    assert summary["total_deleted"] == 0
    assert client.removed == []              # nothing removed
    # Per-bucket expired counts.
    assert summary["buckets"][BUCKET]["expired"] == 2
    assert summary["buckets"][OUTPUT_BUCKET]["expired"] == 1
    assert summary["buckets"][CAMPAIGN_BUCKET]["expired"] == 1


def test_sweeper_default_is_dry_run():
    # Calling with no dry_run argument must NOT delete.
    client = _FakeClient(_seed_buckets())
    summary = mod.purge_old_storage(client=client)
    assert summary["dry_run"] is True
    assert client.removed == []


def test_sweeper_apply_deletes_only_expired():
    buckets = _seed_buckets()
    client = _FakeClient(buckets)
    summary = mod.purge_old_storage(dry_run=False, client=client)
    assert summary["total_deleted"] == 4
    assert set(client.removed) == {
        "u1/j1/target.pdb",
        "u2/j9/orphan.pdb",
        "u1/j1/designs/design_0.pdb",
        "camp-1/design_0.pdb",
    }
    # Fresh objects survive in the underlying store.
    assert "u1/j2/target.pdb" in buckets[BUCKET]
    assert "u1/j1/designs/design_1.pdb" in buckets[OUTPUT_BUCKET]
    assert "camp-1/results/summary.csv" in buckets[CAMPAIGN_BUCKET]


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
    # Each bucket's list attempt recorded a no-client failure.
    assert len(summary["errors"]) == len(mod.DATA_BUCKETS)


# ---------------------------------------------------------------------------
# Per-user erasure
# ---------------------------------------------------------------------------

def _seed_multiuser():
    buckets = {
        BUCKET: {
            "u1/j1/a.pdb": _iso(1),
            "u1/j2/b.pdb": _iso(1),
            "u2/j3/c.pdb": _iso(1),      # different user — must be untouched
        },
        OUTPUT_BUCKET: {
            "u1/j1/designs/d0.pdb": _iso(1),
            "u2/j3/designs/d0.pdb": _iso(1),
        },
        CAMPAIGN_BUCKET: {
            "camp-u1/design_0.pdb": _iso(1),
            "camp-u1/results/r.csv": _iso(1),
            "camp-u2/design_0.pdb": _iso(1),   # different user's campaign
        },
    }
    campaigns = [
        {"id": "camp-u1", "user_id": "u1"},
        {"id": "camp-u2", "user_id": "u2"},
    ]
    return buckets, campaigns


def test_purge_user_dry_run_finds_but_deletes_nothing():
    buckets, campaigns = _seed_multiuser()
    client = _FakeClient(buckets, campaigns)
    summary = mod.purge_user_objects("u1", client=client)  # default dry-run
    assert summary["dry_run"] is True
    assert summary["campaign_ids"] == ["camp-u1"]
    assert summary["buckets"][BUCKET]["found"] == 2
    assert summary["buckets"][OUTPUT_BUCKET]["found"] == 1
    assert summary["buckets"][CAMPAIGN_BUCKET]["found"] == 2
    assert client.removed == []


def test_purge_user_apply_deletes_only_that_user():
    buckets, campaigns = _seed_multiuser()
    client = _FakeClient(buckets, campaigns)
    summary = mod.purge_user_objects("u1", dry_run=False, client=client)
    assert set(client.removed) == {
        "u1/j1/a.pdb", "u1/j2/b.pdb",
        "u1/j1/designs/d0.pdb",
        "camp-u1/design_0.pdb", "camp-u1/results/r.csv",
    }
    # u2's objects survive across all three buckets.
    assert "u2/j3/c.pdb" in buckets[BUCKET]
    assert "u2/j3/designs/d0.pdb" in buckets[OUTPUT_BUCKET]
    assert "camp-u2/design_0.pdb" in buckets[CAMPAIGN_BUCKET]
    assert summary["buckets"][CAMPAIGN_BUCKET]["deleted"] == 2


def test_purge_user_empty_id_refused():
    buckets, campaigns = _seed_multiuser()
    client = _FakeClient(buckets, campaigns)
    summary = mod.purge_user_objects("", dry_run=False, client=client)
    assert "empty user_id refused" in summary["errors"]
    assert client.removed == []      # never enumerated or deleted anything
