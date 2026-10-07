"""esmfold2_design streams one heartbeat per design, as it is shaped.

Before this, the tool sent no heartbeat at all: a run that was stopped,
cancelled or killed mid-flight left ``inputs._partial_candidates`` empty, so
``shared/job_recovery.py::reconstruct`` had nothing with scores to rebuild from
and the live job page stayed blank until the whole batch finished.

Its own file rather than appended to test_esmfold2_design_critic_mapping.py:
that module's subject is which critic row wins a bucket, and the save-as-you-go
change is a different property on the same function.

Runs fully offline. ``requests.post`` is patched on the pipeline module, which
is the attribute ``send_heartbeat`` resolves through, and the upload pair is
patched by name for the same reason. PDB_OUTPUT_DIR is redirected into tmp_path
so ``_save_complex_pdb``'s real write (and therefore the real ``stored``
decision) runs without touching /tmp/results.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap

import pytest

from tools.esmfold2_design import run_pipeline
from tools.esmfold2_design.run_pipeline import CRITIC_REAL_IPTM, _shape_designs

_WEBHOOK = "https://hub.example.com/webhooks/modal"
_ENDPOINT = "https://hub.example.com/api/jobs/abc/upload-urls"


class _Complex:
    """Minimal stand-in for upstream's ProteinComplex."""

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def to_pdb_string(self) -> str:
        return f"ATOM  {self.tag}\n"


def _rows(n: int) -> list[dict]:
    """``n`` designs, one CRITIC_REAL_IPTM row each, each with a complex.

    Distinct designed_sequence per design: ``_shape_designs`` groups on that
    field, so two rows sharing it would collapse to ONE design and a
    per-design assertion below would pass over the wrong number of beats.
    """
    return [
        {
            "critic_name": CRITIC_REAL_IPTM,
            "designed_sequence": f"TARGET|QVQLVQSGG{'A' * i}",
            "iptm": 0.80 + i / 100,
            "distogram_iptm_proxy": 0.60,
            "cdr_distogram_iptm_proxy": 0.70,
            "final_loss": 0.30,
            "complex": _Complex(f"c{i}"),
        }
        for i in range(n)
    ]


@pytest.fixture
def beats(monkeypatch, tmp_path):
    """Capture every heartbeat body; keep uploads and file IO off the network.

    Returns the list the POSTs land in. ``upload_pdb`` succeeds here; the
    failure case patches it again on top.
    """
    posted: list[dict] = []

    class _Resp:
        status_code = 200

    def _post(url, json=None, timeout=None):  # noqa: A002 — requests' kwarg
        posted.append({"url": url, "body": json})
        return _Resp()

    monkeypatch.setattr(run_pipeline, "PDB_OUTPUT_DIR", tmp_path / "results")
    monkeypatch.setattr(run_pipeline.requests, "post", _post)
    monkeypatch.setattr(
        run_pipeline,
        "request_upload_urls",
        lambda endpoint, token, names: {n: f"https://put.example/{n}" for n in names},
    )
    monkeypatch.setattr(run_pipeline, "upload_pdb", lambda url, data: None)
    return posted


def _shape(**over):
    kwargs = {
        "upload_endpoint": _ENDPOINT,
        "job_token": "tok",
        "webhook_url": _WEBHOOK,
        "job_id": "job-1",
    }
    kwargs.update(over)
    rows = kwargs.pop("rows", None)
    if rows is None:
        rows = _rows(3)
    return _shape_designs(rows, True, **kwargs)


def _candidates(posted):
    return [p["body"]["new_candidate"] for p in posted]


def test_one_heartbeat_per_design_carrying_its_own_scores(beats):
    """The core of the change: N designs shaped, N beats sent, no sharing.

    Asserts the per-beat PAIRING, not just the count: a loop that sent N beats
    all describing the last design would satisfy a count assertion. Paired on
    pdb_key rather than name -- see
    test_the_streamed_name_is_the_pre_sort_name for why name is not stable.
    """
    designs = _shape()
    assert len(beats) == 3
    streamed = {(c["pdb_key"], c["iptm"]) for c in _candidates(beats)}
    assert streamed == {(d["pdb_key"], d["iptm"]) for d in designs}


def test_the_streamed_name_is_the_pre_sort_name(beats):
    """KNOWN AND NOT FIXED HERE: the streamed ``name`` is the one assigned
    while the design is shaped, and ``_shape_designs`` renames every design
    after sorting by iPTM (``d["name"] = f"{pdb_prefix}design_{rank}"``). So a
    live row labelled design_0 can be design_2 on the finished page.

    Streaming after the sort is the only way to make them agree, and that
    means no beat until the whole batch is done -- which is the thing this
    change exists to stop. What the beat does keep is the name CONSISTENT WITH
    THE KEY beside it, so the live row's label and the file its "View 3D"
    fetches are the same design. Recovery does not care either way:
    ``shared/job_recovery.py::reconstruct`` joins on
    ``posixpath.basename(part["pdb_key"])`` and rebuilds a candidate of rank,
    pdb_key and scores only -- it reads no name. The manifest's own name
    already disagreed with its pdb_key before this change, for the same
    reason.
    """
    designs = _shape()
    for cand in _candidates(beats):
        assert cand["pdb_key"] == f"{cand['name']}_complex.pdb"
    by_key = {d["pdb_key"]: d["name"] for d in designs}
    renamed = [
        c for c in _candidates(beats) if by_key[c["pdb_key"]] != c["name"]
    ]
    assert renamed, (
        "the post-sort rename no longer moves any name; if _shape_designs "
        "stopped renaming, drop this test and pair on name above"
    )


def test_every_beat_goes_to_the_heartbeat_path(beats):
    _shape()
    assert {p["url"] for p in beats} == {
        "https://hub.example.com/webhooks/heartbeat"
    }


def test_the_candidate_is_flat_because_the_hub_drops_anything_nested(beats):
    """``webhooks/modal.py::_sanitize_candidate`` reads short keys off the TOP
    level of new_candidate and emits a flat dict. A nested ``scores`` would
    arrive empty, so every streamed row would show a blank iPTM column."""
    _shape()
    for cand in _candidates(beats):
        assert "scores" not in cand
        assert isinstance(cand["iptm"], float)
        assert isinstance(cand["rank"], int)


def test_the_candidate_carries_the_fields_the_hub_keeps(beats):
    """Exactly the keys ``_sanitize_candidate`` projects for this tool. An
    allowlist, not a forbid-list: a key it drops (distogram_iptm_proxy,
    final_loss, isoelectric_point) is weight on every beat for nothing."""
    _shape()
    for cand in _candidates(beats):
        assert set(cand) == {"rank", "name", "pdb_key", "iptm", "filter_status"}


def test_the_design_counts_are_omitted(beats):
    """0/0, deliberately. A fan-out child is handed ``n_seeds = 1`` and the
    FULL batch_size (modal_app.py::run_tool), so a per-seed "3 of 6" would be
    stored as the whole job's progress -- and
    ``shared/job_recovery.py::_completion_signal`` reads completed >= total as
    "complete", which lets a stopped run be rebuilt and billed as a success.
    With 0/0 it returns "unknown" and the clean-exit evidence decides."""
    _shape()
    for p in beats:
        assert p["body"]["designs_completed"] == 0
        assert p["body"]["designs_total"] == 0


def test_the_body_holds_no_nan(beats):
    """``requests`` refuses a NaN outright rather than encoding it:
    ``PreparedRequest.prepare_body`` calls ``json.dumps(..., allow_nan=False)``
    and raises ``InvalidJSONError`` (checked against requests 2.33.1 in this
    venv), which ``send_heartbeat``'s ``except Exception`` turns into a warning
    -- so one NaN field loses that design's whole beat, scores included. The
    assertion below uses the same flag requests does. ``_finite`` is what keeps
    iptm float-or-None (run_pipeline.py: ``iptm = _finite(row.get("iptm"))``)
    and this fails if a NaN field is ever added."""
    _shape()
    for p in beats:
        json.dumps(p["body"], allow_nan=False)


def test_rank_offset_shifts_the_streamed_rank_only(beats):
    """The live table keys rows on rank alone (``renderedRanks`` /
    ``live-viewer-row-<rank>`` in templates/job_detail.html), so without the
    offset every seed's rank 0 collapses into one row with a duplicate DOM id.
    The manifest rank stays within-seed because modal_app.py::_aggregate
    re-ranks the concatenation globally."""
    designs = _shape(rank_offset=6)
    assert sorted(c["rank"] for c in _candidates(beats)) == [6, 7, 8]
    assert sorted(d["rank"] for d in designs) == [0, 1, 2]


def test_no_offset_means_no_offset(beats):
    _shape()
    assert sorted(c["rank"] for c in _candidates(beats)) == [0, 1, 2]


def test_a_stored_upload_streams_its_key_and_counts_no_failure(beats):
    failures: list = []
    designs = _shape(upload_failures=failures)
    assert failures == []
    keys = {c["pdb_key"] for c in _candidates(beats)}
    assert keys == {d["pdb_key"] for d in designs}
    assert None not in keys


def test_a_failed_upload_streams_no_key_and_is_counted(beats, monkeypatch):
    """Two properties, one cause.

    The beat must withhold the key: the live page turns a streamed pdb_key
    straight into a "View 3D" button against ``/api/jobs/<id>/pdb/<key>``,
    which 404s for bytes that never landed.

    And the failure must be COUNTED, because nothing else records it -- the
    design still appears in the manifest with its sequence, so neither
    designs_completed nor n_failures moves.
    """
    def _boom(url, data):
        raise RuntimeError("503 from storage")

    monkeypatch.setattr(run_pipeline, "upload_pdb", _boom)
    failures: list = []
    designs = _shape(upload_failures=failures)

    assert len(designs) == 3, "a failed upload must not drop the design"
    assert [c["pdb_key"] for c in _candidates(beats)] == [None, None, None]
    assert len(failures) == 3
    # The manifest key is deliberately unchanged on this path, so the count is
    # the only signal -- which is why it is asserted and not left implicit.
    assert all(d["pdb_key"] for d in designs)


def test_a_design_with_no_complex_is_not_an_upload_failure(beats):
    """``_save_complex_pdb`` returns a None pdb_key with nothing to upload.
    Counting that as an upload failure would report a storage fault on every
    run whose fold diverged."""
    rows = _rows(2)
    rows[0]["complex"] = None
    failures: list = []
    _shape(rows=rows, upload_failures=failures)
    assert failures == []
    assert sorted(c["pdb_key"] is None for c in _candidates(beats)) == [
        False,
        True,
    ]


def test_no_upload_endpoint_is_not_an_upload_failure(beats):
    """Nothing was attempted, so nothing failed. ``_save_complex_pdb`` reports
    ``stored`` False on this path too, which is why the caller tests
    upload_endpoint rather than ``not stored`` alone."""
    failures: list = []
    _shape(upload_endpoint="", upload_failures=failures)
    assert failures == []


def test_no_webhook_url_sends_nothing_and_still_shapes(beats):
    """Local and smoke runs have no hub. ``send_heartbeat`` returns early."""
    designs = _shape(webhook_url="")
    assert beats == []
    assert len(designs) == 3


def test_a_dead_webhook_does_not_fail_the_run(beats, monkeypatch):
    """Fire and forget. The designs are the deliverable; a flaky webhook hop
    must not cost the user an H100 session."""
    def _boom(*a, **kw):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(run_pipeline.requests, "post", _boom)
    assert len(_shape()) == 3


def test_upload_failures_is_optional(beats, monkeypatch):
    """Every pre-existing caller omits it (the tests in
    test_esmfold2_design_critic_mapping.py call ``_shape_designs(rows,
    is_antibody=...)``), so None must not raise."""
    monkeypatch.setattr(
        run_pipeline, "upload_pdb", lambda url, data: (_ for _ in ()).throw(
            RuntimeError("503")
        )
    )
    assert len(_shape()) == 3


# ---------------------------------------------------------------------------
# Call sites. A correct helper nothing wires up is the failure mode these
# cover: every assertion above would still pass with _run sending no job_id,
# no rank_offset and no upload_failures, over a feature that never fires in
# production.
# ---------------------------------------------------------------------------


def _tree(func):
    """``func``'s AST. Parsed, not matched in the text: ``"rank_offset" in
    inspect.getsource(...)`` is satisfied by the word appearing in a comment,
    and every property below is about code a comment can name just as readily.
    UNVERIFIABLE FROM THIS REPO, offered as the reason: the sibling rfdiffusion
    change (leowan7/llm-proteinDesigner#33) met exactly that, a key replaced by
    a TODO comment naming it."""
    return ast.parse(textwrap.dedent(inspect.getsource(func)))


def _call_keywords(func, callee: str) -> set[str]:
    """Keyword names on each ``callee`` call inside ``func``."""
    found: set[str] = set()
    for node in ast.walk(_tree(func)):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == callee:
            found |= {kw.arg for kw in node.keywords if kw.arg}
    return found


def _first_args(func, callee: str) -> set[str]:
    """The first positional argument of each ``callee`` call, unparsed."""
    return {
        ast.unparse(node.args[0])
        for node in ast.walk(_tree(func))
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == callee
        and node.args
    }


def test_run_wires_the_heartbeat_through_to_shape_designs():
    kws = _call_keywords(run_pipeline._run, "_shape_designs")
    assert {"webhook_url", "job_id", "rank_offset", "upload_failures"} <= kws


def test_run_reads_the_webhook_url_and_the_rank_offset():
    """The keywords above would also be satisfied by two hardcoded zeros."""
    assert "'WEBHOOK_URL'" in _first_args(run_pipeline._run, "os.environ.get")
    assert "'rank_offset'" in _first_args(run_pipeline._run, "job_spec.get")


def test_the_summary_reports_the_upload_failure_count():
    """In the result JSON at least. Nothing renders it
    (templates/tools/esmfold2_design_results.html passes neither failure count
    to its template), so the JSON and the log line are the whole audit trail.

    The VALUE is asserted, not merely the key: a hardcoded ``0`` satisfied the
    key-only form of this assertion and reported a clean run over every lost
    structure. (How that was found -- mutating the line rather than reading it
    -- is provenance this repo cannot check.)
    """
    values = [
        ast.unparse(value)
        for node in ast.walk(_tree(run_pipeline._run))
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and key.value == "n_upload_failures"
    ]
    assert values == ["len(upload_failures)"], values


def test_the_orchestrator_gives_each_child_a_disjoint_rank_window():
    """``run_tool`` is a ``modal.Function`` and not locally callable, so the
    spawn loop is checked structurally, the way
    tests/test_esmfold2_orchestrator_reaps_children.py checks its reaping.

    The VALUE is asserted, not merely the presence of the key: ``i`` alone, or
    ``i * n_seeds``, would overlap the windows for batch_size > 1 and put two
    designs on one live row -- the exact defect the offset exists to prevent.
    """
    from tools.esmfold2_design import modal_app

    tree = ast.parse(inspect.getsource(modal_app))
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "run_tool"
    )
    values = [
        ast.unparse(node.value)
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and any(
            ast.unparse(t).endswith("['rank_offset']") for t in node.targets
        )
    ]
    assert values == ["i * batch_size"], values


def test_the_orchestrator_sums_the_child_upload_failures():
    """An umbrella result that dropped the children's count would report zero
    upload failures for every multi-seed run."""
    from tools.esmfold2_design import modal_app

    src = inspect.getsource(modal_app._aggregate)
    assert 'smoke.get("n_upload_failures")' in src
    assert '"n_upload_failures": upload_failures' in src
