# tools-hub — handoff 2026-08-19 (deploy trigger covers static/example)

Four of the nine GPU images bake a shared example file into themselves as a built-in smoke
test, but those example files sat in a folder the deploy system was not watching. Editing one
would have quietly changed four images without redeploying any of them, with nothing failing
and nothing turning red. That is now fixed and shipped, and a new automated check makes the
same mistake impossible to repeat for any file, not just these three. The fix is live on main
and the redeploy of all nine apps has been confirmed as green. A follow-up review then found
six defects in the new check itself, none of which affect the fix that shipped; those are
fixed in PR #160, which is open and not yet merged.

## Done this session

- Fixed the deploy trigger so the three shared example files are watched. Shipped as PR #159,
  merged to main as `4ffcb52`.
- Confirmed the fix actually worked in production: the deploy ran on the exact merge commit
  and all nine apps redeployed successfully. The test suite on that commit passed too.
- Added an automated guard that fails the build if any image ever bakes in a file the deploy
  system is not watching. It checks the general rule, not just this one case, and it uses
  Modal's own file-matching code so it cannot drift from what the real build does.
- Ran an independent QC review before committing anything, which found two real problems in
  the first draft of the guard. Both were fixed, along with three smaller points.
- Established that no damage was ever done by this bug. The three example files have not been
  edited since April, so no image was ever left stale. The fix is purely preventive.
- Merged the latest main into the branch after two other pull requests landed mid-session, and
  absorbed a follow-up change that had originally been written down as someone else's to-do.

## Next steps

- Merge PR #160. A second independent review, run after this work was already merged, found
  six defects in the new automated check. All six are live on main right now. #160 fixes five
  of them and is ready and passing. Leo has left it with the session that opened it.
- Optional hardening once #160 lands: extend the check to cover the second way a file can
  reach an image (`add_local_file` in a `modal_app.py`). There is no gap today because every
  current use points at a file already being watched.

## Notes

- The original fix is sound and doing its job. The six follow-up defects are all in the new
  automated check: an incomplete list of special characters it refuses to interpret, and one
  self-check that could never actually run. Neither weakens the deploy trigger itself.

- Anyone editing the list of watched deploy paths in future must also update the matching list
  in `tests/test_deploy_paths_exclusions.py`. That test compares the two lists exactly, on
  purpose, because the order of entries changes their meaning. Changing one without the other
  turns the suite red.
- The three example files are listed individually rather than by wildcard. Two other files live
  in the same folder and are used only by documentation, so a wildcard would have triggered a
  rebuild of all nine images to ship a file that no image contains.
- A second Claude session was working on the same branch at the same time and had already
  pushed its own version of the same merge. Nothing was lost, but only because the rejected
  push was investigated rather than forced through. Worth knowing that concurrent sessions on
  one branch is a live pattern here.
