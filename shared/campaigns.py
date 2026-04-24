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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from shared.credits import get_service_client

logger = logging.getLogger(__name__)

_TABLE = "lab_campaigns"

ASSAY_TYPES = ("yeast_display", "mammalian_display", "dms")
BUDGET_BANDS = ("pilot", "sprint", "custom")
STATUSES = ("submitted", "reviewed", "scoped", "accepted", "declined")


@dataclass(frozen=True)
class Campaign:
    """Immutable view of a lab_campaigns row."""

    id: str
    user_id: str
    source_job_id: str
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

    @classmethod
    def from_row(cls, row: dict) -> "Campaign":
        kd = row.get("affinity_goal_kd_nm")
        return cls(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            source_job_id=str(row["source_job_id"]),
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
        from datetime import datetime, timezone  # noqa: PLC0415
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
