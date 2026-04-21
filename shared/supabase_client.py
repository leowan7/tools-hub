"""Supabase client factory for the Ranomics tools hub.

Centralises Supabase configuration in one place so auth and future
per-tool data access share the same project. The tools hub is designed
to share Epitope Scout's existing Supabase project via environment
variables (SUPABASE_URL, SUPABASE_KEY) — no new project is created.
"""

import logging
import os

logger = logging.getLogger(__name__)


def get_supabase_client():
    """Return a configured Supabase client, or None if env vars are missing.

    Reads SUPABASE_URL and SUPABASE_KEY from the environment. Either
    SUPABASE_KEY or SUPABASE_ANON_KEY is accepted for backwards compatibility
    with the Epitope Scout deployment (which uses SUPABASE_ANON_KEY).

    Returns:
        supabase.Client instance, or None if credentials are absent or the
        supabase package is not installed.
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (
        os.environ.get("SUPABASE_KEY", "").strip()
        or os.environ.get("SUPABASE_ANON_KEY", "").strip()
    )
    if not url or not key:
        logger.warning(
            "SUPABASE_URL or SUPABASE_KEY not set — auth unavailable."
        )
        return None
    try:
        from supabase import create_client  # noqa: PLC0415
        return create_client(url, key)
    except Exception:
        logger.warning("Could not create Supabase client.", exc_info=True)
        return None
