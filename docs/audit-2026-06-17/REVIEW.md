# tools-hub — Correctness Review (`/review`)

**Date:** 2026-06-17 · **Method:** gstack-style fan-out, 9 subsystems, every candidate bug adversarially re-verified against the code. Read-only.
**Result:** 34 candidate bugs -> **20 confirmed** (14 rejected as false positives by the verification pass).
Confirmed severity: 1 critical · 8 high · 10 medium · 1 low.

> Companion to `cso.md` (security) and `health.md` (code quality). Master index: `../AUDIT-2026-06-17-comprehensive.md`.

## Confirmed bugs
| Sev | Conf | Title | File:line |
|---|---|---|---|
| CRITICAL | 95 | Unhandled error status from modal_client.poll leaves job stuck | `app.py:4879-4897` |
| HIGH | 95 | Uncaught ValueError in campaigns_submit for affinity_goal_kd_nm | `app.py:5661` |
| HIGH | 95 | Uncaught ValueError in campaigns_submit for timeline_weeks | `app.py:5664` |
| HIGH | 95 | Wallet transactions pagination always shows no next page | `app.py:2861` |
| HIGH | 95 | cleanup_old_jobs fails to delete job directories with nested subdirectories | `scout/jobs.py:83-86` |
| HIGH | 92 | Missing type check for candidates in export_csv | `app.py:5324-5335` |
| HIGH | 92 | Missing type checks for candidates/sequences in export_fasta | `app.py:5360-5383` |
| HIGH | 88 | Missing attempts increment in webhook dispatch backpressure path | `shared/webhooks.py:599-607` |
| HIGH | 85 | gpu_seconds falsy coalescing: treating 0 as missing value | `gpu/modal_client.py:465,472` |
| MEDIUM | 95 | cost_band() can raise TypeError on non-numeric band values | `tools/platform_api/calibrated_targets.py:196-205` |
| MEDIUM | 90 | Flask SECRET_KEY set to bytes instead of string when SESSION_SECRET_KEY env var is unset | `app.py:949-951` |
| MEDIUM | 90 | Staff email sent without text fallback in send_campaign_submitted_emails | `shared/email.py:490` |
| MEDIUM | 90 | Unhandled exception in _nearest_clean_residues loop logic | `shared/pdb_preflight.py:978-988` |
| MEDIUM | 88 | Invalid base64 not validated in export_zip | `app.py:5611` |
| MEDIUM | 85 | Webhook URL re-validation does not manage socket timeout stack correctly | `shared/webhooks.py:529-541` |
| MEDIUM | 78 | Heartbeat state append is not atomic, concurrent writes clobber progress | `webhooks/modal.py:500-540` |
| MEDIUM | 72 | No bounds check on gpu_seconds before int conversion in CAS updates | `shared/jobs.py:1047,1144` |
| MEDIUM | 0.95 | Off-session PaymentIntent amount conversion loses precision due to unquantized int() truncation | `billing/checkout.py:540` |
| MEDIUM | 0.85 | wallet_preflight returns incorrect hard_cap_usd when REASON_SELF_SERVE_CEILING reason is returned | `shared/wallet.py:451-459` |
| LOW | 85 | Incorrect sort order in list_keys (newest-first comment mismatch) | `shared/api_keys.py:562` |


### 1. [CRITICAL] Unhandled error status from modal_client.poll leaves job stuck
**`app.py:4879-4897`** · confidence 95 · verifier 95% confirms real

- **Evidence:** modal_client.poll() can return status=error (gpu/modal_client.py line 280-283), but app.py only checks for succeeded/failed/running. When error status occurs, no job update happens and job remains pending/running indefinitely.
- **Impact:** Jobs with polling errors are abandoned in non-terminal states, blocking cancellation and wallet settlement. Users cannot see errors and holds remain locked.
- **Fix:** Add elif to handle poll["status"] == "error": call complete_job(terminal_status="failed", error={"bucket":"modal-poll","detail":poll.get("error")})

### 2. [HIGH] Uncaught ValueError in campaigns_submit for affinity_goal_kd_nm
**`app.py:5661`** · confidence 95 · verifier 98% confirms real

- **Evidence:** affinity_goal_kd_nm = float(raw_kd) if raw_kd else None — If raw_kd is a non-empty string that cannot be converted to float (e.g., 'invalid', '1.5nM'), this raises an uncaught ValueError, causing a 500 error response.
- **Impact:** User submitting a campaign with malformed affinity_goal_kd_nm input triggers an unhandled exception instead of gracefully rejecting the input, resulting in 500 error instead of form re-display with error message.
- **Fix:** Wrap in try/except: try: affinity_goal_kd_nm = float(raw_kd) if raw_kd else None; except ValueError: affinity_goal_kd_nm = None

### 3. [HIGH] Uncaught ValueError in campaigns_submit for timeline_weeks
**`app.py:5664`** · confidence 95 · verifier 95% confirms real

- **Evidence:** timeline_weeks = int(raw_weeks) if raw_weeks else None — If raw_weeks is a non-empty string that cannot be converted to int (e.g., '3.14', 'abc'), this raises an uncaught ValueError, causing a 500 error response.
- **Impact:** User submitting a campaign with malformed timeline_weeks input triggers an unhandled exception instead of gracefully rejecting the input, resulting in 500 error instead of form re-display with error message.
- **Fix:** Wrap in try/except: try: timeline_weeks = int(raw_weeks) if raw_weeks else None; except ValueError: timeline_weeks = None

### 4. [HIGH] Wallet transactions pagination always shows no next page
**`app.py:2861`** · confidence 95 · verifier 98% confirms real

- **Evidence:** Code: `.range(offset, offset + page_size)` fetches exactly page_size rows. Comment on lines 2857-2858 says 'Pull one extra row so we can tell whether a next page exists without a second count query.' But the check at line 2865 `if len(rows) > page_size:` can never be true because only page_size rows are fetched, never more.
- **Impact:** The pagination UI will never show a 'next page' button or indicate that more transactions exist, even when there are additional pages. Users cannot navigate beyond the first page of wallet transactions.
- **Fix:** Change line 2861 from `.range(offset, offset + page_size)` to `.range(offset, offset + page_size + 1)` to actually fetch one extra row as the comment and logic intend.

### 5. [HIGH] cleanup_old_jobs fails to delete job directories with nested subdirectories
**`scout/jobs.py:83-86`** · confidence 95 · verifier 95% confirms real

- **Evidence:** The cleanup function deletes files via `entry.rglob("*")` and `child.unlink()`, but only deletes files, not directories. Subsequent `entry.rmdir()` on line 86 will fail (caught silently on line 88-90) when the directory has empty subdirectories left behind. The failed deletion is logged silently and the job directory remains in the filesystem.
- **Impact:** Over time, storage accumulates with undeletable job directories containing nested subdirectories. The cleanup function's return value (deleted_count) is inaccurate as failed deletions are silent. Once a job directory with nested structure fails to delete, cleanup_old_jobs will never remove it in future runs since the subdirectory structure persists. This causes unbounded storage growth.
- **Fix:** Use `shutil.rmtree()` to recursively delete the entire directory tree, or recursively delete subdirectories in reverse topological order before attempting rmdir on the parent. Current code: `for child in entry.rglob("*"): if child.is_file(): child.unlink(); entry.rmdir()` should become something like: `shutil.rmtree(entry)` or delete directories depth-first.

### 6. [HIGH] Missing type check for candidates in export_csv
**`app.py:5324-5335`** · confidence 92 · verifier 95% confirms real

- **Evidence:** for cand in candidates: cand.get('scores') assumes each candidate is a dict without isinstance check. If candidates contains non-dict values, calling .get() raises AttributeError.
- **Impact:** Malformed job result with non-dict candidates crashes the CSV export endpoint with 500 error instead of handling gracefully.
- **Fix:** Add: for cand in candidates: if not isinstance(cand, dict): continue

### 7. [HIGH] Missing type checks for candidates/sequences in export_fasta
**`app.py:5360-5383`** · confidence 92 · verifier 92% confirms real

- **Evidence:** Lines 5361 and 5371 call .get() on cand and seq_obj without isinstance(dict) checks. If candidates or sequences contain non-dict values, AttributeError is raised.
- **Impact:** Malformed job result crashes the FASTA export endpoint with 500 error instead of handling gracefully.
- **Fix:** Add: if not isinstance(cand, dict): continue; if not isinstance(seq_obj, dict): continue

### 8. [HIGH] Missing attempts increment in webhook dispatch backpressure path
**`shared/webhooks.py:599-607`** · confidence 88 · verifier 95% confirms real

- **Evidence:** In _bounded_dispatch line 600-606: when the semaphore cannot be acquired, _update_delivery is called with attempts=prior_attempts (NOT prior_attempts+1). The comment says the row is queued for cron sweep, but the attempts counter is NOT incremented. This violates the protocol that attempts should increment on every entry to _dispatch_once or _bounded_dispatch.
- **Impact:** When backpressure occurs, the webhook delivery does not increment the attempt counter. This could cause the same delivery to be attempted multiple times from the cron sweep without the attempt count advancing, potentially leading to an infinite retry loop if the semaphore remains saturated.
- **Fix:** Change line 601 to `attempts=prior_attempts + 1` to match the pattern in _dispatch_once (line 547) where attempts is always incremented on every dispatch attempt.

### 9. [HIGH] gpu_seconds falsy coalescing: treating 0 as missing value
**`gpu/modal_client.py:465,472`** · confidence 85 · verifier 95% confirms real

- **Evidence:** Line 465: `smoke.get("runtime_seconds") or smoke.get("gpu_seconds")` treats 0 as falsy, falling back to gpu_seconds. Same at line 472. Zero runtimes are silently discarded.
- **Impact:** Fast jobs returning 0 runtime_seconds report incorrect fallback value. Billing inaccuracy on instant/minimal-overhead operations.
- **Fix:** Use explicit None check: `smoke.get("runtime_seconds") if smoke.get("runtime_seconds") is not None else smoke.get("gpu_seconds")`.

### 10. [MEDIUM] cost_band() can raise TypeError on non-numeric band values
**`tools/platform_api/calibrated_targets.py:196-205`** · confidence 95 · verifier 95% confirms real

- **Evidence:** def cost_band(entry: dict, experiment_type: str) -> Optional[list[int]]: """Return ``[low, high]`` USD band for the entry+assay, or ``None``. Returns ``None`` when the entry doesn't support the experiment type. """ bands = entry.get("typical_campaign_range_usd") or {} band = bands.get(experiment_type) if not band or len(band) != 2: return None return [int(band[0]), int(band[1])] # <-- Can raise TypeError if band[0] or band[1] is None or non-numeric
- **Impact:** If the CATALOG data is corrupted or dynamically modified to contain None or non-numeric values in the cost band, the int() conversion will raise a TypeError instead of returning None. In POST /api/v1/experiments/cost-estimate (line 532 of routes.py), there is no try/except around the cost_band() call, so the exception propagates as an unhandled 500 error instead of a proper 400/404 response.
- **Fix:** Add try/except around the int() conversions or check that band values are numeric before conversion. The function signature promises Optional[list[int]] (None or list) but can actually raise exceptions. Alternatively, wrap the cost_band() call at line 532 of routes.py in a try/except.

### 11. [MEDIUM] Flask SECRET_KEY set to bytes instead of string when SESSION_SECRET_KEY env var is unset
**`app.py:949-951`** · confidence 90 · verifier 95% confirms real

- **Evidence:** flask_app.config["SECRET_KEY"] = os.environ.get( "SESSION_SECRET_KEY", os.urandom(32) ) os.environ.get() returns a string when the key exists, but os.urandom(32) returns bytes. When SESSION_SECRET_KEY is not set, the fallback is bytes instead of string. Flask session cookies expect SECRET_KEY to be a string.
- **Impact:** In environments where SESSION_SECRET_KEY is not configured (e.g., development, staging, or restarts after deployment), the SECRET_KEY is set to bytes instead of the expected string type. While Flask may handle this in some cases, it creates inconsistency and could cause session signing/verification issues.
- **Fix:** Use os.urandom(32).hex() to get a hex string, or convert the bytes to string: os.environ.get('SESSION_SECRET_KEY', os.urandom(32).hex())

### 12. [MEDIUM] Staff email sent without text fallback in send_campaign_submitted_emails
**`shared/email.py:490`** · confidence 90 · verifier 95% confirms real

- **Evidence:** Line 490: `(list(STAFF_EMAILS), staff_subject, staff_html, staff_html)` passes staff_html as both html_body and text_body parameters. Staff email will be sent with only HTML rendering, no text fallback. User email correctly has both: `(user_email, user_subject, user_html, user_text)` with separate text_body.
- **Impact:** Staff members using email clients with HTML disabled or text-only viewers will see raw HTML markup instead of readable text. User confirmation emails work correctly; only internal staff alerts are broken.
- **Fix:** Create a staff_text variable (plain text version) and pass it as the fourth tuple element: `(list(STAFF_EMAILS), staff_subject, staff_html, staff_text)`.

### 13. [MEDIUM] Unhandled exception in _nearest_clean_residues loop logic
**`shared/pdb_preflight.py:978-988`** · confidence 90 · verifier 95% confirms real

- **Evidence:** Line 982-988: inner loop `for d in dropped_ints: if abs(r - d) <= window: ... break` — the break statement exits the inner loop but then continues the outer for-loop iteration. However, the logic is to find the minimum distance from r to ANY dropped hotspot, but only the FIRST dropped hotspot within the window updates candidates[r], then breaks. If multiple dropped hotspots exist, only the first one encountered contributes to the distance calculation for that r.
- **Impact:** When multiple dropped hotspots exist, _nearest_clean_residues returns residues near the FIRST dropped hotspot only, ignoring distance to other dropped hotspots. This gives incomplete suggestions to the user about clean residues near their dropped hotspots.
- **Fix:** Remove the `break` at line 988, allowing all dropped_ints to update candidates[r] with the minimum distance. Alternative: change to compute the true minimum: `dist = min(abs(r - d) for d in dropped_ints if abs(r - d) <= window)`.

### 14. [MEDIUM] Invalid base64 not validated in export_zip
**`app.py:5611`** · confidence 88 · verifier 95% confirms real

- **Evidence:** base64.b64decode(encoded) lacks validate=True. Invalid base64 silently decodes incorrectly rather than raising exception. Compare to correct usage at lines 5464 and 5512 with validate=True.
- **Impact:** Malformed pdb_content_b64 results in corrupted PDB files in the ZIP without warning, creating invalid downloads.
- **Fix:** Change to: base64.b64decode(encoded, validate=True) with proper exception handling

### 15. [MEDIUM] Webhook URL re-validation does not manage socket timeout stack correctly
**`shared/webhooks.py:529-541`** · confidence 85 · verifier 95% confirms real

- **Evidence:** In _dispatch_once line 532-541: validate_webhook_url_safe(target_url) is called. Inside validate_webhook_url_safe (line 335), socket.setdefaulttimeout(_DNS_LOOKUP_TIMEOUT_SECONDS) is set and restored at line 350 inside the finally block. However, if _dispatch_once was called from _bounded_dispatch which could be called on a different thread, and that thread is a daemon thread from sweep_due_deliveries, each call to validate_webhook_url_safe modifies the process-wide socket timeout, which can affect other threads briefly.
- **Impact:** Race condition on socket.getdefaulttimeout() across threads during DNS validation. Multiple webhook dispatch threads could temporarily override each other's socket timeouts, causing one thread's DNS lookup to timeout earlier/later than intended.
- **Fix:** Localize socket operations: instead of changing the process-wide default, use socket.create_connection with explicit timeout or pass timeout to socket.getaddrinfo via a context manager.

### 16. [MEDIUM] Heartbeat state append is not atomic, concurrent writes clobber progress
**`webhooks/modal.py:500-540`** · confidence 78 · verifier 95% confirms real

- **Evidence:** _append_heartbeat_state does read-modify-write without CAS. Concurrent heartbeats at 500-508 fetch, 512-516 merge, 536-538 update. Second read misses first write, causing data loss in _progress or _partial_candidates.
- **Impact:** On high-frequency heartbeats, partial candidates or progress state is lost/overwritten. Users see incomplete or stale progress on status page.
- **Fix:** Use atomic database merge with PostgREST RPC or SQL jsonb operators instead of application-level read-modify-write.

### 17. [MEDIUM] No bounds check on gpu_seconds before int conversion in CAS updates
**`shared/jobs.py:1047,1144`** · confidence 72 · verifier 95% confirms real

- **Evidence:** Lines 1047 and 1144: `int(cumulative_gpu_seconds)` without validation. If payload contains huge/NaN value, int() produces unexpected results persisted to database.
- **Impact:** Malformed heartbeat gpu_seconds corrupts billing records. Incorrect refunds or charges, incorrect workspace cap calculations.
- **Fix:** Validate 0 <= cumulative_gpu_seconds <= MAX_GPU_SECONDS (e.g. 86400) before int() conversion at lines 1047 and 1144.

### 18. [MEDIUM] Off-session PaymentIntent amount conversion loses precision due to unquantized int() truncation
**`billing/checkout.py:540`** · confidence 0.95 · verifier 95% confirms real

- **Evidence:** Line 540: `unit_amount_cents = int(amount * 100)` performs direct truncation of the float product, whereas line 371-374 in the same file correctly uses `.quantize(Decimal("1"), rounding=ROUND_HALF_UP)` before converting to int. A Decimal amount like 10.335 would become 1033 cents (truncate) instead of 1034 cents (properly rounded).
- **Impact:** Auto-reload off-session PaymentIntents will undercharge by up to 1 cent per transaction due to banker's truncation. While individually small, this creates a pattern of systematic undercharging and ledger inconsistency compared to regular Stripe Checkout sessions.
- **Fix:** Change line 540 to: `unit_amount_cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))` to match the pattern at lines 371-374 in create_topup_session().

### 19. [MEDIUM] wallet_preflight returns incorrect hard_cap_usd when REASON_SELF_SERVE_CEILING reason is returned
**`shared/wallet.py:451-459`** · confidence 0.85 · verifier 95% confirms real

- **Evidence:** At line 451-459, when `estimated_cost_usd > SELF_SERVE_CEILING_USD`, the function returns `hard_cap_usd=hard_cap` (the parameter-scaled cap) rather than `hard_cap_usd=SELF_SERVE_CEILING_USD` (the actual $1000 limit). This causes the email handler at line 1065-1072 to pass the wrong cap value to send_job_capped_email(), confusing the user about which limit blocked them.
- **Impact:** User receives email stating an incorrect cost cap when their job exceeds the $1000 self-serve ceiling. If a user's scaled hard cap is lower than $1000 (e.g., $500 for AF2), the email will show $500 instead of the actual $1000 ceiling that blocked them.
- **Fix:** Change line 458 to `hard_cap_usd=SELF_SERVE_CEILING_USD` when returning REASON_SELF_SERVE_CEILING, so users see the correct limit in the email.

### 20. [LOW] Incorrect sort order in list_keys (newest-first comment mismatch)
**`shared/api_keys.py:562`** · confidence 85 · verifier 99% confirms real

- **Evidence:** Comment states 'Active first, newest first within each group.' Code executes: `contexts.sort(key=lambda c: (c.revoked_at is not None, c.created_at or ''), reverse=False)`. With reverse=False and ISO 8601 timestamp strings, created_at sorts ascending (oldest→newest). The code produces oldest-first within each group, contradicting the documented intent of newest-first.
- **Impact:** Users viewing /account/api-keys see their API keys in oldest-first order rather than the documented newest-first order. Least problematic: purely cosmetic UX issue (ordering expectation mismatch). Does not affect functionality or security.
- **Fix:** Change sort to use reverse=True on the created_at field to achieve newest-first within each group, OR update the comment if oldest-first is intentional.


---
*Each finding survived independent adversarial verification (a second agent read the code and tried to refute it). The 14 rejected candidates are omitted.*
