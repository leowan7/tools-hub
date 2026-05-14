"""Transactional email helper for the Ranomics tools-hub.

Wave 2 (iterative binder design platform). Long-running pilot jobs
(BindCraft 45 min, PXDesign 35 min) cannot be UX'd as a tab the user
holds open — the run finishes when it finishes, and the user gets an
email with a link to the results page.

Provider: Resend (https://resend.com). Single-call HTTP API, generous
free tier (3000/mo), no SMTP fiddling. Other providers (Postmark, SES,
SendGrid) wire in by changing this file's ``_send`` function only —
the rest of the app calls ``send_job_complete_email`` and does not
care.

Environment
-----------
    RESEND_API_KEY    — Resend API key. If unset the helper logs the
                        intended email and returns False; the rest of
                        the app continues. Lets local dev run without
                        outbound email.
    EMAIL_FROM        — From address. Defaults to "Ranomics
                        Tools <noreply@tools.ranomics.com>". The domain
                        must be verified in Resend.
    PUBLIC_BASE_URL   — Base URL prepended to job-detail links inside
                        the email. Defaults to "https://tools.ranomics.com".

Usage
-----
    from shared.email import send_job_complete_email
    send_job_complete_email(user_email="leo@ranomics.com", job=job)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_FROM = "Ranomics Tools <noreply@tools.ranomics.com>"
DEFAULT_BASE_URL = "https://tools.ranomics.com"
RESEND_ENDPOINT = "https://api.resend.com/emails"


def send_job_complete_email(*, user_email: str, job) -> bool:  # noqa: ANN001
    """Send the "your job is ready" email for ``job``.

    ``job`` is a :class:`shared.jobs.ToolJob`. Returns True on confirmed
    send; False on missing config or send failure (the caller should not
    treat this as a hard error — the email is best-effort).
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    base_url = os.environ.get("PUBLIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    from_addr = os.environ.get("EMAIL_FROM", DEFAULT_FROM)

    job_url = f"{base_url}/jobs/{job.id}"
    tone = _result_tone(job)
    tool = _tool_label(job.tool)
    subject = {
        "success":   f"Your {tool} run finished",
        "empty":     f"Your {tool} run finished — no candidates",
        "failed":    f"Your {tool} run failed",
    }[tone]
    html_body = _render_html(job=job, job_url=job_url, tone=tone)
    text_body = _render_text(job=job, job_url=job_url, tone=tone)

    if not api_key:
        logger.info(
            "EMAIL (no RESEND_API_KEY, skipping send): to=%s subject=%r url=%s",
            user_email,
            subject,
            job_url,
        )
        return False

    try:
        response = requests.post(
            RESEND_ENDPOINT,
            json={
                "from": from_addr,
                "to": [user_email],
                "subject": subject,
                "html": html_body,
                "text": text_body,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    except Exception:
        logger.warning(
            "Resend POST failed for job %s", getattr(job, "id", "?"),
            exc_info=True,
        )
        return False

    if response.status_code >= 300:
        logger.warning(
            "Resend non-2xx for job %s: HTTP %d body=%s",
            getattr(job, "id", "?"),
            response.status_code,
            response.text[:200],
        )
        return False

    logger.info(
        "Email sent for job %s to %s (resend id=%s)",
        getattr(job, "id", "?"),
        user_email,
        (response.json() or {}).get("id"),
    )
    return True


# ---------------------------------------------------------------------------
# Body rendering
# ---------------------------------------------------------------------------


def _tool_label(slug: str) -> str:
    """Map a slug back to a human label without depending on the registry.

    The job-complete email might be sent from a worker that hasn't
    imported the tool adapter modules; keep this self-contained.
    """
    labels = {
        "bindcraft": "BindCraft",
        "rfantibody": "RFantibody",
        "boltzgen": "BoltzGen",
        "pxdesign": "PXDesign",
        "proteinmpnn": "ProteinMPNN",
    }
    return labels.get(slug, slug)


def _render_html(*, job, job_url: str, tone: str) -> str:  # noqa: ANN001
    """Plain HTML — no template engine to keep this email worker-portable."""
    summary = _result_summary(job, tone=tone)
    tool = _tool_label(job.tool)
    headline = {
        "success": f"Your {tool} run is ready",
        "empty":   f"Your {tool} run finished — no candidates",
        "failed":  f"Your {tool} run failed",
    }[tone]
    cta_bg = "#1f9d55" if tone == "success" else "#525252"
    cta_label = "View results" if tone == "success" else "View job details"
    cta = (
        '<a href="' + job_url + '" '
        f'style="display:inline-block;padding:12px 22px;background:{cta_bg};'
        'color:#fff;text-decoration:none;border-radius:6px;font-weight:600;">'
        f"{cta_label}"
        "</a>"
    )
    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                color:#1a1a1a;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin-top:0;">{headline}</h2>
      <p>{summary}</p>
      <p style="margin:24px 0;">{cta}</p>
      <hr style="border:none;border-top:1px solid #e5e5e5;margin:24px 0;">
      <p style="font-size:13px;color:#666;">
        Job <code>{job.id}</code> · preset <code>{job.preset}</code> ·
        {job.credits_cost} credits · submitted {(job.created_at or '')[:19]}
      </p>
      <p style="font-size:12px;color:#999;">
        Ranomics Tools — <a href="https://tools.ranomics.com" style="color:#999;">tools.ranomics.com</a>
      </p>
    </div>
    """.strip()


def _render_text(*, job, job_url: str, tone: str) -> str:  # noqa: ANN001
    summary = _result_summary(job, tone=tone)
    tool = _tool_label(job.tool)
    headline = {
        "success": f"Your {tool} run is ready.",
        "empty":   f"Your {tool} run finished — no candidates.",
        "failed":  f"Your {tool} run failed.",
    }[tone]
    link_label = "View results" if tone == "success" else "View job details"
    return (
        f"{headline}\n\n"
        f"{summary}\n\n"
        f"{link_label}: {job_url}\n\n"
        f"Job {job.id} · preset {job.preset} · "
        f"{job.credits_cost} credits · submitted {(job.created_at or '')[:19]}\n\n"
        "Ranomics Tools — tools.ranomics.com"
    )


def send_campaign_submitted_emails(*, campaign, user_email: str) -> None:
    """Send user confirmation + internal staff notification on campaign submit.

    Best-effort: failures are logged but not raised to the caller.
    """
    from shared.auth import STAFF_EMAILS  # noqa: PLC0415

    base_url  = os.environ.get("PUBLIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    from_addr = os.environ.get("EMAIL_FROM", DEFAULT_FROM)
    api_key   = os.environ.get("RESEND_API_KEY", "").strip()

    campaign_url = f"{base_url}/campaigns/{campaign.id}"

    # User confirmation
    user_subject = f"Scoping request received — {campaign.target_name}"
    user_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                color:#1a1a1a;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin-top:0;">Scoping request received</h2>
      <p>We've received your yeast display scoping request for
         <strong>{campaign.target_name}</strong> ({len(campaign.candidate_indices)}
         candidate{'s' if len(campaign.candidate_indices) != 1 else ''}).</p>
      <p>The Ranomics team will review feasibility against current lab capacity
         and follow up within <strong>2 business days</strong>.</p>
      <p style="margin:24px 0;">
        <a href="{campaign_url}"
           style="display:inline-block;padding:12px 22px;background:#2B9E7E;
                  color:#fff;text-decoration:none;border-radius:6px;font-weight:600;">
          View campaign
        </a>
      </p>
      <hr style="border:none;border-top:1px solid #e5e5e5;margin:24px 0;">
      <p style="font-size:12px;color:#999;">
        Ranomics Tools — <a href="https://tools.ranomics.com" style="color:#999;">tools.ranomics.com</a>
      </p>
    </div>
    """.strip()
    user_text = (
        f"Scoping request received for {campaign.target_name}.\n\n"
        "The Ranomics team will review and follow up within 2 business days.\n\n"
        f"View campaign: {campaign_url}\n\n"
        "Ranomics Tools — tools.ranomics.com"
    )

    # Staff notification
    staff_subject = f"New campaign: {campaign.target_name} from {user_email}"
    admin_url = f"{base_url}/admin/campaigns/{campaign.id}"
    staff_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                color:#1a1a1a;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin-top:0;">New scoping request</h2>
      <table style="font-size:14px;border-collapse:collapse;width:100%;">
        <tr><td style="color:#666;padding:4px 12px 4px 0;">Target</td>
            <td><strong>{campaign.target_name}</strong></td></tr>
        <tr><td style="color:#666;padding:4px 12px 4px 0;">From</td>
            <td>{user_email}</td></tr>
        <tr><td style="color:#666;padding:4px 12px 4px 0;">Assay</td>
            <td>{campaign.assay_type.replace('_', ' ').title()}</td></tr>
        <tr><td style="color:#666;padding:4px 12px 4px 0;">Candidates</td>
            <td>{len(campaign.candidate_indices)}</td></tr>
        <tr><td style="color:#666;padding:4px 12px 4px 0;">Budget</td>
            <td>{campaign.budget_band.title()}</td></tr>
      </table>
      <p style="margin:24px 0;">
        <a href="{admin_url}"
           style="display:inline-block;padding:12px 22px;background:#2B9E7E;
                  color:#fff;text-decoration:none;border-radius:6px;font-weight:600;">
          Review in admin
        </a>
      </p>
    </div>
    """.strip()

    if not api_key:
        logger.info(
            "EMAIL (no key) campaign_submitted: user=%s target=%s id=%s",
            user_email, campaign.target_name, campaign.id,
        )
        return

    for to_addr, subject, html_body, text_body in [
        (user_email, user_subject, user_html, user_text),
        (list(STAFF_EMAILS), staff_subject, staff_html, staff_html),
    ]:
        try:
            to_list = to_addr if isinstance(to_addr, list) else [to_addr]
            requests.post(
                RESEND_ENDPOINT,
                json={"from": from_addr, "to": to_list, "subject": subject,
                      "html": html_body, "text": text_body},
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                timeout=10,
            )
        except Exception:
            logger.warning("send_campaign_submitted_emails failed", exc_info=True)


def send_campaign_status_email(*, campaign, user_email: str, prev_status: str) -> None:
    """Notify the submitter that their campaign status changed.

    Best-effort: failures are logged but not raised.
    """
    base_url  = os.environ.get("PUBLIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    from_addr = os.environ.get("EMAIL_FROM", DEFAULT_FROM)
    api_key   = os.environ.get("RESEND_API_KEY", "").strip()

    campaign_url = f"{base_url}/campaigns/{campaign.id}"
    status_label = campaign.status.replace("_", " ").title()
    subject      = f"Your campaign has been {status_label.lower()} — {campaign.target_name}"

    note_block = ""
    if campaign.notes_internal:
        note_block = f"<p><strong>Note from Ranomics:</strong> {campaign.notes_internal}</p>"

    html_body = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                color:#1a1a1a;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin-top:0;">Campaign update: {status_label}</h2>
      <p>Your scoping request for <strong>{campaign.target_name}</strong>
         has moved to <strong>{status_label}</strong>.</p>
      {note_block}
      <p style="margin:24px 0;">
        <a href="{campaign_url}"
           style="display:inline-block;padding:12px 22px;background:#2B9E7E;
                  color:#fff;text-decoration:none;border-radius:6px;font-weight:600;">
          View campaign
        </a>
      </p>
      <hr style="border:none;border-top:1px solid #e5e5e5;margin:24px 0;">
      <p style="font-size:12px;color:#999;">
        Ranomics Tools — <a href="https://tools.ranomics.com" style="color:#999;">tools.ranomics.com</a>
      </p>
    </div>
    """.strip()

    if not api_key:
        logger.info(
            "EMAIL (no key) campaign_status: user=%s status=%s id=%s",
            user_email, campaign.status, campaign.id,
        )
        return

    try:
        requests.post(
            RESEND_ENDPOINT,
            json={"from": from_addr, "to": [user_email], "subject": subject,
                  "html": html_body, "text": html_body},
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            timeout=10,
        )
    except Exception:
        logger.warning("send_campaign_status_email failed", exc_info=True)


def send_workspace_cap_warning(
    *,
    user_email: str,
    workspace,
) -> bool:
    """Notify a customer that their Workspace has crossed 80% of cap.

    Triggered when ``shared.workspaces.charge_for_job`` reports a
    crossed warning threshold. Pre-emptive — gives the user a chance to
    upgrade to XL or activate a second Workspace before they hit the
    hard 100% block mid-iteration.

    Best-effort: returns False on missing config or send failure.
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    base_url = os.environ.get("PUBLIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    from_addr = os.environ.get("EMAIL_FROM", DEFAULT_FROM)

    workspace_url = f"{base_url}/workspaces/{workspace.id}"
    target_label = workspace.target_label or workspace.target_pdb_id
    pct_used = workspace.pct_used
    remaining_usd = workspace.remaining_usd
    cap_usd = workspace.modal_cap_usd

    subject = f"Your Workspace is at {pct_used:.0f}% — {target_label}"

    sku_label = (
        "Target Workspace XL"
        if workspace.sku == "workspace_xl"
        else "Target Workspace"
    )
    upgrade_cta = ""
    if workspace.sku == "workspace_standard":
        upgrade_cta = """
      <p style="margin:18px 0 0 0; font-size:14px;">
        Need more compute on this target? <strong>Target Workspace XL</strong>
        gives you 5× the budget ($500 Modal cap), priority GPU queue, and a
        30-minute onboarding call — $2,499 per target.
      </p>
      <p style="margin:10px 0 0 0;">
        <a href="{pricing_url}" style="color:#2B9E7E;">See Workspace XL →</a>
      </p>
    """.format(pricing_url=f"{base_url}/pricing")

    html_body = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                color:#1a1a1a;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin-top:0;">Workspace at {pct_used:.0f}%</h2>
      <p>Your <strong>{sku_label}</strong> for <strong>{target_label}</strong>
         has used {pct_used:.0f}% of its compute budget. About
         <strong>${remaining_usd:.2f}</strong> of Modal compute remains
         (cap ${cap_usd:.2f}).</p>
      <p>Designs will stop dispatching once the cap is reached — but you
         can keep using anything already finished, and the Workspace stays
         readable until it expires.</p>
      <p style="margin:18px 0;">
        <a href="{workspace_url}"
           style="display:inline-block;padding:12px 22px;background:#2B9E7E;
                  color:#fff;text-decoration:none;border-radius:6px;font-weight:600;">
          Open Workspace
        </a>
      </p>
      {upgrade_cta}
      <hr style="border:none;border-top:1px solid #e5e5e5;margin:24px 0;">
      <p style="font-size:12px;color:#999;">
        Ranomics Tools — <a href="https://tools.ranomics.com" style="color:#999;">
        tools.ranomics.com</a>
      </p>
    </div>
    """.strip()

    text_body = (
        f"Your {sku_label} for {target_label} is at {pct_used:.0f}% of "
        f"compute budget (${remaining_usd:.2f} remaining of ${cap_usd:.2f}).\n\n"
        f"Open the Workspace: {workspace_url}\n\n"
        f"Need more compute? See Workspace XL: {base_url}/pricing\n\n"
        "Ranomics Tools — tools.ranomics.com"
    )

    return _send_simple(
        api_key=api_key,
        from_addr=from_addr,
        to_email=user_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        log_tag=f"workspace_warn ws={workspace.id}",
    )


def send_workspace_cap_exhausted(
    *,
    user_email: str,
    workspace,
) -> bool:
    """Notify a customer that their Workspace cap has been fully consumed.

    Triggered when a submission is blocked because the Workspace is at
    100%. Sent at most once per Workspace (caller should de-dupe).
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    base_url = os.environ.get("PUBLIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    from_addr = os.environ.get("EMAIL_FROM", DEFAULT_FROM)

    workspace_url = f"{base_url}/workspaces/{workspace.id}"
    target_label = workspace.target_label or workspace.target_pdb_id
    sku_label = (
        "Target Workspace XL"
        if workspace.sku == "workspace_xl"
        else "Target Workspace"
    )

    subject = f"Workspace compute cap reached — {target_label}"
    upgrade_block = ""
    if workspace.sku == "workspace_standard":
        upgrade_block = f"""
      <p style="margin:18px 0 0 0; font-size:14px;">
        <strong>Need to keep going?</strong> Workspace XL gives you 5× the
        compute budget on a new target — $2,499 per target.
      </p>
      <p style="margin:10px 0 0 0;">
        <a href="{base_url}/pricing" style="color:#2B9E7E;">See Workspace XL →</a>
      </p>
        """

    html_body = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                color:#1a1a1a;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin-top:0;">Compute cap reached</h2>
      <p>Your <strong>{sku_label}</strong> for <strong>{target_label}</strong>
         has used its full ${workspace.modal_cap_usd:.2f} compute budget.</p>
      <p>New design runs on this target are paused. Results from completed
         runs remain available until the Workspace expires.</p>
      <p style="margin:18px 0;">
        <a href="{workspace_url}"
           style="display:inline-block;padding:12px 22px;background:#525252;
                  color:#fff;text-decoration:none;border-radius:6px;font-weight:600;">
          View results
        </a>
      </p>
      {upgrade_block}
      <hr style="border:none;border-top:1px solid #e5e5e5;margin:24px 0;">
      <p style="font-size:12px;color:#999;">
        Ranomics Tools — <a href="https://tools.ranomics.com" style="color:#999;">
        tools.ranomics.com</a>
      </p>
    </div>
    """.strip()

    text_body = (
        f"Your {sku_label} for {target_label} has used its full "
        f"${workspace.modal_cap_usd:.2f} compute budget.\n\n"
        f"View results: {workspace_url}\n\n"
        f"Need to keep going? See Workspace XL: {base_url}/pricing\n\n"
        "Ranomics Tools — tools.ranomics.com"
    )

    return _send_simple(
        api_key=api_key,
        from_addr=from_addr,
        to_email=user_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        log_tag=f"workspace_exhausted ws={workspace.id}",
    )


def send_daily_digest(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    payload_summary: Optional[dict] = None,
) -> bool:
    """Send the daily activity digest.

    Plain wrapper around the existing Resend helper. The HTML body is
    rendered upstream in ``cron.daily_digest.render_digest_html`` so
    this layer just handles delivery + logging.

    Returns True on confirmed send. Failures are logged but the caller
    does not need to special-case them — the digest is best-effort.
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_addr = os.environ.get("EMAIL_FROM", DEFAULT_FROM)
    # Plain-text version mirrors the headline of the HTML body. Email
    # clients that strip HTML still see the key counts at a glance.
    summary = payload_summary or {}
    text_body = (
        f"{subject}\n\n"
        f"New signups: {summary.get('signups', 0)}\n"
        f"Rejected:    {summary.get('rejections', 0)}\n"
        f"Tool runs:   {summary.get('runs', 0)}\n"
        f"Active users:{summary.get('active_users', 0)}\n\n"
        "Open in a HTML-capable client for the full breakdown."
    )
    return _send_simple(
        api_key=api_key,
        from_addr=from_addr,
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        log_tag="daily_digest",
    )


def _send_simple(
    *,
    api_key: str,
    from_addr: str,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    log_tag: str,
) -> bool:
    """Shared Resend POST helper for workspace emails."""
    if not api_key:
        logger.info("EMAIL (no RESEND_API_KEY): %s to=%s subject=%r",
                    log_tag, to_email, subject)
        return False
    try:
        response = requests.post(
            RESEND_ENDPOINT,
            json={
                "from": from_addr,
                "to": [to_email],
                "subject": subject,
                "html": html_body,
                "text": text_body,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    except Exception:
        logger.warning("Resend POST failed for %s", log_tag, exc_info=True)
        return False
    if response.status_code >= 300:
        logger.warning(
            "Resend non-2xx for %s: HTTP %d body=%s",
            log_tag, response.status_code, response.text[:200],
        )
        return False
    logger.info("Email sent: %s to=%s (resend id=%s)",
                log_tag, to_email,
                (response.json() or {}).get("id"))
    return True


def _result_tone(job) -> str:  # noqa: ANN001
    """Return ``"success"``, ``"empty"``, or ``"failed"``.

    ``empty`` covers the soft-fail case: the pipeline ran end-to-end but
    produced no usable output (every design rejected by the post-pipeline
    filter, MPNN returned no sequences, etc.). The job row is technically
    ``status="succeeded"`` but the user has nothing to look at.
    """
    if job.status != "succeeded":
        return "failed"
    if _is_empty_result(job):
        return "empty"
    return "success"


def _is_empty_result(job) -> bool:  # noqa: ANN001
    """True when a succeeded job's result payload contains no useful output.

    Recognised "useful output" shapes:
      * ``sequences`` (sequence-design tools — MPNN, future LigandMPNN)
      * ``candidates`` (composite binder tools — RFantibody, RFdiffusion,
        BoltzGen, BindCraft, PXDesign)
      * ``pdb_b64`` (structure-prediction tools — AF2, ColabFold, ESMFold)

    A tool whose result shape is not recognised is treated as a real
    success — we'd rather show the user a working page than misclassify
    a future tool's output.
    """
    result = job.result or {}
    if not isinstance(result, dict):
        return False
    seqs = result.get("sequences")
    if isinstance(seqs, list):
        return len(seqs) == 0
    cands = result.get("candidates")
    if isinstance(cands, list):
        return len(cands) == 0
    if result.get("pdb_b64"):
        return False
    return False


def _result_summary(job, *, tone: str) -> str:  # noqa: ANN001
    if tone == "failed":
        err = job.error or {}
        if isinstance(err, dict):
            detail = err.get("detail") or err.get("message") or "see job page for details"
        else:
            detail = str(err)
        # complete_job → _refund_unused_credits issues a full refund when a
        # failed job consumed no GPU time. Mirror that condition here so we
        # don't claim a refund happened when one didn't.
        is_full_refund = (
            job.status == "failed"
            and not job.gpu_seconds_used
            and (job.credits_cost or 0) > 0
        )
        if is_full_refund:
            n = job.credits_cost
            unit = "credit" if n == 1 else "credits"
            verb = "was" if n == 1 else "were"
            return (
                f"The run did not complete and your {n} {unit} {verb} refunded. "
                f"Detail: {detail}"
            )
        return f"The run did not complete: {detail}"

    if tone == "empty":
        result = job.result or {}
        seqs = result.get("sequences") if isinstance(result, dict) else None
        if isinstance(seqs, list):
            return (
                "The run finished but no sequences were returned. See the job "
                "page for details, or rerun with different parameters."
            )
        return (
            "The pipeline finished but produced no passing candidates. This "
            "can happen for difficult targets or when a small design budget "
            "leaves no room to filter — see the job page for the per-design "
            "scores, then try expanding binder length, hotspot list, or "
            "number of designs."
        )

    # tone == "success" — empty-result cases were already handled above.
    result = job.result or {}
    if not isinstance(result, dict):
        return "Run finished — see the job page for results."

    # Sequence-design tools (D1 MPNN, future LigandMPNN): 'sequences[]'.
    seqs = result.get("sequences")
    if isinstance(seqs, list):
        n = len(seqs)
        return (
            f"{n} sequence{'s' if n != 1 else ''} returned with score and "
            "recovery — see the job page."
        )

    # Structure-prediction tools (D2 AF2, D3 ColabFold, D4 ESMFold):
    # 'pdb_b64' + 'mean_plddt' (and optionally 'iptm'/'ptm').
    if result.get("pdb_b64"):
        plddt = result.get("mean_plddt")
        if isinstance(plddt, (int, float)):
            return (
                f"Structure prediction complete (mean pLDDT {plddt:.1f}). "
                "PDB and per-residue metrics on the job page."
            )
        return (
            "Structure prediction complete — PDB and metrics on the job page."
        )

    # Composite binder-design tools (RFantibody, BindCraft, BoltzGen,
    # PXDesign, RFdiffusion): 'candidates[]'.
    cands = result.get("candidates", []) or []
    n = len(cands)
    return (
        f"{n} candidate{'s' if n != 1 else ''} returned with real scores and "
        "downloadable PDBs."
    )


# ===========================================================================
# Wallet email senders (Wave 2)
# ===========================================================================
# Real Resend-backed implementations for the 11 user-facing senders that the
# wallet machinery (``shared.wallet``, ``shared.wallet_funnel``, the Stripe
# webhook handler) calls by name. Plus two internal Slack alerters.
#
# All senders accept their typed kwargs plus ``**_extra`` so the wallet code
# can pass extras forward without TypeError. Returns ``True`` on confirmed
# send, ``False`` on missing config or any failure. Best effort: failures are
# logged but never raise to the caller.
# ---------------------------------------------------------------------------

from decimal import Decimal
from pathlib import Path

import jinja2

_TEMPLATES_ROOT = Path(__file__).resolve().parents[1] / "templates" / "email"
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATES_ROOT)),
    autoescape=jinja2.select_autoescape(["html", "htm", "xml"]),
    trim_blocks=False,
    lstrip_blocks=False,
)


# ---------------------------------------------------------------------------
# Tool label table (mirrors _tool_label above but extended for the wallet
# senders, which include tools not in the original job-complete map).
# ---------------------------------------------------------------------------

_WALLET_TOOL_LABELS = {
    "bindcraft":    "BindCraft",
    "rfantibody":   "RFantibody",
    "rfdiffusion":  "RFdiffusion",
    "boltzgen":     "BoltzGen",
    "pxdesign":     "PXDesign",
    "proteinmpnn":  "ProteinMPNN",
    "mpnn":         "ProteinMPNN",
    "af2":          "AlphaFold2",
    "alphafold2":   "AlphaFold2",
    "colabfold":    "ColabFold",
    "esmfold":      "ESMFold",
}


def _label_for_tool(slug: Optional[str]) -> str:
    """Return a human-readable tool label, falling back to the slug."""
    if not slug:
        return "tool"
    return _WALLET_TOOL_LABELS.get(slug, slug)


# ---------------------------------------------------------------------------
# Money formatting
# ---------------------------------------------------------------------------


def _money(amount) -> str:
    """Format any numeric type as a plain dollar amount string.

    Returns the integer form when the value is whole (e.g. "5"), else two
    decimals (e.g. "12.50"). Used in template variables so the rendered email
    body says "$5" instead of "$5.00" for the signup credit case.
    """
    if amount is None:
        return "0"
    try:
        d = Decimal(str(amount))
    except Exception:
        return str(amount)
    if d == d.to_integral_value():
        return str(int(d))
    return f"{d:.2f}"


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def _render_template(template_name: str, **context) -> str:
    """Render a Jinja2 template from ``templates/email/`` with ``context``.

    Uses a standalone Jinja2 environment so callers do not need a Flask
    app context (these senders run from worker/cron paths as well as
    request paths).
    """
    template = _jinja_env.get_template(template_name)
    return template.render(**context)


def _html_to_text(html: str) -> str:
    """Strip HTML tags + Jinja comments + collapse whitespace for the text part.

    Quick and deliberately dumb: replaces ``<br>``/``</p>`` with newlines,
    drops every other tag. Email clients that prefer text/plain still get a
    legible message even though the HTML version is the canonical body.
    """
    import re  # noqa: PLC0415

    txt = re.sub(r"\{#.*?#\}", "", html, flags=re.S)
    txt = re.sub(r"(?i)<br\s*/?>", "\n", txt)
    txt = re.sub(r"(?i)</p>", "\n\n", txt)
    txt = re.sub(r"(?i)</h[1-6]>", "\n\n", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = re.sub(r"&nbsp;", " ", txt)
    txt = re.sub(r"&amp;", "&", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


# ---------------------------------------------------------------------------
# Resend transport
# ---------------------------------------------------------------------------


def _from_address() -> str:
    """Resolve the from-address.

    The plan dispatch prompt referenced ``RESEND_FROM_TRANSACTIONAL``; the
    existing codebase uses ``EMAIL_FROM``. Honour ``RESEND_FROM_TRANSACTIONAL``
    when set, fall back to the existing ``EMAIL_FROM`` so we stay compatible
    with the older senders in this module and the Railway env that is already
    deployed.
    """
    override = os.environ.get("RESEND_FROM_TRANSACTIONAL", "").strip()
    if override:
        return override
    return os.environ.get("EMAIL_FROM", DEFAULT_FROM)


def _support_email() -> str:
    return os.environ.get("SUPPORT_EMAIL", "support@ranomics.com").strip() or (
        "support@ranomics.com"
    )


def _base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _resolve_user_email(user_id: str) -> Optional[str]:
    """Look up the auth.users email for ``user_id`` via the service-role client.

    Mirrors ``shared.jobs._resolve_email_for_user`` so the wallet senders
    can be invoked with just a ``user_id`` (which is what the wallet code
    has on hand).
    """
    if not user_id:
        return None
    try:
        from shared.credits import get_service_client  # noqa: PLC0415

        client = get_service_client()
        if client is None:
            return None
        page = client.auth.admin.list_users()
        users = getattr(page, "users", None) or page
        for user in users:
            uid = getattr(user, "id", None) or (
                user.get("id") if isinstance(user, dict) else None
            )
            if uid == user_id:
                email = getattr(user, "email", None) or (
                    user.get("email") if isinstance(user, dict) else None
                )
                return email
    except Exception:
        logger.warning(
            "wallet email: could not resolve email for user %s",
            user_id, exc_info=True,
        )
    return None


def _post_resend(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
    log_tag: str,
) -> bool:
    """POST one message to Resend; return True on confirmed send."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_addr = _from_address()
    if not api_key:
        logger.info(
            "EMAIL (no RESEND_API_KEY, skipping): %s to=%s subject=%r",
            log_tag, to_email, subject,
        )
        return False
    payload = {
        "from": from_addr,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "text": text_body or _html_to_text(html_body),
    }
    try:
        response = requests.post(
            RESEND_ENDPOINT,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    except Exception:
        logger.warning("Resend POST failed for %s", log_tag, exc_info=True)
        return False
    if response.status_code >= 300:
        logger.warning(
            "Resend non-2xx for %s: HTTP %d body=%s",
            log_tag, response.status_code, response.text[:200],
        )
        return False
    try:
        resend_id = (response.json() or {}).get("id")
    except Exception:
        resend_id = None
    logger.info(
        "Email sent: %s to=%s (resend id=%s)", log_tag, to_email, resend_id
    )
    return True


# ---------------------------------------------------------------------------
# Wallet email senders. Signatures take typed kwargs plus **_extra so callers
# (the wallet, the Stripe webhook, the funnel module) can pass any forward
# compatible extras without TypeError. Existing call sites use kwargs only,
# so this is fully backward compatible with the stubs they previously called.
# ---------------------------------------------------------------------------


def send_signup_credit_email(
    *,
    user_id: str,
    **_extra: Any,
) -> bool:
    """Welcome plus signup credit confirmation.

    Trigger: first time a ``user_wallets`` row is created. Caller dedupes.
    """
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_signup_credit_email: no email for user %s; skipping", user_id
        )
        return False
    credit_usd = _money(
        os.environ.get("WALLET_SIGNUP_CREDIT_USD", "5")
    )
    base_url = _base_url()
    subject = (
        f"${credit_usd} in compute credit added to your Ranomics tools account"
    )
    html = _render_template(
        "send_signup_credit.html",
        base_url=base_url,
        credit_usd=credit_usd,
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"signup_credit user={user_id}",
    )


def send_topup_confirmation_email(
    *,
    user_id: str,
    amount_usd,
    new_balance_usd=None,
    **_extra: Any,
) -> bool:
    """Manual top up confirmation.

    Trigger: ``checkout.session.completed`` webhook for ``kind=topup``.
    """
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_topup_confirmation_email: no email for user %s", user_id
        )
        return False
    amt = _money(amount_usd)
    bal = _money(new_balance_usd if new_balance_usd is not None else amount_usd)
    base_url = _base_url()
    subject = f"${amt} added to your Ranomics tools wallet"
    html = _render_template(
        "send_topup_confirmation.html",
        base_url=base_url,
        amount_usd=amt,
        new_balance_usd=bal,
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"topup_confirmation user={user_id}",
    )


def send_auto_reload_charged_email(
    *,
    user_id: str,
    amount_usd,
    new_balance_usd=None,
    **_extra: Any,
) -> bool:
    """Auto reload succeeded confirmation.

    Trigger: ``payment_intent.succeeded`` webhook for ``metadata.kind=auto_reload``.
    """
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_auto_reload_charged_email: no email for user %s", user_id
        )
        return False
    amt = _money(amount_usd)
    bal = _money(new_balance_usd if new_balance_usd is not None else amount_usd)
    base_url = _base_url()
    subject = f"Auto reload added ${amt} to your Ranomics tools wallet"
    html = _render_template(
        "send_auto_reload_charged.html",
        base_url=base_url,
        amount_usd=amt,
        new_balance_usd=bal,
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"auto_reload_charged user={user_id}",
    )


_AUTO_RELOAD_REASON_LABELS = {
    "no_payment_method":    "no saved card on file",
    "no_amount_configured": "auto reload amount not set",
    "card_declined":        "the card was declined",
    "expired_card":         "the saved card is expired",
    "insufficient_funds":   "insufficient funds on the saved card",
}


def send_auto_reload_failed_email(
    *,
    user_id: str,
    reason: str = "",
    **_extra: Any,
) -> bool:
    """Auto reload PaymentIntent declined or no saved card.

    Trigger: ``payment_intent.payment_failed`` with ``kind=auto_reload``, OR
    ``_maybe_auto_reload`` finds no payment method.
    """
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_auto_reload_failed_email: no email for user %s", user_id
        )
        return False
    base_url = _base_url()
    reason_label = _AUTO_RELOAD_REASON_LABELS.get(
        reason, reason or "unknown reason"
    )
    subject = (
        "Auto reload could not complete on your Ranomics tools account"
    )
    html = _render_template(
        "send_auto_reload_failed.html",
        base_url=base_url,
        reason=reason_label,
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"auto_reload_failed user={user_id}",
    )


def send_auto_reload_rate_limited_email(
    *,
    user_id: str,
    **_extra: Any,
) -> bool:
    """Auto reload skipped due to 24h count rate limit."""
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_auto_reload_rate_limited_email: no email for user %s",
            user_id,
        )
        return False
    base_url = _base_url()
    subject = "Auto reload skipped on your Ranomics tools account"
    html = _render_template(
        "send_auto_reload_rate_limited.html",
        base_url=base_url,
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"auto_reload_rate_limited user={user_id}",
    )


def send_auto_reload_monthly_cap_email(
    *,
    user_id: str,
    total_usd,
    cap_usd,
    **_extra: Any,
) -> bool:
    """Auto reload paused: monthly cap reached."""
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_auto_reload_monthly_cap_email: no email for user %s",
            user_id,
        )
        return False
    base_url = _base_url()
    subject = "Auto reload paused for this month on your Ranomics tools account"
    html = _render_template(
        "send_auto_reload_monthly_cap.html",
        base_url=base_url,
        total_usd=_money(total_usd),
        cap_usd=_money(cap_usd),
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"auto_reload_monthly_cap user={user_id}",
    )


def send_low_balance_email(
    *,
    user_id: str,
    balance_usd,
    **_extra: Any,
) -> bool:
    """Balance dropped below low balance threshold.

    Trigger: after any ``charge`` debit leaves ``balance_usd < $5``.
    Caller throttles to one per 24 hours per user.
    """
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_low_balance_email: no email for user %s", user_id
        )
        return False
    base_url = _base_url()
    subject = "Your Ranomics tools wallet balance is low"
    html = _render_template(
        "send_low_balance.html",
        base_url=base_url,
        balance_usd=_money(balance_usd),
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"low_balance user={user_id}",
    )


def send_job_capped_email(
    *,
    user_id: str,
    tool_slug: str = "",
    attempted_usd=None,
    cap_usd=None,
    **_extra: Any,
) -> bool:
    """Submission blocked by per tool hard cap.

    Trigger: ``wallet_preflight`` returns ``job_exceeds_per_tool_cap`` or
    ``job_exceeds_self_serve_ceiling``.
    """
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_job_capped_email: no email for user %s", user_id
        )
        return False
    label = _label_for_tool(tool_slug)
    base_url = _base_url()
    contact_url = (
        "https://ranomics.com/ranomics-contact?service=binder-pilot"
    )
    subject = (
        f"Your {label} run was blocked by the per job spend cap"
    )
    html = _render_template(
        "send_job_capped.html",
        base_url=base_url,
        tool_label=label,
        attempted_usd=_money(attempted_usd),
        cap_usd=_money(cap_usd),
        contact_url=contact_url,
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"job_capped user={user_id} tool={tool_slug}",
    )


def send_overrun_warning_email(
    *,
    user_id: str,
    tool_slug: str = "",
    attempted_usd=None,
    cap_usd=None,
    **_extra: Any,
) -> bool:
    """Mid run soft warning: cumulative cost exceeded 1.5x the estimate.

    Trigger: ``mid_run_monitor_check`` in ``shared/jobs.py`` once per
    job. The job keeps running but the user gets a heads up so a
    runaway loop is visible before the hard kill threshold trips.
    """
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_overrun_warning_email: no email for user %s", user_id
        )
        return False
    label = _label_for_tool(tool_slug)
    base_url = _base_url()
    subject = (
        f"Your {label} run is running above estimate on Ranomics tools"
    )
    html = _render_template(
        "send_overrun_warning.html",
        base_url=base_url,
        tool_label=label,
        attempted_usd=_money(attempted_usd),
        cap_usd=_money(cap_usd),
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"overrun_warning user={user_id} tool={tool_slug}",
    )


def send_overrun_kill_email(
    *,
    user_id: str,
    tool_slug: str = "",
    attempted_usd=None,
    cap_usd=None,
    **_extra: Any,
) -> bool:
    """Mid run safety kill: cumulative cost exceeded 2x estimate plus cap.

    Trigger: ``mid_run_monitor_check`` decides to abort. Sent after the
    Modal cancel is issued. The wallet hold is settled against the cap
    and the user is notified so they can decide whether to retry with
    different parameters.
    """
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_overrun_kill_email: no email for user %s", user_id
        )
        return False
    label = _label_for_tool(tool_slug)
    base_url = _base_url()
    subject = (
        f"Your {label} run was stopped by the safety kill on Ranomics tools"
    )
    html = _render_template(
        "send_overrun_kill.html",
        base_url=base_url,
        tool_label=label,
        attempted_usd=_money(attempted_usd),
        cap_usd=_money(cap_usd),
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"overrun_kill user={user_id} tool={tool_slug}",
    )


def send_daily_cap_email(
    *,
    user_id: str,
    cap_usd,
    **_extra: Any,
) -> bool:
    """Daily spend cap reached.

    Trigger: ``wallet_preflight`` returns ``daily_cap_reached``. Caller
    throttles to one per day per user.
    """
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_daily_cap_email: no email for user %s", user_id
        )
        return False
    base_url = _base_url()
    default_cap = _money(
        os.environ.get("WALLET_DEFAULT_DAILY_CAP_USD", "200")
    )
    subject = "You hit your daily spend cap on Ranomics tools"
    html = _render_template(
        "send_daily_cap.html",
        base_url=base_url,
        cap_usd=_money(cap_usd),
        default_cap=default_cap,
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"daily_cap user={user_id}",
    )


def send_pilot_intro_email(
    *,
    user_id: str,
    spent_30d_usd,
    **_extra: Any,
) -> bool:
    """Funnel: Binder Pilot intro.

    Trigger: 30 day spend crosses $1,000 (one shot, dedup via funnel_alerts).
    """
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_pilot_intro_email: no email for user %s", user_id
        )
        return False
    base_url = _base_url()
    subject = (
        "You are doing real work on Ranomics tools. "
        "Have you considered a Binder Pilot?"
    )
    html = _render_template(
        "send_pilot_intro.html",
        base_url=base_url,
        spent_30d_usd=_money(spent_30d_usd),
        pilot_url="https://ranomics.com/binder-pilot",
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"pilot_intro user={user_id}",
    )


def send_wallet_frozen_email(
    *,
    user_id: str,
    dispute_id: str = "",
    **_extra: Any,
) -> bool:
    """Wallet frozen pending chargeback dispute review."""
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_wallet_frozen_email: no email for user %s", user_id
        )
        return False
    base_url = _base_url()
    subject = (
        "Your Ranomics tools wallet has been frozen pending dispute review"
    )
    html = _render_template(
        "send_wallet_frozen.html",
        base_url=base_url,
        dispute_id=dispute_id or "unknown",
        support_email=_support_email(),
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"wallet_frozen user={user_id} dispute={dispute_id}",
    )


# ---------------------------------------------------------------------------
# Internal Slack alerters
# ---------------------------------------------------------------------------
# These do not have HTML templates because they post to Slack via incoming
# webhook URLs. If the relevant env var is unset, the alerter logs the payload
# and returns False without raising.


def _post_slack(
    *,
    webhook_url: str,
    payload: dict,
    log_tag: str,
) -> bool:
    """POST a Slack incoming-webhook payload; return True on confirmed delivery.

    On missing webhook URL, logs and returns False. Never raises.
    """
    if not webhook_url:
        logger.info(
            "SLACK (no webhook URL, skipping): %s payload=%r", log_tag, payload
        )
        return False
    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
    except Exception:
        logger.warning("Slack POST failed for %s", log_tag, exc_info=True)
        return False
    if response.status_code >= 300:
        logger.warning(
            "Slack non-2xx for %s: HTTP %d body=%s",
            log_tag, response.status_code, response.text[:200],
        )
        return False
    logger.info("Slack delivered: %s", log_tag)
    return True


def _sales_slack_webhook() -> str:
    """Resolve the #sales-leads webhook URL.

    Honours both ``SLACK_SALES_WEBHOOK_URL`` (per dispatch prompt) and
    ``WALLET_FUNNEL_ALERT_SLACK_WEBHOOK_URL`` (per the plan's Railway env
    table). Either works; the dispatch-prompt name takes precedence when
    both are set.
    """
    return (
        os.environ.get("SLACK_SALES_WEBHOOK_URL", "").strip()
        or os.environ.get("WALLET_FUNNEL_ALERT_SLACK_WEBHOOK_URL", "").strip()
    )


def _ops_slack_webhook() -> str:
    """Resolve the #ops webhook URL."""
    return os.environ.get("SLACK_OPS_WEBHOOK_URL", "").strip()


def alert_sales_slack(
    *,
    user_id: str,
    spent_30d_usd,
    **_extra: Any,
) -> bool:
    """Internal Slack alert: pilot qualified lead (30 day spend >= $5000)."""
    email = _resolve_user_email(user_id) or "unknown"
    spent = _money(spent_30d_usd)
    text = (
        ":fire: Pilot-qualified lead\n"
        f"User: {email} ({user_id})\n"
        f"30 day spend: ${spent}\n"
        f"Internal link: {_base_url()}/admin/users/{user_id}\n"
        "Outreach window: within 48h"
    )
    return _post_slack(
        webhook_url=_sales_slack_webhook(),
        payload={"text": text},
        log_tag=f"sales_slack user={user_id}",
    )


def alert_sales_slack_high(
    *,
    user_id: str,
    spent_30d_usd,
    **_extra: Any,
) -> bool:
    """Internal Slack alert: high value spend (30 day spend >= $10000)."""
    email = _resolve_user_email(user_id) or "unknown"
    spent = _money(spent_30d_usd)
    text = (
        ":rotating_light: High value spend\n"
        f"User: {email} ({user_id})\n"
        f"30 day spend: ${spent}\n"
        f"Internal link: {_base_url()}/admin/users/{user_id}\n"
        "Outreach window: within 24h"
    )
    return _post_slack(
        webhook_url=_sales_slack_webhook(),
        payload={"text": text},
        log_tag=f"sales_slack_high user={user_id}",
    )


def alert_ops_slack(
    *,
    event: str = "",
    user_id: str = "",
    dispute_id: str = "",
    **_extra: Any,
) -> bool:
    """Internal ops Slack alert (wallet freeze, reconciliation drift, etc.)."""
    parts = [f":warning: ops event: {event or 'unspecified'}"]
    if user_id:
        parts.append(f"user: {user_id}")
    if dispute_id:
        parts.append(f"dispute: {dispute_id}")
    for k, v in (_extra or {}).items():
        parts.append(f"{k}: {v}")
    text = "\n".join(parts)
    return _post_slack(
        webhook_url=_ops_slack_webhook(),
        payload={"text": text},
        log_tag=f"ops_slack event={event} user={user_id}",
    )
