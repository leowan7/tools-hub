"""P-1 staging-gate smoke for Proteina-Complexa (run AFTER seed_volumes.py).

    modal run tools/proteina/_validate_smoke.py
    modal run tools/proteina/_validate_smoke.py --app-name ranomics-proteina-staging

Calls a DEPLOYED app's ``run_tool`` with a ``validate`` payload. That runs
run_pipeline's free validate tier INSIDE that app's image + mounted Volumes: it
imports ``proteinfoundation.generate`` / ``.filter`` (the true test that the
built AF2/JAX/tmol/RF3 env imports cleanly), asserts all three variant configs
are present, and asserts a model ``*.ckpt`` exists under the weights mount
(/opt/proteina/ckpts). No design compute, no webhook, no upload — it writes
/tmp/smoke_results.json which run_tool returns inline.

This is a GPU-attached container but the validate branch only does CPU import +
file checks (seconds of A100 time, ~$0.01), so it is the cheap "two green
smokes" gate before any priced design shard. Prints the returned smoke_result;
exit non-zero if status != COMPLETED so it can gate a canary sequence.

WHAT IT PROVES, AND WHAT IT DOES NOT — read this before quoting a green run as
evidence. It proves that the app named by ``--app-name`` (default
``ranomics-proteina-prod``), meaning WHATEVER IMAGE IS CURRENTLY DEPLOYED under
that name, imports cleanly and has its Volumes mounted. It proves NOTHING about
a fresh build of ``tools/proteina/Dockerfile.modal``. On 2026-08-04 this smoke
passed green — while a fresh build of that same Dockerfile was producing an
image in which every design run died at import (unpinned dm-haiku 0.0.17
against the pinned jax 0.4.29). It passed because it was testing the
2026-07-16 deploy, which was fine; it could not answer "did MY build break?",
because it was never pointed at MY build. Hence ``--app-name``: deploy the
candidate under a staging name and aim this at it.

The build-time import gate in Dockerfile.modal is the check that answers "did
MY build break?" and it answers it before the image can be deployed at all.
This script answers the different question "is what is deployed still good, end
to end, with the real Volumes attached?" — neither replaces the other.
"""

from __future__ import annotations

import json
import sys

# ---------------------------------------------------------------------------
# The console, made incapable of killing the run
# ---------------------------------------------------------------------------
#
# BEFORE ``import modal``, AND BEFORE ANY OTHER STATEMENT THAT COULD PRINT.
# Container output reaches this process through modal's log pump, and the
# proteina container prints "  <check mark> ...", "  <round pushpin> ..." and
# box-drawing characters. On a Windows cp1252 console that write raises
# ``UnicodeEncodeError: 'charmap' codec can't encode character '✓'`` and
# kills the LOCAL entrypoint — while the REMOTE container carries on billing to
# completion or to its timeout. That is what killed ``_hotspot_canary --phase
# 0`` on 2026-08-04. Here the remote leg is only a validate tier (cents), but
# the same raise also destroys the smoke's own verdict, which is the thing a
# staging gate exists to deliver.
#
# ``_harden_stream`` mutates the stream's error handler IN PLACE and returns
# the SAME object, which is the point: modal's log pump, rich's renderer and
# the interpreter's own traceback printer each captured ``sys.stdout`` when they
# started, so REPLACING ``sys.stdout`` would leave every one of them writing to
# the strict original. Wrapping is only the fallback for a stream with no usable
# ``reconfigure``. NOT ``PYTHONIOENCODING=utf-8``: that works, and an operator
# forgets it exactly once.
#
# DUPLICATED FROM ``_canary_scoring.py`` RATHER THAN IMPORTED, deliberately.
# That module is reachable (it is a sibling file) but it is canary-lifetime
# code — ``_hotspot_canary.py`` and ``_design_canary.py`` both say "delete
# before flag-on" — while this script is the permanent staging gate. A
# hardening measure that makes an operational script die at import the day the
# canaries are deleted is worse than the crash it prevents. Importing it as
# ``tools.proteina._canary_scoring`` is worse still: that drags the web-tier
# adapter in through ``tools/proteina/__init__.py``. ~50 stateless stdlib lines
# is the cheaper cost. Canonical copy and its tests: ``_canary_scoring.py`` and
# ``tests/test_proteina_canary.py``.

# ``backslashreplace`` rather than ``replace``: the operator needs to be able to
# tell WHICH character could not be rendered.
CONSOLE_ERRORS = "backslashreplace"


def _safe_text(value, encoding=None, errors=CONSOLE_ERRORS):
    """``value`` rendered so that encoding it to ``encoding`` cannot raise.

    Lossless when the console can carry the text; ``None``/unknown encodings
    degrade to ASCII, which every console can take.
    """
    text = value if isinstance(value, str) else str(value)
    for candidate in (encoding, "ascii"):
        if not candidate:
            continue
        try:
            return text.encode(candidate, errors).decode(candidate, "replace")
        except (LookupError, UnicodeError, TypeError, ValueError):
            continue
    return text.encode("ascii", "backslashreplace").decode("ascii")


class _SafeStream:
    """Delegating proxy whose ``write`` cannot raise ``UnicodeEncodeError``.

    The FALLBACK only. Everything except ``write`` is delegated, so ``fileno``,
    ``encoding``, ``isatty`` and ``buffer`` keep working for anything that hands
    the stream to a subprocess.
    """

    def __init__(self, stream, errors=CONSOLE_ERRORS):
        object.__setattr__(self, "_stream", stream)
        object.__setattr__(self, "_errors", errors)

    def write(self, text):
        stream = object.__getattribute__(self, "_stream")
        try:
            return stream.write(text)
        except UnicodeEncodeError:
            return stream.write(_safe_text(
                text, getattr(stream, "encoding", None),
                object.__getattribute__(self, "_errors")))

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_stream"), name)


def _harden_stream(stream, errors=CONSOLE_ERRORS):
    """The stream to use in place of ``stream``, unable to raise on an
    unencodable character.

    Returns the SAME object whenever it could be reconfigured. ``None`` for
    ``None``, and the original for a stream with no encoding to fail at
    (``io.StringIO``, pytest's capture) — wrapping something that cannot raise
    only adds a layer between the caller and a real file descriptor.
    """
    if stream is None:
        return None
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(errors=errors)
            return stream
        except (ValueError, OSError, TypeError, AttributeError, LookupError):
            pass
    if not getattr(stream, "encoding", None):
        return stream
    if isinstance(stream, _SafeStream):
        return stream
    return _SafeStream(stream, errors)


sys.stdout = _harden_stream(sys.stdout)
sys.stderr = _harden_stream(sys.stderr)


import modal  # noqa: E402 — imported only after the console cannot kill us

app = modal.App("ranomics-proteina-validate-smoke")

# The deployed app this smoke interrogates. NOT a constant: see the docstring —
# a smoke that can only ever ask about prod cannot gate a candidate build.
DEFAULT_APP_NAME = "ranomics-proteina-prod"


@app.local_entrypoint()
def main(app_name: str = DEFAULT_APP_NAME) -> None:
    run_tool = modal.Function.from_name(app_name, "run_tool")
    payload = {
        "tier": "validate",
        "job_tier": "validate",
        "job_id": "validate-smoke",
        "job_spec": {
            "preset": "validate",
            "config_name": "search_binder_local_pipeline",
            "task_name": "02_PDL1",
        },
        "webhook_url": "",
    }
    print(f"[validate-smoke] app={app_name}", flush=True)
    print("[validate-smoke] invoking run_tool(validate) ...", flush=True)
    result = run_tool.remote(payload)
    print("[validate-smoke] raw result:", flush=True)
    print(json.dumps(result, indent=2, default=str), flush=True)

    smoke = (result or {}).get("smoke_result") or {}
    status = smoke.get("status")
    print(f"[validate-smoke] status={status} validate_ok={smoke.get('validate_ok')}", flush=True)
    if status != "COMPLETED":
        print(f"[validate-smoke] FAILED on app={app_name} — see error above", flush=True)
        sys.exit(1)
    print(f"[validate-smoke] PASS on app={app_name}", flush=True)
