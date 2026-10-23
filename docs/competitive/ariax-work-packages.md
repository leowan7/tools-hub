# Work packages from the ariax teardown (scoped 2026-09-24)

Nine packages. Sizes are my estimate, not measured. "Conflict" is which other package
would fight it in the same files — this drives the order, not the value.

## P1 — Persistent left sidebar  [APPROVED by Leo]
The signed-in app gets a permanent left rail, grouped the way the catalog already groups
tools by intent, plus a Manage group (jobs, wallet, account, API keys). Collapsible,
remembers its state, becomes a drawer on mobile. Top bar keeps balance and add-funds.
- Size: large. Conflict: `base.html`, `_header.html`, global CSS — fights nearly every
  other template package. **Must land first and mostly alone.**

## P2 — Jobs table earns its keep
Add runtime, spend so far, and progress to the jobs list. Keep the empty-state ladder and
the compare checkboxes, which already beat theirs. Retire the inline styles on that table.
- Size: medium. Conflict: P1 (same layout family). Data already exists on the job record.

## P3 — Show the input remap  [highest value]
Wherever we renumber residues, reassign chains, synthesise SEQRES or drop records, say so
in plain language on the form and on the job detail page, naming before and after.
- Size: medium. Conflict: low — mostly `shared/pdb_preflight*` plus one component.
- Why first among the small ones: this is our worst defect class, not a cosmetic gap.

## P4 — Free standalone target-prep page
Search RCSB by keyword as well as ID, pick chains and residues, click hotspots in 3D,
download the trimmed file, hand the prepared state into any tool's form. No spend required.
- Size: large. Conflict: P3 (shares the prep plumbing) — sequence after it, same worker.

## P5 — Split Validate from Run
A free "check my settings" action distinct from the paid submit, on every tool form.
- Size: medium. Conflict: P6 (same forms).

## P6 — Collapsed sections that summarise themselves
Long setup forms collapse, and each collapsed section states its current state in one line.
- Size: medium. Conflict: P5.

## P7 — Publish the rate table
Per-GPU-hour table with parity rows shown, plus the small runtime calculator.
- Size: small. Conflict: none. Good filler.

## P8 — Scoped API keys
View-only versus full access, with an expiry.
- Size: small-medium. Conflict: none.

## P9 — Team shared balance
Shared credit pool, invite and join flow.
- Size: large, and it is money. Not before the rest.

## Not taking
Four zero-value stat cards on an empty dashboard. Our empty state is better.

## Recommended order
1. P1 alone (conflict magnet, and Leo approved it).
2. P3 in parallel — different files entirely.
3. Then P2 and P7 once P1 has landed.
4. Then P4 (after P3), then P5+P6 as one worker, then P8, then P9.
