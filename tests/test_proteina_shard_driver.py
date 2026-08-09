"""Tests for the multi-shard length-sweep driver.

Nothing here contacts Modal. ``run_campaign`` takes its ``Function`` off the
``modal`` module at call time, so a fake with ``spawn``/``get`` drives the
WHOLE money loop offline — the resume state machine, the reconnect rule, the
budget stop and the one-GPU guarantee included. An earlier revision tested only
the pure helpers, and three mutations that broke the campaign's core
invariants (append the ledger AFTER the wait; resubmit instead of reconnect;
drop the budget lookahead) all passed 25/25.
"""

import base64
import csv
import json
import pathlib
import types

import pytest

from tools.proteina import shard_driver as sd
from tools.proteina.direct_call_fc import build_job_spec, build_payload


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

def _pdb(n_ca: int, chain: str = "C") -> bytes:
    """A target CA on chain A plus ``n_ca`` binder CAs on ``chain``."""
    lines = ["ATOM      1  CA  GLY A 100      0.000   0.000   0.000"]
    lines += [f"ATOM  {i + 2:>5}  CA  ALA {chain} {i + 1:>3}"
              f"      0.000   0.000   0.000" for i in range(n_ca)]
    return ("\n".join(lines) + "\nEND\n").encode()


def _result(lengths=(55, 56), *, status="COMPLETED", exit_code=0, ranks=None):
    ranks = ranks if ranks is not None else list(range(1, len(lengths) + 1))
    cands = [
        {"rank": r, "name": f"bon_orig{i}_r0", "pdb_key": f"design_{i:03d}.pdb",
         "scores": {"total_reward": -0.3 - i, "af2_iptm": 0.7, "af2_plddt": 0.8,
                    "binder_scrmsd": 1.2, "cluster_id": None},
         "pdb_content_b64": base64.b64encode(_pdb(n)).decode()}
        for i, (n, r) in enumerate(zip(lengths, ranks))
    ]
    return {"exit_code": exit_code,
            "smoke_result": {"status": status, "runtime_seconds": 5000,
                             "designs_total": len(cands),
                             "designs_completed": len(cands),
                             "candidates": cands}}


class FakeCall:
    def __init__(self, object_id, result=None, exc=None, on_get=None):
        self.object_id = object_id
        self._result, self._exc, self._on_get = result, exc, on_get
        self.gets = 0

    def get(self, timeout=None):
        self.gets += 1
        if self._on_get is not None:
            self._on_get(self)
        if self._exc is not None:
            raise self._exc
        return self._result if self._result is not None else _result()


class FakeFn:
    """Records every spawn. ``script`` maps a spawn ordinal to a FakeCall."""

    def __init__(self, script=None):
        self.spawns: list[dict] = []
        self._script = script

    def spawn(self, payload):
        i = len(self.spawns)
        self.spawns.append(payload)
        if self._script is None:
            return FakeCall(f"fc-fake-{i}")
        return self._script(i)


def _install(monkeypatch, fn, from_id=None):
    """Point modal.Function/FunctionCall at the fakes. modal.exception stays
    REAL, because _collect discriminates on modal's own timeout hierarchy and
    a fake would not reproduce that."""
    import modal
    monkeypatch.setattr(
        modal, "Function", types.SimpleNamespace(from_name=lambda a, b: fn))
    monkeypatch.setattr(
        modal, "FunctionCall",
        types.SimpleNamespace(
            from_id=from_id or (lambda cid: FakeCall(cid))))


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    """A 5-shard campaign (one round of the five bins) wired to fakes."""
    monkeypatch.setattr(sd, "SHARDS_PER_BIN", 1)
    monkeypatch.setattr(sd, "_stage_target",
                        lambda job_id, target: "https://fake/presigned")
    target = tmp_path / "target.pdb"
    target.write_bytes(_pdb(10, chain="A"))
    outdir = tmp_path / "out"

    monkeypatch.setenv("PROTEINA_DRIVER_LOCK", str(tmp_path / "driver.lock"))

    def make_args(**over):
        argv = ["--run", "--yes", "--outdir", str(outdir),
                "--target", str(target), "--budget", str(over.pop("budget", 999.0)),
                "--timeout", str(over.pop("timeout", 9000))]
        return sd.build_parser().parse_args(argv)

    return types.SimpleNamespace(outdir=outdir, make_args=make_args,
                                 ledger=outdir / "ledger.jsonl")


# --------------------------------------------------------------------------
# the money loop
# --------------------------------------------------------------------------

class TestTheCallIdIsDurableBeforeTheWait:
    """The single property the module is built around."""

    def test_the_submitted_record_exists_before_get_is_called(
            self, campaign, monkeypatch):
        seen = []

        def on_get(call):
            # Read the ledger from DISK at the moment of the wait: if the
            # append happened after, this shard is not in it yet.
            state = sd.ledger_replay(campaign.ledger)
            seen.append([(i, r.get("state"), r.get("call_id"))
                         for i, r in state.items()
                         if r.get("call_id") == call.object_id])

        fn = FakeFn(lambda i: FakeCall(f"fc-{i}", on_get=on_get))
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0
        assert len(seen) == 5
        for entry in seen:
            assert entry and entry[0][1] == "submitted", (
                "the call id was not durably recorded before the driver "
                "started waiting on it; a crash here orphans a paid run")


class TestResumeNeverDoubleCharges:

    def test_an_in_flight_shard_is_reconnected_not_respawned(
            self, campaign, monkeypatch):
        campaign.outdir.mkdir(parents=True)
        sd.ledger_append(campaign.ledger, {
            "index": 0, "round": 0, "bin": list(sd.BINS[0]),
            "state": "submitted", "job_id": "j0", "call_id": "fc-PRIOR"})

        reconnected = []
        fn = FakeFn()
        _install(monkeypatch, fn,
                 from_id=lambda cid: (reconnected.append(cid)
                                      or FakeCall(cid)))
        assert sd.run_campaign(campaign.make_args()) == 0
        assert reconnected == ["fc-PRIOR"], "the in-flight call was not reused"
        assert len(fn.spawns) == 4, (
            "shard 0 was resubmitted despite already being paid for")

    def test_terminal_shards_are_skipped(self, campaign, monkeypatch):
        campaign.outdir.mkdir(parents=True)
        for i, st in enumerate(("collected", "empty", "failed",
                                "harvest_error")):
            sd.ledger_append(campaign.ledger, {
                "index": i, "round": 0, "bin": list(sd.BINS[i]),
                "state": st})
        fn = FakeFn()
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0
        assert len(fn.spawns) == 1, "a finished shard was re-run"

    def test_an_intent_without_a_call_id_stops_the_resume(
            self, campaign, monkeypatch):
        """The one hole write-before-wait cannot close: a container may exist
        and its id is gone. Guessing either double-charges or leaves two A100s
        running."""
        campaign.outdir.mkdir(parents=True)
        sd.ledger_append(campaign.ledger, {
            "index": 0, "round": 0, "bin": list(sd.BINS[0]),
            "state": "intent", "job_id": "j-orphan"})
        fn = FakeFn()
        _install(monkeypatch, fn)
        with pytest.raises(SystemExit, match="j-orphan"):
            sd.run_campaign(campaign.make_args())
        assert fn.spawns == [], "spawned despite an unresolved container"


class TestATimeoutIsNotAFailure:
    """A timed-out container may still be RUNNING and billing. Writing it off
    as failed abandons paid work AND puts a second A100 in flight."""

    @pytest.mark.parametrize("exc", [
        "modal_timeout", "modal_function_timeout", "builtin_timeout"])
    def test_the_campaign_stops_and_the_shard_stays_submitted(
            self, campaign, monkeypatch, exc):
        import modal.exception
        raised = {
            "modal_timeout": modal.exception.TimeoutError("t"),
            # NOTE: modal's TimeoutError does NOT subclass the builtin, so a
            # driver catching only `TimeoutError` misses every Modal timeout.
            "modal_function_timeout": modal.exception.FunctionTimeoutError("t"),
            "builtin_timeout": TimeoutError("t"),
        }[exc]
        fn = FakeFn(lambda i: FakeCall(f"fc-{i}", exc=raised))
        _install(monkeypatch, fn)

        assert sd.run_campaign(campaign.make_args()) == 4
        assert len(fn.spawns) == 1, (
            "a second shard was spawned while a timed-out container may still "
            "hold the GPU")
        assert sd.ledger_replay(campaign.ledger)[0]["state"] == "submitted", (
            "a live container was written off; its designs are now unreachable")

    def test_a_timeout_on_the_RECONNECT_path_also_stops(
            self, campaign, monkeypatch):
        """The branch a resume actually takes. A stuck container reached by
        reconnect is exactly as live as one reached by spawn."""
        import modal.exception
        campaign.outdir.mkdir(parents=True)
        sd.ledger_append(campaign.ledger, {
            "index": 0, "bin": list(sd.BINS[0]), "state": "submitted",
            "job_id": "j0", "call_id": "fc-STUCK"})
        fn = FakeFn()
        _install(monkeypatch, fn, from_id=lambda cid: FakeCall(
            cid, exc=modal.exception.FunctionTimeoutError("t")))
        assert sd.run_campaign(campaign.make_args()) == 4
        assert fn.spawns == [], (
            "spawned beside a container that is still running")
        assert sd.ledger_replay(campaign.ledger)[0]["state"] == "submitted"

    def test_the_shard_is_reconnected_on_the_next_run(
            self, campaign, monkeypatch):
        import modal.exception
        fn = FakeFn(lambda i: FakeCall(
            f"fc-{i}", exc=modal.exception.FunctionTimeoutError("t")))
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 4

        fn2 = FakeFn()
        _install(monkeypatch, fn2, from_id=lambda cid: FakeCall(cid))
        assert sd.run_campaign(campaign.make_args()) == 0
        assert len(fn2.spawns) == 4, "the timed-out shard was respawned"

    def test_a_real_error_IS_terminal_and_the_run_continues(
            self, campaign, monkeypatch):
        fn = FakeFn(lambda i: FakeCall(f"fc-{i}",
                                       exc=RuntimeError("boom") if i == 0
                                       else None))
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0
        assert len(fn.spawns) == 5, "one bad shard ended the campaign"
        assert sd.ledger_replay(campaign.ledger)[0]["state"] == "failed"


class TestEveryUnresumableLedgerStateStops:
    """The loop is skip-terminal / reconnect-submitted / ELSE SPAWN, and this
    module's own recovery message tells operators to hand-edit the ledger. An
    earlier revision guarded only `intent`, so a hand-edit that dropped the
    call id — or any typo — fell through to a fresh spawn beside a container
    that may still be running."""

    @pytest.mark.parametrize("rec, needle", [
        ({"state": "submitted", "job_id": "j0"}, "submitted"),
        ({"state": "colected", "call_id": "fc-PAID"}, "colected"),
        ({"state": "intent", "job_id": "j-orphan"}, "j-orphan"),
        ({"state": "running", "call_id": "fc-X"}, "running"),
    ])
    def test_it_refuses_rather_than_spawning(self, campaign, monkeypatch,
                                             rec, needle):
        campaign.outdir.mkdir(parents=True)
        sd.ledger_append(campaign.ledger,
                         {"index": 0, "bin": list(sd.BINS[0]), **rec})
        fn = FakeFn()
        _install(monkeypatch, fn)
        with pytest.raises(SystemExit, match=needle):
            sd.run_campaign(campaign.make_args())
        assert fn.spawns == [], (
            f"state {rec['state']!r} fell through to a spawn; if a container "
            "is live that is two A100s")

    def test_a_submitted_WITH_a_call_id_is_still_resumable(
            self, campaign, monkeypatch):
        campaign.outdir.mkdir(parents=True)
        sd.ledger_append(campaign.ledger, {
            "index": 0, "bin": list(sd.BINS[0]), "state": "submitted",
            "job_id": "j0", "call_id": "fc-OK"})
        fn = FakeFn()
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0


class TestTheIntentRecordCoversTheSpawnAndNothingElse:

    def test_a_staging_failure_leaves_no_orphan_intent(
            self, campaign, monkeypatch):
        """Staging is an S3 upload, fallible, run 80 times over 4.7 days.
        Writing intent before it meant one transient failure wedged the resume
        into a hard refusal for a container that was never created."""
        def boom(job_id, target):
            raise OSError("S3 timeout")

        monkeypatch.setattr(sd, "_stage_target", boom)
        fn = FakeFn()
        _install(monkeypatch, fn)
        with pytest.raises(OSError):
            sd.run_campaign(campaign.make_args())
        assert sd.ledger_replay(campaign.ledger) == {}, (
            "an intent was recorded for a container that never existed; the "
            "resume will hard-refuse and demand a hand-edit")

        # And the resume proceeds normally rather than refusing.
        monkeypatch.setattr(sd, "_stage_target",
                            lambda job_id, target: "https://fake/presigned")
        assert sd.run_campaign(campaign.make_args()) == 0

    def test_the_intent_precedes_the_spawn(self, campaign, monkeypatch):
        """It is the only trace that a container may exist."""
        seen = []

        def spy(payload):
            seen.append(sd.ledger_replay(campaign.ledger).get(
                len(seen), {}).get("state"))
            return FakeCall(f"fc-{len(seen)}")

        fn = FakeFn()
        fn.spawn = spy
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0
        assert seen and all(s == "intent" for s in seen), (
            f"states at spawn time were {seen}; a spawn with no prior intent "
            "record can strand a live container")


class TestEachShardsBinReachesItsPayload:
    """The campaign's entire premise. If binder_length silently fails to
    steer, every shard still returns 64 plausible designs."""

    def test_the_spawned_payloads_carry_the_planned_bins(
            self, campaign, monkeypatch):
        fn = FakeFn()
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0
        sent = [p["job_spec"]["binder_length"] for p in fn.spawns]
        assert sent == [list(b) for b in sd.BINS], (
            f"payload bins {sent} do not match the plan {sd.BINS}")

    def test_each_payload_still_selects_inline_delivery(
            self, campaign, monkeypatch):
        fn = FakeFn()
        _install(monkeypatch, fn)
        sd.run_campaign(campaign.make_args())
        for p in fn.spawns:
            assert "upload_urls_endpoint" not in p
            assert "job_token" not in p


class TestADerivedFileCannotKillTheCampaign:

    def test_a_failing_manifest_rebuild_is_logged_not_fatal(
            self, campaign, monkeypatch):
        """os.replace over a destination another process holds open raises
        PermissionError on Windows, so opening manifest.csv in Excel to check
        progress on day 2 used to stop the run — and each restart advanced
        exactly one shard."""
        def boom(outdir):
            raise PermissionError("[WinError 5] Access is denied")

        monkeypatch.setattr(sd, "rebuild_manifest", boom)
        fn = FakeFn()
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0
        assert len(fn.spawns) == 5, "a manifest rebuild ended the campaign"
        assert sd.ledger_replay(campaign.ledger)[0]["state"] == "collected"

    def test_the_shard_rows_survive_a_failed_rebuild(
            self, campaign, monkeypatch):
        monkeypatch.setattr(sd, "rebuild_manifest",
                            lambda outdir: (_ for _ in ()).throw(OSError("x")))
        fn = FakeFn()
        _install(monkeypatch, fn)
        sd.run_campaign(campaign.make_args())
        assert (campaign.outdir / "shard_000" / "rows.csv").is_file(), (
            "the per-shard rows are the durable copy; they must be written "
            "regardless of the derived manifest")


class TestTheBudgetActuallyStops:

    def test_it_refuses_to_start_a_shard_that_would_cross(
            self, campaign, monkeypatch):
        fn = FakeFn()
        _install(monkeypatch, fn)
        # Enough for two shards, not three.
        budget = sd.shard_usd_ceiling() * 2.5
        assert sd.run_campaign(campaign.make_args(budget=budget)) == 3
        assert len(fn.spawns) == 2, (
            f"spawned {len(fn.spawns)} shards against a {budget:.2f} ceiling")

    def test_a_zero_budget_spawns_nothing(self, campaign, monkeypatch):
        fn = FakeFn()
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args(budget=0.0)) == 3
        assert fn.spawns == []

    def test_the_ceiling_is_priced_above_the_estimate(self):
        """A guard priced at the optimistic end of its own uncertainty cannot
        fire: the measurement allows up to $2.958/hr and the plan is costed at
        $2.50/hr."""
        assert sd.shard_usd_ceiling() > sd.shard_usd()
        assert sd.USD_PER_SECOND_CEILING == pytest.approx(0.5528 / 673.0)

    def test_the_guard_uses_the_ceiling_at_the_POINT_OF_USE(
            self, campaign, monkeypatch):
        """Pinning the constant is not enough — the loop has to actually call
        it. A budget priced at the estimate stops one shard too late."""
        fn = FakeFn()
        _install(monkeypatch, fn)
        # Midway between "3 shards at the estimate" and "3 shards at the
        # ceiling": affords 3 of the cheap price but only 2 of the dear one,
        # so the two pricings give DIFFERENT answers. A budget that happens to
        # stop at the same shard either way proves nothing.
        budget = (3 * sd.shard_usd_ceiling() + 3 * sd.shard_usd()) / 2
        assert budget >= 3 * sd.shard_usd()
        assert budget < 3 * sd.shard_usd_ceiling()
        assert sd.run_campaign(campaign.make_args(budget=budget)) == 3
        assert len(fn.spawns) == 2, (
            f"{len(fn.spawns)} shards spawned against ${budget:.2f}; the guard "
            "is pricing at the optimistic estimate, not the ceiling")

    def test_a_reconnect_does_not_consume_fresh_budget(
            self, campaign, monkeypatch):
        """That money is already spent; refusing here would strand it."""
        campaign.outdir.mkdir(parents=True)
        sd.ledger_append(campaign.ledger, {
            "index": 0, "round": 0, "bin": list(sd.BINS[0]),
            "state": "submitted", "job_id": "j0", "call_id": "fc-PRIOR"})
        fn = FakeFn()
        _install(monkeypatch, fn)
        # Budget is already exhausted by the one submitted shard.
        rc = sd.run_campaign(campaign.make_args(
            budget=sd.shard_usd_ceiling() * 1.2))
        assert rc == 3, "expected the budget to stop the NEXT shard"
        assert sd.ledger_replay(campaign.ledger)[0]["state"] == "collected", (
            "the already-paid shard was not collected before stopping")


class TestOneDriverAtATime:

    def test_a_second_driver_is_refused(self, campaign, monkeypatch):
        with sd.DriverLock(sd.default_lock_path()):
            fn = FakeFn()
            _install(monkeypatch, fn)
            with pytest.raises(SystemExit, match="another driver"):
                sd.run_campaign(campaign.make_args())
            assert fn.spawns == [], "two drivers means two A100s"

    def test_the_lock_is_global_not_per_outdir(self, campaign, monkeypatch,
                                               tmp_path):
        """An outdir-scoped lock let two drivers with two --outdir values both
        spawn — and --outdir defaults to a RELATIVE path, so two shells in
        different working directories already evaded it."""
        with sd.DriverLock(sd.default_lock_path()):
            fn = FakeFn()
            _install(monkeypatch, fn)
            other = sd.build_parser().parse_args(
                ["--run", "--yes", "--outdir", str(tmp_path / "somewhere-else"),
                 "--target", str(tmp_path / "target.pdb")])
            with pytest.raises(SystemExit, match="another driver"):
                sd.run_campaign(other)
            assert fn.spawns == []

    def test_the_lock_path_is_not_inside_the_outdir(self, monkeypatch):
        monkeypatch.delenv("PROTEINA_DRIVER_LOCK", raising=False)
        assert sd.default_lock_path().parent == pathlib.Path.home()

    def test_the_lock_is_released_on_exit(self, campaign, monkeypatch):
        fn = FakeFn()
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0
        assert not sd.default_lock_path().exists()

    def test_the_lock_is_released_even_when_the_run_stops_early(
            self, campaign, monkeypatch):
        fn = FakeFn()
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args(budget=0.0)) == 3
        assert not sd.default_lock_path().exists(), (
            "an early return leaked the lock; the next run cannot start")

    def test_a_losing_acquire_does_not_delete_the_holders_lock(self, tmp_path):
        path = tmp_path / "l.lock"
        with sd.DriverLock(path):
            with pytest.raises(SystemExit):
                with sd.DriverLock(path):
                    pass
            assert path.exists(), "the loser removed the winner's lock"
        assert not path.exists()


class TestHarvestFailuresDoNotEndTheCampaign:

    def test_a_raising_harvest_is_recorded_and_the_run_goes_on(
            self, campaign, monkeypatch):
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("malformed candidate")
            return [], 0

        monkeypatch.setattr(sd, "_harvest", boom)
        fn = FakeFn()
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0
        assert len(fn.spawns) == 5, (
            "a harvest error ended the campaign; the resume would reconnect "
            "and re-crash on the same shard forever")
        assert sd.ledger_replay(campaign.ledger)[0]["state"] == "harvest_error"

    def test_harvest_error_is_terminal_so_a_resume_does_not_loop(
            self, campaign, monkeypatch):
        campaign.outdir.mkdir(parents=True)
        sd.ledger_append(campaign.ledger, {
            "index": 0, "round": 0, "bin": list(sd.BINS[0]),
            "state": "harvest_error"})
        fn = FakeFn()
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0
        assert len(fn.spawns) == 4

    def test_a_rank_of_None_does_not_raise(self, tmp_path):
        """`.get("rank", 0)` returns the None when the key is PRESENT and
        None, so the default never applies and the format spec raises. The
        fallback is the candidate's POSITION, not 0, so two malformed
        candidates cannot collide onto one filename."""
        out = {"exit_code": 0, "smoke_result": {"candidates": [
            {"rank": None, "name": "x", "scores": {},
             "pdb_content_b64": base64.b64encode(_pdb(55)).decode()}]}}
        rows, with_atoms = sd._harvest(
            out, {"index": 0, "round": 0, "bin": [50, 59]}, "j", "c", tmp_path)
        assert with_atoms == 1
        assert (tmp_path / "shard_000" / "design_001.pdb").is_file()
        assert rows[0]["rank"] is None, (
            "the manifest must report the candidate's real rank, even when "
            "the filename had to fall back to a position")

    def test_the_paid_result_is_persisted_before_candidates_are_parsed(
            self, tmp_path):
        """Row-building can raise; the diagnosis must survive it."""
        out = {"exit_code": 1, "smoke_result": {
            "status": "FAILED", "error": {"check": "target_conflict"},
            "candidates": [{"rank": 1, "scores": {},
                            "pdb_content_b64": "!!!not-base64!!!"}]}}
        with pytest.raises(Exception):
            sd._harvest(out, {"index": 4, "round": 0, "bin": [50, 59]},
                        "j", "c", tmp_path)
        saved = json.loads(
            (tmp_path / "shard_004" / "smoke_result.json").read_text())
        assert saved["error"]["check"] == "target_conflict"


class TestTheManifestCannotDoubleCount:

    def test_reharvesting_a_shard_does_not_duplicate_its_rows(
            self, campaign, monkeypatch):
        fn = FakeFn()
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0
        first = list(csv.DictReader(
            (campaign.outdir / "manifest.csv").open(encoding="utf-8")))

        # Simulate a crash between the CSV write and the ledger write: the
        # shard looks uncollected again, so the resume re-harvests it.
        sd.ledger_append(campaign.ledger, {
            "index": 0, "state": "submitted", "job_id": "j0",
            "call_id": "fc-again"})
        _install(monkeypatch, FakeFn(), from_id=lambda cid: FakeCall(cid))
        assert sd.run_campaign(campaign.make_args()) == 0

        again = list(csv.DictReader(
            (campaign.outdir / "manifest.csv").open(encoding="utf-8")))
        assert len(again) == len(first), (
            f"re-harvest duplicated rows: {len(first)} -> {len(again)}")

    def test_the_manifest_is_derived_from_the_shard_files(self, tmp_path):
        for i in (2, 0, 1):
            d = tmp_path / f"shard_{i:03d}"
            d.mkdir()
            sd._write_shard_rows(d, [{**{c: "" for c in sd.MANIFEST_COLUMNS},
                                      "shard_index": i}])
        assert sd.rebuild_manifest(tmp_path) == 3
        rows = list(csv.DictReader(
            (tmp_path / "manifest.csv").open(encoding="utf-8")))
        assert [r["shard_index"] for r in rows] == ["0", "1", "2"]

    def test_pdb_paths_are_posix(self, tmp_path):
        out = _result(lengths=(55,))
        rows, _ = sd._harvest(out, {"index": 1, "round": 0, "bin": [50, 59]},
                              "j", "c", tmp_path)
        assert "\\" not in rows[0]["pdb_file"]
        assert rows[0]["pdb_file"] == "shard_001/design_001.pdb"


class TestTheRequestedBinIsCheckedAgainstReality:
    """If binder_length silently fails to steer the model, every shard still
    returns 64 plausible designs and the only symptom is the lengths."""

    def test_lengths_inside_the_bin_pass(self):
        rows = [{"binder_length": n} for n in (50, 55, 59)]
        assert sd._check_lengths(rows, {"bin": [50, 59]}) == ""

    def test_lengths_that_ignore_the_bin_are_reported(self):
        rows = [{"binder_length": n} for n in (95, 101, 110)]
        msg = sd._check_lengths(rows, {"bin": [50, 59]})
        assert "outside the requested bin" in msg

    def test_it_is_recorded_on_the_ledger(self, campaign, monkeypatch):
        fn = FakeFn(lambda i: FakeCall(f"fc-{i}", result=_result((99, 100))))
        _install(monkeypatch, fn)
        sd.run_campaign(campaign.make_args())
        rec = sd.ledger_replay(campaign.ledger)[0]
        assert rec["state"] == "collected"
        assert rec["length_mismatch"], (
            "a shard whose designs ignored the requested bin was recorded "
            "as a clean success")

    def test_a_shard_with_no_coordinates_says_so(self):
        assert "no design carried coordinates" in sd._check_lengths(
            [{"binder_length": ""}], {"bin": [50, 59]})

    def test_even_ONE_design_outside_the_bin_is_reported(self):
        """A >50% rule let exactly half a shard sit 36 aa off target and still
        report clean. Upstream samples inside [lo, hi], so a correct shard has
        none outside."""
        rows = [{"binder_length": n} for n in (50, 51, 52, 95)]
        assert sd._check_lengths(rows, {"bin": [50, 59]})

    def test_exactly_half_outside_is_reported(self):
        rows = [{"binder_length": n} for n in (50, 51, 95, 96)]
        assert sd._check_lengths(rows, {"bin": [50, 59]})

    def test_an_all_capped_shard_is_still_checked(self, campaign, monkeypatch):
        """The branch runs from the money loop, not only from a unit test —
        an all-cap-dropped shard is exactly when nobody is looking."""
        out = {"exit_code": 0, "smoke_result": {
            "status": "COMPLETED", "candidates": [
                {"rank": 1, "name": "capped", "scores": {"total_reward": -1}}]}}
        fn = FakeFn(lambda i: FakeCall(f"fc-{i}", result=out))
        _install(monkeypatch, fn)
        sd.run_campaign(campaign.make_args())
        rec = sd.ledger_replay(campaign.ledger)[0]
        assert rec["state"] == "empty"
        assert rec["length_mismatch"], (
            "a shard that delivered no coordinates recorded a silent clean pass")


class TestRunRefusesWithoutConfirmation:

    def test_run_without_yes_refuses_and_names_the_cost(
            self, campaign, monkeypatch):
        """Fully isolated. An earlier version of this test drove main() with a
        real argv, no fakes and the DEFAULT relative --outdir, relying entirely
        on main()'s gate; when a mutation removed that gate it spawned against
        the real Modal Function and wrote a live ledger into the repo tree."""
        import sys as _sys
        fn = FakeFn()
        _install(monkeypatch, fn)
        monkeypatch.setattr(_sys, "argv", [
            "shard_driver.py", "--run", "--outdir", str(campaign.outdir)])
        with pytest.raises(SystemExit, match=r"\$"):
            sd.main()
        assert fn.spawns == []

    def test_run_campaign_ALSO_enforces_the_gate(self, campaign, monkeypatch):
        """Belt and braces: a spend gate with one enforcement point is one
        refactor away from not existing."""
        fn = FakeFn()
        _install(monkeypatch, fn)
        args = campaign.make_args()
        args.yes = False
        with pytest.raises(SystemExit, match="requires args.yes"):
            sd.run_campaign(args)
        assert fn.spawns == []

    def test_run_with_yes_proceeds(self, campaign, monkeypatch):
        """Guard the guard: a --yes that still refused would be silent."""
        fn = FakeFn()
        _install(monkeypatch, fn)
        assert sd.run_campaign(campaign.make_args()) == 0

    def test_dry_run_never_imports_modal(self, monkeypatch, capsys):
        args = sd.build_parser().parse_args(["--dry-run"])
        assert sd.cmd_dry_run(args) == 0
        assert "upload_urls_endpoint present: False" in capsys.readouterr().out


class TestTimeoutMustExceedTheContainerCeiling:

    def test_a_short_timeout_is_refused_before_spawning(
            self, campaign, monkeypatch):
        fn = FakeFn()
        _install(monkeypatch, fn)
        with pytest.raises(SystemExit, match="container ceiling"):
            sd.run_campaign(campaign.make_args(timeout=7200))
        assert fn.spawns == []

    def test_the_default_clears_it(self):
        args = sd.build_parser().parse_args([])
        assert args.timeout > sd.CONTAINER_CEILING_S


# --------------------------------------------------------------------------
# plan, ledger, cost
# --------------------------------------------------------------------------

class TestThePlanStaysBalanced:

    def test_every_prefix_is_balanced_to_within_one_shard(self):
        seen = {tuple(b): 0 for b in sd.BINS}
        for item in sd.build_plan():
            seen[tuple(item["bin"])] += 1
            counts = list(seen.values())
            assert max(counts) - min(counts) <= 1, (
                f"after {item['index'] + 1} shards the bins are {counts}; "
                "a bin-by-bin plan makes an interrupted run unanalysable")

    def test_the_plan_covers_every_bin_equally(self):
        plan = sd.build_plan()
        assert len(plan) == len(sd.BINS) * sd.SHARDS_PER_BIN
        for b in sd.BINS:
            assert sum(1 for i in plan if i["bin"] == list(b)) == sd.SHARDS_PER_BIN

    def test_index_is_stable_across_calls(self):
        assert sd.build_plan() == sd.build_plan()

    def test_the_defaults_are_read_at_call_time(self, monkeypatch):
        """Written as `per_bin=SHARDS_PER_BIN` the constant is captured once at
        import and every guard downstream reasons about a stale plan."""
        monkeypatch.setattr(sd, "SHARDS_PER_BIN", 3)
        assert len(sd.build_plan()) == 3 * len(sd.BINS)


class TestPlanValidationRefusesBeforeSpending:

    def test_a_bin_outside_the_validated_range_is_refused(self):
        for bad in ([10, 40], [50, 400]):
            with pytest.raises(SystemExit, match="20-300"):
                sd._validate_plan([{"index": 0, "round": 0, "bin": bad}])

    def test_an_inverted_bin_is_refused(self):
        with pytest.raises(SystemExit, match="lo > hi"):
            sd._validate_plan([{"index": 0, "round": 0, "bin": [90, 60]}])

    def test_overlapping_bins_are_refused(self):
        """Shared lengths get double the campaign weight, which makes the
        length comparison unsound."""
        with pytest.raises(SystemExit, match="overlap"):
            sd._validate_plan(sd.build_plan(
                bins=[(50, 60), (60, 70)], per_bin=1))

    def test_the_shipped_bins_are_disjoint(self):
        edges = sorted(sd.BINS)
        for (lo_a, hi_a), (lo_b, _) in zip(edges, edges[1:]):
            assert hi_a < lo_b, f"bins {(lo_a, hi_a)} and {(lo_b, _)} touch"

    def test_a_shard_that_would_outrun_the_deadline_is_refused(self, monkeypatch):
        monkeypatch.setattr(sd, "DESIGNS_PER_SHARD", 200)
        with pytest.raises(SystemExit, match="subprocess deadline"):
            sd._validate_plan(sd.build_plan())

    def test_the_overhead_is_subtracted_not_added(self, monkeypatch):
        """At 86 designs the PIPELINE fits under the deadline but the CONTAINER
        does not, so this configuration is legal only if the correction has the
        right sign. The shipped N and an absurd N both pass either way, which
        is why this case exists."""
        monkeypatch.setattr(sd, "DESIGNS_PER_SHARD", 86)
        pipeline = sd.shard_seconds(86) - sd.CONTAINER_OVERHEAD_S
        assert pipeline < sd.DESIGN_SUBPROCESS_DEADLINE_S
        assert sd.shard_seconds(86) + sd.CONTAINER_OVERHEAD_S > \
            sd.DESIGN_SUBPROCESS_DEADLINE_S
        sd._validate_plan(sd.build_plan())

    def test_the_deadline_is_imported_from_upstream_not_copied(self):
        from tools.proteina.run_pipeline import (
            DESIGN_SUBPROCESS_DEFAULT_TIMEOUT_S as upstream)
        assert sd.DESIGN_SUBPROCESS_DEADLINE_S is upstream

    def test_the_shipped_configuration_validates(self):
        sd._validate_plan(sd.build_plan())


class TestTheLedgerSurvivesACrash:

    def test_later_records_merge_onto_earlier_ones(self, tmp_path):
        led = tmp_path / "ledger.jsonl"
        sd.ledger_append(led, {"index": 0, "state": "submitted",
                               "call_id": "fc-1", "job_id": "j0"})
        sd.ledger_append(led, {"index": 0, "state": "collected", "designs": 64})
        rec = sd.ledger_replay(led)[0]
        assert rec["state"] == "collected"
        assert rec["call_id"] == "fc-1", (
            "the merge dropped the call id; a resume could not reconnect")

    def test_a_truncated_final_line_is_skipped_not_fatal(self, tmp_path):
        led = tmp_path / "ledger.jsonl"
        sd.ledger_append(led, {"index": 0, "state": "submitted",
                               "call_id": "fc-1"})
        with led.open("a", encoding="utf-8") as fh:
            fh.write('{"index": 1, "state": "sub')      # killed mid-write
        state = sd.ledger_replay(led)
        assert set(state) == {0}

    def test_a_missing_ledger_is_an_empty_campaign(self, tmp_path):
        assert sd.ledger_replay(tmp_path / "nope.jsonl") == {}

    @pytest.mark.parametrize("bad", ['{"index": "oops", "state": "failed"}',
                                     '{"index": null, "state": "failed"}',
                                     '{"state": "failed"}'])
    def test_an_unusable_index_is_skipped_not_a_traceback(self, tmp_path, bad):
        """This module TELLS operators to hand-edit the ledger, so a bad index
        is foreseeable input rather than an internal error."""
        led = tmp_path / "l.jsonl"
        sd.ledger_append(led, {"index": 0, "state": "collected"})
        with led.open("a", encoding="utf-8") as fh:
            fh.write(bad + "\n")
        assert sd.ledger_replay(led)[0]["state"] == "collected"

    def test_both_writers_fsync(self, tmp_path, monkeypatch):
        """Write-before-wait is only a guarantee if the write reached the
        disk. A ledger buffered in the OS page cache is lost by exactly the
        power-loss the ordering exists to survive — and the shard rows must be
        durable too, or a ledger that says `collected` outlives the CSV it
        refers to and the resume skips a shard with no rows."""
        synced = []
        monkeypatch.setattr(sd.os, "fsync", lambda fd: synced.append(fd))
        sd.ledger_append(tmp_path / "l.jsonl", {"index": 0, "state": "x"})
        assert len(synced) == 1, "ledger_append did not fsync"
        d = tmp_path / "shard_000"
        d.mkdir()
        sd._write_shard_rows(d, [{c: "" for c in sd.MANIFEST_COLUMNS}])
        assert len(synced) == 2, "_write_shard_rows did not fsync"


class TestAChangedPlanCannotMislabelPaidWork:

    def test_a_changed_bin_is_refused(self):
        with pytest.raises(SystemExit, match="BINS changed"):
            sd._verify_plan_matches_ledger(
                sd.build_plan(),
                {0: {"index": 0, "bin": [30, 40], "state": "submitted"}})

    def test_a_ledger_longer_than_the_plan_is_refused(self):
        plan = sd.build_plan()
        with pytest.raises(SystemExit, match="only"):
            sd._verify_plan_matches_ledger(
                plan, {len(plan) + 5: {"index": len(plan) + 5,
                                       "state": "collected"}})

    def test_a_matching_ledger_passes(self):
        plan = sd.build_plan()
        sd._verify_plan_matches_ledger(
            plan, {0: {"index": 0, "bin": plan[0]["bin"],
                       "state": "collected"}})


class TestTheBudgetCountsEverythingThatWasBilled:

    def test_every_billed_state_counts(self):
        state = {i: {"state": s} for i, s in enumerate(
            ("collected", "failed", "empty", "submitted", "intent",
             "harvest_error"))}
        assert sd._spent_usd(state) == pytest.approx(6 * sd.shard_usd())

    def test_an_empty_campaign_has_spent_nothing(self):
        assert sd._spent_usd({}) == 0.0

    def test_the_cost_model_reproduces_the_measured_run(self):
        assert sd.shard_usd(8) == pytest.approx(0.5528, abs=0.005)

    def test_cost_helpers_follow_a_changed_design_count(self, monkeypatch):
        """Import-time defaults would freeze DESIGNS_PER_SHARD into the
        signature and silently ignore a change to NSAMPLES/REPLICAS."""
        base_s, base_usd = sd.shard_seconds(), sd.shard_usd()
        monkeypatch.setattr(sd, "DESIGNS_PER_SHARD", 128)
        assert sd.shard_seconds() > base_s
        assert sd.shard_usd() > base_usd
        assert sd.shard_usd_ceiling() > sd.shard_usd()


class TestHarvestKeepsWhatWasPaidFor:

    def test_binder_length_counts_chain_C_only(self):
        assert sd._binder_length(_pdb(66)) == 66
        assert sd._binder_length(_pdb(66, chain="B")) == 0

    def test_rows_carry_the_realised_length_not_the_bin(self, tmp_path):
        rows, _ = sd._harvest(_result(lengths=(54,)),
                              {"index": 3, "round": 0, "bin": [50, 59]},
                              "job-x", "fc-x", tmp_path)
        assert rows[0]["binder_length"] == 54
        assert rows[0]["bin_lo"] == 50 and rows[0]["bin_hi"] == 59

    def test_a_cap_dropped_candidate_keeps_its_scores(self, tmp_path):
        out = {"exit_code": 0, "smoke_result": {"candidates": [
            {"rank": 1, "name": "capped", "pdb_key": "design_001.pdb",
             "scores": {"total_reward": -0.9, "af2_iptm": 0.1}}]}}
        rows, with_atoms = sd._harvest(
            out, {"index": 0, "round": 0, "bin": [50, 59]}, "j", "c", tmp_path)
        assert with_atoms == 0 and len(rows) == 1
        assert rows[0]["total_reward"] == -0.9 and rows[0]["pdb_file"] == ""

    def test_colliding_ranks_never_overwrite_a_paid_design(self, tmp_path):
        """Two malformed candidates both mapping to one filename meant the
        second silently clobbered the first: one file, two rows pointing at
        it, and a row claiming 55 aa beside 77 aa coordinates."""
        # DUPLICATE EXPLICIT ranks, not two Nones: a None falls back to the
        # candidate's position, which is already unique, so it would not
        # exercise the dedup at all.
        out = {"exit_code": 0, "smoke_result": {"candidates": [
            {"rank": 1, "name": "a", "scores": {},
             "pdb_content_b64": base64.b64encode(_pdb(55)).decode()},
            {"rank": 1, "name": "b", "scores": {},
             "pdb_content_b64": base64.b64encode(_pdb(77)).decode()},
        ]}}
        rows, with_atoms = sd._harvest(
            out, {"index": 0, "round": 0, "bin": [50, 59]}, "j", "c", tmp_path)
        assert with_atoms == 2
        paths = [r["pdb_file"] for r in rows]
        assert len(set(paths)) == 2, f"both rows point at {paths}"
        files = sorted(f.name for f in (tmp_path / "shard_000").glob("*.pdb"))
        assert len(files) == 2, f"a paid design was overwritten: {files}"
        for row in rows:
            on_disk = sd._binder_length(
                (tmp_path / row["pdb_file"]).read_bytes())
            assert on_disk == row["binder_length"], (
                "the manifest length does not match the file it points at")

    def test_a_zero_design_shard_persists_its_diagnosis(self, tmp_path):
        out = {"exit_code": 1, "smoke_result": {"status": "FAILED",
                                                "candidates": []}}
        rows, with_atoms = sd._harvest(
            out, {"index": 7, "round": 1, "bin": [90, 100]}, "j", "c", tmp_path)
        assert rows == [] and with_atoms == 0
        saved = json.loads(
            (tmp_path / "shard_007" / "smoke_result.json").read_text())
        assert saved["status"] == "FAILED"


class TestBinderLengthReachesTheJobSpec:

    def test_the_default_is_unchanged_for_every_existing_caller(self):
        assert build_job_spec(
            preset="protein_binder", nsamples=4, replicas=2
        )["binder_length"] == [60, 120]

    def test_a_bin_is_passed_through(self):
        assert build_job_spec(
            preset="protein_binder", nsamples=16, replicas=4,
            binder_length=(50, 59))["binder_length"] == [50, 59]

    def test_it_survives_into_the_payload(self):
        payload = build_payload(
            "https://x", preset="protein_binder", nsamples=16, replicas=4,
            job_id="j", binder_length=(90, 100))
        assert payload["job_spec"]["binder_length"] == [90, 100]
        assert "upload_urls_endpoint" not in payload

    def test_the_pair_is_a_list_of_two_ints(self):
        """run_pipeline indexes [0]/[1]; a scalar or short sequence raises out
        of its list comprehension and burns a fully billed A100."""
        spec = build_job_spec(preset="protein_binder", nsamples=1, replicas=1,
                              binder_length=(50, 59))
        assert isinstance(spec["binder_length"], list)
        assert len(spec["binder_length"]) == 2
        assert all(isinstance(v, int) for v in spec["binder_length"])

    def test_every_shipped_bin_reaches_a_payload_intact(self):
        for lo, hi in sd.BINS:
            payload = build_payload(
                "https://x", preset="protein_binder", nsamples=sd.NSAMPLES,
                replicas=sd.REPLICAS, job_id="j", binder_length=(lo, hi))
            assert payload["job_spec"]["binder_length"] == [lo, hi]
