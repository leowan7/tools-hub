# QC round 3 — Wave C (Phase 4d, the copy pass), PR #156

- **Reviewed SHA:** `667d73a821fcf4484aa1ece657c2b27a4e553872` (branch `copy/wave-c-phase-4d`)
- **Trunk at review time:** `origin/main` = `7fd180df35086cfc5da3710ff336024901d8e73b`
  (PR #158, scout `interface_competition` scoring, landed mid-session).
  `git fetch origin && git rev-parse origin/main` + `git rev-parse
  copy/wave-c-phase-4d` — both re-run by me at the start of this round, both
  SHAs confirmed as given.
- Round 1 reviewed `98dd20d`; round 2 reviewed `7e2da87` against `48b4b71`.
  **The branch is not rebased onto `7fd180d`**, so a head-vs-trunk count would
  be apples to oranges (the branch does not carry #158's tests). I measured a
  third tree: the actual merge.
- Worktrees, all created by me with `git worktree add --detach` under my own
  session scratchpad: `r3head` (667d73a), `r3trunk` (7fd180d), `r3merge`
  (7fd180d + 667d73a), and `r3mA`…`r3mD` for mutations. **The main working tree
  (on `fix/pin-gpu-image-digests`) was never touched**, and neither were the
  worktrees belonging to other agents in the same scratchpad.
- Harnesses live at a private path (`scratchpad/r3priv/render_r3.py`,
  `scratchpad/r3priv/mutate_r3.py`), not a shared filename.

---

## Verdict: **MERGE**

Nothing this PR ships is factually wrong, self-contradictory, or broken. All
five claims I was asked to try to break held, and the two positive controls I
wrote to catch a guard certifying false both went red.

What I found instead is **seven new coverage holes in the new guards** (§7).
Every one of them I re-rendered and confirmed **does reach a live page** — a
defect of that shape could ship with the suite green. None of them is true of
the copy today; I swept the live pages for each defect class before saying so.
The only two live inaccuracies I can name (§4c's unfalsifiable force-field
hedge; §9's unglossed `backbone` on `/help/faq`) are both **pre-existing at
`48b4b71` and untouched by this diff**, checked with `git show`.

Reasoning for MERGE rather than BLOCKED is in §10.

---

## 1. Suite, re-measured from scratch — claim CONFIRMED

Command, run from each worktree root, **no path argument**, output redirected to
a file and read whole (never piped through `tail` in a way that could hide a
failure; `grep -cE "^(FAILED|ERROR)"` run over each file as a second check):

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| tree | SHA | result |
|---|---|---|
| **current trunk** | `7fd180d` | **5273 passed, 20 skipped** in 286.47s |
| **PR head** | `667d73a` | **5281 passed, 20 skipped** in 250.13s |
| **trunk + PR, merged** | `8e7f47c` | **5292 passed, 20 skipped** in 261.76s |

All three green first try, zero flake re-runs, zero `FAILED`/`ERROR` lines.

**The +19 arithmetic, both ways:**

- against the round-2 base: `5281 − 5262 = +19` (5262 is round 2's measured
  `48b4b71`; I did not re-measure that tree, I re-measured trunk instead);
- **against current trunk, which is the number that matters**:
  `5292 − 5273 = +19`. Trunk gained +11 from #158 and the merged tree carries
  both, so the PR's contribution is +19 on a tree that did not exist when the
  builder counted.

**The merge is clean and green.** `git merge 667d73a` onto `7fd180d` produced no
conflicts and 5292/20 — so "does not conflict" is not just a textual claim, the
merged tree passes.

### Node ids, not just counts

`pytest -q --collect-only -p no:cacheprovider`, trunk **5293** vs merge **5312**.

```
only in trunk (1):  test_scout_anonymous_access.py::TestStillGated::
                    test_feasibility_get_requires_login[/scout/feasibility/download/20f89cf5-…]
only in merge (20): the same test with a different random UUID param,
                    + 19 new ids, ALL in tests/test_public_tool_pages.py
```

The 19, by class:

| class | new ids |
|---|---|
| `TestRenderedLedeRules` | 7 |
| `TestIptmThresholdHasOneSource` | 5 |
| `TestCatalogLoopStepTwoCountIsDerived` | 4 |
| `TestProteinaScoringClaimIsConsistent` | 2 |
| `TestEveryJsonLdBlockIsClean` | 1 |
| | **19** |

**Zero removed, zero renamed, skips 20 → 20.** Claim CONFIRMED exactly as
stated. (The 46 deletion lines under `tests/` since `7e2da87` are all in-place
rewrites of test bodies and docstrings — no node id disappears.)

---

## 2. The app, driven anonymously — and the page-count arithmetic

`create_app()` + `test_client()`, no session, every registered adapter flagged
on. The vacuity guard is **asserted, not eyeballed** — my harness raises unless
both hold:

```
OK: 14 adapters registered AND 14/14 tool pages at 200
REGISTERED: ['af2','bindcraft','boltz2','boltzgen','colabfold','esmfold',
             'esmfold2-design','iggm','mpnn','opendde','proteina','pxdesign',
             'rfantibody','rfdiffusion']
PAGES RENDERED: 32
```

**The builder's correction to 32 is right and round 2's 33 was an arithmetic
slip** — round 2 lists the same four extras and 14 + 14 + 4 = 32. Confirmed by
rendering and counting rather than by adding up its prose.

### …but 32 is not "every public page", and that matters for §4

Enumerating `url_map` for zero-argument GET rules and fetching each one
anonymously, **13 further pages answer 200 and are not in the 32**:

```
/developability  /help  /help/troubleshooting  /pricing  /privacy  /showcase
/scout/  /scout/example  /terms  /login  /signup  /forgot-password
/reset-password   (+ /sitemap.xml, /robots.txt)
```

`/help` renders the word "Proteina" and is **outside** the sweep in
`test_no_rendered_page_claims_the_three_model_stack`, which hardcodes the same
32 paths. I checked all 13 by hand for every claim variant and for
`above roughly <n>`: **zero hits today.** So this is a coverage limit, not a
live defect — measured as mutation **M-P3** in §7.

---

## 3. Claim 1 — round 2's four green mutations are now caught — **CONFIRMED, all four, by name**

Applied together in one worktree (`r3mA`) but on **different slugs and
different files**, so every failure's own offenders dict attributes it
unambiguously. All four verified landed with `git diff --unified=0` and
render-checked through `test_client()` **before** the run.

Full suite: **6 failed, 5275 passed, 20 skipped.**

| round-2 mutation | now fails | the offender the test names |
|---|---|---|
| **M-D** — `anyone can run` instead of `you`, on `rfdiffusion` `seo_phrase` | `TestRenderedLedeRules::test_lede_phrase_does_not_end_in_a_subordinate_clause` | `{'rfdiffusion': (["finite-verb marker ['can']", "17 words > 12: too long to be the noun phrase completing 'is a ...'"], …)}` — caught **twice over**, by the modal list and independently by the length cap |
| **M-E** — all the content rules violated in `seo_long`, on `bindcraft` | `…::test_lede_phrase_never_repeats_the_tools_own_name`, `…::test_no_lede_phrase_leaks_a_raw_slug`, `…::test_no_lede_or_title_advertises_the_run_as_free` | `{'bindcraft/seo_long': …}` on all three — the `half` key in the offender dict is what makes this readable |
| **M-H** — `aim above&nbsp;roughly 0.7` in `help/tool_guide.html` | `TestIptmThresholdHasOneSource::test_the_sourceless_threshold_is_gone_everywhere` | `{'/help/tools/af2': "… aim above roughly 0.7 on a tractable target …", …}` — 14 pages |
| **M-I** — the `{% set %}` interpolation defect moved to `tool_guide.html` | `TestEveryJsonLdBlockIsClean::test_no_page_ships_raw_template_syntax_in_json_ld` | `{'/help/tools/af2 (template syntax)': …}` — and it fails on the **raw-Jinja** assert, not on the completeness assert, so the generalisation is doing the work |

Two things worth stating beyond "it goes red":

- **M-H's fix is on the input side and that is the right fix.** The offender
  string the test prints reads `aim above roughly 0.7` with a plain space — the
  `&nbsp;` is gone *before* the regex runs, because `_visible_text` unescapes it
  to `\xa0` and `\s+` collapses it. Relaxing the pattern a third time would have
  closed the entity and nothing else; this closes line wraps, entities, tags
  mid-phrase and thin spaces for every caller in the file at once.
- **M-I's replacement is discovery-based, not a longer list.** It reads
  `templates/` off disk for `application/ld+json`, expands the app's own
  `url_map`, and **fails if a discovered JSON-LD template was never rendered**.
  That completeness assert is the part that makes "all clean" mean something.
  I broke it deliberately in §6 (M-P7) on a *fourth* template it does not name.

## 4. Claim 2 — the proteina claim, verified by rendering every page

I read the source of truth first, as instructed.

`tools/proteina/Dockerfile.modal:229-231`:

> `PROTEINA_RF3=on -> RF3 reward channel live … Only ligand_binder (RF3 is its
> sole reward) and motif_ame need it; protein_binder scores on AF2 alone, so it
> runs regardless of this switch.`

`about["output_summary"]`, rendered on `/tools/proteina`:

> "Ranked designs with reward scores (AF2 pLDDT / ipTM for protein, RF3 score
> for ligand / motif, force-field energy where applicable) … **The ligand and
> motif variants score on RF3 only.**"

So: no variant runs all three. The composite was false.

### 4a. The sweep is finally complete on the 32 pages — verified by rendering, not grepping

I rendered all 32 and searched the **visible text** of each for the claim in
every shape I could think of, not just the four the test pins:
`AF2 / RF3`, `AF2/RF3`, `AF2 or RF3`, `reward stack`, `force-field reward`,
`three independent scoring checks`, `filters candidates through`,
`all three`, `three models`, `three scoring`, `RF3 / force`.

**Zero hits on any of the 32.** I also swept the **13 further anonymously
reachable 200 pages** listed in §2 (`/help`, `/showcase`, `/pricing`,
`/developability`, `/help/troubleshooting`, `/scout/`, …): **zero hits there
too**, including on `/help`, which does render the word "Proteina".

The two surfaces this round's commits closed (a sixth and seventh, after
round 2's fifth) are:

| surface | where | status |
|---|---|---|
| `seo_faq[2]` answer | visible FAQ copy **and** FAQPage JSON-LD on `/tools/proteina` | rewritten |
| `PILOT["goal"]` — "see what the reward stack returns" | the pilot card | rewritten to "see how the designs score against it" |
| `tools/proteina/__init__.py` module docstring | not user-facing | rewritten anyway, and correctly |

### 4b. The page no longer contradicts itself — five blocks, one story

Read off `/tools/proteina` as rendered:

| block | what it says |
|---|---|
| `about["what_it_is"]` | "a protein target is scored by an AlphaFold2 refold, a small-molecule or motif target by RoseTTAFold3, with a physics force field added where it applies" |
| `seo_faq[2]` (new) | the same clause, **verbatim** |
| `seo_faq[1]` | "The protein-binder variant … is scored by AlphaFold2 confidence" |
| input glossary | "protein_binder … (AF2 reward), ligand_binder … (RF3 reward)" |
| `about["output_summary"]` | "AF2 pLDDT / ipTM for protein, RF3 score for ligand / motif … The ligand and motif variants score on RF3 only" |

All five agree. **Round 2's contradiction is gone.** The verbatim reuse of the
mapping clause is deliberate (the builder says so) and it is also what makes
the positive control in §5 have to be scoped — see there.

### 4c. Is the *new* `seo_faq[2]` wording itself accurate? — **yes on the mapping, with two caveats**

Checked clause by clause against `Dockerfile.modal:229-231`,
`reward_attributions`, and `output_summary`:

| clause | verdict |
|---|---|
| "a protein target is scored by an AlphaFold2 refold" | ✅ "protein_binder scores on AF2 alone" |
| "a small-molecule or motif target by RoseTTAFold3" | ✅ "RF3 is its sole reward … and motif_ame need it" |
| "Each shard keeps what scores well, and the hub then ranks across every shard at once and clusters the winners" | ✅ matches the old answer's global top-K + post-hoc diversity clustering, and `about["parameters"]` ("each shard … returns its survivors, and the hub picks the global top set") |

**Caveat 1 — "with a physics force field added where it applies" is an
unfalsifiable hedge, and the two "only" statements leave it nowhere to apply.**
If `protein_binder` scores on AF2 **alone** and the ligand and motif variants
score on RF3 **only**, there is no variant left for a force field. The hedge is
copied straight from `output_summary`'s pre-existing "force-field energy where
applicable", so **this PR propagates the vagueness rather than creating it** —
but it now appears in three places instead of one, including structured data.
Not false; not checkable either. Worth resolving at source rather than in copy.

**Caveat 2 — the mapping is presented as complete and is not.**
`reward_attributions` lists a fourth channel, **"ESM2 (MIT, Meta AI) — sequence
likelihood"**, and the answer's frame ("which model does that scoring follows
the target") reads as an exhaustive enumeration of two. For a bench biologist
this is the right simplification and I would not change it; it is worth knowing
it is a simplification.

Neither caveat is a blocker: both are inherited, hedged, and less wrong than
what they replaced.

---

## 5. Claim 4 — the 0.4 threshold was sourced, not removed — **CONFIRMED, number is right**

Read at source, not taken on the claim.

`shared/score_legends.py`, `("mpnn", "recovery")`:

```
"good": 0.4,
"excellent": 0.6,
"direction": "higher_is_better",
"explanation": "Recovery is the fraction of native residues recovered.
                Above 0.4 is a usable design; above 0.6 is excellent."
```

New `shared/metric_glossary.py` key:

```
"recovery": { "good_range": "> 0.4 usable; > 0.6 excellent",
              "citation": "Dauparas et al., Science 2022 (ProteinMPNN)" }
```

**Both numbers match, and the wording of `good_range` is a faithful compression
of the legend's own sentence.** `GLOSSARY` goes 15 → 16 keys; the new key
collides with nothing (`sorted(GLOSSARY)` has no other recovery-shaped entry),
and its `good_range` is the only one in the whole dict stating those two
numbers, so no other entry contradicts it. The unsourced embellishment that was
dropped — "well calibrated … on diverse folds" — was the part with no source;
the number itself always had one.

The template now reads it:

```
Higher is better: {{ metric_glossary.get('recovery', {}).get('good_range', '') }}.
```

Rendered on `/tools/af2` (and the other 13, byte-identically):

> ProteinMPNN recovery — Fraction of native residues recovered when ProteinMPNN
> redesigns a known sequence on its native backbone — **the folded shape on its
> own, before a sequence is chosen for it**. Higher is better: > 0.4 usable;
> > 0.6 excellent.

**That also closes round 2's largest disclosed jargon item as a side effect** —
`backbone` unglossed in the shared score legend was the first use on 9 of 14
tool pages, and the gloss now sits inline. Nobody claimed that; I am noting it.

### The glossary-consuming files still pass

`metric_glossary` is imported by six test files
(`test_candidate_table_js_contract`, `test_multichain_iptm_notice`,
`test_opendde_smoke`, `test_public_tool_pages`, `test_results_action_bar`,
`test_result_columns_sync`, `test_target_table_render` — seven, in fact). All
pass in the three green full-suite runs; I did not run them in isolation
because the mandated command has no path argument and an isolated run would
change collection scope.

### One thing in this commit is not copy — flagging it, not objecting to it

The same commit adds `_FORMAT["recovery"] = ".2f"`, which changes how the
recovery value **renders in results tables** from three decimals to two. That
is a behaviour change in a copy pass. I traced it for a client/server
divergence, which is the way this class of change usually bites:

- `templates/components/candidate_table.html:579` formats via
  `format_metric_value`, i.e. the Python side — consistent.
- the only client-side number formatter over result rows is
  `templates/job_detail.html:512` `fmt(v, digits)`, called at lines 548-550 for
  `iptm`, `plddt`, `i_pae` **and nothing else** — `recovery` is not in the live
  poller, so there is no JS/Python disagreement to create.
- adding a `recovery` key also makes `candidate_table.html:430`
  (`metric_glossary.get(col, {})`) render a definition and `good_range` for that
  column where it previously rendered none. That is an improvement, but it is a
  signed-in-page change shipped inside an anonymous-copy PR.

**No defect found. Worth one line in the PR description.**

---

## 6. Claim 3 — the builder's own guard was certifying false and is now real — **CONFIRMED by the mutation it needed**

The instruction was specific: mutate so the **true mapping is deleted** rather
than the false claim restored, because "states no false claim" is satisfied by
an empty answer just as well as by a correct one.

**M-P8** does exactly that. It leaves the answer in place, leaves the question
in place, and removes only the clause naming the two models:

```
- "it is generated, and which model does that scoring follows the "
- "target: a protein target is scored by an AlphaFold2 refold, a "
- "small-molecule or motif target by RoseTTAFold3, with a physics "
- "force field added where it applies. Each shard keeps what "
+ "it is generated. Each shard keeps what "
```

Landed (`git diff --unified=0`, non-empty), rendered (the shortened answer is on
`/tools/proteina`; I read it back off the page). Full suite:

```
FAILED  TestProteinaScoringClaimIsConsistent::test_the_scoring_answer_says_which_model_scores_which_target
E  AssertionError: proteina's scoring answer no longer says which model scores
   which target (missing ['AlphaFold2', 'RoseTTAFold3']): 'Every candidate is
   re-folded and scored against your target as it is generated. Each shard ...'
```

**The fix is real, and I checked the specific reason it used to be fake.** With
M-P8 applied the page STILL contains both model names in two other places —
`seo_faq[1]` ("The protein-binder variant ... is scored by AlphaFold2
confidence") and `about["what_it_is"]` (the same mapping clause, verbatim) —
and the test fails anyway. Resolving the answer **by its question**
(`re.search(r"scored and ranked", q)`) plus `assert len(keys) == 1` is what
stops those two answering for it. That is the right shape of fix: not a longer
string, a narrower subject.

### Is the same "passing off a neighbouring string" pattern in the other new tests?

I looked at each. **Two are clean, three carry a weaker version of it, and one
of those is exploitable.**

| test | verdict |
|---|---|
| `test_no_rendered_page_claims_the_three_model_stack` | Its vacuity guard is `assert seen_proteina` over **all 32 pages**, and "Proteina" renders on `/`, `/tools` and `/help/tools/proteina` too. If `/tools/proteina` stopped rendering its FAQ entirely the guard would still be satisfied — by a catalog card. **Weaker than it looks.** Mitigated: the sweep asserts 200 on every path, and M-P8's test covers that exact block. |
| `TestEveryJsonLdBlockIsClean` | `assert len(blocks) >= 14` can be satisfied **entirely by `base.html`'s Organization block**, which renders on every page — so "14 pages carried a JSON-LD block" is not evidence that any FAQPage or SoftwareApplication block was exercised. The template-completeness assert (`expected - covered`) is the one doing the real work; the `>= 14` line is the neighbouring-string one. |
| `test_the_general_metric_legend_states_no_unsourced_number` | **Same pattern, and exploitable — M-P9 below.** `sourced` is the union of every number in every glossary `good_range`, so a threshold stated for one metric passes on a *different* metric's number. |
| `test_the_sourceless_threshold_is_gone_everywhere` | Clean — a pure negative over visible text, with its positive control living in `test_every_general_legend_reads_the_glossary`. |
| `TestCatalogLoopStepTwoCountIsDerived` | Clean. Both count tests carry an explicit `assert len(members) >= 2` non-vacuity guard, and I re-verified the rendered output: `8` on both surfaces, zero spelled-out counts on `/` and `/tools`, zero broken internal links on either page. |

---

## 7. My own attack — 10 new mutations, plus the 4 reproductions

Every one applied through a private harness that **refuses on a non-unique
anchor** (`apply()` raises unless the anchor occurs exactly once — it fired on
my first attempt at `rfdiffusion`, whose `seo_phrase` string is byte-identical
to `bindcraft`'s) and **refuses on an empty `git diff --unified=0`**. Every
mutation was diff-verified before any suite ran, and **every green one was
re-rendered through `test_client()` to prove the defect reaches a page**.

Batching: mutations sharing a worktree sit in **different files or on different
tool slugs**, so each failing test's own offenders dict attributes it. Where a
batch was expected green, a green result with several defects live is a
*stronger* claim than several separate greens, not a weaker one.

| # | mutation | landed | result | failing test / does it reach a page? |
|---|---|---|---|---|
| **M-D** ↺ | round 2's `anyone can run ...` on `rfdiffusion` `seo_phrase` | yes | **RED** | `test_lede_phrase_does_not_end_in_a_subordinate_clause` |
| **M-E** ↺ | round 2's rule violations moved into `seo_long`, on `bindcraft` | yes | **RED** | `...never_repeats_the_tools_own_name`, `...leaks_a_raw_slug`, `...advertises_the_run_as_free` |
| **M-H** ↺ | `aim above&nbsp;roughly 0.7` restored to `tool_guide.html` | yes | **RED** | `test_the_sourceless_threshold_is_gone_everywhere` |
| **M-I** ↺ | the `{% set %}` interpolation defect in `tool_guide.html` | yes | **RED** | `test_no_page_ships_raw_template_syntax_in_json_ld` |
| **M-P1** ★ | the reward-stack claim **paraphrased** past all four `CLAIMS` regexes | yes | **GREEN** 5281/20 | none — **HOLE**, visible on `/tools/proteina` |
| **M-P2** ★ | the claim planted in `/help/faq`'s `faq_items`, i.e. **JSON-LD only** | yes | **GREEN** 5281/20 | none — **HOLE**, in the FAQPage block, invisible to every visible-text guard |
| **M-P3** ★ | the claim planted on **`/help`**, a public 200 page outside the 32 | yes | **GREEN** 5281/20 | none — **HOLE**, visible on `/help` |
| **M-P4** ★ | unsourced legend threshold with **no decimal and no "roughly"**: "well calibrated above 40% on diverse folds" | yes | **GREEN** 5281/20 | none — **HOLE**, on all 14 tool pages |
| **M-P9** ★ | legend threshold whose number is **sourced from a different metric**: "above 0.75" on **pLDDT**, a 0-100 metric | yes | **GREEN** 5281/20 | none — **HOLE**, on all 14 tool pages |
| **M-P5** ★ | clause marker escaped by an **em dash**: `binder design tool—which labs run daily` | yes | **GREEN** 5281/20 | none — **HOLE**, renders the collision shape |
| **M-P6** ★ | a **non-finite infinitive** clause at exactly the 12-word cap | yes | **GREEN** 5281/20 | none — **HOLE**, renders round 1's defect A nearly verbatim |
| **M-N** | the builder's disclosed ceiling, replayed on `mpnn`: `binder design tool labs run daily` | yes | **GREEN** 5281/20 | none — **disclosure CONFIRMED honest** |
| **M-P7** ★ | raw Jinja in **`showcase.html`**'s `ItemList` JSON-LD — a fourth template, on a page outside the 32 | yes | **RED** | `test_no_page_ships_raw_template_syntax_in_json_ld`, offender `/showcase` |
| **M-P8** ★ | **delete** proteina's true model mapping instead of restoring the false claim | yes | **RED** | `test_the_scoring_answer_says_which_model_scores_which_target` |

↺ = round-2 reproduction. ★ = mine, not tried by any prior round.

Worktrees: `r3mA` = the four reproductions (6 failed, 5275 passed, 20 skipped);
`r3mB` = M-P1/M-P2/M-P3 (5281/20 green); `r3mC` = M-P4/M-P5/M-P6/M-P9/M-N
(5281/20 green, **five live defects and a green suite**); `r3mD` = M-P7/M-P8
(2 failed, 5279 passed, 20 skipped).

### What the GREEN ones actually render

**M-P1 — the claim sweep is four literal regexes, and a paraphrase walks past
all four.** `/tools/proteina` renders, visibly, in the FAQ block:

> **Each search shard filters every candidate through all three scoring models
> — an AlphaFold2 refold, a RoseTTAFold3 score and a physics force field —
> before the hub ranks across shards.** Every candidate is re-folded and scored
> against your target as it is generated, and which model does that scoring
> follows the target: ...

That is the same false composite the four-round sweep exists to kill, and it
contradicts `output_summary` on the same page. `CLAIMS` pins `AF2\s*/\s*RF3`,
`three independent scoring checks`, `filters candidates through`,
`force[-\s]field reward stack`; I wrote "filters **every candidate** through",
spelled the models out, and said "all three scoring models". **How realistic?**
This is exactly how a writer *fixes* a flagged phrase — keep the meaning, change
the words. The test is a regression-lock on four strings, not a check of the
claim. That is a reasonable thing for it to be; it should be described that way.

**M-P2 — `_visible_text` drops `<script>` bodies whole, so JSON-LD is
unswept.** `/help/faq` builds its FAQPage from `{% set faq_items = [...] %}` at
the top of the template and renders its **visible** copy from separate
hardcoded `<dd>` blocks ~90 lines below. They are two independent copies of the
same answers. With M-P2 applied the page ships

```json
{"@type": "Question", "name": "How are designs scored?",
 "acceptedAnswer": {"@type": "Answer",
   "text": "Each search shard filters candidates through an AF2 / RF3 / force-field reward stack."}}
```

green suite, nothing visible on the page. **This is the sharpest of the five.**
The new proteina test's own docstring says the fifth surface mattered *because*
it rendered "visibly AND inside FAQPage JSON-LD" — and the guard it produced
reads visible text only. The visible/structured split in `faq.html` is a
standing divergence hazard quite apart from proteina.

**M-P3 — the sweep hardcodes 32 paths; 13 more public pages answer 200.**
`/help` renders "Proteina filters candidates through an AF2 / RF3 /
force-field reward stack" in its subtitle, suite green.
`TestEveryJsonLdBlockIsClean`, written in the same commit, discovers its pages
from `url_map`; the proteina sweep does not. Reusing that discovery would close
this for free.

**M-P4 — a threshold with no decimal and no "roughly" passes both legend
guards.** All 14 tool pages render

> ProteinMPNN recovery — ... Higher is better, **and well calibrated above 40%
> on diverse folds.**

`STALE` (`\babove\s+roughly\s+\d*\.?\d+`) needs the word "roughly";
`test_the_general_metric_legend_states_no_unsourced_number` scans for
`\d+\.\d+`, and a percentage has no decimal point. This is round 2's F1
restored in the one wording that evades both the phrase guard **and** the
structural guard written so phrase-guessing would not be needed.

**M-P9 — the structural guard sources numbers from the whole glossary, not from
the metric being described.** All 14 tool pages render, in the pLDDT entry:

> pLDDT — Per-residue confidence in the predicted fold. ... **treat anything
> above 0.75 as reliable.**

`0.75` is in `sourced` because **ipTM**'s `good_range` contains it. pLDDT in the
same glossary is a 0-100 scale (`> 80 very high confidence`), so "above 0.75"
is not merely unsourced, it is **nonsense**, and it passes. One-line fix: scan
per-`<dt>` and compare against that metric's own `good_range`.

**M-P5 — the clause-marker check is space-delimited, and this codebase's copy is
full of em dashes.** `/tools/rfdiffusion` renders:

> RFdiffusion is a **binder design tool—which labs run daily** you can run
> through tools.ranomics.com on a dedicated GPU.

`CLAUSE_MARKERS` is tested as `f" {m} " in padded`, so `tool—which` puts no
space before `which` and no marker fires; "labs" and "run" are in neither closed
class; six words is far under the cap. **This is a marker the list already
contains, defeated by punctuation** — a different failure from M-N, which has no
marker at all. `&mdash;` appears throughout these very strings (proteina's own
lede uses one), so a writer producing this is entirely plausible.

**M-P6 — a non-finite verb is in neither closed class, and 12 words is
allowed.** `/tools/bindcraft` renders:

> BindCraft is a **binder design tool to run against a target uploaded from the
> bench** you can run through tools.ranomics.com on a dedicated GPU.

"...uploaded from the bench you can run through..." is round 1's defect A in
shape, almost word for word. `to run` is an infinitive so no modal fires, there
is no clause marker, and the phrase is **exactly 12 words**, which is
`not > 12`. The cap is the only thing between this and the page, and this lands
on its boundary.

**M-N — the builder's disclosure is honest.** `/tools/mpnn` renders:

> ProteinMPNN is a **binder design tool labs run daily** you can run through
> tools.ranomics.com on a dedicated GPU.

Green, and the bad shape genuinely renders. **Verified as stated.** Two notes.
The disclosure says the ceiling is "narrower than the hole QC found" — M-P5 and
M-P6 say it is **wider than one shape**: it is "any verb whose surface form is
not in a 20-word modal list, and any marker not delimited by spaces". Second,
M-N's string is also *false* copy (ProteinMPNN is not a binder design tool) —
that is my mutation's doing, not the PR's, and no guard claims to check it.

### Normalisation gaps in `_visible_text`, tested and reasoned

`_visible_text` is a genuine improvement and it closed the entity hole (M-H is
now red). Two gaps remain, both **reasoned about rather than run**, since M-P2
already demonstrates the class:

- **`<script>` and `<style>` bodies are dropped whole**, which is correct for CSS
  and JS but takes **every JSON-LD block** with them. Every guard built on
  `_visible_text` is therefore blind to structured data. M-P2 is the runnable
  proof.
- **Text living in attributes is invisible**: `title=`, `alt=`, `placeholder=`,
  `aria-label=`. A reader sees a tooltip and a screen reader reads an
  `aria-label`; `re.sub(r"<[^>]+>", " ", ...)` deletes both. No copy under
  review lives there today (I checked the form templates), so this is a latent
  gap, not a finding.
- Zero-width characters (`​`) are **not** matched by `\s`, so they survive
  the collapse — but they cannot hide a decimal from the structural guard, and
  M-P4/M-P9 already defeat that guard more cheaply, so I did not spend a run on
  it. Stated as reasoned, not measured.

---

## 8. Claim 5 — the subordinate-clause guard's disclosed ceiling — **CONFIRMED, and there are at least two more escapes**

M-N is **genuinely green and genuinely renders the bad shape** (§7). The
disclosure is honest and I would not have found the ceiling faster than the
docstring states it.

Asked to find a *second* escape of a different shape, I found two, both green,
both rendering the collision the rule exists to prevent:

| escape | shape | why it slips |
|---|---|---|
| **M-N** (disclosed) | bare-present finite verb, non-pronoun subject: `tool labs run daily` | no word in a closed class; short enough for the cap |
| **M-P5** (new) | a **real** clause marker, punctuation-escaped: `tool—which labs run daily` | `CLAUSE_MARKERS` is matched as `f" {m} " in padded`, and an em dash leaves no space before the marker |
| **M-P6** (new) | **non-finite** infinitive: `tool to run against a target uploaded from the bench` | infinitives are in neither closed class; 12 words is `not > 12` |

M-P5 is the more troubling of the two, because the guard's marker list
**already contains "which"** — the defect is in how membership is tested, not
in the vocabulary. `padded = f" {phrase.lower()} "` with `f" {m} " in padded`
cannot see a marker adjacent to any punctuation, and `&mdash;` is the house
punctuation mark in these strings.

Both are one-line fixes to the *matching*, not the word lists:
`re.search(rf"\b{m}\b", phrase, re.I)` closes M-P5 outright, and lowering
`MAX_PHRASE_WORDS` from 12 to 8 closes M-P6 with margin (the longest real phrase
is 7 words, per the constant's own comment). Neither closes M-N, which does need
a parser — and I agree with the builder that a parser is not worth a dependency
for fourteen strings.

**None of this is true of the copy today.** All fourteen phrases pass all four
checks on the shipped strings; these are coverage limits on a proxy, correctly
labelled as a proxy in the code.

---

## 9. Reading the 14 pages as a bench biologist

Everything in this section is **judgement**, on rendered pages, and is labelled
as such. It is not a basis for BLOCKED.

### Voice — consistent, and proteina's outlier is now resolved to a nit

All 14 blurbs open with an imperative naming what the reader has:
*Paste* × 4, *Upload* × 8, *Choose* × 1, *Describe* × 1. No blurb opens with a
model name. The style rule "lead with what the user has and what they get" holds
on all fourteen.

**Round 2's one outlier is fixed.** Proteina's lede went from the only
imperative — "Upload a target the usual design tools struggle with", which also
stuttered against its own blurb's "Upload a protein or small-molecule target" —
to:

> **Built for the targets the standard design tools stall on** — a recessed
> pocket, a site spanning two chains, or a small molecule rather than a protein:
> every candidate the search generates is re-folded against your target before
> the search builds on it.

No imperative, no stutter, same content. **It is still the only one of its
grammatical shape** — a past-participial fragment, where the other thirteen are
noun phrases ("The reference-standard fold…", "The cheap second opinion…") or
declaratives ("One model covers four binder formats…", "Nanobodies are small
enough to…"). That is a much smaller outlier than an imperative and I would
leave it.

**The remaining outlier I would name is `pxdesign`, and it is redundancy rather
than voice.** Its blurb already says "get back binders that each carry a real
AlphaFold2 confidence score against that target", and its lede then says "every
candidate comes back already re-folded against your target, carrying its own
confidence score for the contact rather than a number borrowed from the
generator". The lede's only added argument — the score is measured, not
self-reported — is the same argument `rfdiffusion`'s lede makes, and pxdesign's
blurb has already made it. That is round 1's S5 shape, surviving on one page.
Minor, and a judgement call.

`opendde` stays the register outlier round 2 named: "first-class parts of the
input", plus "Describe any mix … **in one spec**". Both are software vocabulary
in copy aimed at a bench biologist. Untouched by this PR.

### Overclaims — none found

Checked, not assumed:

- **"the number of designs is unlimited … your wallet balance is the only
  ceiling"** (`/` hero and `/help/faq`) reconciles with every per-tool string I
  could find: `bindcraft`, `pxdesign`, `rfantibody`, `rfdiffusion` all render
  "No fixed ceiling: large requests fan out automatically as a wallet-bounded
  campaign across GPUs". No page states a numeric max design count.
- **"Every tool below is free to open and read before you spend anything"**
  matches what I actually did — 32 pages at 200 with no session.
- **`8 different design tools`** / **`8 tools below do this`** both render 8 and
  both match the rendered band; zero spelled-out counts on `/` or `/tools`;
  round 2 already proved they follow a flag-gated band change on three
  compositions.
- **Zero broken internal links** on `/` or `/tools` (every `href` fetched).
- The proteina scoring copy is accurate on the mapping, with the two caveats in
  §4c (an unfalsifiable force-field hedge, and an enumeration that omits ESM2).
  Both are inherited from `output_summary` / `reward_attributions`.

### Jargon — the known-open list is **still incomplete, by one**

`backbone` first use, computed over all 32 rendered pages:

| page | first use | glossed? |
|---|---|---|
| `/` (chooser row) | "A backbone from somewhere else — the folded shape on its own, before a sequence is chosen for it" | ✅ **round 2's F2 is CLOSED** |
| `/tools` step 3 | same gloss, verbatim | ✅ |
| 10 of 14 `/tools/<slug>` | the recovery legend, now "…on its native backbone — the folded shape on its own, before a sequence is chosen for it" | ✅ **round 2's largest disclosed item is CLOSED**, as a side effect of the §5 fix |
| `/tools/mpnn`, `/help/tools/mpnn`, `/tools/rfdiffusion`, `/help/tools/rfdiffusion` | glossed in the blurb / `what_it_is` before the legend | ✅ |
| **`/help/faq`** | **"A design is one candidate the tool generates, a backbone with a sequence"** | ❌ **UNGLOSSED, and on nobody's list** |
| `/tools/esmfold2-design`, `/tools/iggm`, `/tools/opendde` (+ their guides) | "Framework backbone and sequence are fixed" / "given the complex backbone" / "not just the protein backbone" | ❌ but **ordinary sense** — main chain, not "shape with no sequence". A bench biologist knows this one. Not the same defect. |

So the answer to "is the list now complete" is **no — `/help/faq` is the item it
is short by**, and it is the *only* remaining unglossed first use in the
design sense. It is **pre-existing at `48b4b71` and untouched by this diff**
(`git show 48b4b71:templates/help/faq.html` line 33 is byte-identical to head's
line 47). This is the third round in which the `backbone` disclosure has been
short by exactly one location, and each time the missed location was a page that
is not a tool page. That is a pattern in how the sweep is scoped, not a new
defect.

Two more terms not on any list, both judgement calls, both pre-existing:

- **`cofold`** is unglossed on `/tools/boltz2`, in the `<title>` ("Cofold
  Validation") and the lede ("a no-install online cofold validation tool"). The
  blurb immediately above describes the operation ("Paste a designed binder,
  upload the target it should hit, and get back the predicted complex"), so the
  mechanic is explained before the word in reading order. The `<title>` is not.
- **`epitope`** is unglossed on `/`, in row 2 of the chooser ("A target and a
  rough epitope") — the same table that now glosses `backbone` in row 3. For
  this audience `epitope` is far more common vocabulary than `backbone`, so I
  would leave it; the inconsistency within one table is what makes it worth a
  sentence.

The rest of round 2's disclosed list re-verified: `MSA` (af2, boltz2, colabfold,
esmfold, mpnn), `recycles` (af2, colabfold, opendde), `CDR` (esmfold2-design,
iggm, rfantibody — **and also `/` and `/tools`, via
`shared/tools_catalog.py`'s Developability Scout one-liner, which the list does
not name and which this PR does not touch**), `ipTM` in field help.
`contig` is at **zero across all 32 pages**. `scFv`, `VHH` and `nanobody` are
glossed on first use in prose everywhere they appear.

---

## 10. Verdict: **MERGE**

**SHA reviewed: `667d73a821fcf4484aa1ece657c2b27a4e553872`. Trunk: `7fd180d`.
Merged tree measured at `8e7f47c`: 5292 passed, 20 skipped, +19 over trunk's
5273/20, zero node ids removed or renamed.**

Everything I was asked to try to break held:

1. **Round 2's four green mutations are all red now**, each by name, each with
   the offender the test prints. M-H's fix is on the input side rather than a
   third loosening of the regex, and M-I's is a discovery over `templates/` and
   `url_map` rather than a longer list of URLs. Both are the right shape.
2. **The proteina claim is gone from all 32 pages and from the 13 further
   public pages nobody has been checking**, verified by rendering and searching
   for paraphrases, not by grep. The page now tells one consistent story across
   five blocks, and round 2's self-contradiction is closed. The new wording is
   accurate on the mapping; two inherited soft spots are named in §4c.
3. **The builder's own guard is no longer certifying false.** Deleting the true
   mapping — with both model names still present twice elsewhere on the same
   page — goes red. That is the mutation that distinguishes a real positive
   control from a string match, and it fires.
4. **The 0.4 is right at source** (`score_legends` `good=0.4, excellent=0.6`),
   the new glossary key contradicts nothing, and the template reads it. It also
   closed the largest disclosed jargon item as a side effect.
5. **The disclosed ceiling on the clause guard is honest**, and it is wider than
   disclosed by two more shapes (§8).

### Why not BLOCKED

BLOCKED is reserved for something factually wrong, contradictory, or broken.
I found none of those **in what this PR ships**:

- Every green mutation of mine describes a way a **future** edit could regress
  unnoticed. I re-rendered each one and confirmed the defect reaches a page —
  that is what makes them holes rather than noise — but **not one of them is
  true of the copy today**. I swept the live pages for each defect class myself
  before concluding that.
- The two live inaccuracies I can name (§4c's force-field hedge; §9's
  `/help/faq` `backbone`) are both **pre-existing at `48b4b71` and untouched by
  this diff**, and I checked that with `git show` rather than assuming it.
- The one non-copy change riding along (`_FORMAT["recovery"]`) I traced for a
  client/server divergence and found none.

Fourteen new tests that catch the defects that actually shipped, plus five that
catch what round 2 found, are strictly better than the zero that existed. A copy
pass should not be blocked for its predecessor's backlog, and it should not be
blocked because its new guards are proxies that a determined adversary can walk
around — provided the proxies are labelled as proxies, which these are, and
provided the walkarounds are written down, which is what §7 and §8 are for.

### Follow-ups, in the order I would do them

1. **M-P2 — sweep JSON-LD, not just visible text.** `_visible_text` deletes
   every `<script>` body, so no guard in this file can see structured data.
   `/help/faq` keeps two independent copies of its answers (a `{% set %}` list
   for the FAQPage and hardcoded `<dd>`s for the page), which is a standing
   divergence hazard. *Highest value: it is the exact surface the proteina fix
   was raised about.*
2. **M-P9 — source legend numbers per metric, not from the union.** "above 0.75"
   on pLDDT currently passes because ipTM's band contains 0.75. This one lets a
   **nonsense** threshold onto 14 pages, not merely an unsourced one.
3. **M-P3 — give the proteina sweep the same `url_map` discovery
   `TestEveryJsonLdBlockIsClean` already has.** 13 public pages are outside its
   hardcoded 32, and `/help` mentions Proteina.
4. **M-P5 — match clause markers with `\b`, not with spaces.** The list already
   contains "which"; an em dash defeats it, and em dashes are everywhere in
   this copy.
5. **M-P4 — the phrase guard still needs the word "roughly" and a decimal
   point.** A percentage evades both it and the structural scan.
6. **M-P6 — `MAX_PHRASE_WORDS` at 12 with a longest-real-phrase of 7.** Eight
   would keep the margin and close the infinitive shape.
7. **M-P1 — say in the docstring that `CLAIMS` is a regression-lock on four
   strings, not a claim check.** A paraphrase walks past all four; that is fine,
   but it should not read as more than it is.
8. **`/help/faq`'s `backbone`** — one gloss, wording already exists twice.
9. Cosmetic: `pxdesign`'s lede restates its blurb's argument; `cofold` unglossed
   in `/tools/boltz2`'s `<title>`; `epitope` unglossed in the homepage chooser
   row that now glosses `backbone`.

### What I verified empirically vs. reasoned about

**Ran:** three full-suite baselines (trunk, head, and the real merge) plus four
mutation suites, all with the mandated command and no path argument, output
captured to files and read whole; node-id set diff on trunk vs merge; 14
mutations, each anchor-checked, each `git diff --unified=0`-verified before the
run, each green one re-rendered through `test_client()`; all 32 public pages
rendered anonymously with the 14-adapter and 14×200 assertions **asserted, not
eyeballed**; the 13 further public 200 pages enumerated from `url_map` and swept
for every claim variant and for `above roughly <n>`; the `backbone` first-use
table across all 32 pages; the derived counts and the spelled-out-count sweep;
every internal `href` on `/` and `/tools` fetched; `score_legends` and
`metric_glossary` read at source; the `_FORMAT["recovery"]` change traced to
both its server-side and client-side consumers.

**Reasoned about, not run:** everything in §9 (voice, register, which jargon a
bench biologist actually knows) is judgement and is labelled so; the two
`_visible_text` gaps at the end of §7 (attribute text, zero-width characters)
are reasoned — M-P2 is the runnable proof of the class, and the other two would
not have added a finding; the "neighbouring string" analysis of
`TestEveryJsonLdBlockIsClean`'s `>= 14` assert and the proteina sweep's
`seen_proteina` guard is code reading, not a mutation.

**Not covered:** signed-in rendering except where `_FORMAT` forced me to trace
it; live GPU behaviour; whether the per-tool ipTM bars are scientifically right
(round 2 established they match their own code); anything outside this diff's
files; and the six other test files that import `metric_glossary` were exercised
only as part of the full suite, never in isolation.
