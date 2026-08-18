"""Wallet + billing routes (blueprint refactor, Commit 7b -- MONEY PATH).

Stripe checkout / portal, the retired workspaces stubs, the wallet balance
and estimate APIs, top-up + topup-complete, the ledger view, and
auto-reload config. Lifted verbatim from ``create_app()``; the hold /
settle and Stripe logic is unchanged. Only ``@flask_app.route`` ->
``@wallet_bp.route`` and self-refs -> ``wallet.*``. The two module-level
ledger helpers move in with the routes.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from shared.auth import login_required
from shared.credits import load_user_context
from shared.wallet import (
    MIN_TOPUP_USD,
    SELF_SERVE_CEILING_USD,
    _round_up_topup_amount,
    get_or_create_wallet,
)
from shared.wallet_estimates import compute_hard_cap, estimated_cost_for_tool

logger = logging.getLogger(__name__)

wallet_bp = Blueprint("wallet", __name__)


def _build_tx_lineage_annotations(client, user_id, page_rows):  # noqa: ANN001
    """Compute per-row lineage annotations for the ledger view.

    Presentation only. This reads the ledger; it never writes, never
    changes an amount or a balance, and never touches the
    SUM(amount_usd) invariant. It returns a dict keyed by transaction
    id whose values tell the template how to render each row so a hold
    plus its later settlement read as one true cost, not two charges.

    For every hold referenced by the current page (a hold row on the
    page, or the parent of a settle row on the page) we fetch the whole
    lineage (the hold plus every row whose parent_tx_id is that hold)
    even when the hold and its settlement fall on different pages, and
    compute the group net = SUM(amount_usd) = negative of the actual
    compute cost.

    Annotation shape per row id:
        role      one of 'hold', 'release', 'settlement'
        settled   True when the hold has at least one settle child
        reserved  the amount the hold reserved (positive), for holds
        net       group net = SUM(amount_usd) over the lineage, on the
                  settlement row (the charge, or the release when there
                  is no charge); this is negative of the actual cost

    On any failure the function returns an empty dict so the template
    falls back to plain rendering and the ledger page never 500s.
    """
    annotations: dict = {}
    if client is None or not page_rows:
        return annotations
    try:
        # Collect the hold ids this page touches: ids of hold rows on the
        # page, plus parents of any settle rows on the page.
        hold_ids: set = set()
        for row in page_rows:
            if row.get("kind") == "hold" and row.get("id") is not None:
                hold_ids.add(row.get("id"))
            parent = row.get("parent_tx_id")
            if parent is not None:
                hold_ids.add(parent)
        if not hold_ids:
            return annotations

        hold_id_list = list(hold_ids)
        # Full lineage: the hold rows themselves plus their children,
        # scoped to this user. Two cheap queries keep the OR simple and
        # avoid depending on a specific PostgREST or_ filter syntax.
        lineage: dict = {}

        def _absorb(resp):  # noqa: ANN001
            for r in list(getattr(resp, "data", None) or []):
                rid = r.get("id")
                if rid is not None:
                    lineage[rid] = r

        holds_resp = (
            client.table("wallet_transactions")
            .select("*")
            .eq("user_id", user_id)
            .in_("id", hold_id_list)
            .execute()
        )
        _absorb(holds_resp)
        children_resp = (
            client.table("wallet_transactions")
            .select("*")
            .eq("user_id", user_id)
            .in_("parent_tx_id", hold_id_list)
            .execute()
        )
        _absorb(children_resp)

        # Group rows by their hold id (a hold groups on its own id; a
        # child groups on parent_tx_id).
        groups: dict = {hid: {"hold": None, "children": []} for hid in hold_ids}
        for r in lineage.values():
            rid = r.get("id")
            parent = r.get("parent_tx_id")
            if r.get("kind") == "hold" and rid in groups:
                groups[rid]["hold"] = r
            elif parent in groups:
                groups[parent]["children"].append(r)

        for hid, grp in groups.items():
            children = grp["children"]
            hold_row = grp["hold"]
            settled = len(children) > 0

            # Group net = SUM(amount_usd) over the hold and all children.
            net = Decimal("0")
            have_net = False
            if hold_row is not None:
                net += _tx_amount_decimal(hold_row.get("amount_usd"))
                have_net = True
            for c in children:
                net += _tx_amount_decimal(c.get("amount_usd"))
                have_net = True

            reserved = None
            if hold_row is not None:
                reserved = abs(_tx_amount_decimal(hold_row.get("amount_usd")))

            # Annotate the hold row.
            if hold_row is not None and hold_row.get("id") is not None:
                annotations[hold_row["id"]] = {
                    "role": "hold",
                    "settled": settled,
                    "reserved": reserved,
                }

            # Pick the settlement row that carries the net label: prefer
            # the charge / absorbed_variance row; otherwise the release.
            settlement_row = None
            for c in children:
                if c.get("kind") in ("charge", "absorbed_variance"):
                    settlement_row = c
                    break
            if settlement_row is None:
                for c in children:
                    if c.get("kind") == "hold_release":
                        settlement_row = c
                        break

            for c in children:
                cid = c.get("id")
                if cid is None:
                    continue
                if c is settlement_row and have_net:
                    annotations[cid] = {"role": "settlement", "net": net}
                elif c.get("kind") == "hold_release":
                    annotations[cid] = {"role": "release"}
                else:
                    annotations[cid] = {"role": "settlement", "net": net}
    except Exception:  # noqa: BLE001
        logger.warning(
            "wallet_transactions: lineage annotation failed for %s",
            user_id, exc_info=True,
        )
        return {}
    return annotations


def _tx_amount_decimal(value) -> Decimal:  # noqa: ANN001
    """Coerce a ledger amount to Decimal; 0 on any bad value."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


@wallet_bp.route("/billing/checkout", methods=["GET"])
@login_required
def billing_checkout():
    """Retired: legacy per-target Workspace Stripe checkout.

    The Workspace SKU product ($499/$2,499) was retired with the
    USD-wallet pivot (the wallet is the sole money path). This route
    is kept only so bookmarked/legacy links do not 404; it redirects
    to the pricing page, which explains the wallet model.
    """
    return redirect(url_for("public.pricing"))

# ------------------------------------------------------------------
# Retired Workspace sales routes. The per-target Workspace SKU
# product ($499/$2,499) was retired with the USD-wallet pivot; the
# wallet is the sole money path. These routes are kept as redirects
# so legacy/bookmarked links do not 404. The workspace *compute*
# lifecycle (shared/workspaces.py) is unaffected.
# ------------------------------------------------------------------

@wallet_bp.route("/workspaces", methods=["GET"])
@login_required
def workspaces_list():
    """Retired: redirect to the account dashboard (wallet model)."""
    return redirect(url_for("auth.account"))

@wallet_bp.route("/workspaces/new", methods=["GET"])
@login_required
def workspaces_new():
    """Retired: redirect to pricing (wallet model, no SKU purchase)."""
    return redirect(url_for("public.pricing"))

@wallet_bp.route("/workspaces/new", methods=["POST"])
@login_required
def workspaces_new_submit():
    """Retired: the Workspace purchase flow no longer takes payment.

    Previously staged an uploaded target PDB and redirected to the
    Stripe Workspace checkout. Retired with the wallet pivot; kept as
    a redirect so a stale POST does not 500.
    """
    return redirect(url_for("public.pricing"))

@wallet_bp.route("/workspaces/<workspace_id>", methods=["GET"])
@login_required
def workspace_detail(workspace_id: str):
    """Retired: redirect to the account dashboard (wallet model)."""
    return redirect(url_for("auth.account"))

@wallet_bp.route("/billing/portal", methods=["GET"])
@login_required
def billing_portal():
    """Redirect the user to their Stripe Billing Portal session."""
    from billing.checkout import create_portal_session  # noqa: PLC0415

    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    base = request.url_root.rstrip("/")
    return_url = base + url_for("auth.account")

    # create_portal_session requires the wallet's Stripe customer id;
    # omitting it previously raised a TypeError -> hard 500 on every
    # click. Resolve it here and let the helper return a friendly
    # error (redirected below) when the wallet has no saved card.
    wallet = get_or_create_wallet(ctx.user_id) or {}
    customer_id = wallet.get("stripe_customer_id") or ""

    url, error = create_portal_session(
        customer_id=customer_id, return_url=return_url
    )
    if error or not url:
        logger.warning("Portal creation failed: %s", error)
        return redirect(url_for("auth.account") + "?portal_error=1")
    return redirect(url, code=303)

# ------------------------------------------------------------------
# Wallet endpoints
# ------------------------------------------------------------------

@wallet_bp.route("/api/wallet/estimate", methods=["GET"])
def api_wallet_estimate():
    """Return the wallet estimate, hard cap, and current balance.

    Used by every tool form for the inline Moment 1 display (live
    update of "Estimated cost / Balance / Balance after"). The
    endpoint is read only and idempotent; it never places a hold or
    modifies the wallet.

    Query parameters:

    * ``tool`` (or ``tool_slug``): the tool slug.
    * ``params``: optional JSON object of param values. Falls back
      to flat query params (``num_designs=100``) when omitted.

    Response shape::

        {"estimate_usd": "0.0500",
         "hard_cap_usd": "150.00",
         "balance_usd": "5.0000",
         "ok": true}

    All money values are returned as JSON strings to preserve
    Decimal precision through JSON's float coercion.
    """
    tool_slug = (
        request.args.get("tool")
        or request.args.get("tool_slug")
        or ""
    ).strip()
    if not tool_slug:
        return jsonify({"error": "missing_tool_slug"}), 400

    user_id = session.get("user_id")
    # Resolve params: prefer a JSON ``params`` blob, fall back to
    # flat query args minus the meta keys.
    params: dict[str, object] = {}
    raw_params = request.args.get("params")
    if raw_params:
        try:
            parsed = json.loads(raw_params)
            if isinstance(parsed, dict):
                params = parsed
        except (ValueError, TypeError):
            pass
    if not params:
        for key, value in request.args.items():
            if key in {"tool", "tool_slug", "params"}:
                continue
            if not value:
                continue
            # Coerce numerics so the estimator's scaling math works.
            try:
                params[key] = int(value)
                continue
            except ValueError:
                pass
            try:
                params[key] = float(value)
                continue
            except ValueError:
                pass
            params[key] = value

    try:
        estimate = estimated_cost_for_tool(user_id, tool_slug, params)
    except Exception:  # noqa: BLE001
        logger.warning(
            "api_wallet_estimate: estimate failed for tool=%s",
            tool_slug, exc_info=True,
        )
        return jsonify({"error": "estimate_failed"}), 500

    try:
        hard_cap = compute_hard_cap(tool_slug, params)
    except Exception:  # noqa: BLE001
        logger.warning(
            "api_wallet_estimate: hard cap failed for tool=%s",
            tool_slug, exc_info=True,
        )
        hard_cap = Decimal("0")

    balance = Decimal("0")
    wallet = None
    if user_id:
        wallet = get_or_create_wallet(user_id)
        balance = Decimal(str((wallet or {}).get("balance_usd") or 0))

    # Derived contract values consumed by templates/wallet/_partials.html.
    # The Moment 1 estimate panel and the inline Moment 2 gate both
    # read these flag fields to flip visibility.
    deficit = estimate - balance
    if deficit < 0:
        deficit = Decimal("0")
    rounded_topup = _round_up_topup_amount(deficit)

    exceeds_self_serve = estimate > SELF_SERVE_CEILING_USD
    exceeds_hard_cap = estimate > hard_cap
    # Soft warning band: estimate has eaten 80% of the current
    # balance without going under, so the user is close to a top up
    # gate on the next click. Suppressed when a harder block trips.
    soft_block = False
    if balance > 0 and not exceeds_hard_cap and not exceeds_self_serve:
        soft_block = estimate >= (balance * Decimal("0.8")) and (
            estimate < balance
        )
    # Hard block: balance cannot cover the estimate at all. The
    # gate inside the partial owns the visual; this flag is what
    # the JS reads.
    hard_block = balance < estimate
    wallet_frozen = bool((wallet or {}).get("wallet_frozen"))

    return jsonify({
        "ok": True,
        "tool_slug": tool_slug,
        "estimate_usd": str(estimate),
        "hard_cap_usd": str(hard_cap),
        "balance_usd": str(balance),
        "balance_after_usd": str(balance - estimate),
        "self_serve_ceiling_usd": str(SELF_SERVE_CEILING_USD),
        "exceeds_hard_cap": exceeds_hard_cap,
        "exceeds_self_serve_ceiling": exceeds_self_serve,
        # Wave 3 contract keys consumed by wallet/_partials.html JS.
        # Names align with the partial's documented schema.
        "deficit_usd": str(deficit),
        "rounded_topup_usd": str(rounded_topup),
        "scaled_hard_cap_usd": str(hard_cap),
        "soft_block": soft_block,
        "hard_block": hard_block,
        "self_serve_block": exceeds_self_serve,
        "confirm_band": exceeds_hard_cap,
        "wallet_frozen": wallet_frozen,
    })

@wallet_bp.route("/api/wallet/balance", methods=["GET"])
def api_wallet_balance():
    """Return the current wallet balance for the logged-in user.

    Used by the nav-chip JS to refresh after a top-up redirect or
    window-focus without forcing a full page reload. Read-only and
    idempotent. Returns 401 if no session.

    Response shape::

        {"balance_usd": "5.0000", "wallet_frozen": false}
    """
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401
    wallet = get_or_create_wallet(user_id) or {}
    return jsonify({
        "balance_usd": str(Decimal(str(wallet.get("balance_usd") or 0))),
        "wallet_frozen": bool(wallet.get("wallet_frozen")),
    })

@wallet_bp.route("/account/topup-complete", methods=["GET"])
@login_required
def topup_complete():
    """Stripe Checkout success_url landing page.

    Validates the ``session_id`` query parameter against Stripe and
    renders a confirmation. The webhook handler in
    ``webhooks/stripe.py`` actually credits the wallet on the
    ``checkout.session.completed`` event; this route is the user
    visible confirmation while that webhook flies.

    When the gate flow stashed an original tool form, the user can
    click 'Return to <tool>' from the confirmation to land back on
    the tool form with values preserved.
    """
    from billing.checkout import retrieve_topup_session  # noqa: PLC0415

    ctx = load_user_context()
    session_id = (request.args.get("session_id") or "").strip()
    gate_payload = session.pop("wallet_gate_form", None) or {}
    return_tool = (gate_payload or {}).get("tool")

    if not session_id:
        return render_template(
            "wallet/topup.html",
            topup_error=(
                "No Stripe session was provided. If you just paid, "
                "your wallet will update shortly. Refresh the "
                "Account page to see the balance."
            ),
            wallet=get_or_create_wallet(ctx.user_id) if ctx else None,
            return_tool=return_tool,
        )

    stripe_session, err = retrieve_topup_session(session_id)
    if err or not stripe_session:
        logger.warning(
            "topup_complete: could not retrieve session=%s err=%s",
            session_id, err,
        )
        return render_template(
            "wallet/topup.html",
            topup_error=(
                "Could not validate the Stripe session. The webhook "
                "still credits the wallet when payment clears."
            ),
            wallet=get_or_create_wallet(ctx.user_id) if ctx else None,
            return_tool=return_tool,
        )

    # Owner check: the session metadata.user_id must match the
    # signed in user so a leaked session_id link does not expose
    # another user's amount or status.
    metadata = stripe_session.get("metadata") or {}
    if ctx and metadata.get("user_id") and metadata["user_id"] != ctx.user_id:
        logger.warning(
            "topup_complete: session=%s user mismatch (session=%s viewer=%s)",
            session_id, metadata.get("user_id"), ctx.user_id,
        )
        return render_template(
            "wallet/topup.html",
            topup_error=(
                "This Checkout session belongs to a different account."
            ),
            wallet=get_or_create_wallet(ctx.user_id),
            return_tool=return_tool,
        )

    wallet = get_or_create_wallet(ctx.user_id) if ctx else None
    # Pass ?topup=success on the return URL so wallet-nav.js polls the
    # balance while the Stripe webhook lands — the user can otherwise
    # see a stale chip for a few seconds after redirect.
    return render_template(
        "wallet/topup.html",
        topup_success=True,
        stripe_session=stripe_session,
        wallet=wallet,
        return_tool=return_tool,
        return_tool_url=(
            url_for("tools.tool_form", tool=return_tool) + "?topup=success"
            if return_tool else None
        ),
    )

# ------------------------------------------------------------------
# Wallet UI routes
# ------------------------------------------------------------------
#
# Five routes back the wallet self serve surface:
#
#   GET  /account/wallet                -> overview dashboard
#   GET  /account/wallet/topup          -> top up form (standalone)
#   POST /account/wallet/checkout       -> create Stripe Checkout, 302
#   GET  /account/wallet/transactions   -> paginated ledger
#   POST /account/wallet/auto-reload    -> save auto reload settings
#
# The gate flow lives on the same /account/wallet/topup template via
# the requires_wallet decorator (see _render_topup_gate above); the
# standalone topup form below renders the same template with no
# deficit_usd context.

@wallet_bp.route("/account/wallet", methods=["GET"])
@login_required
def wallet_overview():
    """Render the wallet overview dashboard.

    Shows current balance, today plus 30 day spend, auto reload
    status, the 10 most recent ledger rows, and a Binder Pilot
    callout for users averaging >$1000 / 30d.
    """
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    wallet = get_or_create_wallet(ctx.user_id) or {}

    # Decorate the wallet with derived fields the template reads.
    # _spent_today_usd is a private helper but the values it returns
    # are stable; we shape the dict here rather than pushing the
    # query into the template.
    from shared.wallet import _spent_today_usd  # noqa: PLC0415

    spent_today = _spent_today_usd(ctx.user_id)
    try:
        wallet["spent_today_usd"] = float(spent_today)
    except Exception:  # pragma: no cover (defensive)
        wallet["spent_today_usd"] = 0.0

    # 30-day spend: net of holds, releases, and charges over a
    # rolling 30-day window. Same canonical formula as the daily
    # figure (shared.wallet._net_spend_usd), just a wider cutoff.
    from datetime import datetime, timezone, timedelta  # noqa: PLC0415
    from shared.credits import get_service_client  # noqa: PLC0415
    from shared.wallet import _net_spend_usd  # noqa: PLC0415

    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
    spent_30d = _net_spend_usd(ctx.user_id, cutoff_30d)

    client = get_service_client()
    recent_transactions: list = []
    if client is not None:
        try:
            tx_response = (
                client.table("wallet_transactions")
                .select("*")
                .eq("user_id", ctx.user_id)
                .order("created_at", desc=True)
                .limit(10)
                .execute()
            )
            recent_transactions = list(
                getattr(tx_response, "data", None) or []
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "wallet_overview: recent ledger lookup failed for %s",
                ctx.user_id, exc_info=True,
            )

    try:
        wallet["spent_30d_usd"] = float(spent_30d)
    except Exception:  # pragma: no cover
        wallet["spent_30d_usd"] = 0.0

    return render_template(
        "wallet/overview.html",
        wallet=wallet,
        recent_transactions=recent_transactions,
        user_email=session.get("user_email", ""),
    )

@wallet_bp.route("/account/wallet/topup", methods=["GET"])
@login_required
def wallet_topup():
    """Render the standalone wallet top up form.

    The gate flow renders the same template with a ``deficit_usd``
    and ``next_url`` set; this route renders it bare so the user can
    top up manually without coming from a tool gate. ``topup_error``
    is read from the query string so the POST handler can redirect
    here with an inline error.
    """
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    wallet = get_or_create_wallet(ctx.user_id) or {}
    if wallet.get("wallet_frozen"):
        return redirect(url_for("wallet.wallet_overview") + "?wallet_frozen=1")
    topup_error = (request.args.get("topup_error") or "").strip() or None
    return render_template(
        "wallet/topup.html",
        wallet=wallet,
        min_topup_usd=MIN_TOPUP_USD,
        next_url=None,
        topup_action_url="/account/wallet/checkout",
        topup_error=topup_error,
    )

@wallet_bp.route("/account/wallet/checkout", methods=["POST"])
@login_required
def wallet_checkout():
    """Create a Stripe Checkout Session for a wallet top up.

    Reads ``amount_usd`` from the form, hands off to
    :func:`billing.checkout.create_topup_session`, and redirects to
    the returned Stripe URL. Any error redirects back to
    ``/account/wallet/topup?topup_error=<msg>`` so the user lands on
    a form they can retry.
    """
    from billing.checkout import create_topup_session  # noqa: PLC0415
    from urllib.parse import quote  # noqa: PLC0415

    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    wallet = get_or_create_wallet(ctx.user_id) or {}
    if wallet.get("wallet_frozen"):
        return redirect(url_for("wallet.wallet_overview") + "?wallet_frozen=1")

    amount_raw = (request.form.get("amount_usd") or "").strip()
    if not amount_raw:
        return redirect(
            "/account/wallet/topup?topup_error="
            + quote("Pick an amount to top up.")
        )

    try:
        amount = Decimal(amount_raw)
    except (InvalidOperation, ValueError):
        return redirect(
            "/account/wallet/topup?topup_error="
            + quote("Top up amount must be a number.")
        )

    # NOTE: this used to stash request.form["next"] on the session as
    # wallet_gate_form["return_url"]. Nothing ever read that key — the only
    # reader, topup_complete(), reads gate_payload["tool"], which the
    # wallet_guard decorator sets. It was a dead write of a user-controlled
    # value into the session, so it is gone rather than validated.

    save_pm_raw = (
        request.form.get("save_payment_method") or ""
    ).strip().lower()
    save_payment_method = save_pm_raw in {"on", "true", "1", "yes"}

    result, err = create_topup_session(
        ctx.user_id,
        ctx.email,
        amount,
        save_payment_method=save_payment_method,
    )
    if err or not result:
        return redirect(
            "/account/wallet/topup?topup_error=" + quote(err or "Checkout failed.")
        )

    return redirect(result.get("url"))

@wallet_bp.route("/account/wallet/transactions", methods=["GET"])
@login_required
def wallet_transactions():
    """Render the paginated ledger view.

    ``page`` query param drives offset; 25 rows per page. ``kind``
    query param optionally filters by ledger kind (signup_credit,
    topup, charge, etc.).
    """
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    wallet = get_or_create_wallet(ctx.user_id) or {}

    try:
        page = int((request.args.get("page") or "1").strip())
    except ValueError:
        page = 1
    if page < 1:
        page = 1
    page_size = 25
    offset = (page - 1) * page_size

    filter_kind = (request.args.get("kind") or "").strip() or None

    transactions: list = []
    total_count = None
    has_next = False
    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is not None:
        try:
            query = (
                client.table("wallet_transactions")
                .select("*", count="exact")
                .eq("user_id", ctx.user_id)
            )
            if filter_kind:
                query = query.eq("kind", filter_kind)
            # Pull one extra row so we can tell whether a next page
            # exists without a second count query.
            response = (
                query.order("created_at", desc=True)
                .range(offset, offset + page_size)
                .execute()
            )
            rows = list(getattr(response, "data", None) or [])
            if len(rows) > page_size:
                has_next = True
                rows = rows[:page_size]
            transactions = rows
            total_count = getattr(response, "count", None)
        except Exception:  # noqa: BLE001
            logger.warning(
                "wallet_transactions: ledger lookup failed for %s",
                ctx.user_id, exc_info=True,
            )

    # Read-side lineage annotation. Presentation only: no amount,
    # balance, or ledger row is changed. For every hold on this page
    # (or referenced by a settle row on this page) we fetch the full
    # lineage (the hold plus every row whose parent_tx_id is the
    # hold) so the true net cost of a job can be shown even when the
    # hold and its settlement straddle a page boundary. Any failure
    # falls back to plain rendering; the ledger page must never 500
    # because of this extra query.
    tx_annotations = _build_tx_lineage_annotations(
        client, ctx.user_id, transactions,
    )

    return render_template(
        "wallet/transactions.html",
        wallet=wallet,
        transactions=transactions,
        tx_annotations=tx_annotations,
        filter_kind=filter_kind,
        page=page,
        page_size=page_size,
        has_next=has_next,
        has_prev=page > 1,
        total_count=total_count,
    )

@wallet_bp.route("/account/wallet/auto-reload", methods=["POST"])
@login_required
def wallet_auto_reload():
    """Persist auto reload settings on the ``user_wallets`` row.

    Reads ``auto_reload_enabled`` (on / off), ``threshold_usd``,
    ``amount_usd``, and ``monthly_cap_usd`` from the form. Coerces
    numeric fields and clamps them to safe ranges so a runaway form
    post cannot set a 1 cent threshold or a million dollar cap.
    """
    ctx = load_user_context()
    if ctx is None:
        return redirect(url_for("auth.login"))

    # Form fields land under the input names defined in topup.html
    # (auto_reload_enabled / _threshold_usd / _amount_usd /
    # _monthly_cap_usd). Accept both the bare and the suffixed names
    # so a future template rename does not silently break the route.
    enabled_raw = (
        request.form.get("auto_reload_enabled")
        or request.form.get("enabled")
        or ""
    ).strip().lower()
    enabled = enabled_raw in {"on", "true", "1", "yes"}

    def _coerce(name_a: str, name_b: str, default: Decimal) -> Decimal:
        raw = (
            request.form.get(name_a)
            or request.form.get(name_b)
            or ""
        ).strip()
        if not raw:
            return default
        try:
            return Decimal(raw)
        except (InvalidOperation, ValueError):
            return default

    threshold = _coerce(
        "auto_reload_threshold_usd", "threshold_usd", Decimal("10")
    )
    amount = _coerce(
        "auto_reload_amount_usd", "amount_usd", Decimal("50")
    )
    monthly_cap = _coerce(
        "auto_reload_monthly_cap_usd", "monthly_cap_usd", Decimal("1000")
    )

    # Clamp to plan documented safe bounds. Threshold must be at
    # least $5 (below that auto reload runs constantly); amount must
    # be at least the minimum top up; monthly cap must be at least
    # $100 so a typo cannot disable the safety net entirely.
    if threshold < Decimal("5"):
        threshold = Decimal("5")
    if amount < MIN_TOPUP_USD:
        amount = MIN_TOPUP_USD
    if monthly_cap < Decimal("100"):
        monthly_cap = Decimal("100")

    from shared.credits import get_service_client  # noqa: PLC0415

    client = get_service_client()
    if client is not None:
        try:
            client.table("user_wallets").update(
                {
                    "auto_reload_enabled": enabled,
                    "auto_reload_threshold_usd": float(threshold),
                    "auto_reload_amount_usd": float(amount),
                    "auto_reload_monthly_cap_usd": float(monthly_cap),
                }
            ).eq("user_id", ctx.user_id).execute()
        except Exception:  # noqa: BLE001
            logger.warning(
                "wallet_auto_reload: update failed for %s",
                ctx.user_id, exc_info=True,
            )

    return redirect("/account/wallet/topup#auto-reload")
