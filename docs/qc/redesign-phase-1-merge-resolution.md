# QC — PR #144 merge resolution (`feat/redesign-phase-1-open-tool-pages`)

Independent review. The reviewer did **not** perform this merge.

| | |
|---|---|
| Branch head reviewed | `721edba4d5096dc1646fcbe9996e34fab37194b0` |
| Remote head (`gh pr view 144`) | `721edba4d5096dc1646fcbe9996e34fab37194b0` — **matches** |
| `origin/main` at review time | `37a0f3a` (`Merge pull request #149`) |
| Mergeable | `MERGEABLE` / `mergeStateStatus: CLEAN` |
| Date | 2026-08-18 |

**VERDICT: MERGE.** The merge resolution is clean and *provably* lossless
(method below). One non-blocking correctness finding (F1), one refuted claim
(F2), and one **goal-check FAIL** (F3) that is not caused by this PR but does
mean the PR does not, on its own, achieve its stated purpose.

---

## 1. SHA + mergeability — PASS

```
$ git rev-parse origin/main
37a0f3a9b5e43471a900568e1ac3cf1789ca5aea
$ git merge-base 721edba origin/main
37a0f3a9b5e43471a900568e1ac3cf1789ca5aea
$ gh pr view 144 --json headRefOid,mergeable,mergeStateStatus
{"headRefOid":"721edba...","mergeStateStatus":"CLEAN","mergeable":"MERGEABLE"}
```

`origin/main` **has not moved** since the merge — the merge base *is*
`origin/main`, so `721edba` is a strict fast-forward-able descendant. #150
(`71cf4cf`) and #151 (`2c057fc`) are both already in that history, contained in
`83dbf31` (#148). Nothing to re-merge.

Merge topology:

```
721edba  = merge(85cfa6a, 37a0f3a)          <- #149 in
85cfa6a  = merge(773b955, 83dbf31)          <- #148/#150/#151 in  [the real conflict]
773b955, f20b7b4                            <- the branch's own two commits
```

## 2. Suite baselines — PASS, delta fully accounted

Measured myself, in dedicated detached worktrees, `-m pytest -q` from the repo
root with no path argument, repo venv:

```
origin/main 37a0f3a : 4976 passed, 20 skipped in 297.61s (0:04:57)
head        721edba : 5023 passed, 20 skipped in 311.10s (0:05:11)
```

Delta **+47, zero failures**. Reconciles with the claim: the claimed
`83dbf31 = 4968` plus #149's `+8` gives the 4976 I measured on current main,
and `+47` from `tests/test_public_tool_pages.py` (47 collected) gives 5023.

Per-nodeid set comparison (stronger than counting — an equal total can hide a
deletion offset by an addition):

```
$ comm -23 ids_main.txt ids_head.txt        # removed on head
tests/test_scout_anonymous_access.py::...test_feasibility_get_requires_login[/scout/feasibility/download/c425376e-...]
tests/test_wallet_api.py::TestEstimateEndpointShape::test_returns_zero_balance_for_anonymous
```

Both explained, neither a loss:

- the scout id is a **randomly generated UUID parameter** — it differs between
  any two collections; the added set contains the same test under a different
  UUID.
- `test_returns_zero_balance_for_anonymous` was **renamed and strengthened** to
  `test_returns_null_balance_for_anonymous`. The old test asserted one thing
  (`balance_usd == "0"`); the new one asserts nine, including
  `hard_block is False` and `Decimal(estimate_usd) > 0`. Strictly stronger.

## 3. Lost-hunk hunt — PASS (this was the main job)

**Method.** Counting hunks or eyeballing a diff cannot prove a merge lossless.
Instead I reconstructed what git's own three-way automerge *would* have
produced and compared it to what was committed, then proved set-equality of
both parents' patches against the merged tree.

**Step 1 — automerge vs. committed tree.**

```
$ T=$(git merge-tree --write-tree 85cfa6a 37a0f3a)
$ git diff --stat $T 721edba
                     (empty)
```

The final merge `721edba` is **byte-identical to git's automerge** — there was
no human resolution in it at all. (Note: the `--cc` combined diff shows
`blueprints/tools.py` and `test_developability_anonymous.py`, but that is
automerge combining, not manual editing.)

For the earlier merge `85cfa6a`, where the real conflict lived:

```
$ T=$(git merge-tree --write-tree 773b955 83dbf31)
$ git diff --stat $T 85cfa6a
 app.py                                    | 4 +---
 templates/components/_signin_cta.html     | 2 +-
 tests/test_signup_credit_single_source.py | 7 -------
```

Exactly three deliberate human edits, and **nothing else** — the diff-of-diffs
contains 12 changed lines total, all three accounted for below.

**Step 2 — both parents' patches present verbatim.** For each side I diffed
"the delta that side introduced" against "the delta that actually landed",
after normalising away blob hashes and `@@` line offsets:

```
$ norm() { grep -v '^index ' "$1" | sed -E 's/^@@ -[0-9,]+ \+[0-9,]+ @@/@@/'; }
MAIN-SIDE   (83dbf31..37a0f3a  vs  85cfa6a..721edba) : IDENTICAL
BRANCH-SIDE (83dbf31..85cfa6a  vs  37a0f3a..721edba) : IDENTICAL
```

This is the strongest available textual guarantee:
`merged = main + branch-patch-verbatim = branch + main-patch-verbatim`.
**No hunk from either side was dropped, truncated, or silently superseded.**

**Step 3 — files on one side only.** Enumerated explicitly, since these are the
ones git merges with no conflict signal:

- Added by branch, never on main: `templates/components/_signin_cta.html`,
  `templates/tools/_form_hero.html`,
  `templates/tools/_hotspot_scout_hint.html`,
  `tests/test_public_tool_pages.py`
- Deleted by branch: `templates/tools/_preview.html`,
  `templates/tools/boltzgen_preview.html`,
  `templates/tools/rfdiffusion_preview.html`

All seven reviewed individually (§6, §7).

**Step 4 — semantic supersession on the one contested file.**
`blueprints/tools.py` was changed by both sides (main +32/-4, branch +279).
Textual presence is not enough, so I checked #149's actual intent survived:
`@login_required` is absent from both `/developability` and
`/developability/score` on the merged head, and #149's eight tests pass there
(`8 passed in 3.02s`).

**Step 5 — no conflict markers** anywhere in `*.py` / `*.html` on head.

**The app.py conflict (claim 1) — verified, with one correction.** It was an
add/add in `create_app()` with markers `<<<<<<< 773b955` / `>>>>>>> 83dbf31`,
and **both sides were kept**. All five globals are registered on head:
`viewer_is_signed_in` (463), `signin_url` (470), `signup_url` (479),
`tool_public_context` (493) — the branch's four — and `signup_credit` (508) —
main's. *Correction:* the conflict was in `85cfa6a`, not in `721edba`; the
latter needed no resolution.

## 4. Anonymous POST gate, all 14 tools — PASS (re-verified independently)

Re-verified with real requests in a standalone script that deliberately does
**not** inherit `tests/conftest.py` (which sets `CSRF_PROTECT=0` process-wide).
Here `CSRF_PROTECT=1` — production posture. The `noTok` column is the control:
a token-less POST returns **403 from a `before_request` hook and never reaches
`@login_required`**, so a naive test would be testing CSRF, not auth. The
token is harvested from the anonymous GET so the POST reaches the auth gate.

```
tools discovered: 14 -> ['af2','bindcraft','boltz2','boltzgen','colabfold',
 'esmfold','esmfold2-design','iggm','mpnn','opendde','proteina','pxdesign',
 'rfantibody','rfdiffusion']

slug              GET  tok  noTok  submit                        preflight
af2               200  True 403    302 /login?next=/t job=0 ctx=0  302 ... job=0 ctx=0
...  (all 14 identical) ...
rfdiffusion       200  True 403    302 /login?next=/t job=0 ctx=0  302 ... job=0 ctx=0

PASS: 14 tools, GET 200, POST submit+preflight -> /login,
create_job/load_user_context/get_or_create_wallet never called,
with CSRF ENFORCED and a real harvested token.
```

Also probed **`POST /campaigns`** — the endpoint the anonymous reroute script
rewrites `form.action` to (see F2), which the branch's own suite does not
cover anonymously:

```
anonymous POST /campaigns -> 302 /login?next=/campaigns create_job_calls=0
```

`compute_campaign_create` carries `@login_required` + `@idempotent()`.

*Scope note:* the branch's own gate test parametrises only
`PUBLIC_TOOLS = ("mpnn","proteina","boltz2")` — 3 of 14. The all-14 sweep in
`TestNoAnonymouslyReachableSubmitControl` covers GET and controls but **not**
the POST gate. My run above closes that to 14/14.

## 5. Submit-capable controls in rendered HTML — PASS

Scanned the **rendered** anonymous HTML for all 14 tools with `html.parser`
(not regex over template source), deliberately wider than the branch's own
`_submitting_controls`: `type=submit`/`type=image` **anywhere on the page**,
bare `<button>` inside **any** form, `form=` attribute binding (a control
outside the form can still post it), any element — not just `<button>` — named
inside a `.submit()`/`.requestSubmit()` script, elements named by an
**un-hiding** script (`.hidden=false`, `classList.remove`, `style.display=`,
`.disabled=false`), and inline `on*=` submit handlers.

Result: **zero real submit-capable controls on any of the 14 anonymous pages.**
Every raw hit was an `<input type="file">` or `<input type="number">` — fields
the reroute script *reads*, not controls a user can press.

Two traps worth recording, since this repo shipped a breach here before:

- A naive `'type="submit"' in body` check reports **true** on every anonymous
  tool page. The only occurrence is inside a JS selector string:
  `form.querySelector('[data-submit-cta], button[type="submit"]:not(...)')`.
  Grep-based checking of this invariant produces a false positive; the parser
  correctly returns zero.
- **HTML implicit submission.** "No submit button" does not imply "cannot be
  submitted": with no submit button, Enter submits a form that has exactly one
  field blocking implicit submission. Measured per tool:

  ```
  slug              blockingFields  submitBtns  enterSubmits?
  esmfold           1               0           True      <-- only one
  (other 13)        2..8            0           False
  ```

  `esmfold` **can** be implicitly submitted with Enter anonymously. Not a
  security hole — that POST goes to `/tools/esmfold/submit`, verified above as
  302 → `/login` with `create_job` called 0 times — and `esmfold` is not
  campaign-supported, so no action rewrite applies. Recorded as a UX note.

**Signed-in positive control (the gap the resolver could not close) — PASS.**
The resolver reported it could not prove the signed-in render out-of-suite
because a bare session redirects to `/login` without `isolate_supabase`. Closed
here by blanking the four `SUPABASE_*` vars **before importing `app`** (what
`isolate_supabase` does) and patching `load_user_context` in **both**
`app` and `blueprints.tools`:

```
signed-in: pages 200         -> 14 / 14
signed-in: real submit btn   -> 14 tools: True
signed-in: campaign btn      -> ['bindcraft','proteina','pxdesign','rfantibody','rfdiffusion']
signed-in: anon-only hero    -> absent (correct)
```

So the anonymous result is a real gate, not a deleted feature.

## 6. More stale-value cases of the `_signin_cta.html` shape — PASS

`_signin_cta.html` now reads `${{ signup_credit() }}` (line 28) — **claim 3
verified**, and §8 M4 proves a test now polices it.

Swept the other three branch-added files and the heavily-rewritten partials for
values main has since centralised (signup credit, self-serve ceiling, tool
count, URLs, cost formatting). **No second instance found.**

- `_hotspot_scout_hint.html` — uses `url_for('scout.index')`, not a literal.
- `_campaign_reroute.html` — reads the ceiling from the server-supplied
  `data-campaign-ceiling` attribute; no literal.
- `_form_hero.html` — mentions `tools.ranomics.com` as prose only.
- `blueprints/tools.py:658` adds `https://www.ranomics.com/technology/{slug}`
  (JSON-LD). It is the **only** occurrence of `www.ranomics.com` in Python,
  a different host from the centralised `tools.ranomics.com`
  (`shared/compute_campaigns.py:1793`, `shared/email.py:44`). Not duplication.

## 7. Deleted preview templates genuinely unreferenced — PASS

`grep -rn` across templates, Python, tests and docs for `_preview.html`,
`boltzgen_preview`, `rfdiffusion_preview`: **every** remaining hit is a
reference in prose — code comments explaining the deletion
(`blueprints/tools.py:489,612`, `templates/base.html:101`,
`about_panel.html:21,139`, `_form_hero.html:7`,
`test_multichain_iptm_notice.py:1641`) or the assertion list in
`test_public_tool_pages.py:186-188` that asserts they no longer resolve. **No
`render_template`, `include`, `import`, or `extends` reaches any of them.**
(Unrelated `cost_preview.html` hits in `docs/HANDOFF-*` are a different, older
file.) `TestPreviewShellIsGone` is a real guard — see §8 M3.

## 8. Mutation testing — PASS (6 mutations, 6 kills)

Every mutation confirmed landed via `git diff` **before** running, and reverted
after (`git status --porcelain` empty each time). Run in my own detached
worktree; the main working tree was never touched.

| # | Mutation | Diff confirmed | Result |
|---|---|---|---|
| M1 | Strip `@login_required` from `tool_submit` **and** `tool_preflight` | `-@login_required` ×2 | **9 failed** — the whole `TestSubmitGateStillHolds` class |
| M2 | Delete `{% if viewer_is_signed_in() %}` around the campaign button | `-{% if ... %}`, `-{% endif %}` | **1 failed** — `test_every_tool_form`, naming all 5 tools (`bindcraft, proteina, pxdesign, rfantibody, rfdiffusion`) — reproduces the historical 5-of-14 breach exactly |
| M3 | Restore `templates/tools/_preview.html` from `37a0f3a` | `?? templates/tools/_preview.html` | **2 failed** — `TestPreviewShellIsGone` *and* `test_no_template_hardcodes_the_signup_amount` |
| M4 | Revert `_signin_cta.html` to the stale hardcoded `$5` | `-${{ signup_credit() }}` / `+$5` | **1 failed** — `test_no_template_hardcodes_the_signup_amount` |
| M5 | Drop the Scout hint `{% include %}` from `rfdiffusion_form.html` | `-{% include "tools/_hotspot_scout_hint.html" %}` | **1 failed** — `test_hotspot_field_carries_an_inline_scout_link` |
| M6 | Revert anonymous `balance_usd` to `"0"` | `-None` / `+"0"` | **1 failed** — `test_returns_null_balance_for_anonymous` |

M3 and M4 are the load-bearing ones for **claim 4**: with the
`_preview.html` exclusion dropped, the single-source test *does* fire on the
restored file (`tools\_preview.html:81: New accounts start with a $5 wallet
balance`), and it *does* fire on a reverted `_signin_cta.html`. So the
exclusion was genuinely dropped, the test still passes on head, and it now
covers the file that carried the stale value. These guards do not certify
false — each was broken and watched go red.

## 9. Goal check — **FAIL** (see F3)

Walked the anonymous journey with one client session.

The tool page itself is excellent. `/tools/mpnn` anonymously: real run form,
all input fields, explainer panel, live cost estimate, sign-in CTA,
account-creation CTA, JSON-LD, no submit control. Everything the PR promises.

**But a bench biologist cannot get there from inside the app.** Measured
direct (`/tools/<slug>`) versus walled (`/login?next=/tools/<slug>`) links on
every anonymous entry point:

```
/                        200  direct=0  walled=14
/tools                   200  direct=0  walled=14
/pricing                 200  direct=0  walled=0
/showcase                200  direct=2  walled=0   -> boltzgen, rfdiffusion
/help/getting-started    200  direct=0  walled=0
```

**Where they get stuck: the first click on the homepage.** All 14 catalog cards
read "Sign in to run" and point at `/login`. `templates/index.html:327-332`:

```jinja
{% if tool.external %}
  <a class="home-catalog-card" href="{{ tool.route }}">
{% elif authenticated %}
  <a class="home-catalog-card" href="{{ tool.route }}">
{% else %}
  <a class="home-catalog-card" href="{{ url_for('auth.login', next=tool.route) }}">
{% endif %}
```

The `/tools` comparison page — "All tools and pricing →", the obvious second
try — walls all 14 the same way. The only unwalled in-app path is `/showcase`,
reaching 2 of 14. Today the public tool pages are reachable **only** by typing
the URL or arriving from a search engine.

---

## Findings

### F1 — non-blocking — the branch's POST-gate test covers 3 of 14 tools

`PUBLIC_TOOLS = ("mpnn","proteina","boltz2")`. The all-14 sweep
(`TestNoAnonymouslyReachableSubmitControl`) covers GET and controls but not the
POST gate, so a future tool added with a hand-rolled unguarded submit route
would not be caught. I verified all 14 hold **today** (§4). Fix: parametrise
`TestSubmitGateStillHolds` off `tool_base.all_adapters()` like `all_tools_app`
already does.

### F2 — non-blocking — claim REFUTED: the campaign-reroute script *does* render anonymously

The claim was that the `form.submit()` path "is jinja-guarded behind
`viewer_is_signed_in()` so it never renders anonymously." Only the **button**
is guarded. In `templates/tools/_campaign_reroute.html` the `{% if
viewer_is_signed_in() %}` closes at line 40; the `<script>` runs 44–135 and is
**outside** it. The full script ships to anonymous visitors on all 5 campaign
tools.

Not currently exploitable, for three independent reasons I verified:

1. `campaignBtn = getElementById('campaign-submit-btn')` is `null` anonymously,
   so `campaignBtn.addEventListener('click', ... form.submit())` never binds —
   the only `form.submit()` call is unreachable.
2. All 5 campaign tools have ≥5 implicit-submission-blocking fields, so Enter
   cannot submit them either (§5).
3. `POST /campaigns` is `@login_required` — verified 302 → `/login`,
   `create_job` 0 calls.

It is still a real defence-in-depth gap: `apply()` **does** run anonymously and
**does** rewrite `form.action` to `/campaigns`, inject
`tool`/`requested_designs` hidden fields, and hide the estimate panel whenever
an anonymous visitor types an over-ceiling count with a file attached. The
invariant test would not catch a future change that adds any pressable control.
Fix is one line: move the `<script>` inside the same guard, or wrap
`campaign_reroute_script()` in `{% if viewer_is_signed_in() %}`.

### F3 — BLOCKS THE GOAL, NOT THE MERGE — the homepage still walls all 14 tools

Detailed in §9. `templates/index.html:331` sends anonymous visitors to
`/login?next=...` for every tool card, and `/tools` does the same.

**This is not a merge-resolution defect and not a regression.** `index.html` is
not in this PR's changed-file list, and the same line also defeats #148
(`/login?next=/scout/`) and #149 (`/login?next=/developability`), both already
on main. But it does mean **#144 does not achieve its stated purpose on its
own**: the pages are public and indexable, yet unreachable by navigation.
Fix: drop the `{% elif authenticated %}` branch so the card always links to
`tool.route`, and switch the anonymous CTA copy from "Sign in to run" to
something like "See how it works".

---

## What I could NOT verify

- **Production behaviour behind a real proxy/CDN.** Everything here is the
  Flask test client. Edge caching of a now-public, session-varying page is
  untested — `/tools/<slug>` renders differently for anonymous and signed-in
  viewers, so a cache that keys on URL alone could serve a signed-in render to
  a stranger. Worth confirming `Vary`/`Cache-Control` before deploy.
- **Real browser JS execution.** The reroute script was analysed statically and
  by DOM-shape reasoning, not executed. F2's conclusion that `campaignBtn` is
  `null` follows from the rendered HTML (`'id="campaign-submit-btn"' in body ==
  False` anonymously), which is solid, but no headless browser confirmed it.
- **The `esmfold` Enter-key path end to end** — proven safe by route-level
  assertions (302 → `/login`, `create_job` 0), not by pressing Enter in a
  browser.
- **Rate limiting / abuse surface** of the newly public GETs. #149's docstring
  explicitly defers this to the edge; unchanged and unassessed here.

## Reproduction

Scripts used (kept out of the repo, in the session scratchpad):
`qc_gate.py` (§4), `qc_controls.py` (§5), `qc_implicit.py` (§5),
`qc_journey.py` (§9). Two detached worktrees at `37a0f3a` and `721edba`; the
main working tree was never modified.
