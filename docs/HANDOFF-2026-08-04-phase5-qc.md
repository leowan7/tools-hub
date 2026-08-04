# Phase 5 handoff, 2026-08-04

Read this before touching `blueprints/lab_projects.py`, `blueprints/targets.py`,
`templates/components/candidate_table.html` or `static/js/candidate_table.js`.

## State

Phase 5 is **built, independently QC'd, and NOT remediated**. The code is
committed on branch `docs/a81-campaigns-launch-grouping` as a WIP checkpoint.
It is **not merged, not pushed, and not deployable as-is**.

- Baseline before Phase 5: 2088 passed / 6 skipped.
- Now: **2136 passed / 6 skipped**, +48 tests, zero regressions.
- Phase 3 is on `origin/main` at `f230f14` (PR #101), deployed and verified.

**Migration `0040_lab_campaign_target_source.sql` has NOT been applied.** It must
run in the Supabase SQL editor *before* this code deploys. Pre-0040 the insert
violates 0037's enum CHECK, the exception is swallowed, and the user is
redirected back to the target page with **no flash and no error at all**. That
silent failure is itself finding A-8 below.

## What Phase 5 does

`/targets/<id>` already pooled every design from every tool into one ranked
table (Phase 3). Until now its "Send shortlist to Ranomics lab" bar was hidden
there, because the modal's hidden inputs branch on `campaign_id` and fall back
to `source_job_id`, and on a target table both are empty. Phase 5 wires a target
branch so designs spanning multiple tools can be handed to the wet lab.

Built: migration 0040, `create_campaign_from_target_refs`,
`_submit_target_shortlist` with the IDOR gate, staging prefix `{tool}-{jid8}/`,
the cross-tool breakdown on the handoff email, the 5.2 button hierarchy, and
A82 group headers. **A81 was deliberately dropped** once the diff passed 1200
lines; its register entry is intact and re-verified as still accurate.

## What QC verified as SOUND

Stated because it was the main risk and it held up under mutation:

- **The IDOR boundary.** Tenancy and provenance are two genuinely independent
  checks. The parent target is owner-gated *before* any job lookup,
  `campaign_ids_for_target` is itself owner-scoped and is passed `user_id`, and
  a ref reaching the caller's own job on a *different* target is rejected. Each
  check individually goes red under mutation.
- **Migration 0040 vs 0037.** The new predicate is a strict superset, so it
  **cannot reject any row 0037 accepted**. Idempotent. Enum widened correctly.
- **Dispatcher ordering**, staging prefix sanitisation, CSRF on the new POST
  route, and every `submission_source` consumer handling `'target'`.
- **A84 is TRUE.** Ops really has been receiving empty candidate lists on every
  campaign-sourced shortlist since 0037, because the admin template rendered
  `candidate_indices` and `source_job_id`, both NULL on a `'campaign'` row by
  0037's own CHECK. The fix in this diff is load-bearing.

## The tree-corruption incident, and the two lessons

Two QC agents were run **concurrently against the same working tree**. One
mutates production code to verify tests are load-bearing; the other was reading
that tree. A restore of `blueprints/targets.py` failed silently and left a live
mutant on disk: the starred-export filter reduced to `candidates =
list(candidates)`, a no-op. Had it shipped, "Starred only (CSV)" would have
downloaded **every** design under a filename saying `_starred`.

1. **Never run a mutating reviewer concurrently with any other agent on the same
   tree.** Serialise them, or give the mutator its own worktree.
2. **`git diff --stat` cannot detect a same-line-count mutation.** The diffstat
   read exactly `17 files, +1445/-122` throughout. A single `sha256` after `cp`
   also reported clean while the mutated bytes were still on disk, apparently a
   Windows caching artifact. A green suite would not have caught it either: the
   dangerous case is a mutation **no test caught** being left behind.

Repaired and verified: line endings unchanged (1208 CRLF / 0 bare LF),
`_row_ref` back to 2 references, and every site either reviewer reported as a
*surviving* mutation re-checked by hand and found correct.

## Open findings: 22, none remediated

### Must fix before commit to main

- **B-1 (HIGH). The A82 group-header gate misreads `multi_tool`.** It is gated
  on `multi_tool`, which Phase 3 deliberately redefined as multi-**cohort**
  (`shared/target_results.py`: `len(tools) > 1 or any(len(presets) > 1)`). One
  tool at two presets therefore renders a lone group header over a row list
  identical to percentile order. The comment above the gate explicitly claims it
  prevents "a lone group header over a grouping that did not happen", which is
  verbatim the failure it causes. Reproduced with proteina at two presets.
  `templates/targets/detail.html` already gates its own controls correctly on
  `len(tools) > 1`; the macro cannot currently see a tool count.
  **This is the third recurrence of this exact misreading (A75, A77, now B-1).**
- **B-2 (HIGH).** `test_a_one_tool_target_draws_no_group_header_under_sort_tool`
  passes `multi_tool=False` explicitly, which is not what the caller passes for
  a one-tool split-preset target. Load-bearing for a different property than its
  name claims.
- **B-3 (HIGH). Every JS-to-template hook added here is pinned on ONE side
  only.** Four mutations to `static/js/candidate_table.js` survive the entire
  suite, each yielding an empty CSV named `_starred` at HTTP 200 with no error:
  renaming `.cand-starred-export`, removing the `submit` listener, emitting
  `{j,i}` instead of `{job_id,index}`, and renaming `shortlist-hint-`. There is
  no JS test harness in the repo.
- **A-7 (HIGH). Silent partials on a paid intake.** Rejected refs are dropped
  with no disclosure: 3 of 10 starred designs rejected means the submit proceeds
  with 7 and the confirmation email says 7. Worse, `campaign_ids_for_target`
  returns its partial list **from inside its own `except`**, so a transient DB
  failure silently narrows the accepted campaign set.
- **A-8 (HIGH).** Pre-0040 (and on any transient failure) the handoff is a
  silent no-op: swallowed exception, redirect, no flash.
- **A-2 (HIGH). The `_target_export` "exact" claim is false above 500 starred
  rows.** `_starred_refs` routes through `_parse_candidate_refs`, which silently
  truncates at `_MAX_CANDIDATE_REFS = 500`. That constant's own comment
  enumerates its consumers and misses the third one added in the same diff,
  which is what produced the false claim.
- **B-11 (MEDIUM, scope escape).** `candidate_table` and `results_shell` are
  shared, so 5.2's button changes also landed on the campaign page and 14 tool
  results pages. No test parses those action bars, so a future edit can silently
  reintroduce a second primary on 14 pages.

### False claims to correct (house rule: a comment is a claim)

- **A-1.** Two of the three owner-scoped reads are unpinned because the test
  fakes accept and discard `user_id`. Drop the scope from either and all 102
  tests stay green. A docstring asserts "`get_target` is owner-scoped" when the
  test shows only that the route redirects when a patched fn returns `None`.
  The same file gets this right for `get_job`, with a docstring explaining why.
  *Not a live vulnerability* (blast radius is a target-existence oracle), but it
  is the check most likely to be simplified later with nothing to catch it.
- **A-3.** Migration 0040's safety comment says `shared/targets.py` has "no
  delete path". `_delete_target_row` is at `:665`. It is creation-rollback only,
  so the `ON DELETE CASCADE` is not a data-loss path today.
- **B-6.** The `raw_tool` vs `this_tool` rationale in `candidate_table.html`
  is correct, but mutating `per_tool.get(raw_tool)` to `this_tool` survives the
  whole suite. Needs a one-row fixture.
- **B-7.** `test_the_star_tooltip_names_a_general_shortlist...` asserts on a
  string appearing on both the `<th>` and every `.star-btn`; dropping it from
  either leaves the test green.

### Robustness and disclosure

- **A-4 / A-5.** `_ref_shortlist_view` miscounts refs lacking a `job_id`, and
  **500s on a non-dict element** in `candidate_refs`. Both latent (the app's own
  writer cannot produce them) but a 500 on the ops fulfilment page is bad.
- **A-6.** The ops-facing design count is `len(refs)`, unvalidated against what
  is actually staged. Duplicate refs or an out-of-range index inflate it, and
  the new admin view **promotes that number to a claim it did not previously
  make**.
- **B-9.** No-JS / pre-JS regressed. The send button lost `disabled` and gained
  nothing, so with JS off it looks live and throws `ReferenceError`. "Starred
  only (CSV)" posts the render-time `refs="[]"` and returns a header-only file
  at HTTP 200.
- **B-10.** Zero-star and unresolvable-ref exports are silent, while the same
  function adds `_incomplete` to the filename for a partial read on the stated
  principle that the artifact is opened later, out of context.
- **B-4 / B-5.** Two dead-CSS declarations lost to specificity
  (`.cand-shortlist-hint` font-size and colour; `.cand-group-row td` padding).
  The pre-existing `.viewer-row td` works around the same collision inline; the
  new group row did not carry the workaround over.
- **B-8.** `id="send-to-lab-btn-{{ scope }}"` is now bound by nothing; its only
  reader is a test regex.
- **B-12.** `tests/test_campaign_submitted_email.py` is the only new or modified
  test file without `isolate_supabase`.

### Low, defence-in-depth

- **A-9.** `shared/email.py` interpolates tool slugs and ids into staff-email
  HTML unescaped. Constrained to registry slugs and DB uuids today.
- **A-10.** `job.target_id == source_target_id` is a case-sensitive string
  compare against form input; an uppercase UUID passes `get_target` but fails
  the standalone-job arm, yielding a silently partial shortlist.

### Filed by the builder, not fixed

**A85** admin "Source" links 404 for staff (no staff bypass on owner-scoped
routes). **A86** `PUBLIC_BASE_URL` leaks from the repo-root `.env` into the whole
test session via `load_dotenv()`; `isolate_supabase` blanks only the four
Supabase vars. **A87** the campaign shortlist branch re-queries a rejected job id
once per ref, now bounded at 500.

## Recommended next actions

1. Remediate B-1, B-2, B-3, A-7, A-8, A-2, B-11, then every false claim.
2. **Re-QC the fix set.** In this repo the fix set is reliably the highest-risk
   surface in the diff: Phase 3 needed four rounds, and round 18's findings were
   *all* caused by round 17's own fixes.
3. Apply `0040` in Supabase, then deploy.
4. A browser walk. Phase 3's browser walk found A81 and A82 in about ten minutes
   after four adversarial review rounds and 2088 tests had missed both.

## Non-negotiable constraints for whoever picks this up

- The repo-root `.env` holds **real production Supabase and Stripe credentials**
  and `app.py` calls `load_dotenv()` at import. The only permitted pytest
  invocation, from the repo root:
  `SUPABASE_URL= SUPABASE_KEY= SUPABASE_ANON_KEY= SUPABASE_SERVICE_ROLE_KEY= venv/Scripts/python.exe -m pytest -q --no-header <files>`
  Blanking those four vars is `isolate_supabase` applied process-wide. It does
  **not** blank Stripe.
- Four untracked docs must never be swept into a commit:
  `docs/HANDOFF-2026-07-03-*`, `docs/HANDOFF-2026-07-10-*`, `docs/WALLET-CAP-*`,
  `docs/audit-2026-06-17/`. Stage by explicit path; never `git add -A`.
- Line endings are per file. `blueprints/targets.py` and
  `templates/components/candidate_table.html` are CRLF.
