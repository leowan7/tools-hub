"""The admin lab-project page lists the shortlisted designs themselves.

Campaign 60120b26 (source job 26c1f866, esmfold2-design) showed staff
"Candidates: 0, 1 (indices, 0-based)" and a "Source job" button to
``/jobs/<id>``, which is owner-scoped and 404s for a staff account. The
indices are positions in the STORED candidate list
(``blueprints/lab_projects.py::_submit_job_shortlist`` checks them against
``candidate_count(job.result)``), while the customer's page may re-sort, so
the table has to map each index to its design rather than print it.
"""

from __future__ import annotations

import base64
import csv
import io
import re
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_JID = "26c1f866-0000-4000-8000-000000000000"
_SEQ = {
    "design_1": "MKVLAAGIVGLLLAQ",
    "design_2": "GSHMKELLEKAREL",
    "design_0": "AAAAWWWWYYYYPP",
}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _client(app, email):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = "someone"
        sess["user_email"] = email
    return c


@pytest.fixture
def client(app):
    return _client(app, "leo@ranomics.com")


def _campaign(**over):
    from shared.campaigns import Campaign
    row = {
        "id": str(uuid.uuid4()), "user_id": "u-1", "target_name": "HER2",
        "assay_type": "yeast_display", "budget_band": "pilot",
        "status": "submitted", "submission_source": "web",
        "source_job_id": _JID, "candidate_indices": [0, 1],
    }
    row.update(over)
    return Campaign.from_row(row)


def _job_26c1f866():
    """Stored order design_1, design_2, design_0 (source_rank 0, 1, 2), so
    stored index n is not design_n. ``pI`` is not a pLDDT column, so it
    is printed as stored; ``plddt`` is, and is stored 0-1."""
    cands = [
        {"rank": 0, "name": "design_1", "pdb_key": "design_1.pdb",
         "sequence": _SEQ["design_1"],
         "scores": {"ipTM": 0.930, "pI": 5.1, "plddt": 0.8812}},
        {"rank": 1, "name": "design_2", "pdb_key": "design_2.pdb",
         "sequence": _SEQ["design_2"],
         "scores": {"ipTM": 0.679, "pI": 6.2, "plddt": 0.7}},
        {"rank": 2, "name": "design_0", "pdb_key": "design_0.pdb",
         "sequence": _SEQ["design_0"],
         "scores": {"ipTM": 0.412, "pI": 7.0, "plddt": 0.5}},
    ]
    return SimpleNamespace(id=_JID, tool="esmfold2-design", user_id="u-1",
                           result={"candidates": cands})


def _get(client, campaign, jobs, path=""):
    with patch("shared.campaigns.get_campaign", return_value=campaign), \
            patch("shared.jobs.get_job",
                  side_effect=lambda jid, **kw: jobs.get(jid)):
        return client.get(f"/admin/lab-projects/{campaign.id}{path}")


def _tables(html):
    """(shortlisted rows, not-shortlisted rows) as lists of the
    ``data-design`` attribute, read off the rendered panel."""
    panel = html.split('id="shortlisted-designs"', 1)[1]
    picked, _, rest = panel.partition("not shortlisted")
    rows = lambda part: re.findall(r'<tr data-design="([^"]*)"', part)  # noqa: E731
    return rows(picked), rows(rest)


def _row(html, name):
    """The whitespace-collapsed <tr> for one design."""
    m = re.search(rf'<tr data-design="{name}".*?</tr>', html, re.S)
    return " ".join(m.group(0).split())


def test_a_web_campaign_lists_exactly_the_starred_designs(client):
    resp = _get(client, _campaign(), {_JID: _job_26c1f866()})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    picked, others = _tables(html)
    # Indices 0 and 1 of the STORED list, not designs 0 and 1 by name.
    assert picked == ["design_1", "design_2"]
    assert others == ["design_0"]
    first, second = _row(html, "design_1"), _row(html, "design_2")
    assert _SEQ["design_1"] in first and "0.93" in first
    assert _SEQ["design_2"] in second and "0.679" in second
    # Same pLDDT scale as the CSV: 0.8812 stored, 88.12 shown.
    assert "88.12" in first and "0.8812" not in first
    assert f">{len(_SEQ['design_1'])}<" in first
    assert _SEQ["design_0"] not in html.split("not shortlisted", 1)[0]


def test_the_number_is_the_page_position_not_the_stored_index(client):
    """boltz2 stores ``designs`` and its page re-sorts them by ipTM, so stored
    index 2 is the second row the customer saw and stored index 0 the last."""
    designs = [
        {"name": "d0", "iptm": 0.5, "sequence": "AAAAAA"},
        {"name": "d1", "iptm": 0.9, "sequence": "CCCCCC"},
        {"name": "d2", "iptm": 0.7, "sequence": "DDDDDD"},
    ]
    job = SimpleNamespace(id=_JID, tool="boltz2", user_id="u-1",
                          result={"designs": designs})
    html = _get(client, _campaign(candidate_indices=[2, 0]),
                {_JID: job}).get_data(as_text=True)
    picked, others = _tables(html)
    assert picked == ["d2", "d0"]
    assert others == ["d1"]
    assert _row(html, "d2").startswith('<tr data-design="d2"> <td>2</td>')
    assert _row(html, "d0").startswith('<tr data-design="d0"> <td>3</td>')
    assert _row(html, "d1").startswith('<tr data-design="d1"> <td>1</td>')


def test_the_page_has_no_owner_scoped_links(client):
    html = _get(client, _campaign(), {_JID: _job_26c1f866()}).get_data(as_text=True)
    assert "/jobs/" not in html
    assert 'href="#shortlisted-designs"' in html


def test_the_source_job_is_read_once(client):
    seen: list[str] = []

    def fake_get_job(jid, **kw):
        seen.append(jid)
        return _job_26c1f866()

    campaign = _campaign()
    with patch("shared.campaigns.get_campaign", return_value=campaign), \
            patch("shared.jobs.get_job", side_effect=fake_get_job):
        assert client.get(f"/admin/lab-projects/{campaign.id}").status_code == 200
    assert seen == [_JID]


def test_a_ref_campaign_gets_the_table_too(client):
    campaign = _campaign(
        submission_source="target", source_job_id=None, candidate_indices=[],
        source_target_id=str(uuid.uuid4()),
        candidate_refs=[{"job_id": _JID, "index": 2}],
    )
    html = _get(client, campaign, {_JID: _job_26c1f866()}).get_data(as_text=True)
    picked, others = _tables(html)
    assert picked == ["design_0"]
    assert others == ["design_1", "design_2"]
    assert "/targets/" not in html


def test_the_admin_csv_carries_the_sequence(client):
    resp = _get(client, _campaign(), {_JID: _job_26c1f866()}, f"/source/{_JID}/export.csv")
    assert resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))
    assert [r["sequence"] for r in rows] == [
        _SEQ["design_1"], _SEQ["design_2"], _SEQ["design_0"]]


def _pdb(chains: dict[str, str]) -> bytes:
    """One CA atom per residue, so Biopython reads back ``chains``."""
    lines, n = [], 1
    for cid, seq in chains.items():
        for i, aa in enumerate(seq, start=1):
            three = {"A": "ALA", "C": "CYS", "K": "LYS",
                     "M": "MET", "W": "TRP"}[aa]
            lines.append(f"ATOM  {n:5d}  CA  {three} {cid}{i:4d}    "
                         f"{float(i):8.3f}   0.000   0.000  1.00 90.00"
                         "           C")
            n += 1
        lines.append("TER")
    return ("\n".join(lines) + "\nEND\n").encode()


def _two_chain_job():
    """A job of the shape this CSV used to lose, plus the bytes each row's
    ``pdb_key`` fetches: no stored ``sequence``, so the sequences are only in
    the structure file and the route has to download it."""
    files = {"d0.pdb": _pdb({"A": "MKWMKW", "B": "MKC"}),
             "d1.pdb": _pdb({"A": "MKWMKW", "B": "WCA"})}
    cands = [
        {"rank": 0, "name": "d0", "pdb_key": "d0.pdb", "scores": {"ipTM": 0.9}},
        {"rank": 1, "name": "d1", "pdb_key": "d1.pdb", "scores": {"ipTM": 0.8}},
    ]
    job = SimpleNamespace(id=_JID, tool="boltzgen", user_id="u-1",
                          result={"candidates": cands})
    return job, files


def test_the_staff_copy_of_a_job_csv_carries_the_same_sequences(client, monkeypatch):
    """``/jobs/<id>/export.csv`` is owner-scoped and 404s for staff, so this
    route is the only way staff read that CSV, and it must not be the stale
    one. It reads as the job's OWNER: the staff session here is ``someone``
    (``_client``) and the owner is ``u-1``, so the recorded user_id shows
    which of the two the route passed to storage."""
    import blueprints.jobs as jobs_mod
    job, files = _two_chain_job()
    seen: list[tuple[str, str, str]] = []

    def fake_download(*, user_id, job_id, filename):
        seen.append((user_id, job_id, filename))
        return files[filename]

    monkeypatch.setattr(jobs_mod, "download_output", fake_download)
    resp = _get(client, _campaign(), {_JID: job}, f"/source/{_JID}/export.csv")
    assert resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))
    assert [(r["sequence_chainA"], r["sequence_chainB"]) for r in rows] == [
        ("MKWMKW", "MKC"), ("MKWMKW", "WCA")]
    assert seen == [("u-1", _JID, "d0.pdb"), ("u-1", _JID, "d1.pdb")]


def test_a_design_structure_downloads_by_stored_index(client):
    pdb = b"ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 0.88           C\n"
    job = _job_26c1f866()
    job.result["candidates"][1]["pdb_content_b64"] = base64.b64encode(pdb).decode()
    resp = _get(client, _campaign(), {_JID: job}, f"/source/{_JID}/structure/1")
    assert resp.status_code == 200
    assert b"ATOM" in resp.data
    assert "design_2.pdb" in resp.headers["Content-Disposition"]


def test_a_structure_from_a_job_the_campaign_does_not_name_is_refused(client):
    other = "99999999-0000-4000-8000-000000000000"
    jobs = {_JID: _job_26c1f866(), other: _job_26c1f866()}
    resp = _get(client, _campaign(), jobs, f"/source/{other}/structure/0")
    assert resp.status_code == 404


@pytest.mark.parametrize("path", ["", f"/source/{_JID}/export.csv", f"/source/{_JID}/structure/0"])
def test_non_staff_get_a_404(app, path):
    resp = _get(_client(app, "scientist@example.com"), _campaign(),
                {_JID: _job_26c1f866()}, path)
    assert resp.status_code == 404
