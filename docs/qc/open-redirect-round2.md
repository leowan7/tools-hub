# QC round 2 — PR #152, open redirect on the `/login` `next` parameter

Independent QC. I did not build this change. Round 1 raised two blocking
findings (B-1: a new unauthenticated HTTP 500; B-2: four surviving
mutations); both are claimed fixed. This round verifies the fix and hunts
for anything the fix introduced.

**Verdict: MERGE.** All nine contract items PASS. Three non-blocking
observations are recorded at the end.

---

## 0. Provenance

Confirmed against the remote before starting and again at the end —
unchanged both times:

```
$ git ls-remote origin refs/heads/fix/open-redirect-next-param refs/heads/main
96012ed3d00decefdfb14648d521605345ef32b5	refs/heads/fix/open-redirect-next-param
fa938b09fd43aae6fc06976756f5c6fe379a537f	refs/heads/main
```

| | |
|---|---|
| PR head under review | `96012ed` (merge of `main` into the branch) |
| Fix commits | `383760a` (guard), `5000246` (B-1 fix) |
| Base | `fa938b0` — **the base has moved** since round 1 (was `2c057fc`) |
| `git merge-base fa938b0 96012ed` | `fa938b0` — branch is fully up to date on current trunk |
| Python | 3.13.0 · Werkzeug 3.1.8 |

Diff vs current trunk: `blueprints/auth.py` (+97/-…), `blueprints/wallet.py`
(13), `tests/test_login_redirect.py` (217). Three files, nothing else.

All work was done in throwaway worktrees; the main working tree was never
touched.

---

## 1. Pin the SHA — **PASS**

See above. `96012ed` is the live remote head of the branch, and its
merge-base with `origin/main` *is* `origin/main`, so the branch carries no
stale base.

## 2. Baselines, measured myself — **PASS**

Exact command, from each worktree root, no path argument, no `tail`:

```
venv/Scripts/python.exe -m pytest -q
```

| Tree | Result |
|---|---|
| `fa938b0` (current `origin/main`) | `5029 passed, 20 skipped in 189.58s (0:03:09)` |
| `96012ed` (PR head) | `5185 passed, 20 skipped in 187.66s (0:03:07)` |

**Delta: +156 passed, skips unchanged at 20. Zero failures either side.**

Reconciliation. The builder's numbers (4897 → 5053, 19 skipped) were taken
against the *old* base `2c057fc`; trunk has since absorbed PRs #144/#145,
which is why the absolute counts differ. The quantity that must survive a
base change is the delta, and it does — exactly:

```
$ pytest -q --collect-only tests/test_login_redirect.py
fa938b0:    7 tests collected
96012ed:  163 tests collected
```

`163 - 7 = +156`, and `5185 - 5029 = +156`. The two agree to the test, and
no other test file changed. The builder's `4897 - 7 + 163 = 5053` arithmetic
is the same claim expressed against the old base. **Claim verified.**

## 3. B-1 re-driven, not accepted — **PASS**

I wrote my own driver rather than reading the PR's table or reusing its test
file: `create_app()`, asserted `TESTING` off, `debug` off,
`PROPAGATE_EXCEPTIONS` unset, so an escaping exception really does become a
500.

```
app shape: TESTING=False DEBUG=False PROPAGATE=None

=== B-1: GET /login?next=<payload> on production-shaped app ===
  '//['                    GET -> 200  OK
  '//]'                    GET -> 200  OK
  '//[]'                   GET -> 200  OK
  '//[::1'                 GET -> 200  OK
  '//%5B'                  GET -> 200  OK
  '/%09/%5B'               GET -> 200  OK
  '//%EF%BC%83evil.com'    GET -> 200  OK
```

All seven now 200. The two that need no illegal bytes on the wire (`//%5B`,
`/%09/%5B`) are among them. **B-1 is fixed.**

### Going further than the seven

Fuzzed `urlsplit` over 230,655 candidates (systematic products over a
control/Unicode/delimiter alphabet, plus 200k random strings):

```
candidates: 230655
raising inputs found: 739, exception types: {'ValueError': 739}
safe_next misbehaviours on raising inputs: 0

GET /login 5xx over 400 raising payloads: 0
GET /login 5xx over 4000 arbitrary payloads: 0
safe_next accepted-but-off-origin: 0
```

739 distinct raising inputs on this Python — **all** handled, none 500s, and
nothing `safe_next` accepts survives a same-origin re-check of the
serialised result. I also drove 4000 *arbitrary* (not just raising) payloads
through `GET /login` looking for a raise anywhere else in the request path,
including the template render: zero 5xx.

### The bug this PR actually fixes is live on trunk right now

Driving `POST /login` with `next='/<byte>/evil.com'` and reading the
**serialised** `Location` header:

| byte | `fa938b0` (trunk) | `96012ed` (PR) |
|---|---|---|
| NUL 0x00 | 302 `/%00/evil.com` | 302 `/` |
| **TAB 0x09** | **302 `//evil.com` — OFF-ORIGIN** | 302 `/` |
| LF 0x0a | **500** | 302 `/` |
| VT 0x0b | 302 `/%0B/evil.com` | 302 `/` |
| FF 0x0c | 302 `/%0C/evil.com` | 302 `/` |
| CR 0x0d | **500** | 302 `/` |
| SPACE 0x20 | 302 `/%20/evil.com` | 302 `/%20/evil.com` |
| DEL 0x7f | 302 `/%7F/evil.com` | 302 `/` |
| NEL 0x85 | 302 `/%C2%85/evil.com` | 302 `/` |
| C1 0x9f | 302 `/%C2%9F/evil.com` | 302 `/` |

Trunk emits `Location: //evil.com` today. The PR closes it, and
incidentally also closes the CR/LF 500 that trunk has.

This table also **independently confirms the corrected docstring**: TAB is
the only byte silently stripped; VT/FF/NUL/DEL/NEL are percent-encoded
exactly like SPACE; CR/LF are refused with a 500. All three claims hold.
SPACE surviving as `%20` is correct and deliberate — still same-origin.

## 4. Attack on the broad `except Exception` — **PASS**

- **`KeyboardInterrupt` / `SystemExit` / `GeneratorExit` are not caught.**
  Verified: all three are `BaseException`, not `Exception`
  (`issubclass(...)=False`). No shutdown-signal concern.
- **Masking scope is one stdlib call.** The `try` block contains exactly
  `parts = urlsplit(value)` — nothing else can throw inside it, so there is
  no application bug it could hide. `MemoryError`/`RecursionError` *are*
  subclasses and would be swallowed, but the result is the safe fallback,
  not a wrong redirect.
- **The fallback is always safe.** `fallback` defaults to `"/"` and a
  repo-wide grep finds exactly two call sites, `blueprints/auth.py:120` and
  `:133`, **neither of which passes a `fallback`**. So the returned value is
  the literal `"/"` in every reachable case.
- **Breadth is currently unnecessary but justified.** I ran the narrowing
  mutation `except Exception:` → `except ValueError:` (unique anchor, landing
  verified): `163 passed`. On CPython 3.13 `urlsplit` raises only
  `ValueError` — my fuzz confirms this (739/739 `ValueError`). So the broad
  catch is portability insurance for other CPython versions, exactly as the
  docstring says. Keeping it is the right call and costs nothing.

## 5. All mutations re-run myself — **PASS**

Harness: apply → read back from disk and diff to **prove the edit landed** →
run `tests/test_login_redirect.py` → parse `FAILED` lines for test names →
restore and assert byte-identical. A run with no parseable summary line is
treated as an ERROR, never as green (round 1's failure mode). Every mutation
below reported a real summary line and a real failing-test name.

### The builder's 10 — **9 RED, 1 survivor. Claim verified exactly.**

| # | Mutation | Verdict | pytest summary |
|---|---|---|---|
| M1 | drop `parts.scheme` | RED | `12 failed, 151 passed` |
| M2 | drop `parts.netloc` | **SURVIVED** | `163 passed` |
| M3 | drop `path.startswith("/")` | RED | `8 failed, 155 passed` |
| M4 | `startswith("/")` → `"/" in path` | RED | `4 failed, 159 passed` |
| M5 | delete control-char layer | RED | `20 failed, 143 passed` |
| M6 | drop `startswith("//")` | RED | `8 failed, 155 passed` |
| M7 | drop `startswith("/\\")` | RED | `12 failed, 151 passed` |
| M8 | delete layers 3+4 | RED | `20 failed, 143 passed` |
| M9 | remove the `try`/`except` | RED | `47 failed, 116 passed` |
| M10 | `except` returns `value` not fallback | RED | `40 failed, 123 passed` |

Failures were confirmed **by name**, not merely by redness. Representative:

```
M9-drop-try   RED  47 failing cases | 47 failed, 116 passed in 7.69s
        FAILED-BY-NAME: tests/test_login_redirect.py::test_login_get_sanitises_hidden_next
        FAILED-BY-NAME: tests/test_login_redirect.py::test_login_get_survives_urlsplit_raises
        FAILED-BY-NAME: tests/test_login_redirect.py::test_login_post_failure_rerenders_sanitised_next
        FAILED-BY-NAME: tests/test_login_redirect.py::test_login_post_rejects_offsite_next
        FAILED-BY-NAME: tests/test_login_redirect.py::test_login_post_survives_urlsplit_raises
        FAILED-BY-NAME: tests/test_login_redirect.py::test_safe_next_never_raises
        FAILED-BY-NAME: tests/test_login_redirect.py::test_safe_next_rejects_offsite
```

M9 and M10 — the two that specifically undo the B-1 fix — are killed by
`test_login_get_survives_urlsplit_raises` / `test_safe_next_never_raises`,
i.e. the round-1 regression is pinned by tests that name it.

### 7 new mutations the builder did not try

| # | Mutation | Verdict | Assessment |
|---|---|---|---|
| N1 | control range → C0 only (drop DEL/C1 half) | RED `8 failed` | killed |
| N2 | scan `parts.path` instead of `value` | SURVIVED | not exploitable (below) |
| N3 | `not value` → `value is None` | SURVIVED | **equivalent mutant** |
| N4 | GET branch stops calling `safe_next` | RED `30 failed` | killed |
| N5 | POST branch stops calling `safe_next` | RED `67 failed` | killed |
| N6 | `except Exception` → `except ValueError` | SURVIVED | correct on 3.13, see §4 |
| N7 | C1 upper bound `\x9f` → `\x9e` | SURVIVED | not exploitable (below) |

N4/N5 are the useful new ones: they prove the GET-side and POST-side call
sites are *independently* pinned. N4 fails **only**
`test_login_get_sanitises_hidden_next`; N5 fails only the three POST-side
tests. Neither surface is redundant with the other.

I then checked whether the three new survivors are equivalent mutants or
real gaps, by differential-fuzzing each against the real function over a
282,436-value corpus:

- **N3 — `diffs=0`, a genuinely equivalent mutant.** `""` reaches
  `urlsplit("")`, whose empty path fails `startswith("/")`, so both forms
  return `"/"`. No test could distinguish them. Not a gap.
- **N2 (628 diffs) and N7 (49 diffs) are real behaviour differences.** I
  drove every extra-accepted value through the real app and inspected the
  serialised `Location`:

```
=== N2-ctrl-on-path : 9073 values the mutant accepts but real rejects ===
  OFF-ORIGIN or 5xx serialisations: 0
=== N7-c1-boundary : 894 values the mutant accepts but real rejects ===
  OFF-ORIGIN or 5xx serialisations: 0
```

Both mutants are strictly more permissive, but everything they let through
still serialises same-origin (the extra bytes get percent-encoded). They are
defence-in-depth boundaries that no test pins — the same category as the
documented M2 survivor, not security holes. Recorded as observation O-2.

## 6. The survivor argument, interrogated — **PASS (argument holds)**

The builder claims `parts.netloc` cannot be singled out. A refutation would
be any value `v` where the netloc term is the *only* one that fires:

```
urlsplit(v).netloc != ""  and  scheme == ""  and  path.startswith("/")
and no C0/DEL/C1 byte in v  and  not v.startswith("//") / ("/\\")
```

I searched for one four ways: exhaustive products over a targeted
delimiter/control/Unicode alphabet; **every codepoint in the entire Unicode
range** (0x0–0x10FFFF) substituted into ten templates; NFKC fold-hunting
over every character that normalises to `/` or `:`; and 500k random strings.

```
checked 12723664 candidates
WITNESSES (netloc uniquely load-bearing): 0
```

**12.7 million candidates, zero witnesses.** I could not refute the
enumeration, and the mutation run independently agrees (M2 survives). The
conclusion stands: keeping the layer with the docstring claim withdrawn is
the honest resolution. Deleting a correct check to satisfy a mutation score
would have been worse, and forcing an artificial test would have been worse
still.

One precision note on the *wording*, not the conclusion. The docstring says
a netloc implies the value "leads with a stripped control character". That
is loose — `/\t/evil.com` yields `netloc='evil.com'` with the TAB at index 1,
not leading. The conclusion survives because such values are caught by the
layer-1 *path* term (`urlsplit` strips the TAB, leaving `path=''`) and by
layer 2. Recorded as observation O-1; not blocking.

## 7. `wallet.py` deletion independently verified safe — **PASS**

The deleted statement wrote `session["wallet_gate_form"] = {"return_url": …}`.

- **Static reads.** Repo-wide grep (all file types, templates included) for
  `wallet_gate_form` finds exactly one reader,
  `blueprints/wallet.py:488`, which does
  `gate_payload = session.pop("wallet_gate_form", None) or {}` followed by
  `(gate_payload or {}).get("tool")` — a literal `"tool"`, never
  `"return_url"`.
- **The canonical writer** `shared/wallet_guard.py:95` writes
  `{"tool":…, "form":…, "reason":…}`. No `return_url`.
- **`return_url` elsewhere is a different thing.** The remaining hits
  (`billing/checkout.py:580/598`, `blueprints/wallet.py:256/266`) are the
  Stripe billing-portal `return_url` argument, unrelated to the session key.
- **Dynamic access ruled out by AST**, not grep. I walked every `.py` in the
  repo for `session[<non-literal>]`, `session.get/pop(<non-literal>)`,
  `gate_payload[...]` with a computed key, and `**session` / `**gate_payload`
  unpacking:

```
dynamic session/gate_payload key access: 10
   ('app.py', 262, 'session.get(_CSRF_SESSION_KEY)')          _CSRF_SESSION_KEY = "_csrf_token"
   ('scout\\routes.py', 173, 'session.get(ANON_SESSION_KEY)')  ANON_SESSION_KEY  = "scout_anon_id"
   ('tools\\platform_api\\account_bp.py', 89, ...)             _CSRF_SESSION_KEY = "_platform_api_csrf"
   … (all 10 are these three module-level constants)
** unpacking of session/gate_payload: 0
```

All ten dynamic accesses resolve to module-level constants, none of which is
`wallet_gate_form`. No template touches it. Nothing reads the key.

Worth adding: the deleted write was not merely dead, it was *malformed* —
it stored a dict with **no `"tool"` key**, so had the reader ever seen it,
`.get("tool")` would have been `None` anyway. Deleting rather than
validating a user-controlled session write is the right call.

## 8. The 163 tests are not padding — **PASS**

Collected breakdown:

```
  30  test_safe_next_rejects_offsite                  30  test_login_post_failure_rerenders_sanitised_next
  30  test_login_post_rejects_offsite_next             9  test_safe_next_never_raises
  30  test_login_get_sanitises_hidden_next             8  test_safe_next_preserves_internal
   8  test_login_post_preserves_safe_next              7  test_login_get_survives_urlsplit_raises
   7  test_login_post_survives_urlsplit_raises         2  test_safe_next_defaults
   2  test_login_get_preserves_safe_next             TOTAL 163
```

The four 30-case blocks are the same hostile corpus driven through **four
different assertion surfaces**: the pure function; the serialised `Location`
header on the POST success path; the rendered hidden field on the GET
branch; and the rendered hidden field on the POST *failure* path. These are
distinct properties, and the mutation run proves they are not restatements —
N4 kills only the GET-render surface, N5 only the POST surfaces, M9/M10 only
the raise-set tests. A padded suite would have every mutation hitting the
same tests; this one does not.

The end-to-end tests assert the **serialised header**, which is the level
where the bug actually existed — asserting the pre-serialisation string
would have missed the original TAB exploit entirely. That is the right
choice and the reason this suite is meaningful.

## 9. This document — **PASS**

---

## Non-blocking observations

- **O-1 — docstring wording on the netloc layer.** "leads with a stripped
  control character" should read "contains a stripped control character
  before the authority". The conclusion is unaffected (verified over 12.7M
  candidates); only the phrasing is imprecise.
- **O-2 — two unpinned defence-in-depth boundaries.** The C1 upper bound
  (`\x9f`) and the choice to scan `value` rather than `parts.path` both
  survive mutation. Neither is exploitable (0/9967 extra-accepted values
  serialise off-origin), so this is a coverage note, not a defect. Two
  parametrised cases would close it if anyone cares.
- **O-3 — `safe_next` returns `fallback` unvalidated.** Harmless today: both
  call sites use the `"/"` default. It would become an open redirect if a
  future caller ever passed an attacker-influenced `fallback`. A one-line
  assert or a doc note on the parameter would prevent that.

## What I could not verify

- **Non-3.13 CPython behaviour.** The justification for `except Exception`
  over `except ValueError` rests on other CPython versions having a
  different `urlsplit` raise set. This environment has only Python 3.13.0, so
  I confirmed the 3.13 half (739/739 `ValueError`) and the *reasoning*, but
  could not execute another interpreter to confirm the other half. The broad
  catch is safe regardless, so nothing turns on it.
- **Real-browser resolution.** Off-origin was judged by the serialised
  `Location` header plus a same-origin re-parse, not by driving an actual
  browser. This is the same level at which the original exploit was
  demonstrated, and strictly stricter than the pre-fix code.
- **CSRF interaction.** `tests/conftest.py` disables CSRF process-wide, so
  the PR's `prod_client` is production-shaped in the `TESTING` sense but not
  CSRF-enforcing. I hit this myself (a raw POST returned 403 until I matched
  the convention). Pre-existing repo-wide convention, not this PR's doing,
  and the B-1 vector is the unauthenticated **GET**, which needs no token —
  that path I drove fully.

---

## Verdict: **MERGE**

Both round-1 blockers are genuinely closed, verified independently rather
than accepted:

- **B-1 closed.** All seven payloads 200 on a production-shaped app, plus
  zero 5xx across 739 fuzz-discovered raising inputs and 4000 arbitrary ones.
- **B-2 closed.** 9 of 10 mutations RED with failures confirmed by test
  name; the single survivor is a layer I could not refute across 12.7M
  candidates, and the builder kept the check while withdrawing the
  overreaching docstring claim — the correct resolution.

The fix introduced nothing new: suite delta reconciles exactly (+156, no
failures, skips unchanged), the broad `except` cannot swallow shutdown
signals and always yields the literal `"/"`, and the `wallet.py` deletion is
provably unread including under dynamic access. The change closes a
redirect that is exploitable on trunk today, and closes a CR/LF 500 as a
bonus. No blocking findings.
