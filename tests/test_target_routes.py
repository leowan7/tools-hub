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


def _agg(runs=(), **over):
    """A minimal ``aggregate_target_candidates`` envelope for route-level tests.

    The target page's runs AND its designs now come from ONE call to
    ``aggregate_target_candidates``, which binds ``get_target`` and
    ``list_campaigns_for_target`` at ITS OWN module level, so patching
    ``shared.compute_campaigns`` no longer reaches it.

    Patching the aggregator at the route boundary is the right seam rather than
    a convenience: what it RETURNS is this route's input, and what it DOES is
    covered by tests/test_aggregate_target.py. Reaching past it to patch three
    of its internals would couple these route tests to the fan-in's private
    shape.
    """
    env = {
        "ok": True, "partial": False, "candidates": [], "total": 0,
        "shown": 0, "unranked": 0, "capped": False, "columns": [],
        "tools": [], "per_tool": {}, "campaigns": list(runs),
        "standalone_jobs": 0, "refold_jobs": 0, "passed_total": 0,
        "provisional": False, "sort_mode": "percentile", "multi_tool": False,
        "limit": 300,
    }
    env.update(over)
    # The aggregator computes ``provisional`` as ``partial or any(non-terminal
    # run)``, so partial=True with provisional=False is a combination it can
    # NEVER return. A fake that allows it lets the page be tested in a state
    # production cannot reach, which is exactly how the provisional banner came
    # to assert "Not every run has finished" on a target whose runs were all
    # complete: both partial tests ran with the default provisional=False and
    # never entered that branch.
    #
    # Enforced rather than defaulted, so a test that passes the impossible pair
    # fails loudly instead of being silently corrected.
    if env["partial"] and not env["provisional"]:
        if "provisional" in over:
            raise AssertionError(
                "partial=True with provisional=False is unreachable: "
                "aggregate_target_candidates computes provisional as "
                "`partial or any(...)`. Drop the provisional override."
            )
        env["provisional"] = True
    return env


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
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg(runs=[mine])) as fetch, \
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
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg()):
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
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg()):
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
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg()):
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
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg()):
        resp = client.get(f"/targets/{t.id}")

    assert "structure is still staged" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# The empty state. Three ways to reach it, and only one of them means nothing
# happened.
#
# All four tests are required together, and none of the smaller sets works.
# Each disclosure test alone is satisfied by deleting the "nothing has been
# run" sentence outright, and the last test alone is satisfied by never
# disclosing anything. Only the pair pins "say the true thing AND stop saying
# the false one".
#
# Nothing pinned this block before Phase 3, including the draft disclosure the
# Phase 2 route was built for.
# ---------------------------------------------------------------------------

_NOTHING = "Nothing has been run against this target yet."


def _flat(body):
    """Collapse whitespace so an assertion is not pinned to template wrapping."""
    return " ".join(body.split())


def _detail(client, drafts=(), **agg_over):
    """Render the detail page and return its text with whitespace collapsed.

    Every keyword goes through to ``_agg``, so passing nothing gives the empty
    state and passing ``runs=`` / ``tools=`` / ``partial=`` gives the others.
    """
    _login(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg(**agg_over)), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=list(drafts)):
        resp = client.get(f"/targets/{t.id}")
    assert resp.status_code == 200
    return _flat(resp.get_data(as_text=True))


def test_a_target_whose_every_launch_stranded_at_draft_says_so(client):
    """A draft was never funded, so list_campaigns_for_target excludes it and
    the page has no runs to show. Telling someone who tried that nothing has
    been run is the one wrong answer, and "nothing was charged" is their first
    question."""
    drafts = [
        SimpleNamespace(id="c-1", status="draft"),
        SimpleNamespace(id="c-2", status="draft"),
    ]
    body = _detail(client, drafts=drafts)
    assert "2 runs were created against this target but never funded" in body
    assert "Nothing was charged." in body
    assert _NOTHING not in body


def test_a_refold_only_target_discloses_the_folds_instead_of_claiming_nothing_ran(client):
    """Register item A31's exact user-visible failure, reached through the other
    population. Refolds carry target_id with campaign_id NULL, so they are read,
    counted, and deliberately never ranked. The rollup line that discloses them
    is gated on a non-empty ``agg.tools``, which a refold-only target does not
    have, so without its own branch this page reads as untouched."""
    body = _detail(client, refold_jobs=3)
    assert "3 validation folds ran against this target" in body
    assert "re-measures a design that already exists" in body
    assert _NOTHING not in body


def test_a_standalone_run_that_returned_no_design_is_not_silence_either(client):
    """The non-refold half of the same population: a succeeded standalone job
    that produced no candidate record. It contributes to neither ``tools`` nor
    ``refold_jobs``, so it needs naming separately or it disappears."""
    body = _detail(client, standalone_jobs=1)
    assert "1 run finished against this target without returning a design" in body
    assert "validation fold" not in body
    assert _NOTHING not in body


def test_a_genuinely_untouched_target_does_say_nothing_has_been_run(client):
    """The anchor. Without this, deleting the sentence passes all three above."""
    body = _detail(client)
    assert _NOTHING in body
    assert "never funded" not in body
    assert "validation fold" not in body


# ---------------------------------------------------------------------------
# Round 15 (independent split QC): the partial disclosure and the empty state
#
# Every test below pins a defect an independent reviewer reproduced against the
# real route. None was caught by the four empty-state tests above, because
# those only varied the COUNTS: never `partial`, and never an empty RUN list
# beside a non-empty design table.
# ---------------------------------------------------------------------------

def _one_design():
    """Minimum a candidate row needs to survive the macro in pooled mode."""
    return [{
        "scores": {"ipTM": 0.9}, "pdb_key": "designs/d.pdb",
        "sequence": "MKTAY", "_source_tool": "bindcraft",
        "_source_job_id": "job-1", "_source_index": 0,
        "_metric_key": "ipTM", "_metric_value": 0.9,
        "_rank_percentile": 90, "_ranked": True, "_rank_position": 1,
    }]


def test_a_failed_read_is_disclosed_and_does_not_claim_nothing_was_run(client):
    """BLOCKER. The partial banner was nested inside `{% if agg.tools %}`, so
    it was structurally unreachable in the one state it exists for.

    ``_unreadable`` returns ok=True, partial=True, tools=[]. The page fell
    through to the empty state and told a paying user "Nothing has been run
    against this target yet" because a READ HAD FAILED. Three reachable seams
    produce that envelope: the standalone read raising, every campaign child
    read raising, and the ownership re-ask raising.
    """
    body = _detail(client, partial=True)
    assert "could not be read" in body
    assert _NOTHING not in body


def test_the_partial_banner_still_renders_when_there_is_a_table(client):
    """The pair. Hoisting the banner must not lose the case it already had.

    Without this, deleting the banner from the table branch and adding it to
    the empty one would pass the test above.
    """
    body = _detail(client, partial=True, tools=["bindcraft"],
                   candidates=_one_design(), total=1, shown=1)
    assert "could not be read" in body


def test_one_run_that_returned_nothing_reads_correctly(client):
    """SERIOUS. The singular branch read "but it has returned a completed
    design so far", the opposite of the sentence it belongs to, and it is the
    branch every freshly launched one-run target hits for its whole first run.
    """
    run = SimpleNamespace(id="c-1", name="sweep", tool="bindcraft",
                          status="running", requested_designs=10,
                          total_subjobs=1)
    body = _detail(client, runs=[run], tools=[])
    assert "no run has returned a completed design so far" in body
    assert "but it has returned a completed design" not in body


def test_standalone_designs_are_not_announced_as_having_returned_nothing(client):
    """SERIOUS. Zero campaigns plus standalone jobs that DID return designs.

    The run list is empty so the empty state renders, while the table renders
    below it off ``agg.tools``. Gated on ``agg.standalone_jobs`` alone, the
    panel announced "3 runs finished against this target without returning a
    design" directly above a table of those designs.
    """
    body = _detail(client, tools=["bindcraft"], standalone_jobs=3,
                   candidates=_one_design(), total=1, shown=1)
    assert "without returning a design" not in body
    assert _NOTHING not in body
    assert "3 standalone runs produced the designs listed below" in body


def test_standalone_jobs_that_really_returned_nothing_still_say_so(client):
    """The pair to the above: the branch must survive for its own real case,
    or gating it on `not agg.tools` would be indistinguishable from deleting
    it."""
    body = _detail(client, standalone_jobs=3)
    assert "3 runs finished against this target without returning a design" in body


# ---------------------------------------------------------------------------
# Round 15 minors that were false statements rather than untidiness
# ---------------------------------------------------------------------------

def _run(cid, status):
    return SimpleNamespace(id=cid, name=cid, tool="bindcraft", status=status,
                           requested_designs=10, total_subjobs=1)


def test_a_paused_run_and_a_running_run_are_both_reported(client):
    """The banner branched on paused_runs alone, so a target with one paused
    and one still-running campaign was told only that a top-up was needed. The
    reason its percentiles were about to move was silently dropped.

    They are independent facts, so both sentences render.
    """
    body = _detail(
        client, tools=["bindcraft"], provisional=True, total=1, shown=1,
        candidates=_one_design(),
        runs=[_run("c-1", "paused_insufficient_funds"), _run("c-2", "running")],
    )
    assert "paused waiting on wallet balance" in body
    assert "Not every run has finished" in body


def test_a_paused_run_alone_does_not_claim_designs_are_still_landing(client):
    """The pair. Rendering both sentences unconditionally would satisfy the
    test above while telling a user whose only run is stopped that more designs
    are on the way."""
    body = _detail(
        client, tools=["bindcraft"], provisional=True, total=1, shown=1,
        candidates=_one_design(),
        runs=[_run("c-1", "paused_insufficient_funds")],
    )
    assert "paused waiting on wallet balance" in body
    assert "Not every run has finished" not in body


def test_a_running_run_alone_still_gets_the_shifting_percentiles_sentence(client):
    body = _detail(
        client, tools=["bindcraft"], provisional=True, total=1, shown=1,
        candidates=_one_design(), runs=[_run("c-1", "running")],
    )
    assert "Not every run has finished" in body
    assert "paused waiting on wallet balance" not in body


@pytest.mark.parametrize("capped,total,shown", [(True, 900, 1), (False, 5, 5)])
def test_the_unranked_disclosure_makes_no_positional_claim(client, capped, total, shown):
    """This sentence has been wrong twice, in opposite directions.

    It said "listed last", then (round 15) "fall below the cap rather than
    appearing in the table". Round 16 showed neither survives
    ``canonical_sort_key``, whose key is
    ``(passed, unranked, -rank_fraction, ...)``. ``passed`` LEADS, so an
    unranked row nobody rejected sorts ABOVE every row its own tool marked
    failed, and ``unranked`` only sinks it within its own pass bucket. Under a
    cap it is not reliably dropped either: PER_TOOL_FLOOR reserves slots for
    every tool with passing rows, and an unranked row is ``_passed`` unless its
    own cohort filtered it. Grouping by tool moves it again.

    So the copy must claim the exclusion and nothing about position, in EVERY
    state. Parametrised over capped and uncapped because the previous two
    versions each got exactly one of those right.
    """
    body = _detail(client, tools=["bindcraft"], candidates=_one_design(),
                   total=total, shown=shown, unranked=4, capped=capped)

    assert "excluded from the percentiles" in body
    for wrong in ("listed last", "fall below the cap",
                  "rather than appearing in the table"):
        assert wrong not in body, wrong


def test_the_unranked_disclosure_is_absent_when_every_design_is_ranked(client):
    """The pair. Deleting the sentence entirely satisfies the test above."""
    body = _detail(client, tools=["bindcraft"], candidates=_one_design(),
                   total=5, shown=5, unranked=0, capped=False)
    assert "excluded from the percentiles" not in body


# ---------------------------------------------------------------------------
# Round 16: defects the round-15 FIXES introduced
#
# This repo's documented pattern is that each round's fix creates the next
# round's defect. Round 15 restructured the empty state and widened
# `provisional`; these are the states that combination got wrong.
# ---------------------------------------------------------------------------

def test_a_settled_target_with_a_failed_read_is_not_told_its_runs_are_unfinished(client):
    """Round 15 made ``provisional = partial or any(non-terminal)``, so it is
    True on a target whose every run is COMPLETE when a read failed. The banner
    then branched on ``unfinished or not paused_runs``, and the second disjunct
    converted "there is no paused run" into "there is a running one".

    Result: "Not every run has finished, so percentiles will shift as more
    designs land" rendered directly under a run strip showing every run green.
    Absence of one fact is not evidence of another.
    """
    body = _detail(
        client, tools=["bindcraft"], candidates=_one_design(), total=1, shown=1,
        partial=True, runs=[_run("c-1", "completed"), _run("c-2", "completed")],
    )
    assert "Ranking is provisional" in body
    assert "Not every run has finished" not in body
    assert "paused waiting on wallet balance" not in body
    # It must still explain ITSELF rather than trailing off after "provisional".
    assert "computed on incomplete data" in body


def test_a_provisional_target_always_gives_a_reason(client):
    """The pair for the branch above: every route into the banner names its
    own cause, so "Ranking is provisional." never stands alone."""
    running = _detail(client, tools=["bindcraft"], candidates=_one_design(),
                      total=1, shown=1, provisional=True,
                      runs=[_run("c-1", "running")])
    assert "Not every run has finished" in running
    assert "computed on incomplete data" not in running


def test_a_stranded_draft_does_not_claim_nothing_was_charged_over_real_designs(client):
    """Round 15 ordered the empty state drafts-first, so the draft branch
    preempted the designs-exist branch.

    A target with one stranded draft AND standalone runs that returned designs
    rendered "1 run was created against this target but never funded ...
    Nothing was charged." directly above a populated table. Two panels
    contradicting each other, and an unqualified money claim over designs the
    user WAS billed for, since standalone tool_jobs are wallet charged.
    """
    _login(client)
    t = _target()
    drafts = [SimpleNamespace(id="c-1", status="draft")]
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg(tools=["bindcraft"], standalone_jobs=2,
                                    candidates=_one_design(), total=1, shown=1)), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=drafts):
        body = _flat(client.get(f"/targets/{t.id}").get_data(as_text=True))

    # The attribution leads, so the page never points at designs and says nothing ran.
    assert "2 standalone runs produced the designs listed below" in body
    # The draft is still disclosed, but as an additional fact and without the
    # unqualified bold claim that reads as covering everything on screen.
    assert "never funded" in body
    assert "<strong>Nothing was charged.</strong>" not in body


def test_a_stranded_draft_with_no_designs_keeps_its_unqualified_wording(client):
    """The pair. When there is nothing else on the page, "Nothing was charged"
    is exactly true and is the user's first question."""
    drafts = [SimpleNamespace(id="c-1", status="draft")]
    body = _detail(client, drafts=drafts)
    assert "Nothing was charged." in body
    assert "produced the designs listed below" not in body


def test_a_capped_partial_table_does_not_promise_the_csv_is_complete(client):
    """The capped block states counts from the same read that set ``partial``
    and claimed "The CSV and FASTA exports contain every design". That is not
    merely unknown, it is false: the export route re-runs the same aggregate,
    so a partial read yields a short CSV served as 200 with no disclosure of
    its own."""
    body = _detail(client, tools=["bindcraft"], candidates=_one_design(),
                   total=412, shown=1, capped=True, partial=True)
    assert "cover every design that could be read" in body
    assert "The CSV and FASTA exports contain every design" not in body


def test_a_capped_complete_table_still_promises_the_csv_is_complete(client):
    """The pair: with no failed read the stronger claim is true and useful."""
    body = _detail(client, tools=["bindcraft"], candidates=_one_design(),
                   total=412, shown=1, capped=True)
    assert "The CSV and FASTA exports contain every design" in body
    assert "could be read" not in body
