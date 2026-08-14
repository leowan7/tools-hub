"""Route tests for /targets/*.

Auth, persistence, and Storage are mocked; this covers wiring, ownership, and
the duplicate-upload offer.
"""

from __future__ import annotations

import io
import re
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# These tests assert ownership and isolation, so they must not consult the
# live database that app.py's load_dotenv() would otherwise hand them.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared.targets import (
    TARGET_READ_ABSENT,
    TARGET_READ_OK,
    TARGET_READ_UNAVAILABLE,
    DesignTarget,
    TargetRead,
)


def _read_ok(target):
    """What ``blueprints.targets.read_target`` answers for a row that is there.

    ``target_detail`` resolves its parent through the THREE-outcome read
    (register item A90), so a read that never completed is distinguishable from
    a row that is not there and no longer renders 404 at a user looking at their
    own target. The other /targets/* routes still resolve through
    ``get_target``, which is why only the detail-page tests patch this one.
    """
    return TargetRead(target, TARGET_READ_OK)


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
        "limit": 300, "split_tools": [],
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


def test_create_target_rejects_a_chain_qualified_hotspot(client):
    """PLAIN INTEGERS ONLY ON THIS ROUTE, and it is not a limitation, it is the
    fix for a P0.

    Whatever is stored here is prefilled by ``target_defaults_for_form`` into
    the ONE shared ``hotspot_residues`` field the launch screen posts to EVERY
    selected tool. Accepting "A241" here was executed against the real
    ``_collect_launch_specs``: rfdiffusion, bindcraft, boltzgen and pxdesign
    refuse a token naming a chain the run does not target, and
    ``tools/rfantibody`` parses it with a bare ``int(tok)`` and refuses a prefix
    on ANY target chain — and the launch route is all-or-nothing.

    A protomer reaches proteina through proteina's own ``chain_hotspots``
    field, and reaches the target through
    ``shared.targets.enrich_target_hotspot_spec``. Never through this form.
    """
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.create_target") as mk:
        resp = client.post("/targets", data={
            "target_chain": "A",
            "hotspot_residues": "A241, B241",
            "target_pdb": (io.BytesIO(_PDB), "her2.pdb"),
        }, content_type="multipart/form-data")

    assert resp.status_code == 400
    assert "A241" in resp.get_data(as_text=True)
    mk.assert_not_called()


def test_create_target_still_rejects_a_chain_prefixed_epitope(client):
    """Only the hotspot field opted in. The epitope field feeds IgGM, whose
    parser has never read a prefix, so accepting one here would store a value
    that silently means nothing downstream."""
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.create_target") as mk:
        resp = client.post("/targets", data={
            "epitope_residues": "A32",
            "target_pdb": (io.BytesIO(_PDB), "her2.pdb"),
        }, content_type="multipart/form-data")

    assert resp.status_code == 400
    assert "A32" in resp.get_data(as_text=True)
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


# The "/targets/new must not teach a token this route rejects" guard lives in
# tests/test_multichain_form_affordances.py::
# test_targets_new_residue_examples_parse_on_its_own_route, and only there.
#
# A second check used to sit here, asserting `"Prefix the chain" not in ...`
# and `"A241" not in ...` inside a 600-char window after the field label. It
# was deleted rather than reworded because it was measured and found vacuous:
# with the exact copy that shipped the defect restored into
# templates/targets/new.html ("... prefix the chain to name another (A296,
# B264).", removed by d398782, which is on main), it went on PASSING while the
# property test FAILED. Its two literals cannot match that copy -- lowercase
# "prefix", and the token was A296/B264, never A241 -- so it could not fail for
# the reason its own docstring gave. The property test extracts every residue
# example from the whole containing <form>, including placeholder/title/
# data-tooltip/aria-label, and feeds each to `_parse_residue_list`, the parser
# this route really uses; `input_named` fails there too if the field is gone.


def test_target_detail_404s_for_another_users_target(client):
    """ABSENT still 404s, and that is the half of the three-outcome read that
    did not change (register item A90). A read that COMPLETED and matched no row
    is a permanent verdict about the row -- it does not exist, or it is not this
    caller's -- and 404 is what that means. Only the third outcome moved."""
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target",
                  return_value=TargetRead(None, TARGET_READ_ABSENT)) as fetch:
        resp = client.get(f"/targets/{uuid.uuid4()}")
    assert resp.status_code == 404
    # Owner scope must be enforced in the query, not after the fetch.
    assert fetch.call_args.kwargs["user_id"] == "u-1"


def test_target_detail_503s_when_the_read_did_not_complete(client):
    """THE REGRESSION A90'S OWN FIX CREATED, and this is where it is pinned.

    The submit gate refuses an unreadable parent by redirecting to this page
    with `?handoff=unverified`, and the browser follows that redirect in
    milliseconds, with nothing to say the fault has passed by then. This route
    used to render 404 for it, telling a user that the target they had just been
    looking at does not exist. Before A90 that same user landed on the targets
    list with a 200 and no message, so the 404 was a new, worse answer
    introduced by the fix.

    503, not 404 and not 200: 404 is a claim about the row that a read which
    never answered cannot make, and 200 would present a page carrying none of
    the target's content as the target's page.
    """
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target",
                  return_value=TargetRead(None, TARGET_READ_UNAVAILABLE)):
        resp = client.get(f"/targets/{uuid.uuid4()}")
    assert resp.status_code == 503
    body = _flat(resp.get_data(as_text=True))
    assert "We could not load this target." in body
    # And it must not say the thing 404.html says.
    assert "The page you're looking for doesn't exist" not in body


def test_the_503_page_still_carries_the_handoff_reason(client):
    """WITHOUT THIS THE FIX DROPS THE REFUSAL REASON ON ITS OWN OUTPUT.

    `?handoff=unverified` is how the submit gate tells the user it refused
    rather than ignored their shortlist. It rides the query string, and
    `_submit_target_shortlist` sends it to THIS URL, so a page that read the
    target first and rendered an error without consulting the query string
    would drop it on the very request A90's own unavailable exit produces.
    Nothing here measures how often that request is the one that arrives.
    """
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target",
                  return_value=TargetRead(None, TARGET_READ_UNAVAILABLE)):
        resp = client.get(f"/targets/{uuid.uuid4()}?handoff=unverified")
    assert resp.status_code == 503
    raw = resp.get_data(as_text=True)
    body = _flat(raw)
    assert "Nothing was sent to the lab." in body
    assert ("could not confirm that the designs you starred belong to this "
            "target") in body
    # And the retry link carries the reason forward, so the reload this page
    # asks for lands on the real page WITH the banner rather than without it.
    assert "?handoff=unverified" in raw


def test_the_503_page_still_reports_what_the_size_cap_discarded(client):
    """THE SECOND BANNER PARAGRAPH, which is a different fact from the reason.

    `_submit_target_shortlist` appends `&truncated=N` to every failure exit
    including the unreadable-parent one, so the redirect that lands here can
    carry both. The reason paragraph and this one are separate `{% if %}`s in
    templates/unavailable.html, and the page carried the first with nothing
    asserting the second: deleting the truncation block left the whole suite
    green.

    The count is the one number on this page the user cannot recover by
    reloading -- the refs past the cap were never read, so nothing on the
    reloaded target page can tell them how many there were.
    """
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target",
                  return_value=TargetRead(None, TARGET_READ_UNAVAILABLE)):
        resp = client.get(
            f"/targets/{uuid.uuid4()}?handoff=unverified&truncated=120"
        )
    assert resp.status_code == 503
    body = _flat(resp.get_data(as_text=True))
    assert "up to 120 of your starred designs were over the per-request limit" in body
    # The pair: no `?truncated=` means no second paragraph, so the assertion
    # above cannot be satisfied by an unconditional sentence.
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target",
                  return_value=TargetRead(None, TARGET_READ_UNAVAILABLE)):
        plain = client.get(f"/targets/{uuid.uuid4()}?handoff=unverified")
    # The status assertion is what stops the negative half passing vacuously:
    # without it a 500 on this request satisfies `not in` while proving nothing.
    assert plain.status_code == 503
    assert "over the per-request limit" not in _flat(plain.get_data(as_text=True))


def test_the_503_page_renders_no_banner_for_a_crafted_handoff(client):
    """THE PAIR. The whitelist runs before the read now, so it has to still be
    a whitelist: an unknown value must render no banner at all rather than the
    `{% else %}` arm's "your request could not be submitted", which would let
    any link tell a user their submission failed."""
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target",
                  return_value=TargetRead(None, TARGET_READ_UNAVAILABLE)):
        resp = client.get(f"/targets/{uuid.uuid4()}?handoff=' or 1=1--")
    assert resp.status_code == 503
    body = _flat(resp.get_data(as_text=True))
    assert "Nothing was sent to the lab" not in body
    assert "Your request could not be submitted" not in body


def test_target_detail_lists_only_this_targets_runs(client):
    _login(client)
    t = _target()
    mine = SimpleNamespace(
        id="c-1", name="sweep", tool="rfdiffusion", status="running",
        requested_designs=24, total_subjobs=2,
    )
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target", return_value=_read_ok(t)), \
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


def test_target_detail_renders_the_chain_qualified_hotspots(client):
    """THE READ-SIDE HALF OF THE PERSISTENCE FIX, and nothing else pinned it.

    ``hotspot_residues`` is ``integer[]``, so on an Fc homodimer — both chains
    numbered 234-444 — it holds ``[241, 241]`` with the protomer stripped out.
    Rendering that column instead of ``effective_hotspots`` puts "241, 241" on
    the page for two hotspots the user pinned to different protomers, and the
    page then disagrees with both the launch prefill and the run that target
    will actually produce.

    Asserted against the RENDERED span rather than the template text: the
    template names the property, but only the render proves the property is
    what reaches the page.
    """
    _login(client)
    t = _target(
        target_chain="A B",
        hotspot_residues=[241, 241],
        hotspot_spec=["A241", "B241"],
    )
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target", return_value=_read_ok(t)), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_agg()), \
            patch("shared.compute_campaigns.list_campaigns_for_user"):
        resp = client.get(f"/targets/{t.id}")

    assert resp.status_code == 200
    body = _flat(resp.get_data(as_text=True))
    shown = re.search(r"Hotspots:\s*<span[^>]*>(.*?)</span>", body)
    assert shown, "the target page no longer renders a Hotspots row"
    assert shown.group(1).strip() == "A241, B241", (
        "the detail page dropped the protomer off the stored hotspots")


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
            patch("blueprints.targets.read_target", return_value=_read_ok(t)), \
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
            patch("blueprints.targets.read_target", return_value=_read_ok(t)), \
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
            patch("blueprints.targets.read_target", return_value=_read_ok(t)), \
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
            patch("blueprints.targets.read_target", return_value=_read_ok(t)), \
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


def _detail(client, drafts=(), query="", **agg_over):
    """Render the detail page and return its text with whitespace collapsed.

    Every keyword goes through to ``_agg``, so passing nothing gives the empty
    state and passing ``runs=`` / ``tools=`` / ``partial=`` gives the others.

    ``query`` is appended to the URL, and the aggregate is patched with a
    SIDE EFFECT rather than a fixed return value so that ``?sort=`` is not
    inert. The route reads ``?sort`` off the query string, validates it and
    passes it to ``aggregate_target_candidates``; the template then reads the
    mode back off the ENVELOPE (``sort_mode=agg["sort_mode"]``), so a patch
    returning a frozen envelope swallows the query string entirely and the
    grouped-mode tests below were really being driven by their own
    ``sort_mode=`` kwarg -- deleting ``{query}`` from the URL left them green.
    Reflecting the kwarg back is what makes the round trip load-bearing.

    A caller that passes ``sort_mode=`` explicitly still wins, because those
    tests are about what the TEMPLATE does with a mode rather than about how it
    got there.
    """
    _login(client)
    t = _target()

    def _aggregate(_target_id, **kwargs):
        env = _agg(**agg_over)
        if "sort_mode" not in agg_over:
            env["sort_mode"] = kwargs.get("sort_mode") or env["sort_mode"]
        return env

    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target", return_value=_read_ok(t)), \
            patch("blueprints.targets.aggregate_target_candidates",
                  side_effect=_aggregate), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=list(drafts)):
        resp = client.get(f"/targets/{t.id}{query}")
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
    # Scoped to the drafts. Round 17 removed the unqualified variant entirely;
    # see test_a_stranded_draft_never_preempts_another_fact.
    assert "Nothing was charged for those." in body
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
    # With no table, the banner must not point at one (round 17).
    assert "this page is missing runs, designs, or both" in body
    assert "what is shown below is incomplete" not in body


def test_the_partial_banner_still_renders_when_there_is_a_table(client):
    """The pair. Hoisting the banner must not lose the case it already had.

    Without this, deleting the banner from the table branch and adding it to
    the empty one would pass the test above.

    ROUND 17: this asserted "could not be read", which THREE separate sentences
    on this page now contain (the banner, the empty-state paragraph, and the
    third provisional sentence round 16 added). Deleting the banner outright
    left it green, so the round-15 BLOCKER fix was pinned by nothing. It now
    asserts the banner's own wording.
    """
    body = _detail(client, partial=True, tools=["bindcraft"],
                   candidates=_one_design(), total=1, shown=1)
    assert "what is shown below is incomplete" in body
    assert "Reload to try again" in body


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


def _paragraph_containing(body, needle):
    """The whole ``<p>`` holding ``needle``, whitespace collapsed.

    A containment assertion cannot see a sentence added IN FRONT of the text it
    pins, which is exactly how the unranked disclosure carried a positional
    claim through a round of review that was looking for one. Extracting the
    paragraph turns the assertion into an equality, so a clause added at either
    end fails.
    """
    i = body.index(needle)
    start = body.index(">", body.rindex("<p", 0, i)) + 1
    return " ".join(body[start:body.index("</p>", i)].split())


@pytest.mark.parametrize("capped,total,shown", [(True, 900, 1), (False, 5, 5)])
def test_the_unranked_disclosure_makes_no_positional_claim(
    client, capped, total, shown,
):
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

    ROUND 17. A THIRD version then said "ranked below the scored designs they
    share a filter verdict with", and this test was green over it, because it
    only rejected the two DEAD phrasings and never rendered the grouped mode.
    That claim is true in canonical order and false under ``?sort=tool``, which
    the same page offers: grouping by slug puts an unranked row of an
    alphabetically earlier tool above the scored rows of every later one.

    ROUND 18. The round-17 rewrite anchored only the END of the sentence, so a
    positional claim added as a PRECEDING sentence inside the same paragraph
    still passed, and dropping the deny-list lost coverage of the two historical
    phrasings outright. An equality and a deny-list fail on different mutations
    and both are cheap, so this keeps both, and it compares the two sort modes
    to each other rather than merely rendering each.
    """
    def render(mode):
        return _detail(client, tools=["bindcraft", "boltzgen"], multi_tool=True,
                       candidates=_one_design(), total=total, shown=shown,
                       unranked=4, capped=capped, sort_mode=mode)

    expected = (
        "4 designs carry no value for their tool's ranking metric, so they are "
        "excluded from the percentiles. The CSV export lists every design."
    )
    paragraphs = {}
    for mode in ("percentile", "tool"):
        body = render(mode)
        paragraphs[mode] = _paragraph_containing(
            body, "excluded from the percentiles")
        # Equality, not containment: anchored at BOTH ends.
        assert paragraphs[mode] == expected, mode
        for wrong in ("listed last", "fall below the cap",
                      "rather than appearing in the table", "ranked below"):
            assert wrong not in body, f"{mode}: {wrong}"

    # The invariant this test is NAMED for. A sentence that states no position
    # cannot depend on the ordering, so the two modes must render it
    # identically; anything gated on sort_mode fails right here.
    assert paragraphs["percentile"] == paragraphs["tool"]


def test_one_tool_at_two_presets_does_not_render_cross_tool_copy(client):
    """ROUND 18. A75 widened ``multi_tool`` to mean "more than one COHORT",
    which one tool at two presets satisfies, and left every consumer of the flag
    still speaking tool-language.

    The page then printed "N designs from 1 tool" eight lines above "Different
    tools score on different scales", offered a "Grouped by tool" toggle that
    cannot reorder anything (``apply_sort_mode`` keys SORT_TOOL on
    ``_source_tool`` alone, so with one tool it returns the canonical order it
    was already in), and showed one slug on every row of the Tool column.

    The pooled COLUMNS were right and stay keyed on the cohort flag. Only the
    cross-tool prose and the toggle move to ``len(tools)``.
    """
    body = _detail(client, tools=["proteina"], multi_tool=True,
                   split_tools=["proteina"], candidates=_one_design(),
                   total=1, shown=1)
    assert "from 1 tool" in body
    assert "Different tools score on different scales" not in body
    assert "One tool at two presets does not score on one scale" in body
    # A control that cannot change the table must not be rendered.
    assert "Grouped by tool" not in body


def test_several_tools_still_get_the_cross_tool_copy_and_the_toggle(client):
    """The pair. Suppressing both for everyone passes the test above."""
    body = _detail(client, tools=["bindcraft", "boltzgen"], multi_tool=True,
                   candidates=_one_design(), total=1, shown=1)
    assert "Different tools score on different scales" in body
    assert "Grouped by tool" in body


# The class appears in the macro's <style> block as `.cand-group-row td`, so an
# assertion on the bare slug is true of every render. Only the attribute is
# unique to an actual header row.
_GROUP_ROW = 'class="cand-group-row"'


def test_a_pasted_sort_tool_url_draws_no_group_header_on_a_one_tool_target(client):
    """ROUND 19 (B-1), the route half. The toggle is correctly hidden at one
    tool by the test above, but ``target_detail`` reads ``?sort`` straight off
    the query string, so a pasted or bookmarked URL still reaches grouped
    mode. The macro gated its group headers on ``multi_tool``, which is True
    here because two presets are two cohorts, so this drew a lone header over
    rows ``apply_sort_mode`` had returned in percentile order.

    ROUND 20. No ``sort_mode=`` kwarg here on purpose: the mode has to arrive
    through ``?sort``, be validated by the route and come back out of the
    aggregate, because reaching grouped mode from a pasted URL is the entire
    claim. Passing it directly, as this test used to, exercised the template
    and left the route's half untested.
    """
    rows = [dict(_one_design()[0], _source_tool="proteina",
                 _source_preset=preset, _source_index=i)
            for i, preset in enumerate(("protein_binder", "ligand_binder"))]
    body = _detail(client, query="?sort=tool", tools=["proteina"],
                   multi_tool=True, split_tools=["proteina"],
                   candidates=rows, total=2, shown=2,
                   per_tool={"proteina": {"total": 2, "shown": 2,
                                          "cohort_n": 2, "unranked": 0}})
    assert _GROUP_ROW not in body


def test_two_tools_under_sort_tool_do_draw_group_headers(client):
    """The pair, and the half that makes ``?sort=tool`` load-bearing: drop the
    query string and this one goes red, because grouped mode is then never
    reached at all. Never drawing a header satisfies the test above, and would
    silently delete A82."""
    rows = [dict(_one_design()[0], _source_tool=tool, _source_index=i)
            for i, tool in enumerate(("bindcraft", "boltzgen"))]
    body = _detail(client, query="?sort=tool",
                   tools=["bindcraft", "boltzgen"], multi_tool=True,
                   candidates=rows, total=2, shown=2,
                   per_tool={"bindcraft": {"total": 1, "shown": 1,
                                           "cohort_n": 1, "unranked": 0},
                             "boltzgen": {"total": 1, "shown": 1,
                                          "cohort_n": 1, "unranked": 0}})
    assert body.count(_GROUP_ROW) == 2, body.count(_GROUP_ROW)


def test_an_unknown_pasted_sort_mode_falls_back_to_the_default_mode(client):
    """The route validates ``?sort`` against ``SORT_MODES`` before it goes
    anywhere, and the aggregate reflects back what it was given. A stale link
    from an older version of this page renders in the default mode rather than
    grouping on a mode nothing implements.

    ROUND 21. This asserted ONLY ``_GROUP_ROW not in body``, which is true of
    any mode that is not exactly ``'tool'`` -- including ``'nonsense'`` itself.
    Deleting both validation lines from ``target_detail`` therefore survived
    the whole suite under the name of the test that exists to pin them. The
    assertions below are ones only the fallback can produce:

      * The "Best first" toggle is ACTIVE. The template marks it ``btn-primary``
        on ``sort_mode == 'percentile'`` and ``btn-secondary`` otherwise, so an
        unvalidated mode reaches a page where neither order is shown as the one
        in effect.
      * ``nonsense`` appears in no URL on the page. The macro builds the three
        export links as ``?sort=`` ~ the mode it was handed, so without the
        fallback the page hands the user download links carrying a mode the
        export route would itself have to reject.
    """
    rows = [dict(_one_design()[0], _source_tool=tool, _source_index=i)
            for i, tool in enumerate(("bindcraft", "boltzgen"))]
    body = _detail(client, query="?sort=nonsense",
                   tools=["bindcraft", "boltzgen"], multi_tool=True,
                   candidates=rows, total=2, shown=2,
                   per_tool={"bindcraft": {"total": 1, "shown": 1,
                                           "cohort_n": 1, "unranked": 0},
                             "boltzgen": {"total": 1, "shown": 1,
                                          "cohort_n": 1, "unranked": 0}})
    assert _GROUP_ROW not in body
    assert "sort=nonsense" not in body

    label = body.index(">Best first<")
    anchor = body[body.rindex("<a ", 0, label):label]
    assert 'class="btn-primary"' in anchor, anchor


def test_a_split_cohort_row_names_its_preset(client):
    """The Tool column prints a slug, and for a tool that ran at two presets the
    slug does not identify the population the row was ranked against."""
    cand = dict(_one_design()[0], _source_tool="proteina",
                _source_preset="motif_ame", _metric_key="total_reward",
                _metric_value=12.4)
    body = _detail(client, tools=["proteina"], multi_tool=True,
                   split_tools=["proteina"], candidates=[cand],
                   total=1, shown=1)
    assert "motif_ame" in body


def test_an_unsplit_tool_gets_no_preset_chip(client):
    """The pair. Where the tool IS the cohort, a preset on every row is noise,
    and rendering it unconditionally would pass the test above."""
    cand = dict(_one_design()[0], _source_tool="proteina",
                _source_preset="motif_ame", _metric_key="total_reward",
                _metric_value=12.4)
    body = _detail(client, tools=["proteina", "bindcraft"], multi_tool=True,
                   split_tools=[], candidates=[cand], total=1, shown=1)
    assert "motif_ame" not in body


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
            patch("blueprints.targets.read_target", return_value=_read_ok(t)), \
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


@pytest.mark.parametrize("over,other_fact", [
    ({"refold_jobs": 3}, "3 validation folds ran against this target"),
    ({"standalone_jobs": 2},
     "2 runs finished against this target without returning a design"),
    ({"partial": True}, "this may not be the whole picture"),
])
def test_a_stranded_draft_never_preempts_another_fact(client, over, other_fact):
    """ROUND 17. Round 16 hoisted ``agg.tools`` above ``draft_count`` and left
    the identical defect on every branch BELOW drafts.

    A stranded draft beside billed standalone runs or refolds still preempted
    them: the disclosure vanished from the page entirely and the unqualified
    bold "Nothing was charged." was the only money statement on a target whose
    standalone jobs reserve a wallet hold and whose refolds bill on the
    completion path. Drafts also preempted the failed-read sentence.

    The predecessor of this test asserted the unqualified wording under the
    docstring premise "when there is nothing else on the page", and never
    constructed a case where there was.
    """
    drafts = [SimpleNamespace(id="c-1", status="draft")]
    body = _detail(client, drafts=drafts, **over)
    assert "never funded" in body
    assert other_fact in body
    assert "Nothing was charged for that." in body
    assert "<strong>Nothing was charged.</strong>" not in body
    assert _NOTHING not in body


@pytest.mark.parametrize("bits", range(32))
def test_no_empty_state_fact_preempts_another(client, bits):
    """The WHOLE matrix, because this block has now been wrong three times and
    every time the case that broke was a pair nobody had rendered together.

    Round 15 ordered it drafts-first. Round 16 hoisted ``agg.tools`` above
    drafts and left drafts preempting the three branches below. Each fix was
    verified against the pairs someone thought of. Five independent facts is 32
    states, which is small enough to simply enumerate, so the next person does
    not have to think of the right pair either.

    Each fact asserts ONLY its own presence. Nothing here says where a
    paragraph sits or what it sits beside: that would re-import the ordering
    assumption this test exists to retire.
    """
    tools = ["bindcraft"] if bits & 1 else []
    partial = bool(bits & 2)
    standalone = 2 if bits & 4 else 0
    refold = 3 if bits & 8 else 0
    drafts = [SimpleNamespace(id="c-1", status="draft")] if bits & 16 else []

    body = _detail(
        client, drafts=drafts, tools=tools, partial=partial,
        standalone_jobs=standalone, refold_jobs=refold,
        candidates=_one_design() if tools else [],
        total=1 if tools else 0, shown=1 if tools else 0,
    )

    if tools:
        assert "No campaign runs yet" in body
    if partial:
        assert "could not be read" in body
    if drafts:
        assert "never funded" in body
        # Scoped in EVERY one of the 32 states, which is the property that
        # makes a single draft paragraph sufficient.
        assert "Nothing was charged for that." in body
    if (refold or standalone) and not tools:
        assert ("without returning a design" in body
                or "validation fold" in body)

    # The one sentence that must never coexist with any other fact.
    nothing_claimed = _NOTHING in body
    assert nothing_claimed == (
        not tools and not partial and not standalone and not refold
        and not drafts
    )
    # The unqualified money claim is gone from the template outright.
    assert "<strong>Nothing was charged.</strong>" not in body


def test_the_draft_money_claim_is_scoped_even_as_the_only_fact(client):
    """The pair, and the reason there is only ONE draft paragraph now.

    "Nothing was charged." is true here and was kept for that reason, which is
    how two variants of this paragraph came to exist and how the wrong one came
    to render. The scoped form is true in EVERY state, so keeping the stronger
    one bought nothing and cost a branch that had to be maintained in step.
    """
    drafts = [SimpleNamespace(id="c-1", status="draft")]
    body = _detail(client, drafts=drafts)
    assert "Nothing was charged for that." in body
    assert "<strong>Nothing was charged.</strong>" not in body
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


def test_a_capped_table_says_its_row_numbers_do_not_match_the_export(client):
    """ROUND 17. The page's # column and the export's ``rank`` column number
    the same target from different bases.

    The page ranks with DEFAULT_LIMIT and prints the display index; the CSV and
    FASTA rank the whole set. They agree until ``select_under_cap``'s per-tool
    floor reserves a row from beyond the cap, which is precisely the case the
    floor exists for, and then "row 298" on screen and "rank 298" in the file
    are two different molecules. ``shared/exports.py::export_key`` asserted the
    opposite in its own docstring.

    Said here rather than fixed by renumbering, because ``export_key`` is
    shared with the campaign export and its ``rank`` column is a shipped
    contract. Naming the join key is the useful half.

    ROUND 18: the sentence used to say the files "number every design from 1",
    which the FASTA falsifies. ``candidates_to_fasta`` skips rows with no
    sequence but still numbers from the full list, so not every design appears
    and the numbering has gaps.
    """
    body = _detail(client, tools=["bindcraft"], candidates=_one_design(),
                   total=412, shown=1, capped=True)
    assert "rank the whole set rather than this page's top" in body
    assert "Match rows on the source job and file name" in body


def test_an_uncapped_table_makes_no_claim_about_export_numbering(client):
    """The pair. Uncapped, the two numberings DO agree, so the warning would be
    false and would send a reader looking for a mismatch that is not there."""
    body = _detail(client, tools=["bindcraft"], candidates=_one_design(),
                   total=1, shown=1, capped=False)
    assert "rank the whole set rather than this page's top" not in body


# ---------------------------------------------------------------------------
# shared/targets.py::campaign_ids_for_target -- the PAGE-BOUND exit
#
# THIS TEST IS IN THE WRONG FILE, deliberately. Its siblings live in
# tests/test_targets.py, which a concurrent change owns this round, so adding
# it there would have corrupted that work. Move it back beside
# test_campaign_ids_for_target_pages_past_the_row_clamp at the first chance.
#
# That clamp test seeds 2400 rows -- five pages of 500 -- so it leaves the loop
# through the SHORT-PAGE exit and asserts complete=True. Nothing exercised the
# other exit: flipping the `return ids, False` after `_MAX_PAGES` to True was
# green across the whole suite. It is the exit that matters more. A transient
# read failure also reports False and a retry clears it, but a target with more
# than _PAGE_SIZE * _MAX_PAGES runs can NEVER be read completely, so the lab
# handoff upstream refuses the shortlist on every attempt with nothing the user
# can do about it. Reporting True there would instead admit a membership test
# built on a prefix, and silently narrow a wet-lab order.
# ---------------------------------------------------------------------------

class _AlwaysFullPage:
    """A Supabase client whose every ranged read returns a full page, forever.

    Deliberately NOT a second copy of the PostgREST fake in
    tests/test_targets.py. The only behaviour under test is "the loop ran out
    of pages"; a duplicated fake is exactly the kind of thing that drifts from
    the original and then models a backend neither of them has.
    """

    def __init__(self):
        self.reads = 0
        self._rows: list = []

    # The whole query chain collapses onto self.
    def table(self, _name):
        return self

    def select(self, *_a, **_kw):
        return self

    def eq(self, *_a, **_kw):
        return self

    def order(self, *_a, **_kw):
        return self

    def range(self, start, end):
        self.reads += 1
        self._rows = [{"id": f"c-{i:06d}"} for i in range(start, end + 1)]
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows)


def test_campaign_ids_for_target_reports_the_page_bound_as_incomplete():
    from shared import targets as st

    client = _AlwaysFullPage()
    with patch("shared.targets.get_service_client", return_value=client):
        ids, complete = st.campaign_ids_for_target("t-1", user_id="u-1")

    assert client.reads == st._MAX_PAGES, client.reads
    assert len(ids) == st._PAGE_SIZE * st._MAX_PAGES
    assert complete is False, (
        "the page bound was hit, so these ids are a PREFIX of the real set; "
        "True here admits a membership test that can only answer 'not yours'")
