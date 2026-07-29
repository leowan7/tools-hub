"""Launching N tools against one target in a single gated action (Phase 2).

The route under test spends money, so these pin the ORDER of operations as
hard as the outcomes: validate everything first, gate once, create every run
as an inert draft, and only then fund and drive. A test that merely checks a
kwarg reached a mock would pass against most of the ways this can go wrong.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# These exercise real routes through a real create_app(), and app.py calls
# load_dotenv() at import, so without this fixture every read reaches the
# PRODUCTION Supabase project.
pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared.compute_campaigns import PREAUTH_INSUFFICIENT, PREAUTH_OK
from shared.targets import DesignTarget


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
        kind="pdb",
        name="HER2",
        filename="her2.pdb",
        storage_path="u-1/target-abc/her2.pdb",
        target_chain="A",
        hotspot_residues=[42, 88],
        epitope_residues=[32, 45],
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


def _squash(html: str) -> str:
    """Collapse whitespace runs so attribute assertions can be contiguous.

    The shared field_group macros render one attribute per line, so asserting
    on `name="x" value="y"` against the raw body silently never matches and the
    test passes only because the NEGATIVE half of the assertion held. Squashing
    lets a test name the exact attributes it means.
    """
    return " ".join(html.split())


def _campaign(tool="rfdiffusion"):
    return SimpleNamespace(id=str(uuid.uuid4()), tool=tool, status="draft")


def _preauth(ok=True, reason=PREAUTH_OK, balance="1000", required="1"):
    return SimpleNamespace(
        ok=ok, reason=reason,
        balance_usd=Decimal(balance),
        budget_usd=Decimal("50"),
        required_usd=Decimal(required),
    )


def _form(**kw):
    """What the launch form actually posts for two ungated tools.

    Note what is ABSENT: `preset`. The form never posts one for the five pilot
    tools, so a payload that included it would be testing something the
    browser cannot send.
    """
    data = {
        "tools": ["rfdiffusion", "pxdesign"],
        "target_chain": "A",
        "hotspot_residues": "42,88",
        "pace": "burst",
        "rfdiffusion__designs": "12",
        "rfdiffusion__binder_length_min": "55",
        "rfdiffusion__binder_length_max": "65",
        "pxdesign__designs": "24",
        "pxdesign__binder_length": "80",
    }
    data.update(kw)
    return data


class _Recorder:
    """Records the interleaving of create/fund/drive across the whole launch.

    Ordering is the atomicity guarantee: every row must exist as a draft before
    any of them is funded. Asserting call counts alone cannot tell a correct
    launch from one that funds as it creates.
    """

    def __init__(self, create_results=None, fund_results=None):
        self.calls = []
        self.created = []
        self._create_results = list(create_results or [])
        self._fund_results = list(fund_results or [])

    def create(self, **kwargs):
        self.calls.append(("create", kwargs["tool"]))
        if self._create_results:
            result = self._create_results.pop(0)
        else:
            result = _campaign(kwargs["tool"])
        if result is not None:
            self.created.append((result, kwargs))
        return result

    def fund(self, campaign_id):
        self.calls.append(("fund", campaign_id))
        if self._fund_results:
            return self._fund_results.pop(0)
        return True

    def drive(self, campaign_id):
        self.calls.append(("drive", campaign_id))

    def kwargs_for(self, tool):
        for campaign, kwargs in self.created:
            if kwargs["tool"] == tool:
                return kwargs
        raise AssertionError(f"{tool} was never created")


def _launch(client, target, form=None, recorder=None, preauth=None):
    """POST the launch form with the money and persistence layers mocked."""
    recorder = recorder or _Recorder()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=target), \
            patch("blueprints.targets.touch_target"), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=preauth or _preauth()), \
            patch("shared.compute_campaigns.create_campaign",
                  side_effect=recorder.create), \
            patch("shared.compute_campaigns.fund_campaign",
                  side_effect=recorder.fund), \
            patch("shared.compute_campaigns.drive_campaign_async",
                  side_effect=recorder.drive):
        resp = client.post(
            f"/targets/{target.id}/launch", data=form if form is not None else _form(),
        )
    return resp, recorder


# ---------------------------------------------------------------------------
# GET /targets/<id>/launch
# ---------------------------------------------------------------------------


def test_the_launch_page_lists_the_five_ungated_tools(client, monkeypatch):
    """Red if the route filters SUPPORTED_TOOLS with a bare tool_enabled():
    that helper is fail-closed and the five live tools have no flag env, so a
    naive filter hides every one of them."""
    monkeypatch.delenv("FLAG_TOOL_PROTEINA", raising=False)
    monkeypatch.delenv("FLAG_TOOL_IGGM", raising=False)
    _login(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t):
        resp = client.get(f"/targets/{t.id}/launch")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    for tool in ("rfdiffusion", "bindcraft", "boltzgen", "pxdesign", "rfantibody"):
        assert f'name="tools" value="{tool}"' in body
    assert 'name="tools" value="proteina"' not in body
    assert 'name="tools" value="iggm"' not in body
    # Not just the checkboxes. The page also ships a JSON list of the tools
    # whose preset is a real choice, straight into a <script> tag; shipping the
    # raw constant would print both gated slugs to every user and undo, in a
    # script tag, the reason a gated tool is answered as "unknown" elsewhere.
    assert "proteina" not in body
    assert "iggm" not in body


def test_a_gated_tool_appears_when_its_flag_is_flipped(client, monkeypatch):
    """Red if the flag is read once at import: an operator flips it on the
    running service and must not need a redeploy."""
    monkeypatch.setenv("FLAG_TOOL_IGGM", "on")
    _login(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t):
        resp = client.get(f"/targets/{t.id}/launch")
    body = resp.get_data(as_text=True)
    assert 'name="tools" value="iggm"' in body
    assert 'name="iggm__fasta"' in body
    assert 'name="iggm__epitope"' in body


def test_the_launch_page_prefills_chain_hotspots_and_the_iggm_epitope(
    client, monkeypatch,
):
    """target_defaults_for_form keys the epitope as `epitope`, NOT
    `epitope_residues`. Reading the wrong key prefills nothing and does it
    silently, so assert on the rendered value attribute, never on a
    placeholder that happens to contain similar digits."""
    monkeypatch.setenv("FLAG_TOOL_IGGM", "on")
    _login(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t):
        body = _squash(client.get(f"/targets/{t.id}/launch").get_data(as_text=True))
    # The full attribute run, so a match cannot come from the placeholder
    # ("e.g. 417, 453, 486") sitting a few characters away.
    assert 'name="target_chain" id="target_chain" class="field-input" ' \
           'style="max-width: 120px;" value="A"' in body
    assert 'name="hotspot_residues" id="hotspot_residues" class="field-input" ' \
           'style="max-width: 320px;" value="42,88"' in body
    # target_defaults_for_form keys this `epitope`; the template maps it onto
    # the namespaced field iggm actually parses.
    assert 'name="iggm__epitope" id="iggm__epitope" class="field-input" ' \
           'disabled value="32,45"' in body


def test_the_launch_form_posts_no_files(client):
    """urlencoded, no enctype. Every structure is already staged and no adapter
    reads request.files, so an upload here could only ever be a way to run
    against a structure the target does not point at."""
    _login(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t):
        body = client.get(f"/targets/{t.id}/launch").get_data(as_text=True)
    assert "multipart/form-data" not in body
    assert 'type="file"' not in body


def test_a_target_with_no_stored_structure_renders_no_launch_form(client):
    _login(client)
    t = _target(storage_path=None)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t):
        resp = client.get(f"/targets/{t.id}/launch")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "no stored structure" in body
    assert 'name="tools"' not in body


def test_an_archived_target_still_redirects_to_its_detail_page(client):
    """Phase 1 behaviour (A33) that replacing the redirect with a form must not
    lose: the detail page is where Restore lives."""
    _login(client)
    t = _target(archived_at="2026-07-01T00:00:00Z")
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t):
        resp = client.get(f"/targets/{t.id}/launch")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/targets/{t.id}")


def test_another_users_target_is_404_and_the_fetch_is_owner_scoped(client):
    _login(client)
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=None) as fetch:
        resp = client.get(f"/targets/{uuid.uuid4()}/launch")
    assert resp.status_code == 404
    # The owner-scoped fetch IS the tenancy boundary; filtering afterwards
    # would already have resolved a foreign id to a storage path.
    assert fetch.call_args.kwargs["user_id"] == "u-1"


# ---------------------------------------------------------------------------
# GET /api/targets/<id>/launch-estimate
# ---------------------------------------------------------------------------


def _estimate(client, target, params, preauth=None):
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=target), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=preauth or _preauth()):
        return client.get(
            f"/api/targets/{target.id}/launch-estimate?{params}"
        ).get_json()


def test_the_estimate_prices_every_tool_separately(client):
    """Red if the route prices one tool and multiplies."""
    from shared.compute_campaigns import plan_chunks
    _login(client)
    t = _target()
    data = _estimate(
        client, t,
        "tool=rfdiffusion&designs=12&preset=pilot"
        "&tool=pxdesign&designs=24&preset=pilot",
    )
    assert data["ok"] is True
    by_tool = {r["tool"]: r for r in data["rows"]}
    for tool, count in (("rfdiffusion", 12), ("pxdesign", 24)):
        expected = plan_chunks(tool, count, "pilot")
        assert Decimal(by_tool[tool]["budget_usd"]) == expected.budget_usd
        assert by_tool[tool]["total_subjobs"] == expected.total_subjobs


def test_the_estimate_totals_equal_the_sum_of_its_rows(client):
    """The itemised rows are what the user reads before authorising spend. If
    they could disagree with the headline, the headline is the number they did
    not agree to."""
    _login(client)
    t = _target()
    data = _estimate(
        client, t,
        "tool=rfdiffusion&designs=12&preset=pilot"
        "&tool=pxdesign&designs=24&preset=pilot"
        "&tool=boltzgen&designs=50&preset=pilot",
    )
    assert sum(Decimal(r["budget_usd"]) for r in data["rows"]) == Decimal(
        data["budget_usd"]
    )
    assert sum(Decimal(r["first_wave_usd"]) for r in data["rows"]) == Decimal(
        data["first_wave_usd"]
    )


def test_the_estimate_encodes_money_as_strings(client):
    _login(client)
    t = _target()
    data = _estimate(client, t, "tool=rfdiffusion&designs=12&preset=pilot")
    for key in ("budget_usd", "first_wave_usd", "balance_usd"):
        assert isinstance(data[key], str), key
        Decimal(data[key])
    for row in data["rows"]:
        assert isinstance(row["budget_usd"], str)
        assert isinstance(row["first_wave_usd"], str)


def test_a_desynced_estimate_request_is_rejected_not_zipped(client):
    """Three tools, two counts. zip() would silently price two and the POST
    would then launch three -- a bill for work that was never shown."""
    _login(client)
    t = _target()
    data = _estimate(
        client, t,
        "tool=rfdiffusion&tool=pxdesign&tool=boltzgen"
        "&designs=12&designs=24"
        "&preset=pilot&preset=pilot&preset=pilot",
    )
    assert data["ok"] is False


def test_the_estimate_refuses_a_flag_gated_tool(client, monkeypatch):
    monkeypatch.delenv("FLAG_TOOL_IGGM", raising=False)
    _login(client)
    t = _target()
    data = _estimate(client, t, "tool=iggm&designs=40&preset=cdr_design")
    assert data["ok"] is False


def test_the_estimate_offers_the_narrow_start_when_the_wide_one_is_too_big(
    client,
):
    """The escape valve. Without it the only advice a refused user gets is
    "top up", when starting narrow would have worked."""
    _login(client)
    t = _target()
    data = _estimate(
        client, t,
        "pace=burst"
        "&tool=rfdiffusion&designs=200&preset=pilot"
        "&tool=pxdesign&designs=200&preset=pilot",
        preauth=_preauth(
            ok=False, reason=PREAUTH_INSUFFICIENT, balance="120", required="400",
        ),
    )
    assert data["ok"] is True
    assert data["affordable"] is False
    assert data["alternative"]["pace"] == "steady"
    assert Decimal(data["alternative"]["first_wave_usd"]) <= Decimal("120")


def test_the_estimate_and_the_launch_gate_on_the_same_pair(client):
    """Anti-drift. The preview and the gate must agree by construction, not by
    two pieces of arithmetic that happen to match today."""
    _login(client)
    t = _target()
    form = _form()

    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=_preauth()) as est_gate:
        client.get(
            f"/api/targets/{t.id}/launch-estimate?pace=burst"
            "&tool=rfdiffusion&designs=12&preset=pilot"
            "&tool=pxdesign&designs=24&preset=pilot"
        )
    estimate_args = est_gate.call_args.args

    recorder = _Recorder()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("blueprints.targets.touch_target"), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=_preauth()) as post_gate, \
            patch("shared.compute_campaigns.create_campaign",
                  side_effect=recorder.create), \
            patch("shared.compute_campaigns.fund_campaign",
                  side_effect=recorder.fund), \
            patch("shared.compute_campaigns.drive_campaign_async",
                  side_effect=recorder.drive):
        client.post(f"/targets/{t.id}/launch", data=form)

    assert post_gate.call_args.args == estimate_args


# ---------------------------------------------------------------------------
# POST /targets/<id>/launch -- validation
# ---------------------------------------------------------------------------


def test_bindcraft_launches_because_the_route_supplies_its_preset(client):
    """bindcraft is the one adapter with no internal preset default, and the
    form cannot post one. If the route stops setting it, this alone goes red
    while every other tool keeps working."""
    _login(client)
    t = _target()
    resp, rec = _launch(client, t, form=_form(
        tools=["bindcraft"],
        bindcraft__designs="16",
        bindcraft__binder_length_min="50",
        bindcraft__binder_length_max="100",
    ))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:600]
    assert rec.kwargs_for("bindcraft")["preset"] == "pilot"


def test_pxdesign_gets_the_binder_length_the_user_typed(client):
    """PXDesign reads a SINGULAR binder_length. Wiring it to the min/max pair
    the other tools use would silently run every design at the default 80."""
    _login(client)
    t = _target()
    resp, rec = _launch(client, t, form=_form(
        tools=["pxdesign"], pxdesign__designs="24",
        pxdesign__binder_length="120",
    ))
    assert resp.status_code == 302
    assert rec.kwargs_for("pxdesign")["params"]["binder_length"] == 120


def test_one_tools_validation_failure_creates_nothing(client):
    """iggm without a FASTA, alongside two valid tools. All or nothing."""
    _login(client)
    t = _target()
    with patch.dict("os.environ", {"FLAG_TOOL_IGGM": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["rfdiffusion", "iggm", "pxdesign"],
            iggm__designs="40", iggm__preset="cdr_design",
            iggm__epitope="42,88", iggm__fasta="",
        ))
    assert resp.status_code == 400
    assert rec.calls == []


def test_the_error_names_the_tool_that_failed(client):
    """A seven-tool form answering "Invalid parameters." is unusable."""
    _login(client)
    t = _target()
    with patch.dict("os.environ", {"FLAG_TOOL_IGGM": "on"}):
        resp, _ = _launch(client, t, form=_form(
            tools=["rfdiffusion", "iggm"],
            iggm__designs="40", iggm__preset="cdr_design",
            iggm__epitope="42,88", iggm__fasta="",
        ))
    body = resp.get_data(as_text=True)
    assert "IgGM:" in body
    assert "heavy chain" in body


def test_a_chain_the_target_does_not_have_blocks_the_launch(client):
    """Nothing else validates the chain on this path: the structure is never
    re-uploaded, so resolve_target_upload never runs."""
    _login(client)
    t = _target()
    resp, rec = _launch(client, t, form=_form(target_chain="Z"))
    assert resp.status_code == 400
    assert rec.calls == []
    assert "Z" in resp.get_data(as_text=True)


def test_a_hotspot_outside_the_target_blocks_the_launch(client):
    _login(client)
    t = _target()
    resp, rec = _launch(client, t, form=_form(hotspot_residues="9001"))
    assert resp.status_code == 400
    assert rec.calls == []


def test_no_tools_selected_is_refused(client):
    _login(client)
    t = _target()
    resp, rec = _launch(client, t, form=_form(tools=[]))
    assert resp.status_code == 400
    assert rec.calls == []


def test_the_same_tool_listed_twice_is_rejected(client):
    """The form cannot produce this, so it is a crafted post. Dropping the
    duplicate hides a doubled bill; honouring it creates one."""
    _login(client)
    t = _target()
    resp, rec = _launch(client, t, form=_form(
        tools=["rfdiffusion", "rfdiffusion"],
    ))
    assert resp.status_code == 400
    assert rec.calls == []


def test_a_flag_gated_tool_cannot_be_launched_by_crafting_the_post(
    client, monkeypatch,
):
    monkeypatch.delenv("FLAG_TOOL_IGGM", raising=False)
    _login(client)
    t = _target()
    resp, rec = _launch(client, t, form=_form(
        tools=["iggm"], iggm__designs="40", iggm__preset="cdr_design",
        iggm__epitope="42,88", iggm__fasta=">H\nQVQLVESGGGL" + "A" * 90,
    ))
    assert resp.status_code == 400
    assert rec.calls == []


def test_iggm_affinity_maturation_is_refused_on_the_launch_path(client):
    """Its delivered count expands per masked position, which breaks the
    delivered-equals-chunk-size invariant every campaign hold assumes."""
    _login(client)
    t = _target()
    with patch.dict("os.environ", {"FLAG_TOOL_IGGM": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["iggm"], iggm__designs="40",
            iggm__preset="affinity_maturation",
            iggm__epitope="42,88", iggm__fasta=">H\nQVQLVESGGGL" + "A" * 90,
        ))
    assert resp.status_code == 400
    assert rec.calls == []
    # The status code alone does not discriminate: this payload ALSO fails the
    # adapter's own mask check, so deleting the campaign-level refusal would
    # leave the test green on a different error. Assert the reason.
    body = resp.get_data(as_text=True)
    assert "not available as a campaign" in body
    assert "single-run IgGM form" in body


def test_proteina_ligand_binder_is_refused_against_a_protein_target(client):
    _login(client)
    t = _target()
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["proteina"], proteina__designs="8",
            proteina__preset="ligand_binder",
        ))
    assert resp.status_code == 400
    assert rec.calls == []


def test_the_error_re_render_keeps_every_tool_the_user_picked(client):
    """request.form.to_dict() flattens a MultiDict to its first value, so
    without an explicit getlist the selection collapses to one checkbox on
    every validation error."""
    _login(client)
    t = _target()
    resp, _ = _launch(client, t, form=_form(
        tools=["rfdiffusion", "pxdesign"], target_chain="Z",
    ))
    body = _squash(resp.get_data(as_text=True))
    for tool in ("rfdiffusion", "pxdesign"):
        assert (
            f'name="tools" value="{tool}" id="pick-{tool}" '
            f'data-tool="{tool}" class="tool-pick" checked'
        ) in body
    # A tool the user did NOT pick must come back unchecked, or the re-render
    # would quietly widen the launch.
    assert (
        'name="tools" value="boltzgen" id="pick-boltzgen" '
        'data-tool="boltzgen" class="tool-pick" >'
    ) in body


# ---------------------------------------------------------------------------
# POST /targets/<id>/launch -- money and atomicity
# ---------------------------------------------------------------------------


def test_an_unaffordable_launch_creates_nothing(client):
    _login(client)
    t = _target()
    resp, rec = _launch(
        client, t,
        preauth=_preauth(ok=False, reason=PREAUTH_INSUFFICIENT,
                         balance="1", required="400"),
    )
    assert resp.status_code == 400
    assert rec.calls == []


def test_the_refusal_reads_as_plural_for_a_multi_tool_launch(client):
    _login(client)
    t = _target()
    resp, _ = _launch(
        client, t,
        preauth=_preauth(ok=False, reason=PREAUTH_INSUFFICIENT,
                         balance="1", required="400"),
    )
    body = resp.get_data(as_text=True)
    assert "these 2 runs" in body
    assert "this campaign" not in body


def test_preauth_is_called_once_for_the_whole_launch(client):
    """The defect shared/target_launch.py exists to prevent. campaign_preauth
    never debits, so a per-tool loop reads the same balance N times: all N pass
    a gate only one can afford, and the driver silently parks the rest."""
    _login(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("blueprints.targets.touch_target"), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=_preauth()) as gate, \
            patch("shared.compute_campaigns.create_campaign",
                  side_effect=lambda **kw: _campaign(kw["tool"])), \
            patch("shared.compute_campaigns.fund_campaign", return_value=True), \
            patch("shared.compute_campaigns.drive_campaign_async"):
        client.post(f"/targets/{t.id}/launch", data=_form(
            tools=["rfdiffusion", "pxdesign", "boltzgen", "bindcraft"],
            boltzgen__designs="50", boltzgen__protocol="protein-anything",
            bindcraft__designs="16",
            bindcraft__binder_length_min="50",
            bindcraft__binder_length_max="100",
        ))
    assert gate.call_count == 1


def test_every_run_in_one_launch_shares_a_group_id_and_two_launches_differ(
    client,
):
    _login(client)
    t = _target()
    _, first = _launch(client, t)
    groups = {kw["launch_group_id"] for _, kw in first.created}
    assert len(first.created) == 2
    assert len(groups) == 1

    _, second = _launch(client, t)
    assert {kw["launch_group_id"] for _, kw in second.created} != groups


def test_each_run_is_created_with_its_own_divided_concurrency(client):
    """rfdiffusion 16, proteina 4. Passing None would restore 16 for proteina,
    whose single shard is a full A100; passing 0 is worse, because
    create_campaign treats it as falsy and silently restores the default."""
    _login(client)
    t = _target()
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["rfdiffusion", "proteina"],
            proteina__designs="8", proteina__preset="protein_binder",
        ))
    assert resp.status_code == 302
    assert rec.kwargs_for("rfdiffusion")["concurrency_target"] == 16
    assert rec.kwargs_for("proteina")["concurrency_target"] == 4


def test_no_run_is_ever_created_with_a_falsy_concurrency(client):
    """Route-level: whatever the division produces reaches create_campaign as a
    positive int. Note this cannot exercise divide_concurrency's max(1, ...)
    floor, because at most 7 distinct tools the share is 32 // 28 = 1 already;
    the floor itself is covered directly in tests/test_target_launch.py, which
    drives the function past the widths a route can produce."""
    _login(client)
    t = _target()
    for pace in ("burst", "steady"):
        _, rec = _launch(client, t, form=_form(pace=pace))
        assert rec.created
        for _, kwargs in rec.created:
            assert isinstance(kwargs["concurrency_target"], int)
            assert kwargs["concurrency_target"] >= 1


def test_every_run_inherits_the_staged_path_and_nothing_is_re_uploaded(client):
    """The entire point of a target: the structure is staged once.

    Asserted by what reaches create_campaign, not by patching upload_input:
    blueprints/targets.py never imports it, so a patch there would pass no
    matter what this route did. The launch form posts no file at all, which is
    the structural reason a re-stage is impossible here."""
    _login(client)
    t = _target()
    _, rec = _launch(client, t)
    assert rec.created
    for _, kwargs in rec.created:
        assert kwargs["target_storage_path"] == t.storage_path
        assert kwargs["target_id"] == t.id
        assert kwargs["target_name"] == t.display_name


def test_all_runs_are_created_before_any_is_funded(client):
    """The atomicity claim. Funding as it creates would mean a failure at run
    k leaves k-1 already funded, dispatched and billing."""
    _login(client)
    t = _target()
    _, rec = _launch(client, t, form=_form(
        tools=["rfdiffusion", "pxdesign", "boltzgen"],
        boltzgen__designs="50", boltzgen__protocol="protein-anything",
    ))
    kinds = [kind for kind, _ in rec.calls]
    assert kinds[:3] == ["create", "create", "create"]
    assert "fund" not in kinds[:3]


def test_a_failed_insert_midway_funds_and_drives_nothing(client):
    _login(client)
    t = _target()
    rec = _Recorder(create_results=[_campaign("rfdiffusion"), None])
    resp, rec = _launch(client, t, recorder=rec)
    assert resp.status_code == 400
    assert [kind for kind, _ in rec.calls] == ["create", "create"]
    assert "nothing was charged" in resp.get_data(as_text=True)


def test_a_run_whose_fund_fails_is_never_driven(client):
    """drive_campaign early-returns on a draft, so driving an unfunded run is a
    silent no-op that leaves it parked forever with nothing to see."""
    _login(client)
    t = _target()
    rec = _Recorder(fund_results=[True, False])
    resp, rec = _launch(client, t, recorder=rec)
    assert resp.status_code == 302
    funded = [cid for kind, cid in rec.calls if kind == "fund"]
    driven = [cid for kind, cid in rec.calls if kind == "drive"]
    assert len(funded) == 2
    assert driven == [funded[0]]


def test_a_partially_funded_launch_still_starts_what_it_could(client):
    _login(client)
    t = _target()
    rec = _Recorder(fund_results=[True, False])
    resp, _ = _launch(client, t, recorder=rec)
    assert resp.status_code == 302
    assert "stalled=1" in resp.headers["Location"]


def test_a_clean_launch_reports_no_stalled_runs(client):
    _login(client)
    t = _target()
    resp, _ = _launch(client, t)
    assert "stalled" not in resp.headers["Location"]
    assert "launched=" in resp.headers["Location"]


def test_a_launch_where_every_fund_fails_is_reported_as_started_nothing(client):
    _login(client)
    t = _target()
    rec = _Recorder(fund_results=[False, False])
    resp, rec = _launch(client, t, recorder=rec)
    assert resp.status_code == 400
    assert "was not charged" in resp.get_data(as_text=True)
    assert [kind for kind, _ in rec.calls if kind == "drive"] == []


# ---------------------------------------------------------------------------
# The target page after a launch
# ---------------------------------------------------------------------------


def test_the_banner_counts_only_the_runs_from_this_launch(client):
    _login(client)
    t = _target()
    group = str(uuid.uuid4())
    runs = [
        SimpleNamespace(id="c1", tool="rfdiffusion", name=None, status="funded",
                        requested_designs=12, total_subjobs=1,
                        launch_group_id=group),
        SimpleNamespace(id="c2", tool="pxdesign", name=None, status="funded",
                        requested_designs=24, total_subjobs=1,
                        launch_group_id=group),
        # An older, unrelated run against the same target.
        SimpleNamespace(id="c3", tool="boltzgen", name=None, status="completed",
                        requested_designs=50, total_subjobs=1,
                        launch_group_id=str(uuid.uuid4())),
    ]
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=runs):
        body = client.get(
            f"/targets/{t.id}?launched={group}"
        ).get_data(as_text=True)
    assert "Started 2 runs" in body


def test_an_unknown_launch_group_renders_no_banner(client):
    _login(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=[]):
        body = client.get(
            f"/targets/{t.id}?launched={uuid.uuid4()}&stalled=99"
        ).get_data(as_text=True)
    # A crafted `stalled` only misinforms whoever crafted it: the whole banner
    # is gated on this launch group actually matching rows on the page.
    assert "Started" not in body
    assert "could not be started" not in body
