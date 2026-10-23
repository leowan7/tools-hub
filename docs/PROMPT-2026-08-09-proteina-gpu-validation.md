# Prompt for a fresh session: validate proteina inline delivery on real GPU hardware

Copy everything below the line into a new session.

---

Proteina's inline-delivery work is shipped and deployed but has **never run on a
GPU**. Your job is to prove it works on real hardware, spending as little as
possible, and stopping to ask before every paid step.

## What shipped

`main` is at `1302d47`. Two PRs landed on 2026-08-09 and both rebuilt the
`ranomics-proteina-prod` Modal image:

- **#124** — proteina returns design coordinates inline as `pdb_content_b64`
  when no `upload_urls_endpoint` is supplied, matching the five sibling tools.
  Previously it hard-failed pre-GPU without one. Also closed 14 parity gaps and
  a cross-job leak where a warm container could hand one job the previous job's
  designs and score it `succeeded`.
- **#126** — raised `DESIGN_SUBPROCESS_DEFAULT_TIMEOUT_S` from 6600 to 6780 s,
  which is exactly the headroom floor (`7080 - 300`).

3873 tests pass. **Every one of them stubs the search subprocess**, so the logic
is well covered and the behaviour is entirely unverified.

Phase 0 of the hotspot canary (free, CPU-only) passed against `1302d47`. That
proves the container starts, stages a target and executes the merged code. It
never reaches the model, so it says nothing about design output.

## The goal

Answer two questions, in this order, cheapest first:

1. **Does `run_tool` accept a direct-call payload instead of refusing pre-GPU?**
   That was the original bug. `--validate` answers it.
2. **Do real coordinates come back inline?** Only a real search answers it.
   `--submit` plus `--collect`.

## The harness

`tools/proteina/direct_call_fc.py`. Modes:

| mode | cost | what it proves |
|---|---|---|
| `--dry-run` | free, never contacts Modal | payload shape only; confirms `upload_urls_endpoint` is absent |
| `--validate` | ~$0.50 | short-circuits before GPU *work* but **not** before GPU *billing* |
| `--submit` | ~$4 | one 8-design shard (`nsamples 4 x replicas 2`), the real thing |
| `--collect` | free | retrieves a submitted run by call id |

**There is no cheap GPU confirmation.** `modal_app.py:66` declares
`_GPU = "A100-80GB"` and applies it unconditionally at `:234`, and `run_tool` is
the only `@app.function` in the file. There is no CPU-only sibling, so every
touch of the production path bills A100 wall-clock at roughly $0.105/min (from
the repo's own $12.58 per 7200 s figure).

## Rules

- **Ask before every paid invocation.** Report the expected cost, wait for a
  clear yes. `--dry-run` is the only thing you may run unprompted.
- **Do not deploy, do not merge, do not push to `main`.** Merging any proteina
  PR auto-rebuilds the production image.
- Do not touch other tools, GPU SKUs, or billing rates.
- Do not modify anything under `boltzgen-workspace`. Local-only, no remote, ever.
- If Phase 0 needs re-running (only if the image rebuilt again):
  `modal run tools/proteina/_hotspot_canary.py --phase 0`. Free.

## Environment gotchas that will cost you time

- **Interpreter:** `C:\Users\lab\AppData\Local\Programs\Python\Python313\python.exe`.
  The bare `python` on PATH is a hermes-agent venv. There is no repo venv.
- **`prometheus-client` may be missing locally**, which makes
  `tests/test_metrics.py` throw 6 failures that look exactly like a regression
  and are not. It is declared at `requirements.txt:12`; CI installs it. If you
  see those 6, `pip install prometheus-client` and move on.
- **The live repo working tree is OCCUPIED.** It sits on
  `feat/proteina-inline-designs` at `4b03ab8` with 21 modified and a dozen
  untracked files belonging to a concurrent multi-chain session. **Do not
  `git checkout main` there.** Work in a fresh worktree off `origin/main`.
- Because of the above, a bare `grep` of that tree can return nothing for code
  that exists on trunk. Use `git grep <pattern> origin/main -- <path>`.
- `--submit` stages the target through `shared.storage`, which needs `.env`.
  `.env` lives in the live repo and deliberately does **not** exist in scratch
  worktrees.
- `--submit` writes a state file and **refuses to re-submit the same job id**
  or overwrite an uncollected call. Run `--collect` before submitting again.
  The call id is the only route to a finished run's designs, so losing it
  strands whatever you paid for.

## What counts as success

From `--collect`, the returned result must show:

- `candidates` is non-empty, and each entry carries `pdb_content_b64`.
- Decoding that base64 yields real PDB text with `ATOM` records, not a stub.
- Ranks are dense and 1-based (`1..N`), with no gaps from dropped designs.
- `pdb_key` is a bare basename like `design_001.pdb`, not `designs/...`,
  because there is no upload endpoint on this path.
- `status` honestly reflects what happened. A shard producing zero designs, or
  null scores, must not report `COMPLETED` with no error key.

Report what you actually observed, including anything that disagrees with the
above. A failure found here is the entire point of running it.

## Do not confuse these two

The hotspot canary's **Phase 1 (~$4) does NOT exercise the inline path.** It
runs in a different Modal app (`ranomics-proteina-hotspot-canary`) calling its
own `run_shard`, which borrows `run_pipeline.parse_designs` but never touches
prod's `run_tool` or the `main()` delivery code that builds `candidates` and
`pdb_content_b64`. Phase 1 proves hotspot steering. It is not a substitute for
`--submit`.
