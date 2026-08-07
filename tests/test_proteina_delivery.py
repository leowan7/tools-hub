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
        calls.append(("put", url, len(data)))

    monkeypatch.setattr(rp, "run_streaming", lambda cmd, wd: 0)
    monkeypatch.setattr(rp, "parse_designs", lambda run_dir: rows)
    monkeypatch.setattr(
        rp, "find_pdb_for",
        lambda d, run_dir, idx, total: pdb_dir / f"d{d['_row_index']}.pdb")
    monkeypatch.setattr(rp, "archive_raw_outputs", lambda *a, **k: None)
    monkeypatch.setattr(rp, "send_heartbeat", lambda *a, **k: None)
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

    def test_no_endpoint_makes_no_network_calls(self, tmp_path, monkeypatch):
        """There is no tools-hub server to call back to and no job_token to
        authenticate with, so attempting either is the bug, not a fallback."""
        _, calls = _drive_design_loop(tmp_path, monkeypatch, endpoint="")
        assert calls == []

    def test_field_name_matches_the_other_generators(self, tmp_path, monkeypatch):
        """PXDesign (_candidate_from_design) and BindCraft both emit
        `pdb_content_b64`. A fourth spelling would force per-tool
        special-casing in the campaign's merge step."""
        data, _ = _drive_design_loop(tmp_path, monkeypatch, endpoint="")
        assert all("pdb_content_b64" in c for c in data["candidates"])

    def test_pdb_key_is_still_present_inline(self, tmp_path, monkeypatch):
        """The extension is carried by pdb_key (".pdb"), which is how PXDesign
        records it too — so no extra field is invented for it."""
        data, _ = _drive_design_loop(tmp_path, monkeypatch, endpoint="")
        assert [d["pdb_key"] for d in data["designs"]] == [
            "designs/design_001.pdb", "designs/design_002.pdb"]

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
        assert [c[0] for c in calls] == ["request", "put", "request", "put"]

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
        body = b"ATOM  1234\nEND\n"
        _, calls = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload", pdb_body=body)
        assert [c[2] for c in calls if c[0] == "put"] == [len(body), len(body)]

    def test_endpoint_still_records_pdb_key_pointers(self, tmp_path, monkeypatch):
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload")
        assert [d["pdb_key"] for d in data["designs"]] == [
            "designs/design_001.pdb", "designs/design_002.pdb"]
        assert [c["pdb_key"] for c in data["candidates"]] == [
            "designs/design_001.pdb", "designs/design_002.pdb"]

    def test_inline_is_additive_not_a_replacement(self, tmp_path, monkeypatch):
        """With an endpoint present the upload happens AND the atoms ride
        along. pdb_key stays the pointer of record either way."""
        data, calls = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload")
        assert len([c for c in calls if c[0] == "put"]) == 2
        for c in data["candidates"]:
            assert c["pdb_key"].startswith("designs/")
            assert "pdb_content_b64" in c

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

    def test_slimming_actually_strips_our_web_path_result(self, tmp_path, monkeypatch):
        """Not a restatement of the rule but a run of the real function against
        a real result, so a change to either side is caught here."""
        from shared.jobs import _slim_result_for_persist
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload",
            pdb_body=b"ATOM  x" * 200 + b"\nEND\n")
        assert any("pdb_content_b64" in c for c in data["candidates"])
        slimmed = _slim_result_for_persist(data)
        assert all("pdb_content_b64" not in c for c in slimmed["candidates"]), (
            "the uploaded structures resolve from Storage by pdb_key, so the "
            "inline copy must not reach the persisted row")
        assert all("pdb_content_b64" not in d for d in slimmed["designs"])

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

    def test_scores_are_identical_between_the_two_modes(self, tmp_path, monkeypatch):
        """Delivery mode must change only how atoms travel, never the scores
        or the ranking."""
        web, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload")
        direct, _ = _drive_design_loop(tmp_path, monkeypatch, endpoint="")

        def strip(rows):
            return [{k: v for k, v in r.items() if k != "pdb_content_b64"}
                    for r in rows]

        assert strip(web["designs"]) == strip(direct["designs"])
        assert strip(web["candidates"]) == strip(direct["candidates"])


class TestInlineSizeCap:
    """Modal blob-uploads any return over MAX_OBJECT_SIZE_BYTES (2 MiB)
    transparently — see _utils/blob_utils.format_blob_data, reached from the
    container's return path in container_io_manager.package_output — so
    inlining cannot fail on size. Multi-MB returns are still wasteful, and a
    cap that silently truncated the result set would be worse than no cap."""

    def test_designs_over_the_cap_keep_scores_and_lose_only_atoms(self, tmp_path, monkeypatch):
        data, _ = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="", designs=4,
            pdb_body=b"X" * 1000, cap_bytes=2500)
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
        default must not clip the campaign this work exists to serve."""
        assert rp.INLINE_PDB_TOTAL_CAP_BYTES >= 8 * 350_000

    def test_inline_can_be_turned_off_with_an_endpoint_present(self, tmp_path, monkeypatch):
        """Opt-out for a caller that wants scores only and does not want to
        move base64 it will discard. The upload still happens."""
        data, calls = _drive_design_loop(
            tmp_path, monkeypatch, endpoint="https://hub/upload", inline_env="off")
        assert len([c for c in calls if c[0] == "put"]) == 2
        assert data["candidates"], "guard must run against a populated list"
        assert all("pdb_content_b64" not in c for c in data["candidates"])
        # The upload pointers are still the delivery mechanism.
        assert all(c["pdb_key"].startswith("designs/") for c in data["candidates"])


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

    def test_bare_ints_attach_to_the_first_target_chain(self):
        """Upstream matches f"{chain_id}{res_id}", so a bare 264 addresses
        nothing at all. Attributing it is the documented shared contract and
        is exactly the historical single-chain behaviour."""
        assert rp.normalize_hotspots(
            {"hotspot_residues": [264, 301], "target_chain": "A,B"}) == ["A264", "A301"]

    def test_prefixed_and_bare_may_be_mixed(self):
        assert rp.normalize_hotspots(
            {"hotspot_residues": ["B264", 301], "target_chain": "A B"}) == ["B264", "A301"]

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
