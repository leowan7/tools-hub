# QC round 1 — PR #154 (`fix/qc147-followups`)

Independent QC. I did not build this. Reviewed **`9387be8`** against
`origin/main` = **`1bfce94`**, both confirmed by `git fetch origin` at the
start.

**Trunk moved mid-review.** By the end, `origin/main` was **`66388af`**
(#146, the concurrent redesign-phase-5 work). I re-measured everything
against it — see *"Trunk drift"* below. The PR head did not move: still
`9387be8`. The +7 delta and the green suite both hold against the new trunk.

All work done in throwaway worktrees under the session scratchpad. The main
working tree was never touched.

---

## VERDICT: **BLOCKED**

One factual error in new user-facing copy (**F1**), plus a mirror-image
scoping error (**F2**). Both are in the hotspot deflection, both are small
fixes. Everything else the PR claims is verified — the four defects are
genuinely fixed, the price guard is genuinely no longer tautological, and I
proved that with a mutation that is green on trunk and red on the head.

Two further findings (**F3**, **F4**) are *new* holes I opened in the
rewritten price guard: card-price lies that the whole suite, all 5239 tests,
does not catch. They are not regressions — trunk misses them too — but the
PR's stated purpose is closing exactly this class, so they belong in the
same change or an explicit follow-up.

---

## Measured baselines

Command, run from each worktree root with **no path argument**:

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| tree | SHA | result | wall |
|---|---|---|---|
| `origin/main` (at review start) | `1bfce94` | **5232 passed, 20 skipped** | 175.58s |
| PR head | `9387be8` | **5239 passed, 20 skipped** | 163.41s |
| `origin/main` (at review end) | `66388af` | **5250 passed, 20 skipped** | 155.19s |
| `9387be8` merged with `66388af` | — | **5257 passed, 20 skipped** | 183.23s |

Delta **+7 passed, +0 skipped** — matches the claim exactly, and reconciles:

- `TestPilotCardPriceIsDerived::test_the_guard_above_is_reading_the_form_and_not_the_pilot_dict` → 1
- `TestBudgetDoesNotChangeThePrice` (3 tests) → 3
- `TestHotspotDeflection` (3 tests) → 3

The **skip count is unchanged at 20 on both sides**. That is the check that
matters for claim 3: a leaked `FLAG_TOOL_*` makes other tests run that would
otherwise skip, so if the removed leak had been enabling anything, removing
it would have moved the skip count. It did not. Nothing was being masked.

---

## Trunk drift — re-checked, clean

`origin/main` advanced from `1bfce94` to `66388af` (#146) while I was
reviewing. That commit is the concurrent agent's redesign-phase-5 work
(`templates/help/*`, `README.md`, `blueprints/public.py`,
`tests/test_help_tool_guides.py`) — which I was told to ignore — **but it
also appends 46 lines to `tests/test_tool_categories.py`, a file this PR
edits.** That makes it a contested file, and a contested file that
auto-merges clean is exactly the condition under which a dropped hunk is
invisible. So I checked rather than assumed:

- `git merge origin/main` into `9387be8`: **auto-merged, zero conflicts.**
- **Both sides survived.** The PR's hunk is intact
  (`test_every_adapter_resolves_its_meta_in_the_catalog(monkeypatch)` with
  both `monkeypatch.setenv` calls, at :81-108) and #146's two new
  README-table tests are intact. They land in disjoint regions of the file
  (PR at :81-108, #146 appended at :249+), which is why the automerge is
  actually safe here and not merely quiet.
- **Merged suite: 5257 passed, 20 skipped — green.**
- New trunk `66388af` alone: **5250 passed, 20 skipped**. So the delta is
  still exactly **+7**, and it reconciles to the same three groups.

#146 touches nothing on the pilot-card, estimator or hotspot paths, so every
mutation result below (all measured against `9387be8`) stands unchanged.

---

## Mutation table

Every mutation was applied by a UTF-8-safe Python string replacement that
**asserts its pattern exists** (so a missed pattern is a hard error, not a
silent no-op), and every one was confirmed on disk with
`git diff --unified=0` before any conclusion was drawn. Landed-line counts
are from that diff. Failures are attributed by test name.

Runs marked *(filtered)* used `-k` only — `-k` deselects, it does not change
collection scope, so no path argument was ever passed. Every mutation that
came back green under the filter was **re-run against the full suite** to
confirm nothing else caught it.

| # | mutation | landed? | caught by (head `9387be8`) | on trunk `1bfce94` |
|---|---|---|---|---|
| M1 | `_pilot_context` prices `{"preset": ...}` only — pure card-side lie | yes (1 line) | `test_card_price_equals_the_estimator` | **also caught** |
| M2 | delete `num_designs` from pxdesign's `PILOT["params"]` | yes (1 line) | `test_card_price_equals_the_estimator` **and** `test_the_guard_above_is_reading_the_form_and_not_the_pilot_dict` | **GREEN — the hole** |
| M3 | drop `rfdiffusion` from `HOTSPOT_REQUIRED_TOOLS` | yes (1 line) | `test_the_constant_matches_what_the_adapters_actually_refuse` | n/a (new) |
| M4 | add `mpnn` to `HOTSPOT_REQUIRED_TOOLS` | yes (1 line) | `test_the_constant_matches_what_the_adapters_actually_refuse` | n/a (new) |
| M5 | restore *"Higher budgets cost more and run longer"* to `meta.py` | yes (1 line) | `test_no_page_surface_claims_a_higher_budget_costs_more` | n/a (new) |
| M6 | boltzgen `ToolSpec.scaling_param` → `"budget"` (make budget really scale) | yes (1 line) | `test_the_estimate_really_is_flat` + `test_a_no_op_pilot_is_only_allowed_when_nothing_cheaper_exists` | n/a (new) |
| M7 | `build_payload` `num_designs: 200` → `inputs["budget"]` | yes (1 line) | `test_build_payload_pins_the_candidate_pool` | n/a (new) |
| M8a | boltz2 refuses empty hotspots, check placed **early** in `validate()` | yes (2 lines) | `test_the_constant_matches_what_the_adapters_actually_refuse` | n/a (new) |
| M8b | same refusal, check placed **late** in `validate()` | yes (3 lines) | **NOT caught by the deflection guard.** Full suite: caught only incidentally by `test_submit_chain_gate[boltz2]` (4 failures) | n/a (new) |
| M9 | `_pilot_context` `url=` drops `pilot=1` | yes (1 line) | **ESCAPES. Full suite 5239 passed, 20 skipped — byte-identical to unmutated head.** | escapes |
| M10 | add `disabled` to pxdesign's `num_designs` input | yes (1 line) | **ESCAPES. Full suite 5239 passed, 20 skipped.** | escapes |

### The decisive one is M2

M2 is the mutation that separates the old guard from the new one, and it
does so cleanly:

- on **trunk**: `502 passed, 3 skipped` — **green**. The tautology was real.
- on **head**: 2 failures, both in `TestPilotCardPriceIsDerived`.

Note also that M1 — the builder's "mutation 3", the pure card-side lie — is
caught by the *old* test too. It is not evidence for the rewrite; the old
assertion compared the card against `estimate(PILOT["params"])`, and a card
built from a *different* dict diverges from that just as it diverges from the
new one. **M2 is the only one of the two that demonstrates the fix.** The
claim that "nothing else in the suite catches" M1 is true in the narrow sense
that only the price guard catches it — but that was already so before this PR.

---

## Claim 1 — budget→cost. **VERIFIED, with one caveat (F7)**

**Re-derived independently.** `estimated_cost_for_tool(None, "boltzgen",
{"preset": "pilot", "budget": B})` returns **`8.7380` at B = 1, 4, 10, 50,
200**, at the string forms `"1"/"4"/"50"`, at the garbage value `"abc"`, and
**with the key absent**. Flat, exactly as claimed.

**Mechanism confirmed by reading, not by trusting the numbers:**

- `shared/wallet_estimates.py:277` — boltzgen's `ToolSpec.scaling_param` is
  `"num_designs"`.
- `tools/boltzgen/__init__.py:164` — `build_payload` writes
  `"num_designs": 200`, a literal, with `budget` passed separately at :165.
- The boltzgen form never renders a `num_designs` field, so the estimator
  falls to the baseline every time.

The lever is live, which is what makes the flatness a real property and not
an artifact: `{"preset":"pilot","num_designs":4}` → `17.4760`,
`num_designs=200` → `300.0000` (absolute cap). M6 and M7 both go red, so both
halves of the mechanism are pinned by tests.

**Repo-wide sweep for surviving claims.** I swept every `meta.py`, form
template, help page, `about` dict, FAQ/`seo_faq` entry, README and JSON-LD
for `budget` co-occurring with cost/runtime language. Findings:

- Both copies the builder named are gone, and M5 proves the rendered-page
  guard would catch either coming back.
- `tools/boltzgen/meta.py:88` — *"a budget-tunable number of candidates"* —
  **clean and accurate.** `budget` genuinely does tune how many candidates
  you receive; only the *price* was the lie.
- `templates/components/results_shell.html:43` — *"try increasing the
  budget"* — **clean**, and for boltzgen specifically it is now also *free*
  advice, which is a nicer outcome than before.
- `tools/boltzgen/meta.py:188` — a source comment, not user copy, and it
  states the flat behaviour correctly.
- Nothing else user-facing anywhere in the tree.

**Rendered-page check** (anonymous `create_app()` + `test_client()`, all 14
flags on): `/tools/boltzgen` contains **none** of `budgets cost more`,
`budget costs more`, `run longer`, `Higher budgets`, `Start with 4 designs`,
`then scale up`, `start small`; and it **does** contain both
`at the same price` and `does not change what the run costs`.

### F7 — the new copy asserts the positive, and that half is still unverified

Non-blocking, but it should not be left implicit.

Users are billed **metered-actual**, not the estimate —
`templates/pricing.html:123`: *"places a wallet hold against a live estimate,
then settles at the actual compute consumed"*, and `docs/VALIDATION-LOG.md`
records a real boltzgen job settling at hold + true-up = metered actual. So
*"this does not change what the run costs"* (`meta.py`), *"at the same
price"* (form), and *"costs you nothing extra"* (`PILOT["next_step"]`) are
claims about **GPU seconds burned inside a container in a different repo**
(`llm-proteinDesigner`) — which the builder explicitly did not verify.

Dropping the *"run longer"* half rather than negating it was the right call.
But the *cost* half was not dropped — it went from unverified claim A to
unverified claim not-A. The inference behind it is sound (`build_payload`
pins the pool at 200; the wrapper's own comment at
`tools/boltzgen/__init__.py:100-106` calls `budget` a top-N **selection** out
of that pool), and I judge it very likely correct — but it is inference, and
the code comment presents it as measured. Two notes:

- the `PILOT` `goal`/`next_step` carrying this already shipped in **#147**;
  this PR extends it to the about panel and the form field.
- the cheap close is a `gpu_seconds` comparison across two budgets on the
  next real boltzgen run, or one sentence in the `meta.py` comment marking
  the container half as inferred.

**I could not run this.** The container is not in this repo.

### F8 — minor, pre-existing, not introduced here

`tools/boltzgen/meta.py` `when_to_use` says *"roughly 5 to 60 min per run"*
while `runtime_table` and the module docstring both say **15** to 60 min.

---

## Claim 2 — the card-price guard is no longer tautological. **VERIFIED**

M2 proves it: green on trunk, red on head. The rewrite closes a real
4x-understatement hole on a publicly indexable page.

**Vacuous-collection check — passes.** The `tools_app` fixture
(`tests/test_pilot_recipes.py:46`) asserts `len(slugs) >= 14` before doing
anything, with a comment naming the `import app` side effect. The guard
itself additionally asserts `checked >= 5` (pilot cards actually priced) and
`assert submitted` per tool (form fields actually harvested). All three
layers are real. I confirmed independently that the registry holds exactly
**14** adapters and that all **14** tool pages return **200** anonymously —
see the drive section below — so none of this is passing over an empty set.

The new control test (`test_the_guard_above_is_reading_the_form_and_not_the_pilot_dict`)
is well-aimed: it re-checks that `num_designs` still moves pxdesign's
estimate, so the guard cannot silently decay into pricing a key the estimator
stopped reading.

### F3 — a card-price lie the new guard still misses (M9)

**Constructed and confirmed: full suite green, 5239 passed / 20 skipped,
identical to unmutated head.**

`_pilot_context` builds two things from the same recipe: the price, and the
**link the "Start this pilot" button points at** (`blueprints/tools.py:742`,
`url_for("tools.tool_form", tool=adapter.slug, pilot=1)`). The guard harvests
from a **hard-coded `?pilot=1`** URL it constructs itself — not from
`pilot["url"]`. Drop `pilot=1` from that `url_for` and:

- the card still advertises the pilot price,
- the button now lands the user on the form's **own defaults**,
- and `TestNoPilotIsANoOp::test_a_no_op_pilot_is_only_allowed_when_nothing_cheaper_exists`
  *already asserts* those defaults are strictly more expensive for every tool
  whose pilot differs from them. For pxdesign that is default `num_designs=8`
  vs pilot `4` — **$17.48 against a card reading $8.74**, the exact 4x-class
  understatement this PR exists to prevent, arrived at from the other end.

No test anywhere asserts `pilot["url"]` contains `pilot=1`.

The lazy fix is also the correct one: have `_submitted_params` fetch
`pilot["url"]` instead of building `f"/tools/{slug}?pilot=1"`. That makes the
guard price *what the button actually links to* and closes F3 with no new
test.

### F4 — a second one (M10)

**Also confirmed: full suite green, 5239 passed / 20 skipped.**

`_submitted_params` does not honour `disabled`. A browser posts nothing for a
disabled input; the regex harvest reads its `value=` anyway. Adding
`disabled` to pxdesign's `num_designs` input (`templates/tools/pxdesign_form.html:246`)
means the real POST carries no `num_designs` → real cost `$17.48` against a
card reading `$8.74` → and the guard agrees with the card, because it thinks
the field was submitted.

Same file, related latent gap: `<textarea>` is not harvested at all — only
`<input>` and `<select>`. No tool's scaling param is a textarea today, so
this is dormant rather than live, but it is the same blind spot.

Both are one-line fixes in `_submitted_params` (skip tags matching
`\bdisabled\b`; add a `<textarea>` pass).

---

## Claim 3 — the `os.environ` leak fix. **VERIFIED, one residue (F6)**

Both named leaks are genuine and genuinely fixed — confirmed by reading the
diff:

- `tests/test_tool_categories.py::test_every_adapter_resolves_its_meta_in_the_catalog`
  set 14 `FLAG_TOOL_*` plus `SESSION_SECRET_KEY` via bare `os.environ` with
  no restore → now `monkeypatch`.
- `tests/test_public_tool_pages.py::TestPublicContextIsBuiltOncePerRequest._client`
  — the **second** leak of the same shape, correctly found. It now delegates
  to the module's `all_tools_app` fixture, which is function-scoped and uses
  `monkeypatch.setenv` throughout (`:412-428`). Clean.

**Masking check.** Skip count is 20 on both trunk and head. Had either leak
been enabling tests that would otherwise skip, removing it would have moved
that number. It did not.

**Full sweep of every test #147 added.** #147 touched six test files
(`test_clone_roundtrip`, `test_login_redirect`, `test_pilot_recipes`,
`test_public_tool_pages`, `test_tool_categories`, `test_worked_examples`).
I swept all six plus the rest of `tests/` for `os.environ[...] =`,
`os.environ.setdefault`, and `os.environ.pop`.

### F6 — three residual leaks of the same shape, not fixed

Module-scoped fixtures that restore their `FLAG_TOOL_*` correctly but do
**not** restore `SESSION_SECRET_KEY`:

- `tests/test_pilot_recipes.py:52`
- `tests/test_worked_examples.py:64`
- `tests/test_clone_roundtrip.py:123`

all `os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")` with no
matching pop in teardown.

**Impact today: none that I can demonstrate.** I grepped for any test that
depends on `SESSION_SECRET_KEY` being *absent* and found none — every other
consumer sets it via `monkeypatch.setenv`. So this is hygiene, not a live
bug. But it is the same class the PR claims to have swept, in files #147
introduced, and `setdefault` is precisely the spelling that reads as safe
and is not.

---

## Claim 4 — the hotspot deflection. **SET CORRECT, SCOPING WRONG (F1, F2)**

### The set is right — re-derived independently, by a different method

I did not reuse the test's probe. I drove **all 14** adapters (not just the
10 with a `PILOT`), under **two** input regimes — a deliberately
over-populated generic form, so no tool could be excluded merely for
tripping on some *other* missing field first, and the tool's own PILOT
params — and printed the **full error string** for each so a
mis-attribution would be visible:

```
REFUSES-EMPTY-HOTSPOT (any probe): ['bindcraft', 'pxdesign', 'rfantibody', 'rfdiffusion']
CONSTANT                        : ['bindcraft', 'pxdesign', 'rfantibody', 'rfdiffusion']
MATCH: True
```

I separately chased the one plausible false negative. `boltz2` **does** have
a `hotspot_residues` field and 28 references to hotspots in its adapter, and
my generic probe short-circuited on its preset — so I drove it with a fully
valid form (`preset=msa_server`, a real binder sequence, empty hotspots) and
it returns cleanly with `hotspot_residues: []`. `proteina` likewise accepts
an empty list. Reading `tools/boltzgen/__init__.py:78` (`if raw_hotspots:`)
confirms boltzgen's tolerance at the source.

**No tool is wrongly included and none is wrongly excluded on the
`validate()` criterion.** M3 and M4 both go red, so the constant is pinned in
both directions.

### F1 — BLOCKER: the handoff sentence is false on rfdiffusion

The new copy, rendered on all four cards:

> …score your target's surface with Epitope Scout first — it is free, and
> **its results hand the target and the residues back into this form.**

That handoff is real, and I traced it end to end rather than taking the
copy's word: `scout/routes.py:1195` `POST /scout/handoff/tool` →
`create_handoff` → `handoff_redirect_url` → `/tools/<slug>?handoff=<id>` →
`blueprints/tools.py:1066-1080`, which prefills `target_chain`,
`hotspot_residues` and the staged PDB. It works.

**But it is gated on a whitelist that does not include rfdiffusion:**

```
scout/routes.py:1192   VALID_HANDOFF_TOOLS = {"rfantibody", "bindcraft", "pxdesign", "boltzgen"}
blueprints/tools.py    HOTSPOT_REQUIRED_TOOLS = {"bindcraft", "pxdesign", "rfantibody", "rfdiffusion"}
                       deflected-but-no-handoff: ['rfdiffusion']
```

and the Scout UI agrees — `templates/scout/feasibility.html:183-186` offers
exactly four `<option>`s: rfantibody, bindcraft, pxdesign, boltzgen. There is
no "jump to RFdiffusion".

So on `/tools/rfdiffusion?pilot=1` the card promises a handoff that does not
exist. The user this copy was written for — knows their target, does not know
the model — follows the link, runs Scout, looks for the button back to
RFdiffusion, and does not find it. That is worse than the wall the PR set out
to remove, because it costs them the round trip first.

Two clean fixes: render the *"its results hand… back into this form"* clause
only when `adapter.slug in VALID_HANDOFF_TOOLS` (and keep the Scout pointer
itself unconditional), or add `rfdiffusion` to `VALID_HANDOFF_TOOLS` — but
that is a real feature with its own QC surface, not a copy fix.

Related, smaller: the handoff panel lives on `/scout/feasibility`, which is
`@login_required` (302 → `/login`), and `handoff_to_tool` and `get_handoff`
are both user-scoped. `/scout/` itself is anonymous and returns **200**, so
*"it is free"* is fine — but the "hands back into this form" half needs an
account on **all four** tools, which the copy does not say.

### F2 — the mirror image: boltzgen is excluded, and it is the one that works

`HOTSPOT_REQUIRED_TOOLS` is derived from `validate()` *strictness*. But what
a bench biologist reads is the tool's **stated** requirements, and those do
not agree with `validate()`:

| tool | deflection card? | `about.prerequisites` says | Scout handoff target? |
|---|---|---|---|
| bindcraft | yes | "At least one hotspot residue index…" | yes |
| pxdesign | yes | "At least one hotspot residue." | yes |
| rfantibody | yes | "At least one hotspot residue defining the epitope face." | yes |
| **rfdiffusion** | **yes** | "At least one hotspot residue." | **no** ← F1 |
| **boltzgen** | **no** | **"At least one hotspot residue."** | **yes** ← F2 |
| proteina | no | "*Optionally*, hotspot residues…" | no |
| boltz2 | no | "*Optional*: a list of antigen hotspot residues…" | no |

boltzgen's own about panel says a hotspot residue is required, and its
`PILOT["you_need"]` asks for *"at least one residue on the face you want
bound"* — yet it gets no card-level pointer, because `validate()` happens to
tolerate an empty field. So the deflection is **inverted on exactly the two
tools where it matters**: the tool that can receive a Scout handoff does not
advertise it, and the tool that advertises it cannot receive one.

Partial mitigation, and it is worth stating: the pre-existing
`templates/tools/_hotspot_scout_hint.html` **is** included on boltzgen's form
(and on bindcraft, proteina, pxdesign, rfantibody, rfdiffusion). So the wall
is not total on boltzgen. But that partial is precisely the below-the-fold
hint whose position the PR's own rationale calls insufficient — which is the
argument for the card in the first place.

Sharpening the criterion from "`validate()` refuses" to "the tool's stated
prerequisites require one" would put boltzgen in and keep proteina/boltz2 out
(their prerequisites say *optional*), and would need no change to the
adapters.

### F5 — the drift-lock is order-dependent, so it is weaker than its comment

`blueprints/tools.py:704-707` says the test *"drives every adapter's
validate() with an empty hotspot field and asserts this set EXACTLY, so it
cannot drift off the adapters."*

It can, in one direction. `_rejects_empty_hotspots` probes with
`{"target_chain": "A", "hotspot_residues": ""}` plus `PILOT["params"]`. For
boltz2, iggm and esmfold2-design that form **short-circuits on a different
missing field** ("Paste at least one binder sequence", "Paste the antibody
heavy chain…", "Pick a target preset…") before any hotspot check could run.

M8a/M8b demonstrate the asymmetry directly. The *same* new requirement in
boltz2's `validate()`:

- placed **early** (M8a) → caught, `test_the_constant_matches_what_the_adapters_actually_refuse` fails.
- placed **late** (M8b) → **the deflection guard stays green.** Full suite
  caught it only incidentally, via four unrelated
  `tests/test_submit_chain_gate.py::…[boltz2]` failures — luck, not the
  guard, and luck that would not repeat for iggm or esmfold2-design.

So: **removals** from the set are reliably locked (M3), **additions** only
for tools whose PILOT params otherwise validate. The comment should say that,
or the probe should feed each adapter a form that actually reaches its
hotspot check.

### Reading the new copy as the target user

> **Hotspot residues** are the numbered residues on the patch of your target
> you want the binder to sit on, and this tool will not start without at
> least one.

**This is good.** It is the first thing on any of these pages that answers
*"which residues?"* rather than *"what syntax?"*, and "the patch of your
target you want the binder to sit on" is actionable by someone who knows
their protein and nothing about the model. No model names, no unglossed
jargon. "Binder" is left unglossed but is unavoidable and used consistently
site-wide.

One residual ambiguity, and it is the classic footgun for this exact user:
**"numbered" does not say *whose* numbering.** A bench biologist reads
residue numbers off their own PDB, and author numbering vs a 1-indexed
renumbering is where they get silently wrong answers. The form field below
does say *"in original PDB numbering"* — so the card is not wrong, just
looser than the thing it is trying to front-run. Adding *"in your PDB's own
numbering"* costs five words.

---

## Anonymous drive of the app

`create_app()` + `test_client()`, all `FLAG_TOOL_*` on, no session. Both
non-vacuity assertions are stated and both hold:

```
ADAPTERS REGISTERED: 14  ['af2','bindcraft','boltz2','boltzgen','colabfold','esmfold',
                          'esmfold2-design','iggm','mpnn','opendde','proteina',
                          'pxdesign','rfantibody','rfdiffusion']
PAGE CODES: all 14 -> 200
```

Pilot cards — **10 tools have a PILOT, all 10 render a price**, and the
deflection appears on exactly the four claimed tools and nowhere else:

| tool | card price | Scout deflection |
|---|---|---|
| bindcraft | $4.37 | yes |
| boltz2 | $0.22 | no |
| boltzgen | $8.74 | no ← see F2 |
| esmfold2-design | $9.87 | no |
| iggm | $0.08 | no |
| mpnn | $0.03 | no |
| proteina | $12.59 | no |
| pxdesign | $8.74 | yes |
| rfantibody | $4.37 | yes |
| rfdiffusion | $1.46 | yes ← see F1 |

`/tools/boltzgen` rendered anonymously contains no surviving budget→cost or
budget→runtime claim, and does contain both true statements. `/scout/`
returns 200 anonymously; `/scout/feasibility` 302s to `/login`.

---

## What I verified empirically vs. reasoned about

**Ran and measured:** both suite baselines; the boltzgen price sweep across
budgets, types and absence; the `num_designs` lever; all 11 mutations, each
with its landed diff shown before any conclusion; M1/M2 replayed against
trunk; the independent 14-adapter two-regime hotspot re-derivation; the
boltz2 and proteina empty-hotspot probes; the anonymous 14-page drive; the
10 pilot cards; the `VALID_HANDOFF_TOOLS` / `HOTSPOT_REQUIRED_TOOLS`
set difference; `/scout/` and `/scout/feasibility` reachability.

**Read but could not run:** the Scout→tool handoff as a live round trip
(`create_handoff` needs Supabase and a signed-in user — I traced the route
chain and the whitelist by reading, which is what establishes F1, and the
whitelist is a literal so the conclusion does not depend on running it).

**Could not verify at all:** whether budget changes actual GPU seconds
consumed. The container lives in `llm-proteinDesigner`. This is F7, and I
am not reporting it as verified in either direction.

---

## Summary of findings

| id | severity | finding |
|---|---|---|
| **F1** | **blocker** | rfdiffusion's new card promises a Scout handoff that does not exist (`VALID_HANDOFF_TOOLS` excludes it; the Scout UI offers 4 other tools) |
| **F2** | **blocker-adjacent** | boltzgen is excluded from the deflection although its own prerequisites require a hotspot *and* it is a valid handoff target — the scoping is inverted on the two tools that matter |
| F3 | high | `pilot["url"]` losing `pilot=1` is a 2x card-price understatement that the entire suite misses (M9, full suite green). Fix: harvest from `pilot["url"]`, not a hard-coded `?pilot=1` |
| F4 | high | `_submitted_params` ignores `disabled` (M10, full suite green) and skips `<textarea>` entirely |
| F5 | medium | the deflection drift-lock only catches *additions* for tools whose PILOT params otherwise validate; the code comment overstates it (M8a vs M8b) |
| F6 | low | three #147 fixtures still leak `SESSION_SECRET_KEY` via `setdefault` with no restore — same class, no demonstrable impact today |
| F7 | low | *"does not change what the run costs"* is an unverified claim about a container in another repo, on a metered-actual billing model; sound inference, presented as measured |
| F8 | trivial | pre-existing 5-vs-15 min runtime inconsistency in boltzgen's `meta.py` |

**What is verified and good:** the +7 test delta reconciles exactly; the
price guard is genuinely de-tautologised and M2 proves it against trunk; the
budget→cost claim is false and every copy of it is gone from every rendered
surface; the hotspot-required *set* is correct, independently re-derived by a
different method; both named env leaks are real and fixed with no masking;
and the new hotspot gloss is a genuine improvement in readability for the
user it was written for.

**Blocking on F1 and F2** — both are copy/scoping fixes in
`blueprints/tools.py` and `templates/components/pilot_card.html`, not
redesigns.
