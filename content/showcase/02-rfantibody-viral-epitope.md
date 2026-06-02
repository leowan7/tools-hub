---
title: RFantibody scaffold search against a viral epitope
tool: rfantibody
target_kind: viral_glycoprotein_epitope
top_score: 0.82
date: 2026-05-30
internal_benchmark: true
---

Internal benchmark run. RFantibody pilot against a viral glycoprotein
epitope, with the hotspot residues taken from a published structure of
the antigen complex. Goal: generate VHH (nanobody) scaffolds that frame
the antigen footprint without prior antibody training data on this
target.

What the run delivered:

* 32 nanobody scaffolds with full CDR shaping over the hotspot patch.
* Top ipTM 0.82 at the VHH to antigen interface.
* Top pLDDT 86.1 on the scaffold body, lower on CDR3 as expected.
* Scaffolds threaded through ProteinMPNN for sequence design at
  sampling temperature 0.1, then run through AlphaFold2 multimer for
  fold validation.

Anchors for reading the result. ipTM above 0.7 on a VHH against a
shaped antigen is a tractable design. pLDDT above 80 on the scaffold
body indicates the model is confident in the framework fold. CDR3 pLDDT
is structurally noisier and a lower number there is normal.

Replace with real customer anonymized data before launch.
