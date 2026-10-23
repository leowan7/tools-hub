# tools-hub — handoff 2026-08-19 (metrics zero-byte fix)

The Prometheus metrics page has been silently broken in production for as long as it has existed, returning an empty page instead of any numbers. The cause was a startup ordering mistake: a setting was applied one step too late, after the metrics library had already made a decision it cannot revisit. That is now fixed, merged and deployed, and confirmed working on the live server. Worth knowing before anyone celebrates: the page is still closed to everyone by a separate access rule that was never configured, so nothing can read it yet and nothing ever has.

## Done this session

- Confirmed the metrics page really was returning an empty body in production, checked on the live server rather than only reproduced locally.
- Established the one thing the original report could not: the hosting environment does not supply the setting, so the ordering mistake was genuinely the cause and not a false alarm.
- Fixed it with a four-line change to the gunicorn startup config, shipped as PR #167 and deployed.
- Verified the fix on the live server after deploy. The directory the counters write to went from empty to holding one file per process, which is the direct evidence it now works.
- Added an automated check so this cannot break again silently, and had an independent reviewer try to break it.
- Tightened that check after the reviewer showed the first version could pass while the bug was live.
- Found and recorded two traps for whoever touches this next: adding the setting through the hosting dashboard on its own would crash the site on startup, and editing nixpacks.toml would have done nothing at all.

## Next steps

- Decide what to do with the metrics page: leave it dormant, open it up, or remove it. The recommendation on record is to leave it dormant and not invest in Prometheus, because the same data already lives in the database in a durable, queryable form, while these counters reset on every deploy.
- If the decision is to open it, that is a project rather than a config change: it needs a scraper stood up and the access list configured, and the endpoint sits on the public internet.
- Look at whether two overlapping health-check endpoints are both needed. Only one of them is named in the alerting documentation. Noticed in passing, low stakes, not investigated.

## Notes

- The production fix survived independent review untouched. Everything the reviewer found was in the automated check and in the explanatory comments, which is the same pattern as the six previous review rounds on this project.
- The most useful lesson from the review: the check was asserting that the metrics page was not empty, which is a symptom, when it could have asserted which mode the metrics library had selected, which is the actual property. A leftover file from an earlier run was enough to make the weaker version pass over a live bug.
- To confirm this is still working after any future deploy, run
  `railway ssh --service web --environment production "ls -1 /tmp/prom"`.
  Zero files means it has broken again. One file per process means it is healthy.
- The branch base drifted two commits behind during the session and was caught before pushing. Anyone quoting test counts from this session should quote the change, not the total, because the rebase pulled in five tests from other people's work.
