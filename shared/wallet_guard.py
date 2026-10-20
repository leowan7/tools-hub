"""Wallet gate for tool-submit routes: the ``@requires_wallet`` decorator.

Extracted verbatim from ``app.py`` (blueprint refactor, Commit 0). Lives in a
leaf module so the ``tools`` blueprint (which owns ``/tools/<tool>/submit``)
can import ``requires_wallet`` at module scope instead of ``from app import``
— the keystone that lets that route leave ``app.py`` without an import cycle.

The decorator opens a $0 live-run anchor on a live-charging tier and places a
cushioned hold otherwise, stashes ``g.wallet_hold_tx_id`` for the
handler, and auto-releases the hold on any early-return or exception before the
wrapped view sets ``g.wallet_hold_consumed = True``. That behaviour was
byte-identical to the previous in-``app`` definition until a zero estimate
stopped returning early and started skipping only the hold -- see the comment
on ``free_run`` below for why the frozen-wallet check has to stay in the path.

NOTE: this is distinct from the legacy, unused ``shared.wallet.requires_wallet``
(a different signature, applied to no route). Do not conflate them.
"""

import logging
from decimal import Decimal
from functools import wraps

from flask import g, render_template, request, session, url_for

from shared.credits import load_user_context
from shared.pdb_preflight_rules import TOOL_RULES, runtime_estimate_min
from shared.wallet import (
    MIN_TOPUP_USD,
    REASON_INSUFFICIENT,
    get_or_create_wallet,
    live_due_usd,
    open_live_run,
    release_hold as wallet_release_hold,
    reserve_hold as wallet_reserve_hold,
    wallet_preflight,
)
from shared.wallet_estimates import (
    TOOL_SPECS,
    cushioned_hold_usd,
    estimated_cost_for_tool,
    gpu_class_for_job,
)

logger = logging.getLogger(__name__)

REASON_WALLET_EMPTY = "wallet_empty"

# Live charging (docs/design/LIVE-CHARGING-2026-10-01.md, option (ii)) is on
# only for the tiers that upload each design mid-run, so a hub-side stop keeps
# the designs already finished. Pinned by
# tests/test_live_charging.py::test_live_tools_upload_each_design_mid_run and
# ::test_batch_records_reach_the_container_only_on_the_batch_preset.
# rfdiffusion uploads each design in the llm-proteinDesigner container
# (docker/rfdiffusion/run_pipeline.py::stage_af2_validation), which no test
# here can read; ::test_rfdiffusion_submits_only_the_pilot_tier pins the one
# tier the hub can send it. bindcraft uploads each accepted design on its 60 s
# heartbeat tick, in the same repo at 18382cd
# (docker/bindcraft/run_pipeline.py::_DesignStreamer);
# ::test_bindcraft_submits_only_the_pilot_tier pins its one tier. iggm uploads
# and beats once per design inside its own sweep, in this repo:
# tools/iggm/run_pipeline.py::sweep_designs calls ``upload_one`` and
# ``send_heartbeat`` in its per-design loop, and
# ::test_iggm_uploads_and_beats_once_per_design pins both. Every iggm preset
# streams through that one sweep, so all three are enabled.
# proteina uploads each design in tools/proteina/run_pipeline.py::_run_shard,
# pinned by the same test. esmfold2-design is the exception: each seed uploads
# its designs in tools/esmfold2_design/run_pipeline.py::_shape_designs, which
# runs only after ``designer.design`` returns the seed's whole batch, so a
# stop before then keeps none of that seed's designs. Its start minimum (see
# live_start_usd) keeps a one-seed run clear of the balance stop.
# pxdesign uploads each finished design on its 60 s heartbeat tick, in the
# llm-proteinDesigner repo (docker/pxdesign/run_pipeline.py::_DesignStreamer,
# branch feat/pxdesign-stream-designs), which no test here can read;
# ::test_pxdesign_submits_only_the_pilot_tier pins the one tier the hub sends.
_LIVE_ALL_PRESETS = frozenset(
    {
        "bindcraft",
        "boltz2",
        "esmfold2-design",
        "iggm",
        "proteina",
        "pxdesign",
        "rfdiffusion",
    }
)
# Campaign tools whose children open a live anchor instead of a cushioned
# hold (shared/compute_campaigns.py::_dispatch_chunk). Every other campaign
# tool keeps its hold.
LIVE_CAMPAIGN_TOOLS = frozenset({"proteina"})
PROTEINA_START_USD = Decimal("1.00")
# PROPOSED, not yet decided by Leo. See live_start_usd for the derivation.
PXDESIGN_START_USD = Decimal("2.00")
_LIVE_BATCH_ONLY = frozenset({"af2", "colabfold", "esmfold"})


def live_charging_enabled(tool_slug: str, params: dict) -> bool:
    if tool_slug in _LIVE_ALL_PRESETS:
        return True
    return (
        tool_slug in _LIVE_BATCH_ONLY
        and str(params.get("preset") or "").strip() == "batch"
    )


def live_start_usd(tool_slug: str, params: dict) -> Decimal:
    """The balance a live run needs to start: $0 on all but six tools.

    rfdiffusion uploads its first design in the container's AF2 stage, after
    diffusion and MPNN (llm-proteinDesigner
    docker/rfdiffusion/run_pipeline.py::stage_af2_validation, not readable from
    this repo). Leo, 2026-10-13 (PR #427): a run starts only on a balance that
    covers that stretch. The figure is derived, not measured: the
    ``_RFDIFFUSION`` runtime model's one-design run at its 120-aa pivot, which
    is its fixed term plus one per-design term.

    bindcraft's figure is its base cap, ``TOOL_SPECS["bindcraft"].base_hard_cap_usd``.
    Leo, 2026-10-16 (PR #431): the signup credit stays refused, as it was under
    the up-front hold, which was $8.00 on a 2-design run
    (docs/qa/BINDCRAFT-LIVE-STEP1-2026-10-14.md, "STEP 2 retry").
    tests/test_signup_credit_covers_smallest_run.py::NEEDS_TOPUP pins that.

    iggm's figure is its base cap too, ``TOOL_SPECS["iggm"].base_hard_cap_usd``.
    Leo, 2026-10-16: "use the $3.00 start minimum". It is far above what a
    short iggm run bills -- the paid cancel test charged $0.05 for 37 GPU-s
    (docs/qa/IGGM-LIVE-STEP1-2026-10-16.md, "STEP 2") -- and unlike bindcraft's
    it is below ``SIGNUP_CREDIT_USD``, which therefore still admits an iggm run;
    ::test_the_signup_credit_still_admits_an_iggm_run pins that.

    proteina's figure is a flat $1.00. Leo, 2026-10-19: "lets just say 1
    dollar to start a run as the minimum". The ``_PROTEINA`` runtime model's
    8-design run at the 120-aa pivot is $0.94, and $1.00 is below
    ``SIGNUP_CREDIT_USD``; ::test_proteina_starts_at_one_dollar pins both.

    pxdesign's figure is a flat $2.00, and is a PROPOSAL awaiting Leo's
    decision -- no one has approved it. Derivation: a pxdesign run streams its
    first design only once PXDesign has sampled, converted and AF2-validated
    it, so time-to-first-design is bounded by the length of a run that
    delivered a design, and every such run on record ran 1051 GPU-s or less:
    one-design smoke runs at 993, 1006, 1048 and 1051, and the two pilot runs
    -- the only tier the hub sends -- at 462 and 504 (docs/VALIDATION-LOG.md,
    rows 2026-04-22 to 2026-05-27; the 4517 GPU-s mini_pilot row FAILED and
    delivered nothing). At the longest of them,
    ``live_due_usd("pxdesign", 1051, "A100-80GB")``
    is $1.84, so $2.00 is the next round figure above it, and it is below
    ``SIGNUP_CREDIT_USD``; ::test_pxdesign_starts_at_two_dollars pins both.
    Known limits of the figure: it assumes PXDesign converts and validates
    samples incrementally, so that a one-design run's length is also the time
    to the first design of a larger run. If instead it finishes all sampling
    before any conversion, time-to-first-design approaches whole-run length
    and no modest minimum covers it -- unverified either way, because no run
    has streamed a pxdesign design yet. The figure also does not cover large
    targets toward the 600-aa ceiling, whose first design costs more.
    And it does not scale with ``num_designs``, so it admits a run the balance
    cannot fund: measured with this repo's estimator, a pilot at 1, 8 and 24
    designs estimates $6.29, $25.17 and $75.50 and used to hold $9.44, $37.75
    and $113.24, and all three now start at $2.00. The pre-change hold refused
    every one of them on a $2.00 balance. That is what save-as-you-go is for --
    the meter stops the run at $0 and the user keeps the designs already
    streamed -- so it is sound only once the streaming container is deployed;
    until then a stopped run delivers nothing for the money. Non-scaling is the
    shape of every live figure here, not something peculiar to pxdesign:
    esmfold2-design's paragraph below records the same gap, and no candidate
    flat figure closes it -- even the $13.00 base cap is under a 24-design
    pilot's $75.50. Only the scaled cap ($156.00 at 24) would, which is what
    the cushioned hold did. ::test_pxdesign_start_minimum_does_not_scale pins
    the figures. Scaling it is Leo's call, as it is for esmfold2-design.

    esmfold2-design's figure is its base cap, the most ``live_due_usd`` charges
    one seed, so a one-seed run started at that balance owes no more than it
    had. Leo approved $15 on PR #440 review (2026-10-19). It does not scale
    with ``n_seeds``: a run of n seeds is metered for n GPUs, so while all n
    run, $15 lasts 1/n as long as it does for one seed, and the meter stops it at $0
    whether or not a seed has streamed its designs yet. Scaling it to n seeds
    is waiting on Leo.
    """
    if tool_slug in ("bindcraft", "esmfold2-design", "iggm"):
        return TOOL_SPECS[tool_slug].base_hard_cap_usd
    if tool_slug == "proteina":
        return PROTEINA_START_USD
    if tool_slug == "pxdesign":
        return PXDESIGN_START_USD
    if tool_slug != "rfdiffusion":
        return Decimal("0")
    seconds = runtime_estimate_min(TOOL_RULES[tool_slug], 120, 1) * 60
    return live_due_usd(tool_slug, seconds, gpu_class_for_job(tool_slug), params)


def _balance(user_id: str) -> Decimal:
    wallet = get_or_create_wallet(user_id) or {}
    return Decimal(str(wallet.get("balance_usd") or 0))


def _wallet_params_from_form(form) -> dict:  # noqa: ANN001
    """Return a dict of params relevant to the wallet estimator.

    The wallet estimate only looks at scaling parameters (num_designs,
    iters, target_length, etc.). We strip the form down to a flat dict
    and coerce numerics where possible so the estimator can read them.
    """
    params: dict[str, object] = {}
    for key, value in form.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        # Cheap numeric coercion; the estimator falls back to defaults
        # on unparseable inputs.
        if isinstance(value, (int, float, Decimal)):
            params[key] = value
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                continue
            try:
                params[key] = int(stripped)
                continue
            except ValueError:
                pass
            try:
                params[key] = float(stripped)
                continue
            except ValueError:
                pass
            params[key] = stripped
    return params


def _render_topup_gate(
    *,
    tool_slug: str,
    reason: str,
    form_snapshot: dict,
):
    """Render the 'Top up and run' gate.

    Reuses ``templates/wallet/topup.html``, which shows the gate notice
    when ``gate_reason`` is set.
    """
    # Stash the original form on the session so /account/topup-complete
    # can return the user back to the form with values intact. The form
    # snapshot is JSON serializable text only.
    try:
        session["wallet_gate_form"] = {
            "tool": tool_slug,
            "form": {
                k: v for k, v in form_snapshot.items()
                if isinstance(v, (str, int, float, bool))
            },
            "reason": reason,
        }
    except Exception:  # session writes are best effort
        pass

    # NOTE (blueprint refactor): "tool_form" becomes "tools.tool_form" when
    # the tools routes move into the tools blueprint (Commit 7).
    next_url = url_for("tools.tool_form", tool=tool_slug)
    wallet = get_or_create_wallet(session.get("user_id") or "") or {}
    return render_template(
        "wallet/topup.html",
        wallet=wallet,
        min_topup_usd=MIN_TOPUP_USD,
        next_url=next_url,
        # Same slug as next_url, for the gate paragraph's inline "go back to
        # the <tool> form" link (templates/wallet/topup.html).
        return_tool=tool_slug,
        gate_reason=reason,
        tool_slug=tool_slug,
    )


def requires_wallet(view_func=None, *, tool_slug=None):
    """Flask decorator that gates a tool submit POST on the wallet.

    Two call shapes are supported:

    * Bare decorator: ``@requires_wallet`` on a handler whose Flask
      URL converter binds ``<tool>`` (the slug is read from ``kwargs``).
    * Factory: ``@requires_wallet(tool_slug='mpnn')`` on a route
      whose URL is hardcoded to one tool.

    Three phase contract:

    1. Compute the estimate via ``estimated_cost_for_tool`` based on
       the form params. Resolve the parameter scaled hard cap and the
       user's current balance.
    2. Block flow on any of these reasons by rendering the 'Top up and
       run' gate (Moment 2 of the plan) or the per tool cap message
       (Moment 3): wallet frozen, insufficient balance, per tool cap
       exceeded, self serve ceiling exceeded, daily cap reached.
    3. On allow, atomically reserve the hold via ``reserve_hold`` and
       stash the hold_tx_id plus estimate on ``flask.g`` for the
       handler. If the SQL hold returns null (lost a concurrent race),
       render the gate too. A zero estimate (a free tier) passes through
       step 2 like any other and then skips step 3 only, leaving
       ``g.wallet_hold_tx_id`` None.

    The wrapped handler is expected to read ``g.wallet_hold_tx_id`` and
    persist it on the job row so the settle path in
    :func:`shared.jobs.complete_job` can close out the hold later.
    """

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):  # noqa: ANN002, ANN003
            # Resolve the tool slug from the URL kwarg, the factory
            # argument, or fall through if neither is present.
            resolved_slug = (
                tool_slug
                or kwargs.get("tool")
                or kwargs.get("tool_slug")
                or ""
            )
            if not resolved_slug:
                return f(*args, **kwargs)

            # Resolve user_id from session first; fall back to
            # load_user_context which reads the email and looks up
            # auth.users for the id. Tests that only set user_email
            # in the session take this branch.
            user_id = session.get("user_id")
            if not user_id:
                try:
                    ctx = load_user_context()
                except Exception:
                    ctx = None
                user_id = ctx.user_id if ctx else None
            if not user_id:
                # No identifiable user. Fall through so the
                # @login_required path handles the redirect. The wallet
                # decorator never preempts auth.
                g.wallet_estimate_usd = Decimal("0")
                g.wallet_hold_tx_id = None
                g.wallet_params = {}
                g.wallet_tool_slug = resolved_slug
                return f(*args, **kwargs)

            params = _wallet_params_from_form(request.form)
            try:
                estimate = estimated_cost_for_tool(
                    user_id, resolved_slug, params
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "requires_wallet: estimate failed for user=%s tool=%s",
                    user_id, resolved_slug, exc_info=True,
                )
                estimate = Decimal("0")

            # A zero estimate means there is nothing to RESERVE -- it does not
            # mean there is nothing to CHECK. This used to return here, which
            # was inert while no tier priced at 0; proteina's free ``validate``
            # is the first one that does (shared/wallet_estimates.py, its
            # ToolSpec's ``tier_gpu_seconds``), and returning here would have
            # handed that preset a submit path that never sees
            # ``wallet_frozen``. A PAID submit meets that flag twice -- in
            # ``wallet_preflight`` (shared/wallet.py::wallet_preflight) and
            # again inside the SQL ``try_hold_for_job``
            # (supabase/migrations/0035_phase2_remove_daily_cap.sql:57-65,
            # which returns NULL for a frozen wallet). A free run never takes a
            # hold, so the SQL check is unreachable for it and preflight is the
            # only one left -- while a validate run still spawns the A100
            # container Ranomics pays for. So free runs go through the same
            # gate and skip only the hold, below. The same reasoning covers the
            # estimator-raised fallback a few lines up, which also lands here
            # as 0.
            free_run = estimate <= Decimal("0")

            # Detect a missing service client (tests, dev with no
            # Supabase). When the wallet layer can not even resolve
            # the user_wallets row, fall through to the legacy path
            # instead of locking out every request behind the gate.
            try:
                wallet_row = get_or_create_wallet(user_id)
            except Exception:  # noqa: BLE001
                wallet_row = None
            if wallet_row is None:
                g.wallet_estimate_usd = estimate
                g.wallet_hold_tx_id = None
                g.wallet_params = params
                g.wallet_tool_slug = resolved_slug
                return f(*args, **kwargs)

            pre = wallet_preflight(
                user_id, resolved_slug, estimate, params
            )
            live = not free_run and live_charging_enabled(resolved_slug, params)
            if (not pre.allow and pre.reason == REASON_INSUFFICIENT
                    and not free_run and not live):
                # Short on the point estimate means short on the hold too, and
                # the hold is what must fit (reserve_hold below), so the gate's
                # reason is decided on it.
                pre = wallet_preflight(
                    user_id, resolved_slug,
                    max(estimate, cushioned_hold_usd(user_id, resolved_slug, params)),
                    params,
                )
            if not pre.allow and not (live and pre.reason == REASON_INSUFFICIENT):
                return _render_topup_gate(
                    tool_slug=resolved_slug,
                    reason=pre.reason,
                    form_snapshot=request.form.to_dict() or {},
                )

            # Nothing to reserve on a free tier: the handler proceeds with no
            # hold row, and with no ``hold_tx_id`` on the job there is nothing
            # for ``shared/jobs.py`` to settle. Placed AFTER preflight so a
            # frozen wallet is still refused above. NOT merged into the hold
            # call below: ``cushioned_hold_usd`` floors any tool that sets
            # ``worst_case_gpu_seconds`` at that figure, so a zero point
            # estimate on such a tool would still hold money.
            if free_run:
                g.wallet_estimate_usd = Decimal("0")
                g.wallet_hold_tx_id = None
                g.wallet_params = params
                g.wallet_tool_slug = resolved_slug
                return f(*args, **kwargs)

            if live:
                if 0 < pre.balance_usd < live_start_usd(resolved_slug, params):
                    return _render_topup_gate(
                        tool_slug=resolved_slug,
                        reason=REASON_INSUFFICIENT,
                        form_snapshot=request.form.to_dict() or {},
                    )
                hold_tx_id = (
                    open_live_run(user_id, resolved_slug)
                    if pre.balance_usd > 0 else None
                )
                if not hold_tx_id:
                    empty = pre.balance_usd <= 0 or _balance(user_id) <= 0
                    return _render_topup_gate(
                        tool_slug=resolved_slug,
                        reason=REASON_WALLET_EMPTY if empty else "hold_failed",
                        form_snapshot=request.form.to_dict() or {},
                    )
                g.wallet_live = True
            else:
                # Reserve a cushioned hold (usually covers actual, so settle
                # releases surplus) while ``estimate`` stays the point estimate
                # shown to the user and stored as the job's forecast price. The
                # cushion is clamped to the per-tool hard cap, so it never trips
                # the preflight/SQL cap guards.
                hold_amount = cushioned_hold_usd(user_id, resolved_slug, params)
                hold_tx_id = wallet_reserve_hold(
                    user_id, resolved_slug, None, hold_amount, params
                )
                if not hold_tx_id:
                    # Lost a concurrent race or fell foul of a SQL guard.
                    # Re-preflight against the HELD (cushioned) amount, not the
                    # point estimate, so the gate's reason matches the hold: a
                    # balance that covers the estimate but not the cushioned
                    # reservation is short. Gating the fallback on the point
                    # estimate would report an "ok" reason, a dead-end where the
                    # form will not submit yet the gate gives no cause.
                    fresh = wallet_preflight(
                        user_id, resolved_slug, hold_amount, params
                    )
                    return _render_topup_gate(
                        tool_slug=resolved_slug,
                        reason=fresh.reason or REASON_INSUFFICIENT,
                        form_snapshot=request.form.to_dict() or {},
                    )

            g.wallet_estimate_usd = estimate
            g.wallet_hold_tx_id = hold_tx_id
            g.wallet_params = params
            g.wallet_tool_slug = resolved_slug
            # The wrapped view sets this to True once create_job has run
            # and stashed the hold_tx_id on the job inputs. Any early
            # return before that (form validation, PDB validation,
            # workspace gate, etc.) leaves the flag False and triggers
            # an automatic release in the finally block below. Without
            # this guard, a user who submits with a missing PDB has the
            # estimate deducted from their wallet with no job to settle
            # it, and the only recovery is a manual SQL release.
            g.wallet_hold_consumed = False

            try:
                response = f(*args, **kwargs)
            except Exception:
                # Handler raised. Release the hold so the user is not
                # left with a stranded reservation.
                try:
                    wallet_release_hold(
                        hold_tx_id, reason="handler_exception"
                    )
                except Exception:
                    logger.warning(
                        "requires_wallet: release_hold on exception "
                        "failed for hold=%s",
                        hold_tx_id, exc_info=True,
                    )
                raise

            # Early-return path (no exception, but the view returned
            # without writing a tool_jobs row, e.g. a form-with-error
            # render). Release so the hold does not leak.
            if not getattr(g, "wallet_hold_consumed", False):
                try:
                    wallet_release_hold(
                        hold_tx_id, reason="view_early_return"
                    )
                except Exception:
                    logger.warning(
                        "requires_wallet: release_hold on early return "
                        "failed for hold=%s",
                        hold_tx_id, exc_info=True,
                    )
            return response

        return wrapper

    # Bare-decorator usage: @requires_wallet
    if callable(view_func) and tool_slug is None:
        return decorator(view_func)
    # Factory usage: @requires_wallet(tool_slug='mpnn')
    return decorator
