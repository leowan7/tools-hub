# Session state — multi-chain finish, 2026-08-07

> **This file previously said the opposite of what is below.** It led with
> "PR #120 never taught the gates the prefixed hotspot TOKEN". That was wrong,
> and the correction is the most important thing on this page. Nothing else
> here should be read as settled without checking the branch it names.

## The correction

The handoff `docs/HANDOFF-2026-08-07-multichain-finish.md` says the backend was
finished by PR #120. **It was right.** An earlier pass in this session claimed
otherwise, having "verified" it by executing the gates — but in a working tree
on `feat/proteina-inline-designs`, whose base `4b03ab8` predates #120's merge
`80389d1`. Probing a clean checkout of `origin/main` (`2151a01`) settles it:

```
validate_hotspots(rep, "A",   ["A256"])          -> in_range=['A256']  out_of_range=[]
validate_hotspots(rep, "A,B", ["A256","B256"])   -> in_range=both      out_of_range=[]
validate_hotspots(rep, "A,B", ["C256"])          -> in_range=[]        out_of_range=['C256']
preflight_for_tool("rfdiffusion", "A,B", prefixed) -> READY
preflight_for_tool("pxdesign",    "A,B", prefixed) -> READY
preflight_for_tool("boltzgen",    "A,B", prefixed) -> READY
```

Trunk's implementation is also better than the one written against the stale
base: it handles `bool`, `float`, `"296.0"`, empty strings and whitespace, and
its repair suggestions come back **prefixed** (`"B268"`) so they can be pasted
straight back into a multi-chain field. That rewrite has been dropped entirely.

Lesson, recorded in memory as `worktree-base-drift`: executing against the wrong
base produces *more* confidence than reading would, not less. Probe a detached
`origin/main` worktree before claiming trunk lacks a behaviour.

## Branches

| branch | base | state |
|---|---|---|
| `feat/multichain-ui-finish-v2` | `origin/main` (`2151a01`) | **the real one.** Gate rewrite dropped, everything else rebased. Local only. |
| `feat/multichain-ui-finish` (`47092e5`) | `4b03ab8` (stale) | superseded snapshot. Its commit message states the false premise. Delete once v2 lands. |
| `llm-proteinDesigner` `fix/boltzgen-design-iptm` (`46527d5`) | `master` | pushed as PR #18, **not merged**. Merging deploys five Modal apps. |

The main `tools-hub` working tree still holds the superseded stale-base version
as uncommitted changes. It is fully preserved in `47092e5`, so reverting it
loses nothing — but that tree is shared with another session, so check before
running anything destructive in it.

## What is actually in the change

Templates only, plus tests and one staged asset:

- `chainPrefixed: true` on rfdiffusion / pxdesign / boltzgen (trunk has it on
  proteina only; the picker has implemented the path all along).
- `maxlength` 4 → 32 on the four binder forms, proteina, and both target routes.
  4 physically caps the target at two chains — `"A,B,C"` cannot be typed.
- Hotspot and target-chain help copy that mentions the `A296,B264` form.
- `templates/components/hotspot_picker.html` deleted — dead, and it emits the
  opts block *without* `chainPrefixed`.
- The multi-chain ipTM unreliability banner + `shared/score_legends.py`.
- `static/example/3ave_igg1_fc_dimer.pdb` and
  `docs/HANDOFF-2026-08-07-fc-run-ready.md`.

Carve-outs, both commented at the code: **rfantibody** must never get the flag
(its adapter rejects `"A25"`; its container builds `--hotspots AA25`), and
**bindcraft is held** (prebuilt image, parser vendored nowhere, no smoke tier).

## Not done

1. **Independent QC** — three agents were dispatched into isolated worktrees.
   Check their reports before landing; check each actually returned.
2. **Landing** — `feat/multichain-ui-finish-v2` is local, unpushed, no PR.
3. **PR #18 merge** — Leo's action. Optional zero-GPU de-risk first: pull a
   prior BoltzGen run's `aggregate_metrics_analyze.csv` off the Modal Volume and
   diff `iptm` against `design_iptm` to see whether `IPTM_THRESHOLD = 0.70`
   still holds.
4. **The Fc run** — staged, not launched. Leo launches.
5. **Follow-up when #18 deploys**: drop `"boltzgen"` from
   `MULTICHAIN_IPTM_UNRELIABLE_TOOLS` and delete the test that pins it.

## Still open, inherited, not touched

- `shared/result_columns.py` gives proteina an `af2_iptm` column, and proteina is
  the only tool that has actually run multi-chain on a GPU. If that is an AF2
  complex-wide interface pTM it has the identical defect. Untraced.
- `blueprints/jobs.py` raises `ValueError` on a prefixed hotspot when refolding
  into Boltz-2. Pre-existing; the handoff lists it as out of scope.
- Four unreproduced Round-5 QC findings from PR #120 — chiefly whether the
  campaign and target-launch routes call `preflight_for_tool` at all.
