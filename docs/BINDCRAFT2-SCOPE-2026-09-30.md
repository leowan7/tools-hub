# BindCraft 2 — research and scoping pass

Date: 2026-09-30. Author: hub worker session (research only).
Scope of this document: research and scoping. **No product code was changed and no GPU job was launched.**

Every claim below names the command, `file:line` or URL that verified it.
Anything not verified that way is labelled **UNVERIFIED** or **UNKNOWN**.

---

## 0. Recommendation in one line

**Neither replace nor add alongside: keep BindCraft 1 (FreeBindCraft) exactly as it is, and do not start engineering work on a self-serve BindCraft2 tool until a commercial hosting licence from the Pacesa Lab / University of Zurich is in hand** — BindCraft2 ships under a source-available licence whose Hosted Service Restriction covers, on its face, a self-serve tool on tools.ranomics.com.

There is a second path the same licence leaves open, and it is worth knowing before anyone writes to UZH: the licence expressly exempts "the internal use of the Software" and "the provision of design outputs (for example designed binders, sequences, or structures)" (`LICENSE:60-64`). Running BC2 ourselves and delivering binders as a service — the AI Binder Sprint and custom-campaign side of the business — appears to need no commercial licence. Only the self-serve hub tool does. That distinction should be put to a lawyer before it is relied on; I am not one, and this document is not legal advice.

**Does this recommendation depend on which bindcraft image serves production?** No. It is licence-driven, and the licence question is the same whichever image is running — so the answer is "do not start" either way, and §2.1's UNKNOWN does not need resolving to act on §0. What does shape the *work* if the gate ever clears is which image today's tool runs, and that is partly settled: the hub dispatches every job through Modal and has no RunPod code path (§2.1), so Phase 1 and rungs 1 to 3 are correctly costed as Modal work. Which image the deployed app holds is still UNKNOWN 8, and it decides what baseline a BC2 comparison would run against.

Everything in sections 3 to 5 is contingent on that gate clearing.

---

## 1. The blocker: BindCraft2 is not licensed for what we do

Verified by reading `LICENSE` in the BindCraft2 clone at HEAD `ce3150f8d1132a0c900d66ad8f6d85ec08c9ad17`
(repo <https://github.com/PacesaLab/BindCraft2>, cloned to the session scratchpad on 2026-09-30).

The file is titled **"BindCraft2 Source-Available License (Hosting-Restricted)"**, copyright Martin Pacesa, University of Zurich. The operative clauses, quoted at length because the qualifiers matter:

- **Hosted Service Restriction** (`LICENSE:36-44`) — "You may not, without a separate written commercial license from the copyright holders, provide the Software, a modified version of the Software, or a service whose material functionality is provided by or derived from the Software, to Third Parties as a hosted, managed, cloud, API, web application, workflow platform, or software-as-a-service offering, **where that offering provides Third Parties with access to any substantial set of the features or functionality of the Software**". The emphasised qualifier is load-bearing, and this document does not attempt to judge it.
- A separate paragraph (`LICENSE:52-58`) closes the obvious workaround: "Making the Software available to Third Parties as an invocable tool, plugin, agent action, connector, or workflow step within a hosted platform, such that those Third Parties can run the functionality of the Software without installing it themselves, is treated as provision of a Hosted Service". It carries its own exception, which does not help us: "this does not apply where a Third Party has installed the Software themselves, or on infrastructure operated for their sole benefit, and directs its execution through their own agent, script, or automated tooling."
- **The carve-out that matters** (`LICENSE:60-64`) — "the redistribution of source code or binaries for others to run themselves, the internal use of the Software (including execution directed by your own automated or agentic tooling), and the provision of design outputs (for example designed binders, sequences, or structures) are not Hosted Services and require no commercial license."
- **Naming Restriction** (`LICENSE:66-79`) — narrower than a blanket trademark bar. It forbids using the name "BindCraft2" to identify, market or describe a Hosted Service or derivative "in any manner that asserts or implies equivalent functionality to, or official association with, the original Software", and explicitly "does not prevent accurate statements that a work is based on, derived from, or compatible with BindCraft2" (`:72-74`). A separate paragraph (`:76-79`) reserves the trademark itself to the University of Zurich.
- **Termination** (`LICENSE:102-108`): rights terminate automatically on violation; they are reinstated if you cease within 30 days of the copyright holders notifying you; a subsequent violation after reinstatement terminates them permanently.
- The licence states of itself that it is **not an Open Source Initiative approved open source licence**.

Reading those together: a self-serve form on tools.ranomics.com, where a customer supplies a target and BC2 runs on our infrastructure for them, reads as the "invocable tool … without installing it themselves" case. Running BC2 ourselves and shipping the resulting binders to a Sprint or custom-campaign customer reads as the "provision of design outputs" carve-out. Both readings are mine, not a lawyer's. The two halves of our business fall on opposite sides of the same licence.

The third-party component list does not rescue the self-serve case either. The licence lists AlphaFold2 + ColabDesign (Apache-2.0) and ProteinMPNN (MIT) + HyperMPNN as third-party components whose "rights in it, including any right to operate it as a hosted service, derive from its own terms and not from this license." That is a statement that the *dependencies* are free, not the BindCraft2 pipeline code — and the pipeline code is precisely what a hosted tool would be running.

**Contrast with what we run today.** BindCraft 1 is used via the cytokineking FreeBindCraft fork — both candidate builds clone it (`llm-proteinDesigner/docker/bindcraft/Dockerfile:47` and `Dockerfile.modal:46`; see §2.1 — which of them produced the running image is UNKNOWN) — and FreeBindCraft carries no hosting restriction. Our current hosting of BindCraft 1 is not affected by the BindCraft2 licence.

**Action, and it is a business action not an engineering one:** Leo (or whoever owns partnerships) writes to the Pacesa Lab / UZH technology transfer office asking the terms of a commercial hosting licence, and separately gets a lawyer's read on whether the design-outputs carve-out covers our Sprint work. Until those answers exist, every BC2 engineering rung below is dead weight, **including the free CPU-only image build probe** (rung 0b is the exception: it is about today's BindCraft 1, not BC2, and is unaffected) — building an image is not hosting, but spending worker time on a tool we may never be allowed to serve is the waste this document exists to prevent.

---

## 2. What we run today (BindCraft 1)

### 2.1 Two candidate builds, one dispatch path, and neither build is reproducible

**Which *image* the hub's bindcraft tool runs is UNKNOWN**, and this pass did not establish it. There are two candidate build definitions in the same directory, and telling which one produced the running artifact needs live deploy state. Do not read the two candidate write-ups below as naming the live one.

**What this pass did establish, from a repository read, is the dispatch path.** The tools-hub has exactly three *application* job-submission sites and all three are Modal: `blueprints/tools.py:2635` and `blueprints/jobs.py:1244` call `current_app.modal_client.submit(...)`, and `shared/compute_campaigns.py:2110` calls `ModalClient().submit(...)`. `submit` resolves the target generically — `modal.Function.from_name(modal_app_name(tool), ...)` at `gpu/modal_client.py:474-475`, with `modal_app_name` returning `f"ranomics-{tool}-prod"` at `:379-381` — so there is no per-tool branch and bindcraft resolves to `ranomics-bindcraft-prod`, the name `llm-proteinDesigner/infrastructure/modal/bindcraft_app.py:118` deploys under (`app = modal.App(f"ranomics-{_TOOL}-prod")`). Other call sites spawn Modal functions directly, outside `ModalClient.submit` — operator scripts (`scripts/gate1_boltz2_smoke.py:330`, `scripts/prewarm_esmfold2_design.py:50`) and per-tool fan-out inside proteina and esmfold2_design (e.g. `tools/proteina/shard_driver.py:902`, `tools/esmfold2_design/modal_app.py:724`) — which is why the count above is scoped to application dispatch. Every one of them is Modal, and none is bindcraft. Grepping `runpod` across every `.py` file in the hub returns no dispatch code. Case-sensitively there are three hits, all comments: `shared/pdb_preflight_rules.py:586`, `shared/score_legends.py:140` and `:143`. Case-insensitively there are eleven, of which ten are comments or docstrings and one is a string inside a test's assertion message (`tests/test_rfdiffusion_score_era_caveat.py:142`, which is code but not dispatch). There is no RunPod dispatch in this repository.

So for **the hub's bindcraft tool**, the serving path is Modal. What stays UNKNOWN, and it is the part that matters for a BC2 upgrade, is **which image the deployed `ranomics-bindcraft-prod` app currently holds** — an app's live image is deploy state, and a deployed app can lag the Dockerfile in the checkout. The RunPod `kendrew-bindcraft:v7` image is consumed by `llm-proteinDesigner/backend/config.py:111`, the sibling repo's own backend, which is a different product surface from this hub; whether anything still serves from it is a question about that product, not this one. Note the consequence for §2.8: five hub comments name the v7 tag as this tool's image, and on the evidence above they may be describing the sibling backend's image rather than the hub's runtime. That is not asserted here — it is flagged, because it is the same provenance problem one level down.

**Candidate A — RunPod, `docker/bindcraft/Dockerfile`.** `llm-proteinDesigner/.github/workflows/docker-bindcraft.yml:40-43` builds with `context: docker/bindcraft` and no `file:` key, so Docker's default applies and the file built is `Dockerfile`, not `Dockerfile.modal`. It is tagged `ghcr.io/<owner>/kendrew-bindcraft:v7` (`:44`). Its header comment says "Adapted for RunPod pod deployment" (`Dockerfile:9`). The consuming config is `llm-proteinDesigner/backend/config.py:111`, `runpod_image_bindcraft: str = "ghcr.io/leowan7/kendrew-bindcraft:v7"`, read at `backend/gpu/__init__.py:118` and `backend/jobs/service.py:28`. This is the story five tools-hub comments tell, all of them naming the tag: `shared/pdb_preflight_rules.py:185` and `:586`, `templates/tools/bindcraft_form.html:406`, `tests/test_hotspot_picker_runtime.py:74` and `tests/test_pdb_preflight.py:933`.

**Candidate B — Modal, `docker/bindcraft/Dockerfile.modal`.** Also built by a workflow: `llm-proteinDesigner/.github/workflows/deploy-modal.yml:89` sets `ALL='bindcraft boltzgen pxdesign rfantibody rfdiffusion'`, and the app it deploys, `llm-proteinDesigner/infrastructure/modal/bindcraft_app.py`, sets `_DOCKERFILE = f"docker/{_TOOL}/Dockerfile.modal"` (`:42`) and builds it with `modal.Image.from_dockerfile(_DOCKERFILE, add_python=None)` (`:112`). The hub-side caller is a Modal client: `gpu/modal_client.py:312-313` holds this tool's container ceilings, and `:23` and `:474` resolve `modal.Function.from_name("ranomics-<tool>-prod")`, the name `bindcraft_app.py` deploys under.

**What would settle the rest, and it is cheap.** Two reads, neither of which is a launch and neither of which costs anything.

1. **Confirm the dispatch finding against production rather than against code.** `shared/jobs.py:620` carries `modal_function_call_id` on the job record, written by `set_modal_call` (`:1448-1450`) and read back at `:649` and `:1696`. A Modal `FunctionCall` id exists only for a job Modal ran, so a recent completed bindcraft row with that column populated confirms from live data what the code path says. This is a read of production job state; this session has no production database access, so it was not run here.
2. **Identify the image the deployed app holds**, which is the actual open question. A `modal app list` / deployment inspection for `ranomics-bindcraft-prod`, or the image reference on a recent run, names it. Nothing in either checkout answers this.

Read (1) before (2): if bindcraft rows carry no `modal_function_call_id` while other tools' rows do, the dispatch finding above is wrong and should be re-derived before anything is built on it.

One further file fact, recorded because it is easy to misread as evidence: `shared/pdb_preflight_rules.py:586` names `config.runpod_image_bindcraft`, a symbol that **does not exist anywhere in tools-hub** (grepped) and lives only in the sibling repo's `backend/`. That is a comment reaching across repositories, not a hub code path.

**What is reproducible about either: nothing.** Both clone unpinned `master`:

| | clone | install |
|---|---|---|
| `Dockerfile` | `:47` `git clone --depth 1 https://github.com/cytokineking/FreeBindCraft.git` | `:60` `--no-pyrosetta` |
| `Dockerfile.modal` | `:46` same line | `:54` same flags |

Exactly one tools-hub comment describes the runtime as FreeBindCraft without PyRosetta: `shared/score_legends.py:443`, "FreeBindCraft with --no-pyrosetta (docker/bindcraft/Dockerfile.modal)". Note which file it cites — it is a Candidate B claim, not a neutral one, and it is the only comment in the hub that names the upstream project at all. It is backed by a *build definition*, which is more than a bare assertion. It is less than evidence about the running service. What is established is that both candidate Dockerfiles clone FreeBindCraft. What is **not** established is that the deployed artifact was built from either of them: `shared/pdb_preflight_rules.py:185` says of `kendrew-bindcraft:v7` that it "cannot be inspected from here", and a pushed tag can lag, or diverge from, the workflow that last built it. So the open question is not only *which FreeBindCraft commit* is in the image — it is whether the image is FreeBindCraft at all. Nothing on disk closes that, and no claim about our production service should rest on it.

`llm-proteinDesigner/docker/bindcraft/Dockerfile.modal` (Candidate B), quoted here because §2.2's wrapper reads it:

```
FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04
RUN git clone --depth 1 https://github.com/cytokineking/FreeBindCraft.git /app/src && ...
RUN bash /app/install_bindcraft.sh --pkg_manager conda --cuda 12.1 --no-pyrosetta
RUN pip install --no-cache-dir requests "pydantic>=2,<3"
```

There is **no tag, no commit pin and no image digest** in either file. `--depth 1` takes whatever FreeBindCraft `master` happens to be at build time, and `install_bindcraft.sh` resolves its own dependency set at build time. The brief asked for "the pinned upstream commit or image digest" — **it does not exist for bindcraft, on either candidate.** What source the pushed `kendrew-bindcraft:v7` tag actually contains could only be recovered by reading that image's config blob (registry-API read; not done in this pass).

This matters for the BC2 decision: we have no baseline we can rebuild, so any "BC2 vs BC1" comparison would compare BC2 against an image we cannot reproduce. Same failure class as the proteina image that could not be rebuilt once `dm-haiku` moved.

### 2.2 Modal wrapper (Candidate B only)

`llm-proteinDesigner/infrastructure/modal/bindcraft_app.py`:

- `_GPU = "A100-80GB"`
- `_MAX_SESSION_S = 82800` (23 h — Modal's `@app.function` timeout ceiling)
- `_PYTHON = "/miniforge3/envs/BindCraft/bin/python"`
- raw output Volume `ranomics-bindcraft-raw` mounted at `/raw`
- the subprocess is given `timeout=max(60, _MAX_SESSION_S - 120)`

### 2.3 Container ceilings

`gpu/modal_client.py:312-313`:

```python
("bindcraft", "pilot"):         7200,
("bindcraft", "full"):          14400,
```

**Open anomaly.** `docs/qa/FAILED-RUNS-2026-09-30.md` records job `c43329f3` (2026-09-22) as a **pilot** that failed with "timed out after 14400 s" and ran 14403 seconds of **wall time** — the **`full`** ceiling, not the pilot 7200. (Its GPU-seconds were not recorded; see the note on that file's columns in §2.9.) Three explanations are open: the row selection for a `long_running` preset does not use the preset the job was booked under; the recorded preset is wrong; or the pilot row held a different value on 2026-09-22 and has since been changed — `FAILED-RUNS-2026-09-30.md:101` raises the third itself ("the table now reads pilot 7200 s … when that changed is unverified"). **UNVERIFIED — not traced in this pass.** It overlaps the territory of the concurrent worker `task_9a3538be` (reconciling bindcraft/boltzgen runtime numbers), so it is reported here rather than fixed. Whoever owns it should start at the `submit` path in `gpu/modal_client.py` and at how a `long_running` preset picks its row.

### 2.4 Wallet estimates and caps

`shared/wallet_estimates.py:360-368`:

```python
"bindcraft": ToolSpec(slug="bindcraft", gpu_class="A100-80GB",
    expected_gpu_seconds=3600.0, designs_per_run_baseline=2,
    scaling_param="num_designs", base_hard_cap_usd=Decimal("8.00"),
    absolute_cap_usd=Decimal("500.00")),
```

Rate card `shared/wallet_estimates.py:74`: `"A100-80GB": 0.001028` USD/s raw (= $3.70/GPU-hour). `WALLET_MARKUP = Decimal("1.70")` at `:54` (customer $6.29/GPU-hour). `HOLD_CUSHION_MULTIPLIER = Decimal("1.5")` at `:103`.

### 2.5 The only measured runtime

`docs/VALIDATION-LOG.md`: job `1c4d5803` (2026-05-28) is **the only successful bindcraft run with recorded GPU seconds** — 1170 GPU-s (19.5 min), 2/2 candidates, hold $4.37 / release $2.33 / net $2.04. (Two *failed* runs also carry measured GPU-seconds, 5015 and 5012; see §2.9. Both are failure rows in that file, so neither anchors a runtime curve.) The 2026-04-22 4Z18 pilot passed but its GPU seconds were "(not captured)".

So: **one run, one size, one design count.** Every runtime constant downstream rests on it.

### 2.6 Preflight size envelope

`shared/pdb_preflight_rules.py:574+` (`_BINDCRAFT`): A100-80GB, `multi_chain_supported=True`, **`multi_chain_container_ready=False`**, `hotspots_required=True`, `min_target_aa=30`; SizeEnvelope `hard_cap_target_aa=500`, `soft_warn_target_aa=300`, `hard_cap_combined_aa=600`, `runtime_base_min=40.0`, **`runtime_alpha=1.5`**, `runtime_baseline_designs=4`, `cap_basis="literature"`; GapThresholds warn 10 / needs-fix 20 / hotspot distance 5.

`runtime_alpha=1.5` is **explicitly unmeasured** — it cannot be measured from one run at one size. It is pinned by `tests/test_pdb_preflight.py::test_bindcraft_runtime_curve_reproduces_its_one_measured_run`, whose name is itself the admission.

### 2.7 Output and score fields the product expects

- `shared/result_columns.py:26` — `"bindcraft": ["ipTM","pLDDT","RMSD","shape_complementarity","surface_hydrophobicity"]`; `:68` sort `("ipTM","desc")`.
- `templates/tools/bindcraft_results.html:7` — the same five column names, hard-coded in the partial.
- `shared/score_legends.py` — bindcraft legends at `:405` (ipTM, good 0.75 / excellent 0.85), `:414` (pLDDT), `:423` (RMSD), `:432` (shape_complementarity, 0.65 "antibody-grade"), `:486` (surface_hydrophobicity, reject > 0.35); the membership set at `:1412` is `{"rfdiffusion","pxdesign","bindcraft"}`.
- `tools/bindcraft/example/result.json` — top-level keys `candidate_count, candidates, next_steps, runtime_minutes, total_designs_requested`; per-candidate shape `{"pdb_key": "designs/design_001.pdb", "rank": 1, "scores": {...}}` with scores `Hotspot_RMSD, RMSD, SAP, Target_RMSD, i_pAE, ipTM, pLDDT, pTM, shape_complementarity`. Note `pLDDT: 0.81` — **0–1 scale** in the stored record.
- `tools/bindcraft/__init__.py` (152 lines) — `validate()` accepts only `preset == "pilot"`; chain ids ≤ 4 chars per token; `binder_length_min >= 50`; `binder_length_max <= 150`; `num_designs` 1–500 (default 4). `build_payload()` emits `{target_chain, hotspot_residues, parameters: {binder_length: {min,max}, num_designs}}`.
- `llm-proteinDesigner/docker/bindcraft/run_pipeline.py` (1639 lines) reads `{output_dir}/Accepted/*.pdb` and `{output_dir}/final_design_stats.csv`, and maps BC1's `Average_*` column names onto our score keys via `_METRIC_MAP`.

### 2.8 Customer-visible strings today

All in `tools/bindcraft/meta.py` (406 lines) unless noted:

- `PRESET_RUNTIME = {"pilot": {"typical_minutes": "30 to 45", "minutes": (30, 45)}}`
- preset label "Your target, ~30 min start to first results" (`tools/bindcraft/__init__.py`)
- `paper_citation="Pacesa et al., Nature 2025"`, `github_url="https://github.com/martinpacesa/BindCraft"` — note this points at upstream BindCraft, while both candidate builds clone `cytokineking/FreeBindCraft` (§2.1). Whether that is the right link is a product decision, but the two surfaces do not currently agree
- `seo_faq` — 3 Q&As, one of which says "roughly 20 to 60 minutes"
- `about` dict — `what_it_is`, `when_to_use`, `prerequisites`, `inputs`, `runtime_table`, `output_summary` (states ipTM 0.75 credible / 0.85 strong)
- `PILOT` card — "Trial run: 2 trajectories", `num_designs=2`
- `EXAMPLE` — job `1c4d5803`, 4ZQK chain A, hotspots 54/56/115, ipTM 0.75/0.76, pLDDT 81, SC 0.64/0.60, RMSD 3.04/2.96 Å, surface hydrophobicity 0.29/0.30, `cost_usd "2.04"`, runtime "20 minutes"
- `shared/tools_catalog.py:90` — "Make new binders for my target"
- `shared/tool_chooser.py:114-118` — `_Facts(haves={"target-structure"}, shapes={"mini-protein"})`, with the comparison one-liner quoted in the comment above it
- templates carrying bindcraft copy, from `grep -rlin bindcraft templates/` (case-insensitive, 21 files): `components/about_panel.html`, `components/candidate_table.html`, `components/preflight_panel.html`, `components/results_shell.html`, `email/job_complete.html`, `email/send_job_capped.html`, `email/send_overrun_warning.html`, `help/tool_guide.html`, `index.html`, `jobs_compare.html`, `job_detail.html`, `runs/detail.html`, `scout/feasibility.html`, `showcase.html`, `targets/launch.html`, `tools/bindcraft_form.html` (422 lines), `tools/bindcraft_results.html` (27 lines), `tools/comparison.html`, and additionally `tools/boltz2_form.html`, `tools/proteina_form.html` and `tools/rfdiffusion_form.html`, which mention BindCraft in comparison copy. A case-**sensitive** grep returns only 14 of these, which is why the case-insensitive form is the one to use when auditing this surface
- SEO surfaces: `docs/seo/AUDIT-2026-09-25.md`, `docs/MARKETING-SURFACE-REWRITE.md`

**Every string in this subsection is a proposal surface only.** Per the brief, the Website SEO lead (session `local_2ace4d20-8705-46f1-8854-8855905923d0`) owns the marketing surfaces and must be told before any of it ships.

**P2 hygiene item found while writing this.** Four hub comments cite files in the sibling repo with a bare path and no repo prefix, which is why the build trail reads as missing from tools-hub: `shared/score_legends.py:443` and `:452`, `templates/tools/bindcraft_form.html:401`, `tests/test_hotspot_picker_runtime.py:73`. `tools/boltz2/run_pipeline.py:201` shows the correct form, with the `llm-proteinDesigner/` prefix. Not a provenance problem and not fixed here — this pass changes no product code.

**Provenance warning on this whole inventory.** These are recorded as *the strings that exist today*, not as accurate descriptions. §2.1 establishes that no claim naming the underlying project — "we run FreeBindCraft", "we run BindCraft" — can currently be substantiated against the deployed service. Any string here that names the upstream project is therefore a string to be changed, not a model to copy. The Website SEO lead reports reaching the same conclusion for the marketing pages and removing project names from them until provenance is settled (their report, not verified here). Note the licence half of such a claim is separable and does hold: FreeBindCraft's upstream LICENSE is MIT with no hosting restriction — checked independently via `gh api repos/cytokineking/FreeBindCraft/license`: MIT text, Copyright (c) 2024 Martin Pacesa, zero matches for "host". Its header calls itself an unmodified copy of BindCraft's LICENSE; that self-description was not diffed against upstream, and GitHub classifies the file as `NOASSERTION` because of those header lines. It is the "we run it" half that is unevidenced.

**The class is wider than project names.** The Website SEO lead reports that grepping their own surfaces for both project names found the claim in four places in one blog post rather than the two lines they had boarded, one of them inside an FAQ answer that feeds a `FAQPage` schema block — the form most likely to be lifted verbatim into an AI answer. The same sweep surfaced a second unverified runtime claim, that interface scoring is handled by FreeSASA and OpenMM (their report; the pages were not read here). The general form is: **any claim about what the deployed bindcraft runtime is built from** rests on the same unevidenced footing, not just claims that name the project. **The hub could not corroborate that claim even in principle**, and this is the stronger form of the point — credit to the Website SEO lead, who re-ran my grep rather than adopting it and noticed my filter excluded `.txt`, `Dockerfile*`, `.toml` and `.yml`. Unfiltered on `origin/main`: `openmm` appears in exactly two files, `tools/af2/Dockerfile.modal` and `tools/iggm/Dockerfile.modal`, neither of them bindcraft; `freesasa` is Scout code, Scout docs, `requirements.txt` and `.github/workflows/pytest.yml`. The decisive fact is that **`tools/bindcraft/` holds no container spec at all** — `git ls-tree -r origin/main tools/bindcraft` returns `__init__.py`, `meta.py` and `example/result.json`, and nothing else. So the question is not whether a hub surface repeats the FreeSASA/OpenMM claim; it is that no build definition for this tool exists here to repeat it from.

**One correction to that framing**, because it is a class property rather than a bindcraft anomaly. Five tools have no Dockerfile in the hub — bindcraft, boltzgen, pxdesign, rfantibody and rfdiffusion — and those are exactly the five in `llm-proteinDesigner/.github/workflows/deploy-modal.yml:89`'s `ALL` list (§2.1). They are built and deployed from the sibling repo; the hub holds only their adapters. So "no build spec here" is expected, not suspicious. What it does mean is that **no provenance claim about bindcraft is checkable from this repository at all**, which is the same conclusion arrived at more cleanly. Incidentally, `tools/bindcraft/__init__.py:3` states "Modal app: ``ranomics-bindcraft-prod``" — a third, hub-side corroboration of §2.1's dispatch finding.

**Consequence for any fix, and it is the SEO lead's point, recorded here as a proposal for the surfaces they own.** Under the revised rule, the FreeSASA/OpenMM sentence and the project-name sentence are not two claims to fix but **one claim on two surfaces**: replacing PyRosetta with FreeSASA and OpenMM is FreeBindCraft's distinguishing premise, so the technical signature asserts the same thing as the name. Correcting the name while leaving the signature would restate the unevidenced claim in a form that reads *more* authoritative, not less. They hold or drop together, never one at a time.

### 2.9 The reliability record

`docs/qa/FAILED-RUNS-2026-09-30.md`, bindcraft rows:

That file defines its columns at line 39: "Gs means GPU-seconds used. Wall means seconds from start to finish." The two are not interchangeable, and for these rows the distinction decides what the numbers mean.

| job | date | outcome | GPU-s | wall s | cost |
|---|---|---|---|---|---|
| `cd7150c1` | 06-11 | no_progress_timeout | 5015 | 413271 | $4.37 |
| `763247f5` | 07-02 | safety_kill, billed | 5012 | 5013 | $3.63 absorbed |
| `7eae70ba` | 08-28 | "Failed to download input PDB: HTTP 400" | not recorded | 11346 | $13.11 |
| `c43329f3` | 09-22 | "timed out after 14400 s" | not recorded | 14403 | $12.00 |

Only the first two rows carry measured GPU-seconds; for the last two, only wall time was recorded. `cd7150c1`'s 413271 s of wall time against 5015 GPU-seconds is worth noticing on its own — that job sat for nearly five days.

`docs/qa/FAILED-RUNS-2026-09-30.md:100` attributes **$45.11** to three of these (`cd7150c1`, `763247f5`, `c43329f3`; `7eae70ba` is not among them), and the composition matters: $20.00 is refunded or absorbed customer price — 4.37 + 3.63 + 12.00 — and $25.11 is our Modal cost, which the refund does not recover. That Modal figure is an upper bound rather than a measurement: the same file's caveats at `:122` say "the Modal figures are upper bounds … wall time overstates GPU time on hung runs", and for one of the three (`c43329f3`) wall time is all there is. (That file also records a separate $11.66 caveat at `:123`, but it concerns `7eae70ba` and so reduces the wider OUR FAULT total, not the $45.11.) Note that `7eae70ba` ran 11346 seconds *failing to download its input* — a wrapper defect, not a BindCraft defect, and BC2 would not fix it.

**Caveat on every cite to this file:** `docs/qa/FAILED-RUNS-2026-09-30.md` is untracked in git at the time of writing, so a reader checking out this commit will not find it. It needs committing by whoever owns it before these cites resolve for anyone else.

---

## 3. What BindCraft2 changed upstream

Source: a clone at tag v1.0.3, HEAD `ce3150f8d1132a0c900d66ad8f6d85ec08c9ad17`, commit message "Updating scaffold definitions", dated 2026-09-26. The repository is <https://github.com/PacesaLab/BindCraft2> — a **new repository**, not a v2 tag on `martinpacesa/BindCraft`.

UNVERIFIED, recorded here only because it came from a GitHub API read during this pass that is not reproducible from the checkout: that the latest default-branch commit at clone time was `a8d0f200` (2026-09-29), and that the repository id is 1368271613. Re-check both before relying on them; neither affects any conclusion in this document.

Caution recorded for whoever picks this up: a `WebFetch` of the raw README returned "not stated" for GPU memory, runtime and licence. All three are stated in the repository. Clone it and read the files.

### 3.1 Dependencies and CUDA — better than BC1, and no torch

`pyproject.toml`: `requires-python = ">=3.12"`; runtime deps `jax>=0.11,<0.12`, `dm-haiku`, `optax`, `biotite`, `matplotlib`, `numpy`, `scipy`, `ml_collections`, `absl-py`. Extras `cuda12` / `cuda13` (`jax[cudaXX]` + `cuequivariance-jax` + `cuequivariance-ops-cuXX`) and `rocm`.

**There is no torch dependency at all.** Our most common image-build failure — an unpinned pip pulling torch past the base image, which is what killed the Boltz-2 production image — cannot occur here, because the stack is JAX-only and JAX's CUDA wheels are self-contained. The upper bound `jax<0.12` is a real pin, unlike BC1's `install_bindcraft.sh`.

`containers/Dockerfile`: `FROM ubuntu:24.04`; `pip install -e "/opt/bindcraft[cuda13]"`; `ENV NVIDIA_VISIBLE_DEVICES=all NVIDIA_DRIVER_CAPABILITIES=compute,utility`; `ld.so.conf` entries from `nvidia/*/lib` then `ldconfig -p | grep -q libcupti`; `ARG ALPHAFOLD_PARAMETERS=download-on-first-campaign` (alternative `bake`); build-time verification runs `import jax`, `import bindcraft.proteinmpnn`, `bindcraft --help` and `selfcheck cuda13 --shipped-only`; `ENTRYPOINT ["bindcraft"]`.

The install is **editable and must stay editable** — `settings/` and `scaffolds/` are read from the repo root, not from site-packages.

`install.sh` auto-detects the accelerator from `nvidia-smi`; CUDA 13 requires compute capability ≥ 7.5; it refuses a CPU install outright ("there is no CPU installation… exit 2"). AF2 parameters ("5.3 GB once", `docs/source/installation.md:91`) are fetched separately by `bindcraft fetch-weights`; ProteinMPNN weights ship in package data.

### 3.2 GPU memory and worker packing

`docs/source/installation.md`, lines 156-220:

- "A whole-cycle campaign runs up to **seven workers per card**."
- Per-worker budget (`:160`): `2.0 × (3.4 GB + 38 kB × N²)` for a padded complex of N residues, with 4 GB of the card left as headroom and 4 GB of *host* RAM per worker.
- Per-worker sizes (`:193`): "7.1 GB at 64 residues, 8.0 at 128, 11.8 at 256, 18.0 at 384 and 26.7 at 512".
- `max_trajectories` must exceed the worker count (`:160`).
- Overrides: `BINDCRAFT_WORKERS_PER_GPU`, `BINDCRAFT_MAX_WORKERS_PER_GPU` (8), `BINDCRAFT_DESIGN_WORKERS`, `BINDCRAFT_GPU_IDS`, `BINDCRAFT_WORKER_LAUNCH_STAGGER`; settings `auto_multi_gpu`, `subbatch_size`, `length_bucket_size` (32), `compile_next_length`, `attention_backend`, `use_cueq`.
- Cluster guidance (`:212`): the Slurm script "sizes cores and memory to it at 4 cores and 24 GB per GPU". Its declared defaults (`:219`) are "one GPU, 8 cores, 48 GB and 24 hours".

Applying the formula to our current envelope (`hard_cap_combined_aa=600`): `2.0 × (3.4 + 38e-6 × 600²)` = **34.2 GB per worker**. An A100-80GB with 4 GB headroom fits **two** workers at our maximum allowed size, or four at N ≈ 400. Our existing GPU class is adequate; the parallelism is what would have to be tuned, and `BINDCRAFT_WORKERS_PER_GPU` is the knob.

### 3.3 Runtime

The only timing figure stated anywhere in the repository (`docs/source/installation.md`, same section): **"On a GH200 a forty-trajectory campaign took 3620 s at one worker and 2021 s at seven."** That is 90.5 s per trajectory serial, 50.5 s per trajectory wall-clock at seven workers — a 1.79× speedup for 7× the workers.

- Whether BC2 is faster or slower than BC1 **per design**: **UNKNOWN.** A grep across `README.md` and `docs/source/*.md` for "BindCraft 1", "BC1", "previous version", "faster" and "speed" returned nothing comparative.
- Whether runtime scales linearly with design count: **it does not, and worse, it is not bounded by design count at all.** See 3.4.
- The GH200 figure transferred to an A100-80GB: **UNVERIFIED.** Different card, different memory bandwidth, different worker packing.

### 3.4 The cost contract changed, and this is the second big risk

BC1 runs a fixed number of trajectories and returns what passes. BC2 does not.

- `README.md:34`: "BC2 continues until it reaches that number, with no limit on design attempts."
- `settings/core/reference.json:175`: `"max_trajectories": null`.
- `docs/source/reference.md:118`: "`max_trajectories` | Unset | Optional attempt limit. Leave unset to keep working toward the requested count."
- `docs/source/design-guide.md:79`: "How many attempts are allowed, note that **a difficult target may need thousands of attempts per design**."

So the default BC2 contract is *unbounded*: run until N designs are accepted, however long that takes. Dropped onto a fixed Modal container ceiling, that is precisely the failure mode behind the $45.11 of BC1 losses in §2.9 — of which $20.00 was refunded or absorbed customer price and $25.11 our own Modal cost (an upper bound, not a measurement; see 2.9) — a run that burns the whole ceiling and returns nothing. **Any hub integration must set `max_trajectories` explicitly, derive it from the container ceiling, and treat "budget exhausted" as a first-class customer-visible outcome rather than a timeout.** BC2 does emit a distinct message for it (`bindcraft/campaign.py:230-231`, `campaign_budget_exhausted(...)`) — but only if the adapter reads it. Whether BC1 has any equivalent signal is UNVERIFIED; `run_pipeline.py` was not audited for one in this pass.

### 3.5 Output layout — a full rewrite of the result reader

`bindcraft/campaign_output.py` — the stage and table constants at `:30-45` and the two column tuples at `:88-89`, reproduced together here:

```python
TRAJECTORY_STAGE, REFOLD_STAGE, RANK_STAGE = '1_Trajectories', '2_Refolded', '3_Ranked'
STAGE_TABLE_NAMES = {TRAJECTORY_STAGE:'!_Trajectories.csv', REFOLD_STAGE:'!_Refolded.csv', RANK_STAGE:'!_Ranked.csv'}
RANKING_METRIC = 'i_pDAE'
STRUCTURE_SUFFIXES = ('.cif', '.pdb', '.mmcif', '.ent')
DESIGN_IDENTITY_COLUMNS = ('rank','trajectory','design','length','outcome')
LEADING_CONFIDENCE_COLUMNS = (RANKING_METRIC,'i_pTM','pLDDT','pTM','i_pAE','Unbound_Binder_pLDDT','Target_pLDDT')
```

Every one of these breaks `run_pipeline.py`:

| BC1 (what we read) | BC2 |
|---|---|
| `Accepted/*.pdb` | `3_Ranked/<design>_seq<n>[_<target>].cif` |
| `final_design_stats.csv` | `3_Ranked/!_Ranked.csv` (plus `summary.csv`, `campaign_metadata.json`) |
| `Average_i_pTM`, `Average_pLDDT`, … | `i_pTM`, `pLDDT`, … — the `Average_` prefix is gone |
| ranked by ipTM | ranked by **`i_pDAE`** |
| structures are `.pdb` | structures are **`.cif`** |

Also emitted: `.campaign_state.json`, `workers/worker_<NN>_gpu_<id>.log`. Legacy flat layouts (`accepted.csv`, `ranked.csv`) are still *read* by BC2 but not written by it.

`docs/source/outputs.md` states the scale convention explicitly: CSV `pLDDT` / `i_pTM` / `pTM` are **0–1**, while the structure B-factor column carries per-residue pLDDT on **0–100**. Our stored record is already 0–1 (`tools/bindcraft/example/result.json`), so the CSV side matches — but the `.cif` B-factor side is the same 0–1-versus-0–100 split that has bitten this codebase twice before.

**`shape_complementarity` has no source in BC2.** A grep for `Shape_Complementarity` over `bindcraft/*.py` in the clone returned zero hits, and `bindcraft/rank.py::MODALITY_METRICS` does not list it. Our results column, our score legend at `shared/score_legends.py:432` with its "0.65 antibody-grade" threshold, and the column in the results partial would all have to be removed or sourced elsewhere. `Surface_Hydrophobicity` does survive, but it sits in `ALWAYS_SUPPRESSED_METRICS` (`bindcraft/campaign_log.py:14`), so it is computed and hidden by default.

Downstream consequences already known to this codebase: a `.cif` key flows into the download label (which follows the storage key, not the MIME type) and into the export writers; and the preflight panel has a history of inverting `.cif` handling.

### 3.6 Input contract changed

`examples/pdl1_custom_target.json`:

```json
{"campaign_name": "...", "project_folder": "...", "modality": "binder",
 "targets": [{"name": "...", "target_path": "...", "chains": "A", "hotspots": "A54,A56,A66,A115"}],
 "binder_lengths": [60, 100], "number_of_final_designs": 10, "max_trajectories": 2000}
```

Changes against the settings dict `run_pipeline.py` builds today:

- `targets` is now a **list of objects**; `starting_pdb` / `chains` / `target_hotspot_residues` become per-target fields inside it
- `lengths` → `binder_lengths`
- new `modality` (binder, VHH and others — `bindcraft/rank.py::MODALITY_METRICS`)
- new `max_trajectories`, `binder_scaffold`, `mutate_positions`
- `binder_name` / `design_path` are replaced by `campaign_name` / `project_folder`
- hotspots are now chain-qualified (`A54`), which is strictly better than BC1's bare residue numbers — bare numbers have silently steered the wrong protomer before

The default binder length range for modality `binder` is **60–180**; our `validate()` enforces 50–150. The ranges overlap but do not match.

New acceptance filters, `settings/core/reference.json`: `Unbound_Binder_pLDDT >= 0.8`, `pTM >= 0.55`, `i_pTM >= 0.7`, `i_pAE <= 0.35`, `Backbone_Clashes <= 0`, `Interface_Residues >= 7`, `Off_Paratope_Contact_Fraction` null.

### 3.7 New capability (the upside, for completeness)

Per `README.md`: de novo miniproteins, **scaffolded binders, cyclic peptides, multistate design**, a VHH modality with a `--humanize` property, and named presets carrying both design settings and acceptance filters. A parameter sweep mode exists (`docs/source/reference.md:429-447`). None of this is available in BC1. If the licence clears, this — not speed — is the reason to want BC2.

---

## 4. Replace versus add alongside

**If and only if the licence clears**, the recommendation is **add alongside as a new slug, never replace in place.** Reasons:

1. Replacing in place invalidates the single measured runtime anchor (job `1c4d5803`, 1170 GPU-s), both container ceiling rows, the `expected_gpu_seconds=3600` spec, the whole `SizeEnvelope`, and every bindcraft validation row in `docs/VALIDATION-LOG.md` — with nothing measured to replace them, so the tool would be live on constants known to be wrong. This project has already shipped runtime constants that were stale by 3× and paid for it.
2. The score set is not a superset. `shape_complementarity` disappears. A customer comparing an old run to a new one under the same tool name would see a column vanish and the ranking metric change from ipTM to `i_pDAE`. Two distinct tools is the honest presentation.
3. The **Naming Restriction** (`LICENSE:66-79`) bars using the name "BindCraft2" for a Hosted Service in a way that implies equivalent functionality or official association, while permitting accurate description. Presenting a BC2-backed tool as a new version of our existing "BindCraft" tool sits on the wrong side of that line; presenting it as a separate tool, accurately described, does not.
4. BC1's own headline failures (`7eae70ba`, 11346 s of wall time failing to download its input; the pilot/full ceiling anomaly in §2.3) are **wrapper** defects. They are cheaper to fix in place than to migrate away from, and they would reappear in a BC2 wrapper built on the same scaffolding. Fix those regardless of what happens to BC2.

---

## 5. Cost to validate — cheapest first

Raw Modal A100-80GB cost, `shared/wallet_estimates.py:74` = $0.001028/s = **$3.70/GPU-hour**. Figures below are our raw spend, not customer price. **Every rung is a proposal; Leo approves GPU spend per job. Nothing here has been launched.**

| Rung | What it answers | GPU | Cost | Basis |
|---|---|---|---|---|
| **0. Licence** | May we host it at all? | none | $0 | §1. A written answer from Pacesa Lab / UZH TTO. **Blocking for every BC2 rung below.** Rung 0b is the one exception: it is about today's BindCraft 1 and does not wait on this. |
| **0b. Resolve provenance** | Which image does the hub's bindcraft tool actually run? | none | $0 | §2.1, UNKNOWN 8. The dispatch path is already settled from code — every hub submission is Modal, to `ranomics-bindcraft-prod` (`gpu/modal_client.py:474-475`, `:379-381`). Two reads finish it: confirm that against a production job row's `modal_function_call_id` (`shared/jobs.py:620`), then inspect the deployed app for the image it holds. Neither is a launch and neither costs anything. Cheap, and it unblocks more than it looks: see the multi-chain note below. |
| **1. CPU-only image build probe** | Does the image build, and does JAX import? | none | $0 (see the RunPod caveat below) | `containers/Dockerfile` already runs `import jax`, `import bindcraft.proteinmpnn`, `bindcraft --help` and `selfcheck cuda13 --shipped-only` at build time. Build on a CPU builder with `ALPHAFOLD_PARAMETERS=download-on-first-campaign` so the 5.3 GB of params stay out of the layer. `install.sh` refuses CPU, but the container path does not use `install.sh`. |
| **2. GPU smoke, no design** | Does CUDA actually initialise on our A100, and can we populate a weights Volume? | 1 × A100-80GB, ~10 min | **~$0.62** | `selfcheck cuda13` plus `bindcraft fetch-weights` into a Modal Volume. Size the Volume from the real download, not the doc figure — a previous weights Volume was off by ~7×. |
| **3. One minimal campaign** | Does a real campaign complete end to end, and what does the output tree actually look like? | 1 × A100-80GB, ≤30 min | **~$1.85** | Shipped `hPDL1` target, `binder_lengths [60,60]`, `number_of_final_designs 1`, `max_trajectories 5`, one worker. 5 trajectories × 90.5 s (the GH200 figure, `installation.md`) ≈ 450 s plus model load; budget 1800 s at 2× because the GH200 → A100 transfer is UNVERIFIED. Deliverable: the real `3_Ranked/!_Ranked.csv` header, so the column census stops being a grep of upstream source. |
| **4. Parity pilot** | How does BC2 compare to the one BC1 run we have numbers for? | 1 × A100-80GB, ≤2 h | **~$7.40** | The same input as job `1c4d5803`: 4ZQK chain A, hotspots 54/56/115, 2 designs. `max_trajectories` set so the run cannot exceed the ceiling. Gives the first honest runtime anchor and a like-for-like score comparison. |
| **5. Size/scaling sweep** | What is `runtime_alpha` really? | 3 × A100-80GB, ≤2 h each | **~$22.20** | Three target sizes across the envelope (≈100 / 300 / 500 aa). The only way to replace the unmeasured `runtime_alpha=1.5`. Defer until the tool is committed to — this rung buys accuracy, not a go/no-go. |

**Ladder through rung 4: ≈ $10 of GPU.** Through rung 5: ≈ $32. The go/no-go decision is fully answered at rung 4.

**Rung 0b is leverage on a paid question.** `shared/pdb_preflight_rules.py:178-196` records that `multi_chain_container_ready=False` for bindcraft is set *because of* the RunPod v7 belief: rfdiffusion, boltzgen, pxdesign and rfantibody were each gated by importing llm-pd's `pipeline_normalize` and executing it against a clean two-chain PDB, and bindcraft alone could not be, "ships as a separate prebuilt image … that cannot be inspected from here, so it is UNVERIFIED rather than known-good" (`:185-187`). Read those comments precisely: they state v7 as fact ("ships as a separate prebuilt image (``kendrew-bindcraft:v7``)", `:185`), and the UNVERIFIED label they carry attaches to *chain handling*, not to the image identity. They are careful about the thing they were written to be careful about, and they are not a defect — but they are not a hedge on v7, and §2.1 does not inherit one from them. But the consequence is that **we withhold multi-chain targets from bindcraft customers on a footing that rests entirely on unresolved provenance.** **Correcting the mechanism**, because it is not "run the same check": bindcraft does not use llm-pd's normalizer at all. `shared/pdb_preflight_rules.py:586-587` says so, and `llm-proteinDesigner/docker/bindcraft/run_pipeline.py` confirms it — no `pipeline_normalize` import, its own `_normalize_target_chains` at `:646`, and `"chains": chain` passed to BindCraft's native setting at `:430`. So the other four tools' check does not transfer.

What transfers is the *inspectability*, and that is still worth having. If the Modal image is the runtime, the wrapper's chain handling is a readable file in a checkout rather than an opaque tag — and reading it is suggestive: `_normalize_target_chains` already accepts `"A,B"`, `"A B"` and `"A, B"` and emits the comma form BindCraft's own settings key wants (`:647-657`, citing `docs/MULTI-CHAIN-TARGETS.md`). That is the wrapper half only. What BindCraft itself does with a multi-chain `chains` value is inside the container and is **not** settled by reading the wrapper, so this is a cheaper path to the answer, not the answer. bindcraft has no smoke tier (`shared/pdb_preflight_rules.py:575`), so the alternative route is a full paid pilot. A $0 rung that narrows a paid question should run first regardless of whether BC2 goes anywhere.

**Caveat on the whole ladder.** Rungs 1 to 3 are costed as a Modal image build plus Modal GPU time. That is the right shape: the hub has no RunPod dispatch path (§2.1), so a BC2 tool here would be a Modal image and wrapper. The residual risk is the baseline rather than the cost model — rung 4 compares BC2 against a BC1 image whose contents are UNKNOWN 8. The GPU-second *durations* at rungs 2 to 5 are provider-independent; the dollar figures are not — they are Modal's A100-80GB rate (`shared/wallet_estimates.py:74`), and no RunPod rate was checked in this pass.

---

## 6. Work plan, with a gate between each phase

Phase 0 is not engineering, and it gates everything.

**Phase 0 — Licence.** Owner: Leo / partnerships. Output: a written answer from Pacesa Lab / UZH. *Gate: a commercial hosting licence exists in writing. If it does not, stop; the rest of this document is void.*

**Phase 1 — Image.** A new `llm-proteinDesigner/docker/bindcraft2/Dockerfile.modal` derived from upstream `containers/Dockerfile`, **pinned to a commit or tag** (do not repeat the `--depth 1 master` mistake of §2.1). Weights on a Modal Volume, not baked. Run rungs 1 and 2. *Gate: the image builds reproducibly and `selfcheck` passes on a real A100.*

**Phase 2 — Modal app and wrapper.** A new `infrastructure/modal/bindcraft2_app.py` and a new `run_pipeline.py`. The pipeline script is a **rewrite, not an edit** — §3.5 and §3.6 change the input dict, the output tree, the file format and every column name. It must set `max_trajectories` derived from the container ceiling, parse `3_Ranked/!_Ranked.csv`, collect `.cif` structures, and surface `campaign_budget_exhausted` as a distinct outcome. Run rungs 3 and 4. *Gate: a parity pilot returns candidates with a known runtime and a known score set.*

**Phase 2b — Revisit `multi_chain_container_ready`.** Not follow-up work; part of this plan, and it applies on either recommendation. The gate's justification is the uninspectable v7 image (§5, rung 0b). **Replacing** the runtime removes that justification outright — the new image is ours, pinned and inspectable, so the gate must be re-derived rather than inherited. **Adding alongside** is worse, because there would then be two bindcraft runtimes sharing one gate flag, and a flag that means "one of these two cannot be inspected" is not a flag anyone can reason about. Either way the flag must be re-evaluated against whatever actually ships, and BC2's `targets` list and chain-qualified hotspots (§3.6) suggest the answer may change in the customer's favour. Touching `shared/pdb_preflight_rules.py`, which is also the P2 citation-hygiene surface noted in §2.8. *Gate: the flag's value is derived from an executed check, not inherited from a belief about an image.*

**Phase 3 — Hub plumbing.** In dependency order, all under a new `bindcraft2` slug:

- `tools/bindcraft2/__init__.py` — adapter, `validate()`, `build_payload()`; binder length range reconciled against BC2's 60–180 default
- `gpu/modal_client.py` — new ceiling rows, measured at rung 4 rather than guessed. **Fix the pilot/full selection anomaly of §2.3 for bindcraft first, since bindcraft2 would inherit it.**
- `shared/wallet_estimates.py` — a new `ToolSpec` from the rung-4 measurement; `base_hard_cap_usd` and `absolute_cap_usd` set against the unbounded-trajectory risk of §3.4
- `shared/pdb_preflight_rules.py` — a new `_BINDCRAFT2` envelope; the GPU memory cap derived from the `2.0 × (3.4 GB + 38 kB × N²)` formula rather than from literature; `runtime_alpha` left explicitly unmeasured and labelled so until rung 5
- `shared/result_columns.py` and `templates/tools/bindcraft2_results.html` — a new column set **without** `shape_complementarity`, sorted by `i_pDAE`
- `shared/score_legends.py` — new legends for `i_pDAE`, `Unbound_Binder_pLDDT`, `Target_pLDDT` and `Interface_Residues`; thresholds taken from `settings/core/reference.json` (§3.6) and cited there
- `shared/exports.py` and the download-label path — `.cif` structures, not `.pdb`
- `tools/bindcraft2/example/result.json` — generated from the rung-4 run, never hand-written

*Gate: a full QC round on the delta (`review-code` plus `review-claims`), and a real run through the live form.*

**Phase 4 — Copy and catalog. Customer-visible; the SEO lead owns it.** `tools/bindcraft2/meta.py` (runtime labels, preset labels, `seo_faq`, `about`, `output_summary` thresholds, `EXAMPLE`), `shared/tools_catalog.py`, `shared/tool_chooser.py`, `templates/tools/comparison.html`, `templates/help/tool_guide.html`, `templates/index.html`, `templates/showcase.html`, and the email templates listed in §2.8. Plus the positioning question nobody has answered yet: **what does a customer choose between bindcraft and bindcraft2, in one sentence?** *Gate: SEO lead sign-off. Nothing in this phase ships without it.*

Every string named in §2.8 and in Phase 4 is **customer-visible**.

---

## 7. UNKNOWNs and UNVERIFIEDs that must be resolved before phase 1

1. **Will UZH grant a commercial hosting licence, and on what terms?** UNKNOWN. Blocking; everything else is contingent on it.
2. **BC2 runtime per design on an A100-80GB.** UNKNOWN. The only upstream figure is a GH200 40-trajectory campaign. Resolved by rungs 3 and 4.
3. **BC2 versus BC1 speed.** UNKNOWN. Upstream makes no comparative claim (grepped, §3.3). Resolved by rung 4 against job `1c4d5803`.
4. **What the real `3_Ranked/!_Ranked.csv` header contains.** Currently read from upstream *source*, not from an actual run. Resolved by rung 3.
5. **Trajectories needed per accepted design on a representative target.** UNKNOWN, and it is the cost driver — upstream says a difficult target "may need thousands". Without a number, `max_trajectories` and the wallet cap cannot be set honestly. Partly resolved by rung 4; properly only by several runs.
6. **What replaces `shape_complementarity`,** if anything. A product decision, not a measurement.
7. **Job `c43329f3` ran 14403 s of wall time under a `pilot` booking against a 7200 s pilot ceiling.** UNVERIFIED how the row was selected, and UNVERIFIED whether the row held 7200 on 2026-09-22. Overlaps `task_9a3538be`. Must be understood before any new ceiling row is added.
8. **Which image the deployed `ranomics-bindcraft-prod` app holds.** UNKNOWN (§2.1). **This is the largest single unknown in this scope**, because it decides what a BC2 upgrade *is* and what baseline any comparison runs against. The *dispatch* half of this question is no longer open: the hub submits every job through Modal and has no RunPod code path (§2.1, cited), so Phase 1 and the rungs below are correctly costed as Modal work. What remains is deploy state — which image that app currently serves, and whether it was built from `Dockerfile.modal`. Two free reads finish it (§2.1). Do not settle it by counting comments; the comments are themselves part of what is in doubt. Separately, what source the pushed `kendrew-bindcraft:v7` tag contains is also UNKNOWN — neither Dockerfile pins — and is recoverable from the registry config blob.
9. **The actual size of the AF2 parameter download.** Stated as 5.3 GB; a previous weights Volume was off by ~7× against its doc figure. Measure at rung 2.
10. **Whether `multi_chain_container_ready` would be true for BC2 — and whether it should still be `False` for BC1.** UNKNOWN, and the two halves are one question. The BC1 flag is `False` on a conservative footing that rests on the uninspectable v7 image (`shared/pdb_preflight_rules.py:178-196`), so resolving UNKNOWN 8 makes the *wrapper* half readable (§5 rung 0b) — it does not on its own retire the flag, because what BindCraft does with a multi-chain value stays inside the container. See Phase 2b. For BC2, its `targets` list and chain-qualified hotspots suggest better multi-chain support, but nothing is measured. This is a live customer-facing restriction, not a bookkeeping item.
11. **Whether the licence's design-outputs carve-out (`LICENSE:60-64`) covers the Sprint and custom-campaign business.** UNKNOWN, and it is a lawyer's question, not an engineer's. It is the difference between "BC2 is unusable to us" and "BC2 is usable for everything except the self-serve tool", so it should go to counsel at the same time as the licensing enquiry.

---

## 8. What this pass did not do

No product code was changed. No GPU job was launched. No customer-visible string was edited. The BindCraft2 clone lives in the session scratchpad and is not committed.
