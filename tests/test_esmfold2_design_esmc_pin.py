"""The ESM-C 6B checkpoint pin, and the preflight that catches it drifting.

WHAT THIS EXISTS FOR. On 2026-09-30 three prod ``esmfold2-design`` runs
(a327d5fe, 236797f2, f2e296c7) each burned 32-37 H100 seconds and died in
``torch.linalg.svd``. The real cause was that ``biohub/ESMC-6B`` was resolved
at ``main``, that repo had been re-exported in the transformers-5 state dict
layout, and the installed transformers is 4.57.6 -- so EVERY ESM-C weight was
randomly initialised and the structure head took an SVD of a NaN matrix.
HuggingFace reports unmatched keys as a warning, so nothing raised until the
numerics did, 35 GPU-seconds later, in a traceback that named nothing about
weights.

WHY THE TESTS LOOK LIKE THIS. The code under test runs inside the built Modal
image: ``transformers``, ``torch`` and ``huggingface_hub`` are not installed in
this suite's interpreter, and the checkpoint lives in a Modal Volume. So this
file does two different things, and neither is the other:

  * static assertions over the source, the technique
    tests/test_esmfold2_design_vram_settings.py already uses for the same
    reason -- ORDER IS LOAD-BEARING here exactly as it is there, because a pin
    installed after the model has loaded assigns, greps and reviews clean
    while doing nothing;
  * behavioural tests of ``_pin_esmc_revision`` and
    ``_esmc_checkpoint_mismatch`` against injected fake modules.

WHAT THE FAKES DO AND DO NOT PROVE. They supply the data; the logic they
exercise is ours. They do NOT prove the real ``transformers`` names its keys as
assumed. That was established separately, by loading both revisions on a
CPU-only Modal container against the real image and the real Volume
(``output_loading_info=True``): revision ``main`` gave 802 missing keys, 1048
unexpected, and ``esmc.embed.weight`` all NaN; revision 45b0fa5d gave 0
missing, 6 unexpected (``lm_head.*``, which ``AutoModel`` does not read) and
mean -0.00043 / std 0.176. The asymmetry the fakes below reproduce -- that
``ESMCForMaskedLM`` matches its keys RAW while ``ESMCModel`` matches only after
``esmc.`` is stripped -- is from that same measurement, not invented to make a
test pass.
"""

from __future__ import annotations

import ast
import json
import logging
import pathlib
import re
import sys
import types

import pytest

from tools.esmfold2_design import run_pipeline as rp

_RUN_PIPELINE = pathlib.Path(rp.__file__)

# The two key layouts, as measured on the real checkpoints. Three keys each is
# enough: the comparison is a set relation, not a count.
_T4_KEYS = ["embed.weight", "transformer.blocks.0.attn.k_ln.weight"]
_T5_KEYS = ["embed_tokens.weight", "layers.0.input_layernorm.bias"]


# ===========================================================================
# Static assertions over the source
# ===========================================================================


def _calls(tree: ast.Module) -> dict[str, list[int]]:
    """``{callee name: [line, ...]}`` for every call in the module.

    An attribute call is recorded under BOTH ``attr`` and ``receiver.attr``
    where the receiver is a plain name. The dotted spelling is what the order
    assertion needs: a bare ``load`` also matches the ``json.load`` inside
    ``_esmc_checkpoint_mismatch`` itself, which sits above the real load site
    and silently inverted the comparison the first time this test ran.
    """
    out: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        names: list[str] = []
        if isinstance(func, ast.Name):
            names.append(func.id)
        elif isinstance(func, ast.Attribute):
            names.append(func.attr)
            if isinstance(func.value, ast.Name):
                names.append(f"{func.value.id}.{func.attr}")
        for name in names:
            out.setdefault(name, []).append(node.lineno)
    return out


@pytest.fixture(scope="module")
def calls() -> dict[str, list[int]]:
    return _calls(ast.parse(_RUN_PIPELINE.read_text(encoding="utf-8")))


def test_the_revision_is_pinned_to_a_commit_and_not_a_branch():
    """``main`` is what broke. A branch name here would reinstate the bug."""
    assert re.fullmatch(r"[0-9a-f]{40}", rp._ESMC_REVISION), (
        f"_ESMC_REVISION is {rp._ESMC_REVISION!r}; a pin must be a full "
        "commit SHA, because a branch is re-resolved on every cache miss"
    )
    assert rp._ESMC_REPO == "biohub/ESMC-6B"


def test_the_pin_and_the_check_run_before_anything_loads_the_model(calls):
    """Order, not existence.

    ``_pin_esmc_revision`` wraps ``PreTrainedModel.from_pretrained``, so it has
    no effect on a model that has already been built; and a weights check that
    runs after ``designer.load()`` has already paid for the load it was meant
    to avoid. Asserting the calls merely EXIST would pass on both bugs, which
    is the same trap tests/test_esmfold2_design_vram_settings.py documents for
    ``REUSE_ESMC``.
    """
    for name in ("_pin_esmc_revision", "_esmc_checkpoint_mismatch"):
        assert name in calls, f"{name} is defined but never called in _run"

    # ``bd.ESMFold2Design()`` and ``designer.load(False)`` are the two points
    # past which a pin is inert. Both are matched by receiver, so a rename of
    # either fails this test loudly instead of quietly matching nothing.
    loads = calls.get("designer.load", []) + calls.get("bd.ESMFold2Design", [])
    assert loads, (
        "neither bd.ESMFold2Design() nor designer.load() is called any more; "
        "this test no longer knows where the model is built"
    )
    first_load = min(loads)
    for name in ("_pin_esmc_revision", "_esmc_checkpoint_mismatch"):
        assert max(calls[name]) < first_load, (
            f"{name} is called at line {max(calls[name])}, at or after the "
            f"model is built at line {first_load} -- too late to have any "
            "effect"
        )


def test_the_weights_failure_is_bucketed_as_ours(calls):
    """``preflight:weights`` is the whole point of the fix, not a detail.

    shared/jobs.py's first ``_FAILURE_RULES`` row matches
    ``preflight:weights`` as ``our_side`` ("This run failed on our side, not
    because of your input."). The LAST row, ``numerical``, matches ``svd`` and
    ``nan`` -- which is where this failure landed for a week, and it tells the
    customer to change their seed. Leo disproved that advice by submitting
    f2e296c7 with the seed changed; it failed identically.
    """
    src = _RUN_PIPELINE.read_text(encoding="utf-8")
    assert '_fail_preflight("weights"' in src
    assert '"bucket": "preflight"' in src


# ===========================================================================
# Fake modules
# ===========================================================================


class _FakePreTrainedModel:
    calls: list[tuple[tuple, dict]] = []

    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        cls.calls.append((args, kwargs))
        return "loaded"


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_transformers(keys_by_class: dict[str, list[str]] | None = None):
    """A ``transformers`` stand-in carrying a real classmethod.

    ``_pin_esmc_revision`` reaches for ``from_pretrained.__func__``, so this
    cannot be a plain function: a double that accepts what the real object
    would reject proves nothing.
    """
    mod = types.ModuleType("transformers")
    mod.__version__ = "4.57.6"

    class _PreTrainedModel(_FakePreTrainedModel):
        calls: list[tuple[tuple, dict]] = []

    mod.PreTrainedModel = _PreTrainedModel

    class _Config:
        pass

    config = _Config()

    def _model_cls(name: str, prefix: str, keys: list[str]):
        return type(
            name,
            (),
            {
                "base_model_prefix": prefix,
                "__init__": lambda self, cfg: None,
                "state_dict": lambda self, _keys=keys: dict.fromkeys(_keys, 0),
            },
        )

    keys_by_class = keys_by_class or {}
    mod.AutoConfig = types.SimpleNamespace(
        from_pretrained=lambda *a, **k: config
    )
    # ``__name__``: the real ``AutoModel``/``AutoModelForMaskedLM``
    # are classes, and run_pipeline's log and refusal messages read it.
    mod.AutoModel = types.SimpleNamespace(
        __name__="AutoModel",
        _model_mapping={
            _Config: _model_cls(
                "ESMCModel", "esmc", keys_by_class.get("ESMCModel", _T4_KEYS)
            )
        }
    )
    # ``__name__``: the real ``AutoModel``/``AutoModelForMaskedLM``
    # are classes, and run_pipeline's log and refusal messages read it.
    mod.AutoModelForMaskedLM = types.SimpleNamespace(
        __name__="AutoModelForMaskedLM",
        _model_mapping={
            _Config: _model_cls(
                "ESMCForMaskedLM",
                "esmc",
                keys_by_class.get(
                    "ESMCForMaskedLM", [f"esmc.{k}" for k in _T4_KEYS]
                ),
            )
        }
    )
    return mod


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    """Install fake ``transformers`` / ``torch`` / ``huggingface_hub``.

    ``monkeypatch.setitem`` and nothing else, deliberately: these three keys
    are ABSENT from ``sys.modules`` in this interpreter, and setitem records
    that absence and DELETES the key on teardown. A plain assignment would
    leave a fake ``transformers`` installed for every later test in the
    session.
    """

    def install(index_keys: list[str], keys_by_class=None):
        mod = _fake_transformers(keys_by_class)
        torch = types.ModuleType("torch")
        torch.device = lambda _name: _NullContext()
        index = tmp_path / "model.safetensors.index.json"
        index.write_text(
            json.dumps({"weight_map": dict.fromkeys(index_keys, "a.safetensors")})
        )
        hub = types.ModuleType("huggingface_hub")
        hub.hf_hub_download = lambda *a, **k: str(index)
        for name, module in (
            ("transformers", mod),
            ("torch", torch),
            ("huggingface_hub", hub),
        ):
            monkeypatch.setitem(sys.modules, name, module)
        return mod

    yield install


# ===========================================================================
# The pin reaches the call sites
# ===========================================================================


class TestPinWiring:
    """A pin that never reaches ``from_pretrained`` is a guard that certifies
    false: the tool would keep loading ``main`` while the source read as
    pinned."""

    def test_the_esmc_repo_gets_the_revision_positionally_and_by_keyword(
        self, fake_env
    ):
        mod = fake_env(_T4_KEYS)
        rp._pin_esmc_revision()
        mod.PreTrainedModel.from_pretrained(rp._ESMC_REPO)
        mod.PreTrainedModel.from_pretrained(
            pretrained_model_name_or_path=rp._ESMC_REPO
        )
        revisions = [kw.get("revision") for _a, kw in mod.PreTrainedModel.calls]
        assert revisions == [rp._ESMC_REVISION, rp._ESMC_REVISION], (
            "binder_design.py calls from_pretrained positionally; the "
            "ESMFold2 trunk's load_esmc path does too, so both spellings "
            "must be pinned"
        )

    def test_other_repos_are_left_alone(self, fake_env):
        """Only ESMC is measured to have moved. The four ESMFold2 critic
        checkpoints must keep resolving however they resolve today."""
        mod = fake_env(_T4_KEYS)
        rp._pin_esmc_revision()
        mod.PreTrainedModel.from_pretrained("biohub/ESMFold2-Experimental-Fast")
        _args, kwargs = mod.PreTrainedModel.calls[-1]
        assert "revision" not in kwargs

    def test_an_explicit_revision_wins(self, fake_env):
        """``setdefault``, not assignment -- a caller that knows better is not
        silently overridden."""
        mod = fake_env(_T4_KEYS)
        rp._pin_esmc_revision()
        mod.PreTrainedModel.from_pretrained(rp._ESMC_REPO, revision="deadbeef")
        _args, kwargs = mod.PreTrainedModel.calls[-1]
        assert kwargs["revision"] == "deadbeef"

    @pytest.mark.parametrize(
        "spelling",
        [
            "biohub/ESMC-6B",
            "biohub/ESMC-6B/",
            "  biohub/ESMC-6B  ",
            "BioHub/esmc-6b",
            "/models/hub/ESMC-6B",
        ],
    )
    def test_the_pin_matches_the_spellings_the_call_sites_can_pass(
        self, fake_env, monkeypatch, spelling
    ):
        """Risk 2, named by review-code round 4.

        The pin used ``== _ESMC_REPO``, but the second load site passes
        ``model.config.esmc_id`` read out of the ESMFold2 checkpoint's
        own config JSON -- a string this repo does not control. Any
        divergence and the pin stops firing, the loader resolves
        ``main``, and ``_esmc_checkpoint_mismatch`` still returns ""
        because it validates ``_ESMC_REVISION`` explicitly: a guard
        certifying a checkpoint it did not gate. Matched on the
        case-folded last segment instead."""
        mod = fake_env(_T4_KEYS)
        monkeypatch.setattr(rp, "_PINNED_ESMC_LOADS", [])
        rp._pin_esmc_revision()
        mod.PreTrainedModel.from_pretrained(spelling)
        _args, kwargs = mod.PreTrainedModel.calls[-1]
        assert kwargs.get("revision") == rp._ESMC_REVISION, spelling
        assert rp._PINNED_ESMC_LOADS == [spelling]

    def test_a_run_whose_pin_never_fired_says_so(self, monkeypatch, caplog):
        """Risk 2 cannot be closed by matching harder -- the next
        divergence is one this repo still does not control. So the
        falsifier is recorded: no interception means the preflight
        verdict does not describe the weights in memory, and that gets
        a line in the Modal log instead of resurfacing as the same
        unattributable SVD traceback."""
        monkeypatch.setattr(rp, "_PINNED_ESMC_LOADS", [])
        with caplog.at_level(logging.WARNING, logger=rp.logger.name):
            rp._warn_if_esmc_was_never_pinned()
        assert "intercepted NO load" in caplog.text, caplog.text
        assert rp._ESMC_REVISION in caplog.text
        caplog.clear()
        monkeypatch.setattr(rp, "_PINNED_ESMC_LOADS", ["biohub/ESMC-6B"])
        with caplog.at_level(logging.WARNING, logger=rp.logger.name):
            rp._warn_if_esmc_was_never_pinned()
        assert caplog.text == "", caplog.text

    def test_other_repos_are_still_not_pinned(self, fake_env, monkeypatch):
        """The widened match must not start pinning the four ESMFold2
        critic checkpoints, which are not measured to have moved. The
        ``_T4``/``_T5`` story is about ESMC only."""
        mod = fake_env(_T4_KEYS)
        monkeypatch.setattr(rp, "_PINNED_ESMC_LOADS", [])
        rp._pin_esmc_revision()
        for other in (
            "biohub/ESMFold2-Experimental-Fast",
            "biohub/ESMC-600M",
            "biohub/ESMC-6B-critic",
            "",
            None,
        ):
            mod.PreTrainedModel.from_pretrained(other)
            _args, kwargs = mod.PreTrainedModel.calls[-1]
            assert "revision" not in kwargs, other
        assert rp._PINNED_ESMC_LOADS == []

    def test_pinning_twice_does_not_stack_wrappers(self, fake_env):
        mod = fake_env(_T4_KEYS)
        rp._pin_esmc_revision()
        # ``.__func__``, not the attribute: a classmethod access builds a fresh
        # bound object every time, so an identity check on the attribute fails
        # even when nothing was re-wrapped.
        first = mod.PreTrainedModel.from_pretrained.__func__
        rp._pin_esmc_revision()
        assert mod.PreTrainedModel.from_pretrained.__func__ is first


# ===========================================================================
# The check that would have caught 2026-09-30
# ===========================================================================


class TestCheckpointMismatch:
    def test_the_matching_layout_passes(self, fake_env):
        """``ESMCModel`` matches only prefix-stripped, ``ESMCForMaskedLM``
        only raw -- both are a pass, which is why the rule is a disjunction.
        A rule demanding one spelling would refuse the working checkpoint."""
        fake_env([f"esmc.{k}" for k in _T4_KEYS])
        assert rp._esmc_checkpoint_mismatch() == ""

    def test_the_transformers_5_layout_is_refused(self, fake_env):
        """THE REGRESSION. These are the key names the checkpoint at ``main``
        actually ships; against transformers 4.57.6 they match nothing, which
        is what produced NaN weights and the SVD traceback."""
        fake_env([f"esmc.{k}" for k in _T5_KEYS])
        detail = rp._esmc_checkpoint_mismatch()
        assert detail, "the layout that broke prod is accepted"
        assert "ESMCModel" in detail and "4.57.6" in detail, detail
        assert rp._ESMC_REVISION in detail, "the message must name the pin"

    def test_a_partial_match_is_refused(self, fake_env):
        """One renamed key is still randomly-initialised weights. A count
        comparison, or a check that ANY key matched, would wave this through.
        """
        fake_env([f"esmc.{_T4_KEYS[0]}"] + [f"esmc.{_T5_KEYS[1]}"])
        assert rp._esmc_checkpoint_mismatch()

    def test_an_unreadable_index_proceeds_and_logs(
        self, fake_env, monkeypatch, caplog
    ):
        """Fails OPEN, and this test asserts the OPPOSITE of what it
        asserted when first written.

        It demanded a refusal on an unreadable index. review-code round
        4 named the blast radius: nothing here exercises the real
        huggingface_hub, torch or transformers (all three are absent
        from this interpreter), so the first real execution of that arm
        is in production, and a refusal there fails EVERY run of the
        tool -- including the ones whose weights are fine. A hub blip
        would have been a wider outage than the bug being guarded.
        So: log, and let the designer load.
        """
        fake_env(_T4_KEYS)
        boom = types.ModuleType("huggingface_hub")

        def _raise(*_a, **_k):
            raise OSError("404 revision not found")

        boom.hf_hub_download = _raise
        # monkeypatch, not a bare assignment: a raising huggingface_hub left
        # in sys.modules outlives this test and poisons the session.
        monkeypatch.setitem(sys.modules, "huggingface_hub", boom)
        with caplog.at_level(logging.WARNING, logger=rp.logger.name):
            assert rp._esmc_checkpoint_mismatch() == ""
        assert "404 revision not found" in caplog.text, caplog.text

    def test_an_unbuildable_skeleton_still_checks_the_other_class(
        self, fake_env, caplog
    ):
        """``continue``, not ``return``: a class that cannot be built
        says nothing about the other one, and the break this exists to
        catch is visible in either. Here ``AutoModel`` is unresolvable
        and the transformers-5 layout is still refused through
        ``AutoModelForMaskedLM``."""
        mod = fake_env([f'esmc.{k}' for k in _T5_KEYS])
        mod.AutoModel._model_mapping = {}
        with caplog.at_level(logging.WARNING, logger=rp.logger.name):
            detail = rp._esmc_checkpoint_mismatch()
        assert "ESMCForMaskedLM" in detail, detail
        assert "AutoModel skeleton" in caplog.text, caplog.text

    def test_no_buildable_skeleton_at_all_proceeds(self, fake_env, caplog):
        """The last word on Risk 1: when the check cannot run at all it
        stands aside. An un-exercised code path must not be able to
        take the tool offline for every user."""
        mod = fake_env([f'esmc.{k}' for k in _T5_KEYS])
        mod.AutoModel._model_mapping = {}
        mod.AutoModelForMaskedLM._model_mapping = {}
        with caplog.at_level(logging.WARNING, logger=rp.logger.name):
            assert rp._esmc_checkpoint_mismatch() == ""


# ===========================================================================
# The refusal has to survive the orchestrator
# ===========================================================================
#
# Found by review-code on this delta, and the reason this class is
# BEHAVIOURAL where the assertion above it is static: the static one greps
# for ``_fail_preflight("weights"`` in the source, so it passes whether or
# not the bucket ever reaches the hub. It did not. ``run_tool`` is a
# pass-through for ``n_seeds == 1`` (the three prod failures), but for
# n_seeds >= 2 it fans out and ``_aggregate`` replaced every child error
# with the bare string "All seeds returned zero designs", which matches no
# _FAILURE_RULES row. A free, correctly-diagnosed refusal was reported to
# the customer as "The run stopped for a reason we could not identify."


class TestTheBucketSurvivesFanout:
    """``n_seeds`` is on the form (tools/esmfold2_design/__init__.py), so the
    multi-seed path is reachable by any user, not just a campaign."""

    @staticmethod
    def _refusing_child(seed: int) -> dict:
        """A child that refused in preflight: exit 1, zero designs, bucket."""
        return {
            "exit_code": 1,
            "provider_job_id": f"child-{seed}",
            "smoke_result": {
                "status": "FAILED",
                "tier": "design",
                "designs_total": 0,
                "designs_completed": 0,
                "n_failures": 1,
                "designs": [],
                "candidates": [],
                "error": {
                    "bucket": "preflight",
                    "check": "weights",
                    "detail": f"biohub/ESMC-6B@{rp._ESMC_REVISION} does not match",
                },
            },
        }

    def test_a_multi_seed_refusal_keeps_the_bucket(self):
        from tools.esmfold2_design.modal_app import _aggregate

        successes = [(s, self._refusing_child(s)) for s in (7, 8)]
        out = _aggregate(successes, [], {"tier": "design", "job_id": "umb"})
        err = out["smoke_result"]["error"]
        assert isinstance(err, dict), f"bucket discarded: {err!r}"
        assert err["bucket"] == "preflight"
        assert err["check"] == "weights"
        assert "does not match" in err["detail"]

    def test_the_generic_string_still_covers_what_it_was_written_for(self):
        """Children that RAN and produced nothing keep the old message --
        the fix forwards an error, it does not invent one."""
        from tools.esmfold2_design.modal_app import _aggregate

        barren = dict(self._refusing_child(1))
        barren["exit_code"] = 0
        barren["smoke_result"] = dict(barren["smoke_result"])
        barren["smoke_result"].pop("error")
        barren["smoke_result"]["status"] = "COMPLETED"
        out = _aggregate([(1, barren)], [], {"tier": "design", "job_id": "u"})
        assert out["smoke_result"]["error"] == "All seeds returned zero designs"

    def test_the_surviving_bucket_reaches_the_customer_as_our_fault(self):
        """End-to-end over the real seams, no stubs: aggregate -> billing
        interpreter -> failure class -> page copy."""
        from gpu.modal_client import _interpret_pipeline_return
        from shared.jobs import (
            ToolJob,
            classify_terminal_state,
            failure_advice,
        )
        from tools.esmfold2_design.modal_app import _aggregate

        out = _aggregate(
            [(s, self._refusing_child(s)) for s in (0, 1)],
            [],
            {"tier": "design", "job_id": "umb"},
        )
        interpreted = _interpret_pipeline_return(out)
        assert interpreted["error_bucket"] == "preflight", interpreted

        assert (
            classify_terminal_state(status="failed", error=out["smoke_result"]["error"])
            == "preflight_miss"
        )

        job = ToolJob.from_row(
            {
                "id": "00000000-0000-4000-8000-00000000beef",
                "user_id": "00000000-0000-4000-8000-00000000cafe",
                "tool": "esmfold2-design",
                "preset": "minibinder",
                "status": "failed",
                "failure_class": "preflight_miss",
                "inputs": {},
                "result": None,
                "error": interpreted.get("error"),
                "modal_function_call_id": "fc-x",
                "job_token": "t" * 64,
                "gpu_seconds_used": None,
                "created_at": "2026-09-30T12:00:00Z",
                "started_at": None,
                "completed_at": "2026-09-30T12:01:00Z",
            }
        )
        advice = failure_advice(job)
        assert advice["kind"] == "our_side", advice
        assert "not because of your input" in advice["cause"], advice
        # The advice Leo disproved by submitting seed 7 and failing identically.
        assert "seed" not in advice["fix"].lower(), advice

    def test_the_weights_refusal_is_our_fault_in_BOTH_stored_shapes(self):
        """The two live write paths store the bucket/check pair
        differently, and ``_FAILURE_RULES`` matches the flattened text,
        so a rule can hold for one shape and miss the other.

        The poll path (blueprints/jobs.py:945-948) stores the flattened
        ``"preflight:weights -- detail"``; the webhook
        (webhooks/modal.py:154) stores the raw ``{bucket, check,
        detail}`` dict, which ``_error_text`` joins as "preflight
        weights ...". Before the ``preflight[: ]`` class on the
        ``our_side`` row, only the first matched and the second fell to
        ``generic`` -- the right fix text with the wrong cause, telling a
        customer nothing about whose fault a broken checkpoint on our own
        Volume is. Found by review-claims, measured, then fixed.
        """
        from shared.jobs import failure_advice

        from gpu.modal_client import _stringify_error

        raw = {
            "bucket": "preflight",
            "check": "weights",
            "detail": "biohub/ESMC-6B@45b0fa5d does not match ESMCModel",
        }
        shapes = {
            "webhook (raw dict)": raw,
            "poll (flattened)": {
                "bucket": "preflight",
                "detail": _stringify_error(raw),
            },
        }
        for name, stored in shapes.items():
            job = types.SimpleNamespace(
                status="failed",
                error=stored,
                error_bucket="preflight",
                failure_class="preflight_miss",
                inputs={"seed": 7},
            )
            advice = failure_advice(job)
            assert advice["kind"] == "our_side", (name, advice)
            assert "not because of your input" in advice["cause"], (
                name,
                advice,
            )
            assert "seed" not in advice["fix"].lower(), (name, advice)

    def test_the_shared_preflight_sentence_attributes_fault_to_neither_side(self):
        """One sentence serves checks on BOTH sides, so it can claim neither.

        ``shared/email.py::_result_summary`` renders the failure email from
        ``failure_notice`` (the failure-CLASS sentence), while the job page
        renders ``failure_advice`` (the per-CHECK rule) -- except that a
        check matching no rule falls to ``kind="generic"``, whose cause IS
        this same class sentence. For those checks it is both surfaces.

        An earlier version of this test asserted that every check routing
        here is on the ``our_side`` row. That was FALSE and review-claims
        caught it: mpnn emits ``preflight:fixed_positions`` for "chain 'B'
        is not in the input PDB" (tools/mpnn/run_pipeline.py:301-305), which
        genuinely IS the customer's input. So the sentence is pinned the
        only way it can be -- it must not say their input was rejected
        (false for ESM-C weights, af2's jax-gpu, mpnn's module) and must not
        say our side either (false for fixed_positions).
        """
        import re

        from shared.jobs import (
            _ERROR_BUCKET_TO_FAILURE_CLASS,
            _FAILURE_CLASS_PLAIN_WORDS,
            _FAILURE_RULES,
        )

        assert _ERROR_BUCKET_TO_FAILURE_CLASS["preflight"] == "preflight_miss"
        our_side = next(r for r in _FAILURE_RULES if r[0] == "our_side")
        # The our-side checks DO reach the rule, which is what makes the
        # email's old "rejected the input" wrong for them. The input-side
        # one does NOT reach it, which is why the shared sentence that
        # serves both cannot be flipped to blame us instead.
        assert our_side[1].search("preflight:weights — anything")
        assert not our_side[1].search(
            "preflight:fixed_positions — chain 'B' is not in the input PDB"
        ), "fixed_positions is the customer's input; it must not read as ours"

        sentence = _FAILURE_CLASS_PLAIN_WORDS["preflight_miss"]
        assert not re.search(r"\b(your|the) input\b", sentence, re.I), sentence
        assert not re.search(r"\bour (side|end)\b", sentence, re.I), sentence
