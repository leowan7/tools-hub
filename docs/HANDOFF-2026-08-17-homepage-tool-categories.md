# tools-hub - handoff 2026-08-17 (homepage tool categories)

Proteina-Complexa and OpenDDE were showing up under an "Other" heading on the
homepage because neither had an entry in the tool category map, so a fallback
label supplied their section. Both are now in their real stages, the five stage
sections are numbered so the catalog reads as an ordered pipeline, and the Yeast
Display Library Planner has been taken off the homepage and the tools page. All
of it is merged as PR #140 and confirmed live on tools.ranomics.com. What is left
is cosmetic copy cleanup, plus two decisions that are yours.

## Done this session

- Fixed the reported bug: Proteina-Complexa now sits under "Design binders" and
  OpenDDE co-folding under "Structure prediction". The "Other" section is gone
  from the live site. Those two were the only tools of fourteen affected.
- Numbered the five stage sections (scope, design, sequence, predict, QC) and
  gave each its own band, so the grouping is visible rather than implied.
- Removed the Yeast Display Library Planner tile from the homepage and the tools
  page. Its page still works at /library-planner, so nobody's existing job links
  break.
- Added a test that fails if a future tool is added without a category, so this
  cannot silently happen again.
- Ran an independent QC agent before pushing, as asked. It cleared the change for
  deploy and caught one real defect in the new test, which is fixed.
- Merged and deployed: PR #140, verified on the live site after the deploy
  settled.

## Next steps

- Decide whether the signup-credit email should stop advertising the Library
  Planner. It still names the tool to brand-new users, who can no longer find it
  from the homepage or the tools page. Same stale mention sits in README.md, the
  wallet top-up page, and the jobs list.
- Consider numbering the group headings on the /tools page too, so it matches the
  homepage.
- Small polish: the stage number counts only the sections actually shown, so if
  some tool flags are switched off the last section reads 4 instead of 5.
- Before anyone switches the /readyz uptime monitor back on, give it a retry
  threshold. That endpoint returns an error during every deploy and this repo
  deploys several times a day, so as configured it would alert constantly.

## Blocked / waiting on

- Needs your decision on whether the Library Planner should be fully
  decommissioned rather than just delisted. I deliberately kept its page, code,
  and tests so old job links keep working, and because deleting that folder would
  force a rebuild of all nine GPU images.
- Needs your decision on the signup-credit email copy, since changing outbound
  email wording felt outside the scope of a homepage fix.

## Notes

- The "Other" heading was never in any template. It came from a fallback value in
  the Python catalog, so the fix belonged in the data, not the page markup. If a
  category ever looks wrong again, that map is the place to look.
- The deploy branch was cut fresh from main rather than from the branch that was
  checked out. That branch was 134 commits behind main with 26 modified files, so
  committing there risked both a stale-base merge and disturbing in-flight work.
  Nothing in your working tree was touched.
- Main moved three times while this landed (#141, #142, #143). #142 was the dead
  code sweep, which rebuilt all nine GPU images, and the homepage change went
  live inside that same build.
- The QC agent found that the new test passed for the wrong reason when run on
  its own: the tool list is empty unless the app module is imported first, so the
  test was checking nothing. Worth remembering for any future test shaped as
  "every tool must have X".
