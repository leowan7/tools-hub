# QC round 2 — Wave C (Phase 4d, the copy pass), PR #156

## Verdict: **MERGE**

- **Reviewed SHA:** `7e2da873b87b307fd1696287b1b3433bbf99aaeb` (branch `copy/wave-c-phase-4d`)
- **Merge base:** `origin/main` = `48b4b71eedd2f791142ee4d020ee977a6961a6be`
  (`git fetch origin && git rev-parse origin/main`, re-run at the start of this round —
  trunk had not moved since round 1).
- Round 1 reviewed `98dd20d`; this round reviews the four commits since.
- Worktrees: `scratchpad/qcB-base` (48b4b71) and `scratchpad/qcB-head` (7e2da87), both
  created by this agent with `git worktree add --detach`. **The main working tree
  (checked out on `fix/pin-gpu-image-digests`) was never touched.**

---

## Suite, re-measured from scratch

Command, run from each worktree root, **no path argument**, output captured to a
file and read whole (never piped through `tail` in a way that hides a failure):

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| tree | SHA | result |
|---|---|---|
| merge base | `48b4b71` | **5262 passed, 20 skipped** in 223.31s |
| PR head | `7e2da87` | **5277 passed, 20 skipped** in 203.63s |

Both green first try, no flake re-runs. **+15, claim CONFIRMED.**

A matching count is not a matching set, so node ids were compared with
`pytest -q --collect-only` on both trees: base **5282**, head **5297**.

```
only in base (1):  tests/test_scout_anonymous_access.py::TestStillGated::
                   test_feasibility_get_requires_login[/scout/feasibility/download/0466db0c-...]
only in head (16): the same test with a different random UUID param, plus 15 new ids,
                   ALL in tests/test_public_tool_pages.py:
  TestRenderedLedeRules  x6 (own-name, subordinate-clause, relative-clause,
                             raw-slug, every-tool-has-a-phrase, fallback-leak,
                             no-"free")   [7 methods, one is 'free' -> 7]
  TestCatalogLoopStepTwoCountIsDerived x4 (step2, homepage, spelled-out x2 params)
  TestIptmThresholdHasOneSource x4
```

**Nothing was removed, renamed, or skipped.** Skips 20 -> 20.

### The "zero deletion lines under `tests/`" claim — TRUE ONLY OF THE ROUND-1 DELTA

```
git diff --numstat 98dd20d 7e2da87 -- tests/   ->  396  0   (zero deletions)  ✅
git diff --numstat 48b4b71 7e2da87 -- tests/   ->  416  5   (FIVE deletions)
```

The five are four docstring lines plus `assert "aim above" in body`, the single
assertion round 1 already confirmed and blessed (it was pinning the defective
wording). So the claim is **materially true and stated imprecisely**: this round's
commits deleted nothing under `tests/`, the PR as a whole deleted the one line
round 1 already cleared. No finding.

---

## The app, driven anonymously

Every claim below was checked against **rendered HTML**, produced by
`create_app()` + `test_client()` with no session and `FLAG_TOOL_<SLUG>=on` for
every registered adapter. The vacuity guard, asserted rather than eyeballed:

```
OK: 14 adapters registered AND 14/14 tool pages at 200
REGISTERED: ['af2','bindcraft','boltz2','boltzgen','colabfold','esmfold',
             'esmfold2-design','iggm','mpnn','opendde','proteina','pxdesign',
             'rfantibody','rfdiffusion']
```

33 pages rendered in total: 14 `/tools/<slug>`, 14 `/help/tools/<slug>`, plus
`/`, `/tools`, `/help/faq`, `/help/getting-started`.

---

## Claim 1 — B1, the counts are derived — **CONFIRMED, and it survives a real break**

Band membership extracted from the rendered HTML myself (split on
`<span class="catalog-section-title">`, collect `/tools/<slug>` hrefs to the
next heading):

```
/tools band "Make new binders for my target" (8):
  ['bindcraft','boltzgen','esmfold2-design','iggm','proteina','pxdesign',
   'rfantibody','rfdiffusion']
/tools step 2 renders : "8 tools below do this - start with one, clone the run, adjust, re-run."
/       hero renders  : "8 different design tools sit behind that, and you do not have to name one to start."
```

**Break test — flag-gated composition.** I turned one design tool's feature flag
off and re-rendered both pages. Three separate runs:

| flag off | band rendered | `/tools` step 2 | homepage hero |
|---|---|---|---|
| `iggm` | 7 | "**7** tools below do this" | "**7** different design tools" |
| `proteina` | 7 | "**7** tools below do this" | "**7** different design tools" |
| `bindcraft` | 7 | "**7** tools below do this" | "**7** different design tools" |

The sentence follows the band, on both pages, on a deployment where the flags
differ from mine. The homepage rewrite is also the right call on the merits:
the old "Fourteen models" sat in a paragraph promising binders while six of the
fourteen predict structures or design sequences.

Spelled-out-count sweep over `/` and `/tools` for `Four…Fourteen` x
{tools, models, designers, algorithms}: **zero hits.** The guard's own coverage
is narrower than that — see mutation M-F.

## Claim 3 — B3, the ipTM sweep — **CONFIRMED, and the per-tool decision is right**

`aim above roughly` (whitespace-insensitive) across all 33 rendered pages:
**zero hits.** Every general legend now renders the glossary string
`> 0.75 strong; > 0.65 acceptable` — on `/help/faq` (visible **and** in the
FAQPage JSON-LD), on all 14 `/help/tools/<slug>`, and on all 14 `/tools/<slug>`.

The per-tool numbers, audited off the rendered pages:

| page | number | source, re-derived |
|---|---|---|
| `/tools/rfdiffusion`, `/help/tools/rfdiffusion` | `ipTM >= 0.65` | `shared/score_legends.py ("rfdiffusion","ipTM") good=0.65` |
| `/tools/pxdesign`, `/help/tools/pxdesign` | `ipTM >= 0.70` | `score_legends ("pxdesign","ipTM") good=0.75`, prose bar 0.70 |
| `/tools/boltz2`, `/help/tools/boltz2` | `ipTM > 0.7` | **`tools/boltz2/run_pipeline.py:99 STRICT_IPTM = 0.7`** |
| `/tools/boltzgen` | `ipTM 0.98` | a showcase example, not a threshold |

**The builder's reasoning that these must NOT be forced to one band is correct,
and I verified the strongest case of it empirically:** boltz2's 0.7 is not
prose, it is `STRICT_IPTM = 0.7` in the pipeline that classifies the run.
Rewriting it to 0.65 would make the page disagree with the code that produces
the number. Every per-tool bar sits inside or at the edge of the general band,
and the new sentence "Individual tools set their own pass bar a little either
side of that" licenses the spread explicitly. **No page contradicts another on
ipTM, and no general-guidance threshold is left unsourced.**

## Claim 6 — the structured-data fix — **CONFIRMED by an independent AST scan**

Not a regex over source. I parsed all **112** templates with jinja2's own parser
and walked every `nodes.Const` string reachable from a `nodes.Assign` /
`nodes.AssignBlock`, looking for a literal `{{` or `{%`.

```
HEAD 7e2da87: 112 templates scanned -> 0 hits (in set blocks, and in ANY string constant)
```

**Positive control — the scanner is not certifying false.** The same scanner run
against the merge base finds the defect it was written for:

```
BASE 48b4b71: 112 templates scanned -> 1 hit
  templates\help\faq.html:29  'Your ... Your ${{ signup_cred...'
```

Two further checks on the rendered side: across all 33 rendered pages, **zero**
JSON-LD blocks contain `{{` or `{%`, **every** JSON-LD block parses as valid
JSON, and **zero** occurrences of raw jinja syntax appear anywhere in any
rendered page body. Only 4 templates emit JSON-LD at all
(`base.html`, `help/faq.html`, `help/tool_guide.html`, `showcase.html`).

The defect was **pre-existing at `48b4b71`**, not introduced by this PR — so
this is a live SEO defect the PR found and closed. "Only one instance" holds.

---

## Claim 4 — B4, the lede rules are guarded — **CONFIRMED, with a real hole in the proxy**

Round 1's own reproduction, replayed. With defect A restored the suite now goes
**RED by name**, where round 1 measured it staying green at 5262/20:

```
M-A  rfdiffusion seo_phrase := "RFdiffusion de novo binder design online against a target you upload"
     -> 3 failed, 5274 passed, 20 skipped
        TestRenderedLedeRules::test_lede_phrase_never_repeats_the_tools_own_name
        TestRenderedLedeRules::test_lede_phrase_does_not_end_in_a_subordinate_clause
        TestRenderedLedeRules::test_no_lede_phrase_leaks_a_raw_slug
```

The fallback path, both directions:

```
M-B  delete the "proteina" key  -> 1 failed  test_every_registered_tool_has_its_own_phrase
M-C  fallback := f"free {slug} tool online"  -> 1 failed  test_the_fallback_itself_leaks_no_slug
```

All three mutations verified landed with `git diff --unified=0` **before** the run.

---

## Claim 2 — B2, the proteina claim — **the four named surfaces are fixed and true; the "none overclaims" claim is REFUTED by a fifth**

### The four the builder names are correct

Source, read myself. `tools/proteina/Dockerfile.modal:229-231`:

> `PROTEINA_RF3=on -> RF3 reward channel live … Only ligand_binder (RF3 is its sole
> reward) and motif_ame need it; protein_binder scores on AF2 alone, so it runs
> regardless of this switch.`

`tools/proteina/meta.py:61-67` says the same and cites that line. `reward_attributions`
(meta.py:92-97) splits it the same way: AF2 = protein-binder confidence, RF3 = ligand +
motif reward. **No variant runs all three.** The claim was false and it is now gone from:

| surface | rendered as | now reads |
|---|---|---|
| `comparison_one_liner` (homepage card, `/tools`, `/help/tools/proteina`) | ✅ fixed | "re-folded and scored against your target as it is generated" |
| `about["what_it_is"]` (`/tools/proteina` about panel) | ✅ fixed | "Which model does that scoring follows the target: a protein target … AlphaFold2 … a small-molecule or motif target by RoseTTAFold3" |
| `adapter.blurb` (`/tools/proteina` hero) | ✅ fixed | no scoring claim at all |
| `_PREVIEW_SEO_PHRASES["proteina"]` `seo_long` (the lede) | ✅ fixed | "every candidate the search generates is re-folded against it" |

Grep for surviving variants of the literal phrase: the only hits in the whole repo are
**three explanatory comments** saying why it was removed, plus one unrelated test
docstring in `test_multichain_targets.py`. Clean.

### But a fifth surface still carries the same claim in the base's own wording

`tools/proteina/meta.py:121-127`, `seo_faq[2]`, **untouched by this PR**:

> "How are Proteina-Complexa designs scored and ranked?" — **"Each search shard filters
> candidates through an AF2 / RF3 / force-field reward stack**, and the hub then selects a
> global top-K across all shards…"

That renders **twice on `/tools/proteina`**: as visible FAQ copy, and inside the
`FAQPage` JSON-LD (I parsed the block and read the answer out of the object —
`@type=FAQPage`, so it is rich-result eligible). It is the same false composite: a
protein_binder shard uses AF2 (plus force field), a ligand_binder shard uses RF3 only.
No shard uses the stack.

**It contradicts the same page, two paragraphs apart.** `about["output_summary"]`
renders, on `/tools/proteina`:

> "…reward scores (AF2 pLDDT / ipTM for protein, RF3 score for ligand / motif,
> force-field energy where applicable) … **The ligand and motif variants score on RF3
> only.**"

"score on RF3 only" is the direct negation of "each shard filters through an AF2 / RF3 /
force-field stack".

### Why this is NOT a blocker — checked, not assumed

I dated both strings against the merge base before judging:

```
git show 48b4b71:tools/proteina/meta.py | grep -n "RF3 only"        -> 242 (present at base)
git show 48b4b71:tools/proteina/meta.py | grep -c "AF2 / RF3 / "    -> 3   (present at base, x3)
grep -c "AF2 / RF3 / " tools/proteina/meta.py  (head)               -> 1
git diff 48b4b71 7e2da87 -- tools/proteina/meta.py | grep "reward stack"  -> only DELETIONS
```

**The page already contradicted itself at `48b4b71`** — `output_summary` said "RF3 only"
while `seo_faq` said "AF2 / RF3 / force-field stack". This PR did not create the
contradiction, did not touch the surviving string, and cut the false statements from
four to one while adding an explicit correct one. My first reading was that the PR
manufactured the contradiction by fixing three of four; the git history says otherwise
and I am reporting the corrected version.

So: **the PR strictly improves this page**, and the builder's claim to me — that every
rendered proteina surface is now clean — is **wrong as stated**, by one surface it did
not look at. That is an accuracy finding about the claim, not a defect in the change.
It is the single strongest follow-up in this report: one line, in a file this PR already
edits, on an answer Google indexes.

---

## Reading the 14 pages as a bench biologist

### Does the blurb/lede split read as one voice? — **yes, and it is the best change in the PR**

Round 1's S5 was that blurb and lede said the same sentence twice on ~10 of 14 pages.
The builder's fix was structural: **the blurb states the mechanics** (what you upload,
what comes back, how long) and **the lede sells the task** (why reach for this one).
Read across all 14 rendered heroes, it holds. `rfdiffusion`, round 1's worst case:

> **blurb:** Upload your target structure, mark the residues you want gripped, and get back
> brand-new binders, each carrying a real AlphaFold2 confidence score against your target.
> A pilot run takes roughly 15 to 30 min.
> **lede:** The most widely used de novo binder generator, run end to end: every design it
> invents is re-folded with AlphaFold2 against your own target, so the confidence score you
> read is measured rather than the generator marking its own work.

The lede now carries an argument the blurb does not (the score is measured, not
self-reported). That is a real differentiator, not a restatement. The same holds on all
14. **Judgement call, and I am calling it clearly improved.**

**All three of round 1's named outliers are fixed**, verified on the rendered page:

- `pxdesign` opened with three unglossed acronyms (`ipTM, pLDDT and pAE`). It now reads
  "The same pipeline Ranomics runs for its own wet-lab campaigns: every candidate comes
  back already re-folded against your target, carrying its own confidence score for the
  contact rather than a number borrowed from the generator." **Zero acronyms.** This is
  the single biggest copy win in the PR.
- `af2`'s trailing "results land at /jobs" — gone.
- `proteina`'s "Aim at a recessed pocket…" (never said what you upload) — now says it.
- `opendde`'s "AlphaFold3-class" (a comparison to a model the reader was told not to
  need) — gone.
- `esmfold2-design`'s "by gradient descent instead of diffusion" — gone.

### The one remaining voice outlier: proteina

Thirteen of the fourteen ledes are declaratives or noun phrases — "The reference-standard
fold…", "The cheap second opinion on a shortlist:…", "One model covers four binder
formats…", "Nanobodies are small enough to…". **`proteina` is the only imperative**:
"Upload a target the usual design tools struggle with…". Its blurb also opens "Upload a
protein or small-molecule target…", so it is also the only page where blurb and lede
still open on the same verb — the exact stutter the rewrite eliminated elsewhere. Purely
a judgement call, nothing false, and a small edit.

Minor: `opendde`'s "first-class parts of the input" is software vocabulary in a sentence
aimed at a bench biologist. Not wrong, slightly off-register.

### Overclaims — checked the Scout-handoff class specifically

I resolved the real gate rather than reading the copy: `scout/handoff.py`
`VALID_HANDOFF_TOOLS = ['bindcraft','boltzgen','pxdesign','rfantibody']`, consumed at
`blueprints/tools.py:877`. `templates/components/pilot_card.html` is **untouched by this
PR** and branches on it correctly — `rfdiffusion` (not in the set) renders "you copy the
residues it picks into the field below", `bindcraft` (in the set) renders "its results
hand the target and the residues back into this form". **The gate is honest.**

One pre-existing ambiguity, not introduced here: `/tools/rfdiffusion`'s `seo_faq` says
"run Epitope Scout first … then **hand off the picked residues into the RFdiffusion
form**", while the pilot card on the same page says you copy them manually. "Hand off"
is loose enough to cover a copy-paste, and `git diff` confirms this PR did not touch it.
Worth a word change eventually; not a false promise today.

Everything else checked out: `/scout` -> 308 -> `/scout/` -> **200 anonymously**, so
"it is free" promises nothing behind a login. Every internal `href` on `/` and `/tools`
resolves 2xx/3xx — **zero broken links**. Fact-checks that could have overclaimed and do
not: `iggm`'s "usually takes four separate ones" names four of its five real presets;
`opendde`'s "the protein-only Boltz-2" is accurate (the boltz2 adapter has no ligand
path at all); `bindcraft`'s "run up to four hours" matches its own adapter string.

**Round 1's S2 is fixed and now consistent in three places**: `boltzgen_form.html` and
`meta.py` both say "peptide 10 to 30", the form inputs carry `min="10"`, and
`tools/boltzgen/__init__.py:92-95` refuses anything under 10 server-side. No "peptide 5"
survives anywhere outside an explanatory comment. The copy no longer instructs a value
the tool rejects.

**Round 1's S4 is fixed**: `_PREVIEW_TITLE_PHRASES["mpnn"]` is now
`"Sequence Design on a Backbone"`; `/tools/mpnn` renders
`<title>ProteinMPNN Online | Sequence Design on a Backbone | Ranomics</title>`. No
`<title>` or lede on any of the 14 contains "free", and it is now guarded
(`test_no_lede_or_title_advertises_the_run_as_free`).

### Jargon: the builder's three claims verify; its known-open list does not

Verified on rendered text with `<script>`/`<style>` stripped (my first pass matched CSS
class names and JSON-LD and was worthless — noting it because it is the same
false-positive trap the guards fell into):

| claim | verdict |
|---|---|
| `hotspot` glossed on the homepage | ✅ "mark your hotspot residues — the numbered residues on the patch of your target you want the binder to sit on" |
| `hotspot` glossed on boltzgen | ✅ same gloss, first use |
| `hotspot` glossed on proteina | ✅ same gloss, first use |
| `backbone` glossed in the `/tools` loop | ✅ "the folded shape on its own, before a sequence is chosen for it" |

The gloss is copied verbatim from `pilot_card.html` in all three places, which is why it
reads as one voice rather than three attempts.

**The known-open list is incomplete by one item.** It discloses `backbone` unglossed *in
the shared score legend* — true, that is the first use on 9 of 14 tool pages ("Fraction
of native residues recovered when ProteinMPNN redesigns a known sequence on its native
backbone"). It does **not** disclose the homepage chooser row, which round 1 named
explicitly and which is still there:

> I have | I want | Start with
> **A backbone from somewhere else** | Sequences for it | ProteinMPNN

That is the first use of `backbone` on `/`, one screen below a hero this PR rewrote for
a reader who does not know the word, in a table whose whole job is to tell that reader
which row they are. `mpnn`'s own blurb has the gloss already
("a backbone — a structure with no sequence decided yet"). Cheap fix, and the disclosure
should have named it.

The rest of the disclosed list checks out: `MSA` (af2, boltz2, colabfold, esmfold, mpnn),
`recycles` (af2, colabfold, opendde), `CDR` (esmfold2-design, iggm, rfantibody), `ipTM`
in field help on the non-priority forms. `contig` remains at **zero** pages. `scFv` and
`VHH` are glossed on first use everywhere they appear in prose.

---

## Findings I found that nobody disclosed

### F1 — `about_panel.html` still hardcodes a second general threshold, four entries below the one that was fixed

`templates/components/about_panel.html:252-255`, rendering on **all 14 tool pages**, in
the same `<dl>` as the ipTM entry this PR converted to a glossary read:

> **ProteinMPNN recovery** — Fraction of native residues recovered when ProteinMPNN
> redesigns a known sequence on its native backbone. Higher is better; **well calibrated
> above roughly 0.4 on diverse folds.**

`shared/metric_glossary.py` has **no key for sequence recovery at all** — I enumerated
`GLOSSARY` and it holds 15 metrics, none of them this one. So this is a general-guidance
threshold, on 14 indexable pages, with no source: the exact defect B3 was raised to
eliminate, phrased almost identically ("above roughly 0.4" / "aim above roughly 0.7"),
sitting 18 lines below the fix.

The new guard misses it by one word — `STALE = re.compile(r"aim\s+above\s+roughly")`
does not match `well calibrated above roughly`.

Also worth stating plainly: `GLOSSARY` carries `good_range` for `pLDDT`
(`> 80 very high; 60–80 acceptable`), `pAE`, `i_pAE` and `pTM`, and the panel renders
**none** of them — those entries state no threshold at all, which is safe. So the
"three surfaces, one number" pattern is real but applies to exactly one of the panel's
four metrics.

**Pre-existing at `48b4b71`** (`git show 48b4b71:templates/components/about_panel.html`
line 239, verbatim). Not introduced here. Not a blocker — but it means the answer to
"is any general-guidance threshold left unsourced" is **yes, one**, and it is the
nearest neighbour of the one that was fixed.

### F2 — the homepage chooser row still uses `backbone` unglossed

See the jargon table above. Round 1 named it; the fix landed on `comparison.html` step 3
and not on `templates/index.html`. Not disclosed in the known-open list.

### F3 — the `{% set %}` JSON-LD guard is scoped to `/help/faq`, and three other
templates have the identical shape

`test_the_faq_structured_data_carries_no_template_syntax` fetches `/help/faq` only.
Four templates emit JSON-LD (`base.html`, `help/faq.html`, `help/tool_guide.html`,
`showcase.html`) and **three of the four build it out of `{% set %}` dict literals** —
`tool_guide.html:11 {% set sa = {...} %}`, `showcase.html:71
{% set _ = _items.append({...}) %}`, `base.html:82 {% set _ = _bc_items.append({...}) %}`
— which is precisely the construct that produced the bug. Mutation M-I measures the
consequence. My AST scan says all four are clean **today**; nothing stops the next one.

---

## Mutation table

Nine mutations, each applied in its own dedicated worktree, each **verified landed with
`git diff --unified=0` before the run**, and each of the six GREEN ones additionally
**re-rendered through `test_client()` to prove the defect actually reaches the page** —
a green suite against a mutation that never landed proves nothing. Every run is the full
suite from the worktree root with **no path argument**.

| # | mutation | landed | result | failing test |
|---|---|---|---|---|
| **M-A** | `rfdiffusion` seo_phrase := "RFdiffusion de novo binder design online against a target you upload" (round 1's defect A) | yes | **3 failed**, 5274 passed | `TestRenderedLedeRules::test_lede_phrase_never_repeats_the_tools_own_name`, `…::test_lede_phrase_does_not_end_in_a_subordinate_clause`, `…::test_no_lede_phrase_leaks_a_raw_slug` |
| **M-B** | delete the `proteina` key so the fallback fires | yes | **1 failed**, 5276 passed | `TestRenderedLedeRules::test_every_registered_tool_has_its_own_phrase` |
| **M-C** | fallback := `f"free {slug} tool online"` (the old f-string) | yes | **1 failed**, 5276 passed | `TestRenderedLedeRules::test_the_fallback_itself_leaks_no_slug` |
| **M-D** *(mine)* | seo_phrase gains "…design tool anyone can run against a target uploaded from the bench" — subordinate clause, no pronoun, no marker | yes | **GREEN** 5277/20 | none — **hole** |
| **M-E** *(mine)* | every lede rule violated, but in `seo_long` instead of `seo_phrase` | yes | **GREEN** 5277/20 | none — **hole** |
| **M-F** *(mine)* | `comparison.html`: the `design_count` interpolation replaced by the literal `8` (parity) | yes | **GREEN** 5277/20 | none — **hole** |
| **M-G** | `about_panel.html`: glossary read replaced by the *identical* band as a literal (the disclosed parity hole, re-tested against the NEW guards) | yes | **GREEN** 5277/20 | none — **confirmed** |
| **M-H** *(mine)* | `tool_guide.html`: restore "aim above&nbsp;roughly 0.7", defeating `STALE` with an HTML entity | yes | **GREEN** 5277/20 | none — **hole** |
| **M-I** *(mine)* | the same set-block/interpolation defect, in `tool_guide.html` instead of `faq.html` | yes | **GREEN** 5277/20 | none — **hole** |

### What the GREEN ones actually render

**M-D — the subordinate-clause guard is a proxy, and one word defeats it.** Rendered:

> RFdiffusion is a no-install online de novo binder design tool **anyone can run against a
> target uploaded from the bench you can run through** tools.ranomics.com on a dedicated GPU.

That is round 1's defect verbatim in shape ("a target you upload you can run through"),
with `anyone` substituted for `you`. **How realistic?** Very. A writer told "no second
person in the phrase" reaches for exactly this — "anyone can run", "researchers can run",
"labs can run" — and the guard's own docstring says the frame "supplies the only 'you'
the sentence is allowed to contain", which is the rule a writer would try to satisfy this
way. The relative-clause half is weaker still: the marker list is
`that / which / where / when / so that`, so **whose, who, while, after, once, because**
all pass. The guard catches the literal draft that shipped, not the class.

**M-E — every lede rule stops at the frame; `seo_long` is unguarded.** `_lede_phrase()`
slices `hero.split(_LEDE_FRAME, 1)[0]`, so all five rules see only the half **before**
"you can run through…". Rendered with M-E applied:

> RFdiffusion is a no-install online de novo binder design tool you can run through
> tools.ranomics.com on a dedicated GPU. **Free rfdiffusion runs that you can start now —
> RFdiffusion is the rfdiffusion tool you want, which is free.**

Own name twice, raw slug twice, "free" twice, second person, two relative-clause markers.
Same paragraph, same hero div, suite green. This matters more than the other holes because
**`seo_long` is where round 1's actual findings lived** — pxdesign's three unglossed
acronyms, af2's `/jobs`, proteina's false three-checks claim were all in the second half.
The tests guard the half that was never the problem.

**M-F — the count guards have the same parity hole as the glossary one.** A hardcoded `8`
renders "8 tools below do this" and stays green. So "derived, not recounted" is not
actually enforced; only "currently equal to the band" is. It is caught the moment the band
changes — which is the failure mode that bit round 1 — so this is the same
acceptable-but-real limit as M-G, and it should be disclosed the same way.

**M-H — the ipTM guard is defeated by one HTML entity.** `/help/tools/rfdiffusion` renders,
visibly:

> Individual tools set their own pass bar a little either side of that … **aim above
> roughly 0.7 on a tractable target.**

and `STALE.search(raw_body)` returns **False**, because `&nbsp;` sits where the regex wants
`\s`. The builder hardened this guard once already after it certified false on a line wrap;
`\s+` fixes the wrap and not the entity. The fix is to run the regex over unescaped,
tag-stripped text, which is what the guard's siblings in this very file already do.
**This is the same guard, failing the same way, a second time.**

**M-I — the JSON-LD template-syntax guard is scoped to one URL.** With the defect moved to
`tool_guide.html`, `/help/tools/rfdiffusion` ships

```json
"offers": "New accounts start with ${{ signup_credit() }} in their wallet."
```

into its `SoftwareApplication` JSON-LD, raw braces intact, and the suite is green — the
test only fetches `/help/faq`. Three other templates build JSON-LD from set-block dict
literals (F3). My AST scan is the check that would have generalised; the test is not.

## Claim 5 — the disclosed parity hole — **CONFIRMED, and it matters more than disclosed**

M-G reproduces it against the new guards: hardcoding the identical band stays green.
The builder's defence — drift is what matters, and drift is caught — is **correct as far
as it goes**, and I verified the drift direction is genuinely caught (M-A/M-B/M-C all go
red on change).

Where I disagree with the "acceptable" framing: **M-F shows the same hole now exists on the
count guards too**, and it was disclosed for the glossary only. Two independent
"derive, don't type" guarantees, both actually implemented as "must equal the current
value". That is fine for catching a future edit to the source of truth, and useless for
catching a reviewer who copies the rendered value into a literal — which is exactly how
"Five tools here do this" was written in the first place. Worth one sentence of
disclosure, not a redesign.

---

## Verdict: **MERGE**

**SHA reviewed: `7e2da873b87b307fd1696287b1b3433bbf99aaeb`. Base: `48b4b71`.**

All four blockers are genuinely closed, re-derived independently rather than taken on the
builder's word:

- **B1** — both counts derived, both follow a flag-gated band change on three separate
  compositions, band membership extracted from rendered HTML.
- **B2** — the false claim is gone from all four surfaces the builder names; the source is
  what the builder says it is (I read `Dockerfile.modal:229-231` and `meta.py:61-67`).
- **B3** — "aim above roughly" is at zero across 33 rendered pages; every general legend
  reads the glossary; the decision not to re-source the per-tool bars is **right**, and the
  strongest case for it is empirical — boltz2's 0.7 is `STRICT_IPTM = 0.7` in
  `run_pipeline.py`, not prose.
- **B4** — the rules that were unguarded are now guarded; the exact mutation that stayed
  green in round 1 now fails three tests by name.

Plus round 1's S1, S2 and S4, all fixed and verified on the rendered page, and S3 fixed on
three of its four items.

**Nothing factually wrong is introduced by this PR.** The two false statements still
rendering — proteina's `seo_faq` reward stack, and `about_panel`'s "roughly 0.4" — are
both **pre-existing at `48b4b71`, both untouched by this diff**, and in the proteina case
the page already contradicted itself before the PR, which cut the false copies from four
to one. Blocking a copy pass for its predecessor's backlog is not a standard I can defend.

The guard holes (M-D through M-I) are **coverage limits of new tests, not defects in
shipped copy**. Every one of them describes a way a *future* edit could regress
unnoticed; none of them is true of the page today. Fifteen new tests that catch the
defects that actually shipped are strictly better than the zero that existed.

### Follow-ups, in the order I would do them

1. **`tools/proteina/meta.py:123`** — `seo_faq[2]` still says "Each search shard filters
   candidates through an AF2 / RF3 / force-field reward stack" and it is FAQPage
   structured data. One line, in a file this PR already edits, and it contradicts
   `output_summary` on the same page. *Highest value.*
2. **M-H** — unescape and strip tags before running `STALE`. The guard has now failed
   whitespace-shaped input twice.
3. **M-E** — apply the five lede rules to `seo_long` as well as `seo_phrase`; that is where
   round 1's findings were.
4. **F1** — `about_panel.html:254` "well calibrated above roughly 0.4": source it or drop
   the number.
5. **F2** — gloss `backbone` in the homepage chooser row; `mpnn`'s blurb has the wording.
6. **M-I** — widen the JSON-LD syntax check to every page that emits JSON-LD, or keep the
   AST scan as a test.
7. **M-D / M-F** — disclose the proxy's marker list and the count parity hole; low priority.
8. Cosmetic: `proteina` is the only imperative lede of fourteen.

## What I verified empirically vs. reasoned about

**Ran:** both suite baselines and the node-id set diff (5282 vs 5297, one UUID-param
churn, zero removals); nine mutations, each `git diff --unified=0`-verified and each green
one re-rendered; all 14 tool pages plus `/`, `/tools`, `/help/faq`,
`/help/getting-started` and all 14 `/help/tools/<slug>` rendered anonymously with the
14-adapter and 14x200 assertions; band membership extracted from rendered HTML on four
different flag configurations; a jinja-AST scan of all 112 templates with a positive
control on the merge base; every ipTM number on every rendered page cross-checked against
`score_legends.py` and `run_pipeline.py`; the jargon first-use sweep on script- and
style-stripped text; `VALID_HANDOFF_TOOLS` resolved from code; every internal link on `/`
and `/tools` fetched.

**Reasoned about, not run:** everything in "Reading the 14 pages as a bench biologist" is
judgement and is labelled as such — voice consistency, the proteina-imperative call, the
blurb/lede verdict, and how realistic the M-D wording is. The claim that F1/F2 "should
have been disclosed" is a judgement about the disclosure, not about the code.

**Not covered:** signed-in rendering (the lede is anonymous-only, so the copy under review
lives on the path I drove); live GPU behaviour; whether the per-tool ipTM bars are
*scientifically* right, only that they match their own code; anything outside this diff's
files.
