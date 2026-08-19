"""Boot a real gunicorn arbiter and report what /metrics would render.

Not a test module (leading underscore: pytest does not collect it).
``tests/test_metrics.py`` runs it as a subprocess with cwd = repo root.

WHY A REAL ARBITER, NOT A REPLAY. The defect this guards is an ORDERING
one, and the order is gunicorn's to choose, not ours. ``prometheus_client``
reads ``PROMETHEUS_MULTIPROC_DIR`` once, when it is imported, and freezes
its ValueClass from it; ``gunicorn.conf.py`` is what puts the variable
there. If the provisioning ever moves back behind a hook that
``Arbiter.start()`` runs -- an ``on_starting``, say -- the app has already
been preloaded by then, every Counter is process-local, no per-worker db
file is ever written, and ``/metrics`` answers 200 with a ZERO-BYTE body.
A hand-rolled "exec the conf, then import the app" replay would hard-code
the half of the ordering that is gunicorn's to choose. It would still catch
the hook regression, but it would go on passing if some future gunicorn read
its config later than it does today. So this drives ``WSGIApplication`` +
``Arbiter`` themselves and lets gunicorn decide when the config is read and
when ``preload_app`` imports the app.

WHY A SUBPROCESS. ``prometheus_client`` picks its ValueClass once, at its
own import, and every metric object built afterwards holds a value instance
of whichever class was current when THAT object was built. Rebinding
``values.ValueClass`` later does work, but only for metrics constructed
after the rebind, so the counters ``shared.metrics`` has already built under
pytest can never be moved onto the multiprocess backend. The question is
answerable once per interpreter, and pytest answered it at collection.

The multiprocess directory is the real one gunicorn.conf.py names, because
the variable has to be ABSENT from the environment at process start for the
question to mean anything -- pointing it somewhere private would set it, and
set is the case where the defect cannot happen. That directory is this app's
own scratch space and gunicorn.conf.py wipes it on every boot regardless, so
the only cost is that two of these probes must not run at the same instant.
That collision fails LOUD rather than quiet: a competing wipe removes this
process's db file and the scrape comes back empty. The reverse -- a stale db
file left by someone else rendering samples over a process-local backend --
is why the caller asserts the ValueClass reported below and does not settle
for a non-empty body.

Run by hand for debugging::

    python tests/_metrics_boot_probe.py
    python tests/_metrics_boot_probe.py --no-prometheus

Prints one ``PROBE {...}`` JSON line. ``--no-prometheus`` blocks the import
so ``shared.metrics`` takes its deliberate stub path, which is how the
caller checks that this guard stays quiet when the library is legitimately
absent rather than failing an offline checkout.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types

# Stand-in for app.py. All ``--preload`` needs is a module whose import
# builds the Counters, which is exactly what app.py does via shared.metrics.
# Importing the real app.py here would pull in Supabase/Stripe/Modal and
# load_dotenv() the repo's live credentials for no added coverage.
_STANDIN = '''\
from shared.metrics import (  # noqa: F401
    PROMETHEUS_AVAILABLE,
    SCOUT_RUNS,
    _render_metrics,
)


def app(environ, start_response):  # pragma: no cover - never served
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]
'''

_COUNTER = b"tools_hub_scout_runs_total"


def _install_posix_shims() -> None:
    """Let gunicorn import on Windows.

    gunicorn.util imports fcntl/grp/pwd, gunicorn.sock reads
    ``socket.AF_UNIX``, gunicorn.arbiter builds its SIGNALS list from POSIX
    signal names, and gunicorn.config defaults its user/group settings from
    ``os.geteuid()``/``os.getegid()`` -- all at import time. Of those only
    geteuid/getegid are ever called, and only to compute those two defaults.
    None of them sit on the boot path this probe measures:
    ``Application.__init__``, ``Arbiter.__init__`` and the on_starting hook.
    On Linux every branch below is a no-op.
    """
    for name, attrs in (
        ("fcntl", {"fcntl": lambda *a, **k: 0, "flock": lambda *a, **k: 0,
                   "F_GETFD": 1, "F_SETFD": 2, "FD_CLOEXEC": 1}),
        ("grp", {"getgrnam": lambda n: None, "getgrgid": lambda g: None}),
        ("pwd", {"getpwnam": lambda n: None, "getpwuid": lambda u: None}),
    ):
        if name not in sys.modules:
            try:
                __import__(name)
                continue
            except ImportError:
                module = types.ModuleType(name)
                for key, value in attrs.items():
                    setattr(module, key, value)
                sys.modules[name] = module

    import socket
    if not hasattr(socket, "AF_UNIX"):
        socket.AF_UNIX = 1

    import signal
    for offset, name in enumerate(
        ("SIGHUP", "SIGQUIT", "SIGTTIN", "SIGTTOU", "SIGUSR1", "SIGUSR2",
         "SIGWINCH", "SIGCHLD"),
        start=101,
    ):
        if not hasattr(signal, name):
            setattr(signal, name, offset)

    for name in ("geteuid", "getegid"):
        if not hasattr(os, name):
            setattr(os, name, lambda: 0)


def _block_prometheus_client() -> None:
    """Make ``import prometheus_client`` fail, as an offline checkout would."""

    class _Blocker:
        def find_spec(self, name, path=None, target=None):
            if name == "prometheus_client" or name.startswith("prometheus_client."):
                raise ImportError("blocked by _metrics_boot_probe")
            return None

    sys.meta_path.insert(0, _Blocker())


def main(argv: list[str]) -> int:
    _install_posix_shims()
    if "--no-prometheus" in argv:
        _block_prometheus_client()

    # Railway does not set this (checked against the live service), so the
    # deployed process starts without it. Anything inherited from the test
    # runner would mask the defect.
    os.environ.pop("PROMETHEUS_MULTIPROC_DIR", None)
    os.environ.pop("prometheus_multiproc_dir", None)

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "_hub_standin.py"), "w", encoding="utf-8") as fh:
            fh.write(_STANDIN)
        sys.path.insert(0, tmp)
        sys.path.insert(0, repo)

        # Production passes no -c, so neither do we: gunicorn discovers
        # ./gunicorn.conf.py from the cwd, the same way it does on Railway,
        # and a discovery regression is then something this probe can see.
        # It cannot degrade into a quiet pass either -- with no config found,
        # preload_app reverts to gunicorn's own default of False and the
        # caller's assertion on it fires.
        os.chdir(repo)
        sys.argv = ["gunicorn", "_hub_standin:app"]

        from gunicorn.app.wsgiapp import WSGIApplication
        from gunicorn.arbiter import Arbiter

        # Constructing the Application is what execs gunicorn.conf.py.
        wsgi_app = WSGIApplication("%(prog)s [OPTIONS] [APP_MODULE]")
        # Arbiter.__init__ -> setup() -> honours preload_app and imports the
        # app. This is the moment prometheus_client freezes its ValueClass.
        arbiter = Arbiter(wsgi_app)
        # The app has to be imported ALREADY, by preload inside that
        # constructor. Asserting the preload_app setting is not the same
        # claim: if the import ever slipped to after the hook below, this
        # probe's own `import _hub_standin` would be the thing that triggers
        # it, the env var would be set by then, and a broken config would
        # come back green.
        imported_during_preload = "_hub_standin" in sys.modules
        # ...and this is the next thing Arbiter.start() does. Calling it
        # keeps a provisioning hook in play without binding a socket, so a
        # regression that moves the work back here is still measured end to
        # end rather than being quietly skipped.
        wsgi_app.cfg.on_starting(arbiter)

        import _hub_standin as loaded

        verdict = {
            "preload_app": bool(wsgi_app.cfg.preload_app),
            "imported_during_preload": imported_during_preload,
            "prometheus": bool(loaded.PROMETHEUS_AVAILABLE),
        }

        if verdict["prometheus"]:
            import prometheus_client.values as values

            # One worker's traffic, then a scrape.
            loaded.SCOUT_RUNS.inc()
            body = loaded._render_metrics().get_data()
            verdict["value_class"] = values.ValueClass.__name__
            verdict["multiproc_dir"] = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
            verdict["body_bytes"] = len(body)
            verdict["has_counter"] = _COUNTER in body

    print("PROBE " + json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
