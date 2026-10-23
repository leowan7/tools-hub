# tools-hub - handoff 2026-08-11 (modal stale-result guard)

A P0 bug that could deliver one customer's designs under another customer's job id is now fixed and live in production. It had been sitting written but uncommitted since 2026-08-08; this session committed it, verified it, had it independently reviewed, and shipped it. All nine GPU tools redeployed cleanly. One related weakness remains open by choice, and it is written up below as the next piece of work rather than rushed in alongside this fix.

## Done this session

- Shipped the fix as PR #133, merged, with all nine Modal apps redeployed and green.
- Fixed the leak in the six unguarded GPU tools: boltz2, esmfold2_design, iggm, mpnn, opendde, proteina. The other three already had the guard, so all nine are now covered.
- Committed the 104-test suite that enforces the guard automatically, so a tool added later without it fails the build.
- Had the whole change reviewed by an independent agent that did not write it. Verdict: ship, with no blocking defects found.
- Re-checked a figure quoted in the commit message that had only ever been measured against an older version of the code. It held up exactly.
- Ran down a scary-looking test run (two failures, forty minutes) and proved it was machine load, not the change. The identical re-run was completely clean.

## Next steps

- Close the root cause: the job-result reader in `gpu/modal_client.py` still marks a run successful without checking whether the pipeline actually exited cleanly. This session's fix is currently the only thing preventing the bug class. A background task has been queued for it.
- Before that lands, work out whether any tool can legitimately finish its work and still exit non-zero during cleanup. A naive fix would start failing those runs, which is why it was deliberately kept out of this PR.
- Separately, `tools/esmfold2_design/run_pipeline.py` never clears its `/tmp/results` folder, so a job that dies early can file a previous job's structures inside its own evidence archive. Confirmed this affects the evidence bundle only and not what the customer is served.

## Notes

- Merging is the deploy for this repo. Pushing to main redeploys all nine Modal apps automatically, and one app is allowed to fail while the rest succeed, so every job needs checking individually rather than trusting the overall green tick. All nine were checked and passed.
- Two tests in the suite shell out to Node with a sixty second limit and go red when the machine is busy: one in `test_hotspot_picker_runtime.py` and one in `test_lab_project_confirmation.py`. If either fails, re-run before investigating. The reliable tell is time per test rather than total runtime.
- The guard has to sit in the per-seed worker for esmfold2_design, not the coordinator. The coordinator runs on a different, CPU-only container and never touches the file in question, so putting the fix there would have looked correct and done nothing.
