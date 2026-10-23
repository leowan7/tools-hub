# tools-hub — handoff 2026-08-10 (proteina tier-2 Fc binder campaign)

Ran the second production campaign of the proteina binder sweep against the
CU43-cut IgG1 Fc: 2,944 designs for $117.46, returning 321 passing binders
against tier 1's 93. Before spending it, measured which Fc surfaces CU43
actually uncovers and established there is no second epitope worth targeting,
so the budget went to depth at the known site rather than breadth. The design
pool now stands at 414 passing binders across both tiers, and the open question
is whether to spend more compute or move to selection.

## Done this session

- Ran tier 2 end to end: 46 shards, 2,944 designs, 10 concurrent A100s, 45.41
  GPU-hours, $117.46. No failed or empty shards, budget never tripped.
- Got 321 passing designs, up from 93, at the same quality (ipTM median 0.836
  vs 0.833) and a lower unit cost ($0.37 per pass vs $0.53).
- Proved the tier-1 yield model transfers: predicted 10.2% and 10.6% per bin,
  measured 10.7% and 11.1%.
- Established there is no second epitope. CU43 uncovers one patch of 588 square
  angstroms across 8 residues per protomer, and the existing hotspots already
  captured 93% of it.
- Found and added the one missing residue, K334, and confirmed after the run
  that it earned its place (contacted by 68.5% and 90.3% of passing designs).
- Settled the single-chain question for good: binders must target both
  protomers, and 414 of 414 passing designs across both tiers do.
- Exported everything to sequences, scores, provenance and checksums.
- Committed the tier-2 constants to their own branch and unpinned 23 driver
  tests that were wrongly coupled to campaign configuration.

## Next steps

- Decide what happens to the 414 passing designs: order a subset for wet-lab
  testing, or run a tier 3. There is no scientific case for more length
  exploration, so a tier 3 would only add depth at the same site.
- Push `feat/proteina-tier2-campaign-constants` (commit `13ac02e`) if you want
  the campaign config preserved on the remote.
- Replace the driver's cost model. `shard_seconds()` is still the original
  guess and runs 53% high, so dry-run quotes are inflated and the budget guard
  is awkward to size. There are now 66 shards of real timing to fit it to.
- Investigate suppressing the self-copy mode. It consumed about $33 of the
  $117 this run and roughly a quarter of every run before it.

## Blocked / waiting on

- Needs your decision on tier 3 versus moving to selection and wet-lab. Nothing
  further should be spent on GPU until that is settled.
- Push of the constants branch is awaiting your go. It is deliberately not a
  PR, because merging any proteina PR rebuilds the production Modal image and
  this is campaign config rather than a production fix.

## Notes

- **Do not use distance to the glycan as a test for whether a site is
  covered.** Being 5 angstroms beside a sugar is not the same as being hidden
  behind it. That mistake produced a confident recommendation for a second
  epitope which the correct measurement, buried surface area between the two
  glycan states, then disproved: the proposed site gains 3 square angstroms on
  cutting, against 557 for the real one.
- **Hotspots inherited from the BoltzGen aglyco work need re-checking before
  reuse here.** That campaign compares bare Fc against full glycan; CU43 does
  not produce bare Fc, it leaves a stub behind. Residue 265 is the clearest
  case: it gains 71 square angstroms on going fully bare, but 65 of those are
  still hidden by the stub, leaving 6 of real discrimination.
- Two properties of these binders follow from their binding across both halves
  of the Fc, and neither was a design choice: both glycans have to be cut
  before one will bind, which makes them an all-or-nothing readout on complete
  deglycosylation; and there is only one binding site per antibody, so no
  avidity is available from a two-armed format.
- **Changing the length bins breaks 23 driver tests that have nothing to do
  with bins.** They inherited the bin list from the module while overriding
  only the shard count, so a normal campaign change looks like a regression.
  Fixed this session by pinning a test fixture, but worth knowing if you see it
  again elsewhere.
- **Size the budget against the guard's transient peak, not the true spend.**
  Shards still running are priced at the inflated model rate until they finish,
  so at width 10 the guard can see about $141 on a run that costs $126.
- PR #132 merged while the campaign was mid-flight and rebuilt the production
  image. Harmless here because it only added files that the container never
  imports, but a mid-campaign merge touching the design path would split a
  campaign across two images without saying so.
