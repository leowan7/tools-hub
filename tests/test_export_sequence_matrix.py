"""Every design row's sequence reaches every download, at every scale.

A customer's 1000-design rfantibody campaign (2026-10-27) downloaded a CSV with
no sequences and an empty FASTA: rfantibody rows store only a structure key,
and the campaign routes read no structures. This matrix is tool x surface x
scale. Each row must come out with its chains' sequences, or with a reason the
reader can see: the CSV's ``sequence_note`` column, or the FASTA's leading
``# N structure(s) not read`` line. A blank is the failure.

Rows are each tool's own example row shape (``candidates`` or ``designs``,
from ``tools/<pkg>/example/result.json``) with any stored sequence removed and
a ``pdb_key`` set, so every tool takes the structure-read path, the one the
customer hit. A tool that does store a sequence takes a shorter path than this.
"""

from __future__ import annotations

import copy
import csv
import io
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.test_export_page_parity import _example, _pdb, _stored, _wire, client  # noqa: F401
from tests.test_target_export import _agg, _ctx, _login

pytestmark = pytest.mark.usefixtures("isolate_supabase")

TOOLS = (
    "af2", "bindcraft", "boltz2", "boltzgen", "colabfold", "esmfold",
    "esmfold2-design", "iggm", "opendde", "proteina", "pxdesign",
    "rfantibody", "rfdiffusion",
)
# Antibody tools put out heavy + light chains beside the antigen.
_CHAINS = {"iggm": {"H": "MKWC", "L": "GCKM", "T": "KMKG"},
           "rfantibody": {"H": "MKWC", "L": "GCKM", "T": "KMKG"}}
_DEFAULT_CHAINS = {"A": "MKWAG", "B": "GCKM"}
SURFACES = ("job", "campaign", "target")
CAP = 200  # shared/exports.py _MAX_STRUCTURE_READS, asserted below
SCALES = (1, 3, CAP, CAP + 1, 5000)
_JOBS = 5  # sub-jobs a campaign or target spreads its rows over


def _rows(tool: str, n: int) -> list[dict]:
    example = _example(tool)
    if tool in ("colabfold", "esmfold"):
        # Their examples are the single-fold shape; this is the batch row.
        template = {"name": "d", "mean_plddt": 0.8}
    else:
        template = _stored(example)[0]
    rows = []
    for i in range(n):
        row = copy.deepcopy(template)
        for key in ("sequence", "binder_sequence", "pdb_content_b64"):
            row.pop(key, None)
        row["pdb_key"] = f"designs/design_{i}.pdb"
        rows.append(row)
    return rows


def _container(tool: str) -> str:
    example = _example(tool)
    return "candidates" if example.get("candidates") else "designs"


def _get(client, tool, surface, rows, fmt, monkeypatch):  # noqa: F811
    pdb = _pdb(_CHAINS.get(tool, _DEFAULT_CHAINS))
    if surface == "job":
        job = _wire(monkeypatch, tool, {_container(tool): rows},
                    fetch=lambda **_kw: pdb)
        return client.get(f"/jobs/{job.id}/export.{fmt}").get_data(as_text=True)
    import blueprints.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "download_output", lambda **_kw: pdb)
    merged = [{**r, "_source_job_id": f"job-{i % _JOBS}", "_source_tool": tool,
               "_source_index": i} for i, r in enumerate(rows)]
    if surface == "campaign":
        import blueprints.campaigns as campaigns_mod
        from shared import compute_campaigns as cc
        monkeypatch.setattr(campaigns_mod, "load_user_context", lambda: SimpleNamespace(
            user_id="u-1", tier="free", balance=100, email="u@example.com"))
        monkeypatch.setattr(cc, "aggregate_campaign_candidates", lambda *_a, **_k: {
            "tool": tool, "candidates": merged, "total": len(merged)})
        return client.get(f"/campaigns/c-1/export.{fmt}").get_data(as_text=True)
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg(merged, tools=[tool])):
        return client.get(f"/targets/t-1/export.{fmt}").get_data(as_text=True)


def test_the_matrix_cap_is_the_real_cap():
    from shared import exports
    assert exports._MAX_STRUCTURE_READS == CAP


@pytest.mark.parametrize("n", SCALES)
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("tool", TOOLS)
def test_every_csv_row_carries_its_chains_or_a_visible_reason(
        client, monkeypatch, tool, surface, n):  # noqa: F811
    chains = _CHAINS.get(tool, _DEFAULT_CHAINS)
    body = _get(client, tool, surface, _rows(tool, n), "csv", monkeypatch)
    rows = list(csv.DictReader(io.StringIO(body)))
    assert len(rows) == n
    read = [r for r in rows if all(r.get(f"sequence_chain{c}") == s
                                   for c, s in chains.items())]
    unread = [r for r in rows if r not in read]
    assert len(read) == min(n, CAP)
    capped = f"not read: this export reads at most {CAP} structures"
    assert all(r["sequence_note"] == capped for r in unread)


@pytest.mark.parametrize("n", SCALES)
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("tool", TOOLS)
def test_every_fasta_design_has_records_or_is_counted_as_unread(
        client, monkeypatch, tool, surface, n):  # noqa: F811
    chains = _CHAINS.get(tool, _DEFAULT_CHAINS)
    body = _get(client, tool, surface, _rows(tool, n), "fasta", monkeypatch)
    lines = body.splitlines()
    ranks = {m.group(1) for ln in lines
             if (m := re.match(r">rank(\d+)_", ln))}
    # The job FASTA reads every structure; the merged ones stop at the cap.
    expect_read = n if surface == "job" else min(n, CAP)
    assert len(ranks) == expect_read
    seqs = [ln for ln in lines if ln and not ln.startswith((">", "#"))]
    assert seqs == list(chains.values()) * expect_read
    if expect_read < n:
        assert lines[0] == (f"# {n - expect_read} structure(s) not read: "
                            f"this export reads at most {CAP} structures")
    else:
        assert not lines[0].startswith("#")


def test_a_streamed_row_recovered_from_a_partial_reads_its_structure(
        client, monkeypatch):  # noqa: F811
    """A cancelled, failed-with-delivery or still-running job keeps the rows
    its heartbeats streamed. The heartbeat schema keeps no sequence
    (webhooks/modal.py _sanitize_candidate), so the rebuilt row is a bare
    ``pdb_key`` (shared/job_recovery.py _candidate_from_partial)."""
    from shared.job_recovery import _candidate_from_partial
    from webhooks.modal import _sanitize_candidate

    rows = [_candidate_from_partial(_sanitize_candidate(
        {"rank": i, "pdb_key": f"design_{i}.pdb", "iptm": 0.5,
         "sequence": "IGNORED"})) for i in range(3)]
    assert all("sequence" not in r for r in rows)
    pdb = _pdb(_CHAINS["rfantibody"])
    job = _wire(monkeypatch, "rfantibody",
                {"status": "cancelled", "candidates": rows},
                fetch=lambda **_kw: pdb)
    csv_rows = list(csv.DictReader(io.StringIO(
        client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True))))
    assert [r["sequence_chainH"] for r in csv_rows] == ["MKWC"] * 3
    fasta = client.get(f"/jobs/{job.id}/export.fasta").get_data(as_text=True)
    assert fasta.count(">") == 3 * len(_CHAINS["rfantibody"])


def test_a_stored_sequence_is_exported_without_a_structure(client, monkeypatch):  # noqa: F811
    """esmfold2-design and rfdiffusion store the designed sequence on the row
    (tools/esmfold2_design/run_pipeline.py, tools/rfdiffusion/meta.py)."""
    rows = [{"pdb_key": None, "sequence": "MKWAG", "scores": {"ipTM": 0.8}}]
    job = _wire(monkeypatch, "rfdiffusion", {"candidates": rows},
                fetch=lambda **_kw: None)
    row = next(csv.DictReader(io.StringIO(
        client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True))))
    assert row["sequence"] == "MKWAG"
    assert "MKWAG" in client.get(f"/jobs/{job.id}/export.fasta").get_data(as_text=True)


def test_an_esmfold_single_fold_exports_its_sequence(client, monkeypatch):  # noqa: F811
    result = {"sequence": "MKWAG", "mean_plddt": 0.8}
    job = _wire(monkeypatch, "esmfold", result, fetch=lambda **_kw: None)
    row = next(csv.DictReader(io.StringIO(
        client.get(f"/jobs/{job.id}/export.csv").get_data(as_text=True))))
    assert row["sequence"] == "MKWAG"


def _ingested(tool: str, n: int) -> list[dict]:
    """``_rows`` through the real ingest (shared/jobs.py
    _slim_result_for_persist) and a JSON round trip: even rows carry inline
    bytes, so they store their chains; odd rows do not, so they need a read."""
    import base64
    import json

    from shared.jobs import _slim_result_for_persist

    b64 = base64.b64encode(_pdb(_CHAINS.get(tool, _DEFAULT_CHAINS))).decode()
    rows = [{**r, "pdb_content_b64": b64} if i % 2 == 0 else r
            for i, r in enumerate(_rows(tool, n))]
    kept = _slim_result_for_persist({_container(tool): rows})
    # Slim drops the inline copy only from ``candidates``; drop it from
    # ``designs`` too, so no export below can read a structure.
    return [{k: v for k, v in r.items() if k != "pdb_content_b64"}
            for r in json.loads(json.dumps(kept))[_container(tool)]]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("tool", TOOLS)
def test_stored_chains_reach_rows_past_the_read_cap(
        client, monkeypatch, tool, surface):  # noqa: F811
    """No structure can be read here (the fetch is None), so every chain a
    row exports is the one ingest stored; only rows with neither get a note."""
    n = 2 * CAP + 3
    chains = _CHAINS.get(tool, _DEFAULT_CHAINS)
    rows = _ingested(tool, n)
    stored = n // 2 + 1
    assert sum("chain_sequences" in r for r in rows) == stored
    import tests.test_export_sequence_matrix as me
    monkeypatch.setattr(me, "_pdb", lambda _chains: None)  # _get's fetch
    csv_body = _get(client, tool, surface, rows, "csv", monkeypatch)
    fasta = _get(client, tool, surface, rows, "fasta", monkeypatch)
    out = list(csv.DictReader(io.StringIO(csv_body)))
    assert len(out) == n
    for i, r in enumerate(out):
        has = all(r.get(f"sequence_chain{c}") == s for c, s in chains.items())
        assert has == (i % 2 == 0), (i, r)
    capped = f"not read: this export reads at most {CAP} structures"
    assert all(r["sequence_note"] != capped for r in out[::2])
    ranks = {m.group(1) for ln in fasta.splitlines()
             if (m := re.match(r">rank(\d+)_", ln))}
    assert len(ranks) == stored
