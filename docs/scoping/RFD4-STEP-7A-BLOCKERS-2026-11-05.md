# RFD4-Proteina step 7A: the two blockers answered

Written 2026-11-05. Base commit `762c65c7` (tip of `origin/main`). Investigation
only: this branch adds this file and changes nothing else. No GPU spend, no Modal
call, no container run.

Scope read first: `docs/scoping/RFD4-PROTEINA-SCOPE-2026-10-30.md` §8 (step 7A row)
and §9 items 10 and 11. Sibling note read first:
`side-session-notes/.../rfd4-citation-upstream-conflict-2026-11-04.md`, written by
the step-3 worker.

Every claim below names the file, line or command behind it. Claims I could not
verify are labelled UNVERIFIED.

---

## 0. Headline

**No live money bug on `rfantibody` or `bindcraft`.** Both handle a multi-chain
upload without stranding the customer, by two different mechanisms, and
`rfd4` is the odd one out. Evidence in §1.

**A premise of this investigation does not survive the code read.** The framing
that a customer "pays the start minimum for zero designs" is wrong in two
independent places: `live_start_usd` never charges anything, and the container's
refusal bucket routes to a *full refund* — and so does the fallback bucket, so
there is no billing branch here at all. The real residual cost is smaller and
different. Evidence in §2. This changes how option C (ship with the gap) should
be priced.

---

## 1. What `rfantibody` and `bindcraft` actually do — verified against code

Both containers are built from the sibling repo
`C:\Users\lab\Documents\Claude_projects\llm-proteinDesigner` (HEAD `f37cc99` when
read). `rfd4`'s container is the only one of the three living in *this* repo
(`tools/rfd4/run_pipeline.py`).

### `rfantibody`: CROPS, twice

`docker/rfantibody/run_pipeline.py:1417-1446` is the live path:

1. `download_input(...)` fetches the raw upload.
2. `normalize_for_rfantibody(raw_target_pdb, normalized_pdb, target_chain=chain)` at `:1420`.
   That function (`backend/pdb_utils/pipeline_normalize.py:762`) delegates to
   `normalize_for_pipeline(..., target_chain=target_chain, renumber_residues=False)`.
3. `preprocess_target_pdb(normalized_pdb, target_pdb, target_chain=chain)` at `:1444`, whose
   own comment says multi-chain inputs confuse the hotspot resolver and that it
   filters to `target_chain`.

`normalize_for_pipeline` (`pipeline_normalize.py:352`) states the behaviour in its
docstring at `:383`: *"target_chain: If given, drop all other chains. Case-sensitive. ...
Every named chain must survive cleanup or a ValueError is raised."* Its chain loop at `:480`
sets `drop_chain_reasons[chain_id] = "non-target chain"` for every chain not in
`target_chain_set`. The `ValueError` fires only when a chain the form *named* is
absent — not when an extra, unnamed chain is present.

So an extra polymer chain on an `rfantibody` job is silently dropped, twice, and
the run proceeds against the named chain. The run logs it (`chains_kept` /
`chains_dropped` at `:1424-1426`).

### `bindcraft`: neither crops nor refuses — it hands the chain selector upstream

`docker/bindcraft/run_pipeline.py` **never imports `pipeline_normalize` at all**.
Verified by grepping the whole `docker/` tree in the sibling repo's main checkout:
`pipeline_normalize` appears in `boltzgen`, `pxdesign`, `rfantibody` and
`rfdiffusion` only.

Its flow instead:

- `target_pdb = os.path.join(work_dir, "target.pdb")`
- `download_input(input_url, target_pdb)` at `:1077` and `:1369` — the **raw**
  upload, uncropped
- `write_bindcraft_settings(job_spec, target_pdb, output_dir)` (`:403`), which at
  `:429-430` writes `{"starting_pdb": target_pdb_path, "chains": chain, ...}`

`chains` is BindCraft upstream's own target-chain selector.
`_normalize_target_chains` (`:646`) quotes BindCraft's documented prompt in its
docstring: *"Enter target chains (e.g., A or A,B)"*. So the extra chain is present
in the file BindCraft reads, and BindCraft selects the named chain itself.

UNVERIFIED, and worth stating because the hub's own rules file flags it: the
BindCraft image clones FreeBindCraft unpinned at build time
(`docker/bindcraft/Dockerfile.modal`, per the comment at
`shared/pdb_preflight_rules.py:204-209`), so what FreeBindCraft does with the
*unselected* chain cannot be read from either repo. The settings file we hand it
is verified; its downstream handling is not. What is verified is that nothing in
our code path refuses, and nothing crops.

### `rfd4`: REFUSES the file

`tools/rfd4/run_pipeline.py::resolve_target_chain` (`:643`), whose docstring opens
at `:646` with *"MULTI-CHAIN TARGETS ARE REFUSED."*:

```python
others = [c for c in chains if c != picked]
if others:
    _fail("input", "chain",
          f"the upload holds more than one polymer chain ({', '.join(chains)}); "
          "RFD4-Proteina here accepts a single-chain target only. Crop the "
          "structure to one chain and resubmit.")
```

### Verdict on the deciding question

> *"If they crop, then rfd4 refusing is the odd one out and the cheap fix may be
> in rfd4's own container; if they also strand the customer, this is a live money
> bug on two paid tools."*

They do not strand the customer. `rfantibody` crops; `bindcraft` defers chain
selection to upstream with the chain named. **rfd4 is the odd one out**, and the
cheap fix is plausibly in rfd4's own container — subject to the deploy-trigger
cost in §4, which is what makes it not cheap after all.

The pinned test
(`tests/test_rfd4_adapter.py:211`, named
`test_an_extra_polymer_chain_passes_the_gates_and_the_container_crops` since the
2026-11-10 crop landed; it was `..._the_container_refuses` when this report was
written)
already asserts all three slugs answer `READY` hub-side, its docstring at
`:223-229` records the two gates that make them answer so, and `:234-237`
reached the same conclusion about the siblings' containers. This report
is independent first-hand confirmation of that from the sibling repo's code, not a
re-reading of the test.

---

## 2. What a stranded rfd4 run actually costs — the premise correction

Two separate reads say the customer is not billed.

### `live_start_usd` is a balance gate, not a charge

`shared/wallet_guard.py:119` sets `RFD4_START_USD = Decimal("1.00")` (with the
comment that Leo confirmed the figure flat on 2026-11-04). `live_start_usd` is
defined at `:132` and returns `RFD4_START_USD` at `:270`.

It has **three** consumers outside tests
(`grep -rn live_start_usd --include=*.py`), and none of them debits it:

- `shared/wallet_guard.py:589`, the submit gate:
  `if 0 < pre.balance_usd < live_start_usd(resolved_slug, params)` — a comparison
  against the balance, which routes to the top-up gate.
- `blueprints/wallet.py:538`,
  `required = live_start_usd(tool_slug, params) if balance > 0 else estimate`. That
  feeds `deficit = max(required - balance, 0)` at `:551`, which the template reads
  only as the gate's show flag. Display.
- `shared/compute_campaigns.py:2026`, `start_usd = live_start_usd(campaign.tool,
  base_inputs)`, used at `:2027-2029` for `_wallet_balance_below(...)` and at
  `:2033` as `hold_amount = max(start_usd, Decimal("0.0001"))` — whose own comment
  (`:2031-2032`) says it is "what the refusal below is classified against".
  Classification, not a charge. (rfd4 cannot reach this path anyway:
  `LIVE_CAMPAIGN_TOOLS` is `frozenset({"proteina"})` at `shared/wallet_guard.py:111`.)

So the figure gates and displays; nothing debits it.
I had written that `:589` was its only consumer. It is not, and the wider grep is
the correction — the conclusion survives, the count did not. Billing is wall-clock
GPU seconds since `started_at`, which the first heartbeat stamps
(`webhooks/modal.py`, the heartbeat handler: `mark_running(job.id, ...)` on `first_beat`, then
`cumulative_secs = _elapsed_running_seconds(job)`; the request body's own
`cumulative_gpu_seconds` is commented there as attacker-controlled and ignored).

### The refusal bucket routes to a full refund

`resolve_target_chain` calls `_fail("input", ...)`, so `error.bucket == "input"`.
`_ERROR_BUCKET_TO_FAILURE_CLASS` (`shared/jobs.py:831`) has keys `pipeline`,
`storage`, `modal-submit`, `preflight`, `cancelled`, `overrun_safety_kill`,
`modal_crash`, `modal_oom`, `modal_timeout`. **`"input"` is not among them.** The
table's header comment and `classify_terminal_state`'s docstring (`:861`) both say
an unlisted bucket on a `failed` row classifies `unclassified`, and
`_REFUNDED_FAILURE_CLASSES` (`:821`) contains `unclassified` (and
`_BILLED_FAILURE_CLASSES` is at `:807`).

Caveat on the plumbing: `blueprints/jobs.py:993` builds the failed row's bucket as
`poll.get("error_bucket") or "pipeline"`, and
`gpu/modal_client.py:768` sets `error_bucket` from the container's `err.get("bucket")`.
So `"input"` survives to the row only if the container's result JSON is read; if it
is not, the row defaults to `"pipeline"`, which IS in the table — mapping to
`"tool_error"` (`shared/jobs.py:833`), which is itself in
`_REFUNDED_FAILURE_CLASSES` (`:821-827`). I did not run a job, so which path a
real stranded run takes is UNVERIFIED, but both paths refund, so it does not
change the price (§3).

### What the customer does lose

1. **GPU seconds are real.** `main()` (`tools/rfd4/run_pipeline.py:1171`) sends
   `send_heartbeat(..., stage="preparing", ...)` at `:1188`, **before**
   `download_target` / `load_target` / `resolve_target_chain` at `:1196-1198`. The
   first heartbeat stamps `started_at`, so the few seconds of container start and
   PDB download are metered. They are then refunded under the classification above,
   but they are not zero.
2. **The Workspace cap is debited anyway.** `_charge_workspace_for_completed_job`
   (`shared/jobs.py:2539`) returns early only on
   `job.status not in ("succeeded", "failed")` and on non-positive
   `gpu_seconds_used` (`:2554-2557`). The string `failure_class` does not appear
   anywhere in `:2539-2620`, so a fully refunded run still eats Workspace cap. This is the known defect recorded as memory item
   #345 (`refunded-run-debits-workspace-cap`); it is not new to rfd4.
   *Update 2026-11-10:* Leo decided a refunded run never debits the cap; PR #504
   adds the `_REFUNDED_FAILURE_CLASSES` guard to that function. Line numbers
   above predate it.
3. **No cushioned hold is placed, so the balance does not dip by a reserved
   amount.** `rfd4` is in `_LIVE_ALL_PRESETS` (`shared/wallet_guard.py:104`), so
   `live_charging_enabled` (`:123-129`) is True on every preset and submit takes the
   live branch at `:588-597`: it gates on `live_start_usd`, then calls
   `open_live_run(user_id, resolved_slug)`, which opens a **$0 anchor the meter
   debits as the run proceeds** (`shared/wallet.py:815`; the mechanism is described
   at `shared/compute_campaigns.py:2022-2025`, citing
   `supabase/migrations/0045_live_run_debits.sql`). What gets refunded on an
   `unclassified` row is therefore the debited seconds, via
   `settle_live_run(..., refund=True)` (`shared/jobs.py:2738`), not a reserved
   lump sum. Correcting my own earlier framing: there is no hold to dip the
   balance on this tool.
4. **The customer's time**, and a failure message that tells them to crop and
   resubmit — which is correct advice, just late.

---

## 3. Option C, priced: ship with the gap

What the customer loses: a submit-to-failure round trip (container start plus
download, order-of-seconds), a Workspace-cap debit that is not returned, and the
wait. What we refund: the metered seconds in full, via `unclassified` and
`settle_live_run(..., refund=True)`.

**Both bucket paths refund, so this is not a billing branch.** Which bucket
actually reaches the row is UNVERIFIED (no job was run) — but it does not matter.
If the container's `"input"` bucket arrives, `"input"` is absent from
`_ERROR_BUCKET_TO_FAILURE_CLASS` (`shared/jobs.py:831`) and the row classifies
`unclassified`. If `blueprints/jobs.py:993` defaults it to `"pipeline"`, that
bucket maps to `"tool_error"` (`shared/jobs.py:833`). Both `unclassified` and
`tool_error` are in `_REFUNDED_FAILURE_CLASSES` (`shared/jobs.py:821-827`);
`_BILLED_FAILURE_CLASSES` (`:807-817`) holds only `succeeded`,
`completed_no_yield`, `user_cancelled`, `safety_kill` and
`timeout_with_delivery`. So the GPU seconds are refunded either way, and the only
cost that survives is the Workspace-cap debit plus the customer's time.

What option C does not cost: nothing customer-facing misstates the situation. The
container's own message names the real problem and the real fix ("Crop the
structure to one chain and resubmit"), which is more than the hub-side copy in §4.1
currently manages.

Option C is defensible. It is also the only one of the three that ships step 7A
this week.

---

## 4. The two fixes, costed

### 4.1 Refuse hub-side

**Files that change.** `shared/pdb_preflight.py` (the gate) and
`shared/pdb_preflight_rules.py` (to scope the new behaviour to rfd4 alone).

The gate's current shape is the problem. `preflight_for_tool` computes
`chain_ids = _chain_tokens(target_chain)` at `shared/pdb_preflight.py:605` — the
chains **named on the form** — and `_multi_chain_block` (`:1074`) returns
`None` when `len(chain_ids) <= 1`. For this input `chain_ids == ["A"]`, so the
block never fires. The file's own chain count is available: `preview(...)` at
`:630` runs the tool's normalizer in dry-run mode (`_PREVIEW_FN` at `:244`
includes `"rfd4": normalize_for_rfd4`), and `normalize_for_rfd4`
(`shared/pipeline_normalize.py:693`) delegates to `normalize_for_pipeline` with
`target_chain` passed through, so its report carries the dropped chains. No new
parse is needed.

**Why a new rules field is required, not just a new branch.** The same preview
runs for `rfantibody` and `bindcraft`, whose containers are fine with the extra
chain (§1). Keying a refusal on "the preview dropped a chain" would refuse two
tools that work today — a regression, and a far worse one than the gap it closes.
The refusal has to be gated on a field that is true for rfd4 only; the existing
`multi_chain_supported` / `multi_chain_container_ready` pair cannot carry it.
`rfantibody` has both `False` (`shared/pdb_preflight_rules.py:384-385`) and so
does rfd4 (`:1033-1034`), so `multi_chain_supported` alone cannot separate rfd4
from rfantibody. `bindcraft` differs again — `multi_chain_supported=True`
(`:612`) with only `multi_chain_container_ready=False` (`:626`) — so no
combination of the existing pair is true for rfd4 and false for both siblings.
(`shared/pdb_preflight_rules.py:176-218` documents the pair's intent, and the
pinned test at `tests/test_rfd4_adapter.py:189-191` records that it is inert for
this input.)
So: a third field, something like `container_refuses_extra_chains`, true for rfd4
alone.

**Customer-visible copy changes: yes, and the current wording would be false.**
Both of `_multi_chain_block`'s refusal strings say the chains "were named":

```python
f"... {len(chain_ids)} chains were named ({', '.join(chain_ids)}), and its GPU
  image still handles one target chain at a time."
f"{rules.slug.title()} designs against a single target chain, but
  {len(chain_ids)} were named ({', '.join(chain_ids)})."
fix = f"Enter one chain — {' or '.join(chain_ids)} — and re-run."
```

For a file-counted refusal all three are wrong, in different ways:

- "were named" is false. The customer named one chain. The *file* holds two.
- `len(chain_ids)` is `1`, so the sentence would read "1 chains were named".
- the `fix` string is the worst of the three. It interpolates the form-named ids
  and tells the customer to enter one chain — which they already did. The real fix
  is to crop the upload, which this copy never says.

So this option needs new strings, not reused ones, and the new strings have to name
the file rather than the form. That is a third customer-visible refusal message in
a function that currently has two, each already branched on
`multi_chain_supported`.

**Modal redeploy: no.** `.github/workflows/deploy-modal.yml`'s complete
`on.push.paths` list is eight entries (`grep -n "^      - '"
.github/workflows/deploy-modal.yml`):

```
tools/**
!tools/**/meta.py
!tools/**/example/**
!tools/**/__init__.py
static/example/BPTI.fasta
static/example/ubiquitin.fasta
static/example/1HEW.pdb
.github/workflows/deploy-modal.yml
```

`shared/**` matches nothing in that list, so neither file triggers the workflow.

**What else it affects.** The pinned test goes red by design, which is its job —
`tests/test_rfd4_adapter.py:210-211` says so. Everything else routes through
`preflight_for_tool`, so a new rules field has to be defaulted `False` for the
six other tools that carry a `ToolRules` — `TOOL_RULES`
(`shared/pdb_preflight_rules.py:1132-1140`) has seven entries, not fourteen — or
the preflight suite (`tests/test_pdb_preflight.py`) moves.
The new field is also a *claim about a container*, which under house rules needs
the `file:line` that enforces it — here `tools/rfd4/run_pipeline.py:643`, which is
in this repo and so actually citable. That is a point in this option's favour: the
existing two flags cite a sibling repo nobody reading `pdb_preflight_rules.py` can
check.

### 4.2 Crop in the container

**Files that change.** `tools/rfd4/run_pipeline.py` only — `resolve_target_chain`
swaps its `_fail` for a crop. Arguably a one-function change, and the smallest diff
of the three options.

**Customer-visible copy: none.** No refusal to word, because there is no refusal.
The behaviour becomes what `rfantibody` already does, which is also what the
customer almost certainly wants.

**Modal redeploy: yes — and, on this base, not rfd4 itself.** SUPERSEDED
2026-11-10: #505 (merged, main `613aa2e2`) added rfd4's matrix row, so the fleet
is **ten** apps and the crop now deploys itself. The paragraph below is the
2026-11-05 reading, kept because §4.3's recommendation rests on it; read "nine"
as "the nine that existed then".

`tools/rfd4/run_pipeline.py`
matches `tools/**` and no negation covers it (the three negations are `meta.py`,
`example/**` and `__init__.py`). The workflow's matrix is the nine apps named in its
header: mpnn, af2, colabfold, esmfold, boltz2, esmfold2_design, iggm, proteina,
opendde. **rfd4 is not in that matrix on this base** — step 6 adds its row. So
merging this today would redeploy nine *other* tools' GPU images while not deploying
the change itself.

That is the decisive cost, and it carries a hard constraint: §8 of the scope doc
says **merge only when the fleet is idle**. Per the house rule
(`gpu-redeploy-only-when-idle`), that means checking `tool_jobs` for
pending/running rows across all nine before the merge, not just rfd4's -- ten as
of #505, rfd4's own rows now among them.

**What else it affects.** The container is the only place that knows the file's
real chain list, so cropping there is where the information already is — no new
plumbing. But cropping silently changes what the customer's structure *is*, which
is a thing the hub currently tells them about for `rfantibody` only via container
logs (`docker/rfantibody/run_pipeline.py:1421-1426` for the kept/dropped chain
lists, `:1447` for the preprocessed-target line), not via the UI. Also: `resolve_target_chain`'s docstring opens
*"MULTI-CHAIN TARGETS ARE REFUSED"* and is cited from three other places
(`tools/rfd4/__init__.py`, `shared/pipeline_normalize.py:707-708`,
`tests/test_rfd4_adapter.py`); all four move together or the claims rot.
**Superseded 2026-11-10:** that docstring now opens *"A RUN STILL DESIGNS
AGAINST ONE CHAIN"* and records the crop. `shared/pipeline_normalize.py` and
`templates/tools/rfd4_results.html` were updated in the same change, both to
say that the single-chain rule is about the FIELD and not the upload.

### 4.3 Recommendation for §9 item 11

**Crop in the container (4.2), merged on an idle fleet, after step 6 lands.**

Reasons, in order of weight:

1. It makes rfd4 behave like `rfantibody`, which is the house answer to this input
   (§1). Refusing hub-side would make rfd4 the only tool refusing a file its
   siblings accept — and §1 establishes that the siblings' acceptance is deliberate
   in `rfantibody`'s case, not an oversight.
2. It invents no customer-visible copy. Option 4.1 needs three new strings, and
   §4.1 shows the existing ones are false in three distinct ways for this input, so
   there is no reuse to be had.
3. It is the smallest diff: one function, one file.
4. The redeploy cost, which is 4.2's only real objection, is **already being paid**.
   Step 6 adds rfd4's matrix row, which means a fleet-idle merge is on the critical
   path for step 6 regardless. Landing the crop in that same window costs nothing
   extra. Doing it before step 6 is the expensive ordering, because then the
   redeploy happens twice.

If the fleet cannot be made idle in time and step 7A has to ship this week, take
option C (§3) and leave item 11 open: the gap costs seconds and a Workspace-cap
debit, and the container's own message already tells the customer the right thing.
Do not take 4.1 to avoid the redeploy — it trades a one-function change for a new
rules field, three new strings and a divergence from the two paid siblings.

**This recommendation requires a code change in `tools/**`, which this session was
told not to touch.** Reporting it rather than doing it, as instructed.

**ACCEPTED AND IMPLEMENTED 2026-11-10.** Leo took 4.2: the container crops, with
no hub-side refusal and no preflight change. The crop lives in
`tools/rfd4/run_pipeline.py::load_target` -- it runs on the PDB text before
`pdb_to_cif`, because `resolve_target_chain` receives a mapping and has no access
to the file -- and the mmCIF path still refuses. Scope §9 item 11 is CLOSED. The
merge still needs the idle-fleet window this section argued for. Everything above
is the 2026-11-05 reasoning, kept as the record of the decision.

---

## 5. The citation token — §9 item 10

### 5.1 URL re-check, 2026-11-05

Both checked with `curl` from this worktree today:

```
curl -s -o /dev/null -w "%{http_code} %{content_type} %{size_download}\n" https://rfd4-proteina.org/
→ 200 text/html; charset=utf-8 69019

curl -s -I https://rfd4-proteina.org/assets/paper/RFD4-Proteina.pdf
→ HTTP/1.1 200 OK
  Content-Length: 54797449
  Content-Type: application/pdf
  Last-Modified: Tue, 03 Nov 2026 15:47:39 GMT
  Server: GitHub.com
  ETag: "6aea021b-3442489"

curl -s -r 0-1023 ... → 206, first 8 bytes: %PDF-1.7
```

Three findings, all new:

1. **The project page is up today.** 200, 69 KB of HTML. It was 503 on 2026-11-04
   (sibling note §4). So the 503 was transient, not a dead page.
2. **The PDF is up and is a real PDF.** 54,797,449 bytes — byte-identical in size to
   the 2026-11-04 read — and a range request returns `%PDF-1.7`.
3. **`Server: GitHub.com`.** The domain is GitHub Pages. That is the most useful
   fact here: the page's uptime is GitHub's, which both explains the one-day 503
   and means the content is served from a git repository rather than from
   project-run infrastructure.

A web-fetch subagent independently got 200 on the project page today and read its
title as *"Biomolecular design through latent reasoning with RFD4-Proteina"*,
matching the pinned README's `title`. It could not retrieve the PDF (its own 10 MB
cap), which is why the `curl` probes above are the evidence for that half.

### 5.2 What the guard actually requires — all fourteen entries

`tests/test_citations_name_the_right_model.py`. `REQUIRED_URL_TOKENS` at `:197`,
typed `dict[str, tuple[str, str | None]]`, documented as *"(token required in
paper_url, token required in github_url or None where the tool declares no
repository). A DOI or an owner/repo pair, because those are what actually identify
a work"*.

| slug | paper token | kind | repo token |
|---|---|---|---|
| af2 | `s41586-021-03819-2` | publisher DOI suffix | `sokrypton/ColabFold` |
| bindcraft | `s41586-025-09429-6` | publisher DOI suffix | `martinpacesa/BindCraft` |
| boltz2 | `2025.06.14.659707v1` | bioRxiv dated id | `jwohlwend/boltz` |
| boltzgen | `2025.11.20.689494` | bioRxiv dated id | `HannesStark/boltzgen` |
| colabfold | `s41592-022-01488-1` | publisher DOI suffix | `sokrypton/ColabFold` |
| esmfold | `science.ade2574` | publisher DOI suffix | `facebookresearch/esm` |
| esmfold2_design | `2026.06.03.729735` | bioRxiv dated id | `Biohub/esm` |
| iggm | `2024.09.19.613838` | bioRxiv dated id | `TencentAI4S/IgGM` |
| mpnn | `science.add2187` | publisher DOI suffix | `dauparas/ProteinMPNN` |
| opendde | `2607.03787` | arXiv id | `aurekaresearch/OpenDDE` |
| proteina | `2603.27950` | arXiv id | `NVIDIA-BioNeMo/Proteina-Complexa` |
| pxdesign | `s41467-023-38328-5` | publisher DOI suffix | **`None`** |
| rfantibody | `s41586-025-09721-5` | publisher DOI suffix | `RosettaCommons/RFantibody` |
| rfdiffusion | `s41586-023-06415-8` | publisher DOI suffix | `RosettaCommons/RFdiffusion` |

Totals: 6 publisher DOI suffixes of the `s415…` form plus 2 of the `science.…`
form (8), 4 bioRxiv dated ids, 2 arXiv ids. All fourteen paper tokens are
persistent identifiers. One of fourteen (`pxdesign`) declares no repository.

Two assertions consume the map:

- `test_every_tool_meta_is_covered_here` (`:476`):
  `on_disk = {p.parent.name for p in (REPO/"tools").glob("*/meta.py")}`, then
  `assert on_disk == set(mapping)` for each of the three maps. **This is why no
  `meta.py` is the only state that invents nothing** — the guard discovers tools on
  disk, so the file's existence alone is what demands an entry.
- `test_the_links_point_at_this_tools_own_work` (`:637`):
  `@pytest.mark.parametrize("slug,tokens", sorted(REQUIRED_URL_TOKENS.items()))`,
  asserting `meta.paper_url.startswith("https://")`, `paper_token in meta.paper_url`,
  and either `repo_token in meta.github_url` or `not meta.github_url`.

**The structural fact that decides this question: that second test is parametrized
per slug.** Each slug gets its own test case reading only its own tuple. So adding
or changing *one entry's value* is reachable by exactly one of fifteen cases and
touches no other tool's guarantee. Only a change to the map's **shape** — making
the paper token `Optional`, or adding a skip affordance — would let any of the
other fourteen opt out.

The file also carries two written precedents, both rejections of weak tokens, and
both directly on point:

- `esmfold2_design`'s comment (`:208-209`) rejects using the publisher's path because *"the token
  'biohub.ai' identifies no particular paper, so it would pass a link to any other
  one they publish"*.
- `proteina`'s comment (`:218`) records that its old pair `("proteina-complexa",
  "proteina-complexa")` was *"WEAKER than the rest of this map ... neither half
  identified a particular thing"*.

The common failure in both is a token that identifies a **publisher or a project**
rather than **one document**.

### 5.3 The options, with the weakening quantified

Settled already (sibling note §5, scope §9 item 10): `FIRST_AUTHOR = "Corley"`,
`PAPER_YEAR = "2026"`, from the `@article{corley2026rfd4proteina}` block at README
lines 222-235 of `RosettaCommons/RFD4-Proteina` at SHA
`aaccdd2ad0720c180468df5f9cf037a713d403e5` (`tools/rfd4/Dockerfile.modal:34`). Only
the `REQUIRED_URL_TOKENS` tuple is open.

The repo half is not in question: `RosettaCommons/RFD4-Proteina` is a conventional
owner/repo pair, same form as `rfantibody`'s and `rfdiffusion`'s.

**Option A — keep the shape, use the PDF path.**
`("assets/paper/RFD4-Proteina.pdf", "RosettaCommons/RFD4-Proteina")`.

- Weakening for the other fourteen: **zero cases**. No shape change; the map's
  annotation already accepts any `str`, and per-slug parametrization isolates the
  value to rfd4's own case.
- What rfd4's own entry stops proving: persistence. A DOI cannot be moved; a path
  under `assets/` can be. Nothing would catch upstream reorganising their site.
- What it still proves: that the URL names *this one document*. `RFD4-Proteina.pdf`
  under `assets/paper/` identifies a single file, which is strictly more than either
  rejected precedent managed — `biohub.ai` was a publisher and `proteina-complexa`
  was a project slug. Combined with the 200 and the `%PDF-1.7` bytes in §5.1, the
  token points at a live document.

**Option B — a project-page token.** e.g. `("rfd4-proteina.org", ...)`.

- Weakening for the other fourteen: **zero cases**, same reasoning as A.
- What it stops proving: everything the two recorded precedents were written to
  stop. `rfd4-proteina.org` identifies a *project*, not a paper, and would pass a
  link to any other page on that domain — the exact objection in
  `esmfold2_design`'s comment, transposed. It is also the weaker of the two in the
  one dimension A is weak in: the domain's uptime is GitHub Pages' (§5.1 finding 3)
  and it served a 503 as recently as yesterday.
- **Not recommended**, and the file already says why.

**Option C — grow the shape: `paper_token: str | None`, with `None` meaning
"no persistent identifier exists".**

- Weakening for the other fourteen: **all fourteen cases**. Every one of them
  currently asserts `paper_token in meta.paper_url`; under an `Optional` token that
  assertion becomes conditional, so any future tool — or any future edit to an
  existing one — can satisfy the guard with `None` and no paper link check at all.
  That removes the only thing standing between any of them and a `paper_url`
  pointing at the wrong paper.
- It also interacts badly with `test_the_leak_maps_are_not_empty` (`:492`), which
  documents that an emptied map yields an empty `parametrize` that pytest **skips**
  (there is no pytest ini in the repo making that an error). An `Optional` shape
  moves the guard from "fails loudly" toward "passes quietly", which is the failure
  mode that test exists to name.
- **Not recommended.** It is the only option that costs the other fourteen tools
  anything, and it costs all of them.

**Option D — ship step 7A with no `tools/rfd4/meta.py`.** Not viable, and this is
worth stating precisely because the framing so far has treated the citation guard
as the only consumer. It is not — but the other consumers key on *registration*,
not on the file:

- `tests/test_pilot_recipes.py:141` and `tests/test_worked_examples.py:214`
  discover via the `tools_app` fixture (registry slugs), and `meta_for`
  (`shared/tool_meta.py:57-62`) returns `None` when no `meta.py` imports, so
  `hasattr(None, "PILOT")` is `False`. Both go red the moment rfd4 registers,
  regardless of the citation guard.
- That matches the scope doc's measured experiment (§8): registering produced
  `meta.py declares no PILOT for: ['rfd4']` and `meta.py declares no EXAMPLE for:
  ['rfd4']` among its 66 failures.

So registration requires a `meta.py`, which requires all three map entries. Option
D is only available by not registering, i.e. not shipping step 7A.

### 5.4 Recommendation for §9 item 10

**Option A: `("assets/paper/RFD4-Proteina.pdf", "RosettaCommons/RFD4-Proteina")`,
keeping the guard's shape unchanged.**

A and B both cost the other fourteen tools nothing; B is the weaker token and the
file already records the objection to its form, which is why A wins between
them. Only option C costs anything, and it costs all fourteen.

Option A identifies one document rather than a publisher or a project, which is
the standard the two recorded rejections were enforcing. Both halves resolve
today: the PDF 200s with real PDF bytes (§5.1) and the repo is the one we pin.

One honest caveat to write into the entry's comment, in the present tense because
it is true now: this is the first token in the map that is a URL path rather than a
persistent identifier, so it would not survive upstream moving the file. If and
when the preprint gets a DOI, the entry should be replaced with it. The guard is
unchanged either way, which is the point.

---

## 6. The template question

**`templates/tools/rfd4_form.html` does not exist.** `ls templates/tools/` lists a
`<slug>_form.html` and a `<slug>_results.html` for all fourteen registered tools,
plus `rfd4_results.html` (from step 5) — and no `rfd4_form.html`. rfd4 is the only
slug in that directory with a results template and no form template. `tools/rfd4/__init__.py:273` declares
`form_template="tools/rfd4_form.html"`.

**Does registering without it break the form page? In production, no. In the test
suite, yes.** This is the nuance worth carrying into step 7A.

Production: `blueprints/tools.py::_require_tool` (`:340-347`) runs before any
render —

```python
if adapter is None:  return None, (render_template("404.html"), 404)
if not tool_enabled(tool_slug):  return None, (render_template("coming_soon.html"), 404)
```

`tool_enabled` (`shared/feature_flags.py:41-44`) returns `True` only when
`FLAG_TOOL_RFD4` is set to an on-value. With the flag unset — which is how step 7A
ships — every request to rfd4's form page returns `coming_soon.html` and a 404
before `tool_form` (`:1236`) reaches `render_template(adapter.form_template)` on
any of its branches (`:1296` anonymous, and the signed-in ones). `_form_takes_structure`
(`:747`), which would raise `TemplateNotFound` from
`env.loader.get_source(env, adapter.form_template)` at `:754`, has exactly one
caller, at `:1398` (verified by `grep -rn _form_takes_structure blueprints/`),
inside `tool_form` and therefore behind the same gate.

Test suite: `tests/conftest.py:158`'s `all_tools_app` fixture does
`slugs = sorted(a.slug for a in tool_base.all_adapters())` (`:164`) and then
`mp.setenv(flag_name(slug), "on")` for every one of them — its docstring (`:159`) says
*"Every registered adapter flagged on, not a remembered subset."* So registration
flags rfd4 on inside the suite and the missing template surfaces. That is the
scope doc's measured `24 TemplateNotFound tools/rfd4_form.html`.

**Whose step it is: step 7.** §8's step 7 row names
`shared/tools_catalog.py`, `shared/tool_chooser.py`, `shared/email.py` and the
comparison and guide templates — it does **not** name `rfd4_form.html`. The only
attribution anywhere in the doc is a prose aside at scope-doc line 1132 — in the
paragraph after the §8 table that explains why registration is its own step —
*"47 of the 66 are the two templates steps 7 and 5 own"*; step 5 shipped `rfd4_results.html` (it is on disk),
so by elimination the form template is step 7's.

Recommendation: **add `templates/tools/rfd4_form.html` explicitly to §8's step 7
row.** Right now it is owned only by a prose aside in a different step's row, which
is how a 24-failure dependency goes missing. Not created here, as instructed.

---

## 7. Summary table

| Question | Answer | Where |
|---|---|---|
| Do `rfantibody`/`bindcraft` strand the customer? | **No.** rfantibody crops twice; bindcraft hands the chain selector to upstream. rfd4 is the odd one out. | §1 |
| Is there a live money bug on the two paid tools? | **No.** | §1 |
| Does a stranded rfd4 run bill the start minimum? | **No.** `live_start_usd` is a balance gate; bucket `"input"` is unclassified ⇒ full refund. Residual: Workspace-cap debit (no `failure_class` guard; PR #504 adds one), and the metered seconds of container start, refunded. No cushioned hold: rfd4 is live-charging, so submit opens a $0 anchor. | §2 |
| §9 item 11 recommendation | Crop in the container, on an idle fleet, in step 6's redeploy window. | §4.3 |
| Does that redeploy the fleet? | Crop-in-container: **yes**. On this base, nine apps and not rfd4; as of #505, all **ten** including rfd4. Refuse hub-side: **no**. | §4.1, §4.2 |
| §9 item 10 recommendation | `("assets/paper/RFD4-Proteina.pdf", "RosettaCommons/RFD4-Proteina")`, guard shape unchanged, zero weakening for the other fourteen. | §5.4 |
| Project page / PDF today | **200 / 200.** Yesterday's 503 was transient; the domain is GitHub Pages. | §5.1 |
| Does `rfd4_form.html` exist? | **No.** Flag gate spares production; the suite breaks via `all_tools_app`. Step 7 owns it. | §6 |
