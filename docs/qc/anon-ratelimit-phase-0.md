# QC — Anonymous rate limiting, Phase 0 (ground truth)

**Verdict: PASS WITH CORRECTIONS.**

**SHA reviewed:** `37a0f3a9b5e43471a900568e1ac3cf1789ca5aea` (`origin/main`,
"Merge pull request #149"; contains #148 at `83dbf31`).
**Deliverable reviewed:** `docs/qc/anon-load-baseline.md` in the builder's
worktree `…/.claude/worktrees/agent-a39516b2b2069bd3c` (untracked, ~31 KB).
**Plan:** `docs/HANDOFF-2026-08-18-anon-rate-limiting.md`.
**Reviewed:** 2026-08-18. Reviewer did not build this phase.

QC worktree: `C:\Users\lab\Documents\Claude_projects\tools-hub\.claude\worktrees\agent-adb2d906c48575bfe`
(confirmed via `git worktree list`; 18 live worktrees). No other worktree was
written to, and nothing was pushed, merged, or deployed.

Method: every headline number was **re-derived independently**, not read. Where
I reached a different number, the discrepancy is recorded as a finding.

---

## Verdict in one paragraph

The measurement work is real and largely reproducible — I re-ran it on the same
hardware with the same dependency pins and landed on the same numbers for the
typical case, the dead-lookup cost, the metering model, and the thread-safety
audit. Four things must be corrected in writing before any later phase consumes
this document: (1) the recommendation to **delete** the SAbDab lookup is wrong —
the feature is alive upstream behind a new URL; (2) the worst-case CPU figure is
**understated**, and Phase 4's "generous ceiling" is justified from the typical
cost while an attacker pays the worst-case cost; (3) the NAT figure of ~300 does
not follow from its own sources or answer the question it is used for; (4) the
one probe procedure handed to Phase 2 targets an unmetered route and would log
nothing. None of these invalidate the phase's direction. All four are blocking
inputs for Phases 1 and 4.

---

## 0. Contract adaptations (stated, not silently skipped)

**Both-sides pytest baseline: MOOT, and I verified the precondition myself.**
Read-only `git -C <builder worktree>` (no `cd`, no writes):

```
$ git status --porcelain   ->  ?? docs/qc/anon-load-baseline.md
$ git rev-parse HEAD       ->  37a0f3a9b5e43471a900568e1ac3cf1789ca5aea
$ git diff --stat          ->  (empty)
$ git diff --cached --stat ->  (empty)
```

Exactly one untracked Markdown file, no tracked-file change, HEAD unmoved. There
is no code delta, so a merge-base-vs-branch suite comparison would compare a
commit against itself. I did not run it, per the adapted contract.

**Mutation testing: DOES NOT APPLY.** No tests were written or changed in this
phase, so there is nothing to mutate. Stated rather than skipped.

**Production-exhaustion experiment: NOT RUN.** The builder deliberately did not
run the `X-Forwarded-For` resolving probe against production because its failure
mode is 429ing every real visitor behind a CDN PoP for ten minutes. I did not run
it either. My only production contact was reading response headers on a public
health endpoint; I sent no forged headers and consumed no rate-limit bucket. The
critique of §4.4 below is on paper, as instructed.

---

## 1. Cost per anonymous analysis — the 10x discrepancy

### 1.1 What I did

freesasa 2.2.1 is source-only with no wheels and this Windows box has no C
compiler, so I built an independent Linux environment rather than reuse the
builder's: micromamba + conda-forge on WSL2 Ubuntu 24.04 (kernel
6.6.87.2-microsoft-standard-WSL2, 16 threads, 15 GB). Resolved versions:

| | mine | builder's |
|---|---|---|
| freesasa | **2.2.1** (= `requirements.txt:16`) | 2.2.1 |
| biopython / numpy / scipy | 1.88 / 2.5.2 / 1.18.0 | 1.88 / 2.5.2 / 1.18.0 |
| Python | 3.12 | 3.12.13 |

Identical pins on identical hardware. Any difference is therefore **method, not
environment** — which makes this a clean comparison. `scout/` copied verbatim
from the reviewed SHA. Fresh job directory per repetition, `time.perf_counter()`
for wall, `time.process_time()` **cross-checked against
`resource.getrusage(RUSAGE_SELF)`** (they agreed to 3 decimal places throughout,
so no CPU is being lost to accounting). n=5, median. Harness written from
scratch; the builder's was not reused and was never committed.

### 1.2 Typical structures — reproduced

Two independent runs of my harness, against the builder's figures:

| Structure | builder CPU | my run A | my run B | verdict |
|---|---|---|---|---|
| `1HEW` (shipped example) | 0.057 s | 0.091 s | 0.168 s | same order |
| `3s7g` | 0.058 s | 0.061 s | 0.107 s | same order |
| `3ave` | 0.240 s | 0.270 s | 0.242 s | agrees |

File sizes matched exactly (132,266 / 85,613 / 671,170 bytes). My numbers run
0-3x higher, consistent with running under background load. **Same order of
magnitude, comfortably. The typical case is confirmed: sub-0.3 CPU-seconds.**

**A methodological gap I checked and closed, which neither of us had ruled out:**
the pipeline calls `assign_dssp`, and if production had the `mkdssp` binary
installed, DSSP would run as a **subprocess** whose CPU `time.process_time()`
and `getrusage(RUSAGE_SELF)` both **exclude** — every measurement would silently
undercount. Production does not install it: `nixpacks.toml` sets
`nixPkgs = ["gcc"]` only. Both my run and the builder's logged "DSSP binary
unavailable … falling back to phi/psi classification", which is production's real
path. **No CPU escapes the measurement.** Confirmed, not assumed.

> **Superseded in part, 2026-08-21 (pending merge — not yet deployed).**
> A branch adds `scout/pydssp_numpy.py` between mkdssp and phi/psi, and these
> numbers were *not* measured on it. pydssp is O(L^2) in time and memory where
> phi/psi was O(L), so the CPU figures here become a floor rather than the
> cost. Until that branch deploys, the figures below still describe production.
>
> **Memory is bounded; CPU is not.** `_PYDSSP_MAX_RESIDUES = 2000`
> (scout/scoring.py) is a **per-chain** cap, and the arrays are freed between
> chains, so peak memory holds at ~0.51 GB however many chains arrive. Nothing
> caps the chain COUNT. `ANON_MAX_UPLOAD_BYTES = 8 MiB` admits ~25,900
> backbone residues, and splitting them into 13 chains of 1,999 clears every
> per-chain check: at a re-measured 1.02 s per 2,000-residue chain that is
> **~13 CPU-s on a single anonymous request** — above the `~9 CPU-s`
> adversarial `/progress` figure at `scout/routes.py:145` that this document
> derived and that the limiter was sized against. That figure has **not** been
> re-measured against the new branch, and an earlier version of this banner
> wrongly called the exposure bounded and quoted ~1.6 CPU-s.
> See `docs/qc/scout-pydssp-adoption.md` sections 8 and 9.

### 1.3 Worst case — I do NOT reproduce the builder's number

Same six RCSB entries, all six file sizes matching the builder's table exactly:

| Entry | Size | my atoms | **builder CPU** | **my CPU** |
|---|---|---|---|---|
| `5IRE` | 1.02 MB | 11,024 | 0.76 s | 0.60 s |
| `6VXX` | 2.18 MB | 23,694 | 1.51 s | 1.30 s |
| `1FFK` | 5.34 MB | 64,281 | 3.05 s | 3.50 s |
| `4V19` | 6.20 MB | 69,409 | **5.12 s** ("worst within cap") | 3.66 s |
| `6CGV` | 8.39 MB | 99,723 | 6.38 s | **9.00 s** |

`ANON_MAX_UPLOAD_BYTES = 8 * 1024 * 1024` = 8,388,608 bytes = 8,192 KB
(`scout/routes.py:79`). `6CGV` is 8,194 KB — over by ~1.8 KB. So the **largest
admissible** structure sits essentially at 6CGV's size and costs what 6CGV
costs. The builder labelled `4V19` (6.20 MB) "worst within cap", but there is a
2 MB admissible band above it that they measured and then did not carry into the
label.

**Correction:** worst case within the cap is **~6.4-9.0 CPU-s on this box**, not
5.12. The builder's own §1.6 says "~7 CPU-s", which contradicts their own §1.3
table label and is the closer figure. Applying their own §1.5 ~2x production
factor: **~13-18 CPU-s in production.**

I verified the cap is not bypassable: `ANON_MAX_UPLOAD_BYTES` is enforced on
**both** intake paths — `scout/routes.py:241` (upload) and `:439` (fetch-pdb) —
so an anonymous caller cannot smuggle a 9 MB structure in by PDB id. Good; the
builder asserted the cap does real work and it does.

### 1.4 Where the builder's framing is wrong

The deliverable's headline says the plan's "20-35 seconds of CPU" is "**10-15x
lower**" than measured. That conflates two different quantities:

| | measured (prod, 2x dev) | vs plan's 20-35 CPU-s |
|---|---|---|
| **Typical** anonymous analysis | ~0.2-0.5 CPU-s pipeline, ~4 CPU-s incl. lookup | **~10-100x lower** |
| **Worst case** an attacker chooses | **~13-18 CPU-s** | **within ~1.5-2x** |

**The plan's founding premise is not refuted; it is mis-scoped.** "20-35 seconds"
is a bad description of the typical case and a roughly correct description of the
worst case a hostile caller can force. That reading also explains it benignly,
which the builder half-anticipated (§1.6 offers "wall-clock on a cold worker" and
"before the 8 MB cap existed"). A third and simpler explanation — it was a
worst-case figure — was not considered and fits the data best.

This matters because it inverts the deliverable's central conclusion. §6 argues
the CPU emergency is "roughly 10x smaller than the plan assumes — which makes the
generous per-IP limit of Phase 4 **cheaper and safer**, not riskier." **An
attacker does not pay the typical cost. They pay the worst-case cost, by
choosing their upload.** Sizing a per-IP ceiling on typical cost is exactly the
error the plan's own "bound the real resource" insight exists to prevent.

**Did the builder measure the path an anonymous user actually hits?** Yes, and
correctly decomposed. `GET /scout/progress` executes `run_pipeline`;
`POST /scout/analyze` re-runs it only if `results.csv` is absent, then runs
`resolve_uniprot_id` + `fetch_known_binders` + `detect_interfaces`
(`scout/routes.py:571-606`). The SAbDab fan-out was measured **separately and
then included** in the §1.6 total (~2 CPU-s ≈ 0.1 pipeline + ~1.9 lookup). That
is the right treatment, and it is stated clearly.

---

## 2. "Zero binders for every target, including EGFR" — reproduced, **wrong conclusion**

This claim is used to justify deleting `scout/epitope_db.py`, `query_sabdab` and
`_RCSB_PROBE_LIMIT = 40`. The observation reproduces exactly. **The diagnosis
does not.**

### 2.1 What reproduces

Independently, from this network:

- `_rcsb_pdb_ids_for_uniprot` returns exactly **40** ids for P00533 (EGFR),
  P00698 (lysozyme) and P0DTC2 (spike), in ~0.2 s. The RCSB leg **works**.
- `_sabdab_entry_for_pdb` returns **0 rows** for every one of them.
- `fetch_known_binders("P00533")` → **0 binders**, wall 1.46 s, **CPU 1.61 s**,
  **peak thread count 42**. The builder's 1.42-2.61 CPU-s / peak 42 reproduces.
- The legacy endpoint returns HTTP 200 with a **byte-identical body for every id
  tested** (`3s7g`, `1ivo`, `7k8m`, `6xdg`, `1hew`, `12e8`, `4hkx` — all
  SHA-256 `9d20549853ba…`). I measure **1457** bytes; the builder's 1456 is the
  same body after `.strip()`.

So: the fan-out is real, it is unbounded, it costs ~1.6-1.9 CPU-s, and it
returns nothing. All confirmed.

### 2.2 What the builder missed

I was asked to distinguish four causes: dead upstream / client-or-IP blocked /
transient outage / our own parsing bug. **It is none of them. It is a fifth: our
client points at a retired URL.**

```
GET https://opig.stats.ox.ac.uk/webapps/sabdab-sabpred/sabdab/summary/3s7g/
  -> 301 -> https://sabdab.opig.stats.ox.ac.uk/summary/3s7g/
  -> 200, React SPA shell,  <title>SAbDab2</title>,  uiVersion 2.0.12
```

SAbDab was **rebuilt as a single-page app**. The old TSV endpoint now serves the
SPA's HTML shell — which is precisely why every id returns the same bytes, and
why `_sabdab_entry_for_pdb`'s `text.startswith("<")` check
(`scout/epitope_db.py:606`) reads it as "not in database". The check is behaving
as designed; it is being fed a page that no longer means what it used to.

**The data is alive and reachable.** There is a live FastAPI backend:

```
GET https://sabdab.opig.stats.ox.ac.uk/api/openapi.json
  -> 200, {"title": "SAbDab", "version": "2.0.10", ...}

GET https://sabdab.opig.stats.ox.ac.uk/api/rcsb-pdb-annotations
  -> 200, 14.7 MB JSON, 21,914 antibody-structure rows, 4.97 s, ONE request
```

Sample row: `{"PDB ID": "pdb_000010bt", "model": 0, "heavy chain": {...},
"light chain": {...}, "antigen_instances": [...]}` — heavy/light chain and
antigen annotations, i.e. the same information `query_sabdab` parses today.

Ruling out the other causes: the host is up; the RCSB leg works from the same
client on the same network in the same process; and the new API answers this
same client fine. **Not a block, not an outage, not our parser.**

### 2.3 Consequence

> **Deleting `scout/epitope_db.py` would destroy a working, restorable feature
> on a misdiagnosis.** The brief was explicit that only "genuinely dead upstream"
> justifies deletion. It is not dead.

The better fix is also the **cheapest fix on every axis**, and it is smaller than
either option the builder offers:

| | today | delete (builder's rec.) | repoint to `/api/rcsb-pdb-annotations` |
|---|---|---|---|
| HTTP requests per analyse | 41 | 0 | **1** (cacheable, whole DB) |
| threads spawned per analyse | 40 | 0 | **0** |
| CPU per analyse | ~1.9 s | 0 | ~0 warm |
| feature | broken | **gone** | **restored** |

One bulk fetch replaces the entire 40-thread fan-out. That removes ~90% of the
measured CPU cost *and* the thread-explosion hazard of §5.4 condition 2 *and*
restores the capability — which is strictly better than deletion, and strictly
better than the "bounded `ThreadPoolExecutor`" alternative the builder offers as
its fallback.

### 2.4 Why nobody noticed — and why they will not notice next time

Asked whether any test covers this path. **One file references it, and what it
does is the finding.** `tests/test_scout_anonymous_access.py:133`:

```python
monkeypatch.setattr("scout.epitope_db.fetch_known_binders", lambda *a, **k: [])
```

The only test touching the known-binder path **stubs it to return `[]` — the
exact value the broken production code returns.** There is no test that
exercises the real lookup at all. The suite is therefore incapable of
distinguishing "feature works" from "feature is dead", and it never was. That is
the same pattern already recorded in this repo's memory as *guards that certify
false*: the test passes the precise condition it appears to cover.

Whatever Phase 1 decides to do with this code, it should leave behind one check
that fails when the lookup returns nothing for a known-positive accession.

---

## 3. The NAT figure of ~300 — sources real, arithmetic partly unsupported, **inference does not hold**

### 3.1 The sources say what?

**Partly.** I fetched both.

*UIUC IP spaces* — **confirms the pool**: the page does list
`IllinoisNet Public NAT: 130.126.255.0/24`, fronting private `10.192.0.0/14`.
But it documents **four** NAT pools, not one:

| pool | size | fronts |
|---|---|---|
| `130.126.255.0/24` | 256 | IllinoisNet wireless (`10.192.0.0/14`) |
| `72.36.119.0/24` | 256 | IllinoisNet_Guest (`10.200.0.0/14`) |
| `130.126.101.128/25` | 128 | VPN, MFA |
| `130.126.143.0/25` | 128 | VPN, password-only |

Total ≈ 768 addresses (760 usable). The builder's prose acknowledges "four small
pools" but the **arithmetic uses only one of them (254)** while the numerator is
the **entire campus population**. Guest users egress from a different /24, VPN
users from two more. Numerator and denominator do not describe the same set.

*Illinois News Bureau* — **does not support the numerator as cited**. The article
states total enrollment **60,848** for fall 2025 and **does not mention staff or
faculty at all**. The builder cites it for "**~72,199 people** (60,848 students +
staff)". The ~11,351 staff addend is **uncited**. The headline chain
`72,199 / 254 = 284` therefore rests on one unsourced term and a denominator
covering a third of the egress capacity.

Re-derived honestly, the ratio spans:

| assumption | result |
|---|---|
| students only ÷ wireless pool (60,848 / 254) | **240** |
| all people ÷ all four pools (72,199 / 760) | **95** |
| builder's chain (72,199 / 254) | 284 |

So **95-284**, not a point estimate of 284. "Round to 300" takes the top of a
range whose top is the least defensible corner of it.

*arXiv 2608.06517* — **real, and the title matches exactly**: "Detecting and
Characterizing Massively Shared IP Addresses". The abstract states "over 40% of
total traffic coming from less than 2% of active IP addresses", consistent with
the builder's "1.6% / 41.1%". I could not confirm the specific "50-100
subscribers" figure from the abstract; it is plausibly in the body. Treat the
floor as *cited but not verified by me*.

The builder's note that **RFC 6269 contains no usable users-per-IP number** is a
good catch and I agree with it.

### 3.2 The inference — this is the real problem

Even granting the arithmetic, **enrollment ÷ pool size does not yield concurrent
users per egress IP.** It yields a population-to-address ratio, and the limiter
does not meter populations. It meters *requests keyed by source IP inside a
600-second window*. The quantity that sizes the ceiling is:

> distinct **Scout** users sharing one egress IP **within 10 minutes**
> = campus population × tool penetration × duty cycle

The builder's number drops the last two terms, both of which are far below 1 for
a niche protein tool. To its credit §3.3 says exactly this — "*Everything above
is people behind an IP, not simultaneous users of a niche protein tool*" — and
marks it **NOT MEASURED**. That honesty is genuine and I credit it. But §3.2 and
§6 then hand Phase 4 "**recommended figure ~300**" and "**use ~1,000**" anyway.
A figure cannot be disclaimed as the wrong quantity in one section and used as
the sizing input in the next.

A second unexamined step: NAT pools may assign the egress address **per flow**
rather than pinning each user to one address. Under round-robin, a single user's
requests spread across up to 254 addresses and "users per IP" means something
quite different again — it would make the per-IP key weaker for attackers and
gentler on labs simultaneously. Nobody established which behaviour applies, and
the ratio is only meaningful under the sticky interpretation.

### 3.3 Assessment of the 100-1,000 range and "use the high end"

Two of the three supporting arguments are sound. The asymmetry argument is
correct and well-sourced to `scout/ratelimit.py:10-13, 31-34`. The peer-tool
comparison (NCBI 3/sec ≈ 1,800 per 10 min per IP, on the same population) is a
genuinely useful anchor and the strongest evidence in the section — it does not
depend on the NAT arithmetic at all.

But **"use ~1,000" fails when combined with §1.3.** Bounding from capacity
rather than from population:

```
box capacity   = 2 workers x 600 s          ~ 1,200 CPU-s per 10-min window
attacker cost  = worst-case admissible      ~ 13-18 CPU-s per analyse (prod)
saturation     = 1,200 / 18                 ~ 66 analyses per 10 min
```

**A per-IP ceiling of 1,000 is ~15x the point at which a single IP can consume
the entire box's 10-minute CPU budget.** The justification offered for the high
end is the *typical* ~2-4 CPU-s cost; the attacker pays ~18. A defensible
generous ceiling is **O(100), not O(1,000)** — and it should be derived from
worst-case cost against measured capacity, which is a calculation this document
has all the inputs for and does not perform.

The right next step is the one the builder already identifies in §3.3: run the
`user_events` query for distinct sessions per IP per 10-minute bucket, p99 and
max. Their **leftmost/rightmost trap warning is correct and important** —
`blueprints/public.py:254` writes the leftmost XFF entry while the limiter keys
on `shared/metrics.py:211`'s rightmost, so the query measures a different key
than the limiter enforces. That warning should survive into Phase 4 verbatim.

---

## 4. The socket peer measurement

### 4.1 Was the method controlled?

**Adequately, yes — with one caveat the builder does not name.** The probe
established its own public IP first (`205.210.104.226` via ipify), then sent a
single `GET /healthz` carrying a distinctive UA
`RanomicsPhase0Probe-a39516b2/1.0`, then retrieved the log line **matching that
UA**. Gunicorn's default access format puts `%(h)s` (`REMOTE_ADDR`, the TCP
peer) first, and `accesslog = "-"` (verified in `gunicorn.conf.py`) sends it to
Railway's stream. The UA is the correlator, so a coincident third-party request
would not have matched. That is a real control, not a coincidence.

**Caveat: n=1.** One probe, one mapping. What actually carries the conclusion is
the corroborating RDAP table — five distinct addresses across three cities, all
Datacamp/CDN77, plus `x-railway-edge: jfk1` matching the CDN77-NYC peer the probe
produced. The *distributed third-party edge* conclusion is well-supported; the
single mapping on its own would not be.

I did **not** re-derive this (it needs authenticated Railway CLI access to
production logs). I mark it **accepted, not independently reproduced** — the one
item in this review where that is true.

§4.3's design consequence is the most valuable judgement in the deliverable and
I endorse it: the edge is not one IP, not one /24, and not RFC1918, so Phase 2's
proposed **fail-closed against a hardcoded CIDR allowlist is an outage risk** —
Datacamp renumbers a PoP and every user is refused until someone ships a CIDR
update. Treating the peer set as changeable data is correct.

### 4.2 §4.4's probe procedure is defective — Phase 2 must not run it as written

Two problems, one of which makes it produce nothing.

**(a) It targets an unmetered route.** The procedure says: "Deploy, load
`/scout/` once from a known client IP, read one line with `railway logs`,
revert." But `_client_ip()` is called from exactly **two** places in production
code — I grepped the whole tree:

```
scout/ratelimit.py:203    inside the anon_rate_limit decorator
shared/metrics.py:216     _ip_allowed(), for the /metrics CIDR gate
```

`GET /scout/` (`scout/routes.py:334`) carries **no `anon_rate_limit`
decorator**, which the builder's own §2.2 measures as "0 metered hits". So
loading `/scout/` **never calls `_client_ip()`, the log line never fires, and
the probe returns nothing.** Phase 2 would deploy, see an empty log, and be
stuck. The fix is trivial — hit `/scout/example` (metered `scout_intake`, costs
1 of 10) — but the procedure as written is broken and it is the *only* thing
standing between Phase 2 and its blocking unknown.

**(b) It logs PII on every request.** The snippet places a `logger.warning`
inside `_client_ip()`, which runs on **every metered request fleet-wide**,
emitting each caller's full `X-Forwarded-For` chain into Railway's log stream at
warning level. This repo has a `docs/PII-RETENTION.md`. Even a brief deploy
writes other users' IP chains to logs. Log **once** behind a module-level flag,
or echo the headers back to the probing caller only.

Neither defect affects the §4.2 finding. Both affect the handoff.

**Agreed and unchanged:** the resolving experiment must not be run against
production without the owner's go-ahead, because its failure mode is the outage
it is testing for. I did not run it.

---

## 5. The gthread SAFE verdict — **I concur, on stronger evidence than the builder's**

### 5.1 The hash test is weak evidence, as briefed

24 pipelines producing 1 distinct `results.csv` SHA-256 matching serial output
demonstrates determinism of *this output* under *this timing* on *one run*. It
does not exclude a rare race, a timing-dependent one, or state corruption that
does not reach `results.csv`. Taken alone it would not support a SAFE verdict on
which an entire phase branches.

### 5.2 So I ran a structural audit instead

AST scan (not regex, per this repo's own lesson) over `scout/*.py` plus the three
`shared` modules scout imports — `metrics.py`, `auth.py`, `supabase_client.py` —
for module-level mutable bindings, `global`/`nonlocal` rebinds, and thread
spawns. Then a follow-up grep for **runtime mutation** of every module-level
container found (`.append/.extend/.update/.pop/.remove/.clear/.insert/.add/
.setdefault/.discard`, and subscript assignment).

Module-level containers found: `_CACHE`, `_CACHE_LOCK`, `_THREE_TO_ONE`
(epitope_db); `DIMENSION_WEIGHTS`, `TIER_THRESHOLDS`, `DIMENSION_LABELS`,
`DIMENSION_DESCRIPTIONS` (feasibility); `_CSV_COLUMNS_BASE` (flags);
`_THREE_TO_ONE` (glycan); `CSV_COLUMNS`, `_SS_SCORES`, `FEASIBILITY_CSV_COLUMNS`
(pipeline); `_WINDOWS`, `_LOCK`, `_INFLIGHT_LOCK` + two `global _INFLIGHT`
rebinds (ratelimit); `ALLOWED_EXTENSIONS`, `VALID_HANDOFF_TOOLS` (routes).
Thread spawns: `epitope_db.py:643` and `:751` — exactly the builder's H1 and H2.

**Runtime-mutation grep result: no matches.** Not one module-level lookup table
is ever written to after import. The only mutated module state in the request
path is `_CACHE` (guarded by `_CACHE_LOCK`), `_WINDOWS` (guarded by `_LOCK`) and
`_INFLIGHT` (guarded by `_INFLIGHT_LOCK`) — all three correctly guarded, which I
verified by reading `scout/ratelimit.py:82-118, 144-175`.

**That is a real basis for SAFE, and it independently reproduces the builder's
ten hazards with nothing missing.** Their table is complete for the state that
matters. Flask's `session`/`request` being thread-local under gthread is correct.

### 5.3 One claim in §5.2 is factually false

> "**No `os.chdir`, no `getcwd`, no `tempfile`, and no `os.environ` writes exist
> anywhere in `scout/` or `shared/`**"

`shared/pdb_preflight.py:36` imports `tempfile` and `:516` calls
`tempfile.NamedTemporaryFile(...)`. Separately, `gunicorn.conf.py::on_starting`
writes `os.environ["PROMETHEUS_MULTIPROC_DIR"]`.

**Neither is a hazard.** `pdb_preflight` is **not reachable from scout** — scout
imports only `shared.auth`, `shared.metrics` and `shared.supabase_client`
(verified by grep), and `NamedTemporaryFile` is thread-safe regardless. The
gunicorn write happens in the master process pre-fork, outside the request path.

But the claim was offered as **evidence for the SAFE verdict**, and it is wrong
as written. Correct it to "none on the Scout request path". Given this repo's
documented history of audits that over-claim their own coverage, an audit
sentence that says "anywhere in `shared/`" when it means "on the path I traced"
is worth fixing rather than waving through.

### 5.4 Verdict

**SAFE stands. Option (1), threaded workers, is correct on thread-safety
grounds, and a cross-worker semaphore (option 2) is not required for
correctness.** I reached this independently and by a different method. This is
the deliverable's strongest section.

---

## 6. The 720-thread warning and the 1.39x / 1.45x measurement

**Arithmetic: all correct.** Checked every figure:

| claim | check | verdict |
|---|---|---|
| 8 x 45 x 2 = 720 | 720 | arithmetic ✓ |
| 9.77 / 7.05 = 1.39x wall | 1.386 | ✓ |
| 7.57 / 5.23 = 1.45x CPU | 1.447 | ✓ |
| cpu/wall = 1.07 cores | 7.57/7.05 = 1.074 | ✓ |
| `WEB_CONCURRENCY` default 2 | `gunicorn.conf.py`: `workers = max(1, _int_env("WEB_CONCURRENCY", 2))` | ✓ |
| no `worker_class`, no `threads` | absent from `gunicorn.conf.py`, `nixpacks.toml`, `Procfile` | ✓ |
| 40-thread fan-out, raw unbounded `threading.Thread`, one per PDB id + 1 RCSB search = 41 requests | `epitope_db.py:643-647`, `_RCSB_PROBE_LIMIT = 40` at `:50` | ✓ |

**One input is wrong, in the conservative direction.** The "45 threads per
request" figure adds `_RCSB_PROBE_LIMIT` (40) to `_MAX_CONTACT_STRUCTURES` (5) as
if they were concurrent. They are not: `query_sabdab` **joins all 40 probe
threads** (`epitope_db.py:645-647`) before returning, and only then does
`fetch_known_binders` spawn its ≤5 contact threads. The two fan-outs are
sequential, so peak is ~41. The builder's own §1.4 measured **peak 42**, which
contradicts their own 45 — and I measured **peak 42** independently.

Corrected: 8 x 42 x 2 = **672 OS threads**, not 720. Same order, same hazard,
same decision. Worth fixing because the number will be quoted.

**One point worth adding to Phase 1.** 1.39x throughput for 1.45x CPU means total
CPU draw rises ~45% while work stays constant — that 45% is GIL-contention
overhead. On a CPU-bound box *under saturation*, gthread makes CPU pressure
**worse**, not better. This reinforces the builder's condition 1 ("do not expect
capacity from it") and is a stronger argument for it than the one given: gthread's
value is queueing and making `ANON_MAX_CONCURRENT_RUNS` real, and it is bought at
a measurable CPU premium.

---

## 7. The goal question, answered in writing

### Can six researchers behind one NAT all use the tool in the same afternoon?

**Across an afternoon: yes. Inside any shared 10-minute window: no — it breaks at
the second or third of them.**

The window is 600 seconds, not an afternoon, so six researchers who happen not to
overlap are fine. The realistic failure is the case the front-end redesign is
aimed at: a lab meeting, a workshop, a group all trying it after the same
seminar.

The binding constraint, confirmed by decorator inspection rather than by reading
the builder's prose:

- `/scout/analyze` (`routes.py:549-554`) **and** `/scout/progress`
  (`routes.py:834-840`) both carry `anon_rate_limit("scout_analyze", limit=10)`.
  **One analysis = 2 metered hits.**
- Unmetered: `/scout/` (`:334`), `/scout/quota` (`:339`), `/scout/pdb/<id>`
  (`:800`), `/scout/download/<id>` (`:811`). Page loads and result downloads are
  free — confirmed.
- Effective allowance: **~5 analyses per worker, ~10 per IP per 10 min
  fleet-wide** (10 per worker x 2 workers, and only if requests distribute
  evenly across workers — they need not).

So six researchers doing **one** beeline analysis each = 12 analyze hits + 6
intake, which fits ~20 only under perfect load-balancing. Any one of them trying
a **second chain** — which is the entire point of the tool, since `/scout/upload`
returns the chain list precisely so the user can pick — breaks it. The builder's
own §2.3 "thorough user" at 12 analyze hits **exceeds one worker's allowance of
10 single-handedly**.

> **First place it breaks: the `scout_analyze` bucket, at the 2nd-3rd concurrent
> researcher — or the 1st thorough one.** `hit()` returns `hits <= limit`
> (`ratelimit.py:109`), so the wall lands on request 11 per worker.

Two measured aggravations, both of which I confirmed in the code and both of
which make the *first* impression the worst one:

- **A failed analysis still consumes quota.** The `anon_rate_limit` decorator is
  outermost (`routes.py:550-555`), so it meters before the route body can return
  500. A user with a malformed PDB burns allowance on errors and then meets the
  wall.
- **A dropped SSE stream doubles the CPU.** `/analyze` re-runs `run_pipeline`
  when `results.csv` is absent (`routes.py:577-580`). On the happy path
  `/progress` already wrote it; if the stream is cut — proxy timeout, tab close,
  flaky campus wifi — the pipeline runs a second time at full cost.

### Is one attacker still bounded?

**Today: yes, but by accident, and with one unresolved hole.**

Bounded at ~20 requests per 10 min per IP x ~13-18 CPU-s worst case ≈ 260-360
CPU-s against ~1,200 available — roughly 15-30% of the box per IP. That bound
comes from the sync worker model and the 8 MB cap, not from a deliberate CPU
budget.

The hole is §4.4's unresolved question: **if Railway's edge forwards a
client-supplied `X-Forwarded-For` verbatim, the per-IP key is caller-chosen and
the bound is zero.** `_client_ip()` takes `chain[len - hops]` with `hops = 1`
(`shared/metrics.py:204-212`), which is correct if the edge appends or
overwrites and bypassable if it forwards. The builder is right that this is the
single blocking unknown, and right not to have resolved it destructively.

**After Phase 4 as currently recommended: no.**

> **First place it breaks: Phase 4's ceiling, if it is set from typical cost.**
> At the recommended ~1,000 per IP per 10 minutes, one IP can demand ~18,000
> CPU-s against a ~1,200 CPU-s budget — ~15x oversubscription, achieved by
> uploading large structures, needing no cookies, no account and no distributed
> source. That is not a bounded attacker; it is one IP denying service to
> everyone else, which is the exact failure the limiter exists to prevent.

---

## 8. What Phase 1 and Phase 4 may build on

### Build on these — I reproduced them independently

| # | Number | Where | Reproduced |
|---|---|---|---|
| 1 | Typical pipeline CPU ~0.06-0.27 CPU-s (dev) | §1.2 | yes, within 1.2-1.6x, twice |
| 2 | `fetch_known_binders`: 41 requests, peak **42** threads, ~1.6-1.9 CPU-s, **0** binders | §1.4 | yes, exactly |
| 3 | RCSB leg returns exactly 40 ids; only the SAbDab leg fails | §1.4 | yes |
| 4 | **2 metered hits per analysis**; `/`, `/quota`, `/pdb`, `/download` unmetered | §2.2 | yes, by decorator inspection |
| 5 | `WEB_CONCURRENCY`=2, sync workers, no `worker_class`, no `threads` | ground truth | yes |
| 6 | Failed analyses consume quota; dropped SSE doubles CPU | §2.2 | yes, in code |
| 7 | **gthread is thread-safe** for this path (§5.4 correctness half) | §5.4 | yes, by AST + mutation grep |
| 8 | 1.39x wall / 1.45x CPU / 1.07 cores | §5.3 | arithmetic verified (load test not re-run) |
| 9 | 8 MB cap enforced on **both** intake paths (`routes.py:241`, `:439`) | §1.3 | yes — cap is not bypassable via fetch-pdb |
| 10 | `_WINDOWS` eviction is lowest-hit-count first, ties by soonest expiry | H8 | yes (`ratelimit.py:98-100`) |
| 11 | `'rate_limited'` reason code unused (`0015_signup_rejections.sql:28`) | §6 | yes |

### Re-measure or correct before use — these are blocking

| # | Number | Problem | Action |
|---|---|---|---|
| A | **"SAbDab is dead — delete it"** | False. Live API at `sabdab.opig.stats.ox.ac.uk/api`, 21,914 rows in 1 request | **Repoint the URL. Do not delete.** Add a test that fails on 0 binders for a known positive |
| B | **Worst case "~5.12 CPU-s"** | I measure **~9.0** at the cap boundary; §1.6's "~7" contradicts §1.3's own label | Re-measure at the 8,192 KB boundary; use worst case, not typical, for Phase 4 |
| C | **"10-15x lower than the plan"** | Conflates typical with worst case. Typical ~10-100x lower; **worst case within ~2x** | Rewrite the headline before anyone sizes on it |
| D | **NAT ~300; range 100-1,000; "use ~1,000"** | Uncited staff addend; 1 pool as denominator against 4 pools of egress; and it is not the quantity that sizes a 600 s window | Do not use as a ceiling. Derive from capacity ÷ worst-case cost (**O(100)**), then run the `user_events` query |
| E | **"45 threads per request", 720 OS threads** | Fan-outs are sequential (`epitope_db.py:645-647`); measured peak is 42, contradicting the builder's own §1.4 | Use ~42 and **672** |
| F | **"No tempfile/chdir/environ writes anywhere in `scout/` or `shared/`"** | `shared/pdb_preflight.py:36, 516` | Restate as "none on the Scout request path" |
| G | **§4.4 probe procedure** | Targets `/scout/`, which is unmetered and never calls `_client_ip()`; would log nothing. Also logs PII per request | Use `/scout/example`; log once behind a flag |
| H | Production ~2x dev-box factor | Calibrated from **intake wall time only** (~14 ms), extrapolated to analyse CPU | Honest and disclosed by the builder (§7 item 5), but it multiplies every production figure. Confirm before Phase 4 |

### Not independently reproduced

- **§4.2, the socket peer** (`205.210.104.226` seen as `152.233.47.65`,
  Datacamp/CDN77, `x-railway-edge: jfk1`). Needs authenticated Railway log
  access. Method reviewed and judged sound; **accepted, not re-derived**; n=1.
- **§5.1's 24-thread determinism run** and **§5.3's serial-vs-8-permit timing**.
  I verified the arithmetic and reached the same SAFE verdict by a different
  (structural) route, but did not re-run the load harness.
- **arXiv "50-100 subscribers" floor.** Paper and title confirmed real and
  consistent; that specific figure is not in the abstract.

---

## 9. Verdict

**PASS WITH CORRECTIONS.**

Phase 0 was asked for four numbers and how each was obtained. It delivered all
four with genuine provenance, and I reproduced three of them on identical
hardware with identical pins — which is more than most rounds in this repo can
claim. The `NOT MEASURED` register is honest and unusually complete, the
thread-safety verdict is correct, the metering model is correct, and the §4.3
judgement that a hardcoded CIDR allowlist is an outage risk is the best call in
the document.

It does not pass clean because it contains one recommendation that would delete a
working feature on a misdiagnosis (A), one number that mis-sizes the very phase
it exists to size (B/C/D), and one handoff procedure that cannot work as written
(G). Those are failures of **inference**, not of measurement — which is why the
verdict is not FAIL: the underlying measurement work is sound and re-running it
would waste the effort. But A, B, C, D and G must be corrected **in the document**
before Phase 1 or Phase 4 consumes it, because the whole point of Phase 0 is that
later phases will not recheck these numbers.

**Blocking for Phase 1:** A (delete vs repoint — it changes what Phase 1's
"bound the fan-out" step even is), E, F.
**Blocking for Phase 4:** B, C, D — and the arithmetic in §7, which is the
sizing calculation this document has all the inputs for and does not perform.
**Blocking for Phase 2:** G, plus §4.4's unknown, which still needs the owner's
go-ahead.

---

*Reviewer note: no application code was modified. Measurement harnesses were
written to a scratch directory and not committed. The only production contact was
a public-endpoint header read; no forged headers were sent, no rate-limit bucket
was consumed, and no load test was run against production.*
