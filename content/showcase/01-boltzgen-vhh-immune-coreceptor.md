---
title: BoltzGen nanobody discovery against a lymphocyte surface receptor, 2000 designs narrowed to a validated panel of 12
tool: boltzgen
tool_name: BoltzGen
target_kind: VHH nanobody steered off the receptor functional face
top_score: 0.81
date: 2026-06-26
internal_benchmark: true
glyph: Antibodies (VHH)
stats: 2000=designs generated | 0.974=best discovery ipTM | 12=validated panel | 10 of 12=strong on two scorers
outcome: One public target went from 2000 raw designs to a validated panel of 12 nanobodies, scored by two independent structure predictors. The same BoltzGen pipeline runs on your target.
---

We used BoltzGen to design VHH
nanobodies against a lymphocyte surface receptor, steering the binders toward a
membrane distal surface so they land away from the receptor functional contact
face and do not interfere with native signaling.

What the discovery run delivered:

* 2000 nanobody designs generated in a single unguided discovery run.
* Best interface ipTM 0.974. 1149 of the 2000 designs, about 57 percent, cleared
  ipTM 0.75.
* One recurring epitope emerged across 32 independent designs, median ipTM 0.909
  and median interface PAE 1.5 angstroms, all landing off the receptor functional
  face.

How we validated it, using tools that also run on the platform:

* We refolded the 32 designs in AlphaFold2 multimer as the full three chain
  complex, independent of the scorer used at design time. Five designs were fully
  confirmed at AF2 ipTM 0.71 to 0.81, and a physical superposition test placed the
  nanobody 11 to 18 angstroms clear of the functional face.
* ProteinMPNN diversified the validated backbones into 48 variants. 32 of 44
  passing designs agreed on pose and 19 of 44 were strictly AF2 confirmed, roughly
  three times the raw discovery rate.
* The final panel of 12 constructs was cofolded a second time on Boltz-2. Ten of
  the 12 were strong under both AlphaFold2 and Boltz-2 at ipTM 0.70, and 11 of 12
  reached consensus at 0.60.

Read this as an honest shape for a BoltzGen nanobody campaign on a tractable
public target, from a large unguided pool down to a small panel that survives
independent structure prediction.
