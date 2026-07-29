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


def test_launch_renders_the_multi_tool_screen(client):
    """Phase 2 replaced Phase 1's redirect to the single-tool create form with
    a real screen. The single-tool form still exists and still accepts
    ``?target_id=``; it is simply no longer where this button lands.

    Coverage of the screen itself lives in
    tests/test_target_multi_launch_routes.py; this only pins that the route
    stopped redirecting."""
    _login(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t):
        resp = client.get(f"/targets/{t.id}/launch")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="tools" value="rfdiffusion"' in body
    assert f'action="/targets/{t.id}/launch"' in body


def test_archive_is_owner_scoped_and_redirects(client):
    _login(client)
    tid = str(uuid.uuid4())
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.archive_target", return_value=True) as arch:
        resp = client.post(f"/targets/{tid}/archive")
    assert resp.status_code == 302
    assert arch.call_args.args == (tid, "u-1")


def test_unarchive_restores_and_lands_on_the_target(client):
    _login(client)
    tid = str(uuid.uuid4())
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.unarchive_target", return_value=True) as un:
        resp = client.post(f"/targets/{tid}/unarchive")
    assert resp.status_code == 302
    assert un.call_args.args == (tid, "u-1")
    # Back to the target itself, not the list: the user restored it to use it.
    assert f"/targets/{tid}" in resp.headers["Location"]


def test_unarchive_of_an_unowned_target_changes_nothing_and_says_nothing(client):
    """A no-op must not confirm the id exists."""
    _login(client)
    tid = str(uuid.uuid4())
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.unarchive_target", return_value=False) as un:
        resp = client.post(f"/targets/{tid}/unarchive")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/targets")
    assert un.call_args.args == (tid, "u-1")


def test_an_archived_targets_page_offers_restore_and_not_a_dead_run_button(client):
    """A33. target_launch redirects an archived id straight back to detail, so
    rendering "Run a tool" there gave a button that silently reloaded the same
    page. Nothing told the user the target was archived either."""
    _login(client)
    t = _target(archived_at="2026-07-02T00:00:00Z")
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=[]):
        resp = client.get(f"/targets/{t.id}")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Archived" in body
    assert f"/targets/{t.id}/unarchive" in body
    # The two controls that did nothing useful on an archived target.
    assert f"/targets/{t.id}/launch" not in body
    assert f"/targets/{t.id}/archive" not in body


def test_a_live_targets_page_still_offers_run_and_archive(client):
    """The other half of the branch: the normal page must be unchanged."""
    _login(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=[]):
        resp = client.get(f"/targets/{t.id}")

    body = resp.get_data(as_text=True)
    assert f"/targets/{t.id}/launch" in body
    assert f"/targets/{t.id}/archive" in body
    assert f"/targets/{t.id}/unarchive" not in body


def test_the_targets_list_surfaces_archived_targets_so_restore_is_reachable(client):
    """Archiving redirects to the list, which excludes archived targets. Without
    this section the restore control exists but nothing links to it."""
    _login(client)
    live = _target(name="live-one")
    gone = _target(name="archived-one", archived_at="2026-07-02T00:00:00Z")

    def _list(user_id, **kw):
        return [gone] if kw.get("archived_only") else [live]

    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.list_targets_for_user", side_effect=_list):
        resp = client.get("/targets")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "live-one" in body and "archived-one" in body
    assert f"/targets/{gone.id}/unarchive" in body
    # The archived one must not offer a launch link that would bounce.
    assert f"/targets/{gone.id}/launch" not in body


def test_restoring_from_the_list_returns_to_the_list(client):
    """Restoring from the list usually means restoring several. Bouncing
    through each target's detail page makes that N round trips."""
    _login(client)
    tid = str(uuid.uuid4())
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.unarchive_target", return_value=True):
        resp = client.post(f"/targets/{tid}/unarchive",
                           data={"return_to": "list"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/targets")


def test_return_to_is_matched_against_a_literal_not_used_as_a_url(client):
    """It decides between two known endpoints. If it ever reached redirect()
    as a value this would be an open redirect."""
    _login(client)
    tid = str(uuid.uuid4())
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.unarchive_target", return_value=True):
        resp = client.post(f"/targets/{tid}/unarchive",
                           data={"return_to": "https://evil.example/x"})
    assert resp.status_code == 302
    assert "evil.example" not in resp.headers["Location"]
    assert f"/targets/{tid}" in resp.headers["Location"]


def _list_page(client, *, live=0, archived=0):
    """Render /targets with N live and M archived rows AVAILABLE.

    The route asks for one row more than it renders, so these fakes must
    honour the limit they are handed. Returning everything regardless would
    hide the off-by-one this exists to pin.
    """
    live_rows = [_target(name=f"live-{i}") for i in range(live)]
    arch_rows = [_target(name=f"arch-{i}", archived_at="2026-07-02T00:00:00Z")
                 for i in range(archived)]

    def _list(user_id, **kw):
        rows = arch_rows if kw.get("archived_only") else live_rows
        return rows[:kw.get("limit", 100)]

    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.list_targets_for_user", side_effect=_list):
        return client.get("/targets").get_data(as_text=True)


def test_a_truncated_archived_section_says_so_rather_than_hiding_rows(client):
    """This section is the only route to an archived target short of a URL, so
    a row the page cannot show is indistinguishable from a deleted one."""
    _login(client)
    body = _list_page(client, archived=101)
    assert "most recently archived" in body
    assert "not deleted" in body


def test_an_exactly_full_archived_section_claims_no_hidden_rows(client):
    """The off-by-one. At exactly the cap there is nothing older, so claiming
    "anything older is still stored" invents rows that do not exist. Only the
    extra row the route reads can tell the two cases apart."""
    _login(client)
    assert "most recently archived" not in _list_page(client, archived=100)


def test_a_short_archived_section_makes_no_claim_about_a_cap(client):
    _login(client)
    assert "most recently archived" not in _list_page(client, archived=1)


def test_a_truncated_live_list_says_so_too(client):
    """The live list is capped by the same constant and paginates just as
    little; it needs the same disclosure, not only the archived one."""
    _login(client)
    body = _list_page(client, live=101)
    assert "most recent targets" in body


def test_an_exactly_full_live_list_claims_no_hidden_rows(client):
    _login(client)
    assert "most recent targets" not in _list_page(client, live=100)


def test_the_page_renders_only_the_cap_even_when_more_are_available(client):
    """The extra row is a probe, not content. Rendering it would make the
    page one longer than the banner says it is."""
    _login(client)
    body = _list_page(client, archived=101)
    assert "arch-99" in body
    assert "arch-100" not in body


def test_archiving_your_only_target_does_not_say_you_have_none(client):
    """A user who archived their last target is not a new user, and the
    onboarding empty state renders directly above that target's own card."""
    _login(client)
    body = _list_page(client, live=0, archived=1)
    assert "No targets yet" not in body
    assert "No live targets" in body


def test_a_genuinely_new_user_still_gets_the_onboarding_empty_state(client):
    _login(client)
    body = _list_page(client, live=0, archived=0)
    assert "No targets yet" in body
    assert "No live targets" not in body


def test_an_archived_target_with_no_structure_does_not_promise_one(client):
    """create_target accepts upload=None, so storage_path can be NULL. The
    subtitle already renders "No structure staged"; claiming the structure is
    still staged contradicts the same page, and every launch path rejects a
    target with no path, so "restoring makes it launchable" is false too."""
    _login(client)
    t = _target(archived_at="2026-07-02T00:00:00Z", storage_path=None,
                filename=None)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=[]):
        resp = client.get(f"/targets/{t.id}")

    body = resp.get_data(as_text=True)
    assert "structure is still staged" not in body
    assert "no staged structure" in body
    # Restore is still offered: the row and its runs are worth recovering.
    assert f"/targets/{t.id}/unarchive" in body


def test_an_archived_target_with_a_structure_still_says_it_is_staged(client):
    _login(client)
    t = _target(archived_at="2026-07-02T00:00:00Z")
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=[]):
        resp = client.get(f"/targets/{t.id}")

    assert "structure is still staged" in resp.get_data(as_text=True)
