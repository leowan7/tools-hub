# Handoff — target-first rework, Phase 1 (2026-07-28)

**Phase 0 is MERGED** (PR #97, `8e7e364` on `main`). **Phase 1 is coded on
`feat/phase1-design-targets`, not pushed, not QC'd.**

Approved plan: `~/.claude/plans/i-recently-made-many-jaunty-lerdorf.md`.
Register of known defects: `docs/audit-2026-07-22-campaign-rework-open-items.md`
(addendum 3 is this phase).

## What Phase 1 delivers

A target is now a first-class parent object: upload one structure, launch runs
against it one at a time, no re-upload. That is the phase's exit criterion and
it is met. Multi-tool launch is Phase 2; the combined ranked table is Phase 3.

| Area | What landed |
|---|---|
| Schema | `supabase/migrations/0039_design_targets.sql` — `design_targets` + RLS, plus `compute_campaigns.target_id`/`launch_group_id` and `tool_jobs.target_id`, all nullable, all `ON DELETE SET NULL` |
| Model | `shared/targets.py` — `DesignTarget` + CRUD, owner-scoped reads, paged run lookup |
| Intake | `shared/pdb_intake.py::resolve_target_upload` — lifted out of the run-create route so both paths validate uploads identically |
| Routes | `blueprints/targets.py` — `/targets`, `/targets/new`, `POST /targets`, `/targets/<id>`, `/targets/<id>/launch`, `POST /targets/<id>/archive` |
| UI | `templates/targets/{list,new,detail}.html`, plus a Targets nav link and a target chip on the run form |
| Reuse | `/campaigns/new?target_id=` and a `target:<uuid>` reuse token on the atomic tool forms |
| Retention | `cron/purge_old_storage.py::live_target_input_paths` — see "the one real bug" below |

## Before you push

1. **Apply `supabase/migrations/0039_design_targets.sql`** in the Supabase SQL
   editor. Unlike 0038 this one is load-bearing: `design_targets` does not
   exist without it, so `/targets` cannot work at all. The code degrades
   sensibly (`create_campaign` only sends `target_id` when set, so ordinary
   untargeted runs keep working), but the feature is inert until it runs.
2. Nothing under `tools/**` changed, so no Modal redeploy.
3. Railway auto-deploys web on merge.

## What the QC pass found

Five independent reviewers went over the branch (tenancy, migration, retention,
control flow, test quality), each told to distrust the comments and verify
against the code. **Every one of them found something, and three of the
findings were in code whose own comments asserted the opposite property.** If
you read nothing else here, read this list before writing the next phase.

Fixed on this branch:

- **An archived target was still launchable** through the `target:` reuse token
  (`blueprints/tools.py`), while `/campaigns` rejected the same id. Found by
  three reviewers independently, one of which ran the exploit. Archived targets
  are excluded from the retention sweeper's protected set, so accepting one
  creates a job row, copies nothing, and dies in Storage. The guard's own
  docstring claimed archiving was "the only removal the UI offers" — it wasn't,
  for that route.
- **An attached file overrode a `target:` token but the run was still filed
  under the target.** Override-by-upload is the documented behaviour for every
  reuse token (`templates/tools/_prefill.html` says so verbatim), so this was
  the NORMAL path, not an exotic POST — and it put designs made from one
  structure into another target's merged ranking. The two routes also took
  opposite precedence for the same conflict; they now share one rule (upload
  wins, target link drops).
- **`target_id` was never verified to reach the database.** Deleting the
  assignment from BOTH `create_job` and `create_campaign` left all 74 feature
  tests green: every assertion checked that a keyword argument reached a
  *mock*. `tests/test_target_id_persistence.py` now captures the dict handed to
  `.insert()`.
- **The suite ran against production** and the ownership assertions were
  flaky because of it (A20). Now isolated for these files.
- **Pre-0039 the retention sweep halted permanently**, not "for one pass" as
  the comment claimed — the table is missing on every pass. A missing table is
  now treated as an empty set (correct: no targets exist to protect) while any
  other error still fails closed.
- Plus: `_CAMPAIGN_PAGE_SIZE` is now asserted below `max_rows` at import (it
  was the only thing preventing the exact fail-open bug that shipped once
  before, and nothing pinned it); `target.kind` is checked against the tool;
  `hotspot_error` handles multi-chain strings; the epitope prefill used a key
  no form reads; and four comments that described intent rather than behaviour
  were corrected.

Filed, not fixed: **A18** (`validate_hotspots` rejects every hotspot on a
multi-chain target — pre-existing, and the reason the two paths now disagree),
**A19** (both retention guards page by offset, so a concurrent insert can drop
a row), **A20** (make test isolation autouse), **A21** (resolved: `create_job`'s
schema-gap retry had never fired).

## The one real bug this phase introduced, and caught

**Targets would have been deleted by the retention cron at 30 days.** The
sweeper ages out `tool-inputs` objects, guarded only by
`active_campaign_input_paths()` — which protects a campaign that can still
dispatch. Every campaign eventually goes terminal and stops protecting its
input. A target is long-lived **by design**, so its structure would have been
swept while the target still rendered as a normal launchable card, and the
next run against it would have died on an unrunnable input. Invisible until
someone launched.

Fixed by `live_target_input_paths()`, same contract as the campaign guard:
paged past the `max_rows` clamp, `None` means unknown and fails closed,
archived targets deliberately unprotected. Filed as **A15**.

## Decisions worth not re-deriving

- **`target_storage_path` stays denormalized on every run.** This is what
  keeps the whole phase additive: `_dispatch_chunk` re-mints its presigned URL
  from that column every wave and never learns `design_targets` exist. Do not
  "normalize" it into a join.
- **No new storage bucket.** Targets live in `tool-inputs` under
  `{user_id}/target-{target_id}/{filename}`. `_target_storage_key()` parses
  the id as a UUID because `upload_input` interpolates that slot into the
  object key verbatim — without it, `target-../../<other user>` writes outside
  the owner prefix.
- **`get_target(..., user_id=...)` is the entire tenancy boundary.**
  `copy_input` takes no `source_user_id` and `download_input` will read any
  object in the bucket. Every path that resolves a target id to a storage path
  fetches owner-scoped FIRST. The `target:` token in `blueprints/tools.py`
  resolves before `create_job` for exactly this reason (and so an unowned id
  cannot leave a pending orphan holding wallet funds).
- **Per-run chain/hotspot overrides are validated against the persisted
  `chain_summary`, not a re-download.** See A16. The atomic path still gets a
  full re-inspection, because the existing reuse hard-gate downloads the
  staged bytes back for every non-`alphafold:` token.
- **`archive_target` never touches storage.** `_dispatch_chunk` re-mints from
  the staged input on every wave, so deleting it would break every chunk of
  every live run. Archive is also the only removal offered, because
  `lab_campaigns.source_target_id` (Phase 5, migration 0040) has to be
  `ON DELETE CASCADE` and a hard delete would destroy paid CRO scoping
  requests.
- **`create_job(target_id=...)` is NOT in the schema-gap retry.** Unlike
  `campaign_label`, a silently dropped `target_id` would show the user a
  merged ranking missing designs they paid for. Unreachable pre-0039 anyway,
  since the same migration creates the table a target would have to live in.
- **Duplicate uploads are offered, never forced.** Same sha256 re-renders the
  form pointing at the existing target; `allow_duplicate=1` overrides. Two
  targets for one protein split its designs across two combined tables, which
  is what targets exist to prevent.

## Filed, not fixed

Addendum 3 of the audit doc: **A15** (resolved in phase), **A16**
(informational), **A17** (`/targets/<id>` reads runs twice and caps at 200,
so a heavy user could see an incomplete run strip — Phase 3's fan-in wants the
server-side function anyway).

Still open from addendum 2: **A10** (permanent `"skipped"` stalls a campaign in
`running` forever) is still the highest-value follow-up. **A11** is now wider,
because a live target legitimately pins its input forever — settle the
30-vs-90-day retention contradiction (migration 0021 vs
`templates/legal/terms.html:64`) before bounding it.

## Next: Phase 2

Multi-tool launch against one target: `shared/target_launch.py`, composite
preauth (summed budget, summed first wave, `divide_concurrency` splitting the
32-slot global cap), per-tool param panels, and `POST /targets/<id>/launch`
creating N campaigns as `draft` before funding any of them. `launch_group_id`
already exists in 0039 and is unused until then.
