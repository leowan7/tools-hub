# Anonymous Scout load baseline — Phase 0 ground truth

**Phase:** 0 (measurement only, no code change).
**Worked from SHA:** `37a0f3a9b5e43471a900568e1ac3cf1789ca5aea` (`origin/main`,
"Merge pull request #149"). Contains #148 (`83dbf31`), so `scout/ratelimit.py`
is present.
**Measured:** 2026-08-18.
**Plan:** `docs/HANDOFF-2026-08-18-anon-rate-limiting.md`.

Every number below records how it was obtained. Anything not measured is
labelled **NOT MEASURED** with the action that would resolve it. Nothing here
is an estimate dressed as a measurement.

---

## Headline: two premises of the plan do not survive measurement

1. **"Each anonymous analysis pins 20-35 seconds of CPU" is not reproducible.**
   Measured cost is **~2 CPU-seconds typical**, **~5 CPU-seconds worst case**
   within the 8 MB anonymous upload cap. That is 10-15x lower than the figure
   the plan's urgency rests on. See §1.
2. **Most of that 2 CPU-seconds is spent on a dead feature.** The known-binder
   lookup fans out to 40 threads and 41 HTTPS requests per anonymous
   `/analyze`, and returns zero binders for every target tested — because the
   SAbDab endpoint now answers HTML for every PDB id. See §1.4.

Both change the cost/benefit of Phases 1-4. Neither invalidates the plan's
direction; both change its sizing. See §6.

---

## 1. Real cost per anonymous analysis

### 1.1 How it was measured

freesasa has **no Windows wheel and no wheel of any kind** (source-only for all
14 released versions), and this workstation has no C compiler, so the pipeline
cannot run on the Windows dev box at all. Measuring there would have silently
skipped the SASA step — the expensive one — and produced a fictional number.

Instead the pipeline was run on **real Linux with the exact production
dependency pin**:

| | |
|---|---|
| OS | Ubuntu 24.04 (WSL2, kernel 6.6.87.2) |
| CPU | 12th Gen Intel Core i7-1260P, 16 threads, 15 GB RAM |
| Python | 3.12.13 |
| freesasa | **2.2.1** — exactly the `requirements.txt:16` pin |
| numpy / scipy / biopython | 2.5.2 / 1.18.0 / 1.88 |
| Code | `scout/` copied verbatim from the worked SHA |

`run_pipeline` was called directly with a fresh job directory per repetition.
Wall = `time.perf_counter()`, CPU = `time.process_time()` (user+system, all
threads). n=5 per structure, median reported.

**This is a dev-box figure, not production hardware.** §1.5 calibrates the gap
against a real production measurement.

### 1.2 Typical structures — `run_pipeline` (what `GET /scout/progress` executes)

| Structure | Chain | Residues | File | Intake `parse_pdb` | Analyse wall | Analyse **CPU** |
|---|---|---|---|---|---|---|
| `1HEW` (lysozyme — the shipped one-click example) | A | 129 | 129 KB | 7.2 ms | 0.181 s | **0.057 s** |
| `3s7g` (Fc + antibody) | A | 65 | 84 KB | 5.9 ms | 0.179 s | **0.058 s** |
| `3ave` (IgG1 Fc homodimer) | A | 211 | 655 KB | 34.8 ms | 0.359 s | **0.240 s** |

### 1.3 Worst case an anonymous caller can force

`compute_rsa` runs freesasa on the **whole structure**, not the scored chain
(`scout/sasa.py:98`), so cost tracks total atom count. The lever an attacker
has is therefore file size, bounded by `ANON_MAX_UPLOAD_BYTES` = 8 MB
(`scout/routes.py:79`). Real RCSB entries were downloaded and run:

| Entry | Size | Atoms | Chains | Wall | **CPU** | |
|---|---|---|---|---|---|---|
| `5IRE` | 1.02 MB | 10,940 | 6 | 0.87 s | 0.76 s | |
| `6VXX` (spike) | 2.18 MB | 22,812 | 3 | 1.63 s | 1.51 s | |
| `1FFK` | 5.34 MB | 64,268 | 27 | 3.11 s | 3.05 s | |
| `4V19` | 6.20 MB | 67,809 | 28 | 5.14 s | **5.12 s** | **worst within cap** |
| `6CGV` | 8.39 MB | 99,723 | 21 | 6.43 s | 6.38 s | over cap — refused |
| `1JJ2` | 8.64 MB | 90,418 | 28 | 6.67 s | 6.59 s | over cap — refused |

**The 8 MB cap is doing real work.** It bounds worst-case pipeline CPU at
roughly 5-6 CPU-seconds. Legacy PDB format tops out near 99,999 atoms
(~8.2 MB), so the cap sits almost exactly at the format's own ceiling.

### 1.4 The other half of `/analyze` — and the finding that matters

`POST /scout/analyze` does more than the pipeline. Measured separately:

| Step | Wall | CPU | Notes |
|---|---|---|---|
| `detect_interfaces` | 0.012 s | 0.012 s | negligible |
| `resolve_uniprot_id` | 0.887 s | 0.033 s | network-bound, cheap on CPU |
| `fetch_known_binders` (cold) | 1.2-1.5 s | **1.4-2.6 s** | **40-thread fan-out** |
| `fetch_known_binders` (warm) | 0.00001 s | 0.00001 s | module `_CACHE` hit |

Cold-cache CPU across four real accessions (P00698 lysozyme, P01308 insulin,
P00533 EGFR, P0DTC2 spike): **1.42 / 1.79 / 1.81 / 2.61 CPU-s**, mean **1.9
CPU-s**. Peak thread count during each: **42**.

The very first call in a fresh process measured **11.9 CPU-s** (TLS and import
warm-up on top of the fan-out) — relevant because every worker restart and
deploy resets both the process and its cache.

**This lookup is dead code paying full price.** Measured directly:

- `_rcsb_pdb_ids_for_uniprot("P00533")` returns exactly **40** PDB ids (the
  `_RCSB_PROBE_LIMIT` cap, `scout/epitope_db.py:50`).
- `_sabdab_entry_for_pdb` returns **0 rows** for every one.
- `query_sabdab("P00533")` therefore returns **0 hits**, and
  `fetch_known_binders` returns **0 binders** — for EGFR, one of the most
  antibody-co-crystallised proteins in the PDB.

Probing the endpoint directly
(`https://opig.stats.ox.ac.uk/webapps/sabdab-sabpred/sabdab/summary/<id>/`,
`scout/epitope_db.py:39`) returns **HTTP 200 with an identical 1456-byte HTML
body for every id tested** — `3s7g`, `1ivo`, `7k8m`, `6xdg`, all genuine
antibody complexes. `_sabdab_entry_for_pdb` treats a leading `<` as "not in
database" (`scout/epitope_db.py:606`) and returns `[]`. The host itself is up
(`opig.stats.ox.ac.uk/` returns 200/9016 bytes), so this is not a network
block from the measuring host — the endpoint has moved or changed shape.

So **the single largest CPU cost of an anonymous analysis is a 41-request,
40-thread lookup that always yields nothing.** Caveat: probed from this
network only; a production probe would confirm. That is one log line or one
`railway run` away.

### 1.5 Production delta — measured, not assumed

Four real `GET /scout/example` requests to `https://tools.ranomics.com`, timed
against a static-asset RTT floor on the same connection:

- `/static/style.css` — median **116.4 ms** (RTT floor)
- `/scout/example` — median **130.4 ms**, all 4 returned 200
- **Production server time for Scout intake ≈ 14 ms**

The same intake work (`cleanup_old_jobs` + `count_job_dirs` + `mkdir` + copy
130 KB + `parse_pdb`) measured 7.2 ms of CPU for `parse_pdb` alone on the dev
box. Production is therefore **within roughly 2x of the dev box**, not an order
of magnitude off.

### 1.6 The number

| | Dev box (measured) | Production (dev-box x2, calibrated in §1.5) |
|---|---|---|
| Typical anonymous analysis | **~2 CPU-s** | **~4 CPU-s** |
| Worst case within the 8 MB cap | **~7 CPU-s** | **~14 CPU-s** |
| Of which the dead SAbDab lookup | ~1.9 CPU-s | ~4 CPU-s |

**The plan's "20-35 seconds of CPU" (handoff lines 14-15) could not be
reproduced.** Hardware does not explain it — production is ~2x this box, not
10x. Two benign explanations that a later phase should rule out rather than
assume: the original figure may have been *wall-clock* on a cold worker
(where the first `fetch_known_binders` alone cost 11.9 CPU-s and network waits
add seconds), or measured before the 8 MB anonymous cap existed. Either way,
**Phases 1-4 must not be sized off 20-35 CPU-s.**

---

## 2. What a legitimate session actually does

### 2.1 How it was measured

A **real Flask app instance** (`app.py`, Supabase env blanked so nothing
touched production) served by a real threaded Werkzeug HTTP server on
127.0.0.1:5599. Driven with a real `requests.Session` carrying cookies, in the
exact order `templates/scout/index.html` fires them. `scout.ratelimit.hit` was
wrapped to record every metered call, and a `before_request` hook recorded
every request. Request *counting* is independent of whether the pipeline
succeeds, because the limiter runs before the route body.

### 2.2 Measured result — metered cost per user action

| User action | HTTP requests | **Metered hits** | Bucket |
|---|---|---|---|
| Load `/scout/` (first visit) | 2 (`GET /scout/`, `GET /scout/quota`) | **0** | — |
| Click "load example" / upload / fetch PDB | 1 | **1** | `scout_intake` |
| **Click "Analyze epitopes"** | 3 (`/quota`, `/progress`, `/analyze`) | **2** | `scout_analyze` |
| View 3D structure + download both CSVs | 3 | **0** | — |

Confirmed properties:

- **A page load costs nothing.** `/scout/quota` (`scout/routes.py:339`),
  `/scout/pdb/<id>` (`:800`) and `/scout/download/<id>` (`:811`) carry no
  `anon_rate_limit`. Static assets never reach the limiter.
- **One analysis costs TWO hits on one bucket**, because
  `templates/scout/index.html:333` opens the SSE stream and its `done` handler
  calls `_finalizeAnalysis` (`:384`) which POSTs `/scout/analyze` (`:392`).
  Both carry `anon_rate_limit("scout_analyze", limit=10)`
  (`scout/routes.py:550, 835`). This matches the comment at
  `scout/routes.py:99-103`, and is now confirmed empirically rather than by
  reading.
- **No polling and no retry amplification.** `source.onerror`
  (`templates/scout/index.html:354`) closes the EventSource rather than letting
  it auto-reconnect, and the `stage: error` handler closes it too. Measured: no
  repeat requests.
- **Failed analyses still consume quota.** In the probe, `/scout/analyze`
  returned **500** and was still metered `ALLOWED` — the limiter decorator runs
  outermost. A user whose structure fails burns allowance at the same rate as
  one who succeeds.
- **A dropped SSE stream doubles the CPU.** `/scout/analyze` re-runs the
  pipeline when `results.csv` is absent (`scout/routes.py:577-580`). In the
  happy path `/progress` has already written it, so the pipeline runs once. If
  the stream is cut mid-run — proxy timeout, tab close, flaky network — the
  file is never written and `/analyze` pays the full pipeline cost a second
  time. Observed directly in the probe: the SSE run failed, and `/analyze` then
  executed `run_pipeline` itself.

### 2.3 Verdict on QC's "1-3 intakes" estimate

**Confirmed for intake, but intake was never the binding constraint.**

A first-time visitor who explores rather than beelines: loads the example (1
intake), uploads their own structure (1-2 intakes), then analyses **several
chains** — chain choice is the whole point of the tool, and `/scout/upload`
returns the chain list precisely so the user can pick. Each chain tried is
2 metered `scout_analyze` hits.

| Behaviour | Intake hits | Analyze hits |
|---|---|---|
| Beeline: example, one analysis | 1 | 2 |
| Explorer: example + own upload, 4 chains tried | 2-3 | **8** |
| Thorough: 2 uploads, 6 analyses | 3 | **12** |

Against the fleet-wide ~20 (`10/worker x 2 workers`), the **intake bucket is
comfortable and the analyze bucket is the wall**: it allows only **~10
analyses per IP per 10 minutes fleet-wide**, and **5 per worker**. A single
thorough user is at ~12 hits — already over one worker's share of 10.

**Design input for Phase 4: budget the analyze bucket at 2 hits per run, and
size for ~6 runs per user session, not 1-3.**

---

## 3. Realistic NAT population

### 3.1 Who is actually in the funnel (repo evidence)

`shared/email_domain.py` is the only classifier. Its docstring (lines 5-13)
names three audiences: `business` (corporate email), `academic`
(institutional), `personal` (freemail). `ACADEMIC_SUFFIXES` (lines 134-164)
defines "institution" concretely: `.edu`, `.ac.uk`, `.edu.au/.cn/.sg/...`,
`.uni-`, `.university`, `.college` — 23 suffixes, global. Anything neither
freemail nor academic falls through to `business` (line 245).
`docs/PRODUCT-PLAN.md:127` states the target: "small biotechs, academic PIs
without GPU".

**NOT MEASURED — the actual mix.** The repo contains **no real signup data**.
Every email literal in the tree is a docstring example or test fixture
(`stanford.edu`, `cam.ac.uk`, `biotechco.com` appear only at
`shared/email_domain.py:6, 8, 23, 131`). Real rows live only in the hosted
Supabase project. Resolved by:
`select signup_quality, count(*) from user_profiles group by 1`.

### 3.2 The NAT figure

Derived from published, citable sources rather than intuition:

| Figure | Population | Source | Type |
|---|---|---|---|
| Campus wireless NAT pool `130.126.255.0/24` = **254 usable** public addresses, fronting private `10.192.0.0/14` | UIUC campus wireless/guest/VPN | [Guide to University of Illinois IP Spaces](https://answers.uillinois.edu/illinois/47572) (upd. 2026-06) | Measured (published config) |
| **~72,199 people** (60,848 students + staff) | UIUC Fall 2025 | [Illinois News Bureau](https://news.illinois.edu/illinois-sets-another-freshman-record-as-enrollment-tops-60000/) | Measured |
| "generally shared by at least **50-100 subscribers**"; 1.6% of IPv4 addresses carry 41.1% of traffic | Global CDN access logs | [Detecting and Characterizing Massively Shared IP Addresses](https://arxiv.org/html/2608.06517v1) (2026) | Measured (lower bound) |
| **8,000 users per IP** before port exhaustion | Enterprise office NAT | [Microsoft, NAT support with Microsoft 365](https://learn.microsoft.com/en-us/microsoft-365/enterprise/nat-support-with-microsoft-365) | Extrapolation (hard ceiling) |
| **3 req/sec per IP** unauthenticated; API key exists so you can exceed the limit "irrespective of other users at your Institution" | The same institutional population, on a free protein tool | [NCBI BLAST developer info](https://blast.ncbi.nlm.nih.gov/doc/blast-help/developerinfo.html) | Measured (published policy) |
| **55,000 req/hour per IP** | EMBL-EBI, same population | [Ensembl REST rate limits](https://github.com/Ensembl/ensembl-rest/wiki/Rate-Limits) | Measured (published policy) |

**RFC 6269 contains no usable users-per-IP number** — do not cite it for one.

**Recommended figure: ~300 distinct people behind one institutional egress IP.**
Confidence **medium-high**. Chain: 72,199 people / 254 public addresses = **284**;
students-only check 60,848 / 254 = **240**; round to 300. It sits above the
measured 50-100 floor and far below the 8,000 port ceiling.

The "universities have big public blocks so they don't NAT" intuition is **half
true and misleading here**: UIUC hands public addresses to *wired* hosts but
NATs *campus wireless, guest and VPN* behind four small pools. Researchers reach
a web tool from laptops on wireless or VPN, so the NATted path is the one this
product sees.

**Design range: 100-1,000. Phase 4 should use the HIGH end (~1,000).** Three
reasons:

1. **The costs are asymmetric, and `scout/ratelimit.py:10-13` already says so** —
   a false positive 429s an entire university; a false negative costs "wasted
   CPU on one box" (`scout/ratelimit.py:31-34`). With §1's real numbers, the
   false-negative side is now *even cheaper* than the code assumed.
2. A limit set at the mean fails the whole right tail of institutions.
3. **Peer tools on the identical population sit far above.** NCBI allows 3/sec
   = 1,800 per 10 min per IP. Scout's effective ~20 per 10 min is **~90x
   tighter than NCBI's**, on the same users.

### 3.3 The product-specific number is measurable, and nobody has run the query

**NOT MEASURED — distinct Scout users per IP per 10-minute window.** Everything
above is *people behind an IP*, not *simultaneous users of a niche protein
tool*. No published source can supply it.

The data already exists in production. Verified in this repo:
`public.user_events` stores both `ip inet`
(`supabase/migrations/0016_user_profiles_and_events.sql:77`) and `session_id text`
(`:66`, explicitly "NULL for anonymous events... session_id only"). Scout's
landing page extends `base.html`, which loads `static/js/track.js`, which fires
`page_view` with a localStorage session id on every page including `/scout/`
(`track.js:80, 26-38`). Retention is **365 days** (`cron/purge_old_events.py:67`).

Resolving query:
`count(distinct session_id) per ip per 10-minute bucket` over `/scout%` paths;
take p99 and max.

> **Trap — read before running it.** `user_events.ip` is written from the
> **LEFTMOST** `X-Forwarded-For` entry (`blueprints/public.py:254`), whereas the
> rate limiter keys on `_client_ip()`, which counts from the **RIGHT**
> (`shared/metrics.py:204-212`). **These are not the same value**, and the
> leftmost one is caller-forgeable. Calibrating a limit on `user_events.ip`
> measures a different key than the limiter enforces. The two coincide only if
> the forwarded chain has exactly one entry — which is precisely the unverified
> assumption §4 leaves open. **Phase 0's NAT question and Phase 2's peer
> question are the same unknown.**

---

## 4. The socket peer address on Railway — **MEASURED**

The plan calls this "the single fact Phase 2 needs, and it is currently
unknown". It is now known.

### 4.1 Method

The Railway CLI on this machine is authenticated (`leo@ranomics.com`) and
linked to `tools-hub / production / web`. Gunicorn's default access log format
puts `%(h)s` — `environ['REMOTE_ADDR']`, the TCP socket peer — in the first
field, and `accesslog = "-"` (`gunicorn.conf.py`) sends it to Railway's log
stream. Only read-only commands were used; nothing was deployed or changed.

A controlled probe: my own public IP was established as **`205.210.104.226`**
(api.ipify.org), then one `GET /healthz` was sent to `https://tools.ranomics.com`
with the distinctive UA `RanomicsPhase0Probe-a39516b2/1.0`, and the matching
log line retrieved.

### 4.2 Result

```
152.233.47.65 - - [18/Aug/2026:18:16:51 +0000] "GET /healthz HTTP/1.1" 200 30 "-" "RanomicsPhase0Probe-a39516b2/1.0"
```

**Client `205.210.104.226` was seen by gunicorn as `152.233.47.65`.**
`request.remote_addr` in production is the edge, never the client. Confirmed.

RDAP on every address observed in the `%(h)s` position:

| Observed peer | RDAP | Location | Seen for |
|---|---|---|---|
| `152.233.47.65` | CDN77-NYC / **Datacamp Ltd** | New York | **my controlled probe** |
| `152.233.47.69` | CDN77-NYC / Datacamp Ltd | New York | a Chrome visitor |
| `152.233.30.101/.102/.104` | CDN77-ASH / Datacamp Ltd | Ashburn | UptimeRobot |
| `79.127.178.81/.82` | CDN77-PAR / Datacamp Ltd | Paris | Bytespider crawler |
| `84.17.44.226/.227` | Datacamp range | — | ChatGPT-User crawler |

Response headers corroborate: `Server: railway-hikari`,
`x-railway-edge: jfk1`, `x-hikari-trace: jfk1.aghq`. **Railway's edge runs on
Datacamp/CDN77 infrastructure**, and the PoP varies by client geography — the
`jfk1` header matches the CDN77-NYC peer my probe produced.

### 4.3 What this means for Phase 2 — and how it changes the design

Phase 2 proposes: honour `X-Forwarded-For` only when the socket peer is
Railway's edge; fail **closed** when the peer is unknown. The peer is now
identified, but the shape of the answer is worse than "one address":

- The edge is **not a single IP, not a stable /24, and not RFC1918**. It is a
  **geographically distributed set of third-party (Datacamp/CDN77) public
  ranges**, at least four distinct /24s already observed across three cities.
- **Fail-closed against a hardcoded allowlist is an outage risk.** If Railway
  adds or renumbers a PoP, fail-closed means *every user is refused* until
  someone ships a CIDR update. Datacamp is not Ranomics' vendor and will not
  announce changes.
- **Recommendation for Phase 2:** do not hardcode CIDRs. Prefer a signal
  Railway controls and sends — `x-railway-request-id` / `x-railway-edge` are
  present on every response and are candidates worth checking on the *request*
  side — or accept a maintained allowlist with a **fail-open-and-alert** posture
  for an unrecognised peer rather than silent fail-closed. Whichever is chosen,
  the peer set must be treated as *changeable data*, not a constant.

### 4.4 **NOT MEASURED** — the contents of `X-Forwarded-For`

Still unknown: how many entries the header carries at the app, and whether the
edge appends, overwrites, or forwards a client-supplied header verbatim. This
matters acutely, because with `TRUSTED_PROXY_HOPS` defaulting to 1,
`_client_ip()` returns `chain[len(chain)-1]` (`shared/metrics.py:211`) — the
**rightmost** entry.

> **If the edge appends its own address, the rightmost entry is the CDN77 PoP,
> and every user sharing that PoP shares one rate-limit bucket.** At ~20
> requests per 10 minutes per bucket, that would be a fleet-wide outage
> disguised as a rate limit. This is a plausible failure mode, not an
> established one — it is exactly what must be measured before Phase 2 or
> Phase 4 ships.

**Why it was not resolved here:** the only ways to read the header are to
deploy a log line or to exhaust the limiter against production. Exhausting the
limiter would, *if* the pessimistic case is true, 429 every real visitor behind
CDN77-NYC for 10 minutes. Running an experiment whose failure mode is the
outage it is testing for is not a Phase 0 action; it needs the owner's
go-ahead.

**The single action that resolves it.** Add to `shared/metrics.py`, inside
`_client_ip()` immediately after `hops = _trusted_proxy_hops()` (line 204):

```python
logger.warning(
    "XFF-PROBE peer=%s xff=%r chain_len=%d hops=%d resolved=%s",
    request.remote_addr,
    request.headers.get("X-Forwarded-For", ""),
    len([p for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]),
    hops,
    "(computed below)",
)
```

Deploy, load `/scout/` once from a known client IP, read one line with
`railway logs`, revert. That yields the chain length, the ordering, and whether
a client-supplied header survives — all four facts Phase 2 needs.

---

## 5. Thread-safety audit of the Scout anonymous request path

**Scope:** `GET /scout/` -> `/scout/example` | `/scout/upload` | `/scout/fetch-pdb`
-> `GET /scout/progress` (SSE) -> `POST /scout/analyze`, plus everything they
reach in `scout/` and `shared/`.

### 5.1 Empirical result — the test the plan asks for

24 pipelines (8 each of `1HEW`, `3ave`, `3s7g`) launched from 24 threads
released simultaneously by a `threading.Barrier`, versus the same three run
serially. Comparison is **SHA-256 of the produced `results.csv`**.

```
1HEW.pdb                8/8 completed, 1 distinct hash, matches serial: True
3ave_igg1_fc_dimer.pdb  8/8 completed, 1 distinct hash, matches serial: True
3s7g_fc_ab.pdb          8/8 completed, 1 distinct hash, matches serial: True

TEST A VERDICT: PASS — deterministic under threads
```

No corruption, no cross-talk, no exceptions. The freesasa + numpy + scipy +
Biopython path produces byte-identical output under 8-way concurrency.

### 5.2 Static audit — every hazard found, with locations

Method: AST scan for module-level mutable state and `global` rebinds across
`scout/` and `shared/`, plus targeted greps for `os.chdir`, `tempfile`,
`os.environ` writes. **No `os.chdir`, no `getcwd`, no `tempfile`, and no
`os.environ` writes exist anywhere in `scout/` or `shared/`** — the classic
non-reentrancy sources are simply absent.

| # | Location | Finding | Verdict |
|---|---|---|---|
| H1 | `scout/epitope_db.py:643-647` | `query_sabdab` spawns **one thread per PDB id**, up to `_RCSB_PROBE_LIMIT = 40` (`:50`), per request, with no shared pool | **Correct but unbounded fan-out** — see 5.3 |
| H2 | `scout/epitope_db.py:750-757` | `fetch_known_binders` spawns up to `_MAX_CONTACT_STRUCTURES = 5` (`:57`) more; writes into a pre-sized list by index | Safe (disjoint indices, `join`ed) |
| H3 | `scout/epitope_db.py:64-65, 728-763` | `_CACHE` module dict, guarded by `_CACHE_LOCK` on every read and write | Thread-safe. **Unbounded** and per-worker — memory grows with distinct UniProt ids; lost on every deploy |
| H4 | `scout/sasa.py:91` | `freesasa.setVerbosity()` writes **process-global C-library state** on every `compute_rsa` | Benign — always the same value, idempotent. Flagged because it is a global write from request threads |
| H5 | `scout/routes.py:213/222` then `:385` | `count_job_dirs` capacity check and `create_job_dir` are **TOCTOU** | Over-admission bounded by concurrency (a few extra job dirs). Budget guard, not a security boundary |
| H6 | `scout/routes.py:924-945` | `_slotted` holds a worker thread for the **entire** SSE stream | Correct by design (the docstring explains why wrapping the generator beats decorating the view) — but it is the queue-depth exposure the plan names |
| H7 | `scout/ratelimit.py:140-169` | `_INFLIGHT` global, `global` rebind under `_INFLIGHT_LOCK`, released in `finally` | Correct |
| H8 | `scout/ratelimit.py:55, 90-109` | `_WINDOWS` dict, all access under `_LOCK`; eviction is lowest-hit-count first | Correct, and the eviction ordering the plan's Phase 3 must preserve is intact |
| H9 | `scout/jobs.py:155-160` | `create_job_dir` uses `uuid4` + `mkdir(exist_ok=False)` | Race-free by construction |
| H10 | `scout/jobs.py:163-244` | `cleanup_old_jobs` reaps only strict-UUID dirs older than 1 h, swallowing `OSError` | Safe — the 1-hour cutoff means it cannot race an in-flight job. `base_dir` is relative `tmp/`, safe because nothing ever calls `chdir` |

Flask's `session` and `request` are thread-local under `gthread`, so
`anon_compute_slot`'s `session.get("user_email")` check (`scout/ratelimit.py:157`)
and `_current_owner_key` (`scout/routes.py:150`) are per-request correct.

### 5.3 The throughput measurement that decides the phase

The same 24 pipelines, serial versus an 8-permit semaphore, same process:

| | Wall | CPU |
|---|---|---|
| Serial | 9.77 s | 5.23 s |
| 8 threads | 7.05 s | 7.57 s |
| | **1.39x faster** | **1.45x more CPU** |

Effective parallelism `cpu/wall` = **1.07 cores**. The workload is
**GIL-bound**: Biopython parsing, patch clustering and scoring are Python-level,
and freesasa's C call is a small fraction of the total. Threads overlap the file
I/O and little else.

### 5.4 **VERDICT: option (1), threaded workers, is SAFE — with two conditions**

**Correctness: yes.** Nothing in the request path assumes one-request-per-process.
The module-level state that exists is either immutable lookup tables or properly
lock-guarded. The empirical test is clean. **A cross-worker semaphore (option 2)
is NOT required on thread-safety grounds.**

Two conditions the plan must carry into Phase 1:

1. **Do not expect capacity from it.** gthread buys **1.39x** wall throughput
   for **1.45x** the CPU. Its real value is exactly what the plan says in its
   opening — it lets load *queue* instead of being refused, and it makes the
   existing `ANON_MAX_CONCURRENT_RUNS` semaphore start working as written. It
   is not a 4x capacity increase, and sizing that assumes one will be wrong.

2. **Bound the fan-out before enabling threads (H1).** Each in-flight
   `/analyze` already spawns up to **45** threads of its own. With
   `gthread` at N threads x 2 workers, worst case is `N x 45 x 2` OS threads —
   at N=8 that is **720 threads** on a container sized for 2 sync workers.
   This is the single concrete hazard of turning threads on, and §1.4 shows the
   whole fan-out currently returns nothing. **Cheapest correct fix: delete or
   disable the SAbDab lookup** (it is dead), or bound it with a module-level
   `ThreadPoolExecutor` of fixed size shared across requests. That single change
   removes ~90% of the measured CPU cost of an anonymous analysis *and* the
   thread-explosion risk, and it is a smaller diff than Phase 1 itself.

---

## 6. What changes in Phases 1-6

| Phase | Change |
|---|---|
| **Premise** | "20-35 CPU-s per analysis" is **not reproducible** (§1.6). Real: ~2 CPU-s typical, ~5 worst case in-cap; ~2x that in production. The CPU emergency is roughly 10x smaller than the plan assumes — which makes the generous per-IP limit of Phase 4 **cheaper and safer**, not riskier. |
| **New, unplanned** | The known-binder lookup is **dead and expensive** (§1.4): 41 requests, 40 threads, ~1.9 CPU-s, zero results, every anonymous analysis. Removing it is a smaller diff than Phase 1 and deletes most of the cost Phase 1 exists to bound. **Do this first.** |
| **Phase 1** | Option (1) is **safe** (§5.4). But it delivers 1.39x, not a multiple, and it must be preceded by bounding the 45-thread-per-request fan-out or a `gthread` worker becomes a thread bomb. |
| **Phase 2** | Peer address **resolved** (§4.2): Datacamp/CDN77 edge, e.g. `152.233.47.65`. But it is a *distributed third-party set*, so **fail-closed on a hardcoded CIDR list is an outage risk** (§4.3). XFF chain shape remains **NOT MEASURED**; exact one-line probe supplied (§4.4). Phase 2 is still blocked on that one line. |
| **Phase 3** | Unchanged. `_WINDOWS` eviction ordering is intact and correctly documented (H8). Note `_CACHE` (H3) is a *second* unbounded per-worker structure that a shared-state phase may want to consider. |
| **Phase 4** | Size the analyze bucket at **2 metered hits per run** (§2.2) and ~6 runs per session (§2.3) — not "1-3 intakes". NAT ceiling: design range 100-1,000, **use ~1,000** (§3.2). Before shipping the number, run the `user_events` query — and read the leftmost/rightmost warning in §3.3 first. |
| **Phase 5** | New input: **a failed analysis still consumes quota** (§2.2). A user with a malformed PDB burns their allowance on errors and then meets the wall — the worst possible first impression, and invisible in status-code-only measurement. |
| **Phase 6** | Confirms the plan's concern: the `'rate_limited'` reason code reserved at `supabase/migrations/0015_signup_rejections.sql:28` is **unused**, so refusals are currently not counted anywhere. |

---

## 7. NOT MEASURED — the honest register

| # | Unknown | What would resolve it |
|---|---|---|
| 1 | **Contents and length of `X-Forwarded-For` at the app.** Blocks Phase 2; risks a shared-bucket outage if the edge appends. | The one log line in §4.4: deploy, one page load, one `railway logs`, revert. |
| 2 | **Distinct Scout users per IP per 10 min** — the product-specific NAT number. | The `user_events` query in §3.3. Data exists, 365-day retention. Read the leftmost/rightmost trap first. |
| 3 | **Funnel mix** (academic / business / personal). | `select signup_quality, count(*) from user_profiles group by 1`. |
| 4 | **Whether the SAbDab endpoint also fails from Railway**, or only from this network. | `railway run python -c "import scout.epitope_db as e; print(e.query_sabdab('P00533'))"`, or one log line. |
| 5 | **True production CPU-seconds.** §1.5 measured production *wall* time for intake only (~14 ms) and calibrated a ~2x factor; the analyse half was deliberately not run against production to avoid consuming the shared analyze bucket. | Run the §1 harness inside the container, or read CPU from a `/metrics` scrape once `METRICS_ALLOWED_CIDR` is set. |
| 6 | **Railway container CPU allocation** (shared vCPU count / quota). | Railway dashboard; not exposed by the CLI commands used. |

---

## Reproducing this

- Linux env with the production freesasa pin (no wheels exist; conda-forge has
  prebuilt binaries): `micromamba create -p <env> -c conda-forge python=3.12
  freesasa biopython numpy scipy requests` — resolves freesasa **2.2.1**.
- Harness scripts were written to a scratch directory and deliberately **not
  committed**: no application code was modified in this phase.
- Production probes used only read-only `railway` commands plus ordinary HTTP
  GETs to public endpoints. Nothing was deployed, and no production data was
  written.
