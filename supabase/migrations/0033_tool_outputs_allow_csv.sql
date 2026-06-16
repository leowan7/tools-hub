-- Ranomics tools-hub — allow CSV/TSV uploads into the tool-outputs bucket.
--
-- Composite pipelines (boltzgen/bindcraft/rfdiffusion/rfantibody) PUT a
-- per-job metrics.csv (and rfantibody a scores.tsv) carrying refolding-RMSD
-- and interaction scores. They send Content-Type "text/csv", but the 0021
-- allowlist only accepted [text/plain, chemical/x-pdb, chemical/x-cif,
-- chemical/x-mmcif, application/octet-stream], so those PUTs were rejected
-- with HTTP 400 while the design CIFs (chemical/x-cif) succeeded.
--
-- pxdesign already works around this by tagging its CSV as text/plain; this
-- migration fixes the rest centrally without redeploying any Modal pipeline.
-- Idempotent — safe to re-run.

UPDATE storage.buckets
SET allowed_mime_types = ARRAY[
    'text/plain',
    'text/csv',
    'text/tab-separated-values',
    'chemical/x-pdb',
    'chemical/x-cif',
    'chemical/x-mmcif',
    'application/octet-stream'
]
WHERE id = 'tool-outputs';
