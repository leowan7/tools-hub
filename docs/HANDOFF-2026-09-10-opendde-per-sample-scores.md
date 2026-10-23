# tools-hub — handoff 2026-09-10 (opendde per-sample scores)

OpenDDE was attaching the wrong confidence scores to most of its predictions on
any multi-sample run, so every sample within a seed showed the first sample's
numbers and the ranking built on them was wrong too. It never errored, so a user
comparing samples would conclude that sampling does nothing. That is fixed,
merged as PR #245, and live in production; the Modal image rebuilt 9/9 and prod
reports build `30f6116`. Four independent review rounds went over it and the
fix's logic came through unchanged.

## Done this session

- Fixed the score lookup in `tools/opendde/run_pipeline.py`. It searched the
  structure's folder and its parent and fell back to the first JSON it found,
  and OpenDDE keeps one folder per seed holding every sample, so all samples in
  a seed got the same file.
- Established that OpenDDE already writes one confidence file per sample, so
  nothing upstream had to change. The lookup now derives that one exact file
  and reads no other.
- Made a missing score record as no score instead of borrowing a neighbour's.
  It sorts last and shows a dash, which is visible rather than silently wrong.
- Closed two crash and mis-sort risks found in review: a NaN could scramble the
  ranking of healthy rows, and an oversized number could throw an error that
  escaped and killed a paid GPU run.
- Added nine tests built on the tool's real output layout, each proven to fail
  against five deliberately broken versions of the fix.
- Verified the fix against the actual pinned container image, not just the
  public package, by reading the image's own source layer.
- Recorded the session in the harness memory and compacted the memory index
  back under its size limit.

## Next steps

- Decide whether to add a score-coverage count to the job result, so a run that
  collects no scores at all cannot report success silently. This is the one
  follow-up worth doing.
- Consider capturing a real OpenDDE run to give the tool a worked example on
  its page. It has none, and capturing one costs GPU spend.
- Add a note explaining that single-chain jobs correctly show ipTM 0.00, which
  currently reads as alarming.
- Optional cleanup: a comment in the OpenDDE results template claims nothing
  reads the `filter_status` field, which is not true. Harmless for OpenDDE, and
  it predates this work.

## Notes

- The ranking, not just the displayed numbers, was affected, so any earlier
  multi-sample OpenDDE result a customer still has should be treated as having
  an unreliable order.
- Squash merges make the usual "is my commit an ancestor of main" check say no.
  Confirm a landing by looking for the change in main's files and at the
  deployed build number instead.
- A reviewer's suggested wording is a lead, not a verdict. One round told me a
  claim was unsupported and gave me a replacement; a later round fetched an
  artifact neither of us had checked and showed the replacement was false and
  the original correct.
