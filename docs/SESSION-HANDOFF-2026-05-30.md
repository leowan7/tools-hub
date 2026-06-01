# tools-hub + llm-pd - handoff 2026-05-30 (preview tier dropped, RFantibody scFv stripped, BoltzGen protocol selector live)

Picked up from the 2026-05-29 handoff. Two-pass cleanup of the tool catalog UX motivated by the user notes "nobody will pay for previews" and "site is not clear on what can design a minibinder versus VHH". Pass 1 ripped the residual Preview tier and corrected a misleading scFv claim on RFantibody. Pass 2 prep wired BoltzGen's full Boltz-2 protocol set into the form so it can earn a Dual-capabilities bucket in the upcoming catalog regrouping. All five composite tools now ship a single `pilot` preset on prod; BoltzGen accepts protein / nanobody / antibody / peptide design protocols end-to-end. Both QC layers passed (browser form check + GHA all green for the llm-pd push).

## Done this session

- **Pass 1 - preview tier drop + RFantibody scFv strip** ([tools-hub `125021e`](https://github.com/leowan7/tools-hub/commit/125021e) + [llm-pd `c9ff634`](https://github.com/leowan7/llm-proteinDesigner/commit/c9ff634)). 23 files / +115 -611 on tools-hub, 3 files / +8 -10 on llm-pd. All 5 composites (rfdiffusion, rfantibody, bindcraft, boltzgen, pxdesign) collapse to a single `pilot` preset. Forms swap the preset selector for a hidden `preset=pilot` field and unwrap the pilot-fields visibility gate (always visible now). `shared/wallet_estimates.py` drops the per-tier `tier_gpu_seconds` overrides; `gpu/modal_client.py` PRESET_CAPS drops every `mini_pilot` row. Composite adapters set `requires_pdb=True`. RFantibody scFv removed end to end: form selector deleted, scaffold field dropped from validate, meta `comparison_one_liner` / `about` rewritten VHH only, adapter label changed to "RFantibody, VHH (nanobody) design", Scout handoff dropdown updated. On llm-pd, `FRAMEWORKS["scFv"]` removed from `docker/rfantibody/run_pipeline.py` and both Dockerfiles drop the scFv framework existence check. Tests updated, full suite 629 passed / 6 skipped.

- **Why scFv was stripped, not fixed.** Subagent research showed RFantibody upstream DOES support scFv (ships `hu-4D5-8_Fv.pdb` example), but our wrapper's ProteinMPNN stage hardcodes `--loops H1,H2,H3` (heavy-chain CDRs only). Selecting scFv was silently degrading to a VHH-style run with the wrong framework PDB loaded. Either rip the claim or rewire the ProteinMPNN call to switch loops on scaffold; chose the rip for this pass.

- **Pass 2 prep - BoltzGen protocol selector** ([tools-hub `6ce2f44`](https://github.com/leowan7/tools-hub/commit/6ce2f44) + [llm-pd `6879ec7`](https://github.com/leowan7/llm-proteinDesigner/commit/6879ec7)). Boltz-2 `--protocol` was already flowing through the wrapper to the CLI; the lock was purely client-side (validate accepted only `protein-anything`; form `<select>` only listed it). Surfaced four of the five upstream protocols: protein-anything (default), nanobody-anything (VHH), antibody-anything, peptide-anything. `protein-small_molecule` held back pending a ligand input field. `tools/boltzgen/__init__.py` adds `ALLOWED_PROTOCOLS` frozenset; validate captures + checks `protocol`; build_payload forwards it. Binder-length floor lowered 20 to 10 to fit peptides. Form helper copy lists typical lengths per protocol. Meta `comparison_one_liner` + `about.what_it_is` + `about.when_to_use` + `about.inputs` rewritten to describe all four protocols. On llm-pd, `docker/boltzgen/run_pipeline.py` adds a defensive preflight: unknown `--protocol` values fail fast via the webhook before GPU spin-up (saves ~$0.50+ on typos), mirroring the upstream allow-set.

- **QC layers, both passed.**
  - **QC1 - form render** (browser, Claude in Chrome). Live at `tools.ranomics.com/tools/boltzgen` shows all 4 protocol options in the selector, helper text renders with typical-length ranges, About panel reflects the broader scope, no console errors, no 500. Wallet estimate $8.74 + balance render correctly.
  - **QC2 - llm-pd CI for `6879ec7`**. Test Suite + Build and Push Boltzgen Docker + Deploy Modal apps all completed success. `ranomics-boltzgen-prod` reloaded with the preflight.

## Next steps

- **Pass 2 - catalog regrouping** (the original ask). Group `/tools` into four buckets based on what each tool actually designs, with one-liner subtitles pulled from each `meta.py` `comparison_one_liner`:
  - **De novo minibinders**: rfdiffusion, bindcraft, pxdesign
  - **Antibodies (VHH)**: rfantibody (VHH only per our wrapper)
  - **Dual capabilities (minibinder + antibody scaffolds)**: boltzgen (now that protocol is exposed)
  - **Sequence on a backbone**: mpnn
  - **Structure prediction**: af2, colabfold, esmfold
  - Edits land in the `/tools` catalog template (verify path; `templates/index.html` or similar). Task #12 in TaskList.

- **Cosmetic - runtime_table renders raw `&ndash;` entity.** Surfaced during QC1 on the BoltzGen About panel ("15&ndash;60 min" literal instead of rendered en dash). `about_panel.html` macro escapes `runtime_table.typical`. Pre-existing across rfdiffusion / rfantibody / boltzgen / pxdesign metas (bindcraft is already plain "45"). Cheap fix: rewrite the affected entries to "15 to 60 min" plain prose (also matches the no-dashes rule in user memory). Held pending Leo go-ahead.

- **Live nanobody / antibody / peptide pilot proof** (deferred, ~$5 to $15 GPU each). Upstream BoltzGen README confirms the 4 protocol strings work as CLI flags, but our YAML-spec builder might need tweaks for non-protein-anything paths (multi-chain antibody outputs, very short peptide lengths). Lowest-risk live test is nanobody-anything (same architecture as protein-anything). Antibody-anything and peptide-anything are higher risk because of different output shapes and would benefit from separate runs.

## Notes (optional)

- **Working tree carries pre-existing drift, not staged this session.** tools-hub: `README.md` + `docs/PRODUCT-PLAN.md` (developability + library_planner status flips from "Coming soon" to "Live") + `supabase/.temp/cli-latest` (v2.90.0 to v2.101.0). llm-pd: `.gitignore`, `.planning/phases/11-deployment/11-03-PLAN.md`, `docs/blocker-rfdiffusion.md`, untracked `CLAUDE.md` and `.planning/phases/11-deployment/11-03-PASTE-MANIFEST.md`. Leo can commit when ready.

- **Modal redeploy is fast for run_pipeline-only changes.** Both llm-pd pushes this session (`c9ff634` rfantibody scFv strip and `6879ec7` boltzgen preflight) only touched `run_pipeline.py`, not the Dockerfile. Modal `Image.from_dockerfile + add_local_file` rebuilds only the lightweight last layer; the Deploy Modal apps workflow ran in parallel with the Docker rebuilds and finished in seconds, not the typical 10 to 15 min. The GHCR Docker rebuild is for image-inspection use, not gating Modal.

- **RFantibody scFv is recoverable later.** If we want to add scFv design back: (1) `docker/rfantibody/run_pipeline.py` switch `--loops` to `H1,H2,H3,L1,L2,L3` when scaffold is scFv; (2) re-add `"scFv"` to FRAMEWORKS; (3) expose scaffold selector on the form with L-chain CDR length inputs. The upstream RFantibody repo's `hu-4D5-8_Fv.pdb` example is still bundled in the image.

- **Repo state at handoff.** tools-hub main: `6ce2f44`. llm-pd master: `6879ec7`. ranomics-website-2026 main: `73d9aed` (not touched this session). Working from the ranomics-website-2026 worktree at `C:\Users\lab\Documents\Claude_projects\.claude\worktrees\sad-easley-1e921f` (branch `claude/sad-easley-1e921f`); all the actual file edits landed in the sibling tools-hub + llm-proteinDesigner repos.
