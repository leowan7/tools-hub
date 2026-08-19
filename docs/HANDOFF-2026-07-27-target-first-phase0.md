# Handoff — target-first rework, Phase 0 (2026-07-27)

> **SUPERSEDED 2026-07-28.** Phase 0 shipped: PR #97 merged to `main` as
> `8e7e364`. Current handoff is `HANDOFF-2026-07-28-target-first-phase1.md`.
> Kept for the "why" sections below, which are still the reason those fixes
> look the way they do.

**Phase 0 is COMPLETE and QC-clean.**

## State

- Branch `fix/phase0-campaign-hardening`, **six commits** off `origin/main` (`bb57477`).
- Full suite: **1524 passed, 6 skipped, 0 failed** (10-20 min depending on load;
  the `test_compute_campaigns_driver.py` cases sleep, slowest ~95s, so run it in
  the background and do not read slowness as a hang).
- Approved plan: `~/.claude/plans/i-recently-made-many-jaunty-lerdorf.md`
  (migration numbers and the stale-branch claims in it were corrected this session).

| Commit | What |
|---|---|
| `4d12c86` | presign swallow, `candidate_records` at 8 sites, wrapped-`designs` normalizer, retention sweeper guard |
| `e1311e4` | fan-in pagination, iggm ranking, handoff-email count, idempotent `Location` + migration 0038 |
| `0f772a1` | docs |
| `d09cb0f` | **lab handoff stages the starred design, not its screen position** |
| `82ac902` | **Location replay made real; sweeper guard paged and fails closed** |
| `98094ff` | export provenance + global rank + real metrics in exports |

All 0.1-0.7 steps are done. 0.1 was already merged as PR #95 before the session.

## Before you push

1. **Apply migration 0038** in the Supabase SQL editor. The code is now safe in
   either order (`_claim_key` uses `select("*")`, `_store_response` retries
   without the column), but without the migration a replayed redirect still has
   no `Location` — it degrades to the cached body's link, not a blank page.
2. Nothing else blocks. Railway auto-deploys web on merge; no Modal redeploy
   (nothing under `tools/**` changed).

## The three fixes that came out of QC, and why they matter

An 11-agent adversarial QC pass reviewed all three changesets. It found that
**two of the earlier fixes on this branch were themselves defective and one was
inert**, all with a green suite. Do not assume a passing suite means a fix works.

- **`d09cb0f` was a live blocker.** Threading `candidate_records` through the
  lab handoff turned "stages zero PDBs" into "stages the WRONG PDB" for the
  seven designs-shape tools: their partials re-sort, `_source_index` was stamped
  only on the campaign path, so the starred row's *screen position* indexed the
  *unsorted* list. The CRO would have received a different structure than the
  one starred, with a success email. **Any new re-sorting results template must
  stamp `_source_index` before the sort.**
- **`82ac902`, half one:** the `Location` fix did nothing, because `_claim_key`
  projected an explicit column list without `location`. Use `select("*")`, never
  `,location` — pre-0038 that 400s and `_claim_key` fails OPEN, which means a
  double-clicked submit places a second wallet hold and spawns a second Modal job.
  **Superseded 2026-08-18 (A42 resolved):** that `except` no longer fails OPEN,
  it refuses with a 503. `select("*")` is still right; the consequence of
  getting it wrong is now an outage, not a double-charge.
- **`82ac902`, half two:** the retention guard re-introduced the PostgREST
  1000-row clamp and failed OPEN, so `--apply` could have deleted live campaigns'
  inputs. Now paged, negation-filtered server-side, fails closed on overrun.

Two test fakes were making the green suite lie: `tests/test_idempotency.py`'s
ignored PostgREST column projection, and `_designs_shape()` in
`tests/test_export_shapes.py` invented a nested `scores` dict and a `sequence`
that no designs pipeline emits. Both now model reality. **Do not "simplify"
either back** — nor the `max_rows` clamp modelled in `tests/test_data_retention.py`
and `tests/test_campaign_results.py`.

## Filed, not fixed

`docs/audit-2026-07-22-campaign-rework-open-items.md`, addendum 2, items A8-A14:

- **A10** a permanent `"skipped"` leaves a campaign in `running` forever — no
  terminal state, no email, no TTL. Pre-existing (`adapter is None` already did
  this), widened by the presign fix. Highest-value follow-up.
- **A11** the retention guard has no recency bound, so a stuck campaign pins its
  input forever and quietly defeats the 30-day promise in the Terms.
- **A12** `fund_campaign` can silently no-op, stranding a campaign in `draft`.
- **A13** a hold placed before a failed `create_job` is unreachable if its
  release also fails.
- **A14** root metrics export under pipeline names (`iptm`, not `ipTM`);
  canonical aliasing belongs with the merged target table (Phase 3.3).

## Next: Phase 1

`design_targets` is migration **0039**, and the lab target source is **0040**
(0038 was consumed by `idempotency_location`). The plan file has been corrected.

## Things worth not re-deriving

- `_dispatch_chunk` creates the child row and places the hold *before* the old
  presign call, which is why "return skipped on presign failure" was wrong as
  originally planned. The fix moves the presign ahead of the money.
- `copy_input` / `download_input` / `download_output` take `user_id` as a **path
  component, not an authz check**. The `target:<uuid>` token planned for Phase 1
  must re-fetch scoped to `ctx.user_id` before touching a path, mirroring
  `blueprints/tools.py:1312-1321`.
- `lab_campaigns.source_target_id` must be `ON DELETE CASCADE` to match 0037,
  which is only safe because the UI will **archive** targets and never
  hard-delete. A hard delete would silently destroy paid CRO scoping requests.
- `archive_target` must **not** call `delete_input`: `_dispatch_chunk` re-mints a
  presigned URL every wave, so deleting an input mid-campaign breaks every
  remaining chunk.
