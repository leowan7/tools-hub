# Platform API — fresh adversarial review

**Scope:** every file under `tools/platform_api/`, `shared/api_*.py`,
`shared/webhooks.py`, `shared/campaigns.py`, the four migrations
0023–0026, the platform-API hardening tests, and the
`/account/api-keys*` + boot-guard block in `app.py`.

**Verdict in one paragraph.** Two prior hardening passes have closed
most of the obvious holes (SSRF guard exists, idempotency race is
caught, atomic FSM RPC is wired up, webhook dispatch is bounded). But
the surface is not ready for a first real client. There are two
**CRITICAL** findings that lose data or leak cross-tenant: a single
global `WEBHOOK_SIGNING_SECRET` means any customer can forge a
notification that another customer's receiver verifies as authentic
(**CR-01**), and the in-process semaphore is the *only* dispatcher
without a cron worker, so any saturation, restart, or unexpected
exception silently drops queued webhook deliveries permanently
(**CR-02**). On top of those there are five **HIGH**-severity issues
including a Python-3.11-specific SSRF bypass via IPv4-mapped IPv6
literals (**HI-01**), a TOCTOU between webhook URL validation and
actual POST (**HI-02**), and missing CSRF on the cookie-authenticated
`/account/api-keys/*` POST routes (**HI-03**). Plus assorted
mediums/lows. Fix CR-01 and CR-02 before minting your first key.

---

## Findings table

| ID | Severity | File:Line | Summary |
|---|---|---|---|
| CR-01 | CRITICAL | `shared/webhooks.py:128` | Single global `WEBHOOK_SIGNING_SECRET` enables cross-tenant webhook forgery |
| CR-02 | CRITICAL | `shared/webhooks.py:461-494` | Backpressured / restarted / crashed dispatch threads silently lose deliveries forever — no cron worker |
| HI-01 | HIGH | `shared/webhooks.py:203-224` | SSRF bypass on Python 3.11 via `https://[::ffff:127.0.0.1]/` (IPv4-mapped IPv6) |
| HI-02 | HIGH | `shared/webhooks.py:407-458` | DNS-rebinding TOCTOU: URL re-validated once at dispatch, then re-used across 6h of retries without re-checking |
| HI-03 | HIGH | `app.py:1078-1117` | `/account/api-keys/create` and `/revoke` lack CSRF protection on cookie-authenticated POSTs |
| HI-04 | HIGH | `shared/api_keys.py:198` | `_PREFIX_DISPLAY_LEN=12` stores the first 4 random chars of plaintext — narrows brute-force search space |
| HI-05 | HIGH | `tools/platform_api/routes.py:332-334` | Webhook URL validated synchronously on the request thread; a slow/stuck DNS resolver blocks the API response |
| ME-01 | MEDIUM | `shared/campaigns.py:438-457` | `_is_unique_violation` repr-sniffing depends on supabase-py's error text format — silent break on lib upgrade |
| ME-02 | MEDIUM | `shared/webhooks.py:389-397` | `_post_once` only catches `requests.RequestException`; other exceptions kill the thread and orphan the row |
| ME-03 | MEDIUM | `shared/webhooks.py:75-79` | Each dispatch thread can sleep up to ~7.2h holding a semaphore slot; 8 misbehaving subscribers brown out all delivery |
| ME-04 | MEDIUM | `tools/platform_api/routes.py:344-359` | `IdempotentReplay` returns the existing row in *any* state — incl. `Cancelled` — under HTTP 200 with no warning to the agent |
| ME-05 | MEDIUM | `tools/platform_api/routes.py:625-647` | `_load_owned_campaign` falls through to a 404 when `submission_source != 'api'`, but the response text leaks no detail — fine; however `get_campaign(...)` uses `.single()` which can raise on the 0-row case and swallow the trace — masks debug context |
| ME-06 | MEDIUM | `shared/api_keys.py:95-97` | `API_KEY_LAST_USED_THROTTLE_SECONDS` accepts negative or zero values without validation — minor footgun |
| LO-01 | LOW | `tools/platform_api/routes.py:332-334` | Webhook URL is normalized through `urlparse` but not canonicalized before storage — multiple URLs equivalent at the DNS level can be stored separately |
| LO-02 | LOW | `tools/platform_api/routes.py:599-606` | OpenAPI spec advertises only `bearerAuth`, but actually-shipped endpoints include `/openapi.json` (no auth). Not a bug, but contract documentation drift |
| LO-03 | LOW | `tools/platform_api/openapi_spec.py:286-288` | `Error403` response is defined but never referenced in any path — viewer-key 403 is undocumented for agents |
| LO-04 | LOW | `shared/webhooks.py:74` | `_session` is module-level; first import side-effect creates a thread-unsafe shared adapter pool |
| LO-05 | LOW | `shared/campaigns.py:436` | `create_api_campaign` on insert failure returns `None` for *any* failure (including the unique-violation NOT caught), which the route translates to a 500 — operator can't distinguish DB failure modes |
| LO-06 | LOW | `tools/platform_api/routes.py:614-617` | `_preflight` returns 204 without any CORS-allow-headers; relies on `after_request` to inject — fine in practice but couples the two paths |
| LO-07 | LOW | `tools/platform_api/routes.py:227-237` | `/targets` is an unconditionally empty list — there's no pagination contract, so agents may cache `total: 0` and miss the eventual catalogue |
| LO-08 | LOW | `tools/platform_api/routes.py:660` | `dispatch_webhook` is called with `payload={"delivery_id": None, ...}` which is then *overwritten* in `dispatch_webhook`. Clever but confusing — easy to misread as a real bug |
| IN-01 | INFO | `tools/platform_api/routes.py:336-339` | No per-token rate limit on `/api/v1/cost-estimate` etc. (out of v1 scope per prompt but flagged) |
| IN-02 | INFO | `supabase/migrations/0024_api_keys.sql:35` | `api_keys.role` is a free-text CHECK; future roles need a migration. Mild lock-in |
| IN-03 | INFO | `app.py:986-1018` | `/.well-known/ai-plugin.json` is served when `ENABLE_PLATFORM_API=1` but `name_for_human` is "Ranomics Platform API" — agent UX surface is now public-discoverable; intentional? |
| IN-04 | INFO | `tools/platform_api/routes.py:117-118` | `_MAX_SEQUENCES_PER_SUBMIT=50_000` × `_MAX_SEQUENCE_LEN=2000` = 100MB worst case body. Flask's `MAX_CONTENT_LENGTH=20MB` in `app.py:950` will reject before validators see it, but the validator should document the actual effective cap |

---

## Detailed findings

### CR-01 — Single global `WEBHOOK_SIGNING_SECRET` enables cross-tenant webhook forgery

**File:** `shared/webhooks.py:127-128`, `shared/webhooks.py:131-138`

**Why it's a bug.** Every webhook the platform fires is signed with one
process-wide secret read from `os.environ["WEBHOOK_SIGNING_SECRET"]`.
There is no per-customer or per-key derivation. Combined with
customer-supplied `webhook_url` (taken from the request body), an
attacker can submit an experiment whose `webhook_url` points at another
*customer's* webhook endpoint. The platform will dispatch a real signed
notification to that URL. The victim's verification code — copy-pasted
from the platform documentation — will check
`hmac_sha256(WEBHOOK_SIGNING_SECRET, "<t>.<body>")` and find it valid,
because the secret is the same one the platform sent it.

The payload carries `experiment_id`, `prev_status`, `new_status`,
`results_status`, `delivery_id`, `event_type`, and `timestamp`. The
victim has no way to tell from the message whether the experiment is
theirs. If the victim's automation routes notifications by
`experiment_id` (a common pattern), the attacker can drive arbitrary
state into the victim's downstream systems.

**Trigger.** Attacker mints their own API key, calls
`POST /api/v1/experiments` with `webhook_url:
"https://victim.example.com/ranomics-webhook"` and any sequences.
Platform dispatches signed POST to the victim's URL.

**Impact.** Cross-tenant data leak (experiment_id values leak) and
cross-tenant integrity violation (attacker can drive victim's
downstream state). HIGH-severity in the Stripe webhook model; CRITICAL
here because the alpha customer-base is explicitly automation agents.

**Recommended fix.**
1. Per-customer (or per-API-key) signing secret. Derive at `mint_token`:
   ```python
   import secrets
   webhook_secret = secrets.token_urlsafe(32)
   # persist alongside hashed_token; reveal once on mint
   ```
   Pass to `dispatch_webhook` based on the campaign's `user_id`.
2. Include the customer/owner id in the payload:
   `"owner_user_id": campaign.user_id`. Document that receivers MUST
   verify both the signature and that the owner matches their account.
3. As a defense-in-depth, refuse to accept `webhook_url` values that
   match *another customer's* registered webhook host. Hard to do
   bulletproof, but a simple "is this host already used by a different
   user_id?" check raises the bar.

**Confidence.** CONFIRMED via grep — single secret read once at line
128, never derived per-tenant. The test
`test_webhook_signature_roundtrip` (line 200, `tests/test_platform_api.py`)
uses a hardcoded `"test-secret-for-roundtrip"` confirming the global
model.

---

### CR-02 — Backpressured / restarted / crashed dispatch threads silently lose deliveries forever

**File:** `shared/webhooks.py:461-494`, `shared/webhooks.py:32-36`
(module docstring)

**Why it's a bug.** The dispatcher is purely in-process. The docstring
at line 21-24 is honest about this:

> This module ships an in-process thread-based dispatcher. That's fine
> for the private alpha … The longer-term move is a separate cron
> worker …

But the consequences are not adequately mitigated:

1. **Semaphore saturation.** `_bounded_dispatch` (line 461) uses
   `acquire(blocking=False)`. When the 8-slot cap is full, it stamps the
   row with `attempts=0, next_retry_at=now+60s, last_error="backpressure…"`
   and returns. **Nothing ever rescans the table.** No code path in the
   repo polls `webhook_deliveries WHERE delivered_at IS NULL AND
   next_retry_at <= now()`. The row sits forever.

2. **Process restart.** Railway deploys / restarts kill the daemon
   threads mid-sleep (sleeps go up to 21600s = 6h, line 67). Rows are
   left with `delivered_at=NULL, next_retry_at=<future>` and **never
   retried** because there is no cron.

3. **Unexpected exception in `_dispatch_loop`.** `_post_once` only
   catches `requests.RequestException` (line 397). `ssl.SSLError` is a
   subclass so it's caught, but a `MemoryError`, `OSError` not wrapped by
   requests (rare but possible), or `KeyboardInterrupt` from a
   `SIGTERM` will kill the thread. The `finally` in `_bounded_dispatch`
   releases the semaphore, so capacity recovers — but the row is again
   orphaned.

The README claims "Retried — up to 5 attempts, exponential backoff" but
this is only true if the thread survives. The end state of every
backpressure event and every restart is silent delivery loss.

**Trigger.** Any of:
- 9 in-flight transitions arriving within a few minutes.
- One redeploy of Railway during a dispatch sleep.
- A subscriber that raises an unusual exception (e.g. cert error during
  pool reuse).

**Impact.** Silent delivery failure with no operator alarm. The
`webhook_deliveries` row reads `delivered_at=NULL, last_error="backpressure"`
indefinitely. A customer who relies on webhooks for orchestration sees
their pipeline stall; they have no signal from the platform.

**Recommended fix.**
1. **Short term (this week, before first customer):** add a tiny
   APScheduler / Flask-APScheduler cron in `app.py` that runs every
   60s when `ENABLE_PLATFORM_API=1`, scanning:
   ```sql
   SELECT id, target_url, payload FROM webhook_deliveries
    WHERE delivered_at IS NULL AND next_retry_at <= now()
    ORDER BY next_retry_at LIMIT 50
   ```
   and feeding each row to `_bounded_dispatch`. Use `FOR UPDATE SKIP
   LOCKED` so concurrent Railway replicas don't double-fire.
2. Increase visibility: an admin endpoint or a Slack alert when
   `count(*) WHERE delivered_at IS NULL AND created_at < now() - interval '1 hour'`
   exceeds zero. Mute the "alpha is private" assumption — first customer
   is exactly where you find out.
3. Replace `time.sleep(wait_seconds)` (line 448) with a job-scheduled
   retry: write `next_retry_at` and let the cron pick it up. The thread
   should exit between attempts, not hold the slot for hours.

**Confidence.** CONFIRMED via grep: no `webhook_deliveries` SELECT
exists outside the dispatch path. `_dispatch_loop` (line 407) uses
in-thread `time.sleep`; restart kills it without persisting forward
progress.

---

### HI-01 — SSRF bypass on Python 3.11 via IPv4-mapped IPv6 literal

**File:** `shared/webhooks.py:203-224`

**Why it's a bug.** `_is_private_or_special_ip` checks Python's
`is_private`, `is_loopback`, `is_link_local`, `is_multicast`,
`is_reserved`, `is_unspecified`. On Python **3.11** (the production
runtime per the prompt) these flags **do not** unwrap IPv4-mapped IPv6
addresses. Specifically, `IPv6Address("::ffff:127.0.0.1").is_loopback`
returns `False` and `.is_private` returns `False`. This was fixed in
Python 3.12 (bpo-44582 / gh-91761), but Railway runs 3.11.

Attacker submits:
```
webhook_url = "https://[::ffff:127.0.0.1]/admin"
```
- `urlparse(...).hostname` returns `"::ffff:127.0.0.1"`.
- `ipaddress.ip_address("::ffff:127.0.0.1")` succeeds → `is_literal=True`.
- `_is_private_or_special_ip("::ffff:127.0.0.1")` returns `False` on 3.11.
- Port is None (default for https) → port check passes.
- URL is stored. At dispatch time, `requests` resolves the bracketed
  IPv6, which on most OS dual-stack hosts loops back to 127.0.0.1.

Same bypass for `::ffff:169.254.169.254` (AWS metadata) and
`::ffff:10.0.0.5` (RFC1918).

**Trigger.** Any IPv4 SSRF target wrapped in `::ffff:` notation,
submitted to `POST /api/v1/experiments` with
`webhook_url: "https://[::ffff:<TARGET>]/<path>"`.

**Impact.** Full SSRF against any IPv4 internal endpoint that the
Railway container can reach. The 200-char body snippet returned in
`last_error` (line 403) leaks response content back to the attacker via
`webhook_deliveries.last_error`, which the attacker reads via a future
admin path or — if RLS is not strict on that table — directly. Even
without that leak, *the side effect of the POST* is enough damage.

**Recommended fix.** Unwrap IPv4-mapped before checking. In
`_is_private_or_special_ip`:
```python
def _is_private_or_special_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    # Unwrap IPv4-mapped IPv6 before checks (Python <3.12 doesn't).
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    # ... rest unchanged
```
Also add `::` (IPv6 unspecified) and `::1` to the explicit reject list
even though `is_loopback`/`is_unspecified` should catch them — defense
in depth.

Add a regression test:
```python
@pytest.mark.parametrize("bad", [
    "https://[::ffff:127.0.0.1]/", "https://[::ffff:10.0.0.5]/",
    "https://[::ffff:169.254.169.254]/", "https://[::ffff:100.64.1.1]/",
])
def test_ipv4_mapped_ipv6_rejected(bad):
    with pytest.raises(UnsafeWebhookURLError):
        validate_webhook_url_safe(bad)
```

**Confidence.** CONFIRMED via local repro (Python 3.13 has
`is_loopback=True` for `::ffff:127.0.0.1`; Python 3.11 has False — see
bpo-44582). Prompt explicitly states production runs Python 3.11.

---

### HI-02 — DNS-rebinding TOCTOU between URL validation and POST

**File:** `shared/webhooks.py:407-458`, `shared/webhooks.py:529-537`

**Why it's a bug.** `validate_webhook_url_safe` is called twice — once
at submit time (`routes.py:204`) and once synchronously in
`dispatch_webhook` (line 530, "Re-validate at dispatch time. URL was
validated when stored, but DNS rebinding…"). Both runs resolve DNS via
`socket.getaddrinfo`. But the **actual POST** runs in `_post_once`
(line 383), which calls `_session.post(target_url, ...)`. The `requests`
library re-resolves DNS at request time, **without** going back through
`validate_webhook_url_safe`. The attacker controls DNS for
`attacker.example.com`; they set the TTL to a few seconds. They:

1. Point DNS at a public IP. Submit experiment.
2. `validate_webhook_url_safe` resolves → public IP → OK. Stored.
3. Between line 537 (validation) and line 390 (post), they flip DNS to
   `169.254.169.254`.
4. `requests.post` resolves the new IP. Posts to internal endpoint.

Even worse: **retries don't re-validate**. The 5-retry backoff is
`(30s, 120s, 600s, 3600s, 21600s)`. Across a 6h retry window the
attacker has all the time they need to flip DNS. The retry loop calls
`_post_once` directly (line 428), no `validate_webhook_url_safe`. The
connection pool (`_session` line 83) may also reuse a TLS connection
established to a *previous* IP, but the SNI hostname stays.

**Trigger.** Attacker registers a domain with short-TTL DNS,
short-lived `A` record cycling between a public bounce IP and the
attack target. Submits one experiment with `webhook_url` pointing at
the domain. Times retries.

**Impact.** Same as HI-01: SSRF into anything Railway's outbound DNS
resolves to (cloud metadata, internal services). Mitigated for the
first POST by the TOCTOU race window being narrow, but retries 2-6
give the attacker hours.

**Recommended fix.** Resolve the host yourself once per attempt and
POST to the resolved IP with the original Host header:
```python
# Pseudo-code; production needs careful SNI handling
import socket
addrs = socket.getaddrinfo(parsed.hostname, parsed.port or 443,
                           proto=socket.IPPROTO_TCP)
for entry in addrs:
    if _is_private_or_special_ip(entry[4][0]):
        return (False, "host resolved to private IP at POST time")
# Connect to resolved IP, send Host: original hostname
```
The simpler, less invasive fix: call `validate_webhook_url_safe` at the
top of every iteration of the retry loop in `_dispatch_loop`:
```python
for attempt_index in range(_MAX_ATTEMPTS):
    try:
        validate_webhook_url_safe(target_url)
    except UnsafeWebhookURLError as exc:
        _update_delivery(... last_error=f"rebind: {exc}",
                         delivered_at=datetime.now(timezone.utc))
        return
    # ... existing post-and-backoff
```
That closes the retry-time rebinding window even if it leaves the
single-attempt TOCTOU window open. Combined with HI-01's fix this is a
real improvement.

**Confidence.** CONFIRMED by reading the call graph. The validation
calls at line 204 (submit) and 530 (dispatch enqueue) bracket only the
*pre-thread* path, not the in-thread retry loop.

---

### HI-03 — `/account/api-keys/create` and `/revoke` lack CSRF protection

**File:** `app.py:1078-1117`, `templates/account_api_keys.html` (form
markup), `shared/auth.py:696-708` (login_required uses
`session["user_email"]`)

**Why it's a bug.** Both POST routes are protected by
`@login_required`, which checks `session["user_email"]`. Flask's
default session cookie is `SameSite=Lax` (which **does** block
cross-site POSTs that arrive without explicit form action — Lax blocks
cross-site POSTs by default). However:

- The codebase does not explicitly set `SESSION_COOKIE_SAMESITE` (grep
  for `SESSION_COOKIE` returns no matches anywhere). The default in
  Flask ≥ 2.3 is `'Lax'`, but it's behavior-coupled to the Werkzeug
  version. Relying on framework defaults for a security boundary is
  fragile.
- No CSRF token is present in `templates/account_api_keys.html`
  (verified: grep for `csrf` returns no hits). The mint form has no
  hidden anti-CSRF field; the revoke form similarly.
- A malicious site can `<form action="https://tools.ranomics.com/account/api-keys/create"
  method="POST" target="_blank">` and submit it via user click. With
  `SameSite=Lax` and a top-level navigation, the cookie *is* sent
  (top-level navigations are exempted from Lax restrictions). The
  attacker silently mints a key for the victim's account.
- Worse for revocation: `/revoke` mutates state with no extra
  confirmation; revoke-all is one click.

The minted plaintext is rendered on the response page, but the
attacker's POST opens it in a target window and can be intercepted via
window.opener, postMessage tricks, or simply by serving a
window-grabbing intermediate page first. CSRF + key mint is a known
attack chain.

**Trigger.** Victim is logged into `tools.ranomics.com`. Attacker links
the victim to `https://evil.example/csrf.html` whose body is a
self-submitting form targeting `/account/api-keys/create`. Result: a
new key minted under the victim's account, rendered in the response
page in a popup the attacker controls.

**Impact.** Account takeover (in API-key terms): attacker gets a
`member` role key for the victim's user_id, can call every
`/api/v1/*` write endpoint as that user, including submitting
experiments and reading results. Pairs with CR-01 for cross-tenant
cascade.

**Recommended fix.**
1. Add `flask-wtf` or roll a per-session token. In
   `account_api_keys.html`:
   ```html
   <input type="hidden" name="_csrf" value="{{ csrf_token }}">
   ```
   Validate on POST. The simplest is `flask_wtf.csrf.CSRFProtect(app)`
   gated behind `ENABLE_PLATFORM_API` to scope the dependency.
2. Explicitly set `SESSION_COOKIE_SAMESITE="Strict"` and
   `SESSION_COOKIE_SECURE=True` in the Flask config when
   `ENABLE_PLATFORM_API=1` is set. This is defense in depth — Strict
   blocks even top-level cross-site POST cookies.
3. Consider re-auth (password prompt) before mint. Industry-standard
   for secret reveal.

**Confidence.** CONFIRMED. Grep for `csrf|csrf_token|SameSite|samesite`
across the whole repo returned zero hits in any handler / template.

---

### HI-04 — Stored `prefix` reveals the first 4 random chars of the plaintext

**File:** `shared/api_keys.py:57-58, 198`

**Why it's a bug.**
```python
_TOKEN_PREFIX = "rk_live_"            # 8 chars
_PREFIX_DISPLAY_LEN = 12              # comment: "12 chars including rk_ tag"
prefix = plaintext[:_PREFIX_DISPLAY_LEN]
```
`plaintext = "rk_live_<22 random chars>"`. The first 8 chars are the
literal `rk_live_`. Slicing `[:12]` keeps `rk_live_` PLUS the **first 4
random chars**. Those 4 chars are stored unhashed in `api_keys.prefix`
(plaintext) and read by RLS to authenticated callers via the
`/account/api-keys` page (and by service-role admin views generally).

Suppose `api_keys` ever leaks (SQL injection elsewhere, broken backup
share, accidental Supabase dashboard export). The attacker now has the
hashed_token (no-good, SHA-256) PLUS 4 leading characters of plaintext
randomness. They can brute-force the remaining 18 chars (url-safe
base64) = 64^18 ≈ 2^108 keyspace. Still infeasible. So technically
this is not a break.

**But**: the *intent* of `prefix` is "stable display handle, e.g.
rk_live_abcd". If the goal is the literal `rk_live_…` prefix only, then
storing 4 chars of secret material is unnecessary entropy disclosure
and a deviation from the Stripe convention the codebase claims to
follow (Stripe stores e.g. `sk_test_abcd…wxyz` showing *prefix +
suffix*, never plaintext middle chars). It also breaks the comment at
line 32-35 which says "The plaintext is never written to logs, never
sent in webhooks, and never echoed back after the mint call" — the
prefix IS persisted plaintext.

**Trigger.** Persistent: every mint stores 4 plaintext chars.

**Impact.** Modest entropy reduction on token leak. Not exploitable
alone; a credential-stuffing tier of risk on top of any other DB leak.

**Recommended fix.** Either:
- Store only the literal scheme prefix (`rk_live_…` for display) with
  no plaintext bits: set `_PREFIX_DISPLAY_LEN = 8`. Show only
  `rk_live_…` in the UI plus the `label`.
- Or: keep `_PREFIX_DISPLAY_LEN=12` and accept the docstring needs
  updating; document that 4 plaintext chars are stored deliberately for
  UX. Pick one.

**Confidence.** CONFIRMED via inspection. The slice on line 198 keeps
12 chars; 12-8=4 random chars retained.

---

### HI-05 — Webhook URL validation runs synchronously on the request thread (DNS stall risk)

**File:** `tools/platform_api/routes.py:332-334`, `shared/webhooks.py:285-300`

**Why it's a bug.** `_validate_webhook_url` calls
`validate_webhook_url_safe`, which calls `socket.getaddrinfo(host,
None, proto=socket.IPPROTO_TCP)`. There is no timeout on
`getaddrinfo`; it inherits the system resolver's. A pathological
hostname (slow authoritative NS, intentional DoS DNS) hangs the
request thread.

`POST /api/v1/experiments` is called once per experiment submit. A
malicious client can submit many simultaneous requests with hostnames
that resolve slowly. With Gunicorn/uWSGI sync workers, you exhaust the
worker pool quickly. Even a single slow DNS lookup per submit stalls
that worker for the resolver timeout (often 30s default on Linux).

**Trigger.** `POST /api/v1/experiments {"webhook_url": "https://<slow-domain>/..."}`
in parallel × N requests.

**Impact.** DoS against the create endpoint. The API itself doesn't go
down (other routes still work) but submits stall, and with sync
workers Railway will start 502'ing.

**Recommended fix.** Use a thread with timeout, or `socket.gethostbyname_ex`
with `socket.setdefaulttimeout(...)` set narrowly. Even better:
defer DNS validation to the dispatch thread (after responding 201) and
only do *syntactic* checks (scheme, port, no creds, IP literal not in
private range) at the request handler. Trade-off: an attacker can
submit URLs that fail validation at dispatch, but those just get a
`delivered_at=None, last_error="unsafe"` row and never POST. The
request itself succeeds, the client gets a 201, and any DNS-stall
attacker DoSes only the background dispatcher.

Concrete change in `validate_webhook_url_safe`:
```python
import socket
def _resolve_with_timeout(host, timeout=2.0):
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        return socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    finally:
        socket.setdefaulttimeout(old)
```
Better: split into `validate_url_syntax(url)` (cheap, request-time) and
`validate_url_dns(url)` (slow, dispatch-time).

**Confidence.** CONFIRMED via inspection. `socket.getaddrinfo` at line
285 has no timeout argument and the module-level code never sets a
default socket timeout.

---

### ME-01 — `_is_unique_violation` repr-sniffing is supabase-py-version-coupled

**File:** `shared/campaigns.py:438-457`

**Why it's a bug.** The function tries three strategies:
```python
text = repr(exc)
if "23505" in text or "duplicate key value" in text.lower():
    return True
code = getattr(exc, "code", None) or getattr(exc, "pgcode", None)
if code == "23505":
    return True
body = getattr(exc, "json", None) or getattr(exc, "message", None)
if body is not None and "23505" in str(body):
    return True
```
Strategy 1 relies on `repr(exc)` containing "23505" or "duplicate key
value". Strategy 2 reads `.code` / `.pgcode`. Strategy 3 reads `.json` /
`.message`. **None of these match the current supabase-py shape.**
supabase-py v2 raises `postgrest.exceptions.APIError`, whose `__init__`
stores the JSON body as `details`, `message`, `code`, `hint` attributes
on the instance, but the format of `.code` is the postgres error code
*as a string* — sometimes "23505", sometimes the postgres SQLSTATE.

In the test `test_create_api_campaign_raises_idempotent_replay_on_unique_violation`
(line 437), the mock raises `RuntimeError("duplicate key value violates
unique constraint…SQLSTATE 23505")`. That tests strategy 1 against
`repr(RuntimeError)`, not against the actual supabase-py exception
class. The test passes; production behavior is **not validated**.

If supabase-py upgrades and the error text changes, the idempotency
race catch silently fails closed — the second concurrent POST gets a
500 instead of a replay.

**Trigger.** Two parallel `POST /api/v1/experiments` with the same
`Idempotency-Key` header from the same user. The race that the fix is
meant to handle.

**Impact.** Loss of idempotency guarantee under concurrency; client
sees 500 instead of 200-replay. The first POST persists the campaign,
so no DB corruption — but a retry-on-500 client may then POST a third
time and find the row, replay, succeed. Eventually OK; user experience
muddy.

**Recommended fix.** Either:
1. Test against the real supabase-py exception. Add an integration test
   that hits a real (or in-memory pg) DB so the catch path is exercised.
2. Replace with explicit type check:
   ```python
   try:
       from postgrest.exceptions import APIError
   except ImportError:
       APIError = ()
   def _is_unique_violation(exc):
       if isinstance(exc, APIError):
           code = (getattr(exc, "code", None) or "").lower()
           details = (getattr(exc, "details", None) or "").lower()
           return code == "23505" or "duplicate key" in details
       return "23505" in str(exc).lower() or "duplicate key" in str(exc).lower()
   ```
3. Or use Postgres `INSERT ... ON CONFLICT (user_id, idempotency_key)
   DO NOTHING RETURNING ...` and check whether RETURNING is empty.
   That avoids the catch path entirely. Cleanest fix.

**Confidence.** INFERRED from code; CONFIRMED that no test exercises
the real supabase-py exception shape (grep for `APIError|postgrest` in
`tests/` returns no hits).

---

### ME-02 — `_post_once` only catches `requests.RequestException`

**File:** `shared/webhooks.py:389-398`

**Why it's a bug.** A bare `try: ... except requests.RequestException`.
Any other exception (subprocess error, unexpected `OSError` outside
requests's wrap, `UnicodeDecodeError` on response body, etc.) bubbles
out of `_post_once` → out of `_dispatch_loop` → thread dies. The
`finally` in `_bounded_dispatch` releases the semaphore (good), but the
delivery row is left mid-state. Same family as CR-02 but a different
trigger.

A specific real risk: in `_post_once` line 403, `resp.text` decodes the
response body using the response encoding. A malformed `Content-Type:
text/html; charset=foo` from the subscriber can raise `LookupError`.
That bubbles out, thread dies, row orphaned.

**Trigger.** Subscriber returns a 4xx with an unrecognized charset
declaration in `Content-Type`.

**Impact.** Delivery row stuck in DB; subscriber never gets notified.
No alarm.

**Recommended fix.** Wrap the body decode in its own try/except and
the whole function in a broader `except Exception`:
```python
def _post_once(target_url, body, signature):
    try:
        resp = _session.post(...)
    except requests.RequestException as exc:
        return (False, f"requests error: {exc}")
    except Exception as exc:
        return (False, f"unexpected: {exc.__class__.__name__}: {exc}")
    if 200 <= resp.status_code < 300:
        return (True, f"http {resp.status_code}")
    try:
        snippet = (resp.text or "")[:200]
    except Exception:
        snippet = "<undecodable>"
    return (False, f"http {resp.status_code}: {snippet}")
```

**Confidence.** CONFIRMED via inspection.

---

### ME-03 — Each dispatch thread sleeps up to ~7.2h holding a semaphore slot

**File:** `shared/webhooks.py:425-458, 78-80`

**Why it's a bug.** `_dispatch_loop` does `time.sleep(wait_seconds)`
inside the retry loop. Total worst case across 5 retries: 30+120+600+
3600+21600 = 25,950s ≈ 7.2h. With `_MAX_INFLIGHT=8`, eight independent
unhealthy subscribers fully saturate the dispatch pool **for hours**.
Every other transition during that window hits the backpressure path
which (per CR-02) is non-recoverable.

Even with the cron fix proposed in CR-02, sleeping in-thread is the
wrong shape — threads should `next_retry_at += backoff; return; let
cron re-pick`.

**Trigger.** 8+ active subscribers that all fail; healthy subscribers
on other campaigns are starved.

**Impact.** Same as CR-02. Listed separately to highlight the
in-thread-sleep root cause; CR-02 listed it under "backpressure".

**Recommended fix.** Refactor `_dispatch_loop` to attempt once, then
write `next_retry_at = now + backoff` and return. Cron worker (or a
50ms-tick scheduler) re-picks. Use `webhook_deliveries.attempts` as the
counter, not a Python-local var.

**Confidence.** CONFIRMED via inspection of `_BACKOFF_SECONDS` and
`time.sleep` call.

---

### ME-04 — `IdempotentReplay` returns any-status row under HTTP 200 silently

**File:** `tools/platform_api/routes.py:353-357`,
`shared/campaigns.py:283-294`

**Why it's a bug.** The idempotency-replay handler in `routes.py`:
```python
except IdempotentReplay as replay:
    resp = jsonify(campaign_to_api_view(replay.campaign))
    resp.status_code = 200
    resp.headers["Idempotent-Replay"] = "true"
    return resp
```
This returns the existing row in its current state. If the row has
already progressed to `Cancelled`, `Done`, `InReview`, etc., the agent
receives a 200 with `status: "Cancelled"` and `Idempotent-Replay: true`
header. The agent's submit logic may interpret 200 OK as "submitted
successfully" and ignore the `status` field.

The spec at `openapi_spec.py:127-134` says:
> 200: Idempotent replay of an earlier create.

No constraint on which statuses are valid replays. A faithful client
must read both `status` and the `Idempotent-Replay` header to know
what happened.

**Trigger.** Customer submits with key K, status becomes Cancelled,
customer submits again with key K. They get a 200 back claiming
status=Cancelled.

**Impact.** Customer-side confusion. Not data corruption. But this is
the kind of thing that burns the first customer on first integration
day.

**Recommended fix.** Either:
1. On replay, return 409 if the existing row is in a terminal state
   (`Done`, `Cancelled`). Specify in the error payload that the prior
   submission already terminated.
2. Or: include `previously_created_at` in the response body alongside
   `status` so the client can tell this is not a fresh submit.

**Confidence.** CONFIRMED via inspection. The handler unconditionally
returns the existing row's status.

---

### ME-05 — `get_campaign` uses `.single()` which masks debugging context

**File:** `shared/campaigns.py:192`, `tools/platform_api/routes.py:625-647`

**Why it's a bug.** `query.single().execute()` returns one row or
raises if 0 / many. The handler treats any exception as 404. That's
correct for the client. But:

- On supabase-py errors that aren't "no rows" (e.g., RLS denial, network
  glitch, DB schema mismatch), the user gets 404 instead of 500. The
  operator can't distinguish "doesn't exist" from "broken".
- The 404 message "No experiment with that id is visible on this API
  key" is identical regardless of whether the row exists but belongs
  to another user, exists but is web-source, or actually doesn't
  exist. That's *correct* security posture (no enumeration). But
  **server logs lose the distinction too** — `get_campaign` swallows
  the exception with `except Exception: return None` (line 193-194).

**Trigger.** Any non-existent campaign id query. Operator debugging a
404 in production has no log to grep.

**Impact.** Operability, not security.

**Recommended fix.** Log the exception at WARNING in `get_campaign`'s
except:
```python
except Exception:
    logger.warning("get_campaign exception for id=%s user=%s",
                   campaign_id, user_id, exc_info=True)
    return None
```

**Confidence.** CONFIRMED via inspection.

---

### ME-06 — `API_KEY_LAST_USED_THROTTLE_SECONDS` accepts negative or zero

**File:** `shared/api_keys.py:95-97`

**Why it's a bug.**
```python
_LAST_USED_THROTTLE_SECONDS = int(
    os.environ.get("API_KEY_LAST_USED_THROTTLE_SECONDS", "60")
)
```
No validation. Setting `API_KEY_LAST_USED_THROTTLE_SECONDS=-1` makes
every call stale (`age >= -1` always true), defeating the throttle.
Setting to `0` makes every call stale. Setting to a giant value
disables the touch.

Default is 60s — reasonable. But if an operator types a typo, no
warning fires.

**Trigger.** Operator misconfiguration.

**Impact.** Either lost throttle (DB writes on every API call) or no
last_used_at telemetry (audit log blind).

**Recommended fix.** Validate at import:
```python
_raw = int(os.environ.get("API_KEY_LAST_USED_THROTTLE_SECONDS", "60"))
if _raw < 1:
    logger.warning("API_KEY_LAST_USED_THROTTLE_SECONDS=%d invalid; "
                   "defaulting to 60", _raw)
    _raw = 60
_LAST_USED_THROTTLE_SECONDS = _raw
```

**Confidence.** CONFIRMED via inspection.

---

### LO-01 — Webhook URL not canonicalized before storage

**File:** `tools/platform_api/routes.py:332-334`

Storage stores `webhook_url` as-submitted. `https://example.com/hook`,
`HTTPS://example.com/hook`, and `https://example.com:443/hook` are
equivalent at the network level but stored as distinct strings. Two
campaigns from the same customer with subtly-different URLs end up
with separately-tracked delivery rows. Minor; not a bug.

**Fix.** Normalize before insert: lowercase scheme + host, drop default
port, normalize trailing slash. `requests`-compatible canonical form.

---

### LO-02 — OpenAPI advertises bearerAuth but `/openapi.json` is unauth

**File:** `tools/platform_api/routes.py:599-606`,
`tools/platform_api/openapi_spec.py:55`

`security: [{"bearerAuth": []}]` applies to all paths by default, but
the spec doesn't `security: []` override the `/openapi.json` path
itself. Faithful agents will try to send Bearer headers to the spec
endpoint; benign but suggests inconsistency.

**Fix.** Add `security: []` to the spec endpoint, OR move it under a
separate Blueprint without the global security default.

---

### LO-03 — `Error403` defined but never referenced in any path

**File:** `tools/platform_api/openapi_spec.py:287`

The spec defines `Error403` for "Read-only key on a write endpoint" but
no path response references it. Agents reading the spec won't know that
write endpoints can 403 on viewer-role keys.

**Fix.** Add `"403": {"$ref": "#/components/responses/Error403"}` to
the response map of `POST /experiments` and `POST /quotes/{id}/confirm`.

---

### LO-04 — Module-level `_session` initialization

**File:** `shared/webhooks.py:83-91`

`requests.Session()` is module-level. Created on first import. Adapters
configured immediately. If Flask's app factory pattern initializes the
module twice (e.g., gunicorn preload + workers), connection pool is
shared across forks. Not a bug today; a footgun for future workers.

**Fix.** Lazy-initialize inside `_post_once` first call, or move to a
per-app extension pattern.

---

### LO-05 — `create_api_campaign` returns `None` on any insert failure

**File:** `shared/campaigns.py:430-431`

The except branch logs and returns `None`. The route translates this to
500. But the operator can't tell from the log whether it was a unique
violation that fell through `_is_unique_violation`, a CHECK constraint
failure, RLS, network, etc.

**Fix.** Distinguish error classes and propagate enough context.

---

### LO-06 — Preflight handler relies on `after_request` for CORS headers

**File:** `tools/platform_api/routes.py:609-617`

The `_preflight` view returns `jsonify({})` with 204. The CORS headers
get added by `_api_response_headers` via `setdefault`. Currently fine;
coupled. If someone removes `_api_response_headers` or adds explicit
CORS headers to the preflight, the order matters.

**Fix.** Set CORS headers explicitly on the preflight response.

---

### LO-07 — `/targets` returns `total: 0` without pagination contract

**File:** `tools/platform_api/routes.py:227-237`

Agents caching this response will miss the eventual catalogue. Add
`Cache-Control: no-store` (currently the global default does this — but
worth documenting). Or include a placeholder `next_cursor: null` to
signal future pagination.

---

### LO-08 — `dispatch_webhook` payload has `delivery_id: None` overwritten by id mint

**File:** `tools/platform_api/routes.py:660`,
`shared/webhooks.py:541-542`

In `_fire_webhook` the route passes `payload={"delivery_id": None, ...}`,
then `dispatch_webhook` does `signed_payload = {**payload, "delivery_id":
delivery_id}` to overwrite. Functionally correct but easy to misread as
a bug when scanning. Comment in routes says `# filled by webhook ledger`.

**Fix.** Drop the `delivery_id: None` entry from the caller and let
`dispatch_webhook` add it; cleaner contract.

---

### IN-01 — No per-token rate limit on `/api/v1/*`

**File:** `tools/platform_api/routes.py` (whole module)

Prompt flagged as out of v1 scope. Confirmed: no `flask-limiter`, no
custom limiter. A single token can DoS `/cost-estimate` or
`/experiments`. Recommend `flask-limiter` keyed off `g.api_key_id`
when API is enabled.

---

### IN-02 — `api_keys.role` is free-text CHECK

**File:** `supabase/migrations/0024_api_keys.sql:35`

`role text NOT NULL CHECK (role IN ('member', 'viewer'))`. Future roles
need a migration to extend the CHECK. Acceptable for alpha; flag for
when the role taxonomy grows.

---

### IN-03 — `/.well-known/ai-plugin.json` makes the API publicly discoverable

**File:** `app.py:986-1018`

The manifest is registered when `ENABLE_PLATFORM_API=1`. Anyone
crawling `tools.ranomics.com/.well-known/ai-plugin.json` sees the API
exists, the `description_for_model`, the OpenAPI URL. If the alpha is
meant to be private, this leaks "we have an agent-facing API".
Intentional? If yes, fine. If no, gate behind an additional flag.

---

### IN-04 — Worst-case body size 100MB; Flask cap is 20MB

**File:** `tools/platform_api/routes.py:117-118`, `app.py:950`

`_MAX_SEQUENCES_PER_SUBMIT=50_000` × `_MAX_SEQUENCE_LEN=2000` ≈ 100MB
of sequence bytes alone. `MAX_CONTENT_LENGTH = 20MB` from `app.py:950`
will reject before the validator runs, so the validator caps are
effectively unreachable. Suggest documenting the real effective cap
inline so reviewers don't think the validator is the gate.

---

## Reviewed but clean

The prompt called these out as suspicious; here's what I checked and
found OK or already addressed.

- **`UnsafeWebhookURLError` is a `ValueError` subclass** — `routes.py`
  catches `ValueError` in `create_experiment` at line 358 to map to 400.
  However, `_validate_webhook_url` (line 185-207) catches
  `UnsafeWebhookURLError` itself and converts to a string-returned error
  *before* the route's `except ValueError` runs. The SSRF rejection is
  returned as a 400 with `invalid_webhook_url` code, not swallowed.
  The inline comment at `shared/webhooks.py:265-271` explicitly notes
  the narrow `try` around `ipaddress.ip_address(host)` — a real concern
  that the author thought through. Clean (subject to HI-01).

- **Idempotency replay shape** — covered by ME-04 above for terminal
  states. Otherwise: replay returns the existing campaign, sets
  `Idempotent-Replay: true` header, status 200. Consistent with industry
  norm. The 23505 catch in `create_api_campaign:417` does correctly
  re-SELECT and raise `IdempotentReplay`. Concurrency safety of the
  unique index path is real (subject to ME-01).

- **`transition_lab_campaign_api` RPC** — Inspected at
  `0026_lab_campaign_transition_rpc.sql`. `FOR UPDATE` is correctly held
  across the SELECT-then-UPDATE inside a single function call (Postgres
  releases the lock at function end inside a transaction; supabase-py's
  RPC runs each call as its own implicit transaction). Status_log
  append uses `||` JSONB concat under the lock — atomic. Filter on
  `submission_source = 'api'` blocks web rows from being mutated by the
  API path. Clean.

- **`resolve_token` last_used_at update filter** — The `is_("revoked_at",
  "null")` on the UPDATE (line 269) is load-bearing: a token revoked
  between SELECT (line 239) and UPDATE (line 263) would otherwise have
  its last_used_at bumped, leaking "revoked key used recently" into the
  audit. Test `test_last_used_throttle_updates_when_stale` confirms.
  Clean.

- **Signing scheme + `delivery_id` baking** — Confirmed by reading
  `dispatch_webhook` (line 539-542) that the delivery_id is minted
  BEFORE the signed body is computed in `_dispatch_loop:423`. The body
  is `json.dumps(payload, separators=(",", ":"), sort_keys=True)` with
  the delivery_id present. Hash is over the canonical bytes. Receiver
  can verify with `hmac_sha256(secret, f"{t}.{body}")`. Stripe-pattern
  correct. (Note: still subject to CR-01 — the shared secret is the
  real issue, not the format.)

- **Cross-tenant scope on read endpoints** — `_load_owned_campaign`
  passes `user_id=g.api_user_id` to `get_campaign`, which filters
  `.eq("user_id", user_id)` at the query. Service-role client bypasses
  RLS, but the explicit user_id filter is enforced in code. Same path
  used by all five `/experiments/{id}*` and `/quotes/{id}/confirm`. The
  follow-up `if campaign.submission_source != 'api': return 404` blocks
  web-form rows from being visible to the API. Clean.

- **Boot-time guards** — `SESSION_SECRET_KEY` required (raises on
  missing); `WEBHOOK_SIGNING_SECRET` warned-but-not-blocked. The
  `dispatch_webhook` correctly fails closed when the secret is unset
  (line 518). Acceptable posture.

- **Migrations 0023–0026 re-runnable** — Every `ALTER TABLE` uses
  `ADD COLUMN IF NOT EXISTS`. Every constraint addition is wrapped in
  `DO $$ ... IF NOT EXISTS ... $$`. Every index uses
  `CREATE INDEX IF NOT EXISTS`. RLS policies use
  `DROP POLICY IF EXISTS` then `CREATE POLICY`. The status CHECK
  swap in 0023 drops the old constraint name and creates a new name —
  re-running is idempotent because the second drop is a no-op (already
  dropped) and the second create is gated by `IF NOT EXISTS`. Clean.

- **`mint_token` per-user cap** — `_MAX_KEYS_PER_USER` default 10,
  enforced by counting active rows before insert. Race window exists
  (two concurrent mints could both pass the cap check), but the cap is
  soft and the result is one extra key, not a security issue. Clean
  enough for alpha.

- **Idempotency-Key format validation** — 8-128 chars, `isprintable()`
  check at `routes.py:215-218`. Control chars and unicode garbage are
  rejected. UTF-8 emoji etc. pass `isprintable()` but are still
  printable per the spec. Acceptable.

- **Sequence validator** — `:` chain separator handled correctly.
  Empty chains, over-long, non-canonical residues all rejected. Limits
  documented. Subject to IN-04 (Flask body cap is the real limit), but
  the validator itself is sound.

- **Webhook payload field ordering** — `dispatch_webhook` uses
  `sort_keys=True` for the body bytes. Receivers can recompute by
  serializing their decoded payload with `sort_keys=True` — but most
  webhook docs warn receivers to verify *against the raw bytes
  received*, not re-serialize. The platform's documentation should
  explicitly call that out. Not a bug in the platform.

---

_Reviewer: Claude (Opus 4.7) — fresh-eyes adversarial pass._
_Reviewed: 2026-06-04._
_Depth: deep (cross-file, includes call graph + Python runtime
verification)._
