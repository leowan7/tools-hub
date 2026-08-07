"""Design delivery for the Proteina-Complexa tool: the upload path (tools-hub
web tier) and the inline path (a direct ``modal.Function.from_name`` call).

WHY THIS FILE EXISTS. A caller that invokes the Modal function directly — the
way RFdiffusion, PXDesign and BindCraft already work — has no tools-hub server
to receive an upload callback and no ``job_token`` to authenticate one, and
``main()`` used to refuse pre-GPU on exactly that. It now chooses a delivery
mode instead: with an endpoint it uploads and records a ``pdb_key`` pointer,
without one it carries the atoms inline as ``pdb_content_b64``.

NOT the reason, though this docstring used to claim it: "the tools-hub web tier
cannot express a multi-chain target or chain-prefixed hotspots". It can, and
``TestTheWebTierIsAFirstClassMultiChainPath`` below pins that against the real
adapter. The false belief mattered: it is why the bare-hotspot ambiguity
refusal was put on the container's direct-call entry only, leaving the web
form — which promotes a bare ``264`` onto the first contig chain before
dispatch — as the one multi-chain path with no such guard.

THE UPLOAD PATH IS THE ONE REAL JOBS USE. Every test in
``TestUploadPathUnchanged`` asserts it behaves as it did before the change —
same calls, same argv, same pointers, same failure accounting — rather than
merely asserting it still produces output.

Runs fully offline: the GPU stages are stubbed, no Modal, no network.
"""

from __future__ import annotations

import base64
import json
import os

import pytest

from tools.proteina import run_pipeline as rp


def _drive_design_loop(tmp_path, monkeypatch, *, endpoint, designs=2,
                       pdb_body=b"ATOM  fake\nEND\n", inline_env=None,
                       cap_bytes=None, break_upload=False, break_read=False,
                       expect_exit=False):
    """Run ``main()`` through to the design loop with the GPU stages stubbed.

    Returns ``(result_dict, calls)`` where ``calls`` records every
    ``request_upload_urls`` / ``upload_pdb`` invocation in order, so a test can
    assert on the exact upload exchange rather than on its side effects.

    ``break_upload`` breaks the UPLOAD path only — it raises from
    ``request_upload_urls``, which the inline path never calls. ``break_read``
    breaks the one thing the inline path does inside the same try, the
    ``pdb_path.read_bytes()``, so the failure accounting on that branch can be
    pinned too. ``expect_exit`` requires the SystemExit a delivery failure
    raises after writing its result, AND requires its code to be 1 — bare
    ``pytest.raises(SystemExit)`` would accept ``sys.exit(0)``, and the exit
    code is not decoration: ``modal_app.run_tool`` puts ``result.returncode``
    straight into the value the caller receives, so a delivery failure that
    exits 0 is a failed run reported as a clean one to anyone branching on it.
    """
    result_file = tmp_path / "smoke.json"
    monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))

    # exist_ok: the two-mode comparison test drives this helper twice against
    # the same tmp_path.
    pdb_dir = tmp_path / "pdbs"
    pdb_dir.mkdir(exist_ok=True)
    # NO ``rank`` KEY, DELIBERATELY. This fixture used to fabricate
    # ``"rank": i + 1`` — pxdesign's convention, not this pipeline's, whose
    # parser numbers from 0 — while also stubbing ``parse_designs`` out. Every
    # assertion below on a delivered ``pdb_key`` was therefore checking a
    # filename production could not emit, and the whole file was blind to the
    # rank base in either direction: flipping this one line to ``i`` turned
    # three tests red with no production code touched.
    #
    # main() now COUNTS the delivered rank itself (``emitted_rank``) instead of
    # reading one off the row, so omitting the key is not a convenience — it is
    # the guard. Any regression that goes back to trusting the parsed row
    # raises KeyError here rather than quietly agreeing with a fixture.
    # ``TestDeliveredRankIsDenseAndOneBased`` drives the REAL parser end to end
    # for the same reason.
    rows = []
    for i in range(designs):
        (pdb_dir / f"d{i}.pdb").write_bytes(pdb_body)
        rows.append({
            "name": f"design_{i}",
            "_row_index": i,
            "total_reward": -1.0 * (i + 1),
            "scores": {
                "total_reward": -1.0 * (i + 1), "af2_iptm": 0.7,
                "af2_plddt": 0.8, "rf3_score": None,
                "binder_scrmsd": 1.2, "cluster_id": None,
            },
        })

    calls: list[tuple] = []

    def fake_request_upload_urls(ep, token, filenames):
        if break_upload:
            raise RuntimeError("upload_urls request failed: HTTP 500")
        calls.append(("request", ep, token, tuple(filenames)))
        return {f: f"https://put/{f}" for f in filenames}

    def fake_upload_pdb(url, data):
        # The bytes themselves, so a test can assert on content.
        calls.append(("put", url, data))

    monkeypatch.setattr(rp, "run_streaming", lambda cmd, wd: 0)
    monkeypatch.setattr(rp, "parse_designs", lambda run_dir: rows)
    # break_read points at a path that was never written, so read_bytes raises
    # inside the same try the upload pair lives in — the inline path's only
    # failure mode.
    stem = "gone_d" if break_read else "d"
    monkeypatch.setattr(
        rp, "find_pdb_for",
        lambda d, run_dir, idx, total: pdb_dir / f"{stem}{d['_row_index']}.pdb")
    monkeypatch.setattr(rp, "archive_raw_outputs", lambda *a, **k: None)

    def fake_heartbeat(*a, **kw):
        calls.append(("heartbeat", kw.get("stage"), kw.get("new_candidate")))

    monkeypatch.setattr(rp, "send_heartbeat", fake_heartbeat)
    monkeypatch.setattr(rp, "request_upload_urls", fake_request_upload_urls)
    monkeypatch.setattr(rp, "upload_pdb", fake_upload_pdb)
    monkeypatch.setattr(rp, "build_design_cmd", lambda **k: ["true"])
    monkeypatch.setattr(rp, "shard_seed", lambda job_id: 1)
    if cap_bytes is not None:
        monkeypatch.setattr(rp, "INLINE_PDB_TOTAL_CAP_BYTES", cap_bytes)

    payload = {
        "job_spec": {
            "config_name": "search_binder_local_pipeline",
            "task_name": "02_PDL1", "rf3_required": False,
            "nsamples": 4, "replicas": 2,
        },
        "input_presigned_url": "",
        "upload_urls_endpoint": endpoint,
        "job_token": "tok" if endpoint else "",
        "tier": "protein_binder",
    }
    monkeypatch.setenv("JOB_PAYLOAD", json.dumps(payload))
    monkeypatch.setenv("JOB_TIER", "protein_binder")
    monkeypatch.setenv("JOB_ID", "job-deliver")
    monkeypatch.setenv("PROTEINA_RF3", "on")
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    if inline_env is None:
        monkeypatch.delenv("PROTEINA_INLINE_PDBS", raising=False)
    else:
        monkeypatch.setenv("PROTEINA_INLINE_PDBS", inline_env)

    if expect_exit:
        with pytest.raises(SystemExit) as exc:
            rp.main()
        assert exc.value.code == 1, (
            f"a delivery failure exited {exc.value.code!r}; modal_app reports "
            "exit_code to the caller, so it must match _fail's 1")
    else:
        rp.main()
    return json.loads(result_file.read_text()), calls


def _drive_real_parser(tmp_path, monkeypatch, *, rows, endpoint="",
                       break_upload_for=()):
    """Drive ``main()`` with the REAL ``parse_designs`` and ``find_pdb_for``.

    ``_drive_design_loop`` above stubs both, which is right for the delivery
    branches it targets but leaves the whole reward-CSV -> candidate chain —
    ordering, row-to-file matching, and the number the caller finally reads as
    ``rank`` — asserted only against rows a test fabricated. Everything here
    comes out of the pipeline's own parser instead.

    ``rows`` is ``[(name, total_reward, pdb_bytes_or_None)]`` in CSV order,
    which is deliberately NOT reward order: the parser sorts. A row whose
    bytes are ``None`` gets a CSV entry pointing at a file that was never
    written — the real "the design has no structure on disk" case, reached
    through ``find_pdb_for``'s own fallbacks rather than by stubbing it out.

    ``break_upload_for`` names designs whose ``upload_pdb`` raises. It matches
    on the design's BYTES, not on its filename, because the filename is
    derived from the rank this helper exists to test — keying on it would let
    a wrong rank silently break a different design than the test asked for.

    Returns ``(result_dict, calls)``.
    """
    home = tmp_path / "proteina"
    run_dir = home / "inference"
    monkeypatch.setattr(rp, "PROTEINA_HOME", str(home))
    result_file = tmp_path / "smoke.json"
    monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))

    calls: list[tuple] = []

    def fake_design(cmd, work_dir):
        # What `complexa design` leaves behind: the per-design PDBs and the
        # reward CSV. Written from inside run_streaming because main() wipes
        # and recreates ./inference immediately before calling it.
        header = ("pdb_path,total_reward,af2folding_i_ptm_log,"
                  "af2folding_plddt,af2folding_rmsd,metadata_tag")
        lines = [header]
        for name, reward, body in rows:
            pdb = run_dir / f"{name}.pdb"
            if body is not None:
                pdb.write_bytes(body)
            lines.append(f"{pdb},{reward},0.7,0.8,1.2,{name}")
        (run_dir / "rewards_search_binder_local_pipeline_0.csv").write_text(
            "\n".join(lines) + "\n")
        return 0

    broken_bodies = [body for name, _reward, body in rows
                     if body is not None and name in break_upload_for]

    def fake_request_upload_urls(ep, token, filenames):
        calls.append(("request", ep, token, tuple(filenames)))
        return {f: f"https://put/{f}" for f in filenames}

    def fake_upload_pdb(url, data):
        if data in broken_bodies:
            raise RuntimeError("PUT failed: HTTP 500")
        calls.append(("put", url, data))

    monkeypatch.setattr(rp, "run_streaming", fake_design)
    monkeypatch.setattr(rp, "archive_raw_outputs", lambda *a, **k: None)
    monkeypatch.setattr(
        rp, "send_heartbeat",
        lambda *a, **kw: calls.append(
            ("heartbeat", kw.get("stage"), kw.get("new_candidate"))))
    monkeypatch.setattr(rp, "request_upload_urls", fake_request_upload_urls)
    monkeypatch.setattr(rp, "upload_pdb", fake_upload_pdb)
    monkeypatch.setattr(rp, "build_design_cmd", lambda **k: ["true"])
    monkeypatch.setattr(rp, "shard_seed", lambda job_id: 1)

    monkeypatch.setenv("JOB_PAYLOAD", json.dumps({
        "job_spec": {
            "config_name": "search_binder_local_pipeline",
            "task_name": "02_PDL1", "rf3_required": False,
            "nsamples": 4, "replicas": 2,
        },
        "input_presigned_url": "",
        "upload_urls_endpoint": endpoint,
        "job_token": "tok" if endpoint else "",
        "tier": "protein_binder",
    }))
    monkeypatch.setenv("JOB_TIER", "protein_binder")
    monkeypatch.setenv("JOB_ID", "job-realparse")
    monkeypatch.setenv("PROTEINA_RF3", "on")
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("PROTEINA_INLINE_PDBS", raising=False)

    rp.main()
    return json.loads(result_file.read_text()), calls


def _pdb_with_chains(spans):
    """A minimal CA-only PDB, in the columns ``pdb_ca_residues`` really reads."""
    lines = []
    serial = 0
    for chain, lo, hi in spans:
        for resseq in range(lo, hi + 1):
            serial += 1
            lines.append(
                f"ATOM  {serial:5d}  CA  GLY {chain}{resseq:4d}    "
                f"{serial:8.3f}{serial:8.3f}{serial:8.3f}  1.00  0.00           C"
            )
    return "\n".join(lines) + "\nEND\n"


def _drive_prepare_custom_target(tmp_path, monkeypatch, *, target_chain,
                                 target_input="", hotspots=(),
                                 spans=(("A", 1, 200),)):
    """Run the REAL ``prepare_custom_target`` against a synthesised upload.

    Returns ``(error_or_None, staged_filenames)``. ``target_chain`` goes through
    ``normalize_target_chain`` first, exactly as ``main()`` does, so a test can
    hand over the spelling a caller really sends.

    The registry path deliberately does not exist, so a job that clears every
    INPUT guard stops one step later at ``target_registry`` — a DIFFERENT check
    name. That is what makes "it was accepted" an observable outcome here rather
    than an exception, and it is why the controls below can assert the absence of
    a refusal without reaching ``complexa``.
    """
    hub = tmp_path / "hub"
    results = tmp_path / "smoke_results.json"
    monkeypatch.setattr(rp, "_HUB_TARGET_DIR", str(hub))
    monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(results))
    monkeypatch.setattr(rp, "_TARGETS_DICT", str(tmp_path / "no_registry.yaml"))
    monkeypatch.setattr(
        rp, "download_target",
        lambda url, dest: dest.write_text(_pdb_with_chains(spans)))
    with pytest.raises(SystemExit) as excinfo:
        rp.prepare_custom_target(
            input_url="https://example.invalid/target.pdb", job_id="j1",
            target_chain=rp.normalize_target_chain(target_chain),
            target_input=target_input, hotspot_spec=list(hotspots),
            binder_length=[60, 120], run_dir=tmp_path / "run")
    assert excinfo.value.code == 1
    error = json.loads(results.read_text())["error"]
    return error, sorted(p.name for p in hub.glob("hub_*.pdb"))


def _parse_direct_call_args(argv):
    """Parse through direct_call_fc's REAL parser, so a hardcoded default
    cannot slip back in behind a hand-built Namespace."""
    from tools.proteina import direct_call_fc as dc
    return dc.build_parser().parse_args(argv)


class _ReachedTheModalBoundary(Exception):
    """Raised by the stubs that stand in for everything ``cmd_submit`` does
    after its refusal guards. Reaching it is the assertion; EXECUTING PAST IT
    SPENDS REAL MONEY."""


@pytest.fixture
def modal_tripwire(monkeypatch):
    """Make every real side effect past cmd_submit's guards unreachable.

    THIS EXISTS BECAUSE THE TESTS BELOW REALLY DID SPAWN A100 SHARDS. They were
    written as ``with pytest.raises(BaseException): dc.cmd_submit(...)`` on the
    theory that "it gets past the guard, then fails on the modal import and
    staging it is not allowed to do here" — i.e. they depended on the real work
    FAILING to stay offline. It does not fail. ``tests/conftest.py`` documents
    why in its own words: ``app.py`` calls ``load_dotenv()`` at import and the
    repo-root ``.env`` carries real service-role credentials. Any full-suite
    run imports ``app`` during COLLECTION, so by the time these tests execute,
    ``_stage_target`` uploads a 277 KB PDB to production storage and
    ``fn.spawn(payload)`` launches a real ``protein_binder`` shard on
    ``ranomics-proteina-prod`` — nsamples*replicas designs, up to the 7200 s
    timeout. The tests then "fail" on DID NOT RAISE, long after the money is
    committed. They passed in the narrow three-file run precisely because no
    credentials were loaded there, which is the worst possible property for a
    guard: silent offline, expensive in CI.

    So the boundary is now enforced rather than hoped for. `_resolve_target`
    and `_stage_target` raise before anything leaves the machine, and a fake
    ``modal`` whose entry points raise stands behind them, so a future edit
    that removes a stub still cannot reach the network. Tests that need a
    working ``modal`` (the --collect ones) install their own afterwards, which
    takes precedence.

    Returns the ``reached`` list that records entry to ``_load_env_and_path``,
    so a test can still assert it got PAST the guards without going further.
    """
    import sys as _sys
    import types as _types
    from tools.proteina import direct_call_fc as dc

    def _boom(*_a, **_kw):
        raise _ReachedTheModalBoundary(
            "cmd_submit reached real staging / Modal; the tripwire stopped it")

    reached = []
    monkeypatch.setattr(dc, "_load_env_and_path", lambda: reached.append(1))
    monkeypatch.setattr(dc, "_resolve_target", _boom)
    monkeypatch.setattr(dc, "_stage_target", _boom)

    fake_modal = _types.ModuleType("modal")
    fake_modal.Function = _types.SimpleNamespace(from_name=_boom)
    fake_modal.FunctionCall = _types.SimpleNamespace(from_id=_boom)
    monkeypatch.setitem(_sys.modules, "modal", fake_modal)
    return reached


def _drive_collect(tmp_path, monkeypatch, smoke, *, exit_code=0, outdir=None):
    """Run the REAL ``cmd_collect`` against a stubbed Modal call.

    Returns ``(rc, outdir, state_path)``. Everything the operator would get is
    produced by the real function — asserting on a hand-rolled copy of its
    write logic would pin nothing.
    """
    import sys as _sys
    import types as _types
    from tools.proteina import direct_call_fc as dc

    state = tmp_path / "state.json"
    state.write_text(json.dumps(
        {"call_id": "fc-abc123", "job_id": "fc-round-1", "job_spec": {}}))
    monkeypatch.setattr(dc, "STATE", state)
    monkeypatch.setattr(dc, "_load_env_and_path", lambda: None)

    result = {"exit_code": exit_code, "smoke_result": smoke}
    fake_modal = _types.ModuleType("modal")
    fake_modal.FunctionCall = _types.SimpleNamespace(
        from_id=lambda cid: _types.SimpleNamespace(
            get=lambda timeout=None: result))
    monkeypatch.setitem(_sys.modules, "modal", fake_modal)

    out = outdir or (tmp_path / "out")
    rc = dc.cmd_collect(_parse_direct_call_args(
        ["--collect", "--outdir", str(out)]))
    return rc, out, state


def _modal_function_gpus():
    """Resolve every ``@app.function`` in modal_app.py to its ``gpu=`` value.

    Read with ``ast`` rather than imported: importing modal_app builds an App,
    an Image and three Volumes, which is neither offline nor free of side
    effects. Returns ``{function_name: gpu_or_None}`` with ``Name`` kwargs
    resolved through the module's own top-level constants, so ``gpu=_GPU``
    reports the string that is actually deployed.
    """
    import ast
    import pathlib

    src = (pathlib.Path(rp.__file__).resolve().parent
           / "modal_app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = {
        t.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for t in node.targets if isinstance(t, ast.Name)
    }
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call)
                    and getattr(dec.func, "attr", "") == "function"):
                continue
            gpu = {k.arg: k.value for k in dec.keywords}.get("gpu")
            if isinstance(gpu, ast.Name):
                found[node.name] = consts.get(gpu.id)
            elif isinstance(gpu, ast.Constant):
                found[node.name] = gpu.value
            else:
                found[node.name] = None
    return found


def _explode(*_a, **_kw):
    raise AssertionError(
        "reached past the guard — this would have hit Modal or Supabase")


class TestInlineDesignDelivery:
    """A direct Modal call with no upload endpoint must still return atoms."""

    def test_no_endpoint_no_longer_fails_preflight(self, tmp_path, monkeypatch):
        """The blocker itself. main() used to
        _fail("preflight", "upload_urls_endpoint") before the GPU, which made
        Proteina the only one of the four generators that could not be driven
        directly."""
        data, _ = _drive_design_loop(tmp_path, monkeypatch, endpoint="")
        assert data["status"] == "COMPLETED"
        assert data["designs_completed"] == 2

    def test_no_endpoint_returns_usable_coordinates(self, tmp_path, monkeypatch):
        """Scores already travelled inline; only the coordinates did not. The
        base64 must decode to the EXACT bytes on disk — a truncated or
        re-encoded structure would still look like a populated field."""
        body = b"ATOM      1  CA  GLY A   1       1.000   2.000   3.000\nEND\n"
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="", pdb_body=body)
        assert data["candidates"]
        for entry in data["candidates"]:
            assert "pdb_content_b64" in entry
            assert base64.b64decode(entry["pdb_content_b64"]) == body

    def test_no_endpoint_makes_no_upload_calls(self, tmp_path, monkeypatch):
        """There is no tools-hub server to call back to and no job_token to
        authenticate with, so attempting either is the bug, not a fallback."""
        _, calls = _drive_design_loop(tmp_path, monkeypatch, endpoint="")
        assert [c for c in calls if c[0] in ("request", "put")] == []

    def test_field_name_matches_the_other_generators(self, tmp_path, monkeypatch):
        """PXDesign (_candidate_from_design) and BindCraft both emit
        `pdb_content_b64`. A fourth spelling would force per-tool
        special-casing in the campaign's merge step."""
        data, _ = _drive_design_loop(tmp_path, monkeypatch, endpoint="")
        assert all("pdb_content_b64" in c for c in data["candidates"])

    def test_inline_pdb_key_is_a_bare_filename_not_a_storage_path(self, tmp_path, monkeypatch):
        """The `designs/` prefix is a CLAIM that the bytes are in Storage.
        shared/jobs.py _slim_result_for_persist strips the inline copy from any
        candidate carrying it, on the stated grounds that it "resolves from
        Storage". Nothing was uploaded here, so claiming the prefix would let
        slimming delete the only copy and leave a pointer at an object that was
        never written — scores intact, every structure gone, no error. A bare
        filename is the convention jobs.py documents for non-Storage-backed
        candidates. The extension still rides in pdb_key, as PXDesign does."""
        data, _ = _drive_design_loop(tmp_path, monkeypatch, endpoint="")
        keys = [c["pdb_key"] for c in data["candidates"]]
        assert keys == ["design_001.pdb", "design_002.pdb"]
        assert not any(k.startswith("designs/") for k in keys)

    def test_inline_disabled_without_an_endpoint_still_refuses(self, tmp_path, monkeypatch):
        """The refusal is narrowed, not deleted. With no endpoint AND inlining
        off, a finished design genuinely has nowhere to put its atoms, and
        refusing before spending any GPU money is still the only right
        answer."""
        result_file = tmp_path / "smoke.json"
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))
        monkeypatch.setenv("PROTEINA_INLINE_PDBS", "off")
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps({
            "job_spec": {
                "config_name": "search_binder_local_pipeline",
                "task_name": "02_PDL1", "rf3_required": False,
                "nsamples": 4, "replicas": 2,
            },
            "input_presigned_url": "", "upload_urls_endpoint": "",
            "job_token": "", "tier": "protein_binder"}))
        monkeypatch.setenv("JOB_TIER", "protein_binder")
        monkeypatch.setenv("JOB_ID", "job-refuse")
        monkeypatch.setenv("PROTEINA_RF3", "on")
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        with pytest.raises(SystemExit):
            rp.main()
        data = json.loads(result_file.read_text())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "upload_urls_endpoint"


class TestUploadPathUnchanged:
    """The tools-hub web path. Real jobs supply an endpoint and must keep
    uploading exactly as they did; this is the regression surface."""

    def test_endpoint_still_uploads_every_design(self, tmp_path, monkeypatch):
        _, calls = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload")
        assert [c[0] for c in calls if c[0] in ("request", "put")] == [
            "request", "put", "request", "put"]

    def test_no_base64_ever_reaches_a_heartbeat(self, tmp_path, monkeypatch):
        """Heartbeats POST to the tools-hub webhook on every design. Passing
        the candidate dict straight through — the obvious future refactor —
        would put megabytes per design through that endpoint. Pinned in BOTH
        modes because the inline mode is where the base64 exists at all."""
        for endpoint in ("https://hub/upload", ""):
            _, calls = _drive_design_loop(
                tmp_path, monkeypatch, endpoint=endpoint,
                pdb_body=b"ATOM  zzz\nEND\n")
            beats = [c for c in calls if c[0] == "heartbeat" and c[2]]
            assert beats, "guard must run against real heartbeats"
            for _, _, cand in beats:
                assert "pdb_content_b64" not in cand, (
                    f"heartbeat carries base64 (endpoint={endpoint!r})")

    def test_endpoint_requests_the_same_filenames_with_the_token(self, tmp_path, monkeypatch):
        """Exact argv of the upload exchange: one basename per call, bearing
        the job_token. pdb_key must share that basename or the web service's
        resolver 404s at {user}/{job}/designs/<basename>."""
        _, calls = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload")
        made = [c for c in calls if c[0] == "request"]
        assert [c[3] for c in made] == [("design_001.pdb",), ("design_002.pdb",)]
        assert {c[1] for c in made} == {"https://hub/upload"}
        assert {c[2] for c in made} == {"tok"}

    def test_endpoint_uploads_the_real_bytes(self, tmp_path, monkeypatch):
        """Compares CONTENT, not length: a re-encoded body of equal length
        would pass a length check and ship the wrong structure."""
        body = b"ATOM      1  CA  GLY A   1       9.000   8.000   7.000\nEND\n"
        _, calls = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload", pdb_body=body)
        assert [c[2] for c in calls if c[0] == "put"] == [body, body]

    def test_endpoint_still_records_pdb_key_pointers(self, tmp_path, monkeypatch):
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload")
        assert [d["pdb_key"] for d in data["designs"]] == [
            "designs/design_001.pdb", "designs/design_002.pdb"]
        assert [c["pdb_key"] for c in data["candidates"]] == [
            "designs/design_001.pdb", "designs/design_002.pdb"]

    def test_an_endpoint_suppresses_inlining_entirely(self, tmp_path, monkeypatch):
        """THE PAYLOAD, not just the upload calls, must be what it was. A
        second copy of every structure in the Modal return buys nothing when
        the first already resolves by pdb_key, and
        shared/compute_campaigns.py reconcile_campaign_children pulls each
        finished child's FULL return into web-tier memory (max_poll=64) from
        inside a user-facing request — at 8 designs/shard an Fc-sized target is
        ~3.6 MB of base64 per child."""
        data, calls = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload")
        assert len([c for c in calls if c[0] == "put"]) == 2
        assert data["candidates"], "guard must run against a populated list"
        for c in data["candidates"]:
            assert c["pdb_key"].startswith("designs/")
            assert "pdb_content_b64" not in c, (
                "an uploaded design must not also ride inline")

    def test_the_web_result_is_byte_identical_to_the_old_shape(self, tmp_path, monkeypatch):
        """No new key anywhere in a web-path result. The strongest statement
        available offline that the tools-hub product sees what it always saw."""
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload")
        assert set(data["candidates"][0]) == {"rank", "name", "pdb_key", "scores"}
        assert set(data["designs"][0]) == {
            "rank", "name", "pdb_key", "total_reward", "af2_iptm",
            "af2_plddt", "rf3_score", "binder_scrmsd", "cluster_id"}

    def test_the_designs_list_never_carries_base64(self, tmp_path, monkeypatch):
        """PLACEMENT REGRESSION. /tmp/smoke_results.json IS the persisted
        job.result, and shared/jobs.py _slim_result_for_persist strips the
        redundant inline copy by walking result["candidates"] AND NOTHING
        ELSE. A copy on result["designs"] would survive slimming and push
        multi-MB of base64 through the single PostgREST UPDATE in _cas_update,
        which that function documents as throwing and stranding the job in
        "running" after a webhook that already returned 200."""
        for endpoint in ("https://hub/upload", ""):
            data, _ = _drive_design_loop(
                tmp_path, monkeypatch, endpoint=endpoint,
                pdb_body=b"ATOM  x\nEND\n")
            assert data["designs"], "guard must run against a populated list"
            assert all("pdb_content_b64" not in d for d in data["designs"]), (
                f"designs[] carries base64 (endpoint={endpoint!r}); it would "
                "survive _slim_result_for_persist and bloat the DB row")

    def test_an_inline_only_result_survives_slimming_with_its_atoms(self, tmp_path, monkeypatch):
        """Runs the REAL slimming function over a REAL result, so a change to
        either side is caught here rather than restated.

        The inline copy is the ONLY copy, so slimming must keep it. It does,
        because the bare-filename pdb_key does not claim Storage backing."""
        from shared.jobs import _slim_result_for_persist
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="",
            pdb_body=b"ATOM  x" * 200 + b"\nEND\n")
        assert all("pdb_content_b64" in c for c in data["candidates"])
        slimmed = _slim_result_for_persist(data)
        assert all("pdb_content_b64" in c for c in slimmed["candidates"]), (
            "slimming deleted the only copy of every structure")

    def test_a_web_path_result_has_nothing_to_slim(self, tmp_path, monkeypatch):
        """The web path never inlines now, so the multi-MB row that
        _slim_result_for_persist exists to prevent cannot form in the first
        place — and slimming stays a no-op safety net rather than the only
        thing standing between us and a stranded job."""
        from shared.jobs import _slim_result_for_persist
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload",
            pdb_body=b"ATOM  x" * 200 + b"\nEND\n")
        assert _slim_result_for_persist(data) == data

    def test_upload_failure_still_skips_and_counts(self, tmp_path, monkeypatch):
        """Pre-existing behaviour, preserved: a design whose upload raises is
        dropped and counted, never delivered with a pdb_key pointing at
        nothing."""
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload",
            break_upload=True)
        assert data["designs_completed"] == 0
        assert data["n_failures"] == 2
        assert data["status"] == "COMPLETED"

    def test_an_unreadable_pdb_is_counted_on_the_INLINE_path_too(self, tmp_path, monkeypatch):
        """The mirror of test_upload_failure_still_skips_and_counts, for the
        branch that test cannot reach. ``break_upload`` raises from
        ``request_upload_urls``, which the inline path never calls, so the
        inline half of that try — ``pdb_path.read_bytes()`` — had no coverage
        at all: a design whose PDB vanished could stop incrementing
        ``n_failures`` and simply disappear from the result, leaving
        ``designs_completed`` under-reporting against a clean ``n_failures``
        of 0. That is the truncated-result-that-looks-complete failure this
        file exists to prevent, and it must be counted, not swallowed."""
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="", break_read=True)
        assert data["designs_completed"] == 0
        assert data["n_failures"] == 2, (
            "an unreadable PDB must be COUNTED on the inline path, not "
            "silently dropped")
        assert data["candidates"] == []
        assert data["status"] == "COMPLETED"

    def test_a_read_failure_is_counted_the_same_way_on_both_paths(self, tmp_path, monkeypatch):
        """Delivery mode may not change the failure ARITHMETIC either. Pins the
        two branches against each other so neither can drift alone."""
        web, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload",
            break_read=True)
        direct, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="", break_read=True)
        for key in ("status", "designs_completed", "n_failures"):
            assert web[key] == direct[key], f"{key} differs between modes"

    def test_scores_and_ranking_are_identical_between_the_two_modes(self, tmp_path, monkeypatch):
        """Delivery mode may change only HOW the atoms travel — never the
        scores, the ranking, or which designs survive. pdb_key is excluded
        because it legitimately differs (Storage path vs bare filename); every
        other field must match exactly."""
        web, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload")
        direct, _ = _drive_design_loop(tmp_path, monkeypatch, endpoint="")

        def strip(rows):
            return [{k: v for k, v in r.items()
                     if k not in ("pdb_content_b64", "pdb_key")} for r in rows]

        assert strip(web["designs"]) == strip(direct["designs"])
        assert strip(web["candidates"]) == strip(direct["candidates"])
        for key in ("status", "designs_completed", "designs_total", "n_failures"):
            assert web[key] == direct[key], f"{key} differs between modes"


class TestDeliveredRankIsDenseAndOneBased:
    """The number a caller reads as ``rank``, driven through the REAL parser.

    All five sibling generators guarantee a dense 1-based rank and derive the
    filename from it: boltzgen ``rank = rank_idx + 1`` -> ``design_001.pdb``,
    rfantibody, pxdesign, bindcraft and rfdiffusion the same, and boltzgen and
    rfantibody both carry an explicit ``emitted_rank`` counter with a comment
    saying why ("designs without a matching structure file get skipped — so we
    can't use rank_idx for the candidate rank or we end up with gaps").
    Proteina used the parsed reward-sort index instead, which is 0-based and
    goes sparse the moment anything is dropped.

    Two consequences, neither cosmetic. ``shared/exports.py`` states the
    cross-tool invariant — "every tool emits a rank 1 and a ``design_1.pdb``"
    — and copies the tool's own rank into ``source_rank``, so a merged
    campaign export listed proteina's rows off by one against every sibling.
    And ``direct_call_fc`` writes the operator's files as
    ``design_{rank:03d}.pdb``, so a collected shard started at design_000.pdb,
    or after drops at design_002.pdb with nothing to say which had happened.

    EVERY test here uses ``_drive_real_parser``. The class exists because the
    other harness could not see this: it stubbed ``parse_designs`` out and
    fabricated 1-based rows, so it asserted the sibling convention while the
    code under test disagreed.
    """

    _BODY = b"ATOM      1  CA  GLY A   1       1.000   2.000   3.000\nEND\n"

    def _rows(self, *names):
        """CSV order deliberately reversed against reward order, so nothing
        here can pass by the rows happening to arrive pre-sorted."""
        n = len(names)
        return [(name, -1.0 * (n - i), self._BODY)
                for i, name in enumerate(names)]

    def test_the_best_design_is_rank_1_not_rank_0(self, tmp_path, monkeypatch):
        """The bare parity statement. A direct caller's top hit must be rank 1
        / design_001.pdb, as it is from all five siblings."""
        data, _ = _drive_real_parser(
            tmp_path, monkeypatch, rows=self._rows("alfa", "bravo", "charlie"))
        assert [c["rank"] for c in data["candidates"]] == [1, 2, 3]
        assert [c["pdb_key"] for c in data["candidates"]] == [
            "design_001.pdb", "design_002.pdb", "design_003.pdb"]

    def test_rank_1_really_is_the_best_scoring_design(self, tmp_path, monkeypatch):
        """Renumbering must not decouple the rank from the ranking. The rows
        are fed in worst-first, so a counter that ignored the parser's sort
        would label the worst design rank 1."""
        data, _ = _drive_real_parser(
            tmp_path, monkeypatch, rows=self._rows("alfa", "bravo", "charlie"))
        assert [c["name"] for c in data["candidates"]] == [
            "charlie", "bravo", "alfa"]
        rewards = [c["scores"]["total_reward"] for c in data["candidates"]]
        assert rewards == sorted(rewards, reverse=True), (
            "protein total_reward is negative and higher is better")

    def test_the_upload_path_numbers_from_1_too(self, tmp_path, monkeypatch):
        """The web tier is the path real jobs take. Its pointers and its upload
        exchange must carry the same dense 1-based basenames, or the resolver
        at {user}/{job}/designs/<basename> and the candidate disagree."""
        data, calls = _drive_real_parser(
            tmp_path, monkeypatch, rows=self._rows("alfa", "bravo", "charlie"),
            endpoint="https://hub/upload")
        assert [c["rank"] for c in data["candidates"]] == [1, 2, 3]
        assert [c["pdb_key"] for c in data["candidates"]] == [
            "designs/design_001.pdb", "designs/design_002.pdb",
            "designs/design_003.pdb"]
        assert [c[3] for c in calls if c[0] == "request"] == [
            ("design_001.pdb",), ("design_002.pdb",), ("design_003.pdb",)]

    def test_a_design_with_no_pdb_on_disk_leaves_NO_gap(self, tmp_path, monkeypatch):
        """The gap case, through ``find_pdb_for``'s real fallbacks: the two
        BEST designs have no file, so the parsed indices of the survivors are
        2,3,4. Delivered they must be 1,2,3 — rfantibody's ``emitted_rank``
        exists for exactly this, and without it a result set contains no rank
        1 at all while still reporting COMPLETED."""
        rows = self._rows("alfa", "bravo", "charlie", "delta", "echo")
        # rows are worst-first, so the two best are the last two.
        rows[-1] = (rows[-1][0], rows[-1][1], None)
        rows[-2] = (rows[-2][0], rows[-2][1], None)
        data, _ = _drive_real_parser(tmp_path, monkeypatch, rows=rows)

        assert data["n_failures"] == 2
        assert data["designs_completed"] == 3
        assert [c["rank"] for c in data["candidates"]] == [1, 2, 3], (
            "a dropped design left a hole in the delivered ranks")
        assert [c["pdb_key"] for c in data["candidates"]] == [
            "design_001.pdb", "design_002.pdb", "design_003.pdb"]

    def test_an_upload_FAILURE_does_not_burn_a_rank_number(self, tmp_path, monkeypatch):
        """The drop path the siblings do not have. Proteina uploads inside the
        loop and skips the design when the PUT raises, so a rank committed
        before the upload would be spent on a candidate that never shipped —
        moving the gap rather than closing it. The best design's PUT fails
        here; the survivors must still be 1 and 2."""
        rows = self._rows("alfa", "bravo", "charlie")
        data, _ = _drive_real_parser(
            tmp_path, monkeypatch, rows=rows, endpoint="https://hub/upload",
            break_upload_for=("charlie",))
        assert data["n_failures"] == 1
        assert [c["name"] for c in data["candidates"]] == ["bravo", "alfa"]
        assert [c["rank"] for c in data["candidates"]] == [1, 2]
        assert [c["pdb_key"] for c in data["candidates"]] == [
            "designs/design_001.pdb", "designs/design_002.pdb"]

    def test_a_ZERO_BYTE_pdb_does_not_burn_a_rank_number(self, tmp_path, monkeypatch):
        """The third drop path, inline-only: a file that reads as b"" is a
        failed design, not a delivered one, so it may not consume a rank
        either."""
        rows = self._rows("alfa", "bravo", "charlie")
        rows[-1] = (rows[-1][0], rows[-1][1], b"")   # the best design
        data, _ = _drive_real_parser(tmp_path, monkeypatch, rows=rows)
        assert data["n_failures"] == 1
        assert [c["name"] for c in data["candidates"]] == ["bravo", "alfa"]
        assert [c["rank"] for c in data["candidates"]] == [1, 2]
        assert [c["pdb_key"] for c in data["candidates"]] == [
            "design_001.pdb", "design_002.pdb"]

    def test_the_pdb_key_and_the_rank_can_never_disagree(self, tmp_path, monkeypatch):
        """The pair is what the UI and the exporter read, and they are built in
        two places (candidate + designs list). Pinned as an invariant over a
        run that exercises a drop, on BOTH delivery modes, rather than as two
        more hardcoded lists."""
        for endpoint in ("", "https://hub/upload"):
            rows = self._rows("alfa", "bravo", "charlie", "delta")
            rows[-1] = (rows[-1][0], rows[-1][1], None)
            data, _ = _drive_real_parser(
                tmp_path / f"m{bool(endpoint)}", monkeypatch, rows=rows,
                endpoint=endpoint)
            assert data["candidates"], "guard must run against a populated list"
            for cand, design in zip(data["candidates"], data["designs"]):
                expected = f"design_{cand['rank']:03d}.pdb"
                assert cand["pdb_key"].rsplit("/", 1)[-1] == expected
                assert design["rank"] == cand["rank"]
                assert design["pdb_key"] == cand["pdb_key"]

    def test_the_heartbeat_carries_the_delivered_rank(self, tmp_path, monkeypatch):
        """The live UI streams candidates off the heartbeat before the result
        exists, so a heartbeat still announcing rank 0 would show the top
        design as "0" for the whole run and then silently change."""
        data, calls = _drive_real_parser(
            tmp_path, monkeypatch, rows=self._rows("alfa", "bravo", "charlie"))
        beats = [c[2] for c in calls if c[0] == "heartbeat" and c[2]]
        assert [b["rank"] for b in beats] == [1, 2, 3]
        assert [b["rank"] for b in beats] == [
            c["rank"] for c in data["candidates"]]

    def test_the_OPERATOR_S_FILES_start_at_design_001(self, tmp_path, monkeypatch):
        """END TO END, through the real ``cmd_collect``: the thing this parity
        work is for is a direct ``modal.Function.from_name`` call whose output
        directory an operator then reads. It used to start at design_000.pdb —
        and after any drop at design_002.pdb, indistinguishable from an offset.

        The shard result is PRODUCED by main(), not hand-written, so the two
        halves of the contract are pinned against each other."""
        rows = self._rows("alfa", "bravo", "charlie", "delta")
        rows[-1] = (rows[-1][0], rows[-1][1], None)   # drop the best design
        smoke, _ = _drive_real_parser(tmp_path / "shard", monkeypatch, rows=rows)

        rc, outdir, _ = _drive_collect(tmp_path / "op", monkeypatch, smoke)
        assert rc == 0
        assert sorted(p.name for p in outdir.glob("*.pdb")) == [
            "design_001.pdb", "design_002.pdb", "design_003.pdb"]
        assert (outdir / "design_001.pdb").read_bytes() == self._BODY


class TestInlineSizeCap:
    """Modal blob-uploads any return over MAX_OBJECT_SIZE_BYTES (2 MiB)
    transparently — see _utils/blob_utils.format_blob_data, reached from the
    container's return path in container_io_manager.package_output — so
    inlining cannot fail on size. Multi-MB returns are still wasteful, and a
    cap that silently truncated the result set would be worse than no cap."""

    def test_designs_over_the_cap_keep_scores_and_lose_only_atoms(self, tmp_path, monkeypatch):
        # Cap must clear INLINE_PDB_MIN_USEFUL_CAP_BYTES or the run is refused
        # pre-GPU instead: 2 designs fit in 13000, the 3rd and 4th do not.
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="", designs=4,
            pdb_body=b"X" * 6000, cap_bytes=13000)
        assert data["designs_completed"] == 4, "a capped design is still delivered"
        assert data["n_failures"] == 0, "over-cap is not a failure"
        inlined = [c for c in data["candidates"] if "pdb_content_b64" in c]
        assert len(inlined) == 2, "the cap must bound how many designs carry atoms"
        assert len(data["candidates"]) == 4, "capped candidates are still listed"
        for d in data["designs"]:
            assert d["total_reward"] is not None
        for c in data["candidates"]:
            assert c["scores"]["total_reward"] is not None

    def test_the_cap_is_generous_enough_for_a_real_fc_shard(self):
        """A 419-residue target plus binder is ~340 KB of PDB and
        nsamples*replicas defaults to 8, so a real Fc shard is ~2.7 MB. The
        default must not clip the campaign this work exists to serve.

        Asserts the DEFAULT, not the env-seeded live constant, which any CI
        runner exporting PROTEINA_INLINE_PDB_CAP_BYTES would fail for an
        unrelated reason."""
        assert rp.INLINE_PDB_DEFAULT_CAP_BYTES >= 8 * 350_000

    def test_a_malformed_cap_falls_back_instead_of_crashing(self, monkeypatch):
        """The helper's own contract. See the sibling test below for the
        import-time claim this one does NOT make."""
        monkeypatch.setenv("PROTEINA_INLINE_PDB_CAP_BYTES", "64MB")
        assert rp._inline_cap_bytes() == rp.INLINE_PDB_DEFAULT_CAP_BYTES
        monkeypatch.setenv("PROTEINA_INLINE_PDB_CAP_BYTES", "   ")
        assert rp._inline_cap_bytes() == rp.INLINE_PDB_DEFAULT_CAP_BYTES
        monkeypatch.setenv("PROTEINA_INLINE_PDB_CAP_BYTES", "12345")
        assert rp._inline_cap_bytes() == 12345

    def test_a_malformed_cap_does_not_crash_MODULE_IMPORT(self):
        """The defect the test above is NAMED for but never exercised.

        Calling ``_inline_cap_bytes()`` proves nothing about import: collapsing
        the module-scope assignment back to a bare
        ``int(os.environ.get(...) or DEFAULT)`` reintroduces the exact crash —
        ValueError at import, BEFORE ``_fail`` can write
        /tmp/smoke_results.json, so modal_app's ``json.load`` finds nothing and
        a mistyped env var is reported as a webhook delivery failure on an
        already-billing GPU — and left the whole suite green.

        A real subprocess import is the only shape that can fail against that,
        because reloading in-process cannot reproduce a module that never
        finished executing.
        """
        import subprocess
        import sys as _sys
        from pathlib import Path as _Path

        env = dict(os.environ, PROTEINA_INLINE_PDB_CAP_BYTES="64MB")
        proc = subprocess.run(
            [_sys.executable, "-c",
             "import tools.proteina.run_pipeline as rp;"
             "print(rp.INLINE_PDB_TOTAL_CAP_BYTES,"
             " rp.INLINE_PDB_DEFAULT_CAP_BYTES)"],
            cwd=str(_Path(__file__).resolve().parents[1]),
            env=env, capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            "a typo'd PROTEINA_INLINE_PDB_CAP_BYTES killed the module at "
            f"import:\n{proc.stderr}")
        live, default = proc.stdout.split()
        assert live == default, "the malformed cap was not replaced by the default"

    def test_a_cap_that_admits_NOTHING_fails_instead_of_reporting_COMPLETED(
            self, tmp_path, monkeypatch):
        """THE HOLE IN THE PRE-GPU FLOOR. INLINE_PDB_MIN_USEFUL_CAP_BYTES is
        10 KiB and a real design PDB is ~340 KB, so every cap in between clears
        the pre-GPU gate, spends the whole A100 shard, and used to return
        status COMPLETED with a full designs_completed count and candidates
        whose bare-filename pdb_key is backed by nothing at all — no Storage
        object and no inline copy. The counters that knew existed only inside
        logger calls, so no caller could tell this from a good run.

        No pre-GPU threshold can close it (a PDB's size is target-dependent and
        unknowable before the run), so the verdict is post-loop, where the real
        sizes are known: inlined nothing while the cap dropped designs is a
        FAILED delivery, not a completed one."""
        # 20 KB designs under a 11 KB cap: over the 10 KiB pre-GPU floor, so
        # the run starts, and under one design, so nothing is ever admitted.
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="", designs=4,
            pdb_body=b"X" * 20000, cap_bytes=11000, expect_exit=True)
        assert data["status"] == "FAILED", (
            "a shard that delivered zero coordinates is not COMPLETED")
        assert data["error"]["check"] == "inline_cap_admitted_nothing"
        assert data["inline_delivery"]["n_inlined"] == 0
        assert data["inline_delivery"]["n_inline_capped"] == 4
        # The science survives the delivery failure.
        assert len(data["candidates"]) == 4
        assert all(c["scores"]["total_reward"] is not None
                   for c in data["candidates"])

    def test_the_counters_are_machine_readable_not_log_only(self, tmp_path, monkeypatch):
        """A partial cap is NOT a failure — some designs carry atoms — but the
        caller still has to be able to see that some do not, without parsing
        container logs it may never receive."""
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="", designs=4,
            pdb_body=b"X" * 6000, cap_bytes=13000)
        assert data["status"] == "COMPLETED"
        assert data["inline_delivery"] == {
            "n_inlined": 2, "n_inline_capped": 2,
            "inline_bytes_used": 12000, "cap_bytes": 13000,
        }

    def test_a_zero_design_shard_is_not_a_delivery_failure(self, tmp_path, monkeypatch):
        """The verdict keys on "the cap dropped designs", not on "no atoms came
        back". A shard that produced nothing has nothing to deliver and must
        stay COMPLETED, exactly as before."""
        monkeypatch.setattr(rp, "parse_designs", lambda run_dir: [])
        data, _ = _drive_design_loop(tmp_path, monkeypatch, endpoint="",
                                     designs=0)
        assert data["status"] == "COMPLETED"
        assert data["inline_delivery"]["n_inline_capped"] == 0

    def test_a_ZERO_BYTE_pdb_is_not_counted_as_a_delivered_design(
            self, tmp_path, monkeypatch):
        """THE COUNTERS MUST NOT CERTIFY A BLOB THEY DID NOT LOOK AT.

        ``read_bytes()`` on a truncated or not-yet-written file returns ``b""``
        and raises nothing, so an empty design slid past the read/upload except;
        the cap test ``inline_bytes_used + 0 <= CAP`` is then trivially true, an
        EMPTY base64 string was attached, and ``n_inlined`` incremented for it.
        With every design empty the shard reported COMPLETED, 8 designs, 0
        failures and "8 inlined (0.0 MB)" — a delivery of nothing, certified by
        the very counters a caller reads instead of parsing logs, with no
        ``error`` key to contradict it. The post-loop verdict cannot catch it:
        it fires only when the CAP dropped designs, and here it dropped none.

        ``n_inlined == 8`` beside ``inline_bytes_used == 0`` is an impossible
        pair, which is the shape of the lie."""
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="", designs=8, pdb_body=b"")

        assert data["inline_delivery"]["n_inlined"] == 0, (
            "a design with no atoms was counted as inlined: "
            f"{data['inline_delivery']}")
        assert data["n_failures"] == 8, (
            "an empty PDB is a failed design, the same as one that could not "
            "be read at all")
        assert data["designs_completed"] == 0
        assert not (data["status"] == "COMPLETED"
                    and data["designs_completed"]), (
            "the shard reported completed designs while delivering no "
            "coordinates at all")
        assert data["candidates"] == [], (
            "a candidate carrying an empty pdb_content_b64 is a pointer at "
            "nothing — cmd_collect filters it out and rc=1, but any consumer "
            "trusting the counters is told the delivery succeeded")

    def test_a_NON_empty_pdb_is_still_delivered_unchanged(
            self, tmp_path, monkeypatch):
        """The control that stops the guard above from being satisfied by
        refusing everything."""
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="", designs=8,
            pdb_body=b"ATOM  x\nEND\n")
        assert data["inline_delivery"]["n_inlined"] == 8
        assert data["n_failures"] == 0
        assert all(base64.b64decode(c["pdb_content_b64"])
                   for c in data["candidates"])

    def test_the_UPLOAD_path_treats_an_empty_pdb_exactly_as_before(
            self, tmp_path, monkeypatch):
        """INVARIANT, stated rather than assumed. The same weakness exists on
        the upload path — empty bytes are PUT and counted as a completed design
        — and it is NOT fixed here, deliberately: changing it would change what
        the production web tier reports (both ``designs_completed`` and
        ``n_failures``) for a shape no one has observed. This pins the untouched
        behaviour so the scoping is visible, and so a later change to it is a
        decision rather than an accident."""
        data, calls = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload", designs=4,
            pdb_body=b"")
        assert data["status"] == "COMPLETED"
        assert data["designs_completed"] == 4
        assert data["n_failures"] == 0
        assert len([c for c in calls if c[0] == "put"]) == 4
        assert "inline_delivery" not in data

    def test_the_web_path_result_grows_no_inline_delivery_key(self, tmp_path, monkeypatch):
        """INVARIANT. The accounting is inline-only; a real web job's result
        dict must be exactly what it was."""
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload", designs=4,
            pdb_body=b"X" * 20000, cap_bytes=11000)
        assert "inline_delivery" not in data
        assert data["status"] == "COMPLETED"
        assert set(data) == {
            "status", "tier", "designs_total", "designs_completed",
            "n_failures", "designs", "candidates", "runtime_seconds",
            "provider_job_id",
        }

    def test_a_zero_cap_is_refused_pre_gpu_not_after(self, tmp_path, monkeypatch):
        """"0" is truthy, so `env or default` does NOT fall back — the cap
        really is 0 while _inline_enabled() is still True. Without this gate
        the run spends an A100 and returns COMPLETED with zero structures,
        which is the sharpest version of the failure this file guards."""
        result_file = tmp_path / "smoke.json"
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))
        monkeypatch.setattr(rp, "INLINE_PDB_TOTAL_CAP_BYTES", 0)
        monkeypatch.delenv("PROTEINA_INLINE_PDBS", raising=False)
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps({
            "job_spec": {
                "config_name": "search_binder_local_pipeline",
                "task_name": "02_PDL1", "rf3_required": False,
                "nsamples": 4, "replicas": 2,
            },
            "input_presigned_url": "", "upload_urls_endpoint": "",
            "job_token": "", "tier": "protein_binder"}))
        monkeypatch.setenv("JOB_TIER", "protein_binder")
        monkeypatch.setenv("JOB_ID", "job-zerocap")
        monkeypatch.setenv("PROTEINA_RF3", "on")
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        with pytest.raises(SystemExit):
            rp.main()
        data = json.loads(result_file.read_text())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "upload_urls_endpoint"
        assert "cap" in data["error"]["detail"].lower()

    def test_the_off_switch_has_no_effect_when_an_endpoint_is_present(self, tmp_path, monkeypatch):
        """Pins the flag's REAL semantics rather than implying a mode that no
        longer exists. Inlining is exclusive with uploading, so with an
        endpoint the result is the same either way — there is no "scores
        inline but not the atoms" configuration. Asserted as an equality
        between the two settings so it cannot pass vacuously."""
        on, on_calls = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload", inline_env="on")
        off, off_calls = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload", inline_env="off")
        assert on == off, "the flag changed a web-path result"
        assert len([c for c in on_calls if c[0] == "put"]) == 2
        assert len([c for c in off_calls if c[0] == "put"]) == 2
        assert all("pdb_content_b64" not in c for c in on["candidates"])

    def test_a_cap_too_small_for_one_design_is_refused_pre_gpu(self, tmp_path, monkeypatch):
        """`> 0` was too weak: a 1-byte cap passed it, admitted nothing, and
        still spent the A100 to return COMPLETED with zero structures."""
        result_file = tmp_path / "smoke.json"
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))
        monkeypatch.setattr(rp, "INLINE_PDB_TOTAL_CAP_BYTES", 1)
        monkeypatch.delenv("PROTEINA_INLINE_PDBS", raising=False)
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps({
            "job_spec": {
                "config_name": "search_binder_local_pipeline",
                "task_name": "02_PDL1", "rf3_required": False,
                "nsamples": 4, "replicas": 2,
            },
            "input_presigned_url": "", "upload_urls_endpoint": "",
            "job_token": "", "tier": "protein_binder"}))
        monkeypatch.setenv("JOB_TIER", "protein_binder")
        monkeypatch.setenv("JOB_ID", "job-tinycap")
        monkeypatch.setenv("PROTEINA_RF3", "on")
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        with pytest.raises(SystemExit):
            rp.main()
        data = json.loads(result_file.read_text())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "upload_urls_endpoint"


class TestJobSpecAliases:
    """The campaign side and the other three generators spell these fields
    differently than this file does. Both must work; native wins on conflict.
    Contract: llm-proteinDesigner/docs/MULTI-CHAIN-TARGETS.md."""

    @pytest.mark.parametrize("raw,expected", [
        ("A", "A"),
        ("A B", "A B"),
        ("A,B", "A B"),
        ("A, B", "A B"),
        ("  A , B ", "A B"),
        ("A,B,C", "A B C"),
        ("", ""),
    ])
    def test_both_chain_separators_are_accepted(self, raw, expected):
        assert rp.normalize_target_chain(raw) == expected

    def test_chain_order_is_preserved(self):
        """Order drives contig segment order, so it is significant."""
        assert rp.normalize_target_chain("B,A") == "B A"

    def test_duplicate_chains_are_removed(self):
        assert rp.normalize_target_chain("A,B,A") == "A B"

    def test_the_quiet_case_a_mixed_separator_string(self):
        """"A,B" alone was already LOUD: it split to one token, matched no
        chain, and derive_segments returned [] so the caller was told the chain
        was absent. "A B,C" is the quiet one — chain A resolved, "B,C" was
        dropped by derive_segments' continue, and the run designed against one
        protomer of a dimer while looking entirely successful."""
        assert rp.normalize_target_chain("A B,C") == "A B C"

    def test_native_hotspot_spec_still_works(self):
        assert rp.normalize_hotspots(
            {"hotspot_spec": ["A241", "B241"]}) == ["A241", "B241"]

    def test_shared_hotspot_residues_is_accepted(self):
        assert rp.normalize_hotspots(
            {"hotspot_residues": ["A241", "B241"]}) == ["A241", "B241"]

    def test_native_wins_when_both_are_present(self):
        """Nothing already in flight may change meaning."""
        assert rp.normalize_hotspots(
            {"hotspot_spec": ["A1"], "hotspot_residues": ["B9"]}) == ["A1"]

    def test_an_empty_native_key_falls_through_to_the_alias(self):
        """The ONLY shape that reaches the alias branch in anger, and the one
        a direct caller sending just hotspot_residues produces."""
        assert rp.normalize_hotspots(
            {"hotspot_spec": [], "hotspot_residues": ["A241", "B241"]}
        ) == ["A241", "B241"]

    @pytest.mark.parametrize("empty", ["", " ", [""], ("",), (), ",", " , "])
    def test_an_empty_native_key_falls_through_in_EVERY_shape(self, empty):
        """The fallthrough used to be ``raw is None or raw == []``, which is
        True for exactly two shapes. Every other way of being empty — the
        empty-string placeholder a templated job_spec produces for an unused
        field being the obvious one — read as "hotspots were supplied and there
        are none", so the alias beside it was never consulted and every hotspot
        it carried was silently discarded.

        Nothing then complains: missing_hotspots([]) is trivially empty, the
        "all N matched" line is skipped because the spec is falsy, and
        build_target_add_cmd omits --hotspot-residues entirely, so upstream gets
        an all-zero hotspot mask. The A100 runs a completely unconstrained
        search to a successful COMPLETED."""
        assert rp.normalize_hotspots(
            {"hotspot_spec": empty, "hotspot_residues": ["A241", "B241"]}
        ) == ["A241", "B241"], f"the alias was discarded by hotspot_spec={empty!r}"

    def test_a_native_key_that_yields_tokens_still_wins(self):
        """The fallthrough must not become "prefer the alias": emptiness is
        decided by the tokens a field yields, and a field that yields any token
        still wins outright."""
        assert rp.normalize_hotspots(
            {"hotspot_spec": " A1 , A2 ", "hotspot_residues": ["B9"]}
        ) == ["A1", "A2"]

    @pytest.mark.parametrize("bad", [264, True, 3.5, {"a": 1}])
    @pytest.mark.parametrize("field", ["hotspot_spec", "hotspot_residues"])
    def test_a_mistyped_hotspot_field_is_refused_by_TYPE_not_by_traceback(
            self, field, bad):
        """``264`` is a natural shape for a one-hotspot run. It used to reach
        ``for h in raw`` and raise ``TypeError: 'int' object is not iterable``
        — see the sibling test for why that specific exception was expensive.
        Refused rather than wrapped in a list: shape-guessing on the hotspot
        field is the same class of helpfulness that produces a silent mis-aim,
        and the message has to name the field and the two shapes that ARE
        accepted, so it is actionable without reading this source."""
        with pytest.raises(TypeError) as exc:
            rp.normalize_hotspots({field: bad})
        msg = str(exc.value)
        assert field in msg
        assert '["A264", "B264"]' in msg and '"A264 B264"' in msg, (
            f"the refusal does not say what to send instead: {msg}")

    def test_a_DICT_hotspot_field_never_invents_a_token(self):
        """The mistyped shape that did NOT crash, and was worse for it:
        ``for h in {"a": 1}`` iterates KEYS, so the old tokeniser returned
        ``["a"]`` — a hotspot the caller never wrote, which then matches
        nothing and leaves the search unconstrained while the run reports
        success. It must refuse, not yield."""
        with pytest.raises(TypeError):
            rp.normalize_hotspots({"hotspot_spec": {"a": 1}})

    def test_a_scalar_hotspot_field_still_writes_a_RESULT_FILE(
            self, tmp_path, monkeypatch):
        """WHY THE TYPE MATTERS. main() caught only ValueError, so the
        TypeError propagated straight out: the process exited non-zero without
        ever calling _fail, no /tmp/smoke_results.json was written, modal_app's
        json.load found nothing and swallowed the FileNotFoundError, and the
        caller saw `smoke_result: None` — an opaque delivery failure for a
        mistyped field, on a container already allocated and billing. That is
        exactly the failure _inline_cap_bytes' docstring says it exists to
        prevent, left open one function away."""
        result_file = tmp_path / "smoke.json"
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps({
            "job_spec": {
                "config_name": "search_binder_local_pipeline", "task_name": "",
                "target_source": "custom", "target_chain": "A",
                "hotspot_residues": 264, "rf3_required": False,
                "nsamples": 4, "replicas": 2,
            },
            "input_presigned_url": "https://example/t.pdb",
            "upload_urls_endpoint": "https://hub/upload",
            "job_token": "t", "tier": "protein_binder"}))
        monkeypatch.setenv("JOB_TIER", "protein_binder")
        monkeypatch.setenv("JOB_ID", "job-scalar")
        monkeypatch.setenv("PROTEINA_RF3", "on")
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        with pytest.raises(SystemExit):
            rp.main()
        assert result_file.exists(), (
            "the container died with no result file — the hub reports that as "
            "a webhook delivery failure, not as a bad hotspot field")
        data = json.loads(result_file.read_text())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "hotspot_malformed"

    def test_bare_ints_attach_to_a_single_target_chain(self):
        """Upstream matches f"{chain_id}{res_id}", so a bare 264 addresses
        nothing at all. Attributing it is the documented shared contract and
        is exactly the historical single-chain behaviour."""
        assert rp.normalize_hotspots(
            {"hotspot_residues": [264, 301], "target_chain": "A"}) == ["A264", "A301"]

    def test_bare_ints_on_a_MULTI_chain_target_are_refused(self):
        """THE SILENT MIS-AIM. "Attribute to the first chain" is unambiguous
        only for one chain. On a dimer, 264 -> A264; missing_hotspots is a set
        membership test and a real dimer genuinely contains A264, so the guard
        passes, the log reports every hotspot matched, and the run designs
        against protomer A with B completely unconstrained — indistinguishable
        from a correct run. On a symmetric Fc the 16 tokens collapse to 8."""
        with pytest.raises(ValueError) as exc:
            rp.normalize_hotspots(
                {"hotspot_residues": [264, 301], "target_chain": "A,B"})
        assert "no chain prefix" in str(exc.value)
        assert "A264" in str(exc.value), "the message must show the fix"

    def test_the_real_fc_hotspots_in_bare_form_are_refused(self):
        """The exact 16-number set for this campaign, unprefixed. Both
        protomers use identical numbering, so silently prefixing them all with
        A yields 8 distinct tokens and a fully unconstrained B."""
        with pytest.raises(ValueError):
            rp.normalize_hotspots({
                "target_chain": "A,B",
                "hotspot_residues": [241, 243, 244, 246, 260, 262, 264, 301,
                                     241, 243, 244, 246, 260, 262, 264, 301],
            })

    def test_the_refusal_reaches_main_as_a_pre_gpu_failure(self, tmp_path, monkeypatch):
        """A ValueError that escaped main() would crash the container with no
        result file instead of reporting a refusal."""
        result_file = tmp_path / "smoke.json"
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps({
            "job_spec": {
                "config_name": "search_binder_local_pipeline", "task_name": "",
                "target_source": "custom", "target_chain": "A,B",
                "hotspot_residues": [264, 301], "rf3_required": False,
                "nsamples": 4, "replicas": 2,
            },
            "input_presigned_url": "https://example/t.pdb",
            "upload_urls_endpoint": "https://hub/upload",
            "job_token": "t", "tier": "protein_binder"}))
        monkeypatch.setenv("JOB_TIER", "protein_binder")
        monkeypatch.setenv("JOB_ID", "job-ambig")
        monkeypatch.setenv("PROTEINA_RF3", "on")
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        with pytest.raises(SystemExit):
            rp.main()
        data = json.loads(result_file.read_text())
        assert data["status"] == "FAILED"
        assert data["error"]["check"] == "hotspot_chain_ambiguous"

    def test_a_multi_chain_CONTIG_also_refuses_bare_ints(self):
        """prepare_custom_target derives its segments from target_input when
        present and ignores target_chain entirely, so counting target_chain
        alone let a one-chain declaration hide a two-chain contig: bare
        hotspots promoted to A, second protomer unconstrained, no error."""
        with pytest.raises(ValueError):
            rp.normalize_hotspots({
                "target_chain": "A",
                "target_input": "A1-200,B1-200",
                "hotspot_residues": [264, 301],
            })

    def test_a_single_chain_contig_still_allows_bare_ints(self):
        """One chain named twice is one chain."""
        assert rp.normalize_hotspots({
            "target_chain": "A", "target_input": "A1-200",
            "hotspot_residues": [264],
        }) == ["A264"]

    def test_a_contig_REPLACES_target_chain_rather_than_adding_to_it(self):
        """A PHANTOM CHAIN MUST NOT INFLATE THE COUNT. ``prepare_custom_target``
        derives its segments from ``target_input`` when present and never looks
        at ``target_chain`` again, so with a contig the contig's chains ARE the
        design. Counting both invented a protomer: proteina's shipped default
        ``"A"`` over a structure whose chain is C — the normal shape, because
        the form tells the user to leave that field alone and name their chains
        in the contig — has exactly ONE chain, so a bare 264 is unambiguous.
        The union counted two, refused the run, and suggested ``A264``: a
        residue on a chain the upload does not contain. ``shared/pdb_inspect``
        made this same replace-not-union correction on main (0cbfea6) for this
        same input shape."""
        assert rp.normalize_hotspots({
            "target_chain": "A", "target_input": "C1-200",
            "hotspot_residues": [264],
        }) == ["C264"]

    def test_replacing_does_not_weaken_the_case_the_union_was_added_for(self):
        """``{"target_chain": "A", "target_input": "A1-200,B1-200"}`` is the
        input that motivated counting both fields. It never needed the union:
        read from the CONTIG ALONE it is still two chains, so the refusal that
        stops a bare hotspot being promoted to A with the second protomer
        unconstrained is untouched. Asserted here as well as beside its own test
        because it is the thing the fix above could plausibly have broken."""
        with pytest.raises(ValueError) as exc:
            rp.normalize_hotspots({
                "target_chain": "A", "target_input": "A1-200,B1-200",
                "hotspot_residues": [264],
            })
        assert "2 chains (A B)" in str(exc.value), str(exc.value)

    def test_a_contig_that_names_NOTHING_falls_back_to_target_chain(self):
        """Replacement is conditional on the contig actually naming a chain, so
        a degenerate contig cannot silently drop the count to zero and let a
        bare token past the ambiguity check unexamined."""
        assert rp.normalize_hotspots({
            "target_chain": "A", "target_input": ",",
            "hotspot_residues": [264],
        }) == ["A264"]

    @pytest.mark.parametrize("value", [296, 296.0])
    def test_a_WHOLE_NUMBER_FLOAT_is_a_residue_number_not_a_token(self, value):
        """``str(296.0)`` is ``"296.0"``, which the bare-integer regex does not
        match, so a float hotspot was neither attributed to its chain nor
        refused as ambiguous — it travelled on as the literal ``"296.0"``, and
        upstream matches ``f"{chain_id}{res_id}"``, so it addressed nothing that
        can exist. main's 0cbfea6 restored exactly this shape on the web
        preflight side ("a JSON body sending a whole number as a float is the
        shape that reaches it"); the two sides of the container boundary must
        not disagree about it."""
        assert rp.normalize_hotspots(
            {"hotspot_residues": [value], "target_chain": "A"}) == ["A296"]

    def test_a_FLOAT_does_not_slip_past_the_multi_chain_refusal(self):
        """THE REASON THIS IS NOT COSMETIC. The bare-integer refusal is the one
        guard between a caller and a silently half-aimed dimer, and a float
        walked straight through it while the identical int was refused."""
        with pytest.raises(ValueError):
            rp.normalize_hotspots({
                "hotspot_residues": [296.0], "target_chain": "A",
                "target_input": "A1-200,B1-200",
            })

    def test_a_float_with_no_chain_known_is_still_left_bare_but_NORMALISED(self):
        """It must stop being a float without acquiring a chain nobody named —
        ``missing_hotspots`` then refuses ``296`` and says something true about
        it, instead of blaming ``296.0`` for being outside the region."""
        assert rp.normalize_hotspots({"hotspot_residues": [296.0]}) == ["296"]

    def test_a_FRACTIONAL_float_is_refused_rather_than_truncated(self):
        """A DELIBERATE DIVERGENCE from ``shared/pdb_inspect.split_hotspot``,
        which truncates. No residue is numbered 296.7, so resolving it means
        guessing which one was meant, and guessing on the hotspot field is the
        failure class this normaliser exists to stop. It stops nothing real: the
        web adapter's ``_parse_hotspots`` yields ints from a regex and can never
        emit one, and today such a token is refused anyway — by
        ``missing_hotspots``, with a message that blames the wrong thing."""
        with pytest.raises(TypeError) as exc:
            rp.normalize_hotspots(
                {"hotspot_residues": [296.7], "target_chain": "A"})
        assert "296" in str(exc.value) and "297" in str(exc.value), (
            f"the refusal must name the two residues it will not choose "
            f"between: {exc.value}")

    def test_a_BOOL_element_never_becomes_residue_1_or_the_token_True(self):
        """``bool`` subclasses int, so a truncating rule would read ``True`` as
        residue 1, and ``str(True)`` is the token ``"True"`` — a hotspot the
        caller never wrote. ``split_hotspot`` refuses it for the same reason."""
        with pytest.raises(TypeError):
            rp.normalize_hotspots(
                {"hotspot_residues": [True], "target_chain": "A"})

    def test_a_float_hotspot_reaches_main_as_a_pre_GPU_refusal(
            self, tmp_path, monkeypatch):
        """WIRING, and the exit shape: a TypeError raised out of the tokeniser
        must land as a clean ``_fail`` with a result file, not as a container
        that dies before ``/tmp/smoke_results.json`` exists."""
        result_file = tmp_path / "smoke.json"
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps({
            "job_spec": {
                "config_name": "search_binder_local_pipeline", "task_name": "",
                "target_source": "custom", "target_chain": "A",
                "hotspot_residues": [296.7], "rf3_required": False,
                "nsamples": 4, "replicas": 2,
            },
            "input_presigned_url": "https://example/t.pdb",
            "upload_urls_endpoint": "https://hub/upload",
            "job_token": "t", "tier": "protein_binder"}))
        monkeypatch.setenv("JOB_TIER", "protein_binder")
        monkeypatch.setenv("JOB_ID", "job-float")
        monkeypatch.setenv("PROTEINA_RF3", "on")
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        with pytest.raises(SystemExit):
            rp.main()
        assert json.loads(
            result_file.read_text())["error"]["check"] == "hotspot_malformed"

    def test_a_malformed_contig_defers_rather_than_crashing(self):
        """parse_target_input has its own pre-GPU refusal with a better
        message; this guard must not pre-empt it with a ValueError of its
        own about hotspots."""
        assert rp.normalize_hotspots({
            "target_chain": "A", "target_input": "not-a-contig",
            "hotspot_residues": ["A264"],
        }) == ["A264"]

    def test_prefixed_hotspots_on_a_multi_chain_target_are_fine(self):
        """The refusal is about ambiguity, not about multi-chain."""
        assert rp.normalize_hotspots(
            {"hotspot_residues": ["A264", "B264"], "target_chain": "A,B"}
        ) == ["A264", "B264"]

    def test_prefixed_and_bare_may_be_mixed_on_one_chain(self):
        assert rp.normalize_hotspots(
            {"hotspot_residues": ["A264", 301], "target_chain": "A"}) == ["A264", "A301"]

    def test_main_actually_uses_the_normalizers(self, tmp_path, monkeypatch):
        """WIRING. Both normalizers were tested only as pure functions;
        deleting either call site in main() left every alias test green."""
        seen = {}
        monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(tmp_path / "s.json"))

        def spy(**kw):
            seen.update(kw)
            raise SystemExit(0)  # stop before any GPU work

        monkeypatch.setattr(rp, "prepare_custom_target", spy)
        monkeypatch.setattr(rp, "archive_raw_outputs", lambda *a, **k: None)
        monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: None)
        monkeypatch.setenv("JOB_PAYLOAD", json.dumps({
            "job_spec": {
                "config_name": "search_binder_local_pipeline", "task_name": "",
                "target_source": "custom", "target_chain": "A,B",
                "hotspot_residues": ["A241", "B241"], "rf3_required": False,
                "nsamples": 4, "replicas": 2,
            },
            "input_presigned_url": "https://example/t.pdb",
            "upload_urls_endpoint": "https://hub/upload",
            "job_token": "t", "tier": "protein_binder"}))
        monkeypatch.setenv("JOB_TIER", "protein_binder")
        monkeypatch.setenv("JOB_ID", "job-wiring")
        monkeypatch.setenv("PROTEINA_RF3", "on")
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        with pytest.raises(SystemExit):
            rp.main()
        assert seen["target_chain"] == "A B", "comma form never normalized"
        assert seen["hotspot_spec"] == ["A241", "B241"], "alias never resolved"

    def test_a_bare_int_with_no_chain_is_left_alone(self):
        """Passed through untouched rather than guessed at, so the pre-GPU
        missing_hotspots guard refuses it instead of aiming somewhere wrong."""
        assert rp.normalize_hotspots({"hotspot_residues": [264]}) == ["264"]

    def test_a_hotspot_string_is_split(self):
        assert rp.normalize_hotspots(
            {"hotspot_residues": "A241 A243,A244"}) == ["A241", "A243", "A244"]

    def test_absent_hotspots_are_empty(self):
        assert rp.normalize_hotspots({}) == []
        assert rp.normalize_hotspots({"hotspot_spec": []}) == []

    def test_the_real_campaign_hotspots_survive_round_trip(self):
        """The 16 EU-numbered Fc hotspots this work exists to aim at, in the
        campaign's canonical shape. Never pre-converted."""
        spec = {
            "target_chain": "A,B",
            "hotspot_residues": [
                "A241", "A243", "A244", "A246", "A260", "A262", "A264", "A301",
                "B241", "B243", "B244", "B246", "B260", "B262", "B264", "B301",
            ],
        }
        assert rp.normalize_target_chain(spec["target_chain"]) == "A B"
        assert rp.normalize_hotspots(spec) == spec["hotspot_residues"]


class TestEveryRequestedChainMustBePresent:
    """A chain the caller asked for and the upload does not contain must be
    REFUSED, not skipped.

    THE ONE FAILURE MODE THAT COSTS BOTH MONEY AND SCIENCE. ``derive_segments``
    ``continue``s past a chain it finds no residues for, and the guard beside it
    only fired when ALL of them were missing, so a two-chain request against a
    one-chain structure produced a healthy one-chain contig and nothing
    downstream ever mentioned the chain that vanished: every later guard reads
    the already-pruned list, and hotspots on a strict subset of the contig's
    chains are legitimate (see the class below), so the run cleared the hotspot
    gate with "all N hotspot(s) matched", billed an A100 and designed binders
    against a monomer. Indistinguishable from a correct run.

    It matters on THIS branch specifically because ``normalize_target_chain``
    turned ``"A,B"`` from one unmatchable token — which emptied ``segments`` and
    tripped the aggregate guard — into two real chains, and ``"A,B"`` is the
    exact literal ``direct_call_fc.build_job_spec`` sends.
    """

    _MONOMER = (("A", 1, 200),)
    _DIMER = (("A", 1, 200), ("B", 1, 200))

    def test_a_missing_chain_is_refused_even_though_another_resolved(
            self, tmp_path, monkeypatch):
        error, staged = _drive_prepare_custom_target(
            tmp_path, monkeypatch, target_chain="A B",
            hotspots=["A100", "A120"], spans=self._MONOMER)
        assert error["check"] == "target_chain", error
        assert "B" in error["detail"], error["detail"]
        assert "A1-200" in error["detail"], (
            "the refusal must show what the upload does contain: "
            f"{error['detail']}")
        assert staged == [], (
            "a target was staged for the design engine with the requested "
            "chain B absent from it")

    def test_the_COMMA_spelling_THE_DRIVER_SENDS_takes_the_same_path(
            self, tmp_path, monkeypatch):
        """``tools/proteina/direct_call_fc.py`` hardcodes ``"A,B"`` with no
        contig, so this is not a hypothetical spelling. Before
        ``normalize_target_chain`` existed, ``"A,B"`` was one token that matched
        nothing and the aggregate guard caught it; normalising it correctly is
        what routed this input into the silent branch."""
        from tools.proteina import direct_call_fc as dc

        spec = dc.build_job_spec(preset="protein_binder", nsamples=4, replicas=2)
        assert not spec.get("target_input"), (
            "the driver grew a contig — this test no longer exercises the "
            "derive_segments branch it exists for")
        error, staged = _drive_prepare_custom_target(
            tmp_path, monkeypatch, target_chain=spec["target_chain"],
            hotspots=["A241"], spans=self._MONOMER)
        assert error["check"] == "target_chain", error
        assert staged == []

    def test_a_ONE_CHARACTER_TYPO_on_a_real_dimer_is_refused(
            self, tmp_path, monkeypatch):
        """The cheapest way to reach this: the structure really is a dimer, the
        request really is multi-chain, and one letter is wrong."""
        error, staged = _drive_prepare_custom_target(
            tmp_path, monkeypatch, target_chain="A,C",
            hotspots=["A100"], spans=self._DIMER)
        assert error["check"] == "target_chain", error
        assert "C" in error["detail"]
        assert staged == []

    def test_the_all_missing_case_still_names_the_whole_request(
            self, tmp_path, monkeypatch):
        """The aggregate refusal is kept, not replaced: when nothing resolves
        there is no "the rest was fine" to report."""
        error, _ = _drive_prepare_custom_target(
            tmp_path, monkeypatch, target_chain="Z", spans=self._MONOMER)
        assert error["check"] == "target_chain", error
        assert "Z" in error["detail"] and "A1-200" in error["detail"]

    @pytest.mark.parametrize("target_chain,spans", [
        ("A", _MONOMER),
        ("A B", _DIMER),
        ("A,B", _DIMER),
    ])
    def test_a_request_whose_chains_ARE_ALL_present_is_not_refused(
            self, tmp_path, monkeypatch, target_chain, spans):
        """NO OVER-REFUSAL, which on this file is its own defect class. These
        run past every input guard and stop at the registry read — a different
        check name — so a guard that started refusing real work would show up
        here rather than in production."""
        error, _ = _drive_prepare_custom_target(
            tmp_path, monkeypatch, target_chain=target_chain,
            hotspots=["A100"], spans=spans)
        assert error["check"] == "target_registry", (
            f"a legitimate request was refused as {error['check']}: {error}")

    def test_an_explicit_contig_still_refuses_per_chain_as_it_always_did(
            self, tmp_path, monkeypatch):
        """The branch this one was made symmetric with. Unchanged, and asserted
        so that "the two branches agree" is checkable rather than claimed."""
        error, staged = _drive_prepare_custom_target(
            tmp_path, monkeypatch, target_chain="A B",
            target_input="A1-200,B1-200", hotspots=["A100"],
            spans=self._MONOMER)
        assert error["check"] == "target_input", error
        assert "B" in error["detail"]
        assert staged == []


class TestTheWebTierIsAFirstClassMultiChainPath:
    """The premise three files used to state — "the web tier cannot express a
    multi-chain target or chain-prefixed hotspots" — is FALSE, and it was load
    bearing: believing it is why the bare-integer ambiguity refusal was put on
    the container's direct-call entry only. These drive the REAL adapter
    (``blueprints/targets.py`` calls exactly this ``validate``) so the
    corrected comments cannot rot back."""

    @staticmethod
    def _form(**over):
        form = {"preset": "protein_binder", "_has_custom_target": "1"}
        form.update(over)
        return form

    def test_the_web_form_accepts_a_multi_chain_contig(self):
        from tools import proteina as adapter
        inp, err = adapter.validate(
            self._form(target_input="A236-443,B236-443"), {})
        assert err is None
        assert inp["target_chain"] == "A B"
        assert inp["target_input"] == "A236-443,B236-443"

    def test_the_web_form_accepts_chain_prefixed_hotspots(self):
        from tools import proteina as adapter
        inp, err = adapter.validate(
            self._form(target_input="A236-443,B236-443",
                       hotspot_residues="A264 B264"), {})
        assert err is None
        assert inp["hotspot_spec"] == ["A264", "B264"]

    def test_a_promoted_hotspot_is_INDISTINGUISHABLE_from_a_typed_one(self):
        """WHY run_pipeline CANNOT CLOSE THIS. ``_parse_hotspots`` promotes a
        bare ``264`` onto ``contig_chains[0]`` before dispatch, so the token
        reaches the container already chain-prefixed and
        ``normalize_hotspots``' ``bare`` list is empty — the refusal never
        fires, ``missing_hotspots`` returns [] because a real dimer genuinely
        contains A264, and the run designs against protomer A with B entirely
        unconstrained while the log reports a full hotspot match.

        This asserts the part that decides WHERE the fix can live: the payload
        a promoted bare hotspot produces is byte-identical to the one a
        deliberately typed ``A264`` produces, ``hotspot_residues`` carrying the
        bare number in both. No container-side rule can separate them, so the
        fix belongs in ``_parse_hotspots`` (refuse a bare token when
        ``len(chain_ids) > 1``). If this test ever fails, the adapter has grown
        a signal the container could act on — go and use it."""
        from tools import proteina as adapter
        promoted, e1 = adapter.validate(
            self._form(target_input="A236-443,B236-443",
                       hotspot_residues="264,301"), {})
        typed, e2 = adapter.validate(
            self._form(target_input="A236-443,B236-443",
                       hotspot_residues="A264,A301"), {})
        assert e1 is None and e2 is None
        assert promoted == typed, (
            "the adapter now distinguishes a promoted hotspot from a typed "
            "one; the container can and should refuse the ambiguous case")
        assert promoted["hotspot_spec"] == ["A264", "A301"]
        assert rp.normalize_hotspots(promoted) == ["A264", "A301"]

    def test_the_same_intent_from_a_DIRECT_caller_is_refused(self):
        """The other half of the asymmetry, so the gap is documented as a gap
        rather than as an oversight: expressed bare — which is what the direct
        path receives, un-promoted — the identical request IS refused."""
        with pytest.raises(ValueError):
            rp.normalize_hotspots({
                "target_input": "A236-443,B236-443",
                "hotspot_residues": [264, 301],
            })

    def test_hotspots_on_a_SUBSET_of_the_contig_chains_are_legitimate(self):
        """WHY THE TEMPTING ONE-LINE FIX IS WRONG, pinned rather than asserted.

        The obvious way to catch the promotion hole container-side is "refuse
        when the hotspots address fewer chains than the contig names" — it
        would fire on the promoted-onto-A case regardless of who did the
        prefixing. It must not be added: designing against ONE epitope of a
        multi-chain complex, with the other chains present as steric context,
        is an ordinary campaign, and both the adapter and the container accept
        it today. The guard would refuse real work in order to catch a case it
        cannot distinguish anyway (the promoted payload is byte-identical to a
        typed one — see the test above).

        This is the pin the comment in run_pipeline.py points at. It did not
        exist before: no test in tests/test_proteina_smoke.py drives a STRICT
        subset — test_multi_chain_hotspots uses ``A113 C73`` and
        test_multi_chain_contig_round_trip uses ``["A12", "B5"]``, both of
        which address every chain the contig names."""
        from tools import proteina as adapter
        inp, err = adapter.validate(
            self._form(target_input="A12-157,B12-157",
                       hotspot_residues="A113 A120"), {})
        assert err is None, err
        assert inp["target_chain"] == "A B"
        assert inp["hotspot_spec"] == ["A113", "A120"]
        assert rp.normalize_hotspots(inp) == ["A113", "A120"], (
            "the container refused hotspots on a subset of the contig's "
            "chains — that is a legitimate single-epitope campaign")


class TestDirectCallDriver:
    """``tools/proteina/direct_call_fc.py`` — the operator-facing driver. Its
    defaults are the whole safety surface, because a mistake here costs an A100
    shard or destroys a previous run's only recoverable copy of its atoms."""

    def test_two_invocations_get_DIFFERENT_job_ids(self):
        """The default used to be the constant "proteina-direct-fc-01", and the
        job id is the ONLY source of shard variation: run_pipeline.shard_seed
        is sha256(job_id) % 1_000_000 and nothing else. A shard is 8 designs
        and a campaign needs more, so re-running --submit is the normal move —
        and with the constant it re-ran the same seed against the same staged
        structure for bit-identical designs and another ~$4-12 of A100.

        Parses through the REAL argparse so a hardcoded default cannot come
        back in either the parser or the resolver."""
        from tools.proteina import direct_call_fc as dc

        ids = set()
        for _ in range(3):
            args = _parse_direct_call_args([])
            ids.add(dc._resolve_job_id(args))
        assert len(ids) == 3, f"default job ids repeat: {ids}"

    def test_two_default_job_ids_get_different_shard_seeds(self):
        """The consequence, asserted against the real seed function rather than
        restated — distinct ids are worthless if they collide downstream."""
        from tools.proteina import direct_call_fc as dc

        a = dc._resolve_job_id(_parse_direct_call_args([]))
        b = dc._resolve_job_id(_parse_direct_call_args([]))
        assert rp.shard_seed(a) != rp.shard_seed(b)

    def test_an_explicit_job_id_is_still_honoured(self):
        from tools.proteina import direct_call_fc as dc
        args = _parse_direct_call_args(["--job-id", "fc-round-2"])
        assert dc._resolve_job_id(args) == "fc-round-2"

    def test_submit_refuses_to_overwrite_an_UNCOLLECTED_call(
            self, tmp_path, monkeypatch):
        """cmd_submit wrote STATE unconditionally, so the previous run's
        call_id — the only handle that can ever reach its designs — was lost,
        and that run could never be --collect'ed.

        The refusal must land BEFORE modal is imported and before the target is
        staged, which is what makes this testable offline at all: no modal, no
        network, no Supabase upload."""
        from tools.proteina import direct_call_fc as dc

        state = tmp_path / "state.json"
        state.write_text(json.dumps(
            {"call_id": "fc-abc123", "job_id": "fc-round-1", "job_spec": {}}))
        monkeypatch.setattr(dc, "STATE", state)
        monkeypatch.setattr(dc, "_load_env_and_path", _explode)
        monkeypatch.setattr(dc, "_stage_target", _explode)

        args = _parse_direct_call_args(["--submit", "--job-id", "fc-round-2"])
        assert dc.cmd_submit(args) == 2
        assert json.loads(state.read_text())["call_id"] == "fc-abc123", (
            "the prior call id was overwritten and is now unreachable")

    def test_submit_refuses_to_REUSE_a_job_id(self, tmp_path, monkeypatch):
        """The second loss the constant default caused:
        modal_app._raw_archive_name is a pure function of the job id and
        _park_raw_archive shutil.move's onto it, so a re-run destroys the
        previous run's raw tree — which is precisely the fallback the inline
        cap names for coordinates it had to drop."""
        from tools.proteina import direct_call_fc as dc

        state = tmp_path / "state.json"
        state.write_text(json.dumps(
            {"call_id": "fc-abc123", "job_id": "fc-round-1", "job_spec": {}}))
        monkeypatch.setattr(dc, "STATE", state)
        monkeypatch.setattr(dc, "_load_env_and_path", _explode)
        monkeypatch.setattr(dc, "_stage_target", _explode)

        args = _parse_direct_call_args(["--submit", "--job-id", "fc-round-1"])
        assert dc.cmd_submit(args) == 2

    def test_a_COLLECTED_call_no_longer_blocks_the_next_submit(
            self, tmp_path, monkeypatch, modal_tripwire):
        """THE GUARD MUST HAVE A NORMAL-PATH EXIT THAT IS NOT --force.

        The refusal above says "run --collect first", but cmd_collect left
        STATE untouched, so collecting satisfied nothing: every submit after
        the first refused, and --force was the only way forward. --force also
        switches off the job-id reuse refusal beside it, so the dead end
        trained the operator straight into the bypass that costs an A100 shard
        and overwrites a raw archive. Since a shard is 8 designs and a campaign
        needs more, "submit again" is the ordinary move, not an exotic one."""
        from tools.proteina import direct_call_fc as dc

        state = tmp_path / "state.json"
        state.write_text(json.dumps(
            {"call_id": "fc-abc123", "job_id": "fc-round-1", "job_spec": {},
             "collected": True}))
        monkeypatch.setattr(dc, "STATE", state)

        # A fresh default job id, no --force. The tripwire is what proves this
        # got past the guards WITHOUT letting it stage or spawn anything.
        with pytest.raises(_ReachedTheModalBoundary):
            dc.cmd_submit(_parse_direct_call_args(["--submit"]))
        assert modal_tripwire == [1], (
            "a collected run still blocked the next submit — the only exit "
            "left is --force, which disables the reuse guard too")

    def test_a_collected_state_still_blocks_REUSING_its_job_id(
            self, tmp_path, monkeypatch):
        """Collecting retires the "you would lose the call id" loss and nothing
        else. The seed collision and the raw-archive overwrite are not undone
        by having read the result, so that refusal ignores ``collected``."""
        from tools.proteina import direct_call_fc as dc

        state = tmp_path / "state.json"
        state.write_text(json.dumps(
            {"call_id": "fc-abc123", "job_id": "fc-round-1", "job_spec": {},
             "collected": True}))
        monkeypatch.setattr(dc, "STATE", state)
        monkeypatch.setattr(dc, "_load_env_and_path", _explode)
        monkeypatch.setattr(dc, "_stage_target", _explode)

        args = _parse_direct_call_args(["--submit", "--job-id", "fc-round-1"])
        assert dc.cmd_submit(args) == 2

    def test_collect_is_what_marks_the_state_collected(
            self, tmp_path, monkeypatch):
        """The other end of the same contract, driven through the REAL
        cmd_collect with modal stubbed — asserting the flag in the submit tests
        alone would pin a state nothing ever produces.

        Marked once the result is in hand, and the call id is KEPT so
        ``--collect`` stays repeatable."""
        import sys as _sys
        import types as _types
        from tools.proteina import direct_call_fc as dc

        state = tmp_path / "state.json"
        state.write_text(json.dumps(
            {"call_id": "fc-abc123", "job_id": "fc-round-1", "job_spec": {}}))
        monkeypatch.setattr(dc, "STATE", state)
        monkeypatch.setattr(dc, "_load_env_and_path", lambda: None)

        pdb = b"ATOM      1  CA  ALA A   1\nEND\n"
        result = {"exit_code": 0, "smoke_result": {
            "status": "COMPLETED", "designs_completed": 1, "designs_total": 1,
            "candidates": [{"rank": 1, "scores": {"total_reward": 1.0},
                            "pdb_content_b64": base64.b64encode(pdb).decode()}],
        }}

        fake_modal = _types.ModuleType("modal")

        class _FunctionCall:
            @staticmethod
            def from_id(call_id):
                assert call_id == "fc-abc123"
                return _types.SimpleNamespace(get=lambda timeout=None: result)

        fake_modal.FunctionCall = _FunctionCall
        monkeypatch.setitem(_sys.modules, "modal", fake_modal)

        rc = dc.cmd_collect(_parse_direct_call_args(
            ["--collect", "--outdir", str(tmp_path / "out")]))
        assert rc == 0
        parked = json.loads(state.read_text())
        assert parked["collected"] is True, (
            "collecting left the state uncollected, so the submit guard's "
            "'run --collect first' advice can never be satisfied")
        assert parked["call_id"] == "fc-abc123", "--collect must stay repeatable"
        assert (tmp_path / "out" / "design_001.pdb").read_bytes() == pdb

    def test_force_is_the_documented_escape_hatch(
            self, tmp_path, monkeypatch, modal_tripwire):
        """The guard must be overridable, or an operator with a genuinely stale
        state file has no way forward but to delete it by hand."""
        from tools.proteina import direct_call_fc as dc

        state = tmp_path / "state.json"
        state.write_text(json.dumps(
            {"call_id": "fc-abc123", "job_id": "fc-round-1", "job_spec": {}}))
        monkeypatch.setattr(dc, "STATE", state)

        args = _parse_direct_call_args(
            ["--submit", "--job-id", "fc-round-1", "--force"])
        # Getting past the guard is the assertion, and the tripwire is what
        # makes "past the guard" observable without staging or spawning.
        with pytest.raises(_ReachedTheModalBoundary):
            dc.cmd_submit(args)
        assert modal_tripwire == [1], "the guard blocked --force"

    def test_a_clean_slate_is_not_blocked(
            self, tmp_path, monkeypatch, modal_tripwire):
        """No state file means nothing to lose; the first submit of a session
        must not need --force."""
        from tools.proteina import direct_call_fc as dc

        monkeypatch.setattr(dc, "STATE", tmp_path / "absent.json")
        with pytest.raises(_ReachedTheModalBoundary):
            dc.cmd_submit(_parse_direct_call_args(["--submit"]))
        assert modal_tripwire == [1]

    def test_no_submit_test_can_reach_a_REAL_spawn(self, tmp_path, monkeypatch,
                                                   modal_tripwire):
        """The tripwire itself, pinned — the three tests above are only as safe
        as it is, and a stub that silently stopped stubbing would restore the
        original leak without any test going red.

        Asserts the ORDER of the boundary too: nothing may leave this machine
        before the refusal guards have had their say, so a refused submit must
        not even resolve a target."""
        from tools.proteina import direct_call_fc as dc
        import modal as patched

        assert patched.Function.from_name is not None
        with pytest.raises(_ReachedTheModalBoundary):
            patched.Function.from_name("app", "fn")
        with pytest.raises(_ReachedTheModalBoundary):
            dc._stage_target("j", tmp_path / "t.pdb")

        # A REFUSED submit must stop before the tripwire, not at it.
        state = tmp_path / "state.json"
        state.write_text(json.dumps(
            {"call_id": "fc-abc123", "job_id": "fc-round-1", "job_spec": {}}))
        monkeypatch.setattr(dc, "STATE", state)
        rc = dc.cmd_submit(_parse_direct_call_args(
            ["--submit", "--job-id", "fc-round-1"]))
        assert rc == 2
        assert modal_tripwire == [], (
            "a refused submit still entered the staging path — the guards must "
            "run before anything touches the network")

    def test_the_driver_still_selects_INLINE_delivery(self):
        """The payload contract this whole file is about: no
        upload_urls_endpoint and no job_token is what picks INLINE in main()."""
        from tools.proteina import direct_call_fc as dc
        payload = dc.build_payload(
            "https://example/PRESIGNED", preset="protein_binder",
            nsamples=4, replicas=2, job_id="t")
        assert "upload_urls_endpoint" not in payload
        assert "job_token" not in payload

    # --- the two fields the driver's own docstring calls dangerous ----------

    def test_the_driver_declares_target_source_custom_EXPLICITLY(self):
        """CONTRACT QUIRK 1, which had no test at all: mutating this field to
        "curated" left the whole file green. The driver's docstring says it
        "must be set to 'custom' EXPLICITLY. It defaults to 'curated' ...
        falling through to a repo-bundled benchmark target would design against
        the wrong structure and look successful."

        Asserted BOTH ways — the literal the driver writes, and the value
        main()'s own expression reads out of it — so the test fails whichever
        side moves. The blast radius is bounded (main()'s target-source
        invariant refuses a drifted value pre-GPU), but bounded to one wasted
        A100 container, which is the thing worth a two-line test."""
        from tools.proteina import direct_call_fc as dc

        spec = dc.build_job_spec(preset="protein_binder", nsamples=4, replicas=2)
        assert spec["target_source"] == "custom"
        # Verbatim from run_pipeline.main().
        assert (str(spec.get("target_source") or "curated")) == "custom", (
            "the driver's spec reads back as a CURATED run, so the container "
            "would design against a repo-bundled benchmark target")

    def test_binder_length_sits_where_run_pipeline_actually_reads_it(self):
        """CONTRACT QUIRK 2, and the one that fails SILENTLY. The docstring
        says binder_length is "a [lo, hi] pair at job_spec TOP LEVEL.
        `parameters` is never read at all." Moving it under ``parameters`` also
        left the file green — and would stay green in production, because
        run_pipeline's fallback (``or [60, 120]``) happens to equal the value
        the driver hardcodes today. The moment an operator edits the range, the
        edit is silently ignored and the shard designs the old lengths.

        Which is exactly why the STRUCTURAL assertions below are the ones that
        matter: the value read back is identical under the mutation, so only
        "the key is at the top level and there is no ``parameters`` dict" can
        distinguish them."""
        from tools.proteina import direct_call_fc as dc

        spec = dc.build_job_spec(preset="protein_binder", nsamples=4, replicas=2)
        assert "binder_length" in spec, (
            "binder_length left the job_spec top level; run_pipeline reads it "
            "there and nowhere else, so it would silently fall back to "
            "[60, 120]")
        assert "parameters" not in spec, (
            "the driver grew a `parameters` dict; run_pipeline never reads one")
        assert spec["binder_length"] == [60, 120]
        # Verbatim from run_pipeline.main().
        assert [int(v) for v in (spec.get("binder_length") or [60, 120])] == [
            60, 120]

    # --- --collect must persist the result on EVERY path -------------------

    def test_collect_PERSISTS_the_result_when_no_candidate_carries_ATOMS(
            self, tmp_path, monkeypatch):
        """The `if not with_atoms: return 1` used to precede the
        smoke_result.json write, so the failure path — the one where a
        machine-readable diagnosis is worth most — was the one that got none.

        run_pipeline keeps scores, ranks and the full candidate list on a
        FAILED result deliberately (the inline-delivery verdict in its main()
        says so: "the science survives"). Dropping it to terminal scrollback
        here undoes that on the operator's side, and it was NOT recoverable by
        re-running --collect: the fetch succeeds but hits the identical early
        return and still writes nothing.

        The non-zero exit stays — the goal genuinely is not met."""
        smoke = {
            "status": "FAILED", "designs_completed": 4, "designs_total": 8,
            "error": {"bucket": "delivery",
                      "check": "inline_cap_admitted_nothing",
                      "detail": "the cap admitted none of the 4 design(s)"},
            "inline_delivery": {"n_inlined": 0, "n_inline_capped": 4,
                                "inline_bytes_used": 0, "cap_bytes": 1024},
            "candidates": [{"rank": r, "name": f"d{r}",
                            "scores": {"total_reward": 1.0 / r}}
                           for r in range(1, 5)],
        }
        rc, outdir, _ = _drive_collect(
            tmp_path, monkeypatch, smoke, exit_code=1)

        assert rc == 1, "a run that delivered no coordinates must still exit 1"
        saved = outdir / "smoke_result.json"
        assert saved.exists(), (
            "cmd_collect discarded the only machine-readable copy of a paid "
            "shard's scores, ranks and error detail")
        parsed = json.loads(saved.read_text())
        assert parsed["error"]["check"] == "inline_cap_admitted_nothing"
        assert parsed["inline_delivery"]["n_inline_capped"] == 4
        assert [c["rank"] for c in parsed["candidates"]] == [1, 2, 3, 4], (
            "the scores run_pipeline kept on the FAILED result did not survive")

    def test_a_pre_GPU_refusal_with_ZERO_candidates_is_persisted_too(
            self, tmp_path, monkeypatch):
        """The reachable case, and the reason this is not merely theoretical.

        Every pre-GPU `_fail` — the #116 minimum-target-size floor, the #118
        empty-contig refusal, hotspot_chain_ambiguous, hotspot_malformed,
        target_conflict — produces a FAILED result with an empty candidate
        list, so it lands on this same early return without the inline cap
        ever biting. The diagnosis in `error` is the entire value of that
        result and it must reach disk."""
        smoke = {
            "status": "FAILED", "designs_completed": 0, "designs_total": 0,
            "error": {"bucket": "preflight", "check": "target_too_small",
                      "detail": "target has 12 residues, below the floor"},
            "candidates": [],
        }
        rc, outdir, _ = _drive_collect(
            tmp_path, monkeypatch, smoke, exit_code=1)

        assert rc == 1
        parsed = json.loads((outdir / "smoke_result.json").read_text())
        assert parsed["error"]["check"] == "target_too_small"

    def test_the_success_path_still_writes_BOTH_pdbs_and_the_result(
            self, tmp_path, monkeypatch):
        """Hoisting the write must not have moved it out from under the
        success path, and must not have stopped the PDBs landing."""
        pdb = b"ATOM      1  CA  ALA A   1\nEND\n"
        smoke = {
            "status": "COMPLETED", "designs_completed": 1, "designs_total": 1,
            "candidates": [{"rank": 1, "scores": {"total_reward": 1.0},
                            "pdb_content_b64": base64.b64encode(pdb).decode()}],
        }
        rc, outdir, _ = _drive_collect(tmp_path, monkeypatch, smoke)
        assert rc == 0
        assert (outdir / "design_001.pdb").read_bytes() == pdb
        assert json.loads(
            (outdir / "smoke_result.json").read_text())["status"] == "COMPLETED"

    # --- what --validate actually costs ------------------------------------

    def test_the_function_the_driver_calls_really_does_carry_a_GPU(self):
        """Ground truth for every cost claim below, read out of modal_app.py
        rather than asserted: there is exactly ONE @app.function in the app and
        it is unconditionally A100-80GB, so every command that reaches Modal —
        `--validate` included — allocates one. FN_GPU is the driver's copy of
        that fact and drifting it is the failure this catches."""
        from tools.proteina import direct_call_fc as dc

        gpus = _modal_function_gpus()
        assert dc.FN in gpus, (
            f"the driver calls {dc.APP}/{dc.FN}, which modal_app.py no longer "
            f"declares; it declares {sorted(gpus)}")
        assert gpus[dc.FN] == dc.FN_GPU, (
            f"{dc.FN} is deployed with gpu={gpus[dc.FN]!r} but direct_call_fc "
            f"tells the operator {dc.FN_GPU!r}")
        assert dc.FN_GPU, (
            "FN_GPU is falsy, which claims a CPU-only container; if a genuine "
            "CPU-only validate function now exists, route cmd_validate at it "
            "and restore the costless wording")

    def test_the_validate_BANNER_does_not_advertise_a_billed_call_as_free(
            self, tmp_path, monkeypatch, capsys):
        """Driven through the REAL cmd_validate, because the banner is what an
        operator actually reads — it used to print "(free, CPU-only)" about a
        call to an unconditionally A100-80GB function, which is not merely
        vague but inverted on the one resource it names. Modal bills container
        wall-clock, so skipping GPU *work* is not being CPU-only."""
        import sys as _sys
        import types as _types
        from tools.proteina import direct_call_fc as dc

        monkeypatch.setattr(dc, "_load_env_and_path", lambda: None)
        monkeypatch.setattr(dc, "_resolve_target", lambda a: tmp_path / "t.pdb")
        monkeypatch.setattr(dc, "_stage_target",
                            lambda j, t: "https://example/PRESIGNED")
        fake_modal = _types.ModuleType("modal")
        fake_modal.Function = _types.SimpleNamespace(
            from_name=lambda app, fn: _types.SimpleNamespace(
                remote=lambda payload: {"exit_code": 0}))
        monkeypatch.setitem(_sys.modules, "modal", fake_modal)

        assert dc.cmd_validate(_parse_direct_call_args(["--validate"])) == 0
        banner = "\n".join(
            ln for ln in capsys.readouterr().out.splitlines()
            if "[validate]" in ln).lower()

        assert banner, "cmd_validate printed no [validate] banner at all"
        assert "free" not in banner, (
            f"the banner still calls a billed A100 call free: {banner!r}")
        assert "cpu-only" not in banner, (
            f"the banner still calls an A100 container CPU-only: {banner!r}")
        assert dc.FN_GPU.lower() in banner, (
            "the banner should name the accelerator it allocates so the cost "
            f"is visible at the moment it is incurred: {banner!r}")

    def test_no_MODAL_CALLING_command_is_documented_as_free(self):
        """The usage block and cmd_validate's docstring, checked against the
        same ground truth. `--dry-run` is exempt and deliberately so: it never
        imports modal, so it is the one genuinely costless option and the
        docstring should keep recommending it.

        Scoped to this file on purpose. "Free validate" IS true of the
        tools-hub WALLET (tools/proteina/__init__ says "free validate dry-run")
        and that wording must survive; what is false is calling the
        INFRASTRUCTURE free on a path that bypasses the wallet entirely."""
        import inspect
        from tools.proteina import direct_call_fc as dc

        if not _modal_function_gpus().get(dc.FN):
            pytest.skip("FN is genuinely CPU-only; costless wording is honest")

        usage = [ln for ln in (dc.__doc__ or "").splitlines()
                 if "direct_call_fc.py --" in ln]
        assert usage, "the module docstring lost its usage block"
        for line in usage:
            costless = "free" in line.lower()
            if "--dry-run" in line:
                assert costless, (
                    "--dry-run never touches Modal and should stay flagged as "
                    f"the free option: {line!r}")
            else:
                assert not costless, (
                    f"a command that calls {dc.APP}/{dc.FN} (gpu={dc.FN_GPU}) "
                    f"is advertised as free: {line!r}")

        doc = (inspect.getdoc(dc.cmd_validate) or "").lower()
        assert doc, "cmd_validate lost its docstring"
        assert "cpu-only" not in doc.replace("not cpu-only", ""), (
            "cmd_validate's docstring still describes the container as "
            "CPU-only")
        assert "not free" in doc or "cheap, not free" in doc, (
            "cmd_validate's docstring must state the cost plainly; it is the "
            "text an operator reads before deciding to 'just validate first'")
