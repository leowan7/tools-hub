# QC round 2 — PR #146 `feat/redesign-phase-5-help`

**Verdict: MERGE** (with two non-blocking observations, §7)

| | |
|---|---|
| SHA reviewed | `c048561` (`fix(help): make every claim on the help pages true against post-#147 main`) |
| Trunk | `origin/main` = `1bfce94` |
| Merge commit on branch | `26e0c37` |
| Pre-merge base | `3740dbf` (`Merge pull request #143 from leowan7/fix/pin-mpnn-clone`) |
| Reviewer | independent QC agent; did not build this |
| Date | 2026-08-18 |

Both SHAs confirmed unmoved at review time via `git fetch origin`.

All work done in a detached worktree under the session scratchpad. The main
working tree (checked out on `fix/pin-gpu-image-digests` by a concurrent
session) was never touched.

---

## 1. Suite numbers — VERIFIED (measured, not quoted)

Command, run from each worktree root with **no path argument**:

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| Commit | Measured | Claimed | Match |
|---|---|---|---|
| trunk `1bfce94` | **5232 passed, 20 skipped** (183.26s) | 5232/20 | yes |
| head `c048561` | **5250 passed, 20 skipped** (183.28s) | 5250/20 | yes |

Delta trunk -> head = **+18, zero failures, zero new skips**.

Both runs exited 0. Neither run was piped through `tail` in a way that could
hide a failure; full output was captured to a file and the summary line read
from it.

`3740dbf` = 4880/19 and `26e0c37` = 5241/20 were **not** re-measured (two more
3-minute full-suite runs for intermediate commits that no longer exist on the
merge path). The two endpoints that matter both reconcile, so the +9/+9 split
is accepted on the per-module evidence in §1.1 rather than on those two
figures.

### 1.1 Per-module reconciliation — VERIFIED

Measured by running each module alone in each worktree:

| Module | trunk `1bfce94` | head `c048561` | delta |
|---|---|---|---|
| `tests/test_help_tool_guides.py` | file does not exist (0) | **16 passed** | +16 |
| `tests/test_tool_categories.py` | **8 passed** | **10 passed** | +2 |
| | | **total** | **+18** |

+18 is exactly the full-suite delta. Nothing else in the suite changed
count, so no existing test was deleted, renamed, or skipped to make room.

This also confirms the builder's claimed split (9 in the merge + 7 more
in the fix commit = 16 in `test_help_tool_guides.py`; 2 in
`test_tool_categories.py`) sums correctly, without re-measuring the two
intermediate commits.

---

## 2. The merge is lossless — VERIFIED, and the load-bearing claim holds

This is the strongest-evidenced part of the PR. Three independent checks:

**(a) The conflict set is exactly one file.**

```
$ git merge-tree --write-tree 1bfce94 16f7b16
exit=1
df8bf62c8f30d15d9ab3466b232b9f06ca2cf857
100644 e6d60bd... 1  blueprints/public.py
100644 76e73a8... 2  blueprints/public.py
100644 d99e3e9... 3  blueprints/public.py

Auto-merging blueprints/public.py
CONFLICT (content): Merge conflict in blueprints/public.py
```

One conflict, one path.

**(b) The builder's tree differs from git's own merge on that path ONLY.**

```
$ git diff --name-only df8bf62c8f30d15d9ab3466b232b9f06ca2cf857 26e0c37
blueprints/public.py
```

Every other file in the merge commit is **byte-identical** to what git
computed. There is no second file where a hand edit could have crept in.

**(c) The two-sided overlap is exactly one file — this is the load-bearing one.**

```
$ git diff --name-only 3740dbf 16f7b16 | sort > branchside   #   5 files
$ git diff --name-only 3740dbf 1bfce94 | sort > mainside     # 105 files
$ comm -12 branchside mainside
blueprints/public.py
```

**Independently reproduced.** This is what closes the "worktree base drift"
failure mode recorded seven times in this repo: a file changed on *both* sides
that git auto-merges *cleanly* is the case where a lost hunk is invisible.
Here there is no such file. The only two-sided file is the one that
conflicted, i.e. the one a human was forced to look at. Nothing could have
been silently dropped.

**(d) Main's recent work is intact.** Verified by object identity — these
files are the same blob at `1bfce94` and at `c048561`:

| File | Carries | trunk vs head |
|---|---|---|
| `shared/tools_catalog.py` | #145 catalog band renames, `group_catalog`, `CATEGORY_ORDER` | IDENTICAL |
| `blueprints/auth.py` | #152 `safe_next()` (present, `blueprints/auth.py:31`) | IDENTICAL |
| `shared/wallet.py` | #153 `SIGNUP_CREDIT_USD` single source | IDENTICAL |
| `templates/components/pilot_card.html` | #147 pilot machinery | IDENTICAL |
| `templates/components/worked_example.html` | #147 example machinery | IDENTICAL |
| `shared/category_glyphs.py` | #145 glyph keys | IDENTICAL |

Combined with (b), *every* file on the branch except `blueprints/public.py`,
`README.md`, `templates/help/index.html`, `templates/help/getting_started.html`,
`tests/test_help_tool_guides.py` and `tests/test_tool_categories.py` is
bit-for-bit trunk.

---

## 3. Ordering shared across three surfaces — VERIFIED by rendering

Rendered `/help`, `/` and `/tools` anonymously through the real Flask app with
all 14 `FLAG_TOOL_*` on, and extracted the tool slug order in document order.

```
/help    slug order: bindcraft boltzgen esmfold2-design iggm proteina pxdesign
                     rfantibody rfdiffusion | mpnn | af2 boltz2 colabfold
                     esmfold opendde
/tools   slug order: bindcraft boltzgen esmfold2-design iggm proteina pxdesign
                     rfantibody rfdiffusion | mpnn | af2 boltz2 colabfold
                     esmfold opendde
```

**`/help` and `/tools` are character-identical.** The homepage renders the
same grouped sequence after a short featured row (`rfdiffusion boltzgen mpnn
boltz2 colabfold`), which is a homepage-only element, not a band-order
divergence.

Band headings actually rendered on `/help`:

```
Make new binders for my target
Choose sequences for a structure I already have
Predict or check a 3D structure
```

That is a correct-order subsequence of `CATEGORY_ORDER`. The two absent bands
(`Check if my target is a good one to bind`, `See if a binder will hold up in
the lab`) are exactly the two non-adapter tools, which are deliberately split
into the guideless paragraph because `help_tool_guide` 404s on their slugs.
Confirmed empirically: `/help/tools/epitope-scout` and
`/help/tools/developability` both return 404, and neither appears in the grid.

The resolution really did delete the branch's hand-rolled ordering and adopt
`group_catalog()` — `blueprints/public.py:441` now calls it, and the ordering
constant lives in one place (`shared/tools_catalog.py:110`). A band rename now
carries to all three surfaces with no edit to `public.py`. **No regression
observed.**

---

## 4. Links — VERIFIED, and the vacuity guard passes

Own crawl, anonymous, through the real app:

```
### ADAPTERS REGISTERED: 14
### TOTAL LINKS CHECKED: 60   200s: 59
### NON-200:
    ('/account/wallet/topup', 302, '/login?next=/account/wallet/topup',
     ['/help/troubleshooting'])
### /tools/<slug> at 200: 14 of 14
### ?pilot=1 at 200: 14 of 14
```

**60 links, exactly as claimed. Zero 404s.** The single non-200 is the auth
gate on the top-up route, correctly redirecting to `/login` with a `next` —
and note that `next` is now `safe_next()`-filtered by #152. Not a broken link.

The vacuity trap is closed: 14 adapters are registered (not 0), and 14 of 14
tool pages and 14 of 14 `?pilot=1` URLs return 200, so the crawl is asserting
over the full set rather than over an empty flag-gated catalog.

---

## 5. The guards are real — 15 mutations, 15 landed, 15 caught

Method, applied to every row: apply the edit, **prove it reached disk with
`git diff --unified=0` before running anything**, run
`tests/test_help_tool_guides.py tests/test_tool_categories.py`, record the
failing test by NAME, `git checkout -- .`, repeat.

Two traps were live and both were hit and handled:

- **CRLF.** Every file under review is CRLF on disk. Reading with Python's
  default newline translation and writing back rewrites every line ending,
  which buries the mutation in a whole-file diff (or, with a multi-line
  pattern, silently fails to match at all). The harness reads and writes with
  `newline=""` and rewrites LF to CRLF in each pattern to match the file's own
  endings.
- **Silent non-application.** Every mutation reports its diff line count. No
  row below is `PATTERN-NOT-FOUND` or `DID-NOT-LAND`; all 15 produced a real
  on-disk diff before the test run.

| # | Mutation | Landed | Failing test(s) | Verdict |
|---|---|---|---|---|
| M1 | Bypass `group_catalog`, render one flat band | yes (7) | `test_guide_grid_is_grouped_by_catalog_category`, `test_empty_guide_grid_renders_an_empty_state` | CAUGHT |
| M2 | Stop filtering non-adapters out of the guide grid | yes (7) | `test_every_guide_link_resolves`, `test_non_adapter_tools_get_no_guide_link`, `test_one_guide_per_registered_tool`, `test_empty_guide_grid_renders_an_empty_state` | CAUGHT |
| M3 | Delete the guideless-tools paragraph | yes (7) | `test_guideless_tools_are_still_reachable_from_help`, `test_guideless_copy_matches_the_real_gate_on_those_routes` | CAUGHT |
| M4 | `/help` says "Six steps" while the guide has seven | yes (7) | `test_help_index_step_count_matches_the_guide` | CAUGHT |
| M5 | Reword the pilot CTA reference ("Load the settings") | yes (7) | `test_getting_started_names_the_pilot_card_cta_that_exists` | CAUGHT |
| M6 | Drop `proteina` from `_TOOL_CATEGORIES` (silent "Other") | yes (7) | `test_no_guide_falls_into_the_other_bucket`, `test_no_tool_lands_in_other`, `test_every_adapter_has_a_category`, `test_readme_tool_table_matches_the_live_registry`, +2 | CAUGHT |
| M7 | Rename a band without renaming its glyph key | yes (7) | `test_every_rendered_band_resolves_a_glyph`, `test_every_rendered_band_has_a_glyph`, `test_readme_tool_table_matches_the_live_registry`, +5 | CAUGHT |
| M8 | Cut `SIGNUP_CREDIT_USD` from $15 to $10 | yes (7) | `test_signup_credit_actually_covers_every_pilot` | CAUGHT |
| M9 | Break step 2's anonymous-access claim | yes (7) | `test_getting_started_claim_you_can_look_without_an_account` | CAUGHT |
| M10 | Guideless copy claims sign-in while routes are open | yes (7) | `test_guideless_copy_matches_the_real_gate_on_those_routes` | CAUGHT |
| M11 | Add an 8th list item (step drift the OTHER direction) | yes (9) | `test_help_index_step_count_matches_the_guide` | CAUGHT |
| M12 | Make every tool render a pilot card (stales the "most, not every" carve-out) | yes (9) | `test_getting_started_names_the_pilot_card_cta_that_exists` | CAUGHT |
| M13 | Make one *real* adapter's guide 404 (`proteina`) | yes (7) | `test_every_guide_link_resolves` | CAUGHT |
| M14 | Delete the all-flags-off empty state | yes (7) | `test_empty_guide_grid_renders_an_empty_state` | CAUGHT |
| M15 | Re-gate `/developability` behind login | yes (7) | `test_guideless_copy_matches_the_real_gate_on_those_routes` | CAUGHT |

**Zero guard holes.** M11, M12, M13 and M15 are mutations the builder did not
report trying.

### 5.1 The formerly-inverted copy test — genuinely two-sided

The test the brief singled out is now named
`test_guideless_copy_matches_the_real_gate_on_those_routes`. It reads the live
status of every guideless route and branches:

- routes gated -> the paragraph MUST say "sign in"
- routes open -> the paragraph MUST say "without an account" and MUST NOT say
  "sign in"

**Both branches proven red, which is what makes it non-tautological:**

- **M10** left the routes open (`/scout/` 200, `/developability` 200) and made
  the copy claim a sign-in. Failed on the else-branch negative assertion.
- **M15** left the copy saying "without an account" and re-gated
  `/developability` with a 302. Failed on the `if gated:` branch.

A tautology would have passed one of these. Neither passed. The test cannot
invert again the way its predecessor did, because it no longer holds an opinion
about the wording — it holds an opinion about the wording *matching the routes*.

Detection mechanism checked: `gated` is `status_code in (301, 302)`, and M15's
redirect returned 302, so the detector fires on the real thing rather than on a
string.

### 5.2 Vacuous-collection audit — CLEAN

`tools.base._REGISTRY` is empty without `import app`. Both modules import `app`
at module scope, and `test_registry_is_populated` (asserting `>= 14`) is present
and passing. Beyond that single guard, **every** test the PR adds carries its
own floor, so none can pass over an empty collection:

| Test | Non-vacuity floor |
|---|---|
| `test_registry_is_populated` | `len(all_adapters()) >= 14` |
| `test_every_guide_link_resolves` | `assert links` |
| `test_one_guide_per_registered_tool` | set equality vs the live registry |
| `test_the_five_tools_the_hardcoded_list_missed_are_linked` | 5 slugs pinned by name |
| `test_non_adapter_tools_get_no_guide_link` | 2 slugs pinned by name |
| `test_guideless_tools_are_still_reachable_from_help` | `assert guideless` |
| `test_guideless_copy_matches_the_real_gate_on_those_routes` | `assert guideless` |
| `test_no_guide_falls_into_the_other_bucket` | `len(catalog) >= 16` |
| `test_every_rendered_band_resolves_a_glyph` | `len(bands) >= 5` |
| `test_guide_grid_is_grouped_by_catalog_category` | `len(set(bands)) > 1` |
| `test_empty_guide_grid_renders_an_empty_state` | asserts the empty-state string |
| `test_getting_started_claim_you_can_look_without_an_account` | `len(slugs) >= 14` |
| `test_getting_started_claim_the_home_page_groups_by_task` | `len(bands) >= 5` |
| `test_getting_started_names_the_pilot_card_cta_that_exists` | `>= 10` AND `< len(all_adapters())` |
| `test_help_index_step_count_matches_the_guide` | `steps > 1` |
| `test_signup_credit_actually_covers_every_pilot` | `len(prices) >= 10` |
| `test_readme_tool_table_matches_the_live_registry` | `len(adapters) >= 14`, `assert rows` |
| `test_readme_hardcoded_tool_table_matches_the_catalog` | `assert _HARDCODED_TOOLS` |

Note `test_getting_started_names_the_pilot_card_cta_that_exists` carries an
**upper** bound as well as a lower one, which is unusual and correct: it fails
if every tool gains a pilot card, because that would make step 3's carve-out
stale. M12 proved that branch red.

---

## 6. Copy claims re-read against the live app

Loaded all four pages anonymously and read the rendered text, not the diff.

| Claim (page) | Status | Evidence |
|---|---|---|
| "Seven steps" (`/help`) | TRUE | guide renders 7 steps; M4/M11 guard both directions |
| "You do not need an account to look" (step 2) | TRUE | 14/14 `/tools/<slug>` return 200 anonymously |
| "the home page groups the tools by that question" (step 1) | TRUE | all 5 bands render on `/` |
| "Most tool pages carry a pilot card" (step 3) | TRUE | 10 of 14 measured |
| "A few tools have no pilot card, because their smallest possible run is already the only run they do" (step 3) | TRUE — probed hard, held | the 4 pilotless tools are af2, colabfold, esmfold, opendde. All four have two presets, which initially looked like a contradiction, but neither pair is a *scale* dial: af2/colabfold/esmfold's second preset is `batch` (larger, so nothing smaller exists to dial down to), and opendde's `general`/`abag` are two checkpoints at the identical $14.79. `tools/opendde/meta.py:110` states this in as many words. |
| "Load these settings" (step 3) | TRUE | exact CTA string at `templates/components/pilot_card.html:82` |
| "a new account starts with $15" (step 4) | TRUE, and now derived | renders `signup_credit()`; `SIGNUP_CREDIT_USD = Decimal("15.00")` |
| "which covers every pilot on the site" (step 4) | TRUE **as written**, but see 7.1 | dearest pilot = proteina $12.59 vs $15.00 |
| "the guide for the tool you ran spells out which number to sort on and where its cut-off sits" (step 5) | TRUE | all 14 `/help/tools/<slug>` pages state the 0.7 ipTM cut-off |
| "clone the run and raise the design count" (step 6) | TRUE | clone machinery live, `tests/test_clone_roundtrip.py` |
| "Star the designs... to build a shortlist" (step 7) | TRUE | shortlist machinery live in `blueprints/lab_projects.py` |
| "they open without an account" (guideless paragraph) | TRUE | `/scout/` 200, `/developability` 200; M10/M15 guard both directions |
| "no GPU job to set up ... answer in seconds" (guideless paragraph) | TRUE | catalog runtimes `~30 s` and `<5 s` |
| README "Fourteen GPU tools" + slug/category/route table | TRUE | `test_readme_tool_table_matches_the_live_registry` re-derives it from the registry; M6/M7 both turned it red |
| README "`/scout/`, `/developability`, and `/help` all render for anonymous visitors" | TRUE | all three measured at 200 |
| README "only submitting a job (and anything under `/jobs` or `/account`) redirects to `/login`" | TRUE | `/account/wallet/topup` returns 302 to `/login?next=...` |

I found **no false claim** on the four help pages or in the README. The five
the builder says it fixed are all fixed, and the ones it did not touch all hold.

### 6.1 Worked examples ship on mpnn only — CONFIRMED

`EXAMPLE` is non-`None` in exactly one meta (`tools/mpnn/meta.py:210`); the
other 13 are `None`. Rendering all 14 tool pages, the string "A run we actually
did" appears on `/tools/mpnn` and nowhere else. **1 of 14.**

Judgement on the omission: **correct call, and I would not change it.** Writing
getting-started copy that says "see a real run before you commit" when 13 of 14
tools cannot show one would be a worse defect than saying nothing — and it is
exactly the class of claim this PR exists to eliminate. The example is still
discoverable where it matters, on the mpnn page itself at the moment of use.
When coverage rises, step 2 is the natural home for a sentence about it.

---

## 7. Non-blocking observations

### 7.1 The signup-credit margin is $0.21, not $2.41 — and the guard cannot see it

The builder measured the dearest **pilot** (proteina, $12.59) against the $15
credit and reported $2.41 of headroom.

Re-derived through the real estimator
(`shared.wallet_estimates.estimated_cost_for_tool`):

```
opendde   {'preset': 'general'} -> $14.7920
opendde   {'preset': 'abag'}    -> $14.7920
proteina  (pilot, as rendered)  -> $12.59
SIGNUP_CREDIT_USD               -> $15.00
```

**OpenDDE's only possible run costs $14.79 — $0.21 under the credit.** It is
excluded from `test_signup_credit_actually_covers_every_pilot` because that test
scans for the rendered `About <strong>$X</strong>` pilot-card string, and
OpenDDE has no pilot card (deliberately, per `tools/opendde/meta.py:110`).

So step 4's sentence is **literally true** (it says "every *pilot*", and OpenDDE
has none), and the test does correctly guard the claim as worded. But:

- the real headroom on the site's dearest single run is **$0.21, not $2.41**;
- a ~1.5% rise in the OpenDDE GPU rate makes the site's most expensive run
  uncoverable by the signup credit while every test stays green;
- a new user reading "starts with $15 ... which covers every pilot" will not
  parse "pilot" as a term of art that excludes OpenDDE.

Not a blocker — nothing on the page is false today. Worth a follow-up: extend
the test to price every adapter through `estimated_cost_for_tool` rather than
only the ones that render a pilot card.

### 7.2 The $20 minimum top-up is hand-typed in two templates

`templates/help/faq.html:11,85` and `templates/help/troubleshooting.html:81`
hard-code "the minimum top-up is $20". It currently matches
`shared/wallet.py:76` (`MIN_TOPUP_USD = Decimal("20.00")`), so it is true.

This is the identical drift hazard the builder fixed for the signup credit
(hand-typed figure replaced by `signup_credit()`), left in place one field over.
Both files are **outside this PR's diff**, so this is not a regression it
introduced — but the fix it did make is half of the available fix. The FAQ's
signup-credit figure was already converted on trunk by #153; the minimum top-up
was not. No test covers it.

Suggested follow-up: add a `min_topup()` template global alongside
`signup_credit()` and use it in both files.

---

## 8. Reading `/help/getting-started` as a bench biologist

Persona as briefed: knows their target protein, has a PDB, does not know model
names, has never used the site.

**Could they reach a submitted pilot? Yes — but only by leaving the page.**

Steps 1 and 2 work well. Step 1 does not name a single model, which is the right
call for this reader; it routes on "what is on your bench" and points at the
home page, whose bands are literally phrased as the reader's own question
("Make new binders for my target"). Step 2 correctly sets the expectation that
looking is free.

**First hard stop: choosing hotspot residues, in step 2.**

Step 2 introduces the field and defines the term well —

> "a few hotspot residues — the specific amino acids on the target's surface
> you want the new binder to touch"

— and then never says how to pick them. Our persona knows their protein but has
no reason to know which of its ~300 residues form a bindable patch. This is the
same first hard stop the earlier QC round named.

**Does this PR close it? Not on the help page. The product closes it.**

Epitope Scout — the tool that answers exactly this question — appears on
`/help/getting-started` only in **step 6**, and only in the failure branch:

> "If nothing scored well... Score the target's surface first with Epitope
> Scout"

That is the right tool offered four steps too late and framed as a remedy for a
run that already failed, after the reader has spent money. The word "first" in
that sentence is doing work the page's own ordering contradicts.

What rescues it is that step 2's instruction ("open the tool page and look at
the form") lands the reader somewhere the gap *is* closed. Verified: every
hotspot-steering form includes `templates/tools/_hotspot_scout_hint.html`, which
renders directly under the field:

> "Not sure which residues? Score your target's surface with Epitope Scout
> first."

Confirmed on 6 forms (bindcraft, boltzgen, proteina, pxdesign, rfantibody,
rfdiffusion); `iggm` is deliberately excluded because its Epitope residues field
carries its own Scout link. So all 7 hotspot-steering tools answer the question
in place, with the cursor in the field.

**Verdict on the walkthrough:** the persona gets to a submitted pilot. The gap
is closed by #144's form-level hint, not by this PR's copy — so the walkthrough
succeeds, but `/help/getting-started` read on its own still leaves a reader
stuck at step 2 until they open a form.

**Other assumed knowledge, ranked:**

1. **"which chain in that file to aim at"** (step 2) — assumes the reader knows
   their PDB has chains and which one is the target. Partly mitigated: the
   troubleshooting page has a "Chain not found" row explaining chain IDs are
   case-sensitive. But that is a page you visit *after* an error.
2. **Score cut-offs** (step 5) — the page says the per-tool guide "spells out
   which number to sort on"; verified true (all 14 guides state the 0.7 ipTM
   cut-off), so this is a real handoff rather than a dead end.
3. **"roughly 1 in 5 designs passes the in-silico filter"** (step 6 panel and
   FAQ) — an empirical claim with no test and no citation. Not checkable from
   this repo; flagged as unverified rather than false.

**Recommendation (non-blocking, one sentence of copy):** move the Epitope Scout
mention from step 6 into step 2, next to the hotspot definition, so the help
page matches what the forms already do. This is the single highest-value change
available to these pages, and it costs one sentence.

---

## 9. Verdict

**MERGE.**

Everything load-bearing was re-derived rather than accepted:

- Suite numbers measured at both endpoints; both exact; +18 reconciles per
  module.
- The lossless-merge argument reproduced three independent ways, including the
  `comm -12` overlap that is the actual load-bearing claim.
- Band ordering verified by rendering all three surfaces; `/help` and `/tools`
  are character-identical in slug order.
- 60 links crawled, zero 404s, with the vacuity trap explicitly closed
  (14 adapters registered, 14/14 pages at 200).
- 15 mutations, all proven to land on disk, all caught by a named test. Zero
  guard holes. The formerly-inverted copy test is proven red in **both**
  directions.
- No false claim found on any of the four help pages or in the README.

What I could **not** run, stated plainly:

- The `3740dbf` = 4880/19 and `26e0c37` = 5241/20 figures were not re-measured
  (section 1). The endpoints reconcile and the per-module counts sum, so the
  split is consistent, but those two numbers remain unverified.
- The "roughly 1 in 5 designs passes the in-silico filter" statistic is not
  checkable from this repo (section 8).
- No production deploy was exercised; everything is the Flask test client
  against this worktree.

The two observations in section 7 are follow-ups, not merge blockers: nothing on
the pages is false today. 7.1 (the OpenDDE $0.21 margin the credit test cannot
see) is the one I would actually queue.
