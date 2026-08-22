# Should Epitope Scout install `mkdssp`? — decision

> **SUPERSEDED 2026-08-21 (pending merge — the branch is not deployed).**
> Do not resume the install work. The question this document answers ("how do
> we get real DSSP into the deployed image?") is answered without a binary by
> an open branch; it is answered in code, not yet in production. `scout/pydssp_numpy.py`
> is DSSP's H-bond algorithm -- simplified, per upstream: no beta-bulge,
> approximate amide H, 3-state output -- in 113 vendored MIT lines, with no
> new dependency;
> it agrees with mkdssp 4.2.2 on **97.9%** of residues against the
> fallback's 70.2%, and it *accepts headerless coordinate files that
> mkdssp 4.2.2 refuses outright*.
>
> It is **not** "strictly better" than the install, and an earlier draft of
> this banner said so wrongly: mkdssp is the *oracle* those 97.9% are
> measured against, so the binary wins on accuracy by the 2.1 points pydssp
> gives up. pydssp wins on coverage, and on needing no install at all —
> which is what makes the Railpack blocker stop mattering.
> See `docs/qc/scout-pydssp-adoption.md`.
>
> The merits analysis below is still worth reading (§2 and §7 in
> particular, which record where earlier drafts were wrong), and §0 remains
> the authoritative account of why `nixpacks.toml` is inert. But the
> "decision" it reaches is no longer the live one.

**Decision (2026-08-19): the install is BLOCKED on a mechanism defect, not
on the merits. `nixpacks.toml` is not read by this deploy.**

The merits were worked through and reached "install". A commit setting
`nixPkgs = ["gcc", "dssp"]` was written and reviewed across four QC
rounds, then **dropped unmerged** when the Railway dashboard showed the
service builds with **Railpack**, not Nixpacks (§0). The merits reasoning
is kept below; only the mechanism has to be redone.

**SS provenance shipped regardless** (§5), and is the only reason this
class of failure is detectable at all.

Evidence base: `docs/qc/scout-dssp-fallback-measurement.md` (30 chains,
4487 residues, real mkdssp 4.2.2 oracle). Independently QC'd four times;
§2 and §7 record where earlier drafts of this document were wrong.

---

## 0. Why the install is not in this PR

`nixpacks.toml` is not part of this deploy's build path. Read off the
Railway dashboard on 2026-08-19:

| service | source | builder |
|---|---|---|
| tools-hub `web` (production) | `leowan7/tools-hub` @ `main` → tools.ranomics.com | **Railpack**, python@3.13.0 |
| epitope-scout `web` (production) | **`leowan7/epitope-scout`** @ `main` → scout.ranomics.com | **Railpack** |

Build log of the then-active tools-hub production deployment:

```
using build driver railpack-v0.37.0
prepare railpack-v0.37.0
install mise packages: python
```

Railpack installs via **mise**; no nix step appears in the log. So
`nixPkgs = ["gcc", "dssp"]` would have installed nothing — green build, no
binary, and `assign_dssp` silently continuing on the phi/psi fallback.
That is the "install that looks successful and changes nothing" failure
this document had already named as the thing to watch for, arrived at by a
route nobody checked. The existing `gcc` entry is presumably inert too.

**Two consequences beyond this PR.**

1. **`README.md` is stale.** It calls `nixpacks.toml` the deploy's only
   native-dependency source. That premise is what this task was built on,
   and four QC rounds reasoned carefully about nixpkgs attribute names,
   `meta.mainProgram` and libcifpp build flags without anyone checking
   which builder actually runs.
2. **`scout.ranomics.com` deploys from a different repository.** The
   relationship between `leowan7/epitope-scout` and this repo's `scout/`
   package was NOT established. If that host is the Scout users reach,
   everything here — the provenance column included — may not reach them.
   Settle this before more Scout work lands in this repo.

   > **RESOLVED 2026-08-21.** `scout.ranomics.com` 301-redirects to
   > `https://tools.ranomics.com/scout/` (verified by `curl -sI`, which
   > returns `301` then `200`). `leowan7/epitope-scout` is a pure redirect
   > shell: its `app.py` redirects every path except `/health`, and it has
   > done so since 2026-04-24; everything else was deleted on 2026-08-19
   > (`a8c3bf3`). **This repo's `scout/` package is what users reach**, so
   > #158, #161, #165 and this change all landed in the right place. The
   > README still described the old arrangement and has been corrected.

**NOT verified:** that Railpack ignores `nixpacks.toml` outright. The
inference is "builder is Railpack" plus "installs via mise" plus no nix
step in the log; no entry was removed to watch a build fail. The burden
sits on any future native dependency to prove it installs, with a log.

**Redoing it properly** means Railpack configuration (`railpack.json`, or
the service's build settings) plus a build log showing mkdssp present —
and answering (2) first.

## 1. The prerequisite — CLEARED

`assign_dssp` read `residue_data[1]` (the amino-acid column) instead of
`[2]`. With mkdssp present that scores **37.6%** per-residue agreement
against the fallback's **70.2%** — installing the binary first would have
made Scout roughly twice as wrong.

PR #161 merged as `b73bfad`; trunk now reads the right column:

```
$ git fetch origin && git show origin/main:scout/scoring.py | grep -n 'residue_data\['
440:            ss_code = residue_data[2]
```

Run that command rather than trusting this document. Within the single
session that produced this file the claim was true, then false, then true
again: the branch was unpushed, then pushed with an open PR, then merged,
and trunk moved three times. **Do not quote a SHA here as a durable
fact** — check the remote, not a local ref (this repo has ~30 worktrees
and local `main` drifts).

## 2. What the §4 control does and does not establish

The measurement's control scores each arm on how well it recovers the
**real-DSSP** ranking:

| | mean rho | top-1 differs |
|---|---|---|
| phi/psi fallback | 0.7872 | 11/30 |
| no SS term at all | 0.7874 | 8/30 |

Wilcoxon signed-rank **p = 0.73**.

**It establishes:** the phi/psi fallback carries no more information about
the DSSP ranking than a constant does. That is a statement about *the
fallback*, not about the SS term in general.

**It does not establish** that the SS term is worthless. The same table
shows real DSSP moves the top-1 epitope relative to no-SS on **8/30**
chains — the term computed from a real oracle does reorder the list. An
earlier draft read the control as "the term carries no rank information"
and used that as a value bar the install failed. That is absence of
evidence used as evidence of absence, and it is withdrawn.

**The control also cuts the other way:** if the SS term contributes little
rank fidelity, the *ranking risk* of installing DSSP is correspondingly
small, while the label defect in §3 is real and measured. The honest
framing is a tradeoff, not a failed bar.

### The merits, as settled

1. ~~The prerequisite is not on trunk.~~ **CLEARED** by #161 (§1).
2. **The DSSP branch has never executed in production** and no build has
   ever been run with mkdssp in it. Not an argument against installing —
   a step of installing. It means the change cannot be validated by the
   test suite.
3. **Installing churns the ranking Scout actually delivers.** The
   measurement quantifies this four ways; quote the one that matches the
   product surface, not the smallest:

   | churn measure | value |
   |---|---|
   | top-1 epitope differs | 11/30 (37%) |
   | **top-3 set not identical** | **16/30 (53%)** |
   | patches that change rank | 191/281 (68%) |
   | mean abs rank shift | 1.08 places |

   **Scout delivers a top-3** — `epitopes_annotated.csv`
   (`scout/routes.py:726-735`) and `epitopes.csv` (`:746-754`), downloaded
   as `top3_epitopes.csv` (`:829`). So **16/30 is the honest headline:
   more than half of analyses would hand the user a different epitope
   set.** An earlier draft cited only 11/30. Scout's ranking has never
   been checked against binder outcomes, so real DSSP is a *reference*,
   not a ground truth for epitope quality; moving the ranking is not the
   same as improving it.

That third point is a judgement, not a measurement gap. It was weighed
against §3 and lost — which is why the merits verdict is "install", and
why §0 rather than §2 is what stops it.

## 3. The benefit the install buys — the deciding factor

Independent of ranking, the fallback corrupts two user-facing outputs:

- **The displayed secondary-structure label** agrees with truth **50.4%**
  of the time. Rendered at `templates/scout/index.html:554` (legend
  alpha/beta/L) and `:646` (results table).
- **The `loop-only anchor` quality flag** is gated on
  `secondary_structure == "loop"` (`scout/flags.py:108`), fed from
  `scout/routes.py:715`. True-loop recall is **0.339**, so roughly two
  thirds of genuinely-loop patches are labelled helix or strand and the
  advisory is **suppressed on the patches that most need it**.

That is a measured defect in advisory output, visible to users, and it is
what carried the merits decision. It remains unfixed.

## 4. Coverage of an install could not be quantified

Scout has three input paths:

| Path | Source | HEADER present? |
|---|---|---|
| `/scout/example` | `static/example/1HEW.pdb` (hardcoded, `scout/routes.py`) | yes |
| `/scout/fetch-pdb` | RCSB `.pdb`, `.cif` fallback | `.pdb` yes; `.cif` hits the Biopython label/auth chain-ID issue (measurement §6) |
| `/scout/upload` | arbitrary user file | **unknown** |

Only `/upload` is in question, and its headerless share was unrecorded:

- `shared/metrics.py:137` defines the Prometheus counter `SCOUT_RUNS`, and
  **nothing increments it** — a dead counter. So one of the two provenance
  vehicles originally proposed would have recorded nothing.
- The Supabase ledger `public.scout_runs` gets a row per completed run
  (`scout/quota.py`) storing only `{job_id, chain, uniprot_id}`.
- Job directories are reaped hourly (`scout/jobs.py:163`,
  `max_age_seconds=3600`) from a container-local `tmp/` with no volume.

**Correction to the framing this task arrived with:**
`static/example/3s7g_fc_ab.pdb` is *not* a shipped Scout example.
`/scout/example` hardcodes `1HEW.pdb`, which has a HEADER, and
`git grep 3s7g_fc_ab` returns documentation hits only — no code, template
or JS reference. It is a Proteina Fc campaign target
(`docs/HANDOFF-2026-08-07-fc-run-ready.md`), unreachable from Scout's UI.
The headerless obstacle is real — `upload()` writes uploads verbatim and
design-pipeline output routinely lacks a HEADER — but that file does not
demonstrate it, and "3 of the 5 files in `static/example/` are PDBs, one
headerless" is not a traffic estimate.

## 5. The provenance column, which shipped

`ss_method`, recorded per run:

| Where | Value |
|---|---|
| `results.csv` column `ss_method` | `dssp` \| `phi_psi` \| `none` |
| `scout_runs.metadata->>'ss_method'` | same |

`assign_dssp` returns `(ss_map, method)`. An empty map reports `none`
rather than crediting a branch, because every patch then lands on the loop
floor whichever branch produced it — and `_assign_ss_by_phi_psi` reaches
`{}` through its *normal* return path, so that case logs no warning at all.

No migration: `scout_runs.metadata` is `jsonb NOT NULL DEFAULT '{}'`,
documented in `supabase/migrations/0002_scout_runs.sql` as free-form space
for future instrumentation.

This is also what would have caught §0 from the outside: after a
"successful" install, `ss_method` staying `phi_psi` is the only signal
that no binary arrived.

### This instrumentation has a hole, in the population that matters

`/scout/analyze` is reachable **anonymously** (`scout/routes.py:549` —
`@anon_rate_limit`, no `@login_required`), but the ledger write is gated:

```python
_email = session.get("user_email", "")
if _email:
    record_scout_run(...)          # scout/routes.py:774-775
```

So for anonymous runs `ss_method` is written only to `results.csv`, on
container-local disk, deleted within the hour. **The anonymous
top-of-funnel — the population most likely to upload headerless
design-pipeline output — is exactly the population this misses.** Closing
that gap means recording provenance on a path that does not require an
email; not done here.

### Trap hit while adding the column

`scout/flags.py` hand-duplicates `pipeline.CSV_COLUMNS` as
`_CSV_COLUMNS_BASE`, under a comment asking whoever adds a column to
update both. Nothing enforced it. `routes.py` writes
`results_annotated.csv` with `DictWriter(fieldnames=CSV_COLUMNS_ANNOTATED)`
from rows read out of `results.csv`, and `DictWriter` raises `ValueError`
on a key with no fieldname — so adding `ss_method` to `pipeline.py` alone
throws HTTP 500 on **every analyze run**, not merely on download, with the
whole suite still green. Both lists updated;
`test_csv_column_lists_are_in_sync` now enforces the comment.

The duplication is structurally invisible to the existing suite:
`tests/test_scout_anonymous_access.py:103,116` builds its fake
`results.csv` from `flags._CSV_COLUMNS_BASE` rather than
`pipeline.CSV_COLUMNS`, so fixture and writer drift together. The
duplication's stated reason — "on Windows without freesasa, pipeline.py
fails to import at module level" — is stale: `freesasa` is imported lazily
inside `compute_rsa`, and `import scout.pipeline` succeeds on Windows with
no `freesasa` installed. It could be deleted outright; left as out of
scope.

## 6. To pick this up again

1. **Answer §0 item 2 first:** which repo serves the Scout that users
   reach. If it is `leowan7/epitope-scout`, this repo may be the wrong
   place for the whole line of work.
2. Configure Railpack, not `nixpacks.toml`, and prove it with a build log.
3. The merits argument in §2/§3 does not need re-litigating — only the
   mechanism changed.
4. Read the `ss_method` split first. It measures how much traffic an
   install would actually convert, for signed-in runs (§5).

## 7. Not verified

- **No Railway build was ever run with `dssp` in it**, and none will be
  from this file — see §0. That `pkgs/by-name/ds/dssp` exists in nixpkgs
  and sets `meta.mainProgram = "mkdssp"` was read off nixpkgs by a review
  round; no version was ever established (an earlier draft asserted
  v4.4.10 as verified, which was wrong), and it is moot while the builder
  does not read the file.
- **That Railpack ignores `nixpacks.toml`** — inferred, not proven by
  removing an entry and watching a build fail.
- **No live Scout run.** The `ss_method` values this will record in
  production are unobserved.
- **The `dssp` branch of `assign_dssp` has still never executed against a
  real binary** in this repo. Its `"dssp"` provenance value is asserted
  only against a faked DSSP object.
- **`/upload` traffic composition is unmeasured**, which is §4's point,
  and §5 explains why the new instrumentation only partly closes it.
- **The relationship between `leowan7/epitope-scout` and this repo** was
  not investigated at all.
- The measurement's statistics (rho, Wilcoxon p, 37.6% vs 70.2%, the churn
  figures) were taken as given; the generating scripts were never
  committed.
- The `.cif` label/auth chain-ID failure mode was read from the
  measurement document; a review round reproduced the mechanism from
  Biopython's source, but nobody ran it against a real mmCIF.
- **Two risks named but unmeasured**, should the install ever land:
  Biopython runs mkdssp via `Popen` + `communicate()` with **no timeout**,
  twice per run, inside a gunicorn worker (default pool 2) — untested
  under load, on a tier with prior OOM history; and nixpkgs builds
  `libcifpp` with `-DCIFPP_DOWNLOAD_CCD=OFF`, so mkdssp may error at
  runtime and fall back silently.
- End-to-end CSV verification used Biopython `ShrakeRupley` in place of
  `compute_rsa`; `freesasa` has no Windows wheel. That changes patch
  composition, not the provenance plumbing.

**Process note.** Across four QC rounds, every fix round on this document
introduced a new false claim while closing the previous one — a stale SHA,
an unsupportable version, a fix applied to one file and not its sibling, a
corrected citation that was itself wrong. The failures were never in the
code, which was clean from round 1; they were in prose asserting more than
had been checked. And the defect that finally mattered — §0 — was a
premise inherited from `README.md` that no round questioned, because every
round was checking the answer rather than the question. Re-check git facts
against `origin/*`, and check what the deploy actually does before
reasoning about what a config file means.
