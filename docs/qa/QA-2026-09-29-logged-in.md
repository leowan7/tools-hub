# QA 2026-09-29: logged-in customer pass, tools.ranomics.com (production)

Tester: Claude (Claude in Chrome, Leo's signed-in session). This is a report only: no code, no PR, no git writes.

- **Build:** `/health` reported `4d0e56a2380336d646d89ba11712e0882198a975` (#376) at the start, again after sign-in, and again mid-pass. **#378 (API key scope/expiry) is NOT deployed**, so the test key was not created (see Unverified).
- **Spend:** none. The wallet read $996.29 at the start and at the end (`/account/wallet`, read by page text). No run, scale-up, retry, top-up, shortlist, contact or scoping form was submitted.
- **Viewport:** desktop unless marked "phone". Phone means a 375x812 same-origin iframe; the Chrome window cannot go below about 500px.
- **Console:** `read_console_messages onlyErrors` found no errors on the RFdiffusion result page, `/account/wallet`, or the Boltz-2 result page. Console capture for the other pages was not taken (unverified).

## Pages and flows covered

| Area | URL(s) | Result |
|---|---|---|
| Wallet overview | /account/wallet | Covered: bugs W1, W2, PH1 |
| Top-up page (no payment) | /account/wallet/topup, `?tool=rfdiffusion&need=2.59` | Covered: bugs T1-T3 |
| Short-balance gate (#373) | /tools/rfdiffusion | Simulated only (see below) |
| Tool catalog | /tools | Covered: bugs C1, C2 |
| All 14 tool forms + /developability | /tools/<slug>, `?pilot=1` | Covered: load, suggested settings, estimate, free check, runtime text |
| Validation errors | /tools/rfdiffusion (reused structure) | Covered: bug V1 |
| "Check my settings" + summary sections (P5/P6) | rfdiffusion, and all forms via `?pilot=1` | Works |
| Jobs table | /jobs (79 jobs, 4 pages) | Covered: bugs J1-J4 |
| Succeeded result pages | rfdiffusion 25471e07, boltz2 dc88afb7, opendde 4a3e3203, iggm d1cd23a8, rfantibody ef3f5bd3, proteina 6b0c9b44, pxdesign a3429923, proteina validate fc9d14d7, mpnn 66c0baa9 | Covered: bugs R1-R8 |
| Shortlist modal | rfdiffusion 25471e07 | Opened, read, closed with Cancel (bug R9) |
| Failed result page (#371) | iggm e7bd21b1 | Covered: bugs F1-F3 |
| Retry prefill | /tools/iggm?clone_from=e7bd21b1… | Covered: bug F1 |
| API keys page (#378) | /account/api-keys | Read only; #378 not deployed |
| Admin | /admin/users | 404 for this account (not an admin) |
| Scout → binder handoff (#375) | /scout/ | Not tested (needs a Scout analysis) |
| Phone width | wallet, top-up, result page, rfdiffusion form, /jobs | Covered: bug PH1 |

## Bugs

Severity: **P0** = customers see it wrong, money is wrong, or the run breaks. **P1** = real bug. **P2** = wording or polish.

### P1

**PH1: Wallet activity table clips the money columns on a phone (phone)**
- URL: https://tools.ranomics.com/account/wallet
- Steps: open at 375px width and scroll to recent activity.
- Expected: all columns readable, or the table scrolls sideways in its own box (as the /jobs and result-page tables do).
- Actual: the table is 657px wide inside `.panel` (overflow-x: hidden, 310px wide). Column edges are When 49-226, Kind 226-331, Detail 331-424, Amount 424-532, Balance after 532-706, and the panel's right edge is at 335. **Detail, Amount and Balance after are cut off and cannot be scrolled to.** The page itself has no horizontal scroll (scrollWidth 360 < 375).
- Console: none.

**W1: Negative money shown as "$-0.9833" / "$-15.00" (desktop)**
- URL: /account/wallet, recent activity.
- Expected: "−$0.98" style (sign before the currency symbol), with consistent decimals.
- Actual: the minus sign sits after the $, and rows mix 4 and 2 decimal places. Leo asked specifically about negative money.

**R1: Two different prices for "100 more of the same" (desktop)**
- URL: /jobs/25471e07-dc76-4643-a26b-655c16a91f00 (RFdiffusion).
- The result page says "Run 100 candidates — est. $38.74". The RFdiffusion form estimate for 100 designs is **$33.68**.
- Proteina (/jobs/6b0c9b44-3d24-4361-9b82-4f4fd2c78d6f) offers "Run 100 candidates — est. $188.12", while the proteina form estimated **$60 for 1000 designs** (measured earlier this pass) and $12.59 for the suggested first run.
- Likely cause (read from code, not proven on prod): above the single-container ceiling, `shared/scale_up.py::quote` prices the "split" route with `cc.plan_chunks(...).budget_usd`, whereas the form uses `estimated_cost_for_tool`. Whichever is right, the customer sees two prices for the same thing. The proteina gap (3x in the wrong direction for 10x fewer designs) needs a look.

**F1: "Try again with these settings" opens a half-empty form with no notice (desktop)**
- URL: /jobs/e7bd21b1-8aab-485e-befd-6dfa2f135f5f → "Try again with these settings" → /tools/iggm?clone_from=e7bd21b1-…
- Expected: the same inputs as the failed run, or a clear note saying which fields must be re-entered.
- Actual: mode (cdr_design), chain A and samples 40 came back. **Antibody FASTA, epitope residues and the antigen PDB are empty** (`reuse_pdb_token` empty, file input 0). No message says so. The estimate then shows $0.00 until the fields are filled.
- Cause unverified. This is an old job (2026-07-17), so it may predate storing the PDB path and these inputs. The route copies `prior.inputs` as-is (`blueprints/tools.py::tool_form`, clone_from branch). A retry from a recent failed job is needed to rule this out.

### P2

**Wallet and top-up**
- **W2:** "AUTO RELOAD" label appears twice on /account/wallet.
- **T1:** /account/wallet/topup?tool=rfdiffusion&need=2.59 says "go back to the form … Nothing is submitted for you", but there is **no link back to the tool form**; Cancel goes to /account/wallet. The "Return to <tool>" link is only on the post-checkout page (`blueprints/wallet.py` ~683-694), and that was not reached.
- **T2:** Top-up copy says "Design count is unlimited", but every form caps it (e.g. RFdiffusion "Number of designs must be between 1 and 1000").
- **T3 (unverified claim):** "$500 = thousands of RFdiffusion backbones". The form estimates 1000 designs at $336.83, so $500 buys about 1,480. "About 40 ProteinMPNN runs" for its tier is inconsistent with MPNN's $0.03 estimate; I did not check which tier that line belongs to.
- **/wallet (logged out)** returns a 404 saying "or you're signed in with a different account" when nobody is signed in. The real page is /account/wallet.

**Catalog and forms**
- **C1:** PXDesign on /tools cites Bennett 2023 AF2 initial guess as "Ranomics in-house pipeline", and it has no "Source repo" link in the comparison table.
- **C2:** ProteinMPNN says "about 30 seconds" in one place and runtime "1 min" in another.
- **RFdiffusion "TRIAL RUN: 8 BINDERS" card:** it says "About $3.37 … (4 to 8 designs)", but the form defaults to 4. The worked example (8 designs) cost $2.69, yet the minimum estimate is $3.37 (1, 4 and 8 designs all estimate $3.3683).
- **RFdiffusion FAQ** says hotspots are "strongly recommended"; the form refuses to start without one.
- **IgGM wording contradicts itself.** The field is labelled "EPITOPE RESIDUES (REQUIRED)", the glossary says "Epitope. Optional.", and the guided first run says hotspot residues are something "this tool does not require".
- **V1:** "Check my settings" says "Settings look good" for hotspot `9999` on a 115-residue chain (reused structure). Submit does refuse it (`shared/pdb_intake.py:515` → `hotspot_range_message`), so this is not a billing risk. The free check promises more than it checks; `tool_validate`'s docstring says the structure is not inspected.
- **"Check my settings" reports one error at a time.** For example, the missing hotspot is reported first, then after fixing that, the missing PDB.
- **The estimate request puts the `_csrf` token in the query string** (GET /api/wallet/estimate). It ends up in server and proxy logs. This is a security smell, not an exploit.
- **OpenDDE's empty form estimates $14.80** (and 1000 designs estimated $14.79). The real OpenDDE run dc… 4a3e3203 used 210 GPU-s. The estimate looks like a flat ceiling; unverified.
- **ESMFold2 design** estimates $9.87 on suggested settings and $9.86 at "1000 designs". The count field I changed may not be the one the estimate reads; unverified.

**Jobs table (/jobs)**
- **J1:** The Preset column shows raw slugs (cdr_design, protein_binder, ligand_binder, standalone, general, validate). The same slugs appear on result pages ("Preset cdr_design").
- **J2:** Status "SUCCEEDED" and Progress "succeeded" repeat each other.
- **J3:** Submitted shows raw ISO time with no timezone ("2026-09-23T17:13:38"). Result pages show the same.
- **J4:** Spend has 4 decimals ("$0.3168"). The failed-job page says "The $4.3697 hold was returned".
- **Short-id URLs** (/jobs/25471e07) return 404. The list links use full ids, so this only matters for pasted links.
- **Cancelled IgGM jobs** of about 30 min were charged $0.0087. That may be correct (GPU used before the cancel); unverified.

**Result pages**
- **R2:** "← Full-size runs" and "All full-size runs" link to /campaigns on a single job reached from My jobs. This happens on every result page checked, including the failed one.
- **R3:** "Download CSV" is also `btn-primary` on every succeeded page with candidates, so there are two primary buttons beside "Run 100 candidates" / "Screen 3 more". #376 asked for the scale-up to be *the* primary. The contact link is correctly a plain text link ("Talk to the team").
- **R4:** Runtime estimates on result pages are far below reality:
  - RFdiffusion said "≈11.4 min for 8 designs"; the run took 2220 GPU-s (37 min), and the tool card says 25-40 min.
  - RFantibody said "≈7.6 min for 4 designs"; the run took 834 s (13.9 min).
- **R5:** The RFdiffusion CSV has no sequence column (rank, pdb_key, source_rank, ipTM, i_pAE, pLDDT, complex_pLDDT). Sequences are only in the FASTA.
- **R6:** Sort direction is inconsistent. The first click on pLDDT sorts worst-first; the first click on i_pAE sorts best-first.
- **R7:** Sort headers are plain `<th>` with no tabindex or aria-sort, so they can't be reached by keyboard.
- **R8:** The OpenDDE result title reads "co-folding — job 4a3e3203"; the tool name is missing.
- **R9:** "Send shortlist to Ranomics lab" is enabled with 0 starred. The modal opens saying "Nothing starred yet. Close this and star the designs…", but "Submit scoping request" is still enabled (not clicked). The required target name field would stop an empty submit; a 0-candidate scoping request is still possible.

**Failed page**
- **F2:** The /jobs list shows "$0.00 refunded" for e7bd21b1, while the page says "The $4.3697 hold was returned … not charged". Both are true (the hold was released, nothing was charged), but the list's "refunded $0.00" reads like the refund failed.
- **F3:** The cause line is generic ("The tool hit an error while running and stopped", Category: pipeline, "IgGM design.py exited with code 1"). This is correct fallback behaviour, but it gives the customer nothing to change.

## What worked

- **Short-balance gate (#373), simulated.** I stubbed the estimate response in-page (balance 2.41, estimate 3.37, hold 5.00, deficit 2.59).
  - The gate showed estimated cost, amount held to start, balance, and "you need $2.59".
  - The button read "Top up $20.00", opened in a new tab (`target=_blank`), and pointed to /account/wallet/topup?tool=rfdiffusion&need=2.59.
  - Submit was hidden and no negative figure appeared anywhere.
  - The top-up page read `need` and showed "your job needs $2.59 more".
- **Result pages (#376).**
  - Every succeeded design result offers a priced next step: RFdiffusion $38.74, IgGM $10.06, RFantibody $281.41, Proteina $188.12, PXDesign $36.18, Boltz-2 "Screen 3 more" $0.66.
  - Contact is a plain text link.
  - "Test binders in the lab" and "AI Binder Sprint" are secondary buttons.
  - Validate-preset and MPNN pages correctly have no offer.
- **Downloads.** On RFdiffusion 25471e07 all returned 200, were non-empty and had correct magic bytes: CSV 461 B, FASTA 1236 B, ZIP 184,858 B, two .pdb files at 112,995 B and 104,166 B. Other result pages list their export and PDB links (counted, not downloaded).
- **Candidate table.** Sorting works. "#" renumbers as a position, and the TOP badge follows its row.
- **Failed page (#371).** It shows the cause, a refund line and "Try again with these settings". The link goes to `clone_from` on the same tool (prefill caveat: F1).
- **"Check my settings (free)".**
  - Confirmed free in code (`blueprints/tools.py::tool_validate`: no job, no hold) and in the wallet (balance unchanged).
  - With the reused RFdiffusion structure it returned "Settings look good. Nothing was charged and no run was started."
  - The P6 summary lines update live, e.g. "Target reused structure, chain A, 3 hotspots" and "Design 8 designs, 55 to 65 residues".
- **All 14 tool forms load (200)** with Submit and the free check present. Estimates on the suggested settings:

  | Tool | Estimate | Free check result |
  |---|---|---|
  | bindcraft | $6.30 | needs hotspot |
  | boltzgen | $6.07 | needs PDB |
  | esmfold2-design | $9.87 | ok (built-in preset target) |
  | iggm | $0.08 | needs FASTA |
  | proteina | $12.59 | ok (curated target) |
  | pxdesign | $12.59 | needs hotspot |
  | rfantibody | $4.37 | needs hotspot |
  | rfdiffusion | $3.37 | needs hotspot |
  | mpnn | $0.03 | needs PDB |
  | af2 | $0.53 | needs FASTA |
  | boltz2 | $0.22 | needs binder |
  | colabfold | $0.15 | needs FASTA |
  | esmfold | $0.08 | needs FASTA |
  | opendde | $14.80 | needs entity |

  /developability loads with its scoring form.
- **Validation messages are clear.** Tested: a bad hotspot token, min > max binder length, designs 5000 and −3 ("between 1 and 1000"). The inputs carry min=1 and max=1000.
- **No garbled runtime text** on any tool page. The "~9 to 15 min to 1 to 3 min" pattern was not found; the regex matches were ordinary sentences.
- **Phone width.** No page-level sideways scroll on the wallet, top-up, result page, rfdiffusion form or /jobs. The /jobs and candidate tables scroll inside their own boxes (the candidate box is narrow at 214px, but usable). The exception is PH1.
- **Signup credit wording.** The login page reads "New accounts start with $5 of balance, usable for 30 days". The top-up page says the free signup credit "expires 30 days after signup". The wallet shows no credit line because Leo's account has no signup credit (expected).

## Runs / actions for Leo to approve (none were done)

| # | What | Why | Cost |
|---|---|---|---|
| 1 | Low-balance test account (fresh signup with the $5 credit) and open RFdiffusion's suggested run | The real server gate cannot trigger on a $996 balance; only a simulation was done | $0 (no submit needed; the gate shows before submit) |
| 2 | Click "Run 100 candidates" on 25471e07 and stop at the form it opens | Verifies #368's handoff and whether the form's price matches $38.74 (R1). My JS click was refused by the permission check as a transaction, so this is left to Leo. | $0 if not submitted; $38.74 if submitted |
| 3 | $20 top-up from /account/wallet/topup?tool=rfdiffusion&need=2.59 | Verifies the "Return to RFdiffusion" link after checkout (T1) | $20 (real payment) |
| 4 | Retry a *recent* failed job via "Try again with these settings" (open only) | Tells whether F1 is old-data only or current | $0 |
| 5 | Run one Epitope Scout analysis (free, CPU) and follow its "design binders" handoff without submitting the binder form | #375 handoff untested: I did not start any run, even a free one | $0 |
| 6 | Create + revoke one read-only 30-day key | After #378 deploys | $0 |

## Unverified

- **#378 API keys:** not deployed (build 4d0e56a2). No key was created or revoked.
  - The current page offers "Member (read & write)" / "Viewer (read only)" but no expiry field.
  - FYI, not a bug: two old **member** keys are still active, "smoke-test-2026-06-10" (last used 2026-06-10) and "leo-test-1" (never used). Consider revoking them.
- **Admin** (/admin/users paging, funnel, reminder-sends block): /admin/users returns 404 for leowan7@gmail.com. The account is not an admin, or admin is gated elsewhere. Not covered.
- **Scout → binder handoff (#375):** not tested (run 5 above).
- **Real short-balance gate server path:** only the client simulation was tested (run 1).
- **"Run 100 candidates" click-through (#368):** not tested (run 2).
- **Return-to-tool link after checkout:** not tested (run 3).
- **R1 cause:** read from code only.
- **F1 cause:** old job or current bug, unknown.
- **"$8.90 spent in the last 30 days"** on the wallet was not reconciled against transactions.
- **Console errors** were checked only on the three pages named at the top.
- **Real phone device:** only simulated with a 375px iframe.
