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
# Names this file asserts against. Binding one in a form the source reader
# cannot follow is an error here, not a silent miss.
_GUARDED_NAMES = ("_MAX_SESSION_S", "_ORCHESTRATOR_TIMEOUT_S")

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


def _module_constants(path: Path) -> dict[str, int]:
    """Every module-level ``NAME = <int expression>`` in ``path``.

    Read rather than imported, following ``tests/test_gpu_class_drift.py``,
    which reads ``_GPU`` out of these same files: importing a Modal app runs its
    decorators.

    Any OTHER way of binding a guarded name is refused rather than ignored. A
    reader that only recognises ``ast.Assign`` treats ``_MAX_SESSION_S += 1800``,
    ``_MAX_SESSION_S: int = 10800``, a tuple target, a two-target chain, a
    ``globals()[...]`` write, and any binding nested inside ``if``/``try`` as
    INVISIBLE: the runtime value moves and the guard goes on reporting the old
    one. Each of those was demonstrated to keep the whole suite green.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    consts: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            value = _eval_int(node.value, consts)
        except ValueError:
            continue
        assert target.id not in _GUARDED_NAMES or target.id not in consts, (
            f"{path}: {target.id} is assigned twice at module level. This guard "
            f"reads source, so the second one makes it report a value the "
            f"container may not use."
        )
        consts[target.id] = value
    _refuse_unreadable_rebinds(tree, path)
    return consts


def _refuse_unreadable_rebinds(tree: ast.Module, path: Path) -> None:
    """Fail if a guarded name is bound in a form :func:`_module_constants` misses."""
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            if isinstance(node.target, ast.Name):
                names = [node.target.id]
        elif isinstance(node, ast.Assign):
            nested = node not in tree.body
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if nested or len(node.targets) > 1:
                        names.append(t.id)
                elif isinstance(t, ast.Tuple):
                    names += [e.id for e in t.elts if isinstance(e, ast.Name)]
                elif isinstance(t, ast.Subscript):
                    key = t.slice
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        names.append(key.value)
        clashes = sorted(set(names) & set(_GUARDED_NAMES))
        assert not clashes, (
            f"{path}:{getattr(node, 'lineno', '?')}: {clashes} rebound in a form "
            f"this source reader cannot follow. Keep these as one plain "
            f"module-level assignment, or this guard silently reads a stale value."
        )


def _eval_int(node: ast.AST, consts: dict[str, int]) -> int:
    """Evaluate an int expression over already-seen module constants.

    Arithmetic is allowed on purpose -- ``90 * 60`` and ``_MAX_SESSION_S + 15 * 60``
    are both legitimate ways to write these, and ``ast.literal_eval`` rejects
    both. ``max``/``min`` are allowed for the same reason: the subprocess budget
    is written ``max(60, _MAX_SESSION_S - 30)``.

    Names resolve ONLY out of ``consts`` and the only callables recognised are
    the two builtins matched by name, so nothing here reaches into the module
    under test or executes any of it.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_int(node.operand, consts)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _eval_int(node.left, consts)
        right = _eval_int(node.right, consts)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("max", "min")
        and not node.keywords
    ):
        args = [_eval_int(a, consts) for a in node.args]
        if args:
            return max(args) if node.func.id == "max" else min(args)
    raise ValueError("not an int expression")


def _gpu_container_timeout(path: Path) -> int:
    """The ``timeout=`` the GPU ``@app.function`` is actually handed.

    THIS, not ``_MAX_SESSION_S``, is what bounds the container. Guarding only the
    constant left ``@app.function(timeout=_MAX_SESSION_S * 3)`` passing the whole
    suite -- a 3x under-hold, because the wallet floor still priced 5400 s while
    Modal allowed 16200. The constant is read only to resolve this expression,
    and the two must agree.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    consts = _module_constants(path)
    timeouts: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            kwargs = {kw.arg: kw.value for kw in dec.keywords}
            if "gpu" in kwargs and "timeout" in kwargs:
                timeouts[node.name] = _eval_int(kwargs["timeout"], consts)
    assert timeouts, (
        f"{path}: no @app.function passing both gpu= and timeout=; this guard "
        f"is reading nothing."
    )
    distinct = set(timeouts.values())
    assert len(distinct) == 1, (
        f"{path}: GPU functions disagree on the session cap: {timeouts}. "
        f"worst_case_gpu_seconds can mirror only one."
    )
    seconds = distinct.pop()
    declared = consts.get("_MAX_SESSION_S")
    assert seconds == declared, (
        f"{path}: the GPU container is handed timeout={seconds} s while "
        f"_MAX_SESSION_S is {declared}. Every comment and every wallet constant "
        f"is written against _MAX_SESSION_S, so the decorator must pass it "
        f"unmodified."
    )
    return seconds


def _session_cap(slug: str) -> float:
    """Session cap for ``slug``, taken from its GPU container's own timeout."""
    return float(
        _gpu_container_timeout(
            _REPO_ROOT / "tools" / _SESSION_CAP_SOURCE[slug] / "modal_app.py"
        )
    )


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
    # Pinned to the post-floor value this docstring states, not to the pre-floor
    # $14.7920: a >= against the old number passes either way and would not
    # notice the floor ceasing to apply at all.
    assert we.cushioned_hold_usd(None, "esmfold2-design", {"n_seeds": 1}) == Decimal("15.0000")
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
    """``{name: seconds}`` for the three nested esmfold2-design timeouts.

    ``worker`` is the GPU container's own ``timeout=`` (see
    :func:`_gpu_container_timeout`), not the constant beside it. ``orchestrator``
    is the ``timeout=`` on the function that has no ``gpu=``. ``subprocess`` is
    read from INSIDE ``_run_one_seed`` only.

    That last scoping matters: an earlier version took whichever
    ``subprocess.run(timeout=...)`` ``ast.walk`` reached first, which is
    breadth-first rather than source order. A second, more deeply nested
    ``subprocess.run`` anywhere in the file silently became "the budget" and hid
    a real inversion.
    """
    path = _REPO_ROOT / "tools" / "esmfold2_design" / "modal_app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    consts = _module_constants(path)

    worker = _gpu_container_timeout(path)

    orchestrator = None
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            kwargs = {kw.arg: kw.value for kw in dec.keywords}
            if "gpu" not in kwargs and "timeout" in kwargs:
                assert orchestrator is None, (
                    f"{path}: more than one non-GPU @app.function passes a "
                    f"timeout; this guard cannot tell which is the orchestrator."
                )
                orchestrator = _eval_int(kwargs["timeout"], consts)
    assert orchestrator is not None, (
        f"{path}: no CPU-only @app.function with a timeout=; the orchestrator "
        f"bound this test claims to check is not being read."
    )

    seed_fn = next(
        (
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_run_one_seed"
        ),
        None,
    )
    assert seed_fn is not None, f"{path}: no _run_one_seed; guard is reading nothing."
    budgets = [
        kw.value
        for call in ast.walk(seed_fn)
        if isinstance(call, ast.Call) and "subprocess.run" in ast.unparse(call.func)
        for kw in call.keywords
        if kw.arg == "timeout"
    ]
    assert len(budgets) == 1, (
        f"{path}: expected exactly one subprocess.run(timeout=...) inside "
        f"_run_one_seed, found {len(budgets)}. With more than one this guard "
        f"cannot say which bounds the run."
    )
    return {
        "subprocess": _eval_int(budgets[0], consts),
        "worker": worker,
        "orchestrator": orchestrator,
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

    Both inversions were mutation-confirmed to pass the ENTIRE suite before this
    test existed, as were two mutations of the DECORATOR timeouts that this file
    now reads directly (``timeout=_MAX_SESSION_S * 3`` on the worker, and
    ``timeout=600`` on the orchestrator).

    What is pinned and what is not: the worker bound is pinned to equality with
    ``_MAX_SESSION_S`` inside :func:`_gpu_container_timeout`. The orchestrator is
    pinned only as an INEQUALITY -- a hardcoded ``_ORCHESTRATOR_TIMEOUT_S =
    100000`` passes here. Nothing asserts it is derived from the worker.
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
