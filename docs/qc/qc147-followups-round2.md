# QC round 2 — PR #154 (`fix/qc147-followups`)

Independent round. I did not build this and did not run round 1. Everything
below that is marked *verified* was re-derived here; everything I could only
reason about is marked as such.

- **SHA reviewed:** `e4e229b10f665b4bef084ad5a7442da6bfe3414c` (merge of the fix
  commit `517e5f4` with trunk)
- **Trunk at review time:** `origin/main` =
  `66388af63d7157ad7f3407a19971ac886de8bd1a` — matches the SHA I was given.
  Trunk did not move during the round.
- **Branch-only commits vs trunk:** `9387be8` (the #147 follow-up fixes),
  `c14ed25` (round 1 report), `517e5f4` (the repair), `e4e229b` (merge).
- **Worktrees:** three detached worktrees under the session scratchpad
  (`wt-trunk`, `wt-head`, `wt-mut`). The main working tree, checked out on
  `fix/pin-gpu-image-digests`, was never touched.

## VERDICT: **MERGE**

All four round-1 blockers (F1–F4) are genuinely repaired, each provably so: I
replayed round 1's own mutations and every one goes red against a named test.
The M8b probe reaches the end of all fourteen `validate()` chains and its
guard-of-the-guard fires on every attempt I made to blind it. The merge is
byte-for-byte lossless. The copy is accurate for all five carded tools and the
handoff round trip really lands.

Five non-blocking findings are recorded below (§7). The most substantive is a
**live blind spot in the repaired harvest**: `proteina`'s form disables three
controls with JavaScript at page load, and the harvest — whose docstring now
asserts "``disabled`` posts NOTHING" — reads all three. It does not move any
price today, so it is a finding and not a blocker, but it is the same bug class
as F4, still open, on the one form that already uses the pattern.

---

## 1. Suite — verified

Command, run from each worktree root, no path argument:

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| tree | SHA | result |
|---|---|---|
| trunk | `66388af` | **5250 passed, 20 skipped** in 178.46s |
| head | `e4e229b` | **5262 passed, 20 skipped** in 188.62s |

Both claimed figures reproduce exactly. +12 passed, skips unchanged at 20.

`--collect-only` node-id diff (5270 → 5282) gives 13 ids added and 1 removed;
the removed one is the same parametrized test under a freshly generated UUID
(`test_feasibility_get_requires_login[/scout/feasibility/download/<uuid>]`), so
the real net is **+12 distinct tests, 0 removed**.

### The +5 / +7 split — verified

Collecting at `9387be8` (the PR's pre-existing state) and diffing
`tests/test_pilot_recipes.py` node ids against head:

**Added by `517e5f4` (6):**

- `TestHotspotDeflection::test_the_probe_reaches_every_adapters_hotspot_check`
- `TestHotspotDeflection::test_every_tool_that_refuses_without_hotspots_carries_the_card`
- `TestHotspotDeflection::test_the_card_set_is_the_tools_own_stated_prerequisites`
- `TestHotspotDeflection::test_only_real_handoff_targets_promise_the_round_trip`
- `TestHotspotDeflection::test_scouts_own_picker_offers_exactly_the_handoff_set`
- `TestPilotCardPriceIsDerived::test_the_harvest_posts_what_a_browser_would_post`

**Removed by `517e5f4` (1):**

- `TestHotspotDeflection::test_the_constant_matches_what_the_adapters_actually_refuse`

Net **+5**, of which **+4 net inside `TestHotspotDeflection`** plus the harvest
test. Exactly the claimed split. The remaining **+7** are
`TestBudgetDoesNotChangeThePrice` (3), `test_the_guard_above_is_reading_the_form_and_not_the_pilot_dict`
(1) and three `TestHotspotDeflection` tests, all from `9387be8` — the PR's
pre-existing delta, as claimed.

### The skip-count reasoning — partially refuted

The claim is that skips staying at 20 proves the three removed
`os.environ.setdefault("SESSION_SECRET_KEY", …)` leaks unmasked nothing.

The *conclusion* holds, but not for the reason given, and the reasoning offered
is weaker than the builder thinks:

1. Nothing in the repo skips on `SESSION_SECRET_KEY` — I grepped for it and
   found no skip condition referencing the variable. So an unchanged skip count
   is not evidence about this variable at all; it could not have moved.
2. The real evidence is stronger and is what I measured: the full suite is green
   and the pass count rose by **exactly** the 12 node ids that were added, so
   no test flipped pass→skip, pass→fail, or fail→pass.
3. Round 1's F6 named three files — `test_pilot_recipes.py`,
   `test_worked_examples.py`, `test_clone_roundtrip.py`. All three are fixed
   correctly (save into the same `prior` dict the flags use, restore in
   teardown), so **F6 is closed**. But **three more leaks of exactly the same
   shape survive in older files**, which round 1 did not name:
   `tests/test_hotspot_picker_runtime.py:88`,
   `tests/test_multichain_form_affordances.py:179`,
   `tests/test_multichain_iptm_notice.py:254`. Alphabetically the first of these
   collects before `test_pilot_recipes.py`, `test_public_tool_pages.py` and
   `test_worked_examples.py`, so for most of the run `SESSION_SECRET_KEY` is set
   anyway by a leak the PR did not remove. Fixing three of six leaks is still a
   strict improvement — the new save/restore is correct — but "unmasked nothing"
   is partly an artifact of the leaks that remain. Pre-existing; see §7.4.

## 2. The F1/F2 redesign — verified, with a residual

### 2a. The predicates are what the copy needs

Independently derived across all fourteen adapters:

| set | members |
|---|---|
| `_needs_hotspots(meta)` (the card) | bindcraft, boltzgen, pxdesign, rfantibody, rfdiffusion |
| `VALID_HANDOFF_TOOLS` (round trip) | bindcraft, boltzgen, pxdesign, rfantibody |
| `validate()` hard-refuses empty hotspots | bindcraft, pxdesign, rfantibody, rfdiffusion |

Driving every adapter's `validate()` directly with an empty hotspot field
(§3) confirms **boltzgen validates clean** — it really does run unsteered — and
**rfdiffusion really does refuse**. Both halves of the mirror-image bug pair the
PR describes are real, and the split fixes both.

### 2b. `VALID_HANDOFF_TOOLS` — one copy, four readers, all agreeing

`grep -rn VALID_HANDOFF_TOOLS` over `*.py`/`*.html`/`*.js`: exactly one
definition (`scout/handoff.py:49`), read by

- the POST gate — `scout/routes.py:1208` (via a re-export at :1194),
- the pilot card — `blueprints/tools.py:775`,
- the tests.

The fourth surface, Scout's `<select>` in
`templates/scout/feasibility.html`, is hand-written markup and is locked to the
constant by `test_scouts_own_picker_offers_exactly_the_handoff_set`. I found no
second hard-coded copy of the four-slug set anywhere in the repo.

### 2c. The one-directional lock is genuinely one-directional — verified

`test_every_tool_that_refuses_without_hotspots_carries_the_card` asserts
`refuses ⊆ carded`, never the converse. Mutation **A4** proves the direction is
live: making bindcraft's prerequisites bullet contain the word "option" (so
`_needs_hotspots` drops it) while `validate()` still hard-refuses turns that
test red **by name**, plus the drift-alarm test. See §5.

Mutation **A3** proves the reverse looseness is real: doing the same to
boltzgen — which does *not* refuse — is caught **only** by the literal-set drift
alarm, not by the safety lock. That is the designed behaviour and it is
justified, not merely convenient: the card is a subset guarantee ("no
unannounced wall"), and boltzgen's card is a courtesy, not a wall.

### 2d. Residual: the card imports an inaccuracy from boltzgen's about panel

boltzgen's `about["prerequisites"]` says **"At least one hotspot residue."** and
`validate()` accepts an empty field (verified, §3). So that bullet is itself a
false requirement statement — the exact class of contradiction F2 was filed
under, one panel down. The PR's resolution is to make the *card* follow the
*panel* and soften the card's verb to "asks for", which makes the card true. The
panel is still wrong. Non-blocking, and arguably the right call for this PR
(changing boltzgen's stated prerequisites is a product decision, not a QC fix),
but it should be ticketed rather than considered closed. Same family as the iggm
item in §6.

### 2e. Attacking the prerequisites-text parser

`_needs_hotspots` is substring matching over human prose: `"hotspot" in b and
"option" not in b`, lower-cased, per bullet. It is fragile in both directions,
and I flipped it with two plausible copy edits:

- **A1** — proteina's *"Optionally, hotspot residues to aim the binder at a
  specific epitope."* rewritten as *"Hotspot residues, if you have them, to aim
  the binder at a specific epitope."* This is an ordinary copy-editor's
  rewrite. It flips proteina **on** and its card would then read "this tool asks
  for at least one", which is false.
- **A2** — a negation bullet added to esmfold2-design: *"No hotspot residues are
  needed — the gradient loop never touches the target structure."* The parser
  has no negation handling, so this flips esmfold2-design **on** as well.

The most realistic failure is **A1**: "if you have them" is the natural English
for "optional" and carries none of the parser's magic word.

**But the fragility is bounded, and this is the important finding**: both
mutations are caught, loudly, by
`test_the_card_set_is_the_tools_own_stated_prerequisites`, whose literal set
assertion is a deliberate drift alarm. Nothing can ship silently. The residual
risk is social rather than technical — the alarm's failure message is
`assert carded == {...}, sorted(carded)`, and the obvious repair for someone who
did not write this code is to paste the new set into the literal. A message
naming the bullet that changed would make the alarm harder to mis-fix. Minor;
see §7.1.

## 3. The M8b order-dependence fix — verified

Running the probe directly against all fourteen adapters:

```
af2              clean
bindcraft        HOTSPOT   At least one hotspot residue is required.
boltz2           clean
boltzgen         clean
colabfold        clean
esmfold          clean
esmfold2-design  clean
iggm             clean
mpnn             clean
opendde          clean
proteina         clean
pxdesign         HOTSPOT   At least one hotspot residue is required.
rfantibody       HOTSPOT   At least one hotspot residue is required.
rfdiffusion      HOTSPOT   At least one hotspot residue is required.
```

No adapter errors for a non-hotspot reason, so `PROBE_FORM` really does reach
the end of all fourteen chains. The three adapters round 1 named — boltz2, iggm,
esmfold2-design — now all validate clean rather than short-circuiting.

I then tried to blind the probe on three different adapters by inserting an
early required field of a kind `PROBE_FORM` does not populate, immediately
before the `preset = …` line in each `validate()`:

```python
if not (form.get("qc_scoring_mode") or "").strip():
    return None, "Pick a scoring mode."
```

Results in the mutation table (§5, rows B1–B3): the guard-of-the-guard
`test_the_probe_reaches_every_adapters_hotspot_check` fires on **each** of
bindcraft, mpnn and boltz2 — one hotspot-refusing tool, one that ignores
hotspots entirely, and one round 1 specifically named as previously blind. The
lock is not vacuous on any of them.

## 4. The card-price guard — F3 and F4 verified caught

Round 1's two mutations, replayed:

- **C1 / F3** — drop `pilot=1` from `_pilot_context`'s `url_for`. Caught.
- **C2 / F4a** — mark pxdesign's `num_designs` input `disabled`. Caught.
- **C3 / F4b** — `<textarea>`: I removed the textarea branch from
  `_submitted_params`. Caught by the new synthetic-markup unit test.

Full attribution in §5. Every mutation was confirmed on disk with
`git diff --unified=0` **before** the suite was run; the runner refuses to run a
mutation whose diff is empty, so none of the two historical
silent-non-application failure modes (missed pattern, encoding mismatch) can be
mistaken for a pass here.

### Hunting a third hole

I looked for a card-price lie the repaired guard still misses. **I found one,
and it is live in the tree today.**

`templates/tools/proteina_form.html:308-312` disables controls with JavaScript
on page load:

```js
custom.querySelectorAll('input').forEach(function (el) {
  if (el.type !== 'checkbox' && el.type !== 'button') { el.disabled = !isCustom; }
});
```

`refresh()` runs at load, and with no file attached `isCustom` is false, so the
whole `#custom-fields` block is disabled before the user touches anything. The
harvest reads static HTML and sees no `disabled=` attribute. Measured on the
rendered `/tools/proteina?pilot=1` page:

```
harvest thinks the browser posts: _csrf, binder_length_max, binder_length_min,
    campaign_label, hotspot_residues, num_designs, preset, target_chain,
    target_input, task_name
JS-disabled at page load:          hotspot_residues, target_chain, target_input
harvested, but a real browser posts NOTHING for these three:
                                   hotspot_residues, target_chain, target_input
```

The harvest's own docstring now says "``disabled`` posts NOTHING, whatever its
``value=`` says" — true of the attribute, false of the property, and proteina is
the one form in the repo that sets the property.

**Today this is latent, not a lie**: proteina's scaling param is `num_designs`,
which lives outside `#custom-fields`, so the price is unaffected and all ten
pilot cards price correctly (§8). It becomes a real card-price lie the moment
any scaling-relevant field moves into `#custom-fields`, or the same
show/hide-plus-disable pattern is applied to another form — and the guard, whose
whole purpose is this bug class, would stay green through it. Non-blocking
finding; see §7.2.

**A second, narrower hole — the price guard misses duplicated names.**
Mutation **C4** inserts a hidden `name="num_designs" value="64"` *before*
pxdesign's real control. A browser posts both and Werkzeug's `request.form.get`
takes the **first**, while `_submitted_params` builds a plain dict and keeps the
**last** — so the card advertises $8.74 while the run submits the 64-design
price. `test_card_price_equals_the_estimator` **stayed green**; the mutation was
caught only by `TestPilotPrefillActuallyLands::test_every_param_reaches_the_form`
and a clone-roundtrip test, which happen to use the sibling helper
`_posted_value` — and that one is first-wins, matching Werkzeug.

So the two helpers in the same file disagree about duplicated names, and only
the one the price guard does **not** use is correct. The rescue is incidental:
it fires because `num_designs` is in pxdesign's `PILOT["params"]`. A duplicated
name on a field that is *not* a PILOT param would be caught by nothing. See
§7.4.

### How far the guard actually reaches — measured

Since this guard has now failed twice, I measured its sensitivity rather than
assuming it. For each pilot tool, the estimate at the pilot's pre-fill vs at the
form's own defaults (which is exactly what F3 makes the guard read):

| tool | scaling param | pilot | form default | estimate pilot / default | F3 detectable? |
|---|---|---|---|---|---|
| pxdesign | `num_designs` | 4 | 8 | $8.74 / $17.48 | **yes** |
| bindcraft | `num_designs` | 2 | 4 | $4.37 / $8.74 | **yes** |
| rfantibody | `num_designs` | 2 | 4 | $4.37 / $8.74 | **yes** |
| mpnn | `num_seq_per_target` | 8 | 50 | $0.03 / $0.16 | **yes** |
| rfdiffusion | `num_designs` | 8 | 4 | $1.46 / $1.46 | no |
| proteina | `num_designs` | 8 | 8 | $12.59 / $12.59 | no |
| boltzgen | `num_designs` | — | — | $8.74 / $8.74 | no |
| boltz2 | `n_designs_total` | — | — | $0.22 / $0.22 | no |
| iggm | `num_samples` | 1 | 1 | $0.08 / $0.08 | no |
| esmfold2-design | `n_seeds` | 1 | 1 | $9.87 / $9.87 | no |

The guard is live on four of the ten, and the six "no" rows are not blind spots
— sweeping each scaling param shows a billing floor that flattens the low end
(rfdiffusion: $1.46 at 1, 2, 4 and 8 designs, $2.34 at 16, $9.33 at 64), so on
those six the pilot price and the default price genuinely coincide and there is
no lie to detect. The guard is sensitive everywhere a difference exists. Round
1's control test being pointed at pxdesign is the right choice: it has the
widest margin of the four.

Two more gaps I reasoned about but did not mutate, both already disclosed by the
builder and neither a defect in this PR:

- The guard compares two calls to the *same* estimator, so it can only prove the
  card matches the estimate, never that either matches the metered charge. The
  softening to "the same estimate" is the honest wording and is correct.
- `\bdisabled\b` matches inside attribute *values*, and the input regex is
  case-sensitive. Measured against the real `_submitted_params`:

  ```
  harvest of  a(class="field-disabled") b(aria-disabled="true") c(plain)
              d(<INPUT> uppercase)      e(data-x="disabled")
    -> {'c': '3'}      # a browser posts all five
  ```

  `aria-disabled="true"` appears on ten of the fourteen tool forms today (on
  submit buttons, which are skipped anyway). Mutation **C7** puts
  `aria-disabled="false"` on pxdesign's `num_designs` and the guard goes **red**,
  which settles the direction empirically: a false positive drops a field, the
  two sides then disagree, and the guard fails loudly rather than certifying
  false. A robustness note, not a hole. The repo is not on Tailwind, so the
  `disabled:`-variant class that would trip it does not occur.

## 5. Mutation table

Every row was applied in an isolated worktree, confirmed on disk with
`git diff --unified=0` **before** the suite ran (the runner aborts the row if the
diff is empty, so neither of this repo's two historical silent-non-application
failure modes can be mistaken for a pass), then measured with the full suite
(`… -m pytest -q`, no path argument), then reverted. Baseline for comparison is
5262 passed / 20 skipped.

| # | mutation | landed | suite | caught by (test name) |
|---|---|---|---|---|
| **A1** | proteina's *"Optionally, hotspot residues…"* → *"Hotspot residues, if you have them, …"* | yes | 1F / 5261P | `TestHotspotDeflection::test_the_card_set_is_the_tools_own_stated_prerequisites` |
| **A2** | esmfold2-design gains *"No hotspot residues are needed — …"* | yes | 1F / 5261P | same |
| **A3** | boltzgen's bullet gains *"…there is no option to let the model pick…"* (card lost, tool does **not** refuse) | yes | 1F / 5261P | same — **only** the drift alarm, as designed |
| **A4** | bindcraft's bullet gains *"There is no option to skip this."* (card lost, tool **does** refuse) | yes | 2F / 5260P | `…::test_every_tool_that_refuses_without_hotspots_carries_the_card` **plus** the drift alarm |
| **B1** | early `qc_scoring_mode` requirement in `tools/bindcraft/__init__.py::validate` | yes | 43F / 5212P | `…::test_the_probe_reaches_every_adapters_hotspot_check` |
| **B2** | same, `tools/mpnn/__init__.py::validate` | yes | 50F / 5212P | same |
| **B3** | same, `tools/boltz2/__init__.py::validate` | yes | 9F / 5253P | same |
| **C1 (F3)** | drop `pilot=1` from `_pilot_context`'s `url_for` | yes | 2F / 5260P | `TestPilotCardPriceIsDerived::test_card_price_equals_the_estimator` + `…::test_the_guard_above_is_reading_the_form_and_not_the_pilot_dict` |
| **C2 (F4a)** | mark pxdesign's `num_designs` input `disabled` | yes | 2F / 5260P | same two |
| **C3 (F4b)** | delete the `<textarea>` branch from `_submitted_params` | yes | 1F / 5261P | `TestPilotCardPriceIsDerived::test_the_harvest_posts_what_a_browser_would_post` |
| **C4** | hidden `num_designs=64` **before** pxdesign's real control (first-wins vs last-wins) | yes | 2F / 5260P | `TestPilotPrefillActuallyLands::test_every_param_reaches_the_form` + `TestCloneRoundTrip::test_every_stored_input_named_by_a_field_reaches_it` — **not** the price guard (see §7.4) |
| **C5** | delete the `disabled` branch from `_submitted_params` | yes | 1F / 5261P | `TestPilotCardPriceIsDerived::test_the_harvest_posts_what_a_browser_would_post` |
| **C6** | pxdesign's `PILOT["params"]` loses `num_designs` (round 1's original price mutation) | yes | 3F / 5259P | `…::test_card_price_equals_the_estimator` + `…::test_the_guard_above_is_reading_the_form_and_not_the_pilot_dict` + `TestPilotPrefillActuallyLands::test_every_param_reaches_the_form` |
| **C7** | `aria-disabled="false"` on pxdesign's `num_designs` (false-positive blinding of the harvest) | yes | 2F / 5260P | `…::test_card_price_equals_the_estimator` + the control — fails **loudly**, confirming the over-match is the safe direction |

Every replay of round 1's blockers goes red against a named test. B1 and B2
carry large collateral failure counts because breaking an adapter's `validate()`
breaks a lot of unrelated tests; what matters is that the named
guard-of-the-guard is among them in all three.

## 6. Copy, and the handoff round trip

### 6a. Round trip driven end to end — verified for all four

Driving `GET /tools/<slug>?handoff=<id>` with a logged-in session and a stubbed
`Handoff(target_chain="B", hotspot_residues=[241,243,245],
pdb_filename="6xyz_target.pdb")`:

| tool | HTTP | `target_chain` | `hotspot_residues` | `preset` | staged-PDB banner |
|---|---|---|---|---|---|
| bindcraft | 200 | `B` | `241,243,245` | `pilot` | yes |
| boltzgen | 200 | `B` | `241,243,245` | `pilot` | yes |
| pxdesign | 200 | `B` | `241,243,245` | `pilot` | yes |
| rfantibody | 200 | `B` | `241,243,245` | `pilot` | yes |

All four claimed values land, in the form fields, on the page. Verified.

### 6b. The POST gate still refuses rfdiffusion — verified

`POST /scout/handoff/tool`, logged in:

```
'rfdiffusion'   -> 400  {"error":"Unknown tool: rfdiffusion"}
'RFDIFFUSION'   -> 400  (case-normalised)
' rfdiffusion ' -> 400  (whitespace-normalised)
'proteina'      -> 400
'boltz2'        -> 400
bindcraft / boltzgen / pxdesign / rfantibody -> 404 "Scout job not found"
```

The four pass the tool gate and stop only at the (absent) Scout job. rfdiffusion
is refused. The rfdiffusion card's copy — "you copy the residues it picks into
the field below" — matches that reality.

**Observation, not a defect:** the *receiving* side is not gated on
`VALID_HANDOFF_TOOLS` at all. `?handoff=<id>` prefills chain, hotspots, preset
and the staged PDB on rfdiffusion, proteina, boltz2, iggm and mpnn too — I drove
all fourteen and rfdiffusion fills in completely. So rfdiffusion's card now
explains a limitation that is one entry in `VALID_HANDOFF_TOOLS` plus one
`<option>` away from not existing. Writing the copy is the correct
*this-PR* move (the copy must match today's product); wiring rfdiffusion up is
the better follow-up.

### 6c. "asks for at least one" — accurate for all five, verified

`validate()` driven directly (§3) confirms bindcraft, pxdesign, rfantibody and
rfdiffusion refuse an empty hotspot field, and boltzgen accepts one. So "asks
for at least one" is true of all five and "will not start without at least one"
would have been false of boltzgen. The softening is accurate.

It is also, for four of the five, weaker than the truth: a reader who takes
"asks for" as a suggestion and leaves the field blank hits a hard validation
wall on bindcraft, pxdesign, rfantibody and rfdiffusion. A third derived flag
(`hotspot_required`, from the same probe the tests already run) would let each
card say the true thing. Minor; see §7.3.

### 6d. Bench-biologist read

Rendered card text, identical across the five but for the closing clause:

> **Hotspot residues** are the numbered residues on the patch of your target you
> want the binder to sit on, written in your PDB's own numbering — this tool
> asks for at least one. If you do not know yours yet, score your target's
> surface with Epitope Scout first — it is free, and its results hand the target
> and the residues back into this form.

rfdiffusion's closes "…it is free, and you copy the residues it picks into the
field below."

Reading as someone who knows their target protein but not the model names:

- No model names appear at all. Nothing to gloss.
- "Hotspot residues" is defined in the sentence that uses it, in physical terms
  ("the patch of your target you want the binder to sit on") rather than by
  reference to any algorithm. Good.
- "your PDB's own numbering" is the right warning in the right place, and it
  matches the form field's "original PDB numbering" below it. "PDB" is
  unglossed, but it is the file the reader uploaded — not jargon for this
  audience.
- "Epitope Scout" is a product name, immediately glossed by what it does ("score
  your target's surface") and priced ("it is free").
- "binder" is unglossed and is the one term a true outsider would miss, but the
  page is a binder-design tool; the reader arrived by choosing one.

**No unglossed jargon left.** The only wording I would still change is the
"asks for" softening discussed in §6c.

## 7. Findings (all non-blocking)

**7.1 — The prerequisites parser is prose matching, and its drift alarm invites
a bad repair.** `_needs_hotspots` flips on ordinary copy edits (§2e, mutations
A1/A2). Every flip is caught by
`test_the_card_set_is_the_tools_own_stated_prerequisites`, so nothing ships
silently — but that test fails with a bare `assert carded == {…}, sorted(carded)`
and the tempting fix is to update the literal. Suggest the message name the
bullet whose text moved, e.g. by diffing `_states_hotspot_prerequisite` per slug
in the failure text.

**7.2 — The harvest's `disabled` rule is attribute-only; proteina disables three
controls with JavaScript (§4).** Latent today because proteina's scaling param is
outside the affected block. Same bug class as F4, on the one form that already
uses the pattern. Cheapest containment is a test asserting no form both
JS-disables inputs and carries a scaling param among them; the honest fix is to
render `disabled` server-side for the non-custom state.

**7.3 — "asks for at least one" is true but weak for the four tools that hard-
refuse (§6c).** A third derived flag would let the card say "will not start
without one" where that is true and "asks for" where it is not, from the probe
the tests already run.

**7.4 — `_submitted_params` is last-wins on duplicated `name=`; a browser and
Werkzeug are first-wins (§4, mutation C4).** The price guard stayed green through
a genuine 16x understatement; only a sibling test using the *other* helper
(`_posted_value`, which is first-wins and therefore right) caught it, and only
because the field happened to be a PILOT param. One-line fix — skip a name
already in `out` — plus a case in the synthetic-markup unit test.

**7.5 — Round 1's F6 is closed, but three more `SESSION_SECRET_KEY`
`setdefault` leaks of the same shape survive** in
`tests/test_hotspot_picker_runtime.py:88`,
`tests/test_multichain_form_affordances.py:179`,
`tests/test_multichain_iptm_notice.py:254` (§1). Different files from the three
round 1 named; pre-existing, untouched by this PR. They weaken the "nothing was
unmasked" argument, not the fix itself.

## 8. Anonymous drive — verified, with the asserts that make it non-vacuous

`create_app()` + `test_client()`, no session, `FLAG_TOOL_*` all on:

```
ADAPTERS REGISTERED: 14   (assert len(slugs) == 14 — an empty registry would
                           make every check below vacuous)
TOOL PAGES 200: 14 / 14   (assert no non-200 — a bare test env 404s these)
```

Cards appear on exactly `{bindcraft, boltzgen, pxdesign, rfantibody,
rfdiffusion}`; the round-trip clause on exactly `{bindcraft, boltzgen, pxdesign,
rfantibody}`; the copy-across clause on exactly `{rfdiffusion}`.

Every pilot card's advertised price, re-derived from the href the card's own
button carries (not a URL I built):

| tool | card | the form it links to submits | href |
|---|---|---|---|
| bindcraft | $4.37 | $4.37 | `/tools/bindcraft?pilot=1` |
| boltz2 | $0.22 | $0.22 | `/tools/boltz2?pilot=1` |
| boltzgen | $8.74 | $8.74 | `/tools/boltzgen?pilot=1` |
| esmfold2-design | $9.87 | $9.87 | `/tools/esmfold2-design?pilot=1` |
| iggm | $0.08 | $0.08 | `/tools/iggm?pilot=1` |
| mpnn | $0.03 | $0.03 | `/tools/mpnn?pilot=1` |
| proteina | $12.59 | $12.59 | `/tools/proteina?pilot=1` |
| pxdesign | $8.74 | $8.74 | `/tools/pxdesign?pilot=1` |
| rfantibody | $4.37 | $4.37 | `/tools/rfantibody?pilot=1` |
| rfdiffusion | $1.46 | $1.46 | `/tools/rfdiffusion?pilot=1` |

Ten of ten agree.

## 9. The two deliberately-unfixed items — confirmed pre-existing, agree with the call

**boltzgen runtime.** Confirmed on trunk (`git show 66388af:tools/boltzgen/meta.py`)
and untouched by this PR's diff. There are in fact **three** numbers, not two:

- `when_to_use` (:88) — "roughly 5 to 60 min per run"
- `PRESET_RUNTIME` (:26) and `runtime_table` (:154) — "15 to 60"
- the FAQ answer (:56) — "typically finish in 30 to 90 minutes on a dedicated
  A100"

All three predate the PR. **Agree it is out of scope** — nothing in #154 touches
runtime copy — but the third variant makes it slightly worse than reported and
worth a ticket of its own.

**iggm epitope.** `tools/iggm/meta.py:56` says *"Optional: an epitope — click
antigen residues on the structure…"* while `tools/iggm/__init__.py:319` hard-
refuses:

```python
if not epitope:
    return None, "… antigen epitope and cannot infer one from an antigen-only …"
```

`git diff --stat 66388af HEAD -- tools/iggm/` is empty: entirely pre-existing and
untouched. **Agree it is out of scope.** It is the same class as F2 — a stated
prerequisite disagreeing with the enforced one — on a different field name, and
it should be filed. Note that it does not interact with the card: the bullet
contains no "hotspot", so `_needs_hotspots` never sees it, and `PROBE_FORM`
supplies `epitope: "45,46"` so the probe passes iggm cleanly.

## 10. Merge losslessness — verified

```
$ git merge-tree --write-tree 517e5f4 66388af
f7179b4a5aed4643e43351515872b1b9b0835115
$ git rev-parse e4e229b^{tree}
f7179b4a5aed4643e43351515872b1b9b0835115
```

Identical — the merge commit's tree is exactly what a clean automerge produces,
so no hand edit was smuggled in during resolution.

Both contested sides of `tests/test_tool_categories.py` survive:

- the PR's hunk — `test_every_adapter_resolves_its_meta_in_the_catalog(monkeypatch)`
  at :81, `monkeypatch.setenv` at :107-108, and **no** `os.environ` assignment
  left in the file;
- #146's hunk — `import pathlib` at :16, `test_readme_tool_table_matches_the_live_registry`
  at :269 and `test_readme_hardcoded_tool_table_matches_the_catalog` at :287.

Given `worktree-base-drift` has bitten this repo seven times and "a clean
automerge is the condition under which a lost hunk is invisible", I checked the
file's content rather than only the tree hash. Both survive.

## 11. What I verified empirically vs. reasoned about

**Verified by running it:** both suite baselines and the `--collect-only` node-id
diff and its +5/+7 attribution; the merge tree hash and the survival of both
contested hunks; all fourteen `validate()` probes; the three derived sets and
their pairwise relationships; all fourteen adapters registered and all fourteen
tool pages at 200 anonymously; all ten pilot card prices against the href each
card carries; the handoff prefill for all fourteen tools; the handoff POST gate
including case and whitespace normalisation; the rendered card copy for all five
carded tools; the harvest's behaviour on synthetic markup and on the live
proteina page; the estimator's sensitivity curve per tool; and all fourteen
mutations in §5, each confirmed on disk before measurement.

**Reasoned about but not run:** whether the metered GPU charge matches the
estimate (the container lives in `llm-proteinDesigner`; the builder discloses
this and the "same estimate" wording is the honest one); and whether a real
browser would post exactly what I derived from proteina's `refresh()` — I read
the JS and traced the load-time state rather than driving a browser, so the
proteina finding in §4/§7.2 rests on code reading plus the rendered HTML, not on
a live DOM.

**Not attempted:** any GPU run. Nothing in this PR needs one.
