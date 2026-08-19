# QC round 1 — Wave C (Phase 4d, the copy pass), PR #156

## Verdict: **BLOCKED**

Two factual errors, both introduced by this PR, both instances of the exact
defect class the PR set out to eliminate (a page contradicting itself; a number
with no source). Both are small copy edits. Everything else in the PR is sound
and several of its claims verify cleanly — this is a short block, not a rewrite.

- **Reviewed SHA:** `98dd20d24f8050cc2640bce9d208805ca704c694` (branch `copy/wave-c-phase-4d`)
- **Merge base:** `origin/main` = `48b4b71eedd2f791142ee4d020ee977a6961a6be`, confirmed by
  `git fetch origin && git rev-parse origin/main`. **Trunk had not moved**; the stated base is
  the real base.
- 39 files, +726 / -346.
- All work done in dedicated worktrees `scratchpad/qc-wavec` (98dd20d) and `scratchpad/qc-base`
  (48b4b71). The main working tree was never touched.

---

## Blockers

### B1 — `/tools` step 2 says "Five tools", the same page renders eight

`templates/tools/comparison.html`, step 2 of the rewritten four-step loop:

> Upload your target, mark the patch, and get back candidates ranked by confidence.
> **Five tools here do this** — start with one, clone the run, adjust, re-run.

The band `"Make new binders for my target"` in `shared/tools_catalog.py::_TOOL_CATEGORIES`
holds **eight** slugs. Measured off the rendered HTML by extracting `/tools/<slug>` hrefs
between that band heading and the next one:

```
comparison -> band contains 8 distinct tools:
  ['bindcraft','boltzgen','esmfold2-design','iggm','proteina','pxdesign','rfantibody','rfdiffusion']
index      -> band contains 8 distinct tools:  (same list)
```

The count is a fossil of the sentence it replaced. The old copy — *"Run BindCraft, RFdiffusion,
BoltzGen, RFantibody, or PXDesign"* — enumerated five and was correct when written. Converting
an enumeration into a count without recounting produced a number that the reader can falsify by
scrolling one screen down the same page. Proteina, IgGM and ESMFold2 design were added to the
band later.

Fix: drop the count, or derive it. `"Eight tools here do this"` goes stale the same way the next
time a designer is registered.

### B2 — Proteina's "three independent scoring checks" is contradicted by its own tool

New copy, introduced by this PR in three places (`git diff` confirms all three are `+` lines):

- `tools/proteina/meta.py` `comparison_one_liner`: *"Every candidate is filtered through three
  independent scoring checks"* — this feeds the **homepage card**, **/tools**, and
  **/help/tools/proteina**.
- `tools/proteina/meta.py` `about["what_it_is"]`: *"scores every candidate through three
  independent checks (an AlphaFold2 refold, a RoseTTAFold3 fold, and a physics force field)"*.
- `tools/proteina/__init__.py` blurb → rendered lede: *"with every candidate filtered through
  three independent scoring checks"*.

It is not true. `tools/proteina/Dockerfile.modal:229-231`, quoted verbatim:

> Only ligand_binder (RF3 is its sole reward) and motif_ame need it; protein_binder
> scores on AF2 alone, so it runs regardless of this switch.

And `tools/proteina/meta.py:62-67` — a comment **in the same file, 70 lines above the new
copy** — says the same thing and cites that Dockerfile line:

> This used to read "same container, same reward stack". The container is the same; the reward
> stack is NOT. protein_binder scores on AF2 alone, while RF3 is the SOLE reward for
> ligand_binder […]

No variant runs all three. The three are a menu, selected per variant, not a pipeline every
candidate passes through.

**It also contradicts itself on the rendered page.** `/tools/proteina` carries, within one
scroll, both of these:

```
"...three independent scoring checks. The run fans out across GPUs and stops when your wallet does."
"...Ranked designs with reward scores (AF2 pLDDT / ipTM for protein, RF3 score for ligand /
 motif, force-field energy where applicable)..."
```

"AF2 for protein, RF3 for ligand/motif, force-field where applicable" is the true statement and
it is the direct negation of "every candidate, three checks". This is defect C's shape —
the defect this PR was written to fix — reintroduced on a different page.

This is the highest-blast-radius string in the PR: `comparison_one_liner` is described in the
plan itself as *"the real 'which tool do I pick' source of truth"*.

---

## Claim-by-claim

### Claim 1 — suite unchanged, delta zero — **CONFIRMED**

Command, run from each worktree root, **no path argument**:

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| tree | SHA | result |
|---|---|---|
| merge base | `48b4b71` | **5262 passed, 20 skipped** in 178.80s |
| PR head | `98dd20d` | **5262 passed, 20 skipped** in 190.40s |

Both green first try; no flake re-runs needed. Full output captured to files, never piped
through `tail`.

A matching count is not a matching set, so node ids were compared directly with
`pytest -q --collect-only` on both trees: **5282 ids each**, and the set difference is exactly
one line each way:

```
only in base:  tests/test_scout_anonymous_access.py::TestStillGated::
               test_feasibility_get_requires_login[/scout/feasibility/download/94ff49b7-...]
only in head:  ...same test, same class, [/scout/feasibility/download/c99e0eb5-...]
```

That is a randomly generated UUID inside a parametrize id, not a rename. **No test was removed,
renamed, or skipped.**

### Claim 2 — exactly one test changed, and it was the copy that was wrong — **CONFIRMED, with a precise limit**

Every assertion-level change in the whole PR, from `git diff 48b4b71 98dd20d -- tests/`:

```
-        assert "aim above" in body
+        assert str(_escape(_mg.GLOSSARY["ipTM"]["good_range"])) in body
```

One line, one direction. Nothing else in `tests/` changed but comments and a docstring. No other
test asserting on copy was weakened or deleted.

The old assertion pinned template wording (`"aim above"`), so it was enforcing the defect: it
would have failed the fix. Replacing it was correct.

**Mutation results:**

| # | mutation | landed? | failing test |
|---|---|---|---|
| M1 | `about_panel.html`: replace the glossary interpolation with the literal `aim above roughly 0.7` | **yes** | `tests/test_public_tool_pages.py::TestExplainerRendersInBothAuthStates::test_score_legend_and_faq_render` — 6 params (`[False/True-mpnn/proteina/boltz2]`) |
| M2 | same interpolation replaced with the *same* band as a literal (`&gt; 0.75 strong; &gt; 0.65 acceptable`) | **no** — 49 passed | none |
| M3 | restore defect A: `rfdiffusion` seo_phrase = `"RFdiffusion de novo binder design online against a target you upload"` | **no** — full suite 5262/20 green | none |
| M4 | delete the `proteina` key so the fallback fires | **no** — full suite 5262/20 green | none |

M1 proves the new assertion is real. M2 is the honest limit of "strictly stronger": the test
asserts the *value*, not the *sourcing*, so hardcoding the identical band at parity is invisible.
That limit is acceptable in practice — the failure mode that matters is **drift**, and drift is
caught: change the glossary and a hardcoded literal immediately stops matching. Call the claim
true as stated for any threshold change, and note the parity hole.

M3 and M4 were verified to have actually landed by re-rendering the pages, not by trusting the
patch. With the mutation applied, `/tools/rfdiffusion` renders the original defect verbatim —

> RFdiffusion is a **RFdiffusion** de novo binder design online **against a target you upload
> you can run through** tools.ranomics.com on a dedicated GPU.

— both the stutter and the grammatical failure the builder found in its own first draft. `/tools/proteina`
under M4 renders the fallback. **Neither of the two rules is guarded by any test.** Defect A is
fixed by hand and can silently regress the next time a tool is registered — which is precisely
how it got there (the map was written before four tools existed). Not a blocker; a disclosed gap
worth a follow-up guard.

### Claim 3 — the ipTM threshold — **the 0.65 decision is right; the "nowhere else" claim is FALSE**

Independently re-derived. `shared/metric_glossary.py` `GLOSSARY["ipTM"]["good_range"]` is
`"> 0.75 strong; > 0.65 acceptable"` (unchanged by this PR). `shared/score_legends.py`:

| tool | good | excellent |
|---|---|---|
| rfdiffusion | 0.65 | 0.75 |
| af2 / colabfold | 0.6 | 0.75 |
| boltz2 / boltzgen | 0.7 | 0.8 |
| bindcraft / pxdesign | 0.75 | 0.85 |

Per-tool prose numbers: `rfdiffusion/meta.py:166` `ipTM ≥ 0.65`; `pxdesign/meta.py:126`
`ipTM ≥ 0.70`; `boltz2/meta.py:134` `ipTM > 0.7`; `esmfold2_design` `iptm > 0.75`. Every one sits
inside or at the edge of the stated band, and the new panel text explicitly licenses that
("Individual tools set their own pass bar a little either side of that"). **No tool page
contradicts itself on ipTM.** The builder picked correctly: 0.65 is the acceptable floor with a
source; the old flat "0.7" had none.

**But "0.7 living only in `about_panel.html`" is wrong.** `templates/help/faq.html:35`:

> "ipTM is interface confidence, higher is better, **aim above roughly 0.7 on a tractable
> target**. pLDDT is per-residue fold confidence…"

That is the deleted sentence, surviving verbatim, hardcoded, on a help page that also emits it as
JSON-LD `FAQPage` structured data (so it is eligible for a Google rich result). Not a
contradiction inside one page and so not a blocker, but the single-source claim does not hold and
this is the obvious second home for the same glossary read.

### Claim 4 — defect A, the stuttering lede — **CONFIRMED on all 14 rendered pages**

Driven anonymously through `create_app()` + `test_client()`, with `FLAG_TOOL_*` set on for every
registered slug. The vacuity guard the task asked for, asserted rather than eyeballed:

```
REGISTERED (14): ['af2','bindcraft','boltz2','boltzgen','colabfold','esmfold','esmfold2-design',
                  'iggm','mpnn','opendde','proteina','pxdesign','rfantibody','rfdiffusion']
TOOL PAGES 200: 14 / 14
OK: 14 adapters registered AND 14 tool pages at 200
```

All 14 ledes were extracted from the rendered `<div class="hero">` and read as prose. Results:

- **No stutter.** No tool name appears twice; no `seo_phrase` contains its own tool's name.
- **No slug leak** on any of the 14. `esmfold2-design`, `opendde`, `proteina` all now have map
  entries; `_PREVIEW_TITLE_PHRASES` gained `opendde` and `proteina`.
- **No subordinate-clause failure.** Every `"X is a <phrase> you can run through…"` parses.
  I specifically hunted the class the builder found in its own draft and found no survivors.
- **Fallback path**: no slug interpolation, grammatical — but it double-says the frame:
  *"…you can run through tools.ranomics.com **on a dedicated GPU**. Run it through your browser
  **on a dedicated GPU** with no install."* Safe, mildly clumsy, and unreachable while all 14 are
  mapped. Cosmetic.

### Claim 5 — boltzgen runtime — **CONFIRMED, no fourth number**

Every runtime string in `tools/boltzgen/meta.py`:

```
27 : PRESET_RUNTIME  "pilot": {"typical_minutes": "15 to 60"}
62 : faq             "Pilot runs typically finish in 15 to 60 minutes on a ..."
109: when_to_use     "You can wait 15 to 60 minutes per run."
176: runtime_table   {"preset": "pilot", "typical": "15 to 60 min"}
```

`grep` for `minute|hour|to 60|to 90` over the file returns nothing else; `tools/boltzgen/__init__.py`
carries no runtime string. The old 30-90 and 5-60 are gone. Rendered page shows `pilot 15 to 60 min`
in the runtime table and `~15 to 60 min on A100-40GB` in the preset text — consistent.

### Claim 6 — defects B and D — **CONFIRMED, with one new contradiction (see S2)**

**B (binder length).** All three landed, and the prose is the strongest writing in the PR — it
gives a *method* ("wide window buys variety, narrow buys consistency"; "aim short for a small flat
patch, longer for a broad face or a groove") instead of syntax and bounds.

- rfdiffusion: both copies. Form template `templates/tools/rfdiffusion_form.html:267ff`, **and**
  the `meta.about["inputs"]` copy, which renders as *"Binder length (min/max). How long the new
  binder should be. Each design draws its own length from this window. 55 to 65 is a compact
  single-domain binder…"*. Two copies, matched.
- proteina: `templates/tools/proteina_form.html:221ff`. This one earns its place — it explicitly
  kills the trap that "blank is unconstrained": *"Leaving both blank is **not** unconstrained —
  it means 60 to 120, the model's own default. 20 to 300 allowed."*
- boltzgen: had none, now has help.

**D (getting_started step 2).** Compared directly against PR #154's pilot card
(`templates/components/pilot_card.html:87-99`, **unchanged by this PR**):

| | wording |
|---|---|
| pilot card (#154) | "…If you do not know yours yet, *score your target's surface with Epitope Scout* first — it is free, and its results hand the target and the residues back into this form / and you copy the residues it picks into the field below." |
| getting_started (this PR) | "…If you do not know yours yet, *score your target's surface with Epitope Scout* first — it is free, and it is the step before any of this, not a fallback for when a paid run comes back empty." |

The shared clause is byte-identical through "it is free". The tails differ, but they state
*different facts* (what happens next vs. where it sits in the sequence), not two phrasings of
one fact. **Claim holds** — this is a match, not a second voice. Link verified live:
`GET /scout` → 308 → `/scout/` → **200 anonymously**, so "it is free" is not promising a
login wall.

### Claim 7 — no hardcoded money figure — **CONFIRMED**

`git diff 48b4b71 98dd20d | grep '^+' | grep -E '\$[0-9]|USD ?[0-9]'` returns **nothing**. The
one credit figure on the changed hero is `{{ signup_credit() }}`, the jinja global backed by
`shared.wallet.SIGNUP_CREDIT_USD`, and it renders `$15`.

The known gap is real and confirmed by reading the guard:
`tests/test_signup_credit_single_source.py:55` walks `TEMPLATES.rglob("*.html")` only, so
`templates/*.txt` is unwatched. **This PR touches no `.txt` file** (the diff is `.py` and `.html`
exclusively), so nothing hides in the gap here. Still open as a repo-level item.

---

## Secondary findings (not blocking)

**S1 — `templates/help/faq.html:35` keeps "aim above roughly 0.7".** See Claim 3. Should read
from the glossary the same way `about_panel.html` now does, or the single-source claim is
half-true and the FAQ is the surviving copy that Google indexes as structured data.

**S2 — new boltzgen copy instructs a value the field rejects.** The rewritten Protocol help now
says: *"The choice sets what a sensible binder length is above: mini-protein 50 to 100, nanobody
110 to 130, antibody 110 to 200, **peptide 5 to 30** residues."* The binder-length inputs it
points at carry `min="10"` (`boltzgen_form.html:182,188`). A user who picks Peptide and follows
the instruction to type 5 is refused by the browser.

Both halves are pre-existing (base has `min="10"` and "peptide 5 to 30"). What is new is the
sentence that *ties them together*: the old text was a floating list under "Typical binder
lengths:", the new text actively directs the reader to put those numbers into that field. The PR
converts a latent mismatch into an instruction that fails. Cheap fix either way.

**S3 — the jargon disclosure list is incomplete.** I grepped 12 terms across all 18 rendered
pages and read the first use of each in context. The disclosed items check out — `MSA`,
`recycles`, `CDR`, `ipTM` unglossed in field help on non-priority forms, and `backbone` unglossed
on 13 of 14 pages (first use is almost always the shared score legend's *"when ProteinMPNN
redesigns a known sequence on its native backbone"*; only `mpnn` and `rfdiffusion` gloss it
first, both in copy this PR wrote). `contig` is at **zero** pages, which is a genuine win.

Not disclosed, and found:

- **`hotspot` unglossed on first use on `boltzgen` and `proteina`** — both are *priority* forms
  under the plan's item 5, not spillover. boltzgen: *"Upload PDB, optionally pick hotspots, set
  binder length."* proteina: *"It carries its own target, chain range, hotspots and binder
  length."* rfdiffusion, pxdesign, bindcraft, rfantibody and boltz2 all gloss it properly.
- **`hotspot`, `ipTM` and `pLDDT` unglossed on the homepage**, STEP 02: *"upload the target PDB,
  mark hotspots, submit. Ranked candidates with ipTM, pLDDT, shape complementarity…"* — one
  screen below a hero this PR rewrote specifically so a biologist would not hit that wall.
- **`ipTM`/`pLDDT`/`pAE` unglossed in `pxdesign`'s lede** — that is a *priority block* rewritten
  by this PR, not field help: *"Every candidate arrives with real ipTM, pLDDT and pAE measured
  against your own target."* Three unglossed acronyms in the page's first paragraph is the
  clearest single style-rule violation in the diff.
- **`backbone` unglossed in two blocks this PR newly wrote**: `comparison.html` step 3 (*"put
  fresh sequences on any backbone that looks promising"*) and the homepage chooser row (*"A
  backbone from somewhere else"*). `mpnn`'s own blurb glosses it beautifully — *"a backbone — a
  structure with no sequence decided yet"* — so the gloss exists and just was not reused.
- `esmfold2-design`'s lede ends *"by gradient descent instead of diffusion"*, unglossed and
  meaningless to the target reader.

**S4 — decision 1 is applied to the lede but not to the title above it.** `/tools/mpnn` still
renders `<title>ProteinMPNN Online | Free Sequence Design | Ranomics</title>`
(`_PREVIEW_TITLE_PHRASES["mpnn"]`) — in the map this PR edited. The builder's own stated
rationale for dropping "free" (running is billed, the page is indexable) applies *more* strongly
to a `<title>`, which is the most indexable string on the page and the one that shows in the
SERP. It is the only one of the 14 with "Free" in it.

**S5 — blurb and lede repeat each other, near-verbatim, on ~10 of 14 pages.** They render two
paragraphs apart in the same `<div class="hero">`. rfdiffusion is the worst:

> **blurb:** Upload your target structure, mark the residues you want gripped, and get back
> brand-new binders, each carrying a real AlphaFold2 confidence score against your target.
> **lede:** Upload your target, mark the residues you want the binder to touch, and get back
> brand-new binders that each carry a real AlphaFold2 confidence score against your own target.

bindcraft, rfantibody, mpnn, colabfold, esmfold, af2, boltz2 and opendde have the same shape.
Individually each sentence is good; stacked they read as a stutter of a different kind. The
structural cause is that Phase 4d rewrote items 3 and 4 (blurb, `about`) and the lede
(`seo_phrase`/`seo_long`) to the same brief without either knowing the other renders adjacently.
A judgement call, and I would fix it by making `seo_long` carry the *differentiator* the blurb
does not — which is exactly what pxdesign and proteina do.

---

## Reading the 14 pages as a bench biologist

**Voice consistency: good, with three outliers.** Eleven of fourteen ledes follow one clear
pattern — *what you upload, what you mark, what comes back* — and it works. A reader who does not
know what RFdiffusion is learns in one sentence that they upload a target, mark a patch, and get
binders with a confidence score. That is a real improvement over the merge base, where the same
slot read "RFdiffusion is a RFdiffusion de novo binder design online".

Outliers, named:

1. **pxdesign** — the only lede that never says what you give it. It opens with the output and
   three unglossed acronyms, then closes with a marketing line about Ranomics' wet-lab campaigns.
   Different writer, different purpose.
2. **proteina** — starts *"Aim at a recessed pocket…"* and also never says what you upload. Plus
   the false claim in B2.
3. **af2** — ends *"results land at /jobs"*, a URL fragment no other lede carries, left over
   from the merge base.

Minor: `opendde` reaches for *"AlphaFold3-class"*, which is a comparison to a model the target
reader was just told they do not need to name.

**Overclaims.** B2 is the real one. Two lesser ones:

- The hero's *"Fourteen models sit behind that"* sits in a paragraph about uploading a structure
  and getting **binders** back. Eight of the fourteen design binders; five predict structures and
  one designs sequences. Read in context it promises fourteen ways to get a binder. "Fourteen
  tools" would be true and just as strong.
- I checked for the Scout-handoff class specifically (a card promising a round trip that does not
  exist). `pilot_card.html` gates that on `pilot.hotspot_handoff` and is not touched here, and the
  new getting_started sentence promises only "score your target's surface first", which the app
  does deliver — `/scout/` is 200 anonymously. **Clean on that class.**

**Where a bench biologist still gets stuck.** Not at the hero — the chooser table and the
rewritten step 1 genuinely land them on `/tools/rfdiffusion?pilot=1` without ever naming a model.
They get stuck at **Hotspot residues**, one field into the form, on the two priority tools that
do not gloss it (S3), and at the homepage's own STEP 02 which says "mark hotspots" with no gloss
at all. The binder-length work (defect B) shows exactly what fixing that looks like — hotspot
help on boltzgen and proteina deserves the same treatment. Second sticking point: the shared
score legend is the first use of `backbone` on 13 pages and never defines it, so the reference
material assumes the vocabulary the page above it just avoided.

---

## The three disclosed decisions

**1. Dropping "free" from tool-page ledes — RIGHT, but finish it.** Running is billed against
the wallet, the pages are indexable, and "free … tool online" on a page whose Submit button leads
to a paywall is the kind of thing that erodes trust at exactly the moment the user is deciding to
fund. The reasoning is sound. It is half-applied: `_PREVIEW_TITLE_PHRASES["mpnn"]` still ships
`Free Sequence Design` into the `<title>` (S4). Either drop it there too or the decision is
inconsistent in the one place search engines read first.

**2. Lede still opens with the tool name — RIGHT.** The brief said fix defect A at the generator,
and the frame `"{short_name} is a {seo_phrase}…"` lives in `_form_hero.html`, which is out of
scope. The rule that matters ("no algorithm name in the first sentence") is about not *leading*
with a model as the way in — and by the time a reader is on `/tools/rfdiffusion` they arrived via
a task-named band or the chooser, so the name is now an answer to "what am I looking at", not a
gate. The `<h1>` is the tool name anyway; changing only the lede would have been cosmetic.
Deferring the frame rewrite to a phase that owns that template is the correct call.

**3. Rewriting the `/tools` four-step loop — RIGHT to have touched it.** Leaving step 2 reading
"Run BindCraft, RFdiffusion, BoltzGen…" one click after a hero that promises you do not have to
name one would have shipped a visible contradiction; scope discipline that preserves an
inconsistency is not discipline. The judgement was right — **the execution introduced B1.**
Which is itself the argument for the disclosure: it got reviewed because it was flagged.

---

## What I verified empirically vs. reasoned about

**Ran:** both suite baselines and the node-id diff; four mutations, each re-rendered to prove it
landed; all 14 tool pages plus `/`, `/tools`, `/help/getting-started`, `/help/faq` rendered
anonymously through `create_app()`/`test_client()` with the 14-adapter and 14×200 assertions;
the jargon grep over rendered output; the band-membership count off the rendered HTML; the
`/scout` reachability check; the money-figure grep over the diff; the guard's own glob.

**Reasoned about, not run:** copy quality, voice consistency and the outlier calls in the two
sections above are judgement, and I have said so where they are. S5 (blurb/lede redundancy) is a
judgement call — no rule in the brief forbids it. B1 and B2 are **not** judgement: both are
falsifiable against the same rendered page and both are false.

**Not covered:** signed-in rendering (I drove the anonymous path only, which is where the copy
lives); live GPU behaviour; anything about `.github/workflows/deploy-modal.yml`, which another
branch owns.

---

## To unblock

1. `templates/tools/comparison.html` step 2 — remove or correct "Five tools here do this" (eight).
2. `tools/proteina/meta.py` (`comparison_one_liner`, `about["what_it_is"]`) and
   `tools/proteina/__init__.py` (blurb) — replace "three independent scoring checks" with
   something true of the variant, e.g. "scored against your target by a refold the variant
   selects — AlphaFold2 for protein targets, RoseTTAFold3 for ligand and motif". Match
   `about["output_summary"]`, which is already correct.

Then S1, S2 and S4 are three one-line edits worth folding into the same push. S3's unglossed
`hotspot` on boltzgen and proteina is the one that costs a real user a real run, and is the
strongest candidate for a follow-up rather than a re-block.
