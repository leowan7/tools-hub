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

# ---------------------------------------------------------------------------
# WORKER CLASS — sync, on purpose. Read this before changing it.
# ---------------------------------------------------------------------------
#
# No `worker_class` is set here, in the Procfile, or in nixpacks.toml, so
# gunicorn uses its default **sync** worker and no `threads` is set either.
# Each worker serves exactly ONE request at a time; whole-process concurrency
# is `workers`, nothing more. Consequently any in-process counter that caps
# "N concurrent X per worker" for N > 1 is unreachable — scout.ratelimit's
# anonymous compute cap is exactly that, and is documented there as inert
# rather than deleted, because it is the correct guard the moment this line
# changes.
#
# A threaded worker class (`gthread`) was written, measured, reviewed and
# deliberately NOT adopted. The reasoning is recorded here rather than in a
# document nobody will find, because the next person to want more concurrency
# will start in this file.
#
# WHAT gthread WOULD BUY. Exactly one thing: other routes stop queueing behind
# anonymous Scout compute. Under sync workers one anonymous analysis — up to
# ~15 CPU-seconds at the 8 MB upload cap — occupies a whole worker, so two
# concurrent ones make the entire site unresponsive, /healthz included. That
# is a real defect and threads are the only mechanism that fixes it.
#
# WHAT IT WOULD NOT BUY: capacity. Measured on this workload (24 pipelines,
# serial vs an 8-permit semaphore in one process): 1.39x wall throughput for
# 1.45x the CPU, i.e. ~1.07 effective cores. It is GIL-bound. Under saturation
# it makes CPU pressure slightly WORSE, because that extra 45% is contention.
#
# WHY NOT YET — three reasons, in order of weight.
#
#   1. It CANNOT ship before the money-path fixes it widens. Two read-then-act
#      races on money are live today and owned elsewhere:
#      `shared/idempotency.py:_claim_key` (an `upsert(on_conflict=)` that
#      succeeds for BOTH concurrent callers, so the duplicate-submit guard on
#      ten money-spending POSTs does not guard) and `shared/wallet.py`'s
#      auto-reload (read balance -> threshold -> 24 h count -> monthly cap ->
#      charge a card, with no SQL guard and no Stripe idempotency key). These
#      have already fired once under sync workers — see the incident recorded
#      at `blueprints/campaigns.py:238-242`, created=2, funded=2. Threads make
#      them reachable `threads` times more often. That is a sequencing fact,
#      not an opinion: this line moves after those land, not before.
#
#   2. The `timeout` watchdog below is lost, permanently, fleet-wide, and NO
#      gunicorn setting gives it back. Verified against the installed gunicorn
#      24.1.1, not from memory: `workers/gthread.py:288-297` calls
#      `self.notify()` at the top of `while self.alive:` on every pass of the
#      accept loop, then polls with a hard-coded 1 s timeout, and hands
#      requests to a ThreadPoolExecutor that the loop never waits on. The
#      notify is UNCONDITIONAL — it fires even when `can_accept` is False — so
#      lowering `worker_connections` produces backpressure but still does not
#      let the arbiter kill a request-wedged worker. Under sync workers a
#      request that overruns `timeout` takes its worker down and the fleet
#      self-heals; under gthread every wedged thread is permanent until a
#      redeploy. Trading an automatic recovery for a manual one is a real
#      cost, and it is paid by every route in the app, not just Scout's.
#
#   3. The thing it protects is not yet under load. Scout was opened to
#      anonymous callers only in #148. The per-IP limiter still bounds a single
#      address to ~20 metered hits per 10 minutes, so two overlapping anonymous
#      analyses is currently an unlikely event whose consequence is ~30 seconds
#      of slowness that then clears itself.
#
# WHY NOT THE OTHER OPTION EITHER. The plan offers a cross-worker semaphore
# (Postgres advisory lock or counter row) as the alternative. It is not one,
# for two independent reasons:
#
#   * Not implementable on this stack. There is no direct Postgres connection
#     anywhere in this repo — every query goes through Supabase PostgREST over
#     HTTP. A session-level `pg_advisory_lock` needs a connection held across
#     the request, which PostgREST cannot give; `pg_advisory_xact_lock` lives
#     only for one statement. A counter row is possible via `.rpc()`, but a
#     worker SIGKILLed while holding a slot leaks it forever, so it needs a
#     lease and a heartbeat — a distributed protocol, not a Phase 1 change.
#
#   * It turns the wrong way. With 2 sync workers, fleet-wide anonymous
#     concurrency is already at most 2. A cross-worker cap of 2 or more is a
#     no-op; the only setting that does anything is 1, which HALVES anonymous
#     capacity to reserve a worker. For a project whose goal is to let six
#     researchers behind one NAT use the tool at once, that is backwards. It
#     is the right mechanism for a many-worker deployment and this is a
#     two-worker one.
#
# TO FLIP IT, once (1) has landed: set `worker_class = "gthread"` and
# `threads = 8` here, and set `worker_connections` to roughly `threads` so a
# saturated worker stops accepting instead of silently swallowing up to the
# default 1000 connections. The sizing is already derived in
# scout/ratelimit.py and needs no rework:
#
#     8 threads = 2 anonymous compute slots  (ANON_MAX_CONCURRENT_RUNS —
#                    CPU-bound, each holds a thread for the whole SSE stream)
#               + 2 queued waiters           (ANON_MAX_QUEUED_RUNS — parked on
#                    a condition variable, consuming no CPU)
#               + 4 for everything else      (page loads, /scout/quota,
#                    downloads, /healthz, and signed-in routes, which can
#                    block up to 30 s on Supabase — which is why this is 4
#                    and not 1)
#
# Budget the OS threads honestly if you do: `fetch_known_binders` spawns up to
# 5 contact-download threads per call and runs for signed-in callers too, who
# consume no compute slot. So the ceiling is 8 x 5 = 40 contact threads + 4
# user_event + 2 operator-alert = ~54 per worker, ~108 fleet-wide — not the
# ~38 an earlier draft of this comment claimed by costing only the four
# anonymous slots.
#
# And it is only survivable at all because the SAbDab fan-out is gone: before
# that, every anonymous /analyze spawned up to 40 raw unbounded threads of its
# own. Do not re-enable a per-request fan-out without redoing this sum.
#
# One consequence of the worker model that no change here removes: in-process
# state (rate-limit counters included) is per-worker, so a per-process limit of
# L is really `workers x L` fleet-wide, and it resets on every deploy and
# worker recycle. Phase 3 is what fixes that.

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

# This runs at config-module scope on purpose — NOT from an on_starting
# hook. gunicorn execs this file while it builds the Application, and only
# afterwards does Arbiter.setup() honour preload_app and import app:app;
# Arbiter.start() calls on_starting later still. prometheus_client freezes
# its ValueClass the moment it is imported, reading this env var exactly
# once, so a value published from on_starting arrives after every Counter
# has already been built process-local. Nothing then writes the per-worker
# db files, while /metrics still sees the var and aggregates the (empty)
# directory — a 200 with a zero-byte body. Setting it here puts it in the
# environment before the preload import.
#
# The mkdir is not optional. An UNLABELLED counter opens
# <dir>/counter_<pid>.db the moment it is constructed (a labelled one waits
# for its first .labels() call), and shared.metrics builds exactly one
# unlabelled counter, SCOUT_RUNS. So a var pointing at a directory that
# does not exist yet raises FileNotFoundError during the preload import and
# the service never boots -- which is also why simply adding the variable
# to the Railway service without creating the directory would be an outage.
#
# The wipe is for a master restarting on a filesystem that already holds db
# files: a stale file is aggregated as if it were live and inflates the
# counters. It does nothing on a Railway deploy, which is a fresh container
# with an empty /tmp -- local dev and in-place restarts are what it is for.
# Note gunicorn re-execs this file on SIGHUP reload and SIGUSR2 reexec, so
# those wipe too, and the outgoing workers' samples vanish from the scrape
# for the overlap. Nothing in this deployment sends either signal.
_PROMETHEUS_DIR = Path(os.environ.get("PROMETHEUS_MULTIPROC_DIR", "/tmp/prom"))
shutil.rmtree(_PROMETHEUS_DIR, ignore_errors=True)
_PROMETHEUS_DIR.mkdir(parents=True, exist_ok=True)
os.environ["PROMETHEUS_MULTIPROC_DIR"] = str(_PROMETHEUS_DIR)


def child_exit(_server, worker):  # noqa: ANN001 — gunicorn hook signature
    """Clean up a worker's multiprocess files when it exits."""
    try:
        from prometheus_client import multiprocess  # type: ignore[import-untyped]
        multiprocess.mark_process_dead(worker.pid)
    except ImportError:
        pass
