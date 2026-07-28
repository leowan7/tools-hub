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


def test_normalize_candidate_lifts_root_metric_into_scores():
    """iggm persists n_epitope_contacts at the record root while its declared
    primary metric is scores.epitope_contacts, so every row resolved to None
    and the merged campaign table ranked nothing. The per-tool template did
    this reshape inline, which is why only the campaign path lost it."""
    raw = {"pdb_key": "d0.pdb", "n_epitope_contacts": 7}
    out = result_columns.normalize_candidate(raw, "iggm")
    assert out["scores"]["epitope_contacts"] == 7
    assert result_columns.candidate_metric(out, "epitope_contacts") == 7
    # Non-destructive: the source record is untouched.
    assert "scores" not in raw


def test_normalize_candidate_is_passthrough_for_other_tools():
    raw = {"scores": {"ipTM": 0.8}}
    assert result_columns.normalize_candidate(raw, "rfdiffusion") is raw
    assert result_columns.normalize_candidate(raw, "unknown-tool") is raw
    assert result_columns.normalize_candidate("not-a-dict", "iggm") == "not-a-dict"


def test_normalize_candidate_does_not_override_existing_scores():
    """A pipeline that starts emitting the metric properly must win over its
    own legacy root key."""
    raw = {"n_epitope_contacts": 1, "scores": {"epitope_contacts": 9}}
    assert result_columns.normalize_candidate(raw, "iggm")["scores"][
        "epitope_contacts"
    ] == 9


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
    # No provenance on these rows, so only the always-present columns lead.
    assert lines[0] == "rank,pdb_key,source_rank,ipTM,pLDDT"
    assert lines[1].startswith("1,a.pdb,1,0.8")


def test_csv_rank_is_global_and_tool_rank_is_demoted():
    """Across a merge every tool contributes its own rank 1. The export rank
    must be the row index so it stays monotonic and matches the screen."""
    csv = exports.candidates_to_csv([
        {"rank": 1, "pdb_key": "design_1.pdb", "_source_tool": "bindcraft",
         "_source_job_id": "job-aaaaaaaa1", "_source_chunk": 0,
         "_source_campaign_id": "camp-a", "scores": {"ipTM": 0.9}},
        {"rank": 1, "pdb_key": "design_1.pdb", "_source_tool": "boltzgen",
         "_source_job_id": "job-bbbbbbbb2", "_source_chunk": 3,
         "_source_campaign_id": "camp-b", "scores": {"ipTM": 0.7}},
    ])
    lines = csv.splitlines()
    assert lines[0] == (
        "rank,tool,campaign_id,source_job,source_chunk,pdb_key,source_rank,ipTM"
    )
    assert lines[1] == "1,bindcraft,camp-a,job-aaaaaaaa1,0,design_1.pdb,1,0.9"
    # Same pdb_key and same source_rank, disambiguated by rank + provenance.
    assert lines[2] == "2,boltzgen,camp-b,job-bbbbbbbb2,3,design_1.pdb,1,0.7"


def test_csv_omits_provenance_columns_nothing_carries():
    """A single-job export has no source job, so it must not grow blank
    columns; the target-level export gets them because its rows carry them."""
    csv = exports.candidates_to_csv([{"pdb_key": "a.pdb", "scores": {}}])
    assert csv.splitlines()[0] == "rank,pdb_key,source_rank"


def test_fasta_body_and_empty():
    body = exports.candidates_to_fasta([{"sequence": "MKTAY", "pdb_key": "a", "rank": 1}])
    assert body.startswith(">rank1_a")
    assert "MKTAY" in body
    # No sequences anywhere -> empty string (caller supplies the message).
    assert exports.candidates_to_fasta([{"scores": {"ipTM": 0.9}}]) == ""


def test_fasta_ids_are_unique_and_carry_no_slash():
    """A '/' in a FASTA id terminates parsing in several downstream tools, and
    'designs/design_1.pdb' is the pdb_key almost every tool emits."""
    body = exports.candidates_to_fasta([
        {"sequence": "MKTAY", "pdb_key": "designs/design_1.pdb", "rank": 1,
         "_source_tool": "bindcraft", "_source_job_id": "job-aaaaaaaa1"},
        {"sequence": "GGSGG", "pdb_key": "designs/design_1.pdb", "rank": 1,
         "_source_tool": "boltzgen", "_source_job_id": "job-bbbbbbbb2"},
    ])
    ids = [ln for ln in body.splitlines() if ln.startswith(">")]
    assert ids == [
        ">rank1_bindcraft_job-aaaa_design_1.pdb",
        ">rank2_boltzgen_job-bbbb_design_1.pdb",
    ]
    assert len(set(ids)) == 2
    assert not any("/" in i for i in ids)


def test_fasta_skips_rank_for_sequenceless_rows_but_keeps_global_order():
    """A candidate with no sequence is skipped, and the ranks of the rows that
    do emit must still match their CSV rank (the row index, not a counter)."""
    body = exports.candidates_to_fasta([
        {"pdb_key": "a.pdb"},                      # no sequence -> skipped
        {"pdb_key": "b.pdb", "sequence": "MKTAY"},
    ])
    assert [ln for ln in body.splitlines() if ln.startswith(">")] == [">rank2_b.pdb"]


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


# PostgREST clamps EVERY select to the project's max_rows, which
# supabase/config.toml sets to 1000, while a campaign may hold up to
# MAX_SUBJOBS_PER_CAMPAIGN (50000) children. The fake enforces the same clamp
# so an unpaged read truncates here exactly as it does in production — that is
# what makes the pagination test below meaningful rather than decorative.
_FAKE_MAX_ROWS = 1000


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._filters = []
        self._order_col = None
        self._range = None

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._filters.append((col, str(val)))
        return self

    def order(self, col, **k):
        self._order_col = col
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        matched = [
            r for r in self._rows
            if all(str(r.get(c)) == v for c, v in self._filters)
        ]
        if self._order_col:
            matched.sort(key=lambda r: str(r.get(self._order_col)))
        if self._range is not None:
            start, end = self._range
            matched = matched[start:end + 1]
        return _FakeResult(matched[:_FAKE_MAX_ROWS])


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


def test_iter_succeeded_children_pages_and_filters():
    """The shared fan-in primitive, used by BOTH the aggregator and the
    "Passed filters" rollup that runs on every 5s status poll."""
    n = _FAKE_MAX_ROWS + 137
    rows = [
        {"id": f"j{i:05d}", "campaign_id": "C", "status": "succeeded",
         "chunk_index": i, "attempt": 1, "result": {}}
        for i in range(n)
    ]
    # Noise that must be filtered out: another campaign, and a non-terminal
    # child of this one.
    rows.append({"id": "other", "campaign_id": "D", "status": "succeeded",
                 "chunk_index": 0, "attempt": 1, "result": {}})
    rows.append({"id": "pending", "campaign_id": "C", "status": "running",
                 "chunk_index": 999, "attempt": 1, "result": {}})

    got = list(cc.iter_succeeded_children("C", _FakeClient(rows)))

    assert len(got) == n
    assert {r["id"] for r in got} == {f"j{i:05d}" for i in range(n)}
    # No duplicates across page boundaries.
    assert len({r["id"] for r in got}) == len(got)


def test_iter_succeeded_children_narrows_columns():
    """The passed-filters rollup only needs `result`, so it must be able to
    ask for that alone rather than dragging every column across the wire."""
    client = _FakeClient([
        {"id": "j1", "campaign_id": "C", "status": "succeeded",
         "chunk_index": 0, "attempt": 1, "result": {"designs": []}},
    ])
    got = list(cc.iter_succeeded_children("C", client, columns="result"))
    assert len(got) == 1


def test_aggregate_pages_past_the_postgrest_max_rows_clamp(monkeypatch):
    """The fan-in must not stop at max_rows.

    An unpaged .select() is clamped by PostgREST at 1000 rows while a campaign
    may hold 50000 children, so the merged table, both exports, and the
    "global top-N" that the Boltz-2 validation refold spends real GPU on were
    all computed from at most the first 1000 sub-jobs with nothing indicating
    rows were missing. .limit() does not help — it is clamped the same way.
    """
    n = _FAKE_MAX_ROWS + 200
    rows = [
        {"id": f"j{i:05d}", "campaign_id": "C", "status": "succeeded",
         "chunk_index": i, "attempt": 1,
         "result": {"candidates": [_cand(i, i / 10000.0, "pass")]}}
        for i in range(n)
    ]
    _patch(monkeypatch, rows)
    out = cc.aggregate_campaign_candidates("C", user_id="u", limit=None)

    assert out["total"] == n, "truncated at the clamp instead of paging"
    assert len(out["candidates"]) == n
    # Highest ipTM first, so the top row comes from the LAST page — proof the
    # tail was actually fetched and not just counted.
    assert out["candidates"][0]["_source_job_id"] == f"j{n - 1:05d}"


def test_aggregate_ranks_iggm_by_its_root_level_metric(monkeypatch):
    """End-to-end guard for the reshape: the merged table must be ordered by
    epitope_contacts even though the pipeline writes n_epitope_contacts at the
    record root."""
    class _Iggm:
        tool = "iggm"

    rows = [
        {"id": "j0", "campaign_id": "C", "status": "succeeded", "chunk_index": 0,
         "attempt": 1, "result": {"designs": [
             {"pdb_key": "a.pdb", "n_epitope_contacts": 3},
             {"pdb_key": "b.pdb", "n_epitope_contacts": 11},
         ]}},
        {"id": "j1", "campaign_id": "C", "status": "succeeded", "chunk_index": 1,
         "attempt": 1, "result": {"designs": [
             {"pdb_key": "c.pdb", "n_epitope_contacts": 7},
         ]}},
    ]
    _patch(monkeypatch, rows, campaign=_Iggm())
    out = cc.aggregate_campaign_candidates("C", user_id="u", limit=10)

    assert out["total"] == 3
    # Descending by contacts, not pipeline order.
    assert [c["pdb_key"] for c in out["candidates"]] == ["b.pdb", "c.pdb", "a.pdb"]
    assert [c["scores"]["epitope_contacts"] for c in out["candidates"]] == [11, 7, 3]


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


def test_aggregate_uncapped_returns_full_ranked_set(monkeypatch):
    """limit=None returns every candidate (used by CSV / FASTA exports), well
    past the old top-300 cap, and reports capped=False."""
    rows = [
        {"id": "j", "campaign_id": "C", "status": "succeeded", "chunk_index": 0,
         "attempt": 1,
         "result": {"candidates": [_cand(i, 0.5 + i * 0.0001, "pass")
                                   for i in range(350)]}},
    ]
    _patch(monkeypatch, rows)
    out = cc.aggregate_campaign_candidates("C", user_id="u", limit=None)
    assert out["total"] == 350
    assert len(out["candidates"]) == 350
    assert out["capped"] is False
    # Same pool, capped at 300, still reports the true total and capped=True.
    capped = cc.aggregate_campaign_candidates("C", user_id="u", limit=300)
    assert capped["total"] == 350
    assert len(capped["candidates"]) == 300
    assert capped["capped"] is True


# ---------------------------------------------------------------------------
# _campaign_export: CSV / FASTA uncapped, ZIP capped at 300
# ---------------------------------------------------------------------------

def test_campaign_export_csv_fasta_uncapped_zip_capped(monkeypatch):
    """The export route asks the aggregator for the full set (limit=None) for
    CSV / FASTA and for the top-N only (limit=300) for the ZIP, so the text
    exports carry every candidate while the ZIP stays memory-bounded. The ZIP
    download name makes its 'top N of M' truncation explicit."""
    from types import SimpleNamespace
    import blueprints.campaigns as bp

    pool = [
        {"rank": i + 1, "pdb_key": f"d{i}.pdb", "sequence": "MKTAY",
         "scores": {"ipTM": 0.5},
         "pdb_content_b64": base64.b64encode(b"ATOM X").decode(),
         "_source_chunk": 0, "_source_job_id": "j"}
        for i in range(350)
    ]

    seen_limits = []

    def _fake_agg(campaign_id, *, user_id=None, limit=300):
        seen_limits.append(limit)
        sliced = pool if limit is None else pool[:limit]
        return {
            "candidates": sliced,
            "total": len(pool),
            "columns": ["ipTM"],
            "capped": limit is not None and len(pool) > limit,
            "tool": "rfdiffusion",
        }

    monkeypatch.setattr(bp, "load_user_context",
                        lambda: SimpleNamespace(user_id="u"))
    monkeypatch.setattr(
        "shared.compute_campaigns.aggregate_campaign_candidates", _fake_agg)

    # CSV — full set (header + 350 rows), aggregator asked with limit=None.
    csv_resp = bp._campaign_export("camp1234", "csv")
    csv_lines = csv_resp.get_data(as_text=True).strip().splitlines()
    assert len(csv_lines) == 351
    assert seen_limits[-1] is None

    # FASTA — one record per candidate, aggregator asked with limit=None.
    fasta_resp = bp._campaign_export("camp1234", "fasta")
    assert fasta_resp.get_data(as_text=True).count(">") == 350
    assert seen_limits[-1] is None

    # ZIP — capped at the ZIP-specific limit (300), and the filename says so.
    zip_resp = bp._campaign_export("camp1234", "zip")
    assert seen_limits[-1] == bp._CAMPAIGN_ZIP_EXPORT_LIMIT == 300
    names = zipfile.ZipFile(io.BytesIO(zip_resp.get_data())).namelist()
    assert len(names) == 300
    assert "top300of350" in zip_resp.headers["Content-Disposition"]


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
        id="subjob-1", tool="rfdiffusion", target_id=None,
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
    src = SimpleNamespace(id="s", tool="rfdiffusion", target_id=None,
                          inputs={"target_chain": "A"})
    seq = SimpleNamespace(rank=1, pdb_key="d.pdb", sequence="MK", fasta_header="h")
    assert J._spawn_refold_job(
        SimpleNamespace(user_id="u"), SimpleNamespace(slug="boltz2"), "boltz2",
        seq, src, "label", antigen_storage_path=None,
    ) is None


def test_spawn_refold_inherits_the_source_jobs_target(monkeypatch):
    """A yardstick refold lands with campaign_id NULL, so target_id is its only
    link back to the target. Phase 4 re-ranks every tool's designs on one
    predictor by reading exactly these rows; unstamped, they are invisible to
    the fan-in and the comparison silently covers nothing."""
    from types import SimpleNamespace
    import blueprints.jobs as J

    src = SimpleNamespace(id="subjob-1", tool="rfdiffusion", target_id="t-42",
                          inputs={"target_chain": "A"})
    seq = SimpleNamespace(rank=1, pdb_key="d1.pdb", sequence="MKTAY",
                          fasta_header="rank1_d1.pdb")
    captured = {}
    monkeypatch.setattr(J, "create_job", lambda **k: (
        captured.update(k) or SimpleNamespace(id="new-job", job_token="tok")
    ))
    monkeypatch.setattr(J, "url_for", lambda *a, **k: "http://hook")
    monkeypatch.setattr(
        J, "current_app",
        SimpleNamespace(modal_client=SimpleNamespace(submit=lambda *a, **k: None)),
    )

    class _Adapter:
        slug = "esmfold"

        def build_payload(self, inputs, url):
            return dict(inputs)

    jid = J._spawn_refold_job(
        SimpleNamespace(user_id="u"), _Adapter(), "esmfold", seq, src, "label",
    )
    assert jid == "new-job"
    assert captured["target_id"] == "t-42"


def test_spawn_refold_of_an_untargeted_run_carries_no_target(monkeypatch):
    """NULL is the correct answer when there is no target, not a fallback to
    some other job's."""
    from types import SimpleNamespace
    import blueprints.jobs as J

    src = SimpleNamespace(id="j-1", tool="rfdiffusion", target_id=None,
                          inputs={"target_chain": "A"})
    seq = SimpleNamespace(rank=1, pdb_key="d1.pdb", sequence="MKTAY",
                          fasta_header="rank1_d1.pdb")
    captured = {}
    monkeypatch.setattr(J, "create_job", lambda **k: (
        captured.update(k) or SimpleNamespace(id="new-job", job_token="tok")
    ))
    monkeypatch.setattr(J, "url_for", lambda *a, **k: "http://hook")
    monkeypatch.setattr(
        J, "current_app",
        SimpleNamespace(modal_client=SimpleNamespace(submit=lambda *a, **k: None)),
    )

    J._spawn_refold_job(
        SimpleNamespace(user_id="u"), SimpleNamespace(
            slug="esmfold", build_payload=lambda i, u: dict(i),
        ), "esmfold", seq, src, "label",
    )
    assert captured["target_id"] is None


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
