"""GPU canary for the fixed-positions path. Run, do not deploy.

    modal run tools/mpnn/_canary_fixed_positions.py

Exists because the invocation ``modal_app.run_tool``'s own docstring advertises
-- ``modal run ...::run_tool --payload '{...}'`` -- is refused by modal client
1.4.2 with "Parameter `payload` has unparseable annotation: Any". The ``Any``
was chosen to keep that CLI path alive; the CLI has since moved. Calling
``.remote()`` from a local entrypoint sidesteps CLI annotation parsing
entirely and exercises the identical function, image and payload contract.

WHAT THIS PROVES, none of which the 190 unit tests can: they all monkeypatch
run_mpnn, so every one would still pass if our model of upstream were wrong.

  1. Upstream ProteinMPNN ACCEPTS the fixed_positions.jsonl we write -- the
     filename-stem key, the 1-indexing, the per-designed-chain entries.
  2. Semantics are not inverted: the positions we list come back NATIVE, and
     the ones we left free are the ones that moved.
  3. Our chain-length count agrees with upstream's, i.e. verify_fixed_positions
     finds no segment-length disagreement on a real parse.

Target is 1HEW chain A, 129 residues, contiguous and numbered from 1. Freezing
1-89 leaves exactly 40 free -- deliberately ON the
MIN_FREE_FOR_WHOLE_SEQUENCE_DIVERSITY boundary, so every stub guard is active
and the observed Hamming spread is a real measurement of the one threshold that
was reasoned rather than measured.
"""

import json

from tools.mpnn.modal_app import app, run_tool

TARGET_URL = (
    "https://raw.githubusercontent.com/leowan7/tools-hub/main/"
    "static/example/1HEW.pdb"
)
FROZEN = list(range(1, 90))          # 89 positions held fixed
CHAIN_LEN = 129                      # 40 free


@app.local_entrypoint()
def main():
    payload = {
        "tier": "standalone",
        "input_presigned_url": TARGET_URL,
        "job_token": "canary-fixed-positions",
        "job_spec": {
            "target_chain": "A",
            "parameters": {
                "num_seq_per_target": 8,
                "sampling_temp": 0.1,
                "fixed_positions": {"A": FROZEN},
            },
        },
    }
    print(f"[canary] {len(FROZEN)} frozen, {CHAIN_LEN - len(FROZEN)} free")
    result = run_tool.remote(payload)
    print("[canary] RAW RESULT:")
    print(json.dumps(result, indent=2, default=str)[:4000])
