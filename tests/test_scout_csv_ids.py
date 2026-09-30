"""Both Scout CSV downloads use one epitope_id meaning plus a rank column.

QA 2026-09-29 P1-2: on 4ZQK chain A the top-3 file's epitope_id was the rank
while the all-patches file's was the patch id, so id 1 named two different
patches. The rows below are prod's 4ZQK chain A scores (ids, scores,
centroids): patch 5 scores third but sits ~11 A from patch 8, so the 15 A
spacing rule drops it from the top 3.

    pytest tests/test_scout_csv_ids.py -v
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from scout.flags import _CSV_COLUMNS_BASE
from tests.test_scout_chain_scoped_results import (  # noqa: F401 (fixtures)
    app,
    client,
    reap_jobs,
    _upload_two_chain_job,
)

pytestmark = pytest.mark.usefixtures("isolate_supabase")

# (patch id, composite, centroid) in results.csv order, i.e. score descending.
_ROWS = [
    (8, 0.528, (-10.76, 46.02, 126.12)),
    (1, 0.527, (4.17, 41.33, 118.51)),
    (5, 0.496, (-7.06, 56.55, 125.92)),
    (6, 0.488, (-15.1, 32.8, 104.9)),
    (3, 0.483, (-5.87, 34.63, 111.26)),
]


def _run(pdb_path, chain_id, progress_callback=None):
    out = Path(pdb_path).parent / "results.csv"
    with out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS_BASE)
        writer.writeheader()
        for pid, score, (x, y, z) in _ROWS:
            row = dict.fromkeys(_CSV_COLUMNS_BASE, "0")
            row.update({
                "epitope_id": str(pid), "chain_id": chain_id,
                "residues": ",".join(f"ALA{pid * 10 + k}" for k in range(5)),
                "residue_count": "5", "composite_score": str(score),
                "secondary_structure": "loop",
                "centroid_x": str(x), "centroid_y": str(y), "centroid_z": str(z),
            })
            writer.writerow(row)
    return out


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr("scout.pipeline.run_pipeline", _run)
    monkeypatch.setattr(
        "scout.epitope_db.resolve_uniprot_id",
        lambda *a, **k: {"uniprot_id": "", "protein_name": "",
                         "identity_pct": "unknown", "source": ""},
    )
    monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])
    monkeypatch.setattr("scout.interfaces.detect_interfaces", lambda *a, **k: [])


def _csv(client, job_id, full):
    resp = client.get(f"/scout/download/{job_id}" + ("?full=1" if full else ""))
    assert resp.status_code == 200, resp.data
    return list(csv.DictReader(io.StringIO(resp.data.decode())))


def test_one_id_meaning_and_rank_in_both_files(client, stub, reap_jobs):
    job_id = _upload_two_chain_job(client)
    resp = client.post("/scout/analyze", json={"job_id": job_id, "chain": "A"})
    assert resp.status_code == 200, resp.data
    page_ids = [e["epitope_id"] for e in resp.get_json()["epitopes"]]
    assert page_ids == [8, 1, 6]

    top = _csv(client, job_id, full=False)
    full = _csv(client, job_id, full=True)

    assert [(r["rank"], r["epitope_id"]) for r in top] == [("1", "8"), ("2", "1"), ("3", "6")]
    # Ranked patches first in rank order, then the unreported rest by score.
    assert [(r["rank"], r["epitope_id"]) for r in full] == [
        ("1", "8"), ("2", "1"), ("3", "6"), ("", "5"), ("", "3"),
    ]
    # Same id, same patch, in both files.
    by_id = {r["epitope_id"]: r["residues"] for r in full}
    assert all(by_id[r["epitope_id"]] == r["residues"] for r in top)

    # The un-annotated fallback file carries the same ids and ranks.
    fallback = list(csv.DictReader(open(Path("tmp") / job_id / "epitopes.csv", newline="")))
    assert [(r["rank"], r["epitope_id"]) for r in fallback] == [("1", "8"), ("2", "1"), ("3", "6")]
