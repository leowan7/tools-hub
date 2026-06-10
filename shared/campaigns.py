"""Wet-lab campaign handoff CRUD backed by ``public.lab_campaigns``.

Phase 3 (Wave 4). Directionally flipped mirror of :mod:`shared.handoffs`:
a logged-in user shortlists candidates on a completed tool_jobs run and
submits them as a scoping request to the Ranomics CRO team for yeast
display / mammalian display / DMS. The submission:

1. Inserts a ``lab_campaigns`` row (owner-scoped via RLS).
2. Copies each shortlisted candidate's PDB payload from the source job
   into the ``lab-campaigns/{campaign_id}/`` bucket folder, so Ranomics
   staff have durable access that does not depend on the source job's
   payload lifecycle.

Service-role only — ``/campaigns`` routes run under the user's login
but mutate this table via the service client. Admin mutations (status
changes, internal notes) also go through the service client.

Platform API addition (migration 0023+)
---------------------------------------
The /api/v1/experiments endpoint reuses this module via
``create_api_campaign``. API-direct rows leave ``source_job_id`` NULL
(no upstream tool_jobs row) and instead populate ``sequences`` +
``library_design`` JSONB columns. The status FSM for API rows runs on
the longer Adaptyv-compatible enum ('Draft', 'WaitingForConfirmation',
…, 'Done'); web-form rows continue on the short enum ('submitted',
'reviewed', …, 'declined'). The database CHECK accepts both sets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from shared.credits import get_service_client

logger = logging.getLogger(__name__)

_TABLE = "lab_campaigns"

ASSAY_TYPES = ("yeast_display", "mammalian_display", "dms")
BUDGET_BANDS = ("pilot", "sprint", "custom")
STATUSES = ("submitted", "reviewed", "scoped", "accepted", "declined")

# Platform-API FSM. Order is the canonical forward direction; transitions
# never go backwards. 'Cancelled' is reachable from any pre-'Done' state.
API_STATUSES = (
    "Draft",
    "WaitingForConfirmation",
    "QuoteSent",
    "WaitingForMaterials",
    "LibraryConstruction",
    "Sorting",
    "NGS",
    "DataAnalysis",
    "InReview",
    "Done",
    "Cancelled",
)
API_TERMINAL_STATUSES = frozenset({"Done", "Cancelled"})

RESULTS_STATUSES = ("none", "partial", "all")
SUBMISSION_SOURCES = ("web", "api")


@dataclass(frozen=True)
class Campaign:
    """Immutable view of a lab_campaigns row.

    Carries both web and API fields. Fields specific to one path are
    Optional; callers that only handle one path can check
    ``submission_source`` first.
    """

    id: str
    user_id: str
    # NULL on API-direct submissions.
    source_job_id: Optional[str]
    candidate_indices: list[int]
    target_name: str
    target_context: str
    assay_type: str
    affinity_goal_kd_nm: Optional[float]
    timeline_weeks: Optional[int]
    budget_band: str
    status: str
    ranomics_contact: Optional[str]
    notes_internal: Optional[str]
    created_at: Optional[str]
    reviewed_at: Optional[str]
    # Platform API fields (migration 0023).
    name: Optional[str] = None
    webhook_url: Optional[str] = None
    idempotency_key: Optional[str] = None
    sequences: Optional[dict[str, str]] = None
    library_design: Optional[dict[str, Any]] = None
    submission_source: str = "web"
    results_status: str = "none"
    last_transition_at: Optional[str] = None
    status_log: list[dict[str, Any]] = field(default_factory=list)
    # Operator quote (migration 0030). API rows only.
    quote_total_usd: Optional[float] = None
    quote_currency: str = "USD"
    quote_line_items: list[dict[str, Any]] = field(default_factory=list)
    quote_valid_until: Optional[str] = None
    quote_notes: Optional[str] = None
    # Operator results envelope (migration 0031). API rows only.
    results: Optional[dict[str, Any]] = None
    # Customer-facing note (migration 0032). Distinct from notes_internal.
    notes_customer: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict) -> "Campaign":
        kd = row.get("affinity_goal_kd_nm")
        source_job = row.get("source_job_id")
        return cls(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            source_job_id=str(source_job) if source_job is not None else None,
            candidate_indices=list(row.get("candidate_indices") or []),
            target_name=row["target_name"],
            target_context=row.get("target_context") or "",
            assay_type=row["assay_type"],
            affinity_goal_kd_nm=float(kd) if kd is not None else None,
            timeline_weeks=row.get("timeline_weeks"),
            budget_band=row["budget_band"],
            status=row["status"],
            ranomics_contact=row.get("ranomics_contact"),
            notes_internal=row.get("notes_internal"),
            created_at=row.get("created_at"),
            reviewed_at=row.get("reviewed_at"),
            name=row.get("name"),
            webhook_url=row.get("webhook_url"),
            idempotency_key=row.get("idempotency_key"),
            sequences=row.get("sequences"),
            library_design=row.get("library_design"),
            submission_source=row.get("submission_source") or "web",
            results_status=row.get("results_status") or "none",
            last_transition_at=row.get("last_transition_at"),
            status_log=list(row.get("status_log") or []),
            quote_total_usd=(
                float(row["quote_total_usd"])
                if row.get("quote_total_usd") is not None
                else None
            ),
            quote_currency=row.get("quote_currency") or "USD",
            quote_line_items=list(row.get("quote_line_items") or []),
            quote_valid_until=row.get("quote_valid_until"),
            quote_notes=row.get("quote_notes"),
            results=row.get("results"),
            notes_customer=row.get("notes_customer"),
        )


def create_campaign(
    *,
    user_id: str,
    source_job_id: str,
    candidate_indices: list[int],
    target_name: str,
    target_context: str,
    assay_type: str,
    budget_band: str,
    affinity_goal_kd_nm: Optional[float] = None,
    timeline_weeks: Optional[int] = None,
) -> Optional[Campaign]:
    """Insert a new campaign row. Validates enum values app-side before
    hitting the CHECK constraints so we can return a cleaner error path."""
    if assay_type not in ASSAY_TYPES:
        raise ValueError(f"invalid assay_type: {assay_type!r}")
    if budget_band not in BUDGET_BANDS:
        raise ValueError(f"invalid budget_band: {budget_band!r}")
    if not candidate_indices:
        raise ValueError("candidate_indices must be non-empty")

    client = get_service_client()
    if client is None:
        logger.error("Cannot create campaign: service client unavailable.")
        return None
    row = {
        "user_id": user_id,
        "source_job_id": source_job_id,
        "candidate_indices": list(candidate_indices),
        "target_name": target_name,
        "target_context": target_context or "",
        "assay_type": assay_type,
        "budget_band": budget_band,
        "affinity_goal_kd_nm": affinity_goal_kd_nm,
        "timeline_weeks": timeline_weeks,
    }
    try:
        response = client.table(_TABLE).insert(row).execute()
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return None
        return Campaign.from_row(rows[0])
    except Exception:
        logger.error("Failed to insert lab_campaigns row.", exc_info=True)
        return None


def get_campaign(campaign_id: str, *, user_id: Optional[str] = None) -> Optional[Campaign]:
    """Fetch one campaign. Pass ``user_id`` to scope to a submitter;
    omit for admin (service-role) reads."""
    client = get_service_client()
    if client is None:
        return None
    try:
        query = client.table(_TABLE).select("*").eq("id", campaign_id)
        if user_id is not None:
            query = query.eq("user_id", user_id)
        response = query.single().execute()
    except Exception:
        # FIX ME-05 (fresh-review): we still return None (callers translate
        # to 404 with no enumeration leak), but log so a real outage
        # doesn't look like every campaign just stopped existing. The
        # log includes the user_id scope so an operator can distinguish
        # "row not found" from "RLS denial" from "DB unreachable".
        logger.warning(
            "get_campaign returned no row for id=%s user_id=%s",
            campaign_id,
            user_id,
            exc_info=True,
        )
        return None
    data = getattr(response, "data", None)
    if not data:
        return None
    return Campaign.from_row(data)


def list_user_campaigns(user_id: str, *, limit: int = 50) -> list[Campaign]:
    """List a submitter's campaigns, newest first."""
    client = get_service_client()
    if client is None:
        return []
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception:
        logger.warning("list_user_campaigns query failed.", exc_info=True)
        return []
    rows = list(getattr(response, "data", None) or [])
    return [Campaign.from_row(r) for r in rows]


def list_all_campaigns(*, status: Optional[str] = None, limit: int = 200) -> list[Campaign]:
    """Admin view: every campaign, optionally filtered by status."""
    client = get_service_client()
    if client is None:
        return []
    try:
        query = client.table(_TABLE).select("*")
        if status is not None:
            if status not in STATUSES:
                raise ValueError(f"invalid status: {status!r}")
            query = query.eq("status", status)
        response = query.order("created_at", desc=True).limit(limit).execute()
    except Exception:
        logger.warning("list_all_campaigns query failed.", exc_info=True)
        return []
    rows = list(getattr(response, "data", None) or [])
    return [Campaign.from_row(r) for r in rows]


def update_status(
    campaign_id: str,
    *,
    status: str,
    ranomics_contact: Optional[str] = None,
    notes_internal: Optional[str] = None,
) -> Optional[Campaign]:
    """Admin mutation. Sets reviewed_at = now() the first time the row
    leaves status='submitted'."""
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    client = get_service_client()
    if client is None:
        return None
    patch: dict = {"status": status}
    if status != "submitted":
        patch["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    if ranomics_contact is not None:
        patch["ranomics_contact"] = ranomics_contact
    if notes_internal is not None:
        patch["notes_internal"] = notes_internal
    try:
        response = (
            client.table(_TABLE)
            .update(patch)
            .eq("id", campaign_id)
            .execute()
        )
    except Exception:
        logger.error("update_status failed for %s", campaign_id, exc_info=True)
        return None
    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return None
    return Campaign.from_row(rows[0])


def set_campaign_admin_fields(
    campaign_id: str,
    *,
    ranomics_contact: Optional[str] = None,
    notes_internal: Optional[str] = None,
    notes_customer: Optional[str] = None,
) -> Optional[Campaign]:
    """Set admin annotation fields (contact, internal notes, customer note)
    without touching status.

    Used by the admin UI for API-FSM campaigns, whose status moves via
    :func:`transition_api_status` (the atomic RPC) rather than
    :func:`update_status` — the latter validates against the *web* STATUSES
    and would reject an API status. Only fields passed as non-None are
    written, so a blank form field leaves the stored value untouched.

    ``notes_internal`` is operator-only and never surfaced to the customer;
    ``notes_customer`` (migration 0032) is written to the API status view
    and the notification webhook payload. They are deliberately distinct.
    """
    patch: dict = {}
    if ranomics_contact is not None:
        patch["ranomics_contact"] = ranomics_contact
    if notes_internal is not None:
        patch["notes_internal"] = notes_internal
    if notes_customer is not None:
        patch["notes_customer"] = notes_customer
    if not patch:
        return get_campaign(campaign_id)
    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table(_TABLE)
            .update(patch)
            .eq("id", campaign_id)
            .execute()
        )
    except Exception:
        logger.error(
            "set_campaign_admin_fields failed for %s", campaign_id, exc_info=True
        )
        return None
    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return None
    return Campaign.from_row(rows[0])


def set_campaign_quote(
    campaign_id: str,
    *,
    total_usd: Optional[float] = None,
    currency: str = "USD",
    line_items: Optional[list[dict[str, Any]]] = None,
    valid_until: Optional[str] = None,
    notes: Optional[str] = None,
) -> Optional[Campaign]:
    """Persist the operator-entered quote onto an API-source campaign
    (migration 0030 columns).

    Unlike :func:`set_campaign_admin_fields` — which writes only the fields
    you pass and leaves blanks untouched — the admin quote form is the full
    source of truth for the quote, so every quote column is written on each
    save. A cleared field clears the stored value. Status is deliberately
    NOT touched here; the route moves the row to 'QuoteSent' via
    :func:`transition_api_status` separately so the FSM stays on its atomic
    RPC path.

    Returns the refreshed Campaign, or None on DB failure.
    """
    client = get_service_client()
    if client is None:
        logger.error("set_campaign_quote: service client unavailable")
        return None
    patch: dict = {
        "quote_total_usd": total_usd,
        "quote_currency": (currency or "USD"),
        "quote_line_items": list(line_items or []),
        "quote_valid_until": valid_until,
        "quote_notes": notes,
    }
    try:
        response = (
            client.table(_TABLE)
            .update(patch)
            .eq("id", campaign_id)
            .execute()
        )
    except Exception:
        logger.error("set_campaign_quote failed for %s", campaign_id, exc_info=True)
        return None
    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return None
    return Campaign.from_row(rows[0])


def set_campaign_results(
    campaign_id: str,
    *,
    results: Optional[dict[str, Any]],
    results_status: str,
) -> Optional[Campaign]:
    """Persist the operator results envelope (migration 0031 ``results``
    column) and the ``results_status`` flag in one write.

    ``results`` is the full envelope (rounds, sequences, download_paths,
    optional external downloads) that GET /experiments/{id}/results reads;
    pass the merged envelope each save (the route owns the merge of newly
    uploaded files onto any prior ``download_paths``). Stored in a dedicated
    column rather than ``library_design`` so the internal download paths
    never leak through the experiment_spec.library_design passthrough.

    ``results_status`` must be one of RESULTS_STATUSES; it gates whether the
    API serves the envelope. Status (the FSM) is not touched here.
    """
    if results_status not in RESULTS_STATUSES:
        raise ValueError(f"invalid results_status: {results_status!r}")
    client = get_service_client()
    if client is None:
        logger.error("set_campaign_results: service client unavailable")
        return None
    patch: dict = {
        "results": results,
        "results_status": results_status,
    }
    try:
        response = (
            client.table(_TABLE)
            .update(patch)
            .eq("id", campaign_id)
            .execute()
        )
    except Exception:
        logger.error("set_campaign_results failed for %s", campaign_id, exc_info=True)
        return None
    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return None
    return Campaign.from_row(rows[0])


# ---------------------------------------------------------------------------
# Platform API additions (migration 0023+)
# ---------------------------------------------------------------------------


class IdempotentReplay(Exception):
    """Raised by ``create_api_campaign`` when an existing row matches the
    caller-supplied Idempotency-Key.

    The caller should respond with the existing row's API view and the
    same HTTP status as the original create (201). The exception carries
    the existing campaign so the handler doesn't need a second query.
    """

    def __init__(self, campaign: "Campaign") -> None:
        super().__init__(f"idempotent replay for {campaign.id}")
        self.campaign = campaign


def _initial_status_log_entry() -> list[dict[str, Any]]:
    return [
        {
            "status": "Draft",
            "at": datetime.now(timezone.utc).isoformat(),
            "by": "api",
        }
    ]


def find_by_idempotency_key(*, user_id: str, idempotency_key: str) -> Optional[Campaign]:
    """Look up an existing campaign by (user_id, idempotency_key).

    Returns None when no row matches. Used by ``create_api_campaign`` to
    implement the standard Idempotency-Key semantic: the same key from
    the same user always points at the same campaign.
    """
    if not idempotency_key:
        return None
    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .eq("idempotency_key", idempotency_key)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.warning(
            "find_by_idempotency_key query failed", exc_info=True
        )
        return None
    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return None
    return Campaign.from_row(rows[0])


def create_api_campaign(
    *,
    user_id: str,
    name: Optional[str],
    assay_type: str,
    target_name: str,
    target_context: str,
    sequences: dict[str, str],
    library_design: Optional[dict[str, Any]] = None,
    webhook_url: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[Campaign]:
    """Insert an API-direct campaign row.

    Differences vs :func:`create_campaign`:
      - No ``source_job_id`` (the caller did compute elsewhere).
      - ``sequences`` is a dict of ``{user_key: AMINO_STRING}`` — Adaptyv
        convention. Multi-chain entries use ``:`` as the separator.
      - ``library_design`` is a free-form JSONB describing the candidate
        library context (mode, diversity estimate, generator notes).
      - Status starts at ``'Draft'`` on the API FSM; the API handler
        moves it to ``'WaitingForConfirmation'`` immediately on submit.
      - ``budget_band`` is fixed at ``'custom'`` — calibrated targets
        (the future ``/api/v1/targets`` catalogue) will be billed
        differently but every alpha submission is custom-scoped.

    Idempotency
    -----------
    When ``idempotency_key`` is provided and a row already exists for
    ``(user_id, idempotency_key)``, raises :class:`IdempotentReplay`
    carrying the existing campaign. Callers must catch and replay.

    Returns None on database failure.
    """
    if assay_type not in ASSAY_TYPES:
        raise ValueError(f"invalid assay_type: {assay_type!r}")
    if not sequences:
        raise ValueError("sequences must be non-empty")
    if not target_name:
        raise ValueError("target_name is required")

    if idempotency_key:
        existing = find_by_idempotency_key(
            user_id=user_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            raise IdempotentReplay(existing)

    client = get_service_client()
    if client is None:
        logger.error("create_api_campaign: service client unavailable")
        return None

    now_iso = datetime.now(timezone.utc).isoformat()

    row = {
        "user_id": user_id,
        # Schema needs candidate_indices NOT NULL — we keep the
        # sequence-dict keys' positional indices here so admin tooling
        # that reads the legacy column still gets something sensible.
        "candidate_indices": list(range(len(sequences))),
        "target_name": target_name,
        "target_context": target_context or "",
        "assay_type": assay_type,
        "budget_band": "custom",
        "status": "Draft",
        "name": (name or "").strip()[:200] or None,
        "webhook_url": webhook_url,
        "idempotency_key": idempotency_key,
        "sequences": sequences,
        "library_design": library_design or {},
        "submission_source": "api",
        "results_status": "none",
        "last_transition_at": now_iso,
        "status_log": _initial_status_log_entry(),
    }
    try:
        response = client.table(_TABLE).insert(row).execute()
    except Exception as exc:
        # FIX #5 (validation review): if two concurrent POSTs with the
        # same Idempotency-Key both passed find_by_idempotency_key (both
        # saw None), the second insert hits the partial unique index
        # `lab_campaigns_user_idempotency_idx`. We don't want to return
        # 500 — re-fetch and raise IdempotentReplay so the caller surfaces
        # a clean 200 with the existing experiment.
        if idempotency_key and _is_unique_violation(exc):
            existing = find_by_idempotency_key(
                user_id=user_id, idempotency_key=idempotency_key
            )
            if existing is not None:
                raise IdempotentReplay(existing)
        logger.error("create_api_campaign: insert failed", exc_info=True)
        return None
    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return None
    return Campaign.from_row(rows[0])


try:  # pragma: no cover — import shape may shift across supabase-py versions
    from postgrest.exceptions import APIError as _PostgrestAPIError
except Exception:  # pragma: no cover
    _PostgrestAPIError = None  # type: ignore[assignment]


def _is_unique_violation(exc: BaseException) -> bool:
    """Detect Postgres SQLSTATE 23505 (unique_violation) on a supabase-py error.

    FIX ME-01 (fresh-review): the prior implementation only sniffed
    ``repr(exc)``, which the test suite exercised against a synthetic
    ``RuntimeError("…23505…")`` — production raised
    ``postgrest.exceptions.APIError`` whose repr does NOT include "23505"
    in every version. We now (1) match the real APIError class via
    isinstance and read ``.code`` directly, (2) fall back to the broader
    string sniff for non-postgrest paths (e.g. supabase-py wrapping the
    error body differently across releases), and (3) never crash on a
    missing attribute.

    Returning False on ambiguity is the safe failure mode — the caller's
    catch path falls through to logging + 500. Better than a false
    positive that surfaces a non-duplicate failure as a phantom replay.
    """
    if _PostgrestAPIError is not None and isinstance(exc, _PostgrestAPIError):
        code = (getattr(exc, "code", None) or "").strip()
        if code == "23505":
            return True
        details = (getattr(exc, "details", None) or "").lower()
        if "duplicate key" in details or "23505" in details:
            return True
        message = (getattr(exc, "message", None) or "").lower()
        if "duplicate key" in message or "23505" in message:
            return True
        return False

    # Non-postgrest path (legacy / test mocks / wrapping layer).
    text = repr(exc) + " " + str(exc)
    if "23505" in text or "duplicate key value" in text.lower():
        return True
    code = getattr(exc, "code", None) or getattr(exc, "pgcode", None)
    if code == "23505":
        return True
    body = getattr(exc, "json", None) or getattr(exc, "message", None)
    if body is not None and "23505" in str(body):
        return True
    return False


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of :func:`transition_api_status`.

    ``moved`` is True when the row actually changed status (so callers
    know to fire a webhook); False when the row was already at the target
    status, or was in a terminal state. ``prev_status`` is the
    pre-transition status — used by callers to populate the webhook
    payload's ``prev_status`` field honestly (FIX #6).
    """

    moved: bool
    prev_status: Optional[str]
    campaign: Optional[Campaign]


def transition_api_status(
    campaign_id: str,
    *,
    new_status: str,
    by: str = "system",
    results_status: Optional[str] = None,
) -> TransitionResult:
    """Atomically transition an API-FSM campaign to a new status.

    FIX #6 (validation review): the previous implementation did
    READ-MODIFY-WRITE in Python and lost concurrent status_log appends.
    This wraps the operation in the ``transition_lab_campaign_api``
    Postgres function (migration 0026) so the read+append+write happens
    under one row lock.

    Returns a :class:`TransitionResult` rather than the raw Campaign so
    callers can distinguish "no-op" (already in this status) from "moved"
    and fire webhooks accordingly.

    Behaviour
    ---------
    - Invalid ``new_status`` / ``results_status`` raise ``ValueError``
      (caught by routes.py to return a 400; cross-source attempts and
      transitions out of a terminal state are NOT raised — they fall
      through the RPC as ``TransitionResult(moved=False, campaign=…)``,
      which the caller treats as "nothing to do" without a 4xx.
    - Unknown campaign id or RPC failure returns
      ``TransitionResult(False, None, None)``.
    """
    if new_status not in API_STATUSES:
        raise ValueError(f"invalid API status: {new_status!r}")
    if results_status is not None and results_status not in RESULTS_STATUSES:
        raise ValueError(f"invalid results_status: {results_status!r}")

    client = get_service_client()
    if client is None:
        return TransitionResult(moved=False, prev_status=None, campaign=None)

    try:
        response = client.rpc(
            "transition_lab_campaign_api",
            {
                "p_campaign_id": campaign_id,
                "p_new_status": new_status,
                "p_by": by,
                "p_results_status": results_status,
            },
        ).execute()
    except Exception:
        logger.error(
            "transition_api_status RPC failed for %s",
            campaign_id,
            exc_info=True,
        )
        return TransitionResult(moved=False, prev_status=None, campaign=None)

    rows = list(getattr(response, "data", None) or [])
    if not rows:
        return TransitionResult(moved=False, prev_status=None, campaign=None)
    row = rows[0]
    # When the function found no matching row, every field is NULL.
    if row.get("campaign") is None:
        return TransitionResult(moved=False, prev_status=None, campaign=None)
    return TransitionResult(
        moved=bool(row.get("moved")),
        prev_status=row.get("prev_status"),
        campaign=Campaign.from_row(row["campaign"]),
    )


def delete_api_campaign(
    campaign_id: str,
    *,
    user_id: str,
    allowed_statuses: Optional[frozenset[str]] = None,
) -> bool:
    """Hard-delete an API-direct campaign row, fully guarded in the query.

    Backs the Platform API "withdraw" path: a caller removes their own
    not-yet-started experiment. The guard lives on the DELETE statement, not
    just on a prior read, so it holds under concurrency:

    - ``id`` + ``user_id`` — owner scope (never touch another user's row).
    - ``submission_source = 'api'`` — never delete a web-form submission.
    - ``status IN allowed_statuses`` (when given) — only delete while the row
      is still withdrawable, closing the TOCTOU window between the route's
      status check and this delete (a row quoted/advanced concurrently simply
      matches nothing).

    Returns True when a row was deleted, False otherwise (id not found, wrong
    owner, not API-sourced, no longer in an allowed status, or DB failure).
    """
    client = get_service_client()
    if client is None:
        logger.error("delete_api_campaign: service client unavailable")
        return False
    try:
        query = (
            client.table(_TABLE)
            .delete()
            .eq("id", campaign_id)
            .eq("user_id", user_id)
            .eq("submission_source", "api")
        )
        if allowed_statuses:
            query = query.in_("status", sorted(allowed_statuses))
        response = query.execute()
    except Exception:
        logger.error(
            "delete_api_campaign failed for %s", campaign_id, exc_info=True
        )
        return False
    rows = list(getattr(response, "data", None) or [])
    return len(rows) > 0


# ---------------------------------------------------------------------------
# Public API view shaping
# ---------------------------------------------------------------------------


def campaign_to_api_view(campaign: Campaign) -> dict[str, Any]:
    """Shape a Campaign as the public API response body.

    Conventions match Adaptyv where they map cleanly (``experiment_id``
    instead of ``id``, snake_case enums, etc.) so agents recognise the
    surface.
    """
    return {
        "experiment_id": campaign.id,
        "name": campaign.name,
        "status": campaign.status,
        "results_status": campaign.results_status,
        "experiment_spec": {
            "experiment_type": campaign.assay_type,
            "target": {
                "name": campaign.target_name,
                "context": campaign.target_context,
            },
            "library_design": campaign.library_design or {},
            "sequences": campaign.sequences or {},
        },
        "webhook_url": campaign.webhook_url,
        "created_at": campaign.created_at,
        "last_transition_at": campaign.last_transition_at,
        "status_log": campaign.status_log,
        "notes_customer": campaign.notes_customer,
    }


def campaign_to_status_view(campaign: Campaign) -> dict[str, Any]:
    """Lightweight status-only view for the polling endpoint."""
    return {
        "experiment_id": campaign.id,
        "status": campaign.status,
        "results_status": campaign.results_status,
        "last_transition_at": campaign.last_transition_at,
        "status_log": campaign.status_log,
        "notes_customer": campaign.notes_customer,
    }
