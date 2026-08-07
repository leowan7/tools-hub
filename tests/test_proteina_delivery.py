"""Design delivery for the Proteina-Complexa tool: the upload path (tools-hub
web tier) and the inline path (a direct ``modal.Function.from_name`` call).

WHY THIS FILE EXISTS. The tools-hub web tier cannot express a multi-chain
target or chain-prefixed hotspots, so an Fc campaign has to invoke the Modal
function directly and read designs out of the return value — the way
RFdiffusion, PXDesign and BindCraft already work. Such a caller has no
tools-hub server to receive an upload callback and no ``job_token`` to
authenticate one, and ``main()`` used to refuse pre-GPU on exactly that. It now
chooses a delivery mode instead: with an endpoint it uploads and records a
``pdb_key`` pointer, without one it carries the atoms inline as
``pdb_content_b64``.

THE UPLOAD PATH IS THE ONE REAL JOBS USE. Every test in
``TestUploadPathUnchanged`` asserts it behaves as it did before the change —
same calls, same argv, same pointers, same failure accounting — rather than
merely asserting it still produces output.

Runs fully offline: the GPU stages are stubbed, no Modal, no network.
"""

from __future__ import annotations

import base64
import json

import pytest

from tools.proteina import run_pipeline as rp


def _drive_design_loop(tmp_path, monkeypatch, *, endpoint, designs=2,
                       pdb_body=b"ATOM  fake\nEND\n", inline_env=None,
                       cap_bytes=None, break_upload=False):
    """Run ``main()`` through to the design loop with the GPU stages stubbed.

    Returns ``(result_dict, calls)`` where ``calls`` records every
    ``request_upload_urls`` / ``upload_pdb`` invocation in order, so a test can
    assert on the exact upload exchange rather than on its side effects.
    """
    result_file = tmp_path / "smoke.json"
    monkeypatch.setattr(rp, "SMOKE_RESULTS_PATH", str(result_file))

    # exist_ok: the two-mode comparison test drives this helper twice against
    # the same tmp_path.
    pdb_dir = tmp_path / "pdbs"
    pdb_dir.mkdir(exist_ok=True)
    rows = []
    for i in range(designs):
        (pdb_dir / f"d{i}.pdb").write_bytes(pdb_body)
        rows.append({
            "rank": i + 1,
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
    monkeypatch.setattr(
        rp, "find_pdb_for",
        lambda d, run_dir, idx, total: pdb_dir / f"d{d['_row_index']}.pdb")
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

    rp.main()
    return json.loads(result_file.read_text()), calls


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

    def test_a_malformed_cap_falls_back_instead_of_crashing_at_import(self, monkeypatch):
        """int() at module scope raises BEFORE _fail can write a result file,
        so a typo'd env var kills the container with no result and the hub
        reports it as a webhook delivery failure — on an allocated GPU."""
        monkeypatch.setenv("PROTEINA_INLINE_PDB_CAP_BYTES", "64MB")
        assert rp._inline_cap_bytes() == rp.INLINE_PDB_DEFAULT_CAP_BYTES
        monkeypatch.setenv("PROTEINA_INLINE_PDB_CAP_BYTES", "   ")
        assert rp._inline_cap_bytes() == rp.INLINE_PDB_DEFAULT_CAP_BYTES
        monkeypatch.setenv("PROTEINA_INLINE_PDB_CAP_BYTES", "12345")
        assert rp._inline_cap_bytes() == 12345

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
        """The union must not over-refuse: one chain named twice is one chain."""
        assert rp.normalize_hotspots({
            "target_chain": "A", "target_input": "A1-200",
            "hotspot_residues": [264],
        }) == ["A264"]

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
