"""Can each live-charging tool's stored job reach the $0 stop?

The meter stops a run when what it owes passes the balance
(shared/jobs.py::meter_live_runs, ``debit["short"] > 0``). What it owes is
clamped to ``compute_hard_cap`` of the stored params (shared/wallet.py::live_due_usd),
and the start gate refuses a positive balance below ``live_start_usd``
(shared/wallet_guard.py, ``0 < pre.balance_usd < live_start_usd``). So a run
admitted at the start minimum hits the stop only when the cap clears that
minimum by more than one 60 s heartbeat's charge. Each case builds the job's inputs the way submit stores them
(the adapter's ``validate``; a proteina child as compute_campaigns._dispatch_chunk
rebuilds it) and runs the meter on a balance of start minimum plus one heartbeat.
"""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest

from shared.compute_campaigns import sanitize_shared_params
from shared.jobs import ToolJob, _tool_params
from shared.wallet import live_due_usd
from shared.wallet_estimates import compute_hard_cap, fanout_containers, gpu_class_for_job
from shared.wallet_guard import live_charging_enabled, live_start_usd
from tests.test_live_charging import _Jobs, _Wallet, _job_row, _meter

SEQ = "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEK"
HEAVY = (
    "EVQLVESGGGLVQPGGSLRLSCAASGFNIKDTYIHWVRQAPGKGLEWVARIYPTNGYTRYADSVKGRF"
    "TISADTSKNTAYLQMNSLRAEDTAVYYCSRWGGDGFYAMDYWGQGTLVTVSS"
)
PDB_FORM = {"preset": "pilot", "target_chain": "A", "hotspot_residues": "A10"}
IGGM_FORM = {"preset": "complex_prediction", "fasta": f">H\n{HEAVY}",
             "target_chain": "A", "epitope": "7 8 9"}
ESM2_FORM = {"preset": "minibinder", "target_name": "pd-l1"}
# These four store their design count under ``parameters`` (#451); 16 records
# puts the cap the meter reads above the one-design base cap.
NESTED = 16
FASTA = "\n".join(f">s{i}\n{SEQ}" for i in range(NESTED))

_CAP_IS_START = "the cap equals the start minimum, so a run admitted at it never owes more than it had"

CASES = [
    ("af2", {"preset": "batch", "sequences": FASTA}),
    ("colabfold", {"preset": "batch", "sequences": FASTA}),
    ("esmfold", {"preset": "batch", "sequences": FASTA}),
    ("boltz2", {"preset": "msa_server", "target_chain": "A",
                "binder_sequences": "\n".join(["QVQLVESGGGLVQPGGSLRLSCAAS"] * NESTED)}),
    ("rfdiffusion", PDB_FORM),
    ("rfantibody", PDB_FORM),
    ("bindcraft", PDB_FORM),
    ("pxdesign", PDB_FORM),
    ("proteina", {"preset": "protein_binder"}),
    pytest.param("iggm", IGGM_FORM, id="iggm-1-sample",
                 marks=pytest.mark.xfail(strict=True, reason=f"iggm: {_CAP_IS_START}")),
    pytest.param("iggm", {**IGGM_FORM, "num_samples": "2"}, id="iggm-2-samples"),
    pytest.param("esmfold2-design", ESM2_FORM, id="esmfold2-design-1-seed",
                 marks=pytest.mark.xfail(strict=True, reason=f"esmfold2-design: {_CAP_IS_START}")),
    pytest.param("esmfold2-design", {**ESM2_FORM, "n_seeds": "2"}, id="esmfold2-design-2-seeds"),
]


def _stored_inputs(slug: str, form: dict) -> dict:
    adapter = importlib.import_module(f"tools.{slug.replace('-', '_')}")
    inputs, err = adapter.validate(form, {})
    assert err is None, err
    if slug == "proteina":
        # A live proteina run is a campaign child: shared params plus one shard.
        inputs = {**sanitize_shared_params(slug, inputs), "num_designs": 8,
                  "preset": form["preset"]}
    return inputs


@pytest.mark.parametrize(("slug", "form"), CASES)
def test_stored_job_can_reach_the_zero_stop(slug, form):
    inputs = _stored_inputs(slug, form)
    row = _job_row(tool=slug)
    row["inputs"] = {**inputs, **row["inputs"]}
    params = _tool_params(ToolJob.from_row(row))
    assert live_charging_enabled(slug, params)

    gpu = gpu_class_for_job(slug, None)
    heartbeat = live_due_usd(slug, 60 * fanout_containers(slug, params), gpu, params)
    start = live_start_usd(slug, params)
    balance = start + heartbeat
    assert compute_hard_cap(slug, params) > balance
    if slug in ("af2", "colabfold", "esmfold", "boltz2"):
        assert compute_hard_cap(slug, params) == compute_hard_cap(slug, {"n_designs_total": NESTED})

    store, wallet = _Jobs(row), _Wallet(balance)
    run = _meter(store, 10**6, wallet=wallet)
    assert run.summary["stopped"] == 1
    assert wallet.balance == Decimal("0")
