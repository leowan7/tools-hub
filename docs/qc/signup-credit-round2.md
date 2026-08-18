# QC round 2 — signup credit single source (PR #153)

**Reviewed SHA:** `c9e45829ffdd1b5a66987d7d975056c681255e70` (merge commit, branch `fix/signup-credit-one-source`)
**Trunk:** `origin/main` = `5e4fa66af07cd15830ee542331eaf2722c61c69c`
Both SHAs confirmed unmoved after `git fetch origin`.

**Verdict: MERGE** (full reasoning and caveats at the bottom).

All work done in a detached worktree under the session scratchpad. The main
working tree was not touched.

---

## Claim 6 — merge resolution is lossless — VERIFIED

`git merge-tree --write-tree --name-only dfa11fc 5e4fa66` reports exactly two
conflicts and no others:

```
CONFLICT (modify/delete): templates/tools/_preview.html deleted in 5e4fa66 and modified in dfa11fc.
CONFLICT (content): Merge conflict in tests/test_signup_credit_single_source.py
Auto-merging blueprints/auth.py
```

`git diff 880d1a3 c9e4582` (git's own auto-merge tree vs. the actual merge
commit) touches **only those two files**. Nothing else was hand-edited during
the merge — every other file in `c9e4582` is byte-identical to what git
produced unattended.

- `_preview.html`: absent from `c9e4582` (`git ls-tree` empty) — main's
  deletion won, as claimed.
- `tests/test_signup_credit_single_source.py`: main's *entire* change to this
  file (`git diff 37a0f3a 5e4fa66`) was the removal of one entry,
  `"_preview.html"`, from `ALLOWED_LITERAL_DOLLAR`. HEAD deletes the whole
  `ALLOWED_LITERAL_DOLLAR` set. Taking HEAD therefore strictly subsumes main's
  change — removing one member of a set that no longer exists is a no-op.
  **Lossless, confirmed by reading both sides, not by assertion.**

### `blueprints/auth.py` — the clean automerge

This is the one the reviewer flagged as the invisible-loss risk (#152's
`safe_next` open-redirect fix landed on main; this branch also edits the file).

Two independent checks:

1. `git diff 5e4fa66 c9e4582 -- blueprints/auth.py` yields **exactly the three
   hunks this PR intends** (import of `SIGNUP_CREDIT_USD`, the reworded
   lazy-grant comment, the flash message). i.e. merged file == main's file +
   this PR, with nothing of main's removed.
2. The content lines of `git diff 37a0f3a dfa11fc -- blueprints/auth.py` and
   `git diff 5e4fa66 c9e4582 -- blueprints/auth.py` are **byte-identical**
   after stripping diff headers — the branch's intent survived the merge intact
   as well.

`safe_next` occurrences, main vs. merged branch:

| | main `5e4fa66` | branch `c9e4582` |
|---|---|---|
| `def safe_next(...)` | L30 | L31 |
| `safe_next(request.args.get("next"))` | L120 | L121 |
| `safe_next(request.form.get("next"))` | L133 | L134 |
| comment referencing it | L131 | L132 |

Identical set, offset by one line (the added import). **Note a correction to
the brief: there are two `safe_next()` call sites, not three** — the brief's
third is the `def` or the comment mention. Both features present; both
exercised green by the suite (see baselines).

The new flash string is present at `blueprints/auth.py:349-352`.

---

## Baselines — MEASURED, both green

Command, run from the root of a detached worktree, **no path argument**:

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| commit | result | wall |
|---|---|---|
| `5e4fa66` (trunk) | **5185 passed, 20 skipped**, exit 0 | 196.35s |
| `c9e4582` (branch) | **5190 passed, 20 skipped**, exit 0 | 207.38s |

Zero `FAILED`/`ERROR` lines in either full log (`grep -cE '^(FAILED|ERROR)'` = 0
on both). The reviewer's measurement of 5190/20 on the branch is **confirmed**.

### Delta reconciliation: +5, fully accounted

`pytest -q --collect-only` node IDs, sorted, diffed (5205 -> 5210):

**Added (6):**

| test | note |
|---|---|
| `test_signup_credit_single_source.py::test_the_guard_walks_every_module_that_could_drift` | new |
| `test_signup_credit_single_source.py::test_no_python_source_names_a_signup_credit_env_var` | new |
| `test_signup_credit_single_source.py::test_no_python_source_defines_a_parallel_signup_credit_constant` | new |
| `test_signup_credit_single_source.py::test_no_python_source_hardcodes_the_signup_amount` | new |
| `test_signup_credit_single_source.py::test_signup_credit_email_states_the_real_grant` | new |
| `test_scout_anonymous_access.py::TestStillGated::test_feasibility_get_requires_login[/scout/feasibility/download/eade155e-...]` | **not new** — a `uuid4()` in the parametrize id |

**Removed (1):** the same scout test under its previous run's random UUID
(`d44170c0-...`). Same test, non-deterministic node ID.

Net real change: **exactly the 5 new Python-source guards.** No unexplained gap,
nothing silently dropped.

---

## Claim 1 — zero surviving `$5` on any signup-credit surface — VERIFIED

Re-derived with my own script (`qc_scan.py`, scratchpad, not committed), not the
builder's. It sets `FLAG_TOOL_<SLUG>=on` for all 14 slugs and blanks the six
Supabase env names before importing `app`, then drives `create_app()` +
`test_client()` **anonymously**.

**Non-vacuity proven before any zero is believed** — both assertions are hard
`assert`s in the script, and both passed on both commits:

```
[proof] adapters registered: 14 -> ['af2','bindcraft','boltz2','boltzgen',
        'colabfold','esmfold','esmfold2-design','iggm','mpnn','opendde',
        'proteina','pxdesign','rfantibody','rfdiffusion']
[proof] GET /tools/<each of 14> -> 200      (14/14)
[proof] other GET routes at 200: 22
[proof] total surfaces scanned: 36
```

The scan is deliberately **broader than the guard's own regex** — it reports
every `$5` token anywhere with 90 chars of context, so the judgement is mine.

| commit | raw `$5` hits | signup-context `$5` |
|---|---|---|
| `5e4fa66` (trunk) | **6** | 6 |
| `c9e4582` (branch) | **0** | 0 |

The scan is therefore **sensitive, not merely quiet.** The 6 trunk hits are the
exact JSON-LD class round 1 flagged — three tool pages, each twice (once in the
visible `<dd>` FAQ, once inside the `FAQPage` structured data):

```
/tools/af2         "New accounts start with $5 of credit, ..."          x2
/tools/mpnn        "New accounts start with a $5 wallet balance, ..."   x2
/tools/rfdiffusion "New accounts start with a $5 balance, ..."          x2
```

23 of the 36 surfaces contain `$15` on the branch.

### The signup flash — driven for real, not inspected

`GET /tools/*` does not reach it, so I drove the **actual POST success path**
(`qc_flash2.py`): real `_csrf` and `signup_token` scraped from `GET /signup`,
`shared.auth.register_user` mocked to succeed, events silenced.

| commit | rendered flash |
|---|---|
| `5e4fa66` | `'Account created with $5 of compute credit. Sign in to get started.'` |
| `c9e4582` | `'Account created with $15 of compute credit. Sign in to get started.'` |

Both returned HTTP 200 with ~11.2 KB bodies. A first attempt returned 403 (CSRF)
and produced no flash — reported here so it is clear the 200 above is a real
success-path render, not a silent no-op.

---

## Claim 5 — the "circular import" comment was false — VERIFIED

`shared/email.py` now imports `SIGNUP_CREDIT_USD` at module level. Eight
permutations, each in a **fresh interpreter**, from the branch worktree root:

| statement | result |
|---|---|
| `import shared.email` | OK |
| `import shared.wallet` | OK |
| `import app` | OK |
| `import shared.email, shared.wallet` | OK |
| `import shared.wallet, shared.email` (reverse order) | OK |
| `import shared.email; import blueprints.auth` | OK |
| `import blueprints.auth` | OK |
| `import tools.af2.meta` | OK |

No circular import exists. The round-1 comment was indeed false, and removing it
is correct.

**New coupling worth naming:** `tools/*/meta.py` now imports `shared.wallet`.
I checked whether that reaches the GPU container — `tools/af2/modal_app.py` does
not import `meta`, and its only local-file copy is `_RUN_PIPELINE_LOCAL`. So
`meta.py` stays web-side and this does **not** put `shared/` on the Modal image
path. Not a blocker, but it is a new web->tools->shared edge that did not exist.

---

## Claim 3 — the guard is not vacuous — VERIFIED

I loaded the guard module directly and enumerated `_parsed_sources()` myself
rather than trusting its own assertion:

```
walked files: 191
by top dir: tools 85, shared 45, scout 17, scripts 12, blueprints 10,
            cron 8, webhooks 4, billing 3, contracts 3, <root> 2, gpu 2
```

All twelve required modules present:

`app.py`, `gunicorn.conf.py`, `shared/wallet.py`, `shared/email.py`,
`blueprints/auth.py`, `billing/checkout.py`, `webhooks/stripe.py`,
`cron/daily_digest.py`, `scout/routes.py`, `tools/af2/meta.py`,
`tools/mpnn/meta.py`, `tools/rfdiffusion/meta.py` — **12/12 OK**.

325 tracked `.py` files total, 191 walked, 134 skipped. Every skipped file is a
test: 132 under `tests/`, plus `tools/library_planner/tests/{__init__,test_canonical}.py`
(`_SKIP_DIRS` matches any path *part* named `tests`, not just the top level).
**Zero `tests/` paths leak into the walk.** Not vacuous.

Minor note: because `_SKIP_DIRS` matches any part, a future non-test module
placed under any directory literally named `tests/` would be silently exempt.
Theoretical, not present today.

---

## Claim 4 — no false negatives — VERIFIED

The per-file allowlist really is gone (`git grep ALLOWED_LITERAL_DOLLAR` — no
hits on the branch). So the genuinely-different `$5`s must pass on **content**,
which is the stronger property. I ran `SIGNUP_MONEY.search()` against each one
directly:

| genuinely-different `$5` | matched? |
|---|---|
| `_header.html` `title="Wallet below $5"` (LOW_BALANCE_EMAIL_THRESHOLD) | no |
| `send_low_balance.html` `balance_usd < $5` | no |
| `topup.html` top-up denominations (`$50`, `$500`) | no |
| `launch.html` hold-rounding worked example (`$573.6736` -> `$573.67`) | no |
| `billing/checkout.py` `$5,000` auto-reload cap | no |
| `shared/compute_campaigns.py` `$2.02 and $5.03` campaign example | no |
| `blueprints/auth.py:348` comment (`user "$5" for the whole time...`) | no |
| `shared/email.py:1364` docstring (`body says "$5" instead of "$5.00"`) | no |
| `shared/wallet.py:181` `Round the deficit up to the nearest $5` | no |
| `shared/email.py:2156` `30 day spend >= $5000` sales alert | no |

**10/10 pass on content, none by exclusion.**

`blueprints/auth.py:348` specifically is safe **twice over**, and I checked both
independently:
1. Its text does not match `SIGNUP_MONEY` (tested above).
2. It is a `#` comment, so it never becomes an `ast.Constant`. Confirmed:
   `any("for the whole time" in c for c in <all str Constants in auth.py>)` is
   `False`. The Python guard cannot see comments at all, by construction.

### Existing tests were STRENGTHENED, not weakened

Not in the brief, but the obvious way to fake a green suite. Both edited test
files got tighter:

- `tests/test_email_real.py`: dropped `monkeypatch.setenv("WALLET_SIGNUP_CREDIT_USD", "5")`
  and replaced `assert "$5" in body["subject"]` with the constant. The old
  hardcoded `"$5"` is precisely why this test stayed green while the email
  advertised the wrong figure.
- `tests/test_money_display_surfaces.py`: tightened
  `assert [...] == ["credit_usd = _money("]` to `assert [...] == []`. Strictly
  stronger — nothing is allowed to sit on the implicit `nearest` default now.

No assertion was relaxed anywhere in the diff.

---

## Claim 2 — the guard actually enforces it — 18 mutations

Harness: apply by exact UTF-8 text replacement (a Python helper, **not sed** —
it exits non-zero if the pattern is not found exactly once, which is how the
two historical silent-miss failure modes are ruled out), then **prove the
mutation landed with `git diff --unified=0` before running anything**, then run
the guard file and attribute by test NAME, then `git checkout -- .`.

Two mutations genuinely did not land on the first attempt and are reported as
such rather than scored: M9 and M12 anchored on `<body`, which does not exist
in `templates/index.html` (it `{% extends "base.html" %}`). The harness printed
`MUTATION-DID-NOT-LAND: pattern occurs 0 times` and **discarded the result**.
Both were re-run against a valid anchor and are scored on that run.

Guard-file baseline on an unmutated worktree: `11 passed`.

| # | mutation | landed | failing test(s) | verdict |
|---|---|---|---|---|
| M1 | `tools/af2/meta.py` FAQ back to hardcoded `$5` prose | yes (5 lines) | `test_no_python_source_hardcodes_the_signup_amount` | CAUGHT |
| M2 | signup flash in `blueprints/auth.py` back to `$5` | yes (4) | `test_no_python_source_hardcodes_the_signup_amount` | CAUGHT |
| M3 | reintroduce `os.environ.get("WALLET_SIGNUP_CREDIT_USD", "5")` | yes (2) | `..._names_a_signup_credit_env_var` + `..._email_states_the_real_grant` | CAUGHT |
| M4 | `os.environ["WALLET_SIGNUP_CREDIT_USD"]` **subscript** form | yes (2) | same two | CAUGHT |
| M5 | key by `+` concatenation: `"WALLET_SIGNUP" + "_CREDIT_USD"` | yes (2) | same two | CAUGHT |
| M6 | key bound to a **variable** first, then `os.getenv(_k)` | yes (3) | same two | CAUGHT |
| M7 | `os.getenv(key=..., default=...)` **keyword** form | yes (2) | same two | CAUGHT |
| M8 | parallel constant `SIGNUP_CREDIT_DEFAULT_USD = Decimal("5.00")` in `shared/email.py` | yes (1) | `..._defines_a_parallel_signup_credit_constant` | CAUGHT |
| M9 | `<p>New accounts start with $5 in your wallet.</p>` in `templates/index.html` | 1st: **NO** (bad anchor, discarded); 2nd: yes (1) | `test_no_template_hardcodes_the_signup_amount` | CAUGHT |
| M13 | change the grant `Decimal("15.00")` to `Decimal("25.00")` | yes (2) | none — **full suite 5190 passed / 20 skipped, exit 0** | CORRECT (must stay green) |
| M16 | **lower-case** env name `wallet_signup_credit_usd` | yes (2) | `..._email_states_the_real_grant` | CAUGHT (by backstop) |
| M17 | env name dodging the word pair: `WALLET_WELCOME_BONUS_USD` | yes (2) | `..._email_states_the_real_grant` | CAUGHT (by backstop) |
| M18 | env name assembled by slicing so no literal holds both words | yes (2) | `..._email_states_the_real_grant` | CAUGHT (by backstop) |
| **M10** | prose split at the sigil: `"...start with $" + "5 of credit..."` | yes (5) | **none** | **EVADES** |
| **M11** | unlisted phrasing: `"We fund your first $5 so you can try it"` | yes (5) | **none — full suite 5190/20 green** | **EVADES** |
| **M12** | template uses the HTML entity `&#36;5` | yes (1) | **none** | **EVADES** |
| **M14** | `$5` signup prose in `templates/email/reengagement.txt` | yes (1) | **none** | **EVADES** |
| **M15** | `$5` split across two lines in a template | yes (2) | **none** | **EVADES** |

**13 caught, 5 evade, 1 correctly-green.** Every mutation that landed and was
supposed to be caught, was caught, and by the right test.

### The disclosed lower-case ceiling is narrower than advertised — a positive finding

M16 (lower-case), M17 (a name avoiding `SIGNUP`+`CREDIT` entirely) and M18 (a
name no single literal contains) all slip past
`test_no_python_source_names_a_signup_credit_env_var` **as expected** — but all
three are still caught by `test_signup_credit_email_states_the_real_grant`,
which drives `send_signup_credit_email` and asserts the rendered subject and
body against the constant. The behavioural backstop does the work the name
regex cannot. That test earns its place.

### The evasions that matter

M16-M18 are contrived. **M11 and M14 are not.**

M11 was run under the **entire suite** with the mutation confirmed on disk, and
I then re-ran my rendered-surface scan against the mutated tree:

```
[result] raw $5-ish hits across all surfaces: 2
  /tools/af2  [$5]  ...from your wallet. We fund your first $5 so you can try it..."}, "name":   <- FAQPage JSON-LD
  /tools/af2  [$5]  ...from your wallet. We fund your first $5 so you can try it...</dd>          <- visible FAQ
```

So a live, stale `$5` reaches both the visible FAQ **and the FAQPage structured
data** — the exact bug class round 1 found — while **5190 tests pass**. The
guard is keyed to a fixed vocabulary of phrasings, not to the concept.

---

## The two written questions

### Is `$15` genuinely impossible to drift from?

**No. It is much harder to drift from than before, and the realistic
literal-reuse cases are closed, but it is not impossible.** The strongest
evidence in its favour is M13: the grant can be moved `15.00` to `25.00` in one
line of `shared/wallet.py` and the **entire 5190-test suite stays green** with
no other file touched. That is the single-source property, demonstrated rather
than asserted.

**The most likely surface a future edit could break without the guard noticing:
`templates/email/*.txt`.** The template guard is `TEMPLATES.rglob("*.html")` —
plain-text templates are not walked at all. Two `.txt` email templates already
exist (`job_complete.txt`, `reengagement.txt`), and the signup credit email is
itself a template (`templates/email/send_signup_credit.html`). Adding a
plain-text multipart alternative — `send_signup_credit.txt` — is a completely
routine thing to do, and a hardcoded `$5` in it would be invisible to every test
in the repo (M14, proven). This is the only evasion I found that a *legitimate,
likely* change produces **by accident** rather than by contrivance.

Second most likely: a new tool's `meta.py` `seo_faq` answer phrased outside the
regex's vocabulary (M11). Fourteen tools already ship one; the fifteenth will be
written by whoever adds it, and "we fund your first $15" is as natural a
sentence as "new accounts start with $15". It reaches FAQPage JSON-LD.

Cheap closes if wanted (not required for this merge): change the glob to
`rglob("*")` over `templates/`, and add `&#36;`/`&#x24;` to the sigil class.
Neither closes M11, which is inherent to phrase-matching.

### What does the guard newly forbid that a legitimate future change would want?

The regex's bare `|credit` and `|grant` alternatives are broad enough to flag
sentences with nothing to do with signup. I ran eight plausible future strings
through `SIGNUP_MONEY` — **six are falsely flagged**:

| sentence | flagged? | matched |
|---|---|---|
| `A $25 credit will be issued for the failed run.` | **YES** | `$25 credit` |
| `We have applied a $12.40 credit to your wallet for the interrupted job.` | **YES** | `$12.40 credit` |
| `Campaigns start with a $50 minimum spend.` | **YES** | `start with a $5` |
| `Enterprise plans start with an $800 monthly commitment.` | **YES** | `start with an $8` |
| `Your refund of $5 credit has been processed.` | **YES** | `$5 credit` |
| `This run consumed $3.10 of credit.` | **YES** | `$3.10 of credit` |
| `The pilot grant covers $2000 of compute.` | no | |
| `Support may issue up to $100 in credit per incident.` | no | |

A **failed-run refund/credit email** and a **pricing sentence with a minimum**
are both entirely plausible near-term additions, and both would turn this guard
red for no real reason. The failure message points at
`shared.wallet.SIGNUP_CREDIT_USD`, which would be actively misleading advice in
those cases. Not a blocker — the fix is a one-line regex tightening when it
first bites — but the next person to hit it should not be told to import the
signup credit into a refund email.

Second: the guard now forbids reading the signup credit from the environment at
all. That is deliberate and documented, but it does remove the ability to run a
promotional signup credit without a deploy. **Operational follow-up not covered
by any test:** any deployed environment still setting `WALLET_SIGNUP_CREDIT_USD`
now silently does nothing. `docs/WALLET-ENV-VARS.md` says to delete it, but
nothing verifies it was deleted.

---

## Verdict

# MERGE

Reviewed `c9e45829ffdd1b5a66987d7d975056c681255e70` against trunk
`5e4fa66af07cd15830ee542331eaf2722c61c69c`.

All six claims hold up under independent re-derivation:

1. **Zero surviving `$5`** — verified with my own scan, proven non-vacuous
   (14 adapters, 14 pages at 200, 36 surfaces) and proven **sensitive**
   (6 hits on trunk, 0 on branch). The signup flash was driven through the real
   POST success path: `$15` on branch, `$5` on trunk.
2. **The guard enforces it** — 13 of 18 mutations caught, each by the correct
   named test, each with the mutation proven on disk first.
3. **Not vacuous** — 191 files walked, all 12 required modules present, no
   `tests/` leakage.
4. **No false negatives** — all 10 genuinely-different `$5`s pass on content,
   with the allowlist deleted entirely; `auth.py:348` is safe twice over.
5. **The circular-import claim was false** — 8 import permutations, fresh
   interpreter each, both orders, all OK.
6. **The merge is lossless** — `git merge-tree` reproduces exactly the two
   declared conflicts and nothing else; `blueprints/auth.py` diffs against main
   as *only* this PR's three hunks, so #152's `safe_next` work is fully intact.

Baselines green on both sides, +5 reconciled to the 5 named new tests, and both
edited pre-existing tests were strengthened rather than relaxed.

**Not blocking, tracked here for follow-up:**

- `templates/*.txt` is outside the template guard (M14). Most likely real drift
  surface.
- The guard matches phrasings, not the concept, so alternative wording evades
  (M11) — proven to reach FAQPage JSON-LD with the full suite green.
- HTML entities and line-split literals evade (M10, M12, M15) — contrived.
- The regex false-positives on refund/credit and pricing-minimum prose (6/8
  plausible sentences).
- Any environment still setting `WALLET_SIGNUP_CREDIT_USD` is now a silent
  no-op; nothing verifies its removal.

None of these are regressions — every one was equally or more broken before this
PR, which shipped six live stale surfaces. The branch strictly improves the
situation and the guard is real.

---

*QC performed in detached worktrees under the session scratchpad. The main
working tree was never modified. Scan/mutation scripts (`qc_scan.py`,
`qc_flash2.py`, `mutate.py`, `run_mutations.sh`) were kept out of the repo
deliberately; every command and result needed to reproduce is quoted above.*
