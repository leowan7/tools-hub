"""P-1 staging-gate smoke for Proteina-Complexa (run AFTER seed_volumes.py).

    modal run tools/proteina/_validate_smoke.py

Calls the deployed ``ranomics-proteina-prod`` ``run_tool`` with a ``validate``
payload. That runs run_pipeline's free validate tier INSIDE the real image +
mounted Volumes: it imports ``proteinfoundation.generate`` / ``.filter`` (the
true test that the built AF2/JAX/tmol/RF3 env imports cleanly), asserts all
three variant configs are present, and asserts a model ``*.ckpt`` exists under
the weights mount (/opt/proteina/ckpts). No design compute, no webhook, no
upload — it writes /tmp/smoke_results.json which run_tool returns inline.

This is a GPU-attached container but the validate branch only does CPU import +
file checks (seconds of A100 time, ~$0.01), so it is the cheap "two green
smokes" gate before any priced design shard. Prints the returned smoke_result;
exit non-zero if status != COMPLETED so it can gate a canary sequence.
"""

from __future__ import annotations

import json
import sys

import modal

app = modal.App("ranomics-proteina-validate-smoke")


@app.local_entrypoint()
def main() -> None:
    run_tool = modal.Function.from_name("ranomics-proteina-prod", "run_tool")
    payload = {
        "tier": "validate",
        "job_tier": "validate",
        "job_id": "validate-smoke",
        "job_spec": {
            "preset": "validate",
            "config_name": "search_binder_local_pipeline",
            "task_name": "02_PDL1",
        },
        "webhook_url": "",
    }
    print("[validate-smoke] invoking run_tool(validate) ...", flush=True)
    result = run_tool.remote(payload)
    print("[validate-smoke] raw result:", flush=True)
    print(json.dumps(result, indent=2, default=str), flush=True)

    smoke = (result or {}).get("smoke_result") or {}
    status = smoke.get("status")
    print(f"[validate-smoke] status={status} validate_ok={smoke.get('validate_ok')}", flush=True)
    if status != "COMPLETED":
        print("[validate-smoke] FAILED — see error above", flush=True)
        sys.exit(1)
    print("[validate-smoke] PASS", flush=True)
