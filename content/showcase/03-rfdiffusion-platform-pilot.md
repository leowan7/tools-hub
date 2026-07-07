---
title: RFdiffusion backbone pilot run end to end on tools.ranomics.com, 60 designs at real metered cost
tool: rfdiffusion
target_kind: de novo backbone pilot, executed through the platform
date: 2026-07-06
internal_benchmark: true
---

Internal benchmark run executed entirely through tools.ranomics.com, from
submission to download, as a validation of the metered compute path.

What the run delivered:

* 60 RFdiffusion backbone designs, split across five parallel jobs, all completed
  and downloadable.
* Total billed cost $11.58, about $0.19 per design, under the pre run estimate.
* The run paused itself automatically when the wallet balance ran low, then
  resumed cleanly after a top up, with a reconciled ledger at the end.

Read this as a cost and delivery reference for running RFdiffusion on the
platform. You pay by the second of compute, a small pilot lands for a few dollars,
and the run is safe to pause and resume.
