"""Concurrent-heartbeat integrity for ``_append_heartbeat_state`` (REVIEW #16).

Two heartbeats for the same job (a per-candidate beat overlapping the
15-min monitor beat across gunicorn workers) used to read the same
``inputs`` jsonb and clobber each other's ``_partial_candidates`` append.
The write is now guarded by an optimistic CAS on ``inputs._hb_version``
with a bounded retry, degrading to a plain last-writer-wins write if the
CAS filter is unavailable. These tests pin:

* the pure merge (dedup, cap, no input mutation, progress overwrite),
* a lost CAS race re-reads and preserves both candidates,
* a CAS-filter error falls back to a write that still persists progress.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from webhooks import modal as modal_webhook

JID = "job-hb-1"


# ---------------------------------------------------------------------------
# Pure merge
# ---------------------------------------------------------------------------


def test_merge_appends_and_dedups_without_mutating_input():
    base = {"_partial_candidates": [{"pdb_key": "a"}], "tool_param": 1}
    out = modal_webhook._hb_merge_inputs(
        base,
        stage="design",
        designs_completed=2,
        designs_total=10,
        new_candidate={"pdb_key": "b"},
    )
    # New candidate appended, existing preserved, unrelated keys kept.
    assert [c["pdb_key"] for c in out["_partial_candidates"]] == ["a", "b"]
    assert out["tool_param"] == 1
    assert out["_progress"] == {
        "stage": "design",
        "designs_completed": 2,
        "designs_total": 10,
    }
    # Input dict not mutated (retry safety).
    assert base["_partial_candidates"] == [{"pdb_key": "a"}]


def test_merge_dedups_repeat_candidate():
    base = {"_partial_candidates": [{"pdb_key": "a"}]}
    out = modal_webhook._hb_merge_inputs(
        base, stage="s", designs_completed=1, designs_total=1,
        new_candidate={"pdb_key": "a"},
    )
    assert len(out["_partial_candidates"]) == 1


# ---------------------------------------------------------------------------
# CAS store fake
# ---------------------------------------------------------------------------


class _VersionStore:
    """Models a single tool_jobs row with version-gated jsonb updates."""

    def __init__(self):
        self.inputs: dict = {}
        self.inject_lost_race = 0     # force N CAS updates to report 0 rows
        self.inject_filter_error = 0  # raise on N CAS-filtered updates
        self.reads = 0
        self.updates = 0

    def client(self):
        store = self

        class _Select:
            def eq(self, *_):
                return self

            def single(self):
                return self

            def execute(self):
                store.reads += 1
                return MagicMock(data={"inputs": dict(store.inputs)})

        class _Update:
            def __init__(self, payload):
                self.payload = payload
                self.cas_present = False
                self.cas_is_null = False
                self.cas_val = None

            def eq(self, col, val):
                if col != "id":
                    self.cas_present = True
                    self.cas_val = val
                return self

            def is_(self, col, _val):
                self.cas_present = True
                self.cas_is_null = True
                return self

            def execute(self):
                store.updates += 1
                if self.cas_present and store.inject_filter_error > 0:
                    store.inject_filter_error -= 1
                    raise RuntimeError("jsonb filter unsupported")
                if self.cas_present:
                    if store.inject_lost_race > 0:
                        store.inject_lost_race -= 1
                        return MagicMock(data=[])
                    cur = store.inputs.get("_hb_version")
                    ok = (cur is None) if self.cas_is_null else (
                        str(cur) == self.cas_val
                    )
                    if not ok:
                        return MagicMock(data=[])
                store.inputs = dict(self.payload["inputs"])
                return MagicMock(data=[{"id": JID, "inputs": dict(store.inputs)}])

        table = MagicMock()
        table.select = lambda *_, **__: _Select()
        table.update = lambda payload: _Update(payload)
        client = MagicMock()
        client.table.return_value = table
        return client


def _beat(store, candidate):
    with patch.object(
        modal_webhook, "get_service_client", lambda: store.client()
    ):
        modal_webhook._append_heartbeat_state(
            job_id=JID,
            stage="design",
            designs_completed=1,
            designs_total=5,
            new_candidate=candidate,
        )


def test_sequential_beats_keep_both_candidates_and_bump_version():
    store = _VersionStore()
    _beat(store, {"pdb_key": "a"})
    _beat(store, {"pdb_key": "b"})
    keys = [c["pdb_key"] for c in store.inputs["_partial_candidates"]]
    assert keys == ["a", "b"]
    assert store.inputs["_hb_version"] == 2


def test_lost_race_retries_and_preserves_candidate():
    store = _VersionStore()
    _beat(store, {"pdb_key": "a"})  # version -> 1
    store.inject_lost_race = 1      # next CAS write reports 0 rows once
    _beat(store, {"pdb_key": "b"})
    keys = [c["pdb_key"] for c in store.inputs["_partial_candidates"]]
    # Despite the lost race, the retry re-read the row (with 'a') and
    # merged 'b' on top — neither candidate dropped.
    assert keys == ["a", "b"]
    assert store.updates >= 3  # a-write + lost + successful retry


def test_cas_filter_error_falls_back_to_plain_write():
    store = _VersionStore()
    store.inject_filter_error = 99  # every CAS-filtered update raises
    _beat(store, {"pdb_key": "a"})
    # Fallback path still persisted the candidate + progress.
    assert [c["pdb_key"] for c in store.inputs["_partial_candidates"]] == ["a"]
    assert store.inputs["_progress"]["designs_total"] == 5
