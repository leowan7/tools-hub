# tools-hub - handoff 2026-08-18 (GPU image digest pins)

Leo reported an error last week about the OpenDDE "pin" possibly making the tool
non-functional. The underlying problem was real and wider than OpenDDE: all nine
deployed GPU apps were pulling something by a mutable reference, so any of them
could silently change on a merge that had nothing to do with that tool. This is
the same failure that broke every Proteina design run when dm-haiku moved. All
nine are now pinned, a guard test stops it recurring, and the fix is merged as
PR #150. The cold rebuild of all nine images finished green on every app.

## Done this session
- Pinned all eight `tools/*/Dockerfile.modal` base images by digest instead of by
  tag. OpenDDE was the worst case: a one-tag preview image whose own comment
  asked for a digest pin that was never applied.
- Pinned the ninth app, esmfold2_design, which has no Dockerfile and so was
  invisible to any Dockerfile-shaped check. It had five dependencies with no
  version at all.
- Used the versions actually running in production, read out of the built image,
  rather than "latest". Two of them had already drifted on PyPI and would have
  changed on the next merge.
- Added `tests/test_dockerfile_digest_pins.py`, which now catches all 17 attack
  cases tried against it.
- Ran three independent QC rounds. Each found a case where the guard passed the
  exact thing it claimed to block. All fixed and re-proven.
- Merged PR #150 to main (`71cf4cf`). Suite green at 4900 passed, 10 skipped.
- Deploy run 32155177210 rebuilt all nine images from scratch and passed 9 of 9.
  That is the first time these pins were ever actually exercised.

## Next steps
- Run one real job through OpenDDE to close out the original report from Leo's
  point of view, since none of this was validated on a GPU.
- Decide whether to close the `transformers` branch reference described below.
  It needs either `--no-deps` or a vendored fork, so it is its own piece of work.

## Blocked / waiting on
- Needs your decision on the `transformers @ git+...@main` reference inside the
  `esm` package. It points at a branch on a third-party fork and re-resolves on
  every rebuild, which is exactly the hazard this session set out to remove. It
  cannot be pinned from this repo, so it is documented in the code rather than
  fixed.

## Notes
- Merging anything under `tools/` rebuilds all nine GPU images. Because layer
  caches key on the base digest, this merge triggered real cold builds rather
  than the usual 30 second no-op deploys. Expect that again on the next base
  image bump.
- Conda's single `=` is a fuzzy "starts with" match, not a pin, and it leaves the
  build string free. Bioconda republishes: anarci 2020.04.23 exists under four
  build numbers and hmmer 3.4 under twenty-one. The conda specs use `==` with an
  explicit build string for that reason.
- To resolve a digest without pulling the image, use the registry API. Note that
  nvcr.io authenticates at `https://nvcr.io/proxy_auth`, not at
  `authn.nvidia.com`, which returns 401 anonymously.
- `modal shell` does not work on Windows. To read inside an already built image,
  create a `modal.Sandbox` from the image object imported out of the local
  `modal_app.py`. The definition matches, so it is a cache hit and you see the
  deployed image without paying for the GPU the function would allocate.
