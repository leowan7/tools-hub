# esmfold2-design run went silent for six hours (2026-11-05)

Read-only code investigation of one production row. **Nothing in this file was
executed against production**: the prod database is not reachable from these
sessions, so every statement about the row's own column values is labelled
`unverified - needs the SQL below` and the SQL is in §6 for Leo to run.

The row (Leo pulled it from prod; ground truth for this file):

| column | value |
| --- | --- |
| `id` | `e3703b7a-4ccb-410f-aef1-76bfb6fa8ae5` |
| `user_id` | `ca51a861-1681-4bb3-90ab-4fd5e3c3a3f8` (academic lab account) |
| `tool` / `preset` | `esmfold2-design` / `minibinder` |
| `status` / `failure_class` | `timeout` / `no_progress_timeout` |
| `gpu_seconds_used` | 655 |
| `created_at` | 2026-11-05 06:54:04Z |
| `started_at` | 2026-11-05 06:54:11Z |
| `completed_at` | 2026-11-05 13:00:29Z |
| `error` | null |
| hold tx | 1393 |

---

## 1. Timeline

| time (UTC) | event | how this is known |
| --- | --- | --- |
| 11-05 03:32:03 | PR #490 (`f6a11cf3`, "A run that times out keeps its designs, and bills only if it delivered") lands on `main` | `git log --format='%H %cI %s' origin/main` |
| 11-05 03:39:11 | its `pytest (test suite)` run goes green - the gate Railway deploys behind | `gh run list --branch main --json headSha,conclusion,updatedAt` |
| 11-05 06:54:04 | row created, wallet hold 1393 placed | the row |
| 11-05 06:54:11 | first heartbeat -> `status=running`, `started_at` stamped (7 s after create) | the row; `shared/jobs.py::mark_running` |
| ~11-05 07:05 | last heartbeat consistent with `gpu_seconds_used = 655` (~10 min 55 s) | §5 |
| <= 11-05 08:39:11 | the orchestrator container is **certainly** gone: `_ORCHESTRATOR_TIMEOUT_S = 6300` s (1 h 45 m) from its own start, enforced by Modal killing the task | `tools/esmfold2_design/modal_app.py:145-172` |
| 11-05 12:54:11 | row becomes sweeper-eligible (`started_at` + `STUCK_RUNNING_AGE_HOURS`, default 6) | `cron/sweep_stuck_jobs.py:223,228,285` |
| 11-05 13:00:29 | the hourly sweep terminalises it: `status=timeout`, `failure_class=no_progress_timeout`, hold settled | the row; `cron/sweep_stuck_jobs.py` running arm |

**Detection lag: 6 h 06 min from `started_at`.** At least **4 h 20 min** of that
is pure lag - the GPU work could not still have been alive after 08:39Z
(§5). The hourly cadence is *unverified*: it is consistent with
`13:00:29` and with the hourly cron recorded at
`docs/SESSION-HANDOFF-2026-05-26.md:13`, but the Railway schedule lives outside
this repo.

**Deploy of #490 before the sweep: strongly supported, not proven.** Its
pytest gate was green 9 h 21 m before the sweep, and three later `main` commits
carrying it also went green before 13:00Z (`479d60da` 11:23Z, `0f41562e`
11:56Z, `762c65c7` 12:47Z). `/health` serves `9fe76c7c` today, whose gate only
went green at 15:56Z, so the web service does redeploy on merge
(`curl -s https://tools.ranomics.com/health`). The residual unknown is that the
sweeper is a **separate** Railway cron service (`tools-hub-sweep-stuck`,
`docs/SESSION-HANDOFF-2026-05-26.md:30-31`) with its own deploy history, which
is not readable from here. §6 SQL block A settles it from the row instead.

---

## 2. A - which sweeper arm, and was the keep reached?

**The `running` arm**, selected on age alone:

```python
.eq("status", "running").lt("started_at", running_cutoff)
```

`cron/sweep_stuck_jobs.py:284-285`, cutoff computed at `:228` from the env
read at `:223`. Nothing in that query or
anywhere in `sweep_stuck_jobs` reads progress, heartbeats or the tool's own
time limit. It then calls `timeout_stuck_job(job_id)`
(`cron/sweep_stuck_jobs.py:301`).

Inside `shared/jobs.py::timeout_stuck_job` (`:1833`): recovery is tried first
(`recover_stuck_job_result`), and only when it returns nothing does the row go
`mark_timeout` -> conditional keep -> settle (`:1961-1972`):

```python
if not mark_timeout(job_id):
    return ""
fresh = get_job(job_id)
if fresh is not None:
    exit_verdict = verdict.get("exit_verdict")
    if exit_verdict not in ("failed", "failed_unconfirmed"):
        fresh = _keep_streamed_designs(
            fresh, status="timeout",
            reclassify=exit_verdict != "still_running",
        )
    _settle_wallet_hold_for_completed_job(fresh)
```

**`failure_class` staying `no_progress_timeout` is consistent with five
different outcomes, not two.** Ranked by what the SQL would show:

| # | outcome | `result` | `inputs._partial_candidates` | 0047 constraint present |
| --- | --- | --- | --- | --- |
| i | `reconstruct` found nothing to keep (`shared/job_recovery.py::reconstruct`, `:215-253`): no streamed candidates and an empty Storage prefix | NULL | absent/empty | either |
| ii | `exit_verdict == "still_running"` -> designs kept, `reclassify=False`, class deliberately left refunded (`shared/jobs.py:1951-1959`) | has `candidates`, `partial: true` | non-empty | either |
| iii | `exit_verdict` was `failed` / `failed_unconfirmed` -> the keep was skipped entirely | NULL | may be non-empty | either |
| iv | keep ran with `reclassify=True` but **migration 0047's CHECK does not yet name the new class** (whether it was applied is unverified from here - SQL block B reads it), so the second CAS failed the CHECK and the class stayed put | has `candidates`, `partial: true` | non-empty | **absent** |
| v | #490 was not deployed on the sweeper service at 13:00Z - no keep path existed | NULL | may be non-empty | either |

Distinguishing columns: `result` (candidates vs NULL), `result->>'partial'`,
`inputs->'_partial_candidates'`, `inputs->'_progress'`, and whether
`tool_jobs_failure_class_check` names `timeout_with_delivery`. SQL blocks A and
B in §6 read exactly those. Note (iv) is a real possibility and not in the
original two-outcome framing: the sibling note
`timeout-keep-designs-PR490-handover.md` (in this session group's shared notes
folder, outside the repo, so a reader of the repo alone cannot open it) records
0047 as **hand-apply and not
yet applied**, and `supabase/migrations/0047_timeout_with_delivery_failure_class.sql`
says so itself ("Until it is, the second CAS that writes the new class fails
the CHECK ... the row keeps 'no_progress_timeout' -> full refund. The designs
are written by a separate earlier CAS, so they survive either way").

**Status: unverified - needs SQL blocks A and B.**

---

## 3. B - did the customer get anything?

### The email: no, nothing was sent. (verified from code)

Two independent reasons, either alone sufficient:

1. `timeout_stuck_job` never calls `complete_job` on the timeout path
   (`shared/jobs.py:1961-1972` writes through `mark_timeout` /
   `_keep_streamed_designs` / `_settle_wallet_hold_for_completed_job`), and
   `_send_completion_email` is only reached from `complete_job`.
2. Even if it were reached, its own gate drops the row:
   `if job.status not in {"succeeded", "failed"}: return`
   (`shared/jobs.py:2999`).

`shared/email.py` *does* carry refunded-timeout wording ("The run did not
complete; your wallet was not charged."). That gate makes it unreachable for a
swept timeout. So the customer's only signal was the page, if they went back to
look.

### The page: branches entirely on `kept_designs`. (verified from code)

`shared/run_notices.py::kept_designs` (`:88-104`) requires status in
`_KEPT_HEADS`, `result["partial"]` truthy **and** `candidate_records(result)`
non-empty. Then:

- `templates/job_detail.html:263` -
  `{% if kept_designs %}Job timed out before it finished.{% else %}Job timed out before producing a result.{% endif %}`
- `templates/job_detail.html:316` -
  `{% if (job.status == 'succeeded' or kept_designs) and job.result %}` gates
  the whole results partial.

So **with** a `candidates` key the page already shows the designs and the
"before it finished" wording; **without** one it shows "before producing a
result" and no results block. There is no third state.

### Exports: no status gate at all. (verified from code)

`blueprints/jobs.py::export_csv` (`:1552`) and `export_fasta` (`:1599`) read
`page_ordered_records(job.tool, job.result)` with no check on `job.status`. So
a single `result` write fixes the page, the CSV and the FASTA together.

### Are the designs recoverable now?

Only if the seed's batch had actually streamed. `shared/wallet_guard.py:40-130`
records esmfold2-design as the exception that uploads a seed's designs **only
after `designer.design` returns the whole batch**
(`tools/esmfold2_design/run_pipeline.py::_shape_designs`), so a seed cut off
mid-batch leaves nothing for that seed. With 655 GPU-s on a `minibinder` preset
it is entirely possible no batch had completed. Whether anything is in Storage
is **unverified - needs SQL block A** (`inputs._partial_candidates`) plus a
Storage listing of the job's prefix.

### If the designs exist in Storage and the page shows nothing

The repair is to re-run the keep for this one row -
`_keep_streamed_designs(fresh, status="timeout", reclassify=...)` - which
rebuilds `result` from `inputs._partial_candidates` filtered against a Storage
listing, exactly as the sweeper would have. `reclassify` must stay **False**
unless Leo decides this run should be billed, because `reclassify=True` is the
money decision. **Not performed.** It needs a prod write and it sits on the
settlement path.

---

## 4. C - was the refund real?

### What the code writes (verified from code)

`_settle_wallet_hold_for_completed_job` (`shared/jobs.py:2646`):

- reads `inputs._wallet.hold_tx_id` (= 1393) and returns early unless status is
  one of succeeded/failed/timeout/cancelled (`:2686`);
- `failure_reason = "timeout"` for a timeout row (`:2718-2719`);
- when `inputs._wallet.live is True` it **rebinds** `release_hold` to
  `settle_live_run(..., refund=True)` (`:2732-2740`);
- `no_progress_timeout` is in `_REFUNDED_FAILURE_CLASSES`, so it takes the
  refund arm: `release_hold(hold_tx_id, reason="timeout")` and returns
  (`:2748-2759`).

esmfold2-design is in `_LIVE_ALL_PRESETS` (`shared/wallet_guard.py:40-130`), so
this run was live-metered and the live route is the one taken.
`shared/wallet.py::settle_live_run` (`:870-915`) with `refund=True` passes
`p_final_due_usd = Decimal("0")`.

In SQL (`supabase/migrations/0045_live_run_debits.sql:145-252`), with
`v_final = 0` the `IF v_final > 0 AND v_final >= v_taken` test is false, so the
**ELSE** branch runs and inserts exactly one child row:

- `kind = 'hold_release'`
- `amount_usd = v_taken - 0` = everything the `run_debit` children took
- `parent_tx_id = 1393`, `failure_reason = 'timeout'`
- `notes = 'live run: returned what the debits took above the final cost'`
  (or `'live run: closed with nothing taken and nothing owed'` if no debit ever
  fired)

and then sets `user_wallets.balance_usd` to the recomputed balance. It is
idempotent: a pre-existing non-`run_debit` child short-circuits at `:188-197`.

So **the code path is a full return of everything taken, and the customer
should be at $0 for this run.** Whether that row actually landed is
**unverified - needs SQL block C**. The two realistic failure modes it would
expose: no `hold_release` child at all (the RPC raised and was only logged,
`shared/jobs.py:2750-2756`), or `returned < taken_by_debits`.

---

## 5. D - what did we actually pay, and is there a worse defect behind it?

### The six-hour-container hypothesis is impossible under the code (verified)

`tools/esmfold2_design/modal_app.py`:

- `_MAX_SESSION_S = 5400` (`:145`) - each H100 child `_run_one_seed` is
  decorated `timeout=_MAX_SESSION_S`;
- `_ORCHESTRATOR_TIMEOUT_S = _MAX_SESSION_S + 15*60` = 6300 s (`:148`) - the
  CPU orchestrator's own `timeout=`; it has no `gpu=`;
- `_CHILD_WAIT_BUDGET_S = _ORCHESTRATOR_TIMEOUT_S - _REAP_MARGIN_S` (`:171-172`)
  bounds the wait for the slowest child, leaving a reaping margin;
- the comment at `:154-155` states Modal enforces `timeout=` by killing the
  task and raising `FunctionTimeoutError` client-side.

1 h 45 m is therefore the hard ceiling on this run's GPU life, against a
6 h 06 m gap. **The container did not stay alive for six hours.** It died
(or was killed) and no webhook arrived; the remaining >= 4 h 20 m is detection
lag, which is our defect, not Modal's.

### Why 655 s, and why the live meter never noticed

`gpu_seconds_used` is written from each inbound heartbeat by
`mid_run_monitor_check` (`shared/jobs.py:2956-2990`, CAS-guarded on
pending/running), through `billed_gpu_seconds` (`:2914`). With per-seed beats
(`inputs._seed_beats`, stamped by `webhooks/modal.py:625-631`), a seed whose
last beat is older than `_SEED_BEAT_STALE_S = 150` (`:2870`) counts only to its
**last beat**, not to now. So:

- 655 s is a last-heartbeat-derived figure, frozen when the beats stopped. It
  is the number the refund settled against (and a refund makes it moot here).
- Because a seed counts to *now* only while its last beat is under 150 s old
  and falls back to that last beat once it is staler than that, the metered
  figure steps *down* when the beats stop rather than growing. So
  `meter_live_runs` could not run the debits away over six hours and **no wallet stop could ever fire** for a
  stalled run - the metered elapsed simply stops growing. That is by design,
  but it means the money path is structurally blind to a stall.

### What settles the Modal bill

In the Modal dashboard, for this job's function call: the **orchestrator**
`FunctionCall` end time and status, and the **per-container runtimes** of each
`_run_one_seed` child. Those give the real wall time we are billed for; the
expectation from the code is <= 6300 s orchestrator (CPU, cheap) and <= 5400 s
per H100 child. A read-now figure is a **lower bound**: our memory records that
a Modal line keeps settling upward by roughly 1.7% for about an hour after a
run.

### The worse defect behind it

On the `still_running` verdict the sweeper refunds the run and **leaves the
Modal call running** - nothing on any path cancels Modal
(`shared/jobs.py:1951-1959` names the deliberate refund, and names Modal only
as the source of the `still_running` verdict - it says nothing about
*cancelling* Modal; the no-cancel half is recorded in
`docs/design/LIVE-CHARGING-2026-10-01.md:428,440`, which states that
`timeout_stuck_job` "does not call Modal's cancel" (`:428`) and that the
sweep service "has no Modal tokens" (`:440`). That page discusses the stop
button's own gap in the same paragraph, but it states the missing-tokens fact
only for the sweep service; the stop's token situation is **unverified** here.
The sibling note `timeout-keep-designs-PR490-handover.md` lists the no-cancel
gap as pre-existing.) For *this* row the 1 h 45 m ceiling caps the exposure, but for a tool whose own ceiling is longer than
`STUCK_RUNNING_AGE_HOURS` the refund-and-abandon combination pays for GPU we
have already given away.

---

## 6. SQL for Leo (read-only)

### Block A - which outcome, and did the customer get designs

```sql
select id, status, failure_class, gpu_seconds_used,
       created_at, started_at, completed_at, error,
       (result is null)                                   as result_is_null,
       (result ? 'candidates')                            as result_has_candidates,
       jsonb_array_length(coalesce(result->'candidates','[]'::jsonb))  as kept_count,
       result->>'partial'                                 as result_partial,
       jsonb_array_length(coalesce(inputs->'_partial_candidates','[]'::jsonb))
                                                          as streamed_count,
       inputs->'_progress'                                as progress,
       inputs->'_seed_beats'                              as seed_beats,
       inputs->'_wallet'                                  as wallet
  from tool_jobs
 where id = 'e3703b7a-4ccb-410f-aef1-76bfb6fa8ae5';
```

Read it as: `result_has_candidates = true` -> outcome (ii) or (iv), the designs
exist and the page is already showing them; `result_is_null = true` with
`streamed_count > 0` -> outcome (iii) or (v), designs were streamed and the row
lost them; `result_is_null = true` with `streamed_count = 0` -> outcome (i),
there was nothing to keep. `wallet->>'live'` should be `true`; if it is not,
§4's live route does not apply and the refund went through plain `release_hold`
instead.

(If `result` / `inputs` are `json` rather than `jsonb` in prod, the `?` and
`jsonb_array_length` calls need a `::jsonb` cast.)

### Block B - is migration 0047 applied

```sql
select conname,
       pg_get_constraintdef(oid) like '%timeout_with_delivery%'
         as migration_0047_applied
  from pg_constraint
 where conrelid = 'public.tool_jobs'::regclass
   and conname  = 'tool_jobs_failure_class_check';
```

`false` (or no row) means outcome (iv) is live for **every** delivered timeout
from now on, not just this one.

### Block C - the ledger for hold 1393

```sql
select id, kind, amount_usd, balance_after_usd, parent_tx_id,
       tool_slug, gpu_seconds, gpu_class, failure_reason, notes, created_at
  from wallet_transactions
 where id = 1393 or parent_tx_id = 1393
 order by id;
```

and the one-line verdict:

```sql
select (select amount_usd from wallet_transactions where id = 1393) as hold_amount,
       coalesce((select -sum(amount_usd) from wallet_transactions
                  where parent_tx_id = 1393 and kind =  'run_debit'), 0) as taken_by_debits,
       coalesce((select  sum(amount_usd) from wallet_transactions
                  where parent_tx_id = 1393 and kind <> 'run_debit'), 0) as returned,
       (select count(*) from wallet_transactions
         where parent_tx_id = 1393 and kind <> 'run_debit')              as settle_rows;
```

Expected for a correct live refund: `hold_amount = 0` (a live-run anchor is a
zero-amount hold - `0045_live_run_debits.sql:173-176` raises otherwise),
`settle_rows = 1` of kind `hold_release` with `failure_reason = 'timeout'`, and
`returned = taken_by_debits`. `settle_rows = 0` means the refund never landed
and the hold is still open; the stranded-hold pass
(`cron/sweep_stuck_jobs.py::_sweep_stranded_holds`) should pick it up on a
later run, which is itself worth checking.

Do **not** join `wallet_transactions.job_id` to `tool_jobs.id` here: the column
is `bigint` and is NULL on holds (`0017_wallet.sql:80`), while `tool_jobs.id`
is a uuid. `parent_tx_id` is the only reliable link.

---

## 7. The question behind all four: was anything watching?

**No. Nothing in this repo would have noticed.** (verified from code and docs)

- `ALERTING.md` is entirely site-uptime: UptimeRobot on `/health` and `/`,
  Railway native deploy/crash/OOM email, `/readyz`, the synthetic Platform API
  smoke, an outbound-timeout audit, and one operator alert on a new Platform
  API submission. Greps for `stuck` and for `no_progress` in it return
  **nothing at all** (`grep -c -i stuck ALERTING.md` -> 0,
  `grep -c -i no_progress ALERTING.md` -> 0).
- The only operator-alert sender in the codebase is
  `notify_operator_new_submission` (`shared/email.py`, `OPERATOR_ALERT_EMAIL`,
  default `leo@ranomics.com`). There is nothing for a stalled run.
- There is **no Sentry in the Python code at all**
  (`grep -rn sentry --include=*.py .` over non-test files returns nothing), so
  a `logger.warning` from the sweeper reaches the Railway cron log and stops
  there.
- `ALERTING.md:329` records that Railway cron "does not natively alert on a
  script's exit code as cleanly as" the alternative, so a non-zero exit is not
  a channel either.
- The only existing visibility is aggregate and after the fact:
  `cron/daily_digest.py:345-375` puts a per-tool `"timeout"` **count** in the
  window's `tool_activity`. No job id, no user, no "this run has been silent
  for N minutes".

### The smallest change that would have caught it

The row already carries everything needed: `inputs._seed_beats[seed] = [first,
last]` gives a per-seed last-heartbeat timestamp (`shared/jobs.py:2900-2910`),
and the sweeper already holds a service client and runs on a schedule. So:

> In `sweep_stuck_jobs`, add a second read of `status='running'` rows (no age
> cutoff) and flag any row whose newest `_seed_beats` last-beat is older than a
> small multiple of the tool's keepalive interval - while leaving its status
> and its hold completely alone - then **email that list to
> `OPERATOR_ALERT_EMAIL`**, reusing the existing operator-alert sender.

Roughly 40 lines of detection plus a template. The detection half is safe and
self-contained; the **email half is what makes it an alert**, and that is the
part that is not. It is a new recurring outward-facing send, it needs a
staleness threshold and a per-tool scope that are Leo's policy call rather
than a defect fix, and it lands inside the cron that settles wallet holds.
(An earlier draft of this file gave a different reason - that the one
operator-email surface, `cron/daily_digest.py`, was owned by open PR #430.
That is wrong: #430 merged on 2026-10-15 (`6e322cc3`, `gh pr view 430`) and is
an ancestor of this branch, so the file is free.) A log-only version would be
a guard that certifies false - nobody reads cron logs and there is no Sentry
to catch the line.

**So this is written up, not implemented.** It is the P1 below.

---

## 8. Defects this exposes, ranked

| # | severity | defect | evidence | why it is not fixed here |
| --- | --- | --- | --- | --- |
| 1 | **P1** | Nothing alerts on a paying run going silent. 6 h 06 m to notice, >= 4 h 20 m of it after the GPU was certainly dead. | §7 | Needs a new recurring operator email (outward-facing) plus a staleness threshold that is a policy call, in the cron that settles holds. |
| 2 | **P1** | On the `still_running` verdict the sweeper refunds the run and leaves the Modal call running; nothing on any path cancels Modal, and the sweep service holds no Modal tokens (the stop button's own token situation is unverified). | refund-by-design: `shared/jobs.py:1951-1959`; no Modal cancel and no Modal tokens: `docs/design/LIVE-CHARGING-2026-10-01.md:428,440` | Money + production state. Lead schedules. |
| 3 | **P1** | Migration 0047 is hand-apply and, per the sibling handover note, **not yet applied**. Until it is, every delivered timeout keeps its designs *and* stays fully refunded - we give away GPU silently. | ordering requirement: `supabase/migrations/0047_timeout_with_delivery_failure_class.sql:43`. "Hand-apply" and "not yet applied" come from a sibling session note outside the repo and are **unverified** here; SQL block B settles it. | Leo applies migrations. |
| 4 | **P2** | A swept timeout sends the customer **no email**, even though the refunded-timeout wording already exists in `shared/email.py`. Their only signal is re-opening the page. | `shared/jobs.py:2999`; the timeout path never calls `complete_job` (`:1961-1972`) | Outward-facing send; the fix is a policy call (which timeouts mail, and what they say). |
| 5 | **P2** | `STUCK_RUNNING_AGE_HOURS` is a single flat 6 h for every tool, anchored on `started_at` and reading no progress. For esmfold2-design, whose own ceiling is 1 h 45 m, that is 4 h 15 m of guaranteed dead time; for a tool with a longer ceiling it kills live runs (which `shared/jobs.py:1951-1959` already calls "our stall"). A per-tool cutoff derived from each tool's Modal `timeout=` would fix both ends. | `cron/sweep_stuck_jobs.py:223,228,284-285`; `tools/esmfold2_design/modal_app.py:145-148` | Changes when runs are terminated, i.e. the money path. |
| 6 | **P2** | The live meter is structurally blind to a stall: a dead seed's billed seconds stop growing once its last beat is more than 150 s stale (the figure falls back to that last beat), so no wallet stop can ever fire for a silent run. | `shared/jobs.py:2870,2913-2953` | By design today; listed so it is not mistaken for a safety net. |
| 7 | **P3** | Two stale claims in `cron/sweep_stuck_jobs.py`'s own docstrings described detection that does not exist ("running with no progress this long") and a refund policy #490 superseded ("Only jobs with nothing recoverable are ... full-refunded"). | the file, before this change | **Fixed in this PR** - prose only. |
