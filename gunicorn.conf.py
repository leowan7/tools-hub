"""Gunicorn configuration for the Ranomics tools hub.

Forces preload so import errors surface in logs instead of silent worker
death. Railway dashboard start commands override Procfile, so
--preload lives here too.

Also provisions the Prometheus multiprocess directory so /metrics can
aggregate counters across gunicorn workers. Without this, each worker
holds its own state and scrape results depend on which worker accepts
the /metrics request.
"""

import os
import shutil
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    """Parse an int env var, falling back on empty/invalid values.

    Railway can hand back an empty string for an unset-but-declared var;
    a bare int("") would crash config load and stop the app from booting.
    """
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# Load app in master before forking workers.
preload_app = True

# Run more than one worker so a single request blocked on a slow/stalled
# downstream (e.g. a hung Supabase connection) cannot take the whole site
# down. With one worker, one wedged request = full outage (incident
# 2026-06-10). Honours gunicorn's native WEB_CONCURRENCY env var; defaults
# to 2. This value is used UNLESS a start command passes --workers on the
# CLI, so the Procfile / nixpacks start commands must NOT pass --workers
# (and any Railway dashboard custom start command must not either).
# Floored at 1: a stray WEB_CONCURRENCY=0/-1 would otherwise make gunicorn
# refuse to boot — a crash-on-boot outage, the worst possible outcome.
workers = max(1, _int_env("WEB_CONCURRENCY", 2))

# Belt-and-suspenders worker recycle. Supabase calls are now bounded well
# under this (see shared.supabase_client), so this should rarely fire.
# Floored at 60s: it must stay safely above the 30s Supabase budget so a
# legitimately slow request is not killed mid-flight, AND a stray
# GUNICORN_TIMEOUT=0 must not be honoured — gunicorn reads 0 as *infinite*,
# which would silently reintroduce the unbounded-wait failure this fixes.
timeout = max(60, _int_env("GUNICORN_TIMEOUT", 120))

# Show worker lifecycle events (boot, exit, errors).
loglevel = "info"

# Log to stdout/stderr so Railway captures it.
accesslog = "-"
errorlog = "-"


# ---------------------------------------------------------------------------
# Prometheus multiprocess bookkeeping
# ---------------------------------------------------------------------------

_PROMETHEUS_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "/tmp/prom")


def on_starting(_server):  # noqa: ANN001 — gunicorn hook signature
    """Reset the multiprocess dir before workers boot.

    The prometheus_client multiprocess backend appends per-worker db
    files to this directory. Left unswept across deploys, the counter
    history would outlive the process and inflate values. Wipe on boot.
    """
    path = Path(_PROMETHEUS_DIR)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = str(path)


def child_exit(_server, worker):  # noqa: ANN001 — gunicorn hook signature
    """Clean up a worker's multiprocess files when it exits."""
    try:
        from prometheus_client import multiprocess  # type: ignore[import-untyped]
        multiprocess.mark_process_dead(worker.pid)
    except ImportError:
        pass
