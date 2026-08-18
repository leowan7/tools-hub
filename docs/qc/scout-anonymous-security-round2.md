# QC round 2 — anonymous Epitope Scout security fixes

**Commit reviewed:** `d5932448f046632369870f4ef9f0b1825b1ff076` (`d593244`)
**Branch:** `feat/scout-anonymous-access` (PR #148)
**Parent:** `a2859f848f928f74c0720390d2335414d38dd7c9` (`a2859f8`)
**Reviewer:** independent QC agent — did not build this change.
**Date:** 2026-08-18
**Method:** two detached worktrees under the session scratchpad, one at each
SHA. The main working tree was never touched (no `add -A`, `commit -a`,
`stash`, `checkout`, or `reset`).

**Verdict: MERGE.** No blocking findings. Six non-blocking findings and one
required operational follow-up are recorded below.

---

## 1. SHA confirmation — PASS

```
$ git ls-remote origin feat/scout-anonymous-access
d5932448f046632369870f4ef9f0b1825b1ff076	refs/heads/feat/scout-anonymous-access
```

The reviewed worktree HEAD equals that SHA exactly. Diff vs parent: 6 files,
+421/−27.

`scout/routes.py` and `gunicorn.conf.py` are **comment-only** — read hunk by
hunk, no executable line changes. All behaviour change is in
`shared/metrics.py` and `scout/ratelimit.py`.

## 2. Suite baseline — PASS (builder's numbers confirmed exactly)

Run from each worktree root, **no path argument**, repo venv interpreter:

```
venv/Scripts/python.exe -m pytest -q
```

| commit | result |
|---|---|
| `a2859f8` (parent) | `4932 passed, 20 skipped in 147.05s` |
| `d593244` (head)   | `4951 passed, 20 skipped in 180.61s` |

**+19 tests, zero failures, zero new skips.** Matches the builder's claim.
No flakes seen; no re-runs needed.

## 3. S1 re-proved end-to-end — PASS (with a topology caveat, see §4)

Not a unit test. A **real werkzeug WSGI server** was bound to a loopback port
with the real `create_app()`, and driven over **real sockets** with
`http.client` so headers pass through genuine WSGI environ folding. Target
was the real rate-limited route `GET /scout/example` (`scout_intake` bucket,
`limit=10`, `window=600s`). 40 requests per scenario; `ratelimit.reset()`
between scenarios.

| # | scenario | header sent | n | 429s |
|---|---|---|---|---|
| A | rotating XFF, **no proxy in front** | `203.0.113.{i}` | 40 | **0** |
| B | fixed XFF (control) | `198.51.100.7` | 40 | **30** |
| C | no XFF at all (socket peer) | — | 40 | **30** |
| D | duplicate XFF header lines | `203.0.113.{i}` + `198.51.100.9` | 40 | **30** |
| E | long forged chain, **rotating last entry** | `10.0.0.1 ×20, 203.0.113.{i}` | 40 | **0** |
| F | long forged chain, **edge appends real client** | `203.0.113.{i} ×30, 198.51.100.11` | 40 | **30** |

Scenario B/C sequences were `..........LLLLLLLL…` — exactly 10 through, then
429 for the rest. The wall is at request 11, confirming `limit=10` is live.

**Scenario F is the production model and it holds.** A caller padding 30
forged entries in front of what the edge appends is still bucketed by the
entry the edge wrote, and is limited on schedule. Under the old leftmost
read this was 0/40. S1 is fixed for any edge that **appends to** or
**overwrites** `X-Forwarded-For`.

Scenarios A and E are the *pass-through* topology — no trusted proxy actually
in front, or an edge that forwards the client's header verbatim. There the
attack still succeeds. This is the limitation the builder disclosed; see §4.

**Verification limit stated plainly:** I could not verify what Railway's edge
actually does to `X-Forwarded-For` from this environment. The correctness of
the fix in production rests on that unverified property. See the follow-up in
§11.

## 4. Adversarial pass on the hop arithmetic — PASS with findings

### 4a. The disclosed limitation is real and correctly described — CONFIRMED

Scenario E above proves it directly: with a 21-entry chain whose **last**
entry rotates, 40/40 requests got through. The builder's description of the
uncovered case ("edge forwards client XFF verbatim without appending") is
accurate and complete for that case.

### 4b. FINDING N1 (non-blocking) — the default fails OPEN with no proxy present

Scenario A, reduced to a unit probe:

```
== 4b. NO proxy in front but default hops=1 (fail open?) ==
  client sends own XFF  -> 'ATTACKER_CHOSEN'  peer=9.9.9.9
```

`TRUSTED_PROXY_HOPS` defaults to `1`, i.e. the code *asserts* a trusted proxy
exists. If the app is ever reached without the edge in front — a directly
reachable origin, a preview/staging deploy, a container port exposed for
debugging — a single-entry forged header is taken at face value and the
caller picks its own bucket. This is a strict improvement on the parent
(spoofable in *every* topology) but it is not fail-closed.

**Cheap hardening, not required for this merge:** honour `X-Forwarded-For`
only when `request.remote_addr` is itself in a trusted-proxy CIDR. That fails
closed in every topology above, including pass-through, and removes the
dependency on knowing the edge's semantics.

### 4c. FINDING N2 (non-blocking) — misconfiguring the hop count UP fails OPEN

Reality modelled as one appending proxy, header `attacker_forged, REALCLIENT`:

```
  hops=1 (correct)      -> 'REALCLIENT'
  hops=2 (TOO HIGH)     -> 'attacker_forged'      <- attacker-controlled
  hops=5 (way too high) -> 'a'                    <- attacker-controlled
  hops=0 (too low)      -> '9.9.9.9'  (socket peer = edge; one shared bucket)
```

Setting the knob too high hands the key back to the caller; too low collapses
everyone into one bucket (fail-closed, over-restrictive). Asymmetric risk, so
the knob wants a comment saying "raising this is a security change". Malformed
values are safe:

```
  hops unset / '' / 'banana' / '1.9' / '0x2'  -> 'REALCLIENT'   (falls back to 1)
  hops='-3'                                   -> '9.9.9.9'      (clamped to 0, closed)
  hops=' 2 ' (spaces)                         -> 'attacker_forged'  (int() strips; = hops=2)
```

### 4d. Duplicate `X-Forwarded-For` header lines — HANDLED CORRECTLY

This was the case flagged as most likely missed. It is not a defect.

WSGI folds repeated headers into one comma-joined `HTTP_X_FORWARDED_FOR` **in
wire order**, so the hop count is computed over the concatenation and the
right-hand end is still the entry the edge contributed. Scenario D proved it
on real sockets: attacker sends `X-Forwarded-For: 203.0.113.{i}` (rotating),
edge sends its own `X-Forwarded-For: 198.51.100.9`, joined to
`"203.0.113.{i}, 198.51.100.9"` → selected `198.51.100.9` → **30/40 limited**,
identical to the fixed-header control. Adding header lines does not shift the
selection.

*Residual, theoretical:* if an edge **prepends** its header line before the
client's, the joined order inverts and the rightmost entry is attacker-chosen
again. This is the same class as the pass-through case in §4a. No proxy I know
of does this, and it is not testable from here.

*Testing limit:* this was verified under the **werkzeug** WSGI server.
gunicorn is POSIX-only and cannot be run on this box. gunicorn documents the
same comma-join, but I did not execute it.

### 4e. Malformed / exotic header shapes — no crash, one cosmetic gap

```
  IPv6 plain             -> '2001:db8::1'          (correct)
  IPv6 bracketed+port    -> '[2001:db8::1]:443'    <- FINDING N3
  RFC7239 style          -> 'for=1.2.3.4;proto=https'
  junk                   -> '###'
  only commas / empty / whitespace -> '9.9.9.9'    (falls back to peer)
  trailing comma / leading comma / tab-separated   -> 'REALCLIENT'
  null byte              -> 'REAL\x00CLIENT'       (no crash)
```

Nothing raises. Junk becomes a junk *bucket key*, which is harmless for rate
limiting (a stable key is all that is needed) and correctly rejected by
`_ip_allowed`'s `ipaddress.ip_address()` parse.

**FINDING N3 (non-blocking):** `host:port` and bracketed IPv6 forms are not
normalised. If an edge ever writes `ip:port`, every request from one client
gets a *different* port and therefore a different bucket key — the limiter
would silently stop limiting. Railway is not known to do this and I could not
test it, but a `.rsplit(":", 1)` / bracket strip would remove the failure mode
cheaply.

### 4f. Long-chain DoS — NOT a DoS

```
      1,000 entries (     7,999 bytes) ->     0.30 ms
    100,000 entries (   799,999 bytes) ->    14.59 ms
  1,000,000 entries ( 7,999,999 bytes) ->   185.08 ms
```

Unreachable in practice: `gunicorn.conf.py`, `Procfile` and `nixpacks.toml`
set no `limit_request_field_size`, so gunicorn's default **8190 bytes per
header field** applies — about 1,000 entries, i.e. the 0.30 ms row. Not
exploitable.

## 5. S2 eviction policy — PASS

Constructed the actual attack against the live module:

```
_MAX_KEYS=20000  _EVICT_BATCH=200
after 11 hits, victim limited? True   table=1

-- spray 25,000 distinct keys (1 hit each) to force eviction --
spray done in 0.23s  table=19801 (cap 20000)
victim still present? True
victim STILL limited? True   <-- must be True

  'clear()' in hit(): False
```

The over-limit entry survives a spray that evicts 5,000+ others. The sort key
is `(hits, expires)` — lowest hit count first, ties by soonest expiry — as
claimed. To evict an 11-hit victim an attacker needs 20,000 keys each at ≥11
hits = **220,000 requests from 20,000 distinct source addresses**, and after
the §3 fix those must be *genuinely distinct source IPs*, not forged headers.
The two fixes compose: S1 is what makes S2's ordering expensive to attack.

**Batch eviction is not a CPU DoS:**

```
-- CPU cost of sustained spray once table is FULL --
  2000 requests against a FULL table: 63.6 ms total, 31.8 us/req  (10 sorts)
```

31.8 µs/request amortised, against a route whose real work is a multi-second
freesasa+numpy pipeline. `_EVICT_BATCH = 200` keeps it to one sort per 200
requests. No amplification.

*Note, not a defect:* expired entries are reaped lazily (only when the table
reaches `_MAX_KEYS`), so idle memory sits at the cap rather than draining.
That is the documented, bounded design.

## 6. Mutation testing — PASS (5/6 caught; 1 gap, non-security)

**My own mutations, not the builder's list.** Every mutation's landing was
verified before drawing a conclusion: SHA-256 of the file before/after plus
`git diff --stat`, and a hard assert that the restore returned the original
hash. Baseline green re-confirmed before and after the run
(`82 passed, 1 skipped`).

| id | file | mutation | in builder's list? | result |
|---|---|---|---|---|
| M1 | `shared/metrics.py` | `chain[max(0,len-hops)]` → `chain[0]` (re-introduce S1) | yes | **RED** — 10 tests |
| M2 | `shared/metrics.py` | drop the `max(0, …)` clamp | yes | **RED** — 1 test |
| M3 | `shared/metrics.py` | drop the empty-entry filter | likely | **RED** — 3 tests |
| M4 | `shared/metrics.py` | drop `max(0, …)` in `_trusted_proxy_hops` | **no** | **GREEN — not caught** |
| M5 | `scout/ratelimit.py` | evict `(expires,hits)` i.e. oldest-first | **no** | **RED** — 1 test |
| M6 | `scout/ratelimit.py` | replace eviction with `_WINDOWS.clear()` | yes | **RED** — 1 test |

M1 kills `test_client_ip_takes_the_rightmost_of_two_entries`,
`…ignores_a_long_spoofed_chain`, `…tolerates_whitespace_and_empty_entries`,
`…honours_a_deeper_trusted_hop_count`, `…ignores_a_malformed_hop_count` and
`test_metrics_allowlist_cannot_be_defeated_by_a_forged_header`.

**M5 and M6 both go red on the same test**,
`TestCounterTableEviction::test_a_limited_key_stays_limited_through_eviction`.
That test is a real guard: it fails when the thing it claims to block is
re-introduced, by both the blunt route (`clear()`) and the subtle one
(oldest-first ordering the source comment explicitly warns against). This is
the failure mode this repo has shipped before; it does not recur here.

**FINDING N4 (non-blocking):** M4 is a coverage gap. Nothing pins the
`max(0, …)` clamp in `_trusted_proxy_hops`, so a negative
`TRUSTED_PROXY_HOPS` is untested. The *behaviour* is safe — §4c shows `-3`
resolves to the socket peer, fail-closed — and unclamped it would raise
`IndexError` rather than leak. One assertion would close it.

## 7. The four inline leftmost-XFF copies — PASS, claim verified independently

`grep -rn "X-Forwarded-For\|remote_addr" --include=*.py` over non-test source
returns exactly the four disclosed sites and nothing else. Each traced to its
sink:

| site | sink | enforcement? |
|---|---|---|
| `blueprints/admin.py:50` | `log_event(ip=…)` — staff audit trail | no |
| `blueprints/auth.py:74` | `log_event(ip=…)` — `login` event | no |
| `blueprints/public.py:254` | `log_event(ip=…)` — `/api/track` behavioural event | no |
| `blueprints/auth.py:146` (`client_ip`) | 4 uses, all `log_event(ip=…)` / `SignupContext.ip` | no |

`SignupContext.ip` checked independently: `grep -n "ctx\.ip" shared/auth.py`
returns **one** line, `shared/auth.py:561`, passing it to
`_insert_user_profile(..., ip=ip)`, which writes it to a column
(`shared/auth.py:602`). `register_user`'s guards are honeypot, signed-timestamp
timing, email-domain classification and purpose — none read `ctx.ip`. The
builder's conclusion is correct.

I also chased the one thing that looked like an IP throttle:
`SIGNUP_REJECTION_REASONS` in `shared/events.py:65` contains `"rate_limited"`.
It is a **dead reason code** — `grep -rn rate_limited` shows the only producers
are `shared/wallet.py:768` (auto-reload cooldown, user-id keyed, unrelated) and
email helpers. No signup path emits it. No IP-based signup throttle exists.

The only *read* of a stored `ip` anywhere is `cron/daily_digest.py:236`, which
lists up to 3 IPs per repeat-signup-attempt in a human-readable digest. Report
content, not enforcement — nothing auto-blocks off it.

**FINDING N5 (non-blocking, pre-existing):** those four sites remain
spoofable, so an attacker can write arbitrary values into the audit trail and
into the daily digest's `ips` field. That degrades forensic quality and could
be used to frame a third-party address in the digest. Not enforcement, not a
regression, but the scope cut should be recorded as "audit data is
attacker-influenced" rather than "harmless".

## 8. `/metrics` allowlist hardening — PASS (negative *and* positive)

The builder's new test only asserts the 403. A gate that always returned False
would pass it, so I checked both directions plus the pre-existing positive
test.

```
METRICS_ALLOWED_CIDR=10.0.0.0/8, TRUSTED_PROXY_HOPS unset

  OK forged leftmost allowlisted, real client outside     -> 403 (want 403)
  OK genuinely allowlisted (edge appended real 10.x)      -> 200 (want 200)
  OK single-entry allowlisted (overwrite semantics)       -> 200 (want 200)
  OK no header at all, peer outside                       -> 403 (want 403)
```

The forged-header bypass (`X-Forwarded-For: 10.1.2.3, 203.0.113.7`) now 403s,
and legitimate allowlisted access still returns 200 under both edge
semantics. Mutation M3 independently confirms the positive path is guarded:
it fails the pre-existing `test_metrics_is_accessible_when_allowlisted`.

## 9. S4 — the inert concurrency cap — PASS

Verified by grep, not by reading the new comment:

- `grep -n "worker_class\|threads" gunicorn.conf.py Procfile nixpacks.toml` →
  matches only inside the new **comment**. No setting anywhere. Workers are
  gunicorn's default **sync** class.
- `grep -in "gevent" requirements.txt Procfile` → **no match**. gevent is not
  installed, so the old docstring's claim was wrong and is now corrected.
- `Procfile: web: gunicorn app:app --bind 0.0.0.0:$PORT --preload` — no
  worker-class flag.

The code is unchanged (`ANON_MAX_CONCURRENT_RUNS = 4` still there); only the
documentation now tells the truth, and says not to cite it as protection.
Correct call — deleting it would remove the right guard for a future worker
class, and changing the worker class is out of scope.

## 10. Goal-level check — the anonymous flow still works, with a real UX cost

Walked it as a first-time visitor over real sockets: no account, no
pre-seeded cookies, browser-like cookie jar.

```
1. GET /scout                 -> 308  (redirect to /scout/, expected)
2. GET /scout/example         -> 200
   job_id=2590448e-…  chains=['A']  title='Hen Egg White Lysozyme'
3. POST /scout/analyze        -> 500  ModuleNotFoundError: freesasa
4. GET /scout/progress (SSE)  -> 200  data: {"stage":"parsing","pct":10,…}
                                      data: {"stage":"error","msg":"No module named 'freesasa'"}
5. GET /scout/download/{job}  -> 404  "Results not found. Please run analysis first."
```

Intake works anonymously end to end: the example structure loads, chains and
the structure title come back, and the SSE stream renders progress. Steps 3–5
fail **only because `freesasa` is not installed on this Windows box** — an
environment limit, not a code defect. I could not execute the real scoring
compute locally and am not claiming it works; the route layer around it
(session minting, ownership, rate limit, compute slot, SSE framing, error
rendering) is exercised and behaves. Note the SSE error path degrades
gracefully inline, which is the behaviour the limiter's `sse=True` branch
relies on.

### Two different conditions both return 429

Worth knowing before reading any 429 count, including the builder's:

| condition | status | body |
|---|---|---|
| per-IP rate limit | 429 | "Too many Epitope Scout requests from this network…" |
| per-session live-job cap (`ANON_MAX_LIVE_JOBS_PER_SESSION = 5`) | 429 | "You have several Epitope Scout structures still loaded…" |
| fleet live-job cap (`ANON_MAX_LIVE_JOBS = 60`) | 503 | "…at capacity for anonymous runs right now" |

Disambiguated by body. Both pre-existing, unchanged by this commit.

**One persistent browser session, one IP, 15 example loads:**

```
1:OK 2:OK 3:OK 4:OK 5:OK 6:SESSION-JOB-CAP … 10:SESSION-JOB-CAP 11:RATELIMIT … 15:RATELIMIT
```

The session job cap bites at 6, before the rate limit ever does. A real user
loading a 6th structure sees "structures still loaded", not "too many
requests" — the right message.

**Rate limiter isolated (fresh session each request, same IP):**

```
1:OK … 10:OK 11:RATELIMIT … 15:RATELIMIT
```

Wall at exactly request 11. `limit=10` per worker, so **20 per 10 min per IP**
at `WEB_CONCURRENCY=2`, as the builder documented.

### FINDING N6 (non-blocking, UX) — shared-IP institutions will hit the wall

Six distinct researchers behind one university NAT, three structure loads each:

```
  user1: ['OK', 'OK', 'RATELIMIT']
  user2: ['OK', 'OK', 'RATELIMIT']
  user3: ['OK', 'OK', 'RATELIMIT']
  user4: ['OK', 'OK', 'RATELIMIT']
  user5: ['OK', 'RATELIMIT', 'RATELIMIT']
  user6: ['OK', 'RATELIMIT', 'RATELIMIT']
  -> 10/18 succeeded on one shared IP
```

**For a single first-time user: fine.** The code's own comment says a real
first visit uses 1–3 intakes; the budget is 20 fleet-wide, so a solo
biologist has ~7× headroom and will never see the limiter. Analysis is the
tighter half — each scoring run spends **two** tokens from the 10-per-worker
analyze bucket (`POST /analyze` + the `GET /progress` SSE stream), so ~5 runs
per worker, ~10 fleet-wide per 10 min per IP. Still comfortable for one
person exploring.

**For a shared NAT: genuinely tight.** A university, institute or company
egressing through one address shares a single 20-intake / ~10-run budget
across everyone. Two colleagues demoing Scout at the same time will collide;
a workshop or a lab meeting will wall out immediately. The 429 copy does point
at the fix ("sign in for a free account with a higher allowance") and
signed-in users bypass the limiter entirely, so the failure is recoverable
and on-funnel — but it is a real papercut on exactly the audience this PR
exists to serve.

**Honest framing of the trade:** this commit does not change any limit. It
makes the existing limits *effective*, which on the parent they were not — a
NAT user could previously have rotated a header past them. So the NAT
experience is strictly worse than the parent's, in exchange for the limiter
functioning at all. That is the correct trade; the numbers just deserve a
product decision rather than silence.

## 11. Required operational follow-up (not a merge blocker)

1. **Confirm Railway's edge behaviour on `X-Forwarded-For`** — append,
   overwrite, or pass-through. The §3 fix is correct for the first two and
   ineffective for the third. This is the single unverified assumption the
   whole change rests on, and it cannot be checked from a dev box.
2. **Confirm the origin is not directly reachable** bypassing the edge. If it
   is, §4b applies and the limiter is bypassable regardless of edge
   behaviour.
3. Consider the §4b hardening (honour XFF only from a trusted peer CIDR),
   which makes 1 and 2 moot.

---

## Verdict table

| # | item | verdict |
|---|---|---|
| 1 | SHA is remote branch head | **PASS** |
| 2 | suite 4932/20 → 4951/20, +19 | **PASS** |
| 3 | S1 re-proved end-to-end on the real route | **PASS** (topology caveat, §4a) |
| 4 | adversarial hop arithmetic | **PASS** — N1, N2, N3 |
| 5 | S2 eviction survives a spray; no CPU DoS | **PASS** |
| 6 | mutation testing (mine, landing-verified) | **PASS** — 5/6 red; N4 |
| 7 | 4 inline XFF copies are audit-only | **PASS** — N5 |
| 8 | `/metrics` forged-header bypass 403s, legit 200s | **PASS** |
| 9 | S4 worker-class documentation is accurate | **PASS** |
| 10 | anonymous flow works; abuse controls tolerable | **PASS** — N6 |

**MERGE.**
