# Session Handoff — 2026-05-27 PM

Continues from [SESSION-HANDOFF-2026-05-27.md](SESSION-HANDOFF-2026-05-27.md). The AM handoff queued live verification of pilot `7cf73ff6` end-to-end; this session executed that verification, exposed a second bug under the surface, fixed it, and validated PASS on a fresh pilot. Three pilot-tier runs total (~$1.50 net wallet spend across all three).

---

## Status at session end

- **Pilot-tier transport closed end-to-end on caller PDB.** Validated PASS via job `816fc4a9-cce2-4366-9ada-7ed583828105` (PXDesign pilot, 504 GPU-s, Storage populated with design_001.cif + design_002.cif, resolver returns 200 + 138271 bytes, UI renders 3D + .pdb buttons, banner gone).
- **llm-proteinDesigner master:** new commit `64aad13` (pxdesign webhook tier fix). Local only — not pushed.
- **tools-hub refactor/repo-separation:** new commit `141c7d4` (VALIDATION-LOG PASS row). Local only — not pushed. Parallel session is actively committing on this branch; coordinate push order.
- **Modal apps:** both pxdesign apps carry the fix: `kendrew-pxdesign-prod` v21 (where prod tools-hub at `f36837b` routes today) AND `ranomics-pxdesign-prod` v2 (where tools-hub HEAD will route once the parallel rename merges). Deployed back-to-back so the in-flight rename can land without losing the patch.
- **Wallet:** clean. 7cf73ff6 hold (tx 86 −$4.37) + hold_release (tx 87 +$3.85, parent_tx_id=86, gpu_seconds=300) verified. Same shape expected on the d6a6cde5 and 816fc4a9 holds — not audited individually but the settle hook is the same code path.

---

## What shipped

### llm-proteinDesigner commit on `master`

| Commit | Scope |
|---|---|
| `64aad13` | fix(pxdesign): resolve passing-design local path in webhook tier |

Two changes in `docker/pxdesign/run_pipeline.py` (+84 / −13):

1. New `resolve_design_local_path` helper (8-layer fallback ladder) called from `run_webhook_tier` instead of the previous 3-layer inline ladder. Layers, in order: chosen_struct_path absolute → cwd-relative → output_dir-relative → summary_csv_dir-relative → rglob basename under output_dir → design_files direct → design_files stem → design_files substring → `spec_sample_{rank_idx}` → sorted spec_sample[rank_idx]. Returns `(path, source_tag)` for log triage.
2. Content-Type `text/csv` → `text/plain` for CSV PUTs in `upload_output`. The `tool-outputs` bucket's `allowed_mime_types` (migration 0021) accepts `text/plain` but NOT `text/csv`, so metrics.csv PUTs were 400ing. Bytes are identical; downstream doesn't dispatch on MIME.

### tools-hub commit on `refactor/repo-separation`

| Commit | Scope |
|---|---|
| `141c7d4` | docs(validation): pxdesign pilot PASS row for 816fc4a9 (transport closed) |

One row appended at the top of the PXDesign section in `docs/VALIDATION-LOG.md`. Supersedes the 2026-05-26 FLAG on `79228f03`.

### Modal app redeploys (all `--env main`)

| App | Version | Notes |
|---|---|---|
| ranomics-pxdesign-prod | v1 | 11:38 EDT — accidental first deploy (mystery, file said kendrew-*; deployed to ranomics-* anyway). |
| kendrew-pxdesign-prod | v21 | 11:56 EDT — explicit redeploy from main repo, this one landed correctly. Currently serves all prod pxdesign traffic at tools-hub HEAD f36837b. |
| ranomics-pxdesign-prod | v2 | 12:07 EDT — mirror deploy via temporary `app = modal.App("ranomics-...")` edit + revert in pxdesign_app.py. |

Modal builder cache held — all deploys 15–17 s wallclock.

---

## Architecture, end-to-end (delta)

The pilot-tier transport that the AM handoff closed at the framework level was actually still no-op'd inside pxdesign's webhook tier due to a CSV-format quirk. With `64aad13`, the full path now works:

```
Pipeline run → pxdesign emits summary.csv (preview preset, NO design_name column)
   ↓ parse_summary_csv synthesizes "design_N" labels (len(results) fallback)
   ↓ resolve_design_local_path: synthetic "design_N" doesn't match design_files
     (keyed "spec_sample_*"), falls through to spec_sample_{rank_idx} layer → HIT
   ↓ local_file resolved → request_upload_urls (one URL per filename) → upload PUT
   ↓ Storage object lands at tool-outputs/{user}/{job}/designs/design_NNN.cif
   ↓ webhook posts result back; pdb_content_b64 ALSO populated (belt-and-braces)
Browser GET /api/jobs/<id>/pdb/designs/design_001.cif
   ↓ resolver Storage hit → download_output → 200 + bytes
```

The 79228f03 audit row (in VALIDATION-LOG) noted "Audit across 9 successful pilot-tier jobs (all 5 tools): only 1 (boltzgen c6e25830) has b64 populated; the rest produce blank 3D/PDB cells." That's a SYSTEMIC pilot-tier gap. **`64aad13` only closes pxdesign.** The other 4 tools (bindcraft, boltzgen, rfantibody, rfdiffusion) have their own per-pipeline local_path resolution code that hasn't been audited under the new transport. Their next pilot runs may or may not populate Storage — needs per-tool validation.

---

## Live verification artifacts

- **7cf73ff6** (AM-handoff job; PRE-fix repro): status=succeeded, gpu_seconds=300, ipTM=0.83, pdb_key=designs/design_001.pdb, b64=False, Storage empty. Confirmed bug.
- **d6a6cde5** (my first re-submit on v20): status=succeeded, gpu_seconds=468, ipTM=0.82, same shape. Confirmed bug under v20 / no-fix.
- **816fc4a9** (validation on v21 / WITH fix): status=succeeded, gpu_seconds=504, ipTM=0.42/0.09 (below threshold — design quality, not pipeline), pdb_key=.cif, b64=True, Storage populated. **PASS.**

Click-through verify: GET `/api/jobs/816fc4a9-.../pdb/designs/design_001.cif` returned 200, Content-Type `chemical/x-pdb`, 138271 bytes, first line "# By using this file you agree to the legally binding terms of use found at https://protenix-server.com/terms-of-service" (typical PXDesign output header).

Polls and audits scaffolded in `tools-hub/scratch/` — `poll-pxdesign-<jobid>.py`, `check-pxdesign-7cf73ff6.py`, `check-wallet-7cf73ff6.py`. Reusable for future pilot verification (all run via `railway run --service web --environment production --`).

---

## Open work — next session

### 1. Push coordination (PRIMARY)

- **tools-hub `141c7d4`** is on `refactor/repo-separation`, which the parallel naming/dependency session is actively committing to. Don't push without checking in with them — they may want to squash or re-order.
- **llm-proteinDesigner `64aad13`** is on `master`. Parallel session added `99c0328` on top during my work block — additive, no conflict. Should push so the deployed pxdesign app stays consistent with what's recorded.

### 2. Audit b64/Storage gap on the other 4 composite tools

For bindcraft, boltzgen, rfantibody, rfdiffusion: each `run_pipeline.py` has its own local_path resolution + upload + b64 emission. Need a pilot-tier verification run per tool to confirm Storage gets populated. The 2026-05-26 audit found only boltzgen had b64; rest had blank cells. With the new transport, the question shifts: does Storage get bytes?

- bindcraft / rfantibody have prior pilot attestations from `a0dbcf1` / `eea8f143` — those predate the upload_urls_endpoint architecture, so they emitted inline b64. Should still work via the resolver's b64 fallback. New pilot runs would prove Storage path too.
- boltzgen pipeline uses `design_file` directly (likely deterministic path) — probably works without the fallback ladder.
- rfdiffusion uses `backbone_pdb` directly — similar.
- pxdesign needed the fix because its summary.csv format is the outlier.

Recommended: one pilot-tier validation run per tool, each on the canonical fixture (`a0dbcf1` notes use 4Z18 for bindcraft; rfantibody used antibody fixture; etc). 4× $4.37 hold = ~$17 budget worst-case, ~$2 actual.

### 3. Carry-over from yesterday's PM handoff (unchanged)

- Phase 2 item 7 — signup hCaptcha (deferred, no bot signal).
- Phase 2 item 8 — failed-job page UX (wait for a real failure).
- Phase 3 item 10 — Workspace product fate (strategic decision).
- Phase 3 item 12 — Scope-C test gaps (multi-day, batch separately).
- Phase 3 item 13 — pricing transparency (blocked on conversion data).
- PXDesign mini_pilot 2× re-validation — still hidden in `tools/pxdesign/__init__.py`.
- Drop dead `tool_jobs.credits_cost` column via follow-up migration. No urgency.

### 4. Shared upload helper in llm-proteinDesigner

The five `run_pipeline.py` files each carry their own `request_upload_urls` + `upload_output`. With my Content-Type fix on pxdesign, there's now a divergence in copy 1 of 5. Centralising into `docker/_shared/upload_client.py` would fix the divergence and prevent future drift. Low priority but the divergence makes it slightly more urgent than yesterday.

---

## Key facts / gotchas (delta from AM handoff)

- **PXDesign "preview" preset summary.csv has no design_name column.** parse_summary_csv synthesizes "design_N" labels via `f"design_{len(results)}"`. These labels don't match the `spec_sample_*` keys in design_files. Any PXDesign pipeline code that resolves filenames via design_name lookup needs the fallback ladder.
- **Two pxdesign Modal apps coexist temporarily.** `kendrew-pxdesign-prod` (where prod tools-hub at origin/main routes today) AND `ranomics-pxdesign-prod` (where tools-hub HEAD will route once the parallel rename merges). Both have the fix as of v21 / v2 respectively. When the rename lands on prod, `kendrew-*` apps can be safely deleted, but only after confirming no other in-flight job is still routing there.
- **modal deploy + relative path mystery.** On my first deploy attempt today (11:38 EDT), the pxdesign_app.py file at `llm-proteinDesigner/infrastructure/modal/pxdesign_app.py` clearly said `app = modal.App(f"kendrew-{_TOOL}-prod")`, yet the deploy created `ranomics-pxdesign-prod` v1. Second deploy (11:56 EDT) with the same command landed on `kendrew-pxdesign-prod` v21 as expected. No explanation. Worth noting in case it recurs.
- **PXDesign pdb_key is `.cif`, not `.pdb`.** The webhook tier preserves the original extension via `Path(local_path).suffix`. The resolver still serves with Content-Type `chemical/x-pdb`. Mol* viewer sniffs format from content so this works. Other tools may emit different extensions.
- **Pilot tier cost confirmed** at ~$0.50 net charge for a ~5–8 min PXDesign pilot on PD-L1 4ZQK with num_designs=2. The $4.37 hold is sized for worst-case timeout. Three runs today ≈ $1.50 net.
- **Browser-agent file_upload sandbox** still rejects `/tmp/` and `~/.claude/uploads/` — the AM handoff's in-page `fetch('https://files.rcsb.org/download/4ZQK.pdb') + FormData` recipe held. Reused this session to drive d6a6cde5 + 816fc4a9 submits.

---

## Reference

- AM handoff: [SESSION-HANDOFF-2026-05-27.md](SESSION-HANDOFF-2026-05-27.md)
- Validation log entry: [VALIDATION-LOG.md](VALIDATION-LOG.md) — top of PXDesign section (`816fc4a9` row)
- Architectural rule memory: `project_tools_hub_output_transport.md`
- Live PASS pilot: `816fc4a9-cce2-4366-9ada-7ed583828105` (PXDesign pilot, submitted 2026-05-27T15:57:52Z, completed 2026-05-27T16:05:59Z)
- Pre-fix repros (this session): `7cf73ff6` (AM), `d6a6cde5` (PM, my first re-submit)
- Scratch tooling: `tools-hub/scratch/poll-pxdesign-*.py`, `check-pxdesign-*.py`, `check-wallet-*.py` — all run via `railway run --service web --environment production --`.
