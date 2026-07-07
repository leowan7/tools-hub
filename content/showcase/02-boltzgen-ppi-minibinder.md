---
title: BoltzGen de novo minibinders blocking a protein interaction interface, 20,000 designs with 713 high confidence hits
tool: boltzgen
target_kind: de novo minibinder blocking a protein interaction interface
top_score: 0.76
date: 2026-06-14
internal_benchmark: true
---

Internal benchmark run on a public target. We used BoltzGen to design de novo
minibinders aimed at one partner interface on a scaffolding protein, so a binder
competes with the natural partner for the same surface.

What the run delivered:

* 20,000 de novo designs in a single campaign, every sequence unique.
* Top binding confidence 0.758 on BoltzGen native binding confidence metric. 713
  designs scored at or above 0.5 and 984 at or above 0.4.
* The top 20 by composite rank held binding confidence 0.726 to 0.758, ipTM 0.93
  to 0.98, interface PAE around 0.5 angstroms or lower, and binder length 65 to 80
  residues.
* Across the top designs the binder footprint covered up to about 93 percent of
  the annotated target epitope, so the designs sit where they need to sit to
  compete.

Read this as an honest shape for a large de novo minibinder campaign on a public
interface target, where the value is a deep ranked pool a wet lab can sample from
rather than a single hero design.
