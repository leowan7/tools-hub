"""Admin / staff routes (blueprint refactor, Commit 6).

The /admin/* staff surface: wet-lab campaign ops (status/quote/results),
the per-user activity dashboards, and the signup-rejection report. Lifted
verbatim from ``create_app()``; the inline STAFF_EMAILS gating is preserved
unchanged. Only ``@flask_app.route`` -> ``@admin_bp.route`` and admin
endpoint self-refs -> ``admin.*``. The _audit_staff_action helper (used
only by these routes) moves in.
"""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__)


# ------------------------------------------------------------------
# Admin routes — /admin/lab-projects/* (wet-lab campaigns)
# ------------------------------------------------------------------

def _audit_staff_action(action: str, *, target_id: str, props: dict | None = None) -> None:
    """Append a staff/admin state change to the user_events audit trail
    (cso audit L2). Best-effort — log_event never raises. Captures who
    (staff email + user_id), what (action + props), and which entity."""
    from shared.events import log_event  # noqa: PLC0415
    try:
        log_event(
            event_type=f"staff_action:{action}"[:64],
            user_id=session.get("user_id"),
            session_id=session.get("anon_session_id"),
            path=request.path,
            props={
                "staff_email": session.get("user_email") or "",
                "target_id": target_id,
                **(props or {}),
            },
            ip=(
                request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or request.remote_addr
            ),
            user_agent=request.headers.get("User-Agent"),
        )
    except Exception:  # noqa: BLE001 — audit logging must never break the action
        logger.warning("staff audit log failed for %s", action, exc_info=True)

@admin_bp.route("/admin/lab-projects", methods=["GET"])
def admin_campaigns_list():
    from shared.auth import require_staff, STAFF_EMAILS  # noqa: PLC0415
    from shared.campaigns import list_all_campaigns, STATUSES  # noqa: PLC0415
    email = session.get("user_email", "")
    if not email:
        return redirect(url_for("auth.login", next=request.path))
    if email not in STAFF_EMAILS:
        return render_template("404.html"), 404
    status_filter = request.args.get("status") or None
    campaigns = list_all_campaigns(status=status_filter)
    return render_template(
        "admin/campaigns_list.html",
        campaigns=campaigns,
        statuses=list(STATUSES),
        current_status=status_filter,
    )

@admin_bp.route("/admin/lab-projects/<campaign_id>", methods=["GET"])
def admin_campaign_detail(campaign_id: str):
    from shared.auth import STAFF_EMAILS  # noqa: PLC0415
    from shared.campaigns import (  # noqa: PLC0415
        get_campaign,
        STATUSES,
        API_STATUSES,
    )
    email = session.get("user_email", "")
    if not email:
        return redirect(url_for("auth.login", next=request.path))
    if email not in STAFF_EMAILS:
        return render_template("404.html"), 404
    campaign = get_campaign(campaign_id)
    if campaign is None:
        return render_template("404.html"), 404
    flash_msg = None
    flash_kind = "success"
    if request.args.get("updated") == "1":
        flash_msg = "Status updated."
    elif request.args.get("quoted") == "1":
        flash_msg = "Quote saved."
    elif request.args.get("quote_error") == "price_required":
        flash_msg = "Post a price before moving this experiment to QuoteSent (or keep it pre-quote)."
        flash_kind = "error"
    elif request.args.get("quote_error") == "1":
        flash_msg = "Quote could not be saved. Check the values and retry."
        flash_kind = "error"
    elif request.args.get("results_saved") == "1":
        flash_msg = "Results saved."
    elif request.args.get("results_error") == "1":
        flash_msg = "Results could not be saved. Check the JSON, and keep each upload under 20 MB total, then retry."
        flash_kind = "error"
    elif request.args.get("status_error") == "quote_required":
        flash_msg = "Post a price in the Quote panel before moving this experiment to QuoteSent."
        flash_kind = "error"
    # API-direct (MCP/REST) campaigns live on the longer Adaptyv-style FSM;
    # web-funnel campaigns on the short one. Offer the right status set so
    # the admin form posts a value the backend will accept.
    statuses = (
        list(API_STATUSES)
        if campaign.submission_source == "api"
        else list(STATUSES)
    )
    return render_template(
        "admin/campaign_detail.html",
        campaign=campaign,
        statuses=statuses,
        flash_msg=flash_msg or None,
        flash_kind=flash_kind,
    )

@admin_bp.route("/admin/lab-projects/<campaign_id>/status", methods=["POST"])
def admin_campaign_update_status(campaign_id: str):
    from shared.auth import STAFF_EMAILS  # noqa: PLC0415
    from shared.campaigns import (  # noqa: PLC0415
        PRICE_REQUIRED_STATUSES,
        get_campaign,
        update_status,
        transition_api_status,
        set_campaign_admin_fields,
    )
    from shared.email import send_campaign_status_email  # noqa: PLC0415
    email = session.get("user_email", "")
    if not email:
        return redirect(url_for("auth.login"))
    if email not in STAFF_EMAILS:
        return render_template("404.html"), 404

    campaign = get_campaign(campaign_id)
    if campaign is None:
        return render_template("404.html"), 404

    prev_status     = campaign.status
    new_status      = request.form.get("status", "").strip()
    contact         = request.form.get("ranomics_contact", "").strip() or None
    notes_internal  = request.form.get("notes_internal", "").strip() or None
    notes_customer  = request.form.get("notes_customer", "").strip() or None
    notify_customer = request.form.get("notify_customer") == "1"

    # API-direct (MCP/REST) campaigns run on the Adaptyv-style FSM and must
    # transition through the atomic RPC (update_status only accepts the web
    # enum and would reject an API status). Admin changes are SILENT by
    # default — the customer's agent observes them on its next poll. Phase 3
    # adds an opt-in: tick "Notify customer" and, on a real transition into
    # a customer-relevant state (QuoteSent / Done / Cancelled) with a
    # webhook_url set, fire one signed webhook carrying notes_customer.
    # Contact / internal / customer notes are persisted separately since the
    # RPC ignores them.
    if campaign.submission_source == "api":
        # Persist contact / internal / customer notes FIRST, so a blocked
        # transition (the price guard below) never silently discards fields
        # typed in the same submission. These columns are independent of the
        # FSM, so order does not matter for them.
        saved = set_campaign_admin_fields(
            campaign_id,
            ranomics_contact=contact,
            notes_internal=notes_internal,
            notes_customer=notes_customer,
        )

        # Guard: an API row must not cross into 'QuoteSent' OR any later
        # lab-work state without a posted price. The quote form
        # (admin_campaign_save_quote) is the proper path and persists a
        # price before advancing. The FSM RPC is forward-only but NOT
        # adjacency-enforced, so the bare status dropdown could otherwise
        # jump a null-price row straight to WaitingForMaterials (or beyond),
        # skipping the quote line and bypassing confirm_quote's own price
        # guard. Block the whole price-required band; 'Cancelled' is exempt.
        if (
            new_status != prev_status
            and new_status in PRICE_REQUIRED_STATUSES
            and campaign.quote_total_usd is None
        ):
            return redirect(
                url_for("admin.admin_campaign_detail", campaign_id=campaign_id)
                + "?status_error=quote_required"
            )

        transitioned = None
        if new_status and new_status != prev_status:
            try:
                transitioned = transition_api_status(
                    campaign_id, new_status=new_status, by="admin"
                )
            except ValueError:
                # Invalid API status — ignore and fall through to redirect.
                transitioned = None
        # If the operator attached a customer note but the persist failed
        # (service client down / RLS / migration 0032 absent), do NOT fire a
        # webhook carrying a note the stored record never received.
        notes_persist_failed = notes_customer is not None and saved is None

        _NOTIFY_STATUSES = {"QuoteSent", "Done", "Cancelled"}
        if (
            notify_customer
            and not notes_persist_failed
            and transitioned is not None
            and transitioned.moved
            and transitioned.campaign is not None
            and transitioned.campaign.status in _NOTIFY_STATUSES
            and transitioned.campaign.webhook_url
        ):
            try:
                from shared.webhooks import dispatch_webhook  # noqa: PLC0415

                payload = {
                    "event_type": "experiment.status_changed",
                    "experiment_id": transitioned.campaign.id,
                    "prev_status": transitioned.prev_status,
                    "new_status": transitioned.campaign.status,
                    "results_status": transitioned.campaign.results_status,
                    "timestamp": transitioned.campaign.last_transition_at,
                }
                # Source the note from the persisted row so the payload
                # never diverges from what GET /experiments returns. Include
                # it whenever the stored row carries a customer note, not
                # only when this submission set one (a blank form field
                # leaves a prior note in place, and the customer should see
                # it on the notification too).
                if saved is not None and saved.notes_customer:
                    payload["notes_customer"] = saved.notes_customer
                dispatch_webhook(
                    campaign_id=transitioned.campaign.id,
                    event_type="experiment.status_changed",
                    target_url=transitioned.campaign.webhook_url,
                    owner_user_id=transitioned.campaign.user_id,
                    payload=payload,
                )
            except Exception:
                logger.warning(
                    "admin status-change webhook dispatch raised for %s",
                    campaign_id,
                    exc_info=True,
                )
        _audit_staff_action(
            "campaign_status",
            target_id=campaign_id,
            props={
                "source": "api",
                "prev_status": prev_status,
                "new_status": new_status,
                "moved": bool(transitioned and transitioned.moved),
                "notify_customer": notify_customer,
            },
        )
        return redirect(
            url_for("admin.admin_campaign_detail", campaign_id=campaign_id)
            + "?updated=1"
        )

    try:
        updated = update_status(
            campaign_id,
            status=new_status,
            ranomics_contact=contact,
            notes_internal=notes_internal,
        )
    except ValueError:
        return redirect(url_for("admin.admin_campaign_detail", campaign_id=campaign_id))

    if updated and updated.status != prev_status:
        # Look up submitter email via service client.
        from shared.credits import get_service_client  # noqa: PLC0415
        client = get_service_client()
        user_email_for_notify = None
        if client:
            try:
                resp = client.auth.admin.get_user_by_id(updated.user_id)
                user_email_for_notify = getattr(resp.user, "email", None)
            except Exception:
                pass
        if user_email_for_notify:
            try:
                send_campaign_status_email(
                    campaign=updated,
                    user_email=user_email_for_notify,
                    prev_status=prev_status,
                )
            except Exception:
                logger.warning("campaign status email failed", exc_info=True)

    _audit_staff_action(
        "campaign_status",
        target_id=campaign_id,
        props={
            "source": "web",
            "prev_status": prev_status,
            "new_status": (updated.status if updated else new_status),
        },
    )
    return redirect(
        url_for("admin.admin_campaign_detail", campaign_id=campaign_id) + "?updated=1"
    )

@admin_bp.route("/admin/lab-projects/<campaign_id>/quote", methods=["POST"])
def admin_campaign_save_quote(campaign_id: str):
    """Persist an operator-entered quote on an API-FSM campaign.

    Quotes apply only to API-direct (MCP/REST) campaigns; web-funnel
    rows have no quote concept and run on the short status enum, so a
    non-API row 404s. set_campaign_quote writes all quote columns (the
    form is the full source of truth). If "Move status to QuoteSent on
    save" is checked and the row is still pre-quote, we advance the FSM
    via the atomic RPC — silently, like every other admin transition
    (no webhook/email; the customer's agent observes it on its next
    poll). Phase 3 adds opt-in customer notification.
    """
    from shared.auth import STAFF_EMAILS  # noqa: PLC0415
    from shared.campaigns import (  # noqa: PLC0415
        PRICE_REQUIRED_STATUSES,
        get_campaign,
        set_campaign_quote,
        transition_api_status,
    )
    email = session.get("user_email", "")
    if not email:
        return redirect(url_for("auth.login"))
    if email not in STAFF_EMAILS:
        return render_template("404.html"), 404

    campaign = get_campaign(campaign_id)
    if campaign is None:
        return render_template("404.html"), 404
    if campaign.submission_source != "api":
        return render_template("404.html"), 404

    # Authoritative total. Blank -> None (and possibly summed below).
    raw_total = request.form.get("quote_total_usd", "").strip()
    total_usd = None
    if raw_total:
        try:
            parsed = float(raw_total)
        except ValueError:
            parsed = None
        if parsed is not None and parsed >= 0:
            total_usd = parsed

    # Line items come in as three parallel arrays (one row each). Each
    # rendered row always emits all three inputs, so the lists stay
    # index-aligned; drop rows that are entirely blank.
    names = request.form.getlist("line_name")
    amounts = request.form.getlist("line_amount")
    line_notes = request.form.getlist("line_notes")
    line_items: list[dict] = []
    for i, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        raw_amt = (amounts[i] if i < len(amounts) else "").strip()
        note = (line_notes[i] if i < len(line_notes) else "").strip()
        if not name and not raw_amt and not note:
            continue
        item: dict = {"name": name}
        if raw_amt:
            try:
                amt = float(raw_amt)
            except ValueError:
                amt = None
            if amt is not None and amt >= 0:
                item["amount_usd"] = amt
        if note:
            item["notes"] = note
        line_items.append(item)

    # Convenience: no explicit total but the line items carry amounts ->
    # use their sum so the customer still sees a total.
    if total_usd is None and line_items:
        summed = sum(it["amount_usd"] for it in line_items if "amount_usd" in it)
        if summed > 0:
            total_usd = float(summed)

    # A bare date input means "valid through end of that day" (UTC).
    # Validate it here: an invalid/forged value must NOT reach the
    # timestamptz column, where a cast error would be swallowed by
    # set_campaign_quote and look like a silent no-save.
    from datetime import date as _date  # noqa: PLC0415
    raw_valid = request.form.get("quote_valid_until", "").strip()
    valid_until = None
    if raw_valid:
        try:
            parsed_valid = _date.fromisoformat(raw_valid)
        except ValueError:
            return redirect(
                url_for("admin.admin_campaign_detail", campaign_id=campaign_id)
                + "?quote_error=1"
            )
        valid_until = f"{parsed_valid.isoformat()}T23:59:59+00:00"

    quote_notes = request.form.get("quote_notes", "").strip() or None

    # Refuse to advance to (or remain at) QuoteSent without a posted price.
    # Blocks a blank-total "Move to QuoteSent" save, and prevents re-saving a
    # blank form from nulling the price on a row already in QuoteSent (which
    # would strand the customer's quote and make confirm_quote 409). total_usd
    # is final here (explicit value or line-item sum).
    wants_quotesent = request.form.get("set_quote_sent") == "1" and campaign.status in (
        "Draft",
        "WaitingForConfirmation",
    )
    if total_usd is None and (
        wants_quotesent or campaign.status in PRICE_REQUIRED_STATUSES
    ):
        return redirect(
            url_for("admin.admin_campaign_detail", campaign_id=campaign_id)
            + "?quote_error=price_required"
        )

    saved = set_campaign_quote(
        campaign_id,
        total_usd=total_usd,
        currency="USD",
        line_items=line_items,
        valid_until=valid_until,
        notes=quote_notes,
    )
    if saved is None:
        # Write failed (service client down, RLS, CHECK violation, …).
        # Do NOT advance the FSM or claim success — surface an error.
        return redirect(
            url_for("admin.admin_campaign_detail", campaign_id=campaign_id)
            + "?quote_error=1"
        )

    # Quote is persisted, so a customer fetching /quote the instant the
    # status flips already sees real numbers. Only now advance the FSM.
    # transition_api_status is forward-only and a no-op past QuoteSent.
    transitioned = None
    if request.form.get("set_quote_sent") == "1" and campaign.status in (
        "Draft",
        "WaitingForConfirmation",
    ):
        try:
            transitioned = transition_api_status(
                campaign_id, new_status="QuoteSent", by="admin"
            )
        except ValueError:
            transitioned = None

    # Phase 3 parity: posting a quote is exactly when an autonomous agent
    # wants to know. When the operator opts in ("Notify customer") and the
    # row actually moved to QuoteSent with a webhook_url, fire one signed
    # status_changed webhook so the agent can fetch the quote without
    # polling. Silent otherwise, like every other admin change.
    if (
        request.form.get("notify_customer") == "1"
        and transitioned is not None
        and transitioned.moved
        and transitioned.campaign is not None
        and transitioned.campaign.status == "QuoteSent"
        and transitioned.campaign.webhook_url
    ):
        try:
            from shared.webhooks import dispatch_webhook  # noqa: PLC0415

            dispatch_webhook(
                campaign_id=transitioned.campaign.id,
                event_type="experiment.status_changed",
                target_url=transitioned.campaign.webhook_url,
                owner_user_id=transitioned.campaign.user_id,
                payload={
                    "event_type": "experiment.status_changed",
                    "experiment_id": transitioned.campaign.id,
                    "prev_status": transitioned.prev_status,
                    "new_status": transitioned.campaign.status,
                    "results_status": transitioned.campaign.results_status,
                    "timestamp": transitioned.campaign.last_transition_at,
                },
            )
        except Exception:
            logger.warning(
                "quote-ready webhook dispatch raised for %s",
                campaign_id,
                exc_info=True,
            )

    _audit_staff_action(
        "campaign_quote",
        target_id=campaign_id,
        props={
            "quote_total_usd": total_usd,
            "moved_to_quotesent": bool(transitioned and transitioned.moved),
        },
    )
    return redirect(
        url_for("admin.admin_campaign_detail", campaign_id=campaign_id) + "?quoted=1"
    )

@admin_bp.route("/admin/lab-projects/<campaign_id>/results", methods=["POST"])
def admin_campaign_save_results(campaign_id: str):
    """Attach results to an API-FSM campaign (gap G2).

    Accepts up to three result files (enrichment CSV, hits FASTA, raw
    reads FASTQ) uploaded to Supabase Storage under
    lab-campaigns/{id}/results/, and/or a pasted YDS results JSON
    (rounds + sequences, optional external downloads). File uploads are
    additive: each save merges newly uploaded paths onto any previously
    stored download_paths, and a blank JSON box leaves prior rounds /
    sequences intact. The results_status picker gates whether the API
    serves the envelope. When results_status first leaves 'none' (or
    changes among partial/all) and the row has a webhook_url, a
    results-ready webhook fires; otherwise this is silent like every
    other admin change.
    """
    import json as _json  # noqa: PLC0415
    import posixpath as _posixpath  # noqa: PLC0415
    from shared.auth import STAFF_EMAILS  # noqa: PLC0415
    from shared.campaigns import (  # noqa: PLC0415
        RESULTS_STATUSES,
        get_campaign,
        set_campaign_results,
    )
    from shared.storage import StorageError, upload_campaign_result  # noqa: PLC0415

    email = session.get("user_email", "")
    if not email:
        return redirect(url_for("auth.login"))
    if email not in STAFF_EMAILS:
        return render_template("404.html"), 404

    campaign = get_campaign(campaign_id)
    if campaign is None:
        return render_template("404.html"), 404
    if campaign.submission_source != "api":
        return render_template("404.html"), 404

    prev_results_status = campaign.results_status

    results_status = request.form.get("results_status", "").strip() or "none"
    if results_status not in RESULTS_STATUSES:
        return redirect(
            url_for("admin.admin_campaign_detail", campaign_id=campaign_id)
            + "?results_error=1"
        )

    # Start from the existing envelope so uploads are additive and a
    # blank JSON box preserves prior rounds/sequences.
    envelope: dict = dict(campaign.results or {})
    download_paths: dict = dict(envelope.get("download_paths") or {})

    # Optional pasted YDS JSON: validate FIRST (before any upload) so a
    # typo can't orphan a freshly stored file. Present -> replaces
    # rounds/sequences (and external downloads); blank keeps prior values.
    raw_json = request.form.get("results_json", "").strip()
    if raw_json:
        try:
            parsed = _json.loads(raw_json)
        except ValueError:
            return redirect(
                url_for("admin.admin_campaign_detail", campaign_id=campaign_id)
                + "?results_error=1"
            )
        if not isinstance(parsed, dict):
            return redirect(
                url_for("admin.admin_campaign_detail", campaign_id=campaign_id)
                + "?results_error=1"
            )
        if isinstance(parsed.get("rounds"), list):
            envelope["rounds"] = parsed["rounds"]
        if isinstance(parsed.get("sequences"), list):
            envelope["sequences"] = parsed["sequences"]
        if isinstance(parsed.get("downloads"), dict):
            envelope["downloads"] = parsed["downloads"]

    # The three documented download slots; each optional. Uploaded only
    # after JSON validation so a bad paste never leaves an orphaned object.
    slot_content_types = {
        "enrichment_table_csv": "text/csv",
        "hits_fasta": "text/plain",
        "raw_reads_fastq": "application/octet-stream",
    }
    for slot, default_ct in slot_content_types.items():
        uploaded = request.files.get(slot)
        if uploaded is None or not uploaded.filename:
            continue
        data = uploaded.read()
        if not data:
            continue
        # Name the stored object by the slot so re-uploads overwrite the
        # same path; keep the original extension.
        ext = _posixpath.splitext(uploaded.filename)[1]
        stored_name = f"{slot}{ext}" if ext else slot
        try:
            path = upload_campaign_result(
                campaign_id=campaign_id,
                filename=stored_name,
                data=data,
                content_type=uploaded.mimetype or default_ct,
            )
        except StorageError:
            logger.warning(
                "results upload failed for %s slot %s",
                campaign_id,
                slot,
                exc_info=True,
            )
            return redirect(
                url_for("admin.admin_campaign_detail", campaign_id=campaign_id)
                + "?results_error=1"
            )
        download_paths[slot] = path

    if download_paths:
        envelope["download_paths"] = download_paths

    saved = set_campaign_results(
        campaign_id,
        results=envelope,
        results_status=results_status,
    )
    if saved is None:
        return redirect(
            url_for("admin.admin_campaign_detail", campaign_id=campaign_id)
            + "?results_error=1"
        )

    # Notify the agent only when results genuinely became available.
    if (
        saved.results_status != "none"
        and saved.results_status != prev_results_status
        and saved.webhook_url
    ):
        try:
            from shared.webhooks import dispatch_webhook  # noqa: PLC0415

            dispatch_webhook(
                campaign_id=saved.id,
                event_type="experiment.results_ready",
                target_url=saved.webhook_url,
                owner_user_id=saved.user_id,
                payload={
                    "event_type": "experiment.results_ready",
                    "experiment_id": saved.id,
                    "prev_status": saved.status,
                    "new_status": saved.status,
                    "results_status": saved.results_status,
                    "timestamp": saved.last_transition_at,
                },
            )
        except Exception:
            logger.warning(
                "results-ready webhook dispatch raised for %s",
                saved.id,
                exc_info=True,
            )

    _audit_staff_action(
        "campaign_results",
        target_id=campaign_id,
        props={"results_status": saved.results_status},
    )
    return redirect(
        url_for("admin.admin_campaign_detail", campaign_id=campaign_id)
        + "?results_saved=1"
    )

# ------------------------------------------------------------------
# Admin routes — /admin/users/* and /admin/signups/rejected
# ------------------------------------------------------------------

@admin_bp.route("/admin/users", methods=["GET"])
def admin_users_list():
    """Per-user activity dashboard: signup quality, runs, last seen.

    Pulls auth.users via service role (50-row first page), joins
    ``public.user_profiles``, ``credits_balance``, and the trailing
    30-day count from ``public.user_events`` + ``public.tool_jobs``.
    Sorts by last-activity DESC so the most engaged users surface
    first.
    """
    from shared.auth import STAFF_EMAILS  # noqa: PLC0415
    from shared.credits import get_service_client  # noqa: PLC0415

    email = session.get("user_email", "")
    if not email:
        return redirect(url_for("auth.login", next=request.path))
    if email not in STAFF_EMAILS:
        return render_template("404.html"), 404

    client = get_service_client()
    users: list[dict] = []
    if client is not None:
        try:
            from datetime import datetime, timedelta, timezone  # noqa: PLC0415
            window_start = (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).isoformat()

            page = client.auth.admin.list_users()
            auth_users = getattr(page, "users", None) or page

            profile_rows = (
                client.table("user_profiles").select("*").execute().data or []
            )
            profiles_by_id = {r["user_id"]: r for r in profile_rows}

            balance_rows = (
                client.table("user_wallets")
                .select("user_id,balance_usd")
                .execute()
                .data
                or []
            )
            balance_by_id = {
                r["user_id"]: float(r.get("balance_usd") or 0)
                for r in balance_rows
            }

            event_rows = (
                client.table("user_events")
                .select("user_id,event_type,created_at")
                .gte("created_at", window_start)
                .execute()
                .data
                or []
            )
            run_rows = (
                client.table("tool_jobs")
                .select("user_id,created_at,status")
                .gte("created_at", window_start)
                .execute()
                .data
                or []
            )

            from collections import defaultdict  # noqa: PLC0415
            event_count: dict = defaultdict(int)
            last_event: dict = {}
            for r in event_rows:
                uid = r.get("user_id")
                if not uid:
                    continue
                event_count[uid] += 1
                ts = r.get("created_at") or ""
                if ts > last_event.get(uid, ""):
                    last_event[uid] = ts

            run_count: dict = defaultdict(int)
            last_run: dict = {}
            for r in run_rows:
                uid = r.get("user_id")
                if not uid:
                    continue
                run_count[uid] += 1
                ts = r.get("created_at") or ""
                if ts > last_run.get(uid, ""):
                    last_run[uid] = ts

            for u in auth_users:
                uid = getattr(u, "id", None) or (u.get("id") if isinstance(u, dict) else None)
                if not uid:
                    continue
                user_email = (
                    getattr(u, "email", None)
                    or (u.get("email") if isinstance(u, dict) else None)
                )
                created_at = (
                    getattr(u, "created_at", None)
                    or (u.get("created_at") if isinstance(u, dict) else None)
                )
                profile = profiles_by_id.get(uid, {})
                last_activity = max(
                    last_event.get(uid, ""),
                    last_run.get(uid, ""),
                ) or created_at or ""
                users.append({
                    "user_id": uid,
                    "email": user_email,
                    "created_at": str(created_at)[:19] if created_at else "",
                    "signup_quality": profile.get("signup_quality") or "legacy",
                    "domain_class": profile.get("domain_class") or "",
                    "purpose": profile.get("purpose"),
                    "wallet_usd": balance_by_id.get(uid, 0.0),
                    "runs_30d": run_count.get(uid, 0),
                    "events_30d": event_count.get(uid, 0),
                    "last_activity": str(last_activity)[:19] if last_activity else "",
                })
            users.sort(key=lambda u: u.get("last_activity") or "", reverse=True)
        except Exception:
            logger.warning("admin_users_list query failed", exc_info=True)

    return render_template("admin/users_list.html", users=users)

# TODO(account-deletion): There is no in-app account-deletion action yet;
# deleting a user (Supabase dashboard or a future admin/self-serve control)
# cascades the DB rows but leaves their Storage objects orphaned. When an
# account-deletion action is added here, it MUST call
# ``cron.purge_old_storage.purge_user_objects(user_id, dry_run=False)`` BEFORE
# the auth.users row is deleted — the DB cascade removes the user's
# lab_campaigns rows, and those are what locate the user's lab-campaigns
# bucket folders. Until then the erasure is available via the CLI:
# ``flask storage:purge-user --user-id <uuid> --apply``.
@admin_bp.route("/admin/users/<user_id>", methods=["GET"])
def admin_user_detail(user_id: str):
    """Per-user activity timeline: events + tool runs + credits."""
    from shared.auth import STAFF_EMAILS  # noqa: PLC0415
    from shared.credits import get_service_client  # noqa: PLC0415

    viewer = session.get("user_email", "")
    if not viewer:
        return redirect(url_for("auth.login", next=request.path))
    if viewer not in STAFF_EMAILS:
        return render_template("404.html"), 404

    client = get_service_client()
    target = {
        "user_id": user_id,
        "email": None,
        "created_at": "",
        "profile": {},
        "wallet_usd": 0.0,
        "timeline": [],
    }
    if client is None:
        return render_template("admin/user_detail.html", target=target)

    try:
        user_resp = client.auth.admin.get_user_by_id(user_id)
        user_obj = getattr(user_resp, "user", None)
        if user_obj is not None:
            target["email"] = getattr(user_obj, "email", None)
            target["created_at"] = (
                str(getattr(user_obj, "created_at", "") or "")[:19]
            )
    except Exception:
        logger.warning("get_user_by_id failed for %s", user_id, exc_info=True)

    try:
        prof = (
            client.table("user_profiles")
            .select("*")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        target["profile"] = getattr(prof, "data", None) or {}
    except Exception:
        target["profile"] = {}

    try:
        bal = (
            client.table("user_wallets")
            .select("balance_usd")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        data = getattr(bal, "data", None) or {}
        target["wallet_usd"] = float(data.get("balance_usd") or 0)
    except Exception:
        target["wallet_usd"] = 0.0

    # Build a unified timeline by interleaving three sources.
    timeline: list[dict] = []
    try:
        events = (
            client.table("user_events")
            .select("event_type,path,props,created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(200)
            .execute()
            .data
            or []
        )
        for e in events:
            timeline.append({
                "kind": "event",
                "label": e.get("event_type"),
                "detail": e.get("path") or "",
                "props": e.get("props") or {},
                "created_at": e.get("created_at"),
            })
    except Exception:
        logger.warning("user_events query failed", exc_info=True)
    try:
        runs = (
            client.table("tool_jobs")
            .select("id,tool,preset,status,gpu_seconds_used,created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
        for r in runs:
            gpu_s = r.get("gpu_seconds_used") or 0
            timeline.append({
                "kind": "run",
                "label": f"{r.get('tool')} run · {r.get('status')}",
                "detail": (
                    f"preset={r.get('preset')}"
                    + (f" · {gpu_s} gpu-sec" if gpu_s else "")
                ),
                "job_id": r.get("id"),
                "created_at": r.get("created_at"),
            })
    except Exception:
        logger.warning("tool_jobs query failed", exc_info=True)
    try:
        wallet_rows = (
            client.table("wallet_transactions")
            .select("kind,amount_usd,job_id,gpu_seconds,created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
        for w in wallet_rows:
            amount = w.get("amount_usd") or 0
            bits = [f"${float(amount):+.4f}"]
            if w.get("gpu_seconds"):
                bits.append(f"{w.get('gpu_seconds')} gpu-sec")
            timeline.append({
                "kind": "wallet",
                "label": f"{w.get('kind')}",
                "detail": " · ".join(bits),
                "created_at": w.get("created_at"),
            })
    except Exception:
        logger.warning("wallet_transactions query failed", exc_info=True)
    try:
        ledger = (
            client.table("credits_ledger")
            .select("kind,delta,reason,tool,created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
            .data
            or []
        )
        for l in ledger:
            # Internal margin-accounting ledger — kept for audit but
            # not the customer-facing money path (that's wallet_*).
            timeline.append({
                "kind": "ledger",
                "label": f"{l.get('kind')} ({l.get('delta')})",
                "detail": l.get("reason") or "",
                "tool": l.get("tool"),
                "created_at": l.get("created_at"),
            })
    except Exception:
        logger.warning("credits_ledger query failed", exc_info=True)

    timeline.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    target["timeline"] = timeline
    return render_template("admin/user_detail.html", target=target)

@admin_bp.route("/admin/signups/rejected", methods=["GET"])
def admin_signups_rejected():
    """Last 30 days of /signup rejections, grouped by reason."""
    from shared.auth import STAFF_EMAILS  # noqa: PLC0415
    from shared.credits import get_service_client  # noqa: PLC0415

    viewer = session.get("user_email", "")
    if not viewer:
        return redirect(url_for("auth.login", next=request.path))
    if viewer not in STAFF_EMAILS:
        return render_template("404.html"), 404

    from collections import defaultdict  # noqa: PLC0415
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    rows: list[dict] = []
    client = get_service_client()
    if client is not None:
        try:
            window_start = (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).isoformat()
            rows = (
                client.table("signup_rejections")
                .select("*")
                .gte("created_at", window_start)
                .order("created_at", desc=True)
                .limit(500)
                .execute()
                .data
                or []
            )
        except Exception:
            logger.warning(
                "admin_signups_rejected query failed", exc_info=True
            )

    grouped: dict = defaultdict(list)
    for r in rows:
        grouped[r.get("reason") or "unknown"].append(r)
    groups = sorted(
        (
            {
                "reason": reason,
                "count": len(entries),
                "entries": entries[:25],
            }
            for reason, entries in grouped.items()
        ),
        key=lambda g: g["count"],
        reverse=True,
    )

    return render_template(
        "admin/signups_rejected.html",
        groups=groups,
        total=len(rows),
    )
