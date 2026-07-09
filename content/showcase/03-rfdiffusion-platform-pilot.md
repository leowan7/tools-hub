---
title: RFdiffusion de novo backbones, 60 designs delivered end to end on the platform
tool: rfdiffusion
tool_name: RFdiffusion
target_kind: de novo backbone pilot, executed through the platform
date: 2026-07-06
internal_benchmark: true
glyph: De novo minibinders
stats: 60=de novo backbones delivered | 5=parallel jobs | 5 of 5=jobs completed
outcome: A full de novo backbone pilot, start to finish in the browser. No cluster, no install, no subscription.
---

We used RFdiffusion to generate de novo protein backbones, running the pilot
across five parallel jobs. All 60 designs completed and were ready to download.

The run also tested how the platform handles a longer campaign. It paused itself
when the wallet balance ran low, then resumed cleanly after a top up and finished
with a reconciled ledger, so a multi job run survives a mid run balance dip
without losing work.
