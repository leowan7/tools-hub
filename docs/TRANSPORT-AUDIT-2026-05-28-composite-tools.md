# Transport Audit — b64/Storage gap on the 4 other composite tools

Date: 2026-05-28. Follows the pxdesign fix (`llm-proteinDesigner@64aad13`) which closed a
silent byte-loss in the webhook/pilot tier. This audit checks whether the same class of bug
exists in the other four composite tools' `docker/<tool>/run_pipeline.py`.

Audited against `llm-proteinDesigner@origin/master` (post repo-separation, includes the
pxdesign fix). Read-only — no code changed. Source files snapshotted, not the live working tree.

## The pxdesign bug being hunted

The webhook tier resolved each passing design's on-disk file with a too-short ladder. When
resolution returned `None` (e.g. the "preview" preset's `summary.csv` lacked name columns and
the parser synthesized `design_N` labels that didn't match the file index), BOTH delivery
guards skipped silently:
- Storage: `if upload_filename in upload_urls and local_file and os.path.exists(local_file)`
- b64:     `if local_file and os.path.exists(local_file)`

Result: a job that reports COMPLETED, `pdb_key` set on each candidate, but zero deliverable
bytes anywhere. Secondary bug: metrics CSV uploaded as `Content-Type: text/csv`, which the
`tool-outputs` bucket rejects.

## Results matrix

| Tool | A. Resolution asymmetry | B. Silent byte-loss | C. Synthetic naming | D. CSV Content-Type | E. pdb_key basename | Overall |
|------|----|----|----|----|----|----|
| **rfdiffusion** | VULNERABLE | **VULNERABLE** | SAFE (works by naming convention) | VULNERABLE | SAFE | **HIGH** |
| **rfantibody** | divergent (safe direction) | SAFE (drops unresolved) | conditional | VULNERABLE | SAFE | MED |
| **bindcraft** | SAFE (single tier, real glob path) | SAFE | SAFE | VULNERABLE | SAFE | MED |
| **boltzgen** | divergent (safe direction) | SAFE (drops unresolved) | SAFE | VULNERABLE | SAFE | LOW |

Verification: rfdiffusion HIGH finding and the bucket allowlist were verified directly from
source. bindcraft/boltzgen/rfantibody byte-loss verdicts are from per-file agent reads with
cited line numbers (consistent reasoning; the SAFE structural pattern is a drop-on-unresolved
`continue`).

## Finding 1 — rfdiffusion: silent byte-loss (HIGH, same class as pxdesign)

`docker/rfdiffusion/run_pipeline.py`, webhook tier:
- L1413-1417: 2-layer ladder only — `{design_name}.pdb`, then `design_{name.split('_')[-1]}.pdb`.
  No rglob, no hard-fail. (Smoke tier at L403-414 has a 3rd layer + a hard `FAILED` return.)
- L1427-1434: candidate dict built with `pdb_key` (L1429) + `local_file=backbone_pdb` (L1432),
  then **appended unconditionally** at L1434 regardless of whether `backbone_pdb` exists.
- L1435 (Storage) and L1470 (b64) both `os.path.exists`-guard and skip silently on a miss.
- L1478: entry appended to the webhook payload with no bytes.

Today this resolves by luck: `design_name` is a real `seqs/*.fa` stem (L946/L1073) that matches
RFdiffusion's `design_*.pdb` (L876). Any divergence (MPNN renaming, output-layout change) flips
it to a silent COMPLETED-with-no-bytes job. It is one naming-convention change away from the
exact pxdesign failure.

Fix: mirror boltzgen/rfantibody — `continue` (skip the candidate) when neither ladder layer
resolves, OR port pxdesign's `resolve_design_local_path` helper (rglob fallback) + a guard that
fails loudly if no candidate carries bytes.

## Finding 2 — CSV uploaded as text/csv on ALL FOUR tools (universal, low impact)

Authoritative: `tool-outputs` bucket `allowed_mime_types` (tools-hub
`supabase/migrations/0021_tool_outputs_storage.sql` L42-45) = `text/plain`, `chemical/x-pdb`,
`chemical/x-cif`, `chemical/x-mmcif`. The fixed pxdesign `upload_output` maps `.csv`→`text/plain`,
`.cif`→`chemical/x-cif`, `.pdb`→`chemical/x-pdb`.

All four tools still send `.csv` as `text/csv`:
- bindcraft L251 (`metrics.csv` + `bindcraft_results.csv`)
- boltzgen L575-576
- rfantibody L347-348 (`upload_output`)
- rfdiffusion L628

Each PUT 400s and is caught only as a `logger.warning`, so the metrics CSV silently never lands.
Impact is LOW: per-candidate scores are still in the candidate dicts (rendered in the table) and
structures are delivered via b64/Storage. Only the downloadable metrics CSV is lost.

Note: structure uploads (`chemical/x-pdb` / `chemical/x-cif`) are fine — both are allowlisted.
The earlier "is chemical/x-pdb rejected?" worry is resolved: it is accepted.

## Per-tool notes

- **rfantibody (MED):** byte-loss structurally prevented (unresolved designs `continue`, L1344/1349).
  Conditional risk (C): `parse_scores_tsv` synthesizes `design_{len(results)}` (L666) when the TSV
  lacks a tag column; if `qvscorefile` omits `tag`, all designs fail to match and pilot output is
  empty (a loud, non-silent symptom — not byte-loss). Likely benign since qvscorefile emits `tag`.
- **bindcraft (MED):** single tier, `pdb_path` is the literal `glob(Accepted/*.pdb)` hit — never
  reconstructed, never `None`. b64 runs independent of and before Storage upload. Only the CSV
  MIME bug applies.
- **boltzgen (LOW):** unresolved designs dropped via `continue` (L1568); pilot fallback pre-filters
  to `_has_structure_file` designs so fallbacks point at real files. Only the CSV MIME bug applies.

## Recommended next steps

1. **rfdiffusion (HIGH):** patch the webhook-tier ladder + add a drop/guard. Then 1 pilot-tier
   validation run to confirm the real FASTA-stem → `design_N.pdb` mapping holds on the live image.
2. **CSV MIME (all four):** one-line each `text/csv` → `text/plain`. No run needed to fix; the
   rfdiffusion pilot run will incidentally confirm it.
3. **rfantibody (MED):** confirm `qvscorefile` emits a `tag` column (closes C). Foldable into the
   same validation pass.
4. **bindcraft / boltzgen:** no byte-loss action; just the CSV MIME one-liner.

All code fixes land in `llm-proteinDesigner/docker/<tool>/run_pipeline.py`. Deferred here because
this session is leaving the llm-proteinDesigner working tree untouched (parallel session has
uncommitted Phase 11 edits + local master is behind origin).
