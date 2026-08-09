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
from tests.money_display_guard import assert_template_prints_no_raw_money
from shared.targets import TARGET_READ_OK, DesignTarget, TargetRead


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


def _proteina_target(**kw):
    """A stored target comfortably inside proteina's size cap.

    130 residues is the SMALLEST of the three shards proteina's envelope is
    calibrated from (130 / 260 / 415 aa on an A100-80GB), so it is inside the
    500 cap with a wide margin and inside the measured span rather than the
    extrapolated part of it.

    The proteina tests below are about plumbing — does the contig reach the
    container, are bare hotspots chain-prefixed — so they use a size the gate
    cannot have an opinion about, and the refusal itself is covered by
    `test_proteina_oversized_target_is_refused_before_any_run_is_funded`.

    It was written when the cap was 140 and the default `_target()`'s 210
    residues were over it. That is no longer why it exists: 210 fits now. It
    stays at 130 because a plumbing test should sit far from every boundary,
    not because it has to.
    """
    base = dict(
        name="small antigen",
        filename="small.pdb",
        chain_summary={
            "total_standard_residues": 130,
            "chains": [{
                "chain_id": "A", "standard_residue_count": 130,
                "hetatm_resnames": [], "water_count": 0,
                "min_resnum": 1, "max_resnum": 130,
            }],
        },
    )
    base.update(kw)
    return _target(**base)


def _over_cap_target(**kw):
    """A stored target genuinely over proteina's 500 cap.

    600 residues: above the cap, and above the 415 aa where measurement stops.
    Exists because the sizes that used to be over the cap (210, 415) are all
    inside it now, and a gate test needs a fixture the gate actually refuses.
    """
    base = dict(
        name="big antigen",
        filename="big.pdb",
        chain_summary={
            "total_standard_residues": 600,
            "chains": [{
                "chain_id": "A", "standard_residue_count": 600,
                "hetatm_resnames": [], "water_count": 0,
                "min_resnum": 1, "max_resnum": 600,
            }],
        },
    )
    base.update(kw)
    return _target(**base)


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


# ---------------------------------------------------------------------------
# 2dp DISPLAY strings, and which way they are allowed to lose
# ---------------------------------------------------------------------------
#
# The wire carries 4dp. The page shows 2dp. That conversion used to happen in
# the page, to NEAREST, so a $2.6219 hold printed as "$2.62" -- below the amount
# reserved, directly above a checkbox consenting to "the amount above will be
# held". Now the server ships a *_display string per figure: costs and holds
# round UP, balances round DOWN, neither can flatter us.
#
# rfdiffusion@12 is the cohort because BOTH its totals have sub-half-cent
# digits (budget 2.0101, first wave 2.6219), so ceiling and nearest disagree on
# each. A cohort landing on exact cents would pass this test with the rounding
# direction reversed. One tool means the row figures are the totals, so the row
# encoding is covered by the same assertions.


_DISPLAY_COHORT = "pace=burst&tool=rfdiffusion&designs=12&preset=pilot"

# The cohort that exposed the rows-do-not-sum-to-the-total defect: rows of
# $2.02 + $5.03 against a total ceiled from the exact sum of $7.04, and held
# rows of $2.63 + $6.56 against $9.18. Needs two tools; a single row trivially
# equals its own total.
_TWO_TOOL_COHORT = (
    "pace=burst"
    "&tool=rfdiffusion&designs=12&preset=pilot"
    "&tool=pxdesign&designs=12&preset=pilot"
)

# 12 designs is ONE sub-job per tool, which clamps the first wave, so burst and
# steady price identically: $9.19 either way. Any test whose subject is a
# displayed hold is therefore blind to which pace was passed -- swapping
# `plan.pace` for a hardcoded PACE_BURST in the refusal left 257 tests green.
# These cohorts are chosen so the two paces DIVERGE, and each is asserted with
# _assert_pace_is_observable_on() rather than trusted.
#
#   rfdiffusion+pxdesign @200: burst $100.96 vs steady $36.71, and the burst
#   panel ($100.96) differs from the ceiling of its exact sum ($100.95), so it
#   also observes the rows-do-not-sum defect.
_PACE_OBSERVABLE_COHORT = (
    "pace=burst"
    "&tool=rfdiffusion&designs=200&preset=pilot"
    "&tool=pxdesign&designs=200&preset=pilot"
)
#   bindcraft+rfantibody @200 is the only shape found where the STEADY panel
#   ($419.50) also differs from the ceiling of the steady exact sum ($419.49),
#   which is what the narrow-alternative test needs. Searched, not guessed.
_STEADY_DIVERGENT_COHORT_TOOLS = ("bindcraft", "rfantibody")
_STEADY_DIVERGENT_DESIGNS = 200


def test_the_estimate_ships_display_strings_that_never_understate_a_cost(client):
    """Red if a display figure is dropped, left at 4dp, or rounded to nearest."""
    from decimal import ROUND_CEILING, ROUND_HALF_EVEN

    _login(client)
    data = _estimate(client, _target(), _DISPLAY_COHORT)
    assert data["ok"] is True

    costs = ["budget_usd", "first_wave_usd"]
    # Precondition, asserted rather than assumed: unless ceiling and nearest
    # disagree on these figures, every assertion below passes with the rounding
    # direction reversed and the test pins nothing.
    for key in costs:
        exact = Decimal(data[key])
        assert (
            exact.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
            != exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        ), (
            f"{key}={exact} rounds the same way to nearest as to ceiling, so "
            f"this cohort cannot observe the direction. rfdiffusion@12 can."
        )

    for key in costs:
        exact, shown = Decimal(data[key]), data[f"{key}_display"]
        assert isinstance(shown, str)
        assert Decimal(shown).as_tuple().exponent == -2, f"{key} not 2dp: {shown}"
        # The whole point. A hold shown below the hold taken is not a ceiling.
        assert Decimal(shown) >= exact, f"{key} understated: {shown} < {exact}"
        assert Decimal(shown) == exact.quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )

    # Rows carry their own copies, and the page prints those, not the totals.
    for row in data["rows"]:
        for key in costs:
            assert Decimal(row[f"{key}_display"]) >= Decimal(row[key])


def test_the_estimate_never_overstates_the_balance(client):
    """The mirror image, and the reason there are two helpers rather than one.

    A balance rounded UP claims money the wallet does not have.
    """
    from decimal import ROUND_FLOOR, ROUND_HALF_EVEN

    _login(client)
    balance = "573.6756"
    exact = Decimal(balance)
    # Precondition, asserted rather than assumed, exactly as the sibling cost
    # test does it. An earlier version used 573.6736, where FLOOR and NEAREST
    # are BOTH 573.67 -- so it could only ever have caught a switch to ceiling,
    # and not the switch to nearest this whole change exists to prevent. It
    # passed with display_balance_usd set to ROUND_HALF_UP.
    assert (
        exact.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        != exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    ), f"{balance} floors the same way it rounds to nearest; pick another"

    data = _estimate(
        client, _target(), _DISPLAY_COHORT,
        preauth=_preauth(balance=balance),
    )
    assert data["balance_usd"] == balance
    assert data["balance_usd_display"] == "573.67"
    assert Decimal(data["balance_usd_display"]) <= exact


def test_the_estimate_totals_agree_with_the_rows_printed_above_them(client):
    """The panel has to add up to itself.

    Each row display is ceiled independently, so summing the rows gives a bigger
    number than ceiling the exact total: ``sum(ceil(r)) >= ceil(sum(r))``. When
    the totals were ceiled from the exact sums, the Cost column of
    rfdiffusion@12 + pxdesign@12 printed $2.02 and $5.03 above a Total of $7.04,
    and the held column $2.63 and $6.56 above $9.18 -- one cent short in both,
    directly under a checkbox that says "the amount above will be held".

    The sum is the one part of the panel a reader can check without trusting us,
    so it is the part that must not be wrong. The pre-existing
    ``test_the_estimate_totals_equal_the_sum_of_its_rows`` compares only the 4dp
    fields, which always agreed and still do; it could not see this.
    """
    _login(client)
    data = _estimate(client, _target(), _TWO_TOOL_COHORT)

    for key in ("budget_usd", "first_wave_usd"):
        rows = [Decimal(r[f"{key}_display"]) for r in data["rows"]]
        assert len(rows) > 1, "a one-row cohort cannot observe this"
        assert Decimal(data[f"{key}_display"]) == sum(rows), (
            f"{key}: total {data[f'{key}_display']} != sum of printed rows "
            f"{sum(rows)}"
        )
        # And still a ceiling, which is the reason the displays exist at all.
        assert sum(rows) >= Decimal(data[key])


def test_the_launch_page_does_no_money_rounding_of_its_own(client):
    """The drift guard for the fix above. See tests/money_display_guard.py."""
    assert_template_prints_no_raw_money("templates/targets/launch.html")


def test_the_launch_page_puts_each_figure_in_its_own_slot():
    """A right figure under the wrong label is a wrong figure.

    Putting `first_wave_usd_display` in the Balance slot, or the budget in
    "Held to start", left 292 tests green on both consent pages. This is
    SELECTION, not provenance: statically decidable, simply never checked.
    """
    from tests.money_display_guard import assert_money_slots_are_not_crossed
    assert_money_slots_are_not_crossed("templates/targets/launch.html", {
        "est-budget": "budget_usd_display",
        "est-firstwave": "first_wave_usd_display",
        "est-balance": "balance_usd_display",
    })


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


def test_proteina_designs_against_this_target_not_a_benchmark_task(client):
    """Every run launched from a target page designs against THAT structure.

    This used to assert the opposite — that a curated `task_name` typed here was
    passed through — which produced a campaign whose target_storage_path was the
    user's structure while ++generation.task_name selected a repo-bundled
    benchmark target. The container then refused it, after the campaign existed
    and the hold was placed. Designs filed under target X must have been
    designed against structure X, so the route now declares every launch a
    custom-target run and the adapter refuses a curated task alongside it.
    """
    _login(client)
    t = _proteina_target()
    base = dict(
        tools=["proteina"], proteina__designs="8",
        proteina__preset="protein_binder",
    )
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(**base))
        assert resp.status_code == 302, _visible_text(resp)[-400:]
    params = rec.kwargs_for("proteina")["params"]
    assert params["target_source"] == "custom"
    assert params["task_name"] == ""


def test_proteina_receives_the_shared_hotspots_chain_prefixed(client):
    """One shared hotspot field drives every tool on this screen, and it posts
    bare ints ("42,88"). Proteina needs upstream's chain-prefixed form, so bare
    numbers are promoted onto the run's target chain — that promotion is what
    keeps proteina co-launchable with rfdiffusion/pxdesign from one field."""
    _login(client)
    t = _proteina_target()
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["proteina"], proteina__designs="8",
            proteina__preset="protein_binder",
        ))
        assert resp.status_code == 302, _visible_text(resp)[-400:]
    params = rec.kwargs_for("proteina")["params"]
    assert params["hotspot_spec"] == ["A42", "A88"]
    # Bare ints too, so the routes' DesignTarget.hotspot_error range check
    # keeps working unchanged.
    assert params["hotspot_residues"] == [42, 88]


def test_proteina_and_rfdiffusion_co_launch_from_one_hotspot_field(client):
    """The regression that a chain-prefixed-only parser would have caused."""
    _login(client)
    t = _proteina_target()
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["proteina", "rfdiffusion"],
            proteina__designs="8", proteina__preset="protein_binder",
            rfdiffusion__designs="12",
            rfdiffusion__binder_length_min="55",
            rfdiffusion__binder_length_max="65",
        ))
        assert resp.status_code == 302, _visible_text(resp)[-400:]
    assert rec.kwargs_for("proteina")["params"]["hotspot_spec"] == ["A42", "A88"]
    assert rec.kwargs_for("rfdiffusion")["params"]["hotspot_residues"] == [42, 88]


def test_proteina_target_region_reaches_the_container(client):
    """target_chain used to be accepted by the adapter and never read by the
    pipeline, so a multi-chain target was unreachable. The contig is now the
    single source of truth for both."""
    _login(client)
    t = _proteina_target()
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["proteina"], proteina__designs="8",
            proteina__preset="protein_binder",
            proteina__target_input="A1-130",
            hotspot_residues="A42,A88",
        ))
        assert resp.status_code == 302, _visible_text(resp)[-400:]
    params = rec.kwargs_for("proteina")["params"]
    assert params["target_input"] == "A1-130"
    assert params["target_chain"] == "A"


def test_proteina_oversized_target_is_refused_before_any_run_is_funded(client):
    """THE COST HOLE THIS ROUTE HAD. Nothing on the multi-tool launch path
    called the size envelope, so a target of any size funded a campaign — and
    this route funds one PER SELECTED TOOL, with proteina opening 4 concurrent
    shards at ~$12.58 each inside a ~$15/shard hold that covers all of it.

    A 600-residue target is over proteina's 500 cap, so the launch is refused,
    the message names the tool, and `create_campaign` is never reached — the
    refusal has to land before any money moves, not after.

    THE FIXTURE MOVED WITH THE CAP, and it had to. This was posed on the
    default 210 aa target, which was over the 140 cap of the day; 210 is
    comfortably inside the measured 500 cap now, so leaving it there would
    have turned a money gate into a test that asserts nothing.

    AND THE ASSERTION HAD TO MOVE WITH IT, which the fixture change on its own
    did not do. ``_visible_text`` is the WHOLE document, and
    ``templates/base.html:28`` ships a Google Fonts URL on every rendered page
    containing ``wght@8..60,400;8..60,500;8..60,600``, so "500" and "600" are
    unconditionally present in every response body. ``"600" in body and "500"
    in body`` therefore asserted nothing about this gate: with
    ``hard_cap_target_aa`` mutated 500 -> 100000 the route still returns 400,
    because 600 aa plus the adapter's 120 aa max binder is over the 620-aa
    COMBINED budget, and this test passed. The retired 140/210 pair was safe
    from that only by accident — no font weight is 140. Pinned below as a
    contiguous phrase only the target-size branch of ``_check_size_envelope``
    can emit."""
    _login(client)
    t = _over_cap_target()
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["proteina"], proteina__designs="8",
            proteina__preset="protein_binder",
        ))
    assert resp.status_code == 400, _visible_text(resp)[-400:]
    assert rec.calls == []
    body = _visible_text(resp)
    assert "600 residues, above the 500-residue limit" in body, body[-600:]


def test_a_contig_smaller_than_the_upload_is_sized_on_the_contig(client):
    """Sizing the FILE rather than the SELECTION would refuse runs that fit.

    The same 600 aa target, with a contig naming 100 of its residues, is a
    100-residue run. It has to be allowed: the container designs against the
    contig's selection, so refusing it would force the user to hand-trim a PDB
    to run something the gate would then accept unchanged.

    It uses the over-cap fixture for the same reason the test above does — on
    a 210 aa target both the file and the contig now fit, so nothing here
    would depend on which one the gate counted."""
    _login(client)
    t = _over_cap_target()             # 600 aa, over the cap on its own
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["proteina"], proteina__designs="8",
            proteina__preset="protein_binder",
            proteina__target_input="A1-100",
            hotspot_residues="A42,A88",
        ))
        assert resp.status_code == 302, _visible_text(resp)[-400:]
    assert rec.kwargs_for("proteina")["params"]["target_input"] == "A1-100"


def test_a_target_that_fits_but_whose_complex_does_not_is_refused(client):
    """THE OTHER HALF OF THE ENVELOPE, and it was dead on every money route.

    `hard_cap_combined_aa` fires on (target_aa + binder_max_aa), and no caller
    ever passed `binder_max_aa` — not this route, not either /campaigns branch
    — even though the validated binder length was in scope at all three. So a
    400 aa target with a 300 aa max binder is 700 against proteina's 620
    budget: `/tools/proteina/submit` refused it and this route funded four
    shards for it.

    The target half is deliberately INSIDE the cap here (400 < 500), so a gate
    that only ever reads the target size cannot pass this test. 300 is the
    form's maximum binder length; every canary shard ran at 120.
    """
    _login(client)
    t = _proteina_target(chain_summary={
        "total_standard_residues": 400,
        "chains": [{
            "chain_id": "A", "standard_residue_count": 400,
            "hetatm_resnames": [], "water_count": 0,
            "min_resnum": 1, "max_resnum": 400,
        }],
    })                                 # 400 aa — fits on its own
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["proteina"], proteina__designs="8",
            proteina__preset="protein_binder",
            proteina__binder_length_min="60",
            proteina__binder_length_max="300",
        ))
    assert resp.status_code == 400, _visible_text(resp)[-400:]
    assert rec.calls == []
    body = _visible_text(resp)
    # Contiguous, for the reason spelled out on the target-cap test above:
    # "620" happens not to be in base.html today, and "600" is.
    assert "620-aa combined budget" in body, body[-600:]


def test_a_binder_inside_the_combined_budget_still_launches(client):
    """Guards the fix from over-firing: 130 + 120 = 250 is well under the 620
    budget, and this is the shape all three paid canary shards actually ran."""
    _login(client)
    t = _proteina_target()
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["proteina"], proteina__designs="8",
            proteina__preset="protein_binder",
            proteina__binder_length_min="60",
            proteina__binder_length_max="120",
        ))
        assert resp.status_code == 302, _visible_text(resp)[-400:]
    assert rec.kwargs_for("proteina")["params"]["binder_length"] == [60, 120]


def test_the_combined_cap_reads_rfdiffusions_dict_shaped_binder_length(client):
    """THE SHAPE TRAP. Every adapter names and shapes its binder length
    differently — proteina emits a [min, max] LIST, rfdiffusion a {min, max}
    DICT, pxdesign a bare int, boltzgen/bindcraft a separate
    `binder_length_max` key. A reader that assumes proteina's shape returns
    None for rfdiffusion, and None means "no combined cap" rather than an
    error, so the gate would silently do nothing for it.

    500 aa target + 150 aa binder = 650 against rfdiffusion's 600 budget, with
    the target itself at exactly its 500 cap and therefore NOT over it.
    """
    _login(client)
    t = _target(chain_summary={
        "total_standard_residues": 500,
        "chains": [{
            "chain_id": "A", "standard_residue_count": 500,
            "hetatm_resnames": [], "water_count": 0,
            "min_resnum": 1, "max_resnum": 500,
        }],
    })
    resp, rec = _launch(client, t, form=_form(
        tools=["rfdiffusion"], rfdiffusion__designs="12",
        rfdiffusion__binder_length_min="100",
        rfdiffusion__binder_length_max="150",
    ))
    assert resp.status_code == 400, _visible_text(resp)[-400:]
    assert rec.calls == []
    body = _visible_text(resp)
    # "600" alone would have been supplied by base.html's font URL whatever
    # rfdiffusion's budget is; the phrase can only come from this branch.
    assert "600-aa combined budget" in body, body[-600:]


def test_proteina_motif_variant_is_refused_against_a_stored_target(client):
    """AME tasks resolve from configs/design_tasks/ame_dict_v2.yaml, a registry
    `complexa target add` cannot write, so the motif variant can only scaffold a
    bundled benchmark motif — never this target."""
    _login(client)
    t = _target()
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["proteina"], proteina__designs="8",
            proteina__preset="motif_ame",
        ))
    assert resp.status_code == 400, _visible_text(resp)[-400:]
    assert rec.calls == []


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


# ---------------------------------------------------------------------------
# Container / model capability
#
# `_collect_launch_specs` says of itself: "This, not the form UI, is the guard
# against paying for a mis-configured GPU run, so every check that the
# single-tool create route performs has to happen here too." It ran size checks
# only. This blueprint contained ZERO references to preflight_for_tool, whose
# only callers are blueprints/tools.py (the atomic submit route and its AJAX
# panel) and shared/pdb_intake (the reuse-token path) -- so the gate deciding
# whether the image we dispatch to can even parse a multi-chain target guarded
# the route that spends the least, and this one, which funds ONE CAMPAIGN PER
# SELECTED TOOL, was open.
#
# Executed on trunk against the two-chain target below: bindcraft, rfdiffusion,
# pxdesign and rfantibody all reached create -> fund -> drive.
# ---------------------------------------------------------------------------

def _two_chain_target(**kw):
    """A stored target with two chains, both comfortably inside every cap."""
    base = dict(
        name="Fab", filename="fab.pdb", target_chain="A,B",
        chain_summary={
            "total_standard_residues": 220,
            "chains": [
                {"chain_id": "A", "standard_residue_count": 120,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 1, "max_resnum": 120},
                {"chain_id": "B", "standard_residue_count": 100,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 1, "max_resnum": 100},
            ],
        },
    )
    base.update(kw)
    return _target(**base)


def test_a_model_that_cannot_do_multi_chain_is_refused_before_funding(client):
    """rfantibody is multi_chain_supported=False — it builds a VHH against one
    chain by construction, so no image rebuild will ever make this work. Its
    adapter accepts "A,B" because it only length-checks the field at 4
    characters, and nothing downstream of it looked."""
    _login(client)
    t = _two_chain_target()
    resp, rec = _launch(client, t, form=_form(
        tools=["rfantibody"], target_chain="A,B", rfantibody__designs="4",
    ))
    assert resp.status_code == 400
    assert rec.calls == [], (
        f"a two-chain rfantibody launch reached {[c[0] for c in rec.calls]} — "
        f"the model cannot run this at all"
    )
    body = _visible_text(resp)
    assert "designs against a single target chain" in body, body
    # A MODEL limit is permanent; saying "GPU image" here invites the user to
    # wait for a capability that is never coming.
    assert "GPU image" not in body, body


def test_a_container_that_cannot_do_multi_chain_is_refused_too(client):
    """bindcraft's MODEL can (Pacesa 2024) but its image is unverified — it is
    the one binder tool with no smoke tier, so clearing it costs a full paid
    pilot. The refusal has to name the image, not the model."""
    _login(client)
    t = _two_chain_target()
    resp, rec = _launch(client, t, form=_form(
        tools=["bindcraft"], target_chain="A,B", bindcraft__designs="4",
    ))
    assert resp.status_code == 400
    assert rec.calls == []
    body = _visible_text(resp)
    assert "GPU image still handles" in body, body


def test_the_refusal_names_the_tool_that_cannot_take_the_chains(client):
    """Seven tools on one screen and one shared chain field: a launch refused
    without naming the tool is unactionable."""
    _login(client)
    t = _two_chain_target()
    resp, rec = _launch(client, t, form=_form(
        tools=["rfdiffusion", "rfantibody"], target_chain="A,B",
        rfdiffusion__designs="12", rfantibody__designs="4",
    ))
    assert resp.status_code == 400
    assert rec.calls == [], "all-or-nothing: nothing may be created"
    assert "RFantibody:" in _visible_text(resp)


def test_a_verified_tool_still_launches_against_two_chains(client):
    """THE FALSE-REFUSAL FLOOR. rfdiffusion and pxdesign were both cleared on
    a live A100 against a real two-chain 4ZQK, so the gate must let them
    through — a check that refuses these is as bad as the hole it closes."""
    _login(client)
    t = _two_chain_target()
    resp, rec = _launch(client, t, form=_form(
        tools=["rfdiffusion", "pxdesign"], target_chain="A,B",
    ))
    assert resp.status_code in (302, 303), _visible_text(resp)[:400]
    kinds = [c[0] for c in rec.calls]
    assert kinds[:2] == ["create", "create"]
    assert sorted(kinds[2:]) == ["drive", "drive", "fund", "fund"]
    assert rec.kwargs_for("rfdiffusion")["params"]["target_chain"] == "A,B"


def test_a_single_chain_launch_is_untouched_for_every_tool(client):
    """The backward-compatibility floor. One chain is what every launch before
    multi-chain posted, and the gate must be invisible to it — including for
    the two tools it blocks at two chains."""
    _login(client)
    for tool, extra in (
        ("rfantibody", {"rfantibody__designs": "4"}),
        ("bindcraft", {"bindcraft__designs": "4"}),
        ("rfdiffusion", {"rfdiffusion__designs": "12"}),
    ):
        t = _target()          # single chain A, 210 residues
        resp, rec = _launch(client, t, form=_form(
            tools=[tool], target_chain="A", **extra,
        ))
        assert resp.status_code in (302, 303), (
            f"{tool}: {_visible_text(resp)[:300]}"
        )
        assert [c[0] for c in rec.calls] == ["create", "fund", "drive"], tool


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


def test_the_uncharged_claim_is_derived_and_not_a_literal():
    """A SOURCE guard, because the value cannot be reached at runtime.

    Every ``_err`` call site in ``target_launch_submit`` today has an empty
    ``started``, so ``not started`` is a constant and no behavioural test can
    tell it from ``True``. A reviewer confirmed exactly that: replacing it with
    ``True`` left the whole suite green. The predecessor of this expression was
    a ``funded_any`` flag with the same problem, and swapping one unpinnable
    expression for another was not a fix.

    What it protects is the NEXT error path, added after the fund loop, which
    gets the right answer for free only while the derivation survives. Round 5
    shipped that defect: a 400 telling a user with funded, billing campaigns
    that nothing was charged, and because ``shared/idempotency.py`` releases the
    claim on any status >= 400, the retry it invites funds a second full set.

    This proves the expression's SHAPE and nothing about its value. That is a
    real limit, and it is the reason the template half is tested separately.
    """
    import ast

    src = open("blueprints/targets.py", encoding="utf-8").read()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "target_launch_submit"
    )
    keywords = [
        kw for node in ast.walk(fn) if isinstance(node, ast.Call)
        for kw in node.keywords if kw.arg == "nothing_charged"
    ]
    assert len(keywords) == 1, (
        f"expected exactly one nothing_charged= in the route, found "
        f"{len(keywords)}"
    )
    value = keywords[0].value
    assert not isinstance(value, ast.Constant), (
        "nothing_charged is a literal. It must be derived from what the route "
        "actually did, or a future error path after the fund loop will tell a "
        "charged user they were not charged."
    )
    names = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
    assert "started" in names, (
        f"nothing_charged does not reference `started`; it reads {names}"
    )


@pytest.mark.parametrize("pace", ["burst", "steady"])
def test_the_refusal_sentence_quotes_the_same_hold_as_the_panel(client, pace):
    """One screen, one hold figure.

    The 400 re-renders the estimate panel, which totals its rows' 2dp displays.
    ``preauth_message`` used to round the exact total up instead, and those are
    different numbers (``sum(ceil) >= ceil(sum)``): the sentence said $9.18
    while the panel above it said $9.19, over a consent line reading "the amount
    above will be held". Topping up to the sentence's figure gets refused again.

    Measured when this was written: 128 of 240 refused cohorts printed two
    different holds.

    Red if the route stops passing ``required_display``, or if the panel and the
    sentence are derived by two different roundings again.
    """
    from shared.compute_campaigns import display_total_usd
    from shared.target_launch import ToolLaunchSpec, plan_multi_launch

    _login(client)
    t = _target()
    # bindcraft+rfantibody@100, not rfdiffusion+pxdesign@12. Three preconditions
    # have to hold simultaneously and the original cohort met none of them:
    #   1. the paces must price differently (at 12 designs one sub-job per tool
    #      clamps the first wave and burst == steady, so the pace argument is
    #      unpinnable -- that mutation stayed green across 257 tests);
    #   2. the row sum must differ from the ceiling of the exact sum AT BURST;
    #   3. and the same must hold AT STEADY, or the steady case below is vacuous.
    # 81 cohorts satisfy all three; this is the smallest. Searched, not guessed.
    _assert_pace_is_observable_on(["bindcraft", "rfantibody"], 100)

    # The form is passed EXPLICITLY and the plan is built from the same numbers.
    # The previous version computed rfdiffusion@12 + pxdesign@12 while _form()
    # posts pxdesign@**24**, so the expected figure came from a cohort the route
    # never priced. It passed only because those two different cohorts happen to
    # display the same $9.19. Keep these two in step or the test proves nothing.
    #
    # Parametrized over BOTH paces, which is what actually pins the pace
    # argument. Running burst only, the route's `plan.pace` and a hardcoded
    # PACE_BURST are the same value, so the mutation swapping one for the other
    # survived even after this cohort was widened to 200. Only the steady case
    # can tell them apart.
    form = _form(
        tools=["bindcraft", "rfantibody"], pace=pace,
        bindcraft__designs="100", bindcraft__binder_length_min="50",
        bindcraft__binder_length_max="100",
        rfantibody__designs="100", rfantibody__cdr_lengths="H1:8,H2:7,H3:10-16",
    )
    plan = plan_multi_launch(
        [ToolLaunchSpec(tool=tool, preset="pilot", requested_designs=100,
                        params={}) for tool in ("bindcraft", "rfantibody")],
        pace,
    )
    panel = display_total_usd(r["first_wave_usd_display"] for r in plan.rows())
    # The precondition. If the two roundings agreed on this cohort the test
    # would pass with the bug reinstated.
    from shared.compute_campaigns import display_cost_usd
    assert panel != display_cost_usd(plan.first_wave_usd), (
        "this cohort cannot observe the divergence; pick another"
    )

    resp, _ = _launch(
        client, t, form=form,
        preauth=_preauth(ok=False, reason=PREAUTH_INSUFFICIENT,
                         balance="1", required=str(plan.first_wave_usd)),
    )
    body = resp.get_data(as_text=True)
    assert f"${panel} to start" in body, f"sentence does not quote {panel}"
    assert f"${display_cost_usd(plan.first_wave_usd)} to start" not in body


def test_the_narrow_alternative_quotes_the_panel_it_produces(client):
    """"Starting narrow would need $X" is a promise about the next screen.

    Acting on it re-prices at steady pace and prints a panel; that panel totals
    its rows, so this figure has to be totalled the same way rather than ceiled
    from the steady exact sum.

    Red if the alternative goes back to ``display_cost_usd`` of the total.
    """
    from shared.compute_campaigns import display_cost_usd, display_total_usd
    from shared.target_launch import (
        PACE_STEADY, ToolLaunchSpec, first_wave_at_pace, plan_multi_launch,
    )

    _login(client)
    # bindcraft+rfantibody @200, not rfdiffusion+pxdesign @12. Two independent
    # preconditions have to hold at once and only this shape satisfies both:
    # the paces must diverge (else PACE_STEADY is unpinned), and the STEADY
    # panel must differ from the ceiling of the steady exact sum (else the
    # row-sum-versus-ceiling assertion is vacuous). Searched, not guessed.
    _assert_pace_is_observable_on(
        list(_STEADY_DIVERGENT_COHORT_TOOLS), _STEADY_DIVERGENT_DESIGNS
    )
    specs = [ToolLaunchSpec(tool=tool, preset="pilot",
                            requested_designs=_STEADY_DIVERGENT_DESIGNS,
                            params={})
             for tool in _STEADY_DIVERGENT_COHORT_TOOLS]
    steady_plan = plan_multi_launch(specs, PACE_STEADY)
    steady_panel = display_total_usd(
        r["first_wave_usd_display"] for r in steady_plan.rows()
    )
    # The precondition its sibling asserts and this one did not. The two
    # roundings agree in 56 of 120 2- to 7-tool cohorts, so on the wrong cohort
    # this test passes with the bug reinstated and says nothing. That is the
    # exact failure round 7 shipped, and leaving it to the cohort's good luck
    # is how it recurs.
    assert steady_panel != display_cost_usd(
        first_wave_at_pace(plan_multi_launch(specs, "burst"), PACE_STEADY)
    ), "this cohort cannot observe the divergence; pick another"

    # The query is built from the SAME tools and count the plan above uses.
    # Its sibling was passing on a query the route priced differently from the
    # plan the test computed, and only agreed because two unrelated cohorts
    # rendered the same string.
    query = "pace=burst" + "".join(
        f"&tool={tool}&designs={_STEADY_DIVERGENT_DESIGNS}&preset=pilot"
        for tool in _STEADY_DIVERGENT_COHORT_TOOLS
    )
    data = _estimate(
        client, _target(), query,
        preauth=_preauth(ok=False, reason=PREAUTH_INSUFFICIENT,
                         balance=str(steady_plan.first_wave_usd)),
    )
    assert data.get("alternative"), "no narrow alternative offered"
    assert data["alternative"]["first_wave_usd_display"] == steady_panel


def test_the_pace_helper_reproduces_the_row_sum(client):
    """The two ways of computing the displayed hold must not drift.

    ``first_wave_display_at_pace`` exists for callers with no rows in hand, and
    its docstring claims it equals ``display_total_usd`` over ``plan.rows()`` at
    the plan's own pace. That equality is the whole reason it is safe to use in
    the refusal sentence, so it is asserted rather than inspected.
    """
    from shared.compute_campaigns import display_total_usd
    from shared.target_launch import (
        PACE_BURST, PACE_STEADY, ToolLaunchSpec, first_wave_display_at_pace,
        plan_multi_launch,
    )

    cohorts = [("rfdiffusion",), ("rfdiffusion", "pxdesign"),
               ("rfdiffusion", "pxdesign", "boltzgen")]
    for tools in cohorts:
        for pace in (PACE_BURST, PACE_STEADY):
            plan = plan_multi_launch(
                [ToolLaunchSpec(tool=t, preset="pilot", requested_designs=12,
                                params={}) for t in tools],
                pace,
            )
            assert first_wave_display_at_pace(plan, plan.pace) == (
                display_total_usd(
                    r["first_wave_usd_display"] for r in plan.rows()
                )
            ), f"{tools} at {pace}"


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
    t = _proteina_target()
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
    """The uncharged claim now comes from the TEMPLATE, gated on the route's
    money state, so this asserts the rendered sentence rather than a phrase that
    used to be baked into the route's message. Both halves matter: the route
    must say nothing started, and the page must be willing to say nothing was
    charged, which it only does while no campaign has been funded."""
    _login(client)
    t = _target()
    rec = _Recorder(fund_results=[False, False])
    resp, rec = _launch(client, t, recorder=rec)
    assert resp.status_code == 400
    body = _squash(resp.get_data(as_text=True))
    assert "None of those runs could be started." in body
    assert "Nothing was started and nothing was charged." in body
    assert [kind for kind, _ in rec.calls if kind == "drive"] == []
    # Once, not twice. The route's message used to carry the claim as well as
    # the template, so the panel printed it in consecutive sentences.
    assert body.count("nothing was charged") == 1


def test_the_launch_page_makes_no_money_claim_unless_the_route_makes_it(app):
    """The template must not infer "nothing was charged" from the presence of an
    error. Whether a campaign was funded is not a fact a template has, and this
    is the most expensive sentence on the page to get wrong: said falsely it
    invites a relaunch, and because an error here is a 400 the idempotency claim
    has already been released, so that relaunch funds a second full set against
    a gate the user passed once.

    Pins the DEFAULT too. `_launch_context` leaves `nothing_charged` False, so a
    render path that forgets to pass it stays silent instead of guessing. Red if
    the sentence is moved back outside the conditional, and red if the default
    flips to True.
    """
    from flask import render_template

    from blueprints.targets import _launch_context

    t = _target()
    with app.test_request_context(f"/targets/{t.id}/launch"):
        silent = render_template(
            "targets/launch.html", **_launch_context(t, error="Boom."),
        )
        claimed = render_template(
            "targets/launch.html",
            **_launch_context(t, error="Boom.", nothing_charged=True),
        )
    # The error itself renders either way; only the money claim is gated.
    assert "Boom." in silent and "Boom." in claimed
    assert "nothing was charged" not in silent
    assert "nothing was charged" in claimed


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
            patch("blueprints.targets.read_target",
                  return_value=TargetRead(t, TARGET_READ_OK)), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_detail_agg(runs)):
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


def _detail_agg(runs=(), **over):
    """A minimal ``aggregate_target_candidates`` envelope carrying ``runs``.

    Phase 3 moved the detail page's run list out of a direct
    ``list_campaigns_for_target`` call and into the aggregator, which binds that
    name at its own module level, so the old patch target no longer reaches it.

    The banner tests below still count through the REAL filter: the route
    receives this list and applies its own ``launch_group_id`` matching to it.
    Only where the list comes from has changed.
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
    return env


def _render_detail(client, target, query, runs):
    # `target_detail` resolves its parent through the THREE-outcome `read_target`
    # (register item A90), so this patches that and not `get_target`. The other
    # /targets/* routes exercised in this file still use `get_target`.
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target",
                  return_value=TargetRead(target, TARGET_READ_OK)), \
            patch("blueprints.targets.aggregate_target_candidates",
                  return_value=_detail_agg(runs)):
        return client.get(f"/targets/{target.id}?{query}").get_data(as_text=True)


def _run(tool, group, run_id="c1", status="funded", designs=12):
    return SimpleNamespace(
        id=run_id, tool=tool, name=None, status=status,
        requested_designs=designs, total_subjobs=1, launch_group_id=group,
    )


# ---------------------------------------------------------------------------
# Detail-page banner, counted through the REAL query
# ---------------------------------------------------------------------------
#
# `_render_detail` above patches `list_campaigns_for_target` out and hands the
# template a fixed list, which is right for testing the template's wording and
# WRONG for testing the arithmetic: `launched_count` is then a property of the
# fixture, so no change to the draft filter or to the launch route can move it.
# A test that claimed to pin the count against double-counting passed with
# `include_drafts=True` and passed again with a drive-spawn failure re-routed to
# `stalled`, because neither can alter a hardcoded list.
#
# These seed rows into a fake client instead and let the real function filter
# them, so the count is produced by the code under test.


def _row(tool, group, *, target_id, row_id="c1", status="funded", designs=12,
         user_id="u-1"):
    """A campaign row shaped as PostgREST returns it, not a namespace.

    `from_row` subscripts id/user_id/tool/preset/status directly, so a partial
    dict here would raise rather than fail an assertion.

    ``target_id`` is keyword-only with NO default, so every caller states the
    target it belongs to: the query filters on it, and a helper that stamped it
    in silently would keep passing if that filter were dropped. ``user_id`` is
    keyword-only but does default, so this rationale does not cover it; the
    owner filter is pinned separately by the cross-tenant tests.
    """
    return {
        "id": row_id, "user_id": user_id, "tool": tool, "preset": "pilot",
        "status": status, "requested_designs": designs, "chunk_size": designs,
        "total_subjobs": 1, "launch_group_id": group, "created_at": "2026-07-30",
        "budget_usd": "1.0000", "name": None, "target_id": target_id,
    }


class _CampaignQuery:
    """Just enough of the builder `list_campaigns_for_target` actually uses.

    Models `neq` because the draft filter is applied SERVER-side: an in-memory
    filter here would let the page-budget behaviour diverge from production.
    """

    def __init__(self, rows):
        self._rows = rows
        self._eq = {}
        self._neq = {}

    def select(self, *_a, **_kw):
        return self

    def order(self, *_a, **_kw):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def neq(self, col, val):
        self._neq[col] = val
        return self

    def range(self, start, end):
        self._slice = (start, end)
        return self

    def execute(self):
        kept = [
            r for r in self._rows
            if all(r.get(c) == v for c, v in self._eq.items())
            # PostgREST renders neq as `col <> val`, which is NULL and so
            # DROPS the row when the column is NULL. Python `!=` KEEPS it,
            # which is the opposite answer and decides whether a malformed row
            # can reach the caller at all. Matches the sibling fake in
            # tests/test_compute_campaigns.py.
            and all(
                r.get(c) is not None and r.get(c) != v
                for c, v in self._neq.items()
            )
        ]
        start, end = getattr(self, "_slice", (0, len(kept) - 1))
        return SimpleNamespace(data=kept[start:end + 1])


class _CampaignClient:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return _CampaignQuery(self.rows)


def _render_detail_through_the_query(client, target, query, rows):
    """Render the detail page with the run list produced by the real query.

    Deliberately does NOT patch ``aggregate_target_candidates``: the whole point
    of this helper is that ``list_campaigns_for_target`` really runs, so the
    banner counts come out of the real filter rather than a hand-built list.

    Phase 3 moved that call inside the aggregator, which resolves its own
    ``get_target`` and its own service client at ITS module level. Both are
    patched here so the aggregator reaches the campaign read; the read itself
    is still the real one against ``_CampaignClient``.
    """
    with patch("blueprints.targets.load_user_context", return_value=_ctx()), \
            patch("blueprints.targets.read_target",
                  return_value=TargetRead(target, TARGET_READ_OK)), \
            patch("shared.target_results.get_target", return_value=target), \
            patch("shared.target_results.get_service_client",
                  return_value=_CampaignClient(rows)), \
            patch("shared.compute_campaigns.get_service_client",
                  return_value=_CampaignClient(rows)):
        return client.get(f"/targets/{target.id}?{query}").get_data(as_text=True)


@pytest.mark.parametrize("stalled,expected", [
    (1, "1 more could not be started and was not charged"),
    (3, "3 more could not be started and were not charged"),
])
def test_the_stalled_banner_renders_when_some_runs_did_not_start(
    client, stalled, expected,
):
    """The partial-failure path a user actually hits when the wallet drains
    mid-launch. No prior test combined a MATCHING launch group with a non-zero
    ``stalled``: the one test that passed ``stalled=99`` paired it with an
    unknown group, and the one with a matching group passed no ``stalled`` at
    all. Between them the stalled copy never rendered and its singular/plural
    wording never executed.

    This case pairs it with a matching group, so it covers the "N more" wording.
    The no-match wording is a separate test below, because the two halves of the
    banner are now gated independently.

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


def test_an_unknown_launch_group_starts_nothing_but_still_reports_stalled(
    client,
):
    """The two halves are gated independently, and this is the cost of that.

    "Started N" needs a real match, so an unknown group claims nothing started.
    ``stalled`` rides the param alone, so a crafted one DOES render -- and that
    is deliberate, not an oversight. The template cannot distinguish a crafted
    count from the case this gating exists for: a real launch that funded some
    campaigns and stranded others, where the run query then came back empty
    because the same connection fault broke it too. Suppressing the crafted
    count means suppressing that one, and only one of the two misleads someone
    other than its author.
    """
    _login(client)
    t = _target()
    body = _squash(_render_detail_through_the_query(
        client, t, f"launched={uuid.uuid4()}&stalled=99", [],
    ))
    assert "Started" not in body
    assert "99 runs could not be started and were not charged" in body


def test_the_stalled_half_survives_an_empty_run_query(client):
    """The regression this gating exists for, and the reason it is not nested.

    `launched_count` comes from `list_campaigns_for_target`, which excludes
    `draft` AND returns the rows read so far if a page fails. So a launch that
    funded some campaigns and stranded others can land here with nothing in the
    list: either that read broke, or the fund outcome was MIXED, with the
    stalled ones confirmed draft and the rest taking the launch route's "row
    unreadable, treat as started" fall-through while in fact still draft, so
    this query excludes them too. Both are the SAME fault that stranded them, so
    the nested version went dark in precisely the case it was written to report
    -- the user funded real compute, one run did not start, and the page said
    nothing at all.

    It has to be MIXED. If EVERY campaign took that fall-through then nothing is
    stalled, the route drops the query param entirely, and there is no
    disclosure for the nesting to suppress.

    Red if the stalled block is nested back inside the started block.
    """
    _login(client)
    t = _target()
    group = str(uuid.uuid4())
    body = _squash(_render_detail_through_the_query(
        client, t, f"launched={group}&stalled=2", [],
    ))
    assert "Started" not in body
    # "2 more" would be wrong with no "Started N" line above it to be more than.
    assert "2 runs could not be started and were not charged" in body
    assert "2 more" not in body


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
        #
        # Accepts BOTH spellings of the null predicate. Of the 17 `.is_()` call
        # sites in this repo's production code, only the 2 in
        # `shared/idempotency.py` pass None; the other 15 pass the PostgREST
        # string "null" (shared/api_keys.py x4, shared/targets.py x4,
        # shared/jobs.py x3, shared/handoffs.py, shared/compute_campaigns.py,
        # cron/purge_old_storage.py, webhooks/modal.py). Both spellings are
        # valid. Refusing the string would not have caught anything either: an
        # earlier version of this fake asserted `val is None` on the stated
        # grounds that a refusal here escapes, and that was WRONG -- the builder
        # chain in both _release_key and _store_response sits INSIDE the try
        # whose bare `except Exception` swallows it. See the note on delete().
        assert val is None or val == "null", (
            f"is_({col!r}, {val!r}) is not a null predicate"
        )
        self._is_null.append(col)
        return self

    def upsert(self, payload, on_conflict="key"):
        self._upsert = dict(payload)
        return self

    def update(self, payload):
        self._update = dict(payload)
        return self

    def delete(self):
        # Modelled rather than raising, because a raise here is invisible:
        # `_release_key` wraps the whole chain in a bare `except Exception`, so
        # raising is swallowed into the same `False` as omitting the method
        # entirely, and the wrapper then CACHES the failure instead of releasing.
        #
        # No test in THIS file reaches the release path -- every launch here ends
        # 302 or 400-before-commit, and removing this method changes no result.
        # tests/test_idempotency.py owns that path. It is modelled anyway because
        # the release leg only becomes observable under a mutation that releases
        # regardless of status, and a stub that inverted the behaviour under that
        # mutation would make the double-fund assertion unfailable in exactly the
        # direction it exists to catch.
        #
        # The same applies to `is_` above: nothing in this fake can usefully
        # refuse, because every call site swallows. Assertions here document the
        # contract for a reader; they are not a guard.
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
    `_store_response` depends on it, and the delete path (see `_IdemTable`),
    which `execute` completes by popping the row -- so `store.rows == {}` after
    a 4xx is a real observation of the release. An earlier version of this
    docstring said the delete path was NOT modelled, which contradicted the
    method sitting directly above it; tests/test_idempotency.py owns the
    exhaustive coverage of that path, not its only model.
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

    Seeded through the real query rather than handed a fixed list, because
    `launched_count` IS the thing under test. An earlier version of this test
    patched `list_campaigns_for_target` and asserted against a hardcoded
    one-element list, which made "Started 2 runs" unreachable by construction:
    both `include_drafts=True` and re-routing a drive-spawn failure to `stalled`
    left it green.
    """
    _login(client)
    t = _target()
    group = str(uuid.uuid4())
    # Exactly the shape a launch leaves when one of two funds failed: both rows
    # exist, one funded and one still draft. Only the filter decides the count.
    body = _squash(_render_detail_through_the_query(
        client, t, f"launched={group}&stalled=1",
        [
            _row("rfdiffusion", group, target_id=t.id, row_id="c1",
                 status="funded"),
            _row("pxdesign", group, target_id=t.id, row_id="c2",
                 status="draft"),
        ],
    ))
    assert "Started 1 run against this target" in body
    assert "1 more could not be started and was not charged" in body
    # The draft must not be counted as started. With the filter widened this
    # reads "Started 2 runs" while `stalled=1` still claims one did not.
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
    """The one case where "nothing was charged" is true: the row really is still
    draft, so no hold was placed and nothing will dispatch."""
    _login(client)
    t = _target()
    rec = _Recorder()
    resp = _launch_with_fund_result(client, t, rec, "draft")
    assert resp.status_code == 400
    assert "nothing was charged" in _visible_text(resp)
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


# ---------------------------------------------------------------------------
# The chain letter has to survive from the form to the range gate
#
# proteina's validate() emits the hotspot twice: `hotspot_spec` (["B520"],
# which build_payload ships and the container matches on) and
# `hotspot_residues` ([520], the same token with the chain letter stripped).
# This route range-checked the stripped one. On a two-chain contig that field
# cannot distinguish "the user typed 520" from "the user typed B520", so
# reading it as the first chain refused BOTH -- including the canonical
# multi-chain pick, which is a false refusal on a route whose whole job is to
# spend money.
# ---------------------------------------------------------------------------

def _asymmetric_target(**kw):
    """Two chains with DISJOINT numbering: A 1..40, B 500..539.

    Disjoint is the whole point. On a homodimer both protomers carry the same
    numbers, so "is 520 on chain B" and "is 520 on any chain" agree and the
    fixture cannot tell a chain-aware gate from a unioning one.
    """
    base = dict(
        name="Fab HL",
        filename="fab.pdb",
        chain_summary={
            "total_standard_residues": 80,
            "chains": [
                {"chain_id": "A", "standard_residue_count": 40,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 1, "max_resnum": 40},
                {"chain_id": "B", "standard_residue_count": 40,
                 "hetatm_resnames": [], "water_count": 0,
                 "min_resnum": 500, "max_resnum": 539},
            ],
        },
    )
    base.update(kw)
    return _target(**base)


def _proteina_launch_form(hotspots: str, **kw):
    base = dict(
        tools=["proteina"],
        proteina__designs="8",
        proteina__preset="protein_binder",
        proteina__target_input="A1-40,B500-539",
        hotspot_residues=hotspots,
    )
    base.update(kw)
    return _form(**base)


@pytest.mark.parametrize("typed,expected_spec", [
    ("B520", ["B520"]),
    ("A20 B520", ["A20", "B520"]),
])
def test_a_chain_prefixed_hotspot_funds_the_launch(client, typed, expected_spec):
    """RED on a492b71: refused with "520 ... outside this target's chain(s)"
    for a hotspot that is plainly inside B 500-539 and ships as B520."""
    _login(client)
    t = _asymmetric_target()
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_proteina_launch_form(typed))
    assert resp.status_code == 302, _visible_text(resp)[-600:]
    params = rec.kwargs_for("proteina")["params"]
    assert params["hotspot_spec"] == expected_spec
    # And the run really was funded and driven, not merely created.
    assert ("fund", "c-0") in rec.calls or any(
        c[0] == "fund" for c in rec.calls
    ), rec.calls
    assert any(c[0] == "drive" for c in rec.calls), rec.calls


def test_a_bare_hotspot_off_the_first_chain_is_still_refused_here(client):
    """The A1 defect this route was fixed for. Typed bare, 520 is promoted onto
    the contig's FIRST chain and ships as "A520" against a chain that stops at
    40 -- so nothing may be created, funded or driven."""
    _login(client)
    t = _asymmetric_target()
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_proteina_launch_form("520"))
    assert resp.status_code == 400, _visible_text(resp)[-600:]
    assert rec.calls == [], rec.calls
    assert "A520" in _visible_text(resp), _visible_text(resp)[-600:]


def test_single_chain_bare_hotspots_still_launch_unchanged(client):
    """The backward-compatibility floor for this route: one chain, bare ints,
    which is every proteina launch posted before the contig existed."""
    _login(client)
    t = _proteina_target()          # single chain A, 1..130
    with patch.dict("os.environ", {"FLAG_TOOL_PROTEINA": "on"}):
        resp, rec = _launch(client, t, form=_form(
            tools=["proteina"], proteina__designs="8",
            proteina__preset="protein_binder",
        ))
    assert resp.status_code == 302, _visible_text(resp)[-600:]
    params = rec.kwargs_for("proteina")["params"]
    assert params["hotspot_residues"] == [42, 88]
    assert params["hotspot_spec"] == ["A42", "A88"]
