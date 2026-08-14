# The Fc multi-chain run — staged, verified, ready to launch

Written 2026-08-07, closing item 3 of `HANDOFF-2026-08-07-multichain-finish.md`.
Everything below is verified; nothing here has been submitted. **You click Run.**

## Launch this

| field | value |
|---|---|
| tool | **rfdiffusion** (see "why not pxdesign") |
| preset | pilot |
| target PDB | `static/example/3ave_igg1_fc_dimer.pdb` |
| target chain | `A,B` |
| hotspot residues | `A296,B264` |
| binder length | 80 (60–90 all fit) |
| num designs | 8 |

Upload the PDB as-is. Do not trim it — the glycans are chains C and D and
preflight drops them cleanly as non-target, which is part of what this run is
supposed to exercise.

## The structure

The handoff assumed a 446 aa dimer. The only Fc in the repo was
`static/example/3s7g_fc_ab.pdb`, a **130 aa fragment** (2 × 65 residues,
236–300) — not the full dimer, and not what the cap table was written against.

The real thing was already on disk at
`runs/glycoform-pilot-s2g2f/inputs/3AVE.pdb`, but `runs/` is gitignored, so it
was one `git clean` from gone. Copied to `static/example/3ave_igg1_fc_dimer.pdb`
alongside the other example structures.

**3AVE** — "Crystal structure of the fucosylated Fc fragment from human
immunoglobulin G1", deposited 2011-03-04. Single model, byte-identical to the
deposition (ANISOU records kept, which is most of the 671 KB).

| chain | residues | numbering | notes |
|---|---|---|---|
| A | 211 | 234–444 (EU) | + 1 Zn, 140 waters |
| B | 208 | 237–444 (EU) | 109 waters |
| C, D | 0 standard | — | N-glycans only (NAG/FUC/MAN/BMA) |

419 standard residues across A+B. Preflight identified it independently as
UniProt **P01857** (human IgG1 heavy chain constant region), which is the right
answer.

419 rather than 446 because a crystal structure has disordered termini. It is
the real dimer, and it is the largest true Fc available without a download.

## Verified, no GPU spent

Driven through the real gates on the staged file, re-derived against `main`
(`2151a01`) rather than any branch:

```
inspect chains                          -> A: 211 res, 234-444
                                           B: 208 res, 237-444
                                           C, D: 0 standard residues
parse_target_chains("A,B")              -> ['A', 'B']
parse_hotspot_residues("A296,B264", ..) -> ['A296', 'B264']
validate_hotspots(report, "A,B", ..)    -> in_range=['A296','B264'], out_of_range=[]

rfdiffusion  READY_WITH_FALLBACK   hotspots {'surviving': ['A296','B264'], 'dropped': []}
             419 residues kept on target; chains dropped: C, D (non-target)
             target 419 / cap 500      combined 499 / cap 600
             no internal gaps
pxdesign     READY_WITH_FALLBACK   same hotspot and cleanup result
             target 419 / cap 600      combined 499 / cap 950
```

Both clear every hard cap uncropped, exactly as the handoff predicted.

The verdict is `READY_WITH_FALLBACK`, not plain `READY`, and that is expected
rather than a problem: `preflight_for_tool` returns the fallback kind whenever
it has any softness to surface, and here the softness is the size soft-warn
(rfdiffusion over 300, pxdesign over 360) on a 419 aa target. Nothing is
cropped, no hotspot is dropped, and the submit is not gated. The distinction is
called out because the UI renders the two verdicts differently, so expect the
softer banner on the launch page, not a green one.

**This is also the first exercise of the multi-chain hotspot gates on a real
structure**, as opposed to the synthetic two-chain fixtures the suite uses.
The gates themselves are PR #120's, already on `main` and unchanged by this
work; what had never been checked was whether they hold up on a genuine
crystal dimer with four chains in the file, two of which are dropped as
non-target. They do.

The 3D picker was then driven in a real browser against this exact PDB — real
NGL parse, real atom picking, the production opts block:

```
chains NGL parsed : A, B, C, D
chainSel          : (:A or :B)          <- valid disjunction, not the old ":A,B"
clicked           : CA on chain A, CA on chain B
ignored clicks    : none                <- the chain gate accepted both
hotspot field     : "A234,B237"         <- the chain-B pick recorded as B, not A
hotspotSel        : ((234 and :A) or (237 and :B))
```

That is the one claim the node harness cannot make, because it stubs out the
NGL load path: `atom.chainname` really returns `"B"`, and the picker really
attributes the click to the right protomer.

## Why not pxdesign

Same target, same 8 designs, from `runtime_estimate_min`:

| tool | GPU | estimate |
|---|---|---|
| rfdiffusion | A100-40GB | **~0.9 h** |
| pxdesign | A100-80GB | **~25.4 h** |

A 419 aa target puts pxdesign an order of magnitude out. The estimator is
explicitly rough, but not by 25×. rfdiffusion is the right first multi-chain
run; pxdesign is worth a separate decision about cost, not a default.

## Reading the result

The results page will carry the amber **"ipTM is not reliable for this
multi-chain target"** banner. That is correct and expected — it is item 4,
landed in this pass. ipTM here is computed across the interfaces of the whole
complex, including the target's own chain–chain contact, and on a dimer that
internal interface is large and well formed whatever the binder does — so it
holds up a number that is both the displayed value and the ranking key. Judge
these designs on pLDDT, i_pAE and the interface geometry, or re-fold the top
few with Boltz-2 from the buttons under the table.

That last sentence is advice this document can give and the BANNER cannot. The
banner renders on six pages with no parameter telling it which one it is on,
and the pooled target page in multi-cohort mode has neither those columns nor
a re-fold control — so its copy names the remedy ("do not choose on ipTM
alone; confirm with a second-opinion fold") rather than the furniture. This
handoff is about one campaign page, where the columns and the buttons are
really there.

(An earlier version of this section said ipTM is "a maximum over residues" and
quoted ~0.9 for a crystal dimer. Four pipeline files in this repo describe it
as interface-pTM *averaged over every chain pair* instead —
`tools/af2/run_pipeline.py:202` and three siblings. The conclusion holds under
either reduction; the figure does not.)

BoltzGen is the one tool where this was fixable at the source, and that fix
has since MERGED AND DEPLOYED (`leowan7/llm-proteinDesigner#18`,
`design_to_target_iptm` first in `IPTM_KEYS`), so boltzgen no longer carries
this banner — a boltzgen run started now reports the binder-to-target
interface. Its ipTM column tooltip carries the caveat that runs from *before*
the deploy stored the complex-wide value — **both halves of it**: the value and
the ORDER, because boltzgen ranks on ipTM and the pooled reads sort then
truncate at 300, so on a pre-deploy multi-chain run the order of the table is
as indicative as the numbers in it. rfdiffusion and pxdesign need a
per-pair value derived from the chain layout, which nobody has built, so for
this run the banner is the remedy.

## What this run is actually testing

Everything to date has been unit-tested or driven directly at Modal. This is
the first pass through `Flask route → modal_client → results rendering` with a
multi-chain payload, so watch for:

1. `hotspot_residues` arriving at the container as `["A296","B264"]` and not
   flattened to ints or re-prefixed to `AA296`;
2. both protomers present in the returned complex (the 2026-08-05 4ZQK run
   returned `{A:115, B:106, C:56}` — here expect roughly `{A:211, B:208, C:80}`);
3. the binder being a genuinely new chain and not a redesigned protomer;
4. designs gripping both chains, which is the whole premise — the 697 Å² / 706 Å²
   symmetry figure in `MULTI-CHAIN-TARGETS.md` came from **single-chain** runs
   and has never been reproduced multi-chain.

If (4) holds, that doc's motivating claim finally has a multi-chain result
behind it.
