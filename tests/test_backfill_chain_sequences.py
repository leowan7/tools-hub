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


def _no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(bf.time, "sleep", slept.append)
    return slept


def test_transient_errors_are_told_apart():
    from postgrest.exceptions import APIError
    from storage3.exceptions import StorageApiError

    def wrapped(cause):
        try:
            raise bf.StorageError("download failed") from cause
        except bf.StorageError as exc:
            return exc

    assert bf._transient(wrapped(StorageApiError("busy", "too_many_connections", 429)))
    assert bf._transient(wrapped(StorageApiError("db", "DatabaseTimeout", 544)))
    assert bf._transient(APIError({"code": "57014", "message": "statement timeout"}))
    assert bf._transient(APIError({"code": 521, "message": "JSON could not be generated"}))
    assert bf._transient(bf.httpx.ConnectError("refused"))
    assert not bf._transient(wrapped(StorageApiError("Object not found", "not_found", 404)))
    assert not bf._transient(APIError({"code": "42703", "message": "no column"}))


def test_retry_backs_off_on_transient_and_raises_the_rest(monkeypatch):
    from postgrest.exceptions import APIError

    slept = _no_sleep(monkeypatch)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise APIError({"code": "57014"})
        return "ok"

    assert bf._retry(flaky, "x", retries=5) == "ok"
    assert slept == [2.0, 4.0]

    with pytest.raises(APIError):
        bf._retry(lambda: (_ for _ in ()).throw(APIError({"code": "57014"})), "x", retries=2)
    with pytest.raises(ValueError):
        bf._retry(lambda: (_ for _ in ()).throw(ValueError()), "x", retries=5)


def test_downloads_pause_and_a_retried_out_read_lists_the_job(monkeypatch):
    from storage3.exceptions import StorageApiError

    slept = _no_sleep(monkeypatch)

    def fetch(*, user_id, job_id, filename):
        if filename.endswith("busy.pdb"):
            try:
                raise StorageApiError("busy", "too_many_connections", 429)
            except StorageApiError as exc:
                raise bf.StorageError("download failed") from exc
        return _pdb({"H": "MKWC"})

    monkeypatch.setattr(bf, "download_output", fetch)
    failed = set()
    row = _job([{"pdb_key": "designs/a.pdb"}, {"pdb_key": "designs/busy.pdb"}])
    assert bf.backfill_job(row, apply=False, pause=0.5, retries=1, failed=failed) == 1
    assert failed == {"j-1"}
    # a.pdb: one pause; busy.pdb: one 2s backoff, then one pause
    assert sorted(slept) == [0.5, 0.5, 2.0]


def test_a_failed_write_is_retried_then_listed(monkeypatch):
    slept = _no_sleep(monkeypatch)
    monkeypatch.setattr(bf, "download_output", lambda **_kw: _pdb({"A": "MKWAG"}))
    results = iter([False, True])
    monkeypatch.setattr(bf, "_cas_update", lambda *_a, **_k: next(results))
    failed = set()
    row = _job([{"pdb_key": "designs/a.pdb"}])
    assert bf.backfill_job(row, apply=True, retries=3, failed=failed) == 1
    assert failed == set() and slept == [0.0, 2.0]  # download pause, then backoff

    monkeypatch.setattr(bf, "_cas_update", lambda *_a, **_k: False)
    assert bf.backfill_job(row, apply=True, retries=2, failed=failed) == 0
    assert failed == {"j-1"}


class _Query:
    def __init__(self, client, columns):
        self.client, self.columns, self.filters = client, columns, {}

    def __getattr__(self, name):
        def add(*args):
            self.filters[name] = args
            return self
        return add

    def execute(self):
        self.client.queries.append((self.columns, self.filters))
        return self.client.respond(self.columns, self.filters)


class _Client:
    def __init__(self, respond):
        self.respond, self.queries = respond, []

    def table(self, _name):
        return type("T", (), {"select": lambda _s, cols: _Query(self, cols)})()


def _rows(ids):
    return type("R", (), {"data": [{"id": i} for i in ids]})()


def test_page_query_timeout_exits_with_the_resume_cursor(monkeypatch, capsys):
    from postgrest.exceptions import APIError

    slept = _no_sleep(monkeypatch)

    def respond(columns, filters):
        if columns == "id":
            if filters.get("gt") == ("id", "j-1"):
                raise APIError({"code": "57014", "message": "statement timeout"})
            return _rows(["j-1"])
        return type("R", (), {"data": [_job([])]})()

    monkeypatch.setattr(bf, "get_service_client", lambda: _Client(respond))
    assert bf.main(["--apply", "--retries", "2", "--pause", "0"]) == 2
    err = capsys.readouterr().err
    assert "resume with --after j-1" in err and "Traceback" not in err
    assert slept.count(2.0) == 1 and slept.count(4.0) == 1


def test_ids_reads_only_those_rows_and_pages_nothing(monkeypatch, capsys):
    _no_sleep(monkeypatch)
    client = _Client(lambda cols, f: type("R", (), {"data": [
        {**_job([]), "id": f["eq"][1]}]})())
    monkeypatch.setattr(bf, "get_service_client", lambda: client)
    assert bf.main(["--ids", "a, b", "--pause", "0"]) == 0
    assert [(c, f["eq"]) for c, f in client.queries] == [
        ("id,user_id,tool,status,result", ("id", "a")),
        ("id,user_id,tool,status,result", ("id", "b"))]
    assert "done: 2 job(s)" in capsys.readouterr().out
