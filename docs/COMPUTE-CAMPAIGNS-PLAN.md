# Self-serve compute campaigns ("Campaigns") — build plan

Status: APPROVED (design + decisions), NOT started in code. Created 2026-07-03.
Source: multi-agent design workflow `wf_471e3d06-1ef` (8 readers, 3 designs, judge,
4 adversarial-review lenses, final plan). This doc is the resumable reference.

## Goal

Let a logged-in user self-serve a large protein-design run (up to ~20,000 designs)
for a target. The system splits it into many normal single-tool sub-jobs (each sized
to fit one GPU container's timeout), fans them out on Modal's autoscaler with
server-side admission control, tracks aggregate progress, aggregates + ranks results,
and bills it safely. Removes the "no self-serve at scale, route everyone to Pilot"
hard block: clients may self-serve at full scale with cost confirmation + prepaid
guardrails instead of a block.

## Product decisions (Leo, 2026-07-03)

1. **Naming**: the new batched compute feature is **"Campaigns"**. The existing
   wet-lab CRO funnel (shared/campaigns.py, lab_campaigns, /campaigns/*) is relabeled
   **"Lab projects"**. Phase 1 hosts the new feature at `/runs/*` internally to avoid
   destabilizing the live wet-lab funnel; reconcile URLs (new -> `/campaigns`, wet-lab
   -> `/lab-projects`) as a clean cutover before wide launch.
2. **Zero-yield chunks**: bill honestly for GPU consumed, with explicit UI copy.
3. **Positioning**: separate compute-only product (GPU cost + 1.70x markup, no wet-lab
   validation, no 100% binder guarantee); keep a prominent "validate top hits in the
   lab" upsell into the Pilot -> Sprint -> Custom ladder.
4. **Guardrails = Open**: ~$25k/day per-user campaign spend cap; verified-account
   gating only above ~$5000 pre-authorization; prepaid-balance gate is the primary
   safety mechanism. (Exact numbers tunable later.)

## Four load-bearing architectural decisions

### 1. Sub-jobs stay ordinary `tool_jobs` rows
Each chunk is a normal job that runs and reaches terminal state ONLY through the
existing poll / webhook / heartbeat / cancel / sweeper writers via `_cas_update`.
The campaign layer is READ + LAUNCH + RECONCILE and NEVER writes a child's terminal
state, never calls complete_job/mark_*. Preserves the exactly-one-writer invariant.

### 2. Billing = real per-child wallet holds + admission counter (NO escrow trick)
REJECTED the "prepaid escrow with amount_usd=0 child holds" model — all three
reviewers proved it manufactures money (settle_hold/release_hold credit balance
directly, have no campaign awareness, and their parent_tx_id idempotency guard would
strand the final refund). Instead:
- At create, ONE campaign-total pre-authorization RPC checks balance + frozen +
  campaign velocity cap against the summed budget. Does NOT debit balance.
- Each sub-job places a NORMAL per-child wallet hold via the UNCHANGED reserve_hold
  path (real amount, real balance debit), tagged with campaign_id, settled by the
  100%-unchanged `_settle_wallet_hold_for_completed_job` / settle_hold / release_hold.
- `compute_campaigns.reserved_usd` is a RESERVATION COUNTER derived as SUM of open
  child holds under a FOR UPDATE lock; `try_campaign_admission` refuses dispatch when
  `reserved_usd + next_child_estimate > budget_usd`.
- Result: `balance == SUM(amount_usd)` holds automatically; delivered-only billing
  falls out for free (crashed chunks refund via release_hold; completed_no_yield bills
  honest GPU); no orphan/double-refund path.

### 3. One driver, three triggers, DB-enforced against double-submit
`drive_campaign(campaign_id)` is the ONLY launcher. Idempotent; single-flight under
`pg_advisory_xact_lock(campaign_id)` held across the ENTIRE count -> create -> spawn ->
set_modal_call sequence (must span the Modal network call). Triggers:
- (a) once at create to kick the first wave;
- (b) inline hook on child terminal writes — ONE explicitly-scoped edit to
  complete_job/cancel_job: after CAS-win side effects commit and settlement lands, if
  `fresh.campaign_id` is set, enqueue best-effort drive_campaign (swallow errors, never
  block the terminal write);
- (c) `cron/tick_campaigns.py` (flask campaigns:tick; planned at ~60-90s, but
  the Railway cron actually runs `*/5 * * * *` -- verified in the dashboard
  2026-07-30, see A46 -- so a stranded campaign waits up to 5 min; modeled on
  sweep_stuck_jobs.py) as authoritative backstop.
Idempotency guarantee = DB `UNIQUE(campaign_id, chunk_index, attempt)` partial index +
`INSERT..ON CONFLICT DO NOTHING` + CAS launch (`UPDATE..WHERE modal_function_call_id
IS NULL`). COUNT-before-create is only an optimization.

### 4. $1000 ceiling swapped, not deleted (campaign path only)
Monolithic single jobs keep SELF_SERVE_CEILING_USD as a sanity guard. The campaign
path does NOT hit the single-job wallet_preflight ceiling branch. Replace with:
- typed cost-confirmation at /runs/new (net-new UI; confirm_band is a plain boolean
  today at app.py:2692);
- prepaid-balance gate (campaign won't run unless balance covers pre-auth);
- FINITE per-user daily campaign velocity cap (daily_campaign_cap_usd) shipped in
  Phase 1, NOT deferred, so removing the block does not make velocity control inert;
- large first-time pre-auths gate on payment-age/verified status
  (user_wallets.per_job_cap_override_usd, dormant today at 0017:53).

## Data model (migration 0034)

`public.compute_campaigns`: id uuid PK; user_id uuid FK auth.users ON DELETE CASCADE;
name text; tool text; preset text; target_pdb_id text; target_storage_path text (target
staged ONCE, shared by every chunk); target_name text; params jsonb (shared per-chunk
params minus num_designs and underscore keys); requested_designs int; chunk_size int;
total_subjobs int; concurrency_target int DEFAULT 20; max_attempts int DEFAULT 2;
status text CHECK in (draft, funded, running, completing, completed,
completed_with_failures, failed, cancelled); budget_usd numeric; reserved_usd numeric
(SUM open child holds); spent_usd numeric; refunded_usd numeric; escrow_tx_id bigint
(loose link, not FK); created_at/confirmed_at/started_at/completed_at/last_tick_at.
Indexes: (user_id, created_at DESC); partial (status) WHERE status IN
(funded, running, completing).

`tool_jobs` gains: campaign_id uuid nullable FK -> compute_campaigns(id) ON DELETE SET
NULL; chunk_index int; attempt int DEFAULT 1. Partial index (campaign_id, status) WHERE
campaign_id IS NOT NULL (the progress COUNT GROUP BY shape). UNIQUE(campaign_id,
chunk_index, attempt) partial index. Also set campaign_label = campaign name so
sub-jobs surface in existing /jobs grouping for free.

Migration house style: idempotent (CREATE TABLE IF NOT EXISTS, DO $$ guards, CREATE
INDEX IF NOT EXISTS), ENABLE RLS + self-read SELECT USING(auth.uid()=user_id), all
writes service-role. Mirror tool_jobs 0005. create_job gains campaign_id/chunk_index
with the 0022-style schema-gap retry so code can deploy before the migration is
hand-applied. Do NOT re-add a stricter tool_jobs.status CHECK (live 0005 omits
'cancelled' though Python writes it — verify live schema before campaign cancel ships).

## Phased roadmap

- **Phase 1 (L) — Correctness foundation + honest limits. SHIPPED (branch
  feat/compute-campaigns, not merged/deployed).** Small campaigns end-to-end on
  rfdiffusion, bindcraft, boltzgen. Real fan-out via admission control, aggregate
  progress + results, EXACT delivered-only billing. Fixes num_designs-vs-timeout
  mismatch (chunk size from TOOL_SPECS gpu_s/design vs container timeout, not the
  loose validator cap). rfantibody/pxdesign GATED out (1/chunk and validator-cap 24
  make 20k absurd until Phase 4). `MAX_SUBJOBS_PER_CAMPAIGN = 20` (not 50): child
  holds use the UNCHANGED reserve_hold, which enforces the $200/day single-job cap
  (SQL, 0020); 20 sub-jobs keeps even boltzgen (~$8.74/chunk -> ~$175) under $200 so
  a solo campaign never stalls on the daily cap.
- **Phase 2 (L) — Robust driver at ~150-500 sub-jobs.** Daily-cap-EXEMPT child-hold
  RPC (raise MAX_SUBJOBS past the $200/day cap), a STALL REAPER (a campaign whose
  remaining chunks can never hold — persistent balance/cap refusal — must finalize as
  completed_with_failures rather than sit 'running' forever), a per-campaign advisory
  lock (eliminate the transient racing-driver hold churn), self-heal from stalls/lost
  drives/transient failures; concurrency fairness (reserve interactive slots);
  freeze/cancel edge cases; retry REFUNDED-class children as fresh sibling rows;
  tighter campaign-child stuck cutoff; suppress per-child post-settle hooks, fire one
  campaign-level summary.
- **Phase 3 (M) — Aggregate results + export.** Cross-child leaderboard (canonicalize
  score-key casing, namespace pdb_key by child id, keep provenance); combined
  CSV/FASTA; streamed/async ZIP (not io.BytesIO); PDBs via same-origin proxy;
  delivery-reconciliation panel; soften Pilot/Sprint CTAs to self-serve.
- **Phase 4 (M) — campaign_chunk preset + true 20k + verified gating.** Confirm real
  external container ceiling per ranomics-<tool>-prod; register
  PRESET_CAPS['campaign_chunk'] (<=14400s) + teach each validate() to accept it; raise
  pxdesign num_designs cap above 24; re-tune chunk sizer; wire per_job_cap_override_usd
  as verified-account ceiling.

## Cost guardrails (must be in place before ceiling removal)

- Single campaign-total pre-authorization RPC (balance + frozen + velocity, does not
  debit).
- Real per-child holds settled by the unchanged, already-correct settle path.
- Reservation admission counter derived as SUM of open holds (idempotent, no
  leak/underflow).
- Typed cost-confirmation UI (net-new).
- Finite daily campaign velocity cap (Phase 1).
- Per-job SQL hard cap retained per sub-job.
- Refund-unused is automatic + idempotent (no escrow to strand).
- Freeze stops new dispatch; refund/release paths never trap user funds.
- Crash-safe creation (campaign row before any money moves).
- Abuse limits: concurrency clamp + per-user global in-flight cap + reserved
  interactive Modal slots; verified gating above ~$5000.
- DB-enforced sub-job idempotency.

## Deploy gating (each requires Leo)

- Push to main (Railway auto-deploy): explicit per-session authorization required.
- Migration 0034 + the new SQL RPCs: hand-applied to Supabase (no automated runner).
- Confirm external container ceilings (Phase 4) live in the ranomics-<tool>-prod repos.

## Remaining open questions

- Under-funding behavior: if early children run hot and budget exhausts before the
  delivery target, stop-and-resume ("top up and resume") vs. size initial budget off
  per-chunk HARD CAP. Recommendation: explicit resume, not silent stop.
- Notification: one campaign-grain completion email (reuse _send_completion_email at
  campaign level, respecting per-child hook suppression); interim health alerts?

## Key reuse map (verify line refs before editing)

Reused verbatim: ModalClient.submit/poll/cancel; complete_job, mark_* CAS helpers,
_cas_update, _settle_wallet_hold_for_completed_job (reads inputs._wallet.hold_tx_id,
idempotent, routes by failure_class); classify_terminal_state + BILLED/REFUNDED policy;
poll route; Modal webhook + heartbeat; upload_urls_endpoint; sweeper; per-child
settle_hold/release_hold + compute_hard_cap/estimated_cost_for_tool; storage dual-path
download_output; single-job export_csv/fasta/zip + job_candidate_pdb proxy; job_detail
polling IIFE; field_group/status_badge/submit_cta/results_shell/candidate_table macros;
migration house style + RLS self-read.

Verified precedent: the refold route already loops create_job(campaign_label) + submit
sharing one group key — the driver generalizes this with a real FK + admission + billing.

Net-new: shared/compute_campaigns.py; compute_campaigns table + tool_jobs columns
(0034); chunk sizer; drive_campaign + inline hook + cron/tick_campaigns.py; the three
campaign RPCs; hooks-suppressed settle flag; /runs/* routes + templates/runs/*;
Phase-4 campaign_chunk preset + validator acceptance.

Explicitly NOT overloaded: shared/campaigns.py + lab_campaigns + /campaigns/* (wet-lab
CRO, incompatible FSM/RLS/billing — kept separate, relabeled "Lab projects").
