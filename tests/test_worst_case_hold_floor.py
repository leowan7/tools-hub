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

from decimal import Decimal

import pytest

from shared import wallet_estimates as we

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
    worst = _max_billable("esmfold2-design", params, 3600.0, 1)
    assert worst == Decimal("14.7920")  # 3600 s * H100 rate * 1.70
    assert hold >= worst, f"esmfold2 1-seed under-held: {hold} < {worst}"
    assert hold <= we.compute_hard_cap("esmfold2-design", params)


@pytest.mark.parametrize("n_seeds", [2, 8, 64])
def test_esmfold2_multi_seed_hold_scales_with_seeds(low_p90, n_seeds):
    # The critical regression: one job-level hold covers n_seeds separate H100
    # containers, so a p90-shrunk multi-seed job needs a floor that scales. A flat
    # per-seed floor ($14.79) would cover only ONE seed and under-hold the rest.
    params = {"n_seeds": n_seeds, "preset": "pilot"}
    hold = we.cushioned_hold_usd(None, "esmfold2-design", params)
    worst = _max_billable("esmfold2-design", params, 3600.0, n_seeds)
    cap = we.compute_hard_cap("esmfold2-design", params)
    assert hold >= worst, f"esmfold2 {n_seeds}-seed under-held: {hold} < {worst}"
    assert hold <= cap
    # Sanity: the flat one-seed floor would NOT have covered this job.
    flat_one_seed = Decimal("14.7920")
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
    """With no history the tuned bootstrap already meets the worst case, so the
    floor is a no-op there — it must not LOWER any hold below its pre-floor value.
    proteina and esmfold2-design were tuned so the 1.5x cushion equals the
    container max at bootstrap."""
    monkeypatch.setattr(we, "_historical_p90_seconds", lambda slug: None)
    assert we.cushioned_hold_usd(None, "proteina", {"num_designs": 8}) == Decimal("15.0000")
    # esmfold2 bootstrap 2400 s/seed * 1.5 cushion == 3600 s container max.
    assert we.cushioned_hold_usd(None, "esmfold2-design", {"n_seeds": 1}) >= Decimal("14.7920")
    assert we.cushioned_hold_usd(None, "esmfold2-design", {"n_seeds": 8}) >= Decimal("118.3363")
