# tools-hub — handoff 2026-06-10 (PDB-input robustness gaps)

This session audited PDB-input handling across every tool and fixed the one broken case: MPNN multi-chain design ("A B" / "H L" for an antibody heavy+light) was being wrongly rejected at the upload boundary. Five gaps remain where a structurally-odd but recoverable PDB still fails late on the GPU instead of being auto-fixed or flagged upfront with a fix instruction. All five are listed below with acceptance criteria, plus a ready-to-paste prompt at the bottom. The fixes live in app.py + shared/, which a parallel session is also editing, so pull and check status before starting.

The guiding principle to enforce: every PDB-accepting tool must either auto-remediate the PDB so the run proceeds, or reject it upfront with a specific, actionable message telling the user how to fix it. Never a silent acceptance that crashes generically 30-60 min later on the GPU. Today that bar is fully met only for the four binder-design tools (rfdiffusion, bindcraft, boltzgen, rfantibody) on fresh uploads.

## Done this session
- Fixed the MPNN multi-chain false-reject (commit `fe4cf0d`, pushed). `validate_target_chain` in `shared/pdb_inspect.py` now splits on whitespace and validates each chain token, naming the specific missing/ligand-only chain. Single-chain behavior is byte-identical. 20 tests pass; an adversarial QC agent reproduced every edge case and returned "safe to ship as-is."
- Removed user-facing em-dashes and hyphen ranges across all tool marketing surfaces (commits `b65141e` + `dce8f5a`, pushed). Independently re-audited by a review agent; clean.
- Verified the three deferred UI smoke checks: boltzgen budget cap (1 to 50), job-detail cancel confirm prompt, and post-cancel banner branching. All pass.
- Ran a two-agent audit of PDB handling across all 11 tools and the shared upload/preflight layer. Findings are the five gaps below.

## Next steps
- Gap 1 (highest value): pxdesign and boltz2 take target_chain + hotspots but get NO preflight hard-gate (only rfdiffusion/bindcraft/boltzgen/rfantibody are in `BINDER_DESIGN_TOOLS`, `shared/pdb_preflight_rules.py:~230`). A hotspot on an incomplete backbone, an internal gap, or an oversized target passes the boundary and crashes late on Modal. Extend preflight coverage to them. ACCEPTANCE: such a target produces an upfront NEEDS_FIX naming the residue/gap (or "trim to chain X"), cruft auto-cleaned as the binder tools already do, and boltz2's single-antigen-chain expectation enforced.
- Gap 2: reuse-token paths (`job:` / `handoff:` / `example:` / `resample:` in `app.py:~4288-4405`) skip BOTH inspection and the hard-gate (pdb_bytes stays None). `resample:` pipes a fold model's predicted PDB straight into MPNN unchecked. ACCEPTANCE: resolved reuse/handoff/resample bytes run through inspection + (for binder tools) preflight before any Modal call; watch the wallet-hold release on rejection.
- Gap 5 (quick safety fix, found in QC of `fe4cf0d`): `app.py:~4029` reads `inspection.chain(target_chain).min_resnum` with the whole string in the out-of-range-hotspot branch. If target_chain is multi-token, `inspection.chain("A B")` returns None then AttributeError -> 500. Not reachable today (MPNN has no hotspots) but one line from a real crash. FIX: `inspection.chain(target_chain.split()[0])` or guard `if chain is not None:` before reading min_resnum/max_resnum.
- Gap 3: rfantibody `cdr_lengths` (`tools/rfantibody/__init__.py:~49`, forwarded verbatim in build_payload) is an unvalidated free string; malformed values crash on Modal. ACCEPTANCE: validate format + ranges in validate() with a clear message ("CDR spec must look like H1:8,H2:7,H3:10-16").
- Gap 4: boltz2 silently narrows a multi-chain antigen to the named chain. ACCEPTANCE: if the uploaded antigen has more than one protein chain, tell the user ("antigen has chains A,B; only A will be folded") or have them confirm, rather than dropping the rest silently.
- The full ready-to-paste prompt covering all five is in Notes below.

## Blocked / waiting on
- All five fixes touch `app.py` and `shared/`, which a parallel session is actively committing. Blocked on coordination: `git pull` and check `git status` before editing to avoid a merge conflict. As of this handoff the parallel session had pushed through `ca71e10` and left `ALERTING.md` + `.github/workflows/synthetic-smoke.yml` in progress.

## Notes (optional)
- Coverage today, per tool: all 7 PDB tools flag malformed/empty/non-protein uploads, convert CIF->PDB, and check chain-exists + hotspot-range on FRESH uploads. Only the 4 binder tools additionally get the preflight auto-clean + structural hard-gate (server-enforced at submit, not just client JS). mpnn/pxdesign/boltz2 and all reuse paths lack that deeper layer — that is exactly what gaps 1-2 close.
- Commits this session: `b65141e`, `dce8f5a` (dash sweep), `fe4cf0d` (MPNN fix). All pushed to origin/main. The parallel session's working-tree files (app.py, billing/checkout.py, requirements.txt, scout/*, shared/email.py, shared/supabase_client.py, tools/platform_api/routes.py, ALERTING.md, synthetic-smoke.yml) were left untouched.
- Use the venv python on Windows: `venv\Scripts\python.exe -m pytest tests/ -q`. Do each gap as a separate atomic commit. No published pricing, no emojis, no em-dashes/connector-hyphens in user-facing copy (ranges as "X to Y").

### Ready-to-paste prompt for the next session

```
PDB-input robustness: close the remaining gaps in tools-hub so that for EVERY
PDB-accepting tool, a non-conforming PDB is either (a) auto-remediated so the run
proceeds, or (b) rejected UPFRONT with a specific, actionable message telling the
user how to fix it. Never a silent acceptance that crashes generically 30-60 min
later on the GPU. Repo: C:\Users\lab\Documents\Claude_projects\tools-hub.

COORDINATION: app.py, shared/email.py, shared/supabase_client.py may be edited by
a parallel workstream. `git pull` / check `git status` first; keep your diffs
surgical and isolated. shared/pdb_inspect.py, shared/pdb_preflight*.py, the
tools/<slug>/__init__.py adapters, and tests/ are clear to edit.

WHAT EXISTS TODAY (read these first):
- Upload boundary: app.py::tool_submit (~line 3990-4075). On a FRESH upload it runs
  inspect_pdb_bytes + CIF->PDB convert + validate_target_chain + validate_hotspots
  for all PDB tools. Friendly errors, never 500.
- Hard-gate preflight: shared/pdb_preflight.py + pdb_preflight_rules.py, run at
  app.py:~4120 ONLY for adapter.slug in BINDER_DESIGN_TOOLS =
  {rfantibody, rfdiffusion, bindcraft, boltzgen} (pdb_preflight_rules.py:~230). It
  dry-runs the GPU normalizer: cleans HETATM/water/numbering (auto-fix), and
  hard-blocks NEEDS_FIX with actionable messages (size envelope, internal gaps,
  hotspot-on-incomplete-backbone). Server-enforced (re-checked at submit, not just
  client JS).
- The four binder tools already meet the goal. The work is extending the same
  dual behavior (auto-fix OR actionable upfront message) to the tools/paths below.

GAPS TO CLOSE (each must end with auto-fix-or-actionable-message, plus a test):

1. pxdesign and boltz2 have NO preflight despite taking target_chain + hotspots.
   A hotspot on an incomplete backbone, an internal gap, or an oversized target
   passes the boundary and crashes late on Modal. Extend preflight coverage to
   them (add to the gated set / give them appropriate rule entries), or run the
   equivalent structural checks. ACCEPTANCE: uploading such a target produces an
   upfront NEEDS_FIX with a specific instruction (which residue/gap, or "target
   too large, trim to chain X"), and cruft is auto-cleaned where the binder tools
   already do. Confirm boltz2's single-antigen-chain expectation is enforced.

2. Reuse-token paths (job:/handoff:/example:/resample: in app.py ~4288-4405) skip
   BOTH inspection and the hard-gate (pdb_bytes stays None). resample: base64-
   decodes a fold model's predicted PDB straight into MPNN unchecked. ACCEPTANCE:
   reused/handoff/resample bytes run through the same inspection + (for binder
   tools) preflight on the resolved bytes before any Modal call, so a mismatch is
   flagged upfront. Watch the wallet-hold release path on rejection.

3. rfantibody cdr_lengths (tools/rfantibody/__init__.py:~49, forwarded verbatim in
   build_payload) is an unvalidated free string. Malformed values (e.g. "H3:abc",
   lengths exceeding the framework) crash on Modal. ACCEPTANCE: validate format and
   ranges in validate() with a clear message ("CDR spec must look like
   H1:8,H2:7,H3:10-16; H3 length must be 5-20"), rejected at submit.

4. boltz2 multi-chain antigen is silently narrowed to the named chain
   (run_pipeline uses only chain X). ACCEPTANCE: if the uploaded antigen has >1
   protein chain, tell the user ("antigen has chains A,B; only A will be folded")
   or have them confirm, rather than silently dropping the rest.

5. LATENT 500 (found in QC of commit fe4cf0d): app.py:~4029 does
   inspection.chain(target_chain).min_resnum in the out-of-range-hotspot branch.
   If target_chain is multi-token ("A B"), inspection.chain("A B") returns None ->
   AttributeError -> 500. Not reachable today (MPNN has no hotspots) but one line
   from a real crash. FIX: use target_chain.split()[0] there, or guard `if chain
   is not None:` before reading min_resnum/max_resnum.

TESTS: extend tests/test_pdb_inspect.py and add per-tool validate() tests. Run
`venv\Scripts\python.exe -m pytest tests/ -q` (Windows; use the venv python). For
each gap, add a regression test that asserts the upfront rejection/auto-fix fires.

Do the fixes as separate atomic commits (one per gap). Do NOT publish pricing or
touch unrelated files. No emojis, no em-dashes/connector-hyphens in any user-facing
copy (ranges as "X to Y"). Confirm each acceptance criterion before committing.
```
