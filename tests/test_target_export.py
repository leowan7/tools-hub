"""Route tests for /targets/<id>/export.{csv,fasta,zip}.

These mirror the campaign exports, with ONE deliberate difference that most of
this file exists to pin: the ownership sentinel.

``_campaign_export`` gates on ``agg.get("tool") is None``, which is sound there
because a campaign always has exactly one tool. A target has a LIST of tools,
and an owned target whose runs have not yet produced a design has an empty one.
Reusing that idiom would 404 a paying user's freshly launched work, so the
target export gates on ``ok`` instead. Two tests hold that down from both sides:
either alone is satisfied by the wrong gate.

The aggregator is patched at the route boundary. What it RETURNS is the route's
input; what it DOES is covered by tests/test_aggregate_target.py.
"""

from __future__ import annotations

import csv
import io
import uuid
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _ctx(user_id="u-1"):
    return SimpleNamespace(
        user_id=user_id, tier="free", balance=100, email="u@example.com",
    )


def _login(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"


def _cand(tool, job, index, metric="ipTM", value=0.9, seq="MKTAY"):
    return {
        "pdb_key": f"designs/design_{index}.pdb",
        "sequence": seq,
        "scores": {metric: value},
        "_source_tool": tool,
        "_source_job_id": job,
        "_source_index": index,
        "_source_chunk": 0,
        "_source_campaign_id": "c-" + tool,
    }


def _agg(candidates=(), **over):
    env = {
        "ok": True, "partial": False, "candidates": list(candidates),
        "total": len(candidates), "shown": len(candidates), "unranked": 0,
        "capped": False, "columns": [], "tools": ["bindcraft"], "per_tool": {},
        "campaigns": [], "standalone_jobs": 0, "refold_jobs": 0,
        "passed_total": 0, "provisional": False, "sort_mode": "percentile",
        "multi_tool": False, "limit": 300,
    }
    env.update(over)
    return env


_TID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# The ownership sentinel. Both halves are required.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["csv", "fasta", "zip"])
def test_a_foreign_or_missing_target_404s(client, fmt):
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg(ok=False, tools=[])):
        resp = client.get(f"/targets/{_TID}/export.{fmt}")
    assert resp.status_code == 404


@pytest.mark.parametrize("fmt", ["csv", "fasta", "zip"])
def test_an_owned_but_empty_target_exports_an_empty_file_not_a_404(client, fmt):
    """The half that a ``tools == []`` gate would get wrong.

    A user who launched five minutes ago and whose runs have not yet returned a
    design owns this target. Answering 404 would tell them it does not exist.
    """
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg(tools=[], candidates=[])):
        resp = client.get(f"/targets/{_TID}/export.{fmt}")
    assert resp.status_code == 200


def test_an_empty_fasta_still_says_why(client):
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg(tools=[], candidates=[])):
        body = client.get(f"/targets/{_TID}/export.fasta").get_data(as_text=True)
    assert "No sequences found" in body


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

def test_csv_carries_the_tool_column_across_pooled_tools(client):
    """shared/exports.py has declared a `tool` provenance column since Phase 0,
    omitted whenever no row carries `_source_tool`. The target aggregate is the
    first producer of that tag, so this is the column going live."""
    _login(client)
    cands = [_cand("bindcraft", "job-bc", 0), _cand("boltzgen", "job-bz", 0)]
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg(cands, tools=["bindcraft", "boltzgen"])):
        body = client.get(f"/targets/{_TID}/export.csv").get_data(as_text=True)

    rows = list(csv.DictReader(io.StringIO(body)))
    assert [r["tool"] for r in rows] == ["bindcraft", "boltzgen"]
    # Global rank, monotonic, not each tool's own rank 1.
    assert [r["rank"] for r in rows] == ["1", "2"]


def test_zip_namespaces_by_tool_so_two_design_1_pdbs_survive(client):
    _login(client)
    cands = [_cand("bindcraft", "job-bc01", 1), _cand("boltzgen", "job-bz01", 1)]
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg(cands, tools=["bindcraft", "boltzgen"])), \
            patch("shared.storage.download_output", return_value=b"ATOM  "):
        resp = client.get(f"/targets/{_TID}/export.zip")

    names = zipfile.ZipFile(io.BytesIO(resp.data)).namelist()
    assert len(set(names)) == 2, names
    assert all(n.split("/")[0] in ("bindcraft", "boltzgen") for n in names), names


def test_a_capped_zip_names_its_own_truncation(client):
    _login(client)
    cands = [_cand("bindcraft", "job-bc", i) for i in range(3)]
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg(cands, capped=True, total=1240)), \
            patch("shared.storage.download_output", return_value=b"ATOM  "):
        resp = client.get(f"/targets/{_TID}/export.zip")
    assert "top3of1240" in resp.headers["Content-Disposition"]


# ---------------------------------------------------------------------------
# Limits and sort
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt,expected", [
    ("csv", None), ("fasta", None), ("zip", 300),
])
def test_text_exports_are_uncapped_and_the_zip_is_not(client, fmt, expected):
    """The ZIP pulls PDB bytes into the web process; CSV and FASTA do not. A
    target pools more tools than a campaign, so the asymmetry matters more."""
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg()) as agg, \
            patch("shared.storage.download_output", return_value=b"ATOM  "):
        client.get(f"/targets/{_TID}/export.{fmt}")
    assert agg.call_args.kwargs["limit"] == expected


def test_the_sort_mode_is_forwarded_so_the_file_matches_the_screen(client):
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg()) as agg:
        client.get(f"/targets/{_TID}/export.csv?sort=tool")
    assert agg.call_args.kwargs["sort_mode"] == "tool"


def test_an_unknown_sort_mode_falls_back_rather_than_erroring(client):
    """It arrives from a query string, so a stale pasted link must render."""
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg()) as agg:
        resp = client.get(f"/targets/{_TID}/export.csv?sort=nonsense")
    assert resp.status_code == 200
    assert agg.call_args.kwargs["sort_mode"] == "percentile"


def test_the_export_is_scoped_to_the_caller(client):
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg()) as agg:
        client.get(f"/targets/{_TID}/export.csv")
    assert agg.call_args.kwargs["user_id"] == "u-1"
    assert agg.call_args.args[0] == _TID


def test_signed_out_redirects_rather_than_exporting(client):
    resp = client.get(f"/targets/{_TID}/export.csv")
    assert resp.status_code in (301, 302)
