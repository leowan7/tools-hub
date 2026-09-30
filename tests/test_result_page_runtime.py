"""The result page's "Estimated runtime" comes from the same calculation as the form.

QA 2026-09-29 R4: job 25471e07 (rfdiffusion) showed "≈11.4 min for 8 designs"
and job ef3f5bd3 (rfantibody) "≈7.6 min for 4 designs", the minutes frozen into
``inputs._preflight`` at submit, before #323 and #347 corrected the envelopes.
"""

from __future__ import annotations

import re
import uuid

import pytest

from shared.jobs import ToolJob
from shared.pdb_intake import job_preflight_for_display
from shared.pdb_preflight import _check_size_envelope
from shared.pdb_preflight_rules import TOOL_RULES
from tests.test_failed_run_refund_copy import _page, client  # noqa: F401

pytestmark = pytest.mark.usefixtures("isolate_supabase")

# (tool, target aa, designs, minutes the stored snapshot carried)
_QA_JOBS = [
    ("rfdiffusion", 115, 8, 11.4),
    ("rfantibody", 115, 4, 7.6),
]


def _form_minutes(slug: str, target_aa: int, num_designs: int) -> float:
    """The minutes the form panel prints, rounded as _verdict_to_json rounds them."""
    env = _check_size_envelope(
        TOOL_RULES[slug], target_aa, binder_max_aa=None, num_designs=num_designs,
    )
    return round(env.runtime_estimate_min, 1)


def _inputs(slug: str, target_aa: int, num_designs: int, stale: float) -> dict:
    return {
        "num_designs": num_designs,
        "_preflight": {
            "kind": "ready", "ok": True, "tool_slug": slug, "target_chain": "A",
            "hotspots": {"surviving": []}, "cleanup_items": [],
            "size_envelope": {
                "residue_count": target_aa,
                "hard_cap_target_aa": 500, "soft_warn_target_aa": 300,
                "hard_cap_combined_aa": 600, "gpu": "A100-40GB",
                "runtime_estimate_min": stale,
                "runtime_basis": f"{num_designs} designs",
            },
        },
    }


@pytest.mark.parametrize("slug", sorted(TOOL_RULES))
@pytest.mark.parametrize("target_aa,num_designs", [(115, 4), (115, 8), (412, 4), (250, 20)])
def test_display_minutes_equal_the_form_panel_for_every_tool(slug, target_aa, num_designs):
    shown = job_preflight_for_display(_inputs(slug, target_aa, num_designs, stale=1.0))
    assert shown["size_envelope"]["runtime_estimate_min"] == _form_minutes(
        slug, target_aa, num_designs,
    )


@pytest.mark.parametrize("slug,target_aa,num_designs,stale", _QA_JOBS)
def test_result_page_prints_the_current_estimate_not_the_submit_snapshot(
    client, slug, target_aa, num_designs, stale,  # noqa: F811
):
    job = ToolJob.from_row({
        "id": str(uuid.uuid4()), "user_id": "u-1", "tool": slug,
        "preset": "pilot", "status": "running",
        "inputs": _inputs(slug, target_aa, num_designs, stale),
        "result": None, "error": None, "modal_function_call_id": "fc-x",
        "job_token": "t" * 64, "gpu_seconds_used": 0,
        "created_at": "2026-09-04T12:00:00Z", "started_at": None,
        "completed_at": None,
    })
    text = _page(client, job)
    shown = re.search(r"Estimated runtime: ≈([\d.]+) min", text)
    assert shown, "runtime line missing from the result page"
    assert float(shown.group(1)) == _form_minutes(slug, target_aa, num_designs)
    assert float(shown.group(1)) != stale


def test_minutes_are_dropped_when_they_cannot_be_rederived():
    inputs = _inputs("rfdiffusion", 115, 8, stale=11.4)
    del inputs["num_designs"]
    assert job_preflight_for_display(inputs)["size_envelope"]["runtime_estimate_min"] is None


def test_no_preflight_passes_through():
    assert job_preflight_for_display({}) is None
    assert job_preflight_for_display(None) is None
