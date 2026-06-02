---
title: BindCraft pilot against a kinase target, 47 designs, top ipTM 0.91
tool: bindcraft
target_kind: kinase
top_score: 0.91
date: 2026-05-29
internal_benchmark: true
---

Internal benchmark run. We ran BindCraft pilot against an internal kinase
target with two known hotspot residues annotated by Epitope Scout. The
pilot tier produced 47 design candidates over a single GPU session, with
the top design scoring ipTM 0.91 against the AF2 multimer scorer.

What the run delivered:

* 47 designs total, ranked by ipTM at the binder to target interface.
* Top ipTM 0.91, top pLDDT 89.4 on the binder body.
* Median binder length 92 residues. All designs in the 60 to 150 aa
  window BindCraft is calibrated for.
* Top 5 designs handed off to ProteinMPNN for sequence redesign on the
  same backbone, then to AlphaFold2 multimer for a sanity check.

What the run cost: roughly one hour of A100 GPU time, billed to the
wallet at standard tools.ranomics.com per second rates.

Read this entry as a typical BindCraft pilot shape, not a recommendation
for a specific target. Replace with real customer anonymized data before
launch.
