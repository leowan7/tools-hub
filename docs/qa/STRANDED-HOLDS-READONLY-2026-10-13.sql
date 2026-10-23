-- Stranded wallet holds: READ-ONLY diagnostic. SELECTs only, no RPC, no writes.
--
-- Prepared 2026-10-13. NOT RUN: production reads are denied in the session that
-- wrote this (permission classifier, reason "Production Reads"), so the count,
-- the per-hold rows and the stranded dollar total are all still UNKNOWN.
--
-- This corrects the query in PR #425's body (now merged as 995a9c89). That one
-- could not have worked:
--   * It cross-checked holds against tool_jobs via wallet_transactions.job_id.
--     Hold rows carry job_id = NULL -- shared/wallet_guard.py:312 and
--     shared/compute_campaigns.py:2002 both pass None, because the hold is
--     placed before the job row exists -- and the types do not match anyway
--     (wallet_transactions.job_id is bigint, tool_jobs.id is uuid).
--   * The real link runs the other way: blueprints/tools.py:2329-2341 stashes
--     the hold id on the job at inputs._wallet.hold_tx_id. That is the join
--     below.
--
-- Why "no child row" is a sound test for "neither settled nor released": both
-- the release path and the settle/charge path insert a child row carrying
-- parent_tx_id = the hold id (supabase/migrations/0018_wallet_rpcs.sql:180-201,
-- 0017_wallet.sql:301-351).
--
-- Known blind spot, stated because it narrows the result: a live-charging run
-- writes run_debit children against the hold
-- (supabase/migrations/0045_live_run_debits.sql:9), so a stranded hold on a
-- live run that already took at least one debit will NOT appear below.
--
-- Hold amounts are stored negative (0017_wallet.sql:30), hence abs().

-- 1. Per-hold detail and classification.
with stale_holds as (
    select t.id, t.user_id, t.amount_usd, t.tool_slug, t.created_at, t.notes
      from public.wallet_transactions t
     where t.kind = 'hold'
       and t.created_at < now() - interval '2 hours'
       and not exists (
               select 1
                 from public.wallet_transactions c
                where c.parent_tx_id = t.id)
)
select h.id                as hold_tx_id,
       h.user_id,
       abs(h.amount_usd)   as dollars_held,
       h.tool_slug,
       h.created_at,
       j.id                as job_id,
       j.status            as job_status,
       case
           when j.id is null                      then 'STRANDED (no job row)'
           when j.status in ('pending', 'running') then 'in flight (fine)'
           else 'STRANDED (job ' || j.status || ')'
       end                 as classification
  from stale_holds h
  left join public.tool_jobs j
         on j.inputs -> '_wallet' ->> 'hold_tx_id' = h.id::text
 order by h.created_at desc;

-- 2. Totals by class. Run as its own statement.
with stale_holds as (
    select t.id, t.user_id, t.amount_usd, t.created_at
      from public.wallet_transactions t
     where t.kind = 'hold'
       and t.created_at < now() - interval '2 hours'
       and not exists (
               select 1
                 from public.wallet_transactions c
                where c.parent_tx_id = t.id)
), classed as (
    select abs(h.amount_usd) as dollars_held,
           case
               when j.id is null                       then 'STRANDED (no job row)'
               when j.status in ('pending', 'running') then 'in flight (fine)'
               else 'STRANDED (job ' || j.status || ')'
           end as classification
      from stale_holds h
      left join public.tool_jobs j
             on j.inputs -> '_wallet' ->> 'hold_tx_id' = h.id::text
)
select classification,
       count(*)           as holds,
       sum(dollars_held)  as dollars
  from classed
 group by classification
 order by dollars desc nulls last;
