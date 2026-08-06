# tools-hub open-items audit — 2026-07-22

Post-merge audit after the "everything is a campaign" rework (PR #93, `c6f7104`) shipped.

Method: 4 independent survey lenses (plan-vs-shipped, runtime-risk, repo-state, registers) then adversarial refute-by-default verification, then synthesis. Multi-agent workflow, 56 agents, 3.15M tokens.

Lens tallies: plan-vs-shipped: 12 candidate open items; runtime-risk: 9 candidate open items; repo-state: 6 candidate open items; registers: 24 candidate open items; 51 candidate items total, verifying each; 24 of 51 survived refutation


---

## Synthesis (ranked briefing)

## What is still open after the "everything is a campaign" merge

24 raw findings collapse to 16 distinct items. Five pairs were found independently by two different lenses and are merged below (noted inline) — those are the ones with the strongest evidence behind them.

---

# 1. Needs a decision or action from you

**1. esmfold2_design multi-seed jobs are under-billed by up to 64x.**
`tools/esmfold2_design/modal_app.py:512` accumulates `max_runtime = max(...)` across child seeds and returns it as `runtime_seconds` at :549, so `complete_job` (`shared/jobs.py:876-881`) settles a 64-seed run as one container: roughly $14.79 charged against up to ~$946 of real H100 time. Only the `n_seeds > 1` branch is affected; `n_seeds == 1` and opendde are fine. PR #82 fixed the hold, not the charge.
*Next:* change :512 to sum child runtimes (keep max separately if the UI wants wall-clock), add a regression test asserting settle `gpu_seconds == sum` for a multi-seed umbrella, redeploy the Modal app (not just Railway).

**2. Seven call sites read `result["candidates"]` raw instead of `candidate_records()`, and it is already breaking live tools.**
Confirmed sites: `blueprints/jobs.py:659` (CSV export), `:746`, `:889` (zip), `blueprints/lab_projects.py:124` (new campaign shortlist staging) and `:221` (legacy single-job), `shared/refold.py:107`. For the five designs-only tools (af2, colabfold, esmfold, boltz2, iggm) whose result carries `designs[]` and no `candidates[]`, CSV export is header-only and lab staging silently stages zero PDBs with no error. Two lenses filed this separately (campaign lab-handoff, and the Option A registers item); it is one root cause. Bounded upside: the handoff campaign row and `candidate_refs` are still written correctly, so designs remain resolvable from the source job; the loss is the durable bucket copy and the export.
*Next:* replace each raw read with `candidate_records(job.result)` (`shared/jobs.py:79-105`), plus a test that every tool's stored shape yields non-empty rows through the export path.

**3. Campaign fan-in has no row limit and will silently truncate at the PostgREST cap.**
Also found twice. `shared/compute_campaigns.py:718-739` selects every succeeded sub-job's `result` with no `.limit()`/`.order()`/`.range()`, and `supabase/config.toml:18` sets `max_rows = 1000` against `MAX_SUBJOBS_PER_CAMPAIGN = 50000`. Four call sites, and the dangerous one is `blueprints/campaigns.py:669`: the Boltz-2 validation refold spends real GPU on what it believes is the global top-N. The same unbounded pattern predates the rework at `blueprints/campaigns.py:509` (`_campaign_passed_filters`), which runs on the 5s `status.json` poll for the life of every running campaign, so it is hotter than the new path. Latent, not yet corrupting: canaries were 32-48 designs.
*Next:* two decisions. (a) Confirm prod's Settings > API > Max rows in the Supabase dashboard. (b) Adding `.limit()` does not fix it (PostgREST clamps to db-max-rows) — this needs a `.range()` pagination loop like `shared/jobs.py:1595`, ideally reducing into a top-N heap plus a running count rather than materializing `merged`. Cover both the new aggregator and the pre-existing `_campaign_passed_filters`.

**4. proteina `motif_ame` is live and selectable but was never canaried; BYO custom-target takes money before refusing.**
`FLAG_TOOL_PROTEINA` is on and `templates/runs/new.html:68` offers motif_ame in the variant dropdown with no exclusion in `blueprints/campaigns.py`, while `tools/proteina/__init__.py:88-89` still says to canary it first (its upstream reward_model block is commented out). Separately, `blueprints/campaigns.py:321-360` accepts and stages a proteina target upload and `new.html:74` invites it, but the rejection only happens in-container at `tools/proteina/run_pipeline.py:659-676` — campaign created, wallet hold placed, shard dispatched, then refused.
*Next:* two cheap route-level gates (mirror the existing iggm/affinity_maturation exclusion; drop the upload affordance for proteina), or spend ~$1 on a motif_ame one-shard canary if you would rather validate than gate.

**5. Campaign exports are capped at 300 and the banner says the opposite.**
Found twice. `_CAMPAIGN_EXPORT_LIMIT = 300` (`blueprints/campaigns.py:546`, used at :567) equals the on-page cap, and the banner at `templates/runs/detail.html:71-74` says "Use the export buttons to download all" — which is gated on `candidates_capped` (total > 300), so that sentence is false 100% of the times it renders. Not data loss: per-sub-job exports at `blueprints/jobs.py:648-686` and `:865` are uncapped, so the designs are recoverable one sub-job at a time.
*Next:* pick one. Either raise or drop the cap for CSV/FASTA (the memory rationale in the decision-6 comment only really applies to the ZIP path that pulls PDB bytes), or keep the cap and fix the copy plus emit an explicit truncation marker (CSV trailing row, FASTA `# top 300 of N`, ZIP README, `_top300` filename).

**6. Customer-data deletion has no implementation at all.**
`shared/storage.py:174` `delete_input` has zero call sites, there is no retention CLI or cron, and three buckets accumulate: `tool-inputs` (0006), `tool-outputs` (0021), `lab-campaigns` (0011, now also fed by `stage_campaign_candidates`). Terms (`templates/legal/terms.html:64-67`) state a 90-day floor with optional deletion, so this is not a literal breach — but `privacy.html:92-96` offers deletion on request with nothing behind it, and account deletion does not reach Storage since `ON DELETE CASCADE` stops at the DB. Note migration 0021's comment plans 30 days while Terms say 90.
*Next:* decide 30 vs 90 first, then build one sweeper covering all three buckets and a per-user erasure path. Predates the rework; not urgent, but it is a decision only you can make.

**7. No route-level tests for any of the three new endpoint families.**
Nothing in `tests/` exercises `/campaigns/<id>/export.csv|fasta|zip` (`blueprints/campaigns.py:610-625`), `/campaigns/<id>/refold` (:628), or the `_submit_campaign_shortlist` branch (`blueprints/lab_projects.py:62-135`); no cross-user IDOR test at the route layer; no test for the new `standalone_only=True` filter (`shared/jobs.py:1554/1590`) that powers the unified feed. One correction to what you may have been told: template rendering IS covered — `tests/test_runs_detail_template.py::test_merged_results_render_in_campaign_mode` asserts campaign-scoped export links, per-candidate 3D resolving to the source sub-job, the provenance chip, and `source_campaign_id`. The residue is the route tests, which never needed live credentials.
*Next:* add the route tests. The authenticated browser walk is still separately unrun, but it only owes you live-runtime confirmation (Mol\* painting, bytes streaming), not logic coverage.

---

# 2. Housekeeping

**8. Merged CSV/FASTA lose sub-job provenance and emit colliding keys and non-monotonic ranks.** `shared/exports.py:47-66` and :69-103 carry no `_source_chunk`/`_source_job_id` columns even though the aggregator tags them (`shared/compute_campaigns.py:756-763`) and the ZIP namespaces by them (`exports.py:132-140`). Because the aggregator globally re-sorts while each row keeps its local rank, the campaign CSV's `rank` column contradicts row order. FASTA ids also interpolate the `designs/` prefix, giving `>rank1_designs/design_1.pdb`. Fix is confined to `shared/exports.py` plus fixtures: emit source columns, namespace `pdb_key` to match the ZIP arcname, assign a global rank.

**9. Homepage "Recent runs" still shows campaign sub-jobs as standalone runs.** `blueprints/public.py:113` calls `list_jobs_for_user`, which has no campaign filter (`shared/jobs.py:1526-1543`), and each card also offers "Clone and re-run" against a single chunk (`templates/index.html:254`). Not broken links, just the pre-rework mental model leaking. Switch to `list_jobs_paginated(..., standalone_only=True)` merged with `list_campaigns_for_user`, relabel the eyebrow, drop the per-chunk clone.

**10. iggm's merged campaign table renders em-dashes in its primary metric column.** `templates/tools/iggm_results.html:24-39` reshapes root-level `n_epitope_contacts` into `scores.epitope_contacts`; the aggregator skips that reshape, so `candidate_table.html:268/306` finds nothing and `result_columns.candidate_metric` returns None, meaning the declared primary metric orders nothing. Currently invisible because `FLAG_TOOL_IGGM` is off. Move the reshape out of the template into shared code, and add the missing test that `_TOOL_RESULT_COLUMNS` (`shared/result_columns.py:24-48`) matches each partial's column list AND that each primary metric key resolves against `candidate_records()` output.

**11. Campaign cards show a status badge but no n/N progress.** `templates/runs/list.html:48-50`. A per-card `get_progress_counts` is an N+1 across up to 100 rows, so it needs a batched group-by. A passed-filter count is not viable on a list page without a denormalized column — I would drop that half. UX only.

**12. `/campaigns` copy still describes only fan-out.** The blurb at `templates/runs/list.html:31-37` defines a campaign as splitting into GPU sub-jobs while the same page now renders single-run cards at :52-64, and the empty state at :67-70 routes new users to the fan-out form (restricted to `_visible_campaign_tools()`, `blueprints/campaigns.py:191-202`). The nav does offer a path to single runs via `_header.html` and `_footer.html:31`, so this is copy plus one CTA in one template.

**13. Schedule the Modal raw-capture reaper.** `llm-proteinDesigner/scripts/prune_raw_volumes.py` (origin/master `31e3e20`, not in tools-hub), 13 `ranomics-<tool>-raw` volumes, DEFAULT_DAYS=90, dry-run unless `--apply`, needs the Python313 interpreter that has modal. This is Lane B internal tars only; it does not close item 6.

**14. Two canaries still owed.** proteina 1-shard functional canary (~$1, no top-up) driven to natural terminal, since the settle path (PR #92) and the entire results/aggregation path (PR #93) are both newer than the last live proteina run, which was cancelled mid-flight. rfantibody 1-shard functional canary (<=16 designs, one cushioned hold of $52.44) — the shared drain engine is already validated, so it only needs to prove chunking, timeout headroom, and score parsing. Re-read the live wallet balance before sizing the top-up; the $34.10 figure in the register is a 2026-07-13 snapshot.

**15. pytest has no config file at all.** Not a missing `testpaths` key — there is no pytest.ini/pyproject/setup.cfg/tox.ini tracked. Verified: `python -m pytest -q --ignore=tests` exits 0 with 10 passed from `tools/library_planner/tests`. CI (`.github/workflows/pytest.yml:89`) runs bare pytest from root, so a moved or ignored `tests/` would go vacuously green, and any stray `test_*.py` in scratch/tmp/runs would be swept into the gating suite. Pre-existing infra debt.

---

# 3. Known and accepted

**16. No live reveal of partial results.** Found twice. `status.json` (`blueprints/campaigns.py:477-506`) returns counts only, so a page left open never gains designs until the terminal `window.location.reload()` at `detail.html:169-171`. Corrections worth keeping: partials are not withheld from a running campaign (the aggregator runs regardless of status, so any reload shows what has landed), the "reload to load new designs" hint at :82 only renders when zero candidates exist, and shortlist selections survive reload via sessionStorage (`static/js/candidate_table.js:2,26`). The related "passed filters reads zero while children hold passing designs in `inputs._partial_candidates`" is an unfinished enhancement, not a regression — `git show 20023ca -- templates/runs/detail.html` proves the pre-rework page had no results panel at all. A real fix needs a new candidates endpoint or an HTML-fragment refetch, not a JS tweak.

**Roadmap, deliberately not built:** Phase A esmfold2_design campaign fan-out (iggm half is already merged in `5c4bd2f`/`ac277f1`; only the `FLAG_TOOL_IGGM` flip may remain), Phase B fold-tool batch sharding (tool side already landed in `54d0365`; engine side untouched), and the public self-serve proteina free-validate route (an unmade product/abuse decision, not a defect).

---

## Genuinely fine, for the record

The merge itself is clean: migration 0037 applied, Railway verified, 1425 tests green, CI green, and the three pre-merge adversarial fixes hold. The merged campaign table is index-safe — each row carries `_source_index` (`shared/compute_campaigns.py:762`) and the star button emits `data-ref-idx` from it, so the classic reorder-vs-selection mismatch does not apply there. proteina and the five original binder tools are unaffected by the shape bug in item 2 because they emit canonical `candidates`. opendde is unaffected by the settle bug in item 1. The docstring claim that the aggregator transfers no structure bytes is accurate; the problem is row count, not payload size. And no plan document in the repo was violated by any of this — several of these items were framed as broken promises, but `docs/` contains only COMPUTE-CAMPAIGNS-PLAN.md and PHASE-2-PLAN.md and neither speaks to them. They stand on internal contradiction and code evidence alone, which is the stronger footing anyway.


---

## Raw verified survivors (24)

Each survived an adversarial refutation pass. `detail` may correct the original claim.


### 1. Campaign aggregator fetches every sub-job's result JSON unbounded (no limit, no order, no pagination)
- **severity:** high | **owner:** code | **lens:** plan-vs-shipped
- **detail:** Two parts of the framing are wrong and should not be repeated.

(1) "Single hottest NEW code path" is false. The hottest unbounded full-`result` fetch is _campaign_passed_filters in blueprints/campaigns.py (~line 509): client.table("tool_jobs").select("result").eq("campaign_id",...).eq("status","succeeded").execute(), also with no limit/order — and it runs on EVERY status.json poll, which templates/runs/detail.html fires every 5s for the life of a running campaign. That one predates the rework (introduced 2026-07-13 in 23cb868, moved by e7c9b42), so the unbounded-result pattern is pre-existing and has been live for about a week without a reported incident. The new aggregator runs once per detail page render, once per export, once per refold — the detail page's 5s poll hits status.json, which does NOT call the aggregator. So the new path is real but strictly colder than the pre-existing one. Any fix should cover both.

(2) "Plan item 1 promised the fan-in stays cheap" is unverifiable — there is no rework plan doc in the repo (docs/ has COMPUTE-CAMPAIGNS-PLAN.md and PHASE-2, neither covers this). The only in-repo cheapness claim is the aggregate_campaign_candidates docstring, and it is narrowly accurate: "Reads only the metadata columns (result already stores PDBs as Storage refs, so no structure bytes are transferred here)" — i.e. it claims no structure bytes, not O(1) rows.

Also note the silent-truncation half of the claim is unverified: whether the Supabase project actually has db-max-rows set was not checkable from this session. That should be confirmed in the dashboard (Settings > API > Max rows) before the "total/ranking/export are quietly wrong" failure mode is asserted as live.

Finally, scoping the fix: adding .limit()/.range() alone does not solve it. The function deliberately builds the full `merged` list of every candidate in the campaign so it can rank globally and report `total` and `capped`; the O(N) is inherent to global ranking. A real fix needs streaming/paged aggregation (page with .range() + .order("chunk_index") and reduce incrementally, keeping only a top-N heap and a running count) or a DB-side aggregate — a design change, not a one-liner.
- **evidence:** shared/compute_campaigns.py:718-739 — client.table("tool_jobs").select("id,chunk_index,attempt,result").eq("campaign_id", campaign_id).eq("status","succeeded").execute() with no .limit()/.order(); called from blueprints/campaigns.py compute_campaign_detail (limit=300), _campaign_export, and compute_campaign_refold

### 2. Verification checks 2-7 never executed; no route-level tests for the three new endpoint families
- **severity:** high | **owner:** ops | **lens:** plan-vs-shipped
- **detail:** The surviving, non-duplicate open item is narrower than stated: **no route-level tests exist for the three new endpoint families** — `/campaigns/<id>/export.csv|fasta|zip` (blueprints/campaigns.py:610-625), `/campaigns/<id>/refold` (:628), and the `_submit_campaign_shortlist` branch of `/lab-projects/submit` (blueprints/lab_projects.py:62-135) — plus no cross-user IDOR test at the route layer and no test for the new `standalone_only=True` filter (shared/jobs.py:1554/1590) that powers the unified /campaigns feed. Three corrections to the claim as written: (1) the claimed grep evidence is wrong in detail — the four-symbol grep across tests/ returns nothing at all, not "only test_campaign_results.py"; (2) verification check 2 is NOT entirely unverified — tests/test_runs_detail_template.py::test_merged_results_render_in_campaign_mode renders runs/detail.html in campaign mode with the real app Jinja globals and asserts campaign-scoped `/campaigns/camp-smoke/export.zip`, per-candidate 3D resolving to the source sub-job (`/api/jobs/job-bbbbbbbb/pdb/`), the `cand-subjob-tag` provenance chip, `/campaigns/camp-smoke/refold`, and `name="source_campaign_id"`; what remains unverified there is only live runtime behavior (Mol* actually painting, downloads actually streaming bytes); (3) the "checks 2-7 required an authenticated session and were never run" half restates the already-known fact that the authenticated browser walk never happened — the genuinely actionable residue is the automated route tests, which did not need live credentials.
- **evidence:** tests/test_campaign_results.py — test list is columns/metric, csv, fasta, zip x2, aggregate merge-sort-dedupe, aggregate cap, aggregate ownership, refold boltz2 antigen x2, _parse_candidate_refs. grep for 'compute_campaign_export|compute_campaign_refold|_submit_campaign_shortlist|standalone_only' across tests/ returns only test_campaign_results.py (which references none of those routes)

### 3. Campaign exports are silently truncated at 300 and the on-page banner claims the opposite
- **severity:** medium | **owner:** code | **lens:** plan-vs-shipped
- **detail:** Two corrections to the claim. (1) The 'Decision 6 required the cap to be noted with no silent truncation' framing is not supported by the repo — grep -rn 'decision 6' hits only the code comment at blueprints/campaigns.py:545, which explains why the page and export caps are deliberately shared (so a download matches the ranked table and a large campaign cannot bundle unbounded PDBs into memory); no plan doc in docs/ states a no-silent-truncation requirement. The defect stands on internal contradiction alone, not on a violated written decision. (2) The claim understates the severity: because the banner is gated on candidates_capped (total > 300) and the export cap is also 300, the sentence 'Use the export buttons to download all' is false 100% of the times it is displayed — there is no campaign size for which it is true. Also worth scoping: job-level exports at blueprints/jobs.py:650/668/867 are uncapped (they serialize the whole job result), so this is a defect specific to the new campaign export routes, not an inherited pattern. Minimum fix is either (a) drop the export cap (or raise it well above the page cap) for CSV/FASTA while keeping the ZIP bounded for the memory reason, or (b) keep the shared cap and correct the banner plus emit an explicit truncation notice (CSV trailing comment row / FASTA '# top 300 of N' header / ZIP README.txt) and a '_top300' filename marker.
- **evidence:** blueprints/campaigns.py:_CAMPAIGN_EXPORT_LIMIT = 300 (comment: 'the on-screen merged table and every campaign export share one top-N cap'); templates/runs/detail.html:71-74 'Use the export buttons to download all.'; shared/exports.py candidates_to_csv/fasta/zip emit no cap notice

### 4. Campaign cards on the unified list lack the promised progress n/N and passed count
- **severity:** medium | **owner:** code | **lens:** plan-vs-shipped
- **detail:** Accurate on the facts, with two corrections to severity and scope. (1) The list is not blind to progress: the campaign card does render a status badge (pending / funded / running / completed / failed / cancelled / timeout, styled at list.html:19-22), so coarse state IS answerable without opening the campaign. What is missing is granular completed/total and any hit/passed signal. (2) The two halves of the claim have very different feasibility. n/N progress is cheap-ish: one get_progress_counts(campaign_id) per campaign, which is indexed head COUNTs (~6 buckets) but becomes an N+1 across up to 100 listed campaigns, so it needs a batched group-by rather than a per-row call. A passed-filter count is far more expensive - it would require per-campaign fan-in of every sub-job's result artifacts via aggregate_campaign_candidates, which is not viable on a list page without a stored rollup column. The realistic open work is therefore 'add batched n/N progress to campaign cards', with the passed count either denormalized onto compute_campaigns or dropped. Impact is UX/informational only: no correctness, billing, or data-integrity defect, and the detail page one click away shows both.
- **evidence:** templates/runs/list.html:48-50 — '{{ item.tool }} · {{ item.requested_designs }} designs · {{ item.total_subjobs }} sub-jobs · budget ${{ ... }}'; no get_progress_counts or passed-count data is passed to the template (blueprints/campaigns.py compute_campaigns_list only builds entries from list_campaigns_for_user + list_jobs_paginated)

### 5. Homepage 'Recent runs' feed not converted — campaign sub-jobs still surface as standalone runs
- **severity:** medium | **owner:** code | **lens:** plan-vs-shipped
- **detail:** Accurate as written, with three refinements. (1) No in-repo plan document records "plan item 7" — the only relevant docs are docs/COMPUTE-CAMPAIGNS-PLAN.md / PHASE-2-PLAN.md and neither mentions the homepage strip; the plan item exists only in the prior session, so treat the claim's provenance as the session plan, not a committed artifact. The substance is independently verifiable from the diff regardless. (2) Line range for list_jobs_for_user is shared/jobs.py:1526-1543 (not 1545). (3) Severity is inconsistency, not breakage: /jobs/<id> remains a working authenticated page, so the cards are not dead links. One extra mismatch the claim omits: each card also renders a per-chunk "Clone & re-run" link (templates/index.html:254) pointing at tools.tool_form with clone_from=<chunk job id>, which would clone a single campaign chunk's inputs as a standalone run — a second place the pre-rework per-job mental model leaks onto the homepage. Fix is small: add a standalone_only (or campaign_id IS NULL) filter to list_jobs_for_user or switch the homepage to list_jobs_paginated(..., standalone_only=True) merged with list_campaigns_for_user, and relabel the eyebrow.
- **evidence:** templates/index.html:236 '<span class="section-eyebrow">Recent runs</span>'; blueprints/public.py:113 recent_jobs = list(list_jobs_for_user(ctx.user_id, limit=3)); shared/jobs.py:1526-1545 list_jobs_for_user has no campaign_id filter

### 6. Poller does not reveal or refresh results as sub-jobs finish
- **severity:** medium | **owner:** code | **lens:** plan-vs-shipped
- **detail:** Accurate on the mechanism, but two corrections. (1) Partial results are NOT withheld from a running campaign: compute_campaign_detail (blueprints/campaigns.py:429-446) calls aggregate_campaign_candidates regardless of campaign status, so any page load mid-run renders the designs delivered so far. The gap applies only to a page left open, not to a page reloaded. (2) The claim says the running-state placeholder instructs a manual reload, which is true, but that placeholder (detail.html:80-83) only renders when zero candidates exist. As soon as one candidate exists the results panel replaces it and there is no reload hint anywhere on the page, so the user most affected (watching 40 of an eventual 400 designs) receives no signal that more have arrived. Also note the fix is not a JS-only tweak: status.json returns no candidate data, so live reveal requires either a new candidates endpoint or an HTML-fragment re-fetch. Adversarially checked and cleared: the terminal window.location.reload() does not destroy shortlist selections, which persist in sessionStorage (static/js/candidate_table.js:2,26).
- **evidence:** templates/runs/detail.html:82 'This page tracks progress live; reload to load new designs.'; templates/runs/detail.html:169-171 'if (WAS_RUNNING) { window.location.reload(); }' inside the terminal branch of poll()

### 7. Campaign lab-handoff stages PDBs by a raw result['candidates'] index while provenance indices come from shape-normalized candidate_records
- **severity:** medium | **owner:** code | **lens:** plan-vs-shipped
- **detail:** The described trigger is half wrong; the surviving trigger is the "designs" fallback, not the legacy wrapper. (1) The legacy result.output.candidates wrapper path is effectively unreachable for campaign sub-jobs: the unwrap-at-write fix landed 2026-05-29 (016c95e / f0c90e6) and campaigns first shipped 2026-07-03 (89ff9d2 / 30828a3), so no campaign child row can carry the wrapped shape. That half of the claim is theoretical. (2) The concrete, live trigger is a campaign tool that emits designs[] with no candidates[]: iggm does exactly this today (SUPPORTED_TOOLS includes it; flag-gated behind FLAG_TOOL_IGGM, canary already run). This is not a hypothetical "future tool". proteina writes BOTH designs and candidates and candidate_records prefers "candidates", so proteina stays index-aligned and is unaffected; the five original binder tools (rfdiffusion/bindcraft/boltzgen/pxdesign/rfantibody) emit candidates and are unaffected. (3) The claim frames this as campaign-only; the identical raw read exists in the pre-existing legacy single-job path at blueprints/lab_projects.py:~213 (candidates = (job.result or {}).get("candidates", [])), so the one-line fix should replace both reads with candidate_records(job.result). (4) Severity is bounded: the handoff campaign row and its candidate_refs are still created correctly, so staff can still resolve designs from the source job; the loss is the durable PDB copy in the lab-campaigns bucket, silently.
- **evidence:** shared/compute_campaigns.py:756 'for local_idx, cand in enumerate(candidate_records(r.get("result")))' vs blueprints/lab_projects.py _submit_campaign_shortlist 'candidates=(job.result or {}).get("candidates", [])'; shared/jobs.py:79-105 candidate_records normalizes output-wrapped rows and falls back to 'designs'

### 8. result_columns.py duplicates the per-tool column lists in the results partials with nothing keeping them in sync
- **severity:** low | **owner:** code | **lens:** plan-vs-shipped
- **detail:** Not a purely hypothetical future-drift / missing-test item. The sync test is genuinely absent (nothing in tests/ compares shared/result_columns.py:24-48 against the templates/tools/<tool>_results.html column lists), but the divergence already exists for iggm and produces a broken merged campaign table today. templates/tools/iggm_results.html:24-39 builds its candidate rows by renaming the pipeline's root-level n_epitope_contacts (emitted by tools/iggm/run_pipeline.py:561 under result["designs"]) into scores.epitope_contacts; the campaign aggregation in shared/compute_campaigns.py:751-760 skips that reshape entirely and passes raw designs rows through blueprints/campaigns.py:439-441. Because templates/components/candidate_table.html:268/306 reads only cand.scores[col], every iggm candidate shows an em-dash in the epitope_contacts column, and shared/result_columns.py candidate_metric returns None for every row so the declared primary metric ("epitope_contacts", "desc") never orders anything. The six other tools are safe because their partials pass output["candidates"] through unmodified with scores already nested. Fix is two-part: (1) normalize iggm rows in aggregate_campaign_candidates (or move the reshape out of the template into shared code both paths use), and (2) add the missing test asserting _TOOL_RESULT_COLUMNS matches each partial's {% set columns %} AND that each tool's primary metric key is actually resolvable from the shape candidate_records() returns. Currently gated by FLAG_TOOL_IGGM being off, so it is not user-visible in prod until that flag flips.
- **evidence:** shared/result_columns.py:24-48 duplicates the '{% set columns %}' lines at templates/tools/rfdiffusion_results.html:6, bindcraft:6, boltzgen:6, pxdesign:6, rfantibody:10, proteina:11, iggm:40; no test references _TOOL_RESULT_COLUMNS against the templates

### 9. /campaigns page copy and empty state still describe only the fan-out product
- **severity:** low | **owner:** product | **lens:** plan-vs-shipped
- **detail:** Copy mismatch is real but the "no path from here to launch a single run" part is wrong. templates/base.html:99 includes _header.html on every page, and _header.html renders "Tools" (public.index) and "All tools" (tools.tools_comparison) links, plus _footer.html:31 links "My runs" -> jobs.jobs_list (/jobs still exists as a route). So a user on /campaigns does have a global-nav path to launch a single run; what is missing is an in-page CTA. The genuine open item is narrower: (1) the heading blurb at templates/runs/list.html:32-35 defines a campaign purely as fan-out ("splits your request into many GPU sub-jobs, runs them in parallel") while the same page now renders "single run" cards below it (loop branch at :52-64), so the copy contradicts the page contents; (2) the empty state at :68 says "No campaigns yet" and its only CTA routes to campaigns.compute_campaign_new, whose form is restricted to _visible_campaign_tools() (blueprints/campaigns.py:191-202), so a brand-new user wanting a single ESMFold/AF2/MPNN fold is pushed toward the fan-out form. Note the empty state is only reachable when the user has zero campaigns AND zero standalone jobs; a user with only single folds sees the cards, not the empty state. Scope is copy plus one extra CTA in a single template.
- **evidence:** templates/runs/list.html:31-37 (h1 'Campaigns', blurb 'splits your request into many GPU sub-jobs', 'New campaign' button) and :67-70 ('No campaigns yet')

### 10. Campaign fan-in query has no row limit and hits the PostgREST 1000-row cap
- **severity:** high | **owner:** code | **lens:** runtime-risk
- **detail:** Accurate as written, with two corrections. (1) There are FOUR affected call sites, not three exports: blueprints/campaigns.py:431 (run detail page, limit=300), :566 (export csv/fasta/zip, limit=_CAMPAIGN_EXPORT_LIMIT=300), and :669 (the top-N Boltz-2 validation refold, limit=max(n*2, MAX_REFOLD_N)). The refold path is the highest-consequence one: it spawns real GPU spend against what it believes are the campaign's globally top-ranked designs, which would silently be the top of a truncated pool. (2) Adding .limit(n) is NOT a fix - PostgREST clamps any requested limit down to db-max-rows, so the fix must be a .range(start, end) pagination loop (the pattern already used at shared/jobs.py:1595), ideally with a log line when a page comes back full. Severity note: no campaign this large has been run yet (canaries were 32-48 designs), so this is latent-at-scale rather than currently corrupting output; the ops half (confirm the prod project's API 'Max rows' setting, Supabase-hosted default 1000) still stands as stated.
- **evidence:** shared/compute_campaigns.py:722-733 (select with no limit/order); supabase/config.toml:18 `max_rows = 1000`; shared/compute_campaigns.py:109 `MAX_SUBJOBS_PER_CAMPAIGN = 50000`; shared/compute_campaigns.py:224 `_BINDCRAFT_CAMPAIGN_CONTAINER_S = 36000  # 10h -> 16 designs/chunk`; shared/compute_campaigns.py:253 `_CHUNK_SIZE_OVERRIDE = {"pxdesign": 24, "proteina": 8, "iggm": 40}`

### 11. Capped-campaign banner tells the user exports contain all designs; they do not
- **severity:** high | **owner:** code | **lens:** runtime-risk
- **detail:** Accurate on the core mismatch, wrong on impact. The banner at templates/runs/detail.html:70-74 does tell the user the exports contain everything while blueprints/campaigns.py caps every campaign export at the same 300 rows shown on screen. However, the claim that there is "NO product path — page, export, or API — to retrieve designs ranked below 300" does not hold: per-sub-job exports are uncapped. blueprints/jobs.py:648-686 (/jobs/<id>/export.csv and export.fasta) and :865 (export.zip) serve the job's full result.candidates list with no limit, and the campaign detail page links "View individual sub-jobs →" on the same screen. So the missing designs are recoverable, just tediously (one download per sub-job, and a large campaign can have many). The real open item is therefore a one-line honesty fix to the capped-campaign copy (or raising/removing the export cap for CSV/FASTA, where the memory-bloat rationale in the decision-6 comment only actually applies to the ZIP path that fetches PDB bytes), not an unrecoverable data-loss hole.
- **evidence:** templates/runs/detail.html:71-74 ("Use the export buttons to download all."); blueprints/campaigns.py:543-546 (`_CAMPAIGN_EXPORT_LIMIT = 300`, comment claims parity is intended) and :566-568 (export calls the aggregator with that limit)

### 12. Merged CSV/FASTA drop sub-job provenance and emit colliding keys and ranks
- **severity:** medium | **owner:** code | **lens:** runtime-risk
- **detail:** Substantively accurate; three refinements. (1) The duplicate-rank half is conditional, not universal: candidates_to_csv falls back to cand.get(\"rank\", i+1), so tools whose candidate records omit \"rank\" get the merged enumeration index (globally correct). The duplicate-rank symptom bites only tools that emit a per-job rank (e.g. the shape rebuilt in shared/job_recovery.py:56-68 and :122). The pdb_key collision, by contrast, is unconditional. (2) A related defect the claim does not name: because aggregate_campaign_candidates globally re-sorts merged candidates (compute_campaigns.py:770-775) while each row keeps its LOCAL rank, the campaign CSV's rank column is not merely duplicated but non-monotonic against row order, so a reader who sorts or filters on \"rank\" gets an ordering that contradicts the ranked table shown in the UI. (3) Minor: the FASTA header interpolates the full pdb_key including its \"designs/\" prefix, yielding ids like \">rank1_designs/design_1.pdb\" — a slash inside a FASTA record id, which compounds the downstream-tool rejection risk beyond the duplicate-id issue alone. Scope note: both per-job routes (blueprints/jobs.py:652,670) and campaign routes share these serializers, but only the campaign path merges multiple sub-jobs, so the defect is campaign-only in effect. Fix is confined to shared/exports.py (emit _source_chunk/_source_job_id columns, use a namespaced pdb_key matching the ZIP arcname, and assign a global rank) plus its single-source test fixtures.
- **evidence:** shared/exports.py:47-66 (candidates_to_csv fieldnames) and :69-103 (FASTA header `>rank{rank}_{pdb_key}`) vs shared/exports.py:132-140 (ZIP namespaces by _source_chunk); shared/compute_campaigns.py:756-763 (tags _source_job_id/_source_chunk/_source_index)

### 13. Campaign shortlist staging reads result["candidates"] while the ref index came from candidate_records()
- **severity:** medium | **owner:** code | **lens:** runtime-risk
- **detail:** Narrower and differently caused than claimed. (1) The legacy wrapped shape (result.output.candidates) is NOT part of this bug: ToolJob.from_row applies _normalize_result_shape at shared/jobs.py:218, so job.result is already flat by the time blueprints/lab_projects.py reads it, and a wrapped row resolves identically on both sides. (2) "Different ordering / wrong PDBs" is wrong: _source_index is a per-source-job local index assigned before the merged sort in aggregate_campaign_candidates, so whenever result["candidates"] exists both sides index the same list in the same order. The only possible failure is zero staged, never mis-staged. (3) The sole live trigger is the "designs" fallback, and among campaign SUPPORTED_TOOLS only iggm qualifies (proteina emits BOTH "designs" and "candidates" at tools/proteina/run_pipeline.py:818-819; rfdiffusion/bindcraft/boltzgen/pxdesign/rfantibody use the canonical candidates shape). iggm currently ships behind FLAG_TOOL_IGGM, documented off at shared/compute_campaigns.py:71, so exposure today is gated; esmfold2_design (the next campaign tool per the roadmap) is also designs-only and would trip the same path. (4) The identical one-line defect also exists on the pre-existing legacy single-job path at blueprints/lab_projects.py:221, so this is not new to the rework, only re-copied into it. (5) The StorageError sub-point is accurate: an upload failure raises out of stage_campaign_candidates' own per-index loop (shared/storage.py:255-259), abandoning that job's remaining candidates, and the caller only logs a warning. Fix is one line in each place: pass candidate_records(job.result) instead of (job.result or {}).get("candidates", []).
- **evidence:** blueprints/lab_projects.py:124 `candidates=(job.result or {}).get("candidates", [])`; shared/compute_campaigns.py:756 `for local_idx, cand in enumerate(candidate_records(r.get("result")))`; shared/jobs.py:79-105 (candidate_records normalizes wrapper + falls back to "designs"); shared/jobs.py:43-68 (_normalize_result_shape exists precisely because wrapped rows persist in prod); blueprints/lab_projects.py:130-134 + shared/storage.py:220,238-243 (silent skips)

### 14. Campaign results area stays empty for hours while individual sub-jobs stream designs
- **severity:** low | **owner:** product | **lens:** runtime-risk
- **detail:** Three corrections. (1) rfantibody does not stream per-candidate partials — shared/compute_campaigns.py:227 states its pipeline "streams scores only (a chunk is all-or-nothing)", so for rfantibody both surfaces are equally empty and there is no disagreement; the surface conflict is real for bindcraft (per-candidate streaming, 10h chunks) and to a lesser degree boltzgen (per-candidate streaming, pilot-sized chunks). (2) This is not a regression introduced by the rework: git show 20023ca -- templates/runs/detail.html proves the pre-rework campaign page had no merged results panel at all and merely linked to the sub-jobs page. The rework added the panel; the gap is an unfinished enhancement (campaign-level fan-in of running children's inputs._partial_candidates), not something the merge broke. (3) The panel is not stuck for a day regardless of user action — a manual page reload re-runs aggregate_campaign_candidates and picks up any newly-succeeded chunk, and the empty state links "View individual sub-jobs" straight to the streaming surface. The window of emptiness is roughly one chunk duration (~8h for bindcraft at concurrency 16), not the full campaign. The sharpest statement of the open item is: the campaign "Passed filters" counter and results table both read zero during the first wave even though passing designs already exist in inputs._partial_candidates on the running children.
- **evidence:** blueprints/campaigns.py:477-506 (status payload: counts, hits, terminal, paused — no candidates); templates/runs/detail.html:80-83 ("reload to load new designs"); shared/compute_campaigns.py:224,229 (`_BINDCRAFT_CAMPAIGN_CONTAINER_S = 36000`, rfantibody same); blueprints/jobs.py:298-317 (per-job partials stream)

### 15. esmfold2_design multi-seed settle UNDER-BILL (charge half of PR #82)
- **severity:** high | **owner:** code | **lens:** registers
- **detail:** Mechanism confirmed, but the dollar figures in the claim are wrong. esmfold2-design runs on H100 at $0.002417/s with a 1.70x markup and a 3600 s per-container session cap (_MAX_SESSION_S), so ONE worst-case seed is ~$14.79, not ~$3.70 ($3.70/hr is the A100-80GB rate). The under-bill factor equals n_seeds (up to 64x): a 64-seed max-length run is charged ~$14.79 instead of up to ~$946, a loss of ~$931 on a single job. Scope refinements: (a) only the n_seeds > 1 branch is affected — n_seeds == 1 is a pass-through of the child's own return and bills correctly; (b) both the inline-poll and webhook terminal paths are affected, because complete_job (shared/jobs.py:876-881) reads runtime_seconds off the same result dict; (c) opendde also exposes an n_seeds field but is NOT affected (seeds run sequentially inside one container). Fix is a one-line change at tools/esmfold2_design/modal_app.py:512 (sum child runtime_seconds instead of max) plus a separate display field if the UI wants wall-clock; needs a regression test asserting settle gpu_seconds == sum for a multi-seed umbrella, since no test currently touches the settle amount for this tool. Requires redeploying the Modal app, not just the Railway web service.
- **evidence:** project_tools_hub_campaign_expansion.md:41 — "**esmfold2 multi-seed settle UNDER-BILL (task #23, unverified):** `modal_app._aggregate` returns `runtime_seconds = max(child runtimes)` not the sum → a multi-seed job may be CHARGED for one container (~$3.70) not N (~$946). PR #82 fixes the HOLD; this is the CHARGE half."

### 16. Lane-A customer-data retention sweeper never shipped (90-day Terms promise unenforced)
- **severity:** high | **owner:** code | **lens:** registers
- **detail:** Still open, but recharacterize. (1) Not a broken Terms promise: `templates/legal/terms.html:64-67` and `templates/legal/privacy.html:69-71` both say inputs/outputs are "retained for ninety (90) days after the job completes, after which they MAY be permanently deleted" — a retention floor plus an optional deletion right, not a commitment to delete. Retaining past 90 days is not a literal breach. (2) The real gap is that no deletion path exists at all: `delete_input` (shared/storage.py:174) is dead code with zero call sites, there is no storage-retention CLI command or cron, and `privacy.html:92-96` offers users deletion-on-request with no code mechanism behind it — DB-level `ON DELETE CASCADE` on auth.users does not reach Supabase Storage, and the app has no account-deletion feature. (3) Three buckets accumulate un-reaped customer artifacts, not two: `tool-inputs` (0006), `tool-outputs` (0021), and `lab-campaigns` (0011) — the campaign rework's `stage_campaign_candidates` writes additional shortlisted PDB copies into the third. (4) Severity is retention hygiene / GDPR-erasure readiness / storage cost, not an active privacy exposure: all buckets are private, RLS owner-prefix gated, and reachable only via presigned URLs. (5) Note also the number conflict: 0021's comment plans a 30-day reap while the Terms state 90 days — pick one before building it. (6) This predates and is independent of the "everything is a campaign" rework.
- **evidence:** project_pending_landings.md:28 — "Terms (`templates/legal/terms.html:64`) promise inputs+outputs kept **90 days** then \"may be permanently deleted\" — but this is a PROMISE, not enforced: `delete_input` in `shared/storage.py` has ZERO call sites and `0021` deferred a 30-day sweeper that never shipped, so customer data currently lives indefinitely."

### 17. Modal raw-capture reaper (prune_raw_volumes.py) is coded but never scheduled
- **severity:** medium | **owner:** ops | **lens:** registers
- **detail:** Open, but three corrections. (a) Location: the script is NOT in tools-hub — it is llm-proteinDesigner/scripts/prune_raw_volumes.py (on origin/master at 31e3e20). tools-hub contains no prune/reaper script at all. (b) Scope: it enumerates 13 `ranomics-<tool>-raw` Modal Volumes (bindcraft, boltzgen, rfdiffusion, rfantibody, pxdesign, mpnn, af2, colabfold, esmfold, boltz2, iggm, proteina, esmfold2-design), DEFAULT_DAYS=90, dry-run unless --apply, run with C:/Users/lab/AppData/Local/Programs/Python/Python313/python.exe (the interpreter that has modal). (c) The claim that this means "the 90d retention matching Terms is not actually applied" is misleading: the reaper touches Lane B only (internal raw tars on Modal Volumes, never customer-visible, never in tool_jobs.result). Its own docstring says 90d was picked only to avoid inventing a third window alongside 0021's 30d and the Terms' 90d. The customer-facing Terms retention promise is Lane A (Supabase Storage) and is unenforced separately — `delete_input` at tools-hub/shared/storage.py:174 still has zero call sites. Scheduling the reaper does not close the Terms gap; that Lane-A sweeper is its own distinct open item. Urgency is cost-hygiene and scales with job volume, not a correctness or compliance blocker today.
- **evidence:** project_pending_landings.md:27 — "**REMAINING (only):** **schedule the reaper** as a periodic sweep (`prune_raw_volumes.py`, dry-run until `--apply`, 90d retention matching Terms) once Volumes accumulate — not yet scheduled; run with the Python313 that has modal."

### 18. Option A uniform ranking still unlanded, with a live export/selection ordering trap
- **severity:** medium | **owner:** code | **lens:** registers
- **detail:** Three corrections to the description. (1) Half of Option A already landed under a different filename for the campaign path: shared/result_columns.py (_TOOL_RESULT_COLUMNS + _TOOL_PRIMARY_METRIC + primary_metric_for) plus the pass-first/primary-metric sort in shared/compute_campaigns.py::aggregate_campaign_candidates (~line 765). That merged campaign table is index-SAFE because each row carries _source_index (compute_campaigns.py:762) and the star button emits data-ref-idx from it. Remaining Option A scope is therefore narrower than the memory says: the single-job per-tool results pages (and the 5 non-campaign tools result_columns does not cover), not the campaign table. (2) The trap is not conditional on 'if ranking reorders rows' — reordering already happens today in those 7 Jinja-sorting templates, so the mismatch is live on main. (3) For af2/colabfold/esmfold/boltz2/iggm the manifestation is not a silent mis-map but a silent EMPTY: those templates build candidate rows from result['designs'], while blueprints/jobs.py:659 (export_csv) and blueprints/lab_projects.py:220 read the raw .get('candidates', []) instead of shared.jobs.candidate_records() — so CSV export is header-only and stage_campaign_candidates stages zero PDBs without erroring. esmfold2_design's output_candidates branch skips the sort and is safe. Evidence line numbers are stale: app.py ~5861 / ~6233 no longer exist on main (only in the stale worktree copy .claude/worktrees/elegant-rhodes-ef3fd5/app.py); current locations are blueprints/jobs.py:650 and blueprints/lab_projects.py:189.
- **evidence:** project_tools_hub_results_standardization.md:41 — "`export_csv` (app.py ~5861) iterates `job.result.candidates` in STORED order; `candidate_indices` (~app.py:6233) are POSITIONAL. If ranking reorders rows, CSV/export/lab-send/selection must use the SAME order or selections silently point at the wrong design."

### 19. Campaign expansion Phase A (iggm + esmfold2_design count-fanout) not built
- **severity:** medium | **owner:** code | **lens:** registers
- **detail:** Only the esmfold2_design half of Phase A is open. iggm is BUILT and merged on main (5c4bd2f + ac277f1): SUPPORTED_TOOLS entry, _DESIGN_PARAM_KEY["iggm"]="num_samples", _FLAG_GATED_CAMPAIGN_TOOLS gate, runs/new.html iggm block, tests/test_iggm_campaign.py, canary reported green; only the FLAG_TOOL_IGGM flip to on may remain as an ops step. Remaining work: make esmfold2_design campaign-capable as a count-fanout tool — add it to SUPPORTED_TOOLS + _DESIGN_PARAM_KEY, gate behind FLAG_TOOL_ESMFOLD2_DESIGN, force n_seeds=1 on each shard so the tool's own run_tool .spawn fan-out does not nest inside the campaign fan-out, add JOB_ID-derived self-seeding so shards diverge, harden build_payload, add tests, then a prod canary before flipping the flag. Note the separately-tracked esmfold2 multi-seed settle under-bill (modal_app._aggregate returns max(child runtimes) not the sum) is the charge-side companion and should be resolved before or alongside this.
- **evidence:** project_tools_hub_campaign_expansion.md:26 — "## Phase A (NEXT) — iggm + esmfold2_design, count-fanout campaigns\nBoth flag-gated (`FLAG_TOOL_<slug>` off), prod canary each, then flip."

### 20. Campaign expansion Phase B (fold-tool batch-sharding) not built
- **severity:** medium | **owner:** code | **lens:** registers
- **detail:** Accurate as written, with two clarifications. (1) Scope nuance from the shipped rework: after 20023ca a standalone fold job DOES appear in the unified /campaigns list as a "campaign of one" and gets the campaign results/export/shortlist UI, so the user-visible gap is narrower than "fold tools cannot appear in campaigns" — what is still impossible is a real multi-shard compute campaign (fan-out, fund-and-drain holds, driver ticks) over a sliced batch_records input list. (2) The batch-input work already landed on the TOOL side (54d0365): tools/esmfold/__init__.py:167-227, tools/colabfold, and tools/af2 already build and forward a `batch_records` list to their pipelines, so Phase B is genuinely engine-side-only as described. Also note Phase A is now partially shipped (iggm IS in SUPPORTED_TOOLS; esmfold2_design is NOT), which does not affect this Phase B claim.
- **evidence:** project_tools_hub_campaign_expansion.md:37-38 — "## Phase B (LATER) — fold-tool batch-sharding (new engine capability)\nBuild the input-list primitive ONCE (persist master `batch_records`, `records_for_chunk` slice, a batch branch in `_dispatch_chunk`, strip the list from `sanitize_shared_params`, corpus-upload create route, flavor discriminator), then thin per-tool: esmfold(150)/colabfold(90)/af2(32)/boltz2(16)."

### 21. rfantibody campaign canary never run (blocked on wallet top-up)
- **severity:** medium | **owner:** ops | **lens:** registers
- **detail:** The requirement is smaller and cheaper than described, and the dollar figures are a stale 2026-07-13 snapshot. (1) Scope: per the 2026-07-18 shared-engine decision (reference_campaign_drain_validated_shared_engine), the fund-and-drain money machinery is shared and already validated live on the LINEAR/count-scaled pricing class by rfdiffusion campaign 43f57ffd, so rfantibody does NOT need a multi-shard drain canary. It needs a single 1-shard functional canary (<=16 designs) to prove the tool-specific parts: that 16 designs finish in one 10h container under the 23h Modal timeout, that scores parse, and that the new campaign results/aggregation/export render. The chunking-code caveat in that same reference is why it cannot simply be waived: rfantibody added new chunking code rather than only a TOOL_SPECS/adapter row. (2) Cost: a 1-shard run gates at one cushioned chunk hold of $52.4361 (verified by running the shipped code), not the $104.87 that a 32-design/2-wave campaign demands; delivered-only billing then settles to actual with surplus released. (3) The "$34.10 current balance" is a 2026-07-13 figure recorded right after the pxDesign canary and predates the 2026-07-17 iggm/mpnn canaries, so the exact top-up delta (~$19 for 1 shard, ~$71 for 32 designs) is unverified until the live wallet balance is re-read.
- **evidence:** project_pending_landings.md:51 — "**ONLY rfantibody canary REMAINS** (deferred; a small ~32-design one still wants a ~$70+ top-up since per-container hold $52.44 > current $34.10 balance; drive from the live UI or with `PUBLIC_BASE_URL=https://tools.ranomics.com`)."

### 22. proteina 1-shard functional canary on the current build still owed
- **severity:** medium | **owner:** ops | **lens:** registers
- **detail:** The 1-shard proteina functional canary is still owed, but the description should be updated on three points. (a) It is no longer "against the post-#84 raw-capture build" — the relevant baseline is now main @ c6f7104, i.e. post-#92 (reconcile settle fix, f73e3c1) and post-#93 (the everything-is-a-campaign rework, 20023ca). (b) It is not true that nothing has been run: a live ligand_binder 1-shard canary (campaign `644f458d`, 2026-07-20) DID execute on Modal and produced 8 RF3-scored designs, so Modal-side pipeline-runs is already proven. What that run failed to prove is everything downstream — it hung in `running` because of the atomic-shard reconcile bug, was manually cancelled to free the $14.47 hold, and therefore never demonstrated natural settle, results-page render, aggregation or cost bootstrap. (c) The unproven surface is now LARGER than when the item was written, because both the settle path (reconcile_campaign_children, PR #92) and the entire results/aggregation path (aggregate_campaign_candidates + shared/result_columns.py proteina mapping on `total_reward` desc, PR #93) are newer than the last live run and have only unit-test coverage. Scope of the remaining canary: one ~$1 shard, no top-up, driven to natural terminal (do NOT cancel it), confirming the shard settles itself, the merged campaign results page renders proteina designs ranked by total_reward, and the hold trues up to actual GPU spend.
- **evidence:** project_tools_hub_campaign_expansion.md:24 — "proteina closes out on a **1-shard functional canary** (~$1, no top-up) to prove pipeline-runs + results-page + parsing + cost bootstrap on the current build."

### 23. proteina motif_ame variant, BYO custom-target route, and free-validate route all still deferred
- **severity:** medium | **owner:** product | **lens:** registers
- **detail:** Three proteina surfaces remain open on main c6f7104, and the first two are more severe than "deferred":

(a) motif_ame is not merely deferred — it is LIVE and user-selectable. `FLAG_TOOL_PROTEINA` is ON, and `templates/runs/new.html:68` lists motif_ame in the campaign design-variant dropdown with no gate in `blueprints/campaigns.py` (only iggm/affinity_maturation and `validate` are excluded). Meanwhile `tools/proteina/__init__.py:88-89` still says "canary it before exposing it." A customer can fund and burn A100 shards on the variant whose upstream reward_model block is commented out. Fix is either a route-level gate (mirroring the iggm affinity_maturation exclusion) or a ~$1 one-shard canary.

(b) BYO custom-target is worse than "hard-blocked pre-GPU": the block lives only in the container (`tools/proteina/run_pipeline.py:659-676`), while `blueprints/campaigns.py:321-360` still accepts, validates, and stages a proteina PDB/SDF upload, and `templates/runs/new.html:74` explicitly tells users to "leave blank and upload your own target below." The campaign is created, the wallet hold placed, and the shard dispatched before the container refuses. A route-level rejection (or removing the upload affordance/hint for proteina) is the cheap interim fix, independent of wiring the upstream `complexa target` registration.

(c) The public self-serve free-validate route is still absent and is the lowest-severity item: a deliberate unmade product/abuse decision, not a defect. The container `validate` branch exists as a staging gate, but both campaign preview and POST reject `preset=validate` and no `/tools` route serves it.

Note also that item (a) of the cited memory line — the P-4 fund-and-drain canary — is NO LONGER open (dropped as redundant 2026-07-18); only (c) motif_ame and (d) BYO remain from that line, plus the free-validate route from the older P0 entry.
- **evidence:** project_pending_landings.md:41 — "(c) motif_ame DEFERRED (least-verified: reward_model block commented out upstream; not launch-blocking; canary separately; protein+ligand are the launch variants); (d) BYO custom-target route wiring (still hard-blocked pre-GPU)."

### 24. pytest has no testpaths config — suite could go vacuously green
- **severity:** low | **owner:** code | **lens:** registers
- **detail:** Accurate in substance, imprecise in wording. It is not "no testpaths in pytest config" — there is no pytest config file at all (no pytest.ini, pyproject.toml, setup.cfg, or tox.ini is tracked; tests/conftest.py is the only pytest-related file), so the fix means creating a config, not adding a key to one. CI runs bare `python -m pytest -q` from the repo root (.github/workflows/pytest.yml:89) with no path argument and no PYTEST_ADDOPTS. Fallback collection empirically verified: `python -m pytest -q --ignore=tests` exits 0 with exactly 10 passed, all from tools/library_planner/tests/test_canonical.py. Secondary exposure from the same root cause: any stray test_*.py landing in scratch/, tmp/, runs/, or docs/ would be silently swept into the gating suite (none exist today; the full repo copy under .claude/worktrees/elegant-rhodes-ef3fd5/ is skipped only because pytest's default norecursedirs excludes dot-directories). Note the register entry itself labels this pre-existing and out of scope of the campaign rework — it is CI-infra debt in the same repo, not a defect in the campaign code.
- **evidence:** project_pending_landings.md:33 — "Deferred (pre-existing, out of scope): no `testpaths` in pytest config, so if `tests/` were ever moved/ignored, pytest would still collect `tools/library_planner/tests` (10 tests) and exit 0 = green on <1% of the suite."
---

# Addendum — 2026-07-27

Source: pre-implementation audit run while planning the target-first / multi-tool rework (one persistent target, N tool runs in parallel, one combined ranked table, optional Ranomics handoff). These are defects in the **current** code that exist whether or not that rework ships.

Numbered `A*` so they do not collide with either the synthesis list (1-16) or the verification list (1-24) above.

### A1. Presign failure is swallowed and the driver dispatches anyway, burning wallet holds and GPU on unrunnable jobs
- **severity:** high | **owner:** code | **lens:** pre-implementation audit
- **detail:** `_dispatch_chunk` wraps `presigned_input_url(campaign.target_storage_path, ...)` in a bare `except Exception` that logs a warning and leaves `presigned_url = ""`. Execution then falls straight through to `build_payload(child_inputs, "")` and `ModalClient().submit(...)` with no guard on the empty string. So a transient Storage outage, a revoked object, or any presign error does **not** pause the campaign and does **not** surface a distinguishable error class: the driver keeps placing per-child wallet holds and launching GPU containers with no input file, chunk after chunk, until every remaining chunk has dispatched and failed. Cost scales with `total_subjobs`, silently. This outranks synthesis item 1 (esmfold2 under-bill) on live exposure, because under-billing loses margin whereas this spends the customer balance on work that cannot succeed. It is also the reason item 16 / A6 cannot be closed as written: any storage-deletion sweeper built on top of today's behaviour turns a deletion mistake into silent spend rather than a loud stop.
- **evidence:** `shared/compute_campaigns.py:1170-1180` (bare `except Exception`, `presigned_url` left `""`); `:1187-1190` (`build_payload(child_inputs, presigned_url)` then `submit`, no empty check); contrast `:1126-1131` where `insufficient_funds` correctly returns a sentinel that pauses the campaign
- *Next:* return `"skipped"` from `_dispatch_chunk` on presign failure so the chunk is retried on a later tick instead of dispatched. Add a regression test asserting no `submit` and no `reserve_hold` occur when presign raises.

### A2. `@idempotent()` drops response headers, so a replayed redirect returns 302 with no Location
- **severity:** medium | **owner:** code | **lens:** pre-implementation audit
- **detail:** `_store_response` persists `response_status`, `response_body`, and `content_type` only; `_replay_response` rebuilds from those three fields. Headers are never captured. Every return path in `compute_campaign_refold` is a redirect, so a second POST inside the 60s TTL replays as a bare `302` with no `Location` header, which browsers render as a blank or error page. Double-clicking "Re-fold" reproduces it today. Not a data-integrity problem (the first request did the real work and the replay correctly avoids duplicating it), purely a broken-looking response. Worth fixing now rather than later because the natural next use of this decorator is another redirect-returning POST.
- **evidence:** `shared/idempotency.py:172-192` (`_store_response` field list), `:195-202` (`_replay_response`); `blueprints/campaigns.py:628-631` (`@idempotent()` on refold), with redirect returns at `:661`, `:664`, `:700`, `:713`
- *Next:* persist `Location` (or a small header allowlist) alongside the body and restore it on replay. Add `tests/test_idempotent_redirect.py` asserting a replayed 302 still carries `Location`.

### A3. Shortlist candidate `index` is never bounds-checked before insert, producing phantom shortlists
- **severity:** medium | **owner:** code | **lens:** pre-implementation audit
- **detail:** `_parse_candidate_refs` validates only `idx >= 0`, and `create_campaign_from_refs` inserts `candidate_refs` verbatim after checking assay/budget/non-emptiness. The only real bounds check is downstream in `stage_campaign_candidates`, which silently `continue`s on an out-of-range index. Consequence: a POST carrying `{"job_id": "<own job in campaign>", "index": 999999}` creates a `lab_campaigns` row whose `candidate_refs` claims N shortlisted designs, stages fewer (possibly zero) PDBs into the `lab-campaigns` bucket, and still fires the confirmation and staff-notify emails as a success. Ops sees a shortlist that does not match the files. This compounds with synthesis item 2 (`candidate_records`), which independently causes zero-staging for designs-only tools: same visible symptom, two distinct causes. Note the legacy single-job branch is weaker still, accepting negative indices via a bare `int(i)` with no filter. Not a tenancy breach; the ownership and parentage gates are correct.
- **evidence:** `blueprints/lab_projects.py:37-59` (`_parse_candidate_refs`, only `idx >= 0`), `:188-232` (legacy branch, no filter at all); `shared/campaigns.py:226-280` (inserts verbatim); `shared/storage.py:219-221` (the silent skip); `blueprints/lab_projects.py:130-134` (StorageError caught and only warned)
- *Next:* bounds-check each index against `len(candidate_records(job.result))` at parse time and reject the submit if any ref is invalid, rather than dropping it silently.

### A4. Shortlist `candidate_refs` has no length cap and failed lookups are not negatively cached
- **severity:** medium | **owner:** code | **lens:** pre-implementation audit
- **detail:** `_parse_candidate_refs` will return an arbitrarily long list, and the verification loop issues one `get_job` Supabase round-trip per distinct `job_id`. A `job_id` that fails verification `continue`s **without** being written to `jobs_by_id`, so it is re-fetched on every subsequent occurrence. One POST carrying 100k refs that all name the same foreign job therefore triggers 100k sequential Supabase round-trips inside a single request. Authenticated-only, so the blast radius is one account, but it is a free way to pin a worker.
- **evidence:** `blueprints/lab_projects.py:37-59` (no cap); `:86-97` (per-ref `get_job`; the `continue` at `:93-95` skips the `jobs_by_id[jid] = job` cache write that only happens on success at `:96`)
- *Next:* cap ref count at a sane ceiling (the UI cannot star more than the rendered table anyway), and cache rejections as `None` so a repeated bad id costs one round-trip.

### A5. `get_handoff` never enforces `expires_at`
- **severity:** low | **owner:** code | **lens:** pre-implementation audit
- **detail:** The docstring says it "rejects expired/consumed rows" but the body only checks `consumed_at`. `expires_at` is `NOT NULL DEFAULT (now() + interval '2 hours')` in the schema and is loaded onto the dataclass, but never compared to now. An unconsumed Epitope Scout handoff is therefore redeemable indefinitely. Same-user only (the query is `.eq("user_id", user_id)`), so this is a token-lifetime bug rather than a tenancy one, and single-use is still enforced via the conditional `mark_consumed` update. Flagged because the Scout handoff is the intended entry point for a future Scout-to-target flow, and that flow should not inherit a token with no expiry.
- **evidence:** `shared/handoffs.py:94-115` (`get_handoff`, `if data.get("consumed_at"): return None` with no `expires_at` comparison); `supabase/migrations/0007_scout_handoffs.sql:28` (the 2h default); `shared/handoffs.py:118-134` (`mark_consumed`, correctly conditional)
- *Next:* compare `expires_at` to `now()` in `get_handoff` and return `None` when past.

### A6. Amends item 16 — the retention sweeper shipped without a live-campaign guard (RESOLVED 2026-07-27)
- **severity:** n/a (amends an existing item) | **owner:** code | **status:** fixed in the same pass as A1
- **detail:** Item 16 said no deletion path existed. **That is now stale**: `feat/data-retention-30d` merged as PR #96 (`bb57477`), shipping `cron/purge_old_storage.py` with a 30-day age sweep over `tool-inputs` + `tool-outputs`. The 30-vs-90-day conflict item 16 flagged was resolved in favour of 30 and the Terms copy was updated to match, so that half is closed. What shipped **lacked** the guard this addendum was written to demand: the sweeper selected purely on object age, with no reference to `compute_campaigns` or job status, while `_dispatch_chunk` re-mints a presigned URL from `campaign.target_storage_path` on **every wave**. A campaign outliving the retention window (long fan-outs, or a pause up to `_PAUSE_TTL_DAYS`) would have had its input swept out from under it, and pre-A1 the campaign would have answered by continuing to place holds and launch containers rather than stopping. Latent only because the sweeper is dry-run by default and is not scheduled anywhere.
- **fix:** `active_campaign_input_paths()` in `cron/purge_old_storage.py` collects `target_storage_path` for every campaign whose status is not in the new `CAMPAIGN_TERMINAL_STATUSES` (`shared/compute_campaigns.py`), and `purge_old_storage` filters those out of the `tool-inputs` expiry set, reporting them as `protected`. Fails **closed**: when the lookup cannot be read the bucket is skipped entirely and an error is recorded, matching the module's existing "never delete on unknown age" rule. Covered by 7 tests in `tests/test_data_retention.py` (live campaign survives, all four terminal statuses sweep, unknown set blocks deletion, null `target_storage_path` rows do not poison the set).
- **evidence:** `shared/compute_campaigns.py` `_dispatch_chunk` (per-wave presign from the stored path); `cron/purge_old_storage.py` (`purge_old_storage`, `active_campaign_input_paths`); PR #96 `bb57477`
- *Next:* still open from item 16 — the sweeper is not scheduled and `purge_user_objects` is not wired to any account-deletion path (no such feature exists in the repo yet). Keep the first scheduled runs in dry-run.

### A7. Design hazard for future work — `copy_input` / `download_input` / `download_output` carry no ownership check
- **severity:** informational | **owner:** code
- **detail:** Not a live defect: every current call site gates correctly. Recording it because the pattern is easy to break. These three functions take `user_id` as a **path component, not an authorization check**, and run on the service-role client, so they will read or copy any object in the bucket if handed a foreign path. The entire tenancy boundary for the `job:` PDB-reuse token is the user-scoped `get_job` immediately above the `copy_input` call; the GET-side checks that mint the token are decorative, since the token round-trips through a client-controlled hidden field. Any new reuse token (a `target:` token, for instance) must re-fetch the id scoped to `ctx.user_id` and raise before touching a path. Separately, `upload_input` sanitizes `filename` via `secure_filename` but interpolates `user_id` and `job_id` raw, and `blueprints/campaigns.py:384` already repurposes the `job_id` slot as a free-text namespace, so any future client-influenced value in that slot is a path-traversal write.
- **evidence:** `shared/storage.py:150-171` (`copy_input`, no `source_user_id`), `:129-147` (`download_input`), `:437-456` (`download_output`), `:58-100` (`upload_input`, `_safe_filename` on filename only), `:490-499`; `blueprints/tools.py:1312-1321` (the load-bearing `get_job(..., user_id=ctx.user_id)`); `blueprints/campaigns.py:381-390` (free-text `job_id` slot precedent)

## Addendum 2 — 2026-07-27, independent QC of the Phase 0 branch

Eleven findings from an adversarial review of `fix/phase0-campaign-hardening`
(five independent reviewers, one refutation pass each). A8 and A9 were
regressions **introduced by this branch's own fixes** and are already fixed;
A10-A13 are filed, not fixed. Numbering continues from A7.

### A8. A1's `candidate_records` fix turned "stages zero PDBs" into "stages the WRONG PDB" (RESOLVED 2026-07-27)
- **severity:** was blocker | **owner:** code | **status:** fixed in the same pass
- **detail:** Every designs-shape results partial reshapes `result["designs"]` into fresh dicts and then re-sorts by its headline metric, so the row at screen position 0 is normally not `designs[0]`. `_source_index` was stamped in exactly one place, `aggregate_campaign_candidates`, so on the single-job path `candidate_table.html` fell back to `loop.index0` — the **post-sort** position — and the shortlist posted that as `candidate_indices`. `blueprints/lab_projects.py` then resolved it against `candidate_records(job.result)`, which is raw pipeline order. Before the `candidate_records` change the same line read `(job.result or {}).get("candidates", [])`, which is `[]` for these tools, so the failure mode was silent-empty; afterwards it became silent-**wrong**: a different structure copied into the `lab-campaigns` bucket, recorded in `candidate_refs`, and confirmed to the user by a success email. Item 13's note that "the only possible failure is zero staged, never mis-staged" reasoned from `_source_index`, which holds only on the campaign path.
- **affected:** boltz2, af2, colabfold, esmfold, iggm, opendde, and esmfold2_design's legacy fallback branch.
- **fix:** all seven partials stamp `'_source_index': loop.index0` inside the reshape loop, **before** the sort. `tests/test_shortlist_index_mapping.py` renders each tool with a metric order that is the reverse of pipeline order and asserts the posted index resolves, through `candidate_records`, to the design shown in that row; it fails if any stamp is removed. It also pins both esmfold2_design branches (the canonical `candidates` branch is identity because it does not sort).
- *Next:* the durable fix is to move the reshape and sort out of Jinja into shared code so a new partial cannot reintroduce this. Until then, **any new re-sorting results template must stamp `_source_index`.**

### A9. Amends A2 — the `@idempotent()` Location fix was inert (RESOLVED 2026-07-27)
- **severity:** was medium | **owner:** code | **status:** fixed in the same pass
- **detail:** A2's fix persisted `location` and replayed it, but `_claim_key` selected an explicit column list that did not include it. PostgREST projects exactly the columns requested, so the replayed row never carried `location` and `_replay_response` always read `None`. Every guarded route still returned a bare 302. Migration 0038 was scheduled for prod in support of a fix that did nothing.
- **why the obvious fix is wrong:** appending `,location` to the list 400s before 0038 is applied, and `_claim_key`'s bare `except` fails **open**, so every double-submit would re-run its handler — a second wallet hold and a second Modal job per double-click. `.select("*")` is migration-order-independent and matches the house pattern.
- **fix:** `.select("*")`, plus `tests/test_idempotency.py`'s fake now honours the projection (which is what made the two Location tests vacuous) and can model a pre-0038 table that rejects the column on UPDATE. A new test asserts that before the migration the replay degrades to "no Location, body still carries the link" while the handler still runs exactly once.
- *Next:* the deploy-order note stands but is no longer load-bearing: apply 0038 before deploying to get the header, but the code is safe either way.

### A10. A permanently unrecoverable `"skipped"` leaves a campaign in `running` forever
- **severity:** low (pre-existing, widened by A1) | **owner:** code
- **detail:** When `_dispatch_chunk` returns `"skipped"` the driver breaks out of the admission loop with `launched_any=False` and `hit_insufficient=False`, so no status transition runs, and `_maybe_finalize` returns early because `dispatched < total`. In-flight children drain to zero and the campaign sits in `running` indefinitely: no `completed_at`, no pause email, no failure email, and `cron/tick_campaigns.py` keeps re-driving it forever. Only `paused_insufficient_funds` has a TTL. Pre-existing — `adapter is None` and `design_count <= 0` already returned a permanent `"skipped"` — but A1 added a new way to reach it (an input that stops resolving).
- *Next:* bound it like the pause path. Count consecutive no-progress drives for a campaign with undispatched chunks and zero in-flight children, then CAS to a terminal status through the existing notification path. Returning a reason alongside the outcome would let a 404 start the clock immediately and a 5xx only after N retries.

### A11. The retention guard has no upper bound, so a stuck campaign pins its input forever
- **severity:** medium | **owner:** code + product decision
- **detail:** `active_campaign_input_paths()` protects every non-terminal campaign with no recency limit. A campaign stuck in `draft` (see A12) or otherwise abandoned protects its `tool-inputs` object permanently, which quietly defeats the 30-day retention promise the Terms now make. The guard is deliberately fail-safe in this direction, so the cost is storage and a compliance gap, not data loss.
- *Next:* protect only non-terminal campaigns with activity inside a sane window (`_PAUSE_TTL_DAYS` plus a margin), log anything older as stuck, and add a reaper that cancels `draft`/`funded` campaigns that have dispatched nothing after N hours. Pairs with A12.

### A12. `fund_campaign` can silently no-op, stranding a paid-intent campaign in `draft`
- **severity:** low | **owner:** code
- **detail:** The transition is not checked, so a campaign that fails to move `draft -> funded` is redirected to as though it started. It never dispatches, never bills, and never terminalizes; it also pins its input under A11. A draft is inert, so nothing is charged — the cost is a user who thinks they launched a run.
- *Next:* make it a checked CAS transition returning a bool, and have the create route surface an error or retry instead of redirecting.

### A13. A hold placed before `create_job` fails is unreachable if its release fails
- **severity:** low | **owner:** code
- **detail:** On the `create_job`-failed path the hold release is best-effort and its return value is not checked. A failed release leaves a hold with no job row, so nothing downstream can settle or reclaim it and there is no sweeper. Bounded by how rarely `create_job` fails, and it strands rather than overcharges.
- *Next:* check the boolean, retry once, then persist the orphan id for reclamation. Placing the hold after `create_job` succeeds would make every hold reachable from a job row by construction.

### A14. Per-job CSV / FASTA carried no metrics for designs-shape tools (PARTLY RESOLVED 2026-07-27)
- **severity:** medium | **owner:** code
- **detail:** `candidates_to_csv` discovered columns from `cand["scores"]` only, but every designs-shape pipeline puts its metrics at the record **root** and has no `scores` dict. The results templates reshape inline, so the screen looked correct while the export produced the right number of rows with no science in them. A1's `candidate_records` fix corrected the row count, not the columns. The `_designs_shape()` fixture in `tests/test_export_shapes.py` invented a nested `scores` and a `sequence`, so the tests asserting "not header-only" passed against a shape no tool emits.
- **fix:** column discovery now reads the root as well as `scores`, excluding provenance tags, identity fields, and any non-scalar or bulk value (`pdb_content_b64`, per-residue contact lists). The fixture was rebuilt from a real boltz2 row and a test now asserts the metric *values* reach the file, not just the row count.
- *Next:* root metrics export under the pipeline's own names (`iptm`, not `ipTM`). Mapping those onto canonical display names is the cross-tool aliasing work and belongs with the merged target table, not here. FASTA is still empty for designs-shape tools, which is correct — they emit no binder sequence.

---

## Addendum 3 — 2026-07-28 (target-first Phase 1)

Filed while building `design_targets` (migration 0039). One defect was
introduced-and-fixed inside this phase; the rest are notes on what Phase 1
deliberately left for later.

### A15. Targets would have been swept by the retention cron at 30 days (RESOLVED in phase)
- **severity:** high (would have been) | **owner:** code
- **detail:** `cron/purge_old_storage.py` sweeps `tool-inputs` by object age, guarded only by `active_campaign_input_paths()`. That guard protects a *campaign* that can still dispatch, and every campaign eventually reaches a terminal status and stops protecting its input. A target is long-lived **by design** — that is the entire point of 0039 — and stays launchable forever. So a target older than the retention window would have silently lost its structure while still rendering as a normal, launchable card, and the next run against it would have died on an unrunnable input. The failure is invisible until someone launches.
- **fix:** `live_target_input_paths()`, same contract as the campaign guard: paged past the PostgREST `max_rows` clamp, negation applied server-side and re-checked client-side, and `None` (unknown) fails closed. The two protected sets are unioned; either lookup failing skips the whole `tool-inputs` sweep for that pass. Archived targets are deliberately NOT protected, since archiving is how a user says they are done with a structure.
- *Next:* this widens A11. A target with no recency bound pins its input permanently, which is the same compliance gap as a stuck campaign, now with a legitimate long-lived object behind it. The 30-vs-90-day retention decision (still unresolved: migration 0021 says 30, `templates/legal/terms.html:64` says 90) should be settled before adding a recency bound here, because "we keep your uploaded structure while the target is live" may be the honest policy rather than a bug.

### A16. A run launched from a target validates chain and hotspots against the persisted inspection, not the file
- **severity:** informational | **owner:** code
- **detail:** Chain and hotspots are per-RUN and may override the target's defaults, so they still need checking — but the structure is never re-uploaded, so `resolve_target_upload` (and with it `validate_target_chain`) does not run. Rather than download and re-parse on every launch, `DesignTarget.chain_error` / `.hotspot_error` check the `chain_summary` jsonb persisted at upload time. This is the same data the inspection produced, so the answer is identical, and it costs no round-trip. It does mean the check is skipped for a target with no persisted summary — an SDF ligand or a curated task, neither of which has protein chains to name.
- **contrast with the atomic path:** the `target:` reuse token in `blueprints/tools.py` gets the *full* re-inspection, because the existing reuse hard-gate downloads the staged bytes back for every non-`alphafold:` token. Both paths are gated; only the cost differs.
- *Next:* nothing. Noted so a future change does not "helpfully" add a download to the launch path.

### A17. The target hub read runs twice and could hide them all (RESOLVED)
- **severity:** ~~low~~ **medium** | **owner:** code
- **detail:** `/targets/<id>` called `campaign_ids_for_target` (paged select of ids) and then `list_campaigns_for_user(limit=200)`, filtering in memory.
- **this entry originally understated it, and the correction is the point.** It was filed as "a user with more than 200 runs could see an incomplete run strip" — a cosmetic cap. The cap is not on the target's runs, it is on the user's ENTIRE campaign history. A target whose runs all fall outside that window matched nothing, so the page rendered its empty state: "Nothing has been run against this target yet", for runs the user had paid for. Found independently by three reviewers in the second QC pass and confirmed by both verify lenses.
- **fix:** `shared/compute_campaigns.py::list_campaigns_for_target` filters server-side on `target_id`, owner-scoped, paged past `max_rows`, with the newest-first sort applied in memory so page boundaries stay stable. One read instead of two. `tests/test_target_routes.py::test_target_detail_lists_only_this_targets_runs` now asserts `list_campaigns_for_user` is NOT called, so reintroducing the in-memory filter fails the suite.
- *Lesson:* a severity assigned from the mechanism ("two reads, one cap") rather than from the user-visible outcome ("your runs disappear") files a real defect as cleanup and it stays unfixed.

### A18. `validate_hotspots` rejects every hotspot on a multi-chain target
- **severity:** medium (pre-existing, on main) | **owner:** code
- **detail:** `shared/pdb_inspect.py::validate_hotspots` passes the whole `target_chain` string to `report.chain()`. For a multi-chain string like `"A B"` — which ProteinMPNN-style design submits and rfdiffusion's validator accepts (4-char cap) — the lookup returns `None`, so the function reports EVERY hotspot out of range. Reached from `_verify_reuse_pdb_bytes`, so it fires on the `job:` / `handoff:` / `resample:` / `target:` reuse paths, failing a legitimate submission for $0 with a misleading message.
- **why it surfaced now:** `DesignTarget.hotspot_error` (Phase 1) checks the same thing against the persisted `chain_summary` and deliberately does NOT reproduce this: it accepts a residue in ANY named chain. So the identical submission is accepted on the campaign target path and rejected on the atomic `target:` path. The new behaviour is the correct one; the divergence is the cost of not fixing the old function in a phase that was not about it.
- *Next:* make `validate_hotspots` split the chain string and union the ranges, matching `validate_target_chain`, which already iterates `target_chain.split()`. Then delete the divergence note in `shared/targets.py::hotspot_error`.

### A19. Both retention guards page by offset, so a concurrent insert can drop a row
- **severity:** medium (pre-existing for the campaign guard) | **owner:** code
- **detail:** `active_campaign_input_paths` and `live_target_input_paths` both page with `.order("id").range(offset, offset+499)` over a `gen_random_uuid()` primary key, i.e. effectively random ordering. If a row is inserted into an already-read page mid-sweep, every later row shifts down one and exactly one row is never read. That row's storage object is then absent from the protected set and is deleted while still in use. One row lost per interleaved insert; needs >500 live rows of the relevant kind to be reachable at all.
- *Next:* keyset paging — `.gt("id", last_id)` instead of `.range()` — which is immune to inserts and no more code. Same fix for `shared/targets.py::campaign_ids_for_target` and `shared/compute_campaigns.py::iter_succeeded_children`, which share the shape (their failure is a short read, not data loss).

### A20. The pytest suite runs against the production database
- **severity:** high | **owner:** code + ops
- **detail:** `app.py` calls `load_dotenv()` at import and the repo-root `.env` carries real `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`, so any test that imports `app` and exercises a route performs REAL reads and writes against production. `@idempotent()` routes are the worst case: they INSERT into `idempotency_keys` and then replay those cached responses into later runs. Measured on the Phase 1 target tests: 3 of 6 consecutive runs failed, different tests each time, including all three cross-tenant isolation assertions (a target owned by `u-1` came back for `u-2`). With the credentials blanked: 74 passed in 3.8s, clean 5 runs running. A suite that consults a database it does not control cannot be the gate on ownership.
- **partial fix:** `tests/conftest.py::isolate_supabase` blanks the credentials for a test; the four Phase 1 target test files opt in via `pytestmark`. Deliberately opt-in, because making ~1500 existing tests hermetic in one move is its own change with its own blast radius.
- *Next:* make it autouse and let the env-gated suites (`test_rls.py`, `test_af2_smoke.py`, `test_platform_api_hardening.py`, `test_uniprot_lookup.py`) opt back IN explicitly. Then stop shipping usable production credentials in a file every test run loads by default.

### A21. `create_job`'s schema-gap retry had never fired (RESOLVED)
- **severity:** low | **owner:** code
- **detail:** The 0022 guard read `if "campaign_label" in msg and "label" in row`. The second clause tests dict KEYS and the key is `campaign_label`, so it was always False and the retry was dead code — the comment above it advertised a safety net that did not exist. Pre-existing on main and dormant in practice (0022 is applied, so no error mentions the column). Found by `tests/test_target_id_persistence.py`, which asserts the retry preserves `target_id`.
- **fix:** `"campaign_label" in row`. The retry stays dormant in prod; it now works if it is ever needed.

---

## Addendum 2026-07-28b — second QC pass, over the Phase 1 FIXES

The first pass reviewed the branch; its findings were then fixed by the author
and **the fixes were reviewed by nobody**. This pass covered only that fix diff:
six reviewers plus adversarial refuters. Two defects confirmed (A17 above,
restated and re-severitied; A22 below), one finding refuted (A23), nine capped
unverified (A24 to A30).

### A22. The retention fix's fail-open branch had zero coverage, and the test claiming to cover it took the other branch (RESOLVED)
- **severity:** high (test integrity) | **owner:** code
- **detail:** `test_target_lookup_failure_fails_closed`'s docstring said it covered "a database that has not had 0039 applied yet, where the table does not exist". Its fake raised `RuntimeError("transient DB failure")`, which `_is_missing_table` returns False for, so it exercised the fail-CLOSED path while advertising the other one. Deleting the entire `if _is_missing_table(exc): return set()` block left all 36 tests green — a reviewer applied that mutation and ran it. Nothing in `tests/` referenced `42P01`, `PGRST205`, or `_is_missing_table`.
- **why this one stings:** it is the same defect class the fix was written for, reproduced inside the fix. A docstring asserting a property nobody checked, plus a test that passes when the code it names is deleted.
- **fix:** the fake now takes an exception instance so a test can choose WHICH failure it is; `_missing_table_error()` builds a real `postgrest.exceptions.APIError` with the PGRST205 payload and asserts `.code` is populated (a hand-rolled stand-in would satisfy the predicate for the wrong reason); `test_is_missing_table_separates_a_missing_table_from_any_other_error` pins the predicate in BOTH directions; `test_a_missing_design_targets_table_lets_the_sweep_continue` asserts objects were actually deleted, which is only true if the branch fired. Mutation-verified: re-disabling the branch fails that test.

### A23. `_is_missing_table` treats a PostgREST schema-cache miss as a missing table (REFUTED, do not "fix")
- **severity:** informational | **owner:** none
- **claim:** `PGRST205` means "not in the schema cache", not "table absent", so a transient cache anomaly would unprotect every live target and the sweep would delete their structures.
- **why it was refuted:** the mechanism is real but unreachable in shipped code. `purge_old_storage` has exactly one caller, the `flask storage:purge-old` CLI (`app.py:835`), which defaults to dry-run. No Procfile entry, no GitHub workflow, no Railway cron service, and no in-process scheduler invokes it; the only Railway crons are `digest:send` and `jobs:sweep-stuck`. Harm requires an operator typing `--apply` during a cache anomaly.
- **and the proposed remedy is actively wrong.** Narrowing the predicate to SQLSTATE `42P01` would break it: PostgREST resolves the relation in its router against the schema cache, so a genuinely missing table returns `PGRST205` and never the raw SQLSTATE. Restricting to `42P01` means `_is_missing_table` never fires, the guard returns `None` on every pass pre-migration, and the permanent-halt bug returns. The `PGRST205` ambiguity is inherent to the wire protocol and is not distinguishable client-side.
- *Next:* nothing, beyond not scheduling this sweeper without revisiting. Recorded so the "obvious fix" is not applied later by someone reading only the predicate.

### A24 to A30. Capped unverified
Reported by reviewers, not put through the refuters (verify budget capped at 5).
Listed so they are not mistaken for a clean bill.
- **A24.** `target_defaults_for_form`'s `"epitope"` key is untested; renaming it back leaves the target suite green. No fixture sets `epitope_residues`, so the line never executes. *(medium)*
- **A25.** The `target:` token stamps `target_id` on a job even when the adapter has `requires_pdb=False`, so the entire staging block is skipped and the run is filed under a target whose structure it never read — the same mis-attribution the upload-override fix was written to prevent. *(low)*
- **A26. (RESOLVED)** `_spawn_refold_job` omitted `target_id`, so validation refolds landed with both `target_id` and `campaign_id` NULL. Migration `0039`'s own comment claims a "yardstick re-fold" carries it — false when written, and Phase 4 depends on it being true: a refold has campaign_id NULL, so `target_id` is its ONLY link back, and the fan-in that re-ranks every tool on one predictor reads exactly these rows. Unstamped, they are invisible and the comparison silently covers nothing. **Fixed:** the new job inherits `src.target_id`, read from the source job rather than passed in (both call sites hand over a `ToolJob`, and a campaign sub-job already carries its campaign's target_id from `_dispatch_chunk`, so there is no path where the caller knows a target the source does not). NULL for a refold of an untargeted run, which is correct. Mutation-verified; the two `SimpleNamespace` source-job fakes in `test_campaign_results.py` now carry the field, since they stand in for a `ToolJob` that has it. *(the migration comment is now true rather than aspirational)*
- **A27.** `campaign_ids_for_target`'s docstring claimed "every run id" while reading only `compute_campaigns`; standalone `tool_jobs` rows carrying `target_id` can never be returned. **Docstring corrected in place**; the underlying two-table read is Phase 3's fan-in. *(low, and a false comment)*
- **A28.** `test_list_targets_clamps_a_limit_past_the_row_cap` seeds 5 rows and asserts `len(...) == 5`, which holds whether the limit is clamped or not. Decorative. *(low)*
- **A29.** A19's offset-paging hazard restated for `live_target_input_paths` specifically: a row leaving the filtered set between page reads drops exactly one live target from the protected set. *(medium, tracked under A19)*
- **A30.** A18 restated: the same submission passes `DesignTarget.hotspot_error` and fails `validate_hotspots`, and the losing path fails the job only after the row and its Modal-bound staging exist. *(low, tracked under A18)*

**Three of these (A26, A27, and the A17 restatement) are comments asserting properties that are false — written in the pass whose purpose was fixing comments that assert properties that are false.** The rule in `feedback_comments_assert_intent_not_behaviour` is not being applied to new comments as they are written.

---

## Addendum 2026-07-28c — third QC pass, over the A17/A22/A26 fixes

One independent reviewer over `git diff 4f21a4b`. **No logic defect found**; it
independently mutation-tested all three fixes (disabling the missing-table
branch, dropping the `user_id` filter, and swapping `.range()` for `.limit()`)
and confirmed each turns exactly one test red. It also confirmed the `for/else`
is not inverted, owner scope is unreachable with `user_id=None`, and both
`_spawn_refold_job` call sites hand over a real `ToolJob`.

**All three findings were comments asserting properties the code does not
have — for the third consecutive pass, and this time in the very comments
written to fix that same defect.** Corrected in place. The recurrence is the
finding: writing a property-asserting comment is being treated as documenting
intent, when it is making a claim that has not been checked.

### A31. The target page cannot show a target's standalone runs
- **severity:** low (latent) | **owner:** code
- **detail:** `list_campaigns_for_target` reads `compute_campaigns` only. Migration 0039 also puts `target_id` on `tool_jobs`, and two things write it there with `campaign_id` NULL: the `target:` reuse token (`blueprints/tools.py`) and, as of this branch, yardstick refolds (A26). A target holding only those renders "Nothing has been run against this target yet" — the exact user-visible failure A17 was fixed for, through a different table.
- **why it is latent, not live:** no template mints a `target:` token yet (`templates/tools/_prefill.html` emits `pdb_source.token`, and the only minter is `resample:`), so the row cannot be created from the UI today. The docstrings on both the function and the route now say so explicitly rather than claiming completeness.
- *Next:* Phase 3's fan-in reads both tables by definition, so this closes there. Do not widen the summary line without widening the query.

### A33. An archived target's detail page does not say it is archived, and its primary button is a silent no-op **(RESOLVED)**
- **severity:** low (UX) | **owner:** code
- **found:** live browser walk on production, 2026-07-28, target `164eb28e`.
- **detail:** After archiving, `/targets/<id>` renders exactly as before: the header still offers **Run a tool** and **Archive**, with nothing indicating the target is archived. The target is correctly gone from `/targets`, and both launch gates work (A17-era fixes verified live: `/targets/<id>/launch` redirects back to detail, and `/campaigns/new?target_id=` falls back to the plain upload form). But that redirect is what makes the button a dead loop: clicking **Run a tool** on an archived target reloads the same page with no message. From the user's side that is indistinguishable from a broken button, and **Archive** on an already-archived target is meaningless.
- **why it is only low:** nothing is lost or mis-billed, the runs still render, and the page is only reachable by direct URL or browser history once the target leaves the list.
- **fix:** an "Archived" pill plus a **Restore target** button replaces the Run/Archive pair on an archived target's detail page. `POST /targets/<id>/unarchive` and `shared.targets.unarchive_target` are new, so archiving is no longer one-way. `/targets` grew an **Archived** section as a second, separately capped read, because archiving redirects to a list that excludes archived targets and the restore control was otherwise unreachable except by URL. Two dead launch links were found, not one: the header and the Runs empty state.
- **also corrected here:** the archived section is ordered by `archived_at`, not `created_at`, or the target just archived sorts under ones archived months earlier; the "structure is still staged" copy is conditional on `storage_path`, since `create_target` accepts `upload=None`; `unarchive_target` filters `archived_at IS NOT NULL` so its bool means "was archived, now live" rather than "row exists"; and both list sections read one row past their render cap so a full page can be told from a truncated one. See A34 for the index cost this introduces.

### A32. The target run strip has no render ceiling
- **severity:** informational | **owner:** code
- **detail:** The pre-fix code capped the render at 200 rows as a side effect of the bug. The fix removes the cap deliberately (that cap was hiding runs), so the page now renders every run up to the 10,000-row paging bound, each with a status badge. Nobody has looked at what a 2,000-run target does to that page.
- *Next:* nothing yet. Phase 6.4's run strip is the natural place to add paging or a "showing N of M" affordance. Noted so a future slow-page report is not misdiagnosed as a query problem.

### A34. The archived-targets read is unindexed and runs on every targets page load
- **severity:** informational | **owner:** code
- **found:** independent QC over the un-archive diff, 2026-07-29.
- **detail:** Migration 0039 provisions exactly two indexes on `design_targets` (`design_targets_user_created_idx`, `design_targets_user_sha_idx`) and **both** carry `WHERE archived_at IS NULL`. No later migration adds another. So `archived_only=True`, which filters `archived_at IS NOT NULL` and sorts by `archived_at DESC`, can use neither: it is a scan plus a sort. `GET /targets` issues it unconditionally as its second read, so every user pays it on every load, including the majority who have archived nothing and get zero rows back.
- **why it is only informational:** the table is tiny and per-user, the read is capped at 100 rows, and the alternative (one mixed query) reintroduces the bug the two-read split exists to prevent, since archived rows would then compete with live ones for the same capped page. The cost is real but currently unmeasurable.
- *Next:* if `design_targets` ever grows, add a partial index on `(user_id, archived_at DESC) WHERE archived_at IS NOT NULL` in whichever migration is next free, or make the archived section load on demand instead of inline. Do not "fix" it by collapsing back to a single query. The docstring on `list_targets_for_user` states this cost so it is not rediscovered as a mystery.

## Addendum 2026-07-29 — target-first Phase 2 (multi-tool launch)

Found while building `GET|POST /targets/<id>/launch`. The first two were fixed
in the Phase 2 diff because that route could not be built correctly around
them; the rest are filed.

### A35. `@idempotent()` keyed on `(user, route)` only, ignoring the form **(RESOLVED)**
- **severity:** high (silent data loss) | **owner:** code
- **found:** Phase 2 build, 2026-07-29. Verified empirically on Flask 3.1.3 / Werkzeug 3.1.8, not inferred.
- **detail:** `app.py`'s `_enforce_csrf` is a `before_request` that reads `request.form.get("_csrf")` on every protected POST. Parsing the form consumes the request stream, and Werkzeug's `_load_form_data` does not populate `_cached_data`, so by the time `shared/idempotency.py` calls `request.get_data(cache=True)` it receives `b""`. The content hash was therefore `sha256(user_id + request.path + b"")` on all seven decorated routes.
- **two live consequences:** (1) a second, DIFFERENT submission to the same route within the 60s TTL was treated as a duplicate: it never ran and replayed the first response, with the user given no indication. Worst on `blueprints/tools.py:846` `tool_submit` (submit a job for one structure, then another within a minute, and the second silently vanishes) and `blueprints/targets.py` `POST /targets` (the second distinct target is never created). (2) 4xx responses were cached, so a user refused for insufficient balance who topped up in another tab and resubmitted got the same refusal replayed.
- **why the suite could not see it:** `tests/conftest.py:20` sets `CSRF_PROTECT=0` process-wide, which makes `_enforce_csrf` return before touching `request.form`, so every test exercised a code path production never takes.
- **fix:** `_compute_key` falls back to a canonical encoding of the parsed form when the raw body is empty (sorted, built from `form.lists()` so multi-valued fields contribute every value, `_csrf` excluded so token rotation is not a new request, file parts contributing name/filename/size). Separately, a handler response of 4xx/5xx now releases the claim instead of storing it. Both narrow dedup strictly: real double-clicks still collapse. Tests reproduce the production posture with their own form-consuming `before_request`.
- **known limitation, stated not hidden:** two submissions identical in every form field AND uploading files of the same name and size within the TTL still collide. Hashing upload bytes would cost a full pass over every upload on every request. That combination is a double-click far more often than two distinct jobs.

### A12 (follow-up). `fund_campaign` could not report failure **(RESOLVED)**
- **detail:** It called `_update_campaign`, which returns `None` and swallows every exception. `drive_campaign` early-returns on a `draft`, so a fund that silently failed left a campaign the user believed was running parked forever with no signal. Invisible at one campaign per click; a multi-tool launch funds N in a loop.
- **fix:** `fund_campaign(id) -> bool` over the existing `_cas_transition(id, "funded", ("draft",), ...)`. The launch route drives only what actually funded and reports the rest. CAS on `draft` also means it can no longer rewind a `running` campaign; nothing calls it that way today.

### A36. `/campaigns` still runs every PXDesign campaign at `binder_length=80`
- **severity:** medium (silent wrong parameter) | **owner:** code
- **detail:** `templates/runs/new.html` offers only `binder_length_min` / `binder_length_max`. PXDesign reads a **singular** `binder_length` (`tools/pxdesign/__init__.py:61`) and ignores the pair, so it falls back to its `"80"` default on every campaign and the user's entry is silently discarded. Same class as the bindcraft preset defect, without the visible 400.
- **partially addressed:** the Phase 2 launch screen has a dedicated `pxdesign__binder_length` field, so the target flow is correct. The single-tool form is untouched.
- *Next:* add a singular-length field to `runs/new.html` behind the existing per-tool show/hide, or fold the single-tool form into the launch screen at Phase 6.4. Related, lower impact: `rfantibody` campaigns always run at the default `cdr_lengths`, and `iggm` at `max_antigen_size=2000`, for the same reason.

### A37. `list_campaigns_for_user` still lists stranded drafts
- **severity:** low | **owner:** code
- **detail:** Phase 2 excludes `draft` from `list_campaigns_for_target` (a draft was never funded, dispatched or billed, so it is not a run and the page can offer no action on it). `list_campaigns_for_user` is a different query, applies no status filter, and is deliberately unchanged, so the two lists disagree about a stranded draft.
- **corrected 2026-07-29 (QC round 5):** this entry and the docstring citing it both said the query "feeds `/campaigns` and the homepage" and "three surfaces". **Measured: one production caller**, `blueprints/campaigns.py:95` (`GET /campaigns`). The homepage loads `list_jobs_for_user` (`tool_jobs`, not campaigns) and only renders a link to `/campaigns`. So a stranded draft does NOT appear on the homepage.
- **why it was not widened here:** it is a separate query on a separate surface and deserves its own review rather than riding along on a launch diff. The blast radius is smaller than this entry originally claimed, which makes it cheaper to fix, not less real.

### A38. `CAMPAIGN_STATUSES` is missing `paused_insufficient_funds`
- **severity:** low (latent) | **owner:** code
- **detail:** `shared/compute_campaigns.py:179` says it mirrors the CHECK in migration 0034, but 0035 (`0035_phase2_remove_daily_cap.sql:110-114`) widened the DB CHECK to add `paused_insufficient_funds` and the constant was never updated. The driver writes and reads that status regardless. Any future validation written against the constant would reject a live status.
- *Next:* add the member, or delete the constant if nothing validates against it. It currently has no consumers, which is the only reason this is latent.

### A39. `status_badge` has no tint for any campaign status
- **severity:** low (UX) | **owner:** code
- **detail:** `templates/components/status_badge.html:16-23` maps the six `tool_jobs` statuses. `draft`, `funded`, `completing`, `completed_with_failures` and `paused_insufficient_funds` are all campaign statuses and all fall through to the untinted default pill, so a paused-for-funds run is visually identical to a healthy one on the target page.
- *Next:* extend the tint map when Phase 6.4 builds the run strip.

### A40. Several route-exercising test files lack `isolate_supabase`
- **severity:** medium (test hygiene) | **owner:** code
- **detail:** `app.py` calls `load_dotenv()` at import and the repo-root `.env` holds real production credentials, so a test that boots `create_app()` and exercises a route reads production unless it opts into `tests/conftest.py::isolate_supabase`. `tests/test_compute_campaign_routes.py` was missing it (added in this diff). `tests/test_iggm_campaign.py` and `tests/test_csrf_protection.py` still are; both happen to mock their write paths, so the exposure is reads.
- **measured 2026-07-29 (QC round 3):** **26 of the 32 test files that reference `create_app` have no `isolate_supabase`.** The audit above named three by inspection and undercounted by an order of magnitude. Not fixed in this round: the fixture blanks four env vars for the whole module, so adding it to 26 files changes the environment of several hundred existing tests, which is its own change with its own blast radius (the reason the fixture was made opt-in in the first place, per its own docstring).
- **also in this round:** `tests/test_compute_campaigns.py` gained the fixture. It does not boot `create_app()`, so it was not in the class named above, and it still read production: its `fake_client` binds `cc.get_service_client`, while `plan_chunks` prices through `wallet_estimates._historical_p90_seconds`, which late-imports `credits.get_service_client`. Every planner test was pricing money against the live `tool_jobs_p90` view. The module docstring said "No live Modal or Supabase" throughout.
- *Next:* make the fixture autouse in `conftest.py` and have the few tests that genuinely want live config opt out, so the safe direction is the default. Note that patching one module's `get_service_client` is not sufficient on its own: any call that reaches pricing resolves `credits.get_service_client` at call time.

### Two documentation claims corrected in place
- `docs/HANDOFF-2026-07-29-phase2.md` asserted "bindcraft is not broken". Every `POST /campaigns` with `tool=bindcraft` returned 400 "Pick a preset." Fixed in this diff and the handoff corrected.
- The 8-phase plan's §2.2 listed a `framework` control for rfantibody. Its `validate` never reads one; `build_payload` hardcodes `"VHH"`. The real per-tool field is `cdr_lengths`.

## Addendum 2026-07-29b — independent QC over the Phase 2 diff

Eight findings, all confirmed by the reviewer against running code rather than
inferred from prose. All fixed in the same diff. Recorded because the pattern,
not the individual bugs, is the finding.

### The named recurring defect appeared five more times, about the same property
`plan_chunks` prices through `_estimate_chunk_cost` -> `estimated_cost_for_tool`
-> `_historical_p90_seconds`, which SELECTs `tool_jobs_p90`. Measured: **14
Supabase reads during `plan_multi_launch` for a 7-tool launch, plus 7 more in
`rows()`**. Five places claimed the opposite, including the `shared/target_launch.py`
module docstring that **this diff had just rewritten to correct the previous
purity claim** and a comment justifying a design decision as free.

The suite could not see it: `isolate_supabase` blanks `SUPABASE_*`, so
`get_service_client()` returns `None` and the p90 lookup short-circuits. Every
test exercised the branch production does not take. That is the same shape as
A35 (the CSRF fixture hiding the idempotency key defect) found earlier the same
day: **a fixture that makes tests safe can also make an entire code path
untested, and the two are hard to tell apart from inside the suite.**

Consequence beyond the prose: the estimate endpoint answered "would starting
narrow be affordable" by calling `plan_multi_launch` a second time, on a
debounced keystroke path, justified by a comment saying planning was free.
Replaced with `first_wave_at_pace(plan, pace)`, which re-prices from the plan
already in hand (only the concurrency division differs between paces) and is
pinned by an equivalence test against a full re-plan in both directions.

### The other findings
- **`_release_key` returned True without checking the delete matched anything.** A zero-row delete would report success, the caller would skip its cache fallback, and the orphaned claim would answer every retry with 409 for the rest of the TTL: exactly the outcome the function exists to prevent. Now returns `bool(response.data)`.
- **`concurrency_note` told a one-tool launch it was sharing.** One tool at `PACE_STEADY` narrows 16 to 8, so a note is due, but the sentence read "Running 1 tools at once shares one limit of 32 sub-jobs in flight". Wrong cause, wrong grammar, wrong that anything was shared. Split into two sentences: sharing the cap, versus the pace the user chose.
- **The launch page shipped both flag-gated slugs to every user** via `variant_preset_tools|tojson` in a `<script>` tag, undoing in one line the reason a gated tool is answered as "unknown" everywhere else. Now intersected with the visible set.
- **The fake Supabase query builder had no `neq`,** so the new draft filter raised `AttributeError`, which `list_campaigns_for_target`'s own `except` swallowed into `[]`. Four tests failed; worse, the swallow means any future client mismatch on that query degrades to "nothing has ever been run against this target" rather than an error. Same lesson as the standing rule that a test fake must model the backend's real behaviour.
- **Three assertions could not fail.** The affinity-maturation refusal test asserted only status 400, and that payload also fails the adapter's own mask check, so deleting the campaign-level refusal left it green; it now asserts the reason. A `patch("shared.storage.upload_input")` observed nothing because `blueprints/targets.py` never imports it. A concurrency floor test could not reach the floor it named.
- **Two more prose over-claims:** `campaign_tool_gated_off` claimed all callers answer indistinguishably (`api_runs_estimate` does not), and `_resolve_preset` cited the wrong form's disabled controls.

### What to carry forward
Prose in this repo is now the single most defect-dense part of a diff, and the
defects survive review because they read as documentation rather than as
claims. Two habits earn their keep: measure anything a comment calls free or
pure, and when a fixture makes a test pass, check whether it did so by removing
the behaviour under test.

## Addendum 2026-07-29c — second QC pass, over the Phase 2 FIXES

Nine more findings, over the diff that fixed round 1. This is the third time on
this project that reviewing the fixes has been as productive as reviewing the
original work, and the second time a comment written to fix a false-claim
defect contained a false claim.

### The fix for the mis-attributed concurrency note reproduced the bug it fixed
Round 1: one tool at `PACE_STEADY` announced *"Running 1 tools at once shares
one limit of 32 sub-jobs in flight"* to somebody running exactly one tool. The
fix branched on `len(specs) > 1`. But **two** tools at burst get `32 // 2 = 16`
each, which is exactly their solo width, so at n=2 the cap takes nothing and
all narrowing still comes from the pace: the width-based branch blamed the
platform limit for the user's own radio button, at n>=2 instead of n=1. The
docstring added by the fix asserted that the two causes were now distinguished.

Now detected on its own terms: cap narrowing is `divide_concurrency(tools,
PACE_BURST) < solo` (burst is the widest this module returns, so anything it
takes off is the cap), pace narrowing is `chosen < burst`. Both, either, or
neither sentence. Also corrected: *"so each starts narrower than it would
alone"* is false whenever proteina is in the launch, since it is pinned to 4
and a 7-way division leaves it there.

### A fabricated number and a fabricated mechanism
- *"``float(Decimal("280.91"))`` serializes as ``280.90999999999997``"* (in the `rows()` docstring **and** the test docstring). It does not: `str()` and `json.dumps()` both give `280.91`. The real, demonstrable loss is the quantum, `Decimal("4.0200")` -> `4.02`. The `str(Decimal)` change was right; the reason given for it was invented.
- *"a second full plan would roughly double the reads per keystroke"*. Measured at 7 tools: 28 reads with the shortcut, 35 without. 1.25x, not 2x. The optimisation is still worth having; the figure justifying it was not measured.

### Two assertions that could not fail, in tests written to prove the fixes
- `assert Decimal(row[key]) == Decimal(row[key].strip())` compares a whitespace-free string to itself. Replacing `rows()` with a `str(float(...))` variant left the whole test green. Now asserts `Decimal(...).as_tuple().exponent == -4` and equality against the plan's own figure.
- `test_rows_add_up_to_the_plan_totals` ran at 24 designs, where every tool plans 1 to 3 sub-jobs. `first_wave_hold_usd` clamps at `min(total_subjobs, concurrency)`, so dropping the divided concurrency entirely changed nothing. Now runs at 1000 designs and asserts the cohort is actually large enough to discriminate.

### The rest
- **`_form_fingerprint`'s "order-independent" became half-true.** Sorting the `(name, FileStorage)` pairs by name alone fixed the `TypeError` on a repeated field but left repeated parts in wire order, so the same two files posted in the other order would not dedup. Now builds the descriptor strings first and sorts those.
- **"Only `divide_concurrency` and `concurrency_note` are genuinely pure"** excluded two free properties, and said "per tool" where the cost is per SPEC (the class docstring exists specifically to say a repeated tool is two entries).
- **"it cannot disagree with the gate the POST will run"** overclaims: it matches `campaign_preauth`'s balance test exactly, but not the velocity or verification gates, so a launch also over the daily cap is offered a narrower start and then refused again.
- **`tests/test_iggm_campaign.py` had no `isolate_supabase`** while the sibling file edited in the same round gained one. It boots `create_app()`, so it pulled production credentials into `os.environ` for the rest of the pytest process and issued a real `tool_jobs_p90` SELECT (it patched `shared.compute_campaigns.get_service_client`, but `_historical_p90_seconds` resolves `shared.credits.get_service_client`). Added. See A40: the fixture should be autouse with opt-out, not opt-in.

### What to carry forward, sharpened
Round 1 said: measure anything a comment calls free or pure. Round 2 adds two
more that would have caught most of the above without a reviewer:
- **A number in a comment is a claim.** "2x", "280.90999999999997", "one read per tool" are all assertions, and every one of them here was wrong. Run it or drop it.
- **Fix the cause, not the symptom the reviewer showed you.** The n=1 note bug was reported at n=1 and fixed at n=1; the same defect at n=2 shipped in the fix. When a finding names one input, ask what class it belongs to before writing the branch.

## Addendum 2026-07-29d — third QC pass, and the items it filed rather than fixed

Four independent agents reviewed `git diff 02d2a24 HEAD` (4150 lines, 16 files),
one per slice: idempotency, routes + template, shared engine, test integrity.
Verdicts: FIX FIRST x3, SUITE PARTIALLY TRUSTWORTHY. No live production bug, but
two tests that could not fail, one money-copy bug, and a production read from a
test file. All ten remediation items (A-J in
`docs/HANDOFF-2026-07-29-qc-round3.md`) are done and mutation-verified.

The items below are deliberately NOT fixed. Each is either pre-existing, or a
fix whose blast radius exceeds this diff.

### A41. `blueprints/tools.py` error paths return HTTP 200, so `@idempotent` caches their failures
- **severity:** medium | **owner:** code
- **detail:** The release-on-failure logic in `shared/idempotency.py` keys on the status code: a response `>= 400` releases the claim so a corrected retry can run. `tool_submit`'s validation-failure paths re-render the form with a bare `render_template`, which Flask serves as **200**, so those failures are cached for the full TTL and a corrected resubmission inside 60 s replays the stale error page. The module docstring claimed "Failures are not cached" without qualification; corrected in this diff to state the status-code dependency.
- *Next:* give those paths real 4xx codes. That changes what the browser and every existing `tool_submit` test see, so it is its own change.

### A42. `_claim_key`'s upsert is a TOCTOU, not mutual exclusion
- **severity:** medium | **owner:** code
- **detail:** `_claim_key` SELECTs for a live row, then upserts with `on_conflict="key"`. `ON CONFLICT DO UPDATE` succeeds for **both** racing writers, so two concurrent identical submissions can each see "not claimed" and each run the handler. Pre-existing. The `_release_key` scoping added in this diff (`.is_("response_status", None)`) removes the new leg this created for `target_launch_submit` — a losing sibling can no longer delete the winner's cached success — but the underlying race is untouched.
- *Next:* a conditional insert that can actually fail (plain `insert` and treat the unique-violation as "lost the race"), or a DB-level advisory lock.

### A43. `_store_response`'s own failure path leaves `response_status` NULL
- **severity:** low | **owner:** code
- **detail:** If the UPDATE that caches the response fails, the claim row keeps `response_status NULL`, which reads as "in flight" to every subsequent request for the rest of the TTL — the 409-until-expiry state `_release_key` exists to prevent, reached by a different route. Pre-existing.

### A44. The single-tool `/campaigns` route still discards `fund_campaign`'s bool
- **severity:** low | **owner:** code
- **detail:** `fund_campaign` now reports whether the row actually moved out of `draft` (audit item A12), and the multi-tool launch route branches on it to report stalled runs. `blueprints/campaigns.py` still ignores the return, so a single-tool campaign whose fund silently fails is left `draft` — inert and unbilled, but presented to the user as started. A12 is half closed.

### A45. A deliberate identical re-launch inside 60 s is a silent no-op
- **severity:** low (UX) | **owner:** product
- **detail:** `@idempotent()` on `target_launch_submit` cannot distinguish a double-click from a user who genuinely wants the same tools run against the same target twice (adding 400 more BindCraft designs is a legitimate launch). The second is answered from cache and replays "Started N runs", so the UI asserts something that did not happen. The TTL is 60 s, so the window is small, but nothing marks a replay as a replay.
- *Next:* surface `Idempotent-Replay` in the banner, or give the form a nonce the user's second submit would change.

### What this round adds to the carry-forward list

Rounds 1 and 2 said: measure anything a comment calls free or pure, and a number
in a comment is a claim. Round 3 adds two about tests rather than comments.

- **A test that cannot fail is worse than a missing test**, because it is
  counted. Both dead tests here were dead for the same reason: the fixture's
  arithmetic collapsed the distinction under test. `32 // 2 == 16` is
  rfdiffusion's solo width, so dividing concurrency across 2 tools is an
  identity. One sub-job per tool clamps the first wave to 1, so burst and steady
  price identically. Neither is visible by reading the test. Both were found by
  computing the two branches and comparing.
- **So state the precondition as an assertion.** Every cohort-sensitive test in
  this round now carries one: `_assert_pace_is_observable_on`,
  `_assert_float_encoding_is_distinguishable`,
  `test_the_typed_field_cases_all_differ_from_the_adapter_default`,
  `test_csrf_enforcement_is_actually_on_in_this_fixture`,
  `test_a_nul_bearing_value_survives_form_decoding`, and an inline guard in the
  page-budget test. If someone later shrinks a cohort to something cheaper, the
  precondition fails with a message naming the fix instead of the test quietly
  passing forever.
- **A fake that omits a method does not fail loudly; it fails silently in the
  safe-looking direction.** Two instances this round. `tests/test_idempotency.py::_FakeTable`
  had no `is_()`, so the `_release_key` scoping added in this diff raised
  `AttributeError` into a bare `except` and the fix read as a no-op — both the
  fix and its removal left 26 tests green. `tests/test_compute_campaigns.py::_Query`
  had no `update()`, so `fund_campaign`'s CAS could not be modelled at all and
  had zero coverage across 9 behaviours. Compare
  `feedback_test_fakes_must_model_backend_limits`: same lesson, third occurrence.

## Addendum 2026-07-29e — independent QC over the round-3 REMEDIATION

Two agents over the 1759-line remediation diff: one on production code, one on
test integrity. Verdicts: **FIX FIRST** and **SUITE PARTIALLY TRUSTWORTHY**.
The test agent ran 35 production mutations; 33 reddened a named test, 2 did not.

**Fourth consecutive round in which reviewing the fixes was as productive as
reviewing the original work**, and this time the fix contained a defect strictly
worse than the one it fixed.

### The fund/drive guard inverted the reporting on a money path
Round 3 wrapped the launch route's fund/drive loop so nothing could raise past
it. The wrap set `moved = False` in the handler. But **`fund_campaign` cannot
raise** — `_cas_transition` catches everything and returns `False` — so the only
exception the block could ever catch came from `drive_campaign_async`, i.e.
`threading.Thread(...).start()`, i.e. **exactly the case where the campaign is
already `funded`**. And `funded` is in `cron/tick_campaigns.py::_ACTIVE_STATES`,
so the tick drives it within a minute or two and it bills.

So the guard reported N funded, billing campaigns as *"None of those runs could
be started. Your wallet was not charged."* Thread exhaustion is process-wide, so
every tool in a launch fails together and `started` would be empty every time.

The second half is worse than the wrong banner. That answer is a **400**, and
round 3's own `shared/idempotency.py` change releases the claim on any status
>= 400 — so the retry the error copy invites would create and fund a **second
full set** against a gate the user passed once. At HEAD the exception propagated
to a 500, which left `response_status NULL` and 409'd every retry: accidental
protection that the fix removed and replaced with an invitation.

Now: the fund is the sole commit point and the only thing that decides
started-vs-stalled; a drive-spawn failure logs and the campaign stays *started*,
because the cron owns it. Three tests added — **no test anywhere made
`fund_campaign` or `drive_campaign_async` raise**, which is how a whole-launch
reporting inversion shipped inside a QC fix.

### `_release_key`'s scoping was necessary but not sufficient
Scoping the release to `response_status IS NULL` stops a losing sibling deleting
the winner's cached success. It does not stop the loser **overwriting** it: when
the release matches nothing the wrapper falls through to `_store_response`, whose
UPDATE was unscoped. The loser's 400 replaced the winner's cached 302, so the
browser was shown "nothing was started and nothing was charged" for a funded,
billing launch, and every click inside the TTL replayed it. `_store_response` now
carries the same predicate. The isolated `_release_key` test could not see this;
it is the composition that fails.

### The consent-staleness fix cleared the variable, not the number
`debounced()` cleared the internal `latest` and unticked the box but left the
PREVIOUS launch's total and held-to-start **rendered**, for the whole repricing
window. The rendered figure is the only price a user reads, and someone who sees
the tick vanish re-ticks while "$3.20" is still on screen. `clearTotals()`
already existed for this. Separately, `fetchEstimate` had no request-generation
guard, so with two fetches in flight the one that RESOLVED last won — not the one
describing the form as it now stands. Both fixed (`clearTotals()` in
`debounced()`, plus a monotonic `reqSeq` every response checks).

### Tests that could not fail, inside tests written to prove the fixes
- `test_no_placeholder_survives_for_any_reason_or_count` parametrized over a
  hardcoded 5-tuple rather than `_PREAUTH_MESSAGES`. A new refusal reason is the
  normal way a new placeholder arrives, so the case the test exists to catch
  shipped green: proven by adding a `{oops}` reason and getting 94 passed. Now
  derived from the table, with a companion test that the table is complete.
- `test_a_launch_touches_the_target_once_after_every_insert` asserted count and
  argument against a standalone mock, which observes neither order. Moving
  `touch_target` from after the create loop to just after the preauth gate left
  it green. Now logged in the same recorder as create/fund/drive and asserted
  positionally.
- `test_singular_and_plural_copy_differ_where_the_subject_appears` read its
  branch off the very table it asserted on, so stripping the count-sensitive
  placeholders flipped it into the `else` arm where `one == many` then held. The
  regression asserted itself away. Count-sensitivity is now declared per reason.
- The typed-field precondition claimed to relaunch "with the field ABSENT", but
  `_form()` hardcodes `rfdiffusion__binder_length_min/_max`, so it measured the
  FORM default. It agreed with the adapter's only by coincidence.

### Four more false claims, three of them numbers
- **"that total (3.5176)"** — rfdiffusion@12 plans 2.0101 and 2.6219. 3.5176 is
  not produced by that cohort at any figure; it is half of the two-tool total,
  presented as measured. (Caught by the author before review, which is the only
  reason it is not a fifth.)
- **"all four asserted figures carry a trailing zero"** — the test asserts SIX,
  and two carry none (rows 20.1009 and 30.1461). The `any()` precondition still
  holds, so the cohort works; the count was invented.
- **"For all of these except pxdesign the two defaults are byte-identical"** —
  pxdesign's template default is `80` and its adapter default is `"80"`, so it is
  byte-identical too. The carve-out named the wrong tool, and pxdesign is not
  even in the list it annotates.
- **"the previous tests here all drove launched_count to 0"** —
  `test_the_banner_counts_only_the_runs_from_this_launch` asserts "Started 2
  runs". The true statement is that no prior test combined a MATCHING group with
  a non-zero `stalled`.
- Also corrected: a **wrong audit citation** ("Filed as A35", which is a
  different and already-RESOLVED item; the right one is A37); a claim that
  `shared.tools_catalog` entries carry a trailing "-- one line about the tool"
  (measured across all 14 registered adapters: not one label contains any dash,
  and the catalog stores the tagline in a separate key); and a claim that a user
  seeing a stranded draft on /campaigns "can still clear it" (`cancel_campaign`
  would accept it, but no template renders a Cancel control for `draft`).

### What to carry forward, sharpened again
Rounds 1-3 gave: measure anything called free or pure; a number in a comment is
a claim; fix the class, not the reported input; state the precondition as an
assertion. Round 4 adds two that are specifically about *fixes*:

- **A guard is a claim about which exceptions can reach it.** The fund/drive wrap
  was written for the failure the reviewer described (money committed, exception
  escaping) without checking which callee could actually raise. Both callees were
  in the same file; one line of reading would have shown that the reachable case
  was the opposite of the one being handled. Before catching, enumerate what
  throws.
- **A fake that omits a method fails silently in the safe-looking direction, and
  refusing beats omitting.** Four instances now across two rounds:
  `_FakeTable.is_()` missing (the release scoping read as a no-op, and both
  adding and removing the fix left 26 tests green), `_Query.update()` missing
  (`fund_campaign`'s CAS unmodellable, 9 behaviours uncovered), `_FakeTable`
  honouring `is_` on DELETE but not UPDATE (the scoping looked effective while
  the clobber still happened), and `_IdemTable.delete()` missing (swallowed into
  a `False` return that caches instead of releasing). The pattern that works is
  the one `is_()` now uses: **raise on anything not modelled**, so an unmodelled
  path is a loud error instead of a plausible wrong answer. See
  `feedback_test_fakes_must_model_backend_limits` — third and fourth occurrences.

## Addendum 2026-07-29f — QC round 5, over the round-4 fixes

One agent over the 461-line production slice of the round-4 remediation.
Verdict **FIX FIRST**. It ran the four safe test files (226 passed) and
re-checked the launch JS with `node --check`.

**Fifth consecutive round finding defects in the previous round's fixes, and the
second in a row where the defect was in a fix to a money path.**

### `fund_campaign` returning False is AMBIGUOUS, and round 4 leaned on it
Round 4 made the fund the sole commit point: `False` -> stalled -> "was not
charged". But `_cas_transition` catches **every** exception and returns `False`,
so `False` means either "the row was not in draft" or "the UPDATE raised and I
cannot tell". A write that commits in Postgres while the response read times out
lands in the second bucket (see `reference_tools_hub_supabase_http2_railway` for
that failure class on this stack). The route rendered it as the first: the
campaign is `funded`, it bills, and the user is told it was not charged and
invited to launch it again.

This is round 4's own headline defect arriving through the other branch, which is
the point: fixing the reported input is not the same as fixing the class.

Now the route confirms a `False` with an owner-scoped `get_campaign` and only
calls a campaign stalled when the row is **confirmed** still `draft`. Anything
else (moved, or unreadable) is treated as started, because claiming "not charged"
about money that may be committed is the more expensive error and the one that
produces the duplicate. Five tests added; and `_launch()` now models the
confirming read, without which every `fund_results=[False]` test silently took
the indeterminate branch.

### A46. The campaign tick has no schedule in this repo
- **severity:** medium | **owner:** ops (Leo)
- **detail:** Round 4's reasoning ("a funded campaign has started, because the
  cron drives it") depends on `campaigns:tick` actually running. `"funded"` is in
  `cron/tick_campaigns.py::_ACTIVE_STATES` and that module puts a tick at
  ~60-90 s, but the schedule lives **outside the repo**: the `Procfile` declares
  only `release` and `web`, there is no `railway.json`/`railway.toml`, and
  `campaigns:tick` is a Flask CLI command with no in-repo caller. The inline hook
  in `shared/jobs.complete_job` cannot substitute, because a campaign whose
  first-wave drive never spawned has no children to complete.
- **failure scenario:** thread exhaustion on a multi-tool launch plus an absent or
  paused Railway cron leaves N campaigns parked at `funded` forever, with an
  untinted badge (A39), while the user reads "Started N runs".
- **RESOLVED 2026-07-30 by direct observation**, not by document. Checked in the
  Railway dashboard: service `tools-hub-campaigns-tick`, start command
  `flask --app app campaigns:tick`, schedule `*/5 * * * *`, source
  `leowan7/tools-hub` branch `main` with auto deploy, current deployment the
  PR #99 merge. Recent executions ran unbroken every 5 minutes from 11:05 to
  12:11 and every one succeeded; it fired live during the check and took 8 s.
  Sibling cron `tools-hub-sweep-stuck` also present and succeeding.
  **The round-4 reasoning holds** and no code change is needed.
- **Correction:** this entry and `docs/COMPUTE-CAMPAIGNS-PLAN.md:66` both said
  the tick runs at ~60-90 s. It runs every **5 minutes**. A campaign stranded at
  `funded` therefore waits up to 5 minutes, not 90 seconds. Functionally fine,
  and one more document asserting a property the system does not have.
- **Watch item, not a defect:** run durations are mostly 4 to 30 s but the two
  most recent were 1m59s and 2m21s. Still inside the 5 minute window so nothing
  overlaps, but the trend is upward.

### A47. The launch page's JavaScript has no automated coverage at all
- **severity:** medium (test hygiene) | **owner:** code
- **detail:** `grep -rl "reqSeq\|clearTotals\|Repricing" tests/` returns nothing.
  Three consecutive rounds have found defects in this one `<script>` block (the
  consent surviving a reprice, the previous price staying rendered, the
  superseded-response race) and it is still only syntax-checked with
  `node --check`. The block is what decides whether the submit button is live
  against a price the user agreed to.
- *Next:* the cheapest real coverage is a headless-DOM harness over the extracted
  script with `fetch` stubbed; a browser walk needs auth and a real target, which
  needs production credentials.

### Corrected in place
- **"three surfaces"** (both `list_campaigns_for_target`'s docstring and A37
  itself): `list_campaigns_for_user` has exactly **one** production caller,
  `blueprints/campaigns.py:95`. The homepage loads `list_jobs_for_user`
  (`tool_jobs`, not campaigns) and only links to /campaigns, so a stranded draft
  never appears there.
- **`_IdemTable.delete()` raising `NotImplementedError` did not "fail loudly".**
  `_release_key` wraps `.delete()` in a bare `except Exception`, so raising is
  swallowed into the same `False` as omitting the method, and the wrapper then
  caches the failure. The stub therefore inverted the behaviour under test as
  silently as a missing method would, and made the double-fund assertion in
  `test_a_drive_spawn_failure_does_not_release_the_idempotency_claim`
  **unfailable**. Now modelled faithfully; the assertion has been verified to
  fire, with its own message, under a mutation that releases regardless of status.
  Note the asymmetry this teaches: `is_()` CAN refuse, because it is called on the
  builder outside the `except`; `delete()` cannot.
- **The coercion comment overstated its protection.** `Decimal(str(x))` raises
  `InvalidOperation` for a non-numeric string, for `inf`, and for a large
  exponent, and **NaN does not raise at all** -- it quantizes to NaN and rendered
  "about $NaN to start". Now guarded with an `is_finite()` check and a
  try/except that falls back to the wording, so the claim is true. The
  parenthetical about an `AttributeError` also described no state that existed:
  HEAD's expression was `f"${required:.2f}"`, which formats floats fine.
- **The length prefix closed the boundary between parts but not inside one.**
  Parts were a single `f"{field}={value}"` string, and a field NAME may contain
  `=` (`%3D` decodes into `request.form`), so `{"a": "b=c"}` and `{"a=b": "c"}`
  both encoded to `5:a=b=c`. Each component is now framed separately
  (`1:a3:b=c` vs `3:a=b1:c`).
- **A "file" tag was added to file descriptors with a comment claiming it stops a
  form field being spelled as one. Removing the tag changes no test**, because the
  outer per-part prefix already frames each part whole. The comment now says the
  tag is a debugging aid and the framing is the guard. Caught by mutating the
  author's own new code, which is the only reason it is recorded as a correction
  rather than a fifth false claim.

### Carry-forward, after five rounds
Cumulative: measure anything called free or pure; a number in a comment is a
claim; fix the class, not the reported input; state the precondition as an
assertion; a guard is a claim about which exceptions can reach it; a fake that
omits a method fails silently in the safe-looking direction. Round 5 adds:

- **A boolean from a function that swallows exceptions is three-valued.** `True`,
  `False`, and "I could not tell" all arrive as two values. Any user-facing claim
  built on such a bool -- especially "you were not charged" -- has to either
  confirm independently or refuse to make the claim. Two rounds in a row produced
  a money-reporting inversion from treating one of these as definitive.
- **Mutate your own fix, not just the code it touches.** The false "file tag"
  claim and the unfailable double-fund assertion were both found by mutating
  lines written minutes earlier. Adding a test is not evidence the test can fail.

## Addendum 2026-07-29g — QC round 6, over the round-5 fixes

Two independent agents: one over the production slice of `216a2b5` (with the
four unreviewed round-5 fixes as its primary target), one auditing whether the
~1700 new test lines can actually fail. Verdicts **FIX FIRST** and **SUITE
UNTRUSTWORTHY**. Every finding below was re-verified against source before it
was acted on; one was partly misattributed and is recorded as such.

**The round-5 fixes themselves held.** For the first time in six rounds no money
inversion was found in the previous round's work, and unusually, every factual
claim in the round-5 comments measured out (14 adapter labels, the single
`list_campaigns_for_user` caller, the exact `runs/detail.html` status list, the
`_framed` collision pairs, 26 probed inputs to `preauth_message`'s guard). What
broke was around them.

### The stalled disclosure was suppressed exactly when it was needed
`templates/targets/detail.html` nested the stalled block inside the started one.
`launched_count` comes from `list_campaigns_for_target`, which excludes `draft`
and is documented as returning short on a paging error, so a launch that funded
some campaigns and stranded others could arrive with `launched_count == 0`:
either that read failed, or every campaign took round 5's own "row unreadable,
treat as started" fall-through while still in draft. Both are the SAME
connection fault that stranded the campaigns, so the nested block went dark in
precisely the case it existed to report -- the user funded real compute, one run
did not start, and the page said nothing at all.

The two halves are now gated independently. The cost, taken deliberately: a
crafted `stalled` param now renders. The template cannot tell a crafted count
from a real one whose run query came back empty, and only one of those two
misleads somebody other than its author.

Also corrected: `list_campaigns_for_target`'s docstring claimed "the launch
route tells the user what did and did not start, so the omission is disclosed
there". It is disclosed by the query PARAM and the template, neither of which
this function controls, and it was conditional on a read this same function
documents as unreliable. (The reviewer also attributed this claim to
`blueprints/targets.py:478-480`; that comment is about draft exclusion and is
accurate. One location, not two.)

### The launch template asserted "nothing was charged" under every error
`templates/targets/launch.html` printed it unconditionally whenever `error` was
set -- the exact claim round 5 had just restricted to a confirmed draft, made by
the one component that cannot know whether a campaign was funded. It held only
by accident of where the six `_err` sites happen to sit, and it printed the
sentence TWICE in one panel, because the route's own message said it too.

Now derived, not asserted: `target_launch_submit` sets `funded_any` at the
commit point, `_err` passes `nothing_charged=not funded_any`, and
`_launch_context` defaults it to **False** so the claim is never made by
omission. An error path added after the fund loop stops making the claim on its
own, with nobody having to remember why.

### The consent figure rounded the hold DOWN
`money()` in the launch page rounded the 4dp wire values to 2dp to NEAREST, so a
$2.6219 hold printed as "$2.62" directly above a checkbox reading "the amount
above will be held against my wallet balance". This is the identical
understatement `preauth_message` calls out by name and that round 5 fixed for
the refusal sentence -- left in place on the number the user actually consents
to.

Fixed in Decimal rather than JS, because that is where the money discipline
already lives and because the launch script has no automated coverage at all
(A47): `display_cost_usd` (ROUND_CEILING) and `display_balance_usd`
(ROUND_FLOOR) ship `*_display` strings, and `money()` now prepends a symbol and
nothing else. A static test forbids any fixed-point rounding call in that
template. The asymmetry is documented where it shows: a balance between a
requirement and that requirement's ceiling reads as "$573.67 available" under
"$573.68 to start" while still being affordable, which is the safe direction and
does not gate the button.

Writing the guard test found a bug in the guard: a Decimal NaN quantizes without
raising, so the first version of these helpers would have rendered "$NaN". Same
trap round 5 recorded for `preauth_message`, walked into again one round later,
which is the argument for the test rather than for the care.

### `ComputeCampaign.from_row` sat outside four callers' `try`
`from_row` subscripts five columns directly and coerces five more with `int()`.
`get_campaign`, `list_campaigns_for_user`, `list_campaigns_for_target` and
`sweep_paused_campaigns` all called it OUTSIDE the `try` that exists to make
them total, so one unreadable row escaped as a 500 from `/campaigns` and the
target detail page, or aborted the paused-campaign sweep part way. It falsified
`list_campaigns_for_target`'s own "a short run strip beats a 500", and it
mattered most in the fund/drive loop, which calls `get_campaign` after the
commit point where a raise 500s a request that has already spent money and
releases the idempotency claim with it. All four now route through
`_campaign_or_none`. Not reachable through `select("*")` on a schema the
migrations pin; fixed because those functions promise not to raise.

And the claim at `shared/idempotency.py:492` that `target_launch_submit`'s loop
"catches and returns a partial-success redirect rather than propagating" was
false -- it wraps one of its three fallible statements. The property is real but
comes from the TOTALITY of its callees, and the comment now says so, and says
not to restate it as catching.

### Tests that could not fail
- `test_the_banner_counts_never_double_count_a_run` was a duplicate of a wording
  test wearing a docstring about arithmetic. `_render_detail` patches
  `list_campaigns_for_target` out, so `launched_count` was a property of the
  fixture: the reviewer reintroduced BOTH regressions the docstring names
  (widening the draft filter, and a drive-spawn failure re-routed to stalled)
  and it stayed green. It now seeds a funded and a draft row into a fake client
  and lets the real query produce the count.
- `test_fund_campaign_reports_false_with_no_client` could not observe its guard:
  delete the `client is None` check and the resulting AttributeError is caught
  by `_cas_transition`'s own bare `except`, returning the same False.
- `test_a_missing_required_amount_falls_back_to_words` could not observe the
  `is not None` half: coercing None raises and is caught, so the fallback
  arrives either way.

Both of the latter now assert on the LOG. A guard's only signature is that it
returns without going through the handler, so the handler's warning must be
absent. Same shape both times: **a bare `except` at the call site erases the
difference between "guarded" and "recovered", so the result cannot distinguish
them and only the log can.**

### The `is_` refusal rationale was wrong, in both fakes
Round 5 recorded that `is_()` may raise where `delete()` may not, "because `is_`
is called on the builder before `execute`, outside that except". False:
`_release_key` wraps the entire chain, `.is_()` included, in the `try` whose
bare `except Exception` returns False, and `_store_response` calls its chain
from inside a closure invoked in the same kind of `try` -- where the swallow is
worse, since it retries, logs, and returns having cached nothing. Nothing in
either fake can usefully refuse.

Both fakes now accept the `"null"` string as well as `None`. This repo issues
the string in 14 places (`shared/api_keys.py`, `shared/targets.py`,
`shared/jobs.py`, `shared/handoffs.py`, `shared/compute_campaigns.py:1997`,
`cron/`); only `shared/idempotency.py` passes None. A fake refusing the majority
convention would have broken silently on the refactor that adopted it.

`_IdemTable.delete`'s comment also claimed to keep "any assertion about a
released claim" failable. Removing the method changes no result in that file --
no test there reaches the release path. It is retained for the mutation under
which the leg becomes observable, and the comment now says that instead.

### A48. The same money-rounding defect is LIVE on the single-tool route
- **severity:** medium (live) | **owner:** Leo to scope
- **detail:** `templates/runs/new.html:212` is a byte-identical `money()`
  helper, feeding `rp-budget`, `rp-perchunk`, `rp-firstwave` and `rp-balance`
  above its own consent checkbox at `:182`. `/campaigns` is deployed, so every
  non-2dp hold on that screen is understated today.
  `templates/wallet/_partials.html:195` has the same shape (`fmt()`) on a top-up
  deficit figure -- suspected, not confirmed.
- **RESOLVED 2026-07-30, round 7** (Leo authorised it explicitly). Filed as "not
  fixed here" because it is deployed code on a different endpoint
  (`api_runs_estimate`) with its own tests. `api_runs_estimate` now ships
  `*_display` strings for all four figures and `templates/runs/new.html` does no
  arithmetic. Checked, not assumed: this panel is NOT the H1 shape, because
  `BUDGET_BUFFER` is 1.15 so the budget is not the per-chunk price times the
  sub-job count, and nothing on it is presented as the total of anything else.
  `templates/wallet/_partials.html:195` remains **unconfirmed and untouched**;
  its inputs come from a different estimator and were not measured.

### A49. Both fakes assume PostgREST returns the updated representation
- **severity:** low (unverified assumption) | **owner:** code
- **detail:** `_cas_transition` decides whether a CAS won by inspecting
  `resp.data`, and `_store_response` depends on the same. Every fake in the
  suite returns the matched rows from `.update()`, which is correct for
  `returning=representation` (the supabase-py default) but has never been
  verified against the live backend. If a future client version or a
  `Prefer: return=minimal` default changed it, every CAS in this module would
  read as a loss and the suite would stay green.

### A50. The confirming read narrows the fund ambiguity but cannot close it
- **severity:** low (inherent) | **owner:** none
- **detail:** if the fund UPDATE commits in Postgres after the client-side read
  timed out, a confirming `get_campaign` that lands before that commit sees
  `draft`, and the route answers 400 "nothing was charged" for a row that then
  becomes `funded` and is driven by the tick. Inherent to a non-transactional
  confirm. Recorded rather than fixed; closing it needs the fund and the read in
  one transaction, which this stack does not offer through PostgREST.

### Carry-forward, after six rounds
Round 6 adds two:

- **A bare `except` at the call site erases a guard's signature.** When the
  unguarded path is swallowed into the same return value, the result cannot
  distinguish guarded from recovered, and a test asserting only the result
  cannot fail. Assert the log, or assert nothing.
- **Patching out the function that computes the number under test makes the
  number a fixture.** `test_the_banner_counts_never_double_count_a_run` survived
  both regressions it named. If a test is about a COUNT, the count has to come
  through the code that derives it.

Still true, and now six rounds deep: a number in a comment is a claim; fix the
class, not the reported input; state the precondition as an assertion; a guard
is a claim about which exceptions can reach it; a boolean from a function that
swallows is three-valued; mutate your own fix.

## Addendum 2026-07-29h — QC round 7, over the round-6 fixes, plus A48

One agent over the uncommitted round-6 diff, with the three claimed mutations
to reproduce itself. Verdict **FIX FIRST**. It restored the working tree
byte-identically and said so, which is the check that makes a mutation report
worth reading. Every finding below was re-verified against source or by
measurement before it was acted on.

Leo separately authorised the A48 fix (the same rounding defect, live on the
single-tool campaign route), so that is in this round too.

### The round-6 money fix introduced a money regression
Ceiling each row independently and ceiling the exact total are different
numbers: `sum(ceil(row)) >= ceil(sum(row))`. The launch panel prints the rows
directly above the totals, so the column stopped adding up to its own headline.

Reproduced exactly on the cohort the reviewer named, rfdiffusion@12 +
pxdesign@12 at burst: Cost rows of $2.02 and $5.03 above a printed **Total cost
$7.04**, held rows of $2.63 and $6.56 above **Held to start $9.18**. One cent
short in both, immediately under "I understand the amount above will be held".
Measured across 2- to 5-tool cohorts, 50 of 52 tables no longer added up, where
the old nearest-rounding produced 18 of 52 and unbiased.

The sum is the one part of a consent panel a reader can check without trusting
us, so it is the part that must not be wrong. Fixed with `display_total_usd`,
which sums the DISPLAYED rows instead of re-rounding the exact total. Still a
true ceiling: each row display is at or above its own exact value, so the sum is
at or above the exact total, and the gate is applied to the exact figures. Now
measured clean: 224 cohorts, 0 mismatches, 0 totals below exact.

The pre-existing `test_the_estimate_totals_equal_the_sum_of_its_rows` states
this invariant in prose and compares only the 4dp fields, which always agreed
and still do. It could not see this. `rows()` even carried a comment
acknowledging "a 2dp row does not add up" as a reason to keep the 4dp field, and
never followed the thought to what the page actually prints.

**A48 is fixed and is NOT the same shape.** On `/campaigns/new` the reason is
`BUDGET_BUFFER = 1.15`: the budget is not the per-chunk price times the sub-job
count, so the H1 correction genuinely does not apply. Checked rather than
assumed, because the reflex was to apply it to both.

(The second reason originally given here, that "nothing on that panel is
presented as the total of anything else on it", was wrong and is struck. The
panel labels the headline "Estimated total" and prints "Sub-jobs" and "Per
sub-job" directly beneath it, so a reader is plainly invited to multiply. The
product misses by 15%, not by a cent, which is why the rounding fix is still
correct as made. Caught in round 8.)

### Three of the change's own safety mechanisms could be reverted green
- **The commit-point flag.** Deleting `funded_any = True` left the suite fully
  passing: no error path after the fund loop is reachable, so the `True` branch
  had no coverage, and a tidy-up of "an assignment nobody reads" would have
  restored the round-5 defect. Replaced by deriving from `started`, the list the
  redirect already counts. **The claim made here that "existing tests do pin"
  that derivation was false, and round 8 proved it**: those tests pin `started`
  as a list, not `nothing_charged`'s dependence on it, and `nothing_charged=True`
  passed the whole suite. See addendum 2026-07-29i.
- **Three of the four `_campaign_or_none` conversions.** `get_campaign`,
  `list_campaigns_for_user` and `sweep_paused_campaigns` each reverted to bare
  `from_row` with the suite green; only `list_campaigns_for_target` was pinned.
  `get_campaign` carries the change's strongest money claim. All three now have
  a test, and all three mutations now redden.
- **The JS drift guard.** It checked the arguments of `money()` and the absence
  of a fixed-point call, so `'$' + d.balance_usd` rendered the raw 4dp balance
  into the page with the suite green. The guard now also forbids any reference
  to an exact money field anywhere in the template, and lives in
  `tests/money_display_guard.py` so the launch page and the campaign page cannot
  drift apart. Both pages pass it.

### A test that could not fail in the direction it named
`test_the_estimate_never_overstates_the_balance` used $573.6736, where FLOOR and
NEAREST are both 573.67. It could only ever have caught a switch to ceiling, not
the switch to nearest the whole change exists to prevent, and it passed with
`display_balance_usd` set to ROUND_HALF_UP. Now $573.6756, with the
ceiling-vs-nearest precondition asserted the way the sibling cost test already
did it.

### The fake disabled the code the new test was written to exercise
`sweep_paused_campaigns` issues `.lt()` and `.is_()`, and both queries sit in a
try whose bare `except` turns an AttributeError into an empty list. The fake
`_Query` had neither method, so the sweep's entire body never ran and the first
version of the sweep test passed while exercising nothing. Both are modelled
now, and the sweep test asserts the good row still gets notified rather than
merely that the call returns.

`neq` was also wrong in a way that decides what the code can be handed:
PostgREST renders it as `col <> val`, which is NULL for a NULL column and so
DROPS the row, where Python `!=` keeps it. Corrected, and pinned by a test. That
also settles the fixture question the reviewer raised: a NULL-status row is a
malformation this query cannot deliver, so the unreadable-row fixtures now carry
a real status and fail on a missing `tool`/`preset` instead.

### False claims in comments, again
- The banner comment still said an unknown group "simply does not render" after
  the diff deliberately made a crafted `stalled` render on its own. The diff had
  removed that same sentence from the test and left it in the route.
- The un-nesting was justified with "every campaign took the fall-through",
  which cannot occur: if every campaign takes it, nothing is stalled, the route
  drops the query param, and there is nothing to suppress. The real cause is the
  MIXED outcome, plus the failed read. Corrected in both places it appears.
- "The only caller is the estimate endpoint" was said of both display helpers;
  `display_cost_usd` is also called by `MultiLaunchPlan.rows()`, and now by
  `api_runs_estimate` too.
- The fail-closed mechanism was described as "unticking the consent box".
  Neither failure handler unticks it. The mechanism is the DISABLED button, and
  on `/campaigns/new` that was not even true until this round: the handler set a
  warning and left `latest` holding the last successful estimate, so the button
  stayed armed beside figures priced for a different design count. Fixed, and
  that is what makes the helpers safe to raise.
- **A number in a comment, wrong for the third time.** The `is_` null-spelling
  count went 13, then 14, now 15. Re-derived by listing all 17 `.is_()` call
  sites: 2 pass `None` (both in `shared/idempotency.py`), 15 pass `"null"`, and
  the module list had been omitting `webhooks/modal.py`.
- `target_id` and `user_id` were described as keyword-required; `user_id` has a
  default, so the rationale covered a parameter it did not apply to.

### Known limits, stated rather than left implied
- Blanking the body of the fake's `is_()` leaves the sweep test green. Only the
  method's ABSENCE is pinned, which is the failure the comment warns about; the
  filter's fidelity is not observable through this fixture.
- Neither page's `<script>` is executed by any test (A47). Every guard here is
  static: they prove the two known routes to a wrong figure are closed, never
  that the right figure is printed.
- ~~The steady-pace `alternative` figure is still ceiled from its exact value.
  It is prose with no row breakdown beside it, so it has no column to agree
  with.~~ **STRUCK 2026-07-30 (round 9).** False when written, and in the same
  change that made it false: round 8 moved the alternative to
  `first_wave_display_at_pace`, a row sum. The two differ in **64 of 120**
  reachable burst cohorts (max gap 2 cents), re-measured this round. The
  "no column to agree with" reasoning is the error: the column is on the screen
  the user reaches by ACTING on the offer, which is what the sentence promises.
  It is still compared against the balance as an exact value; see S1 below.

### Carry-forward
Round 7 adds one, and it is about the fix rather than the code:

- **A rounding rule is not local.** Choosing a direction for a single figure is
  arithmetic; applying it to figures a reader will add up is a change to what
  the panel asserts. Before rounding anything, ask what else on the page is
  supposed to reconcile with it.

Still true after seven rounds: a number in a comment is a claim, and this one
was wrong three times running; a fake that omits a method fails silently in the
safe-looking direction, and it did so again here on the very test written to
close the previous round's gap; mutate your own fix, because two of this round's
new tests could not fail when first written.

## Addendum 2026-07-29i — QC round 8, over rounds 6 and 7

One agent over the whole uncommitted stack, told to treat round 6 as reviewed
but not proven and round 7 as the primary target. Verdict **FIX FIRST**. It
restored 13 mutated files and verified each by sha256, which is the check that
makes a mutation report worth reading. Both HIGH findings were reproduced by
measurement before anything was changed.

### The round-7 money fix moved the same defect one screen over
Round 7 made the launch panel total its rows' 2dp displays, so the column adds
up. It did not ask what ELSE prints that number. `preauth_message` still
rounded the exact total up, and `sum(ceil) >= ceil(sum)`, so the refusal
sentence and the panel it renders beside disagreed.

Reproduced: rfdiffusion@12 + pxdesign@12 at burst, refused. The red panel reads
"about **$9.18** to start"; the estimate panel on the same 400, above the line
"the amount above will be held", reads **$9.19**. A user who tops up to the
sentence's figure is refused again by the same sentence. Measured across 2- to
7-tool cohorts at 12 designs, both paces: **128 of 240 refused cohorts printed
two different holds**.

This is the round-7 finding recurring inside round 7's own fix. The question
asked was "what else on this page must reconcile with the total", answered
"the rows", and never extended to the only other place the figure appears.

Fixed by making the displayed hold singular and owned by the caller:
`preauth_message` takes `required_display` and renders it verbatim, and
`first_wave_display_at_pace()` produces it. Re-measured over 254 cohorts: the
refusal sentence, the panel total and the row sum agree everywhere, and the
steady-pace alternative now agrees with the panel a user gets by acting on it
(**M-2**, which was the same defect on the "Starting narrow would need $X"
line: it promised $9.18 and produced a panel reading $9.19).

### The round-7 fix for "an unpinnable safety mechanism" pinned nothing
Round 7 replaced the `funded_any` flag with `nothing_charged=not started` and
this register claimed existing tests pin the derivation. **They do not**, and
that claim has been struck above. What they pin is `started` as a list. Every
reachable `_err` has `started == []`, so `not started` is a constant, and the
reviewer's mutation to `True` passed all 277 tests in the opted-in files. One
unpinnable expression had been swapped for another.

The property is genuinely unreachable at runtime, so it is now guarded at the
source: an AST test asserts the route passes a derived expression referencing
`started` rather than a literal. That prevents precisely the "simplify the
constant nobody can make false" tidy-up that would restore the round-5 defect,
and its docstring says plainly that it proves the expression's shape and
nothing about its value.

### The drift guard had three ways past it
All three demonstrated by the reviewer with the suite green: arithmetic on a
display string (`Number(d.first_wave_usd_display) - 0.01`, printing a cent below
the hold in the consent sentence), a rounding call the guard did not know
(`toPrecision(3)`, rendering a $573.68 hold as "$574"), and a field reached by a
computed key (`d['balance' + '_usd']`, rendering $573.6736).

The first two are now closed: the guard forbids the whole family of numeric
coercions, not just the one method. The third is not closable by pattern
matching, and the guard's docstring now says so instead of claiming "no exact
4dp field reaches the renderer at all". A guard that overstates its reach is
worse than a narrow one, because the next author trusts the docstring.

### Smaller corrections
- `tests/money_display_guard.py` was **untracked**. Committing the tracked diff
  alone would have been a collection error across two whole test files, not one
  failure. Now `git add`ed.
- The balance-band docstring described a one-cent window; since round 7 the
  displayed requirement is a row sum, so the window is wider. Measured at 2
  cents across 2- to 7-tool cohorts, and now documented as measured.
- Two docstrings 39 lines apart contradicted each other on the fail-closed
  mechanism, because round 7 corrected one and left the other.
- `display_total_usd`'s trailing comment justified its `quantize` with whole
  dollars; a sum of 2dp Decimals is already 2dp, whole dollars included. Only
  the empty case needs it, no caller passes empty, and the comment now says the
  line is not load-bearing.
- The `_Query.lt` comment claimed "every test of it" passes while exercising
  nothing. True of tests using THAT fake; `tests/test_compute_campaigns_driver.py`
  has its own fake which already modelled both methods.
- Round 7 corrected `neq`'s NULL semantics in one fake and left a new fake in
  the same diff using Python `!=`. Both now drop NULL rows the way PostgREST
  does.
- The failed-estimate handler test asserted three statements were present but
  not their order; reversing them re-arms the button and stayed green. It now
  asserts the order, with the reason.

### A51. Consent on `/campaigns/new` is not invalidated when the inputs change
- **RESOLVED 2026-07-30**, authorised by Leo. `debounced()` now bumps a request
  sequence, unticks the box, clears the figures, shows "Repricing...", and
  disables submit BEFORE scheduling the refetch; both response handlers drop
  themselves if superseded. Ported from the multi-tool page, with the local
  variable named `confirm` rather than `confirmBox`. **5 mutations, 5 killed**,
  including moving the invalidation after the `setTimeout`, which is the shape
  that reads as correct and is not.
- Coverage note against **A47**: these assertions are STATIC, because no test
  executes this script. They prove the statements are present and ORDERED, not
  that the browser behaves. The first draft of the ordering assertion matched
  the word `setTimeout` inside the explanatory comment and failed on a correct
  implementation; comments are now stripped before the check. A guard that
  fails on correct code is how a real guard gets deleted for being flaky.
- **severity:** medium (live) | **owner:** Leo to authorise
- **detail:** `templates/runs/new.html`'s `debounced()` only schedules a
  re-estimate. It does not untick the consent box, clear the figures, or
  sequence responses. Tick the box at 24 designs ($4.03 shown), type 5000, and
  submit inside the 250 ms window: the POST prices 5000 against consent recorded
  for 24. The multi-tool launch page was hardened against exactly this in an
  earlier round (`clearTotals()` plus a request sequence number); the deployed
  single-tool page was not.
- **not fixed here:** it is a behaviour change to a deployed route, and the
  authorisation this round carried was for the rounding defect. Same shape as
  A48: filed, and cheap to fix on a word.

### Carry-forward
Round 8 adds one:

- **A display rule has a blast radius, and it is every place the figure is
  printed.** Round 7 learned that a rounded figure must reconcile with the rows
  beside it, fixed that, and shipped the identical defect to the refusal
  sentence on the same screen. Before changing how a number is displayed,
  enumerate every surface that prints it, not just the one in front of you.

And one that is now three rounds old and still biting: **a claim that a test
pins something is itself a claim, and it needs a mutation, not a reading.**
Round 7 wrote "which existing tests do pin" without running the mutation that
would have disproved it in thirty seconds.

## Addendum 2026-07-30j — QC round 9, over round 8

One agent over the whole uncommitted stack, round 8 as primary target. Verdict
**FIX FIRST**, 8 confirmed defects plus 1 suspected. It restored all 8 files it
mutated and verified each by sha256. Every finding below was reproduced by
measurement here before anything changed.

**Round 8's headline fix survived.** 26 of 27 meaningful mutations died, and the
254-cohort re-measurement found zero disagreement between the refusal sentence,
the panel total and the row sum. It also independently confirmed the `.is_()`
count (17 sites, 2 `None`, 15 `"null"`) that three earlier rounds got wrong.
What broke was the layer around the fix.

### The drift guard let a one-cent understatement through, on both consent pages
`money(d.first_wave_usd_display - 0.01)` passed every check with the suite
green: it names no coercion from the forbidden list, and its 4dp field is hidden
behind the `_display` suffix that check 2 looks past. JS coerces implicitly for
`-`, `*`, `/` and unary `+`. On rfdiffusion@12 + pxdesign@12 the page would print
"Held to start $9.18" directly above "the amount above will be held", over a
column summing to $9.19.

Reproduced here on three variants (`- 0.01`, `Math.trunc(x * 100) / 100`,
`+x + 0`), all passing. Check 3 is now a **full match** on the `money()`
argument, which kills all three.

The same docstring was **also wrong in the opposite direction**: it called the
computed-key route (`d['balance' + '_usd']`) unclosable, when check 3 has caught
it since the day it was written, and check 3's own comment called itself
"redundant with check 2". Acting on that word and deleting it would have
re-opened the row. Both claims corrected. One route genuinely stays open and is
now documented as open: an expression that never names a 4dp field, never names
a coercion, and never passes through `money()`.

### A display rule's blast radius, one round after that became the carry-forward
`templates/runs/detail.html` and `templates/runs/list.html` render the stored
budget with a nearest-rounding format filter over a float. Neither is in the
launch diff, and that is the point: **the diff created a disagreement in files
it never touched.** Before it, both screens agreed; after, the panel that takes
consent says $4.03 for rfdiffusion@24 while the run's own page says $4.02.

Measured, 5 of the 7 campaign tools diverge:

| tool | exact | panel | detail + list |
|---|---|---|---|
| rfdiffusion | 4.0202 | $4.03 | $4.02 |
| bindcraft | 80.4020 | $80.41 | $80.40 |
| rfantibody | 80.4020 | $80.41 | $80.40 |
| proteina | 43.4103 | $43.42 | $43.41 |
| iggm | 3.3501 | $3.36 | $3.35 |

Fixed by exposing `display_cost_usd` as a Jinja global and calling it from both.
Round 8 wrote "enumerate every surface that prints it" as the carry-forward and
then did not do it; the handoff's own list named only `wallet/_partials.html`.

### The source-guard argument was applied to one money route and not its sibling
Round 8 accepted that `nothing_charged` could not be pinned behaviourally and
guarded it at the source. `blueprints/campaigns.py`'s `required_display` has the
identical problem and got only a comment claiming the sentence and the panel are
"the same string by construction". They are the same string by **coincidence**:
`pre.required_usd` is `gate_usd` is `first_wave`, so the default derives the
same text and deleting the kwarg leaves 247 tests green, as the reviewer showed.
Now guarded by an AST test with the same honest docstring about what it proves.

### Smaller corrections
- `display_total_usd`'s docstring said a standalone figure such as the
  steady-pace alternative "is ceiled from its exact value directly". Round 8
  made it a row sum in the same change. The two differ in **64 of 120**
  reachable burst cohorts, max gap 2 cents. The same false claim was written
  into the handoff's "Known limits" while the same file said the opposite two
  paragraphs earlier, and left un-struck at line 1198 above. All three fixed.
- `display_balance_usd`'s caller enumeration omitted `compute_campaign_create`,
  and wrongly said `MultiLaunchPlan.rows()` is reached from the launch POST. It
  is not; only the estimate endpoint calls it. Rewritten as two groups, because
  the fix above adds callers on plain page renders where a raise is a 500 and
  **not** the fail-closed disabled button the old paragraph claimed for all.
- `money()`'s comment claimed a null renders "the same placeholder as no
  estimate yet". It renders a dollar sign plus a dash; the launch page's three
  totals and three of the four figures on the campaign page use a bare dash.
  Corrected in both.
- `test_the_narrow_alternative_quotes_the_panel_it_produces` omitted the
  precondition its sibling asserts. Its cohort does observe the divergence, so
  not a live hole, but the two roundings agree in 56 of 120 cohorts and a cohort
  change would make it silently vacuous. That is the failure round 7 shipped.

### A52. The campaign estimate never sends `preset`, so it always prices as pilot
- **severity:** low (latent, not live) | **owner:** code
- **detail:** `templates/runs/new.html`'s `fetchEstimate()` sends only `tool` and
  `requested_designs`. `blueprints/campaigns.py:137` reads `preset` and defaults
  it to `"pilot"`, and its comment says the threading exists so the estimate
  matches the create path "if pricing ever becomes preset-dependent".
- **the comment is TRUE, measured 2026-07-30.** Pricing does not vary: proteina
  across pilot / protein_binder / motif_ame gives chunk 8, 3 sub-jobs, budget
  $43.42, first wave $45.00 identically; iggm across pilot / cdr_design /
  fr_design / inverse_design / complex_prediction gives chunk 40, 1 sub-job,
  budget $3.36, first wave $4.37 identically.
- **so why file it:** the preset threading is dead code for the only two tools
  whose preset a user can pick, and estimate and submission agree by a
  coincidence of `_campaign_container_seconds` that nothing pins.
- *Next:* not a one-line fix. Two `<select>`s share `name="preset"` and only the
  enabled one submits, so the page must send the enabled one. Cheapest real
  guard is a test asserting `plan_chunks` is preset-invariant for proteina and
  iggm, so the day it stops being true something goes red.

### A53. `templates/wallet/_partials.html` is the third live rounding instance
- **severity:** low (live, pre-existing) | **owner:** Leo to authorise
- **detail:** `fmt()` at `:192-196` parses a cost to a float and re-rounds it to
  NEAREST, then renders the job estimate, the balance, the balance-after, the
  soft cap, and the top-up gate's estimate/deficit/rounded figures (`:214-235`).
  Against `estimated_cost_for_tool(None, slug)` it prints **below** the real
  estimate for **6 of 13** deployed tool forms:

  | tool | exact | shown | should be |
  |---|---|---|---|
  | af2 | 0.5243 | $0.52 | $0.53 |
  | esmfold | 0.0728 | $0.07 | $0.08 |
  | esmfold2-design | 9.8614 | $9.86 | $9.87 |
  | iggm | 0.0728 | $0.07 | $0.08 |
  | mpnn | 0.0241 | $0.02 | $0.03 |
  | opendde | 14.7920 | $14.79 | $14.80 |

  The other 7 (bindcraft, boltz2, boltzgen, colabfold, pxdesign, rfantibody,
  rfdiffusion) round the same either way. Included by every
  `templates/tools/*_form.html`.
- **Correction to the round-9 report, which said 7 of 14.** Measured here
  independently: it is **6 of 13**. The reviewer counted `wallet/_partials.html`
  itself as a form, and included proteina, which has no form template at all
  (only `proteina_results.html`) and so never includes this partial. The defect
  is real; the blast radius was overstated. Filing an agent's figure without
  re-measuring it is the same error this register keeps recording, and it was
  made here.
- **not caused by this diff.** `rounded_topup_usd` is rounded up server-side so
  the top-up CTA is safe; the displayed deficit is not.
- *Next:* the handoff carried this as "not called a third instance until someone
  measures its inputs". It is now measured, and it is one. Same shape as A48 and
  A51, on 14 deployed forms rather than 1, so it is Leo's call.

### A54. The narrow alternative is gated on the exact figure but quotes the ceiling
- **severity:** low | **owner:** code
- **detail:** `blueprints/targets.py:685-700` offers the steady alternative when
  `first_wave_at_pace(...) <= pre.balance_usd`, both exact, then renders the
  row-sum ceiling. Constructed: exact steady 9.1765, balance 9.1800 renders
  "Starting narrow would need $9.19" beside "Balance $9.18", and switching to
  steady would in fact start.
- **direction is safe**: it under-promises, so nobody is encouraged into a
  refusal. But an affirmative recommendation whose job is to be reachable is
  different in kind from a requirement figure, and this one reads as unreachable
  when it is not.
- *Next:* gate on the displayed figure, so the offer and its price are the same
  number. Not fixed this round: it is a behaviour change to the offer logic on
  an unshipped route, and round 9 exists because round 8 widened its own scope.

### Carry-forward
Round 9 adds nothing new. It re-proves the two already written down, which is
itself the finding:

- **A display rule's blast radius is every surface that prints the figure.**
  Written as the carry-forward at the end of round 8, then broken by round 8's
  own diff in two files it never opened.
- **A comment asserting a property is a claim needing a mutation.** Four of the
  eight findings are exactly this, including two docstrings that describe the
  opposite of what the same commit shipped.

## Addendum 2026-07-30k — QC round 10, over round 9 and A51

Verdict **FIX FIRST**: 8 confirmed (1 high, 5 medium, 2 low), 0 suspected. The
reviewer restored every mutated file and verified by sha256. All eight are
defects in work written the same day, and four are in the guards and comments
meant to prevent exactly these defects.

### The consent guard did not pin the statement that enforces consent
**Consent has no server-side component.** Neither `rp-confirm` nor
`confirm-cost` carries a `name`, neither is POSTed, and no route reads one. The
submit button's `disabled` attribute is the entire mechanism and `syncSubmit()`
is the only thing that sets it. So `syncSubmit()` must run AFTER the resets it
reads.

The A51 test ordered `confirm.checked = false` and `clearFigures()` against the
`setTimeout` and asserted `syncSubmit()` was merely PRESENT. Hoisting
`syncSubmit()` to the first statement of `debounced()` left the suite green
while restoring the whole defect, and it reads *more* correct than the bug does:
the box is visibly unticked and the figures are blank, but the button's state
was computed from the previous estimate and nothing re-evaluates it for 250 ms.

The sibling test 200 lines above (`test_a_failed_estimate_disarms...`) asserts
exactly this ordering for the `.catch` handler and states the reason. The
pattern was adjacent and was not applied.

### The port was guarded; the page it was ported FROM was not
`_debounced_body()` was written this round with a `path` parameter and only ever
called with its default. Deleting `confirmBox.checked = false` from
`templates/targets/launch.html`, or dropping its `.then` sequence guard, was
green. That is the page whose checkbox reads "the amount above will be held
against my wallet balance". Both A51 tests are now parametrized over both pages.

### The guard fix had a second open route, and its docstring said it had none
Round 9's grammar was `[\w.]+_display|x`. The `|x` alternative existed for the
definition `function money(x)` and therefore accepted `money(x)` at **every call
site**, so anything could be laundered through a local:

    var x = d.first_wave_usd_display - 0.01; money(x)   // the $9.18-over-$9.19 bug
    var x = d.first_wave_usd_display.slice(0,-1); money(x)
    var x = d['first_wave' + '_usd']; money(x)          // the "dies here" route

All three passed. Fixed by matching calls with a lookbehind that excludes the
definition, and requiring a dotted or indexed path ending in `_display` with an
optional string-literal fallback. That also fixes the mirror defect: the old
grammar REJECTED `money(d.rows[0].x_display)`, `money(d.x_display || '0.00')`
and any local not spelled `x`, so accept/reject turned on a variable's name.

**That docstring has now been wrong twice in opposite directions** and says so.

### The caller enumeration was wrong again, one round after being rewritten
`first_wave_display_at_pace()` was placed in the fail-closed group. It has two
call sites: the estimate, and `target_launch_submit`'s refusal at
`blueprints/targets.py:799`, a POST re-render where a raise is a 500. The same
paragraph carefully qualified `rows()` one line above and gave its sibling no
qualification.

Also struck: **"That is not a regression (`'%.2f'|format` raised on the same
inputs)"**. It did not. `'%.2f' % Decimal('NaN')` returns `'nan'` where these
raise, and Postgres numeric can hold NaN; and `compute_campaign_create`'s
predecessor caught inside its own derived branch. Two total paths became
fallible. Possibly the right trade, but not a no-op, and it was written as one.

### A docstring contradicting its own change-set, again
`tests/test_compute_campaigns.py` said the helpers' "only caller is the estimate
endpoint" and that failure is handled by "unticking consent", while
`shared/compute_campaigns.py` in the same diff says neither failure handler
unticks anything and contains a line explicitly instructing the reader not to
describe it that way. Both claims removed.

### A55. The balance now rounds two different ways on the same screen
- **severity:** low (live) | **owner:** Leo to authorise, fix WITH A53
- **detail:** the estimate panel renders the balance FLOOR (correct: a balance
  rounded up claims money the wallet does not have). Every other surface renders
  it NEAREST via `'%.2f'|format`: `_header.html:34` (on every page, beside the
  panel), `account.html:38`, `base.html:180`, `jobs_list.html:123`,
  `wallet/overview.html:59`, `wallet/topup.html:44` and `:143`,
  `wallet/transactions.html:60`, `wallet/_partials.html:46/:60/:66`.
- **measured:** 573.6756 renders $573.68 in the header and $573.67 in the panel;
  24.4950 renders $24.50 and $24.49. Half of all 4dp balances disagree.
- **this diff created it.** Before it the panel used a nearest-rounding JS call
  and matched. The panel is the surface that is now RIGHT; the others were
  always a cent generous.
- *Next:* a `display_balance_usd` Jinja global applied to all of the above, the
  same shape as the `display_cost_usd` global this diff added for costs. Not
  done here: round 9's carry-forward enumerated the COST surfaces and this is
  the BALANCE enumeration, 9 further deployed templates, and widening a diff
  mid-review is what produced rounds 9 and 10. Pairs naturally with A53.

### Carry-forward
Round 10 adds one, and it is about the harness rather than the code:

- **Line endings are per file, not per repo.** `templates/runs/new.html` is
  CRLF; `templates/targets/launch.html` is **LF**. A mutation harness that
  assumes one silently no-ops on the other and reports SURVIVED, which reads as
  a missing test and sends the next author looking for a hole that is not there.
  Two of this round's six mutations "survived" for exactly that reason plus one
  wrong test-file argument; all six died once the harness was corrected. Detect
  the ending per file, and assert the file actually changed.

And the two that keep recurring, now at rounds 8, 9 and 10:

- **A guard's docstring is a claim about the guard, and it needs a bypass
  attempt, not a reading.** Wrong in both directions on consecutive rounds.
- **A display rule's blast radius is every surface that prints the figure.**
  Round 9 enumerated costs and fixed two files. Nobody enumerated balances.

## Addendum 2026-07-30l — QC round 11, over round 10

Verdict **FIX FIRST**: 2 high, 3 medium, 3 low confirmed, 2 suspected. The
reviewer detected line endings per file and asserted a sha256 change before
every conclusion, so this round produced no false SURVIVED results.

### The guard's grammar still turned on a variable's name
Round 10 required `money()`'s argument to be a path ending in `_display`, and
its docstring said "It rejects a BARE LOCAL". It did not. The root token
`[A-Za-z_$][\w$]*` **backtracks**, so a single identifier ending in the suffix
satisfies the whole pattern: `fw_display` matched where `x` did not. Every
round-10 bypass reopened under a different variable name:

    var fw_display = d.first_wave_usd_display - 0.01; money(fw_display)

Applied to BOTH consent pages at once: **100 passed**. Accept/reject still
turned on what the author named the variable, which is precisely what round 10
claimed to have fixed.

Fixed by requiring the final hop to be a member access (`\.[A-Za-z_$][\w$]*_display`),
so the value's origin is always readable at the call site. Verified against all
14 real call sites and every known bypass.

**The open-route count in that docstring has now been wrong three rounds
running, each time written in the same commit that made it false.** The
docstring now says so and instructs the reader to re-test rather than read it.

### Nothing pinned that the fix is ever reached
Both A51 tests read `debounced()`'s body. Neither asserted anything CALLS it,
and a grep of all of `tests/` for `addEventListener` returned **nothing at
all**. Deleting the design-count listener from either page left **100 passed**
while making the entire A51 remediation unreachable, on the exact input the
defect was filed against.

This is round 10's finding one level up. Round 10 caught a test asserting a
statement was PRESENT without asserting WHERE; this is a test asserting a
function body is correct without asserting anything invokes it. Both pages now
have a wiring assertion.

### Smaller corrections
- `_debounced_body()`'s brace matcher was not string-aware: a `{` inside a
  string widened the extracted body from 310 to 578 characters and swallowed
  the listener block, so the ordering assertions would have been matching text
  from outside the function. Fixed with a single pass that tracks strings and
  comments together. The two intermediate attempts are recorded in the code,
  because each is individually wrong: stripping `//` first deletes a `//` inside
  a URL, and tracking strings first treats the apostrophe in `// the user's
  balance` as opening a string, which both files contain and which broke the
  extraction outright.
- `display_total_usd` was excluded from the parametrized "all three helpers
  raise" test, because it takes an iterable rather than a scalar, so the module
  docstring's claim was asserted for two of three. It has its own test now.
  NaN matters most there: `Decimal.quantize` does not signal on NaN, so the
  explicit `is_finite` check is the only thing between a non-finite row and
  "NaN" rendered into a consent panel.
- Known false positives in the grammar are now listed rather than fixed:
  `d['first_wave_usd_display']`, `d.rows[i].x_display`, `??`, template literals.
  None is used by either template. A guard that fails closed on a valid-but-
  unused form costs one rewrite; a guard that fails open costs money on a
  consent screen. Widening the grammar is how the last two holes were opened.

### A53 EXTENDED — a third class of money figure nobody had enumerated
`templates/wallet/topup.html:131` renders the 4dp `deficit_usd` with a
nearest-rounding filter, under the words **"Add at least that amount"**. On the
same 6 of 13 forms as the cost defect, that instruction is short: opendde needs
$14.80 and the page asks for $14.79. A53 was scoped to `_partials.html` and A55
listed only topup's balances, so this fell between them. Costs were enumerated
in round 9, balances in round 10, and required-top-up by nobody.

### A55 CORRECTED — the enumeration was incomplete, and the fix as filed would break the ledger
Four balance surfaces were missing: `admin/user_detail.html:10`,
`admin/users_list.html:58`, `wallet/overview.html:203`,
`wallet/transactions.html:162`.

More important, and a genuine design constraint rather than a miscount:
`tx.amount_usd` (a cost, would round UP) and `tx.balance_after_usd` (a balance,
would round DOWN) sit in the **same row** of the transactions table. Applying
both rules mechanically makes consecutive rows fail to reconcile in a fixed
direction, on the one page whose entire purpose is that the reader can check the
arithmetic. **A53 and A55 must not be executed as a blind find-and-replace.**
The ledger needs a deliberate decision about which rule governs a running
balance beside a signed amount, and that decision belongs to Leo along with the
authorisation.

### Suspected, not confirmed, filed as A56
Consent may survive a page load on both pages: `fetchEstimate()` runs at load
without going through `debounced()`, and browsers restore checkbox state across
a soft reload. Not reproduced in a browser, so it is suspected only.
`preset`/`iggm_preset` on `runs/new.html` also have no invalidation, which is
safe exactly as long as A52's measured preset-invariance holds.

### Carry-forward
Round 11 adds one:

- **Check that the thing under test is reachable, not just correct.** Every
  assertion about a handler's body is describing dead code until something
  asserts the handler is wired. A grep for the wiring primitive across the whole
  test suite is a ten-second check and it had never been run.

And the recurring pair, now at rounds 8 through 11:

- **A guard's docstring is a claim about the guard, and it needs a bypass
  attempt, not a reading.** Wrong three rounds running.
- **A display rule's blast radius is every surface that prints the figure.**
  Three classes found so far by three separate rounds: costs, balances,
  required top-up. Assume there is a fourth.

## Addendum 2026-07-30m — QC round 12, over round 11

Verdict **FIX FIRST**: 2 high, 1 medium, 2 low, 1 suspected. 13 of the round-11
mutations were killed, so the remediation was not uniformly weak; the failures
concentrated in the two artefacts round 11 added and in one cohort choice.

### The one that was a real money path, not a guard
**Every displayed hold's pace argument was unpinned.** Swapping `plan.pace` for
a hardcoded `PACE_BURST` in the refusal at `blueprints/targets.py:799`, and
`PACE_STEADY` for `plan.pace` in the narrow alternative at `:697`, both left
**257 tests green**.

Cause: every money test written since round 7 runs 12 designs, where one sub-job
per tool clamps the first wave and **burst and steady price identically**
($9.19 either way). The tests could not observe the argument they depended on.
At steady with three tools at 480 the two figures are $38.36 and $191.77.

Fixing this took three attempts, each of which the mutation rejected:

1. Widening the cohort to 200 designs. **Insufficient**: the test still posted
   `pace=burst`, so `plan.pace` and a hardcoded `PACE_BURST` are the same value.
   The mutation survived.
2. Parametrizing over both paces. **Broke its own precondition**: at
   rfdiffusion+pxdesign@200 the steady panel equals the ceiling of the steady
   exact sum, so the steady case asserted nothing.
3. Searching for a cohort satisfying all three conditions at once — paces
   diverge, AND the row sum differs from the ceiling at burst, AND the same at
   steady. 81 cohorts qualify; bindcraft+rfantibody@100 is the smallest.

The file already contained `_assert_pace_is_observable_on()` for exactly this,
with a docstring explaining the clamp. The new tests neither called it nor
honoured it.

**A second defect surfaced while fixing it.** The refusal test computed its
expected figure from rfdiffusion@12 + pxdesign@12 while `_form()` posts
pxdesign@**24**, so the expectation came from a cohort the route never priced.
It passed only because those two different cohorts render the same $9.19. The
form and the plan are now built from the same numbers.

### The guard: stopping, rather than escalating a fifth time
Round 11 required the final hop to be a member access so a bare local would
fail. It does. But **a member is as easy to assign as a local**:

    d.fw_display = d.first_wave_usd_display - 0.01;   money(d.fw_display)

passed on both consent pages, printing $9.18 above "the amount above will be
held" for a $9.19 hold. Four further routes were measured: `money (v)` with a
space (which `money\(` does not see at all), `money\n(v)`, `window.w_display`,
in-place mutation of the field, and a `defineProperty` getter.

**The space is fixed** (`money\s*\(`), because a call the matcher cannot see
makes every check inapplicable. **The laundering route is not, deliberately.**
A pattern matcher cannot establish where a value came from; closing one
assignment shape does not make the next unreachable, and four consecutive
rounds of trying produced four false docstrings, each written in the commit that
falsified it.

The docstring now says this outright: it is a lint for the ACCIDENTAL mistake,
the real protection is that the server computes every displayed string in
Decimal, and **no route count belongs in it**. The member-laundering mutation is
left surviving, on purpose, and is recorded here rather than hidden:

    templates/runs/new.html:  money(d.first_wave_usd_display) -> money(d.fw_display)
    result: 21 passed. Documented limitation, not an unknown gap.

### The wiring assertions did not assert wiring
Round 11's fix for "nothing proves the handler is reachable" was a substring
test naming neither the element nor the collection. Three mutations, all green:
repointing `querySelectorAll('.tool-designs')` at a class that does not exist,
repointing `getElementById('requested_designs')` at a dead id, and **replacing
the entire listener block with a comment containing the same characters**. Its
own limit statement -- "it proves the listener is registered in the source" --
was false.

Now: comments are stripped before the assertion, the selector must appear in
live source, and the selector must resolve against the page's own markup. Note
the markup literal is the full attribute as written (`class="field-input
tool-designs"`); asserting `class="tool-designs"` failed against correct markup,
which is the false-positive direction and would have got the guard deleted.

### A57. A fourth surface family: outbound email
- **severity:** low (live) | **owner:** Leo, with A53/A55
- **detail:** `shared/email.py:_money()` formats with `f"{d:.2f}"`, which rounds
  to NEAREST, disagreeing with both rules. `$24.4950` renders as **"$24.50"** in
  `send_low_balance_email` -- the one message whose entire purpose is to make a
  user act on a balance, and it overstates it.
- A53, A55 and A53-EXTENDED all stop at `templates/`. Nobody had looked outside
  the template directory at all.

### Carry-forward
Round 12 adds one, and it is the most useful of the series:

- **A test cohort is a precondition, and preconditions must be asserted, not
  chosen by habit.** Every money test written across five rounds reused a
  12-design cohort in which the quantity under test is mathematically
  invisible. A helper to catch exactly that already existed in the same file and
  went uncalled. Before asserting anything about a computed figure, assert the
  inputs can distinguish right from wrong.

And the standing pair, now at rounds 8 through 12:

- **A guard's docstring is a claim about the guard.** Wrong four rounds running,
  now replaced with a statement of what it cannot do.
- **A display rule's blast radius is every surface that prints the figure.**
  Four classes found by four separate rounds: costs, balances, required top-up,
  outbound email. Assume there is a fifth.

## Addendum 2026-07-30n — QC round 13, over round 12

Verdict **FIX FIRST**, but for nothing in the diff.

### The finding the last four rounds were building toward
**The shipping code in this diff is sound.** 18 of 18 mutations aimed at its
shipping behaviour were killed, including all five of round 12's own fixes. The
reviewer could not construct a wrong figure, a wrong charge, a double charge, an
ungated launch, or a misstated refusal on any path the diff touches. The pace
argument, the row-sum totals, both refusal sentences, the uncharged claim, the
row-level guards and both consent pages all survive adversarial measurement.

That is the first round since 5 to return no shipping defect in the work under
review, and it is what closes the loop opened at round 6.

### But every round since 3 walked past the same deployed route
Both new shipping defects are in `blueprints/campaigns.py`, the SINGLE-tool
campaign create route. Ten rounds passed through that file on the way to the
multi-tool one and none looked at its decorators.

### A58. `POST /campaigns` is the only money-spending POST with no `@idempotent()`
- **severity:** HIGH (live) | **owner:** Leo to authorise
- **detail:** `blueprints/campaigns.py:182-184` carries `@login_required` and
  nothing else. Every sibling has the decorator: `POST /targets`
  (`targets.py:395`), `POST /targets/<id>/launch` (`:706`),
  `POST /tools/<tool>/submit` (`tools.py:846`), `/campaigns/<id>/refold`
  (`campaigns.py:660`), `/developability/score` (`tools.py:96`),
  `/library-planner/plan` (`tools.py:175`).
- **failure scenario:** a double-submit funds TWO campaigns against one consent.
  Measured with a temporary probe: two identical POSTs gave `created=2
  funded=2`, the same $5.2438 first wave gated twice against the same balance.
  `runs/new.html` has no submit handler, so nothing collapses it client-side;
  the CSRF token is session-scoped and reusable; the POST takes seconds.
- **this repo already calls this a defect** for a route that HAS the decorator:
  `docs/HANDOFF-2026-07-27-target-first-phase0.md:54` and this register at
  `:294` both name the exact failure mode.
- *Next:* one decorator, matching six siblings. Note the diff already hardens
  `shared/idempotency.py` (keys on the form body when CSRF has consumed the
  stream, releases on 4xx), which is what makes adding it safe rather than a
  new way to silently drop a legitimate second submission.

### A59. The single-tool route discards `fund_campaign`'s boolean
- **severity:** medium (live) | **owner:** code, with A58
- **detail:** `blueprints/campaigns.py:417` calls `cc.fund_campaign(campaign.id)`
  and ignores the result, so a failed fund redirects as success and strands the
  campaign at `draft` forever -- `cron/tick_campaigns.py:28` excludes draft from
  `_ACTIVE_STATES`, so nothing ever picks it up.
- No money is lost; this is the round-5 inversion in the other direction (that
  one told a charged user nothing was charged; this tells an uncharged user it
  started). `fund_campaign` was given a real return value in this very diff, and
  the multi-tool route consumes it. The single-tool route was never updated.

### A53 EXTENDED AGAIN — a third rounding DIRECTION, and a JS writer that would undo A55
- `templates/wallet/_partials.html:192` `fmt()` renders costs (must round UP),
  balances (must round DOWN) **and caps** (`scaled_hard_cap_usd`, which must
  round DOWN) through one function. **A53's prescribed fix gets caps backwards.**
  Same shape as the A55 ledger warning: these items are not a find-and-replace.
- `static/js/wallet-nav.js:26` rewrites the header balance on every window
  focus, using its own formatting. Fixing `_header.html:34` per A55 without
  fixing this would be undone the first time the user changes tab.
- Two further nearest-rounding sites in `shared/email.py` outside `_money()`,
  which A57 named.

### Fixed here: the one in-diff finding
Nothing pinned WHICH server figure lands in WHICH slot. Five mutations, all
green across 292 tests: `budget_usd_display` into the "Held to start" slot,
`first_wave_usd_display` into the Balance slot, on both consent pages. The
budget is always the larger number, so that particular crossing overstates the
amount about to be held, directly above the line consenting to it.

This is **selection**, not the documented **provenance** limitation: the
assignment names both destination and source on one line, so it is statically
decidable and was simply never checked. `assert_money_slots_are_not_crossed()`
now checks it on both pages; 2 mutations, 2 killed.

### Carry-forward
Round 13 adds one:

- **Scope the review by the ROUTE, not by the diff.** Ten rounds inspected
  `blueprints/campaigns.py` and none read its decorator list, because attention
  followed the changed lines. The two defects found here are both one line, both
  deployed, and both visible to anyone who compared that route's decorators
  against its six siblings.

---

## Addendum o - round 14 (two independent reviewers, split by concern)

Two reviewers ran in parallel against the same uncommitted diff, one on the live
route changes and one on the money display sweep. Both returned defects: 1
blocker + 4 serious, and 2 blockers + 4 serious. Neither had written the code.

### A60 - the drive spawn was fallible and unguarded under a new @idempotent (FIXED)

`blueprints/campaigns.py` gained `@idempotent()` in this branch (A58). It also
called `cc.drive_campaign_async(campaign.id)` bare, AFTER `fund_campaign` had
committed. The `try` inside `drive_campaign_async` wraps the drive, not
`threading.Thread(...).start()`, so the call raises `RuntimeError` under thread
exhaustion. That exception escapes the view, `@idempotent` RELEASES the claim
and re-raises, Flask returns 500, and the retry the error invites re-runs the
whole handler: a second campaign created and funded against one consent. That is
verbatim the A58 failure the decorator was added to prevent, reached through the
fix's own error path, and thread exhaustion is process-wide so it fires for
every concurrent submitter at once.

`shared/idempotency.py` already stated the rule, in a comment written in an
earlier round: "Adding a fallible call to that loop without a guard reintroduces
the exact hazard this paragraph describes." `target_launch_submit` already
guarded the same call. The A59 comment on the campaigns route asserted "Keep the
two routes' policies identical" while they were not.

Fixed by wrapping the spawn, matching the sibling. Pinned by
`test_a_failed_drive_spawn_does_not_double_fund_the_campaign`.

### A61 - the low-balance email was never changed (FIXED)

Round 12 filed outbound email as the fourth class of money surface and named
`send_low_balance_email` as the case: "the one message whose entire purpose is
to make the reader act on a balance". The fix changed `send_reengagement_email`
instead. `send_low_balance_email` stayed on NEAREST, so a wallet holding
$24.4950 was still told "$24.50" while the header chip, the wallet page and the
reengagement email all said $24.49.

The guard test was named `test_the_low_balance_email_call_site_asks_for_down`
and asserted a source substring belonging to the OTHER function. It was green
throughout.

Fixed at the real call site. The test now RENDERS the email with `_post_resend`
patched and asserts on the body, which is the only form that cannot pass for the
wrong function.

Twelve of the module's other `_money` call sites were also still on the NEAREST
default, including `attempted_usd` in the three overrun emails, which exist
specifically to justify an unexpected charge. All now carry an explicit
direction, and `_money` RAISES on an unknown one rather than falling through to
NEAREST. `test_no_email_money_figure_is_left_on_the_nearest_default` enumerates
the module the way the template guard enumerates `templates/`.

### A62 - stranded drafts and orphaned uploads on the confirmed-draft retry (FILED)

The A59 error path returns 400 with "nothing was charged", which is true about
money and only about money. Each retry mints a fresh `campaign-{uuid4}` storage
object and inserts another `draft` row. Nothing reclaims either:
`cron/tick_campaigns.py::_ACTIVE_STATES` excludes draft and there is no delete
path. `list_campaigns_for_user` does not filter draft, so the ghosts appear in
the user's campaign list as ordinary entries. The 4xx also releases the
idempotency claim, so the retry is unbounded. Compounded by the upload: an HTML
form cannot repopulate a file input, so `pre_fill` restores every field except
the one that matters.

Not fixed here. The comment now claims only what it proves.

### A63 - the ledger column mixes 2dp and 4dp under tabular-nums (FILED)

`display_ledger_usd` emits 2dp for clean values and 4dp for sub-cent ones, by
design. `static/wallet.css` styles that column `text-align: right` +
`font-variant-numeric: tabular-nums`, which aligns decimal points only when
every cell has the same fractional width. A $100.00 top-up above a -$24.4950
charge misaligns by two characters. Every fix has a real cost: padding to 4dp
everywhere renders "$100.0000", and per-page adaptive precision is stateful.
Filed rather than silently accepted.

### The exemption that rested on a browser attribute

`_ALLOWED` in the display guard exempted five auto-reload figures on the stated
grounds that `step="1"` / `step="50"` made the stored values "whole dollars by
construction". `blueprints/wallet.py::_coerce` is `Decimal(raw)` with three
MINIMUM clamps and no integrality check, and the columns are `numeric(8,2)`. A
crafted POST, or a devtools edit of the step, stores $5.60. The diff had also
WIDENED two of those figures from `'%.2f'` to `'%.0f'` on the customer-facing
wallet page to fit the exemption, and the same `'%.0f'` renders into the form's
`value` attribute, so opening the settings and pressing Save rewrote 5.60 to 6.
A display defect became a data mutation.

All five now go through display helpers, the three form inputs render exactly
via `display_ledger_usd` with `step="0.01"` matching what the server accepts, and
the replacement test asserts against `blueprints/wallet.py` rather than markup.

### The guard could not see the class it existed to prevent

Round 12 added `_money_format_sites()` to make a fifth undiscovered class
impossible. Round 14 found two live NEAREST sites it was structurally blind to:
`templates/wallet/overview.html` signup-credit-used (reads a `{% set %}` alias,
so no money token reaches the expression) and `templates/wallet/transactions.html`
"net for this job" (the column is named `ann.net`), the latter sitting directly
beside two `display_ledger_usd` figures it is meant to be the arithmetic of.

Four structural causes, all now fixed:

1. **Token allowlist as the only money signal.** A literal `$` on the line now
   qualifies too. Provenance is not decidable by pattern matching, but `$` is
   the thing actually being printed.
2. **`Path("templates")` was CWD-relative.** `rglob` on a missing directory
   yields nothing and raises nothing, so the whole guard passed vacuously from
   any other working directory. Now anchored to the repo root and asserted.
3. **One mechanism and one file type.** `'%.Nf'|format` only, `*.html` only.
   Now also `|round(`, `.toFixed(`, `*.txt`, and `static/**/*.js`.
4. **The can-it-fail test never called the guard.** It re-implemented the regex
   inline, so the token filter, the exemption filter and the directory walk were
   all untested. It now runs the real function over a fixture tree covering all
   five shapes, including one the old guard would have missed.

Exemptions are now keyed on a substring of the offending LINE rather than a
variable name, and a dead exemption is a hard failure. Two of the previous seven
could never match, which is what made the auto-reload fields look covered.

### Reviewer 1's test findings, all confirmed and fixed

- The preauth mock patched `shared.target_launch.campaign_preauth` while the
  route calls `cc.campaign_preauth`, so the REAL gate ran in all new tests. They
  passed by coincidence (wallet mocked to $1000, KYC off, spend-today 0 under
  `isolate_supabase`) and would have turned red on any preauth env change.
  Verified fixed by an INVERTED mutation: break the real function, tests stay
  green.
- No test pinned the form-fingerprint fallback the A58 comment names as the sole
  reason the decorator is safe. Both existing tests posted IDENTICAL forms, so
  they could not distinguish "the key is a function of the form" from "the key
  is a function of nothing". Added
  `test_two_different_campaigns_in_the_ttl_both_run`.
- `get_campaign=None` was a sentinel meaning "apply no patch", so the
  unreadable-row test injected nothing and asserted against an
  `isolate_supabase` side effect. Now `_UNSET`, with the read recorded and
  asserted.
- Both fund-branch tests ran with idempotency effectively disabled while one
  docstring asserted idempotency behaviour as the reason its assertion matters.
  The store is now injected and the release is observed via `store.rows == {}`.
- The third fund branch (row moved but `fund_campaign` reported False) had no
  test at all, though the whole three-valued read exists for it.

### Also corrected

- `display_ledger_usd` was documented as EXACT while quantizing anything finer
  than 4dp with the context default, ROUND_HALF_EVEN. It now raises, and the
  4dp precondition is stated rather than assumed. Its "fail-closed" claim is
  now "fail-fast", matching `display_balance_usd`: this page has no submit
  button, so a raise is a 500, not a blocked spend.
- The `_IdemStore` docstring said the delete path was not modelled, directly
  above an `_IdemTable.delete()` that models it and an `execute()` that pops the
  row. That is what makes `store.rows == {}` a real observation of the release.
- `templates/wallet/_partials.html` rounded the per-job charge CEILING down with
  the balances. A ceiling understated understates maximum exposure; it now
  rounds up. The headroom argument that groups caps with balances fits a monthly
  reload cap, not a per-job charge ceiling.
- `templates/admin/campaign_detail.html` rendered a quote into a form `value`
  with `'%.2f'`, the same data-mutation shape as the auto-reload inputs. Now
  exact.

### Standing lessons

- **A comment is a claim, and the most dangerous ones are written in the commit
  that falsifies them.** Three this round: "Keep the two routes' policies
  identical", "whole dollars by construction", and a test named for a function
  it did not test. The rule that catches these is to verify the claim against
  the code at the moment of writing it, not to write it from intent.
- **A guard that has never been seen to fire is not a guard.** The can-it-fail
  test must exercise the real entry point, not a copy of its regex.
- **An exemption is a claim about a system property.** Assert it against the
  system (a module constant, a route's coercion), never against markup that only
  a browser enforces.
- **A form `value` attribute is not a display.** Rounding it rewrites the stored
  value on the next save.
- **Split the reviewers by concern.** One agent over a 17-file diff reviews
  everything shallowly. Two over halves found 3 blockers between them, and
  neither blocker was in the other's half.

## Addendum p - target-first Phase 3 (the combined ranked table)

Phase 3 turns `/targets/<id>` from a list of runs into one ranked table pooling
every design from every run against that target, plus CSV, FASTA and ZIP of it.
Two new modules: `shared/ranking.py` (cohort and percentile math, no I/O, no
Supabase import) and `shared/target_results.py` (the fan-in over both tables).

Two decisions taken before building shape everything below and are recorded here
so a later reader does not read them as gaps.

**No cross-tool metric aliasing.** The master plan's section 3.3
(`CANONICAL_COLUMNS`, `_METRIC_ALIASES`, `canonical_metric`) is dropped. Each row
prints its own tool's primary metric under that metric's own name, and ranking is
by within-tool percentile, which is scale invariant and so needs no aliasing at
all. The driving evidence is that `tools/proteina/run_pipeline.py:118-121`
resolves `af2_iptm` to an RF3 interface pTM for two of three presets and a
log-transformed AF2 value for the third, so the planned `af2_iptm -> ipTM` alias
would have mis-ranked paid GPU output under a header naming the wrong model.
Proteina's pLDDT is also 0 to 1 while every other tool is 0 to 100, a silent 100x
error a percentile hides forever. `shared/score_legends.py:8-13` already stated
the non-comparability thesis in prose, and its table already encoded it in
numbers: the same `ipTM` label carries good thresholds of 0.65 for rfdiffusion,
0.70 for boltzgen and boltz2, and 0.75 for bindcraft and pxdesign, a spread of
0.10 on what is nominally one metric.

**Cohorts key on (target, tool, preset), not (target, tool).** Proteina's
`total_reward` is the negated AF2 interface pAE under `protein_binder` and an RF3
composite under `ligand_binder`. Two proteina runs at different presets on one
target would otherwise be percentile-ranked against each other across two
incompatible scales, which is the same error the decision above exists to
prevent, reappearing one level down where it is much harder to see.

### A31 (follow-up). Target-tagged standalone runs are now read - HALF CLOSED, not resolved

`shared/target_results.py` reads both tables by construction: this target's
compute-campaign children, and `tool_jobs` rows carrying `target_id` with
`campaign_id` NULL. So `target:` token standalone runs are genuinely read,
ranked and exported, which is the half that closes.

The half that does not: yardstick refolds land in that same population and are
deliberately **counted but never ranked**. A refold re-measures a design that is
already a row in the table, so ranking it would double count the molecule and
file it under the refolder's tool, turning one design into two rows attributed to
two tools. `candidate_records` reads `designs[]`, which is exactly what boltz2
and esmfold emit, so unfiltered they would merge in silently. The exclusion is
mutation-verified. Making refolds a yardstick column instead is Phase 4 and
depends on a FASTA-header join key nobody has validated.

**Found while writing this entry, and fixed:** A31's own user-visible failure
survived Phase 3 through the refold population. The rollup line that discloses
refolds sits inside the `agg.tools` branch, and a target holding only refolds has
no contributing tool, so the page fell through to "Nothing has been run against
this target yet" while `refold_jobs` was non-zero. Same sentence, same class of
wrongness, different table. The empty state now has three branches (drafts,
standalone/refold activity, genuinely nothing) and four tests, mutation-verified
five ways. Worth noting that **nothing pinned that block before**, including the
draft disclosure the Phase 2 route was written for.

### A39. `status_badge` has no tint for any campaign status (RESOLVED)

Six tints added, not the five A39 enumerated. That entry listed `draft`,
`funded`, `completing`, `completed_with_failures` and
`paused_insufficient_funds`, and omitted campaign `completed`, which is a
distinct status from `tool_jobs` `succeeded` and was untinted for the same
reason. Counted against migration 0034's CHECK rather than against the register
entry. `paused_insufficient_funds` is amber rather than red because the run is
recoverable by topping up; red stays reserved for `failed`, which no top-up
brings back. `completed_with_failures` and `paused_insufficient_funds` also get
label overrides, since raw snake_case is not a status a scientist should have to
parse.

### A38 stays latent, and Phase 3 is the reason to keep checking

This design consumes `CAMPAIGN_TERMINAL_STATUSES` for the `provisional` flag,
never `CAMPAIGN_STATUSES`, so the missing `paused_insufficient_funds` member
still has no consumer and still cannot go live. That is a property of this
diff, not a fix.

**The reason usually given for that choice is wrong, and this entry gave it
too.** The Phase 3 brief, the plan's section 5, and the first draft of this
paragraph all said the danger was "a paused run misjudged as terminal". It is
not. Enumerated directly against both constants:

- `CAMPAIGN_STATUSES` is draft, funded, running, completing, completed,
  completed_with_failures, failed, cancelled.
- `CAMPAIGN_TERMINAL_STATUSES` is completed, completed_with_failures, failed,
  cancelled.
- `paused_insufficient_funds` is in **neither**, so it is non-terminal under
  both and `provisional` is true for it whichever set is consulted.

The two sets disagree on exactly draft, funded, running and completing. So
swapping in `CAMPAIGN_STATUSES` as a terminality proxy would report an
**actively running** campaign as finished, which is the worse failure of the
two: the page would drop the "still producing designs" banner while designs
were still arriving. Do not reach for `CAMPAIGN_STATUSES` here, but reach for
the right reason.

### The Pctile column shipped "93th", and a test asserted it did

Found by driving the finished page in a real browser, which is the only step
that had not been done. `templates/components/candidate_table.html` rendered
`{{ pct }}th`. Percentiles run 0 to 99 (`rank_statistics` clamps at 99), so a
bare "th" is wrong for every x1, x2 and x3 outside the teens: **27 of the 100
reachable values**. "93th" sat directly beside "97th" on the flagship new
column of the flagship new page.

Never shipped, since the column is new on this branch. What makes it worth an
entry is the test:

```python
def test_percentile_column_renders_a_percentile():
    pctiles = [cells[4] for cells in table.rows]
    assert all(p.endswith("th") for p in pctiles)
```

Green throughout. It asserted the shape the code **happened to have** rather
than the shape it should have, which is the same defect class as round 14's
test named for one function while asserting another. A test written from the
implementation cannot fail with it.

Fixed with `shared.ranking.ordinal`, registered as a Jinja global beside the
other two. Two follow-on notes worth carrying:

- **The obvious guard would have reintroduced the bug.** The neighbouring
  `score_legend_for` is called as `x if score_legend_for else None`, and copying
  that idiom here gives `ordinal(pct) if ordinal else (pct ~ 'th')`, whose
  fallback is the defect verbatim. It is deliberately unguarded so an
  unregistered global raises instead.
- **The first replacement tests were self-referential and mutation caught it.**
  Both render tests built their expected ordinals by calling
  `ranking.ordinal_suffix`, the function under test, so breaking that function
  moved the expectation with it and both stayed green. They now use a literal
  membership table sharing no logic with the implementation. Verified: with the
  literal table, breaking the helper reddens them; before, it did not.

### A64. `_campaign_passed_filters` does not dedupe retry siblings while the aggregator does (FILED)

- **severity:** low (latent) | **owner:** code
- **detail:** `blueprints/campaigns.py:617` sums `count_passed_candidates` over
  every succeeded child returned by `iter_succeeded_children`, with no dedupe.
  The aggregators reduce by chunk, keeping the highest attempt. A chunk with two
  succeeded attempts therefore double counts in the campaign page's "Passed
  filters" card and counts once in the target table.
- **why it is latent:** a retry is spawned when a chunk fails, so two succeeded
  attempts of one chunk should not occur.
- **why it is filed anyway:** Phase 3 puts the two numbers on adjacent pages a
  user navigates between, so a divergence that was previously invisible now has
  a place to show up. Routing the card through the aggregator is the fix, and it
  is a campaign-page change rather than a ranking change.

### A65. SEVEN metrics format two ways on two adjacent pages (FILED)

**Widened by round 15.** This entry originally said two. An independent
reviewer enumerated every column in `_TOOL_RESULT_COLUMNS` instead of spotting
cases, and found five more: `af2_iptm`, `af2_plddt`, `rf3_score`,
`binder_scrmsd` and `cluster_id` have no `_FORMAT` entry at all, so
`format_value` falls to its `.3f` default while the template's inline chain
gives them `%.2f` (and `cluster_id`, a discrete id, gets `%d`). Seven metrics,
not two. The original wording is kept below because the reasoning still holds
and only the count was wrong; the lesson is that spotting finds instances and
enumerating finds the set.

- **severity:** low (cosmetic, but it is a number) | **owner:** code
- **detail:** `templates/components/candidate_table.html` formats native metric
  columns inline, with a `'%.2f'` else branch. `shared/metric_glossary._FORMAT`
  registers `epitope_contacts` and `n_hotspot_contacts` as `.0f`. The new Score
  cell uses `format_value`, the native columns do not, so an IgGM design reads
  `9` in the target table's Score column and `9.00` in the per-run table's
  `epitope_contacts` column. Both are contact counts, which are integers.
- **not fixed here** because the inline branch is shared with 14 `results_panel`
  call sites on live pages, and changing it is a visible change to shipped run
  pages for zero Phase 3 benefit. The fix is to route the inline branch through
  `format_value` and delete the duplicated format table.

### A66. The `result_columns` sync fence is membership only, not render based (FILED)

- **severity:** informational | **owner:** code
- **detail:** `tests/test_result_columns_sync.py` asserts
  `primary_metric_for(t)[0] in columns_for(t)` for all seven tools, plus that
  every primary metric has a non-blank glossary label. That is a real fence: the
  Score column renders the primary metric's NAME, so drift would print a wrong
  name beside a real number. It is not the render-based test the Phase 0 plan
  named, which needs seven per-tool fixtures; `tests/test_export_shapes.py` has
  two generic shapes.
- **the honest limit:** membership does not prove the column is populated for any
  real result, only that the two registries agree about its name.

### A67. Which epitope produced which design is answerable, and not answered (FILED)

- **severity:** low (missing information) | **owner:** code
- **the fact, established rather than assumed:** a multi-chain target can take a
  different `target_chain` and hotspot set per launch
  (`blueprints/targets.py:82`, `_SHARED_LAUNCH_FIELDS`), and those overrides
  **do** persist. `ToolLaunchSpec.params` is the adapter's validated form, and
  `sanitize_shared_params` strips only underscore-prefixed keys, every tool's
  design-count key, and `preset`, so `target_chain`, `hotspot_residues` and
  iggm's `epitope_pdb_resnums` all survive into `compute_campaigns.params`. The
  standalone side carries the same keys in `tool_jobs.inputs`, which the
  aggregator already selects.
- **so the column is buildable, not blocked.** The envelope already carries the
  whole `ComputeCampaign` objects, so `campaign.params` needs no extra read.
- **two things to get right when it is built:** iggm names its epitope
  differently from every other tool, and a launch that overrode nothing inherits
  the target's default chain, so the column has to distinguish "same as target"
  from "not recorded" rather than rendering both as blank.

### A68. The ZIP can contain fewer structures than the table has rows (FILED)

- **severity:** low | **owner:** code
- **detail:** the `_fetch` closure swallows `StorageError` and skips the member
  (`blueprints/campaigns.py:713`), so a purged or missing object silently
  reduces the archive. Pre-existing on the campaign export and inherited by the
  target export, where it is likelier: a target pools more runs, over a longer
  window, against the same retention purge.
- *Next:* the archive needs a count-mismatch line, or a manifest member naming
  what was skipped. Silence currently reads as completeness.

### A69. Target-level cancel (FILED, deliberately not built)

- **severity:** low (UX) | **owner:** code
- **detail:** seven runs against one target is seven cancel clicks, and Phase 3
  makes the target page the one you would look on. A target cancel has to fan
  out to the existing per-run cancels so the refund path stays untouched.
- **why it is not in this diff:** refunds are the money surface with the worst
  history in this repo (see the rounds 6 to 14 narrative above). It gets its own
  change and its own QC rather than riding on a ranking diff.

### Recorded so they are not later mistaken for oversights

- **The CSV and FASTA exports are uncapped by design; the ZIP is capped at 300.**
  The asymmetry is deliberate and it is not about row count: the ZIP pulls every
  candidate's PDB bytes into web-process memory, and the text exports do not. A
  14,000-design target therefore builds a several-megabyte string, which matches
  the campaign export and is accepted.
- **The campaign fan-in is a loop, never `.in_()`.** `_MAX_CHILD_PAGES` is
  derived per campaign, so widening it to an IN list silently truncates at 101k
  rows with only a `logger.error`; one pathological campaign could exhaust a
  shared page budget; and per-campaign merging makes the retry-dedupe collision
  unwriteable rather than patched, since `best_by_chunk` keys on `chunk_index`,
  which is unique only within a campaign. A mutation test pins it. Do not
  "optimise" it later.
- **`ok` is the export ownership sentinel, not `tools == []`.** The campaign
  export gates on `agg.get("tool") is None`, which is sound there because a
  campaign has exactly one tool. A target has a list, and an owned target whose
  runs have not yet produced a design has an empty one, so the same idiom would
  404 a paying user's freshly launched work. Two tests hold it from both sides
  because either alone passes under the wrong gate.
- **Multi-tool table headers omit `data-col` deliberately.**
  `static/js/candidate_table.js:180` binds its sort handler to `th[data-col]`,
  not to `.sortable-col`, which is only hover CSS. Leaving the headers alone
  would let a user client-side sort 0.91 against 12.40 in the browser and
  reintroduce exactly the cross-tool comparison the whole design forbids.
  Omitting the attribute disables the sorter with zero JavaScript change, and it
  is provable by parsing the rendered header row. A render test does exactly
  that; asserting on `sortable-col` would have passed while the handler still
  bound.

### One documentation claim corrected in place

`docs/PRODUCT-PLAN.md:82` marked item 8, "Cross-run results comparison: pick N
past jobs, see a stacked table of top candidates ranked by composite score", as
shipped. It was never built, and the same file already said so at :585 while the
checkbox above kept claiming otherwise. Phase 3 does not make it true either: it
is scoped to one target rather than an arbitrary set of jobs, and there is no
composite score at all, by decision. The checkbox now says both things.

## Addendum q - round 15, independent split QC over the whole Phase 3 diff

Two reviewers in parallel over the uncommitted diff, split by concern (ranking
plus aggregation; presentation plus export), each required to write its
enumeration BEFORE reading. Every blocker and serious finding then went to a
separate agent instructed to REFUTE it. 17 findings, 7 blocker or serious, 6
survived refutation. **All six were in the presentation half**, which is the
half that had had no adversarial review of its own: the aggregator had already
had three lenses, and the ranking half came back with nothing confirmed.

That distribution is the argument for splitting by concern, again. See
[[feedback_split_reviewers_by_concern]] in the workspace memory.

### The blocker: the failure disclosure was unreachable in the failure case

`templates/targets/detail.html` carried the `agg.partial` banner INSIDE
`{% if agg.tools %}`. `shared/target_results.py::_unreadable` returns
`ok=True, partial=True, tools=[]`, and its docstring says outright that
"`partial` True is the disclosure that the emptiness is a failure and not an
answer". With `tools` empty the banner could not render, so the page fell
through to the empty state and told a paying user **"Nothing has been run
against this target yet"** because a read had FAILED.

Three seams reach it, none contrived: the standalone read raising, every
campaign's child read raising, and the ownership re-ask raising. The reviewer
reproduced all three against the real route without injecting an envelope.

The reviewer's stated mechanism was wrong, and the refuter caught it: it
claimed `get_service_client()` returning None takes that path, when the route's
own `get_target` 404s first, so the template never renders. The defect stood on
the other three seams. Recorded because a finding can be right about the defect
and wrong about how you get there, and only the second half is checkable
cheaply.

Fixed by hoisting the banner above the branch, and by teaching every
"nothing has been run" / "none returned a design" sentence to stand down while
`partial` is set. Neither is known to be true when a read failed.

### Five more, all copy that was false under real data

- **The singular branch said the opposite of its own sentence.** With exactly
  one run and no designs the page read "but **it has** returned a completed
  design so far". The negation existed only in the plural branch. That is the
  state every freshly launched one-run target sits in for its whole first run,
  so it was the copy most users would have seen first. Now one sentence with no
  singular branch.
- **Standalone designs were announced as having returned nothing.** A target
  with zero campaigns and standalone runs that DID produce designs renders the
  runs empty state (the run list is campaigns only) above a populated table.
  Gated on `agg.standalone_jobs` alone, the panel said "3 runs finished against
  this target without returning a design" directly above those designs. The
  empty state now has five branches in priority order and the comment
  enumerates all five.
- **The export links did not carry `?sort=`.** Both the sort toggle's comment
  and `_target_export`'s docstring asserted that they had to, "for the file to
  match the screen". They carried nothing, so under `?sort=tool` the CSV came
  back in percentile order. The guard test was even named
  `test_the_sort_mode_is_forwarded_so_the_file_matches_the_screen` and asserted
  only that the ROUTE reads `?sort` when present, which was the half never in
  doubt.
- **"Top" meant top of the view, not top of the ranking.** The badge, its
  tooltip ("Top-ranked design across every tool run against this target") and
  the highlight rail were all on `loop.first`. Under `?sort=tool` that is the
  alphabetically first tool's best design, so bindcraft outranked rfdiffusion
  by spelling. Now anchored on `_rank_position`, which the ranking layer stamps
  in canonical order before any display sort.
- **A single-tool target table numbered its rows per sub-job.** The pooled
  branch was keyed on `multi_tool`, which understates pooling by exactly the
  case that reads worst: one target run twice with bindcraft, two sub-jobs
  each, read 1,2,3,1,2,3,1,2,3,1,2,3 beside a CSV that ranked the same rows
  1 to 12. Keyed on `target_id` now, under a `pooled` flag that says what it
  means.

### One finding refuted, and worth recording

A reviewer reported that the multi-tool `#` column should render
`_rank_position` rather than `loop.index`. Mechanically true and the harm was
false: the fix would make the visible row numbers non-contiguous under
`?sort=tool` (1, 7, 12, ...) for no gain, since the CSV already carries
`source_rank` separately. Rejected with evidence rather than applied.

### Minors that were not cosmetic

- **`provisional` was False whenever the run list could not be read.**
  `campaigns` stays `[]` and `any()` over an empty list is False, so a target
  whose runs were mid-flight was certified as settled by a read that never saw
  them. Now `partial or any(...)`: a read that could not enumerate the runs
  cannot certify they are terminal, and provisional is the safe direction.
- **The provisional banner was an either/or.** Any paused run suppressed the
  "not every run has finished" sentence entirely, so a user with one paused and
  one running campaign was told only that a top-up was needed. They are
  independent facts; both sentences now render, and `CAMPAIGN_TERMINAL_STATUSES`
  is a Jinja global so the template does not carry a second copy of the set.
- **"Listed last" was false under a cap.** Unranked rows sort to the bottom of
  canonical order, so under a cap they are the first rows dropped and are
  usually not in the table at all. The copy sent users scrolling for rows that
  were not there. It now names the cap and points at the CSV.
- **Three comments contradicted the code written in the same change.** The
  `runs/detail.html` note claimed the exports "really are the full ranked set
  now", which `_MAX_CHILD_PAGES` still falsifies; `tests/test_status_badge.py`
  said five campaign statuses while the template comment beside it said six;
  and the macro's "Requires in Jinja globals" list omitted `ordinal`, which the
  percentile cell calls UNGUARDED so a missing registration raises. All three
  corrected. This is the same defect class as rounds 6 to 14, in a diff written
  with that class explicitly in mind.
- **One assertion in `tests/test_ranking.py` could not fail with the code.** It
  compared `_rank_fraction` against `max(r["_rank_fraction"] for r in
  canonical)`, both sides recomputed by `annotate_rows` from the same input, so
  any error in the statistic moved them together. Replaced by the hand-computed
  79/80 that the fixture fixes. Mutation-verified: inflating the numerator now
  reddens 13 tests, including this one.

### A70. The pooled CSV and FASTA carry no ranking statistic (FILED)

- **severity:** low (missing information) | **owner:** code
- **detail:** `shared/exports.py::_metric_columns` skips every key beginning
  with `_`, so `_rank_percentile`, `_metric_key`, `_metric_value` and
  `_rank_within_cohort` are all dropped. The downloaded file's row ORDER is
  determined by the percentile, but the number itself is not in the file, so a
  user cannot see why one row outranks another, or reproduce the ordering.
- **why it is filed rather than fixed:** the provenance columns are a declared
  list in `_PROVENANCE_COLUMNS`, and adding ranking columns means deciding
  whether they appear on campaign exports too, which changes a live download.
  That is the same reasoning that kept provenance stamping off the campaign
  path in Phase 3.

## Addendum r - round 16, review of the round-15 fixes themselves

Two reviewers over the round-15 fix set, split by concern. **7 blocker/serious,
and every one of them was a defect the round-15 FIX had introduced or left.**
That is this project's oldest pattern, holding for the eleventh consecutive
round: each round's fix creates the next round's defect. The fixes are the
highest-risk surface in a diff, not the lowest.

**The verify stage never ran.** All seven refutation agents died on a spend
limit, so the workflow returned `confirmed: 0` with an empty `refuted` list
carrying `why_not: null`. That number is an artifact, not a verdict. Every
finding below was therefore verified by hand against source before being acted
on. Recorded because a harness that reports zero confirmed findings when its
verifiers all crashed is indistinguishable, at a glance, from a clean run.

### The round-15 fixes that were themselves wrong

- **Absence of one fact was used as evidence of another.** Round 15 made the
  provisional banner render both its sentences by gating the second on
  `unfinished or not paused_runs`. Round 15 ALSO widened `provisional` to
  `partial or any(non-terminal)`. Together: a target whose every run is
  COMPLETE, on which a read failed, has `unfinished == []` and
  `paused_runs == []`, so the fallback fired and the page asserted "Not every
  run has finished, so percentiles will shift as more designs land" directly
  under a run strip showing every run green. Each sentence is now gated on its
  own fact, and a third names the remaining cause so "Ranking is provisional."
  never stands alone.
- **The unranked disclosure was wrong twice, in opposite directions.** It said
  "listed last"; round 15 changed it to "fall below the cap rather than
  appearing in the table". Neither survives `canonical_sort_key`, whose key is
  `(passed, unranked, -rank_fraction, ...)`. **`passed` LEADS**, so an unranked
  row nobody rejected sorts ABOVE every row its own tool marked failed, and
  `unranked` only sinks it within its own pass bucket. Under a cap it is not
  reliably dropped either: `PER_TOOL_FLOOR` reserves slots for every tool with
  passing rows and an unranked row is `_passed` unless its own cohort filtered
  it. Grouping by tool moves it again. Both versions guessed at position from
  the name of a sort term. The copy now claims the exclusion and nothing about
  position, and the test is parametrised over capped and uncapped because each
  previous version got exactly one of those right.
- **Reordering the empty state introduced a false money claim.** Round 15 put
  drafts first, so the draft branch preempted the designs-exist branch: a
  target with one stranded draft AND standalone runs that returned designs
  rendered "1 run was created against this target but never funded ...
  **Nothing was charged.**" directly above a populated table of designs the
  user WAS billed for, since standalone `tool_jobs` are wallet charged. The two
  facts are independent and are no longer branches of one `if`.
- **The capped block was never stood down under partial.** Round 15 applied
  that principle to the empty state only. The capped block states counts from
  the failed read and claimed "The CSV and FASTA exports contain every design",
  which is not merely unknown but false: `_target_export` re-runs the same
  aggregate, so a partial read yields a short CSV served as 200 with no
  disclosure of its own.
- **The envelope fake did not model the invariant the fix created.** With
  `provisional = partial or any(...)`, `partial=True` with `provisional=False`
  is unreachable, yet both partial tests ran in exactly that state and so never
  entered the branch that was broken. `_agg` now RAISES on the impossible pair
  rather than silently correcting it. See
  [[feedback_test_fakes_must_model_backend_limits]].

### A71. A wallet test makes live Stripe calls and is date dependent (FIXED)

- **severity:** medium (test hygiene, live external call) | **owner:** code
- **found:** 2026-08-01, when this session crossed midnight UTC and
  `tests/test_wallet.py::test_auto_reload_monthly_cap_blocks` went from green
  to red with no code change. Nothing in the Phase 3 diff touches
  `tests/test_wallet.py`, `shared/wallet.py` or `billing/checkout.py`.
- **two independent causes, and fixing only the first is not enough:**
  1. The test seeded its "already reloaded this month" row at `now - 2 days`
     while `_auto_reload_total_month` sums the CALENDAR month. On the 1st and
     2nd the row lands in the previous month, `month_total` reads 0, and the
     cap does not block.
  2. `auto_reload_if_needed` checks the 24 HOUR rate limit (`:766`) BEFORE the
     monthly cap (`:769`). Early on the 1st, "inside this calendar month" and
     "more than 24 hours ago" have NO OVERLAP, so no seed timestamp can reach
     the cap check at all. Anchoring to the month start alone just trades a
     `monthly_cap` failure for a `rate_limited` one, which is what happened on
     the first attempt.
- **what it did when it failed:** fell through to
  `create_off_session_payment_intent`, which issued a REAL Stripe API call.
  Observed request id `req_iy1E5cceO4bkhu`, rejected with "No such
  PaymentMethod: 'pm_test'", so no money moved. The exposure is real: `.env`
  holds live Stripe config, `app.py` calls `load_dotenv()` at import, and this
  test does not mock the charge. **Same class as the Supabase hazard behind
  A40, against a different vendor.** Only the fake payment method id stopped it.
- **fix:** the row is anchored inside the current month with
  `max(month_start, now - 2 days)`, and `_auto_reload_count_24h` is stubbed to
  0 so the test exercises the gate it is named for. The rate limiter has its
  own test directly above. Mutation-verified both ways: reverting the anchor
  reddens it, and dropping the stub reddens it with `rate_limited`.
- *Next, not done here:* the wider question is which other tests can reach a
  live Stripe call. A40 counted the Supabase exposure and found 26 files;
  nobody has counted this one.

### Standing lesson

**A relative date in a fixture is a claim about the calendar.** "Two days ago"
silently meant "last month" for two days in every thirty, and the surrounding
code had a second window that made those two days unsatisfiable. Anchor
fixtures to the boundary the code under test actually uses, and stub the gates
the test is not about.

## Addendum s - round 17, the pre-PR review

Twelve agents, three lenses, nine of nine verifiers returned (round 16 lost all
seven to a spend limit and reported `confirmed: 0`, which reads exactly like a
clean pass; this one did not). 21 findings, 9 blocker/serious, 8 confirmed and 1
refuted. Every confirmed finding was re-verified by hand against the code before
any of it was acted on. The eight collapse to six distinct defects: two lenses
independently found the unranked positional claim, and two independently found
the empty-state chain.

**The pattern held for a third round.** Round 15's fix created round 16's defect;
round 16's fix created two of these. Round 16 hoisted `agg.tools` above
`draft_count` and verified the pair it was thinking of, leaving drafts
preempting the three branches BELOW them. Round 16 also added a third sentence
containing "could not be read", which silently falsified round 15's banner test.

- **A72. The empty state's facts were an if/elif chain.** A stranded draft
  preempted both the failed-read sentence and the standalone/refold disclosure,
  printing an unqualified "Nothing was charged." over runs that ARE billed
  (standalone jobs reserve a wallet hold; refolds bill through `charge_for_job`)
  and deleting the A31 disclosure from the page. **fix:** five independently
  gated paragraphs that compose, only "nothing" exclusive; the two divergent
  draft paragraphs unified on the scoped money claim, which is true in every
  state, so there is no longer a second variant to keep in step. Pinned by a
  32-state matrix test rather than by the pairs someone thought of.
- **A73. The unranked disclosure still made a positional claim.** "Ranked below
  the scored designs they share a filter verdict with" is true in canonical
  order and false under `?sort=tool`, which the same page offers. The comment
  directly above it asserted "Position is not stated at all". **fix:** clause
  dropped; the test now pins the whole sentence up to its closing tag, in both
  sort modes, instead of denying three dead phrasings.
- **A74. `_target_export` never read `agg["partial"]`.** A failed read
  downloaded as a complete file, and the FASTA positively asserted the target
  had no sequences. **fix:** the filename carries `_incomplete`, mirroring how
  `capped` is already disclosed, and the FASTA says what actually happened. Not
  a leading CSV comment row: `candidates_to_csv` is shared with the campaign
  export and every existing consumer parses that shape.
- **A75. `multi_tool` was `len(tools) > 1`.** The property the display depends
  on is more than one COHORT. One tool at two presets rendered today's
  single-tool table: a browser-sortable native metric column pooling two
  populations whose numbers do not mean the same thing, no percentile column,
  no disclosure. That is the error the preset half of the cohort key exists to
  prevent, reappearing at the display layer. Reachable from the launch form,
  which offers proteina at two presets and iggm at four. **fix:** derived from
  `per_tool[t]["presets"]`, with `columns` gated on the same flag.
- **A76 (filed, not fixed). The page and the export number rows from different
  bases.** The page ranks with `DEFAULT_LIMIT`; the CSV and FASTA rank the whole
  set. They agree until `select_under_cap`'s per-tool floor reserves a row from
  beyond the cap, which is the case the floor exists for, and then "row 298" on
  screen and "rank 298" in the file are different molecules. `export_key`
  asserted the opposite in its own docstring. **Half fixed:** the docstring is
  corrected and the capped banner now names the join key (source job plus file
  name). **Not fixed:** the numbering itself, because `export_key` is shared
  with the shipped campaign export and renaming its `rank` column is a contract
  change that deserves its own diff.
- **The one refutation.** A reviewer claimed `tests/test_wallet.py` could reach
  a live Stripe charge with a production key. The key in `.env` is `sk_test_`.
  The mechanism is real and the exposure question behind A71 stands; the
  severity does not. *Note: this reviewer's claim about the key was not
  independently confirmed here, since reading credentials is blocked.*

**Round 17 minors not actioned**, each real and each smaller than its fix: the
`source_rank` fallback fabricates a global index for pipelines that emit no rank
of their own; `PER_TOOL_FLOOR` and `_floor_reserved`'s value are unpinned
downward; `_CampaignQuery` does not implement `.is_()`, so the tests rendering
through it all run with `partial=True`; the `total_reward` glossary entry names
two variants where proteina has more; and three pieces of new copy carry no
assertion.

### Standing lesson

**A fix verified against the pair you were thinking of is not verified.** Three
consecutive rounds broke on a state nobody had rendered, and each time the fix
was correct for the case that prompted it. Where the state space is small,
enumerate it: the 32-state matrix test costs less than the three rounds of
review that found these one pair at a time.

## Addendum t - round 18, over the round-17 fixes

11 agents, 3 lenses (fix correctness, test strength, comments-as-claims), 8 of 8
verifiers returned. 16 findings, 8 blocker/serious, **6 confirmed**. Run against
the uncommitted round-17 diff, before it was committed, which is the point.

**A fourth consecutive round in which the previous round's FIX was the defect.**
Three of the six confirmed findings are one root cause, and it is A75's.

- **A77. A75 renamed what `multi_tool` MEANS and left every consumer speaking
  the old language.** Widening it to "more than one cohort" was right for the
  pooled COLUMNS, which is what it was needed for. But the same flag also gates
  the sort toggle and the explanatory prose, so one tool at two presets rendered
  "N designs from 1 tool" eight lines above "Different tools score on different
  scales", with a "Grouped by tool" control that provably cannot reorder
  anything (`apply_sort_mode` keys SORT_TOOL on `_source_tool` alone; a verifier
  confirmed both modes return a byte-identical tbody) and a Tool column showing
  one slug over two populations 40x apart in raw value. **fix:** the cohort flag
  keeps driving `columns` and the pooled column set; the toggle and the
  cross-tool prose move to `len(tools) > 1`; the envelope gains `split_tools`
  and rows of a split tool carry a preset chip. Four route tests, all
  mutation-verified.
  *Refuted sub-claims, recorded so they are not re-derived:* the PER_TOOL_FLOOR
  half of the finding is immaterial (probed at 400+5 with cap 300 the small
  cohort still gets 4 of its 5 rows, because canonical order is already
  cohort-normalised), and the preset IS named on the Pctile tooltip; what was
  missing is the per-ROW value.
- **A78. The round-17 test for the unranked disclosure anchored only its END.**
  A positional claim added as a PRECEDING sentence in the same paragraph passed,
  and deleting the old deny-list lost coverage of the two historical phrasings
  outright. **fix:** the whole paragraph is extracted and compared for equality,
  the deny-list is restored, and the two sort modes are compared to each other
  rather than merely rendered. Mutation N5 (prepend "Unranked designs are listed
  last.") reddens it now and would have passed before.
- **A79. `export_key`'s rewritten docstring replaced one false claim with
  another.** "The row's index WITHIN THIS FILE" is false for the FASTA:
  `candidates_to_fasta` skips rows with no sequence but numbers from the full
  list, so a target whose best design is a sequenceless backbone yields a file
  whose first record is `rank2`. The user-facing sentence round 17 added
  ("those files number every design from 1") was false for the same reason.
  **fix:** both say what is true of all three serializers.
- **A80. The envelope docstring described two mutually exclusive behaviours.**
  A75 updated the `multi_tool` entry and left the `columns` entry two lines
  above it stating the pre-A75 rule. **fix:** rewritten to name the cohort.

### Standing lesson

**Widening what a flag MEANS is an interface change, and its blast radius is
every consumer, not every producer.** A75 was verified where it was computed and
where its value was asserted, and shipped with a test that pinned three dict
keys while its own docstring promised a display property no test rendered. The
question to ask of a renamed flag is not "is the new value right" but "what does
each reader still believe the old name promised".

## Addendum u - live browser walk, and one new finding

**Phase 3 browser walk DONE 2026-08-01 on the deployed site, zero defects.**
It needed a real two-tool target, which the account did not have, so a miniature
campaign was launched with explicit approval: rfdiffusion 20 + pxdesign 1,
quoted hold $11.81, actual hold $11.36, actual spend **$4.48**. Verified on 29
designs from 2 tools: 28 ordinals against an independently written English rule
with zero wrong (including `32nd`, the exact case the old hardcoded `th` broke);
within-tool percentile ranking proven (pxdesign's ipTM 0.100 TIES rfdiffusion's
best raw value yet correctly ranks below its 0.090, because rank fraction 0.5 <
0.6875 - a raw sort would have put it joint first); zero of the 7 pooled headers
carry `data-col`; ZIP arcnames namespaced `rfdiffusion/01c3b3a6/...` beside
`pxdesign/a3429923/...` where the old `chunk000/` scheme gave both ONE name;
suppressed and ranked cohorts rendering in one table; the provisional banner
present while running and absent once terminal; `?sort=tool` regrouping; CSV
matching the screen and unioning pxdesign's sparse `pAE` column.

Two gates confirmed in passing. The all-or-nothing launch gate rejected an IgGM
tool for a missing antibody chain with "Nothing was started and nothing was
charged", leaving the two valid tools in the same submission unlaunched and
unbilled. And the `@idempotent()` guard held through repeated submit clicks
during a diagnosis: exactly two campaigns exist, not four.

- **A81 (NEW, filed for Phase 5, not fixed). The campaigns list does not group a
  multi-tool launch.** `launch_group_id` is a real column
  (`0039_design_targets.sql:107-116`, "ties the N runs created by one multi-tool
  launch together") and is populated on every multi-tool launch
  (`blueprints/targets.py:982,1001`). It is consumed in exactly ONE place in the
  app: the "you just launched N runs" banner at `blueprints/targets.py:524`.
  `compute_campaigns_list` (`blueprints/campaigns.py:84`) builds a flat feed of
  campaigns plus standalone runs and never reads it, so one launch renders as N
  cards carrying the IDENTICAL name with nothing tying them together. Observed
  live: `phase3-pooled-walk` appears twice.
  **Not a collapse to one row.** Each campaign keeps its own status, budget,
  sub-job count, cancel action and results page, and they genuinely diverge (one
  can pause on funds while the other runs). The fix is one card per launch group
  with per-tool rows nested inside, group status as the rollup and budget as the
  sum. Campaigns with a null `launch_group_id` - every pre-Phase-2 campaign and
  every standalone run - render exactly as today, so it is additive.
  The target page's Runs section has the same shape but reads correctly there,
  because it is already scoped to one target and headed "Runs".

### Standing lesson

**A column added for one consumer stays invisible to every other surface that
needs it.** `launch_group_id` shipped in Phase 1's migration and Phase 2 filled
it in, and the list view that most needed it was never taught to read it. When a
schema change lands, enumerate the surfaces that display the same entity.

- **A82 (NEW, filed for Phase 5, not fixed). "Grouped by tool" groups the ORDER
  but renders no groups.** `apply_sort_mode`'s SORT_TOOL branch
  (`shared/ranking.py`) only reorders the row list; the macro then renders one
  flat `<tbody>` with no group header, no separator and no per-tool subtotal, so
  the boundary between tools is invisible. The `#` column makes it worse: it is
  `loop.index`, so it counts straight through the boundary (pxdesign is 1,
  rfdiffusion continues 2, 3, 4...), which actively argues the rows are one
  continuous ranking. Observed live on target `164eb28e`: the only signal that
  the mode changed at all is that the single pxdesign row moved to the top.
  **Fix shape:** when `sort_mode == 'tool'`, emit a group header row per tool
  carrying the slug and that tool's counts from `agg.per_tool` (total, shown,
  cohort_n), and restart or suppress the `#` column within each group so it
  stops implying a cross-tool ordering the page explicitly refuses to make.
  Percentile mode is unaffected and keeps the global index.
  **Open question for the same change, NOT a defect today:** the `Top` badge
  marks `_rank_position == 1`, the canonical best across the whole target, so in
  grouped mode it lands on whichever row that is rather than on the first row
  shown (live: row 2, under an unbadged pxdesign row 1). That is correct as
  specified and is pinned by a test, but it reads oddly once groups are visible.
  Decide then whether grouped mode should badge each group's own best instead.


## Addendum v - Phase 5 (the target-wide lab handoff, plus A82)

Phase 5.3 closes the gap Phase 3 left open: `/targets/<id>` is the page whose
entire purpose is picking winners, and it had no action for the pick. The
shortlist bar was hidden there deliberately (the modal branched on
`campaign_id` and otherwise fell back to `source_job_id`, both empty in target
mode), so the target branch had to exist before the bar could ship. Migration
`0040_lab_campaign_target_source.sql` adds `lab_campaigns.source_target_id`
plus the widened `submission_source` and shape CHECKs, mirroring 0037.

**A82 is fixed in the same change** and its open question is decided below.
**A81 is NOT fixed** and is deliberately deferred; see the closing note.

### Decisions taken

**The Top badge stays on the canonical winner in grouped mode.** A82 asked
whether, once group headers are visible, `?sort=tool` should badge each
group's own best instead of the target's. It should not. The badge's tooltip
is a claim about the TARGET ("Top-ranked design across every tool run against
this target"), and a control whose meaning changes with the sort mode is worse
than one that lands where it lands. The oddity A82 observed was really the
absence of the group header: with the header present, the badge reads as "and
this one is the overall best" rather than as a mis-placed row-1 marker. So in
grouped mode the badge can still sit below an unbadged row, and that is now
pinned twice: where it lands, by the pre-existing
`test_the_top_badge_marks_the_ranked_best_not_the_first_row_shown` (whose
fixture puts the winner outside the first group), and how many there are, by
`test_grouped_mode_badges_one_row_in_the_whole_table_not_one_per_group`.

**The `#` column restarts inside each group rather than being suppressed.**
Within a block the number is the row's position among that tool's shown
designs, which is a claim the page does make. A blank column would have been
the safer non-claim but reads as a rendering fault.

**"Export starred only" is CSV, and target mode only.** The target export
aggregates with `limit=None` for CSV and FASTA, so filtering it by ref is
exact. The ZIP caps at 300 in canonical order, so a starred design below that
cap would be missing from the archive with nothing in the file to say so;
rather than invent a second cap rule for a starred ZIP, the control is CSV
only. It is target-mode only because only `blueprints/targets.py` reads
`refs`; wiring the job and campaign export routes would widen the diff across
two already-shipped surfaces for a control whose reframing need is the target
page's. Both limits are pinned (`test_only_csv_accepts_a_post`,
`test_campaign_mode_offers_no_starred_export`).

**A POST to the target CSV export always means "only these".** A body with no
`refs`, an unparseable one, or one naming nothing exports ZERO rows, not
everything. Falling back to the full export would make a malformed POST
indistinguishable from a GET and hand back every design under a filename
saying `_starred`.

**No CRO panel was added to the target page.** 5.2 collapses two off-platform
panels into one; adding a third on the new surface would re-create exactly the
redundancy being removed, and the target page already carries the structured
in-product handoff the plan prefers.

### New findings

- **A84 (NEW, FIXED here). The admin fulfilment page has never shown which
  designs a campaign-sourced shortlist named.**
  `templates/admin/campaign_detail.html` rendered
  `campaign.candidate_indices | join(...)` for every non-API row and a "Source
  job" link gated on `source_job_id`. A `submission_source='campaign'` row --
  live since migration 0037 -- has BOTH of those columns NULL by its own shape
  CHECK, so every campaign-sourced handoff has appeared to ops as a scoping
  request with an empty candidate list and a source of "-". This is a
  pre-existing defect in shipped code, not one introduced by Phase 5; it was
  found while verifying that the target branch would render identically, which
  it would have, identically wrongly.
  **Fixed:** `blueprints/admin.py::_ref_shortlist_view` resolves the distinct
  source jobs (deduped, capped at 60 lookups) and the template renders "N
  designs from M jobs" plus the per-tool breakdown, for BOTH 'campaign' and
  'target' rows. 'web' rows keep the index list, which is correct for them.
  *Next:* none; covered by `tests/test_admin_shortlist_fulfilment.py`.

- **A85 (NEW, not fixed). Admin source links 404 for the staff account that
  clicks them.** `/jobs/<id>`, `/campaigns/<id>` and `/targets/<id>` are all
  owner-scoped to the submitter (`get_job(..., user_id=ctx.user_id)` and
  siblings), with no staff bypass, so the "Source" link on the fulfilment page
  opens a 404 for `leo@ranomics.com`. This was already true of the "Source job"
  link that predates Phase 5; the new target and run links inherit it rather
  than introduce it. The id itself is still the useful part of the row.
  *Next:* either add a staff-scoped read path for these three routes, or render
  the id as text and drop the anchor. Do not "fix" it by removing the owner
  scope from the routes.

- **A86 (NEW, not fixed, test hygiene). `PUBLIC_BASE_URL` leaks from the repo
  root `.env` into the whole test session.** `app.py` calls `load_dotenv()` at
  import, so whichever test imports the app first sets `PUBLIC_BASE_URL` for
  every later test in the process. Two email assertions written against the
  module default passed solo and failed in the full suite. The
  `isolate_supabase` fixture blanks only the four Supabase vars, so this class
  of leak is unguarded. Worked around by pinning the var in the affected tests,
  which is what `tests/test_email_real.py` already does.
  *Next:* widen `isolate_supabase` (or add a sibling fixture) to neutralise the
  other `.env` keys that change assertable output, `PUBLIC_BASE_URL` first.

- **A87 (NEW, FIXED with A88). The CAMPAIGN shortlist branch still re-queries a
  rejected job id once per ref.** `_submit_campaign_shortlist` writes to
  `jobs_by_id` only on success, so a miss is never cached and a body naming the
  same foreign job N times issues N identical Supabase round trips. The target
  branch added a `rejected` set; the campaign branch was left alone to keep the
  diff scoped to the new path. The parse-time cap
  (`_MAX_CANDIDATE_REFS = 500`, added to the shared parser so both branches
  inherit it) now bounds the amplification at 500 rather than unbounded.
  **Fixed:** landed as part of the A88 loop rewrite -- `_submit_campaign_
  shortlist` now carries the same `rejected` / `unreadable` pair, so a body
  naming one job N times issues ONE read. `tests/test_campaign_lab_handoff.py`
  pins both halves separately (`test_a_repeated_rejected_job_id_is_looked_up_
  once` and `test_an_unreadable_job_is_read_once_however_many_refs_name_it`),
  each paired with `test_every_ref_naming_one_rejected_job_counts_as_its_own_
  drop` so the cache cannot swallow the shortfall count it short-circuits.
  *Next:* none.

- **A88 (NEW, FIXED). Everything round 19 and round 20 fixed on the TARGET
  shortlist branch is still broken on the CAMPAIGN one.**
  `_submit_campaign_shortlist` is the sibling of `_submit_target_shortlist` in
  `blueprints/lab_projects.py` and was left alone to keep Phase 5's diff scoped
  to the new path. It now trails it in four ways, all of them on a route that
  hands work to a wet lab:
  1. **FIVE silent exits with no reason, not four** (corrected in round 21;
     the count below was wrong when this item was written). The
     `not target_name or not candidate_refs` guard, the `not clean_refs`
     guard, the `except ValueError` arm and the `lab_campaign is None` arm all
     return the user to the compute-campaign page with nothing changed and no
     message, which is the A-8 defect verbatim. The fifth is worse than those
     four and was omitted entirely: `if cc.get_campaign(...) is None:` redirects
     to `jobs.jobs_list`, so a user whose campaign read fails — a transient
     Supabase fault, not only a missing campaign — is silently dropped onto an
     UNRELATED list rather than back onto the page they submitted from, with no
     message there either. The target branch's equivalent
     (`get_target(...) is None`) has the same shape and the same defect.
     Round 19's comment beside the target branch's own version claimed this
     pair "is filed rather than fixed here"; nothing filed it — the register
     ended at A87, which is a different defect. This item is that filing, and
     the comment now names it.
  2. **No truncation disclosure.** It inherits `_MAX_CANDIDATE_REFS = 500`
     from the shared parser and never tells anyone what the bound removed, so
     a 620-design shortlist is announced as 500 with the other 120 unmentioned
     — the A-2 defect, on the paid path rather than the free export. The
     target branch now takes `requested_refs` from
     `_parse_candidate_refs_counted` and reports `truncated` separately from
     `dropped`.

     **Corrected in round 21: the sentence that used to end this paragraph —
     "that helper is already shared and the campaign branch simply does not
     call it" — was FALSE.** `campaigns_submit` calls
     `_parse_candidate_refs_counted` ONCE, above the branch dispatch, so all
     three branches go through it; the campaign arm receives the count and
     discards it by never passing `requested_refs` on to
     `_submit_campaign_shortlist`. The fix is therefore a parameter on that
     function and a `truncated=` on its email call, not a change of parser.
  3. **No dedupe and no index validation at the write path.** A repeated
     `(job_id, index)` is persisted twice and tells ops to order the same
     structure twice; an index past the end of its job's results is persisted,
     counted on both emails, and then silently skipped by
     `stage_campaign_candidates`, so the lab receives fewer PDBs than every
     number anyone can see. `blueprints/admin.py::_ref_shortlist_view` handles
     both at READ time, so the staff email and the ops fulfilment page can
     legitimately disagree about the same campaign-sourced order.
  4. **No negative cache** — that half is A87, kept separate because it is a
     load defect rather than a correctness one.
  **Fixed.** `_submit_campaign_shortlist` now takes `requested_refs` (keyword-
  only, non-defaulted, exactly as the target arm) and reads jobs through
  `read_job`, and carries the full refusal model: dedupe on `(job_id, index)`,
  a negative cache over two verdict sets, an index check against
  `candidate_count(job.result)`, a `dropped` count of distinct refused DESIGNS,
  and an `unresolved` flag that REFUSES the whole submission
  (`?handoff=unverified`) when any refusal had a cause the database caused.
  Both counts now ride the redirect and the emails, and the five silent exits
  became `?handoff=none|noname|rejected|unverified|failed` with a banner apiece
  on `templates/runs/detail.html`, whitelisted through
  `blueprints/campaigns.py::LAB_HANDOFF_REASONS`. The `get_job` -> `read_job`
  swap is a PRECONDITION for reporting `dropped` at all, not a separate
  improvement: on `get_job` a two-second Supabase fault would be reported to a
  paying customer as a permanent rejection. New suites
  `tests/test_campaign_lab_handoff.py` and `tests/test_run_handoff_banners.py`;
  every assertion was mutation-verified.

  Exit 5 (`cc.get_campaign(...) is None` -> `/jobs`) is deliberately UNCHANGED,
  because that exit leaves the page that renders the banners; see A90.

  **The *Next:* line below is DECLINED, and this is the reasoning.** The two
  arms differ on six axes, not one: the parentage predicate, the number of
  `unresolved` setters (two against one), whether an auxiliary read happens at
  all, the staging prefix, whether `source_tools` is sent, and the failure-exit
  URL vocabulary. A six-axis helper is harder to verify than two explicit
  loops, and extracting it means rewriting the TARGET arm -- the only arm
  currently protected by a 1360-line test file -- on a paid intake path. Filed
  as a follow-up now that the campaign arm has a suite of its own.
  *Next (superseded):* lift the target branch's loop into a shared helper rather
  than copying it a second time; the two branches now differ only in their
  parentage test, which is the one thing that must stay distinct. Round 21
  widened the gap further: the campaign branch still reads jobs through
  `get_job`, so it cannot tell a job that is absent from one it failed to read,
  and it has no refusal gate at all.

- **A89 (NEW, round 21, not fixed). A shortlist over `_MAX_CANDIDATE_REFS`
  has no remedy the user can carry out, so nothing may offer one.** The
  truncation copy on the confirmation page and in the customer email used to
  say "star them again on the target page and submit a second request".
  Following that created a SECOND paid lab project covering the SAME designs,
  because three things have to be true for it to work and none of them is:
  1. `static/js/candidate_table.js` never clears the shortlist after a submit,
     so the stars are all still set.
  2. The modal serialises the shortlist in stored order, so the second POST
     carries the identical first 500 refs and cuts in exactly the same place.
  3. `templates/campaigns/detail.html` renders only a COUNT, with no per-design
     list, so the user cannot see which 500 went and cannot narrow the
     selection by hand.
  Round 21 removed the advice and now says what a resend would actually do,
  and added `@idempotent()` to `/lab-projects/submit` — which collapses the
  replay for its 60-second TTL and is a double-click guard, NOT a remedy, and
  must never be described as one. Ops receives the shortfall on the staff email
  ("Over the limit: N starred ref(s) past the per-request cap"), which is what
  the new copy points at.
  *Next:* the followable version needs (a) the submit handler clearing the
  scope's `sessionStorage` shortlist on a successful redirect, or (b) the
  confirmation page listing the refs that were ordered. Either makes a second
  request deliver the remainder; neither is in `blueprints/lab_projects.py`,
  which is why this is filed rather than fixed.

- **A90 (NEW, filed with A88, not fixed). The parent gate on BOTH ref arms
  cannot tell a missing parent from an unreadable one.**
  `_submit_campaign_shortlist` refuses on `cc.get_campaign(...) is None` and
  `_submit_target_shortlist` on `get_target(...) is None`, and both of those
  functions return None for an unreadable row as well as an absent one
  (`shared/compute_campaigns.py::get_campaign` says so in its own docstring).
  So a transient Supabase fault on the parent read bounces the user to an
  unrelated list with no message, on the one action that hands work to a wet
  lab. A88 left this exit untouched on purpose: it LEAVES the page that renders
  the five handoff banners, so there is nowhere to put a reason without a
  three-outcome read to branch on.
  *Next:* a `read_campaign` / `read_target` pair with the `read_job` shape
  (ok / absent / unavailable), then land the user back on their own page with
  `?handoff=unverified`. New shared API plus new fakes across the app, and it
  is symmetric across both arms, so it is one item and not two.

- **A91 (NEW, filed with A88, not fixed). The LEGACY single-job shortlist arm
  has none of the refusal model, and cannot get the truncation half of it.**
  `campaigns_submit`'s third branch reads `candidate_indices` (a bare JSON
  array of ints), not `candidate_refs`, so it never touches
  `_parse_candidate_refs_counted` and `requested_refs` is structurally 0 there
  -- passing `truncated=` would print a hardcoded-false zero, which is the
  exact defect class A88 exists to fix. It is live, not dead:
  `templates/components/candidate_table.html` still emits that modal in
  single-job scope. Four distinct gaps: (a) the list is UNCAPPED, so it is the
  one remaining request-amplification lever on this route; (b) no dedupe, so a
  repeated index tells ops to order the same structure twice; (c) negative and
  out-of-range indices are persisted -- `int("-1")` parses and only
  `shared/storage.py`'s range check stops them, AFTER the row is written and
  both emails are sent; (d) one uncoercible entry voids the whole shortlist
  into a silent `/jobs` redirect.
  *Next:* decide first whether this arm still needs to exist. If it does, the
  fix is to route it through the counted parser on a refs payload rather than
  to bolt a second parser onto `candidate_indices`.

- **A92 (NEW, filed with A88, FIXED). The admin campaigns LIST prints "0"
  in the Cands column for every ref-based row.**
  `templates/admin/campaigns_list.html` renders
  `c.candidate_indices | length`, and a 'campaign' or 'target' row leaves that
  column empty by its own shape CHECK (migration 0037/0040) -- the shortlist
  lives in `candidate_refs`. The DETAIL view is right (A84 fixed it); the list
  above it is not, so ops scanning the queue sees every ref-based order as
  empty. Read-side only, and independent of which refs the write path accepts.
  *Next:* the same `len(candidate_indices) or len(candidate_refs or [])`
  reconciliation `shared/email.py` already uses, or a call into
  `_ref_shortlist_view`'s `count`.
  **Fixed:** the cell now reads `candidate_refs` for `'campaign'` and
  `'target'` and `candidate_indices` otherwise, the same rule
  `templates/campaigns/dashboard.html:34` already carries. Four corrections to
  the filing above, established by reading the code rather than trusting it:
  (a) the route did NOT need changing -- `list_all_campaigns` is
  `select("*")`, so `candidate_refs` was already on the object; (b) the first
  enum arm is `'web'`, not `'job'`; (c) the shape CHECK does not empty
  `candidate_indices` and says nothing about it -- the ref writers simply never
  set the key and the column defaults to empty (0011:27), so nothing stops a
  future writer populating both; (d) `'api'` was NOT broken, because
  `create_api_campaign` does set `candidate_indices`, so "every ref-based row"
  overstated it and a fix applied to all ref rows would have broken a working
  arm. The `_ref_shortlist_view` option was rejected on cost: up to 60 uncached
  `get_job` reads per campaign against a 200-row list is ~12,000 full-row reads
  to render one page. Pinned by `tests/test_admin_campaigns_list.py`;
  mutation-verified (reverting the cell reds it).

- **A93 (NEW, filed with A88, FIXED). The customer confirmation email
  hardcodes "yeast display" for every assay.**
  `shared/email.py` writes "your yeast display scoping request" in the user
  half while the staff half prints the real `assay_type`, and the modal in
  `templates/components/candidate_table.html` offers three. False on ALL THREE
  submit arms today and not made false by A88, but it is the same class of
  defect -- a sentence asserting something the row does not say -- and it needs
  copy tests of its own.
  *Next:* render the row's assay in the customer copy, with a label map so an
  enum value never reaches a customer verbatim.
  **Fixed:** `_ASSAY_CUSTOMER_LABELS` maps the three values the 0011 CHECK
  permits to customer wording, verified against that CHECK, against
  `shared.campaigns.ASSAY_TYPES` and against the modal's radio values -- all
  three agree. Anything outside the map degrades to "your scoping request"
  rather than a raw enum or the word None; that branch is unreachable from any
  row the database currently holds (`assay_type` is NOT NULL and CHECKed), so
  it is cover for a future widening, not for today's data. The plain-text
  customer body never named an assay, so its change is an ADDITION for
  agreement between the two formats, not a correction. The subject line never
  claimed an assay and is untouched.
  **Wider than filed, three ways.** (1) The staff `Assay` cell was
  `campaign.assay_type.replace(...)` inline, and an `AttributeError` there
  fires ABOVE this function's only try block (which wraps just the HTTP post),
  so a row with no usable `assay_type` would have escaped into the callers'
  except blocks and lost the CUSTOMER confirmation as well as the staff
  notification. Now read through `getattr`, printing an em dash. Only
  `assay_type` is guarded: the sibling `budget_band.title()` calls are still
  unguarded, so this function is NOT None-safe. (2) Four other customer-facing
  surfaces asserted the same thing and are corrected:
  `templates/components/candidate_table.html` (the modal subtitle, above a
  radio group offering three), `templates/tools/comparison.html` (three
  places) and `templates/campaigns_new_stub.html`. (3) A pre-existing comment
  in `candidate_table.html` claiming "The microcopy in this modal is UNCHANGED
  and deliberately so" became false when that subtitle was edited, and is
  corrected rather than left behind. Pinned by 194 new lines in
  `tests/test_campaign_submitted_email.py`; three mutations verified (HTML
  body, plain-text lead, staff cell -- each reverts red).

### Ops-visible consequence of A88 (announcement, no code change)

`blueprints/admin.py::_ref_shortlist_view` does at READ time what the campaign
arm did not do at WRITE time. For **new** campaign-sourced rows only:
`duplicates` falls to 0, `out_of_range` falls to 0, and `raw` shrinks to equal
`count`. **`count` -- the order quantity ops fulfils against -- does not move**,
because admin already deduped and range-checked it before displaying it. Rows
written before this commit keep their old shape, so the fulfilment queue now
holds two populations. That is the intended direction; it is recorded here so
it is announced rather than discovered.

### Divergences from the master plan, resolved in favour of the code

- The plan states admin fulfilment "already renders from `candidate_refs` for
  campaign rows". It does not, and never did. See A84.
- The plan's 0.5 item lists the email candidate-count fix as outstanding. It
  had already shipped (`shared/email.py` reads
  `len(candidate_indices) or len(candidate_refs or [])`, and its comment
  already anticipated the 'target' source). Only the target-aware source URL
  and the per-tool line were added here.
- The plan's line numbers for `candidate_table.html` (`:218-224`, `:238`,
  `:393-396`, `:400-406`) are all stale after Phase 3. Everything was located
  by symbol.
- The plan says "no JS change needed" for 5.3. True for 5.3 itself, and the
  modal is wired with none. `static/js/candidate_table.js` did change, but for
  5.2: the disabled-button state became an inline hint, and the starred-export
  form needs its `refs` filled at submit time.

### A81 deferred, deliberately

**A81 (campaigns list does not group a multi-tool launch) is NOT fixed.** It is
a different surface from everything else in this phase -- the campaigns list,
not the results table -- and it needs a UI decision of its own (one card per
launch group with per-tool rows nested inside, group status as a rollup and
budget as a sum), which is a bigger design change than any single item above.
This phase's diff is already ~1250 lines across 16 files plus 3 new ones, and
Phase 5.3 was the blocking gap. A81 stays filed exactly as written, unchanged
and still accurate: `launch_group_id` is still read in exactly one place
(`blueprints/targets.py`, the "you just launched N runs" banner).
