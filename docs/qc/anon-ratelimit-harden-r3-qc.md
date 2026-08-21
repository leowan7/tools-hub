# QC round 3 — `fix/anon-ratelimit-harden-fixes` (`feea6f2` → `dd0cd07`)

Independent review of a single amend. I did not write this code, I did not write
the QC round it answers, and I re-measured every number rather than reading the
author's. Reviewed in four detached worktrees created fresh for this review —
`scratchpad/qc-r3`, `qc-r3-base`, `qc-r3-probe`, `qc-r3-probe-base` — all outside
`.claude/worktrees/`, so `app.py`'s bare `load_dotenv()` cannot walk up into the
main tree's `.env`. The main working tree, the author's `fix-r3`, and every
pre-existing worktree were read but never written.

Throughout: **MEASURED** means I executed it and am quoting my own output.
**READ** means I read it and it looks right, which is not verification.

---

## Verdict

**PASS.** Zero findings.

Every claim in `dd0cd07` reproduces on my own runs — both suite totals, both
scoped totals, all three mutation deltas and all three mutation outcomes, the
`feea6f2`-side MD control, all four byte-delta figures including the two-part
decomposition of the test file, the F-4 line count, and all four line-number
citations — with no discrepancy anywhere.

**The thing I was asked to hunt — a NEW false claim shipped while fixing the
last round's — is not present.** I attacked the replacement prose in
`shared/metrics.py` and `scout/ratelimit.py` specifically, including the
substring-vs-token gap the brief flagged, and drove gunicorn 24.1.1's own parser
over 25 framings to test it. The prose is accurate. This is the first round of
the four that did not trade one wrong statement for another.

Enforcement is unchanged and I re-proved it two ways: 14 parseable framings
through gunicorn's real parser all return `""`, and mutation ME still goes red.

**Recommendation to the next round: do not edit these two comments.** Three
consecutive rounds have each shipped a new false claim while correcting the last
one's. Section 6 records what I measured about the one clause a reader might be
tempted to "tighten", and why tightening it would make it worse.

---

## Numbers I measured

Full suite, from the worktree root, no path argument,
`venv/Scripts/python.exe -m pytest -q`. Both runs were backgrounded (the suite
exceeds this harness's 10-minute foreground cap), and the same shell that ran
pytest wrote `pwd`, `HEAD_PRE`, `STATUS_PRE`, the pytest exit code, `HEAD_POST`
and `STATUS_POST` into the artefact, so each file self-attests against the
wrong-tree failure mode.

| commit | worktree | mine | author's | delta |
|---|---|---|---|---|
| `feea6f2` (former tip) | `qc-r3-base` | **5490 passed / 21 skipped** (6m40s, exit 0) | 5490 / 21 | none |
| `dd0cd07` (tip) | `qc-r3` | **5490 passed / 21 skipped** (6m28s, exit 0) | 5490 / 21 | none |

Both artefacts attest the correct `PWD`, the correct SHA, and an empty
`git status --porcelain` before *and* after; both exited 0 with **zero** `FAILED`
or `ERROR` lines. The suite total does not move across the amend, which is what
"extends a test rather than adding one" predicts.

Scoped, `tests/test_scout_anon_charge_pairing.py`, foreground:

| commit | mine | author's |
|---|---|---|
| `feea6f2` | **42 passed** (12.61s) | 42 |
| `dd0cd07` | **42 passed** (11.57s) | 42 |

`ruff check --no-cache` on `scout/ratelimit.py`, `shared/metrics.py`,
`tests/test_scout_anon_charge_pairing.py`: **All checks passed** (MEASURED).

Line endings on disk at the tip, MEASURED: `ratelimit.py` 42567 B / 867 CRLF /
**0 bare LF**; `metrics.py` 14214 B / 368 CRLF / **0 bare LF**;
`test_scout_anon_charge_pairing.py` 62224 B / 1409 CRLF / **0 bare LF**.

**Byte deltas — all four reproduce, including the decomposition.** Measured on
the CRLF working tree (blob size plus one byte per line, cross-checked against
`wc -c` on both checkouts):

| claim | author | mine |
|---|---|---|
| F-1, `scout/ratelimit.py` | −14 B | **−14 B** |
| F-1, test docstring | −6 B | **−6 B** |
| F-2, test extension | +1126 B | **+1126 B** |
| F-3, `shared/metrics.py` | +316 B | **+316 B** |

The test file's net on-disk change is +1120 B (61104 → 62224), which is exactly
`+1126 − 6`. The author quoted the two hunks separately; both halves are right
and they reconcile.

---

## 1. Chain integrity — MEASURED from scratch, INTACT

Two scripted rewrites half-failed earlier in this session, so I established this
independently rather than taking the author's plumbing guard on trust.

* `c749480..dd0cd07` is **6 commits, 0 merge commits**, linear.
  `git merge-base dd0cd07 c749480` = **`c749480`** exactly.
* Parent chain: `dd0cd07` → `a6ea998` → `2346ebe` → `17a392a` → `22cbe20` →
  `e5c0fd0` → `c749480`.
* **The two parents are byte-identical SHAs.** `git rev-parse feea6f2^` and
  `git rev-parse dd0cd07^` both return
  `a6ea99822bfa8d5b7eb3dbc9b19e93a59a283be1`. `a6ea998`'s tree is still
  `50656036e2834fb27ebeeedfd77b0a6bd559ddfb` — the same value the round-2 report
  recorded before this amend existed. `2346ebe` is unchanged at
  `2346ebe6a9f944a7a0b97110e2769ad9bdb51dd6`.
* **Exactly one commit changed.** The branch reflog shows a single step,
  `feea6f2 → dd0cd07`, "round 3 micro-fixes: amend the tip in place". No reset,
  no temp commit, no cherry-pick this round.
* **Nothing orphaned or duplicated.** No two commits in `c749480..dd0cd07` share
  a tree. `feea6f2` is correctly **not** an ancestor of the branch; it survives
  only as several worktrees' detached HEAD, which costs nothing.
* **Not pushed.** `git ls-remote --heads origin` has no ref matching
  `anon-ratelimit*`. The branch ref is `dd0cd07`.
* **AST scan**, `feea6f2` → `dd0cd07`, top-level *and* methods:
  `ratelimit.py` 18/18, `metrics.py` 24/24, the test file 68/68 — **0 missing,
  0 added, 0 duplicated** in all three. This reproduces the author's 18/24/68
  exactly and is the check the repo's "rebuilt a file from one side's blob and
  lost a whole function" incident calls for. It also independently corroborates
  "extends a test rather than adding one": no new definition exists.

### Did the amend drop anything from `feea6f2`?

No. `git diff --numstat feea6f2 dd0cd07` is three files and nothing else:
`3/3` `scout/ratelimit.py`, `8/4` `shared/metrics.py`, `26/6` the test file —
four hunks total. I accounted for all **six** removed test-file lines:

1. three docstring lines, replaced by three that say the same thing without the
   attribution (F-1);
2. `# Real chunked framing still is the signal, mixed case included.`, replaced
   by an eight-line comment that says that *and* why the substring test is
   deliberate;
3. `assert ratelimit.unmetered_chunked_bodies == 1, (`, replaced by `== 2`;
4. the one-line `assert len(_alarm_records(caplog)) == 1, "real chunked framing
   no longer warns"`, replaced by the same assertion with a longer message.

Nothing was weakened. The mixed-case assertion, the `other`-label assertions and
the export-delta assertion are all still present and unchanged. In
`shared/metrics.py` the four removed lines are all re-expressed in the eight that
replace them — including "Any caller can pick this label by sending chunked
framing", which survives as "so any caller can pick this label by sending that
framing".

**Both source changes are comment-only.** Filtering the diff to non-comment
lines gives **0** changed lines in `scout/ratelimit.py` and **0** in
`shared/metrics.py`. The discriminator, both limiter tiers, every enforcement
path and every policy number are byte-identical to `feea6f2` — the tip's own
claim, MEASURED.

---

## 2. F-2 — the behavioural claim. MEASURED, the guard lands and it lands for the right reason

The extension is at `tests/test_scout_anon_charge_pairing.py:1218-1234` (exact),
ahead of the existing mixed-case assertion, and the closing chunked count goes
1 → 2 (exact).

**Mutation MD, reproduced on both sides with my own landing proof.** Applied by
binary read/replace/write on this pure-CRLF tree, byte delta printed, the file
re-read from disk and the new content re-asserted present / the old absent — so
the ±0 B edit is still *proven* to have landed. `git reset --hard HEAD` and an
empty `git status --porcelain` after each; HEAD printed before and after.

| where | delta | landed? | result | author's |
|---|---|---|---|---|
| `feea6f2` (`qc-r3-probe-base`) | **+0 B** (42581 → 42581) | new=1, old=0, bare LF 0 | **42 passed — GREEN** | GREEN |
| `dd0cd07` (`qc-r3-probe`) | **+0 B** (42567 → 42567) | new=1, old=0, bare LF 0 | **1 failed / 41 passed** | 1 failed / 41 passed |

**The failure is for the right reason, not incidentally.** pytest's own output:

```
>           assert len(_alarm_records(caplog)) == 1, (
E           assert 0 == 1
FAILED …::TestTheAlarmItself::test_a_transfer_coding_that_is_not_chunked_is_not_the_chunked_signal
```

That is line 1230 — the assertion added by this commit, immediately after the
`gzip, chunked` call, reading `0` alarm records where the un-mutated code fires
1. It is not the closing count assertion, not the mixed-case one, and not
enforcement. The `gzip, chunked` case is the specific thing that went silent.

The tightness matters: the assertion is preceded by `assert _alarm_records(caplog)
== []` at line 1209, so the record count is pinned to zero immediately before the
`gzip, chunked` request and to one immediately after. Only that request can move
it.

**The hole was real.** MD on `feea6f2`'s own tree is 42 passed, green — the ±0 B
"tidy-up" that round 2 reported, measured by me on the pre-fix tree. The guard
closes exactly that.

**Other mutations at `dd0cd07`, all mine, all reproducing the author's:**

| Mut | change | my delta | landed | my result | author's |
|---|---|---|---|---|---|
| **MC** | drop the case fold, `"chunked" in encoding` | **−8 B** (42567 → 42559) | new=1, old=0 | **1 failed / 41 passed**, `assert 1 == 2` on the closing count | same |
| **ME** | fail OPEN, `return ""` → `return source()` on the unsized path | **+6 B** (42567 → 42573) | new=1, old=0 | **2 failed / 40 passed** (`…does_not_parse_a_body_of_unknown_length`, `…cannot_redeem_a_credit_and_says_so`) | same |

So after the edit the discriminator has **no unguarded half left**: substring-vs-
equality is pinned by MD, the case fold by MC, fail-closed by ME.

---

## 3. F-1 — the replacement prose. MEASURED true, and it claims no more than exists

`scout/ratelimit.py:364-366` and the test docstring now read:

> gunicorn also accepts `identity`, `compress`, `deflate` and `gzip` as transfer
> codings, and **a ZERO-byte body under any of those arrives with no
> Content-Length** — so a presence test let a ~90-byte bodiless request pick the
> `chunked` label for itself.

Both name no QC round. Three checks:

1. **The property is true, and I measured it myself** — not inherited. Through
   gunicorn 24.1.1's own `http.message.Request` parser and `wsgi.create()`, all
   four codings with a zero-byte body are accepted and all four arrive with
   `CONTENT_LENGTH = None`. See the table in §5.
2. **The attribution the fix removed was genuinely wrong.** VERIFIED READ in the
   round that raised F-C: its §F-C marks the four-coding acceptance **READ**,
   `gunicorn/http/message.py:226-247`, and its measured table has rows for
   `TE: identity` and `TE: gzip` only. Two of four, exactly as `dd0cd07` says.
   A later round measured all four. The commit message's account of this is
   correct in every particular.
3. **`~90 bytes` holds.** MEASURED: the smallest bodiless `TE: gzip` request with
   a realistic Host is **83 bytes** on the wire
   (`POST /scout/analyze HTTP/1.1\r\nHost: scout.ranomics.com\r\nTransfer-Encoding: gzip\r\n\r\n`).

The replacement states a property and cites no evidence class it does not have.
That is the correct fix for the finding, and it does not overshoot: it does not
claim the four codings were measured, and it does not claim they were read.

---

## 4. F-3 — `other`'s "not a closed list". MEASURED, genuinely non-exhaustive

`shared/metrics.py:159-162` (exact):

> `other` is that rule negated: every request the meter could not size whose
> framing is not that. A POST with no body at all (a scanner's opening move) and
> a transfer coding that is not `chunked` are the common cases, **NOT a closed
> list** — the label is the negation, not the enumeration.

* **The third member round 2 found is covered, not newly contradicted.** I
  re-measured it: a POST with a **real body** and **neither** Content-Length nor
  Transfer-Encoding is accepted by gunicorn 24.1.1 on **both** HTTP/1.1 and
  HTTP/1.0 (`CL=None, TE=None, LengthReader`), reaches the branch, and lands on
  `other` (Δother = 1.0). It is neither of the two named shapes and the gloss no
  longer implies it should be. The commit message's "on HTTP/1.1 and 1.0" is
  MEASURED correct.
* **The negation is what the code does.** `_note_unmetered_body` is reached from
  exactly one call site (`scout/ratelimit.py:808` → `:422`), only inside
  `if length is None:`, and the label is `"chunked" if chunked else "other"`.
  There is no third label and no path that skips both.
* **The scoping is honest.** "every request the meter could not size" does not
  over-reach onto the `length > _MAX_FOLLOWUP_BODY_BYTES` path (which the meter
  *could* size and which counts nothing), and the Counter's help string scopes it
  to `POST /scout/analyze` bodies.

---

## 5. Enforcement — MEASURED unchanged, over gunicorn's real parser

I rebuilt round 2's rig independently: gunicorn 24.1.1's own
`http.message.Request` and `wsgi.create()` on Windows, with shims for `fcntl`,
`grp`, `pwd` and `os.geteuid` (the parser calls none of them), then the **real**
`_metered_job_id` inside a real Flask request context over each resulting
environ. 25 framings attempted, 14 accepted.

| framing sent | gunicorn | `HTTP_TRANSFER_ENCODING` | CL | returns | Δchunked | Δother | logs |
|---|---|---|---|---|---|---|---|
| no TE, no body | accepted | `None` | None | `''` | 0 | 1 | 0 |
| `chunked` | accepted | `'chunked'` | None | `''` | 1 | 0 | 1 |
| `Chunked` | accepted | `'Chunked'` | None | `''` | 1 | 0 | 1 |
| `CHUNKED` | accepted | `'CHUNKED'` | None | `''` | 1 | 0 | 1 |
| **`gzip, chunked`** | accepted | `'gzip, chunked'` | None | `''` | **1** | 0 | **1** |
| **`identity, chunked`** | accepted | `'identity, chunked'` | None | `''` | **1** | 0 | **1** |
| **`deflate, gzip, chunked`** | accepted | `'deflate, gzip, chunked'` | None | `''` | **1** | 0 | **1** |
| `identity` (0-byte) | accepted | `'identity'` | None | `''` | 0 | 1 | 0 |
| `compress` (0-byte) | accepted | `'compress'` | None | `''` | 0 | 1 | 0 |
| `deflate` (0-byte) | accepted | `'deflate'` | None | `''` | 0 | 1 | 0 |
| `gzip` (0-byte) | accepted | `'gzip'` | None | `''` | 0 | 1 | 0 |
| `' chunked '` (padded) | accepted | `'chunked'` | None | `''` | 1 | 0 | 1 |
| two headers, `gzip` then `chunked` | accepted | `'gzip,chunked'` | None | `''` | 1 | 0 | 1 |
| body, no CL, no TE | accepted | `None` | None | `''` | 0 | 1 | 0 |
| `chunked, gzip` (not last) | **rejected** `InvalidHeader: TRANSFER-ENCODING` | | | | | | |
| `chunked, chunked` | **rejected** `InvalidHeader` | | | | | | |
| `chunked, identity` | **rejected** `InvalidHeader` | | | | | | |
| two headers, `chunked` then `gzip` | **rejected** `InvalidHeader` | | | | | | |
| `chunked` on HTTP/1.0 | **rejected** `InvalidHeader` | | | | | | |
| `chunkedx` | **rejected** `UnsupportedTransferCoding` | | | | | | |
| `xchunked` | **rejected** `UnsupportedTransferCoding` | | | | | | |
| `x-chunked` | **rejected** `UnsupportedTransferCoding` | | | | | | |
| `chunked-ish` | **rejected** `UnsupportedTransferCoding` | | | | | | |
| `gzip;q=1, chunked` | **rejected** `UnsupportedTransferCoding` | | | | | | |
| `br` | **rejected** `UnsupportedTransferCoding` | | | | | | |

**ENFORCEMENT IS UNCHANGED — MEASURED.** Every one of the 14 parseable framings
returns `''` and fails closed. Only the reporting is split. `gzip, chunked` is
accepted and labelled `chunked` with the alarm firing, which is the specific
claim the brief asked me to re-check, and it holds — as do two stacked shapes
nobody had sent before (`identity, chunked`, `deflate, gzip, chunked`).

---

## 6. The substring-vs-token question, answered — and why the comment is right as written

This is the clause the brief flagged, at `shared/metrics.py:155-158`:

> `chunked` is exactly what the code tests and nothing more — a Transfer-Encoding
> value CONTAINING `chunked`, in any case, stacked with other codings or not —
> so any caller can pick this label by sending that framing.

The code is `chunked="chunked" in encoding.lower()` — a **substring** test.
"Stacked" describes a **token list**. They are not the same predicate, and the
brief was right to ask. Here is what I measured.

**The sentence leads with the substring rule, not the token rule.** Its head is
"a Transfer-Encoding value CONTAINING `chunked`"; "in any case" and "stacked with
other codings or not" are two modifiers on that head, neither of which narrows
it. `"chunked" in encoding.lower()` is precisely "contains `chunked`, case-
insensitively". The description is exact, and it is *more* precise than "stacked
or not" alone would have been — a comment that said only "stacked or not" would
have described the token rule and been wrong about `x-chunked`.

**And the gap has no reachable inhabitants.** I read
`gunicorn/http/message.py:set_body_reader`: it splits the header on commas,
strips each token, and raises `UnsupportedTransferCoding` for any token outside
`{chunked, identity, compress, deflate, gzip}`. So every value that matches the
substring rule but not the token rule is rejected before the app sees it —
MEASURED for `chunkedx`, `xchunked`, `x-chunked`, `chunked-ish` (all
`UnsupportedTransferCoding`). In the other direction, gunicorn sets `chunked`
framing only via a literal `chunked` token, which is always a substring. **Over
everything gunicorn 24.1.1 will pass through, the substring test and the token
test agree exactly — zero false positives, zero false negatives.**

One consequence worth writing down so nobody re-opens it: the *rule* the comment
states is a strict superset of what a caller can actually send (gunicorn also
refuses `chunked` unless it is the last coding, and refuses it entirely on
HTTP/1.0). The comment does not claim otherwise — "any caller can pick this
label by sending that framing" is a statement that the label is caller-chosen,
which is true and which I measured seven ways in §5. It is not a claim that every
string matching the rule is acceptable to gunicorn.

**Do not "tighten" this comment.** Rewriting "CONTAINING" to a token
formulation would make it *false* about the code. Adding "…of the values gunicorn
accepts" would make it true of a moving third-party detail rather than of the
code beside it — the exact staleness failure mode F-3 was fixed to avoid. The
sentence as shipped describes the predicate the code implements, and the
predicate is what will still be true after gunicorn's next release. This is the
fourth round on this prose; it is correct now.

---

## 7. `dd0cd07`'s commit message — every claim checked

Verified by diffing `feea6f2`'s message against `dd0cd07`'s in full. Two changes:
the F-C paragraph's enumeration correction, and the appended ROUND 3 block.
Nothing else was slipped in, and nothing was removed.

**The F-C paragraph correction is genuine.** `other is true of everything that
can reach it: a bodiless POST, and any transfer coding that is not chunked`
became `other is the NEGATION, not a list: … a bodiless POST and a non-chunked
transfer coding are the common cases, not the whole set, see F-3 below`. That is
the same enumeration defect fixed in the same commit as the source comment, which
is what the round claimed.

**F-4, re-derived independently.** `git diff --numstat 2346ebe a6ea998 --
shared/metrics.py` = **`14 0`**, MEASURED. I counted the hunk myself: **8 comment
lines + a 5-line `Counter(...)` statement + 1 blank separator = 14**. The
message's decomposition is exact, and "six" does match the statement plus its
separator and nothing else. `a6ea998` is untouched — its SHA and tree are the
values the round-2 report recorded.

**On "measured versus read".** The message's disclosure paragraph is accurate and
unusually careful: it says gunicorn does not run on Windows, that every claim
about what gunicorn accepts on the wire is QC's measurement through its parser
rather than the author's, and that what the author measured is what the meter
does when handed those framings. That is exactly the division of evidence I found
when I re-ran both halves. `freesasa` is genuinely uninstallable here and
`run_pipeline` is stubbed; no figure in the message is a CPU cost, so nothing
depends on it.

**No new false claim was imported.** I checked every assertion in the ROUND 3
block individually: the ±0 B MD figure, "all 42 tests stayed green" at `feea6f2`,
`set_body_reader` handling the list form, `gzip, chunked` acceptance, the
`:1218-1234` citation, the 1 → 2 count, "the suite total does not move", the F-1
two-of-four attribution history, the `:364-366` and `:155-162` citations, the
HTTP/1.1-and-1.0 third shape, the F-4 line count, all three mutation deltas and
outcomes, both suite totals, both scoped totals, ruff, and 0 bare LF. Every one
reproduces. The only statements I cannot check are the author's own process
claims (see "What I could not verify").

---

## Findings

**None.** I did not find a defect worth numbering, and I am not going to invent
one. The delta is one extended test plus comment text; the test lands, the
mutation evidence is real, and the prose is true.

Two things I looked at hard and decided are **not** findings, recorded here so
the next round does not re-open them:

* The `chunked` gloss's rule is wider than gunicorn's reachable set (§6). Not a
  false claim — the comment describes the code's predicate, which is what a
  comment beside the code should describe. Editing it would make it worse.
* The new failure message at line 1246, "the sample fires on the first and then
  every `_LOG_EVERY`-th", describes a sample that fires at counts ≡ 1 mod
  `_LOG_EVERY` (1, 101, 201). Read as "then every `_LOG_EVERY`-th request
  thereafter" it is correct, and that is the natural reading. It is a pytest
  failure message, not an evidence claim. Leave it.

---

## What I attacked that came out clean — do not re-litigate these

* **Parents byte-identical, exactly one commit changed, nothing orphaned or
  duplicated, not pushed** — established from scratch, not taken from the
  author's plumbing guard. Single reflog step. No two commits share a tree.
* **The amend dropped nothing.** Three files, four hunks, all six removed lines
  accounted for and each replaced by an equal or stronger line. AST 18/24/68 both
  sides, 0 missing / 0 added / 0 duplicated.
* **Both source changes are comment-only** — 0 non-comment changed lines in
  either file, so the discriminator, both limiter tiers, every enforcement path
  and every policy number are byte-identical across the amend.
* **All four byte deltas reproduce, including the +1126/−6 split** of the test
  file, which nets to the +1120 I measured on disk.
* **F-4 re-derived independently**: `14 0`, and 8 + 5 + 1 = 14 counted by hand.
* **MD fails at the right assertion**, line 1230, `assert 0 == 1`, with the
  record count pinned to zero three lines earlier — so nothing but the
  `gzip, chunked` request can satisfy it.
* **MD is green at `feea6f2`** — the hole round 2 reported is real, and I
  measured it on the pre-fix tree rather than believing the report.
* **The discriminator has no unguarded half left**: MD (substring), MC (case
  fold), ME (fail-closed) are each red.
* **Enforcement re-proved independently** over 14 parseable framings through
  gunicorn's own parser, including three stacked shapes and duplicate
  `Transfer-Encoding` headers. All return `''`.
* **Substring-vs-token has no reachable gap** (§6) — four substring-only values
  all rejected `UnsupportedTransferCoding`, MEASURED.
* **F-3's third shape reproduces on HTTP/1.0 as well as 1.1**, and lands on
  `other`.
* **The F-1 attribution history is correct**: VERIFIED READ in the round that
  raised F-C — four-coding acceptance marked **READ** at
  `gunicorn/http/message.py:226-247`, measured table has `identity` and `gzip`
  only.
* **`~90 bytes` is 83 bytes**, measured on the wire.
* **All four line citations are exact**: `ratelimit.py:364-366`,
  `metrics.py:155-162`, test `:1218-1234`, and `_metered_job_id`'s single call
  site at `ratelimit.py:808`.
* **Stale `no_body`: none.** Zero hits anywhere in the tracked tree at the tip.
  The counter, its label key and the `chunked` label value are referenced in
  exactly three files — the three that changed.
* **"The counter has never been deployed" still holds** against the *current*
  `origin/main` (`2422bd1`, which has moved on since round 2's `5ccdf2d`):
  `SCOUT_UNMETERED_BODIES` does not exist there, and the branch has no remote ref.
* **The adjacent "QC sent 25 genuine chunked requests afterwards and got zero
  records"** at `ratelimit.py:369` — unchanged by this amend, and I checked it
  rather than assuming: both the phase-4 report and the round that raised F-C
  record that exact run. Genuine.
* **F-1's fix is scoped, not a sweep, and does not claim to be.** Exactly ten
  other "QC measured" attributions survive in these two files (3 in
  `scout/ratelimit.py`, 7 in the test file, MEASURED); none is alleged false, and
  the message only claims that *both* flagged sites now state the property.
  Correct as written.
* **`ruff check` passes on all three changed files; 0 bare LF on disk in each.**

---

## What I could not verify

1. **Anything inside a running gunicorn worker.** gunicorn does not run on
   Windows. I ran its *parser* and `wsgi.create()` with three shims, which is why
   §5 is real rather than simulated; the worker model, fork behaviour and
   multiprocess mmap are executed by nobody in this chain. The tip's message
   discloses this in the same terms.
2. **Whether Railway's edge would ever forward `gzip, chunked`, or the F-3 shape
   (a body with no framing headers).** I proved gunicorn accepts both. I cannot
   observe the edge. Unchanged from round 2.
3. **Whether Railway sets `PROMETHEUS_MULTIPROC_DIR`.** Round 2's F-A is filed
   separately and deliberately untouched here; that single fact still decides
   whether it applies at all.
4. **The author's process claims** — that their runs were foreground where they
   say so, in their own worktree, with clean status, and that their `feea6f2`
   artefact attests an empty `git diff --stat feea6f2` plus three file sizes. I
   can only report that every *number* they quote reproduces on my own
   independent runs, which it does, without exception.
5. **Any CPU-second figure.** `freesasa` is not installable here, so
   `run_pipeline` is stubbed. No figure in this delta is a CPU cost.
6. **M6 and M7**, and round 2's `_LOG_EVERY` guard-band sweep. Not re-run; they
   are outside this delta and round 2 measured them.
