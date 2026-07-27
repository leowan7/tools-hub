"""Regression tests for ESMFold2-design multi-seed billing.

Each seed of a multi-seed run executes in its OWN Modal container
(``_run_one_seed.spawn`` per seed), so the GPU time the account is billed
for is the SUM of the children, not the max. The umbrella's
``runtime_seconds`` feeds ``gpu_seconds_used`` at the billing seam
(``gpu.modal_client._interpret_pipeline_return``), so if ``_aggregate``
collapses the children with ``max`` the customer is under-charged by up to
Nx (a 64-seed run settled as one container).

These tests pin:
  1. ``_aggregate`` returns ``runtime_seconds == sum`` of child runtimes.
  2. It also exposes ``wall_clock_seconds == max`` for honest UI display.
  3. The summed runtime survives the billing seam as ``gpu_seconds_used``.

Runs fully offline - no Modal, no GPU (``_aggregate`` and the interpreter
are pure functions; ``import modal`` succeeds in the venv but nothing here
touches a live app).
"""

from __future__ import annotations

from gpu.modal_client import _interpret_pipeline_return
from tools.esmfold2_design.modal_app import _aggregate


def _child(seed: int, runtime: int, *, n_designs: int = 1) -> dict:
    """A minimal successful child return in ``run_tool`` shape."""
    candidates = [
        {"sequence": f"SEQ{seed}", "scores": {"ipTM": 0.5 + 0.01 * seed}}
        for _ in range(n_designs)
    ]
    return {
        "exit_code": 0,
        "provider_job_id": f"child-{seed}",
        "raw_tgz_volume_path": f"/raw/seed{seed}.tgz",
        "smoke_result": {
            "status": "COMPLETED",
            "tier": "design",
            "preset": "default",
            "designs_total": n_designs,
            "designs_completed": n_designs,
            "n_failures": 0,
            "designs": [{"iptm": 0.5} for _ in range(n_designs)],
            "candidates": candidates,
            "runtime_seconds": runtime,
        },
    }


class TestMultiSeedBilling:
    def test_runtime_is_sum_not_max(self):
        runtimes = [600, 720, 540]
        successes = [(s, _child(s, rt)) for s, rt in enumerate(runtimes)]
        out = _aggregate(successes, [], {"tier": "design", "job_id": "umb-1"})
        smoke = out["smoke_result"]
        assert smoke["runtime_seconds"] == sum(runtimes) == 1860
        # Not the old max=720 that under-charged the run.
        assert smoke["runtime_seconds"] != max(runtimes)

    def test_wall_clock_is_max_for_parallel_display(self):
        runtimes = [600, 720, 540]
        successes = [(s, _child(s, rt)) for s, rt in enumerate(runtimes)]
        out = _aggregate(successes, [], {"tier": "design", "job_id": "umb-1"})
        assert out["smoke_result"]["wall_clock_seconds"] == max(runtimes) == 720

    def test_summed_runtime_survives_billing_seam(self):
        runtimes = [600, 720, 540]
        successes = [(s, _child(s, rt)) for s, rt in enumerate(runtimes)]
        out = _aggregate(successes, [], {"tier": "design", "job_id": "umb-1"})
        interpreted = _interpret_pipeline_return(out)
        assert interpreted["status"] == "succeeded"
        # The wallet is charged on gpu_seconds_used; it MUST be the sum.
        assert interpreted["gpu_seconds_used"] == sum(runtimes) == 1860
        # The honest-UI value must also survive the seam into the stored
        # result the template reads (esmfold2_design_results.html reads
        # output.get('wall_clock_seconds')). This guards against a future
        # tightening of the flat-unwrap in _interpret_pipeline_return
        # silently dropping the Wall-clock tile.
        assert interpreted["result"]["wall_clock_seconds"] == max(runtimes) == 720
        assert interpreted["result"]["runtime_seconds"] == sum(runtimes)

    def test_64_seed_umbrella_bills_full_gpu_time(self):
        # The concrete regression: 64 seeds x ~600s each is ~10.6 GPU-hours,
        # not ~600s. Old max-collapse billed ~1/64 of the real cost.
        per_seed = 600
        successes = [(s, _child(s, per_seed)) for s in range(64)]
        out = _aggregate(successes, [], {"tier": "design", "job_id": "umb-64"})
        interpreted = _interpret_pipeline_return(out)
        assert interpreted["gpu_seconds_used"] == 64 * per_seed == 38400

    def test_failed_seeds_do_not_inflate_runtime(self):
        # Only succeeded children contribute GPU time; failures carry none.
        successes = [(0, _child(0, 600)), (1, _child(1, 500))]
        failures = [(2, "OOM"), (3, "preempted")]
        out = _aggregate(successes, failures, {"tier": "design", "job_id": "u"})
        smoke = out["smoke_result"]
        assert smoke["runtime_seconds"] == 1100
        assert smoke["wall_clock_seconds"] == 600
        assert smoke["n_seeds"] == 4
        assert smoke["seeds_succeeded"] == 2
        assert smoke["seeds_failed"] == 2


class TestSingleSeedUnaffected:
    def test_lone_success_aggregate_is_a_noop(self):
        # NOTE: this does NOT exercise the production single-seed route. The
        # real n_seeds == 1 path returns _run_one_seed.remote(cp) directly and
        # bypasses _aggregate entirely (modal_app.py run_tool), and that route
        # uses Modal .remote()/.spawn() so it can't run offline. What this pins
        # is that _aggregate over a single success is a no-op: sum == the one
        # child's runtime, so a lone seed is never mis-billed if it were ever
        # routed through aggregation.
        successes = [(0, _child(0, 900))]
        out = _aggregate(successes, [], {"tier": "design", "job_id": "u"})
        smoke = out["smoke_result"]
        assert smoke["runtime_seconds"] == 900
        assert smoke["wall_clock_seconds"] == 900
        assert _interpret_pipeline_return(out)["gpu_seconds_used"] == 900
