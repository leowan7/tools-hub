"""Launching N tools against one target in a single gated action (Phase 2).

The route under test spends money, so these pin the ORDER of operations as
hard as the outcomes: validate everything first, gate once, create every run
as an inert draft, and only then fund and drive. A test that merely checks a
kwarg reached a mock would pass against most of the ways this can go wrong.
"""

from __future__ import annotations

import html
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


def _visible_text(resp):
    """The response body as the user reads it, with HTML entities resolved.

    Error copy is asserted verbatim here, and Jinja autoescapes it, so a raw
    ``in resp.get_data(as_text=True)`` fails on any sentence containing ``>``
    or an apostrophe. Hardcoding the escaped form instead would pin the test to
    one escaper: MarkupSafe emits ``&#39;`` where ``html.escape`` emits
    ``&#x27;``. The subject of these assertions is the sentence the user sees,
    not its transport encoding.
    """
    return html.unescape(resp.get_data(as_text=True))


def _assert_float_encoding_is_distinguishable(money_strings, what):
    """Precondition: these figures must be able to expose a float round trip.

    ``str(float(Decimal("3.5176")))`` is byte-identical to ``str(Decimal(...))``:
    a 4dp Decimal only loses its quantum through float when it carries a
    TRAILING ZERO. So a quantum assertion over figures that all round-trip
    cleanly cannot fail even if the endpoint floats every single one, and the
    test pins nothing while looking rigorous.

    Checked per group, because the groups are encoded at different call sites.
    The endpoint stringifies its own totals; ``MultiLaunchPlan.rows()``
    stringifies the rows. A cohort whose rows carry a trailing zero but whose
    sums do not leaves a totals-only regression invisible.
    """
    assert any(
        str(float(Decimal(s))) != s for s in money_strings
    ), (
        f"every {what} figure {money_strings} round-trips through float "
        f"unchanged, so the 4dp assertions above would pass against "
        f"str(float(...)) and pin nothing. Pick a cohort whose {what} carry a "
        f"trailing zero (rfdiffusion@120 + boltzgen@120 does)."
    )


def _assert_pace_is_observable_on(tools, designs):
    """Precondition: this cohort must price differently at burst and steady.

    Called by tests whose subject is the pace. Their assertions run against a
    patched ``campaign_preauth``, so they cannot notice that the two paces have
    collapsed onto the same number -- they would keep passing while testing
    nothing. This computes the real plans (pure, no client) and refuses to let
    that happen silently.
    """
    from shared.target_launch import (
        PACE_BURST,
        PACE_STEADY,
        ToolLaunchSpec,
        plan_multi_launch,
    )

    specs = [
        ToolLaunchSpec(
            tool=t, preset="pilot", requested_designs=designs, params={}
        )
        for t in tools
    ]
    burst = plan_multi_launch(specs, PACE_BURST).first_wave_usd
    steady = plan_multi_launch(specs, PACE_STEADY).first_wave_usd
    assert burst != steady, (
        f"cohort {tools} at {designs} designs prices identically at both "
        f"paces ({burst}), so any test comparing preauth arguments is blind "
        f"to a pace mix-up. Raise the design count until the first waves "
        f"diverge."
    )


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

    def touch(self, target_id):
        # In the same log as create/fund/drive so a test can assert WHERE in the
        # sequence it happened, not just that it happened once with the right id.
        self.calls.append(("touch", target_id))

    def kwargs_for(self, tool):
        for campaign, kwargs in self.created:
            if kwargs["tool"] == tool:
                return kwargs
        raise AssertionError(f"{tool} was never created")


def _still_draft(campaign_id, **_kw):
    """What `get_campaign` returns in production when a fund genuinely failed.

    The route confirms a `False` from `fund_campaign` with a read, because False
    means either "not in draft" or "the write raised and I cannot tell". Without
    modelling that read every `fund_results=[False]` test would take the
    INDETERMINATE branch instead of the stalled one -- and it would do so
    silently, because `get_campaign` swallows its own exceptions and returns
    None, which under `isolate_supabase` (no client) is what it always returns.
    """
    return SimpleNamespace(id=campaign_id, tool="rfdiffusion", status="draft")


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
            patch("shared.compute_campaigns.get_campaign",
                  side_effect=_still_draft), \
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


def test_a_small_molecule_target_renders_no_launch_form(client):
    """The second ``_launch_blocker`` branch. Untested until now because
    ``_target()`` hardcodes ``kind="pdb"``, so every launch test went down the
    storage_path branch and this one never executed. All seven campaign tools
    take a protein structure; an SDF reaching them would fail on the GPU after
    the wallet had already been held."""
    _login(client)
    t = _target(kind="sdf")
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t):
        resp = client.get(f"/targets/{t.id}/launch")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "small molecule" in body
    assert 'name="tools"' not in body
    # And the two blockers are distinct messages, not one shared "cannot run".
    assert "no stored structure" not in body


def test_a_small_molecule_target_cannot_be_launched_by_posting_anyway(client):
    """The GET hiding the form is presentation. This is the guard."""
    _login(client)
    t = _target(kind="sdf")
    resp, rec = _launch(client, t)
    assert resp.status_code == 400
    assert rec.calls == []


def test_a_small_molecule_target_is_not_priced_by_the_estimate(client):
    """Pricing a launch that the POST refuses shows an affordable quote for
    work that can never run."""
    _login(client)
    t = _target(kind="sdf")
    data = _estimate(client, t, "tool=rfdiffusion&designs=12&preset=pilot")
    assert data["ok"] is False
    # The specific refusal, matching its two siblings. A bare `ok is False`
    # passes on any refusal at all, including one for an unrelated reason.
    assert "small molecule" in data["error"]


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
    """Assert the QUANTUM, not that the string parses.

    ``isinstance(str)`` plus a bare ``Decimal(s)`` both pass against
    ``str(float(...))``, which is exactly the encoding this is here to forbid:
    a 4dp Decimal put through float loses the quantum, so ``Decimal("10.0000")``
    ships as ``"10.0"`` and a figure the user is about to authorise changes
    shape depending on which digits happen to be zero. Only the exponent tells
    the two encodings apart.

    The cohort matters as much as the assertion. ``str(float(x)) == str(x)`` for
    any 4dp Decimal WITHOUT trailing zeros, so priced against rfdiffusion@12
    alone this test passed even with the endpoint floating every figure: that
    cohort plans 2.0101 and 2.6219, both of which survive the round trip intact,
    so there was nothing to catch. rfdiffusion@120 + boltzgen@120 is chosen
    because it carries a trailing zero in BOTH groups: measured, of the six
    asserted figures four have one (totals 50.2470 and 56.2190; rows
    rfdiffusion first_wave 26.2190 and boltzgen first_wave 30.0000), while rows
    rfdiffusion budget 20.1009 and boltzgen budget 30.1461 do not. A trailing
    zero is the only condition under which either encoding is observable, and
    the preconditions are checked per group precisely because one group having
    one does not mean the other does. Both tools are ungated, so no flag
    patching is needed.
    """
    _login(client)
    t = _target()
    data = _estimate(
        client, t,
        "tool=rfdiffusion&designs=120&preset=pilot"
        "&tool=boltzgen&designs=120&preset=pilot",
    )
    assert data["ok"] is True, data.get("error")

    totals = []
    for key in ("budget_usd", "first_wave_usd"):
        assert isinstance(data[key], str), key
        totals.append(data[key])
        assert Decimal(data[key]).as_tuple().exponent == -4, (
            f"{key}={data[key]!r} is not a 4dp Decimal string"
        )
    _assert_float_encoding_is_distinguishable(totals, "total")

    row_money = []
    for row in data["rows"]:
        for key in ("budget_usd", "first_wave_usd"):
            assert isinstance(row[key], str), key
            row_money.append(row[key])
            assert Decimal(row[key]).as_tuple().exponent == -4, (
                f"rows[{row['tool']}].{key}={row[key]!r} is not a 4dp "
                f"Decimal string"
            )
    _assert_float_encoding_is_distinguishable(row_money, "row")
    # balance_usd is deliberately excluded from the quantum check: it is the
    # wallet's own value passed through, not a planned figure, and this test
    # mocks campaign_preauth -- so the exponent here would only describe the
    # fixture. Its encoding is still pinned.
    assert isinstance(data["balance_usd"], str)
    Decimal(data["balance_usd"])


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
    two pieces of arithmetic that happen to match today.

    ``campaign_preauth(user_id, budget_usd, first_wave_usd)`` is the pair, and
    ``budget_usd`` is pace-INVARIANT (it totals every sub-job either way), so
    ``first_wave_usd`` is the only argument that carries the pace. The cohort
    therefore has to be big enough for the two paces to price it differently,
    or a route that read the pace wrong would still produce matching args. At
    one sub-job per tool ``min(total_subjobs, concurrency)`` clamps the first
    wave to 1 and burst and steady are identical, which is why 200 designs (17
    and 9 sub-jobs) is deliberate and not incidental. The precondition below
    fails loudly if the cohort is ever shrunk back under that threshold.
    """
    _login(client)
    t = _target()
    form = _form(**{"rfdiffusion__designs": "200", "pxdesign__designs": "200"})

    _assert_pace_is_observable_on(["rfdiffusion", "pxdesign"], 200)

    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=_preauth()) as est_gate:
        client.get(
            f"/api/targets/{t.id}/launch-estimate?pace=burst"
            "&tool=rfdiffusion&designs=200&preset=pilot"
            "&tool=pxdesign&designs=200&preset=pilot"
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


# Every per-tool field the launch form owns, with a value DELIBERATELY unequal
# to both the template default and the adapter default.
#
# That inequality is the whole point. For EVERY campaign tool, including
# pxdesign, the template default and the adapter default are byte-identical
# (measured: rfdiffusion 55/65, bindcraft and boltzgen 50/100, rfantibody
# "H1:8,H2:7,H3:10-16", iggm 2000, pxdesign 80). So a field renamed on the
# server side of the ``<tool>__`` prefix boundary produces a form that looks
# right, a run that silently uses the default, and a green suite. The tool then
# burns A100 hours on parameters nobody chose. Asserting a non-default value
# through ``adapter.validate()`` into ``create_campaign(params=...)`` is the
# only thing that catches it.
#
# Scope, stated because the cases hand-build the POST body: this covers the
# SERVER side of the boundary (prefix stripping, the per-tool dict, the
# adapter). A field renamed in templates/targets/launch.html alone would still
# pass. Nothing here renders the form and submits what it produced.
_TYPED_FIELD_CASES = [
    # (tool, extra form fields, params key, expected value)
    (
        "rfdiffusion",
        {"rfdiffusion__binder_length_min": "70",
         "rfdiffusion__binder_length_max": "90"},
        "binder_length", {"min": 70, "max": 90},
    ),
    (
        "bindcraft",
        {"bindcraft__designs": "16",
         "bindcraft__binder_length_min": "61",
         "bindcraft__binder_length_max": "77"},
        "binder_length_min", 61,
    ),
    (
        "bindcraft",
        {"bindcraft__designs": "16",
         "bindcraft__binder_length_min": "61",
         "bindcraft__binder_length_max": "77"},
        "binder_length_max", 77,
    ),
    (
        "boltzgen",
        {"boltzgen__designs": "50",
         "boltzgen__protocol": "nanobody-anything",
         "boltzgen__binder_length_min": "50",
         "boltzgen__binder_length_max": "100"},
        "protocol", "nanobody-anything",
    ),
    (
        "boltzgen",
        {"boltzgen__designs": "50",
         "boltzgen__protocol": "protein-anything",
         "boltzgen__binder_length_min": "31",
         "boltzgen__binder_length_max": "137"},
        "binder_length_min", 31,
    ),
    (
        "rfantibody",
        {"rfantibody__designs": "16",
         "rfantibody__cdr_lengths": "H1:9,H2:6,H3:12-15"},
        "cdr_lengths", "H1:9,H2:6,H3:12-15",
    ),
]


@pytest.mark.parametrize(
    "tool,extra,key,expected", _TYPED_FIELD_CASES,
    ids=[f"{c[0]}-{c[2]}" for c in _TYPED_FIELD_CASES],
)
def test_each_tool_receives_the_value_the_user_typed(
    client, tool, extra, key, expected,
):
    _login(client)
    t = _target()
    form = _form(tools=[tool], **extra)
    resp, rec = _launch(client, t, form=form)
    assert resp.status_code == 302, _visible_text(resp)[-400:]
    params = rec.kwargs_for(tool)["params"]
    assert params[key] == expected, (
        f"{tool}.{key} came through as {params.get(key)!r}, not the typed "
        f"{expected!r}"
    )


def test_the_typed_field_cases_all_differ_from_the_adapter_default(client):
    """Precondition for the parametrized test above.

    If a case's value ever equals what the adapter would have defaulted to, that
    case passes whether or not the form field is wired up at all. This asserts
    each expectation is actually distinguishable by launching the same tool with
    every field under test REMOVED and requiring a different answer.

    Removed from the merged form, not merely omitted from ``extra``: ``_form()``
    hardcodes ``rfdiffusion__binder_length_min``/``_max``, so dropping only the
    ``extra`` keys still posts 55/65 and the comparison would measure the FORM
    default rather than the adapter's. Those two happen to coincide today, which
    is exactly the kind of accident that makes a precondition read as working
    while it checks the wrong thing.
    """
    _login(client)
    t = _target()
    for tool, extra, key, expected in _TYPED_FIELD_CASES:
        form = _form(tools=[tool], **{
            k: v for k, v in extra.items() if k.endswith("__designs")
        })
        for field in extra:
            if not field.endswith("__designs"):
                form.pop(field, None)
        assert not any(
            k.startswith(f"{tool}__") and not k.endswith("__designs")
            for k in form
        ), f"{tool}: a namespaced field survived the strip: {sorted(form)}"

        _, rec = _launch(client, t, form=form)
        default = rec.kwargs_for(tool)["params"].get(key)
        assert default != expected, (
            f"{tool}.{key}: the test value {expected!r} equals the adapter "
            f"default, so that case cannot fail. Pick another value."
        )


def test_proteina_receives_the_task_name_the_user_typed(client):
    """Separate from the shared parametrization because proteina is flag-gated
    and picks its preset from the form (it is a design VARIANT, not a tier)."""
    _login(client)
    t = _target()
    base = dict(
        tools=["proteina"], proteina__designs="8",
        proteina__preset="protein_binder",
    )
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(
            client, t,
            form=_form(proteina__task_name="my_custom_task", **base),
        )
        assert resp.status_code == 302, _visible_text(resp)[-400:]
        # Distinguishable from the default the adapter would have chosen.
        _, plain = _launch(client, t, form=_form(**base))
    assert rec.kwargs_for("proteina")["params"]["task_name"] == "my_custom_task"
    assert plain.kwargs_for("proteina")["params"]["task_name"] != "my_custom_task"


def test_iggm_receives_the_max_antigen_size_the_user_typed(client):
    """IgGM is flag-gated and needs an antibody FASTA, so it cannot ride the
    shared parametrization."""
    _login(client)
    t = _target()
    # A real 120 aa VH with CDR-H3 masked. Two adapter rules make a stub
    # useless here, and both reject before any parameter is read: the chain must
    # be ANTIBODY_LEN_MIN = 80 aa or more (a fat-finger guard), and cdr_design
    # requires at least one X to mark a position to redesign. This is the first
    # test in the suite that launches IgGM successfully at all.
    fasta = (
        ">H\n"
        "EVQLVESGGGLVQPGGSLRLSCAASGFNIKDTYIHWVRQAPGKGLEWVARIYPTNGYTRYA"
        "DSVKGRFTISADTSKNTAYLQMNSLRAEDTAVYYCSRXXXXXXXAMDYWGQGTLVTVSS\n"
    )
    with patch.dict("os.environ", {"FLAG_TOOL_IGGM": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["iggm"], iggm__designs="40", iggm__preset="cdr_design",
            iggm__epitope="42,88", iggm__fasta=fasta,
            iggm__max_antigen_size="1234",
        ))
        assert resp.status_code == 302, _visible_text(resp)[-400:]
        assert rec.kwargs_for("iggm")["params"]["max_antigen_size"] == 1234
        # Distinguishable from the default the adapter would have chosen.
        _, plain = _launch(client, t, form=_form(
            tools=["iggm"], iggm__designs="40", iggm__preset="cdr_design",
            iggm__epitope="42,88", iggm__fasta=fasta,
        ))
    assert plain.kwargs_for("iggm")["params"]["max_antigen_size"] != 1234


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
    body = _visible_text(resp)
    assert "IgGM:" in body
    # The adapter's own sentence, verbatim. A bare `"heavy chain" in body`
    # matches the RFantibody field helper ("RFantibody designs a VHH heavy
    # chain"), which this template renders on every launch page including a
    # clean one -- so it passed whether or not the adapter's message survived.
    assert "Paste the antibody heavy chain (>H) sequence." in body


def test_a_chain_the_target_does_not_have_blocks_the_launch(client):
    """Nothing else validates the chain on this path: the structure is never
    re-uploaded, so resolve_target_upload never runs."""
    _login(client)
    t = _target()
    resp, rec = _launch(client, t, form=_form(target_chain="Z"))
    assert resp.status_code == 400
    assert rec.calls == []
    # The whole sentence. A bare `"Z" in body` passes on any page: the session
    # and CSRF tokens are random, so a capital Z turns up in a clean render
    # often enough to make the assertion meaningless.
    assert "Target chain 'Z' is not in this target" in _visible_text(resp)


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
    # The REASON, not just the status. IgGM's own adapter.validate() rejects
    # this FASTA for an unrelated reason, so deleting the flag check entirely
    # still yields 400 and still creates nothing -- the status code and the
    # empty recorder cannot tell the gate from the adapter. Only the message
    # discriminates: gated off, the tool is answered as unknown and never
    # reaches its adapter at all.
    assert "Unknown tool." in resp.get_data(as_text=True)


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
    """The DIVIDED width reaches create_campaign, not the tool's solo width.

    Four tools deliberately, because at two the division is an identity and
    the test could not fail: 32 // 2 == 16 is exactly rfdiffusion's solo width,
    and proteina is pinned to 4 either way. At four, burst gives (8, 8, 8, 4)
    against solo (16, 16, 16, 4), so passing ``launch_concurrency_for(tool)``
    -- or None, which makes create_campaign fall back to it -- goes red on the
    first three. Undivided, a 4-tool launch would ask for 16+16+16+4 = 52
    in-flight shards against a 32 global cap and inflate every first-wave hold.
    """
    _login(client)
    t = _target()
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["rfdiffusion", "pxdesign", "boltzgen", "proteina"],
            boltzgen__designs="50", boltzgen__protocol="protein-anything",
            boltzgen__binder_length_min="50", boltzgen__binder_length_max="100",
            proteina__designs="8", proteina__preset="protein_binder",
        ))
    assert resp.status_code == 302
    assert rec.kwargs_for("rfdiffusion")["concurrency_target"] == 8
    assert rec.kwargs_for("pxdesign")["concurrency_target"] == 8
    assert rec.kwargs_for("boltzgen")["concurrency_target"] == 8
    # Pinned by its own cap, below the divided share, so the division never
    # raises a tool above what its container can actually run.
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


def test_a_launch_touches_the_target_once_after_every_insert(client):
    """``touch_target`` is patched in every other launch test and asserted in
    none, so deleting the call left the suite green while the target's
    last-used ordering silently froze -- the targets page sorts on it, so a
    target you just launched four tools against sinks below one you have not
    opened in a month.

    Once, not per campaign: it is a write, and N tools would mean N writes for
    one user action. And after EVERY create, so a launch that bailed midway does
    not report activity that did not happen.

    The position is asserted through the recorder's interleaving, not with
    ``assert_called_once_with`` alone. A standalone mock observes the count and
    the argument but not the order, so moving the call up to just after the
    preauth gate -- one call, right id, still only on a gated-through launch --
    left it green.
    """
    _login(client)
    t = _target()
    recorder = _Recorder()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("blueprints.targets.touch_target",
                  side_effect=recorder.touch), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=_preauth()), \
            patch("shared.compute_campaigns.create_campaign",
                  side_effect=recorder.create), \
            patch("shared.compute_campaigns.fund_campaign",
                  side_effect=recorder.fund), \
            patch("shared.compute_campaigns.drive_campaign_async",
                  side_effect=recorder.drive):
        resp = client.post(f"/targets/{t.id}/launch", data=_form())
    assert resp.status_code == 302
    assert len(recorder.created) == 2

    kinds = [kind for kind, _ in recorder.calls]
    assert kinds.count("touch") == 1
    assert [arg for kind, arg in recorder.calls if kind == "touch"] == [t.id]
    # Every create precedes it, and it precedes every fund.
    touch_at = kinds.index("touch")
    assert kinds[:touch_at] == ["create", "create"]
    assert set(kinds[touch_at + 1:]) <= {"fund", "drive"}


def test_a_refused_launch_does_not_touch_the_target(client):
    """Nothing was created, so nothing happened to report."""
    _login(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("blueprints.targets.touch_target") as touch, \
            patch("shared.target_launch.campaign_preauth",
                  return_value=_preauth(ok=False, reason=PREAUTH_INSUFFICIENT)):
        resp = client.post(f"/targets/{t.id}/launch", data=_form())
    assert resp.status_code == 400
    touch.assert_not_called()


def _render_detail(client, target, query, runs):
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=target), \
            patch("shared.compute_campaigns.list_campaigns_for_target",
                  return_value=runs):
        return client.get(f"/targets/{target.id}?{query}").get_data(as_text=True)


def _run(tool, group, run_id="c1", status="funded", designs=12):
    return SimpleNamespace(
        id=run_id, tool=tool, name=None, status=status,
        requested_designs=designs, total_subjobs=1, launch_group_id=group,
    )


@pytest.mark.parametrize("stalled,expected", [
    (1, "1 more could not be started and was not charged"),
    (3, "3 more could not be started and were not charged"),
])
def test_the_stalled_banner_renders_when_some_runs_did_not_start(
    client, stalled, expected,
):
    """The partial-failure path a user actually hits when the wallet drains
    mid-launch. It is nested INSIDE the launched banner in detail.html, so it
    can only render when this launch group also matched rows. No prior test
    combined a MATCHING launch group with a non-zero ``stalled``: the one test
    that passed ``stalled=99`` paired it with an unknown group (so the whole
    banner was suppressed), and the one with a matching group passed no
    ``stalled`` at all. Between them the stalled copy never rendered and its
    singular/plural wording never executed.

    "not charged" is the load-bearing half. A stalled run stayed ``draft``,
    which is inert -- never funded, never dispatched, never billed -- and a
    user who thinks they paid for a run that did nothing will ask for a refund
    that is not owed.
    """
    _login(client)
    t = _target()
    group = str(uuid.uuid4())
    # Squashed: the template wraps this sentence mid-phrase, so a contiguous
    # assertion against the raw body silently never matches.
    body = _squash(_render_detail(
        client, t, f"launched={group}&stalled={stalled}",
        [_run("rfdiffusion", group)],
    ))
    assert "Started 1 run" in body
    assert expected in body


def test_the_stalled_banner_is_absent_when_every_run_started(client):
    _login(client)
    t = _target()
    group = str(uuid.uuid4())
    body = _render_detail(
        client, t, f"launched={group}", [_run("rfdiffusion", group)],
    )
    assert "Started 1 run" in body
    assert "could not be started" not in body


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


# ---------------------------------------------------------------------------
# Idempotency, COMPOSED with the real route under production CSRF settings
# ---------------------------------------------------------------------------
#
# Two blind spots, one setup.
#
# The suite default is CSRF_PROTECT=0 (tests/conftest.py), and that is what hid
# the original bug: with enforcement ON, app.py's `_enforce_csrf` before_request
# reads `request.form`, which consumes the stream, so `request.get_data()` in
# `_compute_key` returns b"" and the key degenerates to
# sha256(user_id + path) -- identical for every submission to the route. The
# fix (fall back to a canonical encoding of request.form) is covered against a
# hand-rolled `_consume_form` before_request in tests/test_idempotency.py, but
# nothing tied that stand-in to the real thing.
#
# Separately, `isolate_supabase` blanks the Supabase env, so `_claim_key` gets
# no client and returns "open" -- dedup disabled. Every other route test in
# this file therefore runs with @idempotent as a no-op. These inject a fake for
# the idempotency table ONLY, so the decorator actually engages while nothing
# else can reach production.


class _IdemTable:
    def __init__(self, rows):
        self.rows = rows
        self._key = None
        self._upsert = None
        self._update = None
        self._is_null = []
        self._pending_delete = False

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        if col == "key":
            self._key = val
        return self

    def is_(self, col, val):
        # Modelled, not accepted-and-ignored. Both _release_key and
        # _store_response scope their writes to `response_status IS NULL` so
        # neither can touch a claim that already completed. A fake that
        # swallowed the predicate would let the loser of a concurrent submit
        # overwrite the winner's cached 302 -- and would report the scoping as
        # working while it did nothing.
        assert val is None, f"is_({col!r}, {val!r}) is not modelled"
        self._is_null.append(col)
        return self

    def upsert(self, payload, on_conflict="key"):
        self._upsert = dict(payload)
        return self

    def update(self, payload):
        self._update = dict(payload)
        return self

    def delete(self):
        # Modelled, because refusing does NOT work here. `_release_key` wraps
        # `.delete()` in a bare `except Exception`, so raising is swallowed into
        # the same `False` return as omitting the method entirely, and the
        # wrapper then CACHES the failure instead of releasing it. A stub that
        # raises therefore inverts the behaviour under test exactly as silently
        # as a missing method, and makes any assertion about a released claim
        # unfailable. (This is why `is_` above can refuse and this cannot: `is_`
        # is called on the builder before `execute`, outside that except.)
        self._pending_delete = True
        return self

    def _matches(self, row):
        return row is not None and all(
            row.get(col) is None for col in self._is_null
        )

    def execute(self):
        if self._pending_delete:
            self._pending_delete = False
            row = self.rows.get(self._key)
            if not self._matches(row):
                # Filters excluded it, so PostgREST neither deletes nor returns.
                return SimpleNamespace(data=[])
            return SimpleNamespace(data=[self.rows.pop(self._key)])
        if self._upsert is not None:
            row = self._upsert
            self.rows[row["key"]] = row
            return SimpleNamespace(data=[row])
        if self._update is not None and self._key is not None:
            row = self.rows.get(self._key)
            if not self._matches(row):
                # Filters excluded it, so PostgREST neither writes nor returns.
                return SimpleNamespace(data=[])
            row.update(self._update)
            return SimpleNamespace(data=[row])
        if self._key is not None:
            row = self.rows.get(self._key)
            return SimpleNamespace(data=[dict(row)] if row else [])
        return SimpleNamespace(data=[])


class _IdemStore:
    """In-memory `request_idempotency` good enough for the real decorator.

    Models the `IS NULL` write scoping (see `_IdemTable.is_`) because
    `_store_response` depends on it. It does NOT model the delete path;
    tests/test_idempotency.py owns that and models it there.
    """

    def __init__(self):
        self.rows = {}

    def table(self, _name):
        return _IdemTable(self.rows)


@pytest.fixture
def csrf_app(monkeypatch):
    """The real app with production CSRF enforcement, not the suite default."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("CSRF_PROTECT", "1")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


_CSRF = "test-csrf-token-abc123"


def _login_with_csrf(client):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
        sess["_csrf_token"] = _CSRF


def _launch_csrf(client, target, form, recorder):
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=target), \
            patch("blueprints.targets.touch_target"), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=_preauth()), \
            patch("shared.compute_campaigns.create_campaign",
                  side_effect=recorder.create), \
            patch("shared.compute_campaigns.fund_campaign",
                  side_effect=recorder.fund), \
            patch("shared.compute_campaigns.drive_campaign_async",
                  side_effect=recorder.drive):
        return client.post(
            f"/targets/{target.id}/launch", data=dict(form, _csrf=_CSRF)
        )


def _other_launch_form():
    """A genuinely DIFFERENT launch: one tool, none of them shared with _form()."""
    return _form(
        tools=["boltzgen"], boltzgen__designs="50",
        boltzgen__protocol="protein-anything",
    )


def test_csrf_enforcement_is_actually_on_in_this_fixture(csrf_app):
    """Precondition. If enforcement silently switched off, every test below
    would still pass while exercising the same CSRF_PROTECT=0 path as the rest
    of the file, and the blind spot would be back."""
    client = csrf_app.test_client()
    _login_with_csrf(client)
    t = _target()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t):
        resp = client.post(f"/targets/{t.id}/launch", data=_form())
    assert resp.status_code == 403


def test_a_double_submit_through_the_real_route_launches_once(csrf_app):
    """@idempotent composed with the real launch route, not a synthetic one."""
    store = _IdemStore()
    client = csrf_app.test_client()
    _login_with_csrf(client)
    t = _target()
    rec = _Recorder()
    with patch("shared.idempotency.get_service_client", return_value=store):
        first = _launch_csrf(client, t, _form(), rec)
        second = _launch_csrf(client, t, _form(), rec)
    assert first.status_code == 302
    assert second.status_code == 302
    assert second.headers["Location"] == first.headers["Location"], (
        "the replay must reproduce the redirect, not a 302 to nowhere"
    )
    assert len(rec.created) == 2, (
        f"the second identical submit launched again: {len(rec.created)} runs "
        f"created across two clicks of a 2-tool form"
    )


def test_two_different_launches_within_the_ttl_both_run(csrf_app):
    """The bug this exists to prevent, under the settings that caused it.

    With CSRF enforcement on, the key degenerated to sha256(user_id + path), so
    a user who launched rfdiffusion+pxdesign and then, seconds later,
    deliberately launched boltzgen against the same target got the FIRST
    response replayed and no second launch. Nothing in the UI distinguished
    that from a successful launch.
    """
    store = _IdemStore()
    client = csrf_app.test_client()
    _login_with_csrf(client)
    t = _target()
    first_rec, second_rec = _Recorder(), _Recorder()
    with patch("shared.idempotency.get_service_client", return_value=store):
        first = _launch_csrf(client, t, _form(), first_rec)
        second = _launch_csrf(client, t, _other_launch_form(), second_rec)
    assert first.status_code == 302
    assert [kw["tool"] for _, kw in first_rec.created] == [
        "rfdiffusion", "pxdesign",
    ]
    assert second.status_code == 302
    assert [kw["tool"] for _, kw in second_rec.created] == ["boltzgen"], (
        "the second, DIFFERENT launch was answered from the first launch's "
        "cache; the idempotency key is not sensitive to the form body"
    )


def test_the_key_is_form_sensitive_under_production_csrf(csrf_app):
    """Directly, at the key level: two bodies, one route, one user, two keys.

    Asserted through the real `_enforce_csrf` rather than a stand-in, so a
    reordering of that function that stops consuming the stream -- or one that
    starts consuming it somewhere new -- is visible here.
    """
    from shared.idempotency import _compute_key

    client = csrf_app.test_client()
    _login_with_csrf(client)
    t = _target()
    keys = []

    def _capture(*args, **kwargs):
        key = _compute_key(*args, **kwargs)
        keys.append(key)
        return key

    rec = _Recorder()
    with patch("shared.idempotency.get_service_client",
               return_value=_IdemStore()), \
            patch("shared.idempotency._compute_key", side_effect=_capture):
        _launch_csrf(client, t, _form(), rec)
        _launch_csrf(client, t, _other_launch_form(), rec)
    assert len(keys) == 2
    assert keys[0] != keys[1], (
        "both submissions hashed to the same idempotency key, so the second "
        "was a silent replay: the key saw an empty body because _enforce_csrf "
        "had already consumed the stream"
    )


# ---------------------------------------------------------------------------
# A funded campaign is started even if its drive thread cannot be spawned
# ---------------------------------------------------------------------------
#
# No test made fund_campaign or drive_campaign_async raise, which is how a
# whole-launch reporting inversion shipped: the fund/drive loop caught the
# exception and reported EVERY funded campaign as "not started and not
# charged". Since fund_campaign cannot raise (_cas_transition swallows and
# returns False), the only reachable exception was the thread spawn -- exactly
# the case where the money IS committed.


def _launch_with_drive_failure(client, target, recorder, exc):
    def _boom(_campaign_id):
        raise exc

    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=target), \
            patch("blueprints.targets.touch_target"), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=_preauth()), \
            patch("shared.compute_campaigns.create_campaign",
                  side_effect=recorder.create), \
            patch("shared.compute_campaigns.fund_campaign",
                  side_effect=recorder.fund), \
            patch("shared.compute_campaigns.drive_campaign_async",
                  side_effect=_boom):
        return client.post(f"/targets/{target.id}/launch", data=_form())


def test_a_funded_run_whose_drive_thread_cannot_start_is_still_reported_started(
    client,
):
    """`funded` is in cron/tick_campaigns.py::_ACTIVE_STATES, so the tick drives
    it. drive_campaign_async only moves the first wave off the request path.

    Reporting these as stalled told the user "nothing was started and nothing
    was charged" about two funded, billing campaigns.
    """
    _login(client)
    t = _target()
    rec = _Recorder()
    resp = _launch_with_drive_failure(
        client, t, rec, RuntimeError("can't start new thread")
    )
    assert resp.status_code == 302, _visible_text(resp)[-300:]
    assert len(rec.created) == 2
    # Neither run is counted as stalled: both are funded.
    assert "stalled" not in resp.headers["Location"]


def test_a_drive_spawn_failure_does_not_release_the_idempotency_claim(csrf_app):
    """The consequence that turns a wrong banner into a double charge.

    A 400 releases the claim (shared/idempotency.py), so the retry the error
    copy invites would create and fund a SECOND full set against a gate the
    user passed once. Thread exhaustion is process-wide, so all N tools fail
    together and every campaign would have been reported stalled.
    """
    def _boom(_campaign_id):
        raise RuntimeError("can't start new thread")

    store = _IdemStore()
    client = csrf_app.test_client()
    _login_with_csrf(client)
    t = _target()
    first, second = _Recorder(), _Recorder()

    def _post(recorder):
        with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
                patch("blueprints.targets.get_target", return_value=t), \
                patch("blueprints.targets.touch_target"), \
                patch("shared.target_launch.campaign_preauth",
                      return_value=_preauth()), \
                patch("shared.compute_campaigns.create_campaign",
                      side_effect=recorder.create), \
                patch("shared.compute_campaigns.fund_campaign",
                      side_effect=recorder.fund), \
                patch("shared.compute_campaigns.drive_campaign_async",
                      side_effect=_boom):
            return client.post(
                f"/targets/{t.id}/launch", data=dict(_form(), _csrf=_CSRF)
            )

    with patch("shared.idempotency.get_service_client", return_value=store):
        one = _post(first)
        two = _post(second)

    assert one.status_code == 302
    assert len(first.created) == 2
    assert two.status_code == 302
    assert len(second.created) == 0, (
        f"the retry created {len(second.created)} more campaigns; the drive "
        f"failure released the idempotency claim and the launch double-funded"
    )


def test_a_run_left_in_draft_is_the_only_thing_reported_as_stalled(client):
    """The other half: a fund that genuinely did not move the row. That run IS
    inert and unbilled, so "was not charged" is true only for this case."""
    _login(client)
    t = _target()
    rec = _Recorder(fund_results=[True, False])
    resp, rec = _launch(client, t, recorder=rec)
    assert resp.status_code == 302
    assert "stalled=1" in resp.headers["Location"]


def test_the_banner_counts_never_double_count_a_run(client):
    """Started + stalled must equal the launch, with no run in both.

    The two halves come from different places -- `launched_count` from
    `list_campaigns_for_target` (which excludes only `draft`) and
    `stalled_count` from the query param -- so the arithmetic only holds while a
    stalled run is exactly a run still sitting in `draft`. That is what the
    route now guarantees: the fund is the sole commit point, so a campaign is
    either funded (counted by the query, absent from `stalled`) or still draft
    (invisible to the query, counted in `stalled`).

    It did not hold when a funded-but-undriven campaign was reported stalled: it
    is non-draft, so the query counted it AND the param counted it, and a 2-tool
    launch rendered "Started 2 runs" plus "1 more could not be started" -- three
    runs implied where two exist, one of them billing.
    """
    _login(client)
    t = _target()
    group = str(uuid.uuid4())
    # What the page sees after a launch where one of two funds failed: the
    # funded run is returned, the draft one is filtered out server-side.
    body = _squash(_render_detail(
        client, t, f"launched={group}&stalled=1",
        [_run("rfdiffusion", group)],
    ))
    assert "Started 1 run against this target" in body
    assert "1 more could not be started and was not charged" in body
    assert "Started 2 runs" not in body


# ---------------------------------------------------------------------------
# `fund_campaign` returning False is AMBIGUOUS
# ---------------------------------------------------------------------------
#
# `_cas_transition` catches every exception and returns False, so False means
# either "the row was not in draft" or "the UPDATE raised and I cannot tell".
# A write that commits in Postgres while the response read times out lands in
# the second bucket. Only the first justifies telling the user nothing was
# charged.


def _launch_with_fund_result(client, target, recorder, status_after):
    """Fund reports False; the confirming read sees `status_after`."""
    row = SimpleNamespace(id="c1", tool="rfdiffusion", status=status_after)
    get_campaign = None if status_after is None else row

    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=target), \
            patch("blueprints.targets.touch_target",
                  side_effect=recorder.touch), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=_preauth()), \
            patch("shared.compute_campaigns.create_campaign",
                  side_effect=recorder.create), \
            patch("shared.compute_campaigns.fund_campaign",
                  return_value=False), \
            patch("shared.compute_campaigns.get_campaign",
                  return_value=get_campaign), \
            patch("shared.compute_campaigns.drive_campaign_async",
                  side_effect=recorder.drive):
        return client.post(f"/targets/{target.id}/launch", data=_form())


def test_a_confirmed_draft_is_the_only_thing_called_not_charged(client):
    """The one case where "was not charged" is true: the row really is still
    draft, so no hold was placed and nothing will dispatch."""
    _login(client)
    t = _target()
    rec = _Recorder()
    resp = _launch_with_fund_result(client, t, rec, "draft")
    assert resp.status_code == 400
    assert "not charged" in _visible_text(resp)
    # Nothing was driven, because nothing moved.
    assert [k for k, _ in rec.calls if k == "drive"] == []


@pytest.mark.parametrize("status_after", ["funded", "running"])
def test_a_fund_that_actually_moved_the_row_is_reported_started(
    client, status_after,
):
    """False plus a non-draft row means the write landed and the read of its
    result did not. The campaign is billing; saying otherwise invites a
    re-launch that double-funds it."""
    _login(client)
    t = _target()
    rec = _Recorder()
    resp = _launch_with_fund_result(client, t, rec, status_after)
    assert resp.status_code == 302, _visible_text(resp)[-300:]
    assert "stalled" not in resp.headers["Location"]


def test_an_unreadable_row_after_a_false_fund_is_not_called_not_charged(client):
    """Indeterminate. `get_campaign` swallows its own exceptions and returns
    None, so this is the "we cannot tell" case. Claiming money was not
    committed when that is unknown is the expensive direction: it is the one
    that produces a duplicate charge."""
    _login(client)
    t = _target()
    rec = _Recorder()
    resp = _launch_with_fund_result(client, t, rec, None)
    assert resp.status_code == 302
    assert "stalled" not in resp.headers["Location"]


def test_the_confirming_read_is_owner_scoped(client):
    """It resolves a campaign id straight from a launch this user just made, so
    it must still pass user_id: `get_campaign` takes it as an authz filter, not
    as a hint."""
    _login(client)
    t = _target()
    rec = _Recorder()
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.get_target", return_value=t), \
            patch("blueprints.targets.touch_target",
                  side_effect=rec.touch), \
            patch("shared.target_launch.campaign_preauth",
                  return_value=_preauth()), \
            patch("shared.compute_campaigns.create_campaign",
                  side_effect=rec.create), \
            patch("shared.compute_campaigns.fund_campaign",
                  return_value=False), \
            patch("shared.compute_campaigns.get_campaign") as get_campaign, \
            patch("shared.compute_campaigns.drive_campaign_async",
                  side_effect=rec.drive):
        get_campaign.return_value = SimpleNamespace(
            id="c1", tool="rfdiffusion", status="draft",
        )
        client.post(f"/targets/{t.id}/launch", data=_form())
    assert get_campaign.call_args_list
    for call in get_campaign.call_args_list:
        assert call.kwargs.get("user_id") == "u-1", call
