# proteina paid cancel test — plan (NOT run)

Status: plan only. Nothing here has run. Needs, in order:
1. This branch merged and the proteina Modal app redeployed (Leo).
2. Leo sets the T1 test account balance with `docs/qa/T1-SET-BALANCE-9.00-BINDCRAFT.sql`
   (main checkout; it only touches the T1 account).
3. Leo's per-job yes, typed in the session that runs it.

## What it proves
A proteina run cancelled after its first saved design keeps that design and
bills only the GPU time used up to the cancel, now that the shard sends a
`searching` beat every 60 s during a chunk (`tools/proteina/run_pipeline.py::keepalive`).

## Run
- Account: the T1 test account only. Never the owner account.
- Tool: proteina, preset `protein_binder`, `num_designs` = 8 (one shard, `_SHARD_DESIGNS`
  in `tools/proteina/__init__.py`), a known-good target such as 1HEW.
- Before clicking Cancel, stub `window.confirm = () => true` in the tab
  (the native dialog wedges the tab and the cancel POST never fires).
- Cancel as soon as the run page shows the first saved design.
- Hard stop: cancel at 20 min whether or not a design has arrived.

## Cost
A100-80GB at $0.0017476/s (`shared/wallet_estimates.py`).
- Expected: first design after chunk 1, about 300-400 s → about $0.55-0.70.
- Hold at launch: about $3.67 (released on cancel, minus the charge).
- Worst case at the 20 min stop: 1200 s → about $2.10.

## Record
- job id and campaign id
- seconds between the last keepalive beat and the cancel (from `_progress`)
- `gpu_seconds_used` on the child row after cancel, and the ledger charge
- designs kept on the run page and in the CSV/FASTA export
- Pass: charge = `gpu_seconds_used` × rate, and `gpu_seconds_used` is within
  ~60 s of wall-clock from `started_at` to the cancel; the design is kept.

## Not covered
Every proteina run is a campaign child, and `shared/jobs.py::meter_live_runs`
skips any job with a `campaign_id` (the `if job.campaign_id` check in that
function). This test exercises cancel billing only; live (pay-as-you-go)
metering of proteina needs that skip addressed in the go-live PR.
