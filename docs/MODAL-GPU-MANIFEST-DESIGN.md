# Modal GPU manifest sync: design (Item 5 of PR #17 follow-ups)

Status: draft for review. Not yet implemented.

## Problem

`shared/pdb_preflight_rules.TOOL_RULES[slug].gpu` is a hardcoded string
shown on the preflight panel size envelope row. If
llm-proteinDesigner redeploys a Modal app to a different GPU SKU,
the hardcoded value goes stale silently and the preflight panel
displays the wrong hardware.

Blast radius is informational only. The label does not gate billing
or routing logic. Week 2 calibration verified all four GPU labels
(rfantibody, rfdiffusion, bindcraft on A100 80GB; boltzgen on
A100 40GB) against actual Modal logs, so as of PR #17 the labels
are correct. The goal is making future drift loud instead of silent.

The hook is already in place: `shared/modal_gpu_metadata.py` has
`fetch_modal_gpu_for_tool(slug)` returning `None` (no live source
configured) and `sync_tool_rules_gpu_labels()` called at app startup
with try / except so a Modal outage cannot stop tools hub from
booting. This document picks one of the three candidate paths the
hook module already enumerates.

## Candidate paths

The `shared/modal_gpu_metadata.py` module docstring lists three:

1. Direct Modal API query at runtime.
2. Vendored `gpu_manifest.json` written by llm-proteinDesigner at
   deploy time, pinned via the contracts SHA256 lock.
3. Periodic ops sync via the Modal CLI into a Supabase config table.

### Path 1: live Modal API query

Blocked on Modal SDK. The `modal.Function.from_name` object returned
client side is a lazy stub: it does not expose the deployed GPU
spec, only the function handle for invocation. Adding this requires
either an SDK upgrade that surfaces the spec or a server side
introspection endpoint Modal does not currently provide.

Rejected for now. Revisit only if the Modal SDK adds GPU spec
exposure.

### Path 2: vendored `contracts/gpu_manifest.json`

llm-proteinDesigner is where the GPU class is actually configured
(in the Modal app definition). The two repos already vendor a
`contracts/` directory with a SHA256 lock for cross repo schema
agreement (the `contracts-drift.yml` workflow on both sides catches
divergence at PR time). Adding `gpu_manifest.json` to that contract
slots into the existing pattern.

Properties:
- Zero runtime Modal SDK dependency on tools hub.
- Drift caught at PR time on llm-proteinDesigner because the
  contracts SHA changes break tools hub's contract drift check.
- tools hub side scaffolding (`sync_tool_rules_gpu_labels`,
  `fetch_modal_gpu_for_tool`) is already in place; only
  `fetch_modal_gpu_for_tool` body needs the read.
- Manifest is plain JSON, easy to inspect, no live system call.

Recommended.

### Path 3: periodic ops sync via Modal CLI into Supabase

Adds ops surface (a cron, a Supabase config table) for what is
informational only. The cron failure mode is silent staleness, the
exact problem we are trying to solve. Rejected.

## Recommended design: vendored manifest

### Schema

`contracts/gpu_manifest.json` shape:

```json
{
  "schema_version": 1,
  "generated_at": "2026-06-09T00:00:00Z",
  "tools": {
    "rfantibody": {"gpu_class": "A100-80GB", "modal_app": "ranomics-rfantibody-prod"},
    "rfdiffusion": {"gpu_class": "A100-80GB", "modal_app": "ranomics-rfdiffusion-prod"},
    "bindcraft": {"gpu_class": "A100-80GB", "modal_app": "ranomics-bindcraft-prod"},
    "boltzgen": {"gpu_class": "A100-40GB", "modal_app": "ranomics-boltzgen-prod"},
    "boltz2": {"gpu_class": "A100-40GB", "modal_app": "ranomics-boltz2-prod"}
  }
}
```

The `modal_app` field is informational only (mirrors what
`gpu/modal_client.modal_app_name` derives). The load bearing field
is `gpu_class`.

Atomic CPU tools (developability, library planner) are absent from
the manifest by design: they have no GPU class to drift.

### llm-proteinDesigner side (PR A)

1. Add `scripts/emit_gpu_manifest.py`. Imports each composite tool's
   Modal app definition, reads the `gpu` parameter off the
   `@app.function(...)` decorator (or equivalent), writes the JSON
   to `contracts/gpu_manifest.json` sorted by tool slug for stable
   diffs.
2. Wire the script into `deploy-modal.yml` (the GitHub Actions
   workflow that auto deploys on push to master). Run after deploy,
   commit the manifest if changed (commit author is the workflow
   bot; no human in the loop unless the manifest actually changed).
3. Add `gpu_manifest.json` to `CONTRACTS_SHA256.lock` and to the
   `contracts-drift.yml` check on both repos so a stale manifest
   blocks merge.

### tools hub side (PR B, lands second)

1. `shared/modal_gpu_metadata.fetch_modal_gpu_for_tool(slug)`
   implementation: read `contracts/gpu_manifest.json` at import
   time, look up the slug. Return `None` if the file is missing
   or the slug is absent (defensive: the manifest must never
   crash tools hub).
2. Manifest read is memoized at module import (no per call disk hit).
3. Add a unit test asserting every slug present in `TOOL_RULES`
   with a `gpu` field is also present in the manifest with the
   same `gpu_class`. This is the loud divergence guard.
4. Update `shared/modal_gpu_metadata.sync_tool_rules_gpu_labels`
   to actually populate an `OBSERVED_GPU` overlay dict that the
   preflight panel checks before falling back to `TOOL_RULES`. The
   `ToolRules` dataclass stays frozen; the overlay is a separate
   module level dict keyed by slug.
5. Preflight panel renderer (`shared/pdb_preflight.py` or
   wherever the size envelope row is built; verify at implement
   time) consults `OBSERVED_GPU[slug]` first, falls back to
   `TOOL_RULES[slug].gpu`.

### Ordering

PR A (llm-proteinDesigner) must land first. PR B (tools hub) is
inert until the manifest exists in `contracts/`. The unit test in
PR B will fail until PR A lands; PR B opens as draft until PR A
merges and the contract SHA updates.

## Open questions

1. Should `OBSERVED_GPU` overlay log on every divergence at module
   import, or only at preflight panel render time? Import time
   matches the existing `sync_tool_rules_gpu_labels` INFO log
   pattern and is cheaper.
2. Should the manifest include all atomic CPU tools as
   `{"gpu_class": null}` for completeness, or omit them entirely?
   Omitting is simpler and clearly signals "no GPU drift to
   track here".
3. Does llm-proteinDesigner's Modal app definition expose the GPU
   parameter as a string literal that a static AST walk can read,
   or is it computed? If computed, the deploy script needs to
   actually instantiate the app object and inspect the resolved
   config. Decision happens during PR A scoping.

## Risks

- Manifest emit script breaks at deploy time. Mitigation: PR A
  gates the manifest write behind try / except and logs a warning
  rather than failing the deploy. A stale manifest is the same
  failure mode as no manifest, which is the same failure mode as
  today.
- Manifest read in tools hub at import time slows startup if the
  file grows. Mitigation: the file is tiny (5 to 10 entries) and
  read once at module import.
- `contracts-drift.yml` blocks emergency hotfixes on tools hub if
  a manifest change is in flight. Mitigation: same as for the
  existing contracts; the drift check is fast and the manifest is
  small enough to hand edit if the deploy script is down.

## Out of scope

- Atomic CPU tool labeling. They have no GPU class.
- Modal CPU spec (cores, memory). Same drift class but never
  exposed to the user; live with hardcoded.
- Runtime cost rate sync (`gpu_usd_per_second`). Different problem
  (pricing not hardware label). Tackle separately if it ever
  diverges.

## Acceptance

PR A merges, manifest lands in `contracts/`, contracts SHA updates
on both sides. PR B merges, unit test passes (`TOOL_RULES.gpu`
matches `gpu_manifest.json` for every slug), preflight panel reads
from `OBSERVED_GPU` overlay first. A deliberate test of drift:
change one `gpu_class` in the manifest only, observe the WARNING
log fires at startup and the preflight panel shows the manifest
value.
