# Save as you go, every non-live tool (phase 2b plan)

Written 2026-10-06 by the hub lead at Leo's request: "plan out the next batch of fixes as save as you go fixes for all other tools".
Nothing here is coded. Status words in this file are plans, not facts.

## Why

Live charging v1 (PR #416, option ii, not merged at time of writing) stops a run when the balance reaches $0 and keeps the designs that already finished.
It is enabled only on the af2, colabfold and esmfold batch tiers and boltz2, because only they upload each design mid-run.
Every other tool keeps the up-front hold until a stop on it also keeps finished designs.
This plan says, tool by tool, what that takes.

## Sources

- Census agents, read-only, at tools-hub `origin/main` 4cf12368 and llm-proteinDesigner `origin/master` 8632f32, 2026-10-06. File:line cites below are theirs.
- Lead spot-checks, same day, main checkout at 4cf12368:
  - `cancel_job` never calls the recovery path: `grep -n reconstruct shared/jobs.py` hits only docstrings (:346, :535). The timeout path does: `recover_stuck_job_result` at `shared/jobs.py:1739-1742`.
  - Modal cancel passes no arguments: `gpu/modal_client.py:619` `modal.FunctionCall.from_id(function_call_id).cancel()`.
  - A result that arrives after the job is terminal is dropped: `webhooks/modal.py:140` and `:225` return `already_terminal`.
  - llm-proteinDesigner at 8632f32 (`git cat-file -p origin/master:<path>`): the rfdiffusion per-design loop is at `docker/rfdiffusion/run_pipeline.py:1939`, and rfantibody's comment says uploads are batched after the loop (`docker/rfantibody/run_pipeline.py:1586-1589`).
- Everything else is labelled UNVERIFIED where the agents could not prove it.

## The three ways to save as you go

- **(a) Watcher.** A background thread in the container uploads each design file as it appears and sends a heartbeat with its scores. Needs the tool to write each design to disk as it finishes. About zero extra GPU.
- **(b) Chunks.** Run the design loop in small chunks (boltz2 does 10 per chunk, `tools/boltz2/run_pipeline.py:120`), upload and heartbeat after each. Costs a model reload per chunk.
- **(c) Collect on stop.** When the stop arrives, kill the GPU process, collect what is on disk, upload it, mark the result partial. Needs the tool to have written finished designs to disk, and needs time to upload after the stop.

(a) and (b) upload during the run, so the hub's existing stop path finds the designs. (c) uploads after the stop, which today is too late (see step 0b).

## Step 0: hub and platform (tools-hub, no GPU)

Starts after PR #416 merges, because it touches the same stop path.

- **0a. A user's own cancel keeps finished designs.** `cancel_job` (`shared/jobs.py:1819-1890`) cancels and settles but never rebuilds the result, unlike the timeout path. Run the same recovery before settling. Size S. Helps every tool that uploads, including the four live ones. Decision for Leo: does a cancelled job with kept designs show as "cancelled, N designs kept" (lead default) or as a partial success?
- **0b. Two-step stop, only if any tool uses (c).** Today the stop cancels, rebuilds from Storage and completes in one tick, so an upload made during the container's teardown lands after the rebuild and is lost; the late result post is dropped as `already_terminal`. Fix: the stop tick cancels and marks the run as stopping; the next tick (about 5 min later) rebuilds and completes. The customer's meter stops at the first tick. Size S/M.
- **0c. Measure Modal's cancel, once.** Cancel uses Modal's default (`terminate_containers` not passed), which the repo's prose says delivers a cancellation to the wrapper so its `finally` runs (`tools/esmfold2_design/modal_app.py:163-170,533-542`). UNVERIFIED from repo code: how long that `finally` may run, and whether a GPU grandchild process dies with the container or keeps running. One throwaway CPU-only Modal function answers both. Cost: cents, but it is Modal spend, so Leo's yes first.
- **Not needed: a "stop" field on the heartbeat reply.** Modal's cancel is already the stop signal, and the silent tools send no heartbeats to reply to.

## Per tool, tools-hub

### esmfold2_design: per-seed saving

- **Today.** One H100 container per seed (`modal_app.py:392-399`); inside a seed, one `design()` call for the whole batch (`run_pipeline.py:1586-1594`). PDBs are written and uploaded only in the tail loop after that call (`run_pipeline.py:1158-1196`, `:924-932`). No heartbeats at all: `WEBHOOK_URL` is set (`modal_app.py:211`) and never read; "Send heartbeats" is a TODO (`run_pipeline.py:100-101`). Upload failures are logged and not counted (`:929-932`).
- **Change.** In the existing upload loop, send one heartbeat per design with its scores (copy boltz2's `send_heartbeat(new_candidate=...)`), and count upload failures in the summary. Finished seeds then survive a stop.
- **Extra GPU.** None.
- **Size.** S/M.
- **Limit.** A run with one seed keeps nothing on a stop. Fixing that needs (b), chunking `design()`: size M, GPU delta UNMEASURED (one measured point, batch 6 = 3185 s), and it changes results, so it needs its own validation run. Deferred.
- **Risks.** Editing `run_pipeline.py` rebuilds the image and can re-resolve the unpinned `transformers @ main` (`run_pipeline.py:286-300`).
- **Paid test.** A run with 2-3 seeds, stopped after the first seed lands; check its designs and scores are on the result page. Leo's per-job yes.

### iggm: watcher

- **Today.** One `design.py` subprocess, no timeout (`run_pipeline.py:423-454`), A100-40GB. Designs found by globbing PDBs afterwards (`:457-458`). A nonzero exit uploads nothing (`_fail`).
- **Change.** (a) a watcher thread that uploads each finished PDB and heartbeats its epitope-contact score.
- **Extra GPU.** About none.
- **Size.** S/M.
- **First check (no GPU).** Does IgGM write each sample as it finishes, or all at the end? UNVERIFIED. Read upstream code or a past raw archive. If all at the end, there is nothing to save as you go: keep iggm on the up-front gate.
- **Risks.** Files share basenames across `sample_*` folders (`:578-580`), so upload names must include the folder. Only pick up a file once its size stops changing.
- **Paid test.** One run stopped mid-way; check kept designs. Leo's yes.

### opendde: collect on stop

- **Today.** One `opendde pred` subprocess, no timeout, H100. All results sorted for ranking before upload (`:487-496`). A nonzero exit uploads nothing. `modal_app.py:143-149` has no `try/finally`, so a wrapper timeout loses even the raw archive.
- **Change.** (c): run the pipeline so the stop reaches it, kill `opendde pred`, then fall through to the existing collect, sort and upload, flagged partial. Add the missing `try/finally`. Needs 0b and 0c.
- **Extra GPU.** None during the run; a short upload after the stop.
- **Size.** M.
- **First check (no GPU).** Does `opendde pred` write each sample to disk as it goes? UNVERIFIED. If not, (c) keeps nothing: keep opendde on the up-front gate.
- **Paid test.** One run stopped mid-way. Leo's yes.

### proteina: chunked search

- **Today.** One `complexa design` subprocess (`run_pipeline.py:4278`), A100-80GB, 6780 s search timeout. Stages run over all designs; scores exist only after the last stage. Uploads happen after the subprocess (`:4556-4892`). Silent during the whole search. A timed-out or failed exit already uploads scored designs as partial (`:4997-5010`), but a hub cancel never reaches that path.
- **Change.** (b): run the search in chunks of 8 through the existing `_run_shard`, upload and heartbeat after each chunk, keep ranks dense. Campaigns already shard proteina at 8 (`shared/compute_campaigns.py:530-531`).
- **Extra GPU.** About 10-20% per run at chunk 8 (agent estimate from a 54.8 s fixed cost per shard, `shard_driver.py:177`, plus a model reload; not measured on a real run). Under live charging the customer pays it.
- **Size.** M/L.
- **Also fixes.** The board's "proteina no design-search heartbeat" and the late overrun notice (#394).
- **Decision for Leo.** Accept about 10-20% more GPU per proteina run, or keep proteina on the up-front gate.
- **Paid test.** One run stopped after the first chunk lands. Leo's yes.

### Stay on the up-front gate for good

- **mpnn.** One A10G subprocess capped at about 10 min (`modal_app.py:39-40`, `run_pipeline.py:662`). No Storage upload at all; results travel inline. Saving as you go means inventing an upload path and chunking for a job that costs cents. Do nothing.
- **Single-fold tiers of af2, colabfold, esmfold.** One design per run; nothing useful exists before it finishes. No uploads or heartbeats on the single path (`af2:1170`, `colabfold:1204`, `esmfold:999` read the webhook only in batch). Do nothing. Optional, size XS, separate: one "folding" heartbeat before the fold, so a user's cancel bills the GPU used instead of $0 (the board's A1).

## Per tool, llm-proteinDesigner

Cites in this section are in llm-proteinDesigner at `origin/master` 8632f32 unless marked tools-hub.

**Shared finding.** In all five, every Storage upload happens after the last long GPU subprocess returns, so a stop before then keeps no structures:

| Tool | GPU work ends | First upload |
|---|---|---|
| bindcraft | `docker/bindcraft/run_pipeline.py:1424` | `:1509` |
| boltzgen | `:1942` | `:2120` |
| pxdesign | `:1767` | `:1922` |
| rfantibody | `:1491` | `:1647` |
| rfdiffusion | `:2081` | `:2406` |

A timeout or crash also uploads nothing in four of them: `run_command` raises and the exception skips the upload code. The raw archive is shipped only after the subprocess returns, with no `try/finally` (`infrastructure/modal/bindcraft_app.py:185-253`, same shape in the others).

**Mid-run uploads key on design name, not rank.** Rank is assigned only after the whole set is sorted. An arbitrary basename is accepted as `pdb_key` (tools-hub `tools/boltz2/run_pipeline.py:868`).

**While in each file (cheap add-on, same PR).** Let a timeout or crash fall through to the existing upload code instead of past it (bindcraft :1424, boltzgen :1942, pxdesign :1767, rfantibody :1490). This fixes loss on failure, not on a stop.

### rfdiffusion: upload inside the existing per-design loop (do first)

- **Today.** RFdiffusion and MPNN run as two batch stages. AF2 validation then runs per design in a real Python loop (`stage_af2_validation`, `:1939-2075`). That loop already sends a `new_candidate` heartbeat with scores per design (`:2042-2075`), but with no file.
- **Change.** In that loop, pick the rank_001 PDB (factor the picker out of `:2360-2411`), upload it, and set `pdb_key` on the existing heartbeat. The final loop then skips names already uploaded.
- **Extra GPU.** None. **Size.** S. **Hub change.** None.
- **Limit.** A stop before AF2 starts (during the two batch stages) keeps nothing.
- **Note.** Memory has "RFdiffusion 2/8 never produced a binder", so a stop test needs a target that yields designs.

### pxdesign: watcher in the existing keepalive

- **Today.** One `pxdesign pipeline` subprocess (`:1767`, 5400 s), about 35 GPU-min per design (`:97-99`). Keepalive every 300 s with progress fixed at "0 of N" (`:1748-1762`).
- **Change.** (a) give the keepalive loop a body: find newly finished `global_run_0/*/seed_*/predictions/converted_pdbs/*.pdb`, upload them, send `new_candidate`, and report real progress.
- **Extra GPU.** None. **Size.** M.
- **First check.** Whether `converted_pdbs/` fills per seed or in one pass at the end is UNVERIFIED (upstream is cloned, not vendored). So is whether scores exist mid-run (`locate_summary_csv` `:1232-1241`). If there are structures but no scores, upload with empty scores; the final post fills them in.

### bindcraft: watcher in the existing heartbeat

- **Today.** One FreeBindCraft subprocess (`:1424`), A100-80GB, up to 23 h session. A 60 s heartbeat thread wraps it (`:1418-1424`) with progress fixed at 0.
- **Change.** (a) on each 60 s tick, upload new `Accepted/*.pdb` that already have a row in `final_design_stats.csv` (reuse `parse_bindcraft_results` `:447-487`), and send `new_candidate`.
- **Extra GPU.** None. **Size.** M.
- **First check.** Whether `Accepted/` and the stats CSV are appended per design is UNVERIFIED (upstream cloned at `docker/bindcraft/Dockerfile.modal:44-46`).

### rfantibody: chunks of about 10

- **Today.** Three batch subprocesses (RFdiffusion → MPNN → RF2, `:1457/:1472/:1490`) pass whole-batch Quiver files between them. Per-design PDBs exist only after RF2 (`extract_pdbs` `:1526`). The code says uploads are batched after the loop (`:1588-1589`).
- **Change.** (b) loop the three stages over chunks of about 10, then extract, upload and heartbeat per chunk, using per-chunk `.qv` paths and output dirs.
- **Extra GPU.** Three model loads per extra chunk. No load time measured for any of them (UNVERIFIED). Measure one RF2 cold load before choosing the chunk size.
- **Size.** M/L.
- **Also.** Ranking within a chunk changes what counts as top-N; rank over all chunks at the end. This touches the board's "rfantibody progress mixes units" and "rfantibody campaign chunk" items.
- **Decision for Leo.** Same as proteina: accept extra GPU per run, or stay on the up-front gate.

### boltzgen: stay on the up-front gate

- **Why.** Each stage needs the whole set: refolding takes the top `budget` of all designs (`:14-21`). A watcher would only see unrefolded intermediates, a weaker deliverable than promised. Chunking turns "top 5 of 60" into "top 1 of 12, five times", which is a different scientific result.
- **Do only.** The cheap add-on above: on a timeout or crash, fall back to the intermediate ranked designs that `find_design_files` (`:1305-1363`) already accepts, labelled as not refolded. Size S.

### Not adopted: show score-only rows instead

The census suggested surfacing heartbeat scores without structures as a hub-only alternative. That gives little here: four of the five send their per-design heartbeats only after the GPU work ends (bindcraft `:1475-1545`, boltzgen `:2015-2083`, pxdesign `:1825-1891`, rfantibody `:1543-1611`), so a stop mid-run has no scores either. rfdiffusion already streams scores, and its S change adds the files.

## First checks without GPU spend

Several picks depend on whether an upstream tool writes each design to disk as it finishes. The open questions:

- IgGM samples;
- `opendde pred` outputs;
- BindCraft `Accepted/` and its stats CSV;
- PXDesign `converted_pdbs/`;
- whether Quiver files can be read mid-write.

Two ways to answer them before any paid run:

1. Read the upstream code: each tool is a public repo, cloned in its Dockerfile.
2. List file times inside a past run's raw archive on its Modal volume. Times spread across the run mean the files are written progressively. Whether archived files keep their original times is UNVERIFIED; Python's tarfile keeps them by default.

## Order

1. **rfdiffusion** (S, no GPU, no hub change). It lives in llm-proteinDesigner, so it can start now. How that repo deploys its Modal apps is UNVERIFIED; that is the worker's first check.
2. **esmfold2_design** (S/M, no GPU). Container only, so it can start now.
3. **0a**, a user cancel keeps designs. After #416 merges.
4. **pxdesign, bindcraft, iggm** watchers (M, M, S/M), each after its first check passes.
5. **rfantibody and proteina** chunking (M/L each), after Leo's GPU decision and a measured model load.
6. **opendde**, with 0b and 0c, only if its first check passes.
7. **Stay gated:** boltzgen (timeout add-on only), mpnn, and the single-fold tiers.

Each tool goes through the same steps:

1. Its own branch and PR.
2. The saved `review-code` and `review-claims` agents on the delta before push.
3. One image rebuild.
4. One paid stop test, with Leo's per-job yes.
5. A one-line add to the live-charging tool list.

A tool joins live charging only after its stop test shows kept designs on the result page.

## Open decisions for Leo

1. Start rfdiffusion and esmfold2_design now (one worker, the free slot), or after live charging lands.
2. 0a: a cancelled job with kept designs shows as cancelled with designs kept (lead default) or as a partial success.
3. proteina and rfantibody: accept extra GPU per run for chunking (proteina about 10-20%, estimated; rfantibody unmeasured), or stay on the up-front gate.
4. 0c: cents of Modal spend to measure the cancel window.
