"""Results-page parity across tools (QA 2026-10-01).

a. A streamed row with no structure says "3D when finished", not a dash.
b. One "Returned N / M" line, from results_shell.results_panel.
c. ESMFold's single fold renders the NGL viewer AF2 uses.
d. ESMFold's single fold exports a real row through the CSV/FASTA/ZIP routes.
e. The live progress line prints one design count.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

ROOT = Path(__file__).resolve().parent.parent
_JID = "11111111-2222-3333-4444-555555555555"

RETURNED = re.compile(
    r'class="results-returned".*?Returned <strong[^>]*>(\d+) / (\d+)</strong>',
    re.S,
)


def _unwrapped(text):
    """Body lines joined, so an 80-column FASTA record compares whole."""
    return "".join(line for line in text.splitlines() if not line.startswith(">"))


def _example(tool):
    return json.loads((ROOT / "tools" / tool / "example" / "result.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def flask_app(isolate_supabase_module):
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _render(flask_app, tool, result, inputs=None, job_id=_JID):
    from flask import render_template

    job = SimpleNamespace(id=job_id, tool=tool, status="succeeded",
                          inputs=inputs or {}, result=result, created_at=None)
    with flask_app.test_request_context(f"/jobs/{job_id}"):
        return render_template(f"tools/{tool}_results.html", job=job,
                               send_target_tools=None)


# ---------------------------------------------------------------------------
# b. requested_designs + the shared line
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool, inputs, expected", [
    ("rfdiffusion", {"num_designs": 10}, 10),
    ("boltzgen", {"budget": "20"}, 20),
    ("iggm", {"num_samples": 40}, 40),
    ("rfdiffusion", {}, None),
    ("rfdiffusion", {"num_designs": 0}, None),
    ("rfdiffusion", {"num_designs": "ten"}, None),
    ("rfdiffusion", None, None),
    ("af2", {"num_designs": 10}, None),
])
def test_requested_designs(tool, inputs, expected):
    from shared.jobs import requested_designs

    assert requested_designs(SimpleNamespace(tool=tool, inputs=inputs)) == expected


@pytest.mark.parametrize("tool, n", [
    ("af2", 10), ("boltz2", 12), ("esmfold2_design", 2), ("iggm", 40), ("opendde", 4),
])
def test_designs_tools_print_one_returned_line(flask_app, tool, n):
    html = _render(flask_app, tool, _example(tool))
    assert RETURNED.findall(html) == [(str(n), str(n))]
    for old in ("Designs folded", "Designs returned", "Predictions returned"):
        assert old not in html, f"{tool}: old per-tool tile {old!r} still renders"


@pytest.mark.parametrize("tool, inputs, expected", [
    ("rfdiffusion", {"num_designs": 10}, ("8", "10")),
    ("pxdesign", {"num_designs": 30}, ("25", "30")),
    ("bindcraft", {"num_designs": 4}, ("2", "4")),
    ("boltzgen", {"budget": 10}, ("5", "10")),
])
def test_candidates_tools_print_the_line_from_the_form_count(flask_app, tool, inputs, expected):
    assert RETURNED.findall(_render(flask_app, tool, _example(tool), inputs)) == [expected]


def test_the_line_renders_over_zero_candidates(flask_app):
    html = _render(flask_app, "rfdiffusion", {"candidates": []}, {"num_designs": 10})
    assert RETURNED.findall(html) == [("0", "10")]
    assert "Zero candidates returned" in html


@pytest.mark.parametrize("tool", ["rfdiffusion", "rfantibody", "proteina"])
def test_no_line_without_a_requested_count(flask_app, tool):
    # rfdiffusion with no stored num_designs; rfantibody and proteina never
    # pass requested= (templates/tools/{rfantibody,proteina}_results.html).
    inputs = {} if tool == "rfdiffusion" else {"num_designs": 10}
    assert "results-returned" not in _render(flask_app, tool, _example(tool), inputs)


def test_failures_are_shown_on_the_line(flask_app):
    result = dict(_example("af2"), n_failures=2)
    html = _render(flask_app, "af2", result)
    assert "(2 failed)" in html


# ---------------------------------------------------------------------------
# c. ESMFold standalone viewer
# ---------------------------------------------------------------------------
def test_esmfold_single_fold_renders_the_ngl_viewer_on_0_100(flask_app):
    from shared.pdb_bfactors import bfactors_on_100_b64 as pdb_b64_on_100

    result = _example("esmfold")
    html = _render(flask_app, "esmfold", result)
    m = re.search(r'id="esmfold-ngl-viewer"\s+data-pdb-b64="([^"]+)"', html)
    assert m, "no NGL viewer on the ESMFold single-fold page"
    assert "cdn.jsdelivr.net/npm/ngl@" in html
    assert m.group(1) == pdb_b64_on_100(result["pdb_b64"])
    assert m.group(1) != result["pdb_b64"], "fixture is already on 0-100; rescale untested"


def test_af2_single_fold_still_renders_its_viewer(flask_app):
    pdb = _example("esmfold")["pdb_b64"]
    html = _render(flask_app, "af2", {"tier": "standalone", "pdb_b64": pdb})
    assert 'id="af2-ngl-viewer"' in html


# ---------------------------------------------------------------------------
# d. ESMFold standalone exports
# ---------------------------------------------------------------------------
def test_esmfold_single_fold_links_csv_and_fasta(flask_app):
    html = _render(flask_app, "esmfold", _example("esmfold"))
    assert f"/jobs/{_JID}/export.csv" in html
    assert f"/jobs/{_JID}/export.fasta" in html


def test_the_worked_example_links_no_export_route(flask_app):
    html = _render(flask_app, "esmfold", _example("esmfold"), job_id="example")
    assert "export.csv" not in html and "export.fasta" not in html
    assert 'id="esmfold-ngl-viewer"' in html


def test_page_ordered_records_reads_the_single_fold():
    from shared.exports import candidates_to_csv, candidates_to_fasta, candidates_to_zip
    from shared.jobs import page_ordered_records

    result = _example("esmfold")
    rows = page_ordered_records("esmfold", result)
    assert len(rows) == 1
    csv = candidates_to_csv(rows)
    assert result["sequence"] in csv
    assert "39.0" in csv, "mean_plddt 0.39 not scaled to the 0-100 column"
    assert result["sequence"] in _unwrapped(candidates_to_fasta(rows, tool="esmfold"))
    with zipfile.ZipFile(io.BytesIO(candidates_to_zip(rows, lambda *_: None))) as zf:
        names = zf.namelist()
        assert any(n.endswith("esmfold.pdb") for n in names), names


@pytest.mark.parametrize("tool, result", [
    ("esmfold", {"tier": "standalone"}),
    ("af2", {"tier": "standalone", "pdb_b64": "QUFB", "sequence": "MKV"}),
])
def test_no_row_is_invented(tool, result):
    from shared.jobs import page_ordered_records

    assert page_ordered_records(tool, result) == []


# ---------------------------------------------------------------------------
# e. One count on the live progress line
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stage, total, expected", [
    ("Running BindCraft - 0/2 designs", 2, "Running BindCraft"),
    ("Running AF2 validation — 9/10 designs", 10, "Running AF2 validation"),
    ("Folding (3/10)", 10, "Folding"),
    ("Step 2/5: MPNN", 10, "Step 2/5: MPNN"),
    ("Folding (3/10)", 0, "Folding (3/10)"),
])
def test_progress_stage_drops_the_design_count(stage, total, expected):
    from blueprints.jobs import _progress_one_count

    out = _progress_one_count({"stage": stage, "designs_total": total, "designs_completed": 3})
    assert out["stage"] == expected
    assert out["designs_completed"] == 3


def test_progress_one_count_tolerates_no_progress():
    from blueprints.jobs import _progress_one_count

    assert _progress_one_count(None) == {}


@pytest.fixture()
def client():
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _job(tool, result=None, status="succeeded", inputs=None):
    return SimpleNamespace(
        id=_JID, tool=tool, preset="pilot", status=status,
        created_at="2026-01-01T00:00:00+00:00", inputs=inputs or {},
        result=result, error=None, gpu_seconds_used=None, started_at=None,
        modal_function_call_id=None,
    )


def _get(client, path, job):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    ctx = SimpleNamespace(user_id="u-1", tier="free", balance=100, email="u@example.com")
    with patch("blueprints.jobs.load_user_context", return_value=ctx), \
            patch("blueprints.jobs.get_job", return_value=job):
        return client.get(path)


def test_status_json_serves_the_stage_without_its_count(client):
    job = _job("bindcraft", status="running", inputs={
        "_partial_candidates": [],
        "_progress": {"stage": "Running BindCraft - 1/2 designs",
                      "designs_completed": 1, "designs_total": 2},
    })
    resp = _get(client, f"/jobs/{_JID}/status.json", job)
    assert resp.status_code == 200
    progress = resp.get_json()["progress"]
    assert progress["stage"] == "Running BindCraft"
    assert (progress["designs_completed"], progress["designs_total"]) == (1, 2)


@pytest.mark.parametrize("route", ["export.csv", "export.fasta"])
def test_esmfold_export_routes_return_the_fold(client, route):
    result = _example("esmfold")
    resp = _get(client, f"/jobs/{_JID}/{route}", _job("esmfold", result))
    assert resp.status_code == 200
    assert result["sequence"] in _unwrapped(resp.get_data(as_text=True))


def test_esmfold_export_zip_carries_the_structure(client):
    resp = _get(client, f"/jobs/{_JID}/export.zip", _job("esmfold", _example("esmfold")))
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.get_data())) as zf:
        pdb = [n for n in zf.namelist() if n.endswith(".pdb")]
        assert pdb and zf.read(pdb[0]).startswith((b"ATOM", b"HEADER", b"MODEL", b"PARENT", b"REMARK"))


# ---------------------------------------------------------------------------
# a + e. job_detail.html script contract
# ---------------------------------------------------------------------------
def _job_detail():
    return (ROOT / "templates" / "job_detail.html").read_text(encoding="utf-8")


def test_a_row_without_a_structure_says_when_it_will_have_one():
    assert "3D when finished" in _job_detail()


def test_the_progress_line_prints_one_count():
    src = _job_detail()
    assert "' designs complete'" in src
    assert "'(' + done + '/' + total + ')'" not in src
    assert "returned so far" not in src

