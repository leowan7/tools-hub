# Decision: what actually gates raising the anonymous per-IP ceiling

**Reviewed at:** `d3c60c8` (= origin/main at the time of writing).
**Measured first-hand**, not quoted: see "Evidence" below.

> **RE-TAKEN 2026-08-24 — read ["Re-taken"](#re-taken-2026-08-24--the-trigger-this-document-set-has-fired) at the bottom first.**
> Unknown 2 (what Railway's edge does with `X-Forwarded-For`) is ANSWERED and
> the per-IP key is FIXED, so the ceiling this document is about now binds for
> the first time. **The conclusion below is unchanged — do not raise the
> ceiling — but §4's CPU reasoning was carrying a caveat saying it was
> protecting a control that does not run, and that caveat is now removed.**
> **One sentence IS retracted:** §4a's reason 2 ("not every attacker forges the
> header — the naive ones do not, and the ceiling stops them") is now known
> false. The ceiling stopped nobody, naive or competent. Everything else in
> §§1-5 stands as written.

Two documents disagree about what must be true before `ANON_ANALYZE_LIMIT`
can be raised. This resolves the disagreement, and then reports a third
finding that makes the disagreement largely moot.

---

## 1. The contradiction

**`scout/routes.py:160-175`** names two preconditions for raising the number:

> 1. PHASE 2 [X-Forwarded-For trust] ... 2. PHASE 1'S FAIRNESS IS UNDELIVERED
>    ... The plan says "Phase 1 is what makes generous safe"; it did not.

**`docs/HANDOFF-2026-08-18-anon-rate-limiting.md`, section "What Phase 4 must
NOT do"** says the opposite:

> Phase 4 must not cite Phase 1 as its safety argument. Phase 0's budget is
> CPU *time*, not concurrency, so neither the semaphore nor its absence makes
> a loosened per-IP ceiling safe.

**Correction, 2026-08-22:** framing this as "two documents disagree" was
itself wrong, and an independent QC round caught it. The handoff is on BOTH
sides — `HANDOFF…:307` says "Phase 1 is what makes 'generous' safe", the exact
claim its own "What Phase 4 must NOT do" section forbids Phase 4 from relying
on. The two sentences have coexisted since the document was written, which is
precisely how `scout/routes.py` came to cite one while the plan forbade the
citation. So the disagreement is INTERNAL to the plan, and the code comment
inherited the losing half. `:307` now carries a supersession note.

## 2. Resolution: the plan's "must NOT do" section is right, its Phase 4 section and the code comment are wrong

A semaphore bounds how many requests run **at once**. The per-IP ceiling
bounds how much total CPU one address can **demand inside a window**. These
are different quantities, and neither converts into the other.

If N addresses each demand D CPU-seconds within a 600 s window, the fleet
must supply N x D CPU-seconds no matter how many of those requests are in
flight simultaneously. A concurrency cap changes the **queueing discipline**,
not the **arithmetic** — the same work arrives, it merely arrives in a
different order.

Therefore Phase 1 cannot make a raised ceiling safe, and its absence cannot
make a raised ceiling unsafe. **The number must be sized against the CPU
budget, and against nothing else.** Precondition 2, as written, is a
non-sequitur.

## 3. But precondition 2 is misfiled, not worthless — DEMOTE it, do not delete it

Phase 1's fairness governs the **consequence** of saturation, never its
**threshold**:

| | over-budget window WITHOUT Phase 1 (today) | WITH Phase 1 |
|---|---|---|
| what happens | queues invisibly in the kernel accept backlog | sheds at the queue ceiling |
| `/healthz` | queued behind anonymous compute | still answers |
| how it presents | a total outage | some anonymous 503s |

So the honest one-line statement is: **Phase 1 does not change *when* you
saturate, it changes *what saturation looks like*.** Sizing the ceiling is a
CPU-budget question. Phase 1 is a blast-radius question. The code comment
conflates them by calling the second a precondition for the first.

The defensible policy that keeps both facts:

- Size the ceiling so **expected** fleet demand stays inside budget — that is
  purely Phase 0 arithmetic, and Phase 1 is irrelevant to it.
- Require Phase 1 before accepting any ceiling whose **worst case** exceeds
  budget, because that is the case where the difference between "sheds" and
  "goes down" is the whole outcome.

**Precondition 1 (Phase 2) is untouched and remains a genuine hard gate.** If
the per-IP key is caller-chosen, there is no ceiling to size and no
arithmetic to do. Re-measured first-hand at `d3c60c8`, 50 requests to
`GET /scout/example` from one socket peer, distinct anon sessions, job dirs
cleared between runs so the live-job cap could not confound it:

| X-Forwarded-For | sent | admitted | refused | distinct buckets |
|---|---|---|---|---|
| rotating (`203.0.113.i`) | 50 | **50** | **0** | 50 |
| fixed (`203.0.113.9`) | 50 | 10 | 40 `rate_limited` | 1 |
| absent (socket peer) | 50 | 10 | 40 `rate_limited` | 1 |

**This result is CONDITIONAL, and the condition is the very thing Phase 2 has
not settled.** `_client_ip()` counts hops from the RIGHT with
`TRUSTED_PROXY_HOPS=1`, so a forged value only wins when the app *receives* a
single-value header — which is the third edge behaviour, "forwards verbatim".
The probe above simulates exactly that case and nothing else. Under the other
two the same 50 requests are bounded:

| what the app receives | edge behaviour | sent | admitted |
|---|---|---|---|
| `<forged>` alone | forwards verbatim | 50 | **50** |
| `<forged>, <real client>` | appends | 50 | 10 |
| `<real client>` alone | overwrites | 50 | 10 |

So "the ceiling is bypassed" is true in **one of three** cases and false in
the other two. An earlier draft of this document stated it unconditionally and
used it as the evidence for "Phase 2 is the whole gate" — which is circular,
because it assumed the worst-case answer to the question Phase 2 exists to
ask. Corrected 2026-08-22 after an independent QC round ran the other two
cases.

**What survives the correction, and it is enough:** nobody knows which of the
three holds, so nobody can say today whether the ceiling bounds an attacker at
all. A control whose effectiveness is unknown cannot be sized, and that —
not a measured bypass — is why Phase 2 gates the ceiling.

`gthread`'s cost is real and was re-confirmed here first-hand: gunicorn
24.1.1 `workers/gthread.py` `run()` calls `self.notify()` unconditionally at
the top of the accept loop, **before** the `can_accept` check, so a worker
with every thread wedged on a request heartbeats forever and the arbiter
never kills it. The watchdog is genuinely gone, not merely weakened.

---

## 4. The finding that makes it moot: the ceiling cannot be raised at all today

The arithmetic in the brief that commissioned this work — "~180 CPU-s per IP
per window, ~7 addresses saturate the fleet" — is **stale**. It is the
pre-`#171` number, and it survives in the doc's "What Phase 4 must NOT do"
section. `scout/routes.py` carries it explicitly as the "before:" row and
states the current one directly beneath:

    before: 20 hits/IP fleet-wide, all aimed at /progress = ~180 CPU-s/IP,
            so ~7 addresses saturate the fleet, and 5 analyses per worker.
    now:    20 hits/IP fleet-wide, each buying a whole analysis = ~300
            CPU-s/IP, so ~4 addresses saturate it, and 10 per worker.

Budget is 2 sync workers x 600 s = **1,200 CPU-s** per window. At the current
ceiling of 10 the worst-case per-IP demand is already ~300 CPU-s, so **four
addresses saturate the fleet** — four *distinct* addresses, which is what this
whole section assumes and what section 4a questions: if the edge forwards
`X-Forwarded-For` verbatim then one machine supplies all four and the
threshold is not a threshold at all. Under an appending or overwriting edge
the number is literal. Raising the ceiling reduces that
number **linearly**: at 20 it is two addresses.

The ceiling is therefore not "not yet generous". It is **already above what
the CPU budget can honour under adversarial load**, and the requested change
moves it in the wrong direction.

This is the plan's own insight biting: the per-IP count is a proxy for CPU,
and a bad one. At ceiling C, a real lab spends ~2 CPU-s per analysis and an
attacker ~15. The same C that is stingy for the lab is generous for the
attacker, and no single value of C is both.

**Consequence: `ANON_ANALYZE_LIMIT` is not the lever.** The levers that
actually exist are, in ascending cost:

1. ~~**`WEB_CONCURRENCY`** — raise the CPU budget.~~ **STRUCK 2026-08-22: this
   is not a lever.** It was listed first here, with a parenthetical noticing
   that it raises the ceiling and the budget in the same proportion — which is
   precisely why it cannot move the number this section is about. Writing it
   out for `W` workers and per-worker ceiling `C`:

       addresses to saturate = (W x 600) / (W x C x 15) = 40 / C

   **`W` cancels.** Four addresses at `C=10` whether the fleet runs 1 worker or
   8. The limit is per worker, so more workers hand each address proportionally
   more quota, and the extra budget is spent buying the attacker the extra
   capacity. Worse if workers exceed the vCPU allocation — Railway's is an
   explicit unknown at `docs/qc/anon-load-baseline.md:544` — because then the
   budget grows slower than `W` while the quota grows exactly with it, and
   raising `W` *lowers* the saturation threshold.

   **What it IS good for, and section 5 is the reason:** it is the only single
   knob that lifts the intake and analyze walls TOGETHER, in proportion, with
   no code change — the per-worker limits both multiply by `W`. Measured on a
   round-robin fleet: `W` = 1 / 2 / 3 admits 10 / 20 / 30 intakes from one
   address before refusing. Section 5's problem is that the two constants bind
   different lab shapes and moving one without the other helps only half the
   users; `WEB_CONCURRENCY` moves both at once.

   **It is NOT attacker-neutral, and saying so would contradict the paragraph
   above.** The attacker's quota multiplies by `W` exactly as the lab's does.
   What is true is narrower: raising `W` leaves the saturation threshold
   UNCHANGED at `40/C` *when vCPU scales with `W`*, and LOWERS it when it does
   not — and Railway's vCPU allocation is an explicit unknown
   (`docs/qc/anon-load-baseline.md:544`). So it buys the lab real headroom at
   best-case attacker-neutrality, on an unverified premise. That is a genuine
   lever with a genuine cost, not a free one. Listing it under "levers that
   raise the CPU budget" was the error — it raises budget and quota together.
2. **Cut the ~15 CPU-s adversarial cost.** `#180` (per-chain secondary
   structure) has almost certainly already reduced it; the figure has not
   been re-measured since. See "unknowns".
3. **Phase 1**, so that exceeding the budget degrades instead of collapsing —
   which is what would make a worst-case-over-budget ceiling acceptable at all.
4. **Sign-in**, which Phase 5 already routes refused users toward and which
   bypasses both tiers. For a NAT lab this is the only mechanism that
   distinguishes fifty researchers from one attacker, because the per-IP key
   provably cannot.

### 4a. The strongest argument AGAINST section 4, stated fairly

The measurement above licenses a real counter-argument, and it deserves a
hearing rather than a footnote:

> The ceiling binds only callers who do NOT forge `X-Forwarded-For` — that
> is, honest users. An attacker rotates the header and is unbounded today.
> So the ceiling currently delivers approximately zero protection and one
> hundred percent of the pain. Raising it costs nothing that is not already
> lost, and helps every lab immediately.

**Its premise holds in at most one of three cases** — see the correction under
the measurement above. If Railway's edge appends or overwrites, the rotating
attacker is bounded at 10 like everyone else and this argument has no factual
basis at all. It is worth stating anyway, because if the edge *does* forward
verbatim then it is correct and section 4's CPU arithmetic is conditional on a
ceiling that does not bind.

Even granting its premise, it is rejected, for three reasons:

1. **The same reasoning removes the limiter entirely**, which is plainly
   wrong. Any argument whose logic extends to "so delete it" has proved too
   much.
2. **Not every attacker forges the header.** The naive ones do not, and the
   ceiling stops them. "Zero protection" is measured against the competent
   attacker only.
3. **Timing.** Phase 2 makes the ceiling real. Raising the number *before*
   Phase 2 and landing Phase 2 *after* means shipping a genuine hole and then
   switching it on. Do not loosen a control in the same window in which you
   are making it effective.

The synthesis: the ceiling's current protection value is low and its cost to
labs is real, so **the answer is to fix the bypass, not to widen the gap.**
After Phase 2 the ceiling binds everyone, its true cost becomes measurable
for the first time, and only then can it be sized honestly. Sizing it now
means sizing a control whose current behaviour nobody can observe.

## 5. Second finding: raising the analyze ceiling alone would not unblock a lab

Measured below: `ANON_INTAKE_LIMIT` and `ANON_ANALYZE_LIMIT` are both 10, and
which one binds depends on the shape of the lab, not on its size:

- **Many researchers, one structure and one chain each** — **intake** binds,
  at the 11th structure, on `GET /scout/example`, before an analysis is ever
  reached.
- **Few researchers, many chains each** — **analyze** binds, at the 11th
  analysis.

So raising only `ANON_ANALYZE_LIMIT` serves the second lab and leaves the
first refused at exactly the same point as today. The two constants have to
move together, or the change buys nothing for half the users it targets. The
`10 == 10` balance is documented in `scout/routes.py` as ACCIDENTAL and
nothing asserts it.

---

## Evidence — measured at `d3c60c8`, single worker, real Flask test client

The meter runs ahead of the view, so a nonexistent job exercises the charging
arithmetic exactly as a real one does. No pipeline was run; nothing here
claims a CPU number.

| Probe | Workload, one NAT egress IP | Result |
|---|---|---|
| 1 | one analysis (progress then analyze) | **1** per-IP charge, not 2 |
| A | 6 researchers x 1 chain | 6/6 admitted, no refusals |
| B | 6 researchers x 2 chains | 10/12 admitted; first refusal r6c1, `rate_limited` |
| C | 6 researchers x 3 chains | 10/18 admitted; first refusal r4c2, `rate_limited` |
| D | 1 researcher x 12 chains | 8/12 admitted; first refusal at 9, `session_rate_limited` |
| E | intake, distinct sessions | refused at the **11th** structure, 429 `rate_limited` |

These are per-worker figures; production runs 2 workers with per-worker state,
so fleet-wide capacity is roughly double and which worker a request lands on
is arbitrary.

**Correction to the commissioning brief.** It states one analysis costs 2
metered hits, `/progress` opening the pair and `/analyze` closing it. The
pairing is described correctly but the arithmetic is inverted: the pair
exists **precisely so that** the two requests share ONE charge, and probe 1
measures 1. `scout/ratelimit.py`'s own module docstring says so ("a
legitimate analysis costs 1 instead of 2"). The brief's "#4 is refused on
their second chain" is nonetheless reproducible — probe C — but it is the
**11th analysis** at 3 chains each, not the 8th at 2. The binding constant is
the same either way, so the brief's conclusion survives its arithmetic.

**A first attempt at probe B was invalid, and is reported here because the
failure is instructive.** Bare test clients carry no `scout_anon_id`, so all
six callers landed in the shared `anon:no-session` bucket and were refused
with reason `no_session` — a measurement of the cookieless path, not of six
researchers. It read as a plausible 8/12 with a refusal at researcher 5.
Only the reason code distinguished it from a real result, which is Phase 6's
argument in miniature: a refusal count without a reason label is not evidence.

## Unknowns this decision does NOT resolve

1. **The ~15 CPU-s adversarial figure has not been re-measured since `#180`.**
   That commit scoped secondary-structure assignment to the chain being
   scored, which is a straight reduction in pipeline CPU. Every saturation
   number above inherits the old figure and is therefore **pessimistic by an
   unknown margin**. `freesasa` is not installed on the Windows dev box
   (`tests/test_scout_anonymous_access.py` documents why), so this has to be
   measured in the container. Do that before acting on lever 1 or 2.
2. **Whether Railway's edge appends, overwrites, or forwards
   `X-Forwarded-For` verbatim.** Phase 2. Unchanged, still unverified, still
   the hard gate. See the next section — this one is actionable now.
   **ANSWERED 2026-08-24. It is the OVERWRITE case below** — the edge discards
   everything the caller sends — **with a wrinkle the table does not have: it
   then appends its own internal hop, and that hop rotates.** So one-hop
   resolution keyed on a rotating edge address and the per-IP tier never
   refused anyone. The ceiling this document is about did not operate at all.
   The conclusion here stands; the CPU-budget ground does not, because it was
   protecting a control that does not run. **A fix exists and is landing** —
   keying on `X-Real-Ip` — after which this ceiling binds for the FIRST time
   and the "six researchers behind one NAT" case becomes reachable rather than
   theoretical. Re-read this decision then. See
   [`MEASUREMENT-2026-08-24-per-ip-key-is-not-stable.md`](MEASUREMENT-2026-08-24-per-ip-key-is-not-stable.md).
3. **Real refusal-by-reason rates in production.** That is Phase 6, and it is
   why Phase 6 goes first. Every number above is a simulation.

---

## What Phase 2 needs before it can be built at all

Phase 2 as the plan states it — "honour `X-Forwarded-For` only when the socket
peer is Railway's edge; otherwise use the peer address" — **cannot be
implemented against an unknown edge**, and the gap is not a detail:

- If the edge **overwrites** the header, peer-based trust is correct and easy.
- If the edge **appends**, peer-based trust plus a hop count is correct.
- If the edge **forwards the client's header verbatim**, peer-based trust
  does not help. The header is attacker-chosen and the edge never corrects
  it, and the only unforgeable alternative — `request.remote_addr` — is the
  shared edge PoP, identical for every visitor on earth. **In that third case
  there is no per-IP key at all**, and the honest response is to stop
  pretending there is one and lean on the per-session tier plus sign-in.

Building Phase 2 without knowing which of the three holds means shipping a
control that is correct under two of three cases and silently inert under the
third — which is the exact defect Phase 2 was created to remove, reintroduced
one level up.

**The probe that settles it.** One request to production carrying a
distinguishable multi-value header, e.g.

    X-Forwarded-For: 192.0.2.111, 192.0.2.222

and one observation of what the app resolves `_client_ip()` to:

| app sees | edge behaviour | Phase 2 |
|---|---|---|
| `192.0.2.222` | appends (our value is not last) | hop count, as today |
| the real client address | overwrites | peer trust, straightforward |
| `192.0.2.111` or `192.0.2.222` with no real address anywhere | forwards verbatim | **no usable per-IP key exists** |

It costs a small number of anonymous bucket charges against production and it
needs somewhere to read the resolved value. **Phase 6 makes that cheaper than
it was**: `/metrics` is now reachable with a token, so a refusal counter keyed
by reason is observable without shipping a debug endpoint — send enough
requests with a FIXED forged header to cross `ANON_INTAKE_LIMIT` and watch
whether `tools_hub_scout_refusals_total{reason="rate_limited"}` moves. If it
does, the forged value is the key and the edge is not overwriting it. If it
does not, the edge is replacing the header and the ceiling is real.

Run that before writing a line of Phase 2.

---

## Re-taken 2026-08-24 — the trigger this document set has fired

Unknown 2 above says to re-read this decision once the per-IP key is fixed.
It is fixed: `#189`, main `237fbf3`. Two independent instruments confirm it:

- **the wall itself** — 26 anonymous `POST /scout/upload` against production,
  20 admitted, refused at request 21 with `reason="rate_limited"`.
- **the key's own source** — `#192` added
  `tools_hub_client_ip_source_total{source}`; against prod at `05bce72` it read
  `x_real_ip 100.0%`, i.e. the limiter resolves its key the way `#189` claims,
  not merely walling for some other reason. **n = 3 resolutions.** Thin, but it
  is a different question from the one the wall answers.

**The before/after probes are NOT a controlled A/B, and it matters.** The
pre-fix run (46 requests, 0 refused) carried a constant forged
`X-Forwarded-For`; the post-fix run carried none and relied on the edge, which
is the production path. `MEASUREMENT-2026-08-24-per-ip-key-is-not-stable.md`
flags this about itself. They are two probes of a changed system, not one probe
run twice.

Sub-sections below are numbered **R1-R7** because this document already has a
§1-§5; a bare "§4" always means the original.

### R1. The conclusion survives, and §4's arithmetic now describes production

§4 concluded the ceiling cannot be raised, because at `C = 10` four addresses
already saturate the CPU budget. Unknown 2's answer then undercut the ground:
*"the CPU-budget ground does not stand, because it was protecting a control
that does not run."* **That caveat is removed.** The arithmetic never changed —

    addresses to saturate = (W x 600) / (W x C x 15) = 40 / C

`W` still cancels, and it is four addresses at `C = 10`.

**This is the ADVERSARIAL number, and R3's charge inflation does not touch it.**
That is not a coincidence and it is worth stating: the inflation in R3 comes
from losing a pairing CREDIT, and an attacker driving `/scout/analyze` without
opening a pair never had a credit to lose. The number that sizes the ceiling
against abuse is therefore unaffected. The number that describes a real lab is
not.

**One caveat before citing `40 / C` unhedged.** The denominator is `W x C x 15`
*because the limit is per worker*. Under Phase 3 the limit stops being
per-worker and the formula becomes `40W / C`. A reader who applies the
per-worker form to the Phase 3 end state recommended in R6 computes
`40 / 20 = 2` addresses and concludes the paired change halves the saturation
threshold. The correct value is `40 x 2 / 20 = 4` — unchanged. Always carry the
form with the counter model it belongs to.

### R2. What changed, at the strength the measurement actually supports

**Not "the ceiling bounded nobody at all".** The key was a rotating internal
edge hop drawn from a POOL, and the measurement doc is explicit that this is a
pool rather than a fresh value per request — and equally explicit that *"the
pool's true size was never measured."* Three hops were observed from one
runner:

| | before `#189` | after `#189` |
|---|---|---|
| what bounded a single machine | an unmeasured multiple of the wall — ~3 pool addresses observed, so a ceiling near **60** intake requests | **20** |
| bounded by the caller's own identity | no | yes |

So the bound was roughly **3x looser, not absent.** And whether one machine
could saturate the fleet was **never established**: saturation needs ~80
analyses (1,200 CPU-s / 15), and 60 < 80 at the observed pool size. An earlier
draft of this section claimed "one machine saturated the fleet, now it takes
four addresses." That was wrong in the first half and is withdrawn. **The
defensible delta is that a caller is now bounded by its own identity for the
first time** — which is the qualitative change worth having, independent of the
factor.

**§4a's premise was HALF true, and the missing half is the interesting one.**
Its premise was *"the ceiling currently delivers approximately zero protection
and one hundred percent of the pain."* Zero protection: correct. One hundred
percent of the pain: **wrong** — the tier refused nobody, honest users
included, so it delivered no pain either. A control that buckets nobody is not
a control that hurts only the innocent; it is simply not running.

**§4a's three reasons, re-read.** §4a was rejected for three reasons:

1. *proves too much* — stands.
2. *"not every attacker forges the header. The naive ones do not, and the
   ceiling stops them."* — **RETRACTED.** The ceiling stopped nobody. This is
   the one sentence in §§1-5 that this re-take withdraws.
3. *timing — do not loosen a control in the same window in which you are
   making it effective.* — **this was the operative one.** Its stated mechanism
   was wrong (it named a forged-header bypass; the real cause was a rotating
   internal hop), but its decision rule is precisely the sequence that then
   occurred. An earlier draft said "none of the three was operative." That was
   wrong and is withdrawn.

### R3. The lab numbers: the wall is 20 CHARGES, which is 10-20 ANALYSES

The Evidence table was measured on a single worker, and production runs
`WEB_CONCURRENCY = 2`, so the fleet wall for one address is `limit x workers`
= **20 charges** per 600 s. Converting charges to analyses is where an earlier
draft of this section went wrong, and the reason is in `scout/ratelimit.py`'s
own docstring:

> PHASE 3 WILL CHANGE THIS. When the window counters move to shared, durable
> state, `_FOLLOWUP` MUST MOVE WITH THEM. It is per-worker for exactly the same
> reason the counters are [...] a credit granted on worker 1 is invisible to
> worker 2, so a `/analyze` that lands on the other worker is charged.

`/scout/progress` opens the pair and `/scout/analyze` closes it, and the credit
that makes an analysis cost one charge instead of two **lives in process
memory, like the counters.** So at fleet scale:

| paired requests land | charges per analysis | analyses inside a 20-charge wall |
|---|---|---|
| on the same worker | 1 | 20 |
| split across workers | 2 | 10 |

**The split probability is unmeasured**, and there is a reason to think it is
not 50/50: a sync worker is pinned for the whole `/progress` SSE stream, so the
paired `/analyze` arrives while that worker is busy and is more likely to be
taken by the other one. That biases toward 2 charges, i.e. toward the bottom of
the band.

**Consequences, replacing the lab table an earlier draft of this section
carried.** Quote lab capacity as a BAND, in analyses, and note that intake is a
separate bucket with its own 20-charge wall:

| lab shape, one NAT address | analyses | charges (1x - 2x) | vs a 20-charge wall |
|---|---|---|---|
| 6 researchers x 1 chain | 6 | 6 - 12 | fits either way |
| 6 researchers x 2 chains | 12 | 12 - 24 | fits at best, over at worst |
| 6 researchers x 3 chains | 18 | 18 - 36 | over the wall unless the pairing holds |
| 1 researcher x 12 chains | 12 | 12 - 24 | the SESSION tier's wall is `8 x 2 = 16` charges fleet-wide, so this fits at best and binds at worst |

**The claim "the commissioning brief's 'researcher #4 is refused on their
second chain' does not happen in production" is WITHDRAWN.** At the pessimistic
end of the band the fleet behaves exactly like the single-worker probe that
produced that finding, so it is not excluded — merely less likely, by an
unmeasured amount.

### R4. Per-process state taxes honest users specifically

Two claims here; the first is derivable and the second is not, so only the
first is made.

**Derivable: the pairing credit is the thing that degrades, and only honest
users hold one.** An attacker driving `/scout/analyze` directly never opens a
pair, so pays exactly one charge per analysis whatever the fleet does. A real
lab uses the paired flow and pays between one and two. **Per-process state
therefore costs legitimate users up to 2x of their quota and costs an attacker
nothing.** That is the argument R5 rests on, and it needs no assumption about
how requests are paced.

**Also true, and already on record:** which worker a request lands on is
arbitrary, so two independent counters need not fill evenly and the first
refusal can fall anywhere from request 11 to request 21. This is not a new
finding — `docs/qc/anon-ratelimit-phase-0.md:612` records the fleet figure as
holding *"only if requests distribute evenly across workers — they need not"*,
and `ratelimit.py` says a caller's requests "land on whichever worker is free."

**NOT claimed, and an earlier draft did claim it:** that a 48-request
cookieless probe showing refusals from request 10 is a second reading of this
wall. It is not. It drove the **session** tier (`anon:no-session`, limit 8,
reason `no_session`), which is a different bucket with a different key — and a
GLOBALLY SHARED one that any other cookieless caller charges concurrently, so
its interleaving has an alternative explanation that was never excluded. No
pacing was recorded for either probe, so "one was sequential, the other was
issued fast" was invented to explain the difference. A real lab carries
cookies and never enters that bucket at all.

### R5. The coupling: each change is UNDESIRABLE alone, for OPPOSITE reasons

This is the finding that most changes what to do next. The word is
*undesirable*, not *unsafe*, and the distinction is the point.

| | fleet wall (charges) | adversarial addresses to saturate |
|---|---|---|
| today (per-process, `C = 10`, `W = 2`) | 20 | 4 |
| **Phase 3 alone, constants untouched** | **10** | **8** |
| Phase 3 **and** both constants at 20 | 20 | 4 |
| **constants at 20 alone, no Phase 3** | **40** | **2** |

- **Phase 3 alone is strictly SAFER and merely less generous.** Per-address
  adversarial demand falls to `10 x 15 = 150` CPU-s, so the saturation
  threshold RISES from four addresses to eight. What it costs is legitimate
  users: the wall halves from 20 charges to 10. Phase 3 is currently filed
  purely as an attacker-quota fix (criterion 5, "a deploy mid-window must not
  reset a quota"); nobody has written down that it is also a **2x tightening
  on real users**, and whoever picks it up will not discover that from its
  ticket.
- **Raising the constants alone is the actual security regression** — wall 40,
  saturation two addresses. That is §4's standing objection, unchanged.

**Landed together — shared counters plus both constants at 20 — the pair is:**

- **attacker-neutral inside a window**: four addresses to saturate before and
  after (`40W / C` = `40 x 2 / 20` = 4);
- **strictly tighter across windows**: criterion 5 is met, so a deploy stops
  handing an attacker a fresh quota, which today it does;
- **strictly better for a lab**: the wall becomes exact instead of depending on
  where paired requests land, which by R4 is a tax paid only by honest users.

**Phase 3 must move `_FOLLOWUP` with the counters.** `ratelimit.py` already
requires this in bold, and R3 is why: leave the credit in process memory while
the counters become exact and the offset that makes an analysis cost one charge
stops working.

### R6. Recommendation

1. **Do not raise either constant on its own.** §4's objection stands and is
   now properly grounded.
2. **Do not build Phase 3 on its own either** — not because it is unsafe, but
   because it silently halves the wall for legitimate anonymous users.
3. **If either is wanted, land them as one change:** shared counters (including
   `_FOLLOWUP`) plus `ANON_INTAKE_LIMIT = ANON_ANALYZE_LIMIT = 20`. Both
   constants, because §5 shows moving one alone serves only half the lab
   shapes.
4. **Measure unknown 1 first.** It is the only input that could change the
   arithmetic, and it is currently biased toward refusing a raise.
5. **Urgency is low, and this should be said plainly.** Organic anonymous
   Scout traffic has measured **zero** across three readings — the denominator
   read 122 at 06:20 and still 122 at 12:20, six hours later, and the earlier
   readings of 3 and 28 were separate container lifetimes rather than part of
   that run. Every one of them was our own probes. No lab has yet hit this
   wall, because no lab has yet arrived. Sign-in remains the designed escape
   hatch and Phase 5 already routes refused visitors to it.

### R7. What still gates a raise

- **Unknown 1 is UNCHANGED and is now the binding one.** The ~15 CPU-s
  adversarial cost per analysis has still not been re-measured since `#180`
  scoped secondary-structure assignment to the scored chain. Every saturation
  figure here inherits it, so all of them are **pessimistic by an unknown
  margin — in the direction that would permit a raise.** Measure it in the
  container; `freesasa` is not installed on the Windows dev box.
- **Unknown 2 is ANSWERED** — see the head of this section.
- **Unknown 3 is shipped but still cannot be read.** Phase 6 landed the
  refusal-by-reason counters, but with zero organic anonymous traffic and a
  50-sample floor the alarm normally SKIPs, so there is still no production
  refusal data. **This decision is still taken on simulation**, exactly as the
  original was.
- **NEW: the charge/analysis split ratio is unmeasured**, and R3's whole band
  hangs on it. It is cheap to measure — drive N paired analyses from one
  address against production and read the per-IP counter's movement against N.
