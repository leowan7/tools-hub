# Manual QA, logged-out visitor — 2026-09-29

- **Target:** https://tools.ranomics.com (production)
- **Browser:** the built-in browser pane (Claude_Browser), not Leo's Chrome.
- **Build:** `/health` returned `{"build":"4d0e56a2380336d646d89ba11712e0882198a975","status":"ok"}`, which is #376.
  - Repo `main` is at `ec676463` (#378), so **#377 and #378 are not in this build**.
- **Rules kept:**
  - No account was created, no one was signed in, no credentials were entered and nothing was paid.
  - The contact and scoping forms were never submitted; the contact form was only checked with `checkValidity()`.
  - No code changes and no git writes. This file is untracked.

## How it was checked

- **Crawl:** in-page `fetch` plus `DOMParser`, following links outward from `/`, the header, the footer, `/tools` and every tool page. Logout, `/api/*` and download links were skipped. Every page was fetched once and its status recorded.
- **Layout:**
  - Hidden iframes at 375 px and 1280 px, comparing `scrollWidth` to `clientWidth` and listing elements past the right edge. Broken images were checked as `complete && !naturalWidth`.
  - Hits were then re-checked with `resize_window` in phone emulation (375x812, Android UA) and in a 1280x1000 emulated viewport.
- **Console:** `read_console_messages` was read after each page load in a fresh tab. The check itself was verified: an injected `console.error` and a fetch of a missing page were both captured.
- **Copy:** text scans of every fetched page for `pilot`, `campaign`, `$…/hour`, prices, credits and runtimes.
- **Forms:** read the fields, estimates and buttons on every tool form. Fired the estimate with default, trial (`?pilot=1`) and edited values. POSTed the tool `/submit` route without and with CSRF. Ran the free tools (Scout, Developability, Library Planner) end to end.
- **Screenshots:** in this pane, screenshots taken after a scroll come back blank above the fold. Layout findings therefore rest on measured geometry, plus one unscrolled screenshot each.

## Pages checked

All pages returned 200 with no 404 or 500, from the crawl status list and the sitemap sweep. "Phone" means measured in phone emulation at 375 px; every page listed was also measured at 1280 px.

| Page | Checks | Result |
|---|---|---|
| `/` | links, copy, phone screenshot, phone layout | OK; wording P2s below. At phone width the header wraps to three rows, which is acceptable. |
| `/tools` (chooser #350) | every chooser answer combination, copy, phone layout | 1 P1, 3 P2 |
| `/pricing` | copy vs $5 / $20 / monthly cap, phone layout | numbers consistent; 2 P2 |
| `/tools/{14 slugs}` | form, estimate default and trial, cost card, example, refusal, console | refusal OK on all 14; cost-copy P1s |
| `/tools/{slug}?pilot=1` (14) | trial preset and estimate | see cost table |
| `/help`, `/help/getting-started`, `/help/faq`, `/help/troubleshooting` | status, layout, links | OK |
| `/help/tools/{14 slugs}` | status, layout, links | OK. The proteina overflow seen in the iframe sweep does NOT reproduce at a true 375 px viewport, so it was a false positive from scrollbar width. |
| `/scout/` | full free run on 4ZQK, CSV downloads, handoff links, phone layout | run works; 1 P1 in the CSV |
| `/developability` | valid VHH run, too-short input, bad-character input | OK |
| `/library-planner` | default run | works; orphaned (P2), 1 P2 in the text |
| `/prep` | renders, logged-out state | "Sign in to run this" only; no form. OK. |
| `/showcase` | links, console, phone layout | OK; links to boltzgen and rfdiffusion tool and guide pages |
| `/login`, `/signup`, `/forgot-password` | render, layout (no credentials entered) | `/signup` has 1 P1 layout bug |
| `/terms`, `/privacy` | status, layout | OK |
| `/campaigns/new`, `/jobs`, `/account/wallet/topup` | logged-out access | redirect to `/login` — OK |
| `/sitemap.xml` | every `<loc>` fetched | 42 URLs, all 200, all on tools.ranomics.com |
| `/robots.txt` | content | 200; blocks account, jobs, campaigns, admin, auth and api; points to the sitemap |
| ranomics.com footer targets | status and title for `/`, `/binder-pilot`, `/ai-binder-sprint`, `/yeast-display`, `/biotechnology-services`, `/resource-hub`, `/technology`, `/applications`, `/ranomics-contact` | all 200 |
| ranomics.com `/ranomics-contact` form | renders, validation only (NOT submitted) | name, email and message required; a bad email is flagged — OK |

## Bugs

### P0

None found. No logged-out action started a job or reached a payment page. Evidence is under "Anonymous refusal" in the Worked section.

### P1-1 — Signup: the required "I agree to the Terms…" text is squeezed and cut off (desktop AND phone)

- **URL:** https://tools.ranomics.com/signup (also the Create-account tab on `/login`)
- **Steps:** open `/signup` and look under "What are you working on?".
- **Expected:** a small checkbox with the line "I agree to the Terms of Service and Privacy Policy." beside it, readable.
- **Actual:**
  - The checkbox is stretched to the full field width: 358 px at desktop, 238 px at phone.
  - The consent text is left a 66 px column, one word per line and 174 px tall, rendered in the green uppercase field-label style.
  - Most of it falls outside the card and is clipped by `.login-card { overflow: hidden }`.
- **Measured positions (label-children geometry in the iframe):**

  | Width | Card | Text |
  |---|---|---|
  | 1280 | 403–863 | 822–888 |
  | 375 | 30–330 | 309–375 |

  Seen on a 1280 px screenshot: "I AGREE / TO THE / TERMS" at the card edge.
- **Why it matters:** the checkbox is `required` (`terms_accepted:checkbox*`), so every new user must tick consent text they cannot read.
- **Likely cause:**
  - `static/style.css` `.field input { width: 100% }` also matches the checkbox; the inline `flex-shrink:0` in `templates/login.html` (the `signup-terms` input) then keeps it wide.
  - `.field label` supplies the uppercase green style.
- **Same place:** the "What are you working on?" textarea is unstyled, with a white background and monospace font (`getComputedStyle`: `rgb(255,255,255)`, `monospace`). No `.field textarea` rule was found in `static/style.css` by grep.
- **Console:** none.

### P1-2 — Epitope Scout: the two CSV downloads use different meanings for `epitope_id`

- **URL:** https://tools.ranomics.com/scout/
- **Steps:**
  1. Enter `4ZQK` and click Fetch.
  2. Keep chain A and click Analyze epitopes.
  3. Download "Top 3 CSV" and "All patches CSV".
- **Actual:**
  - In the Top-3 CSV, `epitope_id` is the rank:
    - `1: LYS89,ASP90,SER93,GLU72,LEU94`
    - `2: ALA18…`
    - `3: VAL29…`
  - In the All-patches CSV, the same column is the internal patch id:
    - `8: LYS89…`
    - `1: ALA18…`
    - `5: LEU50…`
    - `6: VAL29…`
  - So `epitope_id=1` names two different patches in two files from the same run. The page's "Sign in to assess" links use the internal ids (`epitope_id=8`, `1`, `6` for ranks 1, 2 and 3).
- **Also:** the All-patches CSV row order puts `LEU50…` (id 5) third, but the page and the Top-3 CSV show `VAL29…` third. Either the full file is not in rank order or a patch is filtered from the top 3 without saying so. Which one is **unverified**.
- **Expected:** one identifier meaning in both files, for example a `rank` column plus a stable `epitope_id`.
- **Console:** none.

### P1-3 — `/tools` chooser never shows ESMFold2 design when the visitor has a structure

- **URL:** https://tools.ranomics.com/tools?have=target-structure&shape=scfv (and `shape=mini-protein`, `shape=unsure`)
- **Actual:**
  - "Structure + scFv" returns only BoltzGen.
  - ESMFold2 design, whose card says it does scFv CDR design that "no other tool here does", is absent.
  - It is also absent from "structure + unsure", the show-me-everything answer.
- **Expected:** ESMFold2 design listed for scFv, or the card says it needs a sequence rather than a structure.
- **Other chooser gaps:**
  - `have=binder-sequence` returns only Developability Scout.
  - An empty or bogus `have=` shows nothing, with no message.

### P1-4 — Cost copy on the same tool page disagrees with itself

On a tool page, the default form, the "Load these settings" trial, the "What it costs" card and the worked example often show different prices for what reads as the same thing. Values were read from the live estimate endpoint and the page text.

| Tool | Default form | Trial (`?pilot=1`) | "What it costs" card | Worked example |
|---|---|---|---|---|
| rfdiffusion | 4 designs, $3.37 | 8 designs, $3.37 | $3.37 "8 binders" | $2.69 |
| bindcraft | 4, $12.59 | 2, $6.30 | $6.30; runtime text says "4 trajectories" | $2.04 |
| pxdesign | 8, $25.17 | 4, $12.59 | $12.59 "from these exact settings" (page's form shows $25.17) | $2.41 |
| rfantibody | 4, **$8.74** | 2, $4.37 | $4.37 | 14 min (runtime says 15 to 60) |
| mpnn | 50 seqs, $0.14 | 8 seqs | $0.03 | — |
| proteina | $12.59 | $12.59 | $12.59, "~9 to 15 min" | 57 min, $6.02 |
| esmfold2-design | cd45, $9.87 | same | $9.87 | PD-L1, $1.26 |
| boltzgen | 4, $6.07 | same | $6.07 | 82 min (runtime says 15 to 60), $6.00 |
| opendde | $14.80 | $14.80 | none | — |
| af2 / esmfold / boltz2 | $0.53 / $0.08 / $0.22 | same (boltz2 trial switches to msa_server, but the estimate stays $0.22) | none | — |

**Why it matters:**
- A visitor holding the $5 signup credit sees a first-run price above $5 on rfdiffusion-trial-adjacent pages: bindcraft default, pxdesign both, rfantibody default, proteina, esmfold2-design, boltzgen and opendde. Next to them sit cheap worked examples.
- The #373 top-up note is known; the in-page disagreement is the new part.

**Estimate robustness (rfdiffusion):**
- Invalid design counts (`0`, `-5`, empty, `abc`) still display $3.37.
- `100000` displays $500.00 against a max of 1000; the native validation message does fire.
- 4 and 8 designs both give $3.37, 20 gives $6.74 and 100 gives $33.69. The price moves in steps, so the design count seems to change nothing until a threshold.
- Before JS runs, the static HTML shows $0.00.

**Other:**
- "Load these settings" on rfdiffusion does not fill hotspots 54, 56 and 115; they appear only as placeholder text.
- Runtime claims vs examples: bindcraft example 20 min vs stated 30–45; proteina `/tools` says "~1 to 15 min" vs a 57-min example.

### P1-5 (partly unverified) — Session-expired submit sends the user to a 405 after sign-in

- **URL:** POST https://tools.ranomics.com/tools/rfdiffusion/submit without a session.
- **Actual:** it redirects to `/login?next=/tools/rfdiffusion/submit`. That next target is POST-only; a GET of it returns **405**.
- **Expected:** `next=/tools/rfdiffusion`.
- **Unverified:** what happens after an actual sign-in. I did not sign in, per the rules.

### P2 — Wording

- **`/` "six feasibility dimensions":** the list has five.
- **"No credits" vs "signup credit":** `/` and the `/pricing` hero say "No credits"; the same pages then describe a $5 "signup credit".
- **`/` "All tools and pricing →":** goes to `/tools`, not `/pricing`.
- **`/` public audit log claim:** "we publish commit SHAs" has no link to where.
- **`/pricing` "Open the tools hub":** links to `/signup`.
- **`/tools` intro:** says "library planning", but `/library-planner` is linked from no page. It is only in the sitemap (checked by fetching `/`, `/tools`, `/pricing`, `/help`, `/showcase`, `/scout/`, `/developability` and `/prep`). The page works; it is orphaned.
- **`/tools`:** "a genuinely second opinion" (Boltz-2) is ungrammatical.
- **`/tools` comparison table:** PXDesign has no "Source repo" link.
- **`/tools/rfdiffusion` worked example:** the candidates header shows a "PILOT" badge ("Candidates (8) PILOT"). This is the only visible "pilot" in customer copy on tools.ranomics.com. The `?pilot=1` query parameter is URL only.
- **`/library-planner` result:** says "3 rounds MACS and FACS rounds", but all three listed rounds are FACS. The headline also reads "Feasible on yeast" while a warning says the library exceeds the single-batch transformation ceiling and will sample 11.3%.
- **`/developability` result:** shows the composite as "71%" while the page promises "a composite 0 to 1 score".
- **Scout known-binders table:** lists `13FL` and `35TL`. They are syntactically valid PDB ids; whether they are real entries is **unverified**.
- **Estimate request (not visible copy):** it sends `_csrf=` in a GET query string (`/api/wallet/estimate?…&_csrf=…`, seen in the network log), which puts the token in server and proxy logs.

## What worked

**Anonymous refusal:**
- None of the 14 tool forms has a submit button when logged out, with or without `?pilot=1`.
- Each shows "Sign in to run this →" (`/login?next=/tools/{slug}`) and "Create a free account" (`/signup`).
- A direct POST to `/submit` without CSRF returns 403; with CSRF it redirects to login. No job was created.

**Other checks:**
- **Scout, free, no account:**
  - Fetching 4ZQK from RCSB returned both chains (PD-L1 and PD-1) plus a UniProt match.
  - Three ranked epitopes came back, with flag explanations, a 3D viewer (canvas present) and 15 known binders with RCSB links.
  - Both CSVs download as `text/csv`.
  - The "Sign in to assess" links carry job, epitope and chain through `next=`.
  - Logged out, there is no "Open tool form" link. The intro instead says "Sign in to carry the residues you pick straight into a design tool", which is consistent. The logged-in handoff into binder tools is **unverified**, since that needs sign-in.
- **Developability:**
  - A valid VHH returns a full five-dimension report.
  - A too-short sequence gives "Sequence length must be between 10 and 2000 residues (got 5)".
  - Bad characters give "Sequence contains non-canonical residues: 1, 2, 3, 4, 5, B, J, X, Z."
- **Library Planner:** the default run returns a full plan.
- **Prices and rates:** no $/GPU-hour rate anywhere. `/pricing` is consistent: $5 for 30 days, $20 minimum top-up, monthly auto-reload cap.
- **Links and pages:**
  - Every `#` anchor on the 14 tool pages resolves.
  - "See what came back →" reaches `#worked-example` on all 14. The example sits below the form in page order; the teaser near the top points down to it.
  - Sitemap (42 URLs) and robots.txt are correct.
  - All footer and ranomics.com targets return 200.
- **Console:** no console errors on any tools.ranomics.com page loaded. Checked per page in a clean tab, with the capture proven by a canary.
- **Phone layout:** no horizontal page scroll at 375 px on `/`, `/tools`, `/pricing`, `/tools/rfdiffusion`, `/tools/proteina`, `/tools/opendde`, `/help`, `/help/faq`, `/help/tools/proteina`, `/scout/`, `/developability`, `/prep`, `/showcase`, `/login`, `/signup` and `/terms`. On `/signup` the page itself does not scroll, but the card clips its contents (P1-1).
- **Images:** no broken images on any page swept.
- **Cookies:** there is no cookie banner on tools.ranomics.com or on the ranomics.com contact page, so there was nothing to decline.

## Not checked / unverified

- Anything after sign-in: the post-login `next=` landing (P1-5), the Scout → tool-form handoff, and top-up or payment pages.
- Whether Google Analytics fires. The `gtag` script is in the HTML and `dataLayer` fills, but no request to googletagmanager appeared in this pane's network log. The pane may block trackers.
- **Phone:** only the 16 pages listed above were measured in true phone emulation. The other help and tool pages were measured in 375 px iframes only.
- Tablet width was not checked.
- The LinkedIn and X targets were not loaded. The footer logo link has no `aria-label`; whether its image carries alt text was not checked.
- Scout on an uploaded file (only the RCSB fetch was used) and the 1HEW example button.
- `/prep` behaviour, since it needs sign-in.
