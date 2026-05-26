# Session Handoff — 2026-05-26 PM (continued)

Continues from [SESSION-HANDOFF-2026-05-26.md](SESSION-HANDOFF-2026-05-26.md). Same day, second session.

---

## Status at session end

- **3 commits shipped to `main`:** `4a7caf2` (handoff doc), `b063ae7` (`credits_cost` cleanup), `7a32e19` (pilot-UX banner + VALIDATION-LOG row).
- **Phase 1 punch list:** materially closed. PXDesign pilot verification ran end-to-end via web UI; pipeline mechanics PASS, customer-facing UX FAIL on PDB transport (see "Open work" below).
- **Phase 3 Item 11:** done — `Preset.credits_cost` field dropped across 9 tool adapters + ToolJob + tests; DB column retained at 0 to honour the migration's NOT NULL.
- **Test suite:** 628 passed, 6 skipped, 0 failures after the cleanup.

---

## What shipped

### `b063ae7` — drop dead `Preset.credits_cost` field

Wallet pivot cleanup. Removed the field from `tools/base.py`, the 15 registration sites across 9 tool adapters, and ToolJob serdes. `create_job` no longer takes a `credits_cost` kwarg; the DB INSERT keeps `"credits_cost": 0` to satisfy the migration-0005 NOT NULL constraint until a follow-up migration drops the column.

### `7a32e19` — pilot-UX banner

Adds a single explanatory banner above the candidate table whenever any candidate has `pdb_key` but no `pdb_content_b64`. Mitigation only — the customer still can't download the PDB or render the 3D viewer for those rows; they just know it's a known gap instead of thinking their job broke. See [candidate_table.html:167](../templates/components/candidate_table.html#L167).

### VALIDATION-LOG row

Today's PXDesign pilot logged as **FLAG**: pipeline mechanics proven, UX FAIL on PDB transport. Audit across 9 successful pilot-tier jobs (all 5 tools) confirmed the gap is systemic: only 1 (`boltzgen c6e25830`) emits `pdb_content_b64`; the other 8 produce blank 3D/PDB cells.

### PXDesign pilot job 79228f03

Job `79228f03-4167-4a9b-8576-0512130f5c68` ran in **7.7 min wallclock / 462 GPU-s** on A100-80GB. 2 candidates surfaced via pilot fallback (`48b0737`):

| rank | ipTM | pLDDT | pAE | filter_status |
|---|---|---|---|---|
| 1 | 0.16 | 88.0 | 22.7 | below threshold |
| 2 | 0.13 | 94.0 | 23.9 | below threshold |

Low scores are **fixture artifact**: lysozyme (1HEW) is a ~130aa enzyme with the catalytic dyad at E35/D52 and substrate cleft at 62 — not a binder-design target. AF2-IG honestly reports no interface. The recorded run is a clean E2E validation of the pipeline path, not a quality benchmark; PD-L1 (4ZQK chain A, residues 18-132, hotspots 35/52/62) is the proper quality fixture for future runs.

### Misc

- Phase 2 item 6 verified: `STAFF_NOTIFY_EMAIL=leo@ranomics.com` on Railway prod.
- Phase 2 item 9 verified: `tool_jobs_p90` view populated (n=2-3 per tool, weekly cadence sufficient).
- Demo-verification memory refreshed: BindCraft `a0dbcf1` + rfantibody `eea8f143` already-attested pilot rows recorded as VERIFIED; PXDesign is the only pilot that was outstanding.
- `tools-hub-sweep-stuck` cron's first scheduled fire at 16:00:42 UTC ran clean: 2 Supabase queries 200'd, `pending=0 running=0 errors=0`. Cron wiring confirmed working.

---

## Architectural concern — tools-hub should NOT depend on Kendrew code

**This is the most important note in the handoff.** The fix I queued earlier (via `spawn_task`) was "patch each Kendrew tool's `docker/<tool>/run_pipeline.py` to mirror the webhook-tier b64 emission to pilot tier" — that's the **wrong direction**. Tools-hub should not have correctness-dependencies on Kendrew pipeline internals. The two projects are independent products with independent release cycles; tools-hub owns the customer-facing storage and rendering contract.

### Right shape of the fix

The pilot-tier transport gap should be fixed inside tools-hub by completing the `_upload_urls_endpoint` protocol that's already half-wired:

1. **Add a `tool-outputs` Supabase Storage bucket** (alongside `tool-inputs` and `lab-campaigns`) with RLS / signed-URL access scoped per `{user_id}/{job_id}/`.
2. **Add a tools-hub route** that returns presigned PUT URLs for `{user_id}/{job_id}/designs/design_N.pdb` paths. This is what `_upload_urls_endpoint` was originally designed to point at — see [gpu/modal_client.py:370](../gpu/modal_client.py#L370) where it's read but [app.py](../app.py) never sets it.
3. **Set `_upload_urls_endpoint` on submit** so the Modal pipeline knows where to POST output PDBs.
4. **Resolve `pdb_key` at results-render time** by generating a presigned GET URL (or fetching + inlining) from the new bucket. Remove the `pdb_content_b64` dependency from `candidate_table.html` and the ZIP-export route in favor of `pdb_key` → storage-resolved bytes.

After that, tools-hub owns the entire output-transport contract. Modal pipelines that POST to provided presigned PUT URLs work out of the box. The "5-tool Kendrew patch" task becomes a one-time pipeline-side adjustment (POST to the URL we hand them) that doesn't repeat per tool, and the contract is in tools-hub source where it belongs.

### Action: dismiss the spawn_task chip

I queued a chip earlier — "Mirror webhook-tier b64 emission to pilot tier (5 tools)" with cwd=llm-proteinDesigner. **Dismiss it.** The replacement is the four-step plan above, inside tools-hub.

---

## Open work — next session

### 1. Build the tool-outputs presigned-PUT path inside tools-hub (HIGH)

Replaces the dismissed Kendrew patch. Four steps in order:

a. **Storage bucket migration.** Add `tool-outputs` to `supabase/migrations/`. Mirror the file_size_limit and mime allowlist from `tool-inputs` (20 MB, `chemical/x-pdb` + `chemical/x-cif`).

b. **Output-URL endpoint** at `/api/upload-urls/<job_id>` (or similar). Owner-scoped (user_id from session must match `tool_jobs.user_id`); returns presigned PUT URLs for `{user_id}/{job_id}/designs/design_N.pdb` with a short TTL (~4 hours, matching pilot wallclock). Reuse the `shared/storage.py` patterns.

c. **Wire `_upload_urls_endpoint`** in the submit path. In [app.py](../app.py) around line 2680-2700 (where `_input_presigned_url` is added to inputs), also set `_upload_urls_endpoint` to the new route URL so the Modal pipeline gets it via `gpu.modal_client.submit`.

d. **Resolve at render.** In `candidate_table.html` and `app.py`'s export routes, when `pdb_key` is set but `pdb_content_b64` is absent, generate a presigned GET URL from `tool-outputs` and use that for download links + 3D-viewer fetch. Remove the now-stale banner from `7a32e19` once the resolver lands and the cells render properly.

Verification: re-run PXDesign pilot on PD-L1 4ZQK (proper fixture), confirm the results page renders 3D viewer + PDB download buttons (not the banner). Append a PASS row to VALIDATION-LOG.

### 2. PXDesign mini_pilot 2× re-validation (deferred from Wave 4)

`tools/pxdesign/__init__.py` still hides mini_pilot from the form pending its own re-validation. Lower priority than item 1.

### 3. Drop the dead `tool_jobs.credits_cost` column (optional)

The DB column is now harmless dead metadata, hardcoded to 0 on insert. A future migration can drop the column + NOT NULL constraint. No urgency.

### 4. Carried over from morning handoff

- **Phase 2 item 7** (signup hCaptcha) — still deferred, no signal that bots are back.
- **Phase 2 item 8** (failed-job page UX) — wait for a real failure.
- **Phase 3 item 10** (Workspace product fate) — strategic decision.
- **Phase 3 item 12** (Scope-C test gaps) — multi-day, batch separately.
- **Phase 3 item 13** (pricing transparency) — blocked on conversion data.

---

## Key facts / gotchas (delta from morning handoff)

- **`pdb_key` is NOT a Storage path.** It's a filename label produced by the Modal pipeline. The PDB bytes never reach tools-hub on pilot tier today. The "server-side resolve `pdb_key` → bytes" path I initially proposed mid-session does not work because there's nothing to resolve against. The fix (item 1) requires a new bucket + presigned-PUT contract.
- **Lysozyme (1HEW) is a poor quality fixture for binder design.** Use PD-L1 (4ZQK chain A, hotspots 35/52/62) for any score-quality verification going forward; 1HEW is fine for pipeline-mechanics-only smoke.
- **Tools-hub commit `b063ae7`** changed `create_job`'s signature (removed `credits_cost` kwarg). Anything calling `create_job` outside this repo would need an update — there shouldn't be any external callers, but flag if you hear of one.

---

## Reference

- Morning handoff: [SESSION-HANDOFF-2026-05-26.md](SESSION-HANDOFF-2026-05-26.md)
- VALIDATION-LOG: today's PXDesign pilot row is at the top of the PXDesign section
- Pilot-shape audit script (gitignored): `scratch/inspect-pilot-shapes.py`
- PXDesign poll script (gitignored): `scratch/poll-pxdesign-79228f03.py` — template for future job pollers
- Wallet ledger shape, topup floor, signup grant, ToS placeholders — unchanged from morning handoff
