# QC round 3 — PR #147 `feat/redesign-phase-2-3-pilots-examples`

Independent QC. I did not build this. Every builder claim below was re-derived
from scratch; nothing is carried over from rounds 1 and 2.

- **SHA reviewed:** `2b09c8530846f4f716443f09ec54822ac5ca52cf`
- **Merge commit in it:** `cfa66c65d9296e00acfaa1d5e27072d9e70f6e86` (parents `150f0da` + `4b7af64`)
- **Trunk at review time:** `origin/main` = `4b7af64d7773a56f32acd8a254f89cd46493f8d2`
- **merge-base(main, head)** = `4b7af64` — the branch is fully up to date with trunk.
- Both SHAs confirmed with `git fetch origin` at the start of this round; neither had moved.
- Work done in three throwaway detached worktrees under the session scratchpad.
  The main working tree (on `fix/pin-gpu-image-digests`) was never touched.

## VERDICT

**MERGE.** See the full statement at the end of this document.

---

## 1. Suite baselines — VERIFIED, all three figures reproduce exactly

Command, run from each worktree's repo root, **no path argument**:

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| rev | what | result |
|---|---|---|
| `773b955` | branch base (builder's baseline) | **4927 passed, 19 skipped** (156.66s) |
| `4b7af64` | `origin/main` | **5190 passed, 20 skipped** (166.24s) |
| `2b09c85` | PR head | **5232 passed, 20 skipped** (176.41s) |

Both builder figures (4927/19 and 5232/20) reproduce to the test. Green on all three.

Delta head − base = **+305 passed, +1 skipped = +306 collected**, exactly the
reconciliation claimed.

### The extra skip is main's, not this PR's — VERIFIED

```
SKIPPED [1] tests/test_scout_anonymous_access.py:188: freesasa is not installed
```

`tests/test_scout_anonymous_access.py` is ABSENT at `773b955`, PRESENT at
`4b7af64` and at head; introduced by `a2859f8` (the PR #148 scout-anonymous
chain). 61 collected items from that file at main and at head, 0 at base.
Claim confirmed.

### Per-file additivity — VERIFIED, and I widened it past the two files claimed

The builder's argument rests on two files summing additively. I ran
`pytest -q --collect-only` (**no path argument**, full-suite scope) at all four
revs and compared **every** test file, not just the two conflicted ones:

| file | base `773b955` | branch `150f0da` | main `4b7af64` | head `2b09c85` | base+Δbranch+Δmain |
|---|---|---|---|---|---|
| `tests/test_login_redirect.py` | 7 | 12 | 163 | **168** | 7 + 5 + 156 = **168** ✓ |
| `tests/test_tool_categories.py` | 1 | 2 | 7 | **8** | 1 + 1 + 6 = **8** ✓ |

Both claimed sums confirmed at collected-item granularity.

Full sweep — `head[f] == branch[f] + main[f] - base[f]` for **every** test file
in the suite, with exactly two intentional exceptions, both from the fix commit
`2b09c85` itself:

```
tests/test_clone_roundtrip.py   base=0 branch=6  main=0 head=9   (expected 6, +3 new)
tests/test_pilot_recipes.py     base=0 branch=15 main=0 head=16  (expected 15, +1 new)
```

- Files that lost tests entirely on head: **none**.
- Files where `head < max(branch, main)`: **none**.
- Collection totals: base 4946, branch 4984, main 5210, head 5252.
  `4984 + 5210 − 4946 = 5248`, `+4` = the four tests the fix commit added. Balances.

This is a stronger result than the builder claimed: the merge is additive on
*every* file, not just the two it happened to check.

---

## 2. The merge is lossless

### 2a. Mechanical re-derivation — VERIFIED

```
git merge-tree --write-tree 150f0da 4b7af64   ->  tree f471e763...
```

Exactly three conflicts, exactly the three disclosed:

```
CONFLICT (content): blueprints/public.py
CONFLICT (content): tests/test_login_redirect.py
CONFLICT (content): tests/test_tool_categories.py
```

`git diff --name-status cfa66c6^{tree} f471e763...` returns **only those three
paths**. Every other path in the real merge commit is byte-identical to what
git's own automerge produced — so nothing was hand-edited under cover of the
merge.

### 2b. Both conflict resolutions are strictly additive — VERIFIED

`git diff 4b7af64 2b09c85 -- tests/test_login_redirect.py` is a pure `+118`
insertion at line 145. Main's `HOSTILE` (33 cases), `SAFE` (8 cases), and all
11 of main's test functions are untouched.

The branch side lost two *names* — `test_login_rejects_offsite_next` and
`test_login_preserves_safe_next` — and I checked whether that is coverage loss.
It is not: main's `test_login_post_rejects_offsite_next` /
`test_login_post_preserves_safe_next` are the renamed versions, and the branch's
parametrize lists are proper subsets of main's:

- branch HOSTILE `//evil.com`, `/\evil.com`, `https://evil.com`, `http://evil.com` — all 4 in main's HOSTILE.
- branch SAFE `/jobs`, `/account/wallet`, `/tools/mpnn` — all 3 in main's SAFE.

`test_tool_categories.py` is likewise a pure `+46` insertion; all 6 of main's
tests survive verbatim.

### 2c. The clean automerges — this is where I went looking

`git diff 4b7af64 2b09c85` over each file main's recent PRs touched:

| PR | file | head vs main |
|---|---|---|
| #152 open redirect | `blueprints/auth.py` | **byte-identical** |
| #152 | `blueprints/wallet.py` | **byte-identical** |
| #153 signup credit | `shared/wallet.py` | **byte-identical** |
| #153 | `shared/email.py` | **byte-identical** |
| #153 | `templates/email/send_signup_credit.html` | **byte-identical** |
| #153 | `tests/test_signup_credit_single_source.py` | **byte-identical** |
| #153 | `tests/test_email_real.py`, `tests/test_money_display_surfaces.py` | **byte-identical** |
| #145 bands | `shared/category_glyphs.py` | **byte-identical** |
| #145 | `templates/index.html`, `templates/tools/comparison.html` | **byte-identical** |

`safe_next()` and the whole signup-credit single-source change live entirely in
files that head does not touch at all. There is no seam for a lost hunk.

The three files that *are* co-modified I read line by line:

- **`shared/tools_catalog.py`** (#145's band renames): head's only delta vs main
  is replacing the local `importlib.import_module(f"tools.{slug}.meta")` +
  `except ImportError: pass` with `meta_for(adapter.slug)`. All of #145's band
  and category code is byte-identical.
- **`blueprints/public.py`** (conflicted): head's only delta vs main is the same
  `meta_for` substitution in `help_tool_guide`, plus the import. #145's changes
  intact.
- **`app.py`**: two deltas — the same `meta_for` substitution in `_tool_about`,
  and `_signin_url` moving from `request.path` to `request.full_path.rstrip("?")`.
- **`tools/{af2,mpnn,rfdiffusion}/meta.py`**: head appends `PILOT`/`EXAMPLE`
  blocks only; #153's edits to those files are intact above them.

**Functional confirmation**, not just textual: main's own guard suites
(`test_login_redirect` 168/168, `test_signup_credit_single_source`,
`test_tool_categories` 8/8, `test_money_display_surfaces`) all pass on head in
the 5232-green run. #152 and #153 are enforced by their own tests and those
tests are all present and green.

**Verdict on claim 2: verified, and verified harder than claimed.**

#### Minor, non-blocking (2c)

`app.py::_signin_url`'s new comment says "the GET branch does NOT validate — it
renders whatever arrives straight into a hidden field". After the merge that is
false: main's #152 added GET-branch sanitisation, pinned by
`test_login_get_sanitises_hidden_next`. Stale prose, correct code.

---

## 3. `_as_form_text` — the clone-repr fix

### 3a. I re-did the adapter sweep from scratch

I did not read the builder's list. I called every one of the 14 adapters'
own `validate()` on the superset form, dumped every stored value that is not a
scalar, then rendered all 14 clone pages and asked, per key, *is this key a
name some `<input>/<select>/<textarea>` on that page actually carries?*

Every non-scalar stored value in the repo, and whether it names a field:

| adapter | key | type | names a field? | renders as |
|---|---|---|---|---|
| af2 | `fasta_records` | list[dict] | no (read into the `fasta` textarea by the template) | FASTA |
| bindcraft | `hotspot_residues` | list[int] | **yes** | `54,56,115` |
| boltz2 | `hotspot_residues` | list[int] | **yes** | `54,56,115` |
| boltz2 | `binder_sequences` | list[dict] | **yes** | `>d0\nQVRL…` |
| boltz2 | `parameters` | dict | no | — |
| boltzgen | `hotspot_residues` | list[int] | **yes** | `54,56,115` |
| esmfold2-design | `parameters` | dict | no | — |
| iggm | `antibody_fasta` | list[dict] | no | — |
| iggm | `epitope_pdb_resnums` | list[int] | no (field is `epitope`) | — |
| iggm | `parameters` | dict | no | — |
| mpnn | `_fixed_positions` | dict | no (underscore-stripped) | — |
| opendde | `spec` | list[dict] | no | — |
| opendde | `parameters` | dict | no | — |
| proteina | `_target_segments` | list | no (underscore-stripped) | — |
| proteina | `hotspot_residues` | list[int] | **yes** | `54,56` |
| proteina | `hotspot_spec` | list[str] | no | — |
| proteina | `binder_length` | list[int] | no (min/max fields) | — |
| proteina | `parameters` | dict | no | — |
| pxdesign | `hotspot_residues` | list[int] | **yes** | `54,56,115` |
| pxdesign | `binder_length` | int (scalar) | **yes** | `95`, uncorrupted |
| rfantibody | `hotspot_residues` | list[int] | **yes** | `54,56,115` |
| rfdiffusion | `hotspot_residues` | list[int] | **yes** | `54,56,115` |
| rfdiffusion | `binder_length` | dict | no (min/max fields) | — |

I then scanned **every** rendered field on all 14 clone pages (not just the
ones I expected) for a value starting `[`, `(` or `{`. **Zero hits.** The
builder's list is complete: `hotspot_residues` on six tools, `binder_sequences`
on boltz2 and `fasta_records` on af2 are the only non-scalars that reach a
field, and all three shapes are handled.

**Structured values that name a field: 8.** The vacuity guard in
`test_no_field_renders_a_container_repr` asserts `structured >= 5`, so it is
non-vacuous with margin.

### 3b. Does `_as_form_text` cover all five prefill sources? — NO, and that is fine

`_normalize_clone_pre_fill` (and therefore `_as_form_text`) is called on the
**`clone_from` path only**. I read the other four:

| source | how it builds `pre_fill` | can it emit a non-string? |
|---|---|---|
| `clone_from` | raw `job.inputs` -> `_normalize_clone_pre_fill` | covered |
| `from_job` | copies only `target_chain`, `hotspot_residues`, with its own inline `",".join(...)` for lists | no |
| `handoff` | builds `",".join(str(r) for r in ho.hotspot_residues)` itself | no |
| `resample_from` | `_resample.RESAMPLE_MPNN_DEFAULTS` — measured, all three values are `str` | no |
| `pilot` (new) | `PILOT["params"]` from meta.py — measured, **all 10 pilots' params are 100% strings** | no today |

So the builder's phrasing ("one function every clone routes through") is accurate
and the other four paths are independently safe. **Latent, not live:** nothing
*guards* the pilot path. A future `PILOT["params"]` value that is a list would
be handed to Jinja and repr'd, and no test in the suite would notice.
`TestPilotDeclaration::test_shape` checks the key set, not the value types.
One-line fix if wanted: assert every param value is a `str` there.

### 3c. The comma-join fallback — safe today, one specific latent case

The builder disclosed that unseen list shapes fall through to `",".join(str(i))`.
I found exactly one stored value in the repo that would hit that badly:
**`opendde.spec`** is a `list[dict]` with no `sequence` key, so it would join
to a Python **dict repr** (`{'name': 'opendde_job', 'modelSeeds': [7, 8], ...}`).
It is inert only because `spec` is not a form field name (opendde's fields are
`spec_mode` / `spec_json`). If anyone renames that field or adds a `spec`
textarea, the fallback ships the exact bug this function exists to stop. Not a
blocker — but note the fallback's safety here is coincidental, not structural.

### 3d. The deleted `af2_form.html` loop — VERIFIED redundant

Old Jinja loop: `'>' ~ (_rec.get('header') or ('chain' ~ loop.index))` + sequence.
New `_as_form_text`: `record.get("name") or record.get("header") or f"seq_{i}"`.
af2's `validate()` emits records keyed `header`/`sequence` (measured), so both
produce byte-identical FASTA; only the never-taken no-header fallback differs
(`chain1` vs `seq_0`).

Round trip re-derived independently: af2 clone renders `>H\nQVQL...\n>L\nDIQM...`
into the `fasta` textarea and af2's own `validate()` parses it back to the
identical `fasta_records` list. And I broke it on purpose — see **M11** below,
which turns `test_af2_fasta_records_round_trip_through_validate` red.

**Verdict on claim 3: verified.** Root-cause fix, complete sweep, no repr
reaches any field on any of the 14 tools. Two latent notes (3b, 3c), neither
blocking.

---

## 4. The D6 guard — mutation testing

13 mutations. Every one applied with an explicit utf-8 read/write (no `sed`, no
encoding round trip) and **verified on disk with `git diff --unified=0` before
the test run** — the diff hunk is printed for each in the harness output.
Failures attributed by full test **node id**, not by count.

| # | mutation | landed | caught by |
|---|---|---|---|
| M1 | bindcraft `PILOT.num_designs` `2`->`4` (restate the form default) | yes | `TestNoPilotIsANoOp::test_a_no_op_pilot_is_only_allowed_when_nothing_cheaper_exists`, `::test_the_retuned_pilots_really_are_cheaper` |
| M2 | rfantibody `PILOT.num_designs` `2`->`4` | yes | same two |
| M3 | pxdesign `PILOT.num_designs` `4`->`8` (restate default; cheaper IS reachable) | yes | `::test_a_no_op_pilot_is_only_allowed_when_nothing_cheaper_exists` |
| M4 | M3 **plus** the old decoy (`binder_length` bumped — a real field the estimator ignores) | yes | `::test_a_no_op_pilot_is_only_allowed_when_nothing_cheaper_exists` — **the decoy no longer buys the exemption** |
| M5 | mpnn `PILOT.num_seq_per_target` `8`->`50` (restate the form default) | yes | `::test_a_no_op_pilot_is_only_allowed_when_nothing_cheaper_exists` |
| M6 | rfantibody `PILOT.num_designs` `2`->`8` (pilot dearer than the defaults) | yes | `::test_a_pilot_is_never_more_expensive_than_the_defaults`, `::test_the_retuned_pilots_really_are_cheaper` |
| M7 | **vacuity probe** — null out all six pilots that bill what their defaults bill | yes | `::test_a_no_op_pilot_is_only_allowed_when_nothing_cheaper_exists` with its own message: `AssertionError: no pilot bills what its form defaults bill, so the floor check below never runs` / `assert []` |
| M8 | **ADVERSARIAL** — pxdesign `PILOT.params` drops `num_designs` entirely | yes | **NOTHING. Full suite 5232 passed / 20 skipped.** |
| M9 | `_as_form_text` short-circuited to `return str(items)` | yes | `TestNoPythonReprReachesAField::test_no_field_renders_a_container_repr`, `::test_boltz2_binder_sequences_round_trip_through_validate`, `::test_af2_fasta_records_round_trip_through_validate` |
| M10 | `_normalize_clone_pre_fill` stops converting lists | yes | same three |
| M11 | `af2_form.html` stops reading `fasta_records` | yes | `::test_af2_fasta_records_round_trip_through_validate` |
| M12 | **ADVERSARIAL** — boltzgen `PILOT.budget` `4`->`50` (the field maximum) | yes | **NOTHING. Full suite 5232 passed / 20 skipped.** |
| M13 | delete `{% if not example %}` from `templates/tools/mpnn_results.html` | yes | `TestNoDeadLinkInsideAnExample::test_rendered_tool_page_has_no_job_scoped_url`, `::test_partial_rendered_in_isolation_is_also_clean` |

11 of 13 caught. The decoy that defeated the previous shape (M4) is dead —
**the rewrite is genuinely enforcing** — and the vacuity guard fires with its
own message rather than passing silently. Claim 4 confirmed.

### The two the new rule still misses

I re-derived the estimator's shape to work out where a hole could exist.
`estimated_cost_for_tool` is, at fixed slug, a **monotone non-decreasing
function of `spec.scaling_param` alone**: `_scale_seconds` uses
`max(actual / baseline, 1.0)`, `compute_hard_cap` uses the same ratio, and
`tier_gpu_seconds` is empty for every shipped tool so `preset` moves nothing.
The rule's floor probe (`scaling_param -> "1"`) therefore does find the global
minimum. **There is no no-op pilot reachable by dialling a param the estimator
reads that the rule misses.** The holes are elsewhere:

**M8 — the card can price a run the form will not submit.** With `num_designs`
dropped from pxdesign's `PILOT.params`:

```
PILOT params      : {'preset': 'pilot'}
card advertises   : $4.37
form will POST    : num_designs=8
that run costs    : $17.4787
```

A **4x understatement** on a publicly indexable page, and the entire suite stays
green. The cause: `test_card_price_equals_the_estimator` compares the card
against `estimated_cost_for_tool(None, slug, pilot["params"])` — the *same input
the card used*. It is a tautology with respect to what the rendered form will
actually POST. `_no_op_violation` misses it too, because `_form_defaults` only
reads back the keys the pilot happens to name.

**I checked the shipped state and it is clean** — for all 10 pilots the card
price equals the cost of the values the `?pilot=1` form actually renders:

```
bindcraft $4.37 (num_designs=2)      boltz2 $0.22             boltzgen $8.74
esmfold2-design $9.87 (n_seeds=1)    iggm $0.08 (num_samples=1)
mpnn $0.03 (num_seq_per_target=8)    proteina $12.59 (num_designs=8)
pxdesign $8.74 (num_designs=4)       rfantibody $4.37         rfdiffusion $1.46
mismatches: none
```

So this is a **latent guard gap, not a live defect**. The strengthening is to
read the scaling param back off the rendered `?pilot=1` page and compare *that*
estimate to the card, rather than re-estimating the pilot dict.

**M12 — the rule is blind to boltzgen's only knob.** boltzgen's estimator
`scaling_param` is `num_designs`, which is **not a field on boltzgen's form at
all**; the form's knob is `budget`. Setting `PILOT.budget` to `50` (the maximum,
the least "starter" value available) leaves every test green. Benign given what
`budget` actually does (see §5) — but the D6 rule provides no coverage
whatsoever for boltzgen.

---

## 5. The flat-price rewording

### 5a. The price claim — VERIFIED empirically, and at the source, not just the estimator

Computed directly via `estimated_cost_for_tool`:

| tool | knob | 1 | 4 | 10 | 50 | scales? |
|---|---|---|---|---|---|---|
| **boltzgen** | `budget` | $8.7380 | $8.7380 | $8.7380 | $8.7380 (also $8.7380 at 200) | **flat** |
| **esmfold2-design** | `n_seeds` | $9.8614 | $39.4454 | $98.6136 | — | **scales linearly** |
| **iggm** | `num_samples` | $0.0728 | $0.2913 | $0.7283 | — | **scales linearly** |

The builder's corrected premise is exactly right: **only boltzgen is truly
flat-price.** esmfold2-design and iggm do scale; their pilots sit at the floor.
Confirmed from the rendered markup that their pilot params are byte-identical to
both the form default and the field minimum:

- `<input type="number" name="n_seeds" min="1" max="64" value="1">`, PILOT `n_seeds: "1"`
- `<input type="number" name="num_samples" min="1" max="100" value="1">`, PILOT `num_samples: "1"`
- boltzgen `<input type="number" name="budget" value="4" min="1" max="50">`, PILOT `budget: "4"`

I did not stop at the estimator, because a flat *estimate* is not a flat *bill* —
the wallet settles on actual Modal cost. I read the source.
`tools/boltzgen/__init__.py::build_payload` hard-codes `"num_designs": 200` in
the job spec regardless of `budget`, and `budget` is the top-N **selection** out
of that fixed 200-candidate pool (the code comment says so verbatim: *"budget is
the top N selected from the num_designs=200 pool"*). **The GPU work genuinely
does not depend on `budget`.** The new copy's claim is true at the level that
matters, not merely true of the displayed number.

### 5b. Reading the new copy as a bench biologist — one real problem

The three reworded cards read honestly. "A guided first run at the tool's normal
cost — not a cheaper trial" is exactly the right sentence, and it is
load-bearing: without it, a card headed "Starter pilot" next to an $8.74 price
implies a discount that does not exist.

I also checked the **three same-priced pilots that were NOT reworded** — boltz2,
proteina and rfdiffusion also bill exactly what their form defaults bill. All
three already carry honest framing in their own prose (proteina: *"8 designs is
one shard on one GPU, and it costs the same as one design would"*; boltz2: *"the
cost scales with how many you submit at once"*; rfdiffusion: *"before paying for
a large run"*). No further rewording needed there.

#### FINDING — the boltzgen page contradicts itself, in two adjacent sections

On the **same anonymous page**, `/tools/boltzgen`:

- the about-panel field explanation (`tools/boltzgen/meta.py:126`):
  > **Budget (designs).** Number of designs Boltz-2 generates and ranks.
  > **Higher budgets cost more and run longer.**
- ~100 rendered lines later, the new pilot card:
  > `budget` only chooses how many of the candidates are returned to you —
  > **it does not change the bill** — so raising it on a later run costs you
  > nothing extra.

Both render to a logged-out visitor. The pilot card is the correct one
(confirmed against `build_payload` above); the about-panel line is stale, and it
also mis-names the field ("Budget (designs)") where the form's own label is
already correct ("Budget (final candidates)").

This is the one thing on the changed surface that a bench biologist would
actually trip over, and it is a two-line fix in `tools/boltzgen/meta.py`. It is
a pre-existing string, so I am not treating it as a merge blocker — but this PR
is the change that makes it a contradiction, and "reword it honestly" is the
explicit brief.

---

## 6. Anonymous walkthrough — could a bench biologist get to a submitted pilot?

### First, the non-vacuity assertion this check needs

```
ADAPTERS REGISTERED: 14
['af2','bindcraft','boltz2','boltzgen','colabfold','esmfold','esmfold2-design',
 'iggm','mpnn','opendde','proteina','pxdesign','rfantibody','rfdiffusion']
14/14 anonymous GET /tools/<slug>         -> 200
14/14 anonymous GET /tools/<slug>?pilot=1 -> 200
/ -> 200, /tools -> 200, /help -> 200
```

All 14 flags on, registry asserted non-empty at 14. The scans below are not
vacuous.

Anonymous POST gate re-checked while I was there (the known landmine in this
repo): the real submit route is `POST /tools/<tool>/submit`, and **14/14 return
403** anonymously. Intact.

### The answer: yes, with one detour

The homepage now opens with a *"What do you have, and what do you want?"* table
keyed on bench objects rather than model names — *"A target protein structure ->
Something that binds it -> Epitope Scout, then a binder pilot"*, *"A backbone
from somewhere else -> Sequences for it -> ProteinMPNN"*. That is the right first
screen for someone who has never heard of RFdiffusion, and it is the single
biggest thing this phase gets right.

From there: tool page -> pilot card (*"Starter pilot: 8 binders ... About $1.46
... Load these settings ->"*) -> `?pilot=1` with `num_designs` prefilled -> sign
in -> back to the prefilled form. I verified the auth hop end to end for **all
ten** pilot tools: every anonymous `?pilot=1` page emits a sign-in link whose
`next` parses back to `/tools/<slug>?pilot=1`. The query string survives, which
is the thing that used to evaporate.

### Where they still get stuck: the three inputs no pilot prefills

A pilot carries counts and presets only. On the binder-design tools the user must
still supply, on their own:

1. a **target structure file** (upload),
2. the **chain ID**,
3. **hotspot residues** — *"Residues the binder should contact, in original PDB numbering"*.

**(3) is the first hard stop.** A bench biologist with a target and no
structural-biology background does not know which residues to type, and the pilot
card's *"What you need"* states the requirement without helping meet it. The page
does deflect — *"Not sure which residues? Score your target's surface with
Epitope Scout first"*, with a live link — and Scout feeds back through the
`handoff=` prefill source, so the loop closes. But that is a detour into a second
tool before the "guided first run" can be started at all, and the pilot card
itself does not mention it; only the hotspot field's help text further down does.

**If one thing were added next:** make the pilot card the thing that routes an
unprepared user to Scout, instead of leaving it to a form field's help text.
Everything else on the path works.

Second-order, non-blocking: the wallet. Signup credit is $15 and the pilots cost
$0.03–$12.59, so every pilot is affordable on the free credit. Nothing to fix.

---

## 7. Dead links inside worked examples — VERIFIED, and I broke it to check

Only **mpnn** ships an `EXAMPLE`; the other 13 declare `EXAMPLE = None`
explicitly, which is the right shape. Checked at both levels.

**Page level**, anonymous, all 14 pages scanned for any `href`/`action`/`src`
containing `/jobs/example`, `job_id=example`, or an `example` download path:

```
mpnn: jobs-scoped=[]   any-'example'-url=['/static/example/1HEW.pdb']
(no other tool emits any example URL at all)
```

`/static/example/1HEW.pdb` exists on disk (132,266 bytes) and `GET` returns 200.
The literal string `/jobs/example` appears nowhere in the rendered page.

**Partial rendered in isolation** — I rendered `mpnn_results.html` through the
same stub `job` mapping myself, with and without the flag:

```
example=True : total urls=0   DEAD=[]
example=False: total urls=2   DEAD=['/tools/mpnn?clone_from=example',
                                    '/jobs/example/export.fasta']
```

The guard is load-bearing and exactly two links depend on it. **M13** deletes the
`{% if not example %}` from the partial and turns
`test_rendered_tool_page_has_no_job_scoped_url` **and**
`test_partial_rendered_in_isolation_is_also_clean` red — the detector is not
blind.

---

## 8. Findings ledger

### Blockers

**None.**

### Should fix (not blocking)

1. **`tools/boltzgen/meta.py:126`** — *"Higher budgets cost more and run longer"*
   directly contradicts the new pilot card on the same page, and is wrong per
   `build_payload`. Also rename to "Budget (final candidates)" to match the form
   label. §5b.
2. **`TestPilotCardPriceIsDerived::test_card_price_equals_the_estimator`** is a
   tautology — it re-estimates the pilot dict instead of reading the rendered
   form back. M8 proves a 4x price understatement passes the whole suite. Read
   `spec.scaling_param` off the `?pilot=1` HTML and compare that.
3. **`tests/test_tool_categories.py::test_every_adapter_resolves_its_meta_in_the_catalog`**
   (new on this branch) mutates `os.environ[flag_name(slug)] = "on"` with no
   monkeypatch and no restore, unlike every sibling fixture in the same PR which
   saves and restores. It leaks every tool flag ON for the rest of the session —
   order-dependent pollution that could mask a flag-gating failure elsewhere.

### Noted, no action needed

4. `_as_form_text` covers the clone path only. The other four prefill sources
   build strings themselves and are safe today; nothing guards the new `pilot`
   path against a future list-valued `PILOT["params"]` value. §3b.
5. The comma-join fallback would repr `opendde.spec` (list[dict], no `sequence`
   key). Inert only because `spec` is not a field name. §3c.
6. The D6 rule has no coverage at all for boltzgen — its estimator scaling param
   (`num_designs`) is not a field on its form. M12.
7. `app.py::_signin_url`'s new comment claims "the GET branch does NOT validate";
   main's #152 added GET-branch sanitisation, so the comment is stale after the
   merge. Code is correct. §2c.

---

## VERDICT: **MERGE**

`2b09c85`. All five builder claims re-derived independently, and all five hold.

- Both baselines reproduce exactly (4927/19 and 5232/20), plus main at 5190/20.
- The merge is lossless — proven mechanically (`merge-tree` tree comparison, only
  the three disclosed paths differ) **and** by per-file additivity across the
  *entire* suite, not just the two files claimed. No test file lost a single
  item. #145 / #152 / #153 are byte-identical or verifiably intact and green.
- The clone-repr fix is at the root: I re-swept all 14 adapters through their own
  `validate()` and scanned every rendered field on every clone page. No repr
  survives anywhere. Three independent mutations turn it red.
- The D6 rewrite genuinely enforces — the decoy that defeated the previous shape
  is dead, and the vacuity guard fires with its own message.
- The flat-price claim is true, and true at the source (`build_payload` pins
  `num_designs=200` independent of `budget`), not merely true of the estimator.
- 14 adapters, 14 pages at 200 anonymously, 14/14 anonymous submits refused, no
  `/jobs/example` href and no dead artifact link anywhere.

The three should-fix items are all small, and none can produce a wrong charge, a
broken link or a lost test in the shipped state. Item 1 (the boltzgen
self-contradiction) is the only one a user would see, and it is a two-line copy
change that can land as a follow-up.

### What I verified empirically vs. what I only reasoned about

**Ran:** all suite counts and per-file collection deltas; the `merge-tree`
re-derivation; every file-level diff cited; the 14-adapter `validate()` sweep and
the full rendered-field repr scan; all 13 mutations, each with its on-disk diff
printed; the price tables; the source reading of `build_payload`; every anonymous
page render, sign-in-hop check, POST-gate probe and dead-link scan; the
partial-in-isolation render with and without the `example` flag.

**Reasoned, not run:** that the estimator is monotone in `scaling_param` alone
(derived from `_scale_seconds` / `compute_hard_cap` / the empty
`tier_gpu_seconds`, spot-checked but not exhaustively fuzzed) — this is the basis
for my claim that no *estimator-visible* no-op pilot escapes the rule; and that
the Scout -> `handoff=` loop closes for a user stuck on hotspot residues (the
prefill source exists and is wired, but I did not drive a real Scout run through
it).
