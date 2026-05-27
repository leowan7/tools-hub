# Session Handoff — 2026-05-27

Continues from [SESSION-HANDOFF-2026-05-26-pm.md](SESSION-HANDOFF-2026-05-26-pm.md). The PM handoff queued the four-step pilot-tier transport fix; this session executed it end-to-end across both repos, redeployed all five Modal apps, and submitted the first pilot job to exercise the new flow.

---

## Status at session end

- **Tools-hub:** 7 commits on `main`, all pushed. Supabase migration 0021 applied to prod (Studio SQL editor, "Success. No rows returned").
- **llm-proteinDesigner:** 1 commit on `master` (`ecfa2f9`) pushed.
- **Modal:** all 5 tool apps redeployed with the pdb_key alignment fix.
- **Test suite:** 656 passed, 6 skipped, 0 failed (was 628 at the start; +28 from the new endpoints/storage/resolver tests).
- **Live verification:** UI wiring confirmed on the existing pre-fix job `79228f03` (banner gone, buttons render, endpoint reachable, graceful fallback). End-to-end verification pending on the live pilot `7cf73ff6` (submitted 12:50, ETA 30–60 min wallclock).

---

## What shipped

### Tools-hub commits on `main`

| Commit | Scope |
|---|---|
| `d25cd50` | docs: session handoff notes for 2026-05-26 PM block |
| `1303358` | feat(storage): tool-outputs bucket migration (0021) |
| `a1f1d37` | feat(storage): tool-outputs helpers — presigned PUT/GET + download + exists |
| `0b81a32` | feat(uploads): /api/upload-urls minter + browser PDB resolver + wire submit |
| `ef55d22` | feat(results): resolver-backed PDB cells + ZIP fallback, drop banner |
| `348c6e1` | test(uploads): cover /api/upload-urls and /api/jobs/.../pdb endpoints |
| `3f407dd` | fix(storage): normalize pdb_key prefix in resolver + storage path |

### llm-proteinDesigner commit on `master`

| Commit | Scope |
|---|---|
| `ecfa2f9` | fix(pipeline): align pdb_key with upload_filename across 5 tools |

Single-line change in each of `docker/{bindcraft,boltzgen,pxdesign,rfantibody,rfdiffusion}/run_pipeline.py` — pdb_key now reads `f"designs/{upload_filename}"` so the candidate's pdb_key and the Storage object's basename match. Without this, the resolver would 404 on every Storage hit even though the file was correctly uploaded.

### Modal app redeploys (all `--env main`)

| App | Version | Notes |
|---|---|---|
| kendrew-pxdesign-prod | v20 | 08:32, deployed first, 20 s wallclock |
| kendrew-bindcraft-prod | latest | parallel deploy ~12:55 |
| kendrew-boltzgen-prod | latest | parallel deploy ~12:55 |
| kendrew-rfantibody-prod | latest | parallel deploy ~12:55 |
| kendrew-rfdiffusion-prod | latest | parallel deploy ~12:55 |

Modal builder cache held — all 5 deploys completed in seconds-not-minutes. Memory note `feedback_modal_builder_cache_reliable.md` continues to hold; redeploys are cheap, do not block on time estimates.

### Supabase

Migration 0021 (`tool-outputs` bucket + 3 RLS policies) applied via Studio SQL editor against project `wjlhbxfnihboqebdvnns`. Bucket visible alongside `tool-inputs` and `lab-campaigns`.

---

## Architecture, end-to-end

The fix realises the architectural rule recorded in [`project_tools_hub_output_transport.md`](../../../.claude/projects/C--Users-lab-Documents-Claude-projects/memory/project_tools_hub_output_transport.md). Tools-hub owns the customer-facing output-transport contract; Kendrew/Modal pipelines write to URLs we hand them.

```
Browser GET /api/jobs/<id>/pdb/designs/design_001.pdb
   ↓ tools-hub resolver — basename normalize, Storage first, inline second
   ↓ output_exists() → True
   ↓ download_output() proxies bytes (server-side, no 302 — keeps the 3D viewer fetch same-origin)
Browser ← chemical/x-pdb bytes ← Supabase Storage (bucket 0021)
                                         ↑ PUT from pipeline (ecfa2f9 across 5 tools)
                                         ↑ presigned URL from /api/upload-urls (0b81a32)
                                         ↑ pipeline reads upload_urls_endpoint from JOB_PAYLOAD
                                         ↑ tools-hub submit sets _upload_urls_endpoint (0b81a32)
```

The 4-step plan from yesterday's PM handoff is closed:
- ✅ tool-outputs Storage bucket
- ✅ /api/upload-urls/<job_id>/<job_token> endpoint
- ✅ _upload_urls_endpoint wired into submit
- ✅ pdb_key → presigned-GET-then-proxy at render (plus basename normalization that yesterday's draft did not anticipate)

---

## Live verification state

### What I drove via the browser agent

On the pre-fix pilot `79228f03` (PXDesign, submitted 2026-05-26 before any of this shipped):
- Blue "missing inlined PDB bytes" banner is gone.
- View 3D + .pdb buttons render on both candidate rows.
- `<a>` hrefs and `data-pdb-url` attributes both point at `/api/jobs/<id>/pdb/designs/design_0.pdb`.
- Endpoint returns `404 + "# Candidate PDB not found.\n"` — exactly the "neither Storage nor inline" branch (Storage was empty because that job pre-dated 0b81a32; pxdesign-on-Modal at submit time did not POST to the upload endpoint).
- Mol* viewer falls back to "Could not load PDB from server." styled error — graceful, no JS crash.

This confirmed: resolver wiring, deploy, banner removal, JS handler switch (initMolViewerFromUrl vs initMolViewer), and CORS behaviour all work.

### What is still pending

A fresh pilot `7cf73ff6` was submitted via the browser agent at 12:50:51:
- PXDesign, pilot tier, num_designs=2, binder_length=80
- Target: PD-L1 (PDB 4ZQK), chain A, hotspots 35/52/62
- Wallet hold: $4.37 (balance $89.16 → $84.79)

This is the first job submitted after both `_upload_urls_endpoint` (tools-hub side) and `ecfa2f9` (pipeline side) shipped. When it completes, the result row should contain candidates with `pdb_key` set, the Storage bucket should contain matching design PDBs, and the results page should render real View 3D + .pdb downloads — not the 404 message.

### Bug caught and fixed mid-verification

PXDesign emits `pdb_key = "designs/design_0.pdb"` (with subfolder prefix), but `_safe_filename` in `shared/storage.py` ran the string through `werkzeug.secure_filename`, which flattens `/` to `_`. Storage path would have been `{user}/{job}/designs/designs_design_0.pdb` while the actual PUT landed at `{user}/{job}/designs/design_001.pdb`. Permanent 404 even after pipeline-side ships.

Fix in `3f407dd`: `posixpath.basename(filename)` before `_safe_filename` in three places (`_output_object_path`, `output_exists`, and the inline-fallback compare in `app.py`). Both `"design_001.pdb"` and `"designs/design_001.pdb"` URL forms now resolve to the same path. Tests added (`tests/test_candidate_pdb.py::TestPdbKeyPrefix`, 2 cases).

The companion fix on the pipeline side (`ecfa2f9`) makes `pdb_key` and `upload_filename` share the same basename so the round-trip is consistent. Either fix in isolation closes the bug; both together belt-and-braces it.

---

## Open work — next session

### 1. Confirm `7cf73ff6` end-to-end (PRIMARY)

When the job finishes (check `/jobs` or wait for the results email):
- Navigate to the job results page.
- Banner should be gone (already confirmed for pre-fix jobs).
- Each candidate row should show View 3D + .pdb buttons.
- **Click .pdb** — should download a real ~30 kB PDB file, not the 404 text. Check the file opens in PyMOL / Mol*.
- **Click View 3D** — should render the designed structure as cartoon, not "Could not load PDB from server."
- **Click Download PDBs (ZIP)** — archive should contain N PDB files where N = candidates returned.
- Append a PASS row to `docs/VALIDATION-LOG.md` for the PXDesign pilot.

If anything em-dashes or 404s, capture the network response and pdb_key values from devtools; the JS-eval recipe from this session is in `scratch/` or can be reconstructed.

### 2. Settle the wallet hold cleanly

`7cf73ff6` reserved a $4.37 hold. On completion the settle hook in `shared/jobs.py` should release the surplus and bill actual GPU seconds. Verify via `/account` or `recent_ledger()` that the post-job balance matches expectation. No code change anticipated — just confirmation.

### 3. Tail wave of carry-over from yesterday

Unchanged from the PM handoff:
- Phase 2 item 7 — signup hCaptcha (deferred, no bot signal).
- Phase 2 item 8 — failed-job page UX (wait for a real failure).
- Phase 3 item 10 — Workspace product fate (strategic decision).
- Phase 3 item 12 — Scope-C test gaps (multi-day, batch separately).
- Phase 3 item 13 — pricing transparency (blocked on conversion data).
- PXDesign mini_pilot 2× re-validation — still hidden in `tools/pxdesign/__init__.py`.
- Drop the dead `tool_jobs.credits_cost` column via a follow-up migration. No urgency.

### 4. Optional: shared upload helper in llm-proteinDesigner

The five `run_pipeline.py` files each have their own copy of `request_upload_urls` and `upload_output`. Same code, same shape. A `docker/_shared/upload_client.py` would centralise it and prevent future drift. Pure refactor, low priority.

---

## Key facts / gotchas (delta from PM handoff)

- **`pdb_key` is now a Storage path**, basename-aligned with the upload filename. Yesterday's PM handoff incorrectly described it as "a filename label, not a Storage path"; that was true at the time but is no longer. Future sessions: the basename of pdb_key IS the Storage object's filename.
- **werkzeug `secure_filename` flattens slashes**, so any code that previously round-tripped `cand.get('pdb_key')` through it lost the path. The fix uses `posixpath.basename()` first, then `_safe_filename()`. Stay on this order if you touch the resolver.
- **Pilot tier cost** is **$4.37** for PXDesign with num_designs=2 against a ~300 kB target — much cheaper than my PM-handoff guess of "~$5–$10". Worth knowing for future scoping.
- **The supabase MCP file_upload sandbox** rejected both `/tmp/` and `~/.claude/uploads/` paths during the form-submission verify. Worked around by building FormData in-page via `fetch('https://files.rcsb.org/download/4ZQK.pdb')` + `fetch('/tools/pxdesign/submit', {body: fd})`. Document this pattern if more browser-agent driven verification becomes routine.
- **Modal builder cache** continues to be reliable; all 5 deploys today were 20 s or less. Stop quoting 10–15 min estimates.
- **Modal app `Created at` is not last-deploy time.** Use `modal app history <app-id>` to get versions + timestamps.

---

## Reference

- PM handoff (yesterday): [SESSION-HANDOFF-2026-05-26-pm.md](SESSION-HANDOFF-2026-05-26-pm.md)
- Architectural rule memory: `project_tools_hub_output_transport.md`
- Live pilot: `7cf73ff6` (PXDesign pilot, submitted 2026-05-27T12:50:51)
- Pre-fix pilot used for wiring verification: `79228f03`
- Wallet topup floor + signup grant — unchanged from prior handoffs.
