---
title: RFdiffusion de novo binder backbones against a target, 60 designs ready for sequence design
tool: rfdiffusion
tool_name: RFdiffusion
target_kind: de novo binder backbone against a target
date: 2026-07-06
internal_benchmark: true
glyph: De novo minibinders
stats: 60=de novo backbones delivered | 5=parallel jobs | 5 of 5=jobs completed
outcome: A single RFdiffusion run produced 60 de novo binder backbones against a target, each fold shaped by diffusion rather than grafted onto a known scaffold.
---

We used RFdiffusion to generate de novo binder backbones against a target,
building each fold from scratch through diffusion rather than grafting a binder
onto an existing scaffold.

The run produced 60 de novo backbones across five parallel jobs, each an
independently generated scaffold positioned against the target surface.
RFdiffusion returns backbone geometry, the starting fold you then take into
sequence design and an independent structure predictor for scoring. The value is
a pool of de novo binder scaffolds to carry forward rather than a single backbone.
