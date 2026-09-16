"""A preset that promises no wallet charge must price at zero.

Reported from tools.ranomics.com 2026-09-11 (main @ ca36d70, signed in; not
re-observed here): the preset labelled ``Validate (free dry-run)`` showed
"Estimated cost $12.59" and a "Balance after this job" reduced by that much.
The estimator reproduces that figure exactly -- $12.5827, which the panel
ceils to cents. The sentence it contradicts -- "... No wallet charge." -- is
the preset's ``description``, which renders on /help/tools/proteina, not on
the form (templates/tools/proteina_form.html:117 emits the slug and the
label, never the description). That description also called the tier
"CPU-only", which this change deleted as false on its own terms: the tier
does no GPU work but holds an A100 for its whole lifetime.
``shared/wallet_estimates.py`` had no ``tier_gpu_seconds`` row for
``validate``, so the preset inherited ``expected_gpu_seconds``: proteina's
full 7200 s A100-80GB session ceiling, $12.5827 marked up.

Not only a display defect. For a signed-in user with a wallet row, an
estimate above 0 is what puts ``shared/wallet_guard.py`` on the hold path, so
a submit WOULD have sized a hold with ``cushioned_hold_usd`` and reserved $15
(the cushion clamped to base_hard_cap). Would, not did: that is the estimator
and the gate traced and re-run in-process, not a completed validate job.

These tests read the ESTIMATOR and the GATE, never the rendered panel. The
panel is drawn client-side from ``/api/wallet/estimate``
(``templates/wallet/_partials.html``), so an assertion on its markup would
pass whatever number the endpoint returned.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from flask import Flask, g

import app  # noqa: F401  -- importing the app is what registers the adapters
from shared.wallet_estimates import (
    compute_hard_cap,
    cushioned_hold_usd,
    estimated_cost_for_tool,
)
from shared.wallet_guard import requires_wallet
from tools.base import _REGISTRY

# Phrases that promise the USER pays nothing, matched over a preset's label
# plus description. Deliberately NOT bare "free": esmfold2-design's
# ``minibinder`` preset reads "Free 60 to 200 aa scaffold ... No framework
# constraints", where free means unconstrained by a framework, and that run
# is billed on an H100 like any other.
_NO_CHARGE_PHRASES = (
    "no wallet charge",
    "no charge",
    "free dry-run",
    "free dry run",
)

# A healthy wallet row, so the REAL wallet_preflight can run offline.
_WALLET_OK = {"balance_usd": 100.0, "wallet_frozen": False}


@pytest.fixture
def offline_estimator():
    """Cut the p90 lookup off from Supabase.

    The tier row keeps ``validate`` out of the historical branch, but the
    PAID presets below have no tier entry and do reach it, and a regression
    that deletes the row sends validate there too. Either way the fallback
    must be deterministic rather than whatever prod happens to hold.
    """
    with patch("shared.credits.get_service_client", return_value=None):
        yield


def _presets_promising_no_charge() -> list[tuple[str, str]]:
    """Every ``(tool_slug, preset_slug)`` matching ``_NO_CHARGE_PHRASES``."""
    found: list[tuple[str, str]] = []
    for tool_slug, adapter in sorted(_REGISTRY.items()):
        for preset in getattr(adapter, "presets", None) or ():
            text = f"{preset.label or ''} {preset.description or ''}".lower()
            if any(phrase in text for phrase in _NO_CHARGE_PHRASES):
                found.append((tool_slug, preset.slug))
    return found


def test_the_adapter_registry_is_populated():
    """``tools.base._REGISTRY`` is empty without the ``import app`` above.

    The sweep below would not go green on an empty registry -- its inertness
    guard fails first -- but it would fail as "proteina/validate not in []",
    which reads like the tier row went missing. This says what broke.
    """
    assert len(_REGISTRY) >= 10


def test_every_preset_that_promises_no_charge_estimates_zero(offline_estimator):
    free = _presets_promising_no_charge()
    # Inertness guard: if the phrase list stops matching (a reworded
    # description, an escaped entity), the loop asserts nothing and this
    # test goes quietly green on a preset that charges again.
    assert ("proteina", "validate") in free, f"matcher found {free}"
    for tool_slug, preset_slug in free:
        estimate = estimated_cost_for_tool(None, tool_slug, {"preset": preset_slug})
        assert estimate == Decimal("0"), (
            f"{tool_slug}/{preset_slug} advertises no charge but estimates "
            f"${estimate}"
        )


def test_proteina_paid_presets_still_cost(offline_estimator):
    """Control: the zero above is the tier row, not a dead estimator.

    Each of these priced at $12.5827 before the tier row was added and still
    does; only ``validate`` moved.
    """
    for preset in ("protein_binder", "ligand_binder", "motif_ame"):
        estimate = estimated_cost_for_tool(None, "proteina", {"preset": preset})
        assert estimate == Decimal("12.5827"), f"{preset} now estimates ${estimate}"


def test_validate_stays_free_at_any_design_count(offline_estimator):
    """``_scale_seconds`` multiplies the tier's 0 by the design ratio.

    The form's ``num_designs`` reaches the estimator on every submit, so a
    large value must not scale a free preset back into a charge.
    """
    estimate = estimated_cost_for_tool(
        None, "proteina", {"preset": "validate", "num_designs": 500}
    )
    assert estimate == Decimal("0")


@pytest.mark.parametrize("tool,param", [
    ("proteina", "num_designs"),
    ("af2", "n_designs_total"),
    ("mpnn", "num_seq_per_target"),
])
def test_a_non_finite_scaling_value_does_not_raise(offline_estimator, tool, param):
    """NaN and the infinities must not reach either Decimal ladder.

    ``float()`` accepts all three by name and both param builders keep what it
    returns. NaN is the one that RAISES: ``min(Decimal("NaN"), cap)`` is an
    InvalidOperation, long a 500 on /api/wallet/estimate and -- once a free run
    started going through ``wallet_preflight``, whose ``compute_hard_cap`` call
    sits outside any try -- a 500 on the submit route too. The infinities do
    not raise; they peg both ladders to the tool's absolute cap, which is a
    silent over-quote rather than a crash. Not proteina-specific: 13 of the 15
    specs have a scaling param (``alphafold2`` and ``opendde`` are the two
    without).
    """
    # 10**400 is the OverflowError case, not the isfinite one: ``int()``
    # accepts 4300 digits so a form field carries it here intact, ``float()``
    # raises rather than returning inf, and OverflowError is not a ValueError.
    # It reaches this module on ANY key, not just the scaling one.
    for value in (
        float("nan"), float("inf"), float("-inf"), 1e400, "nan", "inf", 10**400,
    ):
        params = {"preset": "x", param: value, "campaign_label": value}
        estimate = estimated_cost_for_tool(None, tool, params)
        cap = compute_hard_cap(tool, params)
        hold = cushioned_hold_usd(None, tool, params)
        assert estimate >= Decimal("0")
        assert cap > Decimal("0")
        assert hold >= Decimal("0")


def test_a_non_finite_design_count_does_not_500_the_submit_route(offline_estimator):
    """The gate itself, not just the estimator: the whole POST must survive.

    ``wallet_preflight`` calls ``compute_hard_cap`` with the same params that
    reached the estimator, outside any try. This is the shape that regressed
    when a zero estimate stopped returning early.
    """
    seen: dict = {}
    gate_app = _gate_app("proteina", seen)

    with gate_app.test_client() as client, patch(
        "shared.wallet_guard.wallet_reserve_hold", return_value="tx-001"
    ), patch(
        "shared.wallet_guard.get_or_create_wallet", return_value=_WALLET_OK
    ), patch(
        "shared.wallet.get_or_create_wallet", return_value=_WALLET_OK
    ):
        with client.session_transaction() as sess:
            sess["user_id"] = "u-1"
        resp = client.post(
            "/x", data={"preset": "protein_binder", "num_designs": "nan"}
        )
        # A 400-digit int on a key that is not the scaling param at all:
        # ``_wallet_params_from_form`` coerces it with ``int()`` and every
        # value in the dict reaches the scrubber.
        huge = client.post(
            "/x",
            data={
                "preset": "protein_binder",
                "num_designs": "8",
                "campaign_label": "1" + "0" * 400,
            },
        )

    assert resp.status_code == 200, "a nan design count 500s the submit route"
    assert huge.status_code == 200, "a 400-digit field 500s the submit route"
    assert seen.get("ran") is True


def test_a_whitespace_padded_preset_is_still_free(offline_estimator):
    """The adapter strips the preset before matching; the estimator must too.

    ``tools/proteina/__init__.py::validate`` does ``.strip()``, so a padded
    " validate" RUNS the free pre-flight. The SUBMIT path never disagreed --
    ``_wallet_params_from_form`` strips before the estimator sees it -- but
    /api/wallet/estimate does not strip its query args, so while the estimator
    did not either, that panel displayed $12.5827 for a preset the adapter
    would have run for free.

    The cased variant is an estimator-side assertion only: that adapter strips
    without lowercasing, so " VALIDATE " is refused with "Pick a design
    variant" rather than run free. $0 for an input that never runs is the
    harmless direction.
    """
    for preset in (" validate", "validate ", " VALIDATE "):
        estimate = estimated_cost_for_tool(None, "proteina", {"preset": preset})
        assert estimate == Decimal("0"), f"{preset!r} estimates ${estimate}"


def _gate_app(tool_slug: str, seen: dict) -> Flask:
    """A one-route Flask app whose only view is behind ``@requires_wallet``."""
    gate_app = Flask(__name__)
    gate_app.config["SECRET_KEY"] = "k"

    @requires_wallet(tool_slug=tool_slug)
    def handler():
        seen["ran"] = True
        seen["hold_tx_id"] = getattr(g, "wallet_hold_tx_id", "unset")
        seen["estimate"] = getattr(g, "wallet_estimate_usd", "unset")
        seen["params"] = getattr(g, "wallet_params", "unset")
        # What the real view sets once create_job has stashed the hold id;
        # without it the decorator releases the hold on the way out.
        g.wallet_hold_consumed = True
        return "ok", 200

    gate_app.add_url_rule("/x", view_func=handler, methods=["POST"])
    # _render_topup_gate resolves url_for("tools.tool_form"); without this
    # endpoint a blocked submit raises BuildError instead of rendering.
    gate_app.add_url_rule(
        "/tools/<tool>", endpoint="tools.tool_form", view_func=lambda tool: "form"
    )
    return gate_app


def test_validate_submit_reserves_nothing(offline_estimator):
    """The free preset reaches the handler with no hold placed.

    ``wallet_preflight`` is the REAL one here, reading a healthy wallet: the
    point is that the free run goes through the gate and comes out the other
    side with nothing reserved, not that it goes around it.
    """
    seen: dict = {}
    gate_app = _gate_app("proteina", seen)

    with gate_app.test_client() as client, patch(
        "shared.wallet_guard.wallet_reserve_hold"
    ) as reserve, patch(
        "shared.wallet_guard.get_or_create_wallet", return_value=_WALLET_OK
    ), patch(
        "shared.wallet.get_or_create_wallet", return_value=_WALLET_OK
    ):
        with client.session_transaction() as sess:
            sess["user_id"] = "u-1"
        resp = client.post("/x", data={"preset": "validate", "num_designs": "8"})

    assert resp.status_code == 200
    assert seen.get("ran") is True
    assert seen.get("estimate") == Decimal("0")
    assert seen.get("hold_tx_id") is None
    reserve.assert_not_called()
    # NOT the no-user branch, which sets those same three values a few lines
    # earlier in the decorator and would let this test pass with no session at
    # all. That branch stashes an EMPTY params dict; the free-run branch
    # stashes the parsed form.
    assert seen.get("params", {}).get("preset") == "validate"


def test_a_frozen_wallet_still_refuses_a_free_run(offline_estimator):
    """Free means nothing is charged, not that nothing is checked.

    A paid submit meets ``wallet_frozen`` in ``wallet_preflight``
    (shared/wallet.py:498) and again in the SQL ``try_hold_for_job``. A free
    run takes no hold, so only the first is left. That is why the tier row
    could not ship on its own: while a zero estimate returned before the
    preflight call, adding it WOULD have given this preset a submit path that
    never sees the flag, and each run spawns the A100 container Ranomics pays
    for. Would, not did -- before the tier row this preset priced at $12.5827
    and went down the paid path, where a frozen wallet refused it.
    """
    seen: dict = {}
    gate_app = _gate_app("proteina", seen)
    frozen = {"balance_usd": 100.0, "wallet_frozen": True}

    with gate_app.test_client() as client, patch(
        "shared.wallet_guard.wallet_reserve_hold"
    ) as reserve, patch(
        "shared.wallet_guard.get_or_create_wallet", return_value=frozen
    ), patch(
        "shared.wallet.get_or_create_wallet", return_value=frozen
    ), patch(
        "shared.wallet_guard.render_template", return_value="GATE_RENDERED"
    ) as render:
        with client.session_transaction() as sess:
            sess["user_id"] = "u-1"
        resp = client.post("/x", data={"preset": "validate", "num_designs": "8"})

    from shared.wallet import REASON_WALLET_FROZEN

    assert resp.get_data(as_text=True) == "GATE_RENDERED"
    assert seen.get("ran") is None, "handler ran behind a frozen wallet"
    assert render.call_args.kwargs["gate_reason"] == REASON_WALLET_FROZEN
    reserve.assert_not_called()


def test_a_paid_preset_still_reserves(offline_estimator):
    """Control: the same harness DOES place a hold for a paid preset.

    Without this, ``reserve.assert_not_called()`` above would also pass if
    the decorator had stopped reserving for every submit.
    """
    from shared.wallet import REASON_OK, PreflightResult

    seen: dict = {}
    gate_app = _gate_app("proteina", seen)

    with gate_app.test_client() as client, patch(
        "shared.wallet_guard.wallet_reserve_hold", return_value="tx-001"
    ) as reserve, patch(
        "shared.wallet_guard.get_or_create_wallet",
        return_value={"balance_usd": 100.0, "wallet_frozen": False},
    ), patch(
        "shared.wallet_guard.wallet_preflight",
        return_value=PreflightResult(
            allow=True,
            reason=REASON_OK,
            estimated_cost_usd=Decimal("12.5827"),
            balance_usd=Decimal("100"),
            deficit_usd=Decimal("0"),
            hard_cap_usd=Decimal("15"),
        ),
    ):
        with client.session_transaction() as sess:
            sess["user_id"] = "u-1"
        resp = client.post(
            "/x", data={"preset": "protein_binder", "num_designs": "8"}
        )

    assert resp.status_code == 200
    assert seen.get("hold_tx_id") == "tx-001"
    assert reserve.call_count == 1
