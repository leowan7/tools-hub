"""Route tests for /targets/*.

Auth, persistence, and Storage are mocked; this covers wiring, ownership, and
the duplicate-upload offer.
"""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# These tests assert ownership and isolation, so they must not consult the
# live database that app.py's load_dotenv() would otherwise hand them.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared.targets import DesignTarget

# A minimal, genuinely parseable PDB so resolve_target_upload's Biopython
# inspection succeeds rather than the route rejecting before the code under
# test runs.
_PDB = b"""ATOM      1  N   MET A   1      11.104  13.207  10.000  1.00 20.00           N
ATOM      2  CA  MET A   1      12.560  13.207  10.000  1.00 20.00           C
ATOM      3  C   MET A   1      13.100  14.600  10.000  1.00 20.00           C
ATOM      4  O   MET A   1      12.400  15.600  10.000  1.00 20.00           O
ATOM      5  N   ALA A   2      14.400  14.700  10.000  1.00 20.00           N
ATOM      6  CA  ALA A   2      15.100  15.980  10.000  1.00 20.00           C
ATOM      7  C   ALA A   2      16.600  15.800  10.000  1.00 20.00           C
ATOM      8  O   ALA A   2      17.100  14.700  10.000  1.00 20.00           O
END
"""


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
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


def _target(**kw):
    base = dict(
        id=str(uuid.uuid4()),
        user_id="u-1",
        name="HER2",
        filename="her2.pdb",
        storage_path="u-1/target-x/her2.pdb",
        target_chain="A",
        chain_summary={
            "total_standard_residues": 210,
            "chains": [{
                "chain_id": "A", "standard_residue_count": 210,
                "hetatm_resnames": [], "water_count": 0,
                "min_resnum": 1, "max_resnum": 210,
            }],
        },
    )
    base.update(kw)
    return DesignTarget(**base)


def test_targets_list_renders_empty_state(client):
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.list_targets_for_user", return_value=[]):
        resp = client.get("/targets")
    assert resp.status_code == 200
    assert "No targets yet" in resp.get_data(as_text=True)


def test_targets_list_renders_a_target(client):
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.list_targets_for_user",
                  return_value=[_target()]):
        resp = client.get("/targets")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "HER2" in body
    assert "210 residues" in body


def test_target_new_renders(client):
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()):
        resp = client.get("/targets/new")
    assert resp.status_code == 200
    assert "Add a target" in resp.get_data(as_text=True)


def test_create_target_redirects_to_the_new_target(client):
    _login(client)
    created = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.find_target_by_sha256", return_value=None), \
            patch("blueprints.targets.create_target", return_value=created) as mk:
        resp = client.post("/targets", data={
            "name": "HER2",
            "target_chain": "A",
            "hotspot_residues": "10, 12",
            "target_pdb": (io.BytesIO(_PDB), "her2.pdb"),
        }, content_type="multipart/form-data")

    assert resp.status_code == 302
    assert created.id in resp.headers["Location"]
    assert mk.call_args.kwargs["hotspot_residues"] == [10, 12]
    assert mk.call_args.kwargs["target_chain"] == "A"


def test_create_target_rejects_a_chain_not_in_the_upload(client):
    """Chain validation happens at intake, so a typo never becomes a target
    every future run inherits."""
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.create_target") as mk:
        resp = client.post("/targets", data={
            "target_chain": "Z",
            "target_pdb": (io.BytesIO(_PDB), "her2.pdb"),
        }, content_type="multipart/form-data")

    assert resp.status_code == 400
    # Jinja escapes the quotes around the chain id, so match the prose.
    assert "is not in the uploaded file" in resp.get_data(as_text=True)
    mk.assert_not_called()


def test_create_target_rejects_a_non_numeric_hotspot(client):
    """Silently dropping it would aim the target somewhere the user did not
    ask for."""
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.create_target") as mk:
        resp = client.post("/targets", data={
            "hotspot_residues": "10, twelve",
            "target_pdb": (io.BytesIO(_PDB), "her2.pdb"),
        }, content_type="multipart/form-data")

    assert resp.status_code == 400
    assert "twelve" in resp.get_data(as_text=True)
    mk.assert_not_called()


def test_create_target_offers_an_existing_target_with_the_same_content(client):
    """Two targets for one structure split its designs across two combined
    tables, which is exactly what targets exist to prevent."""
    _login(client)
    existing = _target(name="HER2 (already uploaded)")
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.find_target_by_sha256",
                  return_value=existing), \
            patch("blueprints.targets.create_target") as mk:
        resp = client.post("/targets", data={
            "target_pdb": (io.BytesIO(_PDB), "her2.pdb"),
        }, content_type="multipart/form-data")

    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    assert "already uploaded" in body
    assert existing.id in body
    mk.assert_not_called()


def test_create_target_honours_allow_duplicate(client):
    """Offered, never forced."""
    _login(client)
    created = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.find_target_by_sha256") as dupe, \
            patch("blueprints.targets.create_target", return_value=created):
        resp = client.post("/targets", data={
            "allow_duplicate": "1",
            "target_pdb": (io.BytesIO(_PDB), "her2.pdb"),
        }, content_type="multipart/form-data")

    assert resp.status_code == 302
    dupe.assert_not_called()


def test_target_detail_404s_for_another_users_target(client):
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=None) as fetch:
        resp = client.get(f"/targets/{uuid.uuid4()}")
    assert resp.status_code == 404
    # Owner scope must be enforced in the query, not after the fetch.
    assert fetch.call_args.kwargs["user_id"] == "u-1"


def test_target_detail_lists_only_this_targets_runs(client):
    _login(client)
    t = _target()
    mine = SimpleNamespace(
        id="c-1", name="sweep", tool="rfdiffusion", status="running",
        requested_designs=24, total_subjobs=2,
    )
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=[mine]) as fetch, \
            patch("shared.compute_campaigns.list_campaigns_for_user") as everything:
        resp = client.get(f"/targets/{t.id}")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "sweep" in body
    # The route passes the target and the caller's own id. That the id reaches
    # the QUERY rather than a post-fetch filter is proved by
    # test_list_campaigns_for_target_is_owner_scoped, not here.
    assert fetch.call_args.args[0] == t.id
    assert fetch.call_args.kwargs["user_id"] == "u-1"
    # And NOT derived from the user's global campaign list. That read is capped
    # over their entire campaign history, so a target whose runs all fell
    # outside the cap rendered "nothing has been run against this target yet"
    # for runs they had paid for.
    everything.assert_not_called()


def test_launch_hands_off_to_the_run_form_with_the_target_bound(client):
    _login(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t):
        resp = client.get(f"/targets/{t.id}/launch")
    assert resp.status_code == 302
    assert f"target_id={t.id}" in resp.headers["Location"]


def test_archive_is_owner_scoped_and_redirects(client):
    _login(client)
    tid = str(uuid.uuid4())
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.archive_target", return_value=True) as arch:
        resp = client.post(f"/targets/{tid}/archive")
    assert resp.status_code == 302
    assert arch.call_args.args == (tid, "u-1")
