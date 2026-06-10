"""Supabase client factory for the Ranomics tools hub.

Centralises Supabase configuration in one place so auth and future
per-tool data access share the same project. The tools hub is designed
to share Epitope Scout's existing Supabase project via environment
variables (SUPABASE_URL, SUPABASE_KEY) — no new project is created.
"""

import logging
import os

logger = logging.getLogger(__name__)


# Bound every Supabase table (PostgREST) call. The library default is 120s
# (postgrest.constants.DEFAULT_POSTGREST_CLIENT_TIMEOUT) — long enough for a
# single stalled connection to pin a gunicorn worker until its own --timeout
# fires, which (with one worker) takes the whole site down. 30s is generous
# for any OLTP query while still letting a worker recover quickly. Override
# via SUPABASE_CLIENT_TIMEOUT_S for heavy cron reads if ever needed.
def _timeout_env(name: str, default: float) -> float:
    """Parse a float env var, tolerating empty/invalid values.

    Railway can return an empty string for a declared-but-unset var; a
    bare float("") would raise at import and crash the worker on boot.
    """
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


_CLIENT_TIMEOUT_S = _timeout_env("SUPABASE_CLIENT_TIMEOUT_S", 30.0)


def _client_options():
    """Return ClientOptions with a bounded PostgREST timeout.

    Returns None if the installed supabase version predates ClientOptions /
    the expected shape, so callers fall back to library defaults instead of
    crashing. Connect is capped short (5s) so dead connections fail fast;
    read/write/pool get the full budget for legitimately slow queries.
    """
    try:
        import httpx  # noqa: PLC0415
        from supabase.lib.client_options import ClientOptions  # noqa: PLC0415

        return ClientOptions(
            postgrest_client_timeout=httpx.Timeout(
                _CLIENT_TIMEOUT_S, connect=5.0
            ),
        )
    except Exception:  # pragma: no cover - version/shape guard
        logger.warning(
            "Could not build bounded Supabase ClientOptions; "
            "falling back to library default timeout.",
            exc_info=True,
        )
        return None


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
        return create_client(url, key, options=_client_options())
    except Exception:
        logger.warning("Could not create Supabase client.", exc_info=True)
        return None
