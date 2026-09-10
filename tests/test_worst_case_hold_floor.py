"""Worst-case hold floor for fixed-container / session-capped tools.

These tools bill ACTUAL wall-clock up to a physical Modal session cap
(``_MAX_SESSION_S`` in each tool's ``modal_app.py``). Their historical-p90
estimate only right-sizes the DISPLAYED price; it must never shrink the wallet
HOLD below the marked-up charge a single full-session job can still incur, or an
under-funded user running a heavy job leaves Ranomics silently absorbing the
variance.

``ToolSpec.worst_case_gpu_seconds`` floors ``cushioned_hold_usd`` at that
full-session charge. Two container shapes:

* Single-container tools (proteina: one shard = one container; af2: the whole
  batch folds inside one container; alphafold2: legacy mirror) floor FLAT at one
  container's cap regardless of the scaling param.
* Fan-out tools (esmfold2-design: one H100 container PER seed) set
  ``worst_case_scales_with_param=True`` so the single job-level hold scales with
  the container count — a flat floor would cover only one seed and a p90-shrunk
  multi-seed job would still under-hold.

Each test stubs ``_historical_p90_seconds`` to a small value (simulating the
>=20-run p90 shrink) and asserts the hold still covers the worst-case charge and
never exceeds the hard cap (the clamp that keeps settle safe).
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from shared import wallet_estimates as we

_REPO_ROOT = Path(__file__).resolve().parents[1]

# wallet slug -> the tools/<dir> whose modal_app.py owns its _MAX_SESSION_S.
# Every spec carrying ``worst_case_gpu_seconds`` must appear here; the test
# below fails if one does not, so a new fixed-container tool cannot register a
# floor with nothing pinning it to a container.
_SESSION_CAP_SOURCE = {
    "af2": "af2",
    "alphafold2": "af2",  # historic alias, same container
    "proteina": "proteina",
    "esmfold2-design": "esmfold2_design",
    "opendde": "opendde",
}

# A p90 far below every tool's session cap, to force the floor (not the cushion)
# to be what holds the line.
_LOW_P90_SECONDS = 120.0


def _max_billable(slug: str, params: dict, container_seconds: float, ratio: int) -> Decimal:
    """The most a full-session run of ``slug`` can be billed: the marked-up
    charge for ``container_seconds * ratio`` GPU-seconds, clamped to the
    parameter-scaled hard cap (settle clamps the charge there; Ranomics absorbs
    above it, so the hold need never exceed it)."""
    spec = we.TOOL_SPECS[slug]
    rate = Decimal(str(we.GPU_USD_PER_SECOND[spec.gpu_class]))
    charge = Decimal(str(container_seconds)) * ratio * rate * we.WALLET_MARKUP
    cap = we.compute_hard_cap(slug, params)
    return min(charge, cap).quantize(Decimal("0.0001"))


def _session_cap(slug: str) -> float:
    """``_MAX_SESSION_S`` read out of the tool's ``modal_app.py`` source.

    Read rather than imported, following the precedent in
    ``tests/test_gpu_class_drift.py``, which reads ``_GPU`` out of these same
    files the same way. Read structurally rather than grepped, and taking the
    LAST module-level binding, because that is the one an import would leave
    behind.
    """
    path = _REPO_ROOT / "tools" / _SESSION_CAP_SOURCE[slug] / "modal_app.py"
    value = None
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "_MAX_SESSION_S":
                value = node.value
    assert value is not None, f"{path} has no module-level _MAX_SESSION_S"
    try:
        seconds = ast.literal_eval(value)
    except ValueError:  # pragma: no cover - only if someone writes `90 * 60`
        raise AssertionError(
            f"{path}: _MAX_SESSION_S is {ast.unparse(value)!r}. Keep it a plain "
            "integer literal so this guard can read it without executing the "
            "module."
        ) from None
    assert isinstance(seconds, int), f"{path}: _MAX_SESSION_S is not an int"
    return float(seconds)


@pytest.fixture
def low_p90(monkeypatch):
    """Force the historical-p90 branch to return a small value for every slug."""
    monkeypatch.setattr(we, "_historical_p90_seconds", lambda slug: _LOW_P90_SECONDS)


# ---------------------------------------------------------------------------
# Single-container tools: flat floor
# ---------------------------------------------------------------------------


def test_proteina_hold_floored_at_shard_worst_case(low_p90):
    # One proteina shard = one A100-80GB container capped at 7200 s. Priced at
    # the fixed baseline (num_designs=8), so the floor does not scale.
    params = {"num_designs": 8, "preset": "pilot"}
    hold = we.cushioned_hold_usd(None, "proteina", params)
    worst = _max_billable("proteina", params, 7200.0, 1)
    cap = we.compute_hard_cap("proteina", params)
    assert worst == Decimal("12.5827")  # 7200 s * A100-80GB rate * 1.70
    assert hold >= worst, f"proteina under-held: {hold} < {worst}"
    assert hold <= cap


def test_af2_single_fold_hold_floored_at_max_billable(low_p90):
    # One AF2 fold = one A100-80GB container capped at 14400 s. The full-session
    # charge ($25.17) exceeds the $1.50 base cap, so the max billable — and the
    # floor — is the cap.
    params = {"preset": "pilot"}
    hold = we.cushioned_hold_usd(None, "af2", params)
    worst = _max_billable("af2", params, 14400.0, 1)
    cap = we.compute_hard_cap("af2", params)
    assert worst == cap == Decimal("1.5000")
    assert hold >= worst, f"af2 under-held: {hold} < {worst}"
    assert hold <= cap


def test_af2_batch_hold_covers_one_container_not_scaled(low_p90):
    # The batch folds all records SEQUENTIALLY in ONE container, so the job worst
    # case is one container regardless of n_designs_total: the flat floor covers
    # it, and the hold must not balloon past the max a single container bills.
    params = {"n_designs_total": 50, "preset": "pilot"}
    hold = we.cushioned_hold_usd(None, "af2", params)
    one_container = _max_billable("af2", params, 14400.0, 1)
    cap = we.compute_hard_cap("af2", params)
    assert hold >= one_container, f"af2 batch under-held: {hold} < {one_container}"
    assert hold <= cap


def test_alphafold2_legacy_mirror_hold_floored(low_p90):
    # Historic key, never read by the prod wallet route, kept consistent with af2.
    params: dict = {}
    hold = we.cushioned_hold_usd(None, "alphafold2", params)
    worst = _max_billable("alphafold2", params, 14400.0, 1)
    assert hold >= worst
    assert hold <= we.compute_hard_cap("alphafold2", params)


# ---------------------------------------------------------------------------
# Fan-out tool: floor scales with the container count
# ---------------------------------------------------------------------------


def test_esmfold2_single_seed_hold_floored(low_p90):
    params = {"n_seeds": 1, "preset": "pilot"}
    hold = we.cushioned_hold_usd(None, "esmfold2-design", params)
    worst = _max_billable("esmfold2-design", params, _session_cap("esmfold2-design"), 1)
    # At _MAX_SESSION_S = 5400 s the raw full-session charge is $22.19, so the
    # $15 base_hard_cap is what a seed actually bills — settle clamps there.
    # (At the old 3600 s it was $14.79, under the cap, and the hold sat $0.21
    # below the max chargeable.)
    assert worst == Decimal("15.0000")
    assert hold >= worst, f"esmfold2 1-seed under-held: {hold} < {worst}"
    assert hold <= we.compute_hard_cap("esmfold2-design", params)


@pytest.mark.parametrize("n_seeds", [2, 8, 64])
def test_esmfold2_multi_seed_hold_scales_with_seeds(low_p90, n_seeds):
    # The critical regression: one job-level hold covers n_seeds separate H100
    # containers, so a p90-shrunk multi-seed job needs a floor that scales. A flat
    # per-seed floor ($15.00 at a 5400 s session) would cover only ONE seed.
    params = {"n_seeds": n_seeds, "preset": "pilot"}
    hold = we.cushioned_hold_usd(None, "esmfold2-design", params)
    worst = _max_billable(
        "esmfold2-design", params, _session_cap("esmfold2-design"), n_seeds
    )
    cap = we.compute_hard_cap("esmfold2-design", params)
    assert hold >= worst, f"esmfold2 {n_seeds}-seed under-held: {hold} < {worst}"
    assert hold <= cap
    # Sanity: the flat one-seed floor would NOT have covered this job.
    flat_one_seed = _max_billable(
        "esmfold2-design", {"n_seeds": 1}, _session_cap("esmfold2-design"), 1
    )
    assert worst > flat_one_seed


# ---------------------------------------------------------------------------
# Invariants: the floor only ever RAISES the hold and never exceeds the cap
# ---------------------------------------------------------------------------


def test_floor_never_exceeds_hard_cap(low_p90):
    for slug, params in [
        ("proteina", {"num_designs": 8}),
        ("af2", {}),
        ("af2", {"n_designs_total": 50}),
        ("alphafold2", {}),
        ("esmfold2-design", {"n_seeds": 1}),
        ("esmfold2-design", {"n_seeds": 64}),
    ]:
        hold = we.cushioned_hold_usd(None, slug, params)
        cap = we.compute_hard_cap(slug, params)
        assert Decimal("0") < hold <= cap, f"{slug} {params}: hold {hold} > cap {cap}"


def test_bootstrap_holds_unchanged_by_floor(monkeypatch):
    """The floor must never LOWER a hold below its pre-floor bootstrap value.

    Both pinned values below are the HARD CAP, not a cushion/container-max
    coincidence — the docstring here used to say otherwise for proteina and was
    wrong: its point estimate is $12.5827, x1.5 is $18.87, and compute_hard_cap
    clamps that to $15.00 at num_designs=8. The container max is $12.58 and
    equals neither.

    For esmfold2-design the floor has stopped being a no-op at all: 2400 s x 1.5
    is 3600 s, which WAS the container max and is now 5400 s, so the floor
    raises the 1-seed bootstrap hold from $14.79 to the $15.00 cap.
    """
    monkeypatch.setattr(we, "_historical_p90_seconds", lambda slug: None)
    assert we.cushioned_hold_usd(None, "proteina", {"num_designs": 8}) == Decimal("15.0000")
    assert we.cushioned_hold_usd(None, "esmfold2-design", {"n_seeds": 1}) >= Decimal("14.7920")
    assert we.cushioned_hold_usd(None, "esmfold2-design", {"n_seeds": 8}) >= Decimal("118.3363")


# ---------------------------------------------------------------------------
# The floor is only correct while it names the container's actual cap
# ---------------------------------------------------------------------------


def test_every_worst_case_spec_has_a_container_to_pin_to() -> None:
    """A floor with no named container is a floor nobody can check.

    The test below iterates ``_SESSION_CAP_SOURCE``, so a new fixed-container
    tool that sets ``worst_case_gpu_seconds`` without adding itself there would
    be pinned by nothing while this file still reported green.
    """
    unpinned = sorted(
        slug for slug, spec in we.TOOL_SPECS.items()
        if spec.worst_case_gpu_seconds and slug not in _SESSION_CAP_SOURCE
    )
    assert not unpinned, (
        f"specs with worst_case_gpu_seconds and no container mapping: "
        f"{unpinned}. Add them to _SESSION_CAP_SOURCE."
    )


@pytest.mark.parametrize("slug", sorted(_SESSION_CAP_SOURCE))
def test_worst_case_floor_matches_container_cap(slug: str) -> None:
    """``worst_case_gpu_seconds`` IS the container's ``_MAX_SESSION_S``.

    The two are the same number by design and by hand, in two files, with
    nothing connecting them. Raise the container ceiling alone and the floor
    goes on pricing the old one, on exactly the heavy jobs the floor exists for.

    Why pin the CONSTANTS and not just the dollars: whether a divergence shows
    up in money depends on where the hard cap sits, and the cap can hide it.
    Both directions were mutation-checked against ``esmfold2-design``:

    * spec 5400 -> 3600 (floor below container): caught here AND by
      ``test_hold_covers_a_full_session_container`` (under-held 14.7920
      < 15.0000), because the stale floor falls below the cap.
    * container 5400 -> 7200 (container above floor): caught ONLY here. The
      $15/seed cap binds either way, so every dollar assertion stays green
      while the spec silently describes a ceiling that no longer exists.

    The second is why this test is not redundant. It was written alongside the
    3600 -> 5400 move in this same change — the first time these two numbers
    could have diverged, since the container constant had not moved since the
    tool was introduced.
    """
    spec = we.TOOL_SPECS[slug]
    assert spec.worst_case_gpu_seconds, (
        f"{slug} is mapped to a container but sets no worst_case_gpu_seconds; "
        f"remove it from _SESSION_CAP_SOURCE or give it a floor."
    )
    container = _session_cap(slug)
    assert spec.worst_case_gpu_seconds == container, (
        f"{slug}: worst_case_gpu_seconds={spec.worst_case_gpu_seconds} but "
        f"tools/{_SESSION_CAP_SOURCE[slug]}/modal_app.py caps the session at "
        f"{container} s. Edit both together."
    )


@pytest.mark.parametrize("slug", sorted(_SESSION_CAP_SOURCE))
def test_hold_covers_a_full_session_container(slug: str, low_p90) -> None:
    """End to end: a p90-shrunk estimate still holds the full-session charge.

    ``test_worst_case_floor_matches_container_cap`` checks the constants agree;
    this checks the agreement buys what it is for, priced off the container
    source rather than off the spec it is validating.
    """
    params: dict = {"n_seeds": 1} if slug == "esmfold2-design" else {}
    hold = we.cushioned_hold_usd(None, slug, params)
    worst = _max_billable(slug, params, _session_cap(slug), 1)
    assert hold >= worst, f"{slug} under-held: {hold} < {worst}"
    assert hold <= we.compute_hard_cap(slug, params)


# ---------------------------------------------------------------------------
# The three esmfold2-design timeouts must stay nested
# ---------------------------------------------------------------------------


def _esmfold2_timeouts() -> dict[str, int]:
    """``{name: seconds}`` for the three nested timeouts, evaluated from source.

    ``_ORCHESTRATOR_TIMEOUT_S`` and the subprocess budget are both EXPRESSIONS
    over ``_MAX_SESSION_S``, so they are compiled in a namespace holding only
    that value — no import, and nothing else is reachable from it.
    """
    import ast as _ast

    path = _REPO_ROOT / "tools" / "esmfold2_design" / "modal_app.py"
    tree = _ast.parse(path.read_text(encoding="utf-8"))
    worker = _session_cap("esmfold2-design")
    ns = {"_MAX_SESSION_S": int(worker), "max": max}

    orchestrator = None
    for node in tree.body:
        if (
            isinstance(node, _ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], _ast.Name)
            and node.targets[0].id == "_ORCHESTRATOR_TIMEOUT_S"
        ):
            orchestrator = eval(  # noqa: S307 - arithmetic over one pinned int
                compile(_ast.Expression(node.value), str(path), "eval"), ns, {}
            )
    assert orchestrator is not None, f"{path}: no _ORCHESTRATOR_TIMEOUT_S"

    # The subprocess budget inside _run_one_seed: the `timeout=` kwarg on the
    # subprocess.run call. Located structurally so a moved call still resolves.
    subprocess_budget = None
    for node in _ast.walk(tree):
        if (
            isinstance(node, _ast.Call)
            and "subprocess.run" in _ast.unparse(node.func)
        ):
            for kw in node.keywords:
                if kw.arg == "timeout":
                    subprocess_budget = eval(  # noqa: S307 - same pinned ns
                        compile(_ast.Expression(kw.value), str(path), "eval"),
                        ns, {},
                    )
    assert subprocess_budget is not None, (
        f"{path}: no subprocess.run(timeout=...) found; this guard is reading "
        f"nothing."
    )
    return {
        "subprocess": int(subprocess_budget),
        "worker": int(worker),
        "orchestrator": int(orchestrator),
    }


def test_esmfold2_timeouts_stay_nested() -> None:
    """subprocess < worker < orchestrator, or a layer kills the one below it.

    Each bound does a different job and each inversion is silent:

    * subprocess >= worker: ``subprocess.run`` never raises ``TimeoutExpired``,
      so Modal kills the container first and the ``finally`` that parks the raw
      archive (modal_app ``_park_raw_archive``) never runs. That archive is the
      only route back to an hour of H100 work, so the inversion turns a
      recoverable timeout into a re-paid one.
    * orchestrator <= worker: the parent dies while its children are still
      running and still billing, and the job returns nothing for compute the
      wallet has already charged.

    Both were mutation-confirmed to pass the ENTIRE suite before this test
    existed, and one of them reached a commit in the session that wrote it.
    """
    t = _esmfold2_timeouts()
    assert t["subprocess"] < t["worker"], (
        f"subprocess budget {t['subprocess']} s >= worker timeout "
        f"{t['worker']} s: TimeoutExpired can never fire, so the raw archive "
        f"is never parked on a timeout."
    )
    assert t["orchestrator"] > t["worker"], (
        f"orchestrator timeout {t['orchestrator']} s <= worker timeout "
        f"{t['worker']} s: the parent dies before its own children."
    )
