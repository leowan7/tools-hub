# QC round 4 — Wave C (Phase 4d, the copy pass), PR #156

- **SHA reviewed:** `fa1954f89ff09290be05b23c068e8d83eb1b5315` (branch
  `copy/wave-c-phase-4d`).
- **Scope: the final delta `09bd7a4..fa1954f` only** — one commit, two files
  (`tests/test_public_tool_pages.py`, `templates/help/faq.html`). Rounds 1-3
  cleared everything before it; I did not re-review the whole PR.
- **Trunk:** `origin/main` = `7fd180df35086cfc5da3710ff336024901d8e73b`. Both
  SHAs re-derived by me (`git fetch origin && git rev-parse`), not taken on the
  brief. Note the **local** `copy/wave-c-phase-4d` ref lagged at `09bd7a4`;
  `fa1954f` is on `origin/`.
- **The branch is not rebased**, so head-vs-trunk is apples to oranges. I
  measured trunk and the actual merge, both from scratch.
- Worktrees, all created by me with `git worktree add` under my own session
  scratchpad and named `wcqc4-*`: `wcqc4-trunk` (7fd180d), `wcqc4-merge`
  (7fd180d + fa1954f), `wcqc4-mut` (same merge, for mutations), `wcqc4-doc`
  (this file). **The main working tree — on `fix/pin-gpu-image-digests` — was
  never touched**, and neither were the worktrees belonging to other agents in
  the same scratchpad.
- Harness at a **private** path (`scratchpad/wcqc4priv/`): `mutate.py`,
  `probe.py`, `probe2.py`, `probe3.py`, `batch0.py`, `batch1.py`, `batch2.py`.
  Nothing shared with another agent.

---

## Verdict: **MERGE**

Nothing in this delta is factually wrong about the product, self-contradictory
or broken. Everything it claims to close, it closes: **all seven of round 3's
holes fail by name**, M-N is still green and still renders its bad shape, the
counts are exactly as stated, and the `backbone` gloss lands in both copies with
the wording it says it lifted.

What I found is one **over-claim in a docstring** and five new coverage holes,
all in the same class round 3 reported and none true of the copy today. The
over-claim is the sharpest item and is a one-line wording fix, not a code
change — see §3. Reasoning for MERGE rather than BLOCKED is in §11.

---

## 1. Baselines, measured from scratch — every count CONFIRMED

Command, run from each worktree root with **no path argument**, output
redirected to a file and read whole; `grep -cE "^(FAILED|ERROR)"` run over each
file as a second check.

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| tree | SHA | result | FAILED/ERROR lines |
|---|---|---|---|
| **trunk** | `7fd180d` | **5273 passed, 20 skipped** in 235.65s | 0 |
| **trunk + PR, merged** | `2ab94e5` | **5293 passed, 20 skipped** in 302.14s | 0 |

`git merge 7fd180d` onto `fa1954f` produced **no conflicts**, and the merged
tree is green — "merges clean" is not just textual here.

**+20 exactly: 5293 − 5273 = 20.**

### Node ids, not just counts

`pytest -q --collect-only -p no:cacheprovider`, parametrisation stripped, sorted
and `comm`-diffed:

```
trunk  5293 collected   (= 5273 passed + 20 skipped)
merge  5313 collected   (= 5293 passed + 20 skipped)

only in trunk:  (none)
only in merge:  20 ids, ALL in tests/test_public_tool_pages.py
```

**Zero removed, zero renamed, skips 20 → 20.** The 20 are round 3's 19 plus one
new id, `TestProteinaScoringClaimIsConsistent::test_every_multi_model_sentence
_states_the_mapping`. Claim CONFIRMED exactly as stated, including "one new
test".

---

## 2. All seven of round 3's holes — **ALL SEVEN CLOSED, each by name**

Applied together in one worktree, on **different files and different tool
slugs** so each failing test's own offenders dict attributes it unambiguously.
Every mutation diff-verified with `git diff --unified=0` before the run; the
harness refuses on a non-unique anchor (it fired once, on an
`about_panel.html` anchor I had mis-transcribed) and on an empty diff.

Full suite: **4 failed, 5289 passed, 20 skipped.**

| round-3 hole | landed | now fails | the offender the test names |
|---|---|---|---|
| **M-P1** paraphrase past all four `CLAIMS` regexes | yes | `TestProteinaScoringClaimIsConsistent::test_no_rendered_page_claims_the_three_model_stack` | `"/tools/proteina (visible) ~ ['AF2', 'RF3'] without ['a protein target', 'a ligand/motif target']"` — and the same key for `(json-ld)`. **Caught by the new mapping rule, matching no `CLAIMS` literal.** That is the decisive detail: the fix is the invariant, not a fifth string. |
| **M-P2** claim in `/help/faq`'s `faq_items`, JSON-LD only | yes | same test | `'/help/faq (json-ld) ~ AF2\s*/\s*RF3'` — the `json-ld` view is doing the work; nothing visible on the page. |
| **M-P3** claim on `/help`, outside the old 32 | yes | same test | `'/help (visible) ~ AF2\s*/\s*RF3'` + two more literals — the discovered page. |
| **M-P4** `well calibrated above 40% on diverse folds` | yes | `TestIptmThresholdHasOneSource::test_the_general_metric_legend_states_no_unsourced_number` | `'af2 ~ ProteinMPNN recovery': "['40%'] not in ['recovery']'s own good_range ['0.4', '0.6']"` — on all 14 tools. |
| **M-P9** `above 0.75` on pLDDT | yes | same test | `'af2 ~ pLDDT': "['0.75'] not in ['pLDDT']'s own good_range []"` — **per-metric, and the empty set is the point**: pLDDT's band is `> 80 …; 60–80 …`, all bare integers, so `_numbers` yields `∅` and *any* decimal in that entry is unsourced. |
| **M-P5** em dash before a clause marker | yes | `TestRenderedLedeRules::test_lede_phrase_carries_no_relative_clause_marker` | `{'rfdiffusion': (['which'], 'binder design tool&mdash;which labs run daily')}` — `_marker_haystack` normalises the entity away before matching. |
| **M-P6** 12-word non-finite infinitive | yes | `TestRenderedLedeRules::test_lede_phrase_does_not_end_in_a_subordinate_clause` | `{'bindcraft': (["12 words > 8: too long to be the noun phrase completing 'is a ...'"], …)}` — the cap moved 12 → 8 and the mutation now sits four words over. |

### M-N — still green, and **still renders the bad shape**

Applied in the same batch on `mpnn`. `mpnn` appears in **no** offenders dict in
any of the four failures, and the page renders, read back through
`test_client()`:

> ProteinMPNN is a **binder design tool labs run daily** you can run through
> tools.ranomics.com on a dedicated GPU. *(`/tools/mpnn`, 200)*

**The disclosure is still honest.** A silently-passing M-N would have meant the
ceiling had been closed without the note being deleted; it has not been.

---

## 3. The mapping rule — right shape, but its docstring **over-claims soundness**

The rule: for every sentence in every view of every discovered page, if it names
**both** `AlphaFold2` and `RoseTTAFold3`, it must also name a protein target
(`\bprotein\b`) and a ligand/motif target
(`\bligands?\b|\bsmall[-\s]molecule\b|\bmotifs?\b`).

### 3a. It does not false-red on the live copy — CONFIRMED empirically

I swept all 50 discovered public pages, both views, through the class's own
`_claim_offenders`. **`offenders == {}`.** Multi-model sentences, counted by me:

| where | count |
|---|---|
| `/tools/proteina` (visible) | 4 |
| `/tools/proteina` (json-ld) | 1 |
| `/help/tools/proteina` (visible) | 3 |
| **total (path, view, sentence) hits** | **8** (6 unique strings) |

**The claim says 10; I measure 8.** The substantive half — *all of them pass* —
is confirmed. The count is off by two and I cannot reconstruct the 10 (it is not
the unique-string count either, which is 6). Not a defect; a number in prose
that does not match the tree.

### 3b. The eight inline controls genuinely exercise the matcher — CONFIRMED

The specific bug asked about ("passing off a neighbouring string") is **not
present**. Each control calls `self._claim_offenders("t", {"v": sentence})`
directly on a single sentence — there is no page, no other block, nothing else
that could answer for it. Per-control, decomposed:

| control | names both models? | enters the mapping branch? | matches a `CLAIMS` literal? | result |
|---|---|---|---|---|
| TRUE 1 (the live `seo_faq[2]` clause) | yes | **yes** | no | passes |
| TRUE 2 (the live `output_summary`) | yes | **yes** | no | passes |
| TRUE 3 (the live `seo_faq[1]`) | yes | **yes** | no | passes |
| TRUE 4 (single model) | no | no | no | passes |
| FALSE 1 (the string that shipped) | yes | yes | **yes** (3 of 4) | flagged |
| FALSE 2 (round 3's M-P1) | yes | yes | no | **flagged by the mapping rule alone** |
| FALSE 3 | yes | yes | no | **flagged by the mapping rule alone** |
| FALSE 4 | yes | yes | no | **flagged by the mapping rule alone** |

Three of four TRUE controls enter the branch and survive it (so they are not
passing by never being tested), and three of four FALSE controls are caught with
no `CLAIMS` literal in sight. FALSE 1 is double-covered, which is fine — it also
fails the mapping rule on its own.

### 3c. **The docstring's categorical claim is false.** — the headline finding

> "The false composite never can [name both sides], because a stack has no
> target types to name — and it does not matter how it is worded."

It can, and naming both target types is the *natural* way to word it, because
"whatever your target, all of them run" is exactly what the false claim asserts.
Four counterexamples, all run through `_claim_offenders`, **all clean**:

| false sentence | flagged? |
|---|---|
| "Every candidate — whether the target is a protein or a small molecule — is put through AlphaFold2, RoseTTAFold3 and a physics force field together." | **no** |
| "For a protein or motif target alike, each design is run through both AlphaFold2 and RoseTTAFold3 as well as the force field before ranking." | **no** |
| "AlphaFold2 and RoseTTAFold3 are both applied to every protein design, as is the ligand-aware force field." | **no** — "ligand" satisfies the ligand side from an *unrelated clause* |
| "Every candidate is scored by AlphaFold2. It is also scored by RoseTTAFold3 and a physics force field." | **no** — split across two sentences by `_sentences` |

The rule is a good rule and a large improvement on four literals. It is a
**heuristic that raises the bar**, not an invariant that closes the class, and
the docstring should say so. This is the "guards that certify false" pattern in
its milder form: the guard does not certify false today, but its own
justification claims a completeness it does not have, and that is what a future
writer will read. **One-line wording fix; no code change.** Mutation **M-Q2**
in §7 lands the first of these on a live page with a green suite.

### 3d. False reds exist too, and one is realistic

| true sentence | flagged? | why |
|---|---|---|
| "AlphaFold2 scores **proteins** and RoseTTAFold3 scores **small molecules**." | **yes, wrongly** | `PROTEIN_TARGET` is `\bprotein\b` with no `s?`, and `\bsmall[-\s]molecule\b` cannot match the plural either (the `\b` fails before `s`). `LIGAND_TARGET` has `ligands?`/`motifs?`; the asymmetry is an oversight. |
| "A **PDB** target is scored by AlphaFold2, an **SDF** target by RoseTTAFold3." | yes, wrongly | file formats instead of target nouns — and the form copy does say "a protein PDB" / "a small-molecule SDF target" |
| "AlphaFold2 handles **polypeptide** targets, RoseTTAFold3 handles small-molecule ones." | yes, wrongly | synonym |
| "…and RoseTTAFold3 a **nucleic-acid** target." | yes, wrongly | RF3's real remit; not this app's variants |

The plural one is the one that matters: a writer will hit it. `\bproteins?\b`
and `small[-\s]molecules?` close it.

### 3e. Model names are punctuation-escapable

`MODEL_NAMES` was hardened against `RoseTTAFold-2` (the `3` is anchored) but not
against how the models are ordinarily spelled:

| spelling | recognised? |
|---|---|
| `AlphaFold-2` | **no** — `\bAlphaFold\s?2\b` allows one space or nothing, not a hyphen |
| `RF-3` | **no** |
| `RoseTTAFold‑3` (U+2011 non-breaking hyphen) | **no** — `[\s-]` is ASCII-only and `\s` does not match U+2011 |
| `AlphaFold 2` with `&nbsp;` | yes (`\s` is Unicode-aware after `_visible_text`) |
| `alphafold2` / `rosettafold3` lowercase | yes |

`AlphaFold-2` is standard in the literature. **M-Q1** in §7 uses it to walk a
false composite straight onto `/tools/proteina` with a green suite.

### 3f. The "disclosed ceiling, asserted" line is weaker than its comment claims

> "If somebody widens the trigger, this line fails and points at the disclosure
> that needs deleting."

Mutation **M-Q6**: I widened it the natural way — added a third scoring family
(`"FF": r"force[-\s]field|physics force field"`) to `MODEL_NAMES`. Result:

```
FAILED TestProteinaScoringClaimIsConsistent::test_every_multi_model_sentence_states_the_mapping
E  AssertionError: the mapping check passed a composite claim:
   'Every design is re-folded by AlphaFold2 and rescored by RoseTTAFold3 before it is ranked.'
```

The suite **does** go red — so this is not a guard that certifies false — but
**the ceiling assert is not the line that fired.** It cannot: the ceiling
sentence names AF2 and the force field, which is 2 of 3 families, and the
trigger is `len(named) < len(self.MODEL_NAMES)`, i.e. *every* family. Two
consequences worth writing down:

1. Adding a family makes the rule **narrower**, not wider — it starts demanding
   that all three be named before it checks anything. That is the opposite of
   the intended direction, and the failure message a maintainer sees ("the
   mapping check passed a composite claim") reads as "your copy is bad", not
   "you narrowed the trigger and the note above is now stale".
2. The comment's own upgrade instruction — "widen this to `len(named) >= 2`" —
   is a **no-op today**: `len(MODEL_NAMES)` is already 2.

The protection is real and comes from `false_copy[2]` / `false_copy[3]`. The
line credited with it is close to inert. Comment-level finding.

---

## 4. Page discovery — genuinely discovered, floor is real, nothing missed

`_public_get_paths` measured from my own harness: **50 public 200 GET paths, 44
of them HTML**. Exactly as claimed.

The six non-HTML: `/health`, `/robots.txt`, `/scout/example`, `/scout/progress`,
`/scout/quota`, `/sitemap.xml`.

### Is anything still missed?

I ran an **independent crawl** that does not share the implementation: every GET
rule, single-argument rules expanded over the 14 slugs **plus** eight generic
tokens (`example`, `1`, `index`, `getting-started`, `faq`, `troubleshooting`,
`test`, `abc`), everything that answers 200 kept.

```
INDEPENDENT CRAWL found 50; not in _public_get_paths: []
```

**Nothing anonymously reachable is missed today.** The one GET rule both crawls
skip is `/api/jobs/<job_id>/pdb/<path:filename>` (two arguments; auth-gated and
needs a real job, so out of scope).

Two structural limits, neither live: multi-argument rules are dropped
(`else: continue`) and converter rules are dropped (`if "<" in candidate`), so a
public `/showcase/<int:page>` would be invisible. And single-argument rules are
expanded over **tool slugs only** — a future public route keyed on anything else
is undiscoverable. **M-Q5** in §7 is that shape, and it is green.

### The floor names pages, not just a count — CONFIRMED

```python
_MUST_REACH = ("/", "/tools", "/help", "/help/faq", "/help/getting-started")
... assert not missing, ...
for slug in slugs:
    for path in (f"/tools/{slug}", f"/help/tools/{slug}"):
        assert path in paths, f"discovery missed {path}"
assert len(paths) >= _MIN_PUBLIC_PATHS       # 40
```

**33 pages asserted by name** (5 + 2×14) and a count floor of 40 against a
measured 50. Both halves are there; the named half is what catches a discovery
that stays large while losing the page a claim lives on.

### `_jsonld_text` parses, it does not regex — CONFIRMED

`json.loads` per block, then an explicit stack walk over dicts/lists collecting
every string leaf. `@graph`, `mainEntity` and any nesting are reached by
construction. A block that does not parse is appended **raw** rather than
skipped, which is the right failure mode. Verified in anger by M-P2, whose
offender key is `/help/faq (json-ld)`.

**One hole in how blocks are *found*, not parsed.** `_JSONLD_BLOCK` is
`r'<script type="application/ld\+json">(.*?)</script>'` — no attribute
tolerance. One extra attribute on the tag and the whole block is invisible to
`_jsonld_text` **and** to `TestEveryJsonLdBlockIsClean` (which uses the same
literal). That is **M-Q3** in §7, and it is green.

### Both views reach both content sweeps — CONFIRMED

`_page_views` is consumed by `test_the_sourceless_threshold_is_gone_everywhere`
and `test_no_rendered_page_claims_the_three_model_stack`, the two sweeps
described that way. The legend test reads `<dt>/<dd>` from markup directly,
which is correct — the legend is visible HTML, not structured data.

---

## 5. The threshold rule now validates per-metric — CONFIRMED, with one residue

`_glossary_keys_for` resolves each `<dt>` to the glossary keys it names by
word-boundary match on key or label; `sourced` is built from **those keys'**
`good_range` only. Live state, read off `/tools/mpnn`:

| `<dt>` | keys resolved | numbers in `<dd>` | sourced from |
|---|---|---|---|
| `ipTM` | `['ipTM']` | `0.65`, `0.75` | `['0.65', '0.75']` |
| `pLDDT` | `['pLDDT']` | — | `[]` |
| `i_pAE and pAE` | `['pAE', 'i_pAE']` | — | `[]` |
| `ProteinMPNN recovery` | `['recovery']` | `0.4`, `0.6` | `['0.4', '0.6']` |

**A metric with no number in its own band fails loudly rather than passing
vacuously** — proven, not reasoned: pLDDT's `good_range` yields `∅` under
`_numbers`, and M-P9 fails with `"['0.75'] not in ['pLDDT']'s own good_range
[]"`. The `<dt>`-names-no-metric branch also fires: **M-Q7** renames the pLDDT
`<dt>` to "Fold confidence" and the test goes red on all 14 tools with `"names
no glossary metric, so any number in it is unsourceable"`. Both branches are
real.

Two residues, both green, both in §7:

- **M-Q4** — `THRESHOLD_NUM` is `\d+\.\d+|\d+\s*%`. **Bare integers are exempt
  by design**, so an integer threshold stated about a 0-to-1 metric is invisible:
  "Treat anything above **80** as reliable" in the **ipTM** entry renders on all
  14 tool pages, suite green. That is M-P9's defect with the scales swapped, and
  it is realistic precisely because the pLDDT band next to it *is* written in
  bare integers.
- **M-Q8** — the union survives *inside* one `<dt>`. `_glossary_keys_for`
  returns every matching key and unions their bands, so naming a second metric
  in the label launders that metric's number: `<dt>ipTM (interface pTM)</dt>`
  with "Aim above **0.7**" resolves to `['ipTM', 'pTM']`, and `0.7` is sourced
  from **pTM** while ipTM's own band is `0.75 / 0.65`. Green.

---

## 6. The `backbone` gloss — both copies, and the wording is not a fourth variant

Rendered through `test_client()` and compared byte for byte, not grepped:

```
JSON-LD  : 'A design is one candidate the tool generates: a backbone — a
            structure with no sequence decided yet — with a sequence put on it,
            scored and ranked against the others in the run. …'
visible  : identical
answer in visible copy verbatim: True
```

Both copies carry the gloss, and the `&mdash;` in the `<dd>` unescapes to the
same `—` the `faq_items` string uses literally, so the two do **not** diverge —
which matters, because `test_the_scoring_answer_says_which_model_scores_which
_target` enforces exactly that invariant on the proteina page.

The wording is lifted, not invented. Site-wide, `backbone` glossed in the design
sense reads two ways today, and this uses the existing one:

| surface | gloss |
|---|---|
| `/help`, `/tools/mpnn` | "a backbone — **a structure with no sequence decided yet**" |
| **`/help/faq` (new)** | "a backbone — **a structure with no sequence decided yet**" ✅ same |
| `/` chooser, `/tools` step 3, the recovery legend on 14 tool pages | "a backbone — **the folded shape on its own, before a sequence is chosen for it**" |

**Claim CONFIRMED: not a fourth variant.** Noting, without objecting: the site
now carries two different glosses for one word, in different places. That is
pre-existing and this delta picks the correct one of the two.

---

## 7. My own attack — 8 mutations, none tried by a prior round

Every mutation applied through the private harness, which **refuses on a
non-unique anchor** and **refuses on an empty `git diff --unified=0`**; every
diff printed and read before any suite ran. Batches share a worktree only where
the mutations sit in different files or on different slugs, so each failure's
offenders dict attributes it. **Every green one was re-rendered through
`test_client()` to prove the defect reaches a page.**

| # | mutation | landed | suite | failing test / does it reach a page? |
|---|---|---|---|---|
| **M-P1…M-P6, M-P9** ↺ | round 3's seven holes | yes | **4 failed**, 5289/20 | all seven RED by name — §2 |
| **M-N** ↺ | the disclosed ceiling, on `mpnn` | yes | green | none — renders on `/tools/mpnn`, disclosure honest |
| **M-Q1** ★ | false composite spelled `AlphaFold-2, RoseTTAFold-3 and a physics force field together` | yes | **GREEN** 5293/20 | none — **HOLE**, renders in the FAQ block on `/tools/proteina` |
| **M-Q2** ★ | false composite that **names both target types**: "Whether the target is a protein or a small molecule, every Proteina candidate is put through AlphaFold2, RoseTTAFold3 and a physics force field together" | yes | **GREEN** 5293/20 | none — **HOLE**, renders in the `/help` subtitle. **This is the one that disproves §3c.** |
| **M-Q3** ★ | one extra attribute (`id="tool-guide-ld"`) on `tool_guide.html`'s JSON-LD tag, block carrying the claim **and** raw `{{ tool.slug }}` | yes | **GREEN** 5293/20 | none — **HOLE**, ships on **14/14** guide pages; block verified to contain both `force-field reward stack` and `{{` |
| **M-Q4** ★ | bare-integer threshold on a 0-to-1 metric: "Treat anything above 80 as reliable" in the **ipTM** entry | yes | **GREEN** 5293/20 | none — **HOLE**, renders on **14/14** tool pages |
| **M-Q5** ★ | a public 200 page under a **non-slug single-arg rule**, `/help/topics/<topic>`, carrying the claim *and* `aim above roughly 0.7` | yes | **GREEN** 5293/20 | none — **HOLE**, `/help/topics/scoring` answers 200 with both defects |
| **M-Q6** ★ | guard-the-guard: add a third family to `MODEL_NAMES` | yes | **1 failed** | `test_every_multi_model_sentence_states_the_mapping`, on `false_copy[2]` — **not** the ceiling assert. §3f |
| **M-Q7** ★ | `<dt>` naming no glossary metric ("Fold confidence") | yes | **RED** | `test_the_general_metric_legend_states_no_unsourced_number`, `"names no glossary metric, so any number in it is unsourceable"`, 14 tools |
| **M-Q8** ★ | `<dt>ipTM (interface pTM)</dt>` + "Aim above 0.7" — pTM's number laundered onto ipTM | yes | **GREEN** 5293/20 (re-run **alone**) | none — **HOLE**, renders on 14/14 tool pages; also absent from the batch-2 offenders dict while `Fold confidence` is present |

↺ = reproduction. ★ = mine, not tried by any prior round.

Worktree: `wcqc4-mut`, reset with `git checkout -- .` between batches.
Batch 0 = the seven + M-N (4 failed, 5289/20). Batch 1 = M-Q1…M-Q5 (5293/20
green — **five live defects and a suite indistinguishable from clean**).
Batch 2 = M-Q6/M-Q7/M-Q8 (2 failed, 5291/20). M-Q8 was then **re-run alone**
(5293 passed, 20 skipped — indistinguishable from clean) so its greenness is not
an artefact of sharing a batch with a failing mutation, and re-rendered:
`/tools/af2` reads "…the guide for the tool you ran states its one. **Aim above
0.7 on a tractable target.**"

### What the green ones render

- **M-Q1** on `/tools/proteina`: "Each shard runs every candidate through
  **AlphaFold-2, RoseTTAFold-3** and a physics force field together." Neither
  model name is recognised, so the mapping rule never triggers, and no `CLAIMS`
  literal matches. The claim is the exact composite four rounds exist to kill.
- **M-Q2** on `/help`: "Whether the target is a protein or a small molecule,
  every Proteina candidate is put through AlphaFold2, RoseTTAFold3 and a
  physics force field together before it is ranked." Both models named, both
  target types named, rule satisfied, claim false.
- **M-Q3** on all 14 guide pages: `"disambiguatingDescription": "Each search
  shard filters candidates through an AF2 / RF3 / force-field reward stack.
  {{ tool.slug }}"` inside a valid `application/ld+json` block Google will read.
  Two guards blind at once, from one attribute.
- **M-Q4** on all 14 tool pages, in the ipTM entry, two sentences after the
  glossary's own `> 0.75 strong; > 0.65 acceptable`.
- **M-Q5**: `/help/topics/scoring` → 200, "Proteina filters candidates through
  an AF2 / RF3 / force-field reward stack. Aim above roughly 0.7 on a tractable
  target." Both the claim sweep and the `STALE` threshold guard are blind
  because the page is never fetched.

**None of these is true of the copy today.** I swept all 50 pages for each class
before saying so (§3a, §5, §9).

---

## 8. The app, driven anonymously — asserts, not eyeballs

`create_app()` + `test_client()`, no session, every registered adapter flagged
on. The harness **raises** unless both hold:

```
OK: 14 adapters registered AND 14/14 tool pages at 200
REGISTERED: ['af2','bindcraft','boltz2','boltzgen','colabfold','esmfold',
             'esmfold2-design','iggm','mpnn','opendde','proteina','pxdesign',
             'rfantibody','rfdiffusion']
```

### The `_csrf_token` finding — the tightening is correct

After 50 anonymous GETs my session reads exactly:

```
SESSION AFTER GETS: {'_csrf_token': 'WA_bDYbHfZ2xmcH2LAx_DHtgkFG5hE3kY-cycWkxybU'}
```

**Tightening rather than loosening is the right call, and it is verifiable at
source.** Authentication in this app is `session["user_email"]`
(`shared/auth.py:705` in `login_required`, `app.py:458` in the template
context). `_csrf_token` is set by the app's own CSRF machinery
(`app.py:257-265`) and is only ever *compared* on POST (`app.py:324`) — it
authenticates nobody. So:

- `assert sess == {}` would be **wrong** — it fails on a legitimate anonymous
  session;
- `assert set(sess) <= {"_csrf_token"}` is **correct and tight** — it admits the
  one framework key and nothing else, so any auth key appearing fails;
- dropping the assert, or narrowing it to "no `user_email`", would be weaker.

The assert lives in the builder's private harness, not in the committed file, so
what I verified is the fact it rests on: measured (the session contents) and
traced (which key authenticates).

---

## 9. Repo-wide jargon sweep — headline claims spot-checked

Computed over all 50 discovered pages, on `_visible_text`:

| claim | verdict |
|---|---|
| `contig` appears **zero** times | **CONFIRMED** — 0 pages |
| `MSA` is **unglossed on every page it appears on** | **CONFIRMED** — 9 pages (`/tools/af2`, `/tools/boltz2`, `/tools/colabfold`, `/tools/esmfold`, `/tools/mpnn` and four guides). I read the surrounding sentence on each: "ColabFold MMseqs2 MSA plus AF2", "no informative MSA exists", "No MSA will be computed", "never queries an MSA" — the word carries its meaning on none of them. |
| `CDR` reaches `/` and `/tools` via `shared/tools_catalog.py` | **CONFIRMED** — 8 pages including `/` and `/tools`; the source is `shared/tools_catalog.py:59`, the Developability Scout one-liner ("CDR length, hydrophobic …"). Also on `/developability`, which round 3's list does not name either. |

All three are pre-existing and untouched by this delta.

---

## 10. The open item — the proteina scoring contradiction

**I agree with the scoping decision, and I disagree with "not settleable here".**

### On the decision

Scoping the rule to AF2+RF3 rather than "any two channels" is right. A guard
forcing either answer would assert a fact about what the container runs that
nobody in this repo has measured, and the failure mode of getting that wrong is
worse than the gap: a red suite pointing at correct copy. Disclosing it, and
disclosing it in the code next to the rule rather than only in a report, is the
correct treatment. The one caveat is §3f — the assertion written to hold the
ceiling in place does not fire under the most natural widening; the protection
that does exist comes from `false_copy`, and the comment credits the wrong line.

### On whether the repo can settle it — **it can, near-decisively, and it points against the preset copy**

The disputed sentence is `tools/proteina/__init__.py:811`: "Search is scored by
AlphaFold2 confidence **plus a force-field reward**." Four independent in-repo
sources say protein_binder has no force-field channel:

1. `tools/proteina/Dockerfile.modal:229-231` — "protein_binder scores on **AF2
   alone**".
2. `tools/proteina/run_pipeline.py:56-58` — "reward channels are config-gated,
   NOT flag-gated: protein_binder scores on **AF2 only** (rf3folding commented
   out in `binder_generate.yaml`)".
3. `tools/proteina/run_pipeline.py:285` — recorded against real canary reward
   CSVs @`916eaaed`: "protein_binder reward = `af2folding_*` (AF2 refold);
   **`total_reward == -i_pae`**". A force-field term contributing to the total
   would break that equality. This is the closest thing to a *measurement* in
   the repo.
4. `tools/proteina/meta.py:92-96` `reward_attributions` — the user-facing
   attribution list, which would have to name a force field if one ran, names
   AF2, RF3, ESM2 and Foldseek/MMseqs2/DSSP and **no force field at all**.

Plus `tools/proteina/__init__.py:9`, the module docstring **in the same file**,
which says "`protein_binder` scores on AF2".

Against, exactly one: `tools/proteina/__init__.py:823`, the `ligand_binder`
preset — "the force field does not support protein-ligand complexes" — which
presupposes a force field exists for protein-protein. It is in the same file and
the same authorship as line 811, so it is not independent corroboration.

**What the repo cannot do is close it definitively**: `binder_generate.yaml`
lives in the upstream container and is not vendored here (`find` returns
nothing; it is only ever referred to). So the repo carries strong, one-sided
evidence and not proof.

**Recommendation, out of scope for this PR:** the cheapest settlement is free —
dump the column names of the reward CSV from any already-completed
`protein_binder` job and check whether `total_reward == -i_pae` holds. No GPU,
no new run. If it holds, `__init__.py:811` is wrong and should lose "plus a
force-field reward", which also resolves round 3's §4c caveat 1 (the
unfalsifiable "where it applies" hedge, now in three places including structured
data) at its source. Until then I would not guard it, and I would not widen the
rule. **Reasoned from sources, not measured — I did not run anything against a
container.**

---

## 11. Verdict: **MERGE**

**SHA reviewed `fa1954f`. Trunk `7fd180d` = 5273 passed, 20 skipped. Merged tree
`2ab94e5` = 5293 passed, 20 skipped: +20, all 20 new node ids in
`tests/test_public_tool_pages.py`, zero removed, zero renamed.**

Empirically verified:

1. **All seven of round 3's holes are closed**, each by name, each with the
   offender its test prints, and M-P1 is caught by the mapping rule rather than
   by a fifth literal — which is the point of the change.
2. **M-N is still green and still renders its bad shape.** The disclosure has
   not quietly stopped being true.
3. **The mapping rule does not false-red on any live copy** — zero offenders
   across 50 pages, both views — and its eight inline controls genuinely
   exercise the matcher, with no neighbouring string answering for any of them.
4. **50 public 200 GETs, 44 HTML**, matched by an independent crawl that misses
   nothing; the floor names 33 pages as well as counting 40.
5. **Per-metric threshold validation works, in both branches** — an empty
   number-set fails loudly (M-P9), and a `<dt>` naming no metric fails loudly
   (M-Q7).
6. **The `backbone` gloss lands in both copies of the answer, byte-identical**,
   in the wording `/help` and `/tools/mpnn` already use.
7. `contig` 0, `MSA` unglossed on all 9 pages it reaches, `CDR` on `/` and
   `/tools`.

Reasoned but not measured: the §10 source analysis; the observation that the
`AlphaFold-2` spelling is common in the literature; that a public
`/showcase/<int:page>` would evade discovery (the shape is proven by M-Q5, the
converter path is not).

### Why not BLOCKED

The bar is "factually wrong, contradictory, or broken" about what ships. Nothing
here is: every user-facing string this delta touches is accurate, the guards it
adds all go red on the defect they name, and the tree is green on trunk. The
findings are of two kinds — five new coverage holes, which is the same class
round 3 reported and merged, and one docstring that overstates its own guard's
completeness.

I want to be plain that **§3c is the item I came closest to blocking on**. A
guard whose justification claims a completeness it does not have is how this
file has repeatedly ended up certifying false, and the note as written will tell
the next writer the class is closed. But the guard itself is sound and strictly
better than the four literals it demotes, the correction is a wording change of
one or two sentences with no code behind it, and blocking a green tree over a
test docstring would be inconsistent with how rounds 1-3 treated equivalent
findings. **MERGE**, with §3c and §3f taken as follow-ups rather than lost.

### Follow-ups, none blocking

1. **§3c** — restate the mapping rule's docstring as a bar-raiser, not an
   invariant. Four counterexamples are in this file to paste in.
2. **§3d** — `\bproteins?\b` and `small[-\s]molecules?`, so a plural true
   sentence stops false-redding.
3. **§3e** — allow a hyphen in `AlphaFold[\s-]?2` for symmetry with RF3, and
   consider a Unicode-dash class.
4. **§4 / M-Q3** — `<script[^>]*type="application/ld\+json"[^>]*>` in
   `_JSONLD_BLOCK` and in `TestEveryJsonLdBlockIsClean`; one extra attribute
   currently blinds both.
5. **§5 / M-Q4** — bare integers are exempt from `THRESHOLD_NUM`; an integer
   stated about a 0-to-1 metric is M-P9 with the scales swapped.
6. **§3f** — the ceiling assert does not fire on the natural widening. Either
   assert on the trigger's shape or say in the comment that `false_copy` is what
   holds it.
7. **§10** — settle the `protein_binder` force-field question from one existing
   job's reward CSV columns; it is free, and it resolves both the preset copy
   and round 3's §4c caveat 1 at source.
