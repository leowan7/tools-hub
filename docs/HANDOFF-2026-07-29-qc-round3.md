# Handoff: Phase 2 QC rounds 3 and 4 (remediation COMPLETE, pending commit)

Written 2026-07-29. Supersedes the mid-task version of this file.

## State in one line

All ten round-3 items (A-J) and every round-4 finding are fixed and
mutation-verified in the working tree. **Nothing is committed yet.** Push and PR
remain a separate explicit ask.

```
HEAD      9e4f92d feat(targets): launch N tools against one target in one gated action (Phase 2)
          485cfb6 docs: Phase 2 handoff, git-verified state and open decisions
          fb1cee0 feat(targets): composite preauth and concurrency division for multi-tool launch
branch    feat/phase2-multi-tool-launch   (3 commits ahead of origin/main, NOT pushed)
dirty     blueprints/targets.py  shared/compute_campaigns.py  shared/idempotency.py
          templates/targets/launch.html  docs/audit-2026-07-22-campaign-rework-open-items.md
          tests/test_compute_campaigns.py  tests/test_idempotency.py
          tests/test_target_launch.py  tests/test_target_multi_launch_routes.py
```

## Round 3: four agents over the Phase 2 diff

Verdicts FIX FIRST x3 + SUITE PARTIALLY TRUSTWORTHY. Headlines, all reproduced
by measurement before fixing:

- `preauth_message` rounded the required top-up **down** (`%.2f` is half-even), so
  a user refused at a 573.6736 gate was told "about $573.67" and a wallet topped
  to exactly that was refused again by the same sentence.
- Two tests **could not fail**. Divided concurrency across 2 tools is an identity
  (`32 // 2 == 16` is rfdiffusion's solo width). The flag-gate POST test passed
  with the gate deleted because IgGM's own adapter rejected anyway.
- `tests/test_compute_campaigns.py` priced money against the **live**
  `tool_jobs_p90` view: its fake binds `cc.get_service_client`, but `plan_chunks`
  reaches `wallet_estimates._historical_p90_seconds`, which late-imports
  `credits.get_service_client`.

Items A-J are all done. Each has a named mutation that reddens exactly the
intended test. See the round-3 sections of the audit register for detail.

## Round 4: two agents over the REMEDIATION diff

This is the fourth consecutive round where reviewing the fixes was as productive
as reviewing the original work, and the first where a fix was **worse than the
defect it fixed**. Full write-up in the audit register, addendum 2026-07-29e.

The one that matters, if you read nothing else:

**The fund/drive guard inverted the reporting on a money path.** Round 3 wrapped
the loop so nothing could raise past it, and set `moved = False` in the handler.
But `fund_campaign` cannot raise (`_cas_transition` swallows everything and
returns `False`), so the only reachable exception was from
`drive_campaign_async` -- i.e. `threading.Thread(...).start()` -- i.e. exactly the
case where the campaign is already `funded`. `funded` is in
`cron/tick_campaigns.py::_ACTIVE_STATES`, so the tick drives it and it bills.
The guard therefore told the user "None of those runs could be started. Your
wallet was not charged." about N funded, billing campaigns. And because that is
a **400**, and the round-3 idempotency change releases the claim on any status
>= 400, the retry the copy invites would create and fund a **second full set**
against a gate the user passed once.

Now: the fund is the sole commit point and decides started-vs-stalled; a
drive-spawn failure logs and the campaign stays *started*. No test anywhere made
either callee raise, which is how this shipped inside a QC fix. Three tests added.

Also fixed in round 4:
- `_store_response` was unscoped, so a losing sibling's 400 overwrote the
  winner's cached 302. `_release_key`'s scoping alone was necessary, not
  sufficient; it is the composition that failed.
- `debounced()` cleared the internal `latest` but left the previous price
  **rendered** for the whole repricing window; and `fetchEstimate` had no
  request-generation guard, so with two fetches in flight the last to *resolve*
  won rather than the one describing the current form.
- Four more tests that could not fail, including a placeholder test parametrized
  over a hardcoded tuple instead of the message table.
- Five false comment claims, three of them fabricated numbers, plus a wrong audit
  citation.

## Round 5: one agent over the round-4 FIXES

Verdict **FIX FIRST** again. Fifth consecutive round finding defects in the
previous round's fixes, second in a row where the defect was in a fix to a money
path. Full write-up: audit register, addendum 2026-07-29f.

**`fund_campaign` returning `False` is ambiguous, and round 4 leaned on it.**
`_cas_transition` catches every exception and returns `False`, so `False` means
either "the row was not in draft" or "the UPDATE raised and I cannot tell". A
write that commits in Postgres while the response read times out is the second
case, and round 4 rendered it as the first: campaign is `funded`, it bills, and
the user is told it was not charged and invited to relaunch. Round 4's own
headline defect, arriving through the other branch -- which is the lesson, since
round 4 fixed the reported input rather than the class.

Now the route confirms a `False` with an owner-scoped `get_campaign` and calls a
campaign stalled only when the row is **confirmed** still `draft`. Moved or
unreadable is treated as started, because claiming "not charged" about money that
may be committed is the more expensive error and the one that duplicates.

Also from round 5: my `_IdemTable.delete()` stub raising `NotImplementedError`
did NOT "fail loudly" (`_release_key` swallows it), which made the double-fund
assertion unfailable; the coercion guard did not cover NaN (`Decimal("NaN")`
quantizes without raising and rendered "about $NaN to start"); the length prefix
closed the boundary between parts but not inside one (a field NAME may contain
`=`); "three surfaces" was one; and a "file tag" I added came with a comment
claiming a property it does not provide, caught by mutating my own new code.

## Verification state

- **Full suite: 1794 passed, 6 skipped, 0 failed** after round 5. Baseline before
  this work was **1704 passed / 6 skipped**, so 90 net new tests and no
  regression. Takes ~5 min, not the ~20 an earlier version of this file claimed.
  There is no pytest config, so always confirm the collected count is in the
  1700s, not 10.
- Every behavioural fix in all three rounds is mutation-verified: revert one
  line, confirm exactly the intended test goes red, restore. No `MUTATION`
  marker remains anywhere in the tree.
- `templates/targets/launch.html` parses as Jinja, and its `<script>` block
  passes `node --check` with Jinja expressions stripped.

## Next, in order

1. Push and PR are a **separate explicit ask** from Leo. Do not push.
2. **The round-5 fixes have not themselves been independently reviewed.** Five
   rounds in, every review has found something in the previous round's fixes, so
   the base rate says a round 6 would too. Each round-5 fix is mutation-verified
   individually and two false claims in that batch were caught by mutating the
   author's own new lines, but that is self-review. A round 6 over just the
   round-5 slice (`blueprints/targets.py`'s confirming read,
   `_form_fingerprint`'s framing, `preauth_message`'s finite guard,
   `_IdemTable.delete`) is the cheap next step if Leo wants it.
3. **A46 needs Leo, not code.** The reasoning behind "a funded campaign has
   started" depends on `campaigns:tick` actually being scheduled, and no schedule
   exists in this repo (Procfile has only `release` and `web`; no railway
   manifest). If the Railway cron is absent or paused, a drive-spawn failure
   needs a real retry rather than a log line.
4. No live production walk -- Leo chose "No live walk yet". Wallet is $24.50.
   Cheapest full-path walk: rfdiffusion + pxdesign at 1 design each holds $9.18.
   Report figures, do not spend.
5. **A47:** the launch page's `<script>` has no automated coverage at all, and
   three rounds running have found defects in it. It is only `node --check`ed.

## Still open, filed not fixed

**A41-A45** (idempotency TOCTOU, `blueprints/tools.py` 200-on-error,
`_store_response`'s own failure path, the single-tool route discarding
`fund_campaign`'s bool, a deliberate re-launch inside 60 s reading as a no-op).

**A46** the campaign tick has no schedule in this repo -- needs Leo to confirm the
Railway cron. **A47** the launch page's JS has no automated coverage.

**A40, remeasured: 26 of the 32 test files that reference `create_app` have no
`isolate_supabase`**, so they read production. The register had named three by
inspection and undercounted by an order of magnitude. Not fixed here because the
fixture blanks env for a whole module and adding it to 26 files changes the
environment of several hundred unrelated tests.

## Rules still in force

- **Repo-root `.env` holds real production Supabase service-role credentials**
  and `app.py` calls `load_dotenv()` at import. Any test importing `app` and
  exercising a route reads and writes **production** unless it opts into
  `tests/conftest.py::isolate_supabase`. **Grep any test file for
  `isolate_supabase` before running it.** Never write an ad-hoc script that
  calls `create_app()`.
- Pure planner functions (`divide_concurrency`, `plan_multi_launch`) can be
  probed safely by blanking the four `SUPABASE_*` vars inline: a blank URL or key
  makes `shared/supabase_client.py` return `None` rather than construct a client.
- **Commit != push.**
- **Comments are claims.** Four rounds running, the top defect class. A number in
  a comment is a claim too: five fabricated figures across rounds 2-4.
- **A guard is a claim about which exceptions can reach it.** Before catching,
  enumerate what actually throws. Round 4's headline defect came from wrapping
  the failure a reviewer described without checking which callee could raise.
- **A fake that omits a method fails silently in the safe-looking direction.**
  Five instances across rounds 3-5. Raising instead of omitting works only where
  the call site does not swallow: `is_()` can refuse because it runs on the
  builder outside the `except`, but `delete()` cannot, because `_release_key`
  wraps it in a bare `except Exception` and a raise becomes the same `False` as a
  missing method. Check where the exception would land before choosing.
- **A boolean from a function that swallows exceptions is three-valued.** `True`,
  `False`, and "I could not tell" arrive as two values. Any user-facing claim
  built on one -- above all "you were not charged" -- must confirm independently
  or not make the claim. Two rounds running produced a money-reporting inversion
  from treating such a bool as definitive.
- **Mutate your own fix, not just the code it touched.** Two false claims in the
  round-5 batch were found by mutating lines written minutes earlier. Adding a
  test is not evidence that the test can fail.
- No emojis. No em dashes, en dashes, or connector hyphens in prose.
