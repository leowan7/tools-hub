"""validate() must refuse two batch records that share one storage object.

af2, colabfold and esmfold batch runs upload each fold under a key built from
its record's name. The upload-URL endpoint (``webhooks/uploads.py``) signs each
key for the storage path ``shared/storage.py::_output_object_path`` gives it,
and that function normalises the key, so two records can land on one object
without having the same name.

Each test drives the tool's ``run_pipeline.main`` with the fold and every
network edge stubbed, records the keys it asks ``request_upload_urls`` for,
then asks the adapter's ``validate`` about the same input.

Runs fully offline: no Modal, no Supabase, no GPU.
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.sequence_parsing import parse_fasta_or_lines
from shared.storage import _output_object_path
from tools import af2, colabfold, esmfold
from tools.af2 import run_pipeline as af2_rp
from tools.colabfold import run_pipeline as colabfold_rp
from tools.esmfold import run_pipeline as esmfold_rp


_SEQ_A = "QVQLVESGGGLVQPGGSLRLSCAAS"  # 25 aa, all canonical
_SEQ_B = "EVQLLESGGGLVQPGGSLRLSCAAS"  # 25 aa, all canonical, not _SEQ_A

TOOLS = ["af2", "colabfold", "esmfold"]
_ADAPTER = {"af2": af2, "colabfold": colabfold, "esmfold": esmfold}
_PIPELINE = {"af2": af2_rp, "colabfold": colabfold_rp, "esmfold": esmfold_rp}


def _stub_consolidated_fold(out_subdir):
    """Stand in for ``_run_colabfold_consolidated``.

    Writes every record's rank_001 scores JSON and PDB where the streamer
    looks for them, then returns a process that has already exited 0.
    """

    def _fold(*, fasta_path, workdir, **_):
        out_dir = Path(workdir) / out_subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        for line in Path(fasta_path).read_text().splitlines():
            if line.startswith(">"):
                stem = line[1:]
                (out_dir / f"{stem}_scores_rank_001_m1.json").write_text(
                    json.dumps({"plddt": [80.0, 82.0], "ptm": 0.8}))
                # 250 bytes: tools/colabfold/run_pipeline.py::_run_batch
                # fails a PDB under 200.
                (out_dir / f"{stem}_unrelaxed_rank_001_m1.pdb").write_bytes(
                    b"ATOM\n" * 50)
        return SimpleNamespace(poll=lambda: 0), out_dir

    return _fold


def _requested_keys(tool, tmp_path, monkeypatch, job_spec):
    """Run ``tool``'s ``run_pipeline.main`` on ``job_spec``; return the keys
    it asked ``request_upload_urls`` for, in order."""
    rp = _PIPELINE[tool]
    if tool == "esmfold":
        monkeypatch.setattr(rp, "load_esmfold", lambda: (None, None))
        monkeypatch.setattr(
            rp,
            "_fold_record",
            lambda tokenizer, model, name, seq, *a, **k: {
                "pdb_b64": base64.b64encode(b"ATOM\n").decode(),
                "mean_plddt": 80.0,
                "ptm": 0.8,
                "total_length": len(seq),
            },
        )
        monkeypatch.setattr(rp, "_archive_raw", lambda *a, **k: None)
    else:
        monkeypatch.setattr(
            rp, "_run_colabfold_consolidated",
            _stub_consolidated_fold(f"{tool}_out"),
        )
        monkeypatch.setattr(
            rp, "archive_raw" if tool == "af2" else "archive_work_dir",
            lambda *a, **k: None,
        )
    # Each tool's _run_batch passes dir="/tmp" for its work dir.
    monkeypatch.setattr(rp, "tempfile", SimpleNamespace(
        TemporaryDirectory=lambda **k: tempfile.TemporaryDirectory(dir=tmp_path),
        mkdtemp=lambda **k: tempfile.mkdtemp(dir=tmp_path),
    ))
    monkeypatch.setattr(rp, "preflight", lambda payload: None)
    monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(rp, "upload_pdb", lambda url, data: None)
    monkeypatch.setattr(
        rp, "SMOKE_RESULTS_PATH", str(tmp_path / "smoke_results.json"))
    requested = []

    def _record(endpoint, token, keys):
        requested.extend(keys)
        return {k: "https://example.invalid/put" for k in keys}

    monkeypatch.setattr(rp, "request_upload_urls", _record)
    monkeypatch.setenv("JOB_PAYLOAD", json.dumps({
        "tier": "batch",
        "job_token": "tok",
        "upload_urls_endpoint": "https://example.invalid/upload-urls",
        "job_spec": job_spec,
    }))
    monkeypatch.setenv("JOB_ID", "job-duplicate-names")
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    rp.main()
    return requested


def _keys_for(tool, tmp_path, monkeypatch, fasta):
    """The keys ``main`` requests for ``fasta``'s records.

    The records come from the parser each adapter's ``_validate_batch``
    calls. That function returns each record's name and sequence unchanged in
    ``batch_records`` and ``build_payload`` forwards the list, so ``main``
    would be given the same names had validate accepted.
    """
    records, err = parse_fasta_or_lines(fasta)
    assert err is None, err
    return _requested_keys(tool, tmp_path, monkeypatch, {
        "batch_records": records,
        "parameters": {"num_recycles": 1, "use_templates": False},
    })


def _validate(tool, fasta):
    return _ADAPTER[tool].validate({"preset": "batch", "sequences": fasta}, {})


def _objects(keys):
    return {_output_object_path("u", "j", k) for k in keys}


@pytest.mark.parametrize("tool", TOOLS)
def test_duplicate_names_reach_main_as_one_key(tool, tmp_path, monkeypatch):
    fasta = f">VHH-12\n{_SEQ_A}\n>VHH-12\n{_SEQ_B}\n"

    keys = _keys_for(tool, tmp_path, monkeypatch, fasta)

    assert keys == ["VHH-12.pdb", "VHH-12.pdb"], keys
    inputs, err = _validate(tool, fasta)
    assert inputs is None, (
        f"validate accepted two records that main uploads under one key, "
        f"{keys!r}")
    assert "'VHH-12'" in err and "Rename one" in err, err


@pytest.mark.parametrize("first", ["binder 1", "_binder_1"])
@pytest.mark.parametrize("tool", TOOLS)
def test_names_that_normalise_to_one_path_share_an_object(
    tool, first, tmp_path, monkeypatch,
):
    """The names differ; the storage path does not."""
    fasta = f">{first}\n{_SEQ_A}\n>binder_1\n{_SEQ_B}\n"

    keys = _keys_for(tool, tmp_path, monkeypatch, fasta)

    assert len(keys) == 2, keys
    assert _objects(keys) == {"u/j/designs/binder_1.pdb"}, keys
    inputs, err = _validate(tool, fasta)
    assert inputs is None, (
        f"validate accepted two records whose keys {keys!r} land on one "
        f"storage object")
    for part in (repr(first), "'binder_1'", "'binder_1.pdb'"):
        assert part in err, err


# Two pairs the af2/colabfold key merges before storage sees it: the cut at
# character 60, and the regex, which turns the "." that storage would keep
# into "_".
_MERGED_BY_THE_KEY = [
    ("x" * 60 + "_a", "x" * 60 + "_b", "x" * 60 + ".pdb"),
    ("binder.1", "binder_1", "binder_1.pdb"),
]


@pytest.mark.parametrize("a, b, key", _MERGED_BY_THE_KEY)
@pytest.mark.parametrize("tool", ["af2", "colabfold"])
def test_names_the_key_merges_share_a_key(
    tool, a, b, key, tmp_path, monkeypatch,
):
    fasta = f">{a}\n{_SEQ_A}\n>{b}\n{_SEQ_B}\n"

    keys = _keys_for(tool, tmp_path, monkeypatch, fasta)

    assert keys == [key] * 2, keys
    inputs, err = _validate(tool, fasta)
    assert inputs is None, f"validate accepted two records keyed {keys!r}"
    assert repr(a) in err and repr(b) in err, err


@pytest.mark.parametrize("a, b", [(a, b) for a, b, _ in _MERGED_BY_THE_KEY])
def test_esmfold_keys_on_the_bare_name(a, b, tmp_path, monkeypatch):
    """The same two names are distinct objects for esmfold, and pass."""
    fasta = f">{a}\n{_SEQ_A}\n>{b}\n{_SEQ_B}\n"

    keys = _keys_for("esmfold", tmp_path, monkeypatch, fasta)

    assert keys == [f"{a}.pdb", f"{b}.pdb"], keys
    assert len(_objects(keys)) == 2, keys
    inputs, err = _validate("esmfold", fasta)
    assert err is None, err


@pytest.mark.parametrize("tool", TOOLS)
def test_distinct_names_are_accepted_and_get_two_objects(
    tool, tmp_path, monkeypatch,
):
    """Positive control: the refusal must not fire on ordinary names.

    Feeds validate's output through build_payload into main.
    """
    inputs, err = _validate(tool, f">VHH-12\n{_SEQ_A}\n>VHH-13\n{_SEQ_B}\n")
    assert err is None, err

    keys = _requested_keys(
        tool, tmp_path, monkeypatch,
        _ADAPTER[tool].build_payload(inputs, ""),
    )

    assert len(_objects(keys)) == 2, keys
