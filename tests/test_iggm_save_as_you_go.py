"""iggm uploads and streams each design as design.py writes it.

Before this, the tool made one ``subprocess.run`` of ``design.py`` and only
then globbed its output, so every design reached Storage and the live page in
one burst after the run, even though IgGM had already written finished samples
to disk long before.

What streaming buys, and what it does not:

* The live page shows each design as it lands.
* A container kill DURING THE GPU PHASE can now be recovered as succeeded,
  where before it had its hold released. The sweeps leave
  ``designs_completed == designs_total`` in the snapshot
  ``shared/job_recovery.py::_completion_signal`` reads. At base nothing beat
  during that phase at all -- the last beat before ``run_iggm`` carries
  ``designs_completed`` 0 -- so a kill there always read incomplete. The
  post-run upload loop did already send real counts, so a kill in that short
  window could already read complete; what changes is that the snapshot now
  reaches N/N while ``design.py`` is still running. That the GPU phase is
  where a Modal timeout lands is an inference from it being nearly the whole
  wall clock; nothing in this repo records where real iggm timeouts fell.
  Pinned below by
  ``test_the_last_mid_run_beat_reads_as_complete``.
* A run that produces fewer designs than planned reads incomplete while it
  runs and complete once the final sweep has measured it, because only that
  last sweep knows the real total. Pinned by
  ``test_a_short_run_reads_complete_only_once_it_is_measured``.
* A non-zero ``design.py`` exit delivers the designs the sweeps already
  uploaded, on a run that is refunded in full. The RESULT carries none of them,
  but ``blueprints/jobs.py::job_status`` returns ``inputs._partial_candidates``
  with no terminal-status guard and ``job_candidate_pdb`` serves the bytes on
  ownership alone, while bucket "run" is unmapped in
  ``shared/jobs.py::_ERROR_BUCKET_TO_FAILURE_CLASS`` and so refunds. Whether a
  refunded run should deliver them is a money question open with Leo; see the
  comment on that branch in tools/iggm/run_pipeline.py.

Runs fully offline: ``subprocess.Popen`` and ``requests.post`` are patched on
the pipeline module and ``upload_one`` is a stub, so no design.py, no GPU and
no network. The sweep's own file handling (glob, stat, the size gate) is real
against ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from shared.job_recovery import _completion_signal

from tools.iggm import run_pipeline
from tools.iggm.run_pipeline import (
    _parse_pdb_chains,
    new_design_stream,
    run_iggm,
    sweep_designs,
)

_WEBHOOK = "https://hub.example.com/webhooks/modal"


@pytest.fixture
def beats(monkeypatch):
    """Collect every heartbeat body send_heartbeat would POST."""
    sent: list[dict] = []

    class _Resp:
        status_code = 200

    def _post(url, json=None, **kw):
        sent.append(json)
        return _Resp()

    monkeypatch.setattr(run_pipeline.requests, "post", _post)
    return sent


def _atom(serial: int, chain: str, resseq: int) -> str:
    """One ATOM line in the columns ``_parse_pdb_chains`` reads: chain id at 21,
    resSeq 22:26, coordinates 30:54, element 76:78."""
    line = (
        "ATOM  " + f"{serial:>5}" + " " + " CA  ALA "
        + chain + f"{resseq:>4}" + " " + "   "
        + f"{11.104:>8.3f}{13.207:>8.3f}{10.0:>8.3f}"
        + "  1.00 20.00" + " " * 10 + "C"
    )
    assert line[21] == chain, line
    assert line[76:78].strip() == "C", line
    return line


def _pdb(chains: str = "AB", residues: int = 2) -> str:
    """A parseable complex. The default fixture has to be a real multi-chain PDB
    because a mid-run take now parses it (``_looks_complete``)."""
    lines, serial = [], 1
    for ch in chains:
        for r in range(1, residues + 1):
            lines.append(_atom(serial, ch, r))
            serial += 1
    return "\n".join(lines) + "\n"


def _write(path, body=None):
    path.write_text(_pdb() if body is None else body)
    return path


def _taker(calls, *, fail=(), raise_on=()):
    """An ``upload_one`` stub recording (name, rank) and honouring two failure
    modes: ``fail`` returns None (the upload failed), ``raise_on`` raises."""

    def upload_one(pdb_path, rank):
        calls.append((pdb_path.name, rank))
        if pdb_path.name in raise_on:
            raise OSError("truncated")
        if pdb_path.name in fail:
            return None
        return {
            "rank": rank,
            "name": f"{rank:03d}_{pdb_path.stem}",
            "pdb_key": f"{rank:03d}_{pdb_path.stem}.pdb",
        }

    return upload_one


def _sweep(out_dir, stream, upload_one, *, require_stable=True, total=3, chains=2):
    sweep_designs(
        out_dir, stream, upload_one,
        require_stable=require_stable,
        webhook_url=_WEBHOOK, job_id="job1", planned_total=total,
        expected_chains=chains,
    )


def _signal(beats):
    """The recovery verdict the last beat's progress snapshot would produce."""
    snapshot = {
        "designs_completed": beats[-1]["designs_completed"],
        "designs_total": beats[-1]["designs_total"],
    }
    return _completion_signal(SimpleNamespace(inputs={"_progress": snapshot}))


# ---------------------------------------------------------------------------
# the size-stability gate
# ---------------------------------------------------------------------------


def test_a_growing_file_is_not_taken_until_its_size_holds(tmp_path, beats):
    """Upstream writes the PDB straight to its final path, so a mid-run sweep
    can see one half-written. Nothing is uploaded until the size repeats."""
    out = tmp_path / "out"
    out.mkdir()
    pdb = _write(out / "design_0.pdb", _pdb("AB", 1))
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []
    upload_one = _taker(calls)

    _sweep(out, stream, upload_one)
    assert calls == [], "first sight of a file is never enough"

    _write(pdb, _pdb("AB", 2))  # still being written: more bytes than last sweep
    _sweep(out, stream, upload_one)
    assert calls == [], "the size changed, so it is still not stable"

    _sweep(out, stream, upload_one)
    assert calls == [("design_0.pdb", 0)]
    assert len(stream["designs"]) == 1


def test_an_empty_file_is_never_taken(tmp_path, beats):
    """The final sweep is the case that needs its own assertion: it skips the
    chain gate, so the size check is the only thing standing between a
    zero-byte file and an uploaded design."""
    out = tmp_path / "out"
    out.mkdir()
    _write(out / "design_0.pdb", "")
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []
    for _ in range(3):
        _sweep(out, stream, _taker(calls))
    assert calls == []

    _sweep(out, stream, _taker(calls), require_stable=False)
    assert calls == []


def test_the_final_sweep_takes_a_new_file_at_once(tmp_path, beats):
    """design.py has exited by then, so nothing is still being written."""
    out = tmp_path / "out"
    out.mkdir()
    _write(out / "design_0.pdb")
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []
    _sweep(out, stream, _taker(calls), require_stable=False)
    assert calls == [("design_0.pdb", 0)]


# ---------------------------------------------------------------------------
# keys and ranks
# ---------------------------------------------------------------------------


def test_a_design_is_uploaded_once_however_many_sweeps_pass(tmp_path, beats):
    """The minting side passes no upsert option (presigned_output_put_url in
    shared/storage.py), so re-signing a key already in the bucket should be
    refused -- that refusal is read from the minting code, not observed against
    a live bucket. What this test pins is the half that does not depend on it:
    the sweep never attempts the second upload."""
    out = tmp_path / "out"
    out.mkdir()
    _write(out / "design_0.pdb")
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []
    upload_one = _taker(calls)
    for _ in range(4):
        _sweep(out, stream, upload_one)
    assert calls == [("design_0.pdb", 0)]
    assert len(beats) == 1


def test_ranks_follow_completion_order_not_sample_order(tmp_path, beats):
    """Upstream shuffles its sample queue, so files appear in random order.
    The design that lands first takes rank 0 whatever its sample index. Within
    one sweep there is no completion order to follow -- see the tie test."""
    out = tmp_path / "out"
    out.mkdir()
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []
    upload_one = _taker(calls)

    _write(out / "design_5.pdb")
    _sweep(out, stream, upload_one)
    _sweep(out, stream, upload_one)

    _write(out / "design_0.pdb")
    _sweep(out, stream, upload_one)
    _sweep(out, stream, upload_one)

    assert calls == [("design_5.pdb", 0), ("design_0.pdb", 1)]
    assert [d["pdb_key"] for d in stream["designs"]] == [
        "000_design_5.pdb", "001_design_0.pdb",
    ]


def test_designs_stable_in_the_same_sweep_are_ranked_by_path(tmp_path, beats):
    """The sweep loops ``collect_design_pdbs``, which sorts, so completion
    order only resolves designs to within one poll interval. Two that go
    stable together are ranked by path, not by which finished first."""
    out = tmp_path / "out"
    out.mkdir()
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []

    _write(out / "design_9.pdb")
    _write(out / "design_2.pdb")
    _sweep(out, stream, _taker(calls), require_stable=False)

    assert calls == [("design_2.pdb", 0), ("design_9.pdb", 1)]


def test_the_last_mid_run_beat_reads_as_complete(tmp_path, beats):
    """The money consequence of streaming. Once every planned design has been
    uploaded, the progress snapshot the recovery path reads says "complete", so
    a container kill after that point can be finalized succeeded and billed
    instead of having its hold released. Short of that it still says
    "incomplete"."""
    out = tmp_path / "out"
    out.mkdir()
    stream = new_design_stream()
    for i in range(3):
        _write(out / f"design_{i}.pdb")
        _sweep(out, stream, _taker([]))  # first sight: records the size
        _sweep(out, stream, _taker([]))  # size held: takes it
        expected = "complete" if i == 2 else "incomplete"
        assert _signal(beats) == expected, beats[-1]


def test_a_failed_upload_spends_its_rank_and_the_run_goes_on(tmp_path, beats):
    """The failed design's key may already exist in the bucket, so the next
    design must not be offered that rank."""
    out = tmp_path / "out"
    out.mkdir()
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []
    upload_one = _taker(calls, fail=("design_0.pdb",))

    _write(out / "design_0.pdb")
    _sweep(out, stream, upload_one)
    _sweep(out, stream, upload_one)
    _write(out / "design_1.pdb")
    _sweep(out, stream, upload_one)
    _sweep(out, stream, upload_one)

    assert calls == [("design_0.pdb", 0), ("design_1.pdb", 1)]
    assert [d["rank"] for d in stream["designs"]] == [1]
    assert len(beats) == 1, "only a delivered design is streamed"


def test_a_design_that_cannot_be_read_is_left_for_a_later_sweep(tmp_path, beats):
    """No upload was attempted, so no key is spent and the rank is reoffered."""
    out = tmp_path / "out"
    out.mkdir()
    _write(out / "design_0.pdb")
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []

    _sweep(out, stream, _taker(calls, raise_on=("design_0.pdb",)))
    _sweep(out, stream, _taker(calls, raise_on=("design_0.pdb",)))
    assert calls == [("design_0.pdb", 0)]
    assert stream["designs"] == []

    _sweep(out, stream, _taker(calls))
    assert calls[-1] == ("design_0.pdb", 0)
    assert [d["rank"] for d in stream["designs"]] == [0]


# ---------------------------------------------------------------------------
# what reaches the live page
# ---------------------------------------------------------------------------


def test_each_design_is_streamed_flat_with_an_int_rank(tmp_path, beats):
    out = tmp_path / "out"
    out.mkdir()
    _write(out / "design_0.pdb")
    stream = new_design_stream()
    _sweep(out, stream, _taker([]), total=7)  # first sight: records the size
    _sweep(out, stream, _taker([]), total=7)  # size held: takes it

    assert len(beats) == 1, "the first sweep takes nothing, so it beats nothing"
    body = beats[0]
    assert body["job_id"] == "job1"
    assert body["stage"] == "designing"
    assert body["designs_completed"] == 1
    assert body["designs_total"] == 7
    cand = body["new_candidate"]
    assert isinstance(cand["rank"], int)
    assert cand["pdb_key"] == "000_design_0.pdb"
    assert not any(isinstance(v, dict) for v in cand.values())


# ---------------------------------------------------------------------------
# the poll hook that makes it mid-run
# ---------------------------------------------------------------------------


class _FakeProc:
    """Alive for ``alive_polls`` waits, then exits with ``rc``."""

    def __init__(self, rc=0, alive_polls=2):
        self.rc = rc
        self.alive_polls = alive_polls
        self.waits = 0

    def wait(self, timeout=None):
        self.waits += 1
        if self.waits <= self.alive_polls:
            raise subprocess.TimeoutExpired(cmd="design.py", timeout=timeout)
        return self.rc


@pytest.fixture
def popen(monkeypatch):
    """Patch Popen to return the fake process the test hands back."""

    def _factory(proc):
        monkeypatch.setattr(
            run_pipeline.subprocess, "Popen", lambda *a, **kw: proc
        )
        return proc

    return _factory


def _run(tmp_path, on_poll):
    return run_iggm(
        tmp_path / "design.fasta", tmp_path / "antigen.pdb", tmp_path / "out",
        "design", [], 2000, 2, None,
        on_poll=on_poll, poll_seconds=0.0,
    )


def test_the_sweep_runs_while_design_py_is_still_alive(tmp_path, popen):
    proc = popen(_FakeProc(rc=0, alive_polls=3))
    polls: list[int] = []
    rc = _run(tmp_path, lambda: polls.append(1))
    assert rc == 0
    assert len(polls) == 3, "one sweep per poll, before the child exits"
    assert proc.waits == 4


def test_a_sweep_that_raises_does_not_kill_the_run(tmp_path, popen):
    popen(_FakeProc(rc=0, alive_polls=2))

    def boom():
        raise RuntimeError("storage down")

    assert _run(tmp_path, boom) == 0


def test_the_child_exit_code_is_still_returned(tmp_path, popen):
    popen(_FakeProc(rc=3, alive_polls=1))
    assert _run(tmp_path, lambda: None) == 3


def test_no_poll_hook_still_runs(tmp_path, popen):
    popen(_FakeProc(rc=0, alive_polls=2))
    assert _run(tmp_path, None) == 0


def test_the_fixture_is_a_parseable_two_chain_pdb():
    """Everything below rests on this: the default body has to parse, or the
    mid-run gate would reject it and the size-gate tests would pass vacuously."""
    chains = _parse_pdb_chains(_pdb())
    assert sorted(chains) == ["A", "B"]
    assert [len(chains[c]) for c in sorted(chains)] == [2, 2]
    assert len(_parse_pdb_chains(_pdb("A"))) == 1


def test_a_one_chain_file_is_not_taken_mid_run(tmp_path, beats):
    """A repeated size is not proof the file is finished. Upstream writes one
    chunk per chain, so a stalled flush can leave a complete FIRST chain and
    nothing after it, and epitope_contacts answers that shape with n_contacted
    0 instead of raising -- a design delivered asserting zero epitope contacts.
    A mid-run sweep requires the complex."""
    out = tmp_path / "out"
    out.mkdir()
    _write(out / "design_0.pdb", _pdb("A"))
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []

    for _ in range(4):
        _sweep(out, stream, _taker(calls))

    assert calls == [], "a stable size is not a finished complex"
    assert stream["next_rank"] == 0, "and no rank was spent on it"


def test_the_final_sweep_still_takes_a_one_chain_file(tmp_path, beats):
    """Delivery must not regress. Once design.py has exited, whatever is on
    disk is what IgGM produced, so the final sweep takes it even if it parses
    to a single chain."""
    out = tmp_path / "out"
    out.mkdir()
    _write(out / "design_0.pdb", _pdb("A"))
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []

    _sweep(out, stream, _taker(calls), require_stable=False)

    assert calls == [("design_0.pdb", 0)]


def test_a_short_run_reads_complete_only_once_it_is_measured(tmp_path, beats):
    """design.py can exit 0 having produced fewer designs than planned. Mid-run
    beats carry the plan, so such a run reads incomplete while it runs -- the
    conservative direction, since billing follows this snapshot. The final
    sweep measures the directory and beats that, so the window between it and
    the closing heartbeat is read the way it was before streaming, when every
    beat carried the measured total. The last call below still passes a plan of
    3 and is expected to ignore it."""
    out = tmp_path / "out"
    out.mkdir()
    for i in range(2):
        _write(out / f"design_{i}.pdb")
    stream = new_design_stream()

    _sweep(out, stream, _taker([]), require_stable=True, total=3)
    _sweep(out, stream, _taker([]), require_stable=True, total=3)
    assert (beats[-1]["designs_completed"], beats[-1]["designs_total"]) == (2, 3)
    assert _signal(beats) == "incomplete", "a plan of 3 is not met by 2 designs"

    # The SAME stream, as in production: every design is already taken, so the
    # final sweep's loop body never runs. Handing it a fresh stream here would
    # hide that, because then the loop would take both files and beat.
    _sweep(out, stream, _taker([]), require_stable=False, total=3)
    assert (beats[-1]["designs_completed"], beats[-1]["designs_total"]) == (2, 2)
    assert _signal(beats) == "complete", "the final sweep measures what exists"


def test_an_hl_run_is_not_taken_without_its_antigen(tmp_path, beats):
    """The H+L shape review-code found. Upstream folds H:L:A with the antigen
    LAST, so a stall after the second chunk leaves H+L on disk: a stable size,
    two parseable chains, and no antigen at all. Gating on >= 2 took it, and
    epitope_contacts answers an antibody-only file with a fabricated contact
    count rather than raising. The gate is the submitted chain count."""
    out = tmp_path / "out"
    out.mkdir()
    _write(out / "design_0.pdb", _pdb("HL"))
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []

    for _ in range(4):
        _sweep(out, stream, _taker(calls), chains=3)

    assert calls == [], "H+L with no antigen is not a finished complex"
    assert stream["next_rank"] == 0, "and no rank was spent on it"

    # Same file, nanobody run: two chains IS the whole complex there. One
    # stream across both sweeps, or the size-stability check never sees a
    # repeat and the take is refused for that reason instead of the gate.
    nano = new_design_stream()
    _sweep(out, nano, _taker(calls), chains=2)
    _sweep(out, nano, _taker(calls), chains=2)
    assert calls == [("design_0.pdb", 0)], "a 2-chain H:A complex still streams"


def test_the_final_sweep_beats_even_when_it_takes_nothing(tmp_path, beats):
    """A short run drained mid-run. The final sweep's loop body never runs, so
    before this it beat nothing and the snapshot kept the PLAN -- incomplete,
    and shared/job_recovery.py::recover_stuck_job_result discards the designs
    already in Storage. The measured total has to be beaten unconditionally."""
    out = tmp_path / "out"
    out.mkdir()
    _write(out / "design_0.pdb")
    stream = new_design_stream()

    _sweep(out, stream, _taker([]), total=3)
    _sweep(out, stream, _taker([]), total=3)
    assert _signal(beats) == "incomplete", "1 of a planned 3 is incomplete"
    taken_mid_run = len(beats)

    _sweep(out, stream, _taker([]), require_stable=False, total=3)

    assert len(beats) == taken_mid_run + 1, "the final sweep beat nothing"
    assert "new_candidate" not in beats[-1], "and re-delivered no design"
    assert (beats[-1]["designs_completed"], beats[-1]["designs_total"]) == (1, 1)
    assert _signal(beats) == "complete", "1 of a measured 1 is complete"


def test_an_unreadable_design_is_dropped_by_the_final_sweep(tmp_path, beats):
    """The other half of test_a_design_that_cannot_be_read_is_left_for_a_later_
    sweep: on the FINAL sweep there is no later sweep, so an undecodable design
    is dropped and the run still completes. At base this file failed the whole
    run -- read_text and epitope_contacts sat outside the upload's try -- so one
    corrupt output flipped a run from refunded to billed with fewer designs. The
    gap is recorded: the final beat's total counts the file that was dropped."""
    out = tmp_path / "out"
    out.mkdir()
    _write(out / "design_0.pdb")
    _write(out / "design_1.pdb")
    stream = new_design_stream()
    calls: list[tuple[str, int]] = []

    _sweep(out, stream, _taker(calls, raise_on=("design_1.pdb",)),
           require_stable=False, total=5)

    assert [d["name"] for d in stream["designs"]] == ["000_design_0"]
    assert (beats[-1]["designs_completed"], beats[-1]["designs_total"]) == (1, 2)
    assert _signal(beats) == "incomplete", "one of two globbed designs landed"
