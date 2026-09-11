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


class _ModuleReader:
    """Resolves ``timeout=`` expressions out of a Modal app's SOURCE.

    Read rather than imported, following ``tests/test_gpu_class_drift.py``,
    which reads ``_GPU`` out of these same files: importing a Modal app runs its
    decorators.

    Reading source means the guard can be lied to, and three rounds of review
    found ways to do it. The defence is that the set of names this trusts is
    DERIVED from the expression under test, not listed in advance:
    ``timeout=_WORKER_TIMEOUT_S`` makes ``_WORKER_TIMEOUT_S`` guarded, and so
    does every name it in turn resolves through. For each such name exactly one
    plain module-level ``NAME = <int expr>`` must exist. Anything else fails
    LOUDLY rather than resolving to a stale value:

    * a second module-level assignment, INCLUDING one this reader cannot
      evaluate -- ``_MAX_SESSION_S = 16200.0`` or
      ``= int(os.environ.get("X", "16200"))`` both used to be skipped as
      "not an int expression" and left the earlier value standing while the
      decorator, evaluated later, saw the new one;
    * ``+=``, an annotated assignment, a tuple target, a multi-target chain, a
      ``globals()[...]`` write, or any binding nested inside ``if``/``try``.

    Every one of those was demonstrated to move the real container timeout with
    the entire suite green.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self._plain: dict[str, list[ast.AST]] = {}
        self._unreadable: dict[str, int] = {}
        for node in self.tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                self._plain.setdefault(node.targets[0].id, []).append(node.value)
        for node in ast.walk(self.tree):
            for name, lineno in _unreadable_targets(node, self.tree):
                self._unreadable.setdefault(name, lineno)

    def resolve(self, expr: ast.AST) -> int:
        """Evaluate ``expr`` to an int, vetting every name it depends on."""
        for name in sorted(_names_in(expr)):
            self._vet(name)
        return self._eval(expr)

    def _vet(self, name: str, _seen: tuple[str, ...] = ()) -> None:
        assert name not in _seen, f"{self.path}: {name} is defined circularly"
        bindings = self._plain.get(name, [])
        assert bindings, (
            f"{self.path}: a timeout expression depends on {name}, which has no "
            f"plain module-level assignment this guard can read."
        )
        assert len(bindings) == 1, (
            f"{self.path}: {name} is assigned {len(bindings)} times at module "
            f"level. This guard reads source, so a later binding would leave it "
            f"reporting a value the container does not use."
        )
        assert name not in self._unreadable, (
            f"{self.path}:{self._unreadable[name]}: {name} is rebound in a form "
            f"this source reader cannot follow. Keep it as one plain "
            f"module-level assignment."
        )
        for inner in sorted(_names_in(bindings[0])):
            self._vet(inner, _seen + (name,))

    def _eval(self, node: ast.AST) -> int:
        """Arithmetic over vetted names.

        ``90 * 60`` and ``_MAX_SESSION_S + 15 * 60`` are both legitimate ways to
        write these, and ``ast.literal_eval`` rejects both. ``max``/``min`` are
        allowed because the subprocess budget is ``max(60, _MAX_SESSION_S - 30)``.
        Names resolve only from the vetted module bindings and the only callables
        recognised are those two builtins matched by name, so nothing here
        reaches into or executes the module under test.
        """
        if isinstance(node, ast.Constant):
            assert isinstance(node.value, int) and not isinstance(node.value, bool), (
                f"{self.path}: timeout literal {node.value!r} is not an int; a "
                f"float here would be read by this guard but is not what these "
                f"constants are declared as."
            )
            return node.value
        if isinstance(node, ast.Name):
            return self._eval(self._plain[node.id][0])
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self._eval(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = self._eval(node.left), self._eval(node.right)
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
            and node.args
        ):
            args = [self._eval(a) for a in node.args]
            return max(args) if node.func.id == "max" else min(args)
        raise AssertionError(
            f"{self.path}: timeout expression {ast.unparse(node)!r} is not "
            f"arithmetic this guard can evaluate. Keep these expressions simple "
            f"enough to read, or the guard silently stops guarding."
        )


def _names_in(expr: ast.AST) -> set[str]:
    """Every ``ast.Name`` load in ``expr``, excluding the max/min callees."""
    out = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            out.add(node.id)
    for node in ast.walk(expr):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("max", "min")
        ):
            out.discard(node.func.id)
    return out


def _unreadable_targets(node: ast.AST, tree: ast.Module) -> list[tuple[str, int]]:
    """Names ``node`` binds in a form :class:`_ModuleReader` cannot follow."""
    lineno = getattr(node, "lineno", 0)
    if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        if isinstance(node.target, ast.Name):
            return [(node.target.id, lineno)]
        return []
    if not isinstance(node, ast.Assign):
        return []
    found: list[tuple[str, int]] = []
    nested = node not in tree.body
    for target in node.targets:
        if isinstance(target, ast.Name):
            if nested or len(node.targets) > 1:
                found.append((target.id, lineno))
        elif isinstance(target, ast.Tuple):
            found += [(e.id, lineno) for e in target.elts if isinstance(e, ast.Name)]
        elif isinstance(target, ast.Subscript):
            key = target.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found.append((key.value, lineno))
    return found


def _gpu_container_timeout(path: Path) -> int:
    """The ``timeout=`` the GPU ``@app.function`` is actually handed.

    THIS, not ``_MAX_SESSION_S``, is what bounds the container. Guarding only the
    constant left ``@app.function(timeout=_MAX_SESSION_S * 3)`` passing the whole
    suite: a 3x under-hold, because the wallet floor still priced 5400 s while
    Modal allowed 16200. The constant is still required to AGREE with this, so
    that every comment and wallet value written against ``_MAX_SESSION_S`` stays
    true of the container.
    """
    reader = _ModuleReader(path)
    timeouts: dict[str, int] = {}
    for node in reader.tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            kwargs = {kw.arg: kw.value for kw in dec.keywords}
            assert None not in kwargs, (
                f"{path}: {node.name}'s decorator uses **kwargs, so this guard "
                f"cannot see what Modal is handed."
            )
            if "gpu" in kwargs and "timeout" in kwargs:
                timeouts[node.name] = reader.resolve(kwargs["timeout"])
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
    declared = reader.resolve(ast.Name(id="_MAX_SESSION_S", ctx=ast.Load()))
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
    reader = _ModuleReader(path)
    tree = reader.tree

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
                orchestrator = reader.resolve(kwargs["timeout"])
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
        "subprocess": reader.resolve(budgets[0]),
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
