"""Guards for the rfd4 hub-side adapter (tools/rfd4/__init__.py).

The pipeline's own guards live in tests/test_rfd4_pipeline.py; this file only
covers the web-app half -- the form validator, the job_spec it emits and the
constants it mirrors out of the container.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tools import rfd4 as adapter_mod

REPO = Path(__file__).resolve().parents[1]


def _container_constants() -> dict:
    """Module-level literal assignments in run_pipeline.py, read as source.

    Parsed rather than imported: run_pipeline.py imports ``gemmi`` and
    ``requests`` at module scope and only ever runs inside the GPU image.
    """
    tree = ast.parse(
        (REPO / "tools" / "rfd4" / "run_pipeline.py").read_text(encoding="utf-8")
    )
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    continue
    return out


def test_adapter_bounds_are_the_containers_bounds():
    """The mirrored constants in the adapter are the container's own.

    The adapter cannot import run_pipeline.py (see _container_constants), so
    the two sets of numbers are duplicated. This is what keeps them in step.
    """
    c = _container_constants()
    assert adapter_mod._BINDER_LEN_DEFAULT == c["DEFAULT_BINDER_LENGTH"]
    assert adapter_mod._BINDER_LEN_MIN == c["BINDER_LENGTH_FLOOR"]
    assert adapter_mod._BINDER_LEN_MAX == c["BINDER_LENGTH_CEILING"]
    assert adapter_mod._DEFAULT_NUM_DESIGNS == c["DEFAULT_NUM_DESIGNS"]
    assert adapter_mod._MAX_HOTSPOTS == c["MAX_HOTSPOTS"]


def _form(**over):
    base = {
        "preset": "binder_design",
        "target_chain": "A",
        "hotspot_residues": "56,58,60",
        "binder_length_min": "60",
        "binder_length_max": "120",
        "num_designs": "16",
    }
    base.update(over)
    return base


def test_validate_accepts_a_whole_form_and_stamps_the_scaling_key():
    inputs, err = adapter_mod.validate(_form(), {})
    assert err is None
    assert inputs["target_chain"] == "A"
    assert inputs["hotspot_residues"] == [56, 58, 60]
    # A LIST, not a tuple: run_pipeline.parse_binder_length reads a
    # two-element sequence back out of JSON.
    assert inputs["binder_length"] == [60, 120]
    # The container's key for the design count.
    assert inputs["nsamples"] == 16
    # The wallet's key, by the name shared.wallet_estimates scales on.
    assert inputs["parameters"]["n_designs_total"] == 16


def test_a_blank_chain_field_is_stamped_as_not_named():
    """The default "A" must not look to the container like a customer's pick.

    ``validate`` substitutes "A" for a blank field for its own gates, and
    ``run_pipeline.main`` crops only on a chain the customer named: cropping on
    the substituted default would design against whichever protomer of a dimer
    happens to be chain A. With ``target_chain_named`` False the run reaches
    ``resolve_target_chain``'s refusal instead
    (``tests/test_rfd4_pipeline.py::test_a_two_chain_target_with_no_chain_named_is_refused``).
    """
    named, err = adapter_mod.validate(_form(), {})
    assert err is None
    assert named["target_chain_named"] is True

    inputs, err = adapter_mod.validate(_form(target_chain=""), {})
    assert err is None, err
    assert inputs["target_chain"] == "A"
    assert inputs["target_chain_named"] is False

    # A whitespace-only field never reaches the container at all: `or "A"`
    # keeps the spaces and `parse_target_chains` strips them to nothing.
    assert adapter_mod.validate(_form(target_chain="   "), {})[1] == (
        "Target chain is required."
    )

    # An absent key reads as not named too. blueprints/targets.py sends it
    # as "" rather than omitting it (:104, :213); both land here.
    absent = dict(_form())
    del absent["target_chain"]
    inputs, err = adapter_mod.validate(absent, {})
    assert err is None, err
    assert inputs["target_chain_named"] is False


def test_validate_defaults_match_the_container_defaults():
    inputs, err = adapter_mod.validate(
        {"target_chain": "A", "hotspot_residues": "56"}, {}
    )
    assert err is None
    assert inputs["binder_length"] == list(adapter_mod._BINDER_LEN_DEFAULT)
    assert inputs["nsamples"] == adapter_mod._DEFAULT_NUM_DESIGNS
    assert (
        inputs["parameters"]["n_designs_total"] == adapter_mod._DEFAULT_NUM_DESIGNS
    )


@pytest.mark.parametrize(
    "over",
    [
        {"target_chain": "A,B"},          # run_pipeline.resolve_target_chain _fails
        {"hotspot_residues": ""},         # build_hotspot_selections _fails
        {"hotspot_residues": "56,B58"},   # hotspot off the target chain
        {"num_designs": "0"},
        {"num_designs": "201"},
        {"num_designs": "eight"},
        {"binder_length_min": "19"},      # below BINDER_LENGTH_FLOOR
        {"binder_length_max": "301"},     # above BINDER_LENGTH_CEILING
        {"binder_length_min": "130", "binder_length_max": "120"},
        {"binder_length_min": "sixty"},
        {"preset": "nanobody"},
    ],
)
def test_validate_refuses(over):
    inputs, err = adapter_mod.validate(_form(**over), {})
    assert inputs is None
    assert err


def test_validate_refuses_more_hotspots_than_the_container_takes():
    too_many = ",".join(str(n) for n in range(1, adapter_mod._MAX_HOTSPOTS + 2))
    inputs, err = adapter_mod.validate(_form(hotspot_residues=too_many), {})
    assert inputs is None
    assert str(adapter_mod._MAX_HOTSPOTS) in err


def test_build_payload_carries_every_key_main_reads():
    inputs, err = adapter_mod.validate(_form(), {})
    assert err is None
    payload = adapter_mod.build_payload(inputs, "https://example.invalid/t.cif")
    for key in ("preset", "target_chain", "target_chain_named",
                "hotspot_residues", "binder_length",
                "nsamples", "parameters"):
        assert key in payload, key
    assert payload["parameters"]["n_designs_total"] == 16


def test_the_adapter_is_built_and_the_flag_is_off(monkeypatch):
    """The gate: the contract exists and rfd4 is unreachable twice over.

    ``register(adapter)`` is deliberately NOT in tools/rfd4/__init__.py yet.
    It is one line, and it is the line that makes every guard iterating
    ``tools.base.all_adapters()`` demand a complete registry row: the two
    templates the adapter names, the catalog band, the result columns, the
    score legends, the email label. Those are later steps, and most of them
    are customer-visible copy. Measured both directions on the branch that
    wrote this file and recorded in the step table of
    docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md.

    So two independent gates hold, and both are pinned here:

      * NOT IN THE REGISTRY. blueprints/tools.py::_require_tool reads
        ``tool_base.get(slug)`` and 404s a None in its first arm
        before it looks at the flag, so no env var reaches the form.
      * FLAG OFF. shared/feature_flags.py::tool_enabled is fail-closed on
        an unset env var.

    The step that lands the templates and the registry copy adds
    ``register(adapter)`` and flips the registry assertion below. The flag
    assertions stay, because the flag is what gates reach once a tool is
    registered.
    """
    from shared.feature_flags import flag_name, tool_enabled
    from tools.base import ToolAdapter, get

    assert isinstance(adapter_mod.adapter, ToolAdapter)
    assert adapter_mod.adapter.slug == "rfd4"

    assert get("rfd4") is None, (
        "rfd4 is in the adapter registry. If that is intended, this is the "
        "step that lands tools/rfd4_form.html, tools/rfd4_results.html and "
        "the registry copy -- update this assertion and expect every "
        "all_adapters() guard to demand them."
    )

    assert flag_name("rfd4") == "FLAG_TOOL_RFD4"
    monkeypatch.delenv("FLAG_TOOL_RFD4", raising=False)
    assert tool_enabled("rfd4") is False


def test_an_extra_polymer_chain_passes_the_gates_and_the_container_crops():
    """PINS BOTH SIDES, so neither can move without this going red.

    THE GATES ARE UNCHANGED, by Leo's decision of 2026-11-10 (section 9 item 11
    of docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md): no hub-side refusal, no
    preflight change. The first half below asserts READY for a two-chain upload
    with one chain named, which is the behaviour today, and asserts it for
    ``rfantibody`` and ``bindcraft`` alongside because they answer the same way
    -- so a red here on the preflight half means the house convention moved, not
    that rfd4 grew a bug. The two reasons the gates say READY, for a reader
    checking whether the convention still holds:

      * ``validate`` in tools/rfd4/__init__.py never opens ``files``, so the
        chain count is not a thing it can see.
      * ``shared/pdb_preflight.py::_multi_chain_block`` returns None on
        ``len(chain_ids) <= 1``, and ``chain_ids`` is the list of chains NAMED
        ON THE FORM rather than the chains present in the file. So rfd4's
        ``multi_chain_supported=False`` / ``multi_chain_container_ready=False``
        in shared/pdb_preflight_rules.py are inert for this input.

    THE CONTAINER NOW CROPS, which is the half that changed. It used to
    ``_fail`` in ``resolve_target_chain`` on any polymer chain besides the one
    named -- after the wallet hold and the GPU start, so the customer paid the
    start minimum and received zero designs. The second half asserts the crop
    on the same fixture: the gates let it through and the container keeps the
    named chain and drops the rest, which is what the siblings' normalizers
    already do (``shared/pipeline_normalize.py``).

    WHEN THIS GOES RED, one of the two halves has drifted. Read that scope item
    before editing either assertion.
    """
    from shared.pdb_preflight import VerdictKind, preflight_for_tool

    from tools.rfd4 import numbering
    from tools.rfd4.run_pipeline import _polymer_chains, crop_pdb_to_chain

    # The suite's own builders rather than a hand-rolled body: a PDB that
    # _parse_structure accepts is fiddlier than it looks, and a broken
    # fixture here would read as the gap being closed.
    from tests.test_pdb_preflight import _chain_pdb, _two_chain_pdb

    one_chain = _chain_pdb("A", range(1, 61))
    two_chains = _two_chain_pdb(range(1, 61), range(1, 61))

    for slug in ("rfd4", "rfantibody", "bindcraft"):
        control = preflight_for_tool(
            slug, one_chain, target_chain="A", hotspots=[10, 12],
            binder_max_aa=120, num_designs=8,
        )
        assert control.kind is VerdictKind.READY, (
            f"{slug}: the single-chain control is not READY "
            f"({control.kind}: {control.reason!r}), so the fixture is at "
            f"fault rather than the multi-chain finding."
        )

        verdict = preflight_for_tool(
            slug, two_chains, target_chain="A", hotspots=[10, 12],
            binder_max_aa=120, num_designs=8,
        )
        assert verdict.kind is VerdictKind.READY, (
            f"{slug}: a two-chain upload with one chain named now gets "
            f"{verdict.kind} ({verdict.reason!r}) where it used to get "
            f"READY. The 2026-11-10 decision is that the gates stay as they "
            f"are and the container crops; see section 9 item 11 of "
            f"docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md."
        )

    # And the container side of the same input: cropped, not refused.
    whole_text = two_chains.decode()
    assert _polymer_chains(numbering.pdb_to_cif(whole_text)[1]) == ["A", "B"]
    cropped = crop_pdb_to_chain(whole_text, "A")
    assert cropped is not None
    assert _polymer_chains(numbering.pdb_to_cif(cropped)[1]) == ["A"]


def test_the_wallet_actually_reads_the_stamped_key_off_the_job_spec():
    """EXECUTES the lookup the step-2 guard only greps for.

    tests/test_rfd4_pipeline.py::test_the_adapter_stamps_the_key_the_wallet_
    scales_on asserts the literal ``"n_designs_total"`` appears in the
    adapter's SOURCE. That passes even if the key were nested somewhere
    ``_effective_scaling_value`` never looks -- under ``inputs["target"]``,
    say. This runs the validator and hands its output to the function the
    wallet prices with, so the two halves are joined by execution.

    The fallback it guards is silent: shared/wallet_estimates.py:1284-1288
    reads the key top-level, then under ``parameters``, then
    ``_safe_float(value, spec.designs_per_run_baseline)``. A key the lookup
    cannot see is indistinguishable from no key at all, and prices as eight.
    """
    from shared import wallet_estimates as we

    spec = we.TOOL_SPECS["rfd4"]
    inputs, err = adapter_mod.validate(_form(num_designs="200"), {})
    assert err is None, err

    assert we._effective_scaling_value(spec, inputs) == 200.0, (
        f"validate() stamped {spec.scaling_param!r} somewhere the wallet does "
        f"not read: the lookup returned the baseline instead of 200. "
        f"inputs={inputs!r}"
    )

    # The control proves the assertion above has teeth rather than matching a
    # number that happens to coincide: strip the key and the same call falls
    # back to the baseline WITHOUT raising, which is the whole hazard.
    stripped = {k: v for k, v in inputs.items() if k != "parameters"}
    stripped.pop(spec.scaling_param, None)
    assert we._effective_scaling_value(spec, stripped) == float(
        spec.designs_per_run_baseline
    ), "the silent fallback this test guards is not reachable any more"
