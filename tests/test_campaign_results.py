"""Campaign results fan-in: aggregator, shared exporters, column map, and the
campaign-wide shortlist ref parser. Backs the "everything is a campaign" rework.
"""

import base64
import io
import zipfile

import pytest

from shared import compute_campaigns as cc
from shared import exports, result_columns


# ---------------------------------------------------------------------------
# result_columns
# ---------------------------------------------------------------------------

def test_columns_and_primary_metric():
    assert result_columns.columns_for("boltzgen") == ["ipTM", "pLDDT", "refolding_rmsd"]
    # rfantibody ranks on interface pAE, which is lower-is-better.
    assert result_columns.primary_metric_for("rfantibody") == ("ipAE", "asc")
    assert result_columns.primary_metric_for("rfdiffusion") == ("ipTM", "desc")
    assert result_columns.primary_metric_for("unknown-tool") == (None, "desc")
    assert result_columns.columns_for("unknown-tool") == []


def test_candidate_metric_reads_scores_then_root():
    assert result_columns.candidate_metric({"scores": {"ipTM": 0.8}}, "ipTM") == 0.8
    assert result_columns.candidate_metric({"ipTM": 0.7}, "ipTM") == 0.7
    assert result_columns.candidate_metric({"scores": {}}, "ipTM") is None
    assert result_columns.candidate_metric({"scores": {"ipTM": "NA"}}, "ipTM") is None
    assert result_columns.candidate_metric({"ipTM": 0.5}, None) is None


# ---------------------------------------------------------------------------
# exports
# ---------------------------------------------------------------------------

def test_csv_unions_all_score_keys():
    csv = exports.candidates_to_csv([
        {"rank": 1, "pdb_key": "a.pdb", "scores": {"ipTM": 0.8}},
        {"rank": 2, "pdb_key": "b.pdb", "scores": {"pLDDT": 90}},
    ])
    lines = csv.splitlines()
    assert lines[0] == "rank,pdb_key,ipTM,pLDDT"
    assert lines[1].startswith("1,a.pdb,0.8")


def test_fasta_body_and_empty():
    body = exports.candidates_to_fasta([{"sequence": "MKTAY", "pdb_key": "a", "rank": 1}])
    assert body.startswith(">rank1_a")
    assert "MKTAY" in body
    # No sequences anywhere -> empty string (caller supplies the message).
    assert exports.candidates_to_fasta([{"scores": {"ipTM": 0.9}}]) == ""


def test_zip_namespaces_by_subjob_and_blocks_traversal():
    a = base64.b64encode(b"ATOM A").decode()
    b = base64.b64encode(b"ATOM B").decode()
    cands = [
        {"pdb_key": "../../etc/passwd", "pdb_content_b64": a, "_source_chunk": 0, "_source_job_id": "j1"},
        {"pdb_key": "designs/d.pdb", "pdb_content_b64": b, "_source_chunk": 1, "_source_job_id": "j2"},
    ]
    data = exports.candidates_to_zip(cands, lambda j, f: None, namespace=True)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "chunk000/etc/passwd" in names      # ".." components stripped
    assert "chunk001/designs/d.pdb" in names    # legit subdir preserved
    assert not any(".." in n for n in names)


def test_zip_fetches_from_storage_when_no_inline():
    calls = []

    def _fetch(job_id, filename):
        calls.append((job_id, filename))
        return b"FETCHED"

    cands = [{"pdb_key": "designs/x.pdb", "_source_job_id": "j9"}]
    data = exports.candidates_to_zip(cands, _fetch)
    assert calls == [("j9", "designs/x.pdb")]
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.read("designs/x.pdb") == b"FETCHED"


# ---------------------------------------------------------------------------
# aggregate_campaign_candidates
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._filters = []

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters.append((col, str(val)))
        return self

    def execute(self):
        matched = [
            r for r in self._rows
            if all(str(r.get(c)) == v for c, v in self._filters)
        ]
        return _FakeResult(matched)


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeQuery(self._rows if name == "tool_jobs" else [])


class _Campaign:
    tool = "rfdiffusion"


def _cand(idx, ipTM, fs=None):
    scores = {"ipTM": ipTM}
    if fs is not None:
        scores["filter_status"] = fs
    return {"pdb_key": f"d{idx}.pdb", "scores": scores}


def _patch(monkeypatch, rows, campaign=_Campaign()):
    monkeypatch.setattr(cc, "get_campaign", lambda cid, user_id=None: campaign)
    monkeypatch.setattr(cc, "get_service_client", lambda: _FakeClient(rows))


def test_aggregate_merges_sorts_dedupes(monkeypatch):
    rows = [
        # chunk 0, attempt 1 — superseded by attempt 2 below (must be dropped).
        {"id": "j0a", "campaign_id": "C", "status": "succeeded", "chunk_index": 0,
         "attempt": 1, "result": {"candidates": [_cand(0, 0.9, "pass")]}},
        # chunk 0, attempt 2 — kept.
        {"id": "j0b", "campaign_id": "C", "status": "succeeded", "chunk_index": 0,
         "attempt": 2, "result": {"candidates": [_cand(0, 0.5, "fail"), _cand(1, 0.95, "pass")]}},
        # chunk 1.
        {"id": "j1", "campaign_id": "C", "status": "succeeded", "chunk_index": 1,
         "attempt": 1, "result": {"candidates": [_cand(0, 0.7, "pass")]}},
    ]
    _patch(monkeypatch, rows)
    out = cc.aggregate_campaign_candidates("C", user_id="u", limit=10)

    assert out["tool"] == "rfdiffusion"
    assert out["columns"] == ["ipTM", "pLDDT", "i_pAE", "filter_status"]
    # attempt-1 of chunk 0 dropped -> 2 (j0b) + 1 (j1) = 3
    assert out["total"] == 3
    assert out["capped"] is False
    cands = out["candidates"]
    assert all("_source_job_id" in c for c in cands)
    # Passing first, then ipTM desc: pass 0.95, pass 0.70, fail 0.50.
    assert [round(c["scores"]["ipTM"], 2) for c in cands] == [0.95, 0.70, 0.50]
    assert cands[0]["_source_job_id"] == "j0b"
    assert cands[0]["_source_index"] == 1


def test_aggregate_caps_without_dropping_total(monkeypatch):
    rows = [
        {"id": "j", "campaign_id": "C", "status": "succeeded", "chunk_index": 0,
         "attempt": 1, "result": {"candidates": [_cand(i, 0.5 + i * 0.01, "pass") for i in range(10)]}},
    ]
    _patch(monkeypatch, rows)
    out = cc.aggregate_campaign_candidates("C", user_id="u", limit=3)
    assert out["total"] == 10
    assert len(out["candidates"]) == 3
    assert out["capped"] is True
    # Top-3 by ipTM desc.
    assert [round(c["scores"]["ipTM"], 2) for c in out["candidates"]] == [0.59, 0.58, 0.57]


def test_aggregate_ownership_gate_returns_empty(monkeypatch):
    # get_campaign returns None for a non-owner -> IDOR-safe empty envelope.
    monkeypatch.setattr(cc, "get_campaign", lambda cid, user_id=None: None)
    out = cc.aggregate_campaign_candidates("C", user_id="intruder", limit=10)
    assert out["candidates"] == []
    assert out["total"] == 0
    assert out["tool"] is None


# ---------------------------------------------------------------------------
# campaign-wide shortlist ref parser
# ---------------------------------------------------------------------------

def test_spawn_refold_boltz2_falls_back_to_campaign_antigen(monkeypatch):
    """A campaign sub-job carries no _pdb_storage_path (the antigen lives on the
    campaign row), so the campaign Boltz-2 refold must fall back to the passed
    antigen_storage_path — otherwise the refold silently spawns nothing."""
    from types import SimpleNamespace
    import blueprints.jobs as J

    src = SimpleNamespace(
        id="subjob-1", tool="rfdiffusion",
        inputs={"target_chain": "A", "hotspot_residues": [10, 12]},  # no _pdb_storage_path
    )
    seq = SimpleNamespace(rank=1, pdb_key="d1.pdb", sequence="MKTAY",
                          fasta_header="rank1_d1.pdb")
    captured = {}
    monkeypatch.setattr(J, "presigned_input_url",
                        lambda path, expires_seconds=None: captured.setdefault("antigen", path) or "https://signed")
    monkeypatch.setattr(J, "create_job", lambda **k: SimpleNamespace(id="new-job", job_token="tok"))
    monkeypatch.setattr(J, "url_for", lambda *a, **k: "http://hook")

    class _MC:
        def submit(self, *a, **k):
            captured["submitted"] = True

    monkeypatch.setattr(J, "current_app", SimpleNamespace(modal_client=_MC()))

    class _Adapter:
        slug = "boltz2"

        def build_payload(self, inputs, url):
            return dict(inputs)

    jid = J._spawn_refold_job(
        SimpleNamespace(user_id="u"), _Adapter(), "boltz2", seq, src, "label",
        antigen_storage_path="lab-campaigns/x/target.pdb",
    )
    assert jid == "new-job"
    assert captured.get("antigen") == "lab-campaigns/x/target.pdb"
    assert captured.get("submitted") is True


def test_spawn_refold_boltz2_still_noops_without_any_antigen(monkeypatch):
    """No sub-job path and no campaign fallback -> None (unchanged safety)."""
    from types import SimpleNamespace
    import blueprints.jobs as J
    src = SimpleNamespace(id="s", tool="rfdiffusion", inputs={"target_chain": "A"})
    seq = SimpleNamespace(rank=1, pdb_key="d.pdb", sequence="MK", fasta_header="h")
    assert J._spawn_refold_job(
        SimpleNamespace(user_id="u"), SimpleNamespace(slug="boltz2"), "boltz2",
        seq, src, "label", antigen_storage_path=None,
    ) is None


def test_parse_candidate_refs_sanitizes():
    from blueprints.lab_projects import _parse_candidate_refs
    good = _parse_candidate_refs('[{"job_id":"j1","index":0},{"job_id":"j2","index":3}]')
    assert good == [{"job_id": "j1", "index": 0}, {"job_id": "j2", "index": 3}]
    # Missing job_id, missing/negative/non-int index, and blanks all dropped.
    dirty = _parse_candidate_refs(
        '[{"job_id":"","index":1},{"index":2},{"job_id":"j3","index":"x"},'
        '{"job_id":"j4","index":-1},{"job_id":"j5","index":5}]'
    )
    assert dirty == [{"job_id": "j5", "index": 5}]
    assert _parse_candidate_refs("{}") == []
    assert _parse_candidate_refs("not json") == []
