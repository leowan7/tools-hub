# Handoff — target-first rework, Phase 0 (2026-07-27)

Session paused mid-Phase-0. Everything below is committed; nothing is pushed.

## Start here when you resume

```bash
cd tools-hub && git checkout fix/phase0-campaign-hardening && ./venv/Scripts/python.exe -m pytest -q
```

**This is the one thing that must happen first.** The full suite has NOT been run
since the pagination change in `e1311e4`. The last full run was `1493 passed, 6
skipped` at commit `4d12c86`. Since then only the touched suites were run (142
passed). The risk is real, not theoretical: `iter_succeeded_children` now calls
`.order()` and `.range()`, and any other test fake that stands in for the
Supabase client on the aggregator or the "Passed filters" rollup will raise
`AttributeError` the way `_FakeQuery` in `tests/test_campaign_results.py` did.
Expect a small number of fakes to need the same two methods added.

Expected count after the run: roughly `1493 + 33` new tests, so ~1526.

## Where the work lives

- Branch `fix/phase0-campaign-hardening`, two commits off `origin/main` (`bb57477`).
- Approved plan: `~/.claude/plans/i-recently-made-many-jaunty-lerdorf.md`.
- Commit `4d12c86` — presign swallow, `candidate_records` at 8 sites, wrapped-`designs` normalizer, retention sweeper guard, audit addendum, PRODUCT-PLAN backlog.
- Commit `e1311e4` — fan-in pagination, iggm ranking, handoff-email count, idempotent `Location` + migration 0038.

## Done

| Plan step | State |
|---|---|
| 0.0 file findings | Done. Audit addendum A1-A7 in `docs/audit-2026-07-22-campaign-rework-open-items.md`; backlog in `docs/PRODUCT-PLAN.md`; memory pointers in `project_open_items.md`. |
| 0.1 uncap exports | **Already shipped** as PR #95 before this session. Nothing to do. |
| 0.2 `candidate_records` | Done, 8 sites (one more than planned: the completion email reported "0 candidates" for designs-only tools). Plus a real normalizer gap: `_normalize_result_shape` only unwrapped `output.candidates`, so a wrapped `output.designs` row stayed invisible. |
| 0.3 paginate fan-in | Code done, **verification incomplete** (see above). |
| 0.4 export provenance | **Not started.** The only Phase 0 item with no code yet. |
| 0.5 email count + iggm reshape | Done. |
| 0.6 presign swallow | Done. Resolved *before* the hold and the child row so a failure costs nothing. |
| 0.7 idempotent headers | Done, needs migration 0038 applied. |

## Two corrections to what memory said at session start

1. **`fix/uncap-csv-fasta-exports` and `feat/data-retention-30d` are merged**, as PR #95 and #96. Memory still described both as "CODED, NOT pushed". Local `main` was 9 commits behind when this session started.
2. **Migration numbering shifted.** 0038 is now `idempotency_location`. The plan's `design_targets` becomes **0039** and the lab-campaign target source becomes **0040**. Update the plan before Phase 1.

## Unplanned work this session, and why

The retention sweeper that shipped in PR #96 selects purely on object age with
no reference to campaign state, while `_dispatch_chunk` re-mints a presigned
URL from `campaign.target_storage_path` on every wave. A long-running or paused
campaign could have had its input swept out from under it. Added
`active_campaign_input_paths()` plus a fail-closed filter. This was written up
as A6 in the audit addendum, which originally (wrongly) assumed the sweeper had
not landed.

## Next actions, in order

1. Run the full suite; fix whatever fakes need `.order()`/`.range()`.
2. Finish **0.4 export provenance** in `shared/exports.py`: leading CSV columns become `rank, tool, campaign_id, source_job, source_chunk, pdb_key, source_rank` with `rank` as the global row index and the tool's own rank demoted to `source_rank`; FASTA ids become `>rank{global}_{tool}_{job8}_{basename}` with the `designs/` prefix stripped; extract one `export_key(cand, i)` shared by all three serializers so CSV, FASTA and ZIP cannot disagree.
3. Get independent QC on the two commits before any push (per the standing rule: commit is fine, push is manual and QC-gated).
4. Apply migration 0038 in the Supabase SQL editor **before** the code deploys, or `@idempotent()` silently falls back to caching without `Location`.
5. Then Phase 1 (`design_targets`, now migration 0039).

## Things worth not re-deriving

- `_dispatch_chunk` creates the child row and places the hold *before* the old presign call, which is why "return skipped on presign failure" was wrong as originally planned. The fix moves the presign ahead of the money.
- `copy_input` / `download_input` / `download_output` take `user_id` as a **path component, not an authz check**. The `target:<uuid>` token planned for Phase 1 must re-fetch scoped to `ctx.user_id` before touching a path, mirroring `blueprints/tools.py:1312-1321`.
- `lab_campaigns.source_target_id` must be `ON DELETE CASCADE` to match 0037, which is only safe because the UI will **archive** targets and never hard-delete. If a hard delete ever ships it silently destroys paid CRO scoping requests.
- The test fake in `tests/test_campaign_results.py` now models the PostgREST `max_rows` clamp on purpose. Do not "simplify" it away; it is what makes the pagination test real.
