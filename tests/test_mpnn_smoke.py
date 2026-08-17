"""Unit tests for the D1 — ProteinMPNN standalone atomic tool.

Covers five things per the ATOMIC-TOOLS.md "Definition of Done":

1. The adapter registers with the right slug, presets, credit costs.
2. ``validate()`` accepts well-formed input and rejects every known
   malformed case (missing fields, out-of-range numerics, empty chains).
3. ``build_payload()`` produces the expected Kendrew job_spec shape.
4. The Flask form template renders and submit validation rejects
   malformed data (feature flag must be flipped ON in the test process
   so the route is not 404'd).
5. The Modal webhook handler accepts a well-formed COMPLETED POST for
   an MPNN job and rejects replay / unknown-job / bad-token cases.

Runs fully offline — no Modal, no Supabase, no Storage. Uses the same
monkey-patching pattern as ``tests/test_jobs_phase4.py`` so CI does not
need GPU access.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from tools import mpnn as mpnn_mod
from tools.base import get as get_adapter


# ---------------------------------------------------------------------------
# Test 1 — adapter registration
# ---------------------------------------------------------------------------


class TestAdapterRegistration:
    def test_adapter_registered_under_mpnn_slug(self):
        adapter = get_adapter("mpnn")
        assert adapter is not None, "tools.mpnn did not register its adapter"
        assert adapter.slug == "mpnn"

    def test_presets_shape(self):
        adapter = get_adapter("mpnn")
        slugs = [p.slug for p in adapter.presets]
        assert slugs == ["standalone"]

    def test_standalone_requires_pdb(self):
        """The single standalone tier requires a backbone PDB upload, at
        the preset level AND at the adapter level (the form always shows
        the upload field)."""
        adapter = get_adapter("mpnn")
        standalone = adapter.preset_for("standalone")
        assert standalone is not None
        assert standalone.requires_pdb is True
        assert adapter.requires_pdb is True

    def test_templates_point_at_mpnn_partials(self):
        adapter = get_adapter("mpnn")
        assert adapter.form_template == "tools/mpnn_form.html"
        assert adapter.results_partial == "tools/mpnn_results.html"


# ---------------------------------------------------------------------------
# Test 2 — validate() happy path + rejections
# ---------------------------------------------------------------------------


class TestValidate:
    def test_blank_preset_resolves_to_standalone(self):
        """A missing/blank preset is now treated as ``standalone`` (the
        sole tier). With no other fields the form still validates because
        ``chains_to_design`` defaults to ``A``, confirming the blank
        preset was accepted as standalone rather than rejected."""
        inputs, err = mpnn_mod.validate({}, {})
        assert err is None, err
        assert inputs["preset"] == "standalone"
        # chains_to_design defaults to "A" on the standalone tier.
        assert inputs["target_chain"] == "A"

    def test_rejects_unknown_preset(self):
        inputs, err = mpnn_mod.validate({"preset": "full"}, {})
        assert inputs is None

    def test_standalone_happy_path_basic(self):
        form = {
            "preset": "standalone",
            "chains_to_design": "A",
            "num_seq_per_target": "5",
            "sampling_temp": "0.1",
        }
        inputs, err = mpnn_mod.validate(form, {})
        assert err is None, err
        assert inputs["preset"] == "standalone"
        assert inputs["target_chain"] == "A"
        assert inputs["num_seq_per_target"] == 5
        assert inputs["sampling_temp"] == 0.1

    def test_standalone_normalizes_multiple_chains(self):
        """Accept ``A,B``, ``A B``, ``AB`` → all normalize to space-joined."""
        for raw, expected in [
            ("A,B", "A B"),
            ("A B", "A B"),
            ("A, B", "A B"),
            ("A", "A"),
        ]:
            form = {"preset": "standalone", "chains_to_design": raw}
            inputs, err = mpnn_mod.validate(form, {})
            assert err is None, f"{raw!r}: {err}"
            assert inputs["target_chain"] == expected, raw

    def test_standalone_rejects_empty_chains(self):
        form = {"preset": "standalone", "chains_to_design": "   "}
        inputs, err = mpnn_mod.validate(form, {})
        assert inputs is None
        assert err is not None

    def test_standalone_rejects_num_seq_over_cap(self):
        """The cap == NUM_SEQ_MAX: the cap is accepted, cap+1 is rejected."""
        cap = mpnn_mod.NUM_SEQ_MAX
        ok_form = {
            "preset": "standalone",
            "chains_to_design": "A",
            "num_seq_per_target": str(cap),
        }
        inputs_ok, err_ok = mpnn_mod.validate(ok_form, {})
        assert err_ok is None, err_ok
        assert inputs_ok["num_seq_per_target"] == cap

        over_form = {
            "preset": "standalone",
            "chains_to_design": "A",
            "num_seq_per_target": str(cap + 1),
        }
        inputs, err = mpnn_mod.validate(over_form, {})
        assert inputs is None
        assert "num_seq_per_target" in (err or "")

    def test_standalone_rejects_num_seq_below_min(self):
        form = {
            "preset": "standalone",
            "chains_to_design": "A",
            "num_seq_per_target": "0",
        }
        inputs, err = mpnn_mod.validate(form, {})
        assert inputs is None

    def test_standalone_rejects_temp_out_of_range(self):
        for bad in ("-0.1", "2.0", "10"):
            form = {
                "preset": "standalone",
                "chains_to_design": "A",
                "sampling_temp": bad,
            }
            inputs, err = mpnn_mod.validate(form, {})
            assert inputs is None, f"temp={bad} should fail"

    def test_standalone_rejects_long_chain_id(self):
        form = {"preset": "standalone", "chains_to_design": "ABCDE"}
        inputs, err = mpnn_mod.validate(form, {})
        assert inputs is None


# ---------------------------------------------------------------------------
# Test 3 — build_payload() shape
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_standalone_payload_shape(self):
        inputs, _ = mpnn_mod.validate(
            {
                "preset": "standalone",
                "chains_to_design": "A B",
                "num_seq_per_target": "7",
                "sampling_temp": "0.2",
            },
            {},
        )
        payload = mpnn_mod.build_payload(inputs, presigned_url="https://x")
        assert payload["target_chain"] == "A B"
        assert payload["parameters"]["num_seq_per_target"] == 7
        assert payload["parameters"]["sampling_temp"] == 0.2
        # Presigned URL is forwarded separately — not embedded.
        assert "presigned" not in json.dumps(payload).lower()


# ---------------------------------------------------------------------------
# Test 3b — fixed_positions wiring (form field -> job_spec -> pipeline)
#
# The pipeline-side validation of fixed_positions lives in
# tests/test_mpnn_fixed_positions.py. What is pinned HERE is only the wire:
# that the form field parses, that the parsed shape survives into the payload,
# and that run_pipeline.normalise_fixed_positions accepts that payload
# unchanged. The last one is the reason this feature was unreachable before.
# ---------------------------------------------------------------------------


def _mini_pdb(chains: dict) -> str:
    """Minimal CA-only PDB: chain id -> residue count, numbered from 1."""
    lines, serial = [], 1
    for chain, n in chains.items():
        for i in range(1, n + 1):
            lines.append(
                f"ATOM  {serial:5d}  CA  ALA {chain}{i:4d}    "
                f"{0.0:8.3f}{0.0:8.3f}{float(i):8.3f}  1.00  0.00           C"
            )
            serial += 1
    return "\n".join(lines) + "\n"


def _validated(**form):
    form.setdefault("preset", "standalone")
    return mpnn_mod.validate(form, {})


class TestFixedPositionsWiring:
    @pytest.mark.parametrize("chains", ["A", "A B", "A,B"])
    def test_blank_field_leaves_the_payload_byte_identical(self, chains):
        """The default path must not gain a key. Asserted as the full serialized
        payload, not just "the key is absent", because the claim being made is
        that a plain redesign submits what it submitted before this field
        existed. Spelled out literally so it stays true when HEAD moves.

        (The persisted ``inputs`` blob DOES gain two keys — that is a separate
        thing, and every consumer of it was checked.)"""
        inputs, err = _validated(chains_to_design=chains)
        assert err is None
        payload = mpnn_mod.build_payload(inputs, presigned_url="https://x")
        expected = {
            "target_chain": chains.replace(",", " "),
            "parameters": {"num_seq_per_target": 50, "sampling_temp": 0.1},
        }
        assert json.dumps(payload, sort_keys=True) == json.dumps(
            expected, sort_keys=True
        )

    def test_ranges_expand_inclusively(self):
        inputs, err = _validated(chains_to_design="A", fixed_positions="A:1-4,9")
        assert err is None
        assert inputs["_fixed_positions"] == {"A": [1, 2, 3, 4, 9]}

    def test_a_huge_digit_run_is_refused_not_raised(self):
        """CPython caps int(str) at 4300 digits, and the range regex matches any
        number of them, so an over-long token used to reach ``int()`` and raise
        ValueError. Nothing wraps ``validate``, so that surfaced as a 500 on an
        ordinary form POST — from an unauthenticated-cost path, before any GPU.

        Asserting on the RETURN, not pytest.raises: the contract is that this
        layer reports bad input, never that it explodes on it.
        """
        inputs, err = _validated(
            chains_to_design="A", fixed_positions="A:" + "9" * 4301
        )
        assert inputs is None
        assert "too long" in err

    def test_a_multi_megabyte_field_is_refused_before_it_is_expanded(self):
        """The expansion cap cannot catch this one. "1," ten million times
        expands to the single position {1}, so _MAX_FIXED_POSITIONS never fires,
        yet the raw string is ~20 MB — it burns seconds in the split loop and is
        then persisted verbatim into tool_jobs.inputs, because the raw field is
        stored as typed so clone_from can refill it. That is the multi-MB-blob
        failure this module's own comments cite as a past scar.
        """
        inputs, err = _validated(
            chains_to_design="A", fixed_positions="A:" + "1," * 10_000_000
        )
        assert inputs is None
        assert "too long" in err

    def test_a_realistic_complement_still_fits(self):
        """The cap must not refuse the field's advertised use. Freezing
        everything but a scattered patch on a long chain is the worst legitimate
        case; it has to stay comfortably inside."""
        body = ",".join(f"{i}-{i + 8}" for i in range(1, 900, 10))
        inputs, err = _validated(chains_to_design="A", fixed_positions=f"A:{body}")
        assert err is None
        assert len(inputs["_fixed_positions"]["A"]) == 810

    def test_bare_list_binds_to_the_sole_designed_chain(self):
        inputs, err = _validated(chains_to_design="C", fixed_positions="1-3,7")
        assert err is None
        assert inputs["_fixed_positions"] == {"C": [1, 2, 3, 7]}

    def test_bare_list_is_refused_when_two_chains_are_designed(self):
        """Guessing a chain here would freeze the wrong protomer on a homodimer
        and verify perfectly, because whatever got frozen IS frozen."""
        inputs, err = _validated(chains_to_design="A B", fixed_positions="1-3")
        assert inputs is None
        assert "must name its chain" in err

    def test_groups_bind_to_their_own_chains(self):
        inputs, err = _validated(
            chains_to_design="A B", fixed_positions="A:1-3 B:5,7"
        )
        assert err is None
        assert inputs["_fixed_positions"] == {"A": [1, 2, 3], "B": [5, 7]}

    def test_freezing_a_chain_that_is_not_designed_is_refused(self):
        """Upstream would KeyError on it after the job was billed."""
        inputs, err = _validated(chains_to_design="A", fixed_positions="B:1-3")
        assert inputs is None
        assert "not among the chains to design" in err

    def test_comma_separated_chains_still_resolve(self):
        """chains_to_design normalises "A,B" to "A B" BEFORE the membership
        check, so a comma in that field must not make B look undesigned."""
        inputs, err = _validated(
            chains_to_design="A,B", fixed_positions="B:1-3"
        )
        assert err is None
        assert inputs["_fixed_positions"] == {"B": [1, 2, 3]}

    def test_a_repeated_chain_is_refused_not_silently_overwritten(self):
        inputs, err = _validated(
            chains_to_design="A", fixed_positions="A:1-3 A:9"
        )
        assert inputs is None
        assert "more than once" in err

    @pytest.mark.parametrize(
        "raw, fragment",
        [
            ("A:9-4", "backwards"),
            # Just over the cap, not absurd: an unguarded build must FAIL this
            # case cheaply. The billion-wide input lives in the timing test
            # below, where an unguarded build hangs instead of failing.
            ("A:1-20000", "spans more than"),
            ("A:", "are empty"),
            ("A:1-", "not a position or a range"),
            ("A:x", "not a position or a range"),
            ("A:1.5", "not a position or a range"),
            ("A:-3", "not a position or a range"),
        ],
    )
    def test_malformed_groups_are_refused(self, raw, fragment):
        inputs, err = _validated(chains_to_design="A", fixed_positions=raw)
        assert inputs is None, f"{raw!r} should not validate"
        assert fragment in err, err

    def test_many_small_ranges_cannot_walk_past_the_cap(self):
        """The cap has to bound the CHAIN, not one token. A group holds
        unlimited comma-separated tokens, so 200 in-cap ranges spell the same
        multi-million-position request in ~3 KB — and the expansion is persisted
        verbatim into tool_jobs.inputs and shipped to Modal."""
        import time

        body = ",".join(f"{1 + i * 10_000}-{(i + 1) * 10_000}" for i in range(200))
        start = time.time()
        inputs, err = _validated(chains_to_design="A", fixed_positions=f"A:{body}")
        assert inputs is None, "200 chained in-cap ranges must not validate"
        assert "exceed" in err, err
        assert time.time() - start < 1.0

    def test_the_cap_does_not_reject_a_real_chain(self):
        """A titin-sized 9000-residue chain still validates. The cap exists to
        stop an allocation, not to second-guess the structure — bounds are the
        pipeline's job, against the real PDB."""
        inputs, err = _validated(chains_to_design="A", fixed_positions="A:1-9000")
        assert err is None, err
        assert len(inputs["_fixed_positions"]["A"]) == 9000

    @pytest.mark.parametrize("raw", ["A:0", "A:0-3", "0"])
    def test_position_zero_is_refused(self, raw):
        """Upstream turns 0 into np.array([0]) - 1 == -1 and silently freezes
        the LAST residue. One of only two silent failure modes in the feature,
        and an off-by-one (0-indexed) caller is the likeliest way to reach it."""
        inputs, err = _validated(chains_to_design="A", fixed_positions=raw)
        assert inputs is None, f"{raw!r} must not validate"
        assert "1-indexed" in err, err

    @pytest.mark.parametrize("gap", [" ", "  ", "\t", "\n", " \xa0"])
    def test_groups_split_on_any_whitespace(self, gap):
        """`.split()` with no argument, so a tab, a newline, a double space or a
        pasted non-breaking space all separate groups. A `.split(" ")` would
        pass the ordinary single-space case and mangle every other one."""
        inputs, err = _validated(
            chains_to_design="A B", fixed_positions=f"A:1-3{gap}B:5"
        )
        assert err is None, err
        assert inputs["_fixed_positions"] == {"A": [1, 2, 3], "B": [5]}

    def test_positions_are_persisted_sorted(self):
        """Out-of-order input normalises. The pipeline re-sorts, so this is
        about the blob that gets persisted and shown back, not correctness."""
        inputs, err = _validated(chains_to_design="A", fixed_positions="A:9,2,5-6,2")
        assert err is None
        assert inputs["_fixed_positions"]["A"] == [2, 5, 6, 9]

    def test_a_wide_range_is_refused_before_it_is_expanded(self):
        """Not a style rule: without the span cap this allocates a
        billion-element set from an unauthenticated text field.

        Keep this AFTER the cheap over-cap case above: a build with the cap
        removed hangs here rather than failing, so the fast case has to be the
        one that reports the breakage."""
        import time

        start = time.time()
        inputs, err = _validated(
            chains_to_design="A", fixed_positions="A:1-999999999"
        )
        assert inputs is None and err
        assert time.time() - start < 1.0

    def test_field_is_stored_as_typed_for_clone_prefill(self):
        """clone_from refills the form from inputs, dropping _-prefixed keys.
        The raw string must survive under the FORM FIELD's own name or a cloned
        run silently redesigns the whole chain."""
        inputs, err = _validated(chains_to_design="A", fixed_positions=" A:1-4 ")
        assert err is None
        assert inputs["fixed_positions"] == "A:1-4"
        prefill = {k: v for k, v in inputs.items() if not k.startswith("_")}
        assert prefill["fixed_positions"] == "A:1-4"
        assert "_fixed_positions" not in prefill

    def test_payload_carries_the_parsed_positions(self):
        inputs, _ = _validated(chains_to_design="A", fixed_positions="A:1-4,9")
        payload = mpnn_mod.build_payload(inputs, presigned_url="https://x")
        assert payload["parameters"]["fixed_positions"] == {"A": [1, 2, 3, 4, 9]}

    def test_payload_survives_a_job_persisted_before_this_field_existed(self):
        """build_payload also runs on cloned/re-submitted older inputs blobs."""
        legacy = {
            "target_chain": "A",
            "num_seq_per_target": 5,
            "sampling_temp": 0.1,
        }
        payload = mpnn_mod.build_payload(legacy, presigned_url="https://x")
        assert "fixed_positions" not in payload["parameters"]

    def test_payload_is_accepted_by_the_pipeline_unchanged(self, tmp_path):
        """THE WIRE. What the adapter emits is fed verbatim to the pipeline
        function that consumes it — including the JSON round-trip it makes on
        the way to Modal — and comes back as the same positions."""
        from tools.mpnn import run_pipeline as rp

        inputs, err = _validated(chains_to_design="C", fixed_positions="C:1-2,4")
        assert err is None
        payload = mpnn_mod.build_payload(inputs, presigned_url="https://x")
        payload = json.loads(json.dumps(payload))  # the trip through Modal

        pdb = tmp_path / "design_001.pdb"
        pdb.write_text(_mini_pdb({"A": 8, "C": 6}))
        assert rp.normalise_fixed_positions(
            payload, pdb, payload["target_chain"]
        ) == {"C": [1, 2, 4]}

    def test_the_pipeline_still_rejects_what_the_adapter_cannot_see(self, tmp_path):
        """The adapter has no PDB, so bounds stay the pipeline's job. Pinned so
        nobody "completes" the adapter check and drops the real one."""
        from tools.mpnn import run_pipeline as rp

        inputs, err = _validated(chains_to_design="C", fixed_positions="C:1-2,40")
        assert err is None, "out-of-range is not knowable at the form"
        payload = mpnn_mod.build_payload(inputs, presigned_url="https://x")
        pdb = tmp_path / "design_001.pdb"
        pdb.write_text(_mini_pdb({"A": 8, "C": 6}))
        with pytest.raises(SystemExit):
            rp.normalise_fixed_positions(payload, pdb, payload["target_chain"])


# ---------------------------------------------------------------------------
# Test 4 — Flask form + submit validation
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_mpnn_flag(monkeypatch):
    """Boot the tools-hub Flask app with FLAG_TOOL_MPNN=on so the route
    resolves rather than 404s. Side-effects during create_app (register
    routes) only happen once per process in production, so we build a
    throwaway app against the module-level registry here."""
    monkeypatch.setenv("FLAG_TOOL_MPNN", "on")
    # Session key must be set so login_required's session-bypass in tests works.
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")

    # Import lazily so the monkeypatched env is in place before create_app runs.
    from app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    yield flask_app


def _login_session(client, email="user@example.com"):
    """Set session cookie so ``@login_required`` routes pass."""
    with client.session_transaction() as sess:
        sess["user_email"] = email


def test_form_renders_when_flag_on(app_with_mpnn_flag, monkeypatch):
    """GET /tools/mpnn renders the form when the flag is flipped on."""
    # load_user_context is called on GET; stub it so the page renders
    # without hitting Supabase.
    from types import SimpleNamespace

    monkeypatch.setattr(
        "blueprints.tools.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_mpnn_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/mpnn")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ProteinMPNN" in body
    # Single standalone tier: preset is a hidden field, not a <select>.
    assert '<input type="hidden" name="preset" value="standalone">' in body
    assert '<option value="smoke"' not in body
    # The standalone inputs (backbone PDB + chains) render unconditionally.
    assert 'name="target_pdb"' in body
    assert 'name="chains_to_design"' in body


def test_form_404s_when_flag_off(app_with_mpnn_flag, monkeypatch):
    """With the flag removed, the route must 404 — launch-gate contract."""
    monkeypatch.delenv("FLAG_TOOL_MPNN", raising=False)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "blueprints.tools.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_mpnn_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/mpnn")
    assert resp.status_code == 404


def test_submit_rejects_unknown_preset(app_with_mpnn_flag, monkeypatch):
    """POST with a bad preset rerenders the form with the validation error."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "blueprints.tools.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    client = app_with_mpnn_flag.test_client()
    _login_session(client)
    resp = client.post(
        "/tools/mpnn/submit",
        data={"preset": "bogus"},
    )
    # Form rerendered with error — not a redirect to job_detail.
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Pick a preset" in body or "preset" in body.lower()


def test_export_fasta_serializes_mpnn_sequence_schema(
    app_with_mpnn_flag, monkeypatch
):
    """The shared /export.fasta route must serialize MPNN's ``sequences``
    schema ({seq, score, recovery}) alongside the binder-design tools'
    ``candidates`` schema. Before the Codex P2 fix the route only looked
    at ``candidates``, so every MPNN FASTA download returned the empty
    placeholder."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "blueprints.jobs.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    mpnn_job = SimpleNamespace(
        id="mpnn-job-1",
        tool="mpnn",
        status="succeeded",
        inputs={},
        result={
            "tier": "standalone",
            "chains_designed": "A",
            "sampling_temp": 0.1,
            "sequences": [
                {"seq": "MVLSPADKTNVK", "score": 1.23, "recovery": 0.71, "sample": 1},
                {"seq": "MVLSPADKTNVR", "score": 1.19, "recovery": 0.69, "sample": 2},
            ],
        },
    )
    monkeypatch.setattr(
        "blueprints.jobs.get_job",
        lambda job_id, user_id: mpnn_job if job_id == "mpnn-job-1" else None,
    )
    client = app_with_mpnn_flag.test_client()
    _login_session(client)
    resp = client.get("/jobs/mpnn-job-1/export.fasta")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "# No sequences found" not in body, (
        "FASTA export fell through to empty placeholder on MPNN-shaped result"
    )
    assert ">mpnn_rank1" in body
    assert ">mpnn_rank2" in body
    assert "MVLSPADKTNVK" in body
    assert "MVLSPADKTNVR" in body
    assert "score=1.23" in body
    assert "recovery=0.71" in body


def test_handoff_pilot_preset_runs_standalone_not_smoke(
    app_with_mpnn_flag, monkeypatch
):
    """Cross-tool ``from_job`` handoff sets pre_fill['preset']='pilot'.
    MPNN has no 'pilot' option, and the smoke tier is gone, so the form now
    pins a single hidden ``standalone`` preset. The handoff must therefore
    design the user's backbone on the standalone tier, never the (now
    removed) baked-fixture smoke tier. We assert the hidden preset is
    standalone and no smoke option survives anywhere in the form."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "blueprints.tools.load_user_context",
        lambda: SimpleNamespace(
            user_id="u1", tier="free", balance=10, email="user@example.com"
        ),
    )
    mock_src = SimpleNamespace(
        id="src-job-abc",
        tool="boltzgen",
        inputs={
            "target_chain": "A",
            "hotspot_residues": [10, 20, 30],
            "_pdb_storage_path": "u1/jobs/src-job-abc/target.pdb",
            "_pdb_filename": "1cnn.pdb",
        },
    )
    monkeypatch.setattr(
        "blueprints.tools.get_job",
        lambda job_id, user_id: mock_src if job_id == "src-job-abc" else None,
    )
    client = app_with_mpnn_flag.test_client()
    _login_session(client)
    resp = client.get("/tools/mpnn?from_job=src-job-abc")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '<input type="hidden" name="preset" value="standalone">' in body, (
        "form must pin the standalone preset on a handoff"
    )
    assert '<option value="smoke"' not in body, (
        "smoke option must not survive (the smoke tier was removed)"
    )
    assert 'value="smoke"' not in body, (
        "no smoke preset anywhere on a handoff (would run wrong PDB)"
    )


# ---------------------------------------------------------------------------
# Test 5 — Modal webhook handler accepts/rejects correctly
# ---------------------------------------------------------------------------


class TestWebhookRoundtrip:
    """Exercise the shared webhook handler against an MPNN job. We do
    not test the full complete_job pipeline (Supabase dependency); we
    test the handler's response to each auth state and to each payload
    status."""

    def _fake_job(self, status="running", token="t" * 64, tool="mpnn"):
        """Small stand-in that satisfies the attributes the handler uses."""
        from types import SimpleNamespace
        return SimpleNamespace(
            id="job-uuid-1",
            job_token=token,
            status=status,
            tool=tool,
            user_id="user-uuid-1",
        )

    def test_rejects_unknown_job(self, app_with_mpnn_flag, monkeypatch):
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: None)
        client = app_with_mpnn_flag.test_client()
        resp = client.post(
            "/webhooks/modal/missing-job/some-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 404

    def test_rejects_bad_token(self, app_with_mpnn_flag, monkeypatch):
        fake = self._fake_job(token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_mpnn_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/wrong-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 403

    def test_accepts_completed_with_good_token(
        self, app_with_mpnn_flag, monkeypatch
    ):
        fake = self._fake_job(status="running", token="good-token")
        # complete_job is CAS-guarded and returns the post-transition
        # row; the handler reads ``.status`` to detect a concurrent
        # cancel. For the happy path we return a fresh row with
        # status=succeeded so the handler takes the "recorded" branch.
        fresh = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        monkeypatch.setattr(
            "webhooks.modal.complete_job",
            lambda *a, **kw: fresh,
        )
        client = app_with_mpnn_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/good-token",
            json={
                "status": "COMPLETED",
                "output": {
                    "sequences": [{"seq": "MKWVT", "score": 1.1, "recovery": 0.5}],
                    "runtime_seconds": 42,
                },
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "recorded"

    def test_replay_on_terminal_is_noop(
        self, app_with_mpnn_flag, monkeypatch
    ):
        """Replaying the same POST after the job is already terminal
        must not mutate state — returns ``already_terminal``."""
        fake = self._fake_job(status="succeeded", token="good-token")
        monkeypatch.setattr("webhooks.modal.get_job", lambda _id: fake)
        client = app_with_mpnn_flag.test_client()
        resp = client.post(
            f"/webhooks/modal/{fake.id}/good-token",
            json={"status": "COMPLETED", "output": {}},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "already_terminal"


# ---------------------------------------------------------------------------
# Test 6 — smoke preset shape passed to Modal matches Kendrew payload
# ---------------------------------------------------------------------------


class TestSmokePresetShape:
    def test_app_name_override_maps_mpnn_to_ranomics_namespace(self):
        """Sanity: slug "mpnn" resolves to ``ranomics-mpnn-prod``."""
        from gpu.modal_client import modal_app_name

        assert modal_app_name("mpnn") == "ranomics-mpnn-prod"
        # All tools resolve under the ranomics-*-prod namespace post-Wave 1.
        assert modal_app_name("bindcraft") == "ranomics-bindcraft-prod"

    def test_preset_gpu_seconds_caps_registered(self):
        """Both MPNN presets have an entry in PRESET_CAPS — the generic
        submit route raises ``ValueError`` otherwise."""
        from gpu.modal_client import preset_gpu_seconds

        assert preset_gpu_seconds("mpnn", "smoke") == 120
        assert preset_gpu_seconds("mpnn", "standalone") == 360

    def test_modal_payload_for_standalone_offline_stub(self, monkeypatch):
        from gpu import modal_client as mc

        monkeypatch.setattr(mc, "_import_modal", lambda: None)
        client = mc.ModalClient(environment="main")
        inputs, _ = mpnn_mod.validate(
            {
                "preset": "standalone",
                "chains_to_design": "A",
                "num_seq_per_target": "3",
                "sampling_temp": "0.2",
            },
            {},
        )
        payload = mpnn_mod.build_payload(inputs, presigned_url="https://x")
        result = client.submit(
            "mpnn",
            "standalone",
            inputs={**payload, "_input_presigned_url": "https://x"},
            job_id="job-xyz",
            job_token="tok",
            webhook_url="https://tools/webhook",
        )
        assert result["function_call_id"].startswith("fc-stub-mpnn-standalone-")
        assert result["gpu_seconds_cap"] == 360


# ---------------------------------------------------------------------------
# Test 7 — run_pipeline.py parser + stub rejection
# ---------------------------------------------------------------------------
#
# The full run_pipeline.main() requires a GPU and the MPNN binary, so we
# can only exercise the parser + stub-rejection logic here. The other
# half of the pipeline (preflight, subprocess call) is covered by the
# live smoke validation the user owes on Modal.


class TestRunPipelineParser:
    def _write_fasta(self, tmp_path, pdb_stem, content):
        """Write MPNN-format FASTA output to ``tmp_path/seqs/<stem>.fa``."""
        seqs_dir = tmp_path / "seqs"
        seqs_dir.mkdir()
        fa = seqs_dir / f"{pdb_stem}.fa"
        fa.write_text(content)
        return tmp_path

    def test_parser_extracts_samples_and_skips_native(self, tmp_path):
        """MPNN emits the native first, then one record per sample. The
        native record has no ``sample=`` metadata and must be skipped."""
        from tools.mpnn import run_pipeline as rp

        content = (
            ">target, score=0.0, fixed_chains=[], designed_chains=['A']\n"
            "AAAAAAAAAA\n"
            ">T=0.1, sample=1, score=1.23, global_score=1.25, "
            "seq_recovery=0.52\n"
            "MKWVAHEDEL\n"
            ">T=0.1, sample=2, score=1.18, global_score=1.20, "
            "seq_recovery=0.48\n"
            "MKWVSHNDQL\n"
        )
        self._write_fasta(tmp_path, "target", content)
        sequences = rp.parse_mpnn_output(tmp_path, pdb_stem="target")
        assert len(sequences) == 2
        assert sequences[0]["seq"] == "MKWVAHEDEL"
        assert sequences[0]["sample"] == 1
        assert sequences[0]["score"] == pytest.approx(1.25)
        assert sequences[0]["recovery"] == pytest.approx(0.52)
        assert sequences[1]["seq"] == "MKWVSHNDQL"

    def test_stub_rejection_on_all_identical_sequences(self):
        """Silent-stub failure mode: every returned sequence is identical.
        reject_stub must ``sys.exit(1)`` via the shared ``_fail`` helper."""
        from tools.mpnn import run_pipeline as rp

        sequences = [
            {"seq": "AAAA", "score": 1.0, "recovery": 0.25},
            {"seq": "AAAA", "score": 1.1, "recovery": 0.25},
            {"seq": "AAAA", "score": 1.2, "recovery": 0.25},
        ]
        with pytest.raises(SystemExit):
            rp.reject_stub(sequences)

    def test_stub_rejection_accepts_distinct_sequences(self):
        """Happy path — distinct sequences must not raise."""
        from tools.mpnn import run_pipeline as rp

        sequences = [
            {"seq": "MKWVAH", "score": 1.0, "recovery": 0.50},
            {"seq": "MKWVSH", "score": 1.1, "recovery": 0.48},
        ]
        # Must not raise; no return value to assert.
        rp.reject_stub(sequences)

    def test_stub_rejection_on_identical_score_and_recovery(self):
        """Second stub signature: >=3 samples with identical score+recovery."""
        from tools.mpnn import run_pipeline as rp

        sequences = [
            {"seq": "MKWVA", "score": 0.96, "recovery": 0.08},
            {"seq": "MKWVS", "score": 0.96, "recovery": 0.08},
            {"seq": "MKWVT", "score": 0.96, "recovery": 0.08},
        ]
        with pytest.raises(SystemExit):
            rp.reject_stub(sequences)

    def test_stub_rejection_near_clone_hamming(self):
        """Degenerate mode: sequences differ by <=2 residues over >=3
        samples with diverse-looking float scores. Previous guards missed
        this because score/recovery aren't bit-exact; Codex P2 fix adds a
        pairwise Hamming check."""
        from tools.mpnn import run_pipeline as rp

        # Differ by 1-2 residues only — MPNN collapsed mode.
        sequences = [
            {"seq": "MKWVAHNDQLGHT", "score": 1.21, "recovery": 0.71},
            {"seq": "MKWVAHNDQLGHS", "score": 1.22, "recovery": 0.70},
            {"seq": "MKWVAHNDQLGKT", "score": 1.20, "recovery": 0.72},
        ]
        with pytest.raises(SystemExit):
            rp.reject_stub(sequences)

    def test_stub_rejection_near_clone_tight_score_recovery_cluster(self):
        """Degenerate mode: diverse-looking residues but score+recovery
        spreads < 0.01 — the probability landscape collapsed. Codex P2."""
        from tools.mpnn import run_pipeline as rp

        sequences = [
            {"seq": "MKWVAHNDQLGHT", "score": 1.2100, "recovery": 0.7100},
            {"seq": "PQRSTUVWXYZQA", "score": 1.2105, "recovery": 0.7102},
            {"seq": "DEFGHIKLMNPQV", "score": 1.2099, "recovery": 0.7098},
        ]
        with pytest.raises(SystemExit):
            rp.reject_stub(sequences)

    def test_stub_rejection_accepts_healthy_diverse_output(self):
        """Happy path for the new guards: real MPNN output typically has
        score spread ~0.1+ and recovery spread ~0.05+ across samples,
        with pairwise Hamming on the order of sequence length. Must not
        raise."""
        from tools.mpnn import run_pipeline as rp

        sequences = [
            {"seq": "MKWVAHNDQLGHT", "score": 1.05, "recovery": 0.65},
            {"seq": "PQRSTUVWXYZQA", "score": 1.31, "recovery": 0.72},
            {"seq": "DEFGHIKLMNPQV", "score": 0.92, "recovery": 0.58},
        ]
        rp.reject_stub(sequences)  # Must not raise.
