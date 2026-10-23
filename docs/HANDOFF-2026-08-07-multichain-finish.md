# Handoff — finishing multi-chain for PXDesign + RFdiffusion

Written 2026-08-07. Spans **two repos**: `tools-hub` (UI, gates) and
`llm-proteinDesigner` (containers, docs).

## Where things stand

The backend is done. As of `tools-hub` `80389d1` (PR #120):

- adapters accept `target_chain: "A,B"` and `hotspot_residues: ["A296","B264"]`
- the shared validators understand the chain-prefixed form (that was PR #120 —
  before it, rfdiffusion/pxdesign hard-blocked every multi-chain submit with a
  false "backbone is incomplete" message, and boltzgen returned READY with
  every hotspot silently discarded)
- `multi_chain_container_ready=True` for rfdiffusion, pxdesign, boltzgen,
  proteina; `False` for bindcraft (no smoke tier) and rfantibody (model limit)
- containers GPU-verified 2026-08-05 on **4ZQK** (`{A:115, B:106}`)

**What has never happened: a multi-chain run through the web UI, and any
multi-chain run against the Fc dimer that motivated the work.** The Fc numbers
in `llm-proteinDesigner/docs/MULTI-CHAIN-TARGETS.md` (1,875 designs, 697 Å² on
A / 706 Å² on B, 85% putting ≥30% of interface on the second protomer) are an
analysis of prior **single-chain** runs, not a multi-chain result.

## The four items

### 1. Three UI fixes — `tools-hub`, no GPU

Without these the feature cannot be driven from a browser at all.

**1a. The 3D hotspot picker is bare-int-only.**
`chainPrefixed: true` is set on `templates/tools/proteina_form.html:316` and
nowhere else. Add it to the picker opts block in:

- `templates/tools/rfdiffusion_form.html` (~line 338)
- `templates/tools/pxdesign_form.html` (~line 312)
- `templates/tools/bindcraft_form.html`
- `templates/tools/boltzgen_form.html`

`static/js/hotspot_picker.js` already implements the prefixed path in full —
`_chains()`, `_chainSel()`, `_hotspotSel()` and `_toggleResidue()` all branch
on `this.chainPrefixed` (lines 169, 265, 318, 349). Today, with it unset:

- on a multi-chain target the picker is **inert** (`_chains()` returns the
  literal `["A,B"]`, so the NGL selection `:A,B` matches nothing and every
  click is ignored)
- on a single-chain target, clicking a residue **overwrites** any prefixed
  hotspots the user typed — silent data loss

Verify by driving the picker with the real JS under node with a stub document,
or in the browser preview; do not just read the diff.

**1b. `maxlength="4"` caps the target at two chains.**
Present on `target_chain` in all four binder forms (e.g.
`pxdesign_form.html:211`). `"A,B"` is 3 chars and fits; `"A,B,C"` is 5 and
**physically cannot be typed**. The backend supports N chains and
`tests/test_multichain_targets.py::test_three_chain_target_accepted` proves it.
proteina uses `_MAX_CHAIN_FIELD = 32`; match that. Keep the per-token ≤4-char
rule, which is enforced in `validate()`, not by the input.

**1c. The form copy points the wrong way.**
`rfdiffusion_form.html:224-228` reads "Comma-separated residue indices from the
target chain" — singular — with example `54,56,115`. Nothing tells the user the
`A296,B264` form exists. Same on the other three. Mention both forms and that a
bare integer is read as the **first** target chain.

### 2. BoltzGen ipTM key order — `llm-proteinDesigner`, no GPU

`docker/boltzgen/run_pipeline.py:1323`:

```python
IPTM_KEYS = [
    "iptm", "ipTM", "iPTM", "design_iptm", "protein_iptm",
```

`design_iptm` — the real binder→target interface, already present in
`aggregate_metrics_analyze.csv` — is **fourth**, so complex-wide `iptm` wins.
Move `design_iptm` first.

`run_pipeline.py` is mounted via `add_local_file`, so this is a redeploy, not
an image rebuild. **Do not touch the Dockerfile** — editing any line busts the
Modal cache by content hash, and the BoltzGen image is one of the two that has
historically not come back up after a rebuild.

### 3. The Fc run through the UI

First real multi-chain job on the driving use case, and the first exercise of
the `Flask route → modal_client → results rendering` path with a multi-chain
payload. Everything to date is unit-tested or driven directly at Modal.

The full dimer fits under both tools' caps uncropped — no trimming needed:

| tool | target cap | combined cap | Fc (446 aa) + 80 aa binder |
|---|---|---|---|
| rfdiffusion | 500 | 600 | 446 / 526 — fits, soft-warns over 300 |
| pxdesign | 600 | 950 | 446 / 526 — fits comfortably |

Do this **after** items 1 and 2, so the picker works and at least one tool
ranks honestly.

### 4. Say that multi-chain ipTM is unreliable, in the results UI

Not a blocker, but it decides whether the run's output can be trusted.

`MULTI-CHAIN-TARGETS.md:123-133` states it precisely: ipTM is a **max over
residues**, and a real crystal dimer's target-target interface scores ~0.9, so
it "dominates almost independently of binder quality". It is both the ranking
key **and** the `IPTM_THRESHOLD` gate — a mediocre binder can rank first
carrying a plausible-looking number.

Item 2 fixes this for boltzgen. rfdiffusion and pxdesign need a per-pair value
derived from the chain layout, which is a larger piece of work. Until then the
results UI should mark multi-chain ipTM as not comparable rather than letting
the number stand.

## Also: a stale claim worth two minutes

`llm-proteinDesigner/docs/MULTI-CHAIN-TARGETS.md:136` says:

> **BoltzGen and RFantibody are NOT multi-chain.**

BoltzGen's tools-hub gate says `multi_chain_container_ready=True`, with a GPU
verification note from the **same day** (4ZQK, `{A:115, B:106, C:55}`, 430 s).
BoltzGen multi-chain landed after the doc was written; the doc was never
updated. Correct the line — that file's stated purpose is to be the one place
real capability is recorded, and it is currently wrong about one of five tools.

## Traps carried forward

- **Backward compatibility is mandatory.** `target_chain: "A"` with bare
  integer hotspots must stay byte-identical. PR #120 verified this
  differentially (~2352 cases through `preflight_for_tool` comparing the full
  verdict including `hotspot_status` element *types*, plus 532 through the live
  route). Do not regress it.
- **The preflight panel is not the gate.** `preflight.js` does
  `setSubmitEnabled(!!v.ok)`, so a red panel disables the Run button with no
  re-enable path. A false-red strands a submit the server would accept; a
  false-green only costs a click. The panel parse in
  `blueprints/tools.py` is deliberately tolerant and returns no verdict of its
  own — see the comment block there before making it stricter. Three attempts
  broke it in three different ways.
- **Do not touch `tools/proteina/`** without checking whether a concurrent
  session owns it.
- **Write tests that can fail.** Five QC rounds on PR #120 found repeatedly
  that new tests could not fail: assertions on unreachable strings, fixtures
  whose numbering made a chain-blind bug indistinguishable from correct
  behaviour, and "stays an int" tests that passed ints in. Mutate the
  implementation and confirm the test goes red before trusting it.

## Still open, not in scope here

- `blueprints/jobs.py:405` — `ValueError` on a chain-prefixed hotspot when
  refolding into Boltz-2. Pre-existing. Needs the author-numbering conversion
  alongside it: Boltz-2 reads hotspots as 1-indexed **sequence positions**
  while the design tools use **PDB author numbering**, and
  `shared/pdb_preflight.py:210-219` already documents the difference while the
  refold path bypasses the preflight that encodes it.
- Round 5 QC on PR #120 produced 29 findings whose verification phase died on a
  session limit. Four are worth reproducing: campaign / target-launch routes
  possibly never calling `preflight_for_tool` (which would bypass the
  multi-chain container gate on the money paths), proteina stripping the chain
  prefix before route-level range checks, cloning a multi-chain proteina run
  moving a chain-B hotspot onto chain A, and design targets being unable to
  store a prefixed hotspot at all.
