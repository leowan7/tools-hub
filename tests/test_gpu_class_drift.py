"""Pin every wallet ``ToolSpec.gpu_class`` to the GPU its container runs on.

``ToolSpec.gpu_class`` picks the row of ``GPU_USD_PER_SECOND`` that prices the
estimate, the hold and -- via ``gpu_class_for_job`` -- the settle-side charge.
It is a hand-written string with no link to the Modal app that actually runs
the job, so it drifts silently and the only symptom is a wrong price. Four of
the fifteen specs had drifted when this test was written: mpnn said L4 on an
A10G container, bindcraft and pxdesign said A100-40GB on A100-80GB containers,
and boltzgen said A100-80GB on an A100-40GB container.

Ground truth per tool:

* Tools whose Modal app lives in this repo -- the ``_GPU`` literal in
  ``tools/<dir>/modal_app.py``, PLUS a check that ``gpu=_GPU`` is what the
  ``@app.function`` decorator is actually handed. Reading the literal alone
  proves nothing: a decorator passing ``gpu="H100"`` beside ``_GPU = "A10G"``
  would leave this guard green over an 11.6x underbill.
* The five composites deployed from llm-proteinDesigner (bindcraft, boltzgen,
  pxdesign, rfantibody, rfdiffusion) -- ``shared.pdb_preflight_rules
  .TOOL_RULES[slug].gpu``, the existing in-repo mirror of that repo's ``_GPU``.
  It is a mirror, not the source, so it can drift from llm-proteinDesigner
  together with the wallet spec; what this test buys for those five is that the
  billing rate and the hardware shown on the preflight panel cannot disagree,
  and that one edit updates both. That is narrow, and it is worth being blunt
  about: 3 of the 4 bugs this file was written for (bindcraft, pxdesign,
  boltzgen) were in exactly the class it does NOT close for those five.
  ``docs/MODAL-GPU-MANIFEST-DESIGN.md`` Path 2 (a ``contracts/gpu_manifest
  .json`` emitted by llm-proteinDesigner at deploy time) is the real fix; the
  cross-repo lock-file precedent it would follow already exists in
  ``.github/workflows/contracts-drift.yml``. It needs a PR on that repo first
  and is not implemented. Independently of that, each composite in that repo
  also declares its SKU as a plain string on its ToolPipeline
  (backend/pipelines/<tool>.py, ``gpu_sku``), and all five agree with the
  classes here -- corroboration for the three composite corrections this file
  cannot itself pin, and a better mirror candidate than TOOL_RULES for the
  manifest work, since it sits in the same repo as ``_GPU``.

Note this reads the working tree, which equals the DEPLOYED hardware only if
the app has been redeployed since. It catches source drift, not deploy drift.

Every way a slug can quietly escape coverage is itself a failure here, because
a drift guard that silently covers nothing is worse than no guard:

* a spec with neither ground-truth source fails (never skips);
* a container directory with no spec fails, so a new GPU app cannot be added
  and billed at the default rate while this file looks green;
* an empty parameter set fails, rather than reporting green as pytest's
  ``got empty parameter set`` skip;
* the three copies of the rate card must agree, or a class added to one is
  billed at the default rate through another.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from shared.pdb_preflight_rules import TOOL_RULES
from shared.wallet_estimates import (
    DEFAULT_USD_PER_SECOND,
    GPU_USD_PER_SECOND,
    TOOL_SPECS,
    gpu_class_for_job,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# tools/<dir> -> wallet slug, when the two differ.
_DIR_TO_SLUG = {"esmfold2_design": "esmfold2-design"}
# The reverse, plus the historic ``alphafold2`` alias of the ``af2`` adapter
# (see the note in TOOL_SPECS): a spec with no container of its own.
_SLUG_TO_DIR = {v: k for k, v in _DIR_TO_SLUG.items()} | {"alphafold2": "af2"}


def _gpu_from_source(text: str, rel: str) -> str:
    """The GPU class a Modal app actually requests, read structurally.

    Parsed rather than grepped, because two plausible edits defeat a regex: a
    SECOND module-level ``_GPU = ...`` (the last binding is what import leaves
    behind, but a regex takes the first), and a ``# gpu=_GPU`` comment beside a
    literal ``gpu="H100"`` (a substring search for the wiring is satisfied by
    the comment). Both leave the wallet pricing one class while Modal
    allocates another -- 11.6x for A10G against H100 -- with the guard green.
    """
    tree = ast.parse(text)

    literal = None
    for node in tree.body:  # module level only; the LAST binding is the one
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_GPU":
                    assert isinstance(node.value.value, str), (
                        rel + ": _GPU is not a string literal"
                    )
                    literal = node.value.value
    assert literal is not None, (
        rel + " has no module-level _GPU string literal; this test can no "
        "longer read the container's GPU."
    )

    wired = [
        kw.value
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for dec in node.decorator_list
        if isinstance(dec, ast.Call)
        for kw in dec.keywords
        if kw.arg == "gpu"
    ]
    assert wired, (
        rel + " defines _GPU but no decorator passes gpu=..., so nothing here "
        "proves the literal ever reaches Modal."
    )
    for value in wired:
        assert isinstance(value, ast.Name) and value.id == "_GPU", (
            rel + ": a decorator passes gpu=" + ast.unparse(value) + " rather "
            "than the _GPU this test reads, so the wallet would price a class "
            "Modal never allocates."
        )
    return literal


def _modal_app_path(slug: str) -> Path:
    return _REPO_ROOT / "tools" / _SLUG_TO_DIR.get(slug, slug) / "modal_app.py"


def _container_dirs() -> list[str]:
    return sorted(p.parent.name for p in (_REPO_ROOT / "tools").glob("*/modal_app.py"))


def _container_gpu(slug: str) -> tuple[str | None, str | None]:
    """``(gpu_class, source)`` for ``slug``, or ``(None, None)`` if unknown."""
    path = _modal_app_path(slug)
    if path.exists():
        rel = str(path.relative_to(_REPO_ROOT)).replace("\\", "/")
        return _gpu_from_source(path.read_text(encoding="utf-8"), rel), rel
    rules = TOOL_RULES.get(slug)
    if rules is not None:
        return rules.gpu, "shared/pdb_preflight_rules.py"
    return None, None


def test_every_container_has_a_wallet_spec() -> None:
    """A GPU app with no spec bills at the A100-80GB default, unguarded.

    This file's other tests iterate ``TOOL_SPECS``, so on their own a new
    ``tools/<dir>/modal_app.py`` added without a spec is invisible to them:
    ``gpu_class_for_job`` returns None and shared.wallet.gpu_usd_per_second
    falls through to DEFAULT_USD_PER_SECOND -- the exact bug this file exists
    to kill. Iterating the other direction closes that.

    It also closes the aliasing hole: renaming a spec slug away from its
    directory without updating ``_DIR_TO_SLUG`` would drop that tool onto the
    TOOL_RULES mirror branch, silently comparing a mirror against a mirror.
    """
    dirs = _container_dirs()
    assert dirs, "no tools/*/modal_app.py found; this guard is reading nothing"
    missing = [
        d for d in dirs if _DIR_TO_SLUG.get(d, d) not in TOOL_SPECS
    ]
    assert not missing, (
        f"container dirs with no TOOL_SPECS entry: {missing}. Add a spec, or "
        f"map the directory to its wallet slug in _DIR_TO_SLUG."
    )


@pytest.mark.parametrize("slug", sorted(TOOL_SPECS))
def test_wallet_gpu_class_matches_container(slug: str) -> None:
    """The billed GPU class is the one the container is deployed on."""
    gpu, source = _container_gpu(slug)
    assert gpu is not None, (
        f"No GPU ground truth for wallet slug {slug!r}: neither "
        f"{_modal_app_path(slug).relative_to(_REPO_ROOT)} nor a TOOL_RULES "
        "entry exists. Add one, or this spec's billing rate is unpinned."
    )
    assert TOOL_SPECS[slug].gpu_class == gpu, (
        f"{slug}: wallet_estimates prices at {TOOL_SPECS[slug].gpu_class!r} but "
        f"{source} says the container runs on {gpu!r}. The estimate, the hold "
        "and the charge are all wrong by the ratio of the two rates."
    )


@pytest.mark.parametrize("slug", sorted(TOOL_SPECS))
def test_wallet_gpu_class_is_on_the_rate_card(slug: str) -> None:
    """An unlisted class silently bills at DEFAULT_USD_PER_SECOND."""
    gpu_class = TOOL_SPECS[slug].gpu_class
    assert gpu_class in GPU_USD_PER_SECOND, (
        f"{slug}: gpu_class {gpu_class!r} is not a GPU_USD_PER_SECOND key, so "
        "gpu_usd_per_second falls through to the A100-80GB default rate."
    )


def test_the_three_rate_cards_agree() -> None:
    """Three modules keep their own copy of the rate card.

    The estimate reads shared.wallet_estimates', the settle charge reads
    shared.wallet's, and the workspace ledger reads shared.workspaces'. They
    are separate dict objects, so adding a GPU class to one leaves the others
    pricing it at DEFAULT_USD_PER_SECOND -- and the test above, which reads
    only the wallet_estimates copy, would pass over exactly that.

    "Three" is repo-local. llm-proteinDesigner keeps a fourth in
    backend/pipelines/base.py with DIFFERENT numbers (A100-40GB at 0.000675
    against our 0.000714); it prices only that repo's own pre-submit estimate
    and nothing here reads it, but a reader chasing rate-card drift will meet
    it.
    """
    from shared.wallet import GPU_USD_PER_SECOND as WALLET_CARD
    from shared.workspaces import GPU_USD_PER_SECOND as WORKSPACE_CARD

    assert dict(GPU_USD_PER_SECOND) == dict(WALLET_CARD), (
        "shared.wallet_estimates and shared.wallet rate cards diverged; the "
        "quote and the charge would price the same job differently."
    )
    assert dict(GPU_USD_PER_SECOND) == dict(WORKSPACE_CARD), (
        "shared.wallet_estimates and shared.workspaces rate cards diverged."
    )
    # DEFAULT_USD_PER_SECOND is a row of the same card, triplicated in the same
    # three modules and previously uncompared. It is what every unspecced slug
    # and every off-card reported SKU prices at, so a divergence here is the
    # residue this whole change is about.
    from shared.wallet import DEFAULT_USD_PER_SECOND as WALLET_DEFAULT
    from shared.workspaces import DEFAULT_USD_PER_SECOND as WORKSPACE_DEFAULT

    assert DEFAULT_USD_PER_SECOND == WALLET_DEFAULT == WORKSPACE_DEFAULT, (
        "the three DEFAULT_USD_PER_SECOND copies diverged"
    )
    assert DEFAULT_USD_PER_SECOND == GPU_USD_PER_SECOND["A100-80GB"], (
        "the default rate is documented everywhere as the A100-80GB rate"
    )


def test_the_rate_card_values_are_what_modal_charges() -> None:
    """The card itself was unpinned: scaling a row 10x passed everything.

    These are the rate card's OWN documented USD/hour figures, divided by 3600.
    They are deliberately conservative upper bounds, not Modal's list prices --
    shared/wallet.py:120 says so, and A100-40GB is "rounded up from $2.10
    list". Do not "correct" them against modal.com/pricing: that would
    under-bill A100-40GB by 18%. A typo here misprices every job on that class,
    in both the quote and the charge, with no other test noticing.
    """
    expected_usd_per_hour = {
        "A10G": 0.75, "A100-40GB": 2.57, "A100-80GB": 3.70,
        "H100": 8.70, "L4": 0.85, "L40S": 2.15, "T4": 0.59,
    }
    assert set(GPU_USD_PER_SECOND) == set(expected_usd_per_hour), (
        "a GPU class was added or removed; price it here too"
    )
    for gpu_class, per_hour in expected_usd_per_hour.items():
        actual = GPU_USD_PER_SECOND[gpu_class] * 3600
        assert abs(actual - per_hour) < 0.02, (
            f"{gpu_class}: rate card says ${actual:.2f}/hr, expected "
            f"${per_hour:.2f}/hr"
        )


_PREFLIGHT_SLUGS_WITH_CONTAINER = sorted(
    s for s in TOOL_RULES if _modal_app_path(s).exists()
)


def test_the_preflight_cross_check_is_not_empty() -> None:
    """Guard the guard: an empty parametrize is a green skip, not a failure."""
    assert _PREFLIGHT_SLUGS_WITH_CONTAINER, (
        "no TOOL_RULES slug has an in-repo modal_app.py, so "
        "test_preflight_gpu_label_matches_container would silently collect "
        "nothing and report green."
    )


@pytest.mark.parametrize("slug", _PREFLIGHT_SLUGS_WITH_CONTAINER)
def test_preflight_gpu_label_matches_container(slug: str) -> None:
    """The preflight panel's hardware row must not drift either."""
    gpu, source = _container_gpu(slug)
    assert TOOL_RULES[slug].gpu == gpu, (
        f"{slug}: pdb_preflight_rules shows {TOOL_RULES[slug].gpu!r} on the "
        f"preflight panel but {source} deploys on {gpu!r}."
    )


# ---------------------------------------------------------------------------
# gpu_class_for_job -- the half that moves money
# ---------------------------------------------------------------------------
#
# The tests above pin the static spec table. They say nothing about the
# resolution that reaches settle_hold, and the settle tests all pass an
# explicit gpu_class= so they bypass it entirely: cutting gpu_class_for_job
# down to `return reported` (what the call sites effectively did before this
# change) left every money-path and email test in the suite green. Without
# these, a refactor dropping the spec fallback silently reinstates "every tool
# charges at the A100-80GB rate".


# Hand-naming a few slugs let the fallback be carved out for the other 11 at
# full green. Every spec, or the coverage is decorative.
@pytest.mark.parametrize("slug", sorted(TOOL_SPECS))
def test_gpu_class_for_job_falls_back_to_the_spec(slug: str) -> None:
    """No wrapper reports a SKU today, so this is the live production path."""
    assert gpu_class_for_job(slug, None) == TOOL_SPECS[slug].gpu_class


def test_the_spec_table_has_not_shrunk() -> None:
    """Deleting a spec is invisible to every parametrized test above.

    They iterate TOOL_SPECS, so a removed entry simply collects fewer cases and
    still reports green -- while the tool it billed drops to
    DEFAULT_USD_PER_SECOND. Six of these have no in-repo container either, so
    test_every_container_has_a_wallet_spec would not notice.
    """
    assert set(TOOL_SPECS) == {
        "af2", "alphafold2", "bindcraft", "boltz2", "boltzgen", "colabfold",
        "esmfold", "esmfold2-design", "iggm", "mpnn", "opendde", "proteina",
        "pxdesign", "rfantibody", "rfdiffusion",
    }, "a wallet spec was added or removed; confirm its gpu_class and update"


def test_gpu_class_for_job_prefers_a_reported_sku() -> None:
    """A wrapper that grows a real SKU must win over the spec."""
    assert gpu_class_for_job("opendde", "A100-40GB") == "A100-40GB"


@pytest.mark.parametrize(
    "reported", ["A100-SXM4-40GB", "a10g", "A100", "A10G ", ""],
)
def test_gpu_class_for_job_ignores_an_unpriceable_report(reported: str) -> None:
    """`reported` is the ONLY path that can introduce a new string at runtime.

    Modal's device strings are not rate-card keys: it reports SXM/PCIe part
    names like "A100-SXM4-40GB" where the card is keyed on "A100-40GB", so
    passing one straight through would price it at DEFAULT_USD_PER_SECOND,
    reinstating this whole bug the first time a wrapper reports honestly. Case and whitespace variants miss
    the dict too. The spec must win over anything unpriceable.
    """
    assert gpu_class_for_job("opendde", reported) == "H100"


def test_gpu_class_for_job_is_none_for_an_unregistered_slug() -> None:
    """Unspecced tools keep the old default-rate behaviour rather than crash."""
    assert gpu_class_for_job("not-a-tool", None) is None
    assert gpu_class_for_job(None, None) is None


@pytest.mark.parametrize("failure_class", [None, "succeeded"])
def test_settle_prices_at_the_spec_when_no_sku_is_reported(failure_class) -> None:
    """End to end: the resolved class is what reaches settle_hold.

    Parametrized over failure_class because _settle_wallet_hold_for_completed_job
    has TWO settle_hold call sites: the classifier branch (failure_class set)
    and the legacy heuristic for pre-0029 rows (failure_class NULL).
    mark_succeeded always stamps a class via classify_terminal_state, so the
    classifier branch is the ONLY one a current job takes -- and pinning just
    the legacy one let the live site be reverted to gpu_class=None at full
    green. Both sites, or this pins the path production never walks.

    An opendde job that consumed a full session must settle at the H100 rate it
    was quoted and held at, not the A100-80GB default the None fallback gave.
    """
    from decimal import Decimal
    from inspect import signature
    from unittest import mock

    from shared.wallet import compute_charge_usd, settle_hold as real_settle

    job = mock.Mock(
        tool="opendde",
        inputs={"_wallet": {"hold_tx_id": "hold-1", "estimate_usd": "14.79"}},
        result={},
        status="succeeded",
        gpu_seconds_used=3600.0,
        error=None,
        failure_class=failure_class,
    )
    with mock.patch("shared.wallet.settle_hold") as settle,             mock.patch("shared.wallet.release_hold"):
        from shared.jobs import _settle_wallet_hold_for_completed_job

        _settle_wallet_hold_for_completed_job(job)

    assert settle.called, "settle_hold was not reached"
    # Bound through the real signature rather than read off .kwargs, so a
    # semantics-preserving switch to positional args stays green instead of
    # raising KeyError and hiding this assertion's message.
    bound = signature(real_settle).bind(*settle.call_args.args, **settle.call_args.kwargs)
    assert bound.arguments["gpu_class"] == "H100", (
        f"settled at {bound.arguments['gpu_class']!r}, not the H100 the job "
        "was quoted and held at"
    )
    # The A100-80GB default would have charged $6.2914 for the same 3600 s.
    assert compute_charge_usd(3600, "H100") == Decimal("14.7920")


def test_the_receipt_quotes_the_rate_the_wallet_charged() -> None:
    """The completion email recomputes the charge rather than reading the row.

    So it must resolve exactly as settle does. Reverted, the receipt reads
    "charged $6.29 (3600 GPU-sec on GPU)" for an opendde job the wallet took
    $14.79 for -- a customer-facing understatement of 2.35x.
    """
    from unittest import mock

    from shared.email import _cost_breakdown_line

    job = mock.Mock(
        tool="opendde",
        inputs={"_wallet": {"hold_tx_id": "hold-1", "estimate_usd": "14.79"}},
        result={},
        gpu_seconds_used=3600.0,
    )
    line = _cost_breakdown_line(job, tone="succeeded")
    assert "on H100" in line, line
    assert "charged $14.79" in line, line

    # And the job.result probe, which result={} above never enters: the whole
    # point of the block is lockstep with the settle path, so a reported SKU
    # must reach the receipt too or the two can quote different rates.
    job.result = {"gpu_sku": "A10G"}
    reported_line = _cost_breakdown_line(job, tone="succeeded")
    assert "on A10G" in reported_line, reported_line


def test_the_overrun_monitor_measures_against_the_billed_rate() -> None:
    """The 1.5x warning compares cumulative cost to the stored estimate.

    Priced at the A100-80GB default instead of the spec, the ratio is wrong by
    the rate ratio: mpnn would warn ~4.9x too eagerly and the H100 tools 2.35x
    too late. 3600 H100 seconds is $14.79, just over 1.5x a $9 estimate.
    """
    from unittest import mock

    from shared.jobs import mid_run_monitor_check

    job = mock.Mock(
        id="job-1",
        tool="opendde",
        status="running",
        inputs={"_wallet": {"hold_tx_id": "hold-1", "estimate_usd": "9.00"}},
        gpu_seconds_used=3600.0,
    )
    with mock.patch("shared.jobs.get_job", return_value=job),             mock.patch("shared.jobs._cas_update"),             mock.patch("shared.jobs._send_overrun_warning") as warn,             mock.patch("shared.jobs._stash_wallet_flag"):
        result = mid_run_monitor_check("job-1", cumulative_gpu_seconds=3600.0)

    assert result == "warned", (
        "no overrun warning: at the A100-80GB default 3600 s is $6.29, only "
        "0.70x the $9.00 estimate, so the 1.5x band is never crossed"
    )
    assert warn.called
