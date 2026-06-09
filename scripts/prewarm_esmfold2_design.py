"""One-shot pre-warm for ``ranomics-esmfold2-design-prod``.

Spawns a minimal valid minibinder design call so the H100 container boots,
downloads the ESMFold2 + ESMC HuggingFace weights into the
``ranomics-esmfold2-models`` Volume, runs 150 gradient steps, then commits
the Volume. Subsequent prod calls find the weights cached and skip the
~30-40 GB download.

Usage:
    python scripts/prewarm_esmfold2_design.py

Cost: ~$1.50-3 of H100 time (15-30 min first call).
"""

from __future__ import annotations

import json
import sys

import modal


PAYLOAD = {
    "job_spec": {
        "preset": "minibinder",
        "target_name": "pd-l1",
        "target_sequence": None,
        "binder_name": "minibinder",
        "is_antibody": False,
        "seed": 0,
        "batch_size": 1,
        "use_scaling_critics": False,
        "target": {},
        "parameters": {},
    },
    "job_id": "prewarm-001",
    "job_tier": "minibinder",
    "tier": "prewarm",
    "input_presigned_url": "",
    "upload_urls_endpoint": "",
    "job_token": "",
    "webhook_url": "",
}


def main() -> int:
    fn = modal.Function.from_name(
        "ranomics-esmfold2-design-prod", "run_tool"
    )
    print("[prewarm] spawning run_tool with minibinder/pd-l1/seed=0/batch=1")
    call = fn.spawn(PAYLOAD)
    print(f"[prewarm] FunctionCall id: {call.object_id}")
    print(f"[prewarm] tail logs: modal app logs ranomics-esmfold2-design-prod")
    print("[prewarm] waiting for completion...")
    try:
        result = call.get()
    except Exception as exc:
        print(f"[prewarm] FunctionCall.get() raised: {exc}", file=sys.stderr)
        return 1
    print("[prewarm] returned:")
    print(json.dumps(result, indent=2, default=str)[:2000])
    return 0 if (result or {}).get("exit_code") == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
