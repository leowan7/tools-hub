"""Scout -> Tools-hub handoff CRUD backed by ``public.scout_handoffs``.

Wave 3C. Scout creates a handoff row + stages the target PDB into the
shared ``tool-inputs`` bucket; tools-hub reads the row by id when the
user lands on a tool form, pre-fills visible inputs, and on submit
copies the staged PDB into the new job's storage path.

Service role only — neither app exposes this table to browsers. Scout
resolves ``user_id`` from the Supabase Auth admin API before inserting,
so the row is always owner-scoped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from shared.credits import get_service_client

logger = logging.getLogger(__name__)

_TABLE = "scout_handoffs"


@dataclass(frozen=True)
class Handoff:
    """Immutable view of a scout_handoffs row."""

    id: str
    user_id: str
    pdb_storage_path: str
    pdb_filename: str
    target_chain: str
    hotspot_residues: list[int]
    scout_job_id: Optional[str]
    scout_epitope_id: Optional[str]
    created_at: Optional[str]
    consumed_at: Optional[str]
    expires_at: Optional[str]

    @classmethod
    def from_row(cls, row: dict) -> "Handoff":
        return cls(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            pdb_storage_path=row["pdb_storage_path"],
            pdb_filename=row["pdb_filename"],
            target_chain=row["target_chain"],
            hotspot_residues=list(row.get("hotspot_residues") or []),
            scout_job_id=row.get("scout_job_id"),
            scout_epitope_id=row.get("scout_epitope_id"),
            created_at=row.get("created_at"),
            consumed_at=row.get("consumed_at"),
            expires_at=row.get("expires_at"),
        )


def create_handoff(
    *,
    user_id: str,
    pdb_storage_path: str,
    pdb_filename: str,
    target_chain: str,
    hotspot_residues: list[int],
    scout_job_id: Optional[str] = None,
    scout_epitope_id: Optional[str] = None,
) -> Optional[Handoff]:
    """Insert a new handoff row. Returns None on failure."""
    client = get_service_client()
    if client is None:
        logger.error("Cannot create handoff: service client unavailable.")
        return None
    row = {
        "user_id": user_id,
        "pdb_storage_path": pdb_storage_path,
        "pdb_filename": pdb_filename,
        "target_chain": target_chain,
        "hotspot_residues": list(hotspot_residues),
        "scout_job_id": scout_job_id,
        "scout_epitope_id": scout_epitope_id,
    }
    try:
        response = client.table(_TABLE).insert(row).execute()
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return None
        return Handoff.from_row(rows[0])
    except Exception:
        logger.error("Failed to insert scout_handoffs row.", exc_info=True)
        return None


def get_handoff(handoff_id: str, *, user_id: str) -> Optional[Handoff]:
    """Fetch a handoff row scoped to ``user_id``. Rejects consumed rows.

    Only ``consumed_at`` is checked. The ``expires_at`` column
    (supabase/migrations/0007_scout_handoffs.sql, NOT NULL DEFAULT
    now() + 2 hours) is read by nothing here, so a row older than two
    hours still resolves onto the form for as long as the staged object
    survives in the ``tool-inputs`` bucket. That is deliberate: the
    bucket's own 30-day age sweep (cron/purge_old_storage.py) is the
    real deadline, and expiring the row first would only take a working
    prefill away from someone who came back the next morning.
    """
    client = get_service_client()
    if client is None:
        return None
    try:
        response = (
            client.table(_TABLE)
            .select("*")
            .eq("id", handoff_id)
            .eq("user_id", user_id)
            .single()
            .execute()
        )
    except Exception:
        return None
    data = getattr(response, "data", None)
    if not data:
        return None
    if data.get("consumed_at"):
        return None
    return Handoff.from_row(data)


def mark_consumed(handoff_id: str) -> bool:
    """Set ``consumed_at = now()`` so the handoff cannot be replayed."""
    client = get_service_client()
    if client is None:
        return False
    try:
        response = (
            client.table(_TABLE)
            .update({"consumed_at": "now()"})
            .eq("id", handoff_id)
            .is_("consumed_at", "null")
            .execute()
        )
        return bool(getattr(response, "data", None))
    except Exception:
        logger.warning("Failed to mark handoff consumed.", exc_info=True)
        return False
