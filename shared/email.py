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
from typing import Optional

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
    is_success = job.status == "succeeded"
    subject = (
        f"Your {_tool_label(job.tool)} run finished"
        if is_success
        else f"Your {_tool_label(job.tool)} run failed"
    )
    html_body = _render_html(job=job, job_url=job_url, success=is_success)
    text_body = _render_text(job=job, job_url=job_url, success=is_success)

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


def _render_html(*, job, job_url: str, success: bool) -> str:  # noqa: ANN001
    """Plain HTML — no template engine to keep this email worker-portable."""
    summary = _result_summary(job, success)
    cta = (
        '<a href="' + job_url + '" '
        'style="display:inline-block;padding:12px 22px;background:#1f9d55;'
        'color:#fff;text-decoration:none;border-radius:6px;font-weight:600;">'
        "View results"
        "</a>"
    )
    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                color:#1a1a1a;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin-top:0;">Your {_tool_label(job.tool)} run is ready</h2>
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


def _render_text(*, job, job_url: str, success: bool) -> str:  # noqa: ANN001
    summary = _result_summary(job, success)
    return (
        f"Your {_tool_label(job.tool)} run is ready.\n\n"
        f"{summary}\n\n"
        f"View results: {job_url}\n\n"
        f"Job {job.id} · preset {job.preset} · "
        f"{job.credits_cost} credits · submitted {(job.created_at or '')[:19]}\n\n"
        "Ranomics Tools — tools.ranomics.com"
    )


def _result_summary(job, success: bool) -> str:  # noqa: ANN001
    if not success:
        err = job.error or {}
        if isinstance(err, dict):
            detail = err.get("detail") or err.get("message") or "see job page for details"
        else:
            detail = str(err)
        return f"The run did not complete: {detail}"
    result = job.result or {}
    cands = result.get("candidates", []) if isinstance(result, dict) else []
    n = len(cands)
    if n == 0:
        return (
            "The pipeline finished but produced no passing candidates. This "
            "can happen for difficult targets — see the job page for the full "
            "error taxonomy and try expanding binder length or hotspot list."
        )
    return (
        f"{n} candidate{'s' if n != 1 else ''} returned with real scores and "
        "downloadable PDBs."
    )
