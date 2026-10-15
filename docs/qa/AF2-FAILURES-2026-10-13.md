# af2 failures on 2026-10-13: triage

Source claim: `docs/growth/DIGEST-ANALYSIS-2026-10-14.md` section 4 counts "3 failed and 1 timed out on af2, out of 5 runs" from the 10-13 digest. That count is checked below. It is only partly confirmed.

## Method

- **Evidence:** the raw work-dir archives that `tools/af2/modal_app.py::_park_raw_archive` writes to Modal volume `ranomics-af2-raw`, one per run, whatever the outcome. I downloaded them with `modal volume get`. Each archive holds ColabFold's own `log.txt`, `config.json` and `input.fasta`.
- **No logs:** Modal function logs for 10-13 have expired. `modal app logs ranomics-af2-prod --since 4d` starts at 10-14 08:19 UTC.
- **No staff pages:** they were not read. Chrome is signed in as a test account, and staff pages return Not found for it.
- **No database yet:** nothing below comes from the database. Status, failure class and refunds are taken from the code path, and the query at the end confirms or corrects them.
- **Privacy:** customer sequences stay in the session scratchpad and are not reproduced here.

## Per job (times UTC, 10-13)

| Job | Ran | Input | Outcome in archive | Cause | Side |
|---|---|---|---|---|---|
| d8aa9b50 | 05:31–05:38 | 2-chain complex, 329 + 334 aa, multimer, templates on | No PDB. ColabFold printed "Could not get MSA/templates", then `Done` | `api.colabfold.com` read timeout on the MSA result download, 6 read timeouts from 05:34:04 to 05:38:48 (`colabfold.py:241 download`) | OUR (upstream) |
| 73a098e5 | 05:54–05:59 | Byte-identical input to d8aa9b50 | No PDB. Same message | MSA came back. The template tarball read timed out mid-stream (`colabfold.py:294 tar.extractall`) | OUR (upstream) |
| 87275108 | 06:00–06:05 | Byte-identical input to d8aa9b50 | No PDB. Same message | Same as 73a098e5 | OUR (upstream) |
| edec81fb | 08:33–08:37 | 337 aa monomer, templates on | rank_001 PDB and scores, pLDDT 90.5 | Succeeded on the GPU | – |
| 7d1c86d7 | 08:41–08:43 | 153 aa monomer, templates on | rank_001 PDB and scores, pLDDT 89.3 | Succeeded on the GPU | – |

The volume holds 5 archives for 10-13, which matches the digest's 5 af2 runs. But they show 3 failures, not 4, and neither of the other two failed on the GPU. The digest counts the 24 hours before it was sent, not the UTC day (`cron/daily_digest.py:93`), so its 5 rows need not be these 5. The "1 timed out" is one of two things:
- one of the two folds above, finished but with its result never collected, or
- a row from 10-12 that never reached the GPU and so left no archive.

**Unverified. Query A below settles it.**

## Why the three failed, end to end

1. **The fold ran.** `_run_single` calls `run_colabfold` (`tools/af2/run_pipeline.py:1115`), which adds `--templates` when the form asked for it (`:584-585`).
2. **ColabFold gave up without an error code.** When the MSA server stops answering, ColabFold logs "Could not get MSA/templates" and moves to the next query. It still exits 0. Our wrapper only fails on a nonzero exit (`:644-653`).
3. **Our parser reports the wrong fault.** `parse_af2_output` finds no PDB and fails `parser:pdb_missing` (`:674-678`). So the real cause, the upstream server, never reaches the job row.
4. **The poll path stores the error under bucket `parser`.** The error dict becomes `{"bucket": "parser", "detail": "parser:pdb_missing — no PDB file in …"}` (`gpu/modal_client.py:756`, `:795-802`, `blueprints/jobs.py:989`).
5. **It is classified `unclassified`.** `parser` is not in `_ERROR_BUCKET_TO_FAILURE_CLASS` (`shared/jobs.py:808`), so the run falls through to `unclassified` (`:881`).
6. **Billing.** `unclassified` is a refunded class (`shared/jobs.py:798-804`). If these runs carried a wallet hold, the full hold was released. **Amounts are unverified until Query B.**
7. **What the user saw** (derived from code, page not read):
   - The cause line "This run failed on our side, not because of your input." (`shared/jobs.py:1152`).
   - The advice "Try again with the same settings. If it fails a second time, contact us…" (`:987`).
   - A button labelled "Try again with these settings" (`templates/job_detail.html:339`, `retry_unchanged` at `shared/jobs.py:1297`).
   - The user did exactly that, twice, 23 and 6 minutes apart. Both retries failed because the server was still degraded.

## Not the customer's input

- A run on 10-14 at 08:19 UTC (aeb11433) used the same two chains, in swapped order, with multimer and templates on. It folded: rank_001, ipTM 0.246 (its archive's `log.txt`). Whether it was the same customer is unverified; Query C shows it.

## Not caused by a deploy

- None of #422–#428 touched `tools/af2`, `tools/colabfold` or `gpu/modal_client.py` (`git log origin/main -- tools/af2 tools/colabfold gpu/modal_client.py`).
  - The last change to those paths was #415 (10-06), which only added comment lines to `tools/colabfold/__init__.py`.
  - Before that came #409 (10-01), which changed only the pricing copy in `tools/af2/__init__.py` and `meta.py`.
  - `tools/af2/run_pipeline.py` was last changed in #356 (09-25).
- #422 did touch `shared/jobs.py`, but its hunks are an import line and `cancel_job` (`git show c5a061fc -- shared/jobs.py`). Neither is on the failure path above. Its af2 change is 2 lines of `templates/tools/af2_results.html`.
- af2 runs in this repo's Modal app. The lpd repo (#33–#37) is not involved.

## Earlier matches: same cause, previously UNKNOWN

`docs/qa/FAILED-RUNS-2026-09-30.md` lists two `parser: pdb_missing` rows as UNKNOWN, with "Ours vs. upstream is not determinable without logs." Their archives answer that:

- **0049514e (08-27):** 6 `api.colabfold.com` read timeouts between 03:21:56 and 03:24:22, then "Could not get MSA/templates" (`log.txt`).
- **c6b91dcd (09-30):** "Could not get MSA/templates … Read timed out" at 05:44:49 (`log.txt`).

So the same upstream failure has hit af2 on 08-27, 09-30 and 10-13 (3 runs). Each time it showed up as a parser fault.

## Still happening?

Not as of the last run read. All three af2 runs since then folded:

- aeb11433, 10-14 08:19 UTC
- a768b3f1, 10-14 08:38 UTC
- 499600cb, 10-15 02:05 UTC

The server failure comes and goes. It will recur, and each time it will show up as `parser:pdb_missing`.

## What we own, and options (no code changed)

- **Root cause.** The public ColabFold MSA server is a shared free service with no SLA. Taking it out of the path means running our own MSA search, which needs the databases on our side. That is not a small change.
- **Smallest useful fix.** Name the failure truthfully. After `colabfold_batch` exits 0, look for ColabFold's "Could not get MSA/templates" line, and fail with its own check instead of `parser:pdb_missing`. Then give it a cause line ("the public sequence-search service we use did not respond; your input was fine") and advice to wait before retrying.
  - It changes no billing: the bucket still falls through to `unclassified`, which is refunded.
  - Care: the detail contains "timed out", which the `timeout` advice rule would answer with "crop the target". The new check needs its own row ahead of that rule.
  - `tools/colabfold/run_pipeline.py` has the same `pdb_missing` exit-0 path.

## Query for Leo (read-only)

Run in the Supabase SQL editor. Customer emails are not returned; the `internal` column flags Leo's accounts and test accounts.

```sql
begin transaction read only;

-- A: every af2 row near the 10-13 digest window, with status, class and error
select left(j.id::text, 8)                       as job,
       j.created_at, j.started_at, j.completed_at,
       j.status, j.failure_class, j.preset,
       left(coalesce(j.error::text, ''), 200)    as error,
       j.gpu_seconds_used,
       left(md5(j.user_id::text), 8)             as user_tag,
       (u.email ilike '%@ranomics.com' or u.email ilike 'leowan7%') as internal,
       j.inputs->'_wallet'->>'hold_tx_id'        as hold_tx_id
from tool_jobs j
join auth.users u on u.id = j.user_id
where j.tool = 'af2'
  and j.created_at >= '2026-10-12 00:00+00'
  and j.created_at <  '2026-10-14 00:00+00'
order by j.created_at;

-- B: the wallet rows behind those holds (hold, hold_release, charge)
select left(j.id::text, 8) as job, w.id, w.kind, w.amount_usd,
       w.gpu_seconds, w.parent_tx_id, w.created_at
from tool_jobs j
join wallet_transactions w
  on w.id = (j.inputs->'_wallet'->>'hold_tx_id')::bigint
  or w.parent_tx_id = (j.inputs->'_wallet'->>'hold_tx_id')::bigint
where j.tool = 'af2'
  and j.created_at >= '2026-10-12 00:00+00'
  and j.created_at <  '2026-10-14 00:00+00'
order by j.created_at, w.id;

-- C: af2 runs since, to see whether the same user came back
select left(md5(j.user_id::text), 8) as user_tag,
       left(j.id::text, 8) as job, j.created_at, j.status, j.failure_class
from tool_jobs j
where j.tool = 'af2'
  and j.created_at >= '2026-10-13 00:00+00'
order by j.created_at;

commit;
```
