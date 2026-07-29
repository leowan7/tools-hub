# Handoff: target-first rework, Phase 2

Written 2026-07-29 at the end of the session that shipped Phases 0 and 1.

Read this, then form your own view. It is deliberately split into **verified
facts** (checkable, with file:line) and **the previous session's opinions**
(flagged, and you should feel free to disagree). Do not treat the second
section as settled.

---

## 1. The goal

Upload a protein **target** once. Fan **many design tools** at it in parallel.
Merge every design into **one ranked table**. Download it. **Optionally** hand
a shortlist to the Ranomics CRO for wet-lab validation.

Pricing is a prepaid USD wallet, fund-and-drain. That part is live, canaried,
and must not be redesigned.

The full 8-phase plan is at
`~/.claude/plans/i-recently-made-many-jaunty-lerdorf.md`. **Read it before
starting.** It contains the file:line evidence behind every decision below,
plus an "Edge cases, security, and workflows" section and a "What not to
build" list that are both load-bearing.

---

## 2. Where things stand (git-verified 2026-07-29)

| | |
|---|---|
| Repo | `C:\Users\lab\Documents\Claude_projects\tools-hub` |
| `origin/main` | `f6ae6dc` |
| Working branch | `feat/phase2-multi-tool-launch` |
| Unpushed on it | `fb1cee0` only |
| Working tree | clean |

**Shipped, merged, deployed, and walked on production:**

- **Phase 0** (PR #97) — the defect fixes that Phase 3+ depends on.
- **Phase 1** (PR #98) — `design_targets` as a first-class parent. Migration
  **0039 is applied to production**.
- **Un-archive + archived states** (PR #99, `02d2a24`) — Phase 1 cleanup.

**Coded, committed, NOT pushed:**

- `fb1cee0` — `shared/target_launch.py` + `tests/test_target_launch.py`.
  21 tests pass. This is the first piece of Phase 2 and has **not** had an
  independent QC pass. Treat it as unreviewed.

Migration numbering: 0039 used. **0040 is reserved** for Phase 5's
`lab_campaigns.source_target_id`. Use 0041+ if you need one.

---

## 3. Hard safety constraint — read before running any test

The repo-root `.env` holds **real production** `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY`, and `app.py` calls `load_dotenv()` at import.

**Any test that imports `app` and exercises a route performs real reads and
writes against the production database** unless the test file opts into
`tests/conftest.py::isolate_supabase`:

```python
pytestmark = pytest.mark.usefixtures("isolate_supabase")
```

Grep any test file for `isolate_supabase` before running it. These are known
safe: `test_targets.py`, `test_target_routes.py`, `test_run_create_from_target.py`,
`test_target_id_persistence.py`, `test_target_launch.py`.

Run the suite with `venv/Scripts/python.exe -m pytest -q <files>`. There is no
pytest config file, so a bare `--ignore=tests` exits 0 with 10 unrelated
passes — always confirm the collected count matches what you asked for.

---

## 4. What Phase 2 is

From the plan: **launch up to 7 tools against one target in one gated action.**

Remaining work:

1. **Per-tool param panels** for all 7 tools. A shared block (target chain,
   hotspots, prefilled from the target row) plus a per-tool fieldset. The
   plan's §2.2 table lists exactly what each tool needs beyond the shared
   block, confirmed against the adapters.
2. **`GET /targets/<id>/launch`** (the form) and
   **`GET /api/targets/<id>/launch-estimate`** (itemised per-tool estimate,
   mirroring `api_runs_estimate` at `blueprints/campaigns.py:204`).
3. **`POST /targets/<id>/launch`** with `@login_required @idempotent()`.
   The plan's §2.3 specifies the ordering: own the target → reject flag-gated
   tools → validate every spec → plan → preauth → mint `launch_group_id` →
   create all campaigns as **draft** → fund each → drive each.
4. Tests, mutation-verified.
5. Independent QC before any push.

**The atomicity design is already decided and is the important part:** create
every campaign as `draft` first and fund only afterwards. A draft is inert —
never funded, never dispatched, never billed, and excluded from
`_campaign_spend_today` (`shared/compute_campaigns.py:888`) — so a failure
partway through leaves no cleanup and no charge.

---

## 5. Verified facts you can rely on (check them anyway)

These were traced to source this session. The file:line is so you can confirm
rather than trust.

- **`campaign_preauth` is a pure gate. No debit, no hold.**
  (`shared/compute_campaigns.py:997`.) Real money moves only in
  `_dispatch_chunk` via `reserve_hold` → `try_hold_for_job`. Consequence:
  calling preauth once per tool reads the same balance N times, so all N pass
  a gate only one can afford.
- **`create_campaign` treats concurrency 0 as "unset".**
  `max(1, int(concurrency_target)) if concurrency_target else
  launch_concurrency_for(tool)` (`shared/compute_campaigns.py:615-619`). 0 is
  falsy, so it does not clamp — it silently restores the tool default.
- **proteina is throttled to 4** (`_LAUNCH_CONCURRENCY_OVERRIDE`,
  `shared/compute_campaigns.py:131`) because one shard is a full A100.
  `GLOBAL_USER_INFLIGHT_CAP` is 32 (`:153`).
- **`first_wave_hold_usd` already takes concurrency as a parameter**
  (`:1103`), so dividing concurrency needs no new arithmetic.
- **All 7 tools are in `SUPPORTED_TOOLS`** and all 7 appeared in the live
  production dropdown. proteina does **not** hard-block bring-your-own
  targets, and bindcraft is not broken — an earlier analysis claimed both and
  was wrong.
- **PostgREST clamps `.limit()` to `max_rows` (1000).** Only `.range()` paging
  escapes it. A clamped read is indistinguishable from a complete one at the
  call site.
- **PostgREST returns `PGRST205` for a missing table**, never raw `42P01`.
- **`_dispatch_chunk` re-mints a presigned URL from
  `campaign.target_storage_path` every wave**, which is why targets denormalize
  their path onto each run and why storage must never be deleted mid-campaign.

**Live production walk of Phase 1 (2026-07-28)** — all passed: target created
and parsed (194 residues, 1 chain), launch form inherited the target with the
file input `disabled`, a bad chain was rejected with no spend, a real run
completed 8 candidates, and the wallet settled **$2.19 held → $1.93 billed,
$0.26 released**. Billed ≤ held ≤ authorized.

---

## 6. Open decisions — NOT resolved. Yours or Leo's to make.

- **Pace default for multi-tool launches.** `shared/target_launch.py` defines
  `PACE_BURST` and `PACE_STEADY`. Neither is wired to a UI yet and no default
  has been chosen. Both drain to the same total; only the start gate and ramp
  differ.
- **Whether stranded `draft` campaigns should be filtered or cleaned.** They
  render on the target page today via `list_campaigns_for_target`, which has
  no status filter.
- **Whether the yardstick refold (Phase 4) should place a real wallet hold**
  rather than the check-only gate. Flagged in the plan as Leo's call.
- **30-day vs 90-day retention.** Migration 0021 says 30,
  `templates/legal/terms.html:64` says 90. They still disagree. Leo's call.

---

## 7. Working agreements (Leo's standing preferences)

- **Commit ≠ push.** Auto-commit is fine *after* an independent QC agent
  reviews the diff clean. Pushing and opening PRs is a separate, explicit ask.
- **Mutation-verify every fix**: revert it, confirm exactly one test goes red,
  restore. This is not optional here. Phase 0 shipped 3 real bugs with a fully
  green suite because assertions only checked that a kwarg reached a mock.
- **Comments are claims.** The single most recurring defect on this project,
  found in four consecutive QC rounds, is a comment or docstring asserting a
  property the code does not have — including inside the comments written to
  fix that same problem. Verify every comment you write.
- No emojis. No em dashes, en dashes, or connector hyphens in prose.
  Minimal focused changes. Short concise responses.
- Always overestimate cost. An authorized figure must be a true ceiling.
- Registers: repo defects → `docs/audit-2026-07-22-campaign-rework-open-items.md`
  (currently at A34). Product gaps → `docs/PRODUCT-PLAN.md`.

---

## 8. Money, before any live walk

The wallet is at **$24.50**. Priced against the real chunk planner, 7 tools at
24 designs each:

| Pace | Budget | Start gate |
|---|---|---|
| Burst | $226.66 | $280.91 |
| Steady | $226.66 | $143.42 |

At 48 designs each, burst rises to $416.95 while steady stays at $143.42
(steady starts each tool at one chunk, so the gate stops tracking the design
count). The gate exceeding the budget in burst is expected: the per-chunk hold
is cushioned above the estimate, and this already happens for a single
campaign today.

**Phase 2 cannot be walked end-to-end on production until the wallet is
funded.** Build and test without touching production, then tell Leo the exact
figure the walk needs. Do not launch paid work without explicit approval of
the amount.

---

## 9. The previous session's opinions — disagree freely

Flagged separately because they are judgement, not evidence.

- The plan's own risk note suggests shipping the 4 tools that share an
  identical param block first (rfdiffusion, bindcraft, boltzgen, pxdesign),
  then adding rfantibody, proteina, and iggm. **Leo chose all 7 in one go**
  when asked directly, so build all 7 — but the sequencing note exists because
  iggm requires an antibody FASTA and epitope residues while proteina takes
  neither hotspots nor binder length, and those two are where a form bug
  becomes wrong-parameter GPU spend.
- The previous session believed the real guard is **server-side**: run each
  selected tool's actual `adapter.validate()`, and let any single failure
  block the entire launch and create nothing. Form-level UX is not the
  safety mechanism.
- It leaned toward steady as the multi-tool default, on the grounds that the
  start gate stops depending on design count. This was never tested with a
  user and is not a decision.
- `shared/target_launch.py` returns concurrency **index-aligned with the
  specs** rather than keyed by tool, so the same tool twice on one target
  gets two slots. If you find a reason that is wrong, change it — it is not
  load-bearing anywhere else yet.
