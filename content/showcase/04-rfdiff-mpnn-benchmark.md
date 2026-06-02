---
title: RFdiffusion plus MPNN against an internal benchmark, top 10 sequences
tool: rfdiffusion
target_kind: internal_benchmark_set
top_score: 0.88
date: 2026-05-31
internal_benchmark: true
---

Internal benchmark run. RFdiffusion pilot against an internal benchmark
target, followed by ProteinMPNN sequence design on the top backbone
candidates, then AlphaFold2 multimer scoring on the redesigned
sequences. This is the canonical RFdiffusion plus MPNN plus AF2 loop.

What the run delivered:

* 80 RFdiffusion backbones at the pilot tier.
* Top 10 backbones threaded through ProteinMPNN at sampling
  temperature 0.1 for 20 candidate sequences each.
* All 200 redesigned sequences run through AlphaFold2 multimer for
  ipTM and pLDDT scoring.
* Top combined design scored ipTM 0.88, with a clean predicted
  interface and pLDDT 87.3 on the binder body.

What this pattern is good for. Use RFdiffusion for general de novo
binder design when you do not yet have a strong scaffold prior and you
want AF2 multimer as the final scorer. For nanobody scaffolds use
RFantibody. For mixed mini protein and antibody scaffolds against the
same target use BoltzGen.

Replace with real customer anonymized data before launch.
