# Handoff: P2 batch 2026-11-02

Branch `claude/p2-batch-1102-a3b643`, base `origin/main` = `1ede8b57`.
Commits: `40152fed` (item 5), `28500548` (items 1 and 9).

Parked at the lead's instruction. Everything below that says "verified" names
the command that verified it. The suite was still running when this note was
first written; it has since finished green, and the note is corrected to
match.

## State

| | |
|---|---|
| Items real and fixed | 2 (items 1, 5) |
| Items already fixed, verified | 6 (items 2, 3, 4, 6, 7, 8) |
| Items that were a writing task | 1 (item 9) |
| `review-code` | returned, **no code-behaviour findings** |
| `review-claims` | returned, **17 claims enumerated, none FALSE**, 1 UNVERIFIED (external docs) |
| Full test suite | **PASSED.** 9974 passed, 33 skipped, 3 xfailed in 660.89s, exit 0. |
| Targeted tests | `tests/test_deploy_paths_exclusions.py` + `tests/test_deploy_trigger_covers_dockerfile_copies.py` → 79 passed |

### No unfinished step

The full suite had been launched before this session was asked to park, and it
finished afterwards:

    /c/Users/lab/Documents/Claude_projects/tools-hub/venv/Scripts/python.exe -m pytest -q
    # 9974 passed, 33 skipped, 3 xfailed in 660.89s   (exit 0)

So the earlier "~24%, parked" status in this note's first draft is superseded.
Consistent with the delta being comment-only, proved by
AST-with-docstrings-stripped identity against the base for every changed
Python file.

### Two corrections made after the first round of review

**The item-9 comment misquoted GitHub's page.** It rendered the `**` rule as
"matches any character including the / character", a string that does not appear
on the page, while attributing it to GitHub's table in quotation marks. The
meaning was right and no behaviour claim was affected, but the attribution was
false as written. The hub lead read the live page in a browser and extracted its
wording; the comment now quotes the `'**'` row exactly ("The ** wildcard matches
any character including slash (/)"), and the other quotes are attributed to the
specific places they come from: the filter pattern cheat sheet for `*`, the Note
under the paths section for later-wins. The whole-path rule is a paraphrase and
is marked as one. **The previously UNVERIFIED docs claim is now verified against
the live page.**

**A miscount in `40152fed`'s pushed message.** It says "the three positive
controls", where `tests/test_fold_duplicate_record_names.py:218` is ONE test
parametrized over three tools. Three cases, one test. History was not rewritten
for a pushed commit; verified instead that no comment, docstring or PR-body line
repeats the miscount -- `git diff 1ede8b57..HEAD` over the three changed source
files has no "three" or "positive control" in any added line, and every other
mention in this note and the PR body is this correction itself.

### Gotcha worth keeping

`pathlib.Path.write_text` on Windows translates `\n` to `\r\n`, which silently
converted the whole LF workflow file to CRLF. Reverted to LF before committing.
Use `open(p, "wb")` for files in this repo.

---

# P2 batch 2026-11-02 — per-item outcomes

Branch `claude/p2-batch-1102-a3b643`, base `origin/main` = `1ede8b57`.
Nine items. **Two were real** (1, 5). **Six were already fixed or not defects**
(2, 3, 4, 6, 7, 8). **One was a writing task** (9).

Every "already fixed" below is a verification I ran on this tree, with the
command. Nothing here is taken from the register's own wording.

---

## 1. proteina `KNOWN GAP, NOT FIXED` comment — REAL, FIXED

`tools/proteina/run_pipeline.py` ~5316. The note said the streamed
`target_numbering` has no reader because
`shared/job_recovery.py::_candidate_from_partial` does not carry it.

It carries it now:

    sed -n '105,118p' shared/job_recovery.py
    # -> if part.get("target_numbering") is not None:
    #        candidate["target_numbering"] = part["target_numbering"]

    git merge-base --is-ancestor 92fc4cd2 HEAD   # exit 0

Landed in `92fc4cd2` = PR #441. Comment replaced with the reader chain it
actually has (`_sanitize_candidate` allowlists → `_candidate_from_partial`
copies to root → `templates/tools/proteina_results.html` reads). Proved
comment-only by AST-minus-docstrings identity against HEAD~1.

## 2. iggm cancelled-run antigen/epitope panel blank — ALREADY FIXED

**It was a DISPLAY gap, not data loss** — so no P1 escalation was due.
`templates/tools/iggm_results.html:14-26` already falls back to `job.inputs`
for `run_task`, `antigen_chain`, `epitope_pdb_resnums`, and the run still
writes those keys (`tools/iggm/__init__.py:379-380`, `:433-434`). Closed by
PR #441.

## 3. kept/recovered proteina design loses root `target_numbering` — ALREADY FIXED

Same code as item 1, same proving command. Also a display gap, not data loss.

## 4. rfdiffusion stop-test QA report stranded — ALREADY LANDED

The report blob is byte-identical on `origin/main` and on
`origin/claude/gracious-babbage-54dce2` (same blob SHA `88cc2169…`). Came in
with PR #441.

**Side finding:** the only thing still unique to that stranded branch is
`tests/test_signup_credit_covers_smallest_run.py`, which is code. Left alone
per the docs-only instruction. That branch is still worth a look by whoever
owns the signup-credit area.

## 5. fold-key comment on a stranded local branch — REAL, LANDED

**The brief's line number was wrong.** It pointed at `tools/af2/__init__.py`
~313, which is a *user-facing error string* on a rejection path ("would both
be saved as …") — a legitimate counterfactual, not a defect. The actual false
claim was the COMMENT at ~272, plus its twin in
`tools/colabfold/__init__.py`, asserting that names differing only after
character 60 share one object. They do not: the key is
`re.sub(...)[:60]`, so the cut happens *after* the substitution, and the
claim had the mechanism backwards.

Landed as `40152fed`. `git merge-base --is-ancestor 2d64f49a HEAD` correctly
fails — a cherry-pick has a new SHA, so that command cannot prove this. Proved
instead by extracting all `+`/`-` lines from `git show 2d64f49a -U0` and
`git show 40152fed -U0` and diffing: identical. The original commit message
also carried a September measurement from a different tree; amended it out.

## 6. `wallet_estimates.py` "cut to $5.00" comment — ALREADY FIXED

    git blame -L 622,628 -- shared/wallet_estimates.py
    # -> bc8faccf (PR #459, 2026-10-23)

The comment already reads as a dated history of the constant, ending "removed
on 2026-10-23", and cites
`tests/test_signup_credit_covers_smallest_run.py` for the live behaviour.
**File not touched** — the wallet worker owns this area.

## 7. boltzgen `num_designs=200` hardcode — ALREADY DOCUMENTED

`tools/boltzgen/__init__.py:150-168` already names where the 200 comes from.
All three figures in that comment re-derived rather than trusted:

- `runtime_minutes: 82.4` → `tools/boltzgen/example/result.json:58`
- 4x selectivity = 200 / 50, cap documented at `tools/boltzgen/__init__.py:101-108`
- 6600 s subprocess timeout → `shared/pdb_preflight_rules.py:715-716`

Number unchanged, as instructed.

## 8. 22 test files construct a zero-value, unguarded — ALREADY GUARDED

The single shared guard exists: `tests/conftest.py::_forbid_real_supabase_clients`,
autouse and **session**-scoped, raising `_RealSupabaseClientRefused(BaseException)`
— `BaseException` deliberately, because app code wraps client construction in
`except Exception:` and would swallow an `Exception`. Landed in `296598c4`
= PR #302, which is the same commit the register's own "22, not 23"
re-measurement cites.

**Mutation-proved, not just read:** an unmarked probe file that builds a real
client failed with `_RealSupabaseClientRefused` at `tests\conftest.py:143`.
Probe deleted afterwards; tree clean. No 22-file diff was written.

Note for the register: the item's wording ("construct a zero-value") reads as
`Decimal("0…")`. `grep -rl 'Decimal("0' tests/` gives 23 files, almost all
quantize exponents and zero balances — ordinary money test code with no single
guard point. The item actually means test files constructing **real Supabase
clients**. Worth rewording in the register.

## 9. `**/` path-filter semantics in deploy-modal.yml — VERIFIED, COMMENT WRITTEN

**The three negations DO work as the file assumes.** No trigger path changed.

From GitHub's "Patterns to match file paths" table (Workflow syntax for GitHub
Actions): `*` does not match `/`, `**` does, patterns match the whole path from
the repo root, and "A matching negative pattern (prefixed with `!`) after a
positive match will exclude the path".

One nuance worth the lead's attention: **whether `**` matches ZERO path
segments is shown only by example, never stated** — the table's `docs/**/*.md`
row is given as matching `docs/README.md`. The single real path that turns on
in this repo is `tools/__init__.py`, which exists. Its exclusion is intended,
for the script-mode reason already argued in
`tests/test_deploy_paths_exclusions.py::test_deploy_step_still_passes_a_FILE_path_to_modal_deploy`.

Safe to add comments inside the `paths:` block: that test reads it with
`yaml.safe_load`, and where it reads `run:` blocks it drops comment-only lines
explicitly. Verified after the edit that `paths` still parses to the same eight
entries as `_EXPECTED_PATHS`, and that the nine-app matrix is unchanged.

Also caught in passing: `pathlib.write_text` on Windows translates `\n` to
`\r\n`, which silently converted the whole LF workflow file to CRLF. Reverted
to LF before committing. Use `open(p, "wb")` for files in this repo.

No GPU spend and no Modal redeploy in this session.

---

## Staged PR description, preserved verbatim

P2 batch for 2026-11-02: nine accumulated wording / comment / stale-claim items
in one branch. **Two were real defects. Six were already fixed. One was a
documentation-verification writing task.** Nothing in this PR changes
behaviour: every hunk is a comment or a docstring.

Base is `origin/main` at `1ede8b57`.

## Real, fixed

**1. `tools/proteina/run_pipeline.py` — a `KNOWN GAP, NOT FIXED` note for a gap
that is closed.** The note said the streamed `target_numbering` has no reader
because `shared/job_recovery.py::_candidate_from_partial` does not carry it. It
carries it now (`shared/job_recovery.py:113-114`, landed in `92fc4cd2` / #441,
an ancestor of this branch by `git merge-base --is-ancestor`). Replaced with the
reader chain it actually has: `webhooks/modal.py::_sanitize_candidate`
allowlists the field (`:504-539`), `_candidate_from_partial` copies it to the
candidate root, `templates/tools/proteina_results.html:63` reads it.

**5. The fold-key comment, stranded on an unpushed local branch.** The false
claim was live in `tools/af2/__init__.py` and `tools/colabfold/__init__.py`: a
comment asserting that names differing only after character 60 land on one
object. They do not. The key is
`re.sub(r'[^A-Za-z0-9_-]+', '_', name)[:60] or 'fold'`
(`tools/af2/__init__.py:302`, `tools/colabfold/__init__.py:301`), so the cut
happens *after* the substitution; the comment had the mechanism backwards.
Cherry-picked and amended.

A cherry-pick gets a new SHA, so `git merge-base --is-ancestor 2d64f49a HEAD`
cannot prove this landed and correctly does not. Proved by extracting all
`+`/`-` lines from `git show 2d64f49a -U0` and `git show 40152fed -U0` and
diffing them: identical.

## Verified and written down, no path changed

**9. `**/` path-filter semantics in `.github/workflows/deploy-modal.yml`.** The
three `tools/**` negations **do** exclude what the trigger assumes. Quoted from
GitHub's "Patterns to match file paths" table: `*` does not match `/`, `**`
does, patterns match the whole path from the repo root, and a later negation
wins. One rule that table shows **only by example**, never states, is whether
`**` may match zero path segments — its `docs/**/*.md` row is given as matching
`docs/README.md`. The single real path that turns on here is `tools/__init__.py`,
which exists, and excluding it is intended for the script-mode reason already
argued in
`tests/test_deploy_paths_exclusions.py::test_deploy_step_still_passes_a_FILE_path_to_modal_deploy`.

No trigger path was touched. After the edit the `paths` list still parses to the
same eight entries as `_EXPECTED_PATHS`, and the nine-app matrix is unchanged.
Comments are safe inside that block because the test reads it with
`yaml.safe_load`, and where it reads `run:` blocks it drops comment-only lines
explicitly.

## Already fixed — verified, not assumed

| # | Item | Proof |
|---|------|-------|
| 2 | iggm cancelled-run panel blank | `templates/tools/iggm_results.html:14-26` already falls back to `job.inputs`; the run still writes those keys (`tools/iggm/__init__.py:379-380`, `:433-434`). **A display gap, not data loss**, so no P1 was due. Closed by #441. |
| 3 | recovered proteina design loses root `target_numbering` | Same code as item 1. Also display, not data loss. |
| 4 | rfdiffusion stop-test QA report stranded | Report blob is byte-identical on `origin/main` and the stranded branch (same blob SHA `88cc2169…`). Came in with #441. |
| 6 | `wallet_estimates.py` "cut to $5.00" comment | `git blame -L 622,628` → `bc8faccf` (#459, 2026-10-23). **File not touched** — the wallet worker owns that area. |
| 7 | boltzgen `num_designs=200` undocumented | `tools/boltzgen/__init__.py:150-168` already names the source. All three figures re-derived: `runtime_minutes: 82.4` at `tools/boltzgen/example/result.json:58`; 4x = 200/50 with the cap at `tools/boltzgen/__init__.py:101-108`; the 6600 s subprocess timeout at `shared/pdb_preflight_rules.py:715-716`. Number unchanged. |
| 8 | 22 test files construct a zero-value, unguarded | The single shared guard already exists: `tests/conftest.py::_forbid_real_supabase_clients`, autouse and session-scoped, raising a `BaseException` subclass deliberately (app code wraps client construction in `except Exception:`). Landed in `296598c4` / #302 — the same commit the register's "22, not 23" re-measurement cites. **Mutation-proved**: an unmarked probe file building a real client failed with `_RealSupabaseClientRefused` at `tests\conftest.py:143`. Probe deleted; no 22-file diff written. |

## Corrections to the item list itself

- **Item 5's line number was wrong.** It pointed at `tools/af2/__init__.py` ~313,
  which is a user-facing error string on a rejection path ("would both be saved
  as …") — a legitimate counterfactual, not a defect. The real target was the
  comment at ~272 and its twin in `tools/colabfold/__init__.py`.
- **Item 8's wording is misleading.** "Construct a zero-value" reads as
  `Decimal("0…")`; `grep -rl 'Decimal("0' tests/` gives 23 files, almost all
  quantize exponents and zero balances with no single guard point. The item
  means test files constructing real **Supabase clients**.
- **Side finding on item 4:** the only thing still unique to
  `origin/claude/gracious-babbage-54dce2` is
  `tests/test_signup_credit_covers_smallest_run.py`, which is code and was left
  alone per the docs-only instruction. Worth a look by whoever owns signup
  credit.
- **Imprecision in `40152fed`'s own message**, surfaced by the claims reviewer
  and not corrected by a history rewrite: it says "the three positive controls",
  where `tests/test_fold_duplicate_record_names.py:218` is one test parametrized
  over three tools. Three cases, one test.

## Review and test

Both saved reviewers ran on `1ede8b57..HEAD` before this branch was pushed.

- `review-code`: **no code-behaviour findings.** It independently re-parsed the
  workflow and confirmed the `paths` list unchanged.
- `review-claims`: 17 claims enumerated, 16 checked. **None FALSE.** 15
  verified. One UNVERIFIED: the quotation of GitHub's documentation table, which
  is external and cannot be checked offline — the repo does not contradict it
  and the negation order is pinned by `_EXPECTED_PATHS`
  (`tests/test_deploy_paths_exclusions.py:145-154`, asserted at `:338`).

Comment-only is proved, not asserted: the AST of every changed Python file with
docstrings stripped is identical to its base version.

No GPU spend and no Modal redeploy in this session.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
