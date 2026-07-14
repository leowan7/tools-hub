"""Wallet gate for tool-submit routes: the ``@requires_wallet`` decorator.

Extracted verbatim from ``app.py`` (blueprint refactor, Commit 0). Lives in a
leaf module so the ``tools`` blueprint (which owns ``/tools/<tool>/submit``)
can import ``requires_wallet`` at module scope instead of ``from app import``
— the keystone that lets that route leave ``app.py`` without an import cycle.

Behavior is byte-identical to the previous in-``app`` definition: the decorator
places a cushioned hold, stashes ``g.wallet_hold_tx_id`` for the handler, and
auto-releases the hold on any early-return or exception before the wrapped view
sets ``g.wallet_hold_consumed = True``.

NOTE: this is distinct from the legacy, unused ``shared.wallet.requires_wallet``
(a different signature, applied to no route). Do not conflate them.
"""

import logging
from decimal import Decimal
from functools import wraps

from flask import g, render_template, request, session, url_for

from shared.credits import load_user_context
from shared.wallet import (
    MIN_TOPUP_USD,
    REASON_INSUFFICIENT,
    SELF_SERVE_CEILING_USD,
    _round_up_topup_amount,
    get_or_create_wallet,
    release_hold as wallet_release_hold,
    reserve_hold as wallet_reserve_hold,
    wallet_preflight,
)
from shared.wallet_estimates import cushioned_hold_usd, estimated_cost_for_tool

logger = logging.getLogger(__name__)


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
    estimate: Decimal,
    balance: Decimal,
    deficit: Decimal,
    reason: str,
    hard_cap: Decimal,
    form_snapshot: dict,
):
    """Render the 'Top up and run' gate.

    Reuses ``templates/wallet/topup.html`` which already supports the
    gate flow via ``next_url`` and ``deficit_usd`` (Agent H may swap
    in a dedicated topup-and-run template later; the context here is
    forward compatible with that swap).
    """
    suggested = _round_up_topup_amount(deficit)
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
    next_url = url_for("tool_form", tool=tool_slug)
    wallet = get_or_create_wallet(session.get("user_id") or "") or {}
    return render_template(
        "wallet/topup.html",
        wallet=wallet,
        deficit_usd=deficit,
        estimate_usd=estimate,
        balance_usd=balance,
        hard_cap_usd=hard_cap,
        suggested_amount=suggested,
        min_topup_usd=MIN_TOPUP_USD,
        next_url=next_url,
        gate_reason=reason,
        tool_slug=tool_slug,
        self_serve_ceiling_usd=SELF_SERVE_CEILING_USD,
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
       render the gate too.

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

            # Smoke runs with a zero estimate skip the gate entirely.
            # No hold row is placed; the handler simply proceeds.
            if estimate <= Decimal("0"):
                g.wallet_estimate_usd = Decimal("0")
                g.wallet_hold_tx_id = None
                g.wallet_params = params
                g.wallet_tool_slug = resolved_slug
                return f(*args, **kwargs)

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
            if not pre.allow:
                return _render_topup_gate(
                    tool_slug=resolved_slug,
                    estimate=pre.estimated_cost_usd,
                    balance=pre.balance_usd,
                    deficit=pre.deficit_usd,
                    reason=pre.reason,
                    hard_cap=pre.hard_cap_usd,
                    form_snapshot=request.form.to_dict() or {},
                )

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
                # point estimate, so the gate shows the real deficit: a
                # balance that covers the estimate but not the cushioned
                # reservation must still top up the difference. Gating the
                # fallback on the point estimate would report a $0 deficit
                # and an "ok" reason, a dead-end where the form will not
                # submit yet the gate says nothing is owed.
                fresh = wallet_preflight(
                    user_id, resolved_slug, hold_amount, params
                )
                return _render_topup_gate(
                    tool_slug=resolved_slug,
                    estimate=fresh.estimated_cost_usd,
                    balance=fresh.balance_usd,
                    deficit=fresh.deficit_usd,
                    reason=fresh.reason or REASON_INSUFFICIENT,
                    hard_cap=fresh.hard_cap_usd,
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
