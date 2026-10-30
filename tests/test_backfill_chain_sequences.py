"""scripts/backfill_chain_sequences.py: dry run writes nothing, apply writes
in pieces, a rerun finds nothing left."""

from __future__ import annotations

import copy

import pytest

from scripts import backfill_chain_sequences as bf
from tests.test_export_page_parity import _pdb

pytestmark = pytest.mark.usefixtures("isolate_supabase")


def _job(rows, job_id="j-1"):
    return {"id": job_id, "user_id": "u-1", "tool": "rfantibody",
            "status": "completed", "result": {"candidates": rows}}


class _Resp:
    def __init__(self, data):
        self.data = data


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
    """Fake service client: ``table()`` answers through ``respond``; the two
    migration 0046 functions run in Python against ``jobs``, as their SQL
    does."""

    def __init__(self, respond=None, jobs=()):
        self.respond, self.queries, self.writes = respond, [], []
        self.jobs = {j["id"]: copy.deepcopy(j) for j in jobs}

    def table(self, _name):
        return type("T", (), {"select": lambda _s, cols: _Query(self, cols)})()

    def rpc(self, name, args):
        return type("Q", (), {"execute": lambda _s: _Resp(getattr(self, name)(**args))})()

    def backfill_candidate_page(self, p_job_id, p_offset, p_limit):
        result = self.jobs[p_job_id]["result"]
        key = next((k for k in ("candidates", "designs")
                    if isinstance(result.get(k), list)), None)
        if key is None:
            return None
        rows = result[key]
        return {"key": key, "total": len(rows),
                "rows": copy.deepcopy(rows[p_offset:p_offset + p_limit])}

    def backfill_store_chain_sequences(self, p_job_id, p_status, p_key, p_total, p_chains):
        self.writes.append(p_chains)
        job = self.jobs[p_job_id]
        rows = job["result"][p_key]
        if job["status"] != p_status or len(rows) != p_total:
            return False
        for i, chains in p_chains.items():
            rows[int(i)] = {**rows[int(i)], "chain_sequences": chains}
        return True


def _run(row, **kw):
    client = _Client(jobs=[row])
    return bf.backfill_job(row, client=client, **kw), client


def test_dry_run_reads_apply_writes_and_a_rerun_is_a_no_op(monkeypatch):
    pdb = _pdb({"H": "MKWC", "T": "KMKG"})
    fetched = []

    def fetch(*, user_id, job_id, filename):
        fetched.append((user_id, job_id, filename))
        return None if filename.endswith("missing.pdb") else pdb

    monkeypatch.setattr(bf, "download_output", fetch)
    row = _job([{"pdb_key": "designs/a.pdb"}, {"pdb_key": "designs/missing.pdb"},
                {"pdb_key": "designs/b.pdb", "sequence": "MKWC"}])
    client = _Client(jobs=[row])

    assert bf.backfill_job(row, client=client, apply=False) == 1
    assert client.writes == []
    assert ("u-1", "j-1", "designs/a.pdb") in fetched

    assert bf.backfill_job(row, client=client, apply=True) == 1
    assert client.writes == [{"0": [["H", "MKWC"], ["T", "KMKG"]]}]
    a, missing, b = client.jobs["j-1"]["result"]["candidates"]
    assert a["chain_sequences"] == [["H", "MKWC"], ["T", "KMKG"]]
    assert "chain_sequences" not in missing and "chain_sequences" not in b

    client.writes.clear()
    assert bf.backfill_job(row, client=client, apply=True) == 0
    assert client.writes == []


def test_a_large_job_is_written_in_pieces_each_to_its_own_rows(monkeypatch):
    seqs = ["MKWC", "KMKG", "AGWM", "WWKA"]
    monkeypatch.setattr(bf, "download_output", lambda *, user_id, job_id, filename:
                        _pdb({"H": seqs[int(filename[8:-4]) % 4]}))
    row = _job([{"pdb_key": f"designs/{i}.pdb"} for i in range(250)])
    n, client = _run(row, apply=True, piece=100)
    assert n == 250 and [len(w) for w in client.writes] == [100, 100, 50]
    rows = client.jobs["j-1"]["result"]["candidates"]
    assert [r["chain_sequences"] for r in rows] == [
        [["H", seqs[i % 4]]] for i in range(250)]


def test_the_budget_holds_across_pieces(monkeypatch):
    from shared import exports

    monkeypatch.setattr(exports, "_MAX_STORED_CHAIN_CHARS", 10)
    monkeypatch.setattr(bf, "download_output", lambda **_kw: _pdb({"H": "MKWC"}))
    n, client = _run(_job([{"pdb_key": f"designs/{i}.pdb"} for i in range(5)]),
                     apply=True, piece=1)  # 1 + 4 = 5 characters a row
    assert n == 2 and client.writes == [{"0": [["H", "MKWC"]]}, {"1": [["H", "MKWC"]]}]


def test_a_lost_status_race_reports_nothing_filled(monkeypatch):
    monkeypatch.setattr(bf, "download_output",
                        lambda **_kw: _pdb({"A": "MKWAG"}))
    row = _job([{"pdb_key": "designs/a.pdb"}])
    client = _Client(jobs=[{**row, "status": "failed"}])
    failed = set()
    assert bf.backfill_job(row, client=client, apply=True, failed=failed) == 0
    assert failed == {"j-1"}


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
    assert _run(row, apply=False, pause=0.5, retries=1, failed=failed)[0] == 1
    assert failed == {"j-1"}
    # a.pdb: one pause; busy.pdb: one 2s backoff, then one pause
    assert sorted(slept) == [0.5, 0.5, 2.0]


def test_a_timed_out_write_is_retried_then_listed(monkeypatch):
    from postgrest.exceptions import APIError

    slept = _no_sleep(monkeypatch)
    monkeypatch.setattr(bf, "download_output", lambda **_kw: _pdb({"A": "MKWAG"}))
    row = _job([{"pdb_key": "designs/a.pdb"}])
    client = _Client(jobs=[row])
    real, calls = client.backfill_store_chain_sequences, []

    def flaky(**kw):
        calls.append(1)
        if len(calls) < 2:
            raise APIError({"code": "57014", "message": "statement timeout"})
        return real(**kw)

    client.backfill_store_chain_sequences = flaky
    failed = set()
    assert bf.backfill_job(row, client=client, apply=True, retries=3, failed=failed) == 1
    assert failed == set() and slept == [0.0, 2.0]  # download pause, then backoff

    client.jobs["j-1"] = copy.deepcopy(row)
    client.backfill_store_chain_sequences = lambda **_kw: (_ for _ in ()).throw(
        APIError({"code": "57014"}))
    assert bf.backfill_job(row, client=client, apply=True, retries=2, failed=failed) == 0
    assert failed == {"j-1"}


def _rows(ids):
    return _Resp([{"id": i} for i in ids])


def test_page_query_timeout_exits_with_the_resume_cursor(monkeypatch, capsys):
    from postgrest.exceptions import APIError

    slept = _no_sleep(monkeypatch)

    def respond(columns, filters):
        if columns == "id":
            if filters.get("gt") == ("id", "j-1"):
                raise APIError({"code": "57014", "message": "statement timeout"})
            return _rows(["j-1"])
        return _Resp([_job([])])

    monkeypatch.setattr(bf, "get_service_client",
                        lambda: _Client(respond, jobs=[_job([])]))
    assert bf.main(["--apply", "--retries", "2", "--pause", "0"]) == 2
    err = capsys.readouterr().err
    assert "resume with --after j-1" in err and "Traceback" not in err
    assert slept.count(2.0) == 1 and slept.count(4.0) == 1


def test_a_piece_read_timeout_exits_with_the_resume_cursor(monkeypatch, capsys):
    from postgrest.exceptions import APIError

    _no_sleep(monkeypatch)
    client = _Client(lambda cols, f: _Resp([_job([])]), jobs=[_job([])])
    client.backfill_candidate_page = lambda **_kw: (_ for _ in ()).throw(
        APIError({"code": "57014", "message": "statement timeout"}))
    monkeypatch.setattr(bf, "get_service_client", lambda: client)
    assert bf.main(["--ids", "j-1", "--retries", "1", "--pause", "0"]) == 2
    assert "resume with --ids j-1" in capsys.readouterr().err


def test_ids_reads_only_those_rows_and_pages_nothing(monkeypatch, capsys):
    _no_sleep(monkeypatch)
    client = _Client(lambda cols, f: _Resp([_job([], f["eq"][1])]),
                     jobs=[_job([], "a"), _job([], "b")])
    monkeypatch.setattr(bf, "get_service_client", lambda: client)
    assert bf.main(["--ids", "a, b", "--pause", "0"]) == 0
    assert [(c, f["eq"]) for c, f in client.queries] == [
        ("id,user_id,tool,status", ("id", "a")),
        ("id,user_id,tool,status", ("id", "b"))]
    assert "done: 2 job(s)" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--retries", "-1"], ["--ids", "a", "--after", "b"],
                                  ["--piece", "0"]])
def test_args_that_would_silently_do_nothing_are_refused(argv):
    with pytest.raises(SystemExit) as exc:
        bf.main(argv)
    assert exc.value.code == 2
