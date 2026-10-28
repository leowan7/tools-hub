"""scripts/backfill_chain_sequences.py: dry run writes nothing, apply writes
once, a rerun finds nothing left."""

from __future__ import annotations

import pytest

from scripts import backfill_chain_sequences as bf
from tests.test_export_page_parity import _pdb

pytestmark = pytest.mark.usefixtures("isolate_supabase")


def _job(rows):
    return {"id": "j-1", "user_id": "u-1", "tool": "rfantibody",
            "status": "completed", "result": {"candidates": rows}}


def test_dry_run_reads_apply_writes_and_a_rerun_is_a_no_op(monkeypatch):
    pdb = _pdb({"H": "MKWC", "T": "KMKG"})
    fetched, writes = [], []

    def fetch(*, user_id, job_id, filename):
        fetched.append((user_id, job_id, filename))
        return None if filename.endswith("missing.pdb") else pdb

    monkeypatch.setattr(bf, "download_output", fetch)
    monkeypatch.setattr(bf, "_cas_update", lambda job_id, payload, *, allowed_current:
                        writes.append((job_id, payload, allowed_current)) or True)
    row = _job([{"pdb_key": "designs/a.pdb"}, {"pdb_key": "designs/missing.pdb"},
                {"pdb_key": "designs/b.pdb", "sequence": "MKWC"}])

    assert bf.backfill_job(row, apply=False) == 1
    assert writes == []
    assert ("u-1", "j-1", "designs/a.pdb") in fetched

    assert bf.backfill_job(row, apply=True) == 1
    [(job_id, payload, allowed)] = writes
    assert (job_id, allowed) == ("j-1", ("completed",))
    a, missing, b = payload["result"]["candidates"]
    assert a["chain_sequences"] == [["H", "MKWC"], ["T", "KMKG"]]
    assert "chain_sequences" not in missing and "chain_sequences" not in b

    writes.clear()
    assert bf.backfill_job({**row, "result": payload["result"]}, apply=True) == 0
    assert writes == []


def test_a_lost_status_race_reports_nothing_filled(monkeypatch):
    monkeypatch.setattr(bf, "download_output",
                        lambda **_kw: _pdb({"A": "MKWAG"}))
    monkeypatch.setattr(bf, "_cas_update", lambda *_a, **_k: False)
    assert bf.backfill_job(_job([{"pdb_key": "designs/a.pdb"}]), apply=True) == 0


def test_ingest_stores_inline_chains_and_slim_still_drops_the_bytes():
    import base64

    from shared.jobs import _slim_result_for_persist

    b64 = base64.b64encode(_pdb({"H": "MKWC", "T": "KMKG"})).decode()
    out = _slim_result_for_persist({"candidates": [
        {"pdb_key": "designs/a.pdb", "pdb_content_b64": b64},
        {"pdb_key": "designs/b.pdb"}]})
    a, b = out["candidates"]
    assert a == {"pdb_key": "designs/a.pdb",
                 "chain_sequences": [["H", "MKWC"], ["T", "KMKG"]]}
    assert b == {"pdb_key": "designs/b.pdb"}


def test_ingest_stops_storing_past_the_budget(monkeypatch):
    from shared import exports

    monkeypatch.setattr(exports, "_MAX_STORED_CHAIN_CHARS", 10)
    pdb = _pdb({"H": "MKWC"})  # 1 + 4 = 5 characters a row
    rows, filled = exports.store_chain_sequences(
        [{"pdb_key": f"designs/{i}.pdb"} for i in range(3)],
        fetch_bytes=lambda *_a: pdb, default_job_id="j-1")
    assert filled == 2
    assert "chain_sequences" not in rows[2]
