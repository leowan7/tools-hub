"""A stored job's design count scales its hard cap (QA 2026-10-22, job 61e14125).

boltz2 stores ``n_designs_total`` under ``parameters``; the cap lookup read it
only at the top level, so a 16-binder live run was capped (and billed) at the
one-design $0.40.
"""

from decimal import Decimal
from types import SimpleNamespace

from shared.jobs import _tool_params
from shared.wallet import live_due_usd
from shared.wallet_estimates import TOOL_SPECS, compute_hard_cap
from tools import boltz2 as b2


def test_boltz2_stored_job_caps_at_its_design_count():
    seq = "QVQLVESGGGLVQPGGSLRLSCAAS"
    inputs, err = b2.validate(
        {"preset": "msa_server", "target_chain": "A",
         "binder_sequences": "\n".join([seq] * 16)},
        {},
    )
    assert err is None
    job = SimpleNamespace(inputs={**inputs, "_wallet": {"live": True}, "_progress": {}})
    params = _tool_params(job)  # what meter_live_runs passes

    base = TOOL_SPECS["boltz2"].base_hard_cap_usd
    assert compute_hard_cap("boltz2", params) == base * 16
    # The submit-time hold reads the form's flat count; both must agree.
    assert compute_hard_cap("boltz2", {"n_designs_total": 16}) == base * 16
    # 1709 GPU-s on A100-80GB (the QA run) owes more than the old $0.40 cap.
    assert live_due_usd("boltz2", 1709, "A100-80GB", params) > Decimal("2.00")
