"""Scout -> Tools-hub handoff helpers.

Wave 3C. When a user clicks "Design binder in Tools" on a feasibility
result, Scout:

  1. Resolves the logged-in email to a Supabase user_id via the Auth
     admin API (requires the service-role key).
  2. Uploads the target PDB from ``tmp/<scout_job_id>/input.pdb`` to the
     shared ``tool-inputs`` bucket under
     ``{user_id}/scout-handoff-{handoff_id}/input.pdb``.
  3. Inserts a row into ``public.scout_handoffs`` recording the storage
     path + target chain + hotspot residues.
  4. Redirects the browser to
     ``https://tools.ranomics.com/tools/<slug>?handoff=<id>`` where the
     tools-hub form route pre-fills the visible fields and (on submit)
     copies the staged PDB into the new job's storage prefix.

Env vars (set on Railway scout service):
  SUPABASE_URL                 — same project as tools-hub
  SUPABASE_SERVICE_ROLE_KEY    — service-role key (not the anon key)
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BUCKET = "tool-inputs"
TOOLS_ORIGIN = os.environ.get("TOOLS_ORIGIN", "https://tools.ranomics.com")


def _get_service_client():
    """Return a Supabase client authed with the service-role key, or None."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        logger.warning(
            "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set — "
            "handoff unavailable."
        )
        return None
    try:
        from supabase import create_client  # noqa: PLC0415
        return create_client(url, key)
    except Exception:
        logger.warning("Could not create service-role client.", exc_info=True)
        return None


def resolve_user_id(email: str) -> Optional[str]:
    """Look up a Supabase Auth user by email. Returns UUID string or None."""
    if not email:
        return None
    client = _get_service_client()
    if client is None:
        return None
    try:
        page = client.auth.admin.list_users()
        users = getattr(page, "users", None) or page
        for u in users:
            u_email = getattr(u, "email", None) or (
                u.get("email") if isinstance(u, dict) else None
            )
            if u_email and u_email.lower() == email.lower():
                uid = getattr(u, "id", None) or (
                    u.get("id") if isinstance(u, dict) else None
                )
                return str(uid) if uid else None
    except Exception:
        logger.warning("Could not resolve user id for %s", email, exc_info=True)
    return None


def stage_pdb(
    *,
    user_id: str,
    handoff_id: str,
    pdb_path: Path,
    filename: str = "input.pdb",
) -> Optional[str]:
    """Upload ``pdb_path`` into the shared tool-inputs bucket.

    Returns the storage object path, or None on failure. The path is
    chosen so tools-hub's RLS owner-prefix match still holds when the
    service client later copies the file into the new job's prefix.
    """
    client = _get_service_client()
    if client is None:
        return None
    if not pdb_path.is_file():
        logger.warning("Scout PDB missing at %s", pdb_path)
        return None
    try:
        data = pdb_path.read_bytes()
    except Exception:
        logger.warning("Could not read PDB at %s", pdb_path, exc_info=True)
        return None
    object_path = f"{user_id}/scout-handoff-{handoff_id}/{filename}"
    try:
        bucket = client.storage.from_(BUCKET)
        bucket.upload(
            path=object_path,
            file=data,
            file_options={
                "content-type": "chemical/x-pdb",
                "upsert": "true",
            },
        )
    except Exception:
        logger.warning(
            "Failed to upload Scout PDB to %s", object_path, exc_info=True
        )
        return None
    return object_path


def create_handoff(
    *,
    user_email: str,
    scout_job_id: str,
    target_chain: str,
    hotspot_residues: list[int],
    scout_epitope_id: Optional[str] = None,
    pdb_path: Optional[Path] = None,
) -> Optional[str]:
    """Full end-to-end: upload PDB, insert handoff row, return its id.

    Returns None on any failure; caller surfaces a generic error.
    """
    user_id = resolve_user_id(user_email)
    if not user_id:
        return None

    handoff_id = str(uuid.uuid4())
    path = pdb_path or (Path("tmp") / scout_job_id / "input.pdb")
    storage_path = stage_pdb(
        user_id=user_id,
        handoff_id=handoff_id,
        pdb_path=path,
    )
    if not storage_path:
        return None

    client = _get_service_client()
    if client is None:
        return None
    row = {
        "id": handoff_id,
        "user_id": user_id,
        "pdb_storage_path": storage_path,
        "pdb_filename": path.name,
        "target_chain": (target_chain or "A").strip() or "A",
        "hotspot_residues": list(hotspot_residues or []),
        "scout_job_id": scout_job_id,
        "scout_epitope_id": scout_epitope_id,
    }
    try:
        response = client.table("scout_handoffs").insert(row).execute()
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return None
    except Exception:
        logger.warning("Failed to insert scout_handoffs row.", exc_info=True)
        return None
    return handoff_id


def handoff_redirect_url(tool_slug: str, handoff_id: str) -> str:
    """Build the tools-hub URL the browser is redirected to."""
    return f"{TOOLS_ORIGIN}/tools/{tool_slug}?handoff={handoff_id}"
