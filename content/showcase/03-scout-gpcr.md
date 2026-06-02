---
title: Epitope Scout on a GPCR, three predicted epitopes with structural context
tool: scout
target_kind: gpcr_extracellular_loops
top_score: 0.78
date: 2026-05-31
internal_benchmark: true
---

Internal benchmark run. Epitope Scout against an internal GPCR target.
The goal of Scout is to identify candidate epitopes before committing
GPU time on a downstream binder design tool. Scout scores each candidate
site on per dimension feasibility (surface area, conservation,
hydrophilicity, framework distance) and returns a ranked shortlist.

What the run delivered:

* Three predicted epitopes on the extracellular loops, with surface
  context.
* Top feasibility score 0.78 on the loop most exposed in the resting
  state of the receptor.
* Two of three sites with hotspot residues annotated, ready to hand
  off to BindCraft or RFantibody via the Scout to tools handoff flow.
* Predicted clash zones and framework proximity flagged on the
  remaining site so we deprioritized it before paying for design GPU
  time.

The Scout run cost was the smoke tier, billed at nominal CPU rates.
This is the recommended first step before any binder design campaign.

Replace with real customer anonymized data before launch.
