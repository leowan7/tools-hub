"""Offline tests for boltz2's chunked folding.

``run_pipeline.main`` folds ``FOLD_CHUNK`` designs per ``boltz predict``
process rather than one, so the start-up and checkpoint load are paid once a
chunk. Six things have to hold for that to be safe, and each has its own
class below:

- every design reads ITS OWN record's structure and scores out of the shared
  output directory,
- the chunking actually happens, or the change is inert,
- ``msa_server`` is not batched,
- a record the batch did not produce is re-folded alone, and its neighbours
  are delivered either way,
- the live progress number never counts backwards,
- a binder name never becomes a boltz path, whatever it is called.

The fake ``boltz predict`` here writes the real output layout — one directory
per record, files named after the record — and the real ``collect_outputs``
globs it. Stubbing ``collect_outputs`` instead would make the first class
vacuous, since the mix-up it guards against lives inside that glob.

Runs fully offline — no Modal, no Supabase, no GPU.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.boltz2 import run_pipeline as rp


def _iptm_for(record_id: str) -> float:
    """A score unique to one record, so a mixed-up read is visible."""
    return round(0.01 * (int(record_id.split("_")[1]) + 1), 4)


class FakeBoltz:
    """Stands in for the ``boltz predict`` subprocess.

    Writes ``<out_dir>/boltz_results_in/predictions/<record>/`` with the two
    files the pipeline reads, exactly as Boltz names them. Every record gets a
    distinct structure body and a distinct ipTM, which is what lets the tests
    below tell "design 3 got record 3" from "design 3 got whatever sorted
    first".
    """

    def __init__(self, skip: set[str] | None = None, skip_once: bool = False):
        self.skip = set(skip or ())
        self.skip_once = skip_once
        self.calls: list[list[str]] = []

    def __call__(self, data_path, out_dir, msa_server: bool) -> int:
        records = sorted(p.stem for p in Path(data_path).iterdir())
        self.calls.append(records)
        preds = Path(out_dir) / "boltz_results_in" / "predictions"
        for rid in records:
            if rid in self.skip:
                if self.skip_once:
                    self.skip.discard(rid)
                continue
            d = preds / rid
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{rid}_model_0.pdb").write_text(f"ATOM  structure of {rid}\n")
            (d / f"confidence_{rid}_model_0.json").write_text(
                json.dumps({"iptm": _iptm_for(rid), "complex_plddt": 0.9})
            )
        return 0


@pytest.fixture
def run_pipeline(tmp_path, monkeypatch):
    """Drive ``rp.main`` over ``n`` binders and hand back what it produced.

    Heartbeat kwargs land on ``run_pipeline.beats`` rather than in the return
    value, so the tests that do not care about them keep their two-tuple.
    """
    beats: list[dict] = []

    def _run(
        fake: FakeBoltz,
        n: int = 12,
        tier: str = "standalone",
        names: list[str] | None = None,
    ):
        binders = names if names is not None else [f"binder{i}" for i in range(n)]
        result_file = tmp_path / "smoke_results.json"
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))

        antigen = tmp_path / "antigen.pdb"
        antigen.write_text("ATOM\n")
        monkeypatch.setattr(rp, "download_antigen_pdb", lambda url, dest: antigen)
        monkeypatch.setattr(rp, "chain_seq", lambda path, chain="A": "GGGGSGGGGS")
        monkeypatch.setattr(rp, "archive_raw_outputs", lambda *a, **k: None)
        monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: beats.append(k))
        monkeypatch.setattr(rp, "run_boltz", fake)
        monkeypatch.setattr(
            rp,
            "hotspot_contacts",
            lambda *a, **k: {
                "n_contacted": 0,
                "n_hotspots": 0,
                "contacted": [],
                "antigen_chain": "A",
            },
        )
        monkeypatch.setattr(
            rp,
            "request_upload_urls",
            lambda endpoint, token, keys: {k: f"https://example.invalid/{k}" for k in keys},
        )
        uploaded: dict[str, str] = {}
        monkeypatch.setattr(
            rp,
            "upload_pdb",
            lambda url, data: uploaded.__setitem__(
                url.rsplit("/", 1)[-1], data.decode("utf-8")
            ),
        )

        payload = {
            "tier": tier,
            "job_token": "tok",
            "upload_urls_endpoint": "https://example.invalid/upload-urls",
            "input_presigned_url": "https://example.invalid/antigen.pdb",
            "job_spec": {
                "antigen_chain": "A",
                "hotspot_residues": [],
                "binder_sequences": [
                    {"name": name, "sequence": "EVQLVESGGG"} for name in binders
                ],
            },
        }
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps(payload))
        monkeypatch.setenv("JOB_TIER", tier)
        monkeypatch.setenv("JOB_ID", "job-batched")
        monkeypatch.delenv("WEBHOOK_URL", raising=False)

        rp.main()
        return json.loads(result_file.read_text()), uploaded

    _run.beats = beats
    return _run


# ---------------------------------------------------------------------------
# 1 — the mix-up a shared output directory invites
# ---------------------------------------------------------------------------


class TestEachDesignGetsItsOwnStructure:
    """Ten records share one ``--out_dir``; each design must read only its own.

    Before chunking, ``collect_outputs`` globbed the whole tree and took
    ``[0]``. That is correct for a directory holding one record and silently
    wrong for one holding ten: every design in the chunk would carry the
    first record's structure, its ipTM and its verdict, and nothing in the
    result would look unusual. The same shape shipped once already on another
    tool, where every OpenDDE sample showed sample 1's scores.
    """

    def test_scores_and_structures_line_up_with_their_designs(self, run_pipeline):
        result, uploaded = run_pipeline(FakeBoltz(), n=12)

        assert result["status"] == "COMPLETED"
        assert result["designs_completed"] == 12

        for design in result["designs"]:
            rid = rp._record_id(design["rank"])
            assert design["iptm"] == _iptm_for(rid), (
                f"{design['name']} (rank {design['rank']}) carries ipTM "
                f"{design['iptm']}, which belongs to another record — a "
                f"design is reading a neighbour's confidence JSON"
            )
            assert uploaded[design["pdb_key"]] == f"ATOM  structure of {rid}\n", (
                f"{design['pdb_key']} was uploaded with another record's "
                f"structure"
            )

        assert len({v for v in uploaded.values()}) == 12, (
            "twelve designs uploaded fewer than twelve distinct structures"
        )

    def test_the_unscoped_glob_is_what_would_break_it(self, tmp_path):
        """Negative control: the same directory, read the old way.

        Without this, the test above would also pass against a
        ``collect_outputs`` that never learned to scope, on a fake that
        happened to write one record.
        """
        fake = FakeBoltz()
        fake(self._inputs(tmp_path, ["d_000", "d_001"]), tmp_path / "out", False)

        scoped = [
            rp.collect_outputs(tmp_path / "out", rid)[1]["iptm"]
            for rid in ("d_000", "d_001")
        ]
        assert scoped == [_iptm_for("d_000"), _iptm_for("d_001")]

        unscoped = rp.collect_outputs(tmp_path / "out")[1]["iptm"]
        assert unscoped == _iptm_for("d_000"), (
            "the unscoped read returns one record for both designs — this is "
            "the behaviour the record_id argument exists to replace"
        )

    @staticmethod
    def _inputs(tmp_path: Path, record_ids: list[str]) -> Path:
        in_dir = tmp_path / "in"
        in_dir.mkdir(parents=True, exist_ok=True)
        for rid in record_ids:
            (in_dir / f"{rid}.yaml").write_text("version: 1\n")
        return in_dir


# ---------------------------------------------------------------------------
# 2 — the chunking happens at all
# ---------------------------------------------------------------------------


class TestFoldsRunInChunks:
    def test_twelve_designs_are_two_processes_not_twelve(self, run_pipeline):
        fake = FakeBoltz()
        result, _ = run_pipeline(fake, n=12)

        assert [len(c) for c in fake.calls] == [rp.FOLD_CHUNK, 12 - rp.FOLD_CHUNK], (
            f"expected one process per chunk of {rp.FOLD_CHUNK}; boltz was "
            f"called with {[len(c) for c in fake.calls]} record(s)"
        )
        assert result["designs_completed"] == 12

    def test_a_chunk_is_a_directory_of_index_named_yamls(self, run_pipeline):
        """Record ids come from the index, never from the binder's name.

        The name is user text and reaches the filesystem as a filename; the
        index cannot collide and cannot contain a separator.
        """
        fake = FakeBoltz()
        run_pipeline(fake, n=12)

        assert fake.calls[0] == [rp._record_id(i) for i in range(10)]
        assert fake.calls[1] == [rp._record_id(i) for i in (10, 11)]


# ---------------------------------------------------------------------------
# 3 — msa_server is exempt
# ---------------------------------------------------------------------------


class TestMsaServerIsNotBatched:
    """This preset keeps one design per process.

    Its ~214 s/design is dominated by the MSA-server fetch rather than by the
    model load batching removes, so the saving would be small — and it has
    not been measured. What these two tests pin is the decision, not the
    reason for it: the chunking must not reach this preset by accident.
    """

    def test_one_process_per_design(self, run_pipeline):
        fake = FakeBoltz()
        result, _ = run_pipeline(fake, n=3, tier="msa_server")

        assert [len(c) for c in fake.calls] == [1, 1, 1], (
            f"msa_server folded {[len(c) for c in fake.calls]} records per "
            f"process; it must stay one"
        )
        assert result["designs_completed"] == 3

    def test_a_dropped_record_is_not_retried(self, run_pipeline):
        """And the solo retry stays off here, which is the other half.

        The retry exists to undo what batching can cost a design. This preset
        is not batched, so a dropped record had no neighbour to lose, and a
        retry would buy a second ~214 s MSA fetch for one design.
        """
        fake = FakeBoltz(skip={"d_001"}, skip_once=True)
        result, _ = run_pipeline(fake, n=3, tier="msa_server")

        assert [len(c) for c in fake.calls] == [1, 1, 1], (
            f"msa_server re-folded a dropped record: {fake.calls}"
        )
        assert result["designs_completed"] == 2
        assert result["n_failures"] == 1


# ---------------------------------------------------------------------------
# 4 — one missing record must not cost the chunk
# ---------------------------------------------------------------------------


class TestAMissingRecordIsRetriedAlone:
    def test_the_retry_recovers_it_and_the_neighbours_survive(self, run_pipeline):
        """The batch drops d_005; re-folding it alone succeeds."""
        fake = FakeBoltz(skip={"d_005"}, skip_once=True)
        result, uploaded = run_pipeline(fake, n=12)

        assert result["designs_completed"] == 12
        assert result["n_failures"] == 0
        assert fake.calls[1] == ["d_005"], (
            f"expected a single-record retry after the batch dropped d_005; "
            f"boltz's second call was {fake.calls[1]}"
        )
        assert uploaded["binder5_complex.pdb"] == "ATOM  structure of d_005\n"

    def test_a_trailing_chunk_of_one_is_retried_too(self, run_pipeline):
        """11 designs leave design 10 alone in ``b_001``; it still retries.

        The retry used to be gated on ``len(chunk) > 1``, which decided this
        on ``11 % FOLD_CHUNK``: one transient fault recovered design 5 and
        dropped design 10, with nothing in the log to say why.
        """
        fake = FakeBoltz(skip={"d_010"}, skip_once=True)
        result, uploaded = run_pipeline(fake, n=11)

        assert result["designs_completed"] == 11
        assert result["n_failures"] == 0
        assert fake.calls[-1] == ["d_010"], (
            f"expected a solo retry for the run's trailing design; boltz's "
            f"last call was {fake.calls[-1]}"
        )
        assert uploaded["binder10_complex.pdb"] == "ATOM  structure of d_010\n"

    def test_a_record_that_never_folds_costs_only_itself(self, run_pipeline):
        """Negative control: the retry must not paper over a real failure.

        Eleven of twelve still deliver, and the twelfth is counted as a
        failure rather than dropped silently or handed a neighbour's PDB.
        """
        fake = FakeBoltz(skip={"d_005"})
        result, uploaded = run_pipeline(fake, n=12)

        assert result["designs_completed"] == 11
        assert result["designs_folded"] == 11
        assert result["n_failures"] == 1
        assert "binder5_complex.pdb" not in uploaded
        assert {d["rank"] for d in result["designs"]} == set(range(12)) - {5}


# ---------------------------------------------------------------------------
# 5 — the live progress number
# ---------------------------------------------------------------------------


class TestProgressNeverCountsBackwards:
    """``job_detail.html`` renders ``designs_completed`` as a live done/total.

    A chunk sends a heartbeat before it enters the GPU, because nothing else
    reaches the page for the minutes that chunk takes. That heartbeat has to
    count the same thing the per-design ones do. Counting deliveries there
    instead would, on a run with a failure, report a number lower than the
    heartbeat before it and tick the page backwards.
    """

    def test_folding_heartbeats_are_non_decreasing(self, run_pipeline):
        run_pipeline(FakeBoltz(skip={"d_005"}), n=12)

        progress = [
            b["designs_completed"]
            for b in run_pipeline.beats
            if b.get("stage") == "folding"
        ]
        assert progress == sorted(progress), (
            f"the live progress number goes backwards during folding: "
            f"{progress}"
        )
        # A failed design must not stall it either: the run still walks the
        # whole submitted list.
        assert progress[-1] == 12


# ---------------------------------------------------------------------------
# 6 — a binder name that ends in .pdb
# ---------------------------------------------------------------------------


class TestABinderNamedLikeAFileDoesNotBecomeOne:
    """Boltz names its whole output tree after the input yaml's stem.

    While that stem was the binder name, a binder called ``mydesign.pdb``
    made boltz create DIRECTORIES called ``mydesign.pdb``. Those match
    ``collect_outputs``'s ``**/*.pdb`` glob, sort ahead of the real model
    file, and end ``main`` from a ``read_text()`` outside any try — a crash
    AFTER the fold rather than before it. It is reachable by a plain
    submission: the hub's own FASTA export writes headers of that shape and
    ``_parse_binder_text`` takes a header as a name, while #338's rules
    refuse '/', NUL, a leading '.' and over 200 bytes but not a '.pdb'
    ending.

    Chunked folding writes ``d_000.yaml``, so the stem is an index and no
    boltz path carries the name at all. The second test is the falsifier:
    read the pre-batching way, the same tree still hands back a directory.
    """

    def test_the_name_does_not_reach_the_record_id(self, run_pipeline):
        fake = FakeBoltz()
        result, uploaded = run_pipeline(fake, names=["mydesign.pdb", "VHH-12"])

        assert fake.calls == [["d_000", "d_001"]], (
            f"boltz was handed record ids {fake.calls}; a binder name must "
            f"not be one of them"
        )
        assert result["designs_completed"] == 2
        assert result["n_failures"] == 0
        # The name does still reach the storage key. That is a rename, not a
        # crash — see ``tools/boltz2/__init__.py``'s BINDER_NAME_MAX_BYTES
        # block, and PR #340, which fixes the refold half of it hub-side.
        assert "mydesign.pdb_complex.pdb" in uploaded

    def test_the_pre_batching_glob_still_picks_a_directory(self, tmp_path):
        out_dir = tmp_path / "out"
        # The tree boltz wrote back when the stem was the binder name.
        pred = (
            out_dir / "boltz_results_mydesign.pdb" / "predictions" / "mydesign.pdb"
        )
        pred.mkdir(parents=True)
        (pred / "mydesign.pdb_model_0.pdb").write_text("ATOM  real model\n")

        pdb_path, _ = rp.collect_outputs(out_dir)

        assert pdb_path is not None and pdb_path.is_dir(), (
            f"collect_outputs returned {pdb_path!r}; if the unscoped glob no "
            f"longer picks a directory then the test above proves nothing"
        )
