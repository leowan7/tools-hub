# Proteina parity with the five sibling generators — handoff

Branch `qc/proteina-inline-converge`, based on `origin/main` = `2151a01`.
Nothing pushed, no PR, nothing deployed. The live `ranomics-proteina-prod` app
has not been touched.

## What the goal was narrowed to, and why

The original brief was "unblock a direct `modal.Function.from_name` call so it
returns designs inline". That was reframed to **make proteina behave like the
five sibling tools** (`llm-proteinDesigner/docker/{bindcraft,boltzgen,pxdesign,
rfantibody,rfdiffusion}/run_pipeline.py`). The reframe is the reason this
converged: parity is a finite, checkable rubric. The earlier open-ended
"find all bugs" loop burned 1.64M tokens over three rounds and never converged.

All five siblings already returned coordinates inline as `pdb_content_b64` and
ran fine with no upload endpoint — each does a bare
`payload.get("upload_urls_endpoint", "")` and never refuses. Proteina was the
only one of six that hard-failed pre-GPU without it.

## Commits on the branch

| commit | what |
|---|---|
| `d9c1c2a` | the original inline-delivery feature |
| `70f1516` → `da3e2b8` | two earlier QC fix rounds |
| `1d29fbb` | an uncommitted fix round, committed |
| `079b297` | round-1 fix agent, INTERRUPTED mid-write, committed unverified and labelled as such |
| `160b0ff` | verified `079b297`: it was RED, two fixture defects fixed, rank fix mutation-checked |
| `7958579` | round 2 — the 13 gaps below |

**`160b0ff` is unsafe in isolation.** It deletes the unconditional pre-GPU
refusal for a payload with no `upload_urls_endpoint`; the replacement guard
lands in `7958579`. Do not land, review, or bisect these apart.

## Verification status

- **`3706 passed, 19 skipped`**, repo-wide, in a worktree with no `.env`.
  Baseline before round 2 was `3635`; +71 tests.
- **20 mutations, all RED** — 16 from the group-A fixer, 4 from an independent
  auditor who wrote none of the code.
- **Nothing has run on a GPU.** Every test stubs `run_streaming`, so the search
  subprocess has never executed. No claim about real design output is proven:
  not that `complexa design` writes a reward CSV whose columns `parse_designs`
  matches, not that `find_pdb_for` resolves real filenames, not that a real
  inlined structure loads in a viewer.

The `.env` absence is the safety property that made running the full suite
possible at all — the worktree structurally cannot load credentials, spawn an
A100, or reach production storage. **The live repo still has `.env`, so a
full-suite run there can still spawn.**

## The 13 gaps closed in round 2

**A — the delivery verdict lied.** A shard that delivered zero coordinates, or
delivered them with no scores, or exited non-zero, returned `COMPLETED` / exit 0
with no error key; only the size-cap path tripped the verdict. `delivery_verdict`
now decides once for every path, `census_output_tree` makes "the search wrote
nothing" distinguishable from "the filter culled everything", and `rc != 0`
surfaces as `partial` plus the real exit code.

Four pre-existing tests asserted `COMPLETED` on shards that delivered nothing —
they encoded the bug. Their counting assertions were kept verbatim and only the
status assertion tightened, so each now asserts strictly more than before.

**B — candidates misreported what they carried.** An unmatched or unreadable PDB
used to be dropped from both `designs` and `candidates`, destroying its
A100-computed scores; it now keeps the candidate and omits only the coordinates,
as bindcraft does. A cap-dropped candidate no longer keeps a `pdb_key` that
nothing in inline mode can resolve. pLDDT is delivered on the [0,100] scale its
label implies.

**C — robustness, and the endpoint question.** The pre-GPU refusal is back, but
for hub-shaped payloads only. This looked like a conflict between parity and
"don't break the web path" and is not: `modal_client.py` makes `job_token` and
`webhook_url` required on every web submission, `modal_app.py:114-116` puts them
in the container env, and `direct_call_fc.py:216` documents setting neither — so
`(job_token or webhook_url) and not upload_endpoint` refuses a web-tier bug free
and loud while a direct call inlines exactly as the siblings do. Also: an upload
failure rescues the design inline instead of dropping it; `complexa design` has
its own deadline so the container kill no longer lands on `run_pipeline.py`
itself; and `main()` has a catch-all so a crash returns a structured result
instead of `smoke_result: None`.

## Round 3: the last parity gap, closed

The convergence sweep came back clean on candidate shape, score key names,
heartbeat payloads and env-var contracts, and found exactly one gap left — the
purest one in the whole exercise.

**Proteina was the only one of six that never cleared `/tmp/smoke_results.json`,
so on a warm container a hard-killed shard handed the hub the PREVIOUS shard's
designs.** Verified empirically, not inferred: with `_run_shard` replaced by
`os._exit(137)` — a faithful stand-in for a signal kill, since it skips every
`except` and `finally` — against a file holding a prior shard's result, the hub
read shard A's 8 candidates, with atoms, as shard B's output and scored the job
**succeeded**. The independent reviewer reproduced this side by side with the
fixed code:

```
WITHOUT the fix : provider_job_id=SHARD-A  status=COMPLETED  candidates=8  hub=succeeded
WITH the fix    : provider_job_id=SHARD-B  status=FAILED     candidates=0  hub=failed
```

`_reset_result_file()` now unlinks the file and leaves a `did_not_complete`
placeholder, mirroring bindcraft. Two details worth knowing before editing it:

- **The unlink is the load-bearing half; the placeholder is the courtesy.**
  `open(..., "w")` truncates, but only once it has opened — and the `OSError` arm
  of the writer is one of the two ways this leak is reachable at all. Remove
  first, and a failed placeholder write leaves no file, which the hub reports as
  a failure, instead of another job's designs.
- **The placeholder deliberately does not go through `_write_result`.** That
  would set `_RESULT_WRITTEN` before the shard had reported anything, and
  `main()`'s catch-all would then decline to write the real diagnosis — trading a
  rare stale-result leak for routinely losing every crash diagnosis. `_dump_result`
  was split out so the placeholder can reuse the write path without the flag.

Verified: **`3720 passed, 19 skipped`** repo-wide (3706 + 14 new tests). The
fixer ran 7 mutations; the reviewer reproduced all 7 with matching red counts and
added 11 more. One survived — the placeholder's `bucket` string — and was traced
to ground: `gpu/modal_client.py:506-512` flattens the error dict to a string
before `blueprints/jobs.py:322` re-wraps it, so the pipeline's own bucket never
reaches billing classification. Cosmetic, not a defect.

The residual window is **~0.4 s**, measured, not microseconds: a kill between the
child starting and `_reset_result_file()` executing still leaks. It is low-risk
for the right reason — nothing has allocated the 64 MiB of PDB or the ~85 MiB of
base64 yet, which is what makes the OOM likely. Closing it entirely needs the
wrapper change this task ruled out.

This overlaps the paused wrapper-side "Modal stale-result guard" work (6
wrappers, coded, not shipped). Bindcraft's own comment argues the container-side
fix is the one that closes it for every tier without touching the wrapper, so
this is now the primary guard and the wrapper work becomes belt-and-braces.

## OPEN — needs a human decision

### 1. The design deadline is 480 s tighter than the kill it replaces

`DESIGN_SUBPROCESS_DEFAULT_TIMEOUT_S = 6600` (`run_pipeline.py:2075`), while
`modal_app.py:70` sets `_MAX_SESSION_S = 7200` and `:261` gives run_pipeline
`max(60, 7200 - 120) = 7080` s.

**A web job whose search takes 6600–7080 s used to complete and now gets
killed.** That is a silent capability reduction on the live product, and nobody
has measured real Fc-shard runtimes against it. Some headroom is unavoidable —
the timeout has to fire early enough to write a result — but 600 s of it is a
guess. It is overridable with `PROTEINA_DESIGN_TIMEOUT_S`.

`test_the_deadline_leaves_the_shard_time_to_report` reads `_MAX_SESSION_S` out
of `modal_app.py` by AST and asserts ≥300 s headroom, so raising the default to
6780 is the most that test allows without also changing it.

### 2. Five further web-path changes, none of them in the accepted list

Going in, exactly two web-path changes were accepted: the target-chain dedup and
`prepare_custom_target`'s per-chain absence refusal. The audit confirmed both are
correctly characterised and narrow — and found five more:

1. **Every Storage object is renamed.** The dense 1-based rank changes uploaded
   basenames from `designs/design_000.pdb` to `designs/design_001.pdb`, and
   `source_rank` in merged campaign exports with them. Intended — it is what
   aligns proteina with `shared/exports.py`'s cross-tool invariant — but
   production-visible.
2. **A failed PUT no longer drops the design.** Web-path `n_failures` arithmetic
   changes and the result grows a `failed_uploads` key.
3. **A web job can now go `COMPLETED` → `FAILED` and exit 1.** Defensible,
   arguably correct, but it changes what the hub records.
4. **New keys on web results** (`output_census`, and `partial`/`search` when
   `rc != 0`). Only the fully-healthy web path is byte-identical.
5. The deadline, above.

### 3. One conditional hotspot risk — routed, not fixed

`run_pipeline.py:2559` routes hotspots through `normalize_hotspots`. Today's
adapter always sends `hotspot_spec` and `hotspot_residues` consistently, so live
submissions are unaffected. But a **stored target record predating the
`hotspot_spec` key** would send an empty spec alongside bare-int
`hotspot_residues`, and the alias fallback then changes behaviour: single-chain
becomes constrained where it was not, and multi-chain hard-fails pre-GPU with
`hotspot_chain_ambiguous`. Whether such a record exists is a data question that
cannot be answered offline.

Hotspot chain-semantics are owned by a concurrent session and were deliberately
out of scope here. This is reported, not touched.

## Things that came back clean

Score key names and candidate shape are a superset of pxdesign's reference
`_candidate_from_design`, and every score key is already carried by name in
`webhooks/modal.py:515-524`. The missing `filter_status` is deliberate —
`shared/ranking.py:341-350` names proteina among the tools that carry no filter.
Heartbeat payloads match `_sanitize_candidate`. Environment-variable contracts
match. `run_validate` is untouched.

Docs-only nit: the module docstring's Contract block (`run_pipeline.py:9-17`)
omits the three env vars this branch added — `PROTEINA_INLINE_PDBS`,
`PROTEINA_INLINE_PDB_CAP_BYTES`, `PROTEINA_DESIGN_TIMEOUT_S` — all three of which
appear in failure text telling an operator to change them.

## Working method notes for whoever picks this up

- **The worktree is at** `…/5762449d-…/scratchpad/qc-worktree`. Work there, not
  in the live repo — a concurrent session owns `shared/`, `templates/`,
  `static/`, `tools/base.py` and is actively writing them.
- **Interpreter is the repo venv**, `venv/Scripts/python.exe`. Bare `python` on
  PATH is a different venv and fabricates a false baseline.
- **Always say which scope a test count came from.** `pytest` at the repo root
  collects 3725; `pytest tests/` collects 3715. Comparing one against the other
  manufactures a fake ten-test discrepancy.
- **An interrupted agent's output can parse, read beautifully, and still be
  red.** `079b297` did. Both its failures were fixture defects — one shared PDB
  blob across all designs made a per-design "break this upload" switch break
  every upload. A fixture that cannot distinguish the bug from correct behaviour
  is the recurring failure mode on this codebase.
