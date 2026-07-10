---
title: BoltzGen nanobody discovery against a lymphocyte surface receptor, 2000 designs narrowed to a validated panel of 12
tool: boltzgen
tool_name: BoltzGen
target_kind: VHH nanobody steered off the receptor functional face
date: 2026-06-26
internal_benchmark: true
glyph: Antibodies (VHH)
stats: 2000=designs generated | 0.974=best discovery ipTM | 12=validated panel | 10 of 12=strong on two scorers
outcome: A single BoltzGen run took a receptor target from 2000 raw designs to a validated panel of 12 nanobodies, scored by two independent structure predictors.
---

We used BoltzGen to design VHH nanobodies against a lymphocyte surface receptor,
steering them toward a membrane distal surface so they land away from the
receptor's functional contact face and do not interfere with native signaling.

The run generated 2000 designs. The best scored ipTM 0.974, and one recurring
epitope emerged across 32 independent designs. We narrowed to a final panel of 12
and confirmed them with AlphaFold2 and Boltz-2, two structure predictors
independent of the design step; ten of the twelve held up strongly under both.
The lead nanobodies sit 11 to 18 angstroms clear of the functional face, so they
bind the receptor without blocking it.
