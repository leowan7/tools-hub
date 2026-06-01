# tools-hub - handoff 2026-06-01 (catalog regrouped into design-intent buckets)

Picked up the Pass 2 task left open in the 2026-05-30 handoff: regroup `/tools` and the homepage tile grid so each section reflects what each tool actually designs, not a flat "Design binders" bucket. Pass 2 is now done. The earlier session left BoltzGen's protocol selector live as the prerequisite for the new "Dual capabilities" bucket; this session wired the catalog to it. Visual QC passed at 1440x900.

## Done this session

- **Pass 2 - catalog regrouping.** `app.py` `_TOOL_CATEGORIES` remapped from one "Design binders" bucket to four scaffold-class buckets, plus a renamed "Structure prediction". Both `category_order` tuples (`/` index handler + `/tools` comparison handler) updated to the new workflow-aligned order: Scope → De novo minibinders → Antibodies (VHH) → Dual capabilities (minibinder + antibody scaffolds) → Sequence on a backbone → Structure prediction → Check developability → Other. Catalog tile subtitles now pull `meta.py` `comparison_one_liner` ("Pick X when...") with `tagline` as a safety fallback so the section framing and the tile body tell the same story. 3 files / +31 -23 (app.py + templates/index.html + templates/tools/comparison.html).

- **Bucket-to-tool mapping locked.**
  - De novo minibinders: rfdiffusion, bindcraft, pxdesign
  - Antibodies (VHH): rfantibody
  - Dual capabilities (minibinder + antibody scaffolds): boltzgen
  - Sequence on a backbone: mpnn
  - Structure prediction: af2, colabfold, esmfold
  - (Scope the target and Check developability buckets unchanged.)

- **Visual QC at 1440x900** (headless via gstack browse against a local Flask on :5099 with all FLAG_TOOL_* forced on). All 7 sections render in the right order on both `/` and `/tools`. The longest header label, "DUAL CAPABILITIES (MINIBINDER + ANTIBODY SCAFFOLDS)", fits on a single line at desktop. Screenshots archived under `%TEMP%\catalog-qc\` (home-tools-section.png + home-fold.png + tools-catalog-section.png + tools-top.png).

## Next steps

- **Cosmetic - `runtime_table` raw `&ndash;` entity** still pending Leo go-ahead. Affects rfdiffusion / rfantibody / boltzgen / pxdesign meta `runtime_table.typical` ("15&ndash;60 min" literal). Cheap rewrite to plain "15 to 60 min" prose. Carried forward from 2026-05-30 handoff.

- **Live nanobody / antibody / peptide pilot proof** for BoltzGen's new protocols. Deferred from 2026-05-30. Nanobody-anything is the lowest-risk first run (~$5 to $15 GPU).

- **Mobile pass on long bucket label.** "Dual capabilities (minibinder + antibody scaffolds)" was verified at 1440px desktop only. The CSS uppercases + letter-spaces section titles; at narrow viewports it may wrap. Worth eyeballing on a phone before declaring the regroup final. If it wraps, "Dual capabilities" alone or "Dual scaffolds" is a drop-in shorter label.

## Notes (optional)

- **Card height irregularity within a row.** The `comparison_one_liner` strings vary in length (rfdiffusion's is 5 sentences with cross-refs, bindcraft's is 1). On the "De novo minibinders" row this produces visibly uneven card heights. Not broken; just irregular. A `min-height` on `.catalog-card` or a uniform sentence-length pass on meta files would tighten it. Future cosmetic.

- **Subtitle duplication on `/tools`.** `comparison_one_liner` now appears both on the tile (as a subtitle) and in the matrix below (in the "Best for" column). Same source-of-truth, different layouts. Defensible (scan vs deep-compare) but reads slightly noisy if you're scrolling top-to-bottom.

- **Pre-existing working-tree drift carried forward.** tools-hub `README.md` + `docs/PRODUCT-PLAN.md` (developability + library_planner status flips) + `supabase/.temp/cli-latest` (v2.90.0 to v2.101.0). llm-pd `.gitignore`, `.planning/phases/11-deployment/11-03-PLAN.md`, `docs/blocker-rfdiffusion.md`, untracked `CLAUDE.md` + `.planning/phases/11-deployment/11-03-PASTE-MANIFEST.md`. All flagged in the 2026-05-30 handoff; not staged this session either.

- **Repo state at handoff.** tools-hub main: this commit. llm-pd master: `6879ec7` (not touched this session). Working from the ranomics-website-2026 worktree at `C:\Users\lab\Documents\Claude_projects\.claude\worktrees\sad-easley-1e921f` (branch `claude/sad-easley-1e921f`); all file edits landed in the sibling tools-hub repo.
