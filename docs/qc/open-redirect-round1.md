# QC round 1 — PR #152 `fix/open-redirect-next-param`

Independent QC agent. Did not build this change.

- **Head reviewed:** `383760a464204bb3a4d9b3168f6cf91f05cc43bc`
- **Remote head:** `git ls-remote origin refs/heads/fix/open-redirect-next-param`
  → `383760a464204bb3a4d9b3168f6cf91f05cc43bc` — **matches**
- **Merge base:** `git merge-base 383760a origin/main` → `2c057fcb99da3f4ad97028c7d5dac60e232aff46` — matches the claimed base
- **Commits in range:** exactly one (`383760a fix(auth): close open redirect on the /login next parameter`)
- **Files touched:** `blueprints/auth.py` (+69/-…), `tests/test_login_redirect.py` (+120)

## VERDICT: DO-NOT-MERGE

The redirect defence itself is **sound** — 251 hostile payloads driven through the
real POST redirect branch produced **zero** off-site `Location` headers. But the
patch introduces a **new unauthenticated 500** that did not exist at the merge
base, and the accompanying test suite **fails to detect the removal of the layer
its own docstring calls the primary defence**.

---

## Baselines (measured, not quoted)

Command, run from each worktree root, no path argument:

```
C:/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
```

| tree | result |
|---|---|
| base `2c057fc` | `4897 passed, 19 skipped in 265.00s (0:04:24)` |
| branch `383760a` | `4967 passed, 19 skipped in 286.90s (0:04:46)` |

**+70, no failures, no flakes observed.** Builder's suite claim: **PASS (exact match).**

---

## Per-item results

| # | Item | Result |
|---|---|---|
| 1 | Pin SHA / confirm remote head | **PASS** |
| 2 | Measure both baselines | **PASS** — 4897/19 → 4967/19 |
| 3 | Attack the fix (payload corpus, real requests) | **FAIL** — new 5xx (B-1) |
| 4 | Verify the SPACE decision empirically | **PASS** |
| 5 | Fix applies to the GET branch | **PASS** |
| 6 | Mutation-test with own mutations | **FAIL** — 4/13 survived (B-2) |
| 7 | Independent redirect-sink sweep | **PASS** (with one correction) |
| 8 | Write this report | done; **not committed/pushed** (gate not met) |

---

## Original bug: reproduced at base

Confirms the PR is fixing a real defect. At `2c057fc`, production-shaped app,
`verify_login` patched to succeed:

```
'/\t/evil.com' -> 302 '//evil.com'      <-- OPEN REDIRECT, server-side
'/\n/evil.com' -> 500 None
'/\r/evil.com' -> 500 None
'//evil.com'   -> 302 '/'
'/\\evil.com'  -> 302 '/'
```

- The tab exploit is real and needs no browser quirk. **Builder claim: PASS.**
- CR/LF hard-500 at base. **Builder's "bonus find" claim: PASS.**

---

## BLOCKING B-1 — new unauthenticated 500 via unhandled `ValueError` from `urlsplit`

`safe_next()` calls `urlsplit(value)` as its **first** statement, with no
exception handling. `urllib.parse.urlsplit` raises `ValueError` on several
inputs. The value comes straight from an attacker-controlled query parameter.

Evidence — branch `383760a`, `create_app()` with **TESTING off** (production
error handling):

```
'//['            -> GET 500
'//[::1'         -> GET 500
'//]'            -> GET 500
'//[]'           -> GET 500
'//＃evil.com'    -> GET 500
'//a[b'          -> GET 500
'//['            -> POST 500
'//＃evil.com'    -> POST 500
```

Same payloads at base `2c057fc`:

```
'//['         -> GET 200
'//[::1'      -> GET 200
'//]'         -> GET 200
'//＃evil.com' -> GET 200
```

**This is a regression introduced by this PR.**

Two distinct raise sites in CPython 3.13 `urllib/parse.py`:

- `Invalid IPv6 URL` / `'' does not appear to be an IPv4 or IPv6 address`
  — unbalanced `[` / `]` in the netloc (`parse.py:505` → bracket check)
- `netloc '…' contains invalid characters under NFKC normalization`
  — `_checknetloc`, `parse.py:436`; triggered by e.g. U+FF03, U+2100, U+FF0F, U+FE6B, U+2047

Severity notes:

- **Pre-auth.** `GET /login?next=//[` needs no session, no credentials, no CSRF token.
- **Fully URL-safe payload.** Percent-encoded form works identically:
  `GET /login?next=//%5B` → `ValueError: Invalid IPv6 URL`.
- Also reachable through a value that *looks* like a path:
  `next=/%09/%5B` decodes to `/\t/[`; `urlsplit` strips the tab, leaving `//[` → raise.
- Every 500 is an unhandled exception: error-tracker noise, log spam, and a
  trivially repeatable availability nuisance on the login page.

Fix is one line — wrap the parse, or run the cheap character checks first:

```python
try:
    parts = urlsplit(value)
except ValueError:
    return fallback
```

No test in the PR covers any input that makes `urlsplit` raise.

---

## BLOCKING B-2 — the guard does not detect removal of its own "primary" layer

13 mutations applied to `blueprints/auth.py`. Each mutation was **verified to
have landed** (`git diff --stat` non-empty and printed) before the guard suite
was run; the tree was `git checkout --`'d clean between every one, and the final
tree was confirmed clean. Guard suite: `tests/test_login_redirect.py`,
unmutated baseline `77 passed in 13.83s`.

| mutation | guard |
|---|---|
| B1 drop control-char reject | RED — 15 failed |
| **B2 drop urlsplit allowlist** | **GREEN — 77 passed** |
| B3 drop `/\` check | RED — 9 failed |
| B4 drop raw `//` check | RED — 6 failed |
| B5 unvalidate GET branch | RED — 19 failed |
| Q1 neuter `safe_next` entirely | RED — 57 failed |
| Q2 unvalidate POST branch only | RED — 19 failed |
| Q3 narrow ctl reject to CR/LF/TAB only | RED — 15 failed |
| **Q4 drop the scheme check, keep netloc** | **GREEN — 77 passed** |
| Q5 fallback returns the attacker value | RED — 57 failed |
| **Q6 relax `path.startswith("/")` to `"/" in path`** | **GREEN — 77 passed** |
| Q7 drop only the C1/DEL half of the ctl reject | RED — 6 failed |
| **Q8 POST failure path re-renders the raw form value** | **GREEN — 77 passed** |

**4 of 13 survived.** B2 is on the builder's own list of mutations claimed to
turn the suite red. It does not.

### Why B2 survives — the docstring's central claim is false

The docstring states each layer *"is load-bearing for at least one hostile value
that every other layer waves through (measured, not assumed)"*. Decomposing
`safe_next` into its four independent predicates and evaluating each against the
PR's own `HOSTILE` list:

```
--- values ONLY that layer catches (i.e. layer is load-bearing) ---
L1a-scheme/netloc    *** NOTHING — REDUNDANT across this corpus ***
L1b-path-rooted      ['evil.com']
L2-ctl               ['/\x0b/evil.com', '/\x0c/evil.com', '/\x00/evil.com',
                      '/\x7f/evil.com', '/\x85/evil.com']
L3+L4-raw//-or-/\    ['///evil.com', '////evil.com', '/\\evil.com',
                      '/\\\\evil.com', '/\\/evil.com']
```

The scheme/netloc allowlist — the layer the docstring calls **"primary"** — is
caught by nothing uniquely. Every hostile value it rejects is rejected by some
other layer as well, so deleting it changes no test outcome. The suite
**certifies a property it does not test**: exactly the failure mode a prior QC
round here missed.

Q4 and Q6 are the same defect narrowed. Q8 is a coverage hole against a claim
the diff comment makes explicitly ("Applied before the field is re-rendered on
the failure path as well as before the redirect") — the real code does apply it,
but no test would notice if it stopped.

Minimum to close: add a hostile value that **only** the scheme/netloc allowlist
rejects, and a POST-failure-path assertion on the re-rendered hidden field.

---

## Attack corpus — what did NOT break (the fix's real strength)

Harness drove the actual Flask routes with `CSRF_PROTECT=0` (what
`tests/conftest.py` sets) and `verify_login` patched to succeed. **251 POSTs
reached the redirect branch.** Detector resolved each `Location` the way a
browser does — strip TAB/CR/LF, treat `\` as `/` in special URLs, `urljoin`
against `http://localhost/login`, strip userinfo — then compared the host.

> Method note: my first harness run produced **zero** POST findings because
> every POST was a silent **403** (CSRF). That is a false green of exactly the
> kind this contract warns about; it was caught by asserting a nonzero count of
> POSTs that actually reached the redirect branch, and the harness was fixed.
> The count is printed on every run.

Covered and **all rejected or rendered harmless** (no off-site host, no
strippable control char in the header):

- scheme-relative: `//`, `///`, `////`, `/\/`, `/\\`, `/\`, `\\`, `\/`, `//\`, `\//`
- every C0 control 0x00–0x20 plus DEL, NEL, NBSP — in three positions each
- U+2028, U+2029, U+FEFF, U+200B, U+3000, U+180E, U+2007, U+00AD
- unicode look-alikes: U+FF0F, U+2044, U+2215, U+29F8, U+FF3C, U+0338, U+FF1A
- userinfo: `/@evil.com`, `//user@evil.com`, `//localhost:80@evil.com`, `//evil.com\@localhost`
- `javascript:` / `data:` / `vbscript:`, with leading SPACE, TAB, LF, CR, VT, 0x01, and mixed case
- own-host prefix confusion: `/https://evil.com`, `/http://evil.com`, `https:/evil.com`, `https:evil.com`, `http:\\evil.com`, `//localhost.evil.com`
- dot-segment: `/..//evil.com`, `/./\evil.com`, `/localhost/../..//evil.com`
- percent-encoded and double-encoded: `%2f%2f`, `%2F%5C`, `%252f%252f`, `%25252f%25252f`, `%5c%5c`, `%09`, `%0d%0a`, `%00`, `%0b`, `%2e%2e`
- overlong UTF-8 for `/`: `%c0%af`, `%e0%80%af` (both 2- and 3-byte)
- length: 60,000-char path; 5,000 leading slashes; 5,000 embedded tabs

Off-site `Location` results: **0**. Strippable control chars in a `Location`: **0**.
The only failures in the whole corpus are the `ValueError` crashes of B-1.

---

## Item 4 — SPACE decision, verified empirically (PASS)

Not accepted on assertion; driven through the real POST branch:

```
'/a b'            -> 302 Location='/a%20b'            host=localhost
'/ /evil.com'     -> 302 Location='/%20/evil.com'     host=localhost
'/foo bar/baz'    -> 302 Location='/foo%20bar/baz'    host=localhost
'/ //evil.com'    -> 302 Location='/%20//evil.com'    host=localhost
'/\xa0/evil.com'  -> 302 Location='/%C2%A0/evil.com'  host=localhost
'/ \\/evil.com'   -> 302 Location='/%20%5C/evil.com'  host=localhost
'/ \t/evil.com'   -> 302 Location='/'                 host=localhost
```

SPACE is percent-encoded to `%20` by Werkzeug, is not browser-normalisable, and
cannot start an authority. Allowing it is **safe**. The last line confirms no
browser-normalisable sequence sneaks through alongside a space — a value
containing both SPACE and TAB is still rejected, because the control-char layer
runs on the whole string.

### Correction to a builder claim (non-blocking)

> *"VT, FF, NUL, DEL and NEL pass through BOTH urlsplit and Werkzeug raw, so
> they are rejected server-side."*

**REFUTED.** At base, with no guard in the way, Werkzeug percent-encodes all of
them exactly as it does SPACE:

```
VT   -> Location='/%0B/evil.com'
FF   -> Location='/%0C/evil.com'
NUL  -> Location='/%00/evil.com'
DEL  -> Location='/%7F/evil.com'
NEL  -> Location='/%C2%85/evil.com'
SP   -> Location='/%20/evil.com'
```

They do **not** reach the browser raw. Layer 2 is therefore harmless
defence-in-depth, not the barrier the docstring describes, and the five tests
that "prove" it are asserting against a threat that the header layer already
neutralises. This does not need to change the code — but the docstring and the
PR description assert a mechanism that does not exist, and that is the same
class of error as B-2.

---

## Item 5 — GET branch (PASS)

`safe_next` is applied on the GET branch (`blueprints/auth.py:92`). Verified by
driving `GET /login?next=<payload>` across the full corpus and regexing the
rendered hidden field: every hostile value collapses to `value="/"`.

Raw (non-re-encoded) query strings confirm decoding happens before validation:

```
next=/%09/evil.com           -> 200 field=[b'/']
next=/%0d%0a/evil.com        -> 200 field=[b'/']
next=%2f%2fevil.com          -> 200 field=[b'/']
next=%252f%252fevil.com      -> 200 field=[b'/']
next=%5c%5cevil.com          -> 200 field=[b'/']
next=%c0%af%c0%afevil.com    -> 200 field=[b'/']
```

Values that survive into the field (`/ /evil.com`, `/..//evil.com`,
`/%C0%AF/evil.com`, `/https://evil.com`, `/@evil.com`, unicode look-alikes) were
each resolved browser-style and **all keep host `localhost`** — they are
path-absolute references, not off-site. Not findings.

Mutation B5/Q2 confirm both branches are independently guarded.

---

## Item 7 — independent redirect-sink sweep

Swept **158** `redirect(` call sites outside `tests/` and `venv/`, plus every
direct `Location` header assignment. Isolated the ~65 that do not go straight
through `url_for(...)` and traced each target's provenance.

**Conclusion: `next` in `auth.login` is the only user-controlled redirect target.
Builder's claim stands.** Detail:

| sink | target provenance | verdict |
|---|---|---|
| `blueprints/auth.py:125` | `safe_next(request.form["next"])` | the subject of this PR |
| `blueprints/wallet.py:271` | `create_portal_session(...)` → Stripe API | off-site by design, server-supplied |
| `blueprints/wallet.py:693` | `result.get("url")` → Stripe Checkout | off-site by design, server-supplied — **builder's list omitted this one**; still not request-controlled |
| `blueprints/wallet.py:858` | string literal | safe |
| `blueprints/lab_projects.py` (×18) | `detail = url_for(...)` + literal `?handoff=` + `trunc_qs` (`f"&truncated={truncated}"`, a bool) | safe |
| `blueprints/campaigns.py` (incl. `_cutover_redirect`) | `url_for(...)` | safe |
| `blueprints/targets.py:1485` | `request.form["return_to"]` compared to the literal `"list"`, never used as a URL | safe |
| `blueprints/jobs.py:580,633` | `url_for(...)` | safe |
| `blueprints/tools.py:1037`, `admin.py` (×14), `app.py:784` | `url_for(...)` | safe |
| `shared/wallet.py:945,950` | `url_for("auth.account") + literal` | safe |
| `scout/routes.py:975` → `scout/handoff.py:190` | `f"{TOOLS_ORIGIN}/tools/{tool_slug}?handoff={handoff_id}"` — authority fixed by the `TOOLS_ORIGIN` constant and precedes all interpolation | safe |
| `shared/idempotency.py:429` | replays a `Location` cached from a prior response | inherits that response's gating; not a new sink |

One extra observation the builder's sweep did not surface:

- `blueprints/wallet.py:673-675` reads a **user-controlled** `request.form["next"]`
  into `session["wallet_gate_form"] = {"return_url": next_url}`. The only reader
  of that key (`wallet.py:451`) pulls `"tool"` and never `"return_url"`, and no
  template references it. **Dead write, not a redirect sink** — but it is a
  loaded gun sitting next to the exact bug this PR fixes, and worth deleting.

---

## What I could not verify

- **Real browser behaviour.** All `Location` conclusions come from a
  deterministic model of browser URL resolution (strip TAB/CR/LF, `\`→`/` in
  special URLs, RFC-3986 `urljoin`), not from driving Chrome/Firefox/Safari. The
  unicode look-alike and dot-segment cases are the ones where a real engine
  could in principle differ; nothing in the corpus was marginal enough for me to
  consider it likely.
- **Non-CPython / other CPython versions.** The `urlsplit` raise set in B-1 and
  the TAB/CR/LF stripping behaviour in layer 1 were measured on
  `3.13.0 (tags/v3.13.0:60403a5)` only. The raise sites are version-sensitive —
  which is itself an argument for the `try/except`.
- **Production deployment shape.** Tested through Flask's test client. A proxy
  or WAF in front of the app could reject `[` in a query string before it
  reaches Python, which would blunt B-1's reachability. I did not inspect the
  deployment config, so I have assumed it does not.
- **The two known flaky node tests.** Both full-suite runs were green, so no
  rerun was needed and I did not exercise the flake path.

---

## Blocking findings to clear before merge

1. **B-1** — wrap `urlsplit` in `try/except ValueError: return fallback` (or move
   the character checks ahead of the parse). Add tests for `//[`, `//]`, `//[]`,
   `//[::1`, `//＃evil.com`, and the encoded `next=//%5B` / `next=/%09/%5B` forms,
   asserting a 302/200 rather than a 500.
2. **B-2** — add at least one hostile value that **only** the scheme/netloc
   allowlist rejects, so mutation B2 turns the suite red; add a POST-failure-path
   assertion on the re-rendered hidden field (Q8). Correct the docstring's
   "load-bearing" and "Werkzeug raw" claims to match measured behaviour.

Non-blocking: delete the dead `return_url` write at `blueprints/wallet.py:673-675`.
