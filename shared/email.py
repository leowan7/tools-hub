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
import threading
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
    subject = f"Your {tool} run is done"

    ctx = _job_complete_template_context(
        job=job, base_url=base_url, job_url=job_url, tone=tone, tool=tool,
    )
    try:
        html_body = _render_template("job_complete.html", **ctx)
        text_body = _render_template("job_complete.txt", **ctx)
    except Exception:
        logger.warning(
            "job_complete template render failed for job %s; "
            "falling back to inline body",
            getattr(job, "id", "?"),
            exc_info=True,
        )
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


def _job_complete_template_context(
    *, job, base_url: str, job_url: str, tone: str, tool: str,  # noqa: ANN001
) -> dict:
    """Build the variable dict for ``templates/email/job_complete.{html,txt}``.

    Extracts top-candidate score plus an interpretation from the
    score-legends table (C7) via ``score_legends.email_caption`` — one line
    about the metric, plus that metric's caveat when THIS job's target names
    more than one chain — and resolves the natural next-step tool
    via the C3 SOURCE_TOOLS / DESTINATION_TOOLS mapping in shared.refold.
    Robust against partial result payloads: every optional field falls
    back to "" so the templates render cleanly with no missing-data
    warning text.
    """
    headline = {
        "success": f"Your {tool} run is ready",
        "empty":   f"Your {tool} run finished with no candidates",
        "failed":  f"Your {tool} run failed",
    }[tone]
    top_score_label, top_score_value, top_score_caption, top_pdb_key = (
        _top_candidate_summary(job=job, tone=tone)
    )
    next_step_url, next_step_label = _next_step_for_job(
        job=job, base_url=base_url, tone=tone,
    )
    return {
        "base_url":          base_url,
        "tool_label":        tool,
        "headline":          headline,
        "summary":           _result_summary(job, tone=tone),
        "cost_line":         _cost_breakdown_line(job, tone=tone),
        "job_id":            getattr(job, "id", ""),
        "job_preset":        getattr(job, "preset", "") or "",
        "job_created":       (getattr(job, "created_at", "") or "")[:19],
        "job_url":           job_url,
        "tone":              tone,
        "top_score_label":   top_score_label,
        "top_score_value":   top_score_value,
        "top_score_caption": top_score_caption,
        "top_pdb_key":       top_pdb_key,
        "next_step_url":     next_step_url,
        "next_step_label":   next_step_label,
    }


def _top_candidate_summary(*, job, tone: str) -> tuple[str, str, str, str]:  # noqa: ANN001
    """Pull (label, value, 1-line caption, pdb_key) for the top candidate.

    Returns four empty strings when the job has no candidate scores to
    surface (sequence-design tools, structure-prediction tools, failed
    runs). The caption comes from shared.score_legends; when no legend
    is registered for the chosen column the caption falls back to "".
    """
    if tone != "success":
        return ("", "", "", "")
    result = getattr(job, "result", None) or {}
    if not isinstance(result, dict):
        return ("", "", "", "")
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ("", "", "", "")
    top = candidates[0]
    if not isinstance(top, dict):
        return ("", "", "", "")
    scores = top.get("scores")
    if not isinstance(scores, dict) or not scores:
        # Some adapters inline the score at the candidate root instead of
        # under .scores. Fall back to a small allowlist of known keys.
        flat = {
            k: top.get(k)
            for k in ("iptm", "ipTM", "plddt", "pLDDT", "ptm", "i_pae")
            if isinstance(top.get(k), (int, float))
        }
        scores = flat or {}
    if not scores:
        return ("", "", "", "")

    try:
        from shared.score_legends import (  # noqa: PLC0415
            score_legends_for,
        )
        legends = score_legends_for(getattr(job, "tool", "") or "")
    except Exception:
        legends = {}

    # Prefer columns with a registered legend so the caption is
    # meaningful. Then fall back to the first numeric score we see.
    chosen_col = None
    for col in scores:
        if col in legends and isinstance(scores.get(col), (int, float)):
            chosen_col = col
            break
    if chosen_col is None:
        for col, val in scores.items():
            if isinstance(val, (int, float)):
                chosen_col = col
                break
    if chosen_col is None:
        return ("", "", "", "")

    value = scores[chosen_col]
    if isinstance(value, float):
        value_str = f"{value:.3f}"
    else:
        value_str = str(value)
    caption = ""
    legend = legends.get(chosen_col)
    if isinstance(legend, dict):
        # ``email_caption``, not ``legend["explanation"]`` and not
        # ``legend_text``. A legend's optional ``caveat`` says what an OLD
        # STORED result may hold, and THIS MAIL IS SENT ABOUT STORED RESULTS:
        # shared/jobs.complete_job is called by timeout_stuck_job (recovering
        # a result out of Storage), by the inline poll in
        # blueprints/jobs.job_status, and by scripts/finalize_stuck_job.py —
        # each of them arbitrarily long after the run. An earlier comment here
        # asserted the opposite ("its number always comes from the container
        # running now"), and all three paths were then driven with the
        # transport captured: each mailed a pre-deploy BoltzGen score
        # described as "the binder-to-target interface", uncaveated.
        #
        # ``email_caption`` appends the caveat only when THIS job's target
        # names more than one chain — the condition the caveat states, and one
        # the legend cannot evaluate because it never sees a job. The chain
        # comes from ``job.inputs`` (the value the run was SUBMITTED with),
        # not from a target row, which is editable and is overridden per
        # launch (templates/targets/launch.html: "Overrides the target default
        # for these runs only").
        from shared.score_legends import email_caption  # noqa: PLC0415

        inputs = getattr(job, "inputs", None)
        target_chain = inputs.get("target_chain") if isinstance(inputs, dict) else None
        caption = email_caption(legend, target_chain)
    pdb_key = top.get("pdb_key") or ""
    if not isinstance(pdb_key, str):
        pdb_key = str(pdb_key)
    return (str(chosen_col), value_str, caption, pdb_key)


def _next_step_for_job(
    *, job, base_url: str, tone: str,  # noqa: ANN001
) -> tuple[str, str]:
    """Resolve the natural next-step tool for a binder-design job.

    Uses the C3 SOURCE_TOOLS / DESTINATION_TOOLS mapping in
    shared.refold: tools that produce binder sequences get ColabFold as
    the no-MSA orthogonal validator. Returns ("", "") when no handoff
    applies (failed/empty result, or a tool not in SOURCE_TOOLS).
    """
    if tone != "success":
        return ("", "")
    try:
        from shared.refold import SOURCE_TOOLS  # noqa: PLC0415
    except Exception:
        return ("", "")
    tool_slug = getattr(job, "tool", "") or ""
    if tool_slug not in SOURCE_TOOLS:
        return ("", "")
    dest_slug = "colabfold"
    # _label_for_tool is defined further down in the wallet-senders
    # block; it covers colabfold/esmfold and falls through to the slug
    # if a future destination is added. Forward reference is fine since
    # this function is only invoked at email-send time.
    return f"{base_url}/tools/{dest_slug}", _label_for_tool(dest_slug)


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
    cost_line = _cost_breakdown_line(job, tone=tone)
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
    cost_block = (
        f'<p style="font-size:13px;color:#666;margin:8px 0 0 0;">{cost_line}</p>'
        if cost_line else ""
    )
    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                color:#1a1a1a;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin-top:0;">{headline}</h2>
      <p>{summary}</p>
      {cost_block}
      <p style="margin:24px 0;">{cta}</p>
      <hr style="border:none;border-top:1px solid #e5e5e5;margin:24px 0;">
      <p style="font-size:13px;color:#666;">
        Job <code>{job.id}</code> · preset <code>{job.preset}</code> ·
        submitted {(job.created_at or '')[:19]}
      </p>
      <p style="font-size:12px;color:#999;">
        Ranomics Tools — <a href="https://tools.ranomics.com" style="color:#999;">tools.ranomics.com</a>
      </p>
    </div>
    """.strip()


def _render_text(*, job, job_url: str, tone: str) -> str:  # noqa: ANN001
    summary = _result_summary(job, tone=tone)
    cost_line = _cost_breakdown_line(job, tone=tone)
    tool = _tool_label(job.tool)
    headline = {
        "success": f"Your {tool} run is ready.",
        "empty":   f"Your {tool} run finished — no candidates.",
        "failed":  f"Your {tool} run failed.",
    }[tone]
    link_label = "View results" if tone == "success" else "View job details"
    cost_block = f"{cost_line}\n\n" if cost_line else ""
    return (
        f"{headline}\n\n"
        f"{summary}\n\n"
        f"{cost_block}"
        f"{link_label}: {job_url}\n\n"
        f"Job {job.id} · preset {job.preset} · "
        f"submitted {(job.created_at or '')[:19]}\n\n"
        "Ranomics Tools — tools.ranomics.com"
    )


def _cost_breakdown_line(job, *, tone: str) -> str:  # noqa: ANN001
    """One-line cost summary for the completion email. Empty if no wallet ctx.

    Returns strings like ``"Estimated $0.45, charged $0.52 (95 GPU-sec on L4)."``.
    The "charged" figure is capped at the per-tool hard cap so absorbed
    variance does not surface here — the user only sees what their wallet
    actually paid.
    """
    if tone == "failed":
        # _result_summary already says "wallet was not charged" when
        # applicable; suppress to avoid contradiction.
        return ""
    wallet_ctx = (job.inputs or {}).get("_wallet") or {}
    if not isinstance(wallet_ctx, dict):
        return ""
    if not wallet_ctx.get("hold_tx_id"):
        return ""
    gpu_seconds = job.gpu_seconds_used or 0
    if gpu_seconds <= 0:
        return ""
    gpu_class = wallet_ctx.get("gpu_class") or "GPU"
    estimate_raw = wallet_ctx.get("estimate_usd")
    try:
        from decimal import Decimal  # noqa: PLC0415

        from shared.wallet import compute_charge_usd  # noqa: PLC0415
        from shared.wallet_estimates import compute_hard_cap  # noqa: PLC0415
    except Exception:
        return ""
    params = {
        k: v
        for k, v in (job.inputs or {}).items()
        if isinstance(k, str) and not k.startswith("_")
    }
    actual = compute_charge_usd(gpu_seconds, gpu_class)
    try:
        hard_cap = compute_hard_cap(job.tool, params)
        if actual > hard_cap:
            actual = hard_cap
    except Exception:
        pass
    bits = []
    if estimate_raw:
        try:
            bits.append(f"Estimated ${float(Decimal(str(estimate_raw))):.2f}")
        except Exception:
            pass
    bits.append(f"charged ${float(actual):.2f}")
    return f"{', '.join(bits)} ({int(gpu_seconds)} GPU-sec on {gpu_class})."


def _handoff_source_link(base_url: str, campaign) -> Optional[tuple]:  # noqa: ANN001
    """``(label, url)`` of the results page this shortlist was picked on.

    Branches on ``submission_source``, not on whichever id happens to be set.
    The ``lab_campaigns_submission_source_shape`` CHECK requires each source to
    carry ITS OWN parent (a 'target' row must have source_target_id, a
    'campaign' row source_campaign_id, a 'web' row source_job_id) but it does
    NOT forbid the others being non-null, so "whichever id is set" is not a
    well-defined rule at the database level even though the writers in this
    codebase only ever populate one.

    Returns None for an 'api' row, which has no upstream page in this product.
    Every attribute is read through ``getattr``: this function is reached from
    a best-effort email path that must not raise on a partially built row.
    """
    source = str(getattr(campaign, "submission_source", "") or "web")
    if source == "target":
        tid = getattr(campaign, "source_target_id", None)
        return ("Target", f"{base_url}/targets/{tid}") if tid else None
    if source == "campaign":
        cid = getattr(campaign, "source_campaign_id", None)
        return ("Run", f"{base_url}/campaigns/{cid}") if cid else None
    if source == "web":
        jid = getattr(campaign, "source_job_id", None)
        return ("Source job", f"{base_url}/jobs/{jid}") if jid else None
    return None


def _source_tools_line(source_tools) -> str:  # noqa: ANN001
    """``"rfdiffusion (4), pxdesign (3)"`` from ``{slug: design_count}``.

    Ordered by count descending, then slug, so the same shortlist always
    renders the same string. Empty when there is nothing to say, which the
    callers below test rather than printing a bare label.
    """
    if not source_tools:
        return ""
    items = [(str(k), int(v)) for k, v in dict(source_tools).items() if v]
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{slug} ({n})" for slug, n in items)


# Customer vocabulary for the assay a lab_campaigns row records. The keys are
# the three values the assay_type CHECK in migration 0011 accepts, which is
# also shared.campaigns.ASSAY_TYPES.
_ASSAY_CUSTOMER_LABELS = {
    "yeast_display": "yeast display",
    "mammalian_display": "mammalian display",
    "dms": "deep mutational scanning",
}


def _assay_customer_label(assay_type) -> str:  # noqa: ANN001
    """Lower-case, mid-sentence assay name for CUSTOMER copy, or ``""``.

    ``""`` is returned for anything outside ``_ASSAY_CUSTOMER_LABELS``, which
    the caller handles by dropping the adjective: "your scoping request"
    rather than a raw enum or the word None. That branch is unreachable from
    any row the database currently holds -- assay_type is NOT NULL and CHECKed
    to those three values (migration 0011) and every writer validates against
    ASSAY_TYPES before insert -- so it is cover for a future widening of that
    CHECK, not for today's data.

    Deliberately NOT the ``.replace('_', ' ').title()`` transform used for the
    staff Assay rows below: that renders 'dms' as "Dms", and title case is
    wrong for a phrase sitting mid-sentence after the word "your".

    Takes the value rather than the campaign, so the caller can read the
    attribute through ``getattr`` with a default and this helper cannot raise
    on a row that lacks it. That is narrower than the sender being None-safe:
    the sender still calls ``.title()`` on ``budget_band`` unguarded.
    """
    return _ASSAY_CUSTOMER_LABELS.get(str(assay_type or "").strip(), "")


def send_campaign_submitted_emails(
    *, campaign, user_email: str, source_tools=None, dropped: int = 0,  # noqa: ANN001
    truncated: int = 0,
) -> None:
    """Send user confirmation + internal staff notification on campaign submit.

    ``source_tools`` is an optional ``{tool_slug: design_count}`` map over the
    ACCEPTED shortlist. Supplied by the target branch, where the shortlist
    spans tools and the spread is the whole point; omitted by the campaign and
    single-job branches, which have exactly one tool by construction
    (``compute_campaigns.tool`` is NOT NULL) and would only print it back.

    ``dropped`` is how many DISTINCT designs the caller REJECTED before
    creating this campaign; ``truncated`` is how many REFS its per-request cap
    discarded before it looked at them at all. Both messages otherwise report
    the accepted count with nothing to compare it against, so a user who
    starred ten designs reads "7 candidates" as the number they chose, and ops
    reads it as the whole order (register item A-7). Zero for both is the
    overwhelmingly common case and prints nothing.

    THEY ARE TWO ARGUMENTS BECAUSE THEY COUNT DIFFERENT THINGS IN DIFFERENT
    UNITS. ``dropped`` is designs, deduped and decided one at a time by the
    write path; ``truncated`` is refs, because the tail past the cap is never
    parsed into pairs. Summing them would force one sentence to be wrong about
    half of what it counts, and would put a design count and a ref count under
    one noun.

    Earlier rounds justified the split as "opposite remedies" and gave the
    truncated half a retry instruction. That was wrong twice over at the time:
    the shortlist was never cleared, so a second send carried the identical
    refs, and the route is ``@idempotent()`` besides. The instruction is back,
    in one form only -- ``blueprints/lab_projects.py::campaign_detail`` now
    hands the browser the refs THIS request covered and the browser un-stars
    exactly those (register item A89), so what stays starred is the remainder.
    This body carries no list of what the request covers, so its sentence points
    at the page that does rather than standing alone; see the note at the
    ``truncated`` branch.

    WHAT ``dropped`` MAY CLAIM, and why it can be blunt. BOTH ref callers refuse
    the whole submission rather than reporting a shortfall they could not decide
    (a job read that never completed; on the target arm also a campaign id set
    that came back short), so every design counted here was refused by a check
    that ran to completion. That is what makes "rejected", rather than "we could
    not confirm", an honest word for it. This function does not enforce that --
    it is a property of the callers -- so a caller passing ``dropped`` without
    such a gate makes the sentence below false. ALL THREE CALLERS NOW HAVE ONE:
    A91 gave the legacy single-job arm the same shape, refusing the whole
    submission with ``?handoff=unverified`` when its parent ``read_job`` comes
    back UNAVAILABLE, decided above the loop that rejects any individual design.
    Before that it read through ``get_job``, which cannot tell an unreadable row
    from an absent one, so it could not have had the gate and correctly passed
    no ``dropped`` count. ``truncated`` never needed the gate; that arm omitted
    it because it never counted at all, which A91 fixed in the same change.

    WHAT THE COPY MAY CLAIM. Nothing here observes the Storage bucket, so no
    sentence below asserts that any PDB was written:
    ``stage_campaign_candidates`` silently skips a candidate that resolves to
    no bytes, and the caller swallows ``StorageError`` per source job. The
    verified fact is what the ROW names -- ``candidate_refs`` on the two ref
    arms, ``candidate_indices`` on the job arm, deduped at the write path on all
    three -- so the copy says the request "covers" N designs rather than that N
    "were sent". The index check behind that is CONDITIONAL rather than
    universal: every arm skips it when ``candidate_count`` answers ``None``
    (register item A107), which is a second reason "covers" is the strongest
    word available here.

    Best-effort: failures are logged but not raised to the caller.
    """
    from shared.auth import STAFF_EMAILS  # noqa: PLC0415

    base_url  = os.environ.get("PUBLIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    from_addr = os.environ.get("EMAIL_FROM", DEFAULT_FROM)
    api_key   = os.environ.get("RESEND_API_KEY", "").strip()

    # THE COUNTS RIDE THIS LINK, for the same reason they ride the redirect's:
    # `campaign_detail` reads them off the query string and has no other source
    # for them, so a link without them reaches a page that shows the design list
    # and says nothing about what did NOT arrive. The `truncated` sentence below
    # sends the reader to this URL, so a bare one would point at a page missing
    # the very fact it was sent to explain.
    #
    # NOT `submitted=1`. That flag is what makes the page hand the browser its
    # covered refs to un-star, and an email opened days later is not a submit:
    # the reader may have re-starred a covered design on purpose since. It also
    # carries the "Scoping request submitted" banner, which is a statement about
    # the last few seconds. Both belong to the redirect and to nothing else.
    campaign_url = f"{base_url}/lab-projects/{campaign.id}"
    _params = []
    if dropped:
        _params.append(f"dropped={int(dropped)}")
    if truncated:
        _params.append(f"truncated={int(truncated)}")
    if _params:
        campaign_url += "?" + "&".join(_params)
    # `&` is a raw ampersand in the plain-text body and an entity in the HREF.
    # An unescaped `&` in an attribute is what an HTML parser resolves against
    # its entity table, and the parameter names here begin with `d` and `t`, so
    # the risk is a client that is stricter than a browser rather than a live
    # break. Only this URL can carry a query, so only this one is escaped.
    campaign_href = campaign_url.replace("&", "&amp;")
    source_link = _handoff_source_link(base_url, campaign)
    tools_line = _source_tools_line(source_tools)

    # Built here rather than inline in the staff table below. An f-string
    # nested inside another f-string's replacement field only parses on Python
    # 3.12+ (PEP 701), and this repo's deployed interpreter is chosen by a
    # nixpkgs pin rather than by runtime.txt, so the version this file runs on
    # is not something the file can assert.
    _td = 'style="color:#666;padding:4px 12px 4px 0;"'
    tools_row = (
        f"<tr><td {_td}>Designs from</td><td>{tools_line}</td></tr>"
        if tools_line else ""
    )
    source_row = ""
    if source_link:
        source_row = (
            f"<tr><td {_td}>{source_link[0]}</td>"
            f'<td><a href="{source_link[1]}">{source_link[1]}</a></td></tr>'
        )

    # Shortlist size. A 'web' row carries candidate_indices; a 'campaign' or
    # 'target' row leaves that empty and keeps the shortlist in candidate_refs
    # (migration 0037), so reading candidate_indices alone reported "0
    # candidates" on BOTH the customer confirmation and the staff notify for
    # every campaign handoff.
    n_candidates = len(campaign.candidate_indices) or len(
        campaign.candidate_refs or []
    )

    # Built out here, not inline, for the PEP 701 reason given above: an
    # f-string nested in another f-string's replacement field does not parse on
    # every interpreter this can deploy onto.
    _sentences: list = []
    dropped_row = ""
    truncated_row = ""
    if dropped:
        _plural = "s" if dropped != 1 else ""
        # "matched to a DESIGN in the results", not "matched to the results":
        # the write paths reject on four grounds, and one of them is an index
        # past the end of an otherwise-legitimate run of this very parent.
        #
        # NAMES NO PARENT KIND. Both ref callers pass `dropped` -- the target arm
        # against "a design on this target", the campaign arm against "a child of
        # this compute campaign" -- and this function takes no parameter saying
        # which. A branch on `campaign.submission_source` would need a field this
        # module reads only through `getattr` (see _handoff_source_link), and its
        # else-arm would go stale on the next source added.
        _sentences.append(
            f"{dropped} starred design{_plural} could not be matched to a "
            f"design in the results this shortlist was built from and "
            f"{'were' if dropped != 1 else 'was'} left out. This request covers "
            f"{n_candidates} design{'s' if n_candidates != 1 else ''}."
        )
        dropped_row = (
            f"<tr><td {_td}>Not included</td><td>{dropped} "
            f"starred design{_plural} rejected</td></tr>"
        )
    if truncated:
        _tplural = "s" if truncated != 1 else ""
        _twas = "were" if truncated != 1 else "was"
        # "UP TO", because this number counts REFS and the sentence counts
        # DESIGNS. The tail past the cap is never parsed into (job, index)
        # pairs, so a repeat hiding in it cannot be subtracted and the figure is
        # an upper bound on the designs actually missing. The staff row below
        # keeps the exact unit ("refs"), which is why the two read differently.
        #
        # THE RETRY ADVICE IS ROUTED THROUGH THE CAMPAIGN PAGE, NOT GIVEN HERE.
        # This once said "star them again on the target page and send a second
        # request", and following it created a DUPLICATE PAID REQUEST: nothing
        # cleared the shortlist, the modal serialises it in stored order, so the
        # second POST carried the identical first 500 refs and the designs over
        # the limit were still over it. `campaign_detail` now hands the browser
        # the refs that request COVERED and the browser un-stars exactly those
        # (register item A89), so what is left starred is the remainder -- but
        # that happens in a browser nothing here can hear back from, and the
        # page's safeguard against a silent failure is that it prints the
        # designs the request already covers directly under the advice. This
        # email has no such list, so the instruction must not lead: it is
        # PREFIXED by "check your campaign page for what this request covers",
        # which sends the reader to the surface that does list them before it
        # tells them to do anything. `campaign_url` above carries the counts
        # that page needs to render the same disclosure this sentence makes.
        #
        # AND IT ASSERTS NOTHING ABOUT WHAT THAT PAGE RENDERS. "Check your
        # campaign page for what this request covers" is an instruction; "your
        # campaign page lists the designs" would be a claim about a panel
        # rendered under conditions this module does not evaluate and could only
        # duplicate. The server-side guarantee that makes the instruction sound
        # is narrower and is pinned in tests/test_lab_project_confirmation.py:
        # `_ordered_shortlist` never answers None for a row carrying a non-empty
        # shortlist column, and no caller passes `truncated` for a row that
        # carries an empty one.
        #
        # WHERE THE TWO OVERLAP, they must not drift: both tell the reader that
        # this request covers a fixed set, that the rest must be starred, and
        # that a second request sends them. What the page adds on top is the
        # designs THEMSELVES, listed. It is not the count: the `dropped`
        # sentence above prints `n_candidates` in this same body whenever that
        # branch fires, so the number is on both surfaces. The staff copy keeps
        # the exact ref count, so ops can still pick the rest up.
        #
        # NO DESIGN COUNT IN THIS SENTENCE. The page counts DISTINCT designs and
        # `n_candidates` above is the STORED length; they agree on every row
        # either ref arm writes, because those arms dedupe before persisting.
        # This is NOT a drift protection -- the `dropped` sentence a few lines
        # up already prints `n_candidates` in this same body, so the number is
        # on both surfaces whenever both branches fire. It is here because the
        # sentence has no list to count against: the page states its own count
        # next to its own designs, and a bare figure here would be one more
        # number the reader cannot reconcile.
        _sentences.append(
            f"Up to {truncated} further starred design{_tplural} {_twas} over "
            f"the per-request limit and {_twas} not read. Check your campaign "
            f"page for what this request covers, then star the rest and send a "
            f"second request; the Ranomics team has the shortfall as well."
        )
        truncated_row = (
            f"<tr><td {_td}>Over the limit</td><td>{truncated} "
            f"starred ref{_tplural} past the per-request cap</td></tr>"
        )
    dropped_note_html = "".join(
        '<p style="color:#8a5a00;background:#fff6e5;border-radius:6px;'
        f'padding:10px 12px;">{s}</p>'
        for s in _sentences
    )
    dropped_note_text = ("\n" + "\n".join(_sentences) + "\n") if _sentences else ""

    # Customer copy names the assay THIS row records. The word was hardcoded
    # to "yeast display", so a mammalian_display or dms submission was
    # confirmed back with an assay the customer did not pick (A93). The
    # trailing space is folded into the local so the unknown case degrades to
    # "your scoping request" without a second branch at the use site, and so
    # neither body needs an f-string nested in another f-string's replacement
    # field (the PEP 701 constraint noted above).
    _assay = _assay_customer_label(getattr(campaign, "assay_type", None))
    assay_phrase = f"{_assay} " if _assay else ""
    # Sentence-initial in the plain-text body, mid-sentence in the HTML, so the
    # capitalised form is built once here. `.capitalize()` is safe on every
    # value of the map because all three labels are already lower case.
    assay_text_lead = (
        f"{assay_phrase.capitalize()}scoping request received"
        if assay_phrase else "Scoping request received"
    )
    # Staff vocabulary, unchanged for all three values the CHECK permits
    # ('yeast_display' -> "Yeast Display", 'dms' -> "Dms"). Read through
    # getattr only so a row with no usable assay_type prints an em dash instead
    # of raising: this used to be `campaign.assay_type.replace(...)` inline, and
    # an AttributeError there fires ABOVE the only try block in this function
    # (which wraps just the HTTP post), so it escaped into the callers' except
    # blocks and lost the customer confirmation as well as this notification.
    # Only assay_type is guarded here; the sibling budget_band.title() calls
    # below are not, so this does not make the function None-safe.
    _assay_staff = str(getattr(campaign, "assay_type", "") or "").replace("_", " ").title()
    assay_staff_label = _assay_staff or "—"

    # User confirmation
    # Subject deliberately carries NO assay: it never claimed one, so there is
    # nothing here to correct. The assay is in the opening paragraph of the
    # HTML body -- the <h2> above that paragraph names none either -- and in
    # the first line of the plain-text one.
    user_subject = f"Scoping request received — {campaign.target_name}"
    user_html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                color:#1a1a1a;max-width:560px;margin:0 auto;padding:24px;">
      <h2 style="margin-top:0;">Scoping request received</h2>
      <p>We've received your {assay_phrase}scoping request for
         <strong>{campaign.target_name}</strong> ({n_candidates}
         candidate{'s' if n_candidates != 1 else ''}).</p>
      {dropped_note_html}
      <p>The Ranomics team will review feasibility against current lab capacity
         and follow up within <strong>2 business days</strong>.</p>
      <p style="margin:24px 0;">
        <a href="{campaign_href}"
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
    # The count is in the FIRST LINE, not only in the HTML lead. The shortfall
    # note below it names a figure and compares it against the request size, so
    # a text body that never states the request size left that comparison
    # pointing at nothing (the note previously read "Only the 7 above were
    # sent" in a body with no 7 anywhere above it).
    # The assay is an ADDITION to this half, not a correction: the plain-text
    # body never named one. It is here so the two formats of one email agree.
    user_text = (
        f"{assay_text_lead} for {campaign.target_name} "
        f"({n_candidates} candidate{'s' if n_candidates != 1 else ''}).\n"
        f"{dropped_note_text}\n"
        "The Ranomics team will review and follow up within 2 business days.\n\n"
        f"View campaign: {campaign_url}\n\n"
        "Ranomics Tools — tools.ranomics.com"
    )

    # Staff notification
    staff_subject = f"New campaign: {campaign.target_name} from {user_email}"
    admin_url = f"{base_url}/admin/lab-projects/{campaign.id}"
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
            <td>{assay_staff_label}</td></tr>
        <tr><td style="color:#666;padding:4px 12px 4px 0;">Candidates</td>
            <td>{n_candidates}</td></tr>
        {dropped_row}
        {truncated_row}
        {tools_row}
        <tr><td style="color:#666;padding:4px 12px 4px 0;">Budget</td>
            <td>{campaign.budget_band.title()}</td></tr>
        {source_row}
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

    staff_text = (
        f"New scoping request\n\n"
        f"Target: {campaign.target_name}\n"
        f"From: {user_email}\n"
        f"Assay: {assay_staff_label}\n"
        f"Candidates: {n_candidates}\n"
        + (f"Not included: {dropped} starred design(s) rejected\n"
           if dropped else "")
        + (f"Over the limit: {truncated} starred ref(s) past the "
           f"per-request cap\n" if truncated else "")
        + (f"Designs from: {tools_line}\n" if tools_line else "")
        + f"Budget: {campaign.budget_band.title()}\n"
        + (f"{source_link[0]}: {source_link[1]}\n" if source_link else "")
        + f"\nReview in admin: {admin_url}\n"
    )

    if not api_key:
        logger.info(
            "EMAIL (no key) campaign_submitted: user=%s target=%s id=%s",
            user_email, campaign.target_name, campaign.id,
        )
        return

    for to_addr, subject, html_body, text_body in [
        (user_email, user_subject, user_html, user_text),
        (list(STAFF_EMAILS), staff_subject, staff_html, staff_text),
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

    campaign_url = f"{base_url}/lab-projects/{campaign.id}"
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
    crossed warning threshold. Pre-emptive: warns before the hard 100%
    block so the user can wrap up in-flight design. Legacy Workspace
    holders only (the Workspace product is retired); the forward path is
    now a self-serve campaign funded from the wallet, not an XL upsell.

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
    campaigns_url = f"{base_url}/campaigns/new"
    wallet_cta = f"""
      <p style="margin:18px 0 0 0; font-size:14px;">
        Want to keep designing beyond this target? Run any tool self-serve,
        funded from your wallet, with unlimited design count. A large ask
        fans out into a campaign on any target.
      </p>
      <p style="margin:10px 0 0 0;">
        <a href="{campaigns_url}" style="color:#2B9E7E;">Start a campaign →</a>
      </p>
    """

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
      {wallet_cta}
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
        f"Keep designing self-serve, funded from your wallet. "
        f"Start a campaign: {base_url}/campaigns/new\n\n"
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


def send_reengagement_email(
    *,
    user_email: str,
    candidate,  # noqa: ANN001 — cron.reengagement.Candidate
    base_url: str,
) -> bool:
    """C6 — re-engagement email for a user with credits sitting unused.

    Reads the suggestion list off ``candidate.suggestions`` (built by
    the sweep) and the user's balance off ``candidate.balance_usd``.
    Returns True on confirmed send. Failures are logged but never raise.
    """
    subject = "You have credits sitting unused"
    suggestions = list(getattr(candidate, "suggestions", []) or [])
    context = {
        "base_url":    base_url,
        "balance_usd": _money(getattr(candidate, "balance_usd", 0) or 0, "down"),
        "suggestions": suggestions,
    }
    try:
        html_body = _render_template("reengagement.html", **context)
        text_body = _render_template("reengagement.txt", **context)
    except Exception:
        logger.warning(
            "reengagement template render failed for user %s",
            getattr(candidate, "user_id", "?"), exc_info=True,
        )
        return False
    return _send_simple(
        api_key=os.environ.get("RESEND_API_KEY", "").strip(),
        from_addr=_from_address(),
        to_email=user_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        log_tag=f"reengagement user={getattr(candidate, 'user_id', '?')}",
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
        # Failed jobs that consumed no GPU time get their wallet hold
        # released, billing the user nothing. Surface that reassurance in
        # the email body for jobs that actually carried a hold; free smoke
        # runs skip the message since no charge was ever possible.
        wallet_ctx = (job.inputs or {}).get("_wallet") or {}
        has_hold = isinstance(wallet_ctx, dict) and bool(wallet_ctx.get("hold_tx_id"))
        no_charge = (
            job.status == "failed"
            and not job.gpu_seconds_used
            and has_hold
        )
        if no_charge:
            return (
                f"The run did not complete; your wallet was not charged. "
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
    # PXDesign, RFdiffusion): 'candidates[]'. candidate_records also covers the
    # designs-only shape (boltz2, iggm), which reaches this fallthrough without
    # a root-level pdb_b64 and would otherwise report "0 candidates returned"
    # in an otherwise-successful completion email.
    from shared.jobs import candidate_records  # noqa: PLC0415
    cands = candidate_records(result)
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

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
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


_MONEY_DIRECTIONS = ("down", "up", "nearest")


def _money(amount, direction: str = "nearest") -> str:
    """Format any numeric type as a plain dollar amount string.

    ``direction`` picks the rounding, matching the server-side display helpers:
    ``"down"`` for a balance or a cap (``display_balance_usd``), ``"up"`` for a
    cost, a spend or a required top-up (``display_cost_usd``), ``"nearest"``
    (the default) for a figure that is neither, such as a credit granted or a
    fixed advertised amount. Outbound email was the fourth class of money
    surface found in this review and the first outside ``templates/``; if you
    add a figure here, pick its direction deliberately.

    The default is named ``"nearest"`` and not ``"exact"``: it rounds to
    NEAREST, so calling it exact would have made it sound like
    ``display_ledger_usd``, which is a different behaviour entirely.

    Unknown directions RAISE. A template that names a missing helper is a hard
    ``UndefinedError``, so the server side cannot fail silently; a bare string
    compare here would have let ``_money(bal, "DOWN")`` fall through to NEAREST
    and quietly reintroduce the defect this argument exists to remove.

    Returns the integer form when the value is whole (e.g. "5"), else two
    decimals (e.g. "12.50"). Used in template variables so the rendered email
    body says "$5" instead of "$5.00" for the signup credit case.
    """
    if direction not in _MONEY_DIRECTIONS:
        raise ValueError(
            f"unknown money direction {direction!r}; use one of "
            f"{_MONEY_DIRECTIONS}"
        )
    if amount is None:
        return "0"
    try:
        d = Decimal(str(amount))
    except Exception:
        return str(amount)
    if d == d.to_integral_value():
        return str(int(d))
    if direction == "down":
        # A balance rounds DOWN. `{d:.2f}` rounds to NEAREST, so a wallet
        # holding $24.4950 was told it had "$24.50".
        return str(d.quantize(Decimal("0.01"), rounding=ROUND_FLOOR))
    if direction == "up":
        # A cost or a required top-up rounds UP: never quote less than is
        # actually taken. Matches display_cost_usd.
        return str(d.quantize(Decimal("0.01"), rounding=ROUND_CEILING))
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
    return os.environ.get("SUPPORT_EMAIL", "info@ranomics.com").strip() or (
        "info@ranomics.com"
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
# Operator alert: new Platform API submission
# ---------------------------------------------------------------------------

# Operator-alert fan-out runs off the request thread, capped so a burst of
# Platform API submissions (or a stalled Resend) can never pile up unbounded
# threads — the same shed-when-full rule shared.events uses for analytics.
_OPERATOR_ALERT_INFLIGHT = threading.BoundedSemaphore(2)


def notify_operator_new_submission(
    *,
    experiment_id: Optional[str],
    name: Optional[str],
    experiment_type: Optional[str],
    target_name: Optional[str],
    sequence_count: int,
    submitter_user_id: Optional[str],
) -> None:
    """Fire-and-forget operator alert for a new Platform API submission.

    A customer submission via the MCP server / REST API
    (POST /api/v1/experiments) should never sit unseen. Emails
    OPERATOR_ALERT_EMAIL (default leo@ranomics.com) in a daemon thread so the
    API response is never delayed or failed by email latency; bounded by
    ``_post_resend``'s 10s timeout; a no-op without RESEND_API_KEY.
    Best-effort: every failure is swallowed and logged.

    Call only on a genuine create (HTTP 201), not on an idempotent replay —
    the caller is responsible for that distinction.
    """
    operator = (
        os.environ.get("OPERATOR_ALERT_EMAIL", "").strip()
        or "leo@ranomics.com"
    )

    if not _OPERATOR_ALERT_INFLIGHT.acquire(blocking=False):
        # Every slot busy — a Resend stall is in progress. Shed rather than
        # queue: an operator alert is best-effort and must never accumulate
        # work behind a downstream stall.
        logger.warning("operator submission alert shed (inflight full)")
        return

    def _send() -> None:
        try:
            from html import escape  # noqa: PLC0415

            exp = escape(str(experiment_id or "(unknown)"))
            disp_name = escape(str(name or "(unnamed)"))
            etype = escape(str(experiment_type or "unknown"))
            target = escape(str(target_name or "unknown"))
            submitter = escape(str(submitter_user_id or "unknown"))
            subject = f"New Platform API submission: {name or '(unnamed)'}"[:200]
            html_body = (
                "<p>A new experiment was submitted via the Ranomics "
                "Platform API (MCP / REST).</p>"
                "<ul>"
                f"<li><strong>Experiment:</strong> {disp_name} ({exp})</li>"
                f"<li><strong>Type:</strong> {etype}</li>"
                f"<li><strong>Target:</strong> {target}</li>"
                f"<li><strong>Sequences:</strong> {int(sequence_count)}</li>"
                f"<li><strong>Submitter user_id:</strong> {submitter}</li>"
                "</ul>"
                '<p>Review at '
                '<a href="https://tools.ranomics.com/admin/lab-projects">'
                "/admin/lab-projects</a>.</p>"
            )
            _post_resend(
                to_email=operator,
                subject=subject,
                html_body=html_body,
                log_tag="platform_submission_alert",
            )
        except Exception:
            logger.warning(
                "notify_operator_new_submission failed", exc_info=True
            )
        finally:
            _OPERATOR_ALERT_INFLIGHT.release()

    try:
        threading.Thread(
            target=_send, name="platform_submission_alert", daemon=True
        ).start()
    except Exception:
        # Could not start the thread — release the slot so it is not leaked.
        _OPERATOR_ALERT_INFLIGHT.release()
        logger.warning(
            "Could not start platform_submission_alert thread", exc_info=True
        )


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
    # State the credit the wallet actually grants, by reading the grant.
    #
    # This used to read a WALLET_SIGNUP_CREDIT_USD env var, with a comment
    # claiming it let a deployment "change the grant without a code push".
    # It never did: the grant is SIGNUP_CREDIT_USD in shared/wallet.py, and
    # the env var only changed the number printed in this email. Its sole
    # possible effect was to make the welcome email disagree with the balance
    # the user was actually given -- which is exactly what happened. When the
    # grant went 5.00 -> 15.00 (2026-08-18) the preview environment still had
    # WALLET_SIGNUP_CREDIT_USD=5 set, so preview welcome emails advertised $5
    # against a $15 balance. Removing the override is what makes the "one
    # source" claim true; test_signup_credit_single_source.py now enforces it
    # for Python as well as templates.
    #
    # Imported inside the function: shared.wallet imports shared.email, so a
    # module-level import here would be a cycle.
    from shared.wallet import SIGNUP_CREDIT_USD  # noqa: PLC0415

    # "nearest": a fixed advertised amount, neither a balance nor a cost.
    credit_usd = _money(SIGNUP_CREDIT_USD, "nearest")
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
    # Credit added and the resulting balance: both balance-like, so DOWN.
    # Overstating either overstates the user's position.
    amt = _money(amount_usd, "down")
    bal = _money(new_balance_usd if new_balance_usd is not None else amount_usd, "down")
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
    # `amt` is what was charged to the CARD, so it rounds UP; `bal` is the
    # resulting balance, so it rounds DOWN. Same line, opposite directions.
    amt = _money(amount_usd, "up")
    bal = _money(new_balance_usd if new_balance_usd is not None else amount_usd, "down")
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
        total_usd=_money(total_usd, "up"),
        cap_usd=_money(cap_usd, "down"),
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
        # A61. The message whose ENTIRE purpose is to make the reader act on a
        # balance. A wallet holding $24.4950 was told "$24.50". An earlier pass
        # claimed to have fixed this and changed send_reengagement_email
        # instead, while the guard test named for THIS call site asserted the
        # other one's source text and stayed green.
        balance_usd=_money(balance_usd, "down"),
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"low_balance user={user_id}",
    )


def send_campaign_paused_email(
    *,
    user_id: str,
    campaign_id: str,
    campaign_name: str = "",
    **_extra: Any,
) -> bool:
    """A compute campaign paused because the wallet cannot fund the next chunk.

    Trigger: the campaign driver pauses a campaign into
    ``paused_insufficient_funds`` (fires once per pause event). The user tops up
    to resume; designs already produced stay downloadable meanwhile.
    """
    email = _resolve_user_email(user_id)
    if not email:
        logger.info(
            "send_campaign_paused_email: no email for user %s", user_id
        )
        return False
    base_url = _base_url()
    label = campaign_name.strip() or "Your campaign"
    subject = f"{label} is paused: add funds to continue"
    html = _render_template(
        "send_campaign_paused.html",
        base_url=base_url,
        campaign_name=label,
        campaign_url=f"{base_url}/campaigns/{campaign_id}",
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"campaign_paused user={user_id} campaign={campaign_id}",
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
        # Compute actually consumed: a COST. These three emails exist to
        # justify a charge the user did not expect, so quoting it low is the
        # one thing they must not do.
        attempted_usd=_money(attempted_usd, "up"),
        cap_usd=_money(cap_usd, "down"),
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
        # Compute actually consumed: a COST. These three emails exist to
        # justify a charge the user did not expect, so quoting it low is the
        # one thing they must not do.
        attempted_usd=_money(attempted_usd, "up"),
        cap_usd=_money(cap_usd, "down"),
    )
    return _post_resend(
        to_email=email,
        subject=subject,
        html_body=html,
        log_tag=f"overrun_warning user={user_id} tool={tool_slug}",
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
        spent_30d_usd=_money(spent_30d_usd, "up"),
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
    spent = _money(spent_30d_usd, "up")
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
    spent = _money(spent_30d_usd, "up")
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
