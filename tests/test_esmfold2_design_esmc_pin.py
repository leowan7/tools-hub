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
    mod.AutoModel = types.SimpleNamespace(
        _model_mapping={
            _Config: _model_cls(
                "ESMCModel", "esmc", keys_by_class.get("ESMCModel", _T4_KEYS)
            )
        }
    )
    mod.AutoModelForMaskedLM = types.SimpleNamespace(
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

    ``raising=False``: the real modules are absent from this interpreter, and
    ``monkeypatch.delitem`` on an absent key records nothing to undo.
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

    def test_an_unreadable_index_is_refused_not_ignored(self, fake_env):
        """Fails closed. A missing revision or a network error must not read
        as "the weights are fine" -- that is how this stayed invisible."""
        fake_env(_T4_KEYS)
        boom = types.ModuleType("huggingface_hub")

        def _raise(*_a, **_k):
            raise OSError("404 revision not found")

        boom.hf_hub_download = _raise
        sys.modules["huggingface_hub"] = boom
        detail = rp._esmc_checkpoint_mismatch()
        assert "404 revision not found" in detail, detail
