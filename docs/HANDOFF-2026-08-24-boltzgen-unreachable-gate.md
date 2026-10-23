# tools-hub - handoff 2026-08-24 (BoltzGen unreachable gate)

Every BoltzGen candidate we have ever delivered was labelled "below threshold": 65 out
of 65 across six production runs. The cause is not the model and not the designs. Two of
the three pass/fail legs were reading numbers whose range never touches their own bar,
and the interface score in particular is being compared to a bar that belongs to a
different measurement entirely, so no run could ever pass. The fix is written and both
test suites are green, but nothing is pushed yet: the container repo carries the real
fix, tools-hub carries the copy and tooltip corrections, and the two need to land
together.

## Done this session
- Found the root cause: the pLDDT leg read the whole conditioned complex (54.5 to 66.1
  against a bar of 80) rather than the binder's own refold, and the interface score
  (0.084 to 0.583) was being compared to 0.70, a bar that comes from a different kind of
  fold that BoltzGen never runs.
- Proved the interface bar cannot be fixed by picking a better column: BoltzGen refolds
  the design on its own, with no target present, so none of the 206 columns it writes
  contains a binder-to-target interface score at all.
- Confirmed the designs themselves are fine. Independently re-folding the same sequences
  with Boltz-2 gives 0.363 to 0.852, and the top pick by the in-run number came fourth on
  the real one, so the old number was not just miscalibrated, it ranked wrong too.
- Fixed the container pipeline: pLDDT now reads the refold, the pass/fail label uses
  pLDDT and RMSD only, the interface score is still shown and still ranked on but no
  longer gates, and a missing score now fails closed instead of passing silently.
- Added a warning that fires if a future BoltzGen version stops emitting the refold
  column, which is the exact path that would bring this bug back unnoticed.
- Fixed the user-facing side in tools-hub: the false 0.7 bar had been copied into the
  score legend, so it was repeated in the results tooltip and in every job-completion
  email. Also corrected the tool blurb, which claimed candidates arrive "already checked
  against the site you aimed at" when the refold has no target in it.
- Ran both suites in full. tools-hub 5587 passed / 21 skipped, exit 0, which is +4 tests
  over its base. Container repo 462 passed / 8 skipped / 3 xfailed, +8 tests, and 5 of
  the new ones were confirmed to fail against the old code first.

## Next steps
- Coordinate with the other session before touching the tools-hub branch, then rebase it:
  it was cut from `d96d66d` and main has moved to `395c523`.
- Push and open both PRs together. The tools-hub copy change is meaningless while the
  container still gates on the wrong number.
- Read the per-metric column log from the first real run after the container ships. It
  answers, for free, whether the wrapper is reading the metrics file from the right
  directory, which is the one thing here still unverified.
- Re-check the six existing production jobs after the fix is live. Their stored labels
  stay wrong until they are re-run or re-labelled.

## Blocked / waiting on
- Pushing is blocked on the parallel session that was preparing a PR on
  `claude/nostalgic-sanderson-3c2fe9`. Both branches are local-only, so nothing is at
  risk, but confirm order with them first.
- Needs your decision on whether BoltzGen should ever report a true interface score. That
  requires a second, co-folding pass, which is new GPU spend, not a code change.
- Nothing here was validated by a real BoltzGen run, since running one bills an A100.
  Every figure came from result files already on disk.

## Notes
- Branches: tools-hub `claude/nostalgic-sanderson-3c2fe9` = `a4fa162` (web tier) and
  `9c36520` (tools/ copy), base `d96d66d`. Container repo llm-proteinDesigner
  `fix/boltzgen-unreachable-gate` = `62837b1`, base `3a2382a`.
- The two tools-hub commits are deliberately separate because any edit under `tools/`
  rebuilds all nine GPU images. Keep them apart through the rebase.
- The container fix changes the pass/fail label and the pLDDT column. It does not change
  the interface number that gets reported.
- Do not try to line up the co-fold results against the audit spreadsheet row by row.
  They are separate stochastic runs and the same design gets different numbers, so any
  comparison table built that way is fiction. I built one, checked it, and threw it away.
- Full diagnosis, measurements and the evidence trail are in memory under
  `boltzgen-gate-unreachable`.
