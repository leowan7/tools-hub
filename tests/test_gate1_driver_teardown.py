"""The gate1 driver's money guards, exercised with `modal` stubbed out.

`scripts/gate1_boltz2_smoke.py` spawns a billed A100. Its spend guards exist
only to bound what a failure costs, so every one of them is invisible on the
happy path and is exactly the code that will not have been exercised when it
is finally needed:

  1. every exit from the poll loop leaves the container dead, not just the
     deadline exit -- a leak is bounded only by modal's `_MAX_SESSION_S`
     (3600 s, tools/boltz2/modal_app.py), $2.57 against a $0.79 ceiling;
  2. the window between the spawn and the fsynced call id, which sits
     outside that loop, leaves nothing running unrecorded -- and the id
     reaches the console before the write, so a cancel that fails there
     still leaves a findable container;
  3. a failed raw re-download neither destroys an already-fetched archive --
     the only evidence separating "folds" from "no folds" -- nor scores 0
     while that archive sits unread;
  4. a stale call-log record is cancelled rather than reattached to, and the
     sweep covers every recorded tier, not just the one that tripped it;
  5. a previous run's ledger is never overwritten.

These run the REAL driver as `__main__` through `runpy`, against a fake
`modal`, so what is under test is the shipped file rather than a paraphrase
of it. No GPU, no network, no spend. `monkeypatch` owns the globals they
set and `_drop_imported_scripts` removes the ones they import, which
`test_nothing_leaks_into_the_rest_of_the_suite` checks.
"""
import io
import json
import os
import runpy
import sys
import tarfile
import time
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
DRIVER = os.path.join(SCRIPTS, "gate1_boltz2_smoke.py")


@pytest.fixture(autouse=True)
def _drop_imported_scripts():
    """Remove the script modules these tests import.

    `monkeypatch` restores what it is handed, but the refetch tests reach
    `gate1_raw` through `delitem(..., raising=False)` on a key that is
    normally absent, which records nothing to undo. The module then stays in
    `sys.modules` bound to a dead `modal` stub, where anything importing it
    later would pick it up.

    Remove this fixture and `test_nothing_leaks_into_the_rest_of_the_suite`
    goes red. That is the in-repo evidence for the leak; the `delitem`
    mechanism above is only the explanation offered for it.
    """
    yield
    for name in ("gate1_raw", "gate1_boltz2_smoke"):
        sys.modules.pop(name, None)


_STUBS = []          # every fake `modal` this file installs


def _install_modal(monkeypatch, behaviour):
    """Put a fake `modal` in sys.modules. Returns the call recorder.

    `behaviour` picks what the poll's `FunctionCall.get()` does, which is the
    only axis the teardown tests vary.
    """
    rec = types.SimpleNamespace(
        cancels=[], spawns=[], read_file=lambda path: iter(()),
        spawn_id="fc-SPAWNED", cancel_raises=None,
    )

    class FunctionTimeoutError(Exception):
        pass

    class _PollTimeout(Exception):
        pass

    exc = types.ModuleType("modal.exception")
    exc.FunctionTimeoutError = FunctionTimeoutError
    exc.TimeoutError = _PollTimeout

    class _Call:
        def __init__(self, object_id):
            self.object_id = object_id

        def get(self, timeout=None):
            if behaviour == "ok":
                # A real terminal payload: the uploads are rigged to fail, so
                # a healthy run still reports FAILED with 0 designs written.
                return {"status": "FAILED", "designs_completed": 0}
            if behaviour == "generic":
                raise ConnectionError("transient gRPC blip")
            if behaviour == "ctrlc":
                raise KeyboardInterrupt()
            if behaviour == "fn_timeout":
                raise FunctionTimeoutError("modal's own timeout")
            raise AssertionError(behaviour)

        def cancel(self, terminate_containers=False):
            # Recorded before it can raise, so a test can assert the
            # attempt was made as well as what became of it.
            rec.cancels.append((self.object_id, terminate_containers))
            if rec.cancel_raises is not None:
                raise rec.cancel_raises

    def _spawn(payload):
        rec.spawns.append(payload)
        return _Call(rec.spawn_id)

    modal = types.ModuleType("modal")
    modal.exception = exc
    modal.Function = types.SimpleNamespace(
        from_name=lambda app, fn: types.SimpleNamespace(spawn=_spawn)
    )
    modal.FunctionCall = types.SimpleNamespace(from_id=_Call)
    modal.Volume = types.SimpleNamespace(
        from_name=lambda name: types.SimpleNamespace(
            read_file=lambda path: rec.read_file(path)
        )
    )
    _STUBS.append(modal)
    monkeypatch.setitem(sys.modules, "modal", modal)
    monkeypatch.setitem(sys.modules, "modal.exception", exc)
    return rec


def _prepare(monkeypatch, tmp_path, behaviour, call_log=None):
    """Stub modal, isolate the CWD, and force a fresh import of the scripts."""
    rec = _install_modal(monkeypatch, behaviour)
    # `python scripts/gate1_boltz2_smoke.py` puts scripts/ at sys.path[0];
    # runpy does not, and the driver does `from gate1_raw import folds_in_raw`.
    monkeypatch.syspath_prepend(SCRIPTS)
    for name in ("gate1_raw", "gate1_boltz2_smoke"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.chdir(tmp_path)
    if call_log is not None:
        records = call_log if isinstance(call_log, list) else [call_log]
        (tmp_path / "gate1_calls.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records)
        )
    return rec


def _exec():
    """Run the real driver as __main__. Returns how it exited."""
    try:
        runpy.run_path(DRIVER, run_name="__main__")
        return "returned"
    except SystemExit as exc:
        return f"SystemExit({exc.code})"


# --------------------------------------------------------------------------
# 1. every exit from the poll loop leaves the container dead
# --------------------------------------------------------------------------


def test_generic_exception_cancels_the_container(monkeypatch, tmp_path, capsys):
    rec = _prepare(monkeypatch, tmp_path, "generic")
    _exec()
    assert rec.cancels == [("fc-SPAWNED", True)]
    # and the failure is still scored rather than swallowed
    assert "produced no fold" in capsys.readouterr().out


def test_keyboard_interrupt_cancels_and_still_propagates(monkeypatch, tmp_path):
    rec = _prepare(monkeypatch, tmp_path, "ctrlc")
    # KeyboardInterrupt is not an Exception, so it unwinds straight through
    # the poll loop's `except Exception`. Only the `finally` catches it.
    try:
        _exec()
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("KeyboardInterrupt was swallowed")
    assert rec.cancels == [("fc-SPAWNED", True)]


def test_modal_own_timeout_does_not_re_cancel(monkeypatch, tmp_path):
    rec = _prepare(monkeypatch, tmp_path, "fn_timeout")
    _exec()
    # modal already tore the container down; a second cancel would be noise.
    assert rec.cancels == []


def test_clean_return_does_not_cancel(monkeypatch, tmp_path):
    rec = _prepare(monkeypatch, tmp_path, "ok")
    _exec()
    assert rec.cancels == []


# --------------------------------------------------------------------------
# 3. call-log and ledger handling
# --------------------------------------------------------------------------


def _record(job, call_id, age_s):
    return {
        "tier": "standalone",
        "job_id": job,
        "call_id": call_id,
        "deadline_s": 1100,
        "spawned_at": time.time() - age_s,
    }


def test_stale_call_log_is_cancelled_never_reattached(
    monkeypatch, tmp_path, capsys
):
    rec = _prepare(
        monkeypatch,
        tmp_path,
        "ok",
        call_log=_record("gate1-standalone-OLD", "fc-STALE", 86400),
    )
    kind = _exec()
    out = capsys.readouterr().out
    assert kind == "SystemExit(1)"
    assert rec.spawns == []                        # no new billed job
    assert rec.cancels == [("fc-STALE", True)]     # the old one is torn down
    # `wall` counts from spawned_at, so reattaching to a day-old record would
    # have reported tens of dollars for a run that spent nothing.
    assert "TOTAL" not in out


def test_fresh_call_log_still_reattaches(monkeypatch, tmp_path, capsys):
    rec = _prepare(
        monkeypatch,
        tmp_path,
        "ok",
        call_log=_record("gate1-standalone-NOW", "fc-FRESH", 10),
    )
    _exec()
    # Reattach is the whole point of the call log: a killed client must not
    # pay twice for the same container.
    assert "REATTACH call=fc-FRESH" in capsys.readouterr().out
    assert rec.spawns == []


def test_stale_sweep_cancels_every_recorded_call(monkeypatch, tmp_path):
    """The tripped tier is not the only one that can hold a live container.

    Tiers run in order, so on a multi-tier resume the EARLIER tier is the one
    whose record ages past its deadline while the LATER tier is still burning.
    Cancelling only the tripped tier would abandon that live A100 -- and the
    remedy the guard prints (move the call log aside) would then destroy the
    only record of its call id.
    """
    stale_first = _record("gate1-standalone-OLD", "fc-OLD-DONE", 86400)
    live_second = dict(
        _record("gate1-msa_server-NOW", "fc-LIVE-BURNING", 10), tier="msa_server"
    )
    rec = _prepare(monkeypatch, tmp_path, "ok", call_log=[stale_first, live_second])
    kind = _exec()
    assert kind == "SystemExit(1)"
    assert ("fc-OLD-DONE", True) in rec.cancels        # the corpse
    assert ("fc-LIVE-BURNING", True) in rec.cancels    # and the live one
    assert rec.spawns == []


def test_lost_call_id_tears_the_container_down(monkeypatch, tmp_path):
    """The window between the spawn and the fsynced id is not the poll loop's.

    Nothing can cancel a container until its id reaches disk, so a failed
    write would otherwise leave a live A100 with no record of how to stop it.
    Here the id is unserialisable, which fails the write with the container
    already up.
    """
    rec = _prepare(monkeypatch, tmp_path, "ok")
    rec.spawn_id = object()
    try:
        _exec()
    except TypeError:
        pass
    else:
        raise AssertionError("the unloggable id did not fail the write")
    assert rec.cancels == [(rec.spawn_id, True)]


class _PrintableButUnloggable:
    """An id `json.dumps` refuses but an f-string renders.

    Stands in for any failure of the call-log write -- a full or read-only
    CWD is the realistic one -- while keeping the id readable on the
    console, which is the half under test here.
    """

    def __repr__(self):
        return "fc-UNSTOPPABLE"


def test_cancel_failure_after_a_lost_id_still_names_the_container(
    monkeypatch, tmp_path, capsys
):
    """Both halves of the spawn window can fail together, and plausibly do.

    Whatever fails the write can fail the control-plane cancel as well, and
    then the console holds the only record of the id anywhere. Two things
    have to hold: the write's error is the diagnosis, so the cancel must not
    replace it, and the id must already have been printed by then.
    """
    rec = _prepare(monkeypatch, tmp_path, "ok")
    rec.spawn_id = _PrintableButUnloggable()
    rec.cancel_raises = ConnectionError("control plane unreachable")
    try:
        _exec()
    except ConnectionError:
        raise AssertionError("the cancel's error masked the write's")
    except TypeError:
        pass                                       # the WRITE's error, kept
    else:
        raise AssertionError("the unloggable id did not fail the write")
    assert rec.cancels == [(rec.spawn_id, True)]   # it did try to cancel
    out = capsys.readouterr().out
    assert "spawned call=fc-UNSTOPPABLE" in out    # printed BEFORE the write
    assert "modal app history" in out              # and the operator is told


def test_previous_ledger_is_not_clobbered(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, "ok")
    prior = tmp_path / "gate1_results.json"
    prior.write_text('{"real":"paid run"}')
    _exec()
    assert prior.read_text() == '{"real":"paid run"}'
    assert (tmp_path / "gate1_results.1.json").exists()


# --------------------------------------------------------------------------
# 2. a failed re-download does not destroy a good archive
# --------------------------------------------------------------------------


def _good_tar(tmp_path):
    """Bytes of a real .tgz holding one fold under an out/ directory."""
    out = tmp_path / "src" / "d_000" / "out"
    out.mkdir(parents=True)
    (out / "design.pdb").write_text("ATOM\n")
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tf:
        tf.add(str(tmp_path / "src"), arcname="src")
    return blob.getvalue()


def _explode(_path):
    yield b"\x1f\x8b partial"
    raise ConnectionError("dropped mid-stream")


def _refetch_scenario(fetch, rec, tmp_path):
    """Fetch a good archive, then re-fetch with the Volume failing mid-stream.

    Returns (first_result, second_result, bytes_before, bytes_after).
    """
    payload = _good_tar(tmp_path / "build")
    raw_dir = str(tmp_path / "raw")
    rec.read_file = lambda path: iter([payload])
    first = fetch("job1", raw_dir=raw_dir)

    local = os.path.join(raw_dir, "job1.tgz")
    before = open(local, "rb").read()
    rec.read_file = _explode
    second = fetch("job1", raw_dir=raw_dir)
    after = open(local, "rb").read() if os.path.exists(local) else b"<gone>"
    return first, second, before, after


def test_failed_refetch_falls_back_to_the_cached_archive(monkeypatch, tmp_path):
    """Preserving the archive is only half the fix -- it has to be read.

    Scoring 0 while an archive proving a fold sits on disk would abort the
    run on a transient Volume blip, which is the same wrong answer the
    truncating version gave, just without the data loss.
    """
    rec = _install_modal(monkeypatch, "ok")
    monkeypatch.syspath_prepend(SCRIPTS)
    monkeypatch.delitem(sys.modules, "gate1_raw", raising=False)
    import gate1_raw

    first, second, before, after = _refetch_scenario(
        gate1_raw.folds_in_raw, rec, tmp_path
    )
    assert first[0] == 1, first          # the good fetch really saw a fold
    assert second[0] == 1, second        # and the blip still scores that fold
    assert after == before               # off an archive that survived intact


def _prefix_folds_in_raw(job_id, raw_dir):
    """The pre-fix download, re-created: `open(local, "wb")` truncates.

    A re-creation of the one line that changed, not the historical source --
    its only job is to be code the assertion above demonstrably fails
    against, so that assertion is not vacuous. (The same scenario was run
    once against the real pre-fix blob from git and behaved identically.)
    """
    import modal

    os.makedirs(raw_dir, exist_ok=True)
    local = os.path.join(raw_dir, f"{job_id}.tgz")
    try:
        vol = modal.Volume.from_name("ranomics-boltz2-raw")
        with open(local, "wb") as fh:
            for chunk in vol.read_file(f"{job_id}.tgz"):
                fh.write(chunk)
        with tarfile.open(local, "r:gz") as tf:
            under_out = [n for n in tf.getnames() if "/out/" in n]
    except Exception:  # noqa: BLE001 -- mirrors the shipped signature
        return 0, 0, None
    return sum(n.endswith(".pdb") for n in under_out), len(under_out), local


def test_negative_control_truncate_before_read_destroys_the_archive(
    monkeypatch, tmp_path
):
    rec = _install_modal(monkeypatch, "ok")
    first, second, before, after = _refetch_scenario(
        _prefix_folds_in_raw, rec, tmp_path
    )
    assert first[0] == 1, first          # same starting point as the test above
    assert second == (0, 0, None)        # scores the same 0 ...
    assert after != before               # ... but has eaten the evidence


def test_nothing_leaks_into_the_rest_of_the_suite():
    """Runs last, so it sees whatever every test above it left behind.

    Measured before the fixture existed: `gate1_raw` survived the file.

    Selected on its own it passes vacuously -- there are no teardowns ahead
    of it to observe. It guards only in a whole-file or whole-suite run,
    which is how CI invokes it.
    """
    assert not [n for n in ("gate1_raw", "gate1_boltz2_smoke") if n in sys.modules]
    # `modal` is not checked for absence: another test file may legitimately
    # have imported the real one, and monkeypatch restoring THAT is correct.
    # What must never survive is a stub of ours.
    assert sys.modules.get("modal") not in _STUBS
