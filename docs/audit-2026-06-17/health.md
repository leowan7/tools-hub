# tools-hub — Code Health (`/health`)

**Date:** 2026-06-17 · **Method:** gstack-style fan-out, 9 subsystems, code-quality lens. Read-only. **75 findings** (1 critical · 17 high · 41 medium · 16 low).

> Quality findings are a maintainability backlog, not adversarially verified like the correctness bugs - triage before acting. Companion to `cso.md` + `REVIEW.md`. Master index: `../AUDIT-2026-06-17-comprehensive.md`.

## Findings
| Sev | Conf | Title | File:line |
|---|---|---|---|
| CRITICAL | 0.9 | tool_submit function excessive cyclomatic complexity | `app.py:3961-4704` |
| HIGH | 0.95 | Module-level imports placed inside function body (unreachable code) | `app.py:361-390` |
| HIGH | 0.95 | Repeated admin auth checks should use decorator pattern | `app.py:5765-5769` |
| HIGH | 0.95 | Duplicate rate card and markup constants across wallet and wallet_estimates modules | `shared/wallet.py, shared/wallet_estimates.py:73-108 (wallet.py), 43-61 (wallet_estimates.py)` |
| HIGH | 0.95 | Duplicated params-extraction pattern across multiple functions | `shared/jobs.py:864-868, 1095-1099` |
| HIGH | 0.95 | Duplicate signed-URL extraction logic creates maintenance debt | `shared/storage.py:122-126 and 367-372` |
| HIGH | 0.95 | Mismatched dimension in docstring vs implementation | `scout/feasibility.py:1-41` |
| HIGH | 0.92 | Repeated render_template with identical error-handling structure | `app.py:4100-4104, 4116-4122, 4141-4144, 4180-4189, 4540-4547, 4605-4612, 4659-4674` |
| HIGH | 0.92 | Wallet frozen check duplicated across topup routes | `app.py:2740-2741 and 2770-2772` |
| HIGH | 0.92 | Overly broad generic Exception catches mask distinct failure modes | `shared/jobs.py:320, 356, 787-790, 1050-1055, 1079-1084` |
| HIGH | 0.9 | Overly broad silent exception handling masks failures | `shared/storage.py:476-477` |
| HIGH | 0.9 | analyze() function exceeds healthy size and complexity | `scout/routes.py:291-527` |
| HIGH | 0.88 | Overly broad exception handling (BLE001 suppressions) | `app.py:161, 181, 514, 560, 805, 827, 2015, 2147, 2706, 2714, 3068, 3731, 5513, 5556, 6166` |
| HIGH | 0.88 | Weak exception handling: 67 bare 'except Exception' clauses without specific error types | `app.py:Multiple throughout file` |
| HIGH | 0.88 | Magic numbers: wallet thresholds and caps not extracted as constants | `app.py:2940-2945 and 2501` |
| HIGH | 0.88 | Duplicated PDB byte-scanning logic in three functions | `shared/pdb_preflight.py:223 (_ca_residue_counts), 694 (_clean_resnums_on_chain), 1014 (inline in _verdict_from_normalizer_value_error)` |
| HIGH | 0.85 | Repeated wallet hold release error handling pattern (7+ instances) | `app.py:888-896, 4169-4179, 4227-4237, 4331-4342, 4529-4539, 4594-4599` |
| HIGH | 0.85 | Duplicated SSE progress handler patterns across two routes | `scout/routes.py:562-648 and 759-868` |
| MEDIUM | 0.95 | Excessive late-binding imports (PLC0415 suppression pattern) | `app.py:136, 207, 1121, 1282, 1283, 4431, 4475, 6073, 6193, 6194` |
| MEDIUM | 0.95 | Inconsistent exception string matching patterns across auth error handling | `shared/auth.py:343-351 vs 505-524 vs 686-691` |
| MEDIUM | 0.95 | Duplicated hotspot residue parsing logic across 5 tool adapters | `tools/bindcraft/__init__.py:35-43` |
| MEDIUM | 0.9 | Repeated wallet hold release exception pattern | `app.py:4530-4539, 4595-4603, 4649-4657, 5531-5538, 6170-6171` |
| MEDIUM | 0.9 | Overly long admin_campaign_save_quote function (182 lines) | `app.py:5995-6175` |
| MEDIUM | 0.9 | Repetitive PreflightResult construction in wallet_preflight function | `shared/wallet.py:434-496 (wallet_preflight)` |
| MEDIUM | 0.9 | Repeated user object polymorphism handling pattern | `shared/auth.py:297-299, 337-339, 536-538, 1136-1145` |
| MEDIUM | 0.9 | Defensive exception catches with no logging context | `shared/pdb_preflight.py:238, 647, 711` |
| MEDIUM | 0.9 | Redundant variable initialization in analyze() route | `scout/routes.py:304-334` |
| MEDIUM | 0.88 | Inconsistent flash message handling in admin routes | `app.py:5797-5814` |
| MEDIUM | 0.88 | Missing validation in resolve_user_email_and_meta: defensive getattr/get chains vulnerable to shape changes | `shared/jobs.py:1270-1284` |
| MEDIUM | 0.88 | Broad except Exception catching without specific error context in email.py | `shared/email.py:70-76, 215-216, 384-397, 1146-1150` |
| MEDIUM | 0.85 | PDB inspection logic duplicated between fresh uploads and reuse paths | `app.py:4069-4156 (fresh uploads) vs. 4549-4612 (reuse tokens)` |
| MEDIUM | 0.85 | Password field not stripped in login/signup, inconsistent with email | `app.py:1497 and 1581` |
| MEDIUM | 0.85 | Repeated auto-reload disable-and-email pattern in auto_reload_if_needed | `shared/wallet.py:720-732 (no_payment_method), 745-757 (no_amount_configured), 800-811 (Stripe error)` |
| MEDIUM | 0.85 | _settle_wallet_hold_for_completed_job: complex multi-path routing with inconsistent error recovery | `shared/jobs.py:816-973` |
| MEDIUM | 0.85 | Over-defensive type-checking logic in _top_candidate_summary | `shared/email.py:186-208` |
| MEDIUM | 0.85 | Large complex function with dense nested decision trees | `shared/pdb_preflight.py:382-636 (preflight_for_tool)` |
| MEDIUM | 0.85 | Three response initialization patterns for SSE error streams | `scout/routes.py:572-584, 771-777, 800-805` |
| MEDIUM | 0.85 | Bare exception handlers across quota and handoff modules | `scout/quota.py:103, 127, 148, 176, 270` |
| MEDIUM | 0.82 | Magic string literals for tool slugs and status values | `app.py:3160-3210 (_PREVIEW_SEO_PHRASES), 3268-3279 (_RELATED_TOOLS), 4054-4058 (reuse token prefixes), 6255-6260 (slot names)` |
| MEDIUM | 0.82 | Hardcoded constant defined inside function instead of at module level | `app.py:4819` |
| MEDIUM | 0.82 | Bare BLE001 exception handlers lack granularity | `app.py:2464, 2473, 2706, 2870, 2960` |
| MEDIUM | 0.82 | Unused STRIPE_EVENTS import indicates incomplete refactoring or dead code path | `webhooks/modal.py:551` |
| MEDIUM | 0.82 | Magic numbers scattered throughout PDB parsing | `shared/pdb_preflight.py, pipeline_normalize.py, uniprot_lookup.py:pdb_preflight lines 22:26 (resnum), 12:16 (atom), 30:38 (x), 38:46 (y), 46:54 (z); uniprot_lookup lines 14:18, 20:24, 26:32, 33:41, 55:60, 62:67` |
| MEDIUM | 0.8 | Cryptic underscore-alias imports reducing readability | `app.py:136, 207, 1121, 1282, 1283, 371-374` |
| MEDIUM | 0.8 | Magic numbers in timeout and pagination without named constants | `app.py:158, 178, 2837, 4715` |
| MEDIUM | 0.8 | Over-complex conditional in admin_campaign_update_status webhook trigger | `app.py:5913-5920` |
| MEDIUM | 0.8 | Overly long auto_reload_if_needed function with complex control flow | `shared/wallet.py:676-813` |
| MEDIUM | 0.8 | Bare except block with pass silently swallows metrics errors, hiding operational issues | `webhooks/modal.py:561-562` |
| MEDIUM | 0.8 | Weak error handling with silent swallowing in idempotency key claim | `shared/idempotency.py:118-155` |
| MEDIUM | 0.8 | Temporary-fix comment left in production code | `shared/webhooks.py:459` |
| MEDIUM | 0.8 | Overly broad exception handling in _json_body() suppresses parse errors without logging | `tools/platform_api/routes.py:113` |
| MEDIUM | 0.8 | Magic numbers scattered through epitope filtering logic | `scout/routes.py:344-432` |
| MEDIUM | 0.8 | Weak exception handling masks errors in cleanup_old_jobs() | `scout/jobs.py:74-90` |
| MEDIUM | 0.8 | Duplicate CSV chain response building in upload, fetch_pdb, and example routes | `scout/routes.py:189-197, 244-252, 273-281` |
| MEDIUM | 0.78 | Inefficient set construction in export_csv: iterating instead of set.update() | `app.py:5324-5327` |
| MEDIUM | 0.78 | Inline nested function _cand_key reduces code clarity and testability | `webhooks/modal.py:523-526` |
| MEDIUM | 0.75 | Inconsistent error-page rendering (status codes) | `app.py:3146, 5597, 5740, 5794, 6022, 6023, 6024` |
| MEDIUM | 0.75 | Duplicate error response patterns in AF2 download handlers | `app.py:5511-5518, 5554-5561` |
| MEDIUM | 0.72 | Loose parsing of hotspot residues with inconsistent error tolerance | `app.py:5045-5056` |
| LOW | 0.9 | Duplicated size-cap constant in storage.py | `shared/storage.py:51, 266` |
| LOW | 0.9 | Duplicated _parse_int utility function across 4 tool adapters | `tools/colabfold/__init__.py:47-54` |
| LOW | 0.88 | Missing documentation for constants and rate sourcing in wallet_estimates.py | `shared/wallet_estimates.py:51-61 (GPU_USD_PER_SECOND, DEFAULT_USD_PER_SECOND)` |
| LOW | 0.85 | Magic constant thresholds scattered across email wallet senders | `shared/email.py:1708-1741, 1836-1880` |
| LOW | 0.85 | Unused imports in tools/platform_api/routes.py | `tools/platform_api/routes.py:37, 41, 50` |
| LOW | 0.78 | Unused 'pass' statements in exception handlers | `app.py:36, 527, 615, 620, 716, 2442, 2454, 2459, 3312, 3589, 3872, 5054, 5979` |
| LOW | 0.75 | Defensive ctx None check repeated in @login_required routes | `app.py:2735-2737, 2766-2768, 2825-2827` |
| LOW | 0.75 | Inconsistent exception handling in billing/checkout.py helper functions | `billing/checkout.py:156-171 (_default_min_topup_usd), 181-191 (_default_max_topup_usd)` |
| LOW | 0.75 | Magic numbers for heartbeat cap and safety ratios scattered across modules | `shared/jobs.py:980, 985-986` |
| LOW | 0.75 | Unreachable return statement in _is_empty_result | `shared/email.py:894-896` |
| LOW | 0.75 | Inconsistent variable naming: kept_count vs residues_kept_per_chain | `shared/pipeline_normalize.py:313, 386` |
| LOW | 0.75 | build_payload functions use unchecked dict subscript access on guaranteed keys | `tools/colabfold/__init__.py:295, 303` |
| LOW | 0.72 | mark_* terminal-status functions lack centralized guard-constraint validation | `shared/jobs.py:377-507` |
| LOW | 0.7 | Implicit session key access without centralization | `app.py:1502, 1557, 1763, 1849, 2008, 2024, 2389, 2713, 5714` |
| LOW | 0.7 | Implicit feature flag in result-shape checking for sequence-design tools | `shared/email.py:872-896, 944-951` |
| LOW | 0.7 | Inefficient file deletion in cleanup_old_jobs() | `scout/jobs.py:80-86` |


### 1. [CRITICAL] tool_submit function excessive cyclomatic complexity
**`app.py:3961-4704`** · confidence 0.9

- **Evidence:** The tool_submit function spans 743 lines (3961-4704) with ~115 conditional branches (if/elif/else/try/except/for), creating a cyclomatic complexity well over 50. The function handles: form validation, PDB inspection, CIF conversion, AlphaFold fetching, preflight gating, storage uploads with 5+ reuse-token branches (job:/handoff:/example:/resample:/alphafold:), wallet hold management, reuse verification, Modal submission, and error recovery. Each branch has nested error handling and multiple similar render_template() calls.
- **Impact:** Extreme difficulty in testing, maintaining, and understanding control flow. High risk of bug propagation when changes are made. Each new reuse-token type requires adding more elif branches. Error handling paths are repetitive and easy to get wrong (wallet hold release code repeated ~7 times with slightly different variations). The function is responsible for wallet hold lifecycle which is critical financial logic.
- **Fix:** Refactor into 4-5 smaller functions: (1) form_validation(), (2) pdb_inspection_and_gating(), (3) resolve_pdb_bytes_for_reuse() [extract the 6-way if/elif at lines 4373-4509], (4) verify_and_stage_pdb(), (5) submit_to_modal(). Create a shared error recovery helper for wallet hold release to eliminate repetition. Consider a strategy pattern or factory for reuse-token resolution.

### 2. [HIGH] Module-level imports placed inside function body (unreachable code)
**`app.py:361-390`** · confidence 0.95

- **Evidence:** Lines 361-390 contain 8 from/import statements placed AFTER 'return None' on line 360 inside the _verify_reuse_pdb_bytes function. This places essential module imports (storage, category_glyphs, metric_glossary, resample, score_legends, tool_base, and tool adapter modules) in dead code that is unreachable. These imports are required throughout app.py but are never executed in normal operation.
- **Impact:** Critical structural issue: all code references to shared.storage, tool_base, scout_bp, and webhook modules will fail at runtime. This file is either broken or these imports are duplicated elsewhere. If duplicated at module level, this is dead code that should be removed. If not duplicated, the app will crash on first use of these dependencies.
- **Fix:** Move lines 361-390 to the module-level imports section at the top of the file (after line 100, before line 144). Remove the dead copy if imports are duplicated elsewhere. Add linting to catch module-level imports in function bodies.

### 3. [HIGH] Repeated admin auth checks should use decorator pattern
**`app.py:5765-5769`** · confidence 0.95

- **Evidence:** Admin routes manually check `email = session.get('user_email', '')` + email validation + STAFF_EMAILS lookup (lines 5765-5769, 5787-5791, 5842-5846, 6014-6018, 6196-6200, 6360-6364). A `require_staff` decorator exists in shared/auth.py but is not used. This pattern repeats 6+ times across admin routes.
- **Impact:** Code duplication creates maintenance hazard — if auth logic changes (e.g., new staff roles or email validation), all 6+ admin routes must be updated separately. Risk of inconsistent implementation if one route is missed. Decorator pattern would enforce consistency.
- **Fix:** Create an `@admin_required` decorator (or use the existing `@require_staff` if available) instead of manual auth checks in each admin route. Reduces ~8 LOC per route and centralizes auth logic.

### 4. [HIGH] Duplicate rate card and markup constants across wallet and wallet_estimates modules
**`shared/wallet.py, shared/wallet_estimates.py:73-108 (wallet.py), 43-61 (wallet_estimates.py)`** · confidence 0.95

- **Evidence:** WALLET_MARKUP (Decimal(1.70)), GPU_USD_PER_SECOND dict, and DEFAULT_USD_PER_SECOND (0.001028) are copied verbatim in both files. wallet_estimates.py uses intentionally lighter imports but lacks documentation comments that explain rate sourcing, while wallet.py includes detailed inline comments ($0.75/hr equivalents). This creates a single source-of-truth problem: updating Modal rates requires patching both modules, risking out-of-sync pricing.
- **Impact:** Maintenance hazard: rate updates could slip one file or introduce accidental divergence between cost estimation and actual billing. Tests may pass with inconsistent state until a manual audit catches the drift. Billing discrepancies are high-impact.
- **Fix:** Extract GPU_USD_PER_SECOND and related constants to a single shared module (e.g., shared/gpu_rates.py or shared/wallet_constants.py) that both wallet and wallet_estimates import from. Keep the detailed Modal rate comments in the source file to document sourcing (2026-05 Modal sticker price, conservatively rounded). Alias in wallet.py for backward compatibility: `from shared.gpu_rates import GPU_USD_PER_SECOND`.

### 5. [HIGH] Duplicated params-extraction pattern across multiple functions
**`shared/jobs.py:864-868, 1095-1099`** · confidence 0.95

- **Evidence:** The params dict comprehension (filtering inputs by non-underscore keys) appears identically in both _settle_wallet_hold_for_completed_job (line 864) and mid_run_monitor_check (line 1095). This is the only way to extract tool parameters, yet both call sites independently construct it.
- **Impact:** Code duplication makes parameter-extraction logic harder to maintain. If the filtering rule ever needs to change (e.g., for a new prefix or validation rule), both sites must be updated in sync or risk subtle inconsistencies.
- **Fix:** Extract the params filtering into a helper function like `_extract_tool_params(inputs: dict) -> dict` and call it from both sites.

### 6. [HIGH] Duplicate signed-URL extraction logic creates maintenance debt
**`shared/storage.py:122-126 and 367-372`** · confidence 0.95

- **Evidence:** presigned_input_url inlines URL key extraction (lines 122-126) checking ('signedURL', 'signedUrl', 'signed_url'), while _extract_signed_url (367-372) implements the same logic with an extra 'url' key fallback. Two implementations of one behavior.
- **Impact:** DRY violation. When supabase-py response keys change in the future, both paths must be updated. presigned_input_url is missing the 'url' key fallback, creating inconsistent behavior. Maintenance cost increases with every bucket type that has its own presigned URL function.
- **Fix:** Refactor all presigned URL functions (presigned_input_url, presigned_campaign_url, presigned_output_put_url, presigned_output_get_url) to delegate to _extract_signed_url instead of duplicating the extraction logic.

### 7. [HIGH] Mismatched dimension in docstring vs implementation
**`scout/feasibility.py:1-41`** · confidence 0.95

- **Evidence:** Module docstring lists 'prior_precedent (0.15)' as a dimension (line 13), but DIMENSION_WEIGHTS dict (lines 35-41) only defines 5 dimensions and includes 'interface_competition' instead. The documented module exports 'six dimensions' but only implements five.
- **Impact:** Documentation/code mismatch creates maintenance hazard: developers reading the docstring expect a 'prior_precedent' dimension that does not exist in actual scoring. If the dimension was intentionally removed, the docstring should be updated to reflect current implementation.
- **Fix:** Update module docstring to match the actual five dimensions being weighted, or restore the missing 'prior_precedent' dimension to DIMENSION_WEIGHTS if it was intentionally removed. Verify with design team which was intended.

### 8. [HIGH] Repeated render_template with identical error-handling structure
**`app.py:4100-4104, 4116-4122, 4141-4144, 4180-4189, 4540-4547, 4605-4612, 4659-4674`** · confidence 0.92

- **Evidence:** At least 8 clusters of near-identical render_template calls for form error handling. All render `adapter.form_template` with `adapter=adapter, error=<msg>, pre_fill=inputs, pdb_source=None, workspace_ctx=workspace_ctx`. Only the error message differs across sites.
- **Impact:** Code duplication makes maintenance harder; changes to error-render context require edits in 8+ places. Increases risk of silent bugs when one site is updated but others miss the change.
- **Fix:** Extract helper `_render_form_error(adapter, error, inputs, workspace_ctx)` that encapsulates the common render_template call signature. Use it consistently across all form validation error paths in tool_submit.

### 9. [HIGH] Wallet frozen check duplicated across topup routes
**`app.py:2740-2741 and 2770-2772`** · confidence 0.92

- **Evidence:** Both `wallet_topup()` (line 2740-2741) and `wallet_checkout()` (line 2770-2772) repeat identical checks: `wallet = get_or_create_wallet(ctx.user_id) or {}; if wallet.get('wallet_frozen'): return redirect(url_for('wallet_overview') + '?wallet_frozen=1')`. This pattern appears in at least 3 places.
- **Impact:** Duplication of wallet frozen check creates maintenance hazard — if the redirect URL or wallet frozen behavior changes, multiple places need updating. Increases bug risk if one location is missed.
- **Fix:** Extract wallet frozen check into a helper function `_check_wallet_frozen(ctx.user_id)` that either returns early or raises an exception, then call it from both routes. Reduces duplication and centralizes the check.

### 10. [HIGH] Overly broad generic Exception catches mask distinct failure modes
**`shared/jobs.py:320, 356, 787-790, 1050-1055, 1079-1084`** · confidence 0.92

- **Evidence:** Functions like create_job (line 320), get_job (line 356), and mid_run_monitor_check (lines 1050-1055) catch bare `Exception` with generic logger.warning/error calls. This collapses auth errors, network timeouts, database constraints, and schema mismatches into a single log bucket with no signal about what actually failed.
- **Impact:** Operational blind spots: a production schema mismatch, a Supabase auth token expiry, and a transient network blip all look identical in logs. On-call engineers cannot distinguish recoverable failures from systemic ones without reading the full exception trace.
- **Fix:** Distinguish common failure modes: catch `ValueError` / `TypeError` (schema/input issues) separately from `ConnectionError` (network) and `PermissionError` (auth). Log the exception message selectively for the generic catch-all so trace details survive even in summary logs.

### 11. [HIGH] Overly broad silent exception handling masks failures
**`shared/storage.py:476-477`** · confidence 0.9

- **Evidence:** output_exists catches all exceptions with no logging: 'except Exception: return False'. Callers depend on False meaning 'file absent', but False could mean 'API error', 'timeout', or 'client crash'.
- **Impact:** Silent failures. If the Storage listing API breaks, the silent False may cause callers to fall back to stale pdb_content_b64 when they should fail hard. No visibility to operators that Storage is broken.
- **Fix:** Add logging before the return: logger.debug('Storage existence check failed for %s/%s', user_id, job_id, exc_info=True). Or catch only the specific exceptions the Storage API raises.

### 12. [HIGH] analyze() function exceeds healthy size and complexity
**`scout/routes.py:291-527`** · confidence 0.9

- **Evidence:** The analyze() route is 237 lines long and contains multiple logical sections: pipeline invocation (lines 306-342), CSV parsing with residue filtering (lines 358-397), epitope ranking (lines 406-432), quality flag computation (lines 443-454), multi-file CSV writing (lines 456-484), and response assembly (lines 515-527). It manages state across all sections with limited separation.
- **Impact:** Long function reduces testability; any change to filtering logic or scoring requires re-testing the entire pipeline. Nested loops with magic numbers (20, 4, 3, 15.0) and unclear intent make refactoring risky. Future epitope scoring changes will likely touch this already-complex function.
- **Fix:** Extract epitope filtering and ranking (lines 358-432) into a helper function. Extract quality flag application (443-454) into another helper. Move CSV writing to a separate utility. This keeps the route handler focused on request handling and orchestration.

### 13. [HIGH] Overly broad exception handling (BLE001 suppressions)
**`app.py:161, 181, 514, 560, 805, 827, 2015, 2147, 2706, 2714, 3068, 3731, 5513, 5556, 6166`** · confidence 0.88

- **Evidence:** 25+ `except Exception:` clauses with `# noqa: BLE001`. Line 161-164 catches any network error on alphafold fetch and returns None. Line 514-515 catches any error outside request context. Lines 2015, 2147 swallow errors to avoid blocking. Line 2706-2710 catches wallet lookup failures silently.
- **Impact:** Broad exception handlers hide real bugs and mask unexpected failures. Reduces observability when services fail. E.g., line 1963 'not ready' catch could mask timeout vs. malformed response—all treated identically.
- **Fix:** Narrow exception filters: catch specific exceptions (requests.Timeout, ValueError, KeyError) instead of `Exception`. For best-effort paths, add explicit log.exception() with context. Document *why* each broad catch exists.

### 14. [HIGH] Weak exception handling: 67 bare 'except Exception' clauses without specific error types
**`app.py:Multiple throughout file`** · confidence 0.88

- **Evidence:** Grep found 67 instances of 'except Exception:' without specifying the exception type. Examples: line 350 in _verify_reuse_pdb_bytes, line 805 in requires_wallet decorator (with noqa BLE001), line 884 in requires_wallet exception handler, line 1225, line 2015, etc. Some are marked with 'noqa: BLE001' (bare exception rule violation) but the noqa doesn't improve code quality — it just suppresses the linter warning. These catch all exceptions including KeyboardInterrupt, SystemExit, GeneratorExit (in Python 3.8+), which should never be caught.
- **Impact:** Silently suppresses unexpected errors that should propagate (e.g., out-of-memory, keyboard interrupts). Makes debugging harder because the exception context is lost to a logger.warning() call. Masks programming errors by treating them the same as expected network/validation errors. Reduces observability of real failures in production.
- **Fix:** Specify exception types: use 'except (ValueError, KeyError, TypeError):' for validation errors, 'except StorageError:' for storage, 'except requests.RequestException:' for network, etc. Use 'except Exception as exc:' only in top-level request handlers where you must prevent crashes. Remove 'noqa: BLE001' comments; let the linter catch these and fix the root cause instead of suppressing it.

### 15. [HIGH] Magic numbers: wallet thresholds and caps not extracted as constants
**`app.py:2940-2945 and 2501`** · confidence 0.88

- **Evidence:** Hard-coded wallet limits scattered throughout code: Decimal('5') min threshold (line 2940), Decimal('100') min monthly cap (line 2944), 0.8 soft block calculation (line 2501), Decimal('10') default threshold (line 2927), Decimal('1000') default monthly cap (line 2933). These are documented in comments but not centralized.
- **Impact:** Magic numbers reduce readability and make business logic changes error-prone. A future wallet rebalancing would require searching code for all threshold references. Risk of updating one place and missing others.
- **Fix:** Define module-level constants at top: `WALLET_THRESHOLD_MIN_USD = Decimal('5')`, `WALLET_CAP_MIN_USD = Decimal('100')`, `SOFT_BLOCK_THRESHOLD = Decimal('0.8')`, etc. Update wallet_auto_reload() to use these instead of literals.

### 16. [HIGH] Duplicated PDB byte-scanning logic in three functions
**`shared/pdb_preflight.py:223 (_ca_residue_counts), 694 (_clean_resnums_on_chain), 1014 (inline in _verdict_from_normalizer_value_error)`** · confidence 0.88

- **Evidence:** Three separate for-loops scan pdb_bytes.split(b'\\n'), decode, and extract chain/resnum/atoms. _ca_residue_counts finds CA atoms, _clean_resnums_on_chain finds backbone atoms, and the ValueError handler does a crude walk. All parse the same fixed-width PDB format independently.
- **Impact:** Bug fixes (e.g., handling edge cases, insertion codes) must be replicated in three places. Changes to column positions require three edits. Testing burden is tripled.
- **Fix:** Extract a shared pdb_line_parser(pdb_bytes, target_chain, record_types=['ATOM']) generator yielding (chain, resnum, icode, atom_name, coords). Callers filter on atom type. Consider vendoring if this syncs with llm-proteinDesigner.

### 17. [HIGH] Repeated wallet hold release error handling pattern (7+ instances)
**`app.py:888-896, 4169-4179, 4227-4237, 4331-4342, 4529-4539, 4594-4599`** · confidence 0.85

- **Evidence:** The pattern 'if hold_tx_id: try: wallet_release_hold(...) except Exception: logger.warning(...)' appears 7+ times throughout tool_submit and other handlers. Each instance has identical error handling but is copy-pasted with slight variable name changes. Shows the pattern in requires_wallet decorator and multiple branches in tool_submit and job_refold.
- **Impact:** Code duplication makes maintenance harder; a fix to error handling logic requires changes in 7+ places. Increases bug risk — if the exception handling needs refinement (e.g., different logging, retry logic), edits must be consistent across all copies. Reading and understanding the codebase requires pattern-matching across multiple locations.
- **Fix:** Extract a helper function like `def _release_wallet_hold_safe(hold_tx_id: str, reason: str) -> None:` that wraps the try/except/logger pattern. Use it everywhere instead of repeating. This single-point-of-change also makes wallet hold safety concerns easy to audit.

### 18. [HIGH] Duplicated SSE progress handler patterns across two routes
**`scout/routes.py:562-648 and 759-868`** · confidence 0.85

- **Evidence:** progress() and feasibility_progress() functions have nearly identical SSE (Server-Sent Events) handling logic: nested _generate(), _run_worker(), gevent vs stdqueue branching, error stream patterns, and SSE formatting. Only the stage_labels dict and pipeline call differ.
- **Impact:** Code duplication makes maintenance harder; any bug fixes or improvements to the SSE pattern must be applied twice. If one is fixed and the other isn't, behavioral inconsistency between routes results.
- **Fix:** Extract the common SSE streaming logic into a reusable helper function (e.g., _create_sse_stream(worker_fn, stage_labels)) that both routes can call, passing only the differing components.

### 19. [MEDIUM] Excessive late-binding imports (PLC0415 suppression pattern)
**`app.py:136, 207, 1121, 1282, 1283, 4431, 4475, 6073, 6193, 6194`** · confidence 0.95

- **Evidence:** 127 instances of `# noqa: PLC0415` suppressing late imports within route handlers and utility functions. Examples: line 207 (`import math as _math`), line 1282-1283 (hmac/secrets inside function), line 4431 (examples import mid-handler). Module-scope imports scattered throughout.
- **Impact:** Scattered imports reduce readability, make dependency graph unclear, and increase cognitive load when tracing execution paths. Some imports (hmac, secrets, json, math) could live at module level.
- **Fix:** Consolidate top-level imports: move hmac, secrets, json, math, datetime, re, and UUID imports to module scope. Keep only truly optional/fallback imports late-bound (e.g., Modal SDK). Document rationale for each remaining PLC0415 suppression.

### 20. [MEDIUM] Inconsistent exception string matching patterns across auth error handling
**`shared/auth.py:343-351 vs 505-524 vs 686-691`** · confidence 0.95

- **Evidence:** verify_login() checks 'invalid|credentials|email' in msg.lower() in-line (lines 345-347), but register_user() extracts to 'low' variable first (line 507), and update_password() checks 'password' + 'weak|short' with separate checks (lines 686-687). Different patterns for identical-purpose exception message parsing create maintenance burden and inconsistent error classification.
- **Impact:** Future developers may miss the pattern when adding similar error handling, leading to inconsistent catch-all behaviors. Harder to audit which exception messages trigger which user-facing responses.
- **Fix:** Extract to shared helper function: def _supabase_error_code(exc: Exception) -> str that returns a unified classification code ('invalid_creds', 'weak_password', 'duplicate_user', 'unknown') so all paths use identical logic.

### 21. [MEDIUM] Duplicated hotspot residue parsing logic across 5 tool adapters
**`tools/bindcraft/__init__.py:35-43`** · confidence 0.95

- **Evidence:** The pattern `hotspot_residues = [int(tok.strip()) for tok in raw_hotspots.split(",") if tok.strip()]` with identical error messages is replicated 5 times across tool adapters (bindcraft, pxdesign, rfantibody, rfdiffusion, boltzgen) instead of being extracted to a shared utility function.
- **Impact:** Maintenance burden: any fix or enhancement to hotspot parsing requires updating 5 separate locations. Testing burden increases with duplication.
- **Fix:** Extract hotspot parsing into a shared utility function in tools/base.py. Define as `parse_hotspot_residues(raw: str) -> tuple[list[int], Optional[str]]` and reuse across all adapters to reduce code duplication by ~30 lines.

### 22. [MEDIUM] Repeated wallet hold release exception pattern
**`app.py:4530-4539, 4595-4603, 4649-4657, 5531-5538, 6170-6171`** · confidence 0.9

- **Evidence:** The pattern 'if hold_tx_id: try: wallet_release_hold(...) except Exception: logger.warning(...)' repeats 5+ times across tool_submit exception paths (storage_failure, reuse_preflight_failed, modal_submit_failure, etc.). Code at lines 4530-4539, 4595-4603, 4649-4657 is nearly identical.
- **Impact:** Duplication increases bug risk if logging format or release logic changes in one place but not others. Exception handler boilerplate obscures the critical path through tool submission.
- **Fix:** Extract `_release_wallet_hold_if_set(hold_tx_id, reason)` helper that wraps try/except/log pattern. Call it from each exception path to reduce duplication.

### 23. [MEDIUM] Overly long admin_campaign_save_quote function (182 lines)
**`app.py:5995-6175`** · confidence 0.9

- **Evidence:** Function spans 182 lines with multiple nested concerns: form parsing (lines 6027-6060), quote validation (lines 6088-6103), webhook dispatch (lines 6141-6171). Line item parsing alone is 20+ LOC (6044-6060). Multiple early redirects on validation failures.
- **Impact:** Length and complexity reduce maintainability. Multiple validation paths and early returns make it hard to trace control flow. Webhook dispatch logic is tightly coupled with quote persistence.
- **Fix:** Extract helpers: `_parse_quote_line_items(form) -> list[dict]`, `_validate_quote_date(raw_date) -> str | None`, `_should_transition_to_quotesent(campaign, form) -> bool`. Move webhook dispatch to separate function. Reduces function to ~100 LOC core logic.

### 24. [MEDIUM] Repetitive PreflightResult construction in wallet_preflight function
**`shared/wallet.py:434-496 (wallet_preflight)`** · confidence 0.9

- **Evidence:** Seven return statements build PreflightResult with identical boilerplate: estimated_cost_usd, balance_usd, deficit_usd, hard_cap_usd fields repeated verbatim. Only the `allow` and `reason` fields vary. Lines 434-441, 443-450, 452-459, 461-468, 472-479, 481-488, 489-496 are near-duplicate blocks.
- **Impact:** Code duplication increases maintenance burden (6 duplicate returns × 6 shared fields = 36 lines of copy-paste risk) and obscures the core logic: the function is a series of guard clauses that could be table-driven. Harder to refactor if PreflightResult gains new fields.
- **Fix:** Refactor using a helper function or early-return guard clause pattern: define a closure `_deny(reason: str) -> PreflightResult` that captures estimated_cost_usd, balance_usd, deficit_usd, hard_cap_usd and returns a deny result with the given reason. Replace each deny return with `return _deny(REASON_WALLET_FROZEN)` etc. Reduces duplication and clarifies intent.

### 25. [MEDIUM] Repeated user object polymorphism handling pattern
**`shared/auth.py:297-299, 337-339, 536-538, 1136-1145`** · confidence 0.9

- **Evidence:** Pattern repeats 4+ times: getattr(user_obj, 'id', None) OR (user_obj.get('id') if isinstance(user_obj, dict) else None). Lines 297-299 handle admin list_users page results. Lines 337-339, 536-538 extract user_id from response.user. Lines 1136-1145 in _resolve_user_email repeat again. No single abstraction for the polymorphic response object (object attr vs dict key).
- **Impact:** Code duplication makes fixing Supabase SDK polymorphism issues harder. If Supabase SDK behavior changes or a new response shape is discovered, all four locations must be updated independently. Test coverage fragmented across multiple functions.
- **Fix:** Extract helper: def _get_user_attr(obj: Any, attr: str, default: Any = None) -> Any that handles getattr + dict.get() polymorphism. Use throughout auth.py and email.py (_resolve_user_email).

### 26. [MEDIUM] Defensive exception catches with no logging context
**`shared/pdb_preflight.py:238, 647, 711`** · confidence 0.9

- **Evidence:** Three 'except Exception: # noqa: BLE001' blocks silently skip on decode or lookup errors. No logging, no visibility. The noqa comment acknowledges it's intentional but doesn't justify silence.
- **Impact:** Preflight failures disappear. If UniProt lookup fails persistently, no operator will know. User confusion when downstream error messages don't match preflight behavior.
- **Fix:** Add logger.debug('...', exc_info=True) before the continue statement. The defensive catch is justified; silence is not.

### 27. [MEDIUM] Redundant variable initialization in analyze() route
**`scout/routes.py:304-334`** · confidence 0.9

- **Evidence:** known_binders is initialized as empty list at line 304, then re-initialized as empty list again at line 312 inside the try block, and again at line 334 inside the exception handler. Only the lines 327-330 actually populate it meaningfully.
- **Impact:** Unnecessary initialization increases cognitive load when reading the code and suggests unclear control flow. If an exception is caught, known_binders is set to [] but the variable was already [] from line 304, making the reset at line 334 redundant.
- **Fix:** Initialize known_binders once at line 304, remove redundant re-initialization at line 312, and initialize it to [] only in the exception handler (line 334) if needed for clarity.

### 28. [MEDIUM] Inconsistent flash message handling in admin routes
**`app.py:5797-5814`** · confidence 0.88

- **Evidence:** admin_campaign_detail() builds flash messages via manual string assignments in long if/elif chain (lines 5797-5814). Same pattern repeats in admin_campaign_update_status and admin_campaign_save_results. Flash messages are hardcoded strings (e.g., 'Status updated.', 'Quote saved.'). Different routes handle missing flash messages differently.
- **Impact:** Fragile message handling — flash message text is scattered and not centralized. If UX requires changing message wording, multiple routes need updates. No single source of truth for flash message keys/values.
- **Fix:** Create a flash message registry constant: `FLASH_MESSAGES = {'status_updated': 'Status updated.', 'quote_saved': ...}`. Use it to build flash_msg by key: `flash_msg = FLASH_MESSAGES.get(flash_key)`. Centralizes message text and makes it easier to maintain.

### 29. [MEDIUM] Missing validation in resolve_user_email_and_meta: defensive getattr/get chains vulnerable to shape changes
**`shared/jobs.py:1270-1284`** · confidence 0.88

- **Evidence:** The function calls `client.auth.admin.list_users()` then iterates all users in memory, falling back from `.users` property to the page object itself. For each user it chains getattr/dict.get with OR fallbacks (lines 1273-1280), but never validates that the user object it extracts is the one it wants. If Supabase pagination changes or the page object structure shifts, the loop still runs and may return stale or incorrect results.
- **Impact:** Silent data corruption: if the page object is unexpectedly empty or the user matching logic fails, the function returns (None, {}) without logging that the user was not found. Completion emails silently skip for unintended users. No audit trail of the mismatch.
- **Fix:** Log the user_id being searched for, count the users found, and raise an explicit warning if the page object lacks a `.users` attribute or is empty. Add assertions that `uid is not None` before comparing to user_id.

### 30. [MEDIUM] Broad except Exception catching without specific error context in email.py
**`shared/email.py:70-76, 215-216, 384-397, 1146-1150`** · confidence 0.88

- **Evidence:** 18 bare except Exception blocks across email.py (lines 70, 105, 215, 264, 384, 396, 402, 502, 560, 794, 842, 1047, 1146, 1188, 1199, 1280, 1291, 1804). Example: line 215 catches any import/function error from score_legends and silently sets legends={}, losing context about whether the import failed or the function call failed. Line 384-397 catches compute_charge_usd errors and falls through without surfacing what computation failed.
- **Impact:** When one of these silent catches fires in production, logs show wallet email errors but hide the root cause (API timeout? Permission error? Supabase unavailable?). Harder to diagnose recurring failures and to set alerting thresholds.
- **Fix:** Catch specific exception types: requests.RequestException for HTTP timeouts, supabase.exceptions for DB errors, ImportError for missing modules. Log the exception type, not just exc_info=True. Add metrics tracking which exception types are caught so operations can respond to patterns.

### 31. [MEDIUM] PDB inspection logic duplicated between fresh uploads and reuse paths
**`app.py:4069-4156 (fresh uploads) vs. 4549-4612 (reuse tokens)`** · confidence 0.85

- **Evidence:** Two nearly-identical inspection blocks: fresh uploads call inspect_pdb_bytes + validate_target_chain + validate_hotspots + CIF conversion inline (lines 4069-4156). Reuse tokens call _verify_reuse_pdb_bytes (lines 4549-4612) which wraps the same calls. Logic is duplicated across paths.
- **Impact:** Duplication risks inconsistent validation. If one path is updated (e.g., stricter CIF handling), the other may not get the fix. Hard to reason about which gates apply to each path.
- **Fix:** Refactor so both paths call shared `_inspect_and_gate_pdb(pdb_bytes, adapter, inputs, filename)` returning (ok: bool, error: str | None). Consolidate validation logic in one place.

### 32. [MEDIUM] Password field not stripped in login/signup, inconsistent with email
**`app.py:1497 and 1581`** · confidence 0.85

- **Evidence:** In login() line 1497: `password = request.form.get('password', '')` (no .strip()). In signup() line 1581: `password = request.form.get('password', '')` (no .strip()). Email is stripped (line 1496, 1580), but password is not. This inconsistency could allow passwords with leading/trailing spaces that the user didn't intend.
- **Impact:** Passwords with accidental whitespace will not match on next login, causing user frustration. Inconsistent handling of password vs email creates confusion for maintainers. Potential security edge case if whitespace has special meaning.
- **Fix:** Consistently apply .strip() to password fields or explicitly document why passwords should NOT be stripped (e.g., to preserve intentional spaces in passphrases). For security best practice, consider NOT stripping password at form level — let auth backend handle it.

### 33. [MEDIUM] Repeated auto-reload disable-and-email pattern in auto_reload_if_needed
**`shared/wallet.py:720-732 (no_payment_method), 745-757 (no_amount_configured), 800-811 (Stripe error)`** · confidence 0.85

- **Evidence:** Three code blocks repeat the same pattern: (1) try to update user_wallets.auto_reload_enabled=False with exception handling, (2) call _send_email_safe('send_auto_reload_failed_email', user_id, reason). Blocks at lines 719-732, 744-757, 799-811 are structurally identical except for the reason string.
- **Impact:** Maintenance hazard: if the disable-and-email behavior needs to change (e.g., add a log entry, change the email function), three sites must be patched. Exception handling is repeated 3x. Violates DRY and makes the function harder to follow (now 138 lines, lines 676-813).
- **Fix:** Extract a helper function `_disable_auto_reload_and_notify(user_id: str, reason: str) -> None` that wraps the update try/except and email dispatch. Call it at lines 719, 744, and 800. Reduces auto_reload_if_needed from 138 to ~110 lines and centralizes the policy.

### 34. [MEDIUM] _settle_wallet_hold_for_completed_job: complex multi-path routing with inconsistent error recovery
**`shared/jobs.py:816-973`** · confidence 0.85

- **Evidence:** The function has three distinct routing paths: (1) refunded-class short-circuit (line 894), (2) billed-class with zero-consumption fast path (line 914), and (3) legacy heuristic fallback (line 951). Each path wraps its settle/release call in try/except with identical logging (lines 900-904, 920-925, 954-958, 969-973). The zero-consumption fast path (lines 914-926) duplicates the refund logic (lines 896-905), and both log at WARNING too—no distinction between expected zero GPU and unexpected error.
- **Impact:** 158 lines of logic that are hard to audit for correctness. Subtle bug risk: if the legacy heuristic path is reached due to an unknown failure_class, the refund/settle decision is made on gpu_seconds alone, bypassing the classified-failure intent. Error logging is uniform, making it hard to distinguish working-as-designed from actual errors.
- **Fix:** Extract release_hold/settle_hold calls into a single `_apply_wallet_settlement(hold_tx_id, failure_class, gpu_seconds, gpu_class, params, failure_reason)` helper that encodes all routing logic once. Use distinct log levels: INFO for expected paths (refund, settle), WARNING for unknown failure_class, ERROR for call failures.

### 35. [MEDIUM] Over-defensive type-checking logic in _top_candidate_summary
**`shared/email.py:186-208`** · confidence 0.85

- **Evidence:** Function performs 7 sequential isinstance/get checks in 23 lines: line 189 checks result is dict; line 192 checks candidates is list; line 195 checks top is dict; line 198 checks scores is dict; lines 201-206 build fallback flat dict with loop over known keys; line 207 checks if scores is still empty. Each returns ('', '', '', ''). Pattern is deeply nested early-exit checking that makes the happy-path logic hard to follow.
- **Impact:** Difficult to trace when a particular output shape maps to empty vs non-empty result. If a new output format is discovered, unclear where the check should be added. Testing requires seeding every combination of partial/malformed responses.
- **Fix:** Extract validation to a separate function: def _extract_scores(result: dict) -> Optional[dict] that returns None on any validation failure, so main logic is: `scores = _extract_scores(result) or {}; if not scores: return empty`. Reduces nesting and clarifies responsibility.

### 36. [MEDIUM] Large complex function with dense nested decision trees
**`shared/pdb_preflight.py:382-636 (preflight_for_tool)`** · confidence 0.85

- **Evidence:** preflight_for_tool is 254 lines with five sequential hard-check blocks (min residues, size envelope, gaps, hotspots, READY decision). Each check may return early or accumulate state for later checks. Tracking dependencies is cognitively heavy.
- **Impact:** Hard to add or reorder checks. Hard to test combinations. High review friction.
- **Fix:** Refactor into a CheckPipeline that runs checks independently and composes the final verdict. Each check returns a CheckResult, accumulated in a PipelineState object. This decouples check logic from verdict rendering.

### 37. [MEDIUM] Three response initialization patterns for SSE error streams
**`scout/routes.py:572-584, 771-777, 800-805`** · confidence 0.85

- **Evidence:** Error stream responses are created three different ways: _error_stream() at line 572-584, inline at 771-777, and _err() at 800-805. All yield SSE-formatted error JSON but with inconsistent generator naming and inline vs. helper patterns.
- **Impact:** Inconsistent patterns reduce code readability and are error-prone. If a developer needs to add a new error stream, they have three conflicting precedents to follow.
- **Fix:** Standardize to a single error-stream pattern: either always use an inline generator or always use a named helper function. Extract the common response_class + headers boilerplate into a reusable SSE response builder.

### 38. [MEDIUM] Bare exception handlers across quota and handoff modules
**`scout/quota.py:103, 127, 148, 176, 270`** · confidence 0.85

- **Evidence:** Multiple 'except Exception:' with only logger.warning/error and no re-raise or context capture. Lines 103-105, 127-129, 148-151, 176-179, and 270-272 catch all exceptions but don't differentiate between transient errors (network timeout) and fatal ones (auth failure).
- **Impact:** Fail-open behavior is intentional for auth resilience, but exception breadth masks bugs. If a Supabase client is misconfigured (e.g., invalid URL), the error is logged as a warning and silently fails open, delaying discovery. Future developers may struggle to debug quota-related issues.
- **Fix:** Narrow exception types where possible (catch supabase.SupabaseException instead of Exception). Add context to log messages with exception type and traceback (exc_info=True). Document the fail-open design choice in comments.

### 39. [MEDIUM] Magic string literals for tool slugs and status values
**`app.py:3160-3210 (_PREVIEW_SEO_PHRASES), 3268-3279 (_RELATED_TOOLS), 4054-4058 (reuse token prefixes), 6255-6260 (slot names)`** · confidence 0.82

- **Evidence:** Tool slugs hardcoded in maps: _PREVIEW_SEO_PHRASES (line 3160+), _RELATED_TOOLS (3268+). Reuse-token prefixes checked as strings ('job:', 'handoff:', 'example:', 'resample:', 'alphafold:') at lines 4054-4058. Slot names ('enrichment_table_csv', 'hits_fasta', 'raw_reads_fastq') at line 6255.
- **Impact:** Magic strings are error-prone. Misspelling 'rfdiffusion' in one place silently breaks features. Changing a slug requires edits across multiple files. No single source of truth.
- **Fix:** Create tools/constants.py exporting SLUG_MPNN='mpnn', SLUG_AF2='af2', etc. Consolidate slug-keyed lookup tables there. Create enums for reuse-token prefixes and result slot names.

### 40. [MEDIUM] Hardcoded constant defined inside function instead of at module level
**`app.py:4819`** · confidence 0.82

- **Evidence:** Line 4819 in job_detail(): NON_PDB_INPUT_TOOLS = frozenset({"af2"}) is defined inside the function. This constant is AF2-specific knowledge that logically belongs with other tool configuration but is hidden inside a view handler. If another non-PDB tool is added, developers must find and edit this hardcoded set inside a function rather than seeing it with other tool configs at the module level.
- **Impact:** Knowledge about tool architecture (which tools take PDB vs FASTA input) is scattered throughout the codebase rather than co-located. Increases on-boarding time and bug risk when new tools are added. The constant is recomputed every time job_detail() runs despite being static.
- **Fix:** Move to module level: _NON_PDB_INPUT_TOOLS = frozenset({'af2'}) at the top of app.py with other _TOOL_CATEGORIES, _HARDCODED_TOOLS, etc. Consider whether this should live in shared/tool_base.py or tools/__init__.py as a core tool registry property instead.

### 41. [MEDIUM] Bare BLE001 exception handlers lack granularity
**`app.py:2464, 2473, 2706, 2870, 2960`** · confidence 0.82

- **Evidence:** Multiple `except Exception: # noqa: BLE001` without specific exception types in wallet/billing endpoints (lines 2464, 2473, 2706, 2870, 2960). e.g., `except Exception: # noqa: BLE001` at 2464 logs 'estimate failed' but masks all failures equally. Network errors, database errors, and logic errors all return 500.
- **Impact:** Silent failures are hard to debug. All exceptions logged as 'estimate failed' or 'ledger lookup failed' make it difficult to distinguish transient errors (network) from bugs (database schema change). No way to retry or handle specific failure modes differently.
- **Fix:** Catch specific exceptions: `except (ConnectionError, TimeoutError): logger.warning(...); retry` vs `except ValueError: return jsonify(...error...)'`. If genuinely broad exception handling needed, at least distinguish in log output by exception type.

### 42. [MEDIUM] Unused STRIPE_EVENTS import indicates incomplete refactoring or dead code path
**`webhooks/modal.py:551`** · confidence 0.82

- **Evidence:** STRIPE_EVENTS is imported with `# noqa: PLC0415, F401` but never referenced. The comment on lines 552-554 hints at intentional dead code. The import exists only to ensure shared.metrics is importable, but that validation is not articulated in the code.
- **Impact:** Confusing intent: future maintainers may assume STRIPE_EVENTS is used and miss that this is placeholder code. If shared.metrics module structure changes, the import will break but tests may not catch it (covered by bare except).
- **Fix:** Remove STRIPE_EVENTS import. Add explicit comment: '# Validate shared.metrics is importable (future: add dedicated tool_jobs metric)' before imports. Or refactor to validate module-load separately without the unused import.

### 43. [MEDIUM] Magic numbers scattered throughout PDB parsing
**`shared/pdb_preflight.py, pipeline_normalize.py, uniprot_lookup.py:pdb_preflight lines 22:26 (resnum), 12:16 (atom), 30:38 (x), 38:46 (y), 46:54 (z); uniprot_lookup lines 14:18, 20:24, 26:32, 33:41, 55:60, 62:67`** · confidence 0.82

- **Evidence:** PDB and DBREF fixed-width column indices are hardcoded. No named constants, scattered across multiple functions and files. Format spec not referenced.
- **Impact:** Readability. Developer unfamiliar with PDB must look up the spec. If the spec changes (unlikely but possible in variants), grep finds them but it's tedious. Hard to verify correctness.
- **Fix:** Define module-level constants: PDB_ATOM_NAME_START, PDB_ATOM_NAME_END, PDB_RESNUM_START, etc. Add a docstring referencing the PDB v3.3 format spec. Or delegate to BioPython's PDB reader (already used in the module).

### 44. [MEDIUM] Cryptic underscore-alias imports reducing readability
**`app.py:136, 207, 1121, 1282, 1283, 371-374`** · confidence 0.8

- **Evidence:** Modules imported with underscore prefixes: `import re as _re` (136), `import math as _math` (207), `from pathlib import Path as _Path` (1121), `import hmac as _hmac` (1282). Intended to dodge linters or avoid collisions, but adds cognitive load.
- **Impact:** New developers must infer why `_re` is used instead of `re`. Inconsistent with direct imports (lines 59-75). Creates unnecessary mental overhead.
- **Fix:** Use standard import names for stdlib (re, math, hmac, secrets). For local modules, drop the underscore or use namespace prefixes (e.g., `from shared.glyphs import category_glyphs`).

### 45. [MEDIUM] Magic numbers in timeout and pagination without named constants
**`app.py:158, 178, 2837, 4715`** · confidence 0.8

- **Evidence:** Timeouts hardcoded as timeout=8 (line 158) and timeout=20 (line 178) in _fetch_alphafold_bytes without explanation. Page size hardcoded as 25 in two places (line 2837 in workspaces_new and line 4715 in jobs_list). No module-level constants for these values, so if they need to be adjusted (e.g., AlphaFold API becomes slower), developers must search and update multiple locations.
- **Impact:** Reduces maintainability and discoverability. If AlphaFold API performance degrades, you must find all timeout values and remember which ones apply to AF. Page size changes require touching multiple views. Makes testing harder — tests must inject magic numbers or repeat them.
- **Fix:** Define module-level constants at the top of app.py: AF_METADATA_TIMEOUT_SEC = 8, AF_PDB_TIMEOUT_SEC = 20, JOBS_PAGE_SIZE = 25. Use these names in all locations. Consider whether different timeouts for metadata vs PDB fetch are intentional or should be unified.

### 46. [MEDIUM] Over-complex conditional in admin_campaign_update_status webhook trigger
**`app.py:5913-5920`** · confidence 0.8

- **Evidence:** Webhook dispatch guard at lines 5913-5920: 6-condition AND chain with deep nesting. Hard to parse and trace individual guard conditions.
- **Impact:** Complex condition is error-prone — easy to misread or accidentally invert a condition. If new guard logic needed, chain grows longer. Makes it hard to test individual guard conditions.
- **Fix:** Extract guard into a helper: `def _should_dispatch_webhook(transitioned, notes_persist_failed, notify_customer) -> bool:` that documents each guard condition separately. Makes intent clearer and testable.

### 47. [MEDIUM] Overly long auto_reload_if_needed function with complex control flow
**`shared/wallet.py:676-813`** · confidence 0.8

- **Evidence:** auto_reload_if_needed spans 138 lines with 8+ guard clauses (wallet_frozen, not_enabled, above_threshold, missing payment method, rate_limited, monthly_cap, misconfigured amount, Stripe failure). Each guard calls _send_email_safe. The function interleaves database reads, Decimal coercions, lazy imports, exception handling, and Stripe calls. Cognitive complexity is high.
- **Impact:** Hard to understand the happy path. Mixing responsibilities: wallet state validation, email dispatch, Stripe API calls, and database updates. Testing requires mocking multiple subsystems. A change to one guard clause risks cascading effects on the overall logic flow.
- **Fix:** Break into smaller functions: (1) `_check_auto_reload_preconditions(wallet: dict) -> Optional[str]` returns a deny reason or None, (2) separate Stripe call logic into a sub-function. Use early return for denials so the happy path (actual Stripe call) is at the end. Reduce to ~100 lines with clearer intent.

### 48. [MEDIUM] Bare except block with pass silently swallows metrics errors, hiding operational issues
**`webhooks/modal.py:561-562`** · confidence 0.8

- **Evidence:** In _observe_terminal, the metrics emission is wrapped in `except Exception: pass` with no logging. If shared.metrics is broken or observe_stripe_event raises, the failure is invisible. The function silently succeeds even though the observable metrics side effect never happens.
- **Impact:** Silent metric loss: missing funnel events for tool completions mean the D3 analytics dashboard sees zero traffic even when jobs are succeeding. Ops cannot detect the outage without external monitoring of Stripe events.
- **Fix:** Replace `pass` with at least a logger.debug call to record the exception. Consider logger.warning if metrics failures should page on-call. Test the exception path explicitly, removing `# pragma: no cover`.

### 49. [MEDIUM] Weak error handling with silent swallowing in idempotency key claim
**`shared/idempotency.py:118-155`** · confidence 0.8

- **Evidence:** _claim_key() catches all Exceptions three times (lines 126, 151, 152) and logs 'failed - failing open' but never distinguishes failure types. Network timeout, permission denied, and deadlock all get the same 'open' response. Caller (wrapped decorator) cannot distinguish between 'key collision' and 'infrastructure problem'.
- **Impact:** Genuine infrastructure problems (timeouts, permission errors) silently escalate to allowing duplicate requests to run, defeating the idempotency purpose. No metrics or high-visibility signal when infra failures occur. Difficult to debug production issues.
- **Fix:** Distinguish exception types: catch database-specific exceptions (supabase.exceptions.*) separately and log at ERROR level with distinct fail-open reason. Emit a metric on each fail-open so operations can see infra degradation.

### 50. [MEDIUM] Temporary-fix comment left in production code
**`shared/webhooks.py:459`** · confidence 0.8

- **Evidence:** Comment 'FIX ME-02 (fresh-review):' explains why broad Exception catching is needed (to avoid orphaning rows on thread death). This signals the catch is a workaround, not intentional design, making future maintainers uncertain.
- **Impact:** Developers may misread the intent and remove the broad catch thinking it's redundant, reintroducing the orphaning bug. The fix is actually correct but the comment frames it as temporary.
- **Fix:** Replace 'FIX ME-02' with a neutral comment: '# Catch all exceptions to prevent thread death and orphaned webhook_deliveries rows. Non-requests exceptions are unexpected but survivable via retry logic.' This clarifies it's intentional design, not a temporary fix.

### 51. [MEDIUM] Overly broad exception handling in _json_body() suppresses parse errors without logging
**`tools/platform_api/routes.py:113`** · confidence 0.8

- **Evidence:** Bare `except Exception:` catches ALL exception types (including SystemExit, OutOfMemory) and silently converts to None without logging. JSONDecodeError would be properly handled, but so would unexpected errors.
- **Impact:** Makes debugging harder: unexpected errors are converted to the same response as invalid JSON. Masks genuine failures that should be logged and monitored.
- **Fix:** Catch specific exceptions: `except (ValueError, TypeError):` instead. Add logger.warning() for caught exceptions to maintain observability of JSON parse failures.

### 52. [MEDIUM] Magic numbers scattered through epitope filtering logic
**`scout/routes.py:344-432`** · confidence 0.8

- **Evidence:** Unexplained magic numbers in epitope filtering: 0.40 (_MIN_COMPOSITE), 5 (_MIN_RESI_COUNT), 0.30 (_MAX_PATCH_FRACTION), 999 (gap sentinel), 20 (gap threshold), 4 (fill-gap threshold), 15.0 (_MIN_CENTROID_DIST), 3 (top-N limit). Some are named constants but scattered; 999 and 20 are inline literals.
- **Impact:** Magic numbers are hard to adjust without understanding intent. If product decides 'top 5 epitopes instead of top 3', the code at line 431 requires searching for all references to this limit.
- **Fix:** Move all magic numbers to module-level constants with descriptive names at the top of routes.py, e.g., TOP_EPITOPE_COUNT = 3, RESIDUE_GAP_THRESHOLD = 20, CENTROID_DISTANCE_MIN = 15.0, with comments explaining their origin (e.g., 'from empirical validation study XYZ').

### 53. [MEDIUM] Weak exception handling masks errors in cleanup_old_jobs()
**`scout/jobs.py:74-90`** · confidence 0.8

- **Evidence:** OSError exceptions during directory removal (rglob, unlink, rmdir) are silently caught and skipped (line 88-90). If a permission error or filesystem corruption prevents cleanup, the function returns a count that includes failed deletions as successes. No logging occurs.
- **Impact:** Silent failures in cleanup can mask filesystem issues. If a stale job directory accumulates due to permission errors, the operator never learns about the problem. The count returned is inaccurate, misleading callers about cleanup progress.
- **Fix:** Add logging to the except block (e.g., logger.warning(f'Failed to delete {entry}', exc_info=True)) to surface filesystem errors. Optionally, return a tuple (deleted_count, failed_count) to give callers visibility into partial failures.

### 54. [MEDIUM] Duplicate CSV chain response building in upload, fetch_pdb, and example routes
**`scout/routes.py:189-197, 244-252, 273-281`** · confidence 0.8

- **Evidence:** Three routes (upload, fetch_pdb, example) each build identical response JSON with chains list comprehension from result.chains. The logic and format are identical across all three, but no shared helper exists.
- **Impact:** If the chains response format changes (e.g., add a 'features' field), all three routes must be updated independently. The duplication is small but forces developers to update multiple places for what is logically one contract.
- **Fix:** Extract a helper function _format_job_response(job_id, filename, chains, structure_title) that returns the standardized response dict. All three routes call it, ensuring consistency.

### 55. [MEDIUM] Inefficient set construction in export_csv: iterating instead of set.update()
**`app.py:5324-5327`** · confidence 0.78

- **Evidence:** Lines 5324-5327 in export_csv: for loop iterates candidates, then nested for loop over score keys, checking 'if k not in all_score_keys' before append. This is O(n²) in the number of unique score keys. Should use set operations or dict.setdefault() to avoid repeated membership checks.
- **Impact:** Inefficient for jobs with many candidates or many score types. Not a bottleneck for typical use cases but shows pattern of suboptimal Python. Duplicate code: lines 5360 and 5601 also iterate with enumerate(candidates) and handle similar sequence extraction.
- **Fix:** Replace lines 5324-5327 with: all_score_keys = set(); [all_score_keys.update(cand.get('scores', {}).keys()) for cand in candidates]; all_score_keys = sorted(all_score_keys). Or extract a common '_extract_candidates_and_scores' helper since the same pattern appears in export_csv, export_fasta, and export_zip.

### 56. [MEDIUM] Inline nested function _cand_key reduces code clarity and testability
**`webhooks/modal.py:523-526`** · confidence 0.78

- **Evidence:** Inside _append_heartbeat_state, _cand_key is defined as a nested function (lines 523-526) then immediately used to build a set comprehension (line 530). The function is trivial and never reused or tested independently.
- **Impact:** Readability: readers must scan three scope levels to understand what _cand_key does. The nested scope also makes static analysis harder and prevents independent unit testing.
- **Fix:** Inline the logic directly into the set comprehension or move to module scope as `_candidate_key(cand)` for testability.

### 57. [MEDIUM] Inconsistent error-page rendering (status codes)
**`app.py:3146, 5597, 5740, 5794, 6022, 6023, 6024`** · confidence 0.75

- **Evidence:** 404 responses rendered inconsistently: line 3146 returns `(render_template('404.html'), 404)`. Others return `render_template(...), 404` or sometimes just render_template without explicit status. Some paths lack status codes entirely.
- **Impact:** Inconsistent HTTP status codes. Some 404s may not set status correctly, confusing clients and caches. Hard to audit all error paths.
- **Fix:** Create helper `render_404()` returning `(render_template('404.html'), 404)`. Replace all ad-hoc 404 renders with this helper. Same for 403, 500 error pages.

### 58. [MEDIUM] Duplicate error response patterns in AF2 download handlers
**`app.py:5511-5518, 5554-5561`** · confidence 0.75

- **Evidence:** af2_download_pdb (line 5511-5518) and af2_download_pae (line 5554-5561) have identical error handling: try base64.b64decode() except Exception, then return Response() with '# Malformed <X> payload' and status=500. Similar pattern also in job_candidate_pdb (line 5463-5470) with '# Malformed PDB payload'. These could be a shared helper.
- **Impact:** Copy-paste error handling makes the code harder to maintain. If you need to add retry logic or change the error response format, you must update 3+ places. Each decode operation should have consistent error behavior.
- **Fix:** Extract helper: def _decode_b64_or_error(encoded: str, description: str) -> tuple[bytes | None, Response | None]. Returns (decoded_bytes, error_response) so caller handles success/failure clearly. Use in all 3 locations to unify error handling.

### 59. [MEDIUM] Loose parsing of hotspot residues with inconsistent error tolerance
**`app.py:5045-5056`** · confidence 0.72

- **Evidence:** In job_refold, lines 5046-5055 parse hotspot_residues from string by splitting on ',' and ';', wrapping each parse in try/except ValueError, and silently skipping unparseable tokens. Line 5056 then uses a list comprehension [int(x) for x in raw_hotspots if str(x).strip()] which is less robust (no try/except, no comma/semicolon splitting). The two approaches should be unified.
- **Impact:** Inconsistent behavior: a user refolding a job with hotspots specified as 'A,B,C' (non-numeric) might get different results on refold vs original submit. The list comprehension also calls str(x).strip() on what should already be an int, indicating confusion about type expectations.
- **Fix:** Consolidate to a single _parse_hotspots(raw: str | list) -> list[int] helper that handles both string and list input, splits on both ';' and ',', wraps parsing in try/except, and logs warnings for unparseable entries. Use it everywhere (tool_submit, job_refold, _verify_reuse_pdb_bytes).

### 60. [LOW] Duplicated size-cap constant in storage.py
**`shared/storage.py:51, 266`** · confidence 0.9

- **Evidence:** MAX_UPLOAD_BYTES = 20 * 1024 * 1024 and MAX_CAMPAIGN_RESULT_BYTES = 20 * 1024 * 1024. Same numeric literal appears twice. Line 266 comment says it 'matches MAX_CONTENT_LENGTH', hinting they should be synchronized.
- **Impact:** If the cap changes, both must be edited. If they diverge, one path accepts what the other rejects.
- **Fix:** Define a single FILE_SIZE_CAP_BYTES = 20 * 1024 * 1024 and use it for both constants. Add a comment that it must match Flask's MAX_CONTENT_LENGTH config.

### 61. [LOW] Duplicated _parse_int utility function across 4 tool adapters
**`tools/colabfold/__init__.py:47-54`** · confidence 0.9

- **Evidence:** Identical 8-line _parse_int function appears in af2, colabfold, boltzgen, and mpnn modules. Each has the same logic: try int(value), catch TypeError/ValueError, return default.
- **Impact:** Code duplication (~32 lines total). If a bug is found in the parsing logic, 4 locations must be updated.
- **Fix:** Move _parse_int to tools/base.py and import it in all adapters. This consolidates the utility in one location and makes it available to all adapters.

### 62. [LOW] Missing documentation for constants and rate sourcing in wallet_estimates.py
**`shared/wallet_estimates.py:51-61 (GPU_USD_PER_SECOND, DEFAULT_USD_PER_SECOND)`** · confidence 0.88

- **Evidence:** wallet_estimates.py lacks inline comments for GPU_USD_PER_SECOND entries (e.g., no $ per hour equivalents). wallet.py includes them (lines 101-107: 'A10G 0.000208 # $0.75/hr'). The wallet_estimates.py comment at line 48-50 points to wallet.py's comment, creating a confusing reference loop that assumes users will cross-reference two files.
- **Impact:** Developers reviewing wallet_estimates.py cannot quickly verify rates without jumping to wallet.py. Makes code reviews less efficient and rate-update verification harder. Violates self-documentation principle.
- **Fix:** Duplicate the inline comments in wallet_estimates.py GPU_USD_PER_SECOND so each rate entry includes the $/hr equivalent (e.g., `'A10G': 0.000208, # $0.75/hr`). Or add a module-level comment explaining the source and update process ('Modal rate card as of 2026-05, sourced from modal.com/pricing. Update annually or when Modal raises prices.').

### 63. [LOW] Magic constant thresholds scattered across email wallet senders
**`shared/email.py:1708-1741, 1836-1880`** · confidence 0.85

- **Evidence:** send_pilot_intro_email() triggers at $1,000 30-day spend (docstring line 1716). alert_sales_slack() fires at $5,000 (line 1842). alert_sales_slack_high() fires at $10,000 (line 1865). These thresholds are hardcoded string literals in docstrings/comments with no central constant table. Different code paths may reference different spend thresholds.
- **Impact:** Threshold changes require searching code comments and docstrings rather than updating a config. Risk of accidentally having send_pilot_intro fire at different thresholds depending on which callers invoke it. No validation that callers pass spend that actually crosses the threshold.
- **Fix:** Define constant module dict at top of email.py: WALLET_ALERT_THRESHOLDS = {'pilot_intro': 1000, 'sales_lead': 5000, 'sales_high_value': 10000}. Pass threshold to each sender function and validate caller's spend against it.

### 64. [LOW] Unused imports in tools/platform_api/routes.py
**`tools/platform_api/routes.py:37, 41, 50`** · confidence 0.85

- **Evidence:** Three imports declared but never used: Response (line 37, only in deferred string annotation), API_TERMINAL_STATUSES (line 41), and list_user_campaigns (line 50). grep confirms no usage beyond import statements.
- **Impact:** Clutters module imports and confuses readers about intended API surface. Minimal runtime impact.
- **Fix:** Remove unused imports: drop Response from line 37, remove API_TERMINAL_STATUSES and list_user_campaigns from the import block.

### 65. [LOW] Unused 'pass' statements in exception handlers
**`app.py:36, 527, 615, 620, 716, 2442, 2454, 2459, 3312, 3589, 3872, 5054, 5979`** · confidence 0.78

- **Evidence:** 13 bare `pass` statements, mostly after `except ImportError: pass` or best-effort blocks. Line 36 after dotenv ImportError. Lines 527, 615, 620 nested in import fallback blocks. Serve no functional purpose.
- **Impact:** Minor: reduces code clarity and consumes a line without adding value. Some linters flag empty except blocks.
- **Fix:** Remove unused `pass` statements. If an except genuinely does nothing, add a comment explaining why (e.g., # intentionally swallow ImportError).

### 66. [LOW] Defensive ctx None check repeated in @login_required routes
**`app.py:2735-2737, 2766-2768, 2825-2827`** · confidence 0.75

- **Evidence:** Multiple routes with @login_required decorator manually check `ctx = load_user_context(); if ctx is None: return redirect(url_for('login'))` (lines 2735-2737, 2766-2768, 2825-2827, etc.). Pattern repeats 15+ times across wallet and account routes.
- **Impact:** Dead code or incomplete decorator: if @login_required guarantees session user exists, these None checks are unreachable. If load_user_context can still return None, the decorator is insufficient. Either way, this pattern shouldn't repeat everywhere.
- **Fix:** Either: (1) Guarantee load_user_context never returns None for @login_required routes by strengthening the decorator, OR (2) Remove repetitive checks and let decorator handle it, with a comment explaining why it's safe to skip the None check.

### 67. [LOW] Inconsistent exception handling in billing/checkout.py helper functions
**`billing/checkout.py:156-171 (_default_min_topup_usd), 181-191 (_default_max_topup_usd)`** · confidence 0.75

- **Evidence:** Both functions read env vars and parse Decimals with identical try/except/logger.warning patterns. _default_min_topup_usd falls back to import + return, then naked Decimal(20). _default_max_topup_usd just returns Decimal(5000). If the import fails, it silently returns the hardcoded value but logs nothing.
- **Impact:** Silent fallback obscures whether the env var was malformed, missing, or if MIN_TOPUP_USD import failed. Debugging is harder when the configured floor is ignored. The two functions behave differently on import failure, violating the principle of least surprise.
- **Fix:** Factor out an `_load_decimal_from_env(env_key: str, fallback: Decimal) -> Decimal` helper that consistently handles both env var parsing and import failures. Have both _default_min_topup_usd and _default_max_topup_usd use it.

### 68. [LOW] Magic numbers for heartbeat cap and safety ratios scattered across modules
**`shared/jobs.py:980, 985-986`** · confidence 0.75

- **Evidence:** MID_RUN_MONITOR_INTERVAL_MINUTES (15), _MID_RUN_WARN_RATIO (1.5), and _MID_RUN_KILL_RATIO (2.0) are module-level constants in jobs.py. But other thresholds appear inline: the 1000-candidate cap (webhooks/modal.py:533) and 64-contact cap (line 448).
- **Impact:** Maintenance friction: heartbeat ratios are easy to find and tune, but result-sanitization limits are scattered. No single place documents the design rationale for all safety thresholds.
- **Fix:** Create a shared constants module (e.g., shared/limits.py) with all thresholds and comments explaining the intent.

### 69. [LOW] Unreachable return statement in _is_empty_result
**`shared/email.py:894-896`** · confidence 0.75

- **Evidence:** Function checks: if result.get('pdb_b64'): return False, else fall through to return False. Both branches return the same value, making the condition ineffective. Line 896 return False is always reached. The pdb_b64 check appears to be a placeholder for future logic but is currently dead code.
- **Impact:** Function always returns False for succeeded jobs with any pdb_b64 value (as intended), but the explicit check adds no value and signals incomplete implementation. Future maintainers may assume the pdb_b64 case is special-handled when it is not.
- **Fix:** Either complete the logic (pdb_b64 presence means success, return True for structure-prediction tools) OR remove the unreachable branch and simplify to three explicit checks (sequences, candidates, pdb_b64) with clear semantics.

### 70. [LOW] Inconsistent variable naming: kept_count vs residues_kept_per_chain
**`shared/pipeline_normalize.py:313, 386`** · confidence 0.75

- **Evidence:** Local variable `kept_count` accumulates residues per chain (line 313), then stored in `kept_per_chain[chain_id]` (line 386). Name `kept_count` is ambiguous; the report field is `residues_kept_per_chain`.
- **Impact:** Minor readability friction. Requires clarification that 'kept' means residues, not atoms or records.
- **Fix:** Rename `kept_count` to `residues_kept_this_chain` and `dropped_count` to `residues_dropped_this_chain` for consistency with report field names.

### 71. [LOW] build_payload functions use unchecked dict subscript access on guaranteed keys
**`tools/colabfold/__init__.py:295, 303`** · confidence 0.75

- **Evidence:** build_payload accesses inputs["num_recycles"] and inputs["use_templates"] without .get() fallback. While validate() guarantees these keys, the pattern is not defensive and could raise KeyError if called unexpectedly.
- **Impact:** Low current risk since validate() always produces these keys. But not Pythonic defensive programming; fragile if build_payload is called from new code paths with manually-constructed dicts.
- **Fix:** Use inputs.get("key", default) for optional keys, or add entry-point assertions documenting required keys. Consider converting inputs to a typed dataclass to enforce schema at type level.

### 72. [LOW] mark_* terminal-status functions lack centralized guard-constraint validation
**`shared/jobs.py:377-507`** · confidence 0.72

- **Evidence:** mark_running, mark_succeeded, mark_failed, mark_timeout, and mark_cancelled each accept `allowed_current` kwarg that defaults to _NON_TERMINAL. The _cas_update helper validates non-empty allowed_current (line 1497-1500), but callers can pass empty tuples and only discover the error at database time.
- **Impact:** Silent bugs: passing `allowed_current=()` by mistake raises ValueError inside _cas_update during database execution, not at the mark_* call site. Tests may not cover all paths.
- **Fix:** Validate `allowed_current` in mark_* functions before calling _cas_update, or make it private and always use _NON_TERMINAL as the guard (current calls already do this anyway).

### 73. [LOW] Implicit session key access without centralization
**`app.py:1502, 1557, 1763, 1849, 2008, 2024, 2389, 2713, 5714`** · confidence 0.7

- **Evidence:** Session data accessed via hardcoded string keys ('user_email', 'user_id', 'anon_session_id') scattered throughout. Lines 1502, 1557, 1763 etc. all call `session.get('user_email')` directly. If a key is misspelled, it silently returns None.
- **Impact:** No type safety; typos in session keys cause silent failures. Hard to audit what session keys the app uses without grepping.
- **Fix:** Create SessionKeys constants file (USER_EMAIL='user_email', USER_ID='user_id'). Use these throughout. Optionally add TypedDict-based session wrapper for type hints.

### 74. [LOW] Implicit feature flag in result-shape checking for sequence-design tools
**`shared/email.py:872-896, 944-951`** · confidence 0.7

- **Evidence:** _is_empty_result() treats sequences (sequence-design tools like MPNN) as a valid output shape. _result_summary() line 946-951 renders N sequences returned message. But _top_candidate_summary() (lines 186-247) returns empty strings for sequence-design results (line 195 checks for scores in candidates, which sequences don't have). No explicit code path for sequence-design tool emails, making it unclear whether job-complete emails are sent for MPNN runs.
- **Impact:** Future maintainers adding a new sequence-design tool must hunt through email.py to find all the shape-dependent logic. Current code appears to support sequences but may silently fail to extract meaningful scores for them.
- **Fix:** Extract tool-specific result extraction: def _extract_result_metadata(job, tool_slug) -> dict that returns structured metadata (result_type, scores, sequences) based on known tool patterns. Use this in email renderers so tool-specific logic is centralized.

### 75. [LOW] Inefficient file deletion in cleanup_old_jobs()
**`scout/jobs.py:80-86`** · confidence 0.7

- **Evidence:** cleanup_old_jobs() uses rglob('*') to iterate all nested files, then calls unlink() on each individually, then rmdir() on the parent. This is O(n) unlink calls. More efficient to use shutil.rmtree() in a single operation.
- **Impact:** On filesystems with high-latency per-syscall (e.g., network storage), iterating and unlinking each file separately is much slower than a single rmtree. With thousands of job directories, this could become a bottleneck.
- **Fix:** Replace the rglob + unlink loop with shutil.rmtree(entry, ignore_errors=False) wrapped in try-except. This is atomic and much faster on most filesystems. Verify shutil is imported.

