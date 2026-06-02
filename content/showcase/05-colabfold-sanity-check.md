---
title: ColabFold sanity check on the top 5 BindCraft designs
tool: colabfold
target_kind: bindcraft_redesigns
top_score: 0.89
date: 2026-05-31
internal_benchmark: true
---

Internal benchmark run. We took the top 5 designs from the BindCraft
kinase showcase entry above and ran each through ColabFold for a no MSA
sanity check fold. ColabFold completes in 1 to 2 minutes per run with
no MMseqs2 round trip on the local machine, which makes it the cheapest
spot check before ordering DNA.

What the run delivered:

* 5 designs folded through ColabFold in single sequence mode.
* All 5 produced foldings consistent with their AF2 multimer predicted
  interface, with a tightest pLDDT of 89.4 on the top design.
* Mean per design wall time 88 seconds on the dedicated GPU.
* One design flagged a CDR loop drift between AF2 multimer and the
  ColabFold single sequence fold. We deprioritized that design before
  ordering.

When to use ColabFold in the pipeline. As the cheapest fold sanity
check between an AF2 multimer scored design and ordering DNA. If you
need full MSA plus templates use AF2 standalone. If you only have one
sequence and want the fastest possible monomer fold use ESMFold.

Replace with real customer anonymized data before launch.
