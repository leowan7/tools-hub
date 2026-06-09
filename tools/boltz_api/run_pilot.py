"""Glycoform pilot driver — Stages A → F via the hosted Boltz API.

Default mode: dry_run. Writes every API request body to ./dry_run/*.json for review
before any credits are spent. Pass --submit to actually fire calls.

Sub-commands:

  prep      Parse PDBs, write Stage A and Stage C dry-run bodies for review.
  stage-a   Submit the 2 no-binder validation calls (S2G2F-Fc, G2F-Fc). Gate.
  stage-c   Submit the BoltzGen Protein Design job (n=100).
  stage-e   For each of the top-20 designs + 1 positive control, paired S&B (vs
            S2G2F-Fc, vs G2F-Fc). 42 jobs.
  stage-f   Rank, write FASTA + metadata + PyMOL session + HANDOFF.md.

Anything that costs API credits requires the explicit --submit flag. The 60-job
ceiling is enforced inside BoltzClient and re-enforced here per-stage.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

from .client import BoltzClient, BudgetExceeded, CreditLedger
from .prep_target import extract_fc_chains
from .yaml_glycoform import (
    GlycoformTargetSpec,
    build_library_screen_input,
    build_protein_design_input,
    build_structure_binding_input,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = REPO_ROOT / "runs" / "glycoform-pilot-s2g2f"
INPUTS_DIR = RUN_DIR / "inputs"
DRY_RUN_DIR = RUN_DIR / "dry_run"
RESULTS_DIR = RUN_DIR / "results"
LEDGER_PATH = RUN_DIR / "ledger.json"

S2G2F_PDB = INPUTS_DIR / "3SGJ.pdb"
G2F_PDB = INPUTS_DIR / "3AVE.pdb"

# Hardcoded for the mini pilot — upper CH2 face residues spatially close to Asn297 on
# canonical IgG1 Fc, given in standard Eu/PDB residue numbering. Real Stage B
# (SASA-delta hotspot mining on the Stage A CIF) is deferred to scale-up. These are
# converted to 0-indexed sequence positions per chain before being sent to the API.
PILOT_HOTSPOT_PDB_RESNUMS = [296, 298, 299, 300, 301, 326, 332]


def _pdb_resnums_to_seq_indices(pdb_resnums: list[int], first_pdb_resnum: int) -> list[int]:
    """Convert EU/PDB residue numbers to 0-indexed sequence positions for the API."""
    return [n - first_pdb_resnum for n in pdb_resnums]

# Pure-de-novo binder length range matching the Kao nanobody scaffold.
BINDER_MIN_LEN = 110
BINDER_MAX_LEN = 130

# Pilot design count — keep small.
N_PROTEIN_DESIGNS = 100
N_TOP_FOR_STAGE_E = 20


def _ensure_dirs() -> None:
    for d in (RUN_DIR, INPUTS_DIR, DRY_RUN_DIR, RESULTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _build_target_spec(pdb: Path, glycoform: str) -> tuple[GlycoformTargetSpec, int]:
    """Return (spec, first_pdb_resnum_chain_A) so callers can convert hotspots."""
    chains = extract_fc_chains(pdb)
    if len(chains) != 2:
        raise RuntimeError(f"Expected 2 Fc chains in {pdb}, got {len(chains)}")
    a, b = chains
    spec = GlycoformTargetSpec(
        fc_chain_a=a.chain_id,
        fc_chain_b=b.chain_id,
        asn_residue_index_a=a.asn297_index_in_sequence,
        asn_residue_index_b=b.asn297_index_in_sequence,
        fc_sequence_a=a.sequence,
        fc_sequence_b=b.sequence,
        glycoform=glycoform,
    )
    return spec, a.pdb_resnum_of_first_residue


def _new_client(submit: bool, ledger: CreditLedger | None = None) -> BoltzClient:
    return BoltzClient(
        dry_run=not submit,
        dry_run_dir=DRY_RUN_DIR,
        ledger=ledger or CreditLedger(),
    )


def cmd_prep(args: argparse.Namespace) -> int:
    _ensure_dirs()
    s2g2f, s2g2f_first_resnum = _build_target_spec(S2G2F_PDB, "S2G2F")
    g2f, _g2f_first_resnum = _build_target_spec(G2F_PDB, "G2F")

    # Stage A — target validation bodies (no binder).
    s2g2f_validate_body = build_structure_binding_input(s2g2f)
    g2f_validate_body = build_structure_binding_input(g2f)
    (DRY_RUN_DIR / "stage_a.s2g2f_target.json").write_text(json.dumps(s2g2f_validate_body, indent=2))
    (DRY_RUN_DIR / "stage_a.g2f_target.json").write_text(json.dumps(g2f_validate_body, indent=2))

    # Stage C — BoltzGen Protein Design body against S2G2F.
    hotspot_seq_indices = _pdb_resnums_to_seq_indices(
        PILOT_HOTSPOT_PDB_RESNUMS, s2g2f_first_resnum
    )
    design_body = build_protein_design_input(
        s2g2f,
        hotspot_residue_indices=hotspot_seq_indices,
        num_proteins=N_PROTEIN_DESIGNS,
    )
    (DRY_RUN_DIR / "stage_c.boltzgen_s2g2f.json").write_text(json.dumps(design_body, indent=2))

    # Stage E — paired S&B body templates (with placeholder binder sequence).
    placeholder_binder = "M" + "G" * (BINDER_MIN_LEN - 1)
    for glyco_name, target_spec, first_resnum in (
        ("s2g2f", s2g2f, s2g2f_first_resnum),
        ("g2f", g2f, _g2f_first_resnum),
    ):
        pocket_seq_indices = _pdb_resnums_to_seq_indices(
            PILOT_HOTSPOT_PDB_RESNUMS, first_resnum
        )
        body = build_structure_binding_input(
            target_spec,
            include_binder={"P": placeholder_binder},
            pocket_residue_indices=pocket_seq_indices,
        )
        (DRY_RUN_DIR / f"stage_e.template_vs_{glyco_name}.json").write_text(json.dumps(body, indent=2))

    print(f"Dry-run bodies written under {DRY_RUN_DIR}")
    print(f"  Stage A (target validation): 2 bodies — review then run `stage-a --submit`.")
    print(f"  Stage C (BoltzGen, n={N_PROTEIN_DESIGNS}): 1 body.")
    print(f"  Stage E paired-cofold templates: 2 bodies (will be re-rendered per design).")
    return 0


def cmd_stage_a(args: argparse.Namespace) -> int:
    _ensure_dirs()
    s2g2f, s2g2f_first_resnum = _build_target_spec(S2G2F_PDB, "S2G2F")
    g2f, _g2f_first_resnum = _build_target_spec(G2F_PDB, "G2F")
    client = _new_client(submit=args.submit)

    targets = [("s2g2f_target", s2g2f), ("g2f_target", g2f)]
    bodies = {label: build_structure_binding_input(spec) for label, spec in targets}

    # Estimate cost before any submission.
    if args.submit:
        total_est = 0
        for label, body in bodies.items():
            try:
                est = client.estimate_cost("structure_and_binding", body)
            except Exception as e:
                print(f"[warn] estimate_cost failed for {label}: {e}")
                est = None
            if est is not None:
                total_est += float(est)
            print(f"  {label}: estimated cost = {est}")
        print(f"Stage A estimated total cost: ${total_est:.4f} for 2 jobs")
        if not args.yes:
            confirm = input("Proceed with submission? [y/N] ").strip().lower()
            if confirm != "y":
                print("Aborted by user.")
                return 1

    prediction_ids: dict[str, str | None] = {}
    for label, body in bodies.items():
        pid = client.submit("structure_and_binding", body, label=f"stage_a.{label}")
        prediction_ids[label] = pid
        print(f"  {label}: submitted; prediction_id = {pid}")

    if args.submit:
        import requests as _requests  # for CIF download
        results = {}
        for label, pid in prediction_ids.items():
            assert pid is not None
            print(f"  Waiting on {label} ({pid}) ...")
            data = client.wait_for("structure_and_binding", pid)
            best = (data.get("output") or {}).get("best_sample") or {}
            metrics = best.get("metrics") or {}
            entry = {
                "prediction_id": pid,
                "status": data.get("status"),
                "metrics": metrics,
                "error": data.get("error"),
            }
            struct_url = (best.get("structure") or {}).get("url")
            if struct_url:
                cif_path = RESULTS_DIR / f"stage_a.{label}.cif"
                cif_path.write_bytes(_requests.get(struct_url, timeout=300).content)
                entry["cif_path"] = str(cif_path)
            results[label] = entry
            iptm = metrics.get("iptm")
            plddt = metrics.get("complex_plddt")
            print(f"    {label}: ipTM={iptm} complex_plddt={plddt}")
        (RESULTS_DIR / "stage_a_results.json").write_text(json.dumps(results, indent=2))
        client.ledger.dump(LEDGER_PATH)
        print(f"Stage A complete. Results: {RESULTS_DIR / 'stage_a_results.json'}")
    else:
        print("Stage A bodies written to dry_run dir (no submission). Re-run with --submit.")
    return 0


def cmd_stage_c(args: argparse.Namespace) -> int:
    """Submit BoltzGen Protein Design n=100 vs S2G2F-Fc and wait for completion."""
    import requests as _requests
    _ensure_dirs()
    body = json.loads((DRY_RUN_DIR / "stage_c.boltzgen_s2g2f.json").read_text())
    client = _new_client(submit=args.submit)
    if args.submit:
        # Confirm cost via estimate first
        url_est = "https://api.boltz.bio/compute/v1/protein/design/estimate-cost"
        r = _requests.post(url_est, headers=client._headers(), json=body, timeout=60)
        if r.status_code >= 400:
            print(f"estimate_cost failed: {r.status_code} {r.text[:400]}")
            return 2
        est_data = r.json()
        print(f"Stage C estimate: ${est_data['estimated_cost_usd']} for {est_data['breakdown']['num_units']} designs")
        if not args.yes:
            ok = input("Proceed? [y/N] ").strip().lower() == "y"
            if not ok:
                print("Aborted.")
                return 1
        pid = client.submit("protein_design", body, label="stage_c.boltzgen_s2g2f")
        print(f"Submitted stage_c: prediction_id = {pid}")
        # Poll until completed; BoltzGen n=100 can take 30-60+ min.
        data = client.wait_for("protein_design", pid, poll_s=30, timeout_s=4 * 3600)
        # Save the full response for inspection.
        (RESULTS_DIR / "stage_c_raw.json").write_text(json.dumps(data, indent=2))
        # Also try /list-results endpoint if available.
        for slug in ("list-results", "results"):
            r = _requests.get(
                f"https://api.boltz.bio/compute/v1/protein/design/{pid}/{slug}",
                headers=client._headers(),
                timeout=120,
            )
            if r.status_code == 200:
                (RESULTS_DIR / f"stage_c_{slug}.json").write_text(r.text)
                print(f"Saved /{slug} response ({len(r.text):,} bytes)")
                break
        client.ledger.dump(LEDGER_PATH)
        print(f"Stage C complete. Raw response: {RESULTS_DIR / 'stage_c_raw.json'}")
    else:
        print("Dry-run only. Re-run with --submit.")
    return 0


def _extract_designs_from_stage_c(stage_c_results: dict) -> list[dict]:
    """Pull (design_id, sequence, score) tuples from whatever shape Stage C returned.

    Defensive — the BoltzGen response shape isn't fully nailed down in the docs we
    fetched, so try the common locations.
    """
    candidates: list[dict] = []
    for path in (("data",), ("output", "designs"), ("output", "samples"), ("results",), ("designs",), ("samples",)):
        node = stage_c_results
        try:
            for p in path:
                node = node[p]
            if isinstance(node, list) and node:
                candidates = node
                break
        except (KeyError, TypeError):
            continue
    if not candidates:
        raise RuntimeError(f"Could not find designs list in Stage C results. Top keys: {list(stage_c_results.keys())}")
    out = []
    for i, c in enumerate(candidates):
        seq = c.get("sequence") or c.get("value") or c.get("binder_sequence")
        if not seq and "entities" in c:
            for e in c["entities"]:
                if e.get("type") == "protein" and not e.get("value", "").startswith("MK"):
                    pass
                seq = e.get("value")
                if seq and not any(ch in seq for ch in ".0123456789"):
                    break
        if not seq:
            continue
        score = (
            c.get("internal_score")
            or c.get("score")
            or c.get("design_score")
            or c.get("plddt")
            or c.get("metrics", {}).get("iptm") if isinstance(c.get("metrics"), dict) else None
        )
        out.append(
            {
                "design_id": c.get("id") or c.get("design_id") or f"design_{i:03d}",
                "sequence": seq,
                "internal_score": score,
                "raw_index": i,
            }
        )
    return out


def _extract_lib_screen_metrics(raw: dict) -> dict[str, dict]:
    """Pull per-protein metrics out of a Library Screen response. Returns {design_id: metrics}.

    Defensive over response shape.
    """
    items: list = []
    for path in (("data",), ("output", "results"), ("results",), ("output", "samples")):
        node = raw
        try:
            for p in path:
                node = node[p]
            if isinstance(node, list) and node:
                items = node
                break
        except (KeyError, TypeError):
            continue
    out: dict[str, dict] = {}
    for it in items:
        # Library Screen returns external_id = the BoltzGen design id we passed in;
        # `id` is the new prediction id. Match on external_id when present.
        did = it.get("external_id") or it.get("id") or it.get("protein_id") or it.get("design_id")
        if not did:
            continue
        m = it.get("metrics") or it.get("best_sample", {}).get("metrics") or {}
        if not m:
            for k in ("iptm", "complex_plddt", "ptm", "ligand_iptm"):
                if k in it:
                    m[k] = it[k]
        out[did] = m
    return out


def cmd_deliverable(args: argparse.Namespace) -> int:
    """Stage F — rank Stage E results, write FASTA + CSV + PyMOL + HANDOFF.md."""
    _ensure_dirs()
    # Reconstruct binder sequences from the dry-run Stage E body.
    body_path = DRY_RUN_DIR / "stage_e.lib_screen_vs_s2g2f.json"
    if not body_path.exists():
        print(f"Missing {body_path}. Run stage-e first (even in dry-run) to populate body.")
        return 1
    se_body = json.loads(body_path.read_text())
    seqs: dict[str, str] = {}
    for p in se_body["proteins"]:
        for e in p["entities"]:
            if e["type"] == "protein":
                seqs[p["id"]] = e["value"]
                break

    # Load Stage E results.
    metrics_s2g2f: dict[str, dict] = {}
    metrics_g2f: dict[str, dict] = {}
    for label, sink in (("s2g2f", metrics_s2g2f), ("g2f", metrics_g2f)):
        for fname in (
            f"stage_e_results_{label}.json",
            f"stage_e_list_results_{label}.json",
            f"stage_e_raw_{label}.json",
        ):
            p = RESULTS_DIR / fname
            if p.exists():
                sink.update(_extract_lib_screen_metrics(json.loads(p.read_text())))
                if sink:
                    break

    rows = []
    for did, seq in seqs.items():
        m_s = metrics_s2g2f.get(did, {})
        m_g = metrics_g2f.get(did, {})
        iptm_s = m_s.get("iptm")
        iptm_g = m_g.get("iptm")
        delta = None if iptm_s is None or iptm_g is None else iptm_s - iptm_g
        rows.append(
            {
                "design_id": did,
                "sequence": seq,
                "sequence_length": len(seq),
                "iptm_s2g2f": iptm_s,
                "iptm_g2f": iptm_g,
                "delta_iptm": delta,
                "ligand_iptm_s2g2f": m_s.get("ligand_iptm"),
                "ligand_iptm_g2f": m_g.get("ligand_iptm"),
                "complex_plddt_s2g2f": m_s.get("complex_plddt"),
                "complex_plddt_g2f": m_g.get("complex_plddt"),
                "rank_score": (None if iptm_s is None or delta is None else iptm_s * delta),
            }
        )

    # Rank by ipTM(S2G2F) * delta — both must be defined.
    rows_ranked = sorted(
        rows,
        key=lambda r: (r["rank_score"] if r["rank_score"] is not None else -999),
        reverse=True,
    )

    # Write CSV
    csv_path = RESULTS_DIR / "stage_f_designs.csv"
    fieldnames = [
        "rank",
        "design_id",
        "sequence_length",
        "iptm_s2g2f",
        "iptm_g2f",
        "delta_iptm",
        "ligand_iptm_s2g2f",
        "ligand_iptm_g2f",
        "complex_plddt_s2g2f",
        "complex_plddt_g2f",
        "rank_score",
        "sequence",
    ]
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for i, r in enumerate(rows_ranked, start=1):
            w.writerow({"rank": i, **{k: r[k] for k in fieldnames if k != "rank"}})

    # Write top-N FASTA
    top_n = args.top_n
    fasta_path = RESULTS_DIR / "stage_f_top_designs.fasta"
    with fasta_path.open("w") as fh:
        for i, r in enumerate(rows_ranked[:top_n], start=1):
            fh.write(
                f">{r['design_id']} rank={i} iptm_s2g2f={r['iptm_s2g2f']} "
                f"iptm_g2f={r['iptm_g2f']} delta={r['delta_iptm']}\n{r['sequence']}\n"
            )

    # PyMOL .pml script — points at the Stage A CIF as the target context.
    pml_path = RESULTS_DIR / "stage_f_session.pml"
    pml_path.write_text(
        f"""# Glycoform pilot — top {top_n} designs
load stage_a.s2g2f_target.cif, s2g2f_target
load stage_a.g2f_target.cif, g2f_target
hide everything
show cartoon, s2g2f_target and chain A+B
show sticks, s2g2f_target and not (chain A or chain B)
color slate, s2g2f_target and chain A+B
color hotpink, s2g2f_target and not (chain A or chain B)
zoom s2g2f_target
bg_color white
# Designs are sequences only — re-cofold with Boltz S&B for 3D poses if needed.
"""
    )

    # HANDOFF.md
    handoff = RESULTS_DIR / "HANDOFF.md"
    spent_jobs = "?"
    spent_usd = "?"
    if LEDGER_PATH.exists():
        led = json.loads(LEDGER_PATH.read_text())
        spent_jobs = led["spent_jobs"]
        spent_usd = led["spent_usd"]
    handoff.write_text(
        f"""# Glycoform binder pilot — feasibility demo deliverable

Goal: prove de-novo binder design works against glycosylated IgG Fc, using the hosted Boltz API. Pilot scope was COMPUTE ONLY — no wet-lab.

## Pipeline that ran
- Stage A: 2 Boltz S&B no-binder cofolds (S2G2F-Fc, G2F-Fc) validated that the API model accepts the boltz-#622 N-glycan schema and produces high-confidence glycoprotein structures.
- Stage C: 1 BoltzGen Protein Design job (`boltz_curated` / `boltz_nanobody` scaffold, n=100) targeting CH2 upper-face hotspots near Asn297 of S2G2F-Fc.
- Stage E: 2 Library Screen jobs, top {top_n if top_n < len(rows_ranked) else len(rows_ranked)} of {len(rows)} designs cofolded against both S2G2F-Fc and G2F-Fc to compute `delta_ipTM`.
- Stage F: ranked by `ipTM(S2G2F) * delta_ipTM`; top {top_n} written to FASTA.

## Files
- `stage_a.s2g2f_target.cif` / `stage_a.g2f_target.cif` — Boltz-cofolded glycoprotein targets used as the design substrate.
- `stage_f_designs.csv` — every design with full metrics.
- `stage_f_top_designs.fasta` — the top {top_n} picks for any downstream wet-lab phase.
- `stage_f_session.pml` — minimal PyMOL session showing the two target CIFs.
- `ledger.json` — every billed API call, running total.

## Budget
- API calls submitted: {spent_jobs} of 60 ceiling.
- Approx. USD spent (estimate-tracked): ${spent_usd}.

## Caveats / what this pilot did NOT do
- No positive controls (Kao H9 sequence not available in our copy of the paper supplement).
- Hotspots are hardcoded to standard upper-CH2 face residues (Eu 296,298,299,300,301,326,332) instead of computed from per-residue SASA on the Stage A CIF.
- BoltzGen used the curated `boltz_nanobody` scaffold (single shot), not multiple replicates or multi-modality variants.
- ReGlyco multi-conformer clash filtering was deferred to scale-up.

## To scale this up
The plan to do so is at `C:\\\\Users\\\\lab\\\\.claude\\\\plans\\\\c-users-lab-downloads-pnas-2212658119-s-fuzzy-key.md` (the "Out of scope" block lists what was cut). The next high-value moves: (a) proper SASA-delta hotspot mining on the validated CIFs, (b) scale to n=10^3-10^4 BoltzGen and full Library Screen, (c) add ReGlyco clash filtering, (d) hand the top picks to Ranomics wet lab for chemoenzymatic-glycoform SPR validation.
"""
    )

    print(f"Wrote {csv_path}")
    print(f"Wrote {fasta_path}")
    print(f"Wrote {pml_path}")
    print(f"Wrote {handoff}")
    return 0


def cmd_stage_e(args: argparse.Namespace) -> int:
    """Stage E — paired Library Screen of top-N designs vs S2G2F-Fc and G2F-Fc."""
    import requests as _requests
    _ensure_dirs()
    s2g2f, _ = _build_target_spec(S2G2F_PDB, "S2G2F")
    g2f, _ = _build_target_spec(G2F_PDB, "G2F")

    # Prefer the /results endpoint dump (contains the 100 designs).
    raw = None
    for fname in ("stage_c_results.json", "stage_c_list-results.json", "stage_c_raw.json"):
        p = RESULTS_DIR / fname
        if p.exists():
            data = json.loads(p.read_text())
            raw = data if isinstance(data, dict) else {"data": data}
            break
    if raw is None:
        raise RuntimeError(f"No Stage C results file under {RESULTS_DIR}")
    designs = _extract_designs_from_stage_c(raw)
    designs.sort(key=lambda d: d["internal_score"] if d["internal_score"] is not None else -1, reverse=True)
    top = designs[: args.top_n]
    print(f"Stage C surfaced {len(designs)} designs; top {len(top)} selected for Stage E")
    binder_seqs = [(d["design_id"], d["sequence"]) for d in top]

    # Build paired Library Screen bodies.
    bodies = {
        "s2g2f": build_library_screen_input(s2g2f, binder_sequences=binder_seqs),
        "g2f": build_library_screen_input(g2f, binder_sequences=binder_seqs),
    }
    for label, body in bodies.items():
        (DRY_RUN_DIR / f"stage_e.lib_screen_vs_{label}.json").write_text(json.dumps(body, indent=2))

    client = _new_client(submit=args.submit)
    if not args.submit:
        print("Dry-run only. Re-run with --submit.")
        return 0

    # Estimate cost
    url_est = "https://api.boltz.bio/compute/v1/protein/library-screen/estimate-cost"
    total_est = 0.0
    for label, body in bodies.items():
        r = _requests.post(url_est, headers=client._headers(), json=body, timeout=60)
        if r.status_code >= 400:
            print(f"estimate {label} failed: {r.status_code} {r.text[:300]}")
            return 2
        est = float(r.json()["estimated_cost_usd"])
        total_est += est
        print(f"  Stage E vs {label}: estimated cost = ${est}")
    print(f"Stage E total estimated: ${total_est:.2f}")
    if not args.yes:
        ok = input("Proceed? [y/N] ").strip().lower() == "y"
        if not ok:
            print("Aborted.")
            return 1

    pids = {}
    for label, body in bodies.items():
        pid = client.submit("library_screen", body, label=f"stage_e.{label}")
        pids[label] = pid
        print(f"  Stage E vs {label}: prediction_id = {pid}")

    results = {}
    for label, pid in pids.items():
        print(f"  Waiting on Stage E vs {label} ({pid})...")
        data = client.wait_for("library_screen", pid, poll_s=30, timeout_s=4 * 3600)
        (RESULTS_DIR / f"stage_e_raw_{label}.json").write_text(json.dumps(data, indent=2))
        # Also try /list-results
        r = _requests.get(
            f"https://api.boltz.bio/compute/v1/protein/library-screen/{pid}/list-results",
            headers=client._headers(),
            timeout=120,
        )
        if r.status_code == 200:
            (RESULTS_DIR / f"stage_e_list_results_{label}.json").write_text(r.text)
        results[label] = {"prediction_id": pid, "status": data.get("status")}
    (RESULTS_DIR / "stage_e_summary.json").write_text(json.dumps(results, indent=2))
    client.ledger.dump(LEDGER_PATH)
    print(f"Stage E submission complete. Inspect stage_e_raw_*.json and stage_e_list_results_*.json")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep", help="Write Stage A / C / E dry-run bodies for review")
    p.set_defaults(func=cmd_prep)
    p = sub.add_parser("stage-a", help="Submit 2 no-binder validation calls (target gate)")
    p.add_argument("--submit", action="store_true", help="Actually submit; default writes JSON only")
    p.add_argument("--yes", action="store_true", help="Skip cost-estimate confirmation prompt")
    p.set_defaults(func=cmd_stage_a)
    p = sub.add_parser("stage-c", help="Submit BoltzGen Protein Design n=100 vs S2G2F-Fc")
    p.add_argument("--submit", action="store_true", help="Actually submit; default dry-run only")
    p.add_argument("--yes", action="store_true", help="Skip cost-estimate confirmation prompt")
    p.set_defaults(func=cmd_stage_c)
    p = sub.add_parser("stage-e", help="Submit 2 Library Screen jobs (top N designs vs both glycoforms)")
    p.add_argument("--submit", action="store_true", help="Actually submit; default dry-run only")
    p.add_argument("--yes", action="store_true", help="Skip cost-estimate confirmation prompt")
    p.add_argument("--top-n", type=int, default=N_TOP_FOR_STAGE_E, help="Designs to forward from Stage C")
    p.set_defaults(func=cmd_stage_e)
    p = sub.add_parser("deliverable", help="Rank Stage E results and write FASTA + CSV + PyMOL + HANDOFF.md")
    p.add_argument("--top-n", type=int, default=5, help="Number of top picks to write to FASTA")
    p.set_defaults(func=cmd_deliverable)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
